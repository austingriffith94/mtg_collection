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
from urllib.parse import quote

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Streamlit serves this folder at app/static/ (server.enableStaticServing in
# .streamlit/config.toml). Stored image paths ('image_cache/<id>.jpg',
# 'image_cache/deck_covers/<id>.png') are relative to it — Phase 4 of
# UI_ACTION_PLAN.md moved the cache here so grids can link images by URL.
STATIC_DIR = os.path.join(BASE_DIR, "static")
STATIC_URL_PREFIX = "app/static/"

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
    card_view.card_image_src()/deck_image_src()). Unlike card art, there is
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

# UI plan Phase 6: the one canonical mana palette. Every chart that splits
# by color reads from here (directly or via the aliases below), so a "Red"
# bar always means red mana. Non-color categories use CATEGORY_COLORS
# instead, which deliberately avoids these hues.
MANA_COLORS = {
    "W": "#F8E7B9",      # White
    "U": "#0E68AB",      # Blue
    "B": "#150B00",      # Black
    "R": "#D3202A",      # Red
    "G": "#00733E",      # Green
    "C": "#9E9E9E",      # Colorless (grey)
    "Multi": "#B8860B",  # Multicolor (goldenrod)
}

# Palette for categories that are NOT mana colors (card types, tags, ...).
# Muted teals/violets/ambers/slates, picked to stay clear of W/U/B/R/G.
CATEGORY_COLORS = [
    "#8E6BBF", "#E0903A", "#3AA6A0", "#C06C9B",
    "#8C6D4F", "#6F7FA8", "#B5A642", "#5E8C8C",
]

MANA_CURVE_COLOR_HEX = {
    "W": MANA_COLORS["W"],
    "U": MANA_COLORS["U"],
    "B": MANA_COLORS["B"],
    "R": MANA_COLORS["R"],
    "G": MANA_COLORS["G"],
    "Multi-color": MANA_COLORS["Multi"],
    "Colorless": MANA_COLORS["C"],
}


