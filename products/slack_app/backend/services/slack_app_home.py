"""App Home tab + edit modal renderers for the PostHog Slack app.

The Home tab is the user-facing control panel for the integration. For this
first iteration it carries one card — the AI preferences picker that feeds
Slack-triggered task runs — but the layout leaves room for additional cards
(notifications, account linking, activity feed) as they come online. Each card
follows the same pattern: a one-line "effective" summary, an admin-aware edit
control, and an optional explainer of where the effective value came from.

All Block Kit payloads (views, modals) are built as plain dicts here so they
can be unit-tested without any Slack client. The event/interactivity handlers
in `products/slack_app/backend/api.py` are the ones that actually call
`views.publish` / `views.open` / `views.update`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from products.slack_app.backend.services.ai_preferences import AIPreferences

if TYPE_CHECKING:
    from products.slack_app.backend.models import SlackSettings


# Block / action / callback identifiers. Centralised so the interactivity
# handler in api.py and the renderers here cannot drift apart.
HOME_CALLBACK_ID = "slack_app_home"

ACTION_EDIT_PERSONAL = "slack_app_home:edit_personal"
ACTION_EDIT_WORKSPACE = "slack_app_home:edit_workspace"
ACTION_RESET_PERSONAL = "slack_app_home:reset_personal"

EDIT_MODAL_PERSONAL_CALLBACK_ID = "slack_app_ai_prefs:personal"
EDIT_MODAL_WORKSPACE_CALLBACK_ID = "slack_app_ai_prefs:workspace"

MODAL_ACTION_RUNTIME_ADAPTER = "ai_prefs:runtime_adapter"
MODAL_ACTION_MODEL = "ai_prefs:model"
MODAL_ACTION_REASONING_EFFORT = "ai_prefs:reasoning_effort"

MODAL_BLOCK_RUNTIME_ADAPTER = "block_runtime_adapter"
MODAL_BLOCK_MODEL = "block_model"
MODAL_BLOCK_REASONING_EFFORT = "block_reasoning_effort"

EditScope = Literal["personal", "workspace"]


# Picker data (adapters, model lists, display labels) all comes from the tasks
# product via `products.tasks.backend.facade.run_config`. The imports are
# deferred to call time on purpose — at module load the slack_app api.py is
# imported by Django startup, and the facade module pulls
# `tasks.backend.temporal.process_task.utils` which transitively loads the
# tasks temporal package. In production that's fine; in tests the env config
# rejects it, and the resolver/handler tests already stub the underlying
# module. Lazy imports keep the slack_app side honest about what it actually
# needs at module-load time (nothing from the tasks runtime).


def _picker_choices() -> tuple[Any, ...]:
    from products.tasks.backend.facade.run_config import get_picker_choices

    return get_picker_choices()


def _runtime_adapter_label(value: str | None) -> str:
    if not value:
        return "—"
    from products.tasks.backend.facade.run_config import RUNTIME_ADAPTER_DISPLAY_NAMES

    return RUNTIME_ADAPTER_DISPLAY_NAMES.get(value, value)


def _reasoning_effort_label(value: str | None) -> str:
    if not value:
        return "—"
    from products.tasks.backend.facade.run_config import REASONING_EFFORT_DISPLAY_NAMES

    return REASONING_EFFORT_DISPLAY_NAMES.get(value, value)


def _model_label_lookup(model: str | None) -> str:
    if not model:
        return "—"
    from products.tasks.backend.facade.run_config import MODEL_DISPLAY_NAMES

    return MODEL_DISPLAY_NAMES.get(model, model)


def _models_for(runtime_adapter: str) -> tuple[tuple[str, str], ...]:
    """Return `(value, label)` pairs for the modal's model dropdown."""
    for adapter in _picker_choices():
        if adapter.value == runtime_adapter:
            return tuple((m.value, m.label) for m in adapter.models)
    return ()


def _runtime_adapter_options() -> tuple[tuple[str, str], ...]:
    """Return `(value, label)` pairs for the modal's runtime dropdown."""
    return tuple((a.value, a.label) for a in _picker_choices())


@dataclass(frozen=True)
class PreferenceSource:
    """Which row contributed the effective `(runtime_adapter, model)` pair.

    Used to render the "Source: …" line on the active-model card so the
    precedence (personal → workspace → unset) is visible at a glance.
    """

    label: str
    is_personal: bool
    is_workspace: bool
    is_unset: bool

    @classmethod
    def personal(cls) -> PreferenceSource:
        return cls(label="Your personal override", is_personal=True, is_workspace=False, is_unset=False)

    @classmethod
    def workspace(cls) -> PreferenceSource:
        return cls(label="Workspace default", is_personal=False, is_workspace=True, is_unset=False)

    @classmethod
    def unset(cls) -> PreferenceSource:
        return cls(label="System default", is_personal=False, is_workspace=False, is_unset=True)


