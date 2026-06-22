import pytest
from unittest.mock import MagicMock, patch

from django.test import Client

from posthog.models.integration import Integration, SlackIntegration
from posthog.models.organization import Organization, OrganizationMembership
from posthog.models.team.team import Team
from posthog.models.user import User
from posthog.models.user_integration import UserIntegration, user_slack_integration_from_identity

from products.slack_app.backend.api import resolve_slack_user
from products.slack_app.backend.services.slack_user_link import (
    LINK_FEATURE_FLAG,
    build_invite_token,
    build_invite_url,
    decode_invite_token,
    find_linked_posthog_user,
    link_feature_enabled,
)
from products.slack_app.backend.services.slack_user_oauth import (
    SlackIdentity,
    SlackUserOAuthError,
    build_callback_state,
    decode_callback_state,
)

SLACK_TEAM_ID = "T12345"
SLACK_USER_ID = "U999"


@pytest.fixture
def org_team_user(db):
    org = Organization.objects.create(name="Test Org")
    team = Team.objects.create(organization=org, name="Test Team")
    user = User.objects.create(email="dev@example.com", distinct_id="user-1")
    OrganizationMembership.objects.create(user=user, organization=org)
    return org, team, user


@pytest.fixture
def workspace_integration(org_team_user):
    _, team, _ = org_team_user
    return Integration.objects.create(
        team=team,
        kind="slack",
        integration_id=SLACK_TEAM_ID,
        sensitive_config={"access_token": "xoxb-test"},
    )


class TestFindLinkedPosthogUser:
    def test_returns_linked_user_when_scoped_org_matches(self, org_team_user):
        org, _, user = org_team_user
        user_slack_integration_from_identity(
            user,
            slack_user_id=SLACK_USER_ID,
            slack_team_id=SLACK_TEAM_ID,
            slack_team_name="Test Workspace",
            slack_email_at_link="dev@example.com",
        )
        found = find_linked_posthog_user(
            slack_user_id=SLACK_USER_ID, slack_team_id=SLACK_TEAM_ID, candidate_org_ids={org.id}
        )
        assert found is not None
        assert found.id == user.id

    def test_returns_none_when_team_id_does_not_match(self, org_team_user):
        org, _, user = org_team_user
        user_slack_integration_from_identity(
            user,
            slack_user_id=SLACK_USER_ID,
            slack_team_id=SLACK_TEAM_ID,
            slack_team_name=None,
            slack_email_at_link=None,
        )
        assert (
            find_linked_posthog_user(slack_user_id=SLACK_USER_ID, slack_team_id="T-OTHER", candidate_org_ids={org.id})
            is None
        )

    def test_returns_none_when_user_is_in_different_org(self, org_team_user):
        _, _, user = org_team_user
        user_slack_integration_from_identity(
            user,
            slack_user_id=SLACK_USER_ID,
            slack_team_id=SLACK_TEAM_ID,
            slack_team_name=None,
            slack_email_at_link=None,
        )
        other_org = Organization.objects.create(name="Unrelated")
        assert (
            find_linked_posthog_user(
                slack_user_id=SLACK_USER_ID, slack_team_id=SLACK_TEAM_ID, candidate_org_ids={other_org.id}
            )
            is None
        )

    def test_returns_none_when_no_link_exists(self, org_team_user):
        org, _, _ = org_team_user
        assert (
            find_linked_posthog_user(
                slack_user_id=SLACK_USER_ID, slack_team_id=SLACK_TEAM_ID, candidate_org_ids={org.id}
            )
            is None
        )

    def test_empty_inputs_return_none(self):
        assert find_linked_posthog_user(slack_user_id="", slack_team_id=SLACK_TEAM_ID, candidate_org_ids={1}) is None
        assert find_linked_posthog_user(slack_user_id=SLACK_USER_ID, slack_team_id="", candidate_org_ids={1}) is None
        assert (
            find_linked_posthog_user(slack_user_id=SLACK_USER_ID, slack_team_id=SLACK_TEAM_ID, candidate_org_ids=set())
            is None
        )


