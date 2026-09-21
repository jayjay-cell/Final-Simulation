"""The agent loop: LangGraph's create_react_agent, plus the gates that run
outside the model's control.

run_turn() is the whole public surface. One customer message in, one reply
out, with conversation state updated in place.

Three things happen here that the model cannot influence:

  1. PRE-TURN INTERCEPT. A message that explicitly asks for a human is
     detected before the model runs (policy rule 5). The request is
     recorded on state, so even if the model tries to talk the customer
     into one more step, every subsequent tool call sees
     customer_requested_human=True and the step tool refuses.

  2. STEP LIMIT. recursion_limit caps how many model/tool hops a turn can
     take. Hitting it is a recoverable failure, not a crash.

  3. POST-TURN VERIFICATION. After the model replies, the reply is checked
     against what actually happened. If the agent claimed a handover that
     did not succeed (policy rule 8), the claim is replaced. This is the
     one place a model output is overridden, and it is deliberate: that
     specific lie is the most damaging thing this system could say.

The system prompt is supplied via create_react_agent(prompt=...), never
prepended to the stored message list -- prepending and then persisting the
returned messages would add one more copy of the system prompt every turn.
"""

from __future__ import annotations

import logging
import re

from langgraph.prebuilt import create_react_agent

import config
from agent.prompt import SYSTEM_PROMPT
from agent.providers import build_model, describe_error
from agent.state import ConversationState
from core.knowledge_base import KnowledgeBase
from core.policy import handover_required
from tools import build_tools

logger = logging.getLogger("agent.graph")

# Explicit requests for a person. Matched before the model sees the turn so
# rule 5 does not depend on the model choosing to honour it. Deliberately
# narrow: these are phrasings that unambiguously ask for a human, not
# mentions of one ("did a human write these instructions?").
_HUMAN_REQUEST_PATTERNS = (
    r"\b(speak|talk|chat)\s+(to|with)\s+(a\s+)?(human|person|agent|representative|rep|someone|somebody)\b",
    r"\b(get|give|put)\s+me\s+(a|an|through\s+to)\s+(human|person|agent|representative|rep)\b",
    r"\b(transfer|escalate|put\s+me\s+through)\b",
    r"\bi\s+want\s+(a|an|to\s+speak\s+to\s+a)\s*(human|person|agent|representative|rep|manager)\b",
    r"\b(real|actual|live)\s+(human|person|agent)\b",
    r"\bhuman\s+(being|support|agent|representative)\b",
    r"\bcustomer\s+service\s+(rep|representative|agent)\b",
    # Bare demands: "human please", "agent now", "person!" -- terse and
    # usually impatient, which is exactly when rule 5 matters most.
    r"\b(human|person|agent|representative|rep)\s*[,.!]?\s*(please|now|thanks)\b",
    r"^\s*(a\s+)?(human|person|agent|representative)\s*[!.?]*\s*$",
)

# Phrases a reply must not contain unless a handover actually succeeded.
_HANDOVER_CLAIM_PATTERNS = (
    r"\b(connecting|transferring|putting)\s+you\b",
    r"\b(i'?ve|i\s+have|i'?m|i\s+am)\s+(now\s+)?(connected|connecting|transferred|transferring|escalated|escalating)\s+you\b",
    r"\byou'?re\s+being\s+(connected|transferred|put\s+through)\b",
    r"\b(a|an)\s+(human|person|agent|representative|colleague)\s+will\s+(be\s+with|take\s+over|contact|join)\b",
    r"\bhanded?\s+(this\s+)?over\s+to\s+(a|an|my)\b",
)

_SAFETY_PATTERNS = (
    r"\bburn(ing|t)?\s+(smell|smelling)\b",
    # "smells like burning", "smell of smoke", "smells burnt"
    r"\bsmell(s|ing|ed)?\s+(like|of)?\s*(burn|smoke|scorch)",
    r"\b(smoke|sparks?|sparking)\b",
    r"\b(melt(ed|ing)?|scorch(ed)?)\b",
    r"\b(really|very|too)\s+hot\b",
    r"\b(burning|burnt|on\s+fire)\b",
)


