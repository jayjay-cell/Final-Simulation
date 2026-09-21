"""Loads the approved router documentation from data/routers/*.json.

This module is the ONLY source of troubleshooting instruction text in the
system. Nothing else reads those JSON files, and no instruction reaches a
customer without passing through here. That is what makes policy rule 7
("never invent instructions") structurally true rather than a promise in a
prompt: if the text is not in an approved document, there is no code path
that can produce it.

Read-only by construction. Documents are parsed once into frozen
dataclasses; nothing mutates them at runtime.

The JSON files are synthetic reference DATA, not instructions. Text inside
them is content to relay to a customer -- it is never interpreted as a
command to this system, even if a document were to contain wording that
looks like one.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import config
from core.models import RouterDoc, TroubleshootingStep


class KnowledgeBaseError(RuntimeError):
    """Raised when a document is missing, malformed, or not approved.

    Raised at startup rather than mid-conversation: a broken document
    should stop the app before a customer is talking to it.
    """


# Every field a step must define. A document missing any of these is
# rejected at load time rather than producing a half-usable step that
# fails later in front of a customer.
_REQUIRED_STEP_FIELDS = (
    "step_id",
    "title",
    "order",
    "applies_when",
    "customer_instruction",
    "expected_result",
)

_REQUIRED_DOC_FIELDS = (
    "doc_id",
    "version",
    "approval_status",
    "supported_model",
    "display_name",
    "identification",
    "troubleshooting_steps",
)


def _parse_step(raw: dict, doc_id: str) -> TroubleshootingStep:
    for field_name in _REQUIRED_STEP_FIELDS:
        if field_name not in raw:
            raise KnowledgeBaseError(
                f"{doc_id}: step {raw.get('step_id', '<unknown>')} is missing '{field_name}'."
            )

    return TroubleshootingStep(
        step_id=raw["step_id"],
        title=raw["title"],
        order=int(raw["order"]),
        applies_when=raw["applies_when"],
        customer_instruction=raw["customer_instruction"],
        expected_result=raw["expected_result"],
        prerequisites=tuple(raw.get("prerequisites", ())),
        warnings=tuple(raw.get("warnings", ())),
        # dict -> tuple of pairs so the whole step stays hashable/frozen.
        result_meanings=tuple(raw.get("result_meanings", {}).items()),
    )


def _parse_doc(raw: dict, path: Path) -> RouterDoc:
    for field_name in _REQUIRED_DOC_FIELDS:
        if field_name not in raw:
            raise KnowledgeBaseError(f"{path.name}: document is missing '{field_name}'.")

    # Policy rule 7: only approved documentation may be used. An
    # unapproved or draft document is refused outright rather than loaded
    # and filtered later, so there is no window in which its text is
    # reachable.
    if raw["approval_status"] != "approved":
        raise KnowledgeBaseError(
            f"{path.name}: approval_status is '{raw['approval_status']}', expected 'approved'. "
            "Unapproved documentation must not be used with customers."
        )

    identification = raw["identification"]
    steps = tuple(
        sorted(
            (_parse_step(s, raw["doc_id"]) for s in raw["troubleshooting_steps"]),
            key=lambda s: s.order,
        )
    )
    if not steps:
        raise KnowledgeBaseError(f"{path.name}: document defines no troubleshooting steps.")

    seen: set[str] = set()
    for step in steps:
        if step.step_id in seen:
            raise KnowledgeBaseError(f"{path.name}: duplicate step_id '{step.step_id}'.")
        seen.add(step.step_id)

    return RouterDoc(
        model_id=raw["supported_model"],
        doc_id=raw["doc_id"],
        version=str(raw["version"]),
        approval_status=raw["approval_status"],
        display_name=raw["display_name"],
        identification_question=identification.get("customer_facing_question", ""),
        identification_cues=tuple(identification.get("cues", ())),
        known_symptoms=tuple(raw.get("known_symptoms", ())),
        steps=steps,
        out_of_scope=tuple(raw.get("out_of_scope", ())),
    )


class KnowledgeBase:
    """The loaded set of approved router documents, keyed by model id.

    Model ids are matched case-insensitively ("r100", "R100") because the
    customer's phrasing reaches this lookup, but the canonical id from the
    document is always what gets stored and displayed.
    """

    def __init__(self, docs: dict[str, RouterDoc]):
        self._docs = docs

    def get_router_doc(self, model_id: str) -> Optional[RouterDoc]:
        """The document for a model, or None if we have no approved doc for
        it. None is the signal for policy rule 6 (hand over -- no approved
        instructions available), never a reason to improvise."""
        if not model_id:
            return None
        return self._docs.get(model_id.strip().upper())

    def get_steps(self, model_id: str) -> tuple[TroubleshootingStep, ...]:
        """Every approved step for a model, in documented order. Empty if
        the model is unknown."""
        doc = self.get_router_doc(model_id)
        return doc.steps if doc else ()

    def get_step(self, model_id: str, step_id: str) -> Optional[TroubleshootingStep]:
        doc = self.get_router_doc(model_id)
        return doc.step_by_id(step_id) if doc else None

    def is_supported(self, model_id: str) -> bool:
        return self.get_router_doc(model_id) is not None

    def known_models(self) -> tuple[str, ...]:
        """The models we can actually help with -- used to tell a customer
        which models are supported rather than guessing at one."""
        return tuple(sorted(self._docs))

    def identification_questions(self) -> tuple[str, ...]:
        """The approved customer-facing identification questions, used
        before any model is confirmed. These are the only model questions
        the agent may ask, so identification stays inside approved text
        too."""
        return tuple(doc.identification_question for doc in self._docs.values() if doc.identification_question)


def load_knowledge_base(docs_dir: Optional[Path] = None) -> KnowledgeBase:
    """Reads and validates every router document. Call once at startup.

    Raises KnowledgeBaseError on a missing directory, malformed JSON, a
    missing required field, an unapproved document, or duplicate model ids.
    """
    directory = docs_dir or config.ROUTER_DOCS_DIR
    if not directory.is_dir():
        raise KnowledgeBaseError(f"Router documentation directory not found: {directory}")

    paths = sorted(directory.glob("*.json"))
    if not paths:
        raise KnowledgeBaseError(f"No router documents found in {directory}")

    docs: dict[str, RouterDoc] = {}
    for path in paths:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as err:
            raise KnowledgeBaseError(f"{path.name}: invalid JSON ({err}).") from err

        doc = _parse_doc(raw, path)
        key = doc.model_id.strip().upper()
        if key in docs:
            raise KnowledgeBaseError(f"Two documents both claim model '{key}'.")
        docs[key] = doc

    return KnowledgeBase(docs)
