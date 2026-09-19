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


def mana_symbol_svg_url(letter):
    """Scryfall's own official mana-symbol SVG for one WUBRG letter (or
    'C' for colorless) — Prompt Pass 10 / prompt3.txt's "official MTG
    mana symbols" ask, the actual symbol set shown across Scryfall/
    Gatherer mana costs, not a custom recreation (colored dots/emoji).
    Hotlinked directly from Scryfall's public svgs.scryfall.io CDN, the
    same "just link to Scryfall's own asset, don't reinvent it" pattern
    this app already uses for card art itself (cards.image_uri renders
    as a live <img> hotlink whenever no local cache exists — see
    card_view._image_src()/_deck_image_src()). Unlike card art, there is
    no local-cache path for these symbols at all (no
    local_image_path-style column, no sync step) — rendering one always
    needs a live network connection; a missing connection just shows a
    broken-image icon in the browser, the same graceful-non-crash
    fallback already accepted for card art. Returns None for anything
    that isn't a WUBRG letter or 'C'."""
    letter = str(letter or "").strip().upper()
    if letter not in WUBRG_ORDER and letter != "C":
        return None
    return f"https://svgs.scryfall.io/card-symbols/{letter}.svg"


def deck_color_identity_symbol_urls(ci_str):
    """Official Scryfall mana-symbol SVG URLs for a DECK-level
    color_identity string, in WUBRG order (Prompt Pass 10 / prompt3.txt).
    A colorless deck (no WUBRG letters found in ci_str) still gets one
    URL back — the Colorless ({C}) symbol — rather than an empty list,
    so the deck header always has at least one badge to show."""
    letters = deck_color_identity_letters(ci_str) or ["C"]
    return [u for u in (mana_symbol_svg_url(l) for l in letters) if u]


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


# ------------------------------------------------------------------
# Mana curve chart formatting (Prompt Pass 4) — fixed category order and
# custom colors for the Decks page's "stack by Color" mana curve, so the
# legend always reads W, U, B, R, G, Multi-color, Colorless regardless of
# whatever order pandas happened to discover the color_display values in,
# and each bucket gets a recognizable, consistent color across sessions.
# ------------------------------------------------------------------
MANA_CURVE_COLOR_ORDER = ["W", "U", "B", "R", "G", "Multi-color", "Colorless"]

MANA_CURVE_COLOR_HEX = {
    "W": "#F8E7B9",   # White
    "U": "#0E68AB",   # Blue
    "B": "#150B00",   # Black
    "R": "#D3202A",   # Red
    "G": "#00733E",   # Green
    "Multi-color": "#B8860B",  # Dark yellow (goldenrod)
    "Colorless": "#9E9E9E",    # Grey
}


def mana_curve_color_bucket(color_display):
    """Collapse a card_view-derived `color_display` value ('W', 'B/R',
    'W/U/B', 'Colorless', ...) into one of the seven fixed mana-curve
    buckets: a single WUBRG letter, 'Multi-color' for anything with more
    than one color, or 'Colorless'."""
    if not color_display or color_display == COLORLESS_LABEL:
        return COLORLESS_LABEL
    return "Multi-color" if "/" in str(color_display) else str(color_display)


# ------------------------------------------------------------------
# Deck-page themed color accents (Prompt Pass 10 / prompt3.txt) — reuses
# the exact same WUBRG hex values as the mana curve chart
# (MANA_CURVE_COLOR_HEX above) so a deck's accent color always matches
# its own mana-curve bars elsewhere on the same page, rather than
# inventing a second, different color mapping.
# ------------------------------------------------------------------
def deck_accent_hex(ci_str):
    """A single representative hex color for a deck's color identity —
    its first color in WUBRG order, or MANA_CURVE_COLOR_HEX['Colorless']
    if colorless. For accents that need one flat color (e.g. a solid CSS
    border) as opposed to deck_accent_gradient() below."""
    letters = deck_color_identity_letters(ci_str)
    return MANA_CURVE_COLOR_HEX[letters[0]] if letters else MANA_CURVE_COLOR_HEX["Colorless"]


