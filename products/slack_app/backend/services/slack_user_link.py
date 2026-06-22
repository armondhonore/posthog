"""Linked-user resolution + Slack-side invite for the user-OAuth flow.

This module owns the *inbound* side of the user-link feature: looking a Slack
user id up against ``UserIntegration(kind="slack")``, and posting the "Link my
PostHog account" button to a Slack channel when email matching fails. The
*outbound* OAuth dance (authorize URL, code exchange, users.identity) lives in
``slack_user_oauth`` to keep the two concerns testable in isolation.

Every public function here is a no-op when ``link_feature_enabled`` returns
``False``, so wiring this into existing resolvers is safe to ship with the
flag off.
"""

from typing import Any
from urllib.parse import urlencode

from django.conf import settings
from django.core import signing

import structlog
import posthoganalytics
from slack_sdk import WebClient

from posthog.models.integration import Integration
from posthog.models.organization import OrganizationMembership
from posthog.models.user import User
from posthog.models.user_integration import UserIntegration
from posthog.utils import get_instance_region

logger = structlog.get_logger(__name__)


LINK_FEATURE_FLAG = "slack-user-link"

# Short-lived invite tokens carry the Slack-side context (user id, workspace,
# thread) to the PostHog authorize view; longer would let the same Slack DM be
# replayed by anyone who scrapes it from a forwarded message.
INVITE_TOKEN_SALT = "slack_user_link_invite"
INVITE_TOKEN_MAX_AGE_SECONDS = 15 * 60


def link_feature_enabled(integration: Integration, slack_team_id: str) -> bool:
    """Per-workspace gate. Evaluated against the workspace's PostHog
    organization so we can roll out org-by-org and so a workspace connected
    to multiple PostHog orgs (cross-cutover) still gets a consistent answer.

    Fail-closed on any error: a flaky PostHog API check must not silently
    enable the linked-user lookup for everyone.
    """
    try:
        return bool(
            posthoganalytics.feature_enabled(
                LINK_FEATURE_FLAG,
                f"slack_workspace:{slack_team_id}",
                groups={"organization": str(integration.team.organization_id)},
                person_properties={"region": get_instance_region() or "unknown"},
                only_evaluate_locally=False,
                send_feature_flag_events=False,
            )
        )
    except Exception:
        logger.exception(
            "slack_user_link_feature_flag_check_failed",
            slack_team_id=slack_team_id,
            integration_id=integration.id,
        )
        return False


def find_linked_posthog_user(
    *,
    slack_user_id: str,
    slack_team_id: str,
    candidate_org_ids: set[int],
) -> User | None:
    """Return the PostHog ``User`` linked to this Slack identity, scoped to the
    organizations connected to this workspace.

    The scope check is what stops a Slack user who linked to *some* PostHog
    account from being matched into an unrelated workspace's events. Returns
    ``None`` when no link exists or the linked user is in no connected org.
    Caller still owns the access-level (``effective_membership_level``) check
    on the resolved user — same as the email path.
    """
    if not slack_user_id or not slack_team_id or not candidate_org_ids:
        return None
    # Split the lookup into two queries — the JSON match on `UserIntegration`
    # followed by an org-membership check — instead of a single cross-table
    # join. The chained `user__organization_memberships__organization_id__in`
    # lookup gets rejected by Django when one of the joined models lives on
    # a different database (which `User` does in PostHog's deployment), so a
    # straight-line equivalent keeps the query portable across routers.
    try:
        link = (
            UserIntegration.objects.filter(
                kind=UserIntegration.IntegrationKind.SLACK,
                integration_id=slack_user_id,
                config__slack_team_id=slack_team_id,
            )
            .select_related("user")
            .first()
        )
        if link is None or link.user is None:
            return None
        if not OrganizationMembership.objects.filter(
            user_id=link.user_id, organization_id__in=candidate_org_ids
        ).exists():
            return None
    except Exception:
        logger.warning(
            "slack_user_link_lookup_failed",
            slack_user_id=slack_user_id,
            slack_team_id=slack_team_id,
            exc_info=True,
        )
        return None
    return link.user


