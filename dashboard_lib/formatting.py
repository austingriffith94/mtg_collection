"""
Pure helper functions: card-type bucketing, color-identity display, Scryfall
URL construction, local image path resolution, and (Phase 3) MDFC-aware
land detection plus a keyword/text rule parser for mana-source color
classification. No Streamlit or sqlite3 imports here on purpose, so this
module (and queries.py) can be exercised in a plain Python shell / test
script without a running dashboard.
"""
import os
import re

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Priority order used to bucket a (sometimes multi-type) type_line into one
# primary category for grouping/filtering. Checked top-to-bottom; first match
# wins. "Land" and "Creature" are checked ahead of "Artifact"/"Enchantment"
# so e.g. "Artifact Creature" buckets as Creature and "Enchantment Land"
# (rare, but exists) buckets as Land, matching how most players talk about
# their decks' type breakdowns.
CARD_TYPE_ORDER = [
    "Land",
    "Creature",
    "Planeswalker",
    "Battle",
    "Instant",
    "Sorcery",
    "Artifact",
    "Enchantment",
]
CARD_TYPE_OTHER = "Other"
ALL_CARD_TYPES = CARD_TYPE_ORDER + [CARD_TYPE_OTHER]

WUBRG_ORDER = ["W", "U", "B", "R", "G"]
COLORLESS_LABEL = "Colorless"


def derive_card_type(type_line):
    """Bucket a Scryfall type_line into one primary category.

    MDFCs store both faces separated by ' // ' (e.g. 'Land // Instant') —
    only the front face is used for bucketing, matching how the card behaves
    as a mainboard slot by default.
    """
    if not type_line:
        return CARD_TYPE_OTHER
    front = str(type_line).split(" // ")[0]
    for t in CARD_TYPE_ORDER:
        if t in front:
            return t
    return CARD_TYPE_OTHER


def _order_colors(colors_iterable):
    colors = {c for c in colors_iterable if c in WUBRG_ORDER}
    ordered = [c for c in WUBRG_ORDER if c in colors]
    return "/".join(ordered) if ordered else COLORLESS_LABEL


def color_identity_display(ci_str):
    """CARD-level color_identity: comma-separated (e.g. 'B,R', as written
    by scryfall_lookup.py's to_card_row). 'B,R' -> 'B/R'; '' or None ->
    'Colorless'."""
    if not ci_str:
        return COLORLESS_LABEL
    colors = {c.strip() for c in str(ci_str).split(",") if c.strip()}
    return _order_colors(colors)


def deck_color_identity_letters(ci_str):
    """DECK-level color_identity is a different format from card-level:
    decks.color_identity comes straight from deck_mapping.csv's 'Color'
    column as concatenated WUBRG letters with NO separator (e.g. 'RB',
    'GUW'), not comma-separated like cards.color_identity. Returns the
    individual letters found, in WUBRG order (e.g. 'RB' -> ['R','B'] in
    WUBRG order would be ['R','B']... concretely 'GUW' -> ['G','U','W']).
    Tolerates commas/slashes/spaces too, in case that ever changes."""
    if not ci_str:
        return []
    cleaned = str(ci_str).upper().replace(",", "").replace("/", "").replace(" ", "")
    letters = {ch for ch in cleaned if ch in WUBRG_ORDER}
    return [c for c in WUBRG_ORDER if c in letters]


def deck_color_identity_display(ci_str):
    """DECK-level color_identity -> 'B/R' (or 'Colorless')."""
    return _order_colors(deck_color_identity_letters(ci_str))


