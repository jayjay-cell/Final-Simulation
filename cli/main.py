"""The terminal chat interface.

    python -m cli.main

Thin by design: it reads a line, calls agent.graph.run_turn, prints the
reply. All decisions live below it, so replacing this with an HTTP server
and a React UI later means writing a new entry point, not rewriting logic.

It also prints the structured handover summary when one completes, which is
the assignment's "useful handover summary" deliverable made visible.

Commands:
    /state   show the tracked troubleshooting record
    /summary show the handover summary as it currently stands
    /reset   start a fresh conversation
    /quit    exit
"""

from __future__ import annotations

import logging
import sys

import config
from agent.graph import run_turn
from agent.state import ConversationState, new_conversation
from core.handover import build_summary
from core.knowledge_base import KnowledgeBase, KnowledgeBaseError, load_knowledge_base
from core.models import HandoverReason
from core.policy import failed_step_count, handover_required, remaining_step_count

# Logs go to a file, never the terminal: a stack trace printed mid-chat
# would leak internals to the customer and wreck the transcript. force=True
# replaces any handler a library installed at import time, which would
# otherwise keep writing to stderr.
logging.basicConfig(
    filename="isp_agent.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    force=True,
)
logging.getLogger().propagate = False

BANNER = """
==============================================================
  HomeLink Internet Support  (prototype - synthetic data only)
==============================================================
  Type your message and press Enter.
  Commands:  /state   /summary   /reset   /quit
--------------------------------------------------------------
"""

GREETING = (
    "Hi, you're through to HomeLink support. I can help with internet "
    "connection problems. What's going on?"
)


def _print_wrapped(prefix: str, text: str, width: int = 76) -> None:
    """Prints a reply wrapped to the terminal, keeping the speaker label
    aligned with the text under it."""
    indent = " " * len(prefix)
    words = text.split()
    if not words:
        print(f"{prefix}...")
        return

    line = prefix
    current = len(prefix)
    for word in words:
        if current + len(word) + 1 > width and current > len(prefix):
            print(line)
            line = indent + word
            current = len(indent) + len(word)
        else:
            if line.strip() and line != prefix:
                line += " " + word
                current += len(word) + 1
            else:
                line += word
                current += len(word)
    print(line)


def _show_state(state: ConversationState, kb: KnowledgeBase) -> None:
    """The /state command: the structured record, distinct from whatever
    the model may believe about the conversation."""
    ts = state.troubleshooting
    print("\n--- tracked state ------------------------------------------")
    print(f"  router model        : {ts.claimed_model or '(not given)'}")
    print(f"  confirmed by customer: {ts.model_confirmed}")
    print(f"  asked for a human   : {ts.customer_requested_human}")
    print(f"  distinct failed steps: {failed_step_count(ts)} (threshold {config.MAX_FAILED_STEPS})")
    print(f"  approved steps left : {remaining_step_count(ts, kb)}")
    print(f"  handover completed  : {ts.handover_done}"
          + (f"  ticket {ts.handover_ticket_id}" if ts.handover_ticket_id else ""))

    if ts.attempts:
        print("  steps:")
        for a in ts.attempts:
            status = (
                "completed" if a.completed else "suggested, not done"
            )
            outcome = f" -> {a.outcome.value}" if a.outcome else ""
            prior = "  [before this chat]" if a.completed_before_conversation else ""
            print(f"    {a.step_id}: {status}{outcome}{prior}")
    else:
        print("  steps: none yet")

    reason = handover_required(ts, kb)
    print(f"  handover required   : {reason.value if reason else 'no'}")
    print("------------------------------------------------------------\n")


def _show_summary(state: ConversationState, kb: KnowledgeBase) -> None:
    """The /summary command: the handover record as it currently stands."""
    reason = handover_required(state.troubleshooting, kb) or HandoverReason.CUSTOMER_REQUESTED
    print()
    print(build_summary(state.troubleshooting, reason, kb).to_text())
    print()


def main() -> int:
    try:
        config.validate()
    except config.ConfigError as err:
        print(f"\nCannot start:\n\n{err}\n")
        return 1

    try:
        kb = load_knowledge_base()
    except KnowledgeBaseError as err:
        print(f"\nCannot start - problem with the router documentation:\n\n{err}\n")
        return 1

    print(BANNER)
    state = new_conversation()
    _print_wrapped("Support: ", GREETING)
    state.messages.append({"role": "assistant", "content": GREETING})

    # Tracks what has already been printed, so the summary block appears
    # exactly once when the handover completes.
    summary_shown = False

    while True:
        try:
            user_input = input("\nYou: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n\nGoodbye.")
            return 0

        if not user_input:
            continue

        lowered = user_input.lower()
        if lowered in ("/quit", "/exit", "quit", "exit"):
            print("\nGoodbye.")
            return 0
        if lowered == "/state":
            _show_state(state, kb)
            continue
        if lowered == "/summary":
            _show_summary(state, kb)
            continue
        if lowered == "/reset":
            state = new_conversation()
            summary_shown = False
            print("\n(new conversation started)\n")
            _print_wrapped("Support: ", GREETING)
            state.messages.append({"role": "assistant", "content": GREETING})
            continue

        print()
        reply = run_turn(state, kb, user_input)
        _print_wrapped("Support: ", reply)

        # The deliverable made visible: once a handover has actually
        # succeeded, show the record that was passed to the human.
        if state.troubleshooting.handover_done and not summary_shown:
            summary_shown = True
            reason = handover_required(state.troubleshooting, kb) or HandoverReason.CUSTOMER_REQUESTED
            print()
            print(build_summary(state.troubleshooting, reason, kb).to_text())
            print("\n(This summary was sent to the representative. "
                  "Type /quit to end, or keep chatting.)")


if __name__ == "__main__":
    sys.exit(main())
