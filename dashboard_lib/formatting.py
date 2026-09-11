"""
Pure helper functions: card-type bucketing, color-identity display, Scryfall
URL construction, local image path resolution. No Streamlit or sqlite3
imports here on purpose, so this module (and queries.py) can be exercised
in a plain Python shell / test script without a running dashboard.
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
