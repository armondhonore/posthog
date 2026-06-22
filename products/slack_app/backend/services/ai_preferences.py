"""Per-(workspace, Slack user) AI preferences for task-run sandboxes triggered
from Slack.

Field names mirror the task-run request serializer
(`products/tasks/backend/presentation/serializers.py`) so the resolver output
can be handed to the task layer with zero translation.

Resolution rule: `(runtime_adapter, model)` moves as an atomic pair — a row
that only sets one half is invalid and rejected at write time, so half-set
state never reaches the resolver. Falls back row-by-row: user override →
workspace default → unset. `reasoning_effort` falls back independently but is
only kept if the resolved model actually supports it; otherwise it is dropped
to avoid a stale effort silently sticking after the model changes.

Unset fields stay `None` — the task layer applies its own defaults. We do not
duplicate task defaults here to avoid divergence.

The whole feature is gated by `SLACK_APP_HOME_FLAG` because the surfaces that
write these preferences (the App Home tab and supporting events) require Slack
app manifest changes that roll out separately. When the flag is off the
resolver short-circuits to an empty preferences object, preserving today's
behaviour for workspaces that haven't opted in.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.db.models import Q

import posthoganalytics

from posthog.utils import get_instance_region

if TYPE_CHECKING:
    from posthog.models.integration import Integration

    from products.slack_app.backend.models import SlackSettings

logger = logging.getLogger(__name__)

SLACK_APP_HOME_FLAG = "slack-app-home"


@dataclass(frozen=True)
class AIPreferences:
    """Resolved AI preferences for a single (workspace, slack_user_id) lookup.

    Field names match the task-run request serializer so callers can splat this
    straight into the task creation payload.
    """

    runtime_adapter: str | None = None
    model: str | None = None
    reasoning_effort: str | None = None

    @property
    def is_empty(self) -> bool:
        return self.runtime_adapter is None and self.model is None and self.reasoning_effort is None


_EMPTY = AIPreferences()


def resolve_ai_preferences(integration: Integration, slack_user_id: str | None) -> AIPreferences:
    """Resolve the effective AI preferences for a Slack user in a workspace.

    The integration's `integration_id` is the Slack workspace id (team id). The
    workspace default row has `slack_user_id IS NULL`; a personal override has
    `slack_user_id` set. The user row, if present, wins over the workspace row
    per-field-group; `(runtime_adapter, model)` moves as a pair.
    """

    if not _feature_enabled(integration):
        return _EMPTY

    from products.slack_app.backend.models import SlackSettings

    slack_workspace_id = integration.integration_id
    # SQL `IN (..., NULL)` does not match NULL rows, so the workspace-wide row
    # (slack_user_id IS NULL) needs its own arm in the filter.
    user_row_filter = Q(slack_user_id=slack_user_id) if slack_user_id else Q(pk__in=[])
    rows = list(
        SlackSettings.objects.filter(
            Q(slack_workspace_id=slack_workspace_id) & (Q(slack_user_id__isnull=True) | user_row_filter)
        )
    )
    user_row = next((r for r in rows if slack_user_id is not None and r.slack_user_id == slack_user_id), None)
    workspace_row = next((r for r in rows if r.slack_user_id is None), None)

    runtime_adapter, model = _first_complete_pair(
        _pair(user_row),
        _pair(workspace_row),
    )

    reasoning_effort: str | None = None
    if runtime_adapter is not None and model is not None:
        reasoning_effort = _pick_reasoning_effort(user_row, workspace_row, runtime_adapter, model)

    return AIPreferences(
        runtime_adapter=runtime_adapter,
        model=model,
        reasoning_effort=reasoning_effort,
    )


def _pair(row: SlackSettings | None) -> tuple[str | None, str | None]:
    if row is None:
        return (None, None)
    return (row.ai_runtime_adapter, row.ai_model)


def _first_complete_pair(*pairs: tuple[str | None, str | None]) -> tuple[str | None, str | None]:
    """Return the first `(runtime_adapter, model)` pair where both are set.

    Half-set rows are skipped entirely — never mix runtime_adapter from one row
    with model from another, since the pair is a tightly-coupled unit.
    """
    for adapter, model in pairs:
        if adapter and model:
            return (adapter, model)
    return (None, None)


def _pick_reasoning_effort(
    user_row: SlackSettings | None,
    workspace_row: SlackSettings | None,
    runtime_adapter: str,
    model: str,
) -> str | None:
    """Pick the most specific reasoning_effort that the resolved model actually supports.

    If the stored effort is not supported by the resolved model (e.g. the user
    saved `high` while on a thinking model and then switched to a non-thinking
    one) it is silently dropped rather than passed through and rejected later.
    """
    from products.tasks.backend.facade.run_config import get_supported_reasoning_efforts

    supported = {e.value for e in get_supported_reasoning_efforts(runtime_adapter, model)}
    if not supported:
        return None

    for row in (user_row, workspace_row):
        if row is None:
            continue
        effort = row.ai_reasoning_effort
        if effort and effort in supported:
            return effort
    return None


def _feature_enabled(integration: Integration) -> bool:
    """Fail-closed feature flag check, keyed on Slack workspace + PostHog org.

    Mirrors the existing slack_app flag evaluation pattern (see
    `_assistant_enabled` / `_untagged_thread_followups_enabled` in
    `products/slack_app/backend/api.py`). Fails closed because a transient
    PostHog API outage must not silently enable the feature for everyone.
    """
    try:
        return bool(
            posthoganalytics.feature_enabled(
                SLACK_APP_HOME_FLAG,
                f"slack_workspace:{integration.integration_id}",
                groups={"organization": str(integration.team.organization_id)},
                person_properties={"region": get_instance_region() or "unknown"},
                only_evaluate_locally=False,
                send_feature_flag_events=False,
            )
        )
    except Exception:
        logger.exception(
            "slack_app_ai_preferences_feature_flag_check_failed",
            extra={"integration_id": integration.id},
        )
        return False


def validate_ai_preferences(
    runtime_adapter: str | None,
    model: str | None,
    reasoning_effort: str | None,
) -> None:
    """Validate the `(runtime_adapter, model, reasoning_effort)` triple.

    Raises `django.core.exceptions.ValidationError` if the triple is internally
    inconsistent. Call this from the write path (serializer / view /
    `SlackSettings.clean()` if added) so half-set rows never reach the DB.
    """
    from django.core.exceptions import ValidationError

    from products.tasks.backend.facade.run_config import (
        PUBLIC_REASONING_EFFORTS,
        RuntimeAdapter,
        get_reasoning_effort_error,
    )

    if (runtime_adapter is None) != (model is None):
        raise ValidationError(
            "runtime_adapter and model must be set together — set both to override the default, or both to null to inherit."
        )

    if runtime_adapter is not None:
        valid_adapters = {a.value for a in RuntimeAdapter}
        if runtime_adapter not in valid_adapters:
            raise ValidationError(
                f"Unknown runtime_adapter '{runtime_adapter}'. Valid: {', '.join(sorted(valid_adapters))}."
            )

    if reasoning_effort is not None:
        valid_efforts = {e.value for e in PUBLIC_REASONING_EFFORTS}
        if reasoning_effort not in valid_efforts:
            raise ValidationError(
                f"Unknown reasoning_effort '{reasoning_effort}'. Valid: {', '.join(sorted(valid_efforts))}."
            )

    error = get_reasoning_effort_error(runtime_adapter, model, reasoning_effort)
    if error:
        raise ValidationError(error)
