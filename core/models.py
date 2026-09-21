"""The data shapes for router documentation and conversation state.

Definitions only -- no logic, no I/O, no model. core/knowledge_base.py
fills the documentation shapes from JSON; core/policy.py reads the
conversation shapes to make decisions.

The important design choice here is in StepAttempt. "We suggested a step",
"the customer did it" and "it worked" are three separate facts, so they are
three separate fields rather than one status enum. Collapsing them loses
the distinction the policy depends on: a step suggested but never done must
not count toward the handover threshold, and a step done with an unclear
result must prompt a clarifying question rather than a guess.

Everything a customer tells us is reported, never verified -- this system
cannot observe anyone's equipment. StepAttempt.source records that
permanently so no later code can mistake a claim for an observation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Outcome(str, Enum):
    """What the customer said happened after completing a step.

    UNCLEAR is a real, first-class value, not a failure to parse. An
    ambiguous report ("it's kind of working") must be recorded as UNCLEAR so
    the agent asks a clarifying question, instead of being rounded to
    HELPED or DIDNT_HELP.
    """

    HELPED = "helped"
    DIDNT_HELP = "didnt_help"
    UNCLEAR = "unclear"


class HandoverReason(str, Enum):
    """Why a conversation is being handed to a human representative.

    Mirrors the policy rules one-to-one so the handover summary can state a
    reason that traces back to a specific rule.
    """

    CUSTOMER_REQUESTED = "customer_requested"          # Policy rule 5
    TWO_FAILED_STEPS = "two_failed_steps"              # Policy rule 4
    NO_APPLICABLE_STEPS = "no_applicable_steps"        # Policy rule 6
    UNSUPPORTED_MODEL = "unsupported_model"            # Policy rule 6
    MODEL_UNCONFIRMABLE = "model_unconfirmable"        # Policy rule 2
    SAFETY_STOP = "safety_stop"                        # Safety limits


# --- Router documentation (loaded from data/routers/*.json) ---------------


@dataclass(frozen=True)
class TroubleshootingStep:
    """One approved step from a router document.

    Frozen: documentation is reference data and is never modified at
    runtime. customer_instruction is the exact approved wording -- the
    agent may reword for tone but may not invent substance beyond it.
    """

    step_id: str
    title: str
    order: int
    applies_when: str
    customer_instruction: str
    expected_result: str
    prerequisites: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    result_meanings: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class RouterDoc:
    """One approved router document, with the metadata the policy requires
    (doc id, version, approval status) so a handover summary can cite
    exactly which document version was used."""

    model_id: str
    doc_id: str
    version: str
    approval_status: str
    display_name: str
    identification_question: str
    identification_cues: tuple[str, ...]
    known_symptoms: tuple[str, ...]
    steps: tuple[TroubleshootingStep, ...]
    out_of_scope: tuple[str, ...] = ()

    def step_by_id(self, step_id: str) -> Optional[TroubleshootingStep]:
        for step in self.steps:
            if step.step_id == step_id:
                return step
        return None


# --- Conversation state ---------------------------------------------------


@dataclass
class StepAttempt:
    """One troubleshooting step as it played out with this customer.

    The three-way distinction the policy depends on:

        suggested=True, completed=False            -> offered, not yet done.
                                                      Does NOT count toward
                                                      the handover threshold.
        suggested=True, completed=True, outcome=None
                                                   -> done, result not yet
                                                      reported. Ask.
        completed=True, outcome=DIDNT_HELP          -> a genuine failed step.
                                                      Counts once, no matter
                                                      how often re-reported.

    `source` is fixed at "customer_reported" and never set to anything else:
    this system has no way to verify a customer's claim, and the data shape
    should not allow later code to pretend otherwise.
    """

    step_id: str
    suggested: bool = True
    completed: bool = False
    outcome: Optional[Outcome] = None
    source: str = "customer_reported"
    note: str = ""

    # True when the customer had already done this before the conversation
    # started (policy rule 3: it still counts as completed and must not be
    # suggested again).
    completed_before_conversation: bool = False

    def is_failed(self) -> bool:
        """A completed step that did not resolve the issue. The handover
        counter is built from this, counted over DISTINCT step_ids."""
        return self.completed and self.outcome is Outcome.DIDNT_HELP

    def is_resolved(self) -> bool:
        return self.completed and self.outcome is Outcome.HELPED

    def needs_clarification(self) -> bool:
        """Completed but with no usable result -- either no outcome reported
        yet, or one the customer described ambiguously."""
        return self.completed and (self.outcome is None or self.outcome is Outcome.UNCLEAR)


@dataclass
class HandoverResult:
    """The outcome of the simulated handover operation.

    Policy rule 8 turns on this object: the agent may only tell a customer
    they are being connected when ok is True. A failure carries an error
    code and no ticket, leaving nothing that could be mistaken for success.
    """

    ok: bool
    ticket_id: Optional[str] = None
    error_code: Optional[str] = None
    error_message: str = ""


@dataclass
class TroubleshootingState:
    """Everything tracked about one customer conversation.

    Holds no messages -- the transcript lives in agent/state.py. This is the
    structured record the policy gates read, kept separate so every rule can
    be evaluated without an LLM or a message history.
    """

    # The model the customer mentioned. Set even when unconfirmed, so a
    # guess ("maybe an R200?") is remembered without being treated as fact.
    claimed_model: Optional[str] = None

    # True only when the customer explicitly confirmed the model. A request
    # to assume or skip confirmation must never set this (policy rule 2).
    model_confirmed: bool = False

    # Counts refusals/failures to identify the model, so repeated pressure
    # to skip confirmation routes to a human rather than looping forever.
    model_confirmation_attempts: int = 0

    attempts: list[StepAttempt] = field(default_factory=list)
    reported_symptoms: list[str] = field(default_factory=list)

    # Set when the customer asks for a human (policy rule 5 -- honoured
    # immediately, before any further troubleshooting).
    customer_requested_human: bool = False

    # Set only by a successful handover operation, never optimistically.
    handover_done: bool = False
    handover_ticket_id: Optional[str] = None

    def attempt_for(self, step_id: str) -> Optional[StepAttempt]:
        for attempt in self.attempts:
            if attempt.step_id == step_id:
                return attempt
        return None
