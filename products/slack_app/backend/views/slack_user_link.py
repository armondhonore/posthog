"""Backend OAuth views for the Slack user-identity link flow.

Two endpoints, both pure-backend (no SPA route). Mirrors the GitHub user-link
convention documented in ``posthog/api/github_callback/README.md``: per-user
OAuth callbacks live under ``/complete/<kind>-link/`` so the
``/integrations/<kind>/callback`` namespace stays reserved for the workspace
install routes the SPA owns.

* ``GET /complete/slack-link/start/``
  Entry point the user lands on from the Slack DM button. Login-gated, so an
  unauthenticated visitor is bounced through PostHog login and returns here
  with the invite token still in the URL. Validates the invite, then redirects
  to Slack to start the OAuth dance.

* ``GET /complete/slack-link/``
  Slack redirects here after the user authorizes. We exchange the code for a
  user token, call ``users.identity`` to learn the Slack user id + team, and
  upsert a ``UserIntegration(kind="slack")`` row pinning the Slack identity
  to the currently-logged-in PostHog user. On success we DM the user back in
  the original Slack thread (if we have channel + thread context) and render
  a brief confirmation page.

The whole feature is gated by ``slack-user-link``; with the flag off the
authorize view returns 404 so a leaked invite URL does nothing.
"""

from typing import Any

from django.conf import settings
from django.http import HttpRequest, HttpResponse, HttpResponseNotFound, HttpResponseRedirect
from django.shortcuts import render
from django.views.decorators.http import require_GET

import structlog

from posthog.models.integration import Integration, SlackIntegration
from posthog.models.user_integration import user_slack_integration_from_identity
from posthog.views import login_required

from products.slack_app.backend.services.slack_user_link import decode_invite_token, link_feature_enabled
from products.slack_app.backend.services.slack_user_oauth import (
    SlackUserOAuthError,
    build_authorize_url,
    build_callback_state,
    decode_callback_state,
    exchange_code,
)

logger = structlog.get_logger(__name__)


def _callback_redirect_uri() -> str:
    base = settings.SITE_URL.rstrip("/")
    return f"{base}/complete/slack-link/"


def _load_workspace_integration(posthog_team_id: int, slack_team_id: str) -> Integration | None:
    return (
        Integration.objects.filter(team_id=posthog_team_id, kind="slack", integration_id=slack_team_id)
        .select_related("team", "team__organization")
        .first()
    )


@require_GET
@login_required
def slack_user_link_authorize(request: HttpRequest) -> HttpResponse:
    """Validate the Slack-side invite, then redirect to Slack OAuth.

    The user is necessarily authenticated by the time this runs (the
    ``login_required`` decorator handles the bounce). A missing or expired
    invite renders a friendly error page instead of throwing — these links
    are passed around in Slack DMs and pruning the bad-link experience
    matters.
    """
    token = request.GET.get("state", "")
    invite = decode_invite_token(token) if token else None
    if not invite:
        return render(
            request,
            "slack_user_link/error.html",
            {"reason": "This link has expired. Mention PostHog in Slack again to get a fresh one."},
            status=400,
        )

    posthog_team_id = invite.get("posthog_team_id")
    slack_team_id = invite.get("slack_team_id")
    slack_user_id = invite.get("slack_user_id")
    if not isinstance(posthog_team_id, int) or not isinstance(slack_team_id, str) or not isinstance(slack_user_id, str):
        return render(request, "slack_user_link/error.html", {"reason": "Invalid link."}, status=400)

    workspace_integration = _load_workspace_integration(posthog_team_id, slack_team_id)
    if workspace_integration is None:
        return render(
            request,
            "slack_user_link/error.html",
            {"reason": "This Slack workspace is no longer connected to PostHog."},
            status=404,
        )

    if not link_feature_enabled(workspace_integration, slack_team_id):
        return HttpResponseNotFound()

    callback_state = build_callback_state(
        {
            "slack_user_id": slack_user_id,
            "slack_team_id": slack_team_id,
            "posthog_team_id": posthog_team_id,
            "posthog_user_id": request.user.id,
            "channel": invite.get("channel"),
            "thread_ts": invite.get("thread_ts"),
        }
    )

    try:
        authorize_url = build_authorize_url(redirect_uri=_callback_redirect_uri(), state=callback_state)
    except SlackUserOAuthError:
        logger.exception("slack_user_link_authorize_misconfigured")
        return render(request, "slack_user_link/error.html", {"reason": "Slack is not configured."}, status=500)

    return HttpResponseRedirect(authorize_url)


