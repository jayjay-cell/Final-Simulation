"""Tool 3: record what the customer reports after trying a step.

This is where the assignment's tracking requirements are enforced:

  - A step is only "completed" when the customer says they did it. A step
    the agent suggested and the customer ignored stays uncompleted and
    never counts toward the handover threshold.
  - Re-reporting the same attempt is idempotent. A customer repeating "I
    told you, restarting didn't help" three times leaves one failed step,
    not three, because the record is keyed on step_id (policy rules 3/4).
  - "unclear" is a first-class outcome, not a parsing failure. An ambiguous
    report is recorded as unclear so the agent asks a clarifying question
    instead of guessing, and get_next_step blocks until it is resolved.
  - Everything recorded stays labelled customer-reported. This system
    cannot observe anyone's equipment.

The companion tool at the bottom records a step the customer completed
BEFORE the conversation began ("I already restarted it"), which policy rule
3 treats as just as completed as one we walked them through.
"""

from __future__ import annotations

from langchain_core.tools import tool

from core.knowledge_base import KnowledgeBase
from core.models import Outcome, StepAttempt, TroubleshootingState
from core.policy import failed_step_count, handover_required, remaining_step_count

_VALID_OUTCOMES = {o.value for o in Outcome}


def build_record_step_outcome_tool(state: TroubleshootingState, kb: KnowledgeBase):
    """Builds the tool bound to this conversation's live state."""

    @tool
    def record_step_outcome(step_id: str, customer_completed_it: bool, outcome: str) -> dict:
        """Record what the customer reported about a troubleshooting step.

        Call this every time the customer tells you what happened after
        trying something.

        Args:
            step_id: The step being reported on, e.g. "R100-S2".
            customer_completed_it: True only if the customer actually did
                the step. False if they declined, could not do it, or have
                not done it yet.
            outcome: One of:
                "helped"      - the issue is now resolved.
                "didnt_help"  - they did it and the problem remains.
                "unclear"     - they did it but the result is ambiguous, or
                                you cannot tell from what they said. Use
                                this rather than guessing; you will be
                                prompted to ask a clarifying question.

        Repeating a report for the same step is safe -- it updates that one
        record rather than counting as a second attempt.
        """
        cleaned_id = (step_id or "").strip().upper()

        if outcome not in _VALID_OUTCOMES:
            return {
                "ok": False,
                "code": "ERR_INVALID_OUTCOME",
                "message": (
                    f"'{outcome}' is not a valid outcome. Use one of: "
                    f"{', '.join(sorted(_VALID_OUTCOMES))}."
                ),
            }

        # The step must exist in the confirmed model's approved docs. This
        # blocks recording progress against an invented step id, and
        # against another model's step id.
        step = kb.get_step(state.claimed_model or "", cleaned_id)
        if step is None:
            return {
                "ok": False,
                "code": "ERR_UNKNOWN_STEP",
                "message": (
                    f"'{cleaned_id}' is not an approved step for the confirmed router model. "
                    "Only record outcomes for steps you were given by the step tool."
                ),
            }

        attempt = state.attempt_for(cleaned_id)

        # Rule 3/4 guard: a step that was never suggested cannot be
        # reported on here. The separate prior-attempt tool exists for
        # things the customer did before the conversation.
        if attempt is None:
            return {
                "ok": False,
                "code": "ERR_STEP_NOT_SUGGESTED",
                "message": (
                    f"Step {cleaned_id} has not been given to the customer in this "
                    "conversation. If they did it before contacting support, use the "
                    "prior-attempt tool instead."
                ),
            }

        # Idempotent update. Keyed on step_id, so a repeated report edits
        # this one record and the distinct-failure count cannot be inflated
        # by a customer restating the same thing.
        was_already_completed = attempt.completed
        previous_outcome = attempt.outcome

        attempt.completed = customer_completed_it
        attempt.outcome = Outcome(outcome) if customer_completed_it else None

        repeat = was_already_completed and previous_outcome == attempt.outcome

        if not customer_completed_it:
            return {
                "ok": True,
                "step_id": cleaned_id,
                "recorded": "not_completed",
                "distinct_failed_steps": failed_step_count(state),
                "message": (
                    f"Recorded that the customer has not completed {cleaned_id}. It does not "
                    "count as an attempt. Do not move on to another step until they have "
                    "tried this one or you have a reason not to."
                ),
            }

        if attempt.outcome is Outcome.UNCLEAR:
            return {
                "ok": True,
                "step_id": cleaned_id,
                "recorded": "unclear",
                "distinct_failed_steps": failed_step_count(state),
                "message": (
                    f"Recorded {cleaned_id} as completed with an unclear result. Ask the "
                    "customer one specific question to establish what actually happened -- "
                    f"the documented expected result is: {step.expected_result}"
                ),
            }

        if attempt.outcome is Outcome.HELPED:
            return {
                "ok": True,
                "step_id": cleaned_id,
                "recorded": "helped",
                "issue_resolved": True,
                "message": (
                    f"Recorded {cleaned_id} as resolving the issue. Confirm with the customer "
                    "that everything is working, and close warmly. Do not suggest further steps."
                ),
            }

        # DIDNT_HELP
        reason = handover_required(state, kb)
        return {
            "ok": True,
            "step_id": cleaned_id,
            "recorded": "didnt_help",
            "was_repeat_report": repeat,
            "distinct_failed_steps": failed_step_count(state),
            "approved_steps_remaining": remaining_step_count(state, kb),
            "handover_now_required": reason.value if reason else None,
            "message": (
                (
                    "This is the same report already on record for this step, so it still "
                    "counts as one attempt. Do not re-suggest it. "
                    if repeat
                    else f"Recorded that {cleaned_id} did not resolve the issue. "
                )
                + (
                    "Two steps have now been completed without success -- hand over to a human "
                    "representative using the escalation tool."
                    if reason
                    else "You may offer the next approved step."
                )
            ),
        }

    return record_step_outcome


