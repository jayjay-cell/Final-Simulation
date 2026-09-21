"""The simulated handover to a human representative.

Two deliberately separate halves:

    build_summary()  -- pure, always succeeds. Assembles the structured
                        handover record from state that was already
                        recorded. Nothing is re-derived or inferred here.
    send_handover()  -- the operation that can FAIL. Returns a
                        HandoverResult carrying either a ticket id or an
                        error code, never both.

The split exists because of policy rule 8: the agent may only tell a
customer they are being connected when the handover operation actually
succeeded. Keeping the fallible part in its own function means the tool
layer has a real success/failure value to relay rather than an assumption.
A failed send produces no ticket id at all, so there is nothing in the
result that could be mistaken for success.

There is no real CRM behind this. send_handover simulates a server call and
is the seam where a real ticketing integration would go.
"""

from __future__ import annotations

import random
import string
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from core.knowledge_base import KnowledgeBase
from core.models import HandoverReason, Outcome, TroubleshootingState
from core.policy import completed_attempts, failed_step_count, handover_reason_text

# A customer symptom containing this phrase makes the simulated send fail.
# Deterministic on purpose: policy rule 8's failure path needs to be
# demonstrable on demand, rather than waiting for a random outage that
# never comes during a review.
HANDOVER_FAILURE_TRIGGER = "queue full"

_OUTCOME_TEXT = {
    Outcome.HELPED: "helped",
    Outcome.DIDNT_HELP: "did not help",
    Outcome.UNCLEAR: "result unclear",
    None: "no result reported",
}


@dataclass
class HandoverSummary:
    """The structured record passed to the human representative.

    Its whole job is that the customer does not have to repeat themselves.
    Every field is copied from what was actually recorded during the
    conversation -- this object never infers or reconstructs anything.

    `steps_attempted` entries are explicitly labelled as customer-reported,
    so the representative knows nothing here was independently verified.
    """

    reason: HandoverReason
    reason_text: str
    router_model: Optional[str]
    model_confirmed: bool
    documentation: Optional[str]
    reported_symptoms: list[str] = field(default_factory=list)
    steps_attempted: list[dict] = field(default_factory=list)
    steps_remaining: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    created_at: str = ""

    def to_text(self) -> str:
        """Human-readable block, printed to the terminal at handover."""
        lines = [
            "HANDOVER SUMMARY",
            f"  Reason          : {self.reason_text}",
            f"  Router model    : {self.router_model or 'not identified'}"
            + ("" if self.model_confirmed else "  (NOT CONFIRMED by customer)"),
            f"  Documentation   : {self.documentation or 'none applicable'}",
        ]

        lines.append("  Reported symptoms:")
        if self.reported_symptoms:
            lines += [f"    - {s}" for s in self.reported_symptoms]
        else:
            lines.append("    - none recorded")

        lines.append("  Steps already attempted (all customer-reported, not verified):")
        if self.steps_attempted:
            for step in self.steps_attempted:
                prior = " [done before contacting support]" if step["before_conversation"] else ""
                lines.append(f"    - {step['step_id']}: {step['title']} -> {step['outcome']}{prior}")
        else:
            lines.append("    - none")

        if self.steps_remaining:
            lines.append("  Approved steps not yet tried:")
            lines += [f"    - {s}" for s in self.steps_remaining]

        if self.open_questions:
            lines.append("  Needs clarification:")
            lines += [f"    - {q}" for q in self.open_questions]

        lines.append(f"  Created         : {self.created_at}")
        return "\n".join(lines)


def build_summary(
    state: TroubleshootingState,
    reason: HandoverReason,
    kb: KnowledgeBase,
) -> HandoverSummary:
    """Assembles the handover record. Pure -- reads state, returns a value.

    Records the model as unconfirmed when it is unconfirmed, rather than
    quietly presenting a guess as fact: the representative needs to know
    whether identification actually happened.
    """
    doc = kb.get_router_doc(state.claimed_model or "") if state.claimed_model else None

    steps_attempted = []
    for attempt in completed_attempts(state):
        step = kb.get_step(state.claimed_model or "", attempt.step_id)
        steps_attempted.append(
            {
                "step_id": attempt.step_id,
                "title": step.title if step else attempt.step_id,
                "outcome": _OUTCOME_TEXT[attempt.outcome],
                "before_conversation": attempt.completed_before_conversation,
                "source": attempt.source,
            }
        )

    # Only meaningful for a confirmed, supported model -- otherwise there is
    # no legitimate step list to describe as "remaining".
    steps_remaining: list[str] = []
    if state.model_confirmed and doc:
        done = {a.step_id for a in state.attempts if a.completed}
        steps_remaining = [f"{s.step_id}: {s.title}" for s in doc.steps if s.step_id not in done]

    open_questions = [
        f"{a.step_id}: customer completed this but the result was not clear"
        for a in state.attempts
        if a.needs_clarification()
    ]
    if not state.model_confirmed:
        open_questions.append("Router model was never confirmed by the customer.")

    return HandoverSummary(
        reason=reason,
        reason_text=handover_reason_text(reason),
        router_model=state.claimed_model,
        model_confirmed=state.model_confirmed,
        documentation=f"{doc.doc_id} v{doc.version}" if doc else None,
        reported_symptoms=list(state.reported_symptoms),
        steps_attempted=steps_attempted,
        steps_remaining=steps_remaining,
        open_questions=open_questions,
        created_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
    )


def _new_ticket_id() -> str:
    suffix = "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
    return f"HO-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{suffix}"


def send_handover(summary: HandoverSummary) -> "HandoverResultWithSummary":
    """Simulates the server-side handover call. THIS is the part that fails.

    Returns ok=True with a ticket id, or ok=False with an error code and no
    ticket. The caller must relay this result as-is: policy rule 8 forbids
    announcing a handover that did not happen.

    The simulated failure fires when a reported symptom contains
    HANDOVER_FAILURE_TRIGGER, so the failure path can be demonstrated
    deliberately during a review.
    """
    blob = " ".join(summary.reported_symptoms).lower()
    if HANDOVER_FAILURE_TRIGGER in blob:
        return HandoverResultWithSummary(
            ok=False,
            error_code="HANDOVER_QUEUE_UNAVAILABLE",
            error_message=(
                "The handover could not be completed: no representative could be reached."
            ),
            summary=summary,
        )

    return HandoverResultWithSummary(
        ok=True,
        ticket_id=_new_ticket_id(),
        summary=summary,
    )


@dataclass
class HandoverResultWithSummary:
    """A HandoverResult plus the summary that was sent.

    Carries the summary so the CLI can print the structured block on
    success, and so a failure still shows the representative-facing record
    the customer would otherwise have to recreate by hand.
    """

    ok: bool
    summary: HandoverSummary
    ticket_id: Optional[str] = None
    error_code: Optional[str] = None
    error_message: str = ""
