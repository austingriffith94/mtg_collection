"""
Hypergeometric draw-probability math for the Land Probability page.

Pure Python (uses math.comb) for the core distribution functions — no
Streamlit or pandas dependency required for those, so they can be unit-
tested in isolation. count_color_sources(), mana_source_category_counts(),
and commander_cast_probability_by_turn() take a pandas DataFrame (the
enriched deck-library dataframe from card_view.add_derived_columns) since
they need to inspect real card rows (type_line/oracle_text/color_identity).

Convention: turn 0 = opening hand (7 cards, no draws yet). Turn N (N>=1)
is the start of that player's Nth turn, after that turn's draw step (if
any). "On the play" skips the very first turn's draw, matching standard
tabletop Magic rules regardless of pod size.
"""
import re
from math import comb

from . import formatting as fmt

OPENING_HAND_SIZE = 7

# Phase 3 — the fixed category order for the weighted color-availability
# graph's stacked bars: nonland ramp first, then land-based sources from
# least to most specific.
MANA_SOURCE_CATEGORIES = ["Rocks/Dorks", "Colorless", "Any Color", *fmt.WUBRG_ORDER]


def cards_seen_by_turn(turn, on_the_play, opening_hand=OPENING_HAND_SIZE):
    """Total cards drawn (opening hand + draw steps) by the given turn.
    turn=0 means the opening hand itself, before turn 1 begins."""
    if turn <= 0:
        return opening_hand
    draws = turn - (1 if on_the_play else 0)
    return opening_hand + max(draws, 0)


def prob_exactly(population, successes, sample, k):
    """P(exactly k successes) drawing `sample` cards from a `population`-
    card library that contains `successes` copies of the thing you care
    about (a specific card, or a whole category like 'lands')."""
    if population <= 0 or sample < 0 or sample > population:
        return 0.0
    successes = max(0, min(successes, population))
    if k < 0 or k > sample or k > successes:
        return 0.0
    non_successes = population - successes
    needed_non = sample - k
    if needed_non < 0 or needed_non > non_successes:
        return 0.0
    total = comb(population, sample)
    if total == 0:
        return 0.0
    return comb(successes, k) * comb(non_successes, needed_non) / total


def prob_at_least(population, successes, sample, k):
    """P(at least k successes)."""
    if k <= 0:
        return 1.0
    hi = min(sample, successes)
    if k > hi:
        return 0.0
    return sum(prob_exactly(population, successes, sample, i) for i in range(k, hi + 1))


def distribution(population, successes, sample):
    """Full PMF as {k: probability} for k = 0..min(sample, successes)."""
    hi = min(sample, successes)
    return {k: prob_exactly(population, successes, sample, k) for k in range(0, hi + 1)}


def count_color_sources(library_df, color, lands_only=True):
    """Total quantity of cards in the (already card_view.add_derived_
    columns-enriched, so MDFC-aware 'is_land' + oracle_text-bearing)
    library dataframe that can produce `color` mana, per the Phase 3
    keyword/text rule parser (formatting.classify_mana_colors).

    Restricted to land sources (is_land, MDFC-aware) by default.
    Setting lands_only=False also counts nonland mana rocks/dorks whose
    color-fixing is described in plain text (Sol Ring, Arcane Signet,
    Birds of Paradise, etc.) via the same text-parsing rules, rather than
    only trusting the color_identity field — this fixes cards like
    Command Tower / City of Brass / Exotic Orchard, whose color_identity
    is empty even though their text fixes for any color.

    Prompt Pass 5 fix: the lands_only=False branch used to classify
    EVERY nonland card in the pool, not just actual mana sources.
    classify_mana_colors()'s color_identity fallback (its last resort,
    for text this regex parser can't read) would then misidentify a
    plain nonland card — e.g. a vanilla red creature with no mana
    ability at all — as an "R mana source" purely because its color
    identity happens to be R, even though it never taps for mana. The
    nonland side of the pool is now restricted to cards
    formatting.is_mana_rock_or_dork() actually flags as having a mana
    ability of their own — the same gate mana_source_category_counts()
    below already used, so the two stay consistent with each other.
    """
    if lands_only:
        pool = library_df[library_df["is_land"]]
    else:
        is_source = library_df["is_land"] | library_df.apply(
            lambda r: fmt.is_mana_rock_or_dork(r.get("type_line"), r.get("oracle_text")),
            axis=1,
        )
        pool = library_df[is_source]
    if pool.empty:
        return 0

    def matches(row):
        info = fmt.classify_mana_colors(row.get("type_line"), row.get("oracle_text"), row.get("color_identity"))
        return info["any"] or (color in info["colors"])

    mask = pool.apply(matches, axis=1)
    return int(pool.loc[mask, "quantity"].sum())


