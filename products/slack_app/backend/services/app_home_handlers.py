"""Event + interactivity handlers for the App Home tab and AI preferences modal.

Lives in its own module so `api.py` stays manageable. Public entry points are
re-exported from `api.py` under matching `_handle_*` names so the dispatchers
there can call them with minimal extra wiring.

Concurrency model: each Slack interactivity request is short-lived (<3s SLA),
so all writes use plain Django ORM calls inside the request thread. The
resolver is read at task-creation time inside the Temporal workflow, not here.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.core.exceptions import ValidationError
from django.http import HttpResponse, JsonResponse

from posthog.models.integration import Integration, SlackIntegration

from products.slack_app.backend.services.ai_preferences import (
    SLACK_APP_HOME_FLAG,
    AIPreferences,
    resolve_ai_preferences,
    validate_ai_preferences,
)
from products.slack_app.backend.services.app_home import (
    ACTION_EDIT_PERSONAL,
    ACTION_EDIT_WORKSPACE,
    ACTION_RESET_PERSONAL,
    EDIT_MODAL_PERSONAL_CALLBACK_ID,
    EDIT_MODAL_WORKSPACE_CALLBACK_ID,
    MODAL_ACTION_MODEL,
    MODAL_ACTION_RUNTIME_ADAPTER,
    parse_modal_submission,
    render_edit_modal,
    render_home_view,
)
from products.slack_app.backend.services.slack_user_info import is_slack_workspace_admin

if TYPE_CHECKING:
    from products.slack_app.backend.models import SlackSettings

logger = logging.getLogger(__name__)


# Re-exported so api.py's dispatch table can reference these by alias.
EDIT_PERSONAL = ACTION_EDIT_PERSONAL
EDIT_WORKSPACE = ACTION_EDIT_WORKSPACE
RESET_PERSONAL = ACTION_RESET_PERSONAL
MODAL_RUNTIME_ADAPTER = MODAL_ACTION_RUNTIME_ADAPTER
MODAL_MODEL = MODAL_ACTION_MODEL


def handle_app_home_opened(event: dict, slack_team_id: str) -> None:
    """Publish the Home tab for the user who just opened it.

    No-op when the slack-app-home flag is off — that way installs without the
    manifest changes still get a benign empty Home tab from Slack's default.
    """

    slack_user_id = event.get("user")
    if not slack_user_id:
        return

    integration = _get_slack_integration(slack_team_id)
    if integration is None:
        return

    # Flag check lives in the resolver too, but checking here avoids any work
    # at all (including DB reads for the SlackSettings rows) on installs that
    # haven't opted in. Same fail-closed semantics as the resolver — see
    # `_feature_enabled` in services/ai_preferences.py.
    effective = resolve_ai_preferences(integration, slack_user_id)
    user_row, workspace_row = _load_rows(integration, slack_user_id)

    slack = SlackIntegration(integration)
    is_admin = _is_admin(slack, integration, slack_user_id)

    view = render_home_view(
        effective=effective,
        user_row=user_row,
        workspace_row=workspace_row,
        is_admin=is_admin,
    )
    try:
        slack.client.views_publish(user_id=slack_user_id, view=view)
    except Exception:
        logger.exception(
            "slack_app_home_publish_failed",
            extra={"slack_user_id": slack_user_id, "slack_team_id": slack_team_id},
        )


def handle_ai_prefs_block_action(payload: dict, action: dict) -> HttpResponse:
    """Dispatch a `block_actions` payload originating from the Home tab or modal."""

    action_id = action.get("action_id")
    slack_team_id = (payload.get("team") or {}).get("id", "")
    slack_user_id = (payload.get("user") or {}).get("id", "")
    trigger_id = payload.get("trigger_id")

    integration = _get_slack_integration(slack_team_id)
    if integration is None:
        return HttpResponse(status=200)

    if action_id == ACTION_EDIT_PERSONAL and trigger_id:
        _open_edit_modal(integration, slack_user_id, scope="personal", trigger_id=trigger_id)
        return HttpResponse(status=200)

    if action_id == ACTION_EDIT_WORKSPACE and trigger_id:
        slack = SlackIntegration(integration)
        if not _is_admin(slack, integration, slack_user_id):
            _post_ephemeral_admin_only(slack, payload)
            return HttpResponse(status=200)
        _open_edit_modal(integration, slack_user_id, scope="workspace", trigger_id=trigger_id)
        return HttpResponse(status=200)

    if action_id == ACTION_RESET_PERSONAL:
        _clear_personal_override(integration, slack_user_id)
        _republish_home(integration, slack_user_id)
        return HttpResponse(status=200)

    if action_id in (MODAL_ACTION_RUNTIME_ADAPTER, MODAL_ACTION_MODEL):
        # Modal re-render: a runtime / model change updates which downstream
        # blocks (model list, effort options) are valid. Push an updated view.
        return _update_modal_after_input_change(payload)

    return HttpResponse(status=200)


def handle_app_home_view_submission(payload: dict) -> HttpResponse:
    """Handle the Save click on the personal or workspace edit modal."""

    view = payload.get("view", {})
    callback_id = view.get("callback_id")
    if callback_id not in (EDIT_MODAL_PERSONAL_CALLBACK_ID, EDIT_MODAL_WORKSPACE_CALLBACK_ID):
        return HttpResponse(status=200)

    slack_team_id = (payload.get("team") or {}).get("id", "")
    slack_user_id = (payload.get("user") or {}).get("id", "")

    integration = _get_slack_integration(slack_team_id)
    if integration is None:
        return _modal_error_response("This Slack workspace is no longer connected to PostHog.")

    runtime_adapter, model, reasoning_effort = parse_modal_submission(view)

    try:
        validate_ai_preferences(runtime_adapter, model, reasoning_effort)
    except ValidationError as exc:
        return _modal_error_response(_first_validation_message(exc))

    if callback_id == EDIT_MODAL_PERSONAL_CALLBACK_ID:
        _write_row(
            integration,
            slack_user_id=slack_user_id,
            runtime_adapter=runtime_adapter,
            model=model,
            reasoning_effort=reasoning_effort,
        )
    else:
        slack = SlackIntegration(integration)
        if not _is_admin(slack, integration, slack_user_id):
            return _modal_error_response("Only Slack workspace admins can change the workspace default.")
        _write_row(
            integration,
            slack_user_id=None,
            runtime_adapter=runtime_adapter,
            model=model,
            reasoning_effort=reasoning_effort,
        )

    _republish_home(integration, slack_user_id)
    return JsonResponse({"response_action": "clear"})


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _get_slack_integration(slack_team_id: str) -> Integration | None:
    if not slack_team_id:
        return None
    return (
        Integration.objects.select_related("team", "team__organization")
        .filter(
            kind="slack",
            integration_id=slack_team_id,
        )
        .first()
    )


def _load_rows(integration: Integration, slack_user_id: str) -> tuple[SlackSettings | None, SlackSettings | None]:
    from products.slack_app.backend.models import SlackSettings

    user_row = SlackSettings.objects.filter(
        slack_workspace_id=integration.integration_id,
        slack_user_id=slack_user_id,
    ).first()
    workspace_row = SlackSettings.objects.filter(
        slack_workspace_id=integration.integration_id,
        slack_user_id__isnull=True,
    ).first()
    return user_row, workspace_row


def _row_to_prefs(row: SlackSettings | None) -> AIPreferences:
    if row is None:
        return AIPreferences()
    return AIPreferences(
        runtime_adapter=row.ai_runtime_adapter,
        model=row.ai_model,
        reasoning_effort=row.ai_reasoning_effort,
    )


def _is_admin(slack: SlackIntegration, integration: Integration, slack_user_id: str) -> bool:
    try:
        return is_slack_workspace_admin(slack, integration, slack_user_id)
    except Exception:
        logger.exception(
            "slack_app_home_is_admin_check_failed",
            extra={"slack_user_id": slack_user_id, "integration_id": integration.id},
        )
        return False


def _open_edit_modal(
    integration: Integration,
    slack_user_id: str,
    *,
    scope: str,
    trigger_id: str,
) -> None:
    user_row, workspace_row = _load_rows(integration, slack_user_id)
    current = _row_to_prefs(user_row if scope == "personal" else workspace_row)
    supported = _supported_efforts(current.runtime_adapter, current.model)
    view = render_edit_modal(scope=scope, current=current, supported_efforts=supported)
    slack = SlackIntegration(integration)
    try:
        slack.client.views_open(trigger_id=trigger_id, view=view)
    except Exception:
        logger.exception(
            "slack_app_home_open_modal_failed",
            extra={"slack_user_id": slack_user_id, "scope": scope},
        )


def _update_modal_after_input_change(payload: dict) -> HttpResponse:
    """Re-render the modal in response to a runtime_adapter or model change.

    Reads the in-flight state from `payload["view"]`, derives the new supported
    efforts (changes when the model changes), and pushes the updated view via
    `views.update`. We don't persist anything here — the user still has to
    Save to commit.
    """

    view = payload.get("view", {})
    callback_id = view.get("callback_id")
    if callback_id not in (EDIT_MODAL_PERSONAL_CALLBACK_ID, EDIT_MODAL_WORKSPACE_CALLBACK_ID):
        return HttpResponse(status=200)

    runtime_adapter, model, reasoning_effort = parse_modal_submission(view)
    current = AIPreferences(runtime_adapter=runtime_adapter, model=model, reasoning_effort=reasoning_effort)
    supported = _supported_efforts(runtime_adapter, model)

    scope = "personal" if callback_id == EDIT_MODAL_PERSONAL_CALLBACK_ID else "workspace"
    updated_view = render_edit_modal(scope=scope, current=current, supported_efforts=supported)

    slack_team_id = (payload.get("team") or {}).get("id", "")
    integration = _get_slack_integration(slack_team_id)
    if integration is None:
        return HttpResponse(status=200)

    slack = SlackIntegration(integration)
    try:
        slack.client.views_update(view_id=view.get("id"), hash=view.get("hash"), view=updated_view)
    except Exception:
        logger.exception("slack_app_home_modal_update_failed")
    return HttpResponse(status=200)


def _supported_efforts(runtime_adapter: str | None, model: str | None) -> list[str] | None:
    if not runtime_adapter or not model:
        return None
    from products.tasks.backend.temporal.process_task.utils import get_supported_reasoning_efforts

    return [e.value for e in get_supported_reasoning_efforts(runtime_adapter, model)] or None


def _write_row(
    integration: Integration,
    *,
    slack_user_id: str | None,
    runtime_adapter: str | None,
    model: str | None,
    reasoning_effort: str | None,
) -> None:
    """Upsert a SlackSettings row with the given AI preferences.

    `default_integration` is required by the existing schema; we point it at
    this integration so a fresh AI-preferences-only write still produces a
    coherent row (it doubles as the routing default if no other row exists).
    Existing rows have their AI fields updated in-place.
    """
    from products.slack_app.backend.models import SlackSettings

    SlackSettings.objects.update_or_create(
        slack_workspace_id=integration.integration_id,
        slack_user_id=slack_user_id,
        defaults={
            "default_integration": integration,
            "ai_runtime_adapter": runtime_adapter,
            "ai_model": model,
            "ai_reasoning_effort": reasoning_effort,
        },
    )


def _clear_personal_override(integration: Integration, slack_user_id: str) -> None:
    """Clear just the AI fields on the user's row. Leaves routing alone."""
    from products.slack_app.backend.models import SlackSettings

    SlackSettings.objects.filter(
        slack_workspace_id=integration.integration_id,
        slack_user_id=slack_user_id,
    ).update(
        ai_runtime_adapter=None,
        ai_model=None,
        ai_reasoning_effort=None,
    )


