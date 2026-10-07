"""
Grounding layer, part 1: builds the facts the model is allowed to talk about.

Everything here is computed from the database in plain Python — counts,
curve, colour pips, role tallies, the candidate pool — so the model only
interprets and prioritises numbers it was handed. Model-independent: the
same context goes to every provider.

Roles ("ramp", "draw", "removal", ...) come from conservative oracle-text
heuristics in `mechanical_roles`. They are a cross-check, not ground truth:
guard.py flags a model claim that disagrees with them, and the UI says so.
"""
import re
from dataclasses import dataclass, field

import pandas as pd

from .. import queries as q

ROLES = ("ramp", "draw", "removal", "wipe", "tutor", "counter", "land")

# Cap on how many cards are offered as add-candidates. The schema turns the
# pool into an enum and the prompt lists every entry, so it must stay small.
DEFAULT_CANDIDATE_CAP = 60

_LANDS = r"(?:land|forest|island|swamp|mountain|plains)"
_MANA_ADD = re.compile(r"\badd (?:an additional )?(?:\{[^}]+\})+|\badd (?:one|two|three) mana", re.I)
_LAND_FETCH = re.compile(
    rf"search your library for (?:a|up to \w+) (?:basic )?(?:\w+ )?{_LANDS}"
    r"|put (?:a|up to \w+) (?:basic )?land cards? .{0,40}onto the battlefield", re.I)
_DRAW = re.compile(r"\bdraws? (?:a|an|two|three|four|five|x|\d+) (?:additional )?cards?\b", re.I)
_REMOVAL = re.compile(
    r"\b(?:destroy|exile) target\b|deals? (?:\d+|x) damage to (?:any target|target)"
    r"|target (?:creature|permanent|player) (?:gets|loses) -\d"
    r"|\bsacrifices? (?:a|target) (?:creature|permanent)", re.I)
_WIPE = re.compile(r"\b(?:destroy|exile) all\b|\ball (?:other )?creatures get -", re.I)
_TUTOR = re.compile(rf"search your library for (?:a|an|up to \w+) (?!(?:basic )?(?:\w+ )?{_LANDS})", re.I)
_COUNTER = re.compile(r"\bcounter target\b", re.I)

_PIP = re.compile(r"\{([^}]+)\}")


def front_face(name):
    """Casefolded front-face name, the key used for every name comparison
    (the DB stores MDFCs as "Front // Back" but people and models say one face)."""
    return (name or "").split(" // ")[0].strip().casefold()


def lookup_card_text(conn, name):
    """(name, type_line, oracle_text) for the database's representative
    printing of `name`, matching the front face so MDFCs resolve regardless
    of which face is named (the DB stores them as "Front // Back"). None if
    nothing matches. Shared by tools.get_card (chat mode) and
    guard.check_chat_answer so both resolve a bare name the same way."""
    name = (name or "").strip()
    if not name:
        return None
    row = conn.execute(
        "SELECT name, type_line, oracle_text FROM cards "
        "WHERE name = ? COLLATE NOCASE OR name LIKE ? COLLATE NOCASE LIMIT 1",
        (name, f"{name} // %")).fetchone()
    return dict(zip(("name", "type_line", "oracle_text"), row)) if row else None


def mechanical_roles(oracle_text, type_line):
    """Set of ROLES the card plausibly fills, from its own text. Lands are
    only 'land' (a mana-producing land is not counted as ramp)."""
    if "Land" in (type_line or ""):
        return {"land"}
    t = oracle_text or ""
    roles = set()
    if _MANA_ADD.search(t) or _LAND_FETCH.search(t):
        roles.add("ramp")
    if _DRAW.search(t):
        roles.add("draw")
    if _WIPE.search(t):
        roles.add("wipe")
    if _REMOVAL.search(t):
        roles.add("removal")
    if _TUTOR.search(t):
        roles.add("tutor")
    if _COUNTER.search(t):
        roles.add("counter")
    return roles


def _identity(text):
    """'B,R' / 'BR' / None -> {'B','R'}."""
    return set(re.findall(r"[WUBRG]", text or ""))


@dataclass
class CardFact:
    name: str
    oracle_id: str
    type_line: str
    mana_cost: str
    cmc: float
    oracle_text: str
    quantity: int = 1
    price: float | None = None
    roles: set = field(default_factory=set)
    is_commander: bool = False
    # Candidates only: where the owned copies sit ("Box", another deck's name...)
    locations: list = field(default_factory=list)
    available: bool = True


@dataclass
class DeckContext:
    deck_id: int
    meta: dict
    cards: list          # CardFact, mainboard (commander included, flagged)
    candidates: list     # CardFact, owned cards not in the deck, capped
    stats: dict          # computed facts shown to the user as "Computed"

    def deck_names(self, include_commander=False):
        return [c.name for c in self.cards if include_commander or not c.is_commander]

    def candidate_names(self):
        return [c.name for c in self.candidates]


def _records(df):
    """DataFrame -> list of dicts with SQL NULLs as None (pandas yields NaN,
    which is truthy and breaks every `value or default` below)."""
    return df.astype(object).where(df.notna(), None).to_dict("records")


