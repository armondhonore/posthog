"""End-to-end tests for the App Home tab + AI preferences modal handlers.

Exercises the real handler flow against a real `SlackSettings` row, with the
Slack API client mocked out so we can assert what would be sent to Slack.
"""

from __future__ import annotations

import json
from types import ModuleType

import pytest
from unittest.mock import MagicMock, patch

from posthog.models.integration import Integration
from posthog.models.organization import Organization
from posthog.models.team.team import Team

from products.slack_app.backend.models import SlackSettings
from products.slack_app.backend.services.slack_app_home import (
    ACTION_EDIT_PERSONAL,
    ACTION_EDIT_WORKSPACE,
    ACTION_RESET_PERSONAL,
    EDIT_MODAL_PERSONAL_CALLBACK_ID,
    MODAL_ACTION_MODEL,
    MODAL_ACTION_RUNTIME_ADAPTER,
    MODAL_BLOCK_MODEL,
    MODAL_BLOCK_REASONING_EFFORT,
    MODAL_BLOCK_RUNTIME_ADAPTER,
)
from products.slack_app.backend.services.slack_app_home_handlers import (
    handle_ai_prefs_block_action,
    handle_app_home_opened,
    handle_app_home_view_submission,
)

SLACK_WORKSPACE_ID = "T_HOME"


@pytest.fixture
def slack_integration(db):
    organization = Organization.objects.create(name="Org")
    team = Team.objects.create(organization=organization, name="Team")
    return Integration.objects.create(
        team=team,
        kind="slack",
        integration_id=SLACK_WORKSPACE_ID,
        sensitive_config={"access_token": "xoxb"},
    )


@pytest.fixture
def mock_slack_client():
    """Patch the SlackIntegration so every handler call talks to a MagicMock.

    We assert on `views_publish`, `views_open`, `views_update`, and
    `chat_postEphemeral` calls instead of trying to drive a real Slack SDK.
    """

    fake_client = MagicMock()
    with patch("products.slack_app.backend.services.slack_app_home_handlers.SlackIntegration") as cls:
        instance = MagicMock()
        instance.client = fake_client
        cls.return_value = instance
        yield fake_client


@pytest.fixture
def flag_on():
    with patch(
        "products.slack_app.backend.services.ai_preferences.posthoganalytics.feature_enabled",
        return_value=True,
    ):
        yield


@pytest.fixture
def admin_user():
    with patch(
        "products.slack_app.backend.services.slack_app_home_handlers.is_slack_workspace_admin",
        return_value=True,
    ):
        yield


@pytest.fixture
def non_admin_user():
    with patch(
        "products.slack_app.backend.services.slack_app_home_handlers.is_slack_workspace_admin",
        return_value=False,
    ):
        yield


@pytest.fixture(autouse=True)
def _stub_task_runtime_helpers():
    """Same stub as the resolver tests: keeps the lazy `tasks.temporal` import
    from blowing up the test env (DEBUG=False + SANDBOX_PROVIDER=docker)."""

    supported_by_model = {
        ("claude", "claude-opus-4-7"): {"low", "medium", "high", "xhigh", "max"},
        ("claude", "claude-sonnet-4-6"): {"low", "medium", "high"},
        ("codex", "gpt-5.5"): {"low", "medium", "high", "xhigh"},
    }

    class _Effort:
        def __init__(self, value):
            self.value = value

    class _Adapter:
        def __init__(self, value):
            self.value = value

    class _RuntimeAdapter:
        CLAUDE = _Adapter("claude")
        CODEX = _Adapter("codex")

        def __iter__(self):
            return iter([self.CLAUDE, self.CODEX])

    public_efforts = tuple(_Effort(v) for v in ("low", "medium", "high", "xhigh", "max"))

    def fake_get_supported(adapter, model):
        return tuple(_Effort(v) for v in supported_by_model.get((adapter, model), set()))

    def fake_get_error(adapter, model, effort):
        if adapter is None or model is None or effort is None:
            return None
        if effort in supported_by_model.get((adapter, model), set()):
            return None
        return f"Effort '{effort}' not supported on {model}."

    import sys

    def fake_get_models(adapter):
        adapter_value = adapter.value if hasattr(adapter, "value") else adapter
        if adapter_value == "claude":
            return ("claude-opus-4-7", "claude-sonnet-4-6")
        if adapter_value == "codex":
            return ("gpt-5.5",)
        return ()

    fake = ModuleType("products.tasks.backend.facade.run_config")
    fake.get_supported_reasoning_efforts = fake_get_supported
    fake.get_reasoning_effort_error = fake_get_error
    fake.get_models_for_runtime_adapter = fake_get_models
    fake.PUBLIC_REASONING_EFFORTS = public_efforts
    fake.RuntimeAdapter = _RuntimeAdapter()

    saved = sys.modules.get(fake.__name__)
    sys.modules[fake.__name__] = fake
    try:
        yield
    finally:
        if saved is None:
            sys.modules.pop(fake.__name__, None)
        else:
            sys.modules[fake.__name__] = saved


