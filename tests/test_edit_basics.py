"""docs/archive/EDIT_FLOW_PLAN.md Phase 7: basic lands are generic, but still tracked.

A basic land (here, Swamp) never needs a collection copy and never moves
one — not even when a tracked lot of it exists in Box or in another deck —
yet every add and removal still writes the deck's history row and one
bulk=1 row in the move log. Covered, in order:

  - every entry point (add bar, each Board op, Mainboard save, apply of all
    three change shapes) in three collection states: no lot, a tracked lot
    in Box, a tracked lot sleeved in another deck
  - Undo reverts the list only and logs the opposite step
  - a source / destination sent for a basic is ignored, not refused
  - the allocator, contention, buy list, Reconcile and Build it never list one
  - a collection-side move of a Swamp lot asks nothing about a deck's list
  - Card Lookup's move log reads "basic land supply"
"""
import os
import sqlite3

import pytest

from dashboard_lib import deck_board, formatting as fmt, queries as q, writes

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RING, SWAMP, ISLAND = "Sol Ring", "Swamp", "Island"


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    with open(os.path.join(ROOT, "schema.sql"), encoding="utf-8") as f:
        c.executescript(f.read())
    q.ensure_schema(c)
    writes.add_location_to_catalog(c, "Lands Box")
    for name in (RING, SWAMP, ISLAND):
        c.execute("INSERT INTO cards (scryfall_id, name, set_code, collector_number, oracle_id, is_basic_land)"
                  " VALUES (?,?,'cmd','1',?,?)", (name, name, f"oracle-{name}", 0 if name == RING else 1))
    for deck in ("Gisa", "Tuvasa"):
        c.execute("INSERT INTO decks (name) VALUES (?)", (deck,))
    c.commit()
    return c


@pytest.fixture
def c():
    conn = _conn()
    yield conn
    conn.close()


def _deck(c, name="Gisa"):
    return c.execute("SELECT deck_id FROM decks WHERE name=?", (name,)).fetchone()[0]


def _lots(c):
    return sorted(tuple(r) for r in c.execute("SELECT scryfall_id, location, quantity FROM collection"))


def _bulk_log(c):
    return [dict(r) for r in c.execute(
        "SELECT collection_id, scryfall_id, quantity, from_location, to_location, reason, change_id, bulk"
        " FROM collection_moves WHERE bulk = 1 ORDER BY move_id")]


def _history(c, deck="Gisa"):
    return [dict(r) for r in c.execute(
        "SELECT * FROM deck_changes WHERE deck_id=? AND status='applied' ORDER BY change_id", (_deck(c, deck),))]


def _list(c, card=SWAMP, deck="Gisa"):
    return q.deck_list_quantity(c, _deck(c, deck), card)


def _on_list(c, qty):
    c.execute("INSERT INTO deck_cards (deck_id, scryfall_id, quantity) VALUES (?,?,?)", (_deck(c), SWAMP, qty))
    c.commit()