def mana_source_category_counts(library_df):
    """Phase 3 — total quantity per mana-source category across the whole
    library, for the weighted color-availability graph. Categories (see
    MANA_SOURCE_CATEGORIES): 'Rocks/Dorks' (nonland mana-producing
    permanents, lumped together regardless of color), 'Colorless' (lands
    producing exclusively generic mana), 'Any Color' (Command Tower/City
    of Brass/Exotic Orchard-style lands), and one bucket per WUBRG letter.
    A dual/tri land contributes to every color it can produce, same
    convention as count_color_sources — so these totals can add up to
    more than the deck's actual land count when duals are present; that's
    expected for a per-color composition chart, not a partition."""
    totals = {cat: 0 for cat in MANA_SOURCE_CATEGORIES}
    if library_df.empty:
        return totals

    for _, row in library_df.iterrows():
        qty = int(row.get("quantity") or 0)
        if qty <= 0:
            continue
        type_line = row.get("type_line")
        oracle_text = row.get("oracle_text")
        color_identity = row.get("color_identity")

        if row.get("is_land"):
            info = fmt.classify_mana_colors(type_line, oracle_text, color_identity)
            if info["any"]:
                totals["Any Color"] += qty
            elif info["colorless"]:
                totals["Colorless"] += qty
            else:
                for c in info["colors"]:
                    totals[c] += qty
        elif fmt.is_mana_rock_or_dork(type_line, oracle_text):
            totals["Rocks/Dorks"] += qty

    return totals


def expected_count_by_turn(library_size, count, turn, on_the_play):
    """The probability-weighted EXPECTED number of a `count`-total group
    seen by `turn` (opening hand seen at turn=0), using linearity of
    expectation for the hypergeometric distribution: E[seen] = count *
    (cards_seen / library_size). This is an expected COUNT, not a P(at
    least one) probability — it answers 'how many of this do I expect to
    have access to by now'. Originally the per-category math inside
    weighted_mana_expectation() below (Phase 3); pulled out as its own
    single-count helper in Prompt Pass 5 so the Land Probability page's
    plain "expected lands seen by turn N" figure can reuse it directly
    without building a whole category dict for one number."""
    if library_size <= 0:
        return 0.0
    seen = min(cards_seen_by_turn(turn, on_the_play), library_size)
    return count * seen / library_size


def weighted_mana_expectation(library_size, category_counts, turn, on_the_play):
    """Phase 3 — the probability-weighted EXPECTED number of sources from
    each category seen by `turn` (opening hand seen at turn=0). This is
    an expected count, not a P(at least one) probability — it answers
    'how many of this category do I expect to have access to by now',
    which composes cleanly across categories in a stacked bar. Thin
    wrapper around expected_count_by_turn() (Prompt Pass 5) applied to
    every category in the dict."""
    return {
        cat: expected_count_by_turn(library_size, total, turn, on_the_play)
        for cat, total in category_counts.items()
    }


_PIP_RE = re.compile(r"\{([^}]+)\}")


def parse_colored_pips(mana_cost):
    """Count of each PLAIN colored mana symbol in a mana_cost string, e.g.
    '{2}{U}{U}{B}' -> {'U': 2, 'B': 1}. Used by the Commander Cast
    Probability engine to find how many sources of each color the
    commander actually requires.

    Hybrid ('{U/B}') and Phyrexian ('{U/P}') pips are deliberately
    skipped rather than counted toward every color they mention — they
    can be paid by either color (or life, for Phyrexian), so folding them
    into a strict 'must have a source of color X' requirement would
    overstate how hard the commander actually is to cast. Generic numeric
    and {C} symbols are ignored too, since only colored pips create a
    color-specific mana-source requirement."""
    if not mana_cost:
        return {}
    counts = {}
    for symbol in _PIP_RE.findall(str(mana_cost)):
        token = symbol.strip().upper()
        if token in fmt.WUBRG_ORDER:
            counts[token] = counts.get(token, 0) + 1
    return counts


