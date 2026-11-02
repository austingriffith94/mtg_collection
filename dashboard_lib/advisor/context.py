"""
Grounding layer, part 1: builds the facts the model is allowed to talk about.

Everything here is computed from the database in plain Python — counts,
curve, colour pips, role tallies, the candidate pool — so the model only
interprets and prioritises numbers it was handed. Model-independent: the
same context goes to every provider.

Roles ("ramp", "draw", "removal", ...) come from conservative oracle-text
heuristics in `mechanical_roles`. They are a cross-check, not ground truth:
guard.py flags a model claim that disagrees with them, and the UI says so.

`build_tag_context` is the lighter counterpart for tag proposals: the deck's
cards, one layer's vocabulary and current tags, and no candidate pool.

The deck's own notes (win conditions, strengths, weaknesses, themes) and its
cached Commander Spellbook combos are loaded here too. They steer which
candidates are offered (`candidate_pool`) and feed guard.py's warnings.
"""
import math
import re
from collections import Counter
from dataclasses import dataclass, field

import pandas as pd

from .. import queries as q

ROLES = ("ramp", "draw", "removal", "wipe", "tutor", "counter", "land")

# Cap on how many cards are offered as add-candidates. The schema turns the
# pool into an enum and the prompt lists every entry, so it must stay small.
DEFAULT_CANDIDATE_CAP = 60

# Of those, how many slots can go to cards one piece short of a combo. The
# rest alternate between deck fit and role-filling (see _select_candidates).
COMBO_CANDIDATE_SLOTS = 10

# Brackets (decks.bracket, the user's own score) where a two-card combo is
# generally against the bracket's spirit: completers get no slots here and
# guard.py warns if the model suggests one anyway. Advisory, never a block.
LOW_COMBO_BRACKETS = (1, 2)

# Most Game Changers a deck of this bracket should run. No entry = no limit.
GAME_CHANGER_LIMITS = {1: 0, 2: 0, 3: 3}

# Most tags the model may give a card in one layer.
MAX_TAGS_PER_CARD = 2

# Label pairs a card may not carry together (the definitions say "never both").
EXCLUSIVE_LABELS = [("Removal", "Interaction")]

# Weight of a commander's / the deck notes' words in deck_profile, relative
# to one card in the deck: they state the plan, so one mention counts as
# several cards.
COMMANDER_WORD_WEIGHT = 5
NOTES_WORD_WEIGHT = 3

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
_WORD = re.compile(r"[a-z][a-z'-]{2,}")
_REMINDER_OR_SYMBOL = re.compile(r"\([^)]*\)|\{[^}]*\}")


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
    game_changer: bool = False
    # Candidates only: the 'almost' combos (context dicts) this card finishes.
    completes_combos: list = field(default_factory=list)
    fit: float = 0.0              # Candidates only: deck_profile score
    is_basic: bool = False
    scryfall_ids: list = field(default_factory=list)   # the printings behind this entry


@dataclass
class DeckContext:
    deck_id: int
    meta: dict
    cards: list          # CardFact, mainboard (commander included, flagged)
    candidates: list     # CardFact, owned cards not in the deck, capped
    stats: dict          # computed facts shown to the user as "Computed"
    goals: dict = field(default_factory=dict)    # deck_goals()
    combos: list = field(default_factory=list)   # deck_combo_facts()

    def deck_names(self, include_commander=False):
        return [c.name for c in self.cards if include_commander or not c.is_commander]

    def candidate_names(self):
        return [c.name for c in self.candidates]

    @property
    def low_combo_bracket(self):
        return self.stats.get("bracket") in LOW_COMBO_BRACKETS

    def included_combos(self):
        return [c for c in self.combos if c["category"] == "included"]


@dataclass
class TagContext:
    deck_id: int
    meta: dict
    tag_type: str
    vocabulary: list     # [{"label", "definition"}], the labels offered to the model
    cards: list          # CardFact, mainboard without basics, one entry per card name
    current: dict        # {card name: [label, ...]} in this layer, primary first

    def card_names(self):
        return [c.name for c in self.cards]

    def labels(self):
        return [v["label"] for v in self.vocabulary]


def _records(df):
    """DataFrame -> list of dicts with SQL NULLs as None (pandas yields NaN,
    which is truthy and breaks every `value or default` below)."""
    return df.astype(object).where(df.notna(), None).to_dict("records")


