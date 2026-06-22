"""Block Kit renderer tests for the App Home tab + AI preferences modal.

These cover the pure-function rendering layer. Event/interactivity wiring is
covered in test_app_home_handlers.py.
"""

from __future__ import annotations

import pytest

from products.slack_app.backend.services.ai_preferences import AIPreferences
from products.slack_app.backend.services.app_home import (
    ACTION_EDIT_PERSONAL,
    ACTION_EDIT_WORKSPACE,
    ACTION_RESET_PERSONAL,
    EDIT_MODAL_PERSONAL_CALLBACK_ID,
    EDIT_MODAL_WORKSPACE_CALLBACK_ID,
    MODAL_ACTION_MODEL,
    MODAL_ACTION_REASONING_EFFORT,
    MODAL_ACTION_RUNTIME_ADAPTER,
    MODAL_BLOCK_MODEL,
    MODAL_BLOCK_REASONING_EFFORT,
    MODAL_BLOCK_RUNTIME_ADAPTER,
    MODELS_BY_RUNTIME_ADAPTER,
    PreferenceSource,
    parse_modal_submission,
    render_edit_modal,
    render_home_view,
    resolve_source,
)


def _make_row(*, runtime_adapter=None, model=None, reasoning_effort=None):
    """Stand-in for a SlackSettings row used by the renderer.

    The renderer only reads `ai_runtime_adapter` / `ai_model` /
    `ai_reasoning_effort`, so a plain duck type is enough — avoids dragging
    the database fixture into these pure-function tests.
    """

    class _Row:
        pass

    row = _Row()
    row.ai_runtime_adapter = runtime_adapter
    row.ai_model = model
    row.ai_reasoning_effort = reasoning_effort
    return row


def _action_ids(view: dict) -> list[str]:
    out: list[str] = []
    for block in view["blocks"]:
        for el in block.get("elements", []) or []:
            if "action_id" in el:
                out.append(el["action_id"])
    return out


def _block_ids(view: dict) -> list[str]:
    return [b.get("block_id") for b in view["blocks"] if b.get("block_id")]


class TestRenderHomeView:
    def test_empty_state_renders_buttons_and_no_reset(self):
        view = render_home_view(
            effective=AIPreferences(),
            user_row=None,
            workspace_row=None,
            is_admin=False,
        )
        assert view["type"] == "home"
        ids = _action_ids(view)
        # Personal edit button always present; reset hidden when no override.
        assert ACTION_EDIT_PERSONAL in ids
        assert ACTION_RESET_PERSONAL not in ids
        # Non-admin doesn't see the workspace edit button.
        assert ACTION_EDIT_WORKSPACE not in ids

    def test_admin_sees_workspace_edit_button(self):
        view = render_home_view(
            effective=AIPreferences(),
            user_row=None,
            workspace_row=None,
            is_admin=True,
        )
        assert ACTION_EDIT_WORKSPACE in _action_ids(view)

    def test_personal_override_renders_reset_button(self):
        view = render_home_view(
            effective=AIPreferences(runtime_adapter="claude", model="claude-opus-4-7", reasoning_effort="high"),
            user_row=_make_row(runtime_adapter="claude", model="claude-opus-4-7", reasoning_effort="high"),
            workspace_row=None,
            is_admin=False,
        )
        assert ACTION_RESET_PERSONAL in _action_ids(view)

    def test_active_model_summary_mentions_model_label(self):
        view = render_home_view(
            effective=AIPreferences(runtime_adapter="claude", model="claude-opus-4-7", reasoning_effort="high"),
            user_row=None,
            workspace_row=_make_row(runtime_adapter="claude", model="claude-opus-4-7", reasoning_effort="high"),
            is_admin=True,
        )
        text_blob = " ".join(block["text"]["text"] for block in view["blocks"] if block.get("type") == "section")
        # Friendly label rather than raw model id.
        assert "Claude Opus 4.7" in text_blob
        # Source attribution is visible.
        assert "Workspace default" in _all_text(view)

    def test_source_resolution_is_atomic(self):
        # User has only `reasoning_effort` set (no pair). Source should fall
        # through to the workspace's complete pair.
        assert (
            resolve_source(
                _make_row(reasoning_effort="medium"),
                _make_row(runtime_adapter="claude", model="claude-opus-4-7"),
            )
            == PreferenceSource.workspace()
        )

    def test_source_unset_when_neither_row_has_pair(self):
        assert resolve_source(None, None) == PreferenceSource.unset()
        assert resolve_source(_make_row(reasoning_effort="high"), None) == PreferenceSource.unset()


