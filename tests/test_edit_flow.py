"""EDIT_FLOW_PLAN.md Phase 1: one write path for every deck edit.

The point of the phase is that the same intent leaves the collection in the
same state whichever page it started on. So the core tests here are tables:
one scenario, run once through every entry point that can express it (the
add bar's write, each Board op, the Mainboard table's bulk save, and
apply_change()'s three shapes), each on its own fresh database, and the
resulting deck list + collection compared across all of them.

Then the rules behind those states, one test each: a free copy in storage
before a starter lot, never a copy out of another deck, splitting a lot,
bulk basics, Undo putting copies back, and brewing / dismantled decks.
"""
import os
import sqlite3

import pytest

from dashboard_lib import deck_board, queries as q, writes

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _fresh_conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    with open(os.path.join(ROOT, "schema.sql"), encoding="utf-8") as f:
        c.executescript(f.read())
    q.ensure_schema(c)
    return c


@pytest.fixture
def fresh():
    """fresh() -> a new empty database; all of them closed at teardown."""
    made = []

    def _make():
        made.append(_fresh_conn())
        return made[-1]
    yield _make
    for c in made:
        c.close()


RING, BLADE, SWAMP = "Sol Ring", "Heirloom Blade", "Swamp"


def _world(c, *, box=0, other_deck=0, sleeved=0, listed=0, cut_listed=False, state=None):
    """Gisa (the deck being edited) and Tuvasa (another deck), Sol Ring and
    Heirloom Blade. `box` / `other_deck` / `sleeved`: copies of Sol Ring in
    one lot each in Box, in Tuvasa, in Gisa. `listed`: Sol Ring's quantity on
    Gisa's list (0 = not on it). `cut_listed`: Blade on Gisa's list ×1 with
    its copy sleeved there. Returns Gisa's deck_id."""
    for name in (RING, BLADE, SWAMP):
        c.execute("INSERT INTO cards (scryfall_id, name, set_code, collector_number, oracle_id, is_basic_land)"
                  " VALUES (?,?,'cmd','1',?,?)", (name, name, f"oracle-{name}", 1 if name == SWAMP else 0))
    gisa = c.execute("INSERT INTO decks (name, build_state) VALUES ('Gisa', ?)", (state,)).lastrowid
    c.execute("INSERT INTO decks (name) VALUES ('Tuvasa')")
    for qty, where in ((box, "Box"), (other_deck, "Tuvasa"), (sleeved, "Gisa")):
        if qty:
            writes.add_collection_lot(c, RING, quantity=qty, location=where)
    if listed:
        c.execute("INSERT INTO deck_cards (deck_id, scryfall_id, quantity) VALUES (?,?,?)", (gisa, RING, listed))
    if cut_listed:
        c.execute("INSERT INTO deck_cards (deck_id, scryfall_id, quantity) VALUES (?,?,1)", (gisa, BLADE))
        writes.add_collection_lot(c, BLADE, quantity=1, location="Gisa")
    c.commit()
    return gisa


def _state(c):
    """What a path leaves behind: the list, and every lot by card + where +
    how many + source. Ids are left out on purpose — a split makes a new one,
    and two paths making the same split needn't number it the same."""
    return {
        "list": sorted(tuple(r) for r in c.execute("SELECT scryfall_id, quantity FROM deck_cards")),
        "lots": sorted(tuple(r) for r in c.execute(
            "SELECT scryfall_id, location, quantity, COALESCE(source, '') FROM collection")),
    }