def _planned(c, *, add=None, remove=None, quantity=1):
    cur = c.execute(
        """INSERT INTO deck_changes (deck_id, status, add_scryfall_id, add_name, remove_scryfall_id,
                                    remove_name, quantity, planned_at)
           VALUES (?, 'planned', ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
        (_deck(c), add, add, remove, remove, quantity))
    c.commit()
    return cur.lastrowid


WORLDS = {
    "no lot": lambda c: None,
    "tracked lot in Box": lambda c: writes.add_collection_lot(c, SWAMP, quantity=5, location="Box"),
    "tracked lot in another deck": lambda c: writes.add_collection_lot(c, SWAMP, quantity=5, location="Tuvasa"),
}


def _board(c, **action):
    return deck_board.apply_board_action(c, _deck(c), action)


def _board_add_idea(c):
    cid = writes.add_change(c, _deck(c), add_scryfall_id=SWAMP, add_name=SWAMP, quantity=2)
    return _board(c, op="add", change_id=cid)["moves"]


# (label, setup list quantity, the edit, list after, direction, copies)
ENTRY_POINTS = [
    ("add bar", 0, lambda c: writes.add_deck_card(c, _deck(c), SWAMP, 2), 2, "in", 2),
    ("Board +1", 3, lambda c: _board(c, op="inc", id=SWAMP)["moves"], 4, "in", 1),
    ("Board −1", 3, lambda c: _board(c, op="dec", id=SWAMP)["moves"], 2, "out", 1),
    ("Board set_qty", 3, lambda c: _board(c, op="set_qty", id=SWAMP, qty=5)["moves"], 5, "in", 2),
    ("Board remove", 3, lambda c: _board(c, op="remove", id=SWAMP)["moves"], 0, "out", 3),
    ("Board to Shortlist", 3, lambda c: _board(c, op="to_shortlist", id=SWAMP)["moves"], 0, "out", 3),
    ("Board add on an idea", 0, _board_add_idea, 2, "in", 2),
    ("Mainboard save (bump)", 3, lambda c: writes.bulk_update_deck_cards(c, _deck(c), {SWAMP: 6})[1], 6, "in", 3),
    ("Mainboard save (delete)", 3, lambda c: writes.bulk_update_deck_cards(c, _deck(c), {SWAMP: 0})[1], 0, "out", 3),
    ("apply pure add", 0, lambda c: writes.apply_change(c, _planned(c, add=SWAMP, quantity=2))["moves"], 2, "in", 2),
    ("apply pure cut", 3, lambda c: writes.apply_change(c, _planned(c, remove=SWAMP))["moves"], 0, "out", 3),
]


@pytest.mark.parametrize("world", list(WORLDS))
@pytest.mark.parametrize("label,listed,edit,after,direction,copies", ENTRY_POINTS,
                         ids=[e[0] for e in ENTRY_POINTS])
def test_every_entry_point_changes_the_list_and_logs_but_never_touches_a_lot(
        world, label, listed, edit, after, direction, copies):
    c = _conn()
    try:
        WORLDS[world](c)
        if listed:
            _on_list(c, listed)
        before_lots, before_history = _lots(c), len(_history(c))
        moves = edit(c)
        assert _lots(c) == before_lots, label
        assert _list(c) == after, label
        assert [(m["kind"], m["quantity"], m["direction"]) for m in moves] == [("bulk", copies, direction)], label
        assert len(_history(c)) == before_history + 1, label
        log = _bulk_log(c)
        assert len(log) == 1, label
        row = log[0]
        assert (row["collection_id"], row["scryfall_id"], row["quantity"]) == (None, SWAMP, copies)
        assert (row["from_location"], row["to_location"]) == ((None, "Gisa") if direction == "in" else ("Gisa", None))
        assert row["change_id"] == _history(c)[-1]["change_id"], label
        assert row["reason"] == ("apply" if label.startswith("apply") else "deck_edit")
    finally:
        c.close()


def test_a_swap_logs_the_basic_side_and_moves_the_other(c):
    writes.add_collection_lot(c, RING, quantity=1, location="Box")
    writes.add_collection_lot(c, SWAMP, quantity=4, location="Gisa")   # tracked, sleeved: still left alone
    _on_list(c, 4)
    moves = writes.apply_change(c, _planned(c, add=RING, remove=SWAMP))["moves"]
    assert sorted((m["card"], m["kind"]) for m in moves) == [(RING, "move"), (SWAMP, "bulk")]
    assert (SWAMP, "Gisa", 4) in _lots(c)
    assert [(r["from_location"], r["to_location"], r["quantity"]) for r in _bulk_log(c)] == [("Gisa", None, 4)]


def test_brewing_and_dismantled_decks_get_the_bulk_move_not_a_placeholder(c):
    for state in ("brewing", "dismantled"):
        writes.set_build_state(c, _deck(c), state)
        moves = writes.add_deck_card(c, _deck(c), SWAMP, (_list(c) or 0) + 1)
        assert [m["kind"] for m in moves] == ["bulk"], state


def test_undo_reverts_the_list_only_and_logs_the_reverse_step(c):
    writes.add_collection_lot(c, SWAMP, quantity=5, location="Box")
    _on_list(c, 3)
    forward = _board(c, op="remove", id=SWAMP)
    result = deck_board.apply_board_action(c, _deck(c), forward["undo"], undo_moves=forward["moves"])
    assert _list(c) == 3
    assert _lots(c) == [(SWAMP, "Box", 5)]
    assert [(r["from_location"], r["to_location"], r["quantity"]) for r in _bulk_log(c)] == [
        ("Gisa", None, 3), (None, "Gisa", 3)]
    assert [m["kind"] for m in result["moves"]] == ["bulk"]


def test_a_source_or_destination_sent_for_a_basic_is_ignored_not_refused(c):
    gisa = _deck(c)
    # A Location that doesn't exist and another deck: both would be refused for Sol Ring.
    writes.add_deck_card(c, gisa, SWAMP, 2, source="storage", source_location="Nowhere")
    writes.add_deck_card(c, gisa, ISLAND, 1, source="deck", source_location="Tuvasa")
    writes.add_deck_card(c, gisa, SWAMP, 1, dest="Nowhere")
    assert all(r["source"] is None and r["dest_location"] is None for r in _history(c))
    assert writes.normalize_source(c, "buy", None, scryfall_id=SWAMP) == (None, None)
    assert writes.normalize_dest(c, "Gisa", scryfall_id=SWAMP) is None
    with pytest.raises(ValueError):
        writes.normalize_dest(c, "Gisa", scryfall_id=RING)
    # A bulk save across mixed cards with a dest still saves the basic.
    c.execute("INSERT INTO deck_cards (deck_id, scryfall_id, quantity) VALUES (?,?,1)", (gisa, RING))
    writes.add_collection_lot(c, RING, quantity=1, location="Gisa")
    writes.bulk_update_deck_cards(c, gisa, {RING: 0, SWAMP: 0}, dest="Lands Box")
    assert _lots(c) == [(RING, "Lands Box", 1)]
    # An idea for a basic marked Buy is saved on Auto.
    idea = writes.add_change(c, _deck(c, "Tuvasa"), add_scryfall_id=SWAMP, add_name=SWAMP, source="buy")
    assert writes.get_change(c, idea)["source"] is None


def test_card_sources_and_the_picker_offer_no_choice_for_a_basic(c):
    writes.add_collection_lot(c, SWAMP, quantity=5, location="Lands Box")
    sources = q.card_sources(c, SWAMP, _deck(c))
    assert sources["bulk"] and sources["storage"] == [] and sources["decks"] == []
    assert fmt.source_choices(sources, ["Box", "Lands Box"], ["Tuvasa"]) == [("Basic land — nothing moves", None, None)]
    # The Board's Shortlist row: no options, no Buy.
    fields = deck_board._source_fields({}, sources)
    assert fields["can_buy"] is False and fields["source_options"] == [] and fields["deck_options"] == []


def test_record_purchase_refuses_a_basic(c):
    with pytest.raises(ValueError, match="generic"):
        writes.record_purchase(c, _deck(c), SWAMP, 1)


def test_no_buy_list_contention_reconcile_or_build_row_for_a_basic(c):
    gisa, tuvasa = _deck(c), _deck(c, "Tuvasa")
    writes.add_collection_lot(c, SWAMP, quantity=1, location="Box")   # one tracked copy, many wanted
    for deck in (gisa, tuvasa):
        c.execute("INSERT INTO deck_cards (deck_id, scryfall_id, quantity) VALUES (?,?,10)", (deck, SWAMP))
    c.commit()
    writes.add_change(c, gisa, add_scryfall_id=ISLAND, add_name=ISLAND)   # owned nowhere
    assert all(r["card"] not in (SWAMP, ISLAND) for r in q.buy_list(c)["rows"])
    assert all(r["card"] != SWAMP for r in q.contention_table(c))
    assert all(not d["to_pull"] and not d["to_put_away"] for d in q.deck_reconciliation(c))
    writes.set_build_state(c, gisa, "brewing")
    result = writes.build_deck(c, gisa, dry_run=True)
    assert result["to_pull"] == [] and result["moves"] == [] and result["cleared"]


def test_moving_a_swamp_lot_into_a_deck_asks_nothing_about_its_list(c):
    lot = writes.add_collection_lot(c, SWAMP, quantity=5, location="Box")
    since = q.last_move_id(c)
    writes.move_lot(c, lot, "Gisa")
    assert q.deck_sync_offers(c, since) == []


def test_card_lookup_reads_the_supply(c):
    writes.add_deck_card(c, _deck(c), SWAMP, 2)
    writes.remove_deck_card(c, _deck(c), SWAMP)
    lines = [fmt.format_collection_move(m) for m in q.collection_moves_for_card(c, SWAMP)]
    assert lines[0].endswith("×2 Gisa → basic land supply (deck edit)")
    assert lines[1].endswith("×2 basic land supply → Gisa (deck edit)")


def test_the_pop_up_names_a_basic(c):
    moves = writes.add_deck_card(c, _deck(c), SWAMP, 2)
    from dashboard_lib import card_view
    assert card_view.move_label(moves[0]) == "Swamp ×2: basic land — not tracked in the collection"


def test_the_board_payload_marks_basics(c):
    _on_list(c, 3)
    from dashboard_lib import card_view
    cards = deck_board.board_payload(card_view.add_derived_columns(q.deck_cards_dataframe(c, _deck(c))), {}, {})
    swamp = next(card for card in cards if card["name"] == SWAMP)
    assert swamp["basic"] is True and swamp["flags"]["not_owned"] is False