# ---------------------------------------------------------------------------
# app_home_opened event
# ---------------------------------------------------------------------------


class TestHandleAppHomeOpened:
    def test_publishes_view_for_known_user(self, slack_integration, mock_slack_client, flag_on, admin_user):
        handle_app_home_opened({"user": "U001"}, SLACK_WORKSPACE_ID)
        assert mock_slack_client.views_publish.called
        kwargs = mock_slack_client.views_publish.call_args.kwargs
        assert kwargs["user_id"] == "U001"
        assert kwargs["view"]["type"] == "home"

    def test_noop_when_user_missing(self, slack_integration, mock_slack_client, flag_on):
        handle_app_home_opened({}, SLACK_WORKSPACE_ID)
        assert not mock_slack_client.views_publish.called

    def test_noop_when_integration_missing(self, db, mock_slack_client, flag_on):
        handle_app_home_opened({"user": "U001"}, "T_UNKNOWN")
        assert not mock_slack_client.views_publish.called


# ---------------------------------------------------------------------------
# block_actions
# ---------------------------------------------------------------------------


class TestEditPersonalAction:
    def test_opens_modal(self, slack_integration, mock_slack_client, admin_user):
        payload = _block_action_payload(
            action_id=ACTION_EDIT_PERSONAL,
            slack_user_id="U001",
            trigger_id="trig.1",
        )
        handle_ai_prefs_block_action(payload, payload["actions"][0])
        assert mock_slack_client.views_open.called
        view = mock_slack_client.views_open.call_args.kwargs["view"]
        assert view["callback_id"] == EDIT_MODAL_PERSONAL_CALLBACK_ID


class TestEditWorkspaceAdminGate:
    def test_admin_opens_modal(self, slack_integration, mock_slack_client, admin_user):
        payload = _block_action_payload(
            action_id=ACTION_EDIT_WORKSPACE,
            slack_user_id="U001",
            trigger_id="trig.2",
        )
        handle_ai_prefs_block_action(payload, payload["actions"][0])
        assert mock_slack_client.views_open.called

    def test_non_admin_blocked(self, slack_integration, mock_slack_client, non_admin_user):
        payload = _block_action_payload(
            action_id=ACTION_EDIT_WORKSPACE,
            slack_user_id="U001",
            trigger_id="trig.3",
            channel="C1",
        )
        handle_ai_prefs_block_action(payload, payload["actions"][0])
        # Non-admin should not get the modal — they get an ephemeral notice instead.
        assert not mock_slack_client.views_open.called
        assert mock_slack_client.chat_postEphemeral.called


class TestResetPersonal:
    def test_clears_ai_fields_and_republishes(self, slack_integration, mock_slack_client, flag_on, admin_user):
        SlackSettings.objects.create(
            default_integration=slack_integration,
            slack_workspace_id=SLACK_WORKSPACE_ID,
            slack_user_id="U001",
            ai_runtime_adapter="claude",
            ai_model="claude-opus-4-7",
            ai_reasoning_effort="high",
        )
        payload = _block_action_payload(
            action_id=ACTION_RESET_PERSONAL,
            slack_user_id="U001",
            trigger_id="trig.4",
        )
        handle_ai_prefs_block_action(payload, payload["actions"][0])

        row = SlackSettings.objects.get(slack_workspace_id=SLACK_WORKSPACE_ID, slack_user_id="U001")
        assert row.ai_runtime_adapter is None
        assert row.ai_model is None
        assert row.ai_reasoning_effort is None
        # And the Home tab gets re-published with the cleared state.
        assert mock_slack_client.views_publish.called


