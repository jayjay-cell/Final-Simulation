"""The agent's tool registry.

Unlike a typical LangGraph project, this module exposes build_tools(state,
kb) rather than a module-level ALL_TOOLS list. That is the central security
decision of this codebase and the reason most of the policy rules are
guarantees rather than requests.

Every tool is built as a closure over the live TroubleshootingState for one
conversation. The consequences:

  - No tool takes a conversation id, a customer id, or a router model as a
    parameter, so the model cannot describe a state other than the real
    one. "Use the R200 instructions" is not a malformed request to be
    rejected -- it is unrepresentable, because the schema has no field for
    it.
  - Confirmation, completion and failure counts are read from the object
    the tools themselves wrote to, never from the model's recollection of
    the transcript.
  - Two conversations can never see each other's state, since each gets its
    own closures.

Eight tools in total: five that change state (the assignment's four, plus
record_prior_attempt for steps done before the conversation began), and
three read-only companions that let the agent consult the record and the
policy instead of reasoning about them from the transcript. Each lives in
the file named after its primary tool.

Tool results are DATA, not instructions. A `message` field tells the agent
what the policy permits next; it is never a channel for anything in a
document or a customer message to redirect the agent's behaviour.
"""

from __future__ import annotations

from core.knowledge_base import KnowledgeBase
from core.models import TroubleshootingState
from tools.confirm_router_model import (
    build_confirm_router_model_tool,
    build_identification_help_tool,
)
from tools.escalate_to_human import (
    build_check_handover_needed_tool,
    build_escalate_to_human_tool,
)
from tools.get_next_step import (
    build_get_next_step_tool,
    build_review_progress_tool,
)
from tools.record_step_outcome import (
    build_record_prior_attempt_tool,
    build_record_step_outcome_tool,
)


def build_tools(state: TroubleshootingState, kb: KnowledgeBase) -> list:
    """Builds this conversation's tools, bound to its state.

    Called once per conversation (agent/graph.py). The returned list is
    what create_react_agent binds, so the model's entire surface for
    affecting the world is these nine functions.

    Order matters only for readability in traces -- it follows the shape of
    a real call: identify the router, look up what to try, report back,
    escalate.
    """
    return [
        # --- Identify the router (policy rules 1, 2) ---
        build_confirm_router_model_tool(state, kb),
        build_identification_help_tool(state, kb),
        # --- Decide what to try (policy rules 3, 6, 7) ---
        build_get_next_step_tool(state, kb),
        build_review_progress_tool(state, kb),
        # --- Record what happened (policy rules 3, 4) ---
        build_record_step_outcome_tool(state, kb),
        build_record_prior_attempt_tool(state, kb),
        # --- Hand over to a human (policy rules 4, 5, 6, 8) ---
        build_check_handover_needed_tool(state, kb),
        build_escalate_to_human_tool(state, kb),
    ]


# Human-readable status text per tool, shown in the terminal while a tool
# runs. Kept next to the registry so a new tool is easy to remember to add
# a line for; falls back to a generic message if one is missed.
TOOL_STATUS_TEXT = {
    "confirm_router_model": "Checking the router model",
    "get_model_identification_help": "Looking up how to identify the router",
    "get_next_troubleshooting_step": "Finding the next approved step",
    "review_progress": "Reviewing what's been tried",
    "record_step_outcome": "Noting what happened",
    "record_prior_attempt": "Noting what you already tried",
    "check_if_handover_needed": "Checking whether to involve a colleague",
    "escalate_to_human": "Connecting you to a representative",
}