def _republish_home(integration: Integration, slack_user_id: str) -> None:
    user_row, workspace_row = _load_rows(integration, slack_user_id)
    effective = resolve_ai_preferences(integration, slack_user_id)
    slack = SlackIntegration(integration)
    is_admin = _is_admin(slack, integration, slack_user_id)
    view = render_home_view(
        effective=effective,
        user_row=user_row,
        workspace_row=workspace_row,
        is_admin=is_admin,
    )
    try:
        slack.client.views_publish(user_id=slack_user_id, view=view)
    except Exception:
        logger.exception("slack_app_home_republish_failed")


def _modal_error_response(message: str) -> JsonResponse:
    """Slack-format response: keep the modal open and surface an error.

    Slack expects `response_action=errors` with a `block_id`-keyed errors map.
    We attach the error to the runtime block so it's visible without scrolling.
    """
    from products.slack_app.backend.services.app_home import MODAL_BLOCK_RUNTIME_ADAPTER

    return JsonResponse(
        {
            "response_action": "errors",
            "errors": {MODAL_BLOCK_RUNTIME_ADAPTER: message[:200]},
        }
    )


def _first_validation_message(exc: ValidationError) -> str:
    if exc.messages:
        return exc.messages[0]
    return "Settings could not be saved."


def _post_ephemeral_admin_only(slack: SlackIntegration, payload: dict) -> None:
    """Tell a non-admin that workspace edits are gated.

    The Home tab Edit button is already rendered admin-only, so reaching this
    path means the user came in via a stale view or a hand-crafted payload.
    """
    channel = (payload.get("channel") or {}).get("id") or (payload.get("container") or {}).get("channel_id")
    if not channel:
        return
    try:
        slack.client.chat_postEphemeral(
            channel=channel,
            user=(payload.get("user") or {}).get("id", ""),
            text="Only Slack workspace admins can change the PostHog workspace default.",
        )
    except Exception:
        logger.warning("slack_app_home_admin_only_notice_failed")


# Defensive re-exports — flag name is used by the api.py dispatchers in logs.
__all__ = [
    "EDIT_PERSONAL",
    "EDIT_WORKSPACE",
    "MODAL_MODEL",
    "MODAL_RUNTIME_ADAPTER",
    "RESET_PERSONAL",
    "SLACK_APP_HOME_FLAG",
    "handle_ai_prefs_block_action",
    "handle_app_home_opened",
    "handle_app_home_view_submission",
]
