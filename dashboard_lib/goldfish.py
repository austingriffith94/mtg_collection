"""Goldfish payload + mana parsing (docs/GOLDFISH_PLAN.md, Phase 1).

The Goldfish page lets you play out a deck's first turns alone, entirely in
the browser. This module is the Python half: it turns a deck into the plain
dicts the component receives and does every bit of text parsing, so the JS
never reads a mana cost or an oracle text and the parsing is unit-tested.

  - goldfish_payload(): PURE. Turns the deck dataframe into
    {"commanders": [...], "library": [...], "warnings": [...]}. Cards are sent
    once with a `qty` (the component expands copies, since each copy taps
    separately). No Streamlit and no I/O beyond card_view.card_image_src's
    cached-image mtime. Unit-tested.
  - parse_cost(): a mana cost string -> a cost dict, or None when it can't be
    read (the card then gets no castable mark).
  - mana_abilities(): one face's oracle text -> the mana abilities it taps for.
  - enters_tapped(): one face's oracle text -> "always" / "maybe" / None.

Every text rule is applied PER FACE. `type_line` and `oracle_text` hold both
faces joined by " // ", and the back land's "Add {B}" must never be read as
the front spell's. The `cards` table stores one `mana_cost` per card, so a
multi-face card whose `mana_cost` is NULL has no readable cost on any face.

The game itself (shuffle, draw, tap, undo) lives in the component; Python
never hears back from it. Nothing here writes to the database.
"""
import re

from dashboard_lib import card_view, formatting as fmt
from dashboard_lib.deck_board import _clean_str, _front_face, _split_csv

FACE_SEP = " // "
COLORS = ("W", "U", "B", "R", "G")
NO_COMMANDER_WARNING = "No card matched the commander on this deck; playing without a command zone."
MIN_LIBRARY = 7

# ------------------------------------------------------------------
# Costs
# ------------------------------------------------------------------
_SYMBOL_RE = re.compile(r"\{([^}]+)\}")


def parse_cost(mana_cost):
    """A mana cost string ('{2}{G}{G}') as
    {"generic": n, "W": n, "U": n, "B": n, "R": n, "G": n, "C": n,
     "hybrid": [["W", "U"]], "phyrexian": ["B"], "x": bool},
    or None when blank or when any symbol can't be read (snow, {2/W}), so the
    caller never marks a spell castable on a guess."""
    text = _clean_str(mana_cost)
    if not text:
        return None
    if _SYMBOL_RE.sub("", text).strip():
        return None
    cost = {"generic": 0, **{c: 0 for c in COLORS}, "C": 0, "hybrid": [], "phyrexian": [], "x": False}
    for symbol in _SYMBOL_RE.findall(text):
        symbol = symbol.upper()
        parts = symbol.split("/")
        if symbol.isdigit():
            cost["generic"] += int(symbol)
        elif symbol in ("X", "Y", "Z"):
            cost["x"] = True
        elif symbol in COLORS or symbol == "C":
            cost[symbol] += 1
        elif parts[-1] == "P" and parts[0] in COLORS:
            # {W/P} and hybrid-Phyrexian {G/W/P}: 2 life always pays, so the
            # pip never blocks a cast; the first color is all that is kept.
            cost["phyrexian"].append(parts[0])
        elif len(parts) == 2 and all(p in COLORS for p in parts):
            cost["hybrid"].append(parts)
        else:
            return None
    return cost


# ------------------------------------------------------------------
# Mana abilities
# ------------------------------------------------------------------
_NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6}
_MANA_WORD_RE = re.compile(r"\b(one|two|three|four|five|six)\s+mana\b", re.IGNORECASE)
_RUN_RE = re.compile(r"(?:\{[WUBRGC]\})+", re.IGNORECASE)
_ALTERNATIVE_SPLIT_RE = re.compile(r",\s*or\s+|,\s*|\s+or\s+", re.IGNORECASE)
# Wording that makes an amount depend on the game state.
_VARIABLE_AMOUNT_RE = re.compile(
    r"for each|equal to|\bX\b|that much|twice|an amount|the number of|\{X\}", re.IGNORECASE)


