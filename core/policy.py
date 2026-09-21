"""The policy gates: every rule from data/policy.md that must hold, as code.

This module is the reason the assignment's rules are guarantees rather than
hopes. A system prompt can ask a model not to repeat a completed step; it
cannot ensure it. These functions are consulted by the tools in tools/, and
a tool's answer is whatever the function here returns -- the model has no
argument it can pass to change the outcome.

Pure functions over TroubleshootingState and KnowledgeBase. No LLM, no
network, no I/O, no mutation of the state passed in. Every gate can be
exercised in a REPL with no API key:

    from core.models import TroubleshootingState, StepAttempt, Outcome
    from core.policy import handover_required

Rule numbers in the docstrings refer to data/policy.md.
"""

from __future__ import annotations

from typing import Optional

import config
from core.knowledge_base import KnowledgeBase
from core.models import (
    HandoverReason,
    Outcome,
    StepAttempt,
    TroubleshootingState,
    TroubleshootingStep,
)

# Phrases that ask the assistant to skip, assume, or guess the router model.
# Policy rule 2: none of these is confirmation. Matching is a safety net for
# the obvious phrasings -- the real protection is that confirm_router_model
# requires an explicit `customer_stated_model` flag, so an unmatched
# paraphrase still cannot set model_confirmed. This list makes the common
# cases detectable so the agent can respond to them directly.
_BYPASS_PHRASES = (
    "just assume",
    "assume it",
    "assume it's",
    "does it matter",
    "doesn't matter",
    "does not matter",
    "they're all the same",
    "theyre all the same",
    "they are all the same",
    "pick one",
    "whichever",
    "just guess",
    "guess it",
    "skip that",
    "skip the model",
    "any model",
    "use the r100",
    "use the r200",
    "same instructions",
)

# After this many failed attempts to get the model identified, stop asking
# and route to a human (policy rule 2's tail: a customer who cannot or will
# not identify the model gets a person, not an endless loop).
MAX_MODEL_CONFIRMATION_ATTEMPTS = 3


def looks_like_bypass_request(text: str) -> bool:
    """True when a message appears to ask the assistant to assume or skip
    model confirmation (policy rule 2).

    Advisory only. A False here never grants confirmation -- confirmation
    requires an explicit statement of the model. This exists so the agent
    can recognise and answer the request, not to decide whether to trust it.
    """
    lowered = text.lower()
    return any(phrase in lowered for phrase in _BYPASS_PHRASES)


# --- Rule 1 & 2: model confirmation ---------------------------------------


def is_model_confirmed(state: TroubleshootingState) -> bool:
    """Rule 1: model-specific instructions require explicit confirmation.

    Note this reads model_confirmed, never claimed_model. A customer who
    said "maybe an R200?" has a claimed_model but no confirmation, and must
    not receive model-specific steps.
    """
    return state.model_confirmed and bool(state.claimed_model)


def can_give_model_specific_instructions(state: TroubleshootingState, kb: KnowledgeBase) -> bool:
    """Confirmed AND we actually hold approved documentation for it."""
    return is_model_confirmed(state) and kb.is_supported(state.claimed_model or "")


# --- Rule 3: never repeat a completed action ------------------------------


def is_step_completed(state: TroubleshootingState, step_id: str) -> bool:
    """Rule 3: a completed step must not be suggested again.

    True for steps the customer completed before the conversation started
    too -- "I already restarted it" makes the restart completed, and it is
    then as ineligible as one we walked them through ourselves.
    """
    attempt = state.attempt_for(step_id)
    return attempt is not None and attempt.completed


def completed_attempts(state: TroubleshootingState) -> list[StepAttempt]:
    """Every step actually carried out, in the order they happened. The
    handover summary is built from this."""
    return [a for a in state.attempts if a.completed]


def failed_step_count(state: TroubleshootingState) -> int:
    """Rule 4: the number of DISTINCT steps completed without resolving the
    issue.

    Distinctness is the whole point. A customer who says three times that
    restarting did not help has completed one step, not three -- counting
    reports instead of steps would trip the handover threshold early and
    fail the "customer repeats themselves" scenario. StepAttempt is stored
    one-per-step_id, so counting distinct failed attempts gives this for
    free.
    """
    return len({a.step_id for a in state.attempts if a.is_failed()})


def issue_resolved(state: TroubleshootingState) -> bool:
    return any(a.is_resolved() for a in state.attempts)