def deck_accent_gradient(ci_str):
    """A CSS linear-gradient() string spanning a deck's own actual
    color-identity colors, for page-wide "themed highlights/borders"
    (Prompt Pass 10 / prompt3.txt) — e.g. a Rakdos (B/R) deck gets an
    actual black-to-red gradient, not the flat 'Multi-color' goldenrod
    the mana curve chart uses for ITS legend (that flattening exists
    there only to keep an unbounded number of color *combinations* from
    exploding into one bar color per combo in a chart legend — a page
    accent stripe has no such constraint, so showing the deck's REAL
    colors is strictly more informative, hence not reusing that bucket
    color here). A mono-color or colorless deck's single hex is repeated
    as two identical stops so the returned string is always a valid
    multi-stop gradient — callers never need to special-case a flat
    color separately from an actual multi-color gradient."""
    letters = deck_color_identity_letters(ci_str)
    hexes = [MANA_CURVE_COLOR_HEX[l] for l in letters] if letters else [MANA_CURVE_COLOR_HEX["Colorless"]]
    if len(hexes) == 1:
        hexes = hexes * 2
    return f"linear-gradient(90deg, {', '.join(hexes)})"


# ------------------------------------------------------------------
# Weighted mana-source-availability chart colors (Prompt Pass 12 /
# prompt5.txt) — the Land Probability page's "Weighted mana-source
# availability" graph (probability.MANA_SOURCE_CATEGORIES: Rocks/Dorks,
# Colorless, Any Color, W, U, B, R, G) used to render with Streamlit's
# default auto-assigned categorical palette, which had no relationship to
# the WUBRG colors used everywhere else in the app (MANA_CURVE_COLOR_HEX
# above, the Decks page's mana curve and color-identity accents). The
# W/U/B/R/G entries here are pulled directly from MANA_CURVE_COLOR_HEX
# (not re-typed as separate hex literals) so the two palettes can never
# drift out of alignment; "Colorless" reuses that same dict's grey and
# "Any Color" its goldenrod Multi-color swatch, since an any-color source
# is conceptually the mana-curve chart's "Multi-color" bucket. "Rocks/
# Dorks" (nonland mana rocks/dorks, formatting.is_mana_rock_or_dork) gets
# its own dedicated purple, per the prompt's explicit ask for a new color
# that doesn't overlap White/Blue/Black/Red/Green.
# ------------------------------------------------------------------
MANA_SOURCE_COLOR_HEX = {
    "Rocks/Dorks": "#9B5DE5",  # dedicated purple — non-land mana sources
    "Colorless": MANA_CURVE_COLOR_HEX["Colorless"],
    "Any Color": MANA_CURVE_COLOR_HEX["Multi-color"],
    "W": MANA_CURVE_COLOR_HEX["W"],
    "U": MANA_CURVE_COLOR_HEX["U"],
    "B": MANA_CURVE_COLOR_HEX["B"],
    "R": MANA_CURVE_COLOR_HEX["R"],
    "G": MANA_CURVE_COLOR_HEX["G"],
}


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
    if not isinstance(set_code, str) or not set_code.strip():
        return None
    if collector_number is None or not str(collector_number).strip() or str(collector_number).strip().lower() == "nan":
        return None
    return f"https://scryfall.com/card/{set_code.strip().lower()}/{str(collector_number).strip()}"


def resolve_local_image(local_image_path):
    """Return an absolute path if the cached image file actually exists on
    disk, else None (so callers can fall back to the remote image_uri).

    Guards against a pandas quirk: when a whole column loaded via
    pd.read_sql_query is NULL for every row (e.g. no deck has a custom
    cover image yet), pandas types that column as float64 and represents
    each missing value as NaN — a float, not None. `bool(float('nan'))`
    is True in Python, so a plain `if not local_image_path:` check does
    NOT catch it, and the NaN would reach os.path.join() below and raise
    a TypeError. Requiring an actual non-blank str here (same style as
    split_multi_value()'s NaN handling above) catches NaN, None, and any
    other unexpected type in one place, since every caller routes
    through this function."""
    if not isinstance(local_image_path, str) or not local_image_path.strip():
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