def _ability_amount(effect):
    """Mana one tap makes, from the effect's 'Add ...' sentence: the largest
    run of adjacent symbols in any 'or' alternative ('Add {G} or {W}' is 1,
    'Add {C}{C}' is 2), or a number word ('one mana of any color'). None when
    the amount can't be read ('for each Swamp', 'X mana')."""
    if _VARIABLE_AMOUNT_RE.search(effect):
        return None
    amount = 0
    for alternative in _ALTERNATIVE_SPLIT_RE.split(effect):
        for run in _RUN_RE.findall(alternative):
            amount = max(amount, run.count("{"))
    word = _MANA_WORD_RE.search(effect)
    if word:
        amount = max(amount, _NUMBER_WORDS[word.group(1).lower()])
    return amount or None


def mana_abilities(oracle_text):
    """The tap-for-mana abilities in ONE face's oracle text, as a list of
    {"colors": [...], "any": bool, "amount": n | None}; [] when there are none.

    An ability is a line shaped '<cost>: Add ...' whose cost includes {T}
    (reminder-text parentheses are stripped, so a basic's '({T}: Add {G}.)'
    counts). Only the 'Add' sentence is read, so the 'Talisman deals 1 damage
    to you' after it is ignored. A mana payment in the cost (filter lands,
    Cabal Coffers) or an amount that depends on the board makes `amount` None,
    which the component shows as '?'. 'Spend this mana only to ...' is read as
    normal mana. Identical abilities are listed once."""
    abilities = []
    for line in str(oracle_text or "").replace("(", "").replace(")", "").split("\n"):
        cost, sep, effect = line.partition(":")
        match = re.match(r"\s*Add\b[^.]*", effect)
        if not sep or not match or "{T}" not in cost:
            continue
        sentence = match.group(0)
        colors = []
        for symbol in re.findall(r"\{([WUBRGC])\}", sentence, re.IGNORECASE):
            if symbol.upper() not in colors:
                colors.append(symbol.upper())
        paid = _SYMBOL_RE.search(cost.replace("{T}", ""))
        ability = {
            "colors": colors,
            "any": fmt.produces_any_color(sentence),
            "amount": None if paid else _ability_amount(sentence),
        }
        if ability not in abilities:
            abilities.append(ability)
    return abilities


# ------------------------------------------------------------------
# Entering tapped
# ------------------------------------------------------------------
_ENTERS_TAPPED_RE = re.compile(r"\benters?\b(?: the battlefield)?(?: under your control)? tapped", re.IGNORECASE)
_CONDITION_RE = re.compile(r"\b(unless|if|you may pay)\b", re.IGNORECASE)


def enters_tapped(oracle_text):
    """'always' when ONE face's text says it enters tapped with no condition,
    'maybe' when only a conditional version appears (check lands, shock
    lands, 'unless you control ...'), else None. 'maybe' lands are played
    untapped and carry a badge; the player taps them if the condition holds."""
    state = None
    for sentence in re.split(r"(?<=[.\n])\s+", str(oracle_text or "")):
        if not _ENTERS_TAPPED_RE.search(sentence):
            continue
        if not _CONDITION_RE.search(sentence):
            return "always"
        state = "maybe"
    return state


# ------------------------------------------------------------------
# Faces
# ------------------------------------------------------------------
def face_kind(type_line):
    """What a face does when clicked in hand: 'land', 'creature', 'permanent'
    (artifact, enchantment, planeswalker, battle) or 'spell' (instant,
    sorcery). Land wins over creature (Dryad Arbor), creature over artifact."""
    types = fmt._face_primary_types(type_line)
    if "Land" in types:
        return "land"
    if "Creature" in types:
        return "creature"
    if types & {"Artifact", "Enchantment", "Planeswalker", "Battle"}:
        return "permanent"
    if types & {"Instant", "Sorcery"}:
        return "spell"
    return "permanent"


def _split_faces(value, count):
    """A ' // '-joined column as `count` parts, padded with '' if short."""
    parts = (_clean_str(value) or "").split(FACE_SEP)
    return (parts + [""] * count)[:count]


