"""ChatOpenAI client for the labeling/report agents, routed through the internal
Go ai-gateway when AI_GATEWAY_URL + AI_GATEWAY_API_KEY are set, else direct to OpenAI.

The raw-SDK builders for summarization live in ``posthog.llm.gateway_client`` (importing
from this package would pull in the temporal workflow graph and cycle); this module shares
its ``resolve_ai_gateway_config`` validator.
"""

import os

from django.conf import settings

import httpx
from langchain_openai import ChatOpenAI

from posthog.cloud_utils import is_cloud
from posthog.llm.gateway_client import resolve_ai_gateway_config


def build_openai_chat_client(model: str, timeout: float) -> ChatOpenAI:
    """Return a ChatOpenAI client for the labeling/report agents. Cloud/DEBUG only.

    Routes through the internal Go ai-gateway when configured, else direct to OpenAI.
    In gateway mode the ``phs_`` bearer is team-scoped, so no per-team header is needed.
    """
    if not settings.DEBUG and not is_cloud():
        raise Exception("AI features are only available in PostHog Cloud")

    gateway = resolve_ai_gateway_config()
    if gateway:
        url, api_key = gateway
        return ChatOpenAI(
            model=model,
            api_key=api_key,
            base_url=url,
            timeout=timeout,
            max_retries=2,
            # trust_env=False keeps the in-cluster gateway call off the egress proxy.
            http_client=httpx.Client(trust_env=False),
            http_async_client=httpx.AsyncClient(trust_env=False),
        )

    direct_key = os.environ.get("OPENAI_API_KEY")
    if not direct_key:
        raise Exception("OPENAI_API_KEY is not configured")
    return ChatOpenAI(model=model, api_key=direct_key, timeout=timeout, max_retries=2)