def category_colors(n):
    """The first n CATEGORY_COLORS, cycling if a chart has more series."""
    return [CATEGORY_COLORS[i % len(CATEGORY_COLORS)] for i in range(n)]


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
    """Return an absolute path (stored paths are relative to static/) if the
    cached image file actually exists on disk, else None (so callers can
    fall back to the remote image_uri).

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
    abs_path = os.path.join(STATIC_DIR, local_image_path)
    return abs_path if os.path.isfile(abs_path) else None


def local_image_url(local_image_path):
    """Browser URL for a cached image under static/, or None if the file
    isn't on disk (callers fall back to the remote Scryfall URL). Relative
    ('app/static/...', no leading slash) as Streamlit's docs use it, so it
    resolves against the app's own URL. The ?v=<mtime> suffix changes when
    the file is replaced in place (a new deck cover at the same
    deck_covers/<id>.png), so the browser doesn't keep the old one cached;
    the static route ignores the query string."""
    abs_path = resolve_local_image(local_image_path)
    if not abs_path:
        return None
    rel = os.path.relpath(abs_path, STATIC_DIR).replace(os.sep, "/")
    return f"{STATIC_URL_PREFIX}{quote(rel)}?v={int(os.path.getmtime(abs_path))}"


def static_relative_path(abs_path):
    """The value to store for a file saved under static/ (e.g. a new deck
    cover): relative to static/, forward slashes on every OS."""
    return os.path.relpath(abs_path, STATIC_DIR).replace(os.sep, "/")


def format_money(value):
    if value is None:
        return "—"
    try:
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return "—"


# What the move log's `reason` (writes.MOVE_REASONS) reads as.
MOVE_REASON_LABELS = {
    "deck_edit": "deck edit", "apply": "planned change applied", "reconcile": "reconcile",
    "dismantle": "dismantle", "collection_edit": "collection edit", "import": "order import",
    "prune": "pruned", "deck_delete": "deck deleted", "purchase": "purchase",
    "build": "built the deck",
}


def format_collection_move(move):
    """One collection_moves row (queries.collection_moves_for_card's dict) as
    a line: "2026-10-13  ×2 Box → Prosper (deck edit)". A None side means no
    copy there ("new copy record" / "deleted"); '' means no Location was
    recorded."""
    def place(value, none_text):
        return none_text if value is None else (value or "no location")

    qty = move.get("quantity")
    count = f"×{qty} " if qty else ""
    when = (move.get("moved_at") or "").split(" ")[0]
    # Phase 7: a basic land's step has no lot; its empty side is the supply.
    supply = "basic land supply"
    route = (f"{place(move.get('from_location'), supply)} → {place(move.get('to_location'), supply)}"
             if move.get("bulk") else
             f"{place(move.get('from_location'), 'new copy record')} → {place(move.get('to_location'), 'deleted')}")
    reason = MOVE_REASON_LABELS.get(move.get("reason"), move.get("reason") or "")
    return f"{when}  {count}{route} ({reason})"


def split_multi_value(cell):
    """'Ramp,Card Adv' -> ['Ramp', 'Card Adv']; handles None/NaN/empty."""
    if cell is None:
        return []
    text = str(cell)
    if not text or text.lower() == "nan":
        return []
    return [p.strip() for p in text.split(",") if p.strip()]


# ------------------------------------------------------------------
# Win-rate conditional-formatting scale — used by the Commander Game
# Tracking page's Win-by-Deck summary, Win-by-Player summary, and
# Head-to-Head matrix. A straight 50/50 split doesn't fit a 4-player
# free-for-all pod (see player_elo_ratings()'s own docstring in
# queries.py, which makes the same point for ELO) — with 4 equally-
# matched seats, a "fair" win rate is 1 in 4, so the bucket thresholds
# below sit around that 25% mark rather than around 50%.
#
# Originally a continuous red/white/blue blend anchored at 25% (Prompt
# Pass 9 / prompt2.txt); replaced with three flat buckets for
# readability — a continuous gradient makes it hard to eyeball which
# side of "fair" a given cell falls on at a glance, where a flat color
# reads instantly. Below 20% is red (clearly underperforming); 20-35%
# is amber (roughly at the fair 25% rate); above 35% is green (clearly
# overperforming). All three are dark enough to keep this dashboard's
# white cell text readable — a true bright yellow can't hit that
# contrast, hence "amber" rather than a lighter yellow.
# ------------------------------------------------------------------
WIN_RATE_LOW_THRESHOLD = 0.20   # below this: red
WIN_RATE_HIGH_THRESHOLD = 0.35  # above this: green; in between: amber
WIN_RATE_RED_HEX = "#C0392B"
WIN_RATE_YELLOW_HEX = "#8F7300"
WIN_RATE_GREEN_HEX = "#1E8449"


def win_rate_background_color(value, low=WIN_RATE_LOW_THRESHOLD, high=WIN_RATE_HIGH_THRESHOLD):
    """Bucketed red/amber/green background color for a win-rate `value`
    (0.0-1.0): below `low` is WIN_RATE_RED_HEX, above `high` is
    WIN_RATE_GREEN_HEX, and everything in between (inclusive of both
    thresholds) is WIN_RATE_YELLOW_HEX. `value` is clamped into [0, 1]
    first, so an out-of-range float still resolves to a bucket rather
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
    if value < low:
        return WIN_RATE_RED_HEX
    if value > high:
        return WIN_RATE_GREEN_HEX
    return WIN_RATE_YELLOW_HEX


def win_rate_cell_style(value, low=WIN_RATE_LOW_THRESHOLD, high=WIN_RATE_HIGH_THRESHOLD):
    """CSS `background-color: #RRGGBB` declaration for `value` (see
    win_rate_background_color()), or '' for a value that should stay
    unstyled — '' rather than None specifically because this is meant to
    be handed straight to a pandas Styler, which expects an empty string
    (not None) for a no-op cell style."""
    color = win_rate_background_color(value, low, high)
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


# ------------------------------------------------------------------
# Where a copy came from / went (docs/archive/EDIT_FLOW_PLAN.md Phase 2) — the words the
# source picker, the Board and History share, so one choice reads the same
# everywhere. Pure: they take queries.card_sources()'s dict / a change row.
# ------------------------------------------------------------------
def auto_source_label(sources):
    """The picker's first option, saying what Auto would do right now:
    "Auto — Box", "Auto — already sleeved here", "Auto — new copy record
    (not owned)". Plain "Auto" when the card isn't known yet."""
    auto = (sources or {}).get("auto") or {}
    kind = auto.get("kind")
    if kind == "bulk":
        return "Basic land — nothing moves"
    if kind == "here":
        return "Auto — already sleeved here"
    if kind == "storage":
        return f"Auto — {auto.get('location') or 'a copy with no Location'}"
    if kind == "new":
        return "Auto — new copy record (not owned)"
    return "Auto"


# A deck as a source (docs/archive/EDIT_FLOW_PLAN.md Phase 3) is shown with this prefix
# wherever sources share one list of plain strings (a data_editor column), so
# "Deck: Gisa" can't be mistaken for a storage box called Gisa.
DECK_SOURCE_PREFIX = "Deck: "


def deck_source_label(deck_name):
    return f"{DECK_SOURCE_PREFIX}{deck_name}"


def deck_choice_label(opt):
    """A deck in the source picker, from queries.card_sources()["decks"]:
    "Deck: Gisa — 1 sleeved", or why it can't give a copy right now (the
    write explains in full if it's picked anyway)."""
    if opt.get("quantity", 0) >= opt.get("listed", 0) > 0:
        return f"{deck_source_label(opt['deck'])} — {opt['quantity']} sleeved"
    return f"{deck_source_label(opt['deck'])} — on its list, not recorded as sleeved"


# Buy (docs/archive/EDIT_FLOW_PLAN.md Phase 4) is the last choice of every picker and the
# word a data_editor cell holds for it. It needs no Location and no card
# lookup, so it is always on offer — "keep Gisa's, buy a second" included.
BUY_SOURCE_LABEL = "🛒 Buy"

# Why a card is on the Workbench buy list (queries.buy_list()'s `reason`).
BUY_REASON_LABELS = {
    "awaiting": "On a deck's list, no copy yet",
    "chosen": "Marked Buy",
    "contention": "Contention shortfall",
    "idea": "Idea (unowned)",
}


def source_choices(sources, locations, decks=()):
    """[(label, source, source_location), ...] for the source picker, Auto
    first. With a known card (`sources` = queries.card_sources()) only the
    storage Locations that hold a free copy are offered, with counts, then
    (Phase 3) each other deck whose list runs it; with no card yet (the add
    bar before a name is typed, the Shortlist form) every storage Location
    in `locations` and every deck name in `decks` is, and the write itself
    says if one can't supply it. Buy (Phase 4) is always the last choice —
    except for a basic land (Phase 7), which gets only the one "nothing
    moves" entry: there's no choice to make."""
    choices = [(auto_source_label(sources), None, None)]
    if sources is not None and sources.get("bulk"):
        return choices   # Phase 7: a basic land has nowhere to come from
    if sources is None:
        choices += [(loc, "storage", loc) for loc in locations]
        choices += [(deck_source_label(d), "deck", d) for d in decks]
    else:
        choices += [(f"{opt['location']} — {opt['quantity']} free", "storage", opt["location"])
                    for opt in sources.get("storage") or []]
        choices += [(deck_choice_label(opt), "deck", opt["deck"])
                    for opt in sources.get("decks") or [] if opt.get("listed")]
    choices.append((BUY_SOURCE_LABEL, "buy", None))
    return choices


def apply_result_line(result, verb="Applied"):
    """One writes.apply_changes() result as a "✅ ..." / "❌ ..." line, for
    the Shortlist tab and the Workbench. Reads every shape a planned row can
    have: a swap, a pure add or cut (a dismantle's halves), and (Phase 3) a
    take from another deck, applied as one pair."""
    name = result.get("add_name") or result.get("remove_name") or "That change"
    if not result.get("ok"):
        return f"❌ {name}: {result.get('error')}"
    pair = result.get("pair")
    if pair:
        if pair.get("held"):
            return (f"✅ {pair['card']} joins {pair['to_deck']}'s list; {pair['from_deck']} keeps "
                    f"its copy until {pair['to_deck']} is built.")
        return f"✅ Took {pair['card']} from {pair['from_deck']} into {pair['to_deck']}."
    outcome = result.get("outcome") or {}
    fetch = " (fetched fresh from Scryfall)" if result.get("was_new") else ""
    if outcome.get("created_lot"):
        lot = " — created a new starter collection lot"
    elif outcome.get("assigned_lot_id"):
        lot = " — reassigned an existing owned copy"
    else:
        lot = ""
    add, cut = result.get("add_name"), result.get("remove_name")
    if add and cut:
        return f"✅ {verb} {add}{fetch}, replaced {cut}{lot}."
    if add:
        return f"✅ {verb} {add}{fetch}, nothing cut{lot}."
    return f"✅ Removed {cut}."


def format_change_route(source=None, source_location=None, dest_location=None):
    """A change row's recorded choices as a phrase for History: "from Lands
    Box", "to Lands Box", both, or "" for a row left on Auto / Box. A deck
    reads the same way (Phase 3): "from Gisa", "to Prosper"; a Buy add
    (Phase 4) reads "to buy"."""
    # A pandas NULL text column comes back as NaN, which isn't a string.
    def text(value):
        return value.strip() if isinstance(value, str) else ""

    parts = []
    if source in ("storage", "deck") and text(source_location):
        parts.append(f"from {text(source_location)}")
    elif source == "buy":
        parts.append("to buy")
    if text(dest_location):
        parts.append(f"to {text(dest_location)}")
    return " ".join(parts)