# ------------------------------------------------------------------
# Phase 3 — MDFC-aware land detection & mana-source color classification.
#
# Scryfall's `type_line` for a Modal Double-Faced Card combines both faces
# with " // " (e.g. "Bala Ged Sanctuary // Bala Ged Recovery" style cards
# report type_line as "Land // Sorcery" or similar). derive_card_type()
# above deliberately buckets by the FRONT face only, for display purposes
# (a "how do I mostly think of this card" bucket). The functions below
# look at BOTH faces, because a card with a land on either side needs to
# be recognized as a land for mana-curve/land-ratio/probability math even
# when its front face is a spell.
# ------------------------------------------------------------------
NON_LAND_TYPES = {
    "Creature", "Planeswalker", "Battle", "Instant", "Sorcery",
    "Artifact", "Enchantment", "Kindred", "Tribal",
}


def _face_primary_types(face):
    """The primary type words of one face's type line (before the
    em-dash that introduces subtypes), as a token set — e.g.
    'Legendary Creature — Human Wizard' -> {'Legendary', 'Creature'}."""
    primary = str(face).split("—")[0]
    return set(primary.split())


def has_land_face(type_line):
    """True if ANY face of this card (front or back, split on ' // ')
    is a Land — the MDFC-aware check used for mana-curve exclusion, land
    counts, and color-source detection (Phase 3). A card with Land on
    either side counts as a land for these purposes even if its front
    face (what derive_card_type buckets by) is a spell."""
    if not type_line:
        return False
    for face in str(type_line).split(" // "):
        if "Land" in _face_primary_types(face):
            return True
    return False


def is_pure_land(type_line):
    """True only if EVERY face is Land and nothing but Land (plain basics,
    utility lands like 'Land — Desert', 'Legendary Land', etc.) — used to
    exclude pure lands from the Land Probability page's "Check a specific
    card" filter (Phase 3). A land-having MDFC such as 'Instant // Land'
    is NOT a pure land (its other face is castable), so it stays in that
    tool's list even though has_land_face() above is True for it."""
    if not type_line:
        return False
    for face in str(type_line).split(" // "):
        tokens = _face_primary_types(face)
        if tokens & NON_LAND_TYPES:
            return False
        if "Land" not in tokens:
            return False
    return True


# Phrases that mark a card as an "any color" mana source regardless of
# what its own `color_identity` field says. This matters because cards
# like Command Tower or City of Brass have NO colored mana symbols in
# their own text — their ability only reads "of any color" — so Scryfall
# reports an empty color_identity for them even though they fix for every
# color. Checked case-insensitively against oracle_text.
_ANY_COLOR_PHRASES = (
    "of any color",
    "any one color",
    "mana of any color",
)


def produces_any_color(oracle_text):
    """True if the card's own oracle text says it can add mana of any
    color (Command Tower, City of Brass, Exotic Orchard, Birds of
    Paradise, etc.) — see _ANY_COLOR_PHRASES above."""
    if not oracle_text:
        return False
    text = str(oracle_text).lower()
    return any(phrase in text for phrase in _ANY_COLOR_PHRASES)


_MANA_SYMBOL_RE = re.compile(r"\{([wubrgc])\}", re.IGNORECASE)


def mana_symbols_in_add_text(oracle_text):
    """Mana symbols ({W}/{U}/{B}/{R}/{G}/{C}) found specifically inside
    'Add ...' sentences of the given oracle text — e.g. 'Add {C}.' ->
    {'c'}, '{T}: Add {W} or {U}.' -> {'w', 'u'}. Scoped to Add-sentences
    on purpose (rather than scanning the whole oracle text) so an
    unrelated colored activation cost elsewhere in the text isn't
    mistaken for a mana ability. Returns an empty set if the text has no
    'Add ...' sentence at all."""
    if not oracle_text:
        return set()
    add_clauses = re.findall(r"[Aa]dd[^.]*\.", str(oracle_text))
    if not add_clauses:
        return set()
    scan_text = " ".join(add_clauses)
    return {m.lower() for m in _MANA_SYMBOL_RE.findall(scan_text)}