class TestUserSlackIntegrationFromIdentity:
    def test_creates_row_with_empty_sensitive_config(self, org_team_user):
        _, _, user = org_team_user
        integration = user_slack_integration_from_identity(
            user,
            slack_user_id=SLACK_USER_ID,
            slack_team_id=SLACK_TEAM_ID,
            slack_team_name="Workspace",
            slack_email_at_link="dev@slack.example",
        )
        assert integration.kind == UserIntegration.IntegrationKind.SLACK
        assert integration.integration_id == SLACK_USER_ID
        assert integration.config["slack_team_id"] == SLACK_TEAM_ID
        assert integration.config["slack_team_name"] == "Workspace"
        assert integration.config["slack_email_at_link"] == "dev@slack.example"
        assert integration.sensitive_config == {}

    def test_update_refreshes_row_in_place(self, org_team_user):
        _, _, user = org_team_user
        first = user_slack_integration_from_identity(
            user,
            slack_user_id=SLACK_USER_ID,
            slack_team_id=SLACK_TEAM_ID,
            slack_team_name="Old name",
            slack_email_at_link=None,
        )
        second = user_slack_integration_from_identity(
            user,
            slack_user_id=SLACK_USER_ID,
            slack_team_id=SLACK_TEAM_ID,
            slack_team_name="New name",
            slack_email_at_link="dev@slack.example",
        )
        assert first.id == second.id
        assert second.config["slack_team_name"] == "New name"


class TestLinkFeatureEnabled:
    def test_fails_closed_on_posthoganalytics_error(self, workspace_integration):
        with patch("products.slack_app.backend.services.slack_user_link.posthoganalytics") as mock_ph:
            mock_ph.feature_enabled.side_effect = Exception("boom")
            assert link_feature_enabled(workspace_integration, SLACK_TEAM_ID) is False

    @pytest.mark.parametrize("enabled", [True, False])
    def test_passes_through_flag_value(self, workspace_integration, enabled):
        with patch("products.slack_app.backend.services.slack_user_link.posthoganalytics") as mock_ph:
            mock_ph.feature_enabled.return_value = enabled
            assert link_feature_enabled(workspace_integration, SLACK_TEAM_ID) is enabled
            # Confirm the flag key + groups payload — guards against silent rename.
            call_kwargs = mock_ph.feature_enabled.call_args.kwargs
            assert mock_ph.feature_enabled.call_args.args[0] == LINK_FEATURE_FLAG
            assert call_kwargs["groups"]["organization"] == str(workspace_integration.team.organization_id)