class TestRenderEditModal:
    @pytest.mark.parametrize(
        "scope,callback_id",
        [
            ("personal", EDIT_MODAL_PERSONAL_CALLBACK_ID),
            ("workspace", EDIT_MODAL_WORKSPACE_CALLBACK_ID),
        ],
    )
    def test_callback_id_matches_scope(self, scope, callback_id):
        view = render_edit_modal(scope=scope, current=AIPreferences())
        assert view["callback_id"] == callback_id

    def test_no_runtime_means_no_model_or_effort_blocks(self):
        view = render_edit_modal(scope="personal", current=AIPreferences())
        ids = _block_ids(view)
        assert MODAL_BLOCK_RUNTIME_ADAPTER in ids
        assert MODAL_BLOCK_MODEL not in ids
        assert MODAL_BLOCK_REASONING_EFFORT not in ids

    def test_runtime_picked_unlocks_model_block(self):
        view = render_edit_modal(scope="personal", current=AIPreferences(runtime_adapter="claude"))
        ids = _block_ids(view)
        assert MODAL_BLOCK_MODEL in ids
        # Effort block needs both the model and a non-empty supported list.
        assert MODAL_BLOCK_REASONING_EFFORT not in ids

    def test_model_options_match_runtime(self):
        view = render_edit_modal(scope="personal", current=AIPreferences(runtime_adapter="codex"))
        model_block = next(b for b in view["blocks"] if b.get("block_id") == MODAL_BLOCK_MODEL)
        option_values = [o["value"] for o in model_block["element"]["options"]]
        # Sanity: codex models, not claude models.
        assert set(option_values) == {v for v, _ in MODELS_BY_RUNTIME_ADAPTER["codex"]}

    def test_effort_block_renders_only_when_supported_efforts_provided(self):
        view = render_edit_modal(
            scope="personal",
            current=AIPreferences(runtime_adapter="claude", model="claude-opus-4-7"),
            supported_efforts=["low", "medium", "high"],
        )
        block = next(b for b in view["blocks"] if b.get("block_id") == MODAL_BLOCK_REASONING_EFFORT)
        assert block["optional"] is True
        values = [o["value"] for o in block["element"]["options"]]
        assert values == ["low", "medium", "high"]

    def test_initial_options_reflect_current_values(self):
        view = render_edit_modal(
            scope="workspace",
            current=AIPreferences(
                runtime_adapter="claude",
                model="claude-opus-4-7",
                reasoning_effort="high",
            ),
            supported_efforts=["low", "medium", "high"],
        )
        runtime_block = next(b for b in view["blocks"] if b.get("block_id") == MODAL_BLOCK_RUNTIME_ADAPTER)
        model_block = next(b for b in view["blocks"] if b.get("block_id") == MODAL_BLOCK_MODEL)
        effort_block = next(b for b in view["blocks"] if b.get("block_id") == MODAL_BLOCK_REASONING_EFFORT)
        assert runtime_block["element"]["initial_option"]["value"] == "claude"
        assert model_block["element"]["initial_option"]["value"] == "claude-opus-4-7"
        assert effort_block["element"]["initial_option"]["value"] == "high"

    def test_dispatch_action_set_on_runtime_and_model(self):
        view = render_edit_modal(scope="personal", current=AIPreferences(runtime_adapter="claude"))
        runtime_block = next(b for b in view["blocks"] if b.get("block_id") == MODAL_BLOCK_RUNTIME_ADAPTER)
        model_block = next(b for b in view["blocks"] if b.get("block_id") == MODAL_BLOCK_MODEL)
        # dispatch_action triggers a block_actions payload so the modal can
        # re-render with downstream options matching the new selection.
        assert runtime_block["dispatch_action"] is True
        assert model_block["dispatch_action"] is True


class TestParseModalSubmission:
    def test_all_three_picked(self):
        view = _build_submission(runtime_adapter="claude", model="claude-opus-4-7", effort="high")
        assert parse_modal_submission(view) == ("claude", "claude-opus-4-7", "high")

    def test_no_state_returns_all_none(self):
        assert parse_modal_submission({}) == (None, None, None)

    def test_partial_state_returns_partial_tuple(self):
        view = _build_submission(runtime_adapter="claude")
        assert parse_modal_submission(view) == ("claude", None, None)


def _build_submission(*, runtime_adapter=None, model=None, effort=None) -> dict:
    state: dict = {}
    if runtime_adapter:
        state[MODAL_BLOCK_RUNTIME_ADAPTER] = {
            MODAL_ACTION_RUNTIME_ADAPTER: {"selected_option": {"value": runtime_adapter}}
        }
    if model:
        state[MODAL_BLOCK_MODEL] = {MODAL_ACTION_MODEL: {"selected_option": {"value": model}}}
    if effort:
        state[MODAL_BLOCK_REASONING_EFFORT] = {MODAL_ACTION_REASONING_EFFORT: {"selected_option": {"value": effort}}}
    return {"state": {"values": state}}


def _all_text(view: dict) -> str:
    """Flatten all `text` fields in a view for substring assertions."""
    out: list[str] = []

    def walk(node):
        if isinstance(node, dict):
            if "text" in node and isinstance(node["text"], str):
                out.append(node["text"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(view)
    return " ".join(out)
