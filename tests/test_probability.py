"""dashboard_lib/probability.py: color-source counting, the per-card and
commander cast-probability curves (including the one-land-drop-per-turn
floor), and the expected-count helpers. Pure functions, no database.

Replaces the probability checks of the old scripts/_test_prompt_pass5 and
_pass11 harnesses.
"""
import pandas as pd
import pytest

from dashboard_lib import probability as prob


# ------------------------------------------------------------------
# count_color_sources
# ------------------------------------------------------------------
@pytest.fixture
def library():
    """Already in add_derived_columns() shape (is_land present)."""
    def row(name, qty, is_land, type_line, oracle, identity):
        return {"name": name, "quantity": qty, "is_land": is_land, "type_line": type_line,
                "oracle_text": oracle, "color_identity": identity}
    return pd.DataFrame([
        row("Test Mountain", 10, True, "Basic Land — Mountain", "{T}: Add {R}.", ""),
        row("Test Signet", 1, False, "Artifact", "{T}: Add {R}.", ""),
        row("Test Bird", 1, False, "Creature — Bird", "{T}: Add one mana of any color.", ""),
        # Shares color R but never taps for mana: must NOT count as a source.
        row("Test Vanilla Red Creature", 1, False, "Creature — Human Warrior", "First strike.", "R"),
    ])


def test_lands_only_counts_just_the_land(library):
    assert prob.count_color_sources(library, "R", lands_only=True) == 10


def test_all_sources_counts_rocks_and_dorks_but_not_a_plain_same_color_card(library):
    # 10 land + signet + any-color bird. 13 would mean the vanilla red
    # creature leaked in through its color_identity (the old bug).
    assert prob.count_color_sources(library, "R", lands_only=False) == 12


# ------------------------------------------------------------------
# card_cast_probability_by_turn
# ------------------------------------------------------------------
def card_curve(cmc, pips, **kw):
    args = dict(library_size=99, land_count=35, source_counts={"R": 12}, card_qty=1,
                card_cmc=cmc, colored_pips=pips, on_the_play=True, max_turn=8)
    args.update(kw)
    return prob.card_cast_probability_by_turn(**args)


def test_card_curve_covers_turn_zero_through_max_turn():
    rows = card_curve(3, {"R": 1})
    assert [r["turn"] for r in rows] == list(range(9))


def test_card_curve_factors_are_probabilities_and_multiply():
    for r in card_curve(3, {"R": 1}):
        for key in ("draw_factor", "land_factor", "color_factor", "probability"):
            assert 0.0 <= r[key] <= 1.0
        assert r["probability"] == pytest.approx(r["draw_factor"] * r["land_factor"] * r["color_factor"])


def test_card_curve_draw_factor_matches_prob_at_least_and_never_decreases():
    rows = card_curve(3, {"R": 1})
    assert rows[0]["draw_factor"] == pytest.approx(prob.prob_at_least(99, 1, 7, 1))
    assert all(a["probability"] <= b["probability"] + 1e-9 for a, b in zip(rows, rows[1:]))


def test_free_card_only_depends_on_drawing_it():
    rows = card_curve(0, {}, source_counts={})
    assert all(r["land_factor"] == 1.0 and r["color_factor"] == 1.0 for r in rows)
    assert all(r["probability"] == pytest.approx(r["draw_factor"]) for r in rows)


def test_singletons_with_different_costs_get_different_curves():
    # The point of the per-card engine: the old tool drew the same curve
    # for every singleton.
    cheap, pricey = card_curve(1, {"R": 1}), card_curve(6, {"R": 1})
    assert cheap[3]["probability"] > pricey[3]["probability"]


# ------------------------------------------------------------------
# The land-drop floor: turn T puts at most T lands into play, so a card
# needing N lands has 0% land_factor on any turn below N, however many
# lands were drawn.
# ------------------------------------------------------------------
def test_commander_land_factor_is_zero_below_its_mana_value():
    rows = prob.commander_cast_probability_by_turn(
        library_size=40, land_count=30, source_counts={}, commander_cmc=4,
        colored_pips={}, on_the_play=True, max_turn=10)   # land-flooded on purpose
    by_turn = {r["turn"]: r for r in rows}
    assert by_turn[1]["probability"] == 0.0
    for turn in (1, 2, 3):
        assert by_turn[turn]["land_factor"] == 0.0
    # The floor only blocks turns BELOW the requirement; from turn 4 it is
    # the plain hypergeometric "at least 4 lands seen".
    expected = prob.prob_at_least(40, 30, prob.cards_seen_by_turn(4, True), 4)
    assert by_turn[4]["land_factor"] == expected > 0.0


def test_zero_mana_value_commander_is_never_floored():
    rows = prob.commander_cast_probability_by_turn(
        library_size=40, land_count=30, source_counts={}, commander_cmc=0,
        colored_pips={}, on_the_play=True, max_turn=2)
    assert all(r["land_factor"] == 1.0 for r in rows)


def test_card_land_factor_floor_uses_turn_less_than_needed():
    by_turn = {r["turn"]: r for r in card_curve(3, {}, library_size=40, land_count=30,
                                                source_counts={}, max_turn=5)}
    assert by_turn[0]["land_factor"] == 0.0      # opening hand
    assert by_turn[2]["land_factor"] == 0.0
    assert by_turn[3]["land_factor"] > 0.0       # turn == mana value is allowed


def test_free_card_at_turn_zero_is_not_floored():
    # 0 < 0 is false: the boundary is turn < needed, not <=.
    rows = card_curve(0, {}, library_size=40, land_count=30, source_counts={}, max_turn=1)
    assert rows[0]["land_factor"] == 1.0


# ------------------------------------------------------------------
# expected_count_by_turn / weighted_mana_expectation
# ------------------------------------------------------------------
def test_expected_count_zero_library_is_zero():
    assert prob.expected_count_by_turn(0, 33, 3, True) == 0.0


def test_expected_count_opening_hand_is_linearity_of_expectation():
    assert prob.expected_count_by_turn(99, 33, 0, True) == pytest.approx(33 * 7 / 99)


CATEGORIES = {"Rocks/Dorks": 5, "Colorless": 2, "Any Color": 1, "W": 3, "U": 0, "B": 0, "R": 0, "G": 0}


def test_weighted_mana_expectation_is_expected_count_per_category():
    weighted = prob.weighted_mana_expectation(99, CATEGORIES, 4, True)
    for cat, total in CATEGORIES.items():
        assert weighted[cat] == pytest.approx(prob.expected_count_by_turn(99, total, 4, True))


def test_weighted_mana_expectation_zero_library_is_all_zeros():
    assert all(v == 0.0 for v in prob.weighted_mana_expectation(0, CATEGORIES, 4, True).values())