class TestResolveSlackUserWithLink:
    """Cover the linked-user lookup grafted onto `resolve_slack_user`."""

    @patch("posthog.models.integration.WebClient")
    @patch("products.slack_app.backend.api.link_feature_enabled")
    def test_flag_off_falls_through_to_email_path_unchanged(
        self, mock_flag, mock_webclient_class, org_team_user, workspace_integration
    ):
        _, _, user = org_team_user
        # Even with a link row present, flag-off behavior must not consult it.
        user_slack_integration_from_identity(
            user,
            slack_user_id=SLACK_USER_ID,
            slack_team_id=SLACK_TEAM_ID,
            slack_team_name=None,
            slack_email_at_link=None,
        )
        mock_flag.return_value = False
        mock_client = MagicMock()
        mock_webclient_class.return_value = mock_client
        mock_client.users_info.return_value = {"user": {"profile": {"email": "dev@example.com"}}}

        result = resolve_slack_user(
            SlackIntegration(workspace_integration), workspace_integration, SLACK_USER_ID, "C001", "1234.5"
        )

        assert result is not None
        # users.info IS called — confirms email path ran.
        assert mock_client.users_info.called
        # slack_email populated — also confirms email path.
        assert result.slack_email == "dev@example.com"

    @patch("posthog.models.integration.WebClient")
    @patch("products.slack_app.backend.api.link_feature_enabled")
    def test_flag_on_with_link_short_circuits_email_lookup(
        self, mock_flag, mock_webclient_class, org_team_user, workspace_integration
    ):
        _, _, user = org_team_user
        user_slack_integration_from_identity(
            user,
            slack_user_id=SLACK_USER_ID,
            slack_team_id=SLACK_TEAM_ID,
            slack_team_name=None,
            slack_email_at_link=None,
        )
        mock_flag.return_value = True
        mock_client = MagicMock()
        mock_webclient_class.return_value = mock_client

        result = resolve_slack_user(
            SlackIntegration(workspace_integration), workspace_integration, SLACK_USER_ID, "C001", "1234.5"
        )

        assert result is not None
        assert result.user.id == user.id
        # The whole point: no Slack API hit when a link exists.
        mock_client.users_info.assert_not_called()
        # And the contract: slack_email is None on the linked path.
        assert result.slack_email is None

    @patch("posthog.models.integration.WebClient")
    @patch("products.slack_app.backend.api.UserPermissions")
    @patch("products.slack_app.backend.api.link_feature_enabled")
    def test_flag_on_with_link_but_no_team_access_returns_none(
        self,
        mock_flag,
        mock_permissions_class,
        mock_webclient_class,
        org_team_user,
        workspace_integration,
    ):
        _, _, user = org_team_user
        user_slack_integration_from_identity(
            user,
            slack_user_id=SLACK_USER_ID,
            slack_team_id=SLACK_TEAM_ID,
            slack_team_name=None,
            slack_email_at_link=None,
        )
        mock_flag.return_value = True
        mock_client = MagicMock()
        mock_webclient_class.return_value = mock_client
        mock_permissions = MagicMock()
        mock_permissions.current_team.effective_membership_level = None
        mock_permissions_class.return_value = mock_permissions

        result = resolve_slack_user(
            SlackIntegration(workspace_integration), workspace_integration, SLACK_USER_ID, "C001", "1234.5"
        )
        assert result is None
        # User feedback is posted — access-denied message lands in Slack.
        assert mock_client.chat_postEphemeral.called or mock_client.chat_postMessage.called

    @patch("posthog.models.integration.WebClient")
    @patch("products.slack_app.backend.api.post_link_invite_message")
    @patch("products.slack_app.backend.api.link_feature_enabled")
    def test_flag_on_with_no_link_and_no_membership_posts_invite(
        self,
        mock_flag,
        mock_post_invite,
        mock_webclient_class,
        org_team_user,
        workspace_integration,
    ):
        mock_flag.return_value = True
        mock_client = MagicMock()
        mock_webclient_class.return_value = mock_client
        mock_client.users_info.return_value = {"user": {"profile": {"email": "stranger@example.com"}}}

        with patch("products.slack_app.backend.api.settings") as mock_settings:
            mock_settings.DEBUG = False
            result = resolve_slack_user(
                SlackIntegration(workspace_integration),
                workspace_integration,
                SLACK_USER_ID,
                "C001",
                "1234.5",
            )

        assert result is None
        # The existing text-based feedback still fires …
        assert mock_client.chat_postMessage.called or mock_client.chat_postEphemeral.called
        # … and the invite button is posted alongside it.
        mock_post_invite.assert_called_once()
        invite_kwargs = mock_post_invite.call_args.kwargs
        assert invite_kwargs["slack_user_id"] == SLACK_USER_ID
        assert invite_kwargs["slack_email"] == "stranger@example.com"
        assert invite_kwargs["invite_url"].startswith("http")

    @patch("posthog.models.integration.WebClient")
    @patch("products.slack_app.backend.api.post_link_invite_message")
    @patch("products.slack_app.backend.api.link_feature_enabled")
    def test_flag_off_with_no_membership_does_not_post_invite(
        self,
        mock_flag,
        mock_post_invite,
        mock_webclient_class,
        org_team_user,
        workspace_integration,
    ):
        mock_flag.return_value = False
        mock_client = MagicMock()
        mock_webclient_class.return_value = mock_client
        mock_client.users_info.return_value = {"user": {"profile": {"email": "stranger@example.com"}}}

        with patch("products.slack_app.backend.api.settings") as mock_settings:
            mock_settings.DEBUG = False
            result = resolve_slack_user(
                SlackIntegration(workspace_integration),
                workspace_integration,
                SLACK_USER_ID,
                "C001",
                "1234.5",
            )

        assert result is None
        mock_post_invite.assert_not_called()


