"""Tool 4: hand the conversation to a human representative.

Policy rule 8 is the whole point of this file: the agent may only tell a
customer they are being connected when the handover operation actually
succeeded. So this tool relays core/handover.py's real result rather than
reporting its own intent.

On success it returns a ticket id. On failure it returns an error code and
NO ticket -- there is nothing in a failed result that could be misread as
success. The returned message says explicitly what the agent must tell the
customer in each case, because "I've connected you" after a failed send is
the most damaging thing this system could say: the customer stops seeking
help and nobody is coming.

Escalation is never blocked. Unlike the other tools this one has no gate
refusing to run -- a customer asking for a person always gets one
(rule 5), and an agent that believes it is stuck can always escalate.
"""

from __future__ import annotations

from langchain_core.tools import tool

from core.handover import build_summary, send_handover
from core.knowledge_base import KnowledgeBase
from core.models import HandoverReason, TroubleshootingState
from core.policy import handover_required

# What the model may pass as a reason, mapped to the policy's own enum.
# Constrained to this set so a handover reason always traces back to a
# policy rule rather than being free text the model composed.
_REASON_ALIASES = {
    "customer_requested": HandoverReason.CUSTOMER_REQUESTED,
    "two_failed_steps": HandoverReason.TWO_FAILED_STEPS,
    "no_applicable_steps": HandoverReason.NO_APPLICABLE_STEPS,
    "unsupported_model": HandoverReason.UNSUPPORTED_MODEL,
    "model_unconfirmable": HandoverReason.MODEL_UNCONFIRMABLE,
    "safety_stop": HandoverReason.SAFETY_STOP,
}


def build_escalate_to_human_tool(state: TroubleshootingState, kb: KnowledgeBase):
    """Builds the tool bound to this conversation's live state."""

    @tool
    def escalate_to_human(reason: str) -> dict:
        """Hand this conversation to a human representative.

        Call this when the customer asks for a person, when two steps have
        been completed without resolving the issue, when no approved step
        applies, or when a safety concern is reported.

        Args:
            reason: Why the handover is happening. One of:
                "customer_requested"  - they asked for a human.
                "two_failed_steps"    - two steps done, issue remains.
                "no_applicable_steps" - approved steps exhausted or none fit.
                "unsupported_model"   - no approved docs for their router.
                "model_unconfirmable" - the model could not be confirmed.
                "safety_stop"         - damage, burning smell, or hot unit.

        IMPORTANT: check the returned `ok` field. Only tell the customer
        they are being connected if ok is true. If ok is false, tell them
        plainly that the handover did not go through.
        """
        key = (reason or "").strip().lower()
        chosen = _REASON_ALIASES.get(key)

        if chosen is None:
            # Fall back to the policy's own assessment rather than
            # refusing: a badly-labelled escalation should still escalate.
            chosen = handover_required(state, kb) or HandoverReason.CUSTOMER_REQUESTED

        # An explicit request is recorded so the state reflects why this
        # happened, and so handover_required agrees on any later check.
        if chosen is HandoverReason.CUSTOMER_REQUESTED:
            state.customer_requested_human = True

        summary = build_summary(state, chosen, kb)
        result = send_handover(summary)

        if not result.ok:
            # Deliberately leaves state.handover_done False: nothing about
            # a failed send may look like a completed handover, here or in
            # any later check.
            return {
                "ok": False,
                "code": result.error_code,
                "handover_completed": False,
                "message": (
                    f"THE HANDOVER DID NOT GO THROUGH ({result.error_message}) "
                    "You must NOT tell the customer they are being connected or transferred. "
                    "Apologise, tell them plainly that you could not reach a representative "
                    "right now, and give them the option to call support directly on "
                    "0800 555 0100 or try again shortly. Their details have been noted so "
                    "they will not need to repeat everything."
                ),
                "summary_text": summary.to_text(),
            }

        state.handover_done = True
        state.handover_ticket_id = result.ticket_id

        return {
            "ok": True,
            "handover_completed": True,
            "ticket_id": result.ticket_id,
            "reason": chosen.value,
            "message": (
                f"The handover succeeded. Reference {result.ticket_id}. Tell the customer a "
                "representative will take over, give them this reference, and reassure them "
                "that everything they have already tried has been passed on so they will not "
                "need to repeat it. Do not suggest any further troubleshooting steps."
            ),
            "summary_text": summary.to_text(),
        }

    return escalate_to_human


def build_check_handover_needed_tool(state: TroubleshootingState, kb: KnowledgeBase):
    """Companion read-only tool: should this conversation be escalated?

    Lets the agent ask the policy directly instead of tracking the
    threshold itself. Pure read -- changes nothing.
    """

    @tool
    def check_if_handover_needed() -> dict:
        """Check whether this conversation must now be handed to a human.

        Call this if you are unsure whether to continue troubleshooting.
        """
        reason = handover_required(state, kb)
        if reason is None:
            return {
                "ok": True,
                "handover_required": False,
                "message": "No handover is required yet. You may continue troubleshooting.",
            }

        return {
            "ok": True,
            "handover_required": True,
            "reason": reason.value,
            "message": (
                f"A handover is required ({reason.value}). Do not offer further troubleshooting "
                "steps. Escalate now using the escalation tool."
            ),
        }

    return check_if_handover_needed