def build_invite_token(
    *,
    slack_user_id: str | None,
    slack_team_id: str,
    posthog_team_id: int,
    channel: str | None,
    thread_ts: str | None,
) -> str:
    """Sign the Slack-side context the user carries to the authorize view.

    ``slack_user_id`` is known on the Slack-DM invite path (we got the event
    that failed to match) and unknown on the settings-initiated path (the user
    is just clicking "Link my Slack account"). When omitted, the callback
    cannot run the divergence check, so the OAuth identity is trusted
    end-to-end and pinned only by ``slack_team_id``.

    ``posthog_team_id`` pins which workspace ``Integration`` we'll attach the
    link to on callback — so a user clicking an old invite after the
    integration was removed gets a clean error rather than a cross-workspace
    bind.
    """
    payload: dict[str, Any] = {
        "slack_team_id": slack_team_id,
        "posthog_team_id": posthog_team_id,
    }
    if slack_user_id:
        payload["slack_user_id"] = slack_user_id
    if channel:
        payload["channel"] = channel
    if thread_ts:
        payload["thread_ts"] = thread_ts
    return signing.dumps(payload, salt=INVITE_TOKEN_SALT, compress=True)


def decode_invite_token(token: str) -> dict[str, Any] | None:
    """Return the invite payload on success; ``None`` on bad signature or
    expiry. Callers branch on ``None`` to render a friendly "this link expired"
    page instead of a 400.
    """
    try:
        decoded = signing.loads(token, salt=INVITE_TOKEN_SALT, max_age=INVITE_TOKEN_MAX_AGE_SECONDS)
    except signing.SignatureExpired:
        return None
    except signing.BadSignature:
        return None
    return decoded if isinstance(decoded, dict) else None


def build_invite_url(
    *,
    slack_user_id: str | None,
    slack_team_id: str,
    posthog_team_id: int,
    channel: str | None,
    thread_ts: str | None,
) -> str:
    """Full https URL the Slack button opens. Routes through PostHog's
    ``SITE_URL`` (the canonical public host) so a workspace in a region
    different from the one that received the event still lands on the right
    instance after the proxy.
    """
    token = build_invite_token(
        slack_user_id=slack_user_id,
        slack_team_id=slack_team_id,
        posthog_team_id=posthog_team_id,
        channel=channel,
        thread_ts=thread_ts,
    )
    base = settings.SITE_URL.rstrip("/")
    return f"{base}/complete/slack-link/start/?{urlencode({'state': token})}"


def post_link_invite_message(
    *,
    slack_client: WebClient,
    channel: str,
    slack_user_id: str,
    thread_ts: str | None,
    slack_email: str | None,
    invite_url: str,
) -> None:
    """Post an ephemeral Block Kit message inviting the user to link their
    PostHog account. Visible only to ``slack_user_id``.

    Slack's ``url``-bearing buttons require no interactivity handler — clicks
    open the link in the user's browser and the OAuth dance proceeds without
    another round-trip to PostHog. Failures here are logged and swallowed:
    the existing plain-text feedback already informed the user of the
    underlying matching failure, so a button-post failure must not double-up
    a second visible error.
    """
    intro = (
        f"I couldn't match your Slack email (`{slack_email}`) to a PostHog account."
        if slack_email
        else "I couldn't match your Slack account to a PostHog account."
    )
    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"{intro}\nLink your PostHog account to fix this for good.",
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Link my PostHog account"},
                    "style": "primary",
                    "url": invite_url,
                }
            ],
        },
    ]
    try:
        slack_client.chat_postEphemeral(
            channel=channel,
            user=slack_user_id,
            thread_ts=thread_ts,
            text="Link your PostHog account to fix this for good.",
            blocks=blocks,
        )
    except Exception:
        logger.warning(
            "slack_user_link_invite_post_failed",
            channel=channel,
            slack_user_id=slack_user_id,
            exc_info=True,
        )