def build_record_prior_attempt_tool(state: TroubleshootingState, kb: KnowledgeBase):
    """Companion tool: a step the customer did BEFORE contacting support.

    Separate from record_step_outcome because the two are genuinely
    different events. This one creates the record; the other updates a
    record created when we suggested the step. Keeping them apart means
    record_step_outcome can safely reject a step it never suggested,
    instead of silently inventing history.

    Policy rule 3: a step completed before the conversation is just as
    completed as one we walked them through, and must not be suggested.
    """

    @tool
    def record_prior_attempt(step_id: str, outcome: str) -> dict:
        """Record a troubleshooting step the customer already did before
        contacting support.

        Call this when the customer opens with something like "I've already
        restarted it" or "I tried unplugging it yesterday". The step will be
        treated as completed and will not be suggested again.

        Args:
            step_id: The approved step that matches what they described,
                e.g. "R100-S2" for a power cycle.
            outcome: "helped", "didnt_help", or "unclear" -- what they say
                the result was. Use "unclear" if they did not say.
        """
        cleaned_id = (step_id or "").strip().upper()

        if outcome not in _VALID_OUTCOMES:
            return {
                "ok": False,
                "code": "ERR_INVALID_OUTCOME",
                "message": f"Use one of: {', '.join(sorted(_VALID_OUTCOMES))}.",
            }

        if not state.model_confirmed:
            return {
                "ok": False,
                "code": "ERR_MODEL_NOT_CONFIRMED",
                "message": (
                    "Confirm the router model first -- a step id only has meaning against a "
                    "specific model's documentation."
                ),
            }

        step = kb.get_step(state.claimed_model or "", cleaned_id)
        if step is None:
            return {
                "ok": False,
                "code": "ERR_UNKNOWN_STEP",
                "message": (
                    f"'{cleaned_id}' is not an approved step for {state.claimed_model}. "
                    "Only record prior attempts that match an approved step."
                ),
            }

        existing = state.attempt_for(cleaned_id)
        if existing is not None and existing.completed:
            return {
                "ok": True,
                "step_id": cleaned_id,
                "recorded": "already_on_record",
                "distinct_failed_steps": failed_step_count(state),
                "message": (
                    f"{cleaned_id} was already recorded as completed. Nothing changed -- this "
                    "still counts as one attempt."
                ),
            }

        attempt = existing or StepAttempt(step_id=cleaned_id, suggested=False)
        attempt.completed = True
        attempt.outcome = Outcome(outcome)
        attempt.completed_before_conversation = True
        if existing is None:
            state.attempts.append(attempt)

        reason = handover_required(state, kb)
        return {
            "ok": True,
            "step_id": cleaned_id,
            "title": step.title,
            "recorded": "completed_before_conversation",
            "distinct_failed_steps": failed_step_count(state),
            "approved_steps_remaining": remaining_step_count(state, kb),
            "handover_now_required": reason.value if reason else None,
            "message": (
                f"Recorded that the customer already did {cleaned_id} ({step.title}) before "
                "contacting support. Do not ask them to repeat it. Acknowledge that they "
                "already tried it before offering anything else."
            ),
        }

    return record_prior_attempt