# ------------------------------------------------------------------
# Win-rate conditional-formatting scale (Prompt Pass 9 / prompt2.txt) —
# used by the Commander Game Tracking page's Win-by-Deck summary,
# Win-by-Player summary, and Head-to-Head matrix. A straight 50/50 split
# doesn't fit a 4-player free-for-all pod (see player_elo_ratings()'s own
# docstring in queries.py, which makes the same point for ELO) — with 4
# equally-matched seats, a "fair" win rate is 1 in 4, so that's the
# neutral anchor for this diverging scale instead of the usual 50%.
# Below the anchor shades toward red (underperforming that baseline);
# above shades toward blue (overperforming it); intensity scales with
# distance from the anchor, reaching full color at the 0%/100% extremes.
# Applied identically (same anchor, same two colors) to all three tables
# per the prompt's "color logic applies consistently" instruction — see
# PROJECT_STATE.md for the note on the Head-to-Head table specifically,
# where a pure 1-on-1 reading might otherwise suggest a 50% anchor.
# ------------------------------------------------------------------
WIN_RATE_BASELINE = 0.25  # "fair" win rate for a 4-player Commander pod
WIN_RATE_NEUTRAL_HEX = "#FFFFFF"
WIN_RATE_RED_HEX = "#C0392B"    # full-intensity "below baseline" color
WIN_RATE_BLUE_HEX = "#2E86C1"   # full-intensity "above baseline" color


def _blend_hex(hex_a, hex_b, t):
    """Linear-interpolate two '#RRGGBB' colors; t=0.0 -> hex_a, t=1.0 ->
    hex_b, clamped to [0, 1] for any t outside that range."""
    t = max(0.0, min(1.0, t))
    a = [int(hex_a[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(hex_b[i:i + 2], 16) for i in (1, 3, 5)]
    blended = [round(a[i] + (b[i] - a[i]) * t) for i in range(3)]
    return "#{:02X}{:02X}{:02X}".format(*blended)


def win_rate_background_color(value, baseline=WIN_RATE_BASELINE):
    """Diverging red/white/blue background color for a win-rate `value`
    (0.0-1.0), anchored at `baseline`. Below the anchor blends from white
    toward WIN_RATE_RED_HEX (0% win rate = full red); above blends from
    white toward WIN_RATE_BLUE_HEX (100% = full blue); exactly at the
    anchor is neutral white. `value` is clamped into [0, 1] first, so an
    out-of-range float still resolves to some point on the scale rather
    than being rejected. Returns None (meaning: leave the cell unstyled)
    for None, NaN, or anything that can't be read as a float — callers
    treat a None return as a no-op style."""
    if value is None:
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if value != value:  # NaN != NaN is the standard no-import NaN check
        return None
    value = max(0.0, min(1.0, value))
    if value < baseline:
        t = (baseline - value) / baseline if baseline else 0.0
        return _blend_hex(WIN_RATE_NEUTRAL_HEX, WIN_RATE_RED_HEX, t)
    if value > baseline:
        span = 1.0 - baseline
        t = (value - baseline) / span if span else 0.0
        return _blend_hex(WIN_RATE_NEUTRAL_HEX, WIN_RATE_BLUE_HEX, t)
    return WIN_RATE_NEUTRAL_HEX


def win_rate_cell_style(value, baseline=WIN_RATE_BASELINE):
    """CSS `background-color: #RRGGBB` declaration for `value` (see
    win_rate_background_color()), or '' for a value that should stay
    unstyled — '' rather than None specifically because this is meant to
    be handed straight to a pandas Styler, which expects an empty string
    (not None) for a no-op cell style."""
    color = win_rate_background_color(value, baseline)
    return f"background-color: {color}" if color else ""


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