@require_GET
@login_required
def slack_user_link_callback(request: HttpRequest) -> HttpResponse:
    """Receive Slack's redirect, exchange the code, and persist the link.

    Re-checks login + feature flag at this end so a stale tab can't bypass
    either guard. The PostHog user is the *currently-logged-in* one, not the
    one originally encoded in state — that's a deliberate choice so a user
    who started the flow logged out and signed up in between still gets a
    correct link to their fresh account.
    """
    error = request.GET.get("error")
    if error:
        return render(
            request,
            "slack_user_link/error.html",
            {"reason": f"Slack returned an error: {error}"},
            status=400,
        )

    code = request.GET.get("code", "")
    state_token = request.GET.get("state", "")
    state = decode_callback_state(state_token) if state_token else None
    if not code or not state:
        return render(request, "slack_user_link/error.html", {"reason": "Invalid or expired link."}, status=400)

    posthog_team_id = state.get("posthog_team_id")
    expected_slack_team_id = state.get("slack_team_id")
    expected_slack_user_id = state.get("slack_user_id")
    if not isinstance(posthog_team_id, int) or not isinstance(expected_slack_team_id, str):
        return render(request, "slack_user_link/error.html", {"reason": "Invalid link."}, status=400)

    workspace_integration = _load_workspace_integration(posthog_team_id, expected_slack_team_id)
    if workspace_integration is None or not link_feature_enabled(workspace_integration, expected_slack_team_id):
        return HttpResponseNotFound()

    try:
        identity = exchange_code(code=code, redirect_uri=_callback_redirect_uri())
    except SlackUserOAuthError as exc:
        logger.warning("slack_user_link_callback_exchange_failed", error=str(exc))
        return render(
            request,
            "slack_user_link/error.html",
            {"reason": "Slack rejected the link. Please try again."},
            status=400,
        )

    # Hard-bind to the workspace from the original invite: if the user
    # authorized in a different Slack workspace tab, refuse rather than
    # silently linking them to the wrong workspace.
    if identity.slack_team_id != expected_slack_team_id:
        logger.warning(
            "slack_user_link_callback_team_mismatch",
            expected=expected_slack_team_id,
            actual=identity.slack_team_id,
        )
        return render(
            request,
            "slack_user_link/error.html",
            {"reason": "You signed in to a different Slack workspace than the one that started this flow."},
            status=400,
        )

    # `expected_slack_user_id` is informational — if the user clicks an invite
    # meant for a different person but authorizes as themselves, that's fine
    # (they're linking their own identity). We log the divergence so support
    # can spot a forwarded-button case.
    if isinstance(expected_slack_user_id, str) and identity.slack_user_id != expected_slack_user_id:
        logger.info(
            "slack_user_link_callback_user_diverged_from_invite",
            invite_user=expected_slack_user_id,
            authed_user=identity.slack_user_id,
        )

    user_slack_integration_from_identity(
        request.user,
        slack_user_id=identity.slack_user_id,
        slack_team_id=identity.slack_team_id,
        slack_team_name=identity.slack_team_name,
        slack_email_at_link=identity.slack_email,
    )

    _post_link_success_followup(
        workspace_integration=workspace_integration,
        slack_user_id=identity.slack_user_id,
        channel=state.get("channel"),
        thread_ts=state.get("thread_ts"),
    )

    context: dict[str, Any] = {
        "slack_team_name": identity.slack_team_name or "Slack",
        "posthog_email": request.user.email,
    }
    return render(request, "slack_user_link/success.html", context)


def _post_link_success_followup(
    *,
    workspace_integration: Integration,
    slack_user_id: str,
    channel: Any,
    thread_ts: Any,
) -> None:
    """Best-effort follow-up DM/thread message confirming the link. The user
    has already seen the success page in their browser, so a Slack post
    failure is not surfaced — it's a nice-to-have for context, not a
    correctness requirement.
    """
    if not isinstance(channel, str) or not channel:
        return
    try:
        client = SlackIntegration(workspace_integration).client
        client.chat_postEphemeral(
            channel=channel,
            user=slack_user_id,
            thread_ts=thread_ts if isinstance(thread_ts, str) else None,
            text="✅ Your PostHog account is now linked. Mention me again and I'll route to you correctly.",
        )
    except Exception:
        logger.info(
            "slack_user_link_success_followup_failed",
            channel=channel,
            slack_user_id=slack_user_id,
            exc_info=True,
        )