def _card_from_row(r, **kw):
    return CardFact(
        name=r["name"], oracle_id=r.get("oracle_id") or "", type_line=r.get("type_line") or "",
        mana_cost=r.get("mana_cost") or "", cmc=float(r.get("cmc") or 0),
        oracle_text=r.get("oracle_text") or "",
        price=r.get("current_price_usd"), game_changer=bool(r.get("is_game_changer")),
        is_basic=bool(r.get("is_basic_land")), scryfall_ids=[r["scryfall_id"]],
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
        "game_changers": sum(1 for c in cards if c.game_changer),
        "total_price_usd": round(sum((c.price or 0) * c.quantity for c in cards), 2),
        "games_played": (win_row or {}).get("games_played", 0),
        "win_rate": (win_row or {}).get("win_rate"),
    }


def deck_goals(conn, deck_id):
    """The user's own notes on the deck: ranked win conditions, strengths and
    weaknesses as "Label: Description" strings, and themes by role."""
    def lines(kind):
        rows = q.deck_rank_list(conn, deck_id, kind)
        text = (": ".join(x for x in (r["label"], r["description"]) if x) for r in rows)
        return [t for t in text if t]
    themes = q.deck_themes_list(conn, deck_id)
    return {
        "win_conditions": lines("win_conditions"),
        "strengths": lines("strengths"),
        "weaknesses": lines("weaknesses"),
        "main_themes": [t["theme"] for t in themes if t["role"] == "main"],
        "sub_themes": [t["theme"] for t in themes if t["role"] == "sub"],
    }


def deck_combo_facts(conn, deck_id):
    """Cached Spellbook combos as dicts, most popular first: `uses`/`missing`
    as card-name lists, `uses_keys`/`missing_keys` as front_face keys.
    Commander-illegal combos are left out."""
    def split(text):
        return [x.strip() for x in (text or "").split("\n") if x.strip()]
    out = []
    for category in ("included", "almost"):
        for r in _records(q.deck_combos(conn, deck_id, category)):
            if r["commander_legal"] == 0 or r["bracket_tag"] == "Banned":
                continue
            uses, missing = split(r["uses"]), split(r["missing"])
            out.append({
                "category": category, "uses": uses, "missing": missing,
                "uses_keys": {front_face(n) for n in uses},
                "missing_keys": {front_face(n) for n in missing},
                "produces": split(r["produces"]), "bracket_tag": r["bracket_tag"],
                "popularity": r["popularity"] or 0,
            })
    return out


def _words(card):
    """A card's rules vocabulary: lowercase words of its type line and oracle
    text, without reminder text, mana symbols or its own name."""
    text = _REMINDER_OR_SYMBOL.sub(" ", f"{card.type_line} {card.oracle_text}".lower())
    return set(_WORD.findall(text)) - set(_WORD.findall(card.name.lower()))


def deck_profile(cards, pool, goals=None):
    """{word: weight} for the words that mark this deck out from the owned
    cards it could draw on. A word's weight is how much more common it is in
    the deck than in the pool (log ratio), so "party" or "cleric" score in a
    Nalia deck while "creature" and "target" cancel out with no stop-word
    list. The commander and the user's notes count extra. Lands are left out:
    their text says little about the plan."""
    deck_counts = Counter()
    nonland = [c for c in cards if "land" not in c.roles]
    for c in nonland:
        for w in _words(c):
            deck_counts[w] += COMMANDER_WORD_WEIGHT if c.is_commander else 1
    goals = goals or {}
    notes = goals.get("win_conditions", []) + goals.get("main_themes", []) + goals.get("sub_themes", [])
    for w in set(_WORD.findall(" ".join(notes).lower())):
        deck_counts[w] += NOTES_WORD_WEIGHT
    pool_counts = Counter(w for c in pool for w in _words(c))
    profile = {}
    for w, n in deck_counts.items():
        # A word seen on one ordinary card is a coincidence, not a plan.
        if n < 2:
            continue
        lift = ((n + .5) / (len(nonland) + 1)) / ((pool_counts[w] + .5) / (len(pool) + 1))
        if lift > 1:
            profile[w] = math.log(lift)
    return profile


def _fit(card, profile):
    """Sum of the card's profile words, damped so a wall of text doesn't win
    on length alone."""
    words = _words(card)
    return sum(profile.get(w, 0) for w in words) / math.sqrt(max(len(words), 8))


def _role_order(c):
    return not c.available, not (c.roles - {"land"}), "land" in c.roles, c.cmc, c.name


