"""
Hypergeometric draw-probability math for the Land Probability page.

Pure Python (uses math.comb) for the core distribution functions — no
Streamlit or pandas dependency required for those, so they can be unit-
tested in isolation. count_color_sources() takes a pandas DataFrame (the
enriched deck-library dataframe from card_view.add_derived_columns) since
it needs to inspect real card rows.

Convention: turn 0 = opening hand (7 cards, no draws yet). Turn N (N>=1)
is the start of that player's Nth turn, after that turn's draw step (if
any). "On the play" skips the very first turn's draw, matching standard
tabletop Magic rules regardless of pod size.
"""
from math import comb

from . import formatting as fmt

OPENING_HAND_SIZE = 7


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
    """Total quantity of cards in the (already card_type/color_display-
    enriched) library dataframe whose color_identity includes `color`.

    Restricted to Land-type cards by default — this matches Scryfall's
    color_identity data accurately for basics and any land with an
    explicit colored mana ability (duals, triomes, filter lands, etc.).
    Setting lands_only=False extends the search to nonland cards too, but
    this under-counts mana rocks/dorks whose color-fixing is described in
    plain text rather than colored mana symbols (e.g. Sol Ring, Arcane
    Signet, Birds of Paradise) — Scryfall's color_identity field doesn't
    pick those up, so treat that mode as an approximation.
    """
    pool = library_df[library_df["card_type"] == "Land"] if lands_only else library_df
    if pool.empty:
        return 0
    mask = pool["color_identity"].apply(lambda ci: color in fmt.split_multi_value(ci))
    return int(pool.loc[mask, "quantity"].sum())