def classify_mana_colors(type_line, oracle_text, color_identity):
    """The Phase 3 keyword/text rule parser for mana-source color
    classification. Returns {'any': bool, 'colors': set(...), 'colorless':
    bool} describing what a card's mana ability can produce:

      1) 'any color' phrasing (Command Tower, City of Brass, Exotic
         Orchard, ...) always wins, even over an empty color_identity —
         this is exactly the gap those three named examples expose.
      2) Otherwise, colored mana symbols found in the card's own 'Add ...'
         text set the specific colors (covers basics, duals, triomes,
         filter lands, mana rocks/dorks with a plain colored ability).
      3) Otherwise, a bare {C} in the 'Add ...' text marks it Colorless
         (e.g. Access Tunnel) — reserved for cards that produce
         EXCLUSIVELY generic mana, per the Phase 3 spec.
      4) Otherwise, fall back to the card's own color_identity field
         (covers unusual wordings this parser's regex doesn't catch), and
         failing that, Colorless as the default.
    """
    if produces_any_color(oracle_text):
        return {"any": True, "colors": set(WUBRG_ORDER), "colorless": False}

    symbols = mana_symbols_in_add_text(oracle_text)
    colors = {s.upper() for s in symbols if s.upper() in WUBRG_ORDER}
    if colors:
        return {"any": False, "colors": colors, "colorless": False}

    if "c" in symbols:
        return {"any": False, "colors": set(), "colorless": True}

    ci_colors = {
        c.strip() for c in str(color_identity or "").split(",")
        if c.strip() in WUBRG_ORDER
    }
    if ci_colors:
        return {"any": False, "colors": ci_colors, "colorless": False}

    return {"any": False, "colors": set(), "colorless": True}


def is_mana_rock_or_dork(type_line, oracle_text):
    """True for a NONLAND Artifact or Creature with a mana ability of its
    own — the 'Mana Rocks / Mana Dorks' bucket in the Phase 3 weighted
    color-availability graph, kept separate from land-based sources.
    Lands (including MDFC land faces) are excluded even if they'd
    otherwise match, since has_land_face() already covers those."""
    if has_land_face(type_line):
        return False
    front_types = _face_primary_types(str(type_line or "").split(" // ")[0])
    if not (front_types & {"Artifact", "Creature"}):
        return False
    if produces_any_color(oracle_text):
        return True
    return bool(mana_symbols_in_add_text(oracle_text))


def scryfall_card_url(set_code, collector_number):
    """Build a Scryfall card-page URL from set code + collector number.

    Scryfall's site resolves the bare '/card/<set>/<number>' path (no name
    slug needed) and redirects to the fully-slugged URL, so this is stable
    without needing to store scryfall_uri separately in the DB.
    """
    if not set_code or collector_number in (None, ""):
        return None
    return f"https://scryfall.com/card/{str(set_code).strip().lower()}/{str(collector_number).strip()}"


def resolve_local_image(local_image_path):
    """Return an absolute path if the cached image file actually exists on
    disk, else None (so callers can fall back to the remote image_uri)."""
    if not local_image_path:
        return None
    abs_path = os.path.join(BASE_DIR, local_image_path)
    return abs_path if os.path.isfile(abs_path) else None


def format_money(value):
    if value is None:
        return "—"
    try:
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return "—"


def split_multi_value(cell):
    """'Ramp,Card Adv' -> ['Ramp', 'Card Adv']; handles None/NaN/empty."""
    if cell is None:
        return []
    text = str(cell)
    if not text or text.lower() == "nan":
        return []
    return [p.strip() for p in text.split(",") if p.strip()]


def safe_filename(name, max_length=150):
    """Sanitize an arbitrary string (e.g. a deck name) into a filename
    that's safe across Windows/Mac/Linux: strips characters reserved on
    Windows (< > : " / \\ | ? *), control characters, and trailing
    dots/spaces (which Windows silently drops, causing subtle mismatches
    between what was requested and what actually got written)."""
    if not name:
        return "untitled"
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", str(name))
    cleaned = cleaned.strip().rstrip(". ")
    cleaned = cleaned[:max_length].strip()
    return cleaned or "untitled"
