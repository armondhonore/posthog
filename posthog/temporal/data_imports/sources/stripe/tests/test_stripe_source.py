import pytest

from posthog.temporal.data_imports.sources.generated_configs import StripeAuthMethodConfig, StripeSourceConfig
from posthog.temporal.data_imports.sources.stripe.source import StripeSource
from posthog.temporal.data_imports.sources.stripe.stripe import (
    _is_truncated_stripe_list_response,
    _RateLimitRetryingRequestsClient,
)

_COMPLETE_LIST_BODY = b'{\n  "object": "list",\n  "data": [],\n  "has_more": false\n}'
# A list page cut off mid-string — what Stripe later fails to decode as "Invalid response body".
_TRUNCATED_LIST_BODY = (
    b'{\n  "object": "list",\n  "data": [\n    {\n      "id": "in_1",\n      "description": "a value that got cut'
)
# Webhook write responses are single objects, not lists — must never trigger the read-only retry.
_TRUNCATED_WEBHOOK_BODY = b'{\n  "object": "webhook_endpoint",\n  "id": "we_1",\n  "url": "https://example.com/cut'


class TestStripeSource:
    def setup_method(self):
        self.source = StripeSource()

    @pytest.mark.parametrize(
        "observed_error",
        [
            # 403 raised mid-sync — `str(StripeError)` is "Request <id>: <message>", with no class
            # name, so these are matched on the stable message text rather than "PermissionError".
            "Request req_Zb0EgUuheEd4gf: Permission denied. The provided key 'rk_live_***j4va7j' does not have the required permissions for this endpoint on account 'acct_123'. Enabling \"Prices Read\" ('plan_read') permissions on this key would allow this request to continue.",
            "Request req_abc123: Only Stripe Connect platforms can work with other accounts. If you specified a client_id parameter, make sure it's correct.",
            # 401/403 surfaced as a requests HTTPError keep matching the existing URL-based keys.
            "401 Client Error: Unauthorized for url: https://api.stripe.com/v1/customers",
            "403 Client Error: Forbidden for url: https://api.stripe.com/v1/prices",
            # IP allowlist rejection — matched on the stable phrase, ignoring the appended IP address.
            "The API key provided does not allow requests from your IP address.",
            "The API key provided does not allow requests from your IP address (1.2.3.4).",
            # account_invalid: key not authorized for the configured account, or revoked app access.
            # Raised mid-sync as stripe.PermissionError, matched on the stable phrase (key/account redacted).
            "The provided key 'sk_test_***qPsl' does not have access to account 'stripe_s***less' (or that account does not exist). Application access may have been revoked.",
        ],
    )
    def test_non_retryable_errors_match_permission_failures(self, observed_error):
        non_retryable_errors = self.source.get_non_retryable_errors()
        assert any(key in observed_error for key in non_retryable_errors)

    @pytest.mark.parametrize(
        "other_error",
        [
            # Transient/infra errors must stay retryable.
            "HTTPSConnectionPool(host='api.stripe.com', port=443): Read timed out.",
            "500 Server Error: Internal Server Error for url: https://api.stripe.com/v1/charges",
            "Connection reset by peer",
        ],
    )
    def test_non_retryable_errors_do_not_match_transient(self, other_error):
        non_retryable_errors = self.source.get_non_retryable_errors()
        assert not any(key in other_error for key in non_retryable_errors)

    @pytest.mark.parametrize(
        "config,expected_message",
        [
            # OAuth selected but the integration was never linked (or was deleted): `_get_api_key`
            # raises ValueError("Missing Stripe integration ID"), an internal string the user can't
            # act on. validate_credentials must translate it to the reconnect guidance.
            (
                StripeSourceConfig(auth_method=StripeAuthMethodConfig(selection="oauth", stripe_integration_id=None)),
                "Stripe integration ID is not configured. Please reconnect your Stripe account.",
            ),
            (
                StripeSourceConfig(auth_method=StripeAuthMethodConfig(selection="api_key", stripe_secret_key=None)),
                "Stripe API key is not configured. Please update the source configuration.",
            ),
        ],
    )
    def test_validate_credentials_missing_config_returns_friendly_message(self, config, expected_message):
        ok, message = self.source.validate_credentials(config, team_id=1)

        assert ok is False
        assert message == expected_message

    @pytest.mark.parametrize(
        "body,expected",
        [
            (_TRUNCATED_LIST_BODY, True),
            (_TRUNCATED_LIST_BODY.decode(), True),  # str bodies behave the same as bytes
            (_COMPLETE_LIST_BODY, False),  # complete responses always close with "}"
            (_TRUNCATED_WEBHOOK_BODY, False),  # truncated, but a single object — not a list read
            (b'{\n  "object": "webhook_endpoint",\n  "id": "we_1"\n}', False),
            (b"", False),
            (None, False),
        ],
    )
    def test_is_truncated_stripe_list_response(self, body, expected):
        assert _is_truncated_stripe_list_response(body) is expected

    @pytest.mark.parametrize(
        "response,num_retries,expected",
        [
            # 2xx with a truncated list body is retried while budget remains...
            ((_TRUNCATED_LIST_BODY, 200, {}), 0, True),
            # ...but not once the network-retry budget is exhausted.
            ((_TRUNCATED_LIST_BODY, 200, {}), 2, False),
            # A complete 2xx list body is not retried.
            ((_COMPLETE_LIST_BODY, 200, {}), 0, False),
            # A truncated single-object (webhook write) body is not retried.
            ((_TRUNCATED_WEBHOOK_BODY, 200, {}), 0, False),
            # 429s stay retryable (regression guard for the existing rate-limit handling).
            ((b'{\n  "error": {}\n}', 429, {}), 0, True),
        ],
    )
    def test_rate_limit_client_should_retry(self, response, num_retries, expected):
        client = _RateLimitRetryingRequestsClient()
        assert client._should_retry(response, None, num_retries=num_retries, max_network_retries=2) is expected
