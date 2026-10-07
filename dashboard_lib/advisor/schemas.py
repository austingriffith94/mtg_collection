"""
Structured-output schema for analysis mode.

Card-name fields are string enums built from the deck and the pre-filtered
candidate pool, so a provider that honours schemas cannot name a card
outside them. guard.py still re-checks (providers differ in how strictly
they enforce enums). Written in Gemini's responseSchema dialect (uppercase
type names); other adapters translate it.
"""
from .context import ROLES

_CLAIMABLE_ROLES = [r for r in ROLES if r != "land"]


def _s(**kw):
    return {"type": "STRING", **kw}


def analysis_schema(ctx):
    """JSON schema for one deck-doctor answer, enum-constrained to `ctx`."""
    deck = ctx.deck_names()
    cands = ctx.candidate_names()
    roles = {"type": "ARRAY", "items": _s(enum=_CLAIMABLE_ROLES)}
    cut = {
        "type": "OBJECT",
        "properties": {
            "card": _s(enum=deck),
            "reason": _s(),
            "evidence_quote": _s(description="Exact substring of the card's oracle text supporting the reason."),
            "roles": {**roles, "description": "Roles this card fills, as you read its text."},
        },
        "required": ["card", "reason", "evidence_quote", "roles"],
    }
    add = {
        "type": "OBJECT",
        "properties": {
            "card": _s(enum=cands or ["(none)"]),
            "replaces": _s(enum=deck),
            "reason": _s(),
            "evidence_quote": _s(description="Exact substring of the added card's oracle text."),
            "roles": roles,
        },
        "required": ["card", "replaces", "reason", "evidence_quote", "roles"],
    }
    return {
        "type": "OBJECT",
        "properties": {
            "summary": _s(description="Under 120 words. No card names not in the lists."),
            "strengths": {"type": "ARRAY", "items": _s()},
            "weaknesses": {"type": "ARRAY", "items": _s()},
            "cuts": {"type": "ARRAY", "items": cut},
            "adds": {"type": "ARRAY", "items": add},
        },
        "required": ["summary", "strengths", "weaknesses", "cuts", "adds"],
    }


def chat_answer_schema():
    """JSON schema for restating a finished chat turn (chat.RESTATE_PROMPT)
    so its card mentions can be checked. No enum on `card`: chat is
    open-ended, so there is no small pre-filtered pool to constrain names
    the way analysis_schema does. guard.check_chat_answer instead checks
    each mention's name, quote and claimed roles against the whole database
    (context.lookup_card_text, context.mechanical_roles)."""
    return {
        "type": "OBJECT",
        "properties": {
            "answer": _s(description="Your last answer to the user, unchanged."),
            "mentions": {
                "type": "ARRAY",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "card": _s(),
                        "evidence_quote": _s(description="Exact substring of that card's oracle text you "
                                                           "quoted or relied on; empty string if none."),
                        "roles": {"type": "ARRAY", "items": _s(enum=_CLAIMABLE_ROLES),
                                  "description": "Roles (ramp/draw/removal/...) you claimed this card fills; "
                                                  "empty if you made no such claim."},
                    },
                    "required": ["card", "evidence_quote", "roles"],
                },
            },
        },
        "required": ["answer", "mentions"],
    }