class TestInviteToken:
    def test_round_trips(self):
        token = build_invite_token(
            slack_user_id=SLACK_USER_ID,
            slack_team_id=SLACK_TEAM_ID,
            posthog_team_id=42,
            channel="C001",
            thread_ts="1.2",
        )
        decoded = decode_invite_token(token)
        assert decoded == {
            "slack_user_id": SLACK_USER_ID,
            "slack_team_id": SLACK_TEAM_ID,
            "posthog_team_id": 42,
            "channel": "C001",
            "thread_ts": "1.2",
        }

    def test_rejects_tampered_token(self):
        token = build_invite_token(
            slack_user_id=SLACK_USER_ID,
            slack_team_id=SLACK_TEAM_ID,
            posthog_team_id=42,
            channel=None,
            thread_ts=None,
        )
        assert decode_invite_token(token + "x") is None

    def test_rejects_token_signed_with_other_salt(self):
        # A leaked invite token must not satisfy the callback-state check.
        callback_state = build_callback_state({"hello": "world"})
        assert decode_invite_token(callback_state) is None

    def test_invite_url_contains_signed_state(self):
        url = build_invite_url(
            slack_user_id=SLACK_USER_ID,
            slack_team_id=SLACK_TEAM_ID,
            posthog_team_id=42,
            channel=None,
            thread_ts=None,
        )
        assert "/complete/slack-link/start/?state=" in url


class TestCallbackState:
    def test_round_trips(self):
        payload = {"slack_user_id": "U1", "slack_team_id": "T1", "posthog_user_id": 99}
        token = build_callback_state(payload)
        assert decode_callback_state(token) == payload

    def test_rejects_invite_token(self):
        # Cross-salt protection in the other direction too.
        invite = build_invite_token(
            slack_user_id="U1", slack_team_id="T1", posthog_team_id=1, channel=None, thread_ts=None
        )
        assert decode_callback_state(invite) is None


class TestAuthorizeView:
    @pytest.fixture
    def logged_in_client(self, org_team_user):
        _, _, user = org_team_user
        client = Client()
        client.force_login(user)
        return client, user

    def test_missing_state_returns_400(self, logged_in_client):
        client, _ = logged_in_client
        response = client.get("/complete/slack-link/start/")
        assert response.status_code == 400

    def test_bad_state_returns_400(self, logged_in_client):
        client, _ = logged_in_client
        response = client.get("/complete/slack-link/start/?state=garbage")
        assert response.status_code == 400

    def test_unknown_workspace_returns_404(self, logged_in_client, org_team_user):
        client, _ = logged_in_client
        token = build_invite_token(
            slack_user_id=SLACK_USER_ID,
            slack_team_id="T-DOES-NOT-EXIST",
            posthog_team_id=999_999,
            channel=None,
            thread_ts=None,
        )
        response = client.get(f"/complete/slack-link/start/?state={token}")
        assert response.status_code == 404

    def test_flag_off_returns_404(self, logged_in_client, workspace_integration):
        client, _ = logged_in_client
        token = build_invite_token(
            slack_user_id=SLACK_USER_ID,
            slack_team_id=SLACK_TEAM_ID,
            posthog_team_id=workspace_integration.team_id,
            channel=None,
            thread_ts=None,
        )
        with patch("products.slack_app.backend.views.slack_user_link.link_feature_enabled", return_value=False):
            response = client.get(f"/complete/slack-link/start/?state={token}")
        assert response.status_code == 404

    def test_flag_on_redirects_to_slack_with_user_scope(self, logged_in_client, workspace_integration):
        client, _ = logged_in_client
        token = build_invite_token(
            slack_user_id=SLACK_USER_ID,
            slack_team_id=SLACK_TEAM_ID,
            posthog_team_id=workspace_integration.team_id,
            channel="C001",
            thread_ts="1.2",
        )
        with (
            patch("products.slack_app.backend.views.slack_user_link.link_feature_enabled", return_value=True),
            patch(
                "products.slack_app.backend.services.slack_user_oauth.get_instance_settings",
                return_value={"SLACK_APP_CLIENT_ID": "cid", "SLACK_APP_CLIENT_SECRET": "csecret"},
            ),
        ):
            response = client.get(f"/complete/slack-link/start/?state={token}")
        assert response.status_code == 302
        location = response["Location"]
        assert location.startswith("https://slack.com/oauth/v2/authorize?")
        # The whole point: user_scope is requested, bot scopes stay empty.
        assert "user_scope=identity.basic" in location
        assert "scope=&" in location or location.endswith("scope=")

    def test_unauthenticated_user_is_bounced_through_login(self, db):
        # `db` fixture: PostHog's `login_required` consults `User.objects.exists()`
        # before deciding what to do for anonymous traffic, so an empty test DB
        # is the minimum requirement even though this case never touches the
        # view body.
        client = Client()
        response = client.get("/complete/slack-link/start/?state=anything")
        assert response.status_code in (302, 303)


