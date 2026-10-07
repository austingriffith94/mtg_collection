"""
Chat-mode tools: read-only functions the model can call (ADVISOR_PLAN.md
Phase 3). Each tool takes `conn` first (bound by `dispatch`, never exposed to
the model) and returns a small JSON-safe dict; a tool never raises — a bad
or unknown argument comes back as {"error": ...} so the model can recover
instead of ending the conversation.

TOOL_SPECS is the Gemini-dialect function-declaration list (same dialect as
schemas.py's responseSchema); other adapters translate it when they arrive.
"""
from .. import queries as q
from .context import lookup_card_text


def _s(**kw):
    return {"type": "STRING", **kw}


def get_card(conn, name=""):
    """Oracle text, type, mana cost and ownership for one card, matched on
    front face. The system prompt requires card text come from here, never
    from the model's memory."""
    found = lookup_card_text(conn, name)
    if found is None:
        return {"error": f"No card named '{name}' in the local database."}
    full = q.card_lookup(conn, found["name"]) or {}
    return {
        "name": found["name"], "type_line": found["type_line"], "oracle_text": found["oracle_text"],
        "mana_cost": (full.get("card") or {}).get("mana_cost"),
        "owned_qty": full.get("owned_qty", 0),
        "mainboard_in": [m["deck_name"] for m in full.get("mainboard_in", [])],
    }


def search_cards(conn, query=""):
    """Card names matching a partial or misspelled query, for when the exact
    name isn't known."""
    return {"names": q.search_card_names(conn, query, limit=20)}


def list_decks(conn):
    """Every deck's id, commander and colour identity."""
    df = q.list_decks(conn)
    return {"decks": [{"deck_id": int(r["deck_id"]), "name": r["name"], "commander": r["commander"],
                        "color_identity": r["color_identity"]} for _, r in df.iterrows()]}


def get_deck(conn, deck=""):
    """One deck's computed stats, its cards (name + quantity, no oracle
    text — call get_card for that), and owned cards not in it. `deck` may be
    a deck_id or a name (case-insensitive, exact)."""
    from .context import build_deck_context  # local: avoids a context<->tools import cycle at module load

    deck_id = None
    deck = str(deck).strip()
    if deck.isdigit():
        deck_id = int(deck)
    else:
        row = conn.execute("SELECT deck_id FROM decks WHERE name = ? COLLATE NOCASE", (deck,)).fetchone()
        if row:
            deck_id = row[0]
    if deck_id is None:
        return {"error": f"No deck named or numbered '{deck}'."}
    try:
        ctx = build_deck_context(conn, deck_id)
    except ValueError as e:
        return {"error": str(e)}
    return {
        "deck_id": ctx.deck_id, "name": ctx.meta.get("name"), "commander": ctx.meta.get("commander"),
        "color_identity": ctx.meta.get("color_identity"), "stats": ctx.stats,
        "cards": [{"name": c.name, "quantity": c.quantity, "is_commander": c.is_commander} for c in ctx.cards],
        "owned_not_in_deck": ctx.candidate_names(),
    }


TOOL_SPECS = [
    {"name": "get_card",
     "description": "Look up one card's oracle text, mana cost, type, and whether/where you own it, by name.",
     "parameters": {"type": "OBJECT", "properties": {"name": _s()}, "required": ["name"]}},
    {"name": "search_cards",
     "description": "Find card names matching a partial or misspelled query, when you don't know the exact name.",
     "parameters": {"type": "OBJECT", "properties": {"query": _s()}, "required": ["query"]}},
    {"name": "list_decks",
     "description": "List every deck in the collection with its id, commander, and colour identity.",
     "parameters": {"type": "OBJECT", "properties": {}}},
    {"name": "get_deck",
     "description": "Get one deck's computed stats, its cards, and owned cards not in it, by name or id.",
     "parameters": {"type": "OBJECT", "properties": {"deck": _s()}, "required": ["deck"]}},
]

_FUNCTIONS = {"get_card": get_card, "search_cards": search_cards, "list_decks": list_decks, "get_deck": get_deck}


def dispatch(conn, name, args):
    """Run one tool call by name; never raises (a bad call is an {"error"}
    result the model sees and can react to, not a crashed conversation)."""
    fn = _FUNCTIONS.get(name)
    if fn is None:
        return {"error": f"unknown tool '{name}'"}
    try:
        return fn(conn, **(args or {}))
    except TypeError as e:
        return {"error": f"bad arguments for {name}: {e}"}
