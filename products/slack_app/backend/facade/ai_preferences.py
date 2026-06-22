"""Facade re-exports for the Slack-app AI preferences subsystem.

Cross-product callers (e.g. Temporal activities under `posthog/temporal/`)
should import from here rather than reaching into `services/ai_preferences.py`
directly. The facade is the supported entry point — the internals can move
around without breaking the call site as long as this surface stays stable.

Mirrors the layering already in place for the tasks product
(`products/tasks/backend/facade/`).
"""

from products.slack_app.backend.services.ai_preferences import (
    SLACK_APP_HOME_FLAG,
    AIPreferences,
    resolve_ai_preferences,
    validate_ai_preferences,
)

__all__ = [
    "SLACK_APP_HOME_FLAG",
    "AIPreferences",
    "resolve_ai_preferences",
    "validate_ai_preferences",
]