def resolve_source(
    user_row: SlackSettings | None,
    workspace_row: SlackSettings | None,
) -> PreferenceSource:
    """Return where the effective pair came from.

    Mirrors the same atomic-pair rule the resolver uses: a row only "sources"
    the pair when both halves are set on it.
    """
    if user_row and user_row.ai_runtime_adapter and user_row.ai_model:
        return PreferenceSource.personal()
    if workspace_row and workspace_row.ai_runtime_adapter and workspace_row.ai_model:
        return PreferenceSource.workspace()
    return PreferenceSource.unset()


def render_home_view(
    *,
    effective: AIPreferences,
    user_row: SlackSettings | None,
    workspace_row: SlackSettings | None,
    is_admin: bool,
) -> dict:
    """Render the Block Kit payload for `views.publish` on the App Home tab."""

    source = resolve_source(user_row, workspace_row)
    blocks: list[dict] = []

    blocks.extend(_header_blocks())
    blocks.append({"type": "divider"})
    blocks.extend(_active_model_blocks(effective, source))
    blocks.append({"type": "divider"})
    blocks.extend(_personal_section_blocks(user_row))
    blocks.append({"type": "divider"})
    blocks.extend(_workspace_section_blocks(workspace_row, is_admin=is_admin))
    blocks.append({"type": "divider"})
    blocks.extend(_footer_blocks())

    return {"type": "home", "callback_id": HOME_CALLBACK_ID, "blocks": blocks}


def _header_blocks() -> list[dict]:
    return [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "PostHog · AI settings", "emoji": True},
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": "Pick the model that runs when you @PostHog from Slack. Set a personal override for yourself, or a workspace default for everyone.",
                }
            ],
        },
    ]


def _active_model_blocks(effective: AIPreferences, source: PreferenceSource) -> list[dict]:
    if effective.is_empty:
        return [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "*Active model*\n_Using PostHog's default model._",
                },
            },
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": f"Source: {source.label}"}],
            },
        ]

    runtime_label = _runtime_adapter_label(effective.runtime_adapter)
    model_label = _model_label_lookup(effective.model)
    effort_part = (
        f" · Reasoning: *{_reasoning_effort_label(effective.reasoning_effort)}*" if effective.reasoning_effort else ""
    )
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Active model*\n*{model_label}* · {runtime_label}{effort_part}",
            },
        },
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"Source: {source.label}"}],
        },
    ]


def _personal_section_blocks(user_row: SlackSettings | None) -> list[dict]:
    """Personal override card. Always editable by the user themselves."""

    has_override = bool(user_row and user_row.ai_runtime_adapter and user_row.ai_model)
    summary = _row_summary(user_row) if has_override else "_No personal override — inheriting the workspace default._"

    actions: list[dict] = [
        {
            "type": "button",
            "action_id": ACTION_EDIT_PERSONAL,
            "text": {"type": "plain_text", "text": "Edit my settings", "emoji": True},
        }
    ]
    if has_override:
        actions.append(
            {
                "type": "button",
                "action_id": ACTION_RESET_PERSONAL,
                "style": "danger",
                "text": {"type": "plain_text", "text": "Reset to workspace default", "emoji": True},
                "confirm": {
                    "title": {"type": "plain_text", "text": "Clear your override?"},
                    "text": {
                        "type": "mrkdwn",
                        "text": "You'll inherit the workspace default until you set new personal preferences.",
                    },
                    "confirm": {"type": "plain_text", "text": "Reset"},
                    "deny": {"type": "plain_text", "text": "Cancel"},
                },
            }
        )

    return [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Personal settings*\n{summary}"},
        },
        {"type": "actions", "elements": actions},
    ]


def _workspace_section_blocks(
    workspace_row: SlackSettings | None,
    *,
    is_admin: bool,
) -> list[dict]:
    """Workspace default card. Read-only for non-admins; admins see Edit."""

    has_default = bool(workspace_row and workspace_row.ai_runtime_adapter and workspace_row.ai_model)
    summary = (
        _row_summary(workspace_row)
        if has_default
        else "_No workspace default set — falling back to PostHog's system default._"
    )
    admin_note = "" if is_admin else " _Editable by Slack workspace admins only._"

    blocks: list[dict] = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Workspace default*{admin_note}\n{summary}"},
        }
    ]
    if is_admin:
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "action_id": ACTION_EDIT_WORKSPACE,
                        "text": {"type": "plain_text", "text": "Edit workspace default", "emoji": True},
                    }
                ],
            }
        )
    return blocks


def _footer_blocks() -> list[dict]:
    return [
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": "Tip: `@PostHog settings` opens this from any channel.",
                }
            ],
        }
    ]


def _row_summary(row: SlackSettings | None) -> str:
    if not row or not row.ai_runtime_adapter or not row.ai_model:
        return "_(none)_"
    parts = [
        f"*Model:* {_model_label_lookup(row.ai_model)}",
        f"*Runtime:* {_runtime_adapter_label(row.ai_runtime_adapter)}",
    ]
    if row.ai_reasoning_effort:
        parts.append(f"*Reasoning:* {_reasoning_effort_label(row.ai_reasoning_effort)}")
    return " · ".join(parts)


# ---------------------------------------------------------------------------
# Edit modal
# ---------------------------------------------------------------------------


