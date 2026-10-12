"""Pure tests for the shared "current deck" helpers (Phase 2 of
UI_ACTION_PLAN.md): card_view.seed_current_deck() and
resolve_current_deck(). A plain dict stands in for st.session_state.
Page-level behavior (the deck carrying across pages, the Deck Editor's
create path) is covered by AppTest in test_pages_smoke.py."""
from dashboard_lib.card_view import CURRENT_DECK_KEY, resolve_current_deck, seed_current_deck


def test_param_seeds_current_deck():
    state = {}
    assert seed_current_deck(state, "3")
    assert state[CURRENT_DECK_KEY] == 3


def test_no_param_leaves_state_alone():
    state = {CURRENT_DECK_KEY: 5}
    assert not seed_current_deck(state, None)
    assert state[CURRENT_DECK_KEY] == 5


def test_consumed_param_then_manual_pick_wins():
    state = {}
    seed_current_deck(state, "3")
    state[CURRENT_DECK_KEY] = 7  # user picks another deck in the picker
    # The same param showing up again must not override the manual pick.
    assert not seed_current_deck(state, "3")
    assert state[CURRENT_DECK_KEY] == 7


def test_new_param_value_seeds_again():
    state = {}
    seed_current_deck(state, "3")
    state[CURRENT_DECK_KEY] = 7
    assert seed_current_deck(state, "4")
    assert state[CURRENT_DECK_KEY] == 4


def test_bad_param_is_consumed_without_seeding():
    state = {CURRENT_DECK_KEY: 2}
    assert not seed_current_deck(state, "abc")
    assert state[CURRENT_DECK_KEY] == 2


def test_resolve_keeps_valid_current_deck():
    state = {CURRENT_DECK_KEY: 2}
    assert resolve_current_deck(state, [1, 2, 3]) == 2


def test_resolve_falls_back_to_first_deck():
    for state in ({}, {CURRENT_DECK_KEY: 99}):
        assert resolve_current_deck(state, [4, 5]) == 4
        assert state[CURRENT_DECK_KEY] == 4


def test_created_deck_id_is_selected():
    # The Deck Editor's create flow sets the key to create_deck()'s new id;
    # the picker must honor it once the refreshed deck list includes it.
    state = {CURRENT_DECK_KEY: 1}
    state[CURRENT_DECK_KEY] = 6
    assert resolve_current_deck(state, [1, 6]) == 6
