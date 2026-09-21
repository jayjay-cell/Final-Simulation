"""Tool 1: record the customer's router model, confirmed or not.

Policy rules 1 and 2 live here. Model-specific instructions require the
customer to have explicitly stated their model, and a request to assume or
skip that ("just assume it's an R200") is not a statement.

The gate is the `customer_stated_model` parameter. The model must assert
that the customer actually said it, and the tool records confirmation only
on that assertion. An inference, a guess, or a customer's permission to
guess all arrive with customer_stated_model=False and leave
state.model_confirmed untouched.

This does not rely on catching bypass phrasings -- an unrecognised
paraphrase still cannot set confirmation, because confirmation comes from
the flag, not from the text. The phrase check only lets the tool return a
more useful message back to the agent.
"""

from __future__ import annotations

from langchain_core.tools import tool

from core.knowledge_base import KnowledgeBase
from core.models import TroubleshootingState
from core.policy import looks_like_bypass_request


def build_confirm_router_model_tool(state: TroubleshootingState, kb: KnowledgeBase):
    """Builds the tool bound to this conversation's live state.

    The state is closed over rather than passed as a parameter, so there is
    no field in the tool schema the model could use to describe a different
    conversation's state.
    """

    @tool
    def confirm_router_model(model_id: str, customer_stated_model: bool) -> dict:
        """Record which router model the customer has.

        Call this when the customer tells you their router model, or when
        they describe it clearly enough to name it (for example the sticker
        text, or how many lights are on the front).

        Args:
            model_id: The model, e.g. "R100" or "R200".
            customer_stated_model: True ONLY if the customer themselves
                stated or described the model. Set False if you are
                inferring it, guessing, or the customer asked you to assume
                a model without telling you which one they have. Saying
                "just assume it's an R200" is NOT the customer stating
                their model.

        Returns a dict with `ok`, and on success the confirmed model and
        its documentation reference.
        """
        cleaned = (model_id or "").strip().upper()

        if not cleaned:
            return {
                "ok": False,
                "code": "ERR_NO_MODEL_GIVEN",
                "message": "No model was provided. Ask the customer what their router model is.",
            }

        # Remember what was mentioned either way. An unconfirmed claim is
        # still worth keeping -- it goes in the handover summary, clearly
        # marked as unconfirmed.
        state.claimed_model = cleaned

        if not customer_stated_model:
            state.model_confirmation_attempts += 1
            return {
                "ok": False,
                "code": "ERR_MODEL_NOT_CONFIRMED_BY_CUSTOMER",
                "claimed_model": cleaned,
                "message": (
                    f"Recorded '{cleaned}' as unconfirmed. You may not give model-specific "
                    "instructions yet. A customer asking you to assume or guess a model is not "
                    "confirmation. Ask them to check the router itself: the R100 has a single "
                    "round light on the front and a sticker underneath; the R200 has four lights "
                    "in a row and a sticker on the back."
                ),
            }

        if not kb.is_supported(cleaned):
            state.model_confirmation_attempts += 1
            return {
                "ok": False,
                "code": "ERR_UNSUPPORTED_MODEL",
                "claimed_model": cleaned,
                "supported_models": list(kb.known_models()),
                "message": (
                    f"No approved troubleshooting documentation exists for '{cleaned}'. "
                    f"Approved models are: {', '.join(kb.known_models())}. "
                    "Do not adapt another model's instructions. Hand over to a human representative."
                ),
            }

        doc = kb.get_router_doc(cleaned)
        state.claimed_model = doc.model_id
        state.model_confirmed = True

        return {
            "ok": True,
            "confirmed_model": doc.model_id,
            "display_name": doc.display_name,
            "documentation": f"{doc.doc_id} v{doc.version}",
            "steps_available": len(doc.steps),
            "message": (
                f"Model confirmed as {doc.model_id} ({doc.display_name}). "
                "You may now request troubleshooting steps."
            ),
        }

    return confirm_router_model


def build_identification_help_tool(state: TroubleshootingState, kb: KnowledgeBase):
    """Companion read-only tool: the approved way to ask "which model?".

    Kept separate from confirm_router_model so that asking how to identify
    a router never touches state. It returns only approved text from the
    documentation, so identification questions stay inside approved
    wording too (policy rule 7).
    """

    @tool
    def get_model_identification_help() -> dict:
        """Get the approved questions for helping a customer identify their
        router model. Call this when the customer does not know their model,
        is unsure, or asks you to guess it.

        Returns the approved identifying cues for each supported model.
        """
        state.model_confirmation_attempts += 1

        models = []
        for model_id in kb.known_models():
            doc = kb.get_router_doc(model_id)
            models.append(
                {
                    "model_id": doc.model_id,
                    "display_name": doc.display_name,
                    "ask_the_customer": doc.identification_question,
                    "cues": list(doc.identification_cues),
                }
            )

        return {
            "ok": True,
            "supported_models": models,
            "attempts_so_far": state.model_confirmation_attempts,
            "message": (
                "Use these approved questions to help the customer identify their router. "
                "Do not guess the model on their behalf, and do not proceed with "
                "model-specific steps until they confirm it."
            ),
        }

    return get_model_identification_help
