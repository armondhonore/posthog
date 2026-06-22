"""OAuth helpers for the Slack user-identity link flow.

Distinct from the workspace install flow handled by
``posthog.models.integration.OauthIntegration``: that one mints workspace-level
bot tokens and persists them as an ``Integration`` row. This one runs the
*Sign in with Slack* dance — ``user_scope=identity.basic,identity.email`` — and
treats the resulting user token as transient. We call ``users.identity`` once
to learn the Slack user id + team id, then drop the token on the floor: the
mapping row in ``UserIntegration(kind="slack")`` is all we keep.

Keeping this module thin and pure (no DB writes, no view logic) so the view
layer composes it with state validation, user auth, and the success follow-up
without secrets leaking outside of `exchange_code`.
"""

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

from django.core import signing

import requests
import structlog
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from posthog.models.instance_setting import get_instance_settings

logger = structlog.get_logger(__name__)

SLACK_AUTHORIZE_URL = "https://slack.com/oauth/v2/authorize"
SLACK_TOKEN_URL = "https://slack.com/api/oauth.v2.access"

# `identity.basic` returns `{user: {id, name}, team: {id}}` — the bare minimum
# we need to bind a Slack user to a PostHog user. `identity.email` is requested
# so support can answer "which Slack email did this user link with?" later;
# it's not consulted at resolve time and never persisted as a credential.
USER_IDENTITY_SCOPES = "identity.basic,identity.email"

# Distinct salt from the invite token: an invite leaked from a Slack DM must
# not be replayable as a callback state.
CALLBACK_STATE_SALT = "slack_user_link_oauth"
CALLBACK_STATE_MAX_AGE_SECONDS = 15 * 60


class SlackUserOAuthError(Exception):
    """Raised when the OAuth exchange or identity fetch fails. The view layer
    catches this and renders an error page; the user can retry from Slack."""


@dataclass(frozen=True)
class SlackIdentity:
    """Result of `oauth.v2.access` + `users.identity` for the user flow."""

    slack_user_id: str
    slack_team_id: str
    slack_team_name: str | None
    slack_email: str | None


def _credentials() -> tuple[str, str]:
    """Resolve the Slack app credentials at call time. Mirrors the lookup the
    workspace flow does so dev/test overrides via instance settings work the
    same way for both paths.
    """
    from_settings = get_instance_settings(["SLACK_APP_CLIENT_ID", "SLACK_APP_CLIENT_SECRET"])
    client_id = from_settings.get("SLACK_APP_CLIENT_ID") or ""
    client_secret = from_settings.get("SLACK_APP_CLIENT_SECRET") or ""
    if not client_id or not client_secret:
        raise SlackUserOAuthError("Slack app credentials not configured")
    return client_id, client_secret


def build_authorize_url(*, redirect_uri: str, state: str) -> str:
    """The full Slack URL the user is redirected to. ``user_scope`` is the
    Sign-in-with-Slack lever — bot scopes stay empty so the user doesn't see
    a permissions prompt for things the bot already has.
    """
    client_id, _ = _credentials()
    params = {
        "client_id": client_id,
        "user_scope": USER_IDENTITY_SCOPES,
        "scope": "",
        "redirect_uri": redirect_uri,
        "state": state,
    }
    return f"{SLACK_AUTHORIZE_URL}?{urlencode(params)}"


def build_callback_state(payload: dict[str, Any]) -> str:
    """Sign the state we round-trip through Slack's authorize endpoint. The
    payload always contains the invite context plus ``posthog_user_id`` so the
    callback can attribute the link to the right account even if the user
    swaps browsers mid-flow.
    """
    return signing.dumps(payload, salt=CALLBACK_STATE_SALT, compress=True)


def decode_callback_state(token: str) -> dict[str, Any] | None:
    try:
        decoded = signing.loads(token, salt=CALLBACK_STATE_SALT, max_age=CALLBACK_STATE_MAX_AGE_SECONDS)
    except signing.SignatureExpired:
        return None
    except signing.BadSignature:
        return None
    return decoded if isinstance(decoded, dict) else None


def exchange_code(*, code: str, redirect_uri: str) -> SlackIdentity:
    """Trade the auth code for a user token, then call ``users.identity`` to
    learn who that token belongs to. The token is discarded as soon as we
    have an identity — we don't persist it.

    ``redirect_uri`` must match the one used on ``build_authorize_url``
    exactly; Slack rejects the exchange otherwise.
    """
    client_id, client_secret = _credentials()
    try:
        response = requests.post(
            SLACK_TOKEN_URL,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
                "redirect_uri": redirect_uri,
            },
            timeout=10,
        )
    except requests.RequestException as exc:
        raise SlackUserOAuthError("Slack OAuth request failed") from exc

    try:
        payload = response.json()
    except ValueError as exc:
        raise SlackUserOAuthError("Slack OAuth returned non-JSON response") from exc

    if not payload.get("ok"):
        logger.warning("slack_user_link_oauth_exchange_failed", error=payload.get("error"))
        raise SlackUserOAuthError(f"Slack OAuth exchange failed: {payload.get('error')}")

    authed_user = payload.get("authed_user") or {}
    user_token = authed_user.get("access_token")
    if not user_token:
        raise SlackUserOAuthError("Slack OAuth response missing authed_user.access_token")

    # `authed_user.id` and `team.id` from `oauth.v2.access` are authoritative,
    # but we still call `users.identity` to pick up the email + team name in
    # the same request the user already authorized. Slack returns these
    # under the user-token scope, not the bot token, so we have to use the
    # token we just received.
    try:
        identity_response = WebClient(token=user_token).users_identity()
    except SlackApiError as exc:
        error = exc.response.get("error") if exc.response else None
        logger.warning("slack_user_link_users_identity_failed", error=error)
        raise SlackUserOAuthError(f"Slack users.identity failed: {error}") from exc

    user_info = identity_response.get("user") or {}
    team_info = identity_response.get("team") or {}
    slack_user_id = user_info.get("id") or authed_user.get("id")
    slack_team_id = team_info.get("id") or (payload.get("team") or {}).get("id")
    if not slack_user_id or not slack_team_id:
        raise SlackUserOAuthError("Slack identity response missing user.id or team.id")

    return SlackIdentity(
        slack_user_id=slack_user_id,
        slack_team_id=slack_team_id,
        slack_team_name=team_info.get("name") or (payload.get("team") or {}).get("name"),
        slack_email=user_info.get("email"),
    )