def _card_from_row(r, **kw):
    return CardFact(
        name=r["name"], oracle_id=r.get("oracle_id") or "", type_line=r.get("type_line") or "",
        mana_cost=r.get("mana_cost") or "", cmc=float(r.get("cmc") or 0),
        oracle_text=r.get("oracle_text") or "",
        price=r.get("current_price_usd"),
        roles=mechanical_roles(r.get("oracle_text"), r.get("type_line")), **kw)


def compute_stats(meta, cards, win_row=None):
    """Counts, curve, pips and role tallies for a deck's cards (quantity-weighted)."""
    total = sum(c.quantity for c in cards)
    lands = sum(c.quantity for c in cards if "land" in c.roles)
    curve = {}
    pips = {k: 0 for k in "WUBRG"}
    roles = {r: 0 for r in ROLES}
    cmc_sum = nonland = 0
    for c in cards:
        for r in c.roles:
            roles[r] += c.quantity
        if "land" in c.roles:
            continue
        nonland += c.quantity
        cmc_sum += c.cmc * c.quantity
        bucket = "7+" if c.cmc >= 7 else str(int(c.cmc))
        curve[bucket] = curve.get(bucket, 0) + c.quantity
        for sym in _PIP.findall(c.mana_cost):
            # Hybrid symbols ({W/U}) count toward each colour they can pay.
            for color in set(sym.split("/")) & set(pips):
                pips[color] += c.quantity
    return {
        "total_cards": total, "lands": lands, "nonland": nonland,
        "avg_cmc": round(cmc_sum / nonland, 2) if nonland else 0.0,
        "curve": dict(sorted(curve.items())), "pips": {k: v for k, v in pips.items() if v},
        "roles": {k: v for k, v in roles.items() if k != "land"},
        "bracket": meta.get("bracket"), "color_identity": meta.get("color_identity"),
        "total_price_usd": round(sum((c.price or 0) * c.quantity for c in cards), 2),
        "games_played": (win_row or {}).get("games_played", 0),
        "win_rate": (win_row or {}).get("win_rate"),
    }


def candidate_pool(conn, meta, in_deck_keys, cap=DEFAULT_CANDIDATE_CAP):
    """Owned, commander-legal, colour-identity-legal cards not already in the
    deck, ranked so cards that fill a role and are free to use come first,
    then capped. Pre-filtering in code is what lets the schema enum the pool."""
    deck_ci = _identity(meta.get("color_identity"))
    deck_names = {r[0] for r in conn.execute("SELECT name FROM decks")}
    df = pd.read_sql_query(
        """SELECT c.scryfall_id, c.oracle_id, c.name, c.type_line, c.mana_cost, c.cmc,
                  c.color_identity, c.oracle_text, c.current_price_usd, col.location
           FROM collection col JOIN cards c ON c.scryfall_id = col.scryfall_id
           WHERE c.commander_legal = 1 AND c.is_basic_land = 0 AND c.oracle_text IS NOT NULL""",
        conn)
    pool = {}
    for r in _records(df):
        key =r["oracle_id"] or r["name"]
        if key in in_deck_keys or not _identity(r["color_identity"]) <= deck_ci:
            continue
        card = pool.get(key) or _card_from_row(r)
        loc = (r["location"] or "").strip()
        if loc:
            card.locations.append(loc)
        pool[key] = card
    out = list(pool.values())
    for card in out:
        # Free = some copy is unlocated or sits somewhere other than a tracked deck.
        card.available = not card.locations or any(l not in deck_names for l in card.locations)
    out.sort(key=lambda c: (not c.available, not (c.roles - {"land"}), "land" in c.roles, c.cmc, c.name))
    return out[:cap]


def build_deck_context(conn, deck_id, candidate_cap=DEFAULT_CANDIDATE_CAP):
    """Everything the deck-doctor skill needs for one deck."""
    meta = q.deck_meta(conn, deck_id)
    if not meta:
        raise ValueError(f"No deck with id {deck_id}")
    df = q.deck_cards_dataframe(conn, deck_id)
    oracle = {}
    if len(df):
        marks = ",".join("?" * len(df))
        oracle = dict(conn.execute(
            f"SELECT scryfall_id, oracle_id FROM cards WHERE scryfall_id IN ({marks})",
            list(df["scryfall_id"])).fetchall())
    commanders = {front_face(n) for n in (meta.get("commander"), meta.get("partner")) if n}
    cards = []
    for r in _records(df):
        r["oracle_id"] = oracle.get(r["scryfall_id"])
        cards.append(_card_from_row(r, quantity=int(r["quantity"] or 1),
                                    is_commander=front_face(r["name"]) in commanders))
    in_deck = {c.oracle_id or c.name for c in cards}
    candidates = candidate_pool(conn, meta, in_deck, candidate_cap)
    stats = compute_stats(meta, cards, q.deck_stats_row(conn, deck_id))
    return DeckContext(deck_id, meta, cards, candidates, stats)