def _matches_any(text: str, patterns: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(re.search(p, lowered) for p in patterns)


def wants_human(text: str) -> bool:
    """True when a message explicitly asks for a human representative."""
    return _matches_any(text, _HUMAN_REQUEST_PATTERNS)


def reports_safety_issue(text: str) -> bool:
    """True when a message reports damage, smoke, or a hot unit."""
    return _matches_any(text, _SAFETY_PATTERNS)


def claims_handover(text: str) -> bool:
    """True when a reply tells the customer they are being connected."""
    return _matches_any(text, _HANDOVER_CLAIM_PATTERNS)


def build_agent(kb: KnowledgeBase, state: ConversationState):
    """Builds the ReAct agent for one conversation.

    Tools are built here, closed over this conversation's troubleshooting
    state, so the model's whole ability to affect anything runs through
    functions that already hold the real record.
    """
    tools = build_tools(state.troubleshooting, kb)
    return create_react_agent(build_model(), tools=tools, prompt=SYSTEM_PROMPT)


def _strip_system_messages(messages: list) -> list:
    """Drops system-role messages before persisting.

    Defensive: the system prompt is supplied fresh by `prompt=` on every
    call and must never accumulate in stored state.
    """

    def is_system(m) -> bool:
        role = m.get("role") if isinstance(m, dict) else getattr(m, "type", None)
        return role == "system"

    return [m for m in messages if not is_system(m)]


def _final_text(messages: list) -> str:
    if not messages:
        return ""
    last = messages[-1]
    content = last.get("content") if isinstance(last, dict) else getattr(last, "content", None)
    if isinstance(content, list):
        content = "".join(b.get("text", "") for b in content if isinstance(b, dict))
    return (content or "").strip()


# What to say when the model claimed a handover that did not happen. Fixed
# text rather than another model call: this path exists because the model
# already got this wrong once.
_FALSE_CLAIM_REPLACEMENT = (
    "I'm sorry - I wasn't able to reach a representative just now, so you haven't been "
    "transferred. Everything you've told me has been noted so you won't need to repeat it. "
    "Please call us on 0800 555 0100, or try again here in a few minutes."
)

_STEP_LIMIT_REPLY = (
    "Sorry, I got a bit tangled working that out. Could you tell me again what's happening "
    "with your connection?"
)


def run_turn(state: ConversationState, kb: KnowledgeBase, user_message: str) -> str:
    """Runs one turn and returns the assistant's reply.

    Never raises: a provider failure, timeout, or step-limit hit produces a
    safe reply and leaves the transcript usable for a retry.
    """
    text = user_message.strip()
    if not text:
        return "Sorry, I didn't catch that - could you say it again?"

    # --- Pre-turn gates, outside the model's control ---------------------

    # Rule 5: recorded before the model runs, so every tool call this turn
    # already knows a human was requested.
    if wants_human(text):
        state.troubleshooting.customer_requested_human = True
        logger.info("human request detected")

    if reports_safety_issue(text):
        state.troubleshooting.customer_requested_human = True
        logger.info("safety issue detected")

    state.add_symptom(text)
    state.messages.append({"role": "user", "content": text})

    # --- The model turn --------------------------------------------------

    try:
        agent = build_agent(kb, state)
        result = agent.invoke(
            {"messages": state.messages},
            # LangGraph counts model and tool steps as separate nodes.
            config={"recursion_limit": config.MAX_TURN_STEPS * 2 + 1},
        )
        state.messages = _strip_system_messages(result["messages"])
        reply = _final_text(state.messages)
        state.last_failure = None
    except Exception as err:  # noqa: BLE001 - single failure boundary for the turn
        logger.error("turn failed: %r", err, exc_info=True)
        state.last_failure = f"{type(err).__name__}: {err}"
        reply = (
            _STEP_LIMIT_REPLY
            if "recursion" in type(err).__name__.lower()
            else describe_error(err)
        )
        # Roll the transcript back to before this turn so the next attempt
        # starts clean rather than replaying a half-finished exchange.
        state.messages = [m for m in state.messages if m is not state.messages[-1]] \
            if state.messages and isinstance(state.messages[-1], dict) else state.messages
        state.messages.append({"role": "assistant", "content": reply})
        return reply

    # --- Post-turn verification ------------------------------------------

    # Rule 8: the single case where a model reply is overridden. Claiming a
    # handover that did not happen leaves the customer waiting for help
    # that is not coming.
    if claims_handover(reply) and not state.troubleshooting.handover_done:
        logger.warning("reply claimed a handover that did not succeed - replaced")
        reply = _FALSE_CLAIM_REPLACEMENT
        if state.messages and not isinstance(state.messages[-1], dict):
            state.messages = state.messages[:-1]
            state.messages.append({"role": "assistant", "content": reply})

    if not reply:
        reply = "Sorry, could you tell me a little more about what's happening?"

    return reply


def pending_handover_reason(state: ConversationState, kb: KnowledgeBase):
    """Whether a handover is now required, for the CLI to surface.

    Read-only convenience so the terminal can show the structured summary
    at the right moment without duplicating the policy's logic.
    """
    return handover_required(state.troubleshooting, kb)