def _select_candidates(pool, combo_cards, cap):
    """Pick `cap` cards: combo completers first (capped), then taking turns
    from two lists: best deck fit (synergy pieces, which often fill no role)
    and the role order (cheap ramp/draw/removal the skills lean on when a
    role is thin). Using only one list would starve the other kind of card."""
    chosen = {id(c): c for c in combo_cards[:min(COMBO_CANDIDATE_SLOTS, cap)]}
    by_fit = sorted(pool, key=lambda c: (not c.available, -c.fit, c.cmc, c.name))
    by_role = sorted(pool, key=_role_order)
    for pair in zip(by_fit, by_role):
        for c in pair:
            if len(chosen) < cap:
                chosen[id(c)] = c
    return sorted(chosen.values(), key=lambda c: (not c.available, -c.fit, c.cmc, c.name))


def candidate_pool(conn, meta, in_deck_keys, cap=DEFAULT_CANDIDATE_CAP, cards=(), goals=None, combos=()):
    """Owned, commander-legal, colour-identity-legal cards not already in the
    deck, then narrowed to `cap` (see _select_candidates): free copies first,
    mixing cards that fit this deck with cards that fill a role, plus cards
    that finish a combo unless the bracket is low. Pre-filtering in code is
    what lets the schema enum the pool."""
    deck_ci = _identity(meta.get("color_identity"))
    deck_names = {r[0] for r in conn.execute("SELECT name FROM decks")}
    df = pd.read_sql_query(
        """SELECT c.scryfall_id, c.oracle_id, c.name, c.type_line, c.mana_cost, c.cmc,
                  c.color_identity, c.oracle_text, c.current_price_usd, c.is_game_changer,
                  col.location
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
    by_name = {front_face(c.name): c for c in out}
    for combo in combos:
        # One card short only: anything further is a project, not a swap.
        if combo["category"] == "almost" and len(combo["missing_keys"]) == 1:
            card = by_name.get(next(iter(combo["missing_keys"])))
            if card:
                card.completes_combos.append(combo)
    profile = deck_profile(cards, out, goals)
    for card in out:
        card.fit = _fit(card, profile)
    combo_cards = [] if meta.get("bracket") in LOW_COMBO_BRACKETS else sorted(
        (c for c in out if c.completes_combos),
        key=lambda c: (not c.available, -max(x["popularity"] for x in c.completes_combos), c.name))
    return _select_candidates(out, combo_cards, cap)


def _deck_card_facts(conn, deck_id, meta):
    """The deck's mainboard as CardFacts, one per printing."""
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
    return cards


def build_deck_context(conn, deck_id, candidate_cap=DEFAULT_CANDIDATE_CAP):
    """Everything the deck-doctor skill needs for one deck."""
    meta = q.deck_meta(conn, deck_id)
    if not meta:
        raise ValueError(f"No deck with id {deck_id}")
    cards = _deck_card_facts(conn, deck_id, meta)
    in_deck = {c.oracle_id or c.name for c in cards}
    goals = deck_goals(conn, deck_id)
    combos = deck_combo_facts(conn, deck_id)
    candidates = candidate_pool(conn, meta, in_deck, candidate_cap, cards, goals, combos)
    stats = compute_stats(meta, cards, q.deck_stats_row(conn, deck_id))
    return DeckContext(deck_id, meta, cards, candidates, stats, goals, combos)


def build_tag_context(conn, deck_id, tag_type):
    """What a tag proposal needs for one deck and layer. Basic lands are
    skipped, the same as untagged_deck_cards. Tags are keyed by printing but
    the model sees names, so several printings of a card become one entry
    carrying all their tags."""
    meta = q.deck_meta(conn, deck_id)
    if not meta:
        raise ValueError(f"No deck with id {deck_id}")
    tags = q.get_card_tags_by_type(conn, deck_id, tag_type)
    by_name, current = {}, {}
    for c in _deck_card_facts(conn, deck_id, meta):
        if c.is_basic:
            continue
        labels = current.setdefault(c.name, [])
        labels += [t for t in tags.get(c.scryfall_ids[0], []) if t not in labels]
        kept = by_name.get(c.name)
        if kept:
            kept.quantity += c.quantity
            kept.scryfall_ids += c.scryfall_ids
        else:
            by_name[c.name] = c
    return TagContext(deck_id, meta, tag_type, q.deck_tag_vocabulary(conn, deck_id, tag_type, defined_only=True),
                      list(by_name.values()), {name: t for name, t in current.items() if t})
