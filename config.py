"""Application configuration, loaded once from the environment.

load_dotenv() runs at import time, before anything else reads os.environ.
Any module that needs a setting imports it from here rather than reading
the environment directly, so there is exactly one place where a missing or
malformed setting is detected and reported.

Nothing here talks to the model or the network -- this module only reads
values and validates their shape.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Must happen before any os.environ read below. Plain `python -m cli.main`
# does not load a .env on its own.
load_dotenv()

# Project root, resolved from this file rather than the working directory,
# so the knowledge base loads correctly no matter where the app is started.
PROJECT_ROOT = Path(__file__).resolve().parent
ROUTER_DOCS_DIR = PROJECT_ROOT / "data" / "routers"

# The chat model driving the conversation. Kept as one configurable value
# so correcting the model id is a .env edit, not a code change.
MODEL_NAME = os.environ.get("ISP_MODEL", "gemini-flash-3.6")

# Policy rule 4: hand over after this many DISTINCT troubleshooting steps
# have been completed without resolving the issue. Configurable for
# demonstration, but the enforcement itself lives in core/policy.py -- this
# is only the threshold, never the decision.
MAX_FAILED_STEPS = int(os.environ.get("ISP_MAX_FAILED_STEPS", "2"))

# Ceiling on model/tool steps within a single turn. LangGraph counts a
# model step and a tool step as separate nodes, hence the doubling where
# this is passed as recursion_limit (see agent/graph.py). Hitting it is
# handled as a recoverable failure, not a crash.
MAX_TURN_STEPS = int(os.environ.get("ISP_RECURSION_LIMIT", "8"))

# Per-request timeout in seconds. Short on purpose: a stuck provider should
# be abandoned in seconds rather than leaving the customer watching a dead
# prompt.
REQUEST_TIMEOUT_SECONDS = float(os.environ.get("ISP_TIMEOUT_SECONDS", "30"))


class ConfigError(RuntimeError):
    """Raised when the app cannot start with the current configuration."""


def require_api_key() -> str:
    """Returns the Google API key, or raises with an actionable message.

    Called at startup (cli/main.py) rather than lazily on the first model
    call, so a missing key fails before the customer has typed anything
    instead of halfway through a conversation.
    """
    key = os.environ.get("GOOGLE_API_KEY", "").strip()
    if not key:
        raise ConfigError(
            "GOOGLE_API_KEY is not set.\n"
            "Copy .env.example to .env and add your key:\n"
            "    GOOGLE_API_KEY=your-key-here"
        )
    return key


def validate() -> None:
    """Checks every setting the app depends on. Raises ConfigError on the
    first problem found, with a message safe to show in the terminal."""
    require_api_key()

    if MAX_FAILED_STEPS < 1:
        raise ConfigError("ISP_MAX_FAILED_STEPS must be at least 1.")
    if MAX_TURN_STEPS < 1:
        raise ConfigError("ISP_RECURSION_LIMIT must be at least 1.")
    if REQUEST_TIMEOUT_SECONDS <= 0:
        raise ConfigError("ISP_TIMEOUT_SECONDS must be greater than 0.")
    if not ROUTER_DOCS_DIR.is_dir():
        raise ConfigError(f"Router documentation directory not found: {ROUTER_DOCS_DIR}")