def attempts_needing_clarification(state: TroubleshootingState) -> list[StepAttempt]:
    """Completed steps whose result is unknown or ambiguous. The agent must
    ask about these rather than assuming an outcome."""
    return [a for a in state.attempts if a.needs_clarification()]


# --- Rule 6: what to try next ---------------------------------------------


def _is_step_eligible(state: TroubleshootingState, step: TroubleshootingStep) -> bool:
    """A step is eligible when it has not already been completed.

    `applies_when` is deliberately NOT evaluated here. It is written in
    prose for the model to judge against what the customer has described --
    that is a genuine judgement call about symptoms, which is the model's
    job. What code owns is the hard part: never returning a completed step.
    """
    return not is_step_completed(state, step.step_id)


def next_applicable_step(
    state: TroubleshootingState, kb: KnowledgeBase
) -> Optional[TroubleshootingStep]:
    """The next approved step for the confirmed model, or None.

    None means there is nothing approved left to try, which is a handover
    trigger (rule 6) -- never a licence to improvise a step.

    Returns None when the model is unconfirmed too: without confirmation
    there is no legitimate model-specific step to return at all (rule 1).
    """
    if not can_give_model_specific_instructions(state, kb):
        return None

    for step in kb.get_steps(state.claimed_model or ""):
        if _is_step_eligible(state, step):
            return step
    return None


def remaining_step_count(state: TroubleshootingState, kb: KnowledgeBase) -> int:
    if not can_give_model_specific_instructions(state, kb):
        return 0
    return sum(1 for s in kb.get_steps(state.claimed_model or "") if _is_step_eligible(state, s))


# --- The handover decision ------------------------------------------------


def handover_required(
    state: TroubleshootingState, kb: KnowledgeBase
) -> Optional[HandoverReason]:
    """THE gate. Returns why a human is needed, or None to keep going.

    Every other part of the system asks this rather than deciding for
    itself, so the handover rules live in exactly one place. Checked in
    priority order:

      1. CUSTOMER_REQUESTED    rule 5 -- honoured immediately, before
                               anything else, including any step in progress.
      2. MODEL_UNCONFIRMABLE   rule 2 -- asked too many times, still no
                               usable identification.
      3. UNSUPPORTED_MODEL     rule 6 -- confirmed a model we hold no
                               approved documentation for.
      4. TWO_FAILED_STEPS      rule 4 -- the threshold from config.
      5. NO_APPLICABLE_STEPS   rule 6 -- approved steps exhausted.

    Returns None while the model is simply not confirmed yet: that is a
    reason to keep asking, not to escalate.
    """
    # Rule 5 first: an explicit request for a human outranks everything,
    # including an unresolved step or a still-unconfirmed model.
    if state.customer_requested_human:
        return HandoverReason.CUSTOMER_REQUESTED

    if issue_resolved(state):
        return None

    if state.model_confirmation_attempts >= MAX_MODEL_CONFIRMATION_ATTEMPTS and not is_model_confirmed(state):
        return HandoverReason.MODEL_UNCONFIRMABLE

    if is_model_confirmed(state) and not kb.is_supported(state.claimed_model or ""):
        return HandoverReason.UNSUPPORTED_MODEL

    if failed_step_count(state) >= config.MAX_FAILED_STEPS:
        return HandoverReason.TWO_FAILED_STEPS

    # Only meaningful once we have a confirmed, supported model: an
    # unconfirmed model has no step list to exhaust.
    if can_give_model_specific_instructions(state, kb):
        if next_applicable_step(state, kb) is None:
            return HandoverReason.NO_APPLICABLE_STEPS

    return None


def handover_reason_text(reason: HandoverReason) -> str:
    """A plain, customer-safe explanation of why a handover is happening.

    Never mentions thresholds, tool names, or internal structure
    (confidentiality section of the policy).
    """
    return {
        HandoverReason.CUSTOMER_REQUESTED:
            "The customer asked to speak with a human representative.",
        HandoverReason.TWO_FAILED_STEPS:
            "Two troubleshooting steps were completed and the issue is still not resolved.",
        HandoverReason.NO_APPLICABLE_STEPS:
            "The approved troubleshooting steps for this router have been exhausted.",
        HandoverReason.UNSUPPORTED_MODEL:
            "No approved troubleshooting documentation exists for this router model.",
        HandoverReason.MODEL_UNCONFIRMABLE:
            "The router model could not be confirmed, so no model-specific steps could be given.",
        HandoverReason.SAFETY_STOP:
            "A safety concern was reported that must be handled by a human representative.",
    }[reason]