class TestCallbackView:
    @pytest.fixture
    def logged_in_client(self, org_team_user):
        _, _, user = org_team_user
        client = Client()
        client.force_login(user)
        return client, user

    def _state_for(self, user, posthog_team_id, *, slack_team_id=SLACK_TEAM_ID, slack_user_id=SLACK_USER_ID):
        return build_callback_state(
            {
                "slack_user_id": slack_user_id,
                "slack_team_id": slack_team_id,
                "posthog_team_id": posthog_team_id,
                "posthog_user_id": user.id,
                "channel": "C001",
                "thread_ts": "1.2",
            }
        )

    def test_missing_code_returns_400(self, logged_in_client, workspace_integration):
        client, user = logged_in_client
        state = self._state_for(user, workspace_integration.team_id)
        response = client.get(f"/complete/slack-link/?state={state}")
        assert response.status_code == 400

    def test_slack_error_param_renders_error(self, logged_in_client, workspace_integration):
        client, _ = logged_in_client
        response = client.get("/complete/slack-link/?error=access_denied&state=x")
        assert response.status_code == 400

    def test_happy_path_creates_link_and_renders_success(self, logged_in_client, workspace_integration):
        client, user = logged_in_client
        state = self._state_for(user, workspace_integration.team_id)

        identity = SlackIdentity(
            slack_user_id=SLACK_USER_ID,
            slack_team_id=SLACK_TEAM_ID,
            slack_team_name="My Workspace",
            slack_email="dev@slack.example",
        )
        with (
            patch("products.slack_app.backend.views.slack_user_link.link_feature_enabled", return_value=True),
            patch("products.slack_app.backend.views.slack_user_link.exchange_code", return_value=identity),
            patch("posthog.models.integration.WebClient"),
        ):
            response = client.get(f"/complete/slack-link/?code=abc&state={state}")

        assert response.status_code == 200
        link = UserIntegration.objects.get(user=user, kind=UserIntegration.IntegrationKind.SLACK)
        assert link.integration_id == SLACK_USER_ID
        assert link.config["slack_team_id"] == SLACK_TEAM_ID
        assert link.config["slack_team_name"] == "My Workspace"
        assert link.config["slack_email_at_link"] == "dev@slack.example"

    def test_team_mismatch_refuses_to_link(self, logged_in_client, workspace_integration):
        client, user = logged_in_client
        state = self._state_for(user, workspace_integration.team_id)

        identity = SlackIdentity(
            slack_user_id=SLACK_USER_ID,
            slack_team_id="T-DIFFERENT",
            slack_team_name=None,
            slack_email=None,
        )
        with (
            patch("products.slack_app.backend.views.slack_user_link.link_feature_enabled", return_value=True),
            patch("products.slack_app.backend.views.slack_user_link.exchange_code", return_value=identity),
        ):
            response = client.get(f"/complete/slack-link/?code=abc&state={state}")

        assert response.status_code == 400
        # No row should have been written.
        assert not UserIntegration.objects.filter(user=user, kind=UserIntegration.IntegrationKind.SLACK).exists()

    def test_oauth_exchange_failure_renders_error(self, logged_in_client, workspace_integration):
        client, user = logged_in_client
        state = self._state_for(user, workspace_integration.team_id)

        with (
            patch("products.slack_app.backend.views.slack_user_link.link_feature_enabled", return_value=True),
            patch(
                "products.slack_app.backend.views.slack_user_link.exchange_code",
                side_effect=SlackUserOAuthError("invalid_code"),
            ),
        ):
            response = client.get(f"/complete/slack-link/?code=abc&state={state}")

        assert response.status_code == 400
        assert not UserIntegration.objects.filter(user=user).exists()