def _planned(c, deck, *, add=None, remove=None, quantity=1):
    """A planned row of any shape, written directly (add_change() refuses a
    planned row with no cut, which is the shape a dismantle feeding a deck
    writes)."""
    return c.execute(
        """INSERT INTO deck_changes (deck_id, status, add_scryfall_id, add_name, remove_scryfall_id,
                                    remove_name, quantity, planned_at)
           VALUES (?, 'planned', ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
        (deck, add, add, remove, remove, quantity),
    ).lastrowid


def _board(c, deck, **action):
    return deck_board.apply_board_action(c, deck, action)


# ------------------------------------------------------------------
# Same intent, every path
# ------------------------------------------------------------------
def _add_board_idea(c, g):
    cid = writes.add_change(c, g, add_scryfall_id=RING, add_name=RING)
    return _board(c, g, op="add", change_id=cid)["moves"]


ADD_PATHS = {
    "add bar": lambda c, g: writes.add_deck_card(c, g, RING, 1),
    "Mainboard table": lambda c, g: writes.bulk_update_deck_cards(c, g, {RING: 1})[1],
    "Board add on an idea": _add_board_idea,
    "Board set_qty": lambda c, g: _board(c, g, op="set_qty", id=RING, qty=1)["moves"],
    "apply_change pure add": lambda c, g: writes.apply_change(c, _planned(c, g, add=RING))["moves"],
}


@pytest.mark.parametrize("world,expected_lots", [
    # A free copy in Box is used before a starter lot is made.
    ({"box": 1}, [(RING, "Gisa", 1, "")]),
    # Biggest lot first, split so only one copy moves.
    ({"box": 3}, [(RING, "Box", 2, ""), (RING, "Gisa", 1, "")]),
    # A copy in another deck is never taken: a starter lot instead.
    ({"other_deck": 1}, [(RING, "Gisa", 1, writes.STARTER_LOT_SOURCE), (RING, "Tuvasa", 1, "")]),
    # Not owned at all: a starter lot.
    ({}, [(RING, "Gisa", 1, writes.STARTER_LOT_SOURCE)]),
    # A stray already sleeved here is adopted; nothing moves.
    ({"sleeved": 1, "box": 1}, [(RING, "Box", 1, ""), (RING, "Gisa", 1, "")]),
], ids=["box", "split", "other-deck", "not-owned", "stray-here"])
def test_adding_a_card_leaves_the_same_collection_from_every_path(fresh, world, expected_lots):
    states = {}
    for label, path in ADD_PATHS.items():
        c = fresh()
        g = _world(c, **world)
        moves = path(c, g)
        assert all(m["to"] == "Gisa" for m in moves), label
        states[label] = _state(c)
    first = next(iter(states.values()))
    assert all(s == first for s in states.values()), states
    assert first["list"] == [(RING, 1)]
    assert first["lots"] == sorted(expected_lots)


REMOVE_PATHS = {
    "remove_deck_card (Workbench drop)": lambda c, g: writes.remove_deck_card(c, g, RING),
    "Mainboard table": lambda c, g: writes.bulk_update_deck_cards(c, g, {RING: 0})[1],
    "Board remove": lambda c, g: _board(c, g, op="remove", id=RING)["moves"],
    "Board −1 from 1": lambda c, g: _board(c, g, op="dec", id=RING)["moves"],
    "Board to Shortlist": lambda c, g: _board(c, g, op="to_shortlist", id=RING)["moves"],
    "apply_change pure cut": lambda c, g: writes.apply_change(c, _planned(c, g, remove=RING))["moves"],
}


def test_removing_a_card_leaves_the_same_collection_from_every_path(fresh):
    states = {}
    for label, path in REMOVE_PATHS.items():
        c = fresh()
        g = _world(c, sleeved=1, listed=1)
        moves = path(c, g)
        assert [(m["from"], m["to"], m["kind"]) for m in moves] == [("Gisa", "Box", "move")], label
        states[label] = _state(c)
    first = next(iter(states.values()))
    assert all(s == first for s in states.values()), states
    assert first == {"list": [], "lots": [(RING, "Box", 1, "")]}


def _swap_by_hand(c, g):
    return writes.remove_deck_card(c, g, BLADE) + writes.add_deck_card(c, g, RING, 1)


def _swap_on_board(c, g):
    return _board(c, g, op="remove", id=BLADE)["moves"] + _add_board_idea(c, g)


SWAP_PATHS = {
    "apply_change swap": lambda c, g: writes.apply_change(c, _planned(c, g, add=RING, remove=BLADE))["moves"],
    "remove + add": _swap_by_hand,
    "Mainboard table": lambda c, g: writes.bulk_update_deck_cards(c, g, {BLADE: 0, RING: 1})[1],
    "Board remove + add": _swap_on_board,
}


def test_swapping_leaves_the_same_collection_from_every_path(fresh):
    states = {}
    for label, path in SWAP_PATHS.items():
        c = fresh()
        g = _world(c, box=1, cut_listed=True)
        moves = path(c, g)
        assert sorted((m["card"], m["from"], m["to"]) for m in moves) == [
            (BLADE, "Gisa", "Box"), (RING, "Box", "Gisa")], label
        states[label] = _state(c)
    first = next(iter(states.values()))
    assert all(s == first for s in states.values()), states
    assert first == {"list": [(RING, 1)], "lots": [(BLADE, "Box", 1, ""), (RING, "Gisa", 1, "")]}


# ------------------------------------------------------------------
# The rules, one at a time
# ------------------------------------------------------------------
def test_minus_one_on_a_three_copy_lot_splits_it(fresh):
    c = fresh()
    g = _world(c, sleeved=3, listed=3)
    moves = _board(c, g, op="dec", id=RING)["moves"]
    assert [(m["quantity"], m["from"], m["to"]) for m in moves] == [(1, "Gisa", "Box")]
    assert _state(c)["lots"] == [(RING, "Box", 1, ""), (RING, "Gisa", 2, "")]


def test_plus_one_only_moves_the_difference(fresh):
    c = fresh()
    g = _world(c, sleeved=2, listed=2, box=5)
    moves = _board(c, g, op="inc", id=RING)["moves"]
    assert [(m["quantity"], m["from"], m["to"]) for m in moves] == [(1, "Box", "Gisa")]


def test_minus_one_on_a_deck_never_fully_sleeved_moves_nothing(fresh):
    """The list still needs every copy that is actually there."""
    c = fresh()
    g = _world(c, sleeved=1, listed=3)
    assert _board(c, g, op="dec", id=RING)["moves"] == []
    assert _state(c)["lots"] == [(RING, "Gisa", 1, "")]


def test_cutting_one_printing_for_another_of_the_same_card_moves_nothing(fresh):
    c = fresh()
    g = _world(c, sleeved=1, listed=1)
    foil = "Sol Ring foil"
    c.execute("INSERT INTO cards (scryfall_id, name, set_code, collector_number, oracle_id)"
              " VALUES (?, 'Sol Ring', 'c21', '9', 'oracle-Sol Ring')", (foil,))
    outcome = writes.apply_change(c, _planned(c, g, add=foil, remove=RING))
    assert outcome["moves"] == [] and not outcome["created_lot"]
    assert _state(c)["lots"] == [(RING, "Gisa", 1, "")]


def test_a_swap_moves_only_as_many_copies_as_the_cut_ran(fresh):
    """execute_swap() used to move every lot of the cut card's name."""
    c = fresh()
    g = _world(c, box=1, cut_listed=True)
    writes.add_collection_lot(c, BLADE, quantity=1, location="Gisa")   # a stray second copy
    outcome = writes.apply_change(c, _planned(c, g, add=RING, remove=BLADE))
    assert outcome["moved_out"]
    assert sorted(lot for lot in _state(c)["lots"] if lot[0] == BLADE) == [
        (BLADE, "Box", 1, ""), (BLADE, "Gisa", 1, "")]


def test_apply_keeps_its_old_outcome_keys(fresh):
    c = fresh()
    g = _world(c, box=1, cut_listed=True)
    outcome = writes.apply_change(c, _planned(c, g, add=RING, remove=BLADE))
    assert outcome["moved_out"] and not outcome["created_lot"]
    assert outcome["assigned_lot_id"] == next(m["collection_id"] for m in outcome["moves"] if m["card"] == RING)


def test_bulk_basics_never_move(fresh):
    c = fresh()
    g = _world(c)
    writes.add_collection_lot(c, SWAMP, quantity=None, location="Basics box")
    moves = writes.add_deck_card(c, g, SWAMP, 30)
    assert [(m["kind"], m["quantity"]) for m in moves] == [("bulk", 30)]
    # Phase 7: the cut is recorded as a bulk step out, still with no lot moved.
    assert [(m["kind"], m["quantity"], m["direction"]) for m in writes.remove_deck_card(c, g, SWAMP)] == [
        ("bulk", 30, "out")]
    assert _state(c)["lots"] == [(SWAMP, "Basics box", None, "")]


def test_a_basic_with_no_free_copy_gets_no_starter_lot(fresh):
    c = fresh()
    g = _world(c)
    moves = writes.add_deck_card(c, g, SWAMP, 8)
    assert [m["kind"] for m in moves] == ["bulk"]
    assert _state(c)["lots"] == []


def test_a_source_that_does_not_exist_yet_is_refused(fresh):
    """A source nobody built (Phase 4 made Buy real) must be refused, not saved and ignored; taking from
    a deck (Phase 3) exists now, and refuses when that deck has no spare copy."""
    c = fresh()
    g = _world(c, box=1, listed=1)
    with pytest.raises(ValueError, match="isn't a choice"):
        writes.sleeve_into_deck(c, g, RING, 1, source="teleport")
    with pytest.raises(ValueError, match="no longer needs"):
        writes.sleeve_into_deck(c, g, RING, 1, source="deck", source_location="Tuvasa")
    with pytest.raises(ValueError, match="Location"):
        writes.sleeve_into_deck(c, g, RING, 1, source="storage")   # no Location named


# ------------------------------------------------------------------
# Undo puts the copies back
# ------------------------------------------------------------------
def _undo(c, g, result):
    return deck_board.apply_board_action(c, g, result["undo"], undo_moves=result["moves"])


def test_undoing_a_remove_puts_the_same_lot_back(fresh):
    c = fresh()
    g = _world(c, sleeved=1, listed=1)
    lot_id = c.execute("SELECT collection_id FROM collection").fetchone()[0]
    result = _board(c, g, op="remove", id=RING)
    undone = _undo(c, g, result)
    assert [tuple(r) for r in c.execute("SELECT collection_id, location FROM collection")] == [(lot_id, "Gisa")]
    assert [(m["from"], m["to"]) for m in undone["moves"]] == [("Box", "Gisa")]
    assert _state(c)["list"] == [(RING, 1)]


def test_undoing_an_add_from_box_puts_the_copy_back_in_box(fresh):
    c = fresh()
    g = _world(c, box=1)
    cid = writes.add_change(c, g, add_scryfall_id=RING, add_name=RING)
    result = _board(c, g, op="add", change_id=cid)
    assert _state(c)["lots"] == [(RING, "Gisa", 1, "")]
    _undo(c, g, result)
    assert _state(c) == {"list": [], "lots": [(RING, "Box", 1, "")]}


def test_undoing_a_bump_deletes_the_starter_lot_it_made(fresh):
    c = fresh()
    g = _world(c, sleeved=1, listed=1)
    result = _board(c, g, op="inc", id=RING)
    assert [m["kind"] for m in result["moves"]] == ["new"]
    undone = _undo(c, g, result)
    assert [m["kind"] for m in undone["moves"]] == ["delete"]
    assert _state(c) == {"list": [(RING, 1)], "lots": [(RING, "Gisa", 1, "")]}


def test_undoing_a_split_leaves_two_lots_in_the_same_place(fresh):
    c = fresh()
    g = _world(c, sleeved=3, listed=3)
    _undo(c, g, _board(c, g, op="dec", id=RING))
    assert _state(c)["lots"] == [(RING, "Gisa", 1, ""), (RING, "Gisa", 2, "")]


def test_undo_leaves_a_lot_that_has_moved_on_since(fresh):
    c = fresh()
    g = _world(c, sleeved=1, listed=1)
    result = _board(c, g, op="remove", id=RING)
    lot_id = result["moves"][0]["collection_id"]
    writes.move_lot(c, lot_id, "Binder")   # moved by hand in between
    undone = _undo(c, g, result)
    assert undone["moves"] == [] and "left where it is" in undone["warnings"][0]
    assert _state(c)["lots"] == [(RING, "Binder", 1, "")]
    assert _state(c)["list"] == [(RING, 1)]   # the list change is still undone


def test_undo_does_not_rerun_auto_sourcing(fresh):
    """A forward edit that moved nothing (the copy was already here) must not
    have its undo pull or put away anything either."""
    c = fresh()
    g = _world(c, sleeved=2, listed=1, box=1)   # a stray second copy in the deck
    result = _board(c, g, op="inc", id=RING)
    assert result["moves"] == []
    _undo(c, g, result)
    assert _state(c)["lots"] == [(RING, "Box", 1, ""), (RING, "Gisa", 2, "")]


# ------------------------------------------------------------------
# Brewing and dismantled decks (rule 4)
# ------------------------------------------------------------------
@pytest.mark.parametrize("label", list(ADD_PATHS))
def test_adding_to_a_brewing_deck_changes_the_list_and_moves_nothing(fresh, label):
    c = fresh()
    g = _world(c, box=1, state="brewing")
    moves = ADD_PATHS[label](c, g)
    assert [(m["kind"], m["direction"]) for m in moves] == [("brewing", "in")]
    assert _state(c) == {"list": [(RING, 1)], "lots": [(RING, "Box", 1, "")]}


@pytest.mark.parametrize("label", list(REMOVE_PATHS))
def test_removing_from_a_brewing_deck_changes_the_list_and_moves_nothing(fresh, label):
    c = fresh()
    g = _world(c, sleeved=1, listed=1, state="brewing")
    moves = REMOVE_PATHS[label](c, g)
    assert [(m["kind"], m["direction"]) for m in moves] == [("brewing", "out")]
    assert _state(c) == {"list": [], "lots": [(RING, "Gisa", 1, "")]}


@pytest.mark.parametrize("label", list(SWAP_PATHS))
def test_swapping_on_a_brewing_deck_moves_nothing(fresh, label):
    c = fresh()
    g = _world(c, box=1, cut_listed=True, state="brewing")
    moves = SWAP_PATHS[label](c, g)
    assert {m["kind"] for m in moves} == {"brewing"}
    assert _state(c) == {"list": [(RING, 1)], "lots": [(BLADE, "Gisa", 1, ""), (RING, "Box", 1, "")]}


def test_a_brewing_add_whose_copy_is_already_here_has_nothing_to_report(fresh):
    """A dismantle's removal moves the donor's copy straight into the brew,
    so the brew's own add finds it there: no "will be pulled" placeholder."""
    c = fresh()
    g = _world(c, sleeved=1, state="brewing")
    assert writes.add_deck_card(c, g, RING, 1) == []


def test_force_moves_copies_on_a_brewing_deck(fresh):
    """Phase 5's "Build it" is the only caller that sets it."""
    c = fresh()
    g = _world(c, box=1, listed=1, state="brewing")
    moves = writes.sleeve_into_deck(c, g, RING, 1, force=True)
    assert [(m["from"], m["to"]) for m in moves] == [("Box", "Gisa")]


@pytest.mark.parametrize("label", list(ADD_PATHS))
def test_adding_to_a_dismantled_deck_pulls_nothing_back_in(fresh, label):
    c = fresh()
    g = _world(c, box=1, state="dismantled")
    moves = ADD_PATHS[label](c, g)
    assert [m["kind"] for m in moves] == ["dismantled"]
    assert _state(c) == {"list": [(RING, 1)], "lots": [(RING, "Box", 1, "")]}


def test_a_dismantled_decks_removals_still_take_copies_out(fresh):
    """The Deck Editor marks a deck dismantled when the dismantle is staged,
    before its removals are applied; those removals must still move the
    copies, or the dismantle would do nothing physical at all."""
    c = fresh()
    g = _world(c, sleeved=1, listed=1, state="dismantled")
    tuvasa = c.execute("SELECT deck_id FROM decks WHERE name='Tuvasa'").fetchone()[0]
    c.execute("INSERT INTO deck_cards (deck_id, scryfall_id, quantity) VALUES (?,?,1)", (tuvasa, RING))
    staged = writes.stage_dismantle(c, g, target_deck_id=tuvasa)
    results = writes.apply_changes(c, staged["removed"] + staged["added"])
    assert all(r["ok"] for r in results)
    assert _state(c)["lots"] == [(RING, "Tuvasa", 1, "")]


# ------------------------------------------------------------------
# Lot-only moves report the same way
# ------------------------------------------------------------------
def test_move_copies_reports_a_reconcile_move(fresh):
    c = fresh()
    _world(c, box=3)
    lot_id = c.execute("SELECT collection_id FROM collection").fetchone()[0]
    (move,) = writes.move_copies(c, lot_id, "Gisa", quantity=1)
    assert (move["card"], move["quantity"], move["from"], move["to"], move["kind"]) == (
        RING, 1, "Box", "Gisa", "move")
    assert move["collection_id"] != lot_id   # the split-off lot
    assert _state(c)["lots"] == [(RING, "Box", 2, ""), (RING, "Gisa", 1, "")]


def test_apply_changes_carries_each_rows_moves(fresh):
    c = fresh()
    g = _world(c, box=1, cut_listed=True)
    (result,) = writes.apply_changes(c, [_planned(c, g, add=RING, remove=BLADE)])
    assert result["moves"] == result["outcome"]["moves"] and len(result["moves"]) == 2
