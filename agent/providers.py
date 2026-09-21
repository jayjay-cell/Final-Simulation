"""Builds the chat model.

One provider (Gemini via langchain-google-genai), isolated here so the rest
of the agent never imports an SDK directly. Swapping provider, or adding a
fallback chain later, means changing this file only.

Two settings matter for a terminal chat:

    timeout     -- short and explicit. A stuck provider should be abandoned
                   in seconds rather than leaving a customer watching a
                   dead prompt.
    max_retries -- 0. The SDK's own retry loop would sit inside our timeout
                   and hide the failure; a failed turn is handled once, at
                   the graph level, where it can produce a safe reply.

is_transient_error() classifies failures so callers can distinguish "try
again in a moment" from a real bug. Anything unrecognised is treated as a
real bug and propagates -- a broad except would let a bad model name or an
auth failure masquerade as a provider outage for the rest of the project's
life.
"""

from __future__ import annotations

import logging

from langchain_google_genai import ChatGoogleGenerativeAI

import config

logger = logging.getLogger("agent.providers")

# Substrings that mark a failure as worth retrying rather than a defect.
# Matched against the exception text because the provider SDK surfaces
# several of these as generic errors without distinct types.
_TRANSIENT_MARKERS = (
    "rate limit",
    "429",
    "resource exhausted",
    "quota",
    "timeout",
    "timed out",
    "deadline exceeded",
    "unavailable",
    "503",
    "502",
    "504",
    "internal error",
    "500",
    "connection",
    "temporarily",
)

# Markers of a configuration problem the user must fix. Worth naming
# separately so the CLI can say "your key is wrong" instead of "try again",
# which would be an endless and misleading loop.
_CONFIG_ERROR_MARKERS = (
    "api key not valid",
    "api_key_invalid",
    "invalid api key",
    "permission denied",
    "403",
    "unauthenticated",
    "401",
    "not found",
    "404",
    "is not supported",
    "unknown model",
)


def build_model() -> ChatGoogleGenerativeAI:
    """Constructs the chat model from config.

    config.require_api_key() is called first so a missing key fails with an
    actionable message here, rather than as an opaque SDK error on the
    first customer message.
    """
    config.require_api_key()

    logger.info("building model: %s", config.MODEL_NAME)
    return ChatGoogleGenerativeAI(
        model=config.MODEL_NAME,
        timeout=config.REQUEST_TIMEOUT_SECONDS,
        max_retries=0,
        # Low but not zero: tone should vary naturally across a
        # conversation, while the substance comes from the tools.
        temperature=0.3,
    )


def is_transient_error(err: BaseException) -> bool:
    """True when retrying could plausibly help."""
    text = f"{type(err).__name__} {err}".lower()
    if any(marker in text for marker in _CONFIG_ERROR_MARKERS):
        return False
    return any(marker in text for marker in _TRANSIENT_MARKERS)


def is_config_error(err: BaseException) -> bool:
    """True when the failure is a setup problem (bad key, wrong model id)
    that retrying will never fix."""
    text = f"{type(err).__name__} {err}".lower()
    return any(marker in text for marker in _CONFIG_ERROR_MARKERS)


def describe_error(err: BaseException) -> str:
    """A short, customer-safe description of a failure.

    Never includes the raw exception text, which can carry URLs, model
    ids, or key fragments.
    """
    if is_config_error(err):
        return (
            "The assistant is not configured correctly (check ISP_MODEL and GOOGLE_API_KEY "
            "in your .env file)."
        )
    if is_transient_error(err):
        return "The assistant is temporarily unavailable. Please try again in a moment."
    return "Something went wrong while processing that message."
