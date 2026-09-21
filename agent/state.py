"""Per-conversation state for the agent layer.

Two things are tracked, deliberately kept apart:

    messages   -- the raw transcript, replayed to the model in full every
                  turn. No summarization step: a summary is one more place
                  the record can drift from what was actually said, and
                  this agent's correctness depends on an accurate record of
                  what the customer reported.

    troubleshooting -- the structured TroubleshootingState from core/, which
                  the tools read and write. This is the authoritative record
                  of what was confirmed, suggested, completed and failed.

The separation is the point. The model's understanding of the conversation
lives in `messages`; the facts the policy gates depend on live in
`troubleshooting`. If the model misremembers the transcript, the gates are
unaffected -- they never consult it.

State is in-memory and per-process. A restart loses the conversation, which
is acceptable for a prototype and is stated as a limitation in the README.
Moving to Redis or Postgres later means replacing this module only; nothing
above it reads these fields directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from core.models import TroubleshootingState


@dataclass
class ConversationState:
    """Everything one customer conversation needs to continue.

    Not a TypedDict/LangGraph state schema: this agent keeps its own state
    outside the graph and passes `messages` in per turn (see
    agent/graph.py). That keeps the tools' closure over
    `troubleshooting` valid for the whole conversation rather than only
    for a single graph invocation.
    """

    conversation_id: str
    messages: list = field(default_factory=list)
    troubleshooting: TroubleshootingState = field(default_factory=TroubleshootingState)

    # Set when the last turn failed (provider error, timeout, step limit),
    # so the CLI can explain what happened without the failure needing to
    # still be visible in the transcript.
    last_failure: Optional[str] = None

    def add_symptom(self, text: str) -> None:
        """Records a symptom the customer described.

        Kept here rather than in a tool because symptoms are free-form
        context for the handover summary, not something a policy gate turns
        on -- and because requiring a tool call for every mention of a
        symptom would be noise. Deduplicated so a customer restating the
        problem does not pad the summary.
        """
        cleaned = text.strip()
        if cleaned and cleaned not in self.troubleshooting.reported_symptoms:
            self.troubleshooting.reported_symptoms.append(cleaned)

    def is_finished(self) -> bool:
        """True once a handover has actually completed.

        Reads handover_done, which only a successful handover sets -- a
        failed send leaves this False so the conversation stays open and
        the customer is not abandoned mid-problem.
        """
        return self.troubleshooting.handover_done


def new_conversation(conversation_id: str = "cli-session") -> ConversationState:
    return ConversationState(conversation_id=conversation_id)