def card_faces(row):
    """One dict per face: name, type_line, kind, cost, mana, enters_tapped.
    A one-face card has one face. `cost` is None on every face of a card whose
    `mana_cost` is NULL (the multi-face case), or per face if the stored cost
    is itself ' // '-joined."""
    type_lines = (_clean_str(row.get("type_line")) or "").split(FACE_SEP)
    count = len(type_lines)
    names = _split_faces(row.get("name"), count)
    texts = _split_faces(row.get("oracle_text"), count)
    mana_cost = _clean_str(row.get("mana_cost")) or ""
    costs = mana_cost.split(FACE_SEP) if count > 1 and FACE_SEP in mana_cost else [mana_cost] * count
    if len(costs) != count:
        costs = [None] * count

    faces = []
    for name, type_line, text, cost in zip(names, type_lines, texts, costs):
        kind = face_kind(type_line)
        mana = None
        if kind == "land" or (fmt.is_mana_rock_or_dork(type_line, text) and kind != "spell"):
            mana = mana_abilities(text) or None
        faces.append({
            "name": name or _clean_str(row.get("name")) or "Unknown card",
            "type_line": type_line,
            "kind": kind,
            "cost": parse_cost(cost),
            "mana": mana,
            "enters_tapped": enters_tapped(text) if kind == "land" else None,
        })
    return faces


# ------------------------------------------------------------------
# Payload
# ------------------------------------------------------------------
def goldfish_payload(cards_df, deck_meta, layer_tags=None):
    """The mainboard split into the command zone and the library, for the
    component: {"commanders": [card], "library": [card], "warnings": [str]}.

    cards_df   : loaders.load_deck_cards_df() (card columns + quantity +
                 strategy_tags); a raw deck_cards_dataframe() also works.
    deck_meta  : loaders.load_deck_meta() — commander / partner names,
                 matched on the front face, lowercase, like the deck board.
    layer_tags : queries.deck_tag_layers() — {tag_type: {scryfall_id: [labels]}};
                 each card carries `layers` like the board's, `tags` is the
                 strategy layer.

    Each card is one dict with a `qty` (30 Forests is one entry); the
    component expands copies. Commanders come out in commander, partner order;
    the library is in cmc then name order (the component shuffles it).
    Warnings are for the page to show, not raise."""
    if cards_df is None or cards_df.empty:
        return {"commanders": [], "library": [], "warnings": []}

    layer_tags = layer_tags or {}
    wanted = [_front_face(n) for n in (deck_meta.get("commander"), deck_meta.get("partner")) if _clean_str(n)]

    cards = []
    for _, row in cards_df.iterrows():
        qty = row.get("quantity")
        cmc = row.get("cmc")
        name = _clean_str(row.get("name")) or "Unknown card"
        cards.append({
            "id": row["scryfall_id"],
            "name": name,
            "qty": int(qty) if qty is not None and qty == qty else 1,
            "cmc": float(cmc) if cmc is not None and cmc == cmc else 0.0,
            "img": card_view.card_image_src(row),
            "faces": card_faces(row),
            "is_commander": _front_face(name) in wanted,
            "tags": _split_csv(row.get("strategy_tags")),
            "layers": {t: list(by_card.get(row["scryfall_id"], [])) for t, by_card in layer_tags.items()},
        })

    commanders = sorted((c for c in cards if c["is_commander"]), key=lambda c: wanted.index(_front_face(c["name"])))
    library = sorted((c for c in cards if not c["is_commander"]), key=lambda c: (c["cmc"], c["name"].lower()))

    warnings = []
    if not commanders:
        warnings.append(NO_COMMANDER_WARNING)
    library_size = sum(c["qty"] for c in library)
    if library_size < MIN_LIBRARY:
        warnings.append(f"The library has only {library_size} card{'s' if library_size != 1 else ''} "
                        f"(fewer than {MIN_LIBRARY}); an opening hand will be short.")
    return {"commanders": commanders, "library": library, "warnings": warnings}