# ---------------------------------------------------------------------------
# view_submission
# ---------------------------------------------------------------------------


class TestPersonalSubmit:
    def test_writes_row_and_republishes(self, slack_integration, mock_slack_client, flag_on, admin_user):
        payload = _view_submission_payload(
            callback_id=EDIT_MODAL_PERSONAL_CALLBACK_ID,
            slack_user_id="U001",
            runtime_adapter="claude",
            model="claude-opus-4-7",
            effort="high",
        )
        response = handle_app_home_view_submission(payload)
        assert response.status_code == 200
        assert json.loads(response.content) == {"response_action": "clear"}

        row = SlackSettings.objects.get(slack_workspace_id=SLACK_WORKSPACE_ID, slack_user_id="U001")
        assert row.ai_runtime_adapter == "claude"
        assert row.ai_model == "claude-opus-4-7"
        assert row.ai_reasoning_effort == "high"
        assert mock_slack_client.views_publish.called

    def test_invalid_pair_keeps_modal_open_with_error(self, slack_integration, mock_slack_client, flag_on):
        # Effort unsupported on this model — validate_ai_preferences rejects.
        payload = _view_submission_payload(
            callback_id=EDIT_MODAL_PERSONAL_CALLBACK_ID,
            slack_user_id="U001",
            runtime_adapter="claude",
            model="claude-sonnet-4-6",
            effort="xhigh",
        )
        response = handle_app_home_view_submission(payload)
        body = json.loads(response.content)
        assert body["response_action"] == "errors"
        assert MODAL_BLOCK_RUNTIME_ADAPTER in body["errors"]
        # No row written, no publish (we left the modal open).
        assert not SlackSettings.objects.filter(slack_user_id="U001").exists()


class TestWorkspaceSubmitAdminGate:
    def test_non_admin_blocked(self, slack_integration, mock_slack_client, flag_on, non_admin_user):
        from products.slack_app.backend.services.slack_app_home import EDIT_MODAL_WORKSPACE_CALLBACK_ID

        payload = _view_submission_payload(
            callback_id=EDIT_MODAL_WORKSPACE_CALLBACK_ID,
            slack_user_id="U_NONADMIN",
            runtime_adapter="claude",
            model="claude-opus-4-7",
            effort="high",
        )
        response = handle_app_home_view_submission(payload)
        body = json.loads(response.content)
        assert body["response_action"] == "errors"
        # Nothing persisted at the workspace level.
        assert not SlackSettings.objects.filter(slack_user_id__isnull=True).exists()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _block_action_payload(
    *,
    action_id: str,
    slack_user_id: str,
    trigger_id: str | None = None,
    channel: str | None = None,
) -> dict:
    return {
        "type": "block_actions",
        "team": {"id": SLACK_WORKSPACE_ID},
        "user": {"id": slack_user_id},
        "trigger_id": trigger_id,
        "channel": {"id": channel} if channel else None,
        "actions": [{"action_id": action_id}],
    }


def _view_submission_payload(
    *,
    callback_id: str,
    slack_user_id: str,
    runtime_adapter: str | None,
    model: str | None,
    effort: str | None,
) -> dict:
    state: dict = {}
    if runtime_adapter:
        state[MODAL_BLOCK_RUNTIME_ADAPTER] = {
            MODAL_ACTION_RUNTIME_ADAPTER: {"selected_option": {"value": runtime_adapter}}
        }
    if model:
        state[MODAL_BLOCK_MODEL] = {MODAL_ACTION_MODEL: {"selected_option": {"value": model}}}
    if effort:
        state[MODAL_BLOCK_REASONING_EFFORT] = {"ai_prefs:reasoning_effort": {"selected_option": {"value": effort}}}
    return {
        "type": "view_submission",
        "team": {"id": SLACK_WORKSPACE_ID},
        "user": {"id": slack_user_id},
        "view": {
            "id": "V1",
            "hash": "H1",
            "callback_id": callback_id,
            "state": {"values": state},
        },
    }