def render_edit_modal(
    *,
    scope: EditScope,
    current: AIPreferences,
    supported_efforts: list[str] | None = None,
) -> dict:
    """Build the Block Kit modal payload for personal or workspace editing.

    `supported_efforts` lets the caller pre-compute which efforts are valid for
    the currently selected model (using
    `products.tasks.backend.temporal.process_task.utils.get_supported_reasoning_efforts`).
    When `None`, the effort block is omitted entirely; the modal re-renders via
    `block_actions` on runtime_adapter / model change to fill it in.
    """

    callback_id = EDIT_MODAL_PERSONAL_CALLBACK_ID if scope == "personal" else EDIT_MODAL_WORKSPACE_CALLBACK_ID
    title = "AI settings (personal)" if scope == "personal" else "AI settings (workspace)"

    runtime_pairs = _runtime_adapter_options()
    runtime_options = [
        {
            "text": {"type": "plain_text", "text": label, "emoji": True},
            "value": value,
        }
        for value, label in runtime_pairs
    ]
    runtime_element: dict[str, Any] = {
        "type": "static_select",
        "action_id": MODAL_ACTION_RUNTIME_ADAPTER,
        "placeholder": {"type": "plain_text", "text": "Pick a runtime"},
        "options": runtime_options,
    }
    if current.runtime_adapter and any(v == current.runtime_adapter for v, _ in runtime_pairs):
        runtime_element["initial_option"] = next(o for o in runtime_options if o["value"] == current.runtime_adapter)
    runtime_block: dict[str, Any] = {
        "type": "input",
        "block_id": MODAL_BLOCK_RUNTIME_ADAPTER,
        "label": {"type": "plain_text", "text": "Runtime"},
        "dispatch_action": True,
        "element": runtime_element,
    }

    model_block: dict[str, Any] | None = None
    if current.runtime_adapter:
        model_options = [
            {
                "text": {"type": "plain_text", "text": label, "emoji": True},
                "value": value,
            }
            for value, label in _models_for(current.runtime_adapter)
        ]
        if model_options:
            model_element: dict[str, Any] = {
                "type": "static_select",
                "action_id": MODAL_ACTION_MODEL,
                "placeholder": {"type": "plain_text", "text": "Pick a model"},
                "options": model_options,
            }
            if current.model and any(o["value"] == current.model for o in model_options):
                model_element["initial_option"] = next(o for o in model_options if o["value"] == current.model)
            model_block = {
                "type": "input",
                "block_id": MODAL_BLOCK_MODEL,
                "label": {"type": "plain_text", "text": "Model"},
                "dispatch_action": True,
                "element": model_element,
            }

    effort_block: dict[str, Any] | None = None
    if supported_efforts:
        effort_options = [
            {
                "text": {"type": "plain_text", "text": _reasoning_effort_label(v), "emoji": True},
                "value": v,
            }
            for v in supported_efforts
        ]
        effort_element: dict[str, Any] = {
            "type": "static_select",
            "action_id": MODAL_ACTION_REASONING_EFFORT,
            "placeholder": {"type": "plain_text", "text": "Pick an effort (optional)"},
            "options": effort_options,
        }
        if current.reasoning_effort and current.reasoning_effort in supported_efforts:
            effort_element["initial_option"] = next(o for o in effort_options if o["value"] == current.reasoning_effort)
        effort_block = {
            "type": "input",
            "block_id": MODAL_BLOCK_REASONING_EFFORT,
            "label": {"type": "plain_text", "text": "Reasoning effort"},
            "optional": True,
            "element": effort_element,
        }

    blocks = [
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": (
                        "Pick the runtime and model that should handle PostHog Slack requests for you."
                        if scope == "personal"
                        else "Set the default runtime and model for everyone in this Slack workspace."
                    ),
                }
            ],
        },
        runtime_block,
    ]
    if model_block:
        blocks.append(model_block)
    if effort_block:
        blocks.append(effort_block)

    return {
        "type": "modal",
        "callback_id": callback_id,
        "title": {"type": "plain_text", "text": title, "emoji": True},
        "submit": {"type": "plain_text", "text": "Save"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": blocks,
    }


def parse_modal_submission(view: dict) -> tuple[str | None, str | None, str | None]:
    """Pull `(runtime_adapter, model, reasoning_effort)` out of a Slack view_submission payload.

    Returns `(None, None, None)` for any block the user didn't fill in. The
    caller validates the triple via `validate_ai_preferences`.
    """

    state = view.get("state", {}).get("values", {})

    runtime_adapter = _selected_value(state, MODAL_BLOCK_RUNTIME_ADAPTER, MODAL_ACTION_RUNTIME_ADAPTER)
    model = _selected_value(state, MODAL_BLOCK_MODEL, MODAL_ACTION_MODEL)
    reasoning_effort = _selected_value(state, MODAL_BLOCK_REASONING_EFFORT, MODAL_ACTION_REASONING_EFFORT)
    return runtime_adapter, model, reasoning_effort


def _selected_value(state: dict, block_id: str, action_id: str) -> str | None:
    block = state.get(block_id, {})
    action = block.get(action_id, {})
    selected = action.get("selected_option")
    if isinstance(selected, dict):
        return selected.get("value")
    return None