def commander_cast_probability_by_turn(
    library_size, land_count, source_counts, commander_cmc, colored_pips, on_the_play, max_turn=10
):
    """Phase 3 — Commander Cast Probability engine, turns 1..max_turn.

    Compounds two independent factors per turn:
      1) land_factor  — P(at least `commander_cmc` lands drawn by this
         turn), i.e. the standard 1-land-per-turn, no-ramp land-drop math
         already used elsewhere on this page.
      2) color_factor — the PRODUCT, across every distinct colored pip in
         the commander's cost, of P(at least `need` sources of that color
         drawn by this turn) — using source_counts (see count_color_sources).

    combined = land_factor * color_factor is reported as the estimated
    probability of being ABLE to cast the commander on curve that turn.
    Treating the two factors (and each color within factor 2) as
    independent is a simplification — the same physical draw can only
    satisfy one requirement at a time — so treat this as an optimistic
    estimate, not an exact joint probability."""
    needed_lands = max(0, int(round(commander_cmc or 0)))
    rows = []
    for turn in range(1, max_turn + 1):
        seen = cards_seen_by_turn(turn, on_the_play)
        land_factor = prob_at_least(library_size, land_count, seen, needed_lands)
        color_factor = 1.0
        for color, need in (colored_pips or {}).items():
            color_factor *= prob_at_least(library_size, source_counts.get(color, 0), seen, need)
        rows.append({
            "turn": turn,
            "cards_seen": seen,
            "land_factor": land_factor,
            "color_factor": color_factor,
            "probability": land_factor * color_factor,
        })
    return rows


def card_cast_probability_by_turn(
    library_size, land_count, source_counts, card_qty, card_cmc, colored_pips, on_the_play, max_turn=8
):
    """Prompt Pass 5 — per-card version of commander_cast_probability_by_
    turn() above, powering the Land Probability page's "Check a specific
    card" tool. Unlike the commander (which always sits in the command
    zone, never shuffled into the library), an ordinary library card
    also has to actually be DRAWN before it can be cast, so this adds a
    third independent factor on top of the commander engine's two:

      1) draw_factor  — P(at least 1 copy of the card drawn by this
         turn), using the card's own quantity in the shuffled library —
         this is what makes the result differ card-to-card even though
         every nonland card here is a 1-of; the old version of this tool
         only ever showed this factor alone, which is why every
         singleton looked identical.
      2) land_factor  — P(at least `card_cmc` lands drawn by this turn),
         same land-drop math used elsewhere on this page.
      3) color_factor — the PRODUCT, across every distinct colored pip in
         the card's own cost, of P(at least `need` sources of that color
         drawn by this turn) — using source_counts (see
         count_color_sources()).

    combined = draw_factor * land_factor * color_factor is reported as
    the estimated probability of having BOTH drawn this specific card AND
    had the mana to cast it, by that turn. As with the commander engine,
    treating these three factors as independent is an optimistic
    simplification — the same physical draw can't satisfy two of these
    requirements at once — so read this as an estimate, not an exact
    joint probability. turn=0 is the opening hand, matching the rest of
    this page's convention; max_turn defaults to 8 per spec."""
    needed_lands = max(0, int(round(card_cmc or 0)))
    rows = []
    for turn in range(0, max_turn + 1):
        seen = cards_seen_by_turn(turn, on_the_play)
        draw_factor = prob_at_least(library_size, card_qty, seen, 1)
        land_factor = prob_at_least(library_size, land_count, seen, needed_lands)
        color_factor = 1.0
        for color, need in (colored_pips or {}).items():
            color_factor *= prob_at_least(library_size, source_counts.get(color, 0), seen, need)
        rows.append({
            "turn": turn,
            "cards_seen": seen,
            "draw_factor": draw_factor,
            "land_factor": land_factor,
            "color_factor": color_factor,
            "probability": draw_factor * land_factor * color_factor,
        })
    return rows
