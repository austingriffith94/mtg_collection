"""docs/archive/EDIT_FLOW_PLAN.md Phase 6: collection-side edits, tracked and flowing back.

Two halves:

  * the move log — every lot writer (add / update / move / remove / bulk /
    prune, plus delete_deck's reassignment) writes exactly the moves it made
    to `collection_moves`, a rename writes none, and Phase 1's deck edits log
    with the deck_changes row behind them;
  * deck sync — a collection-side save that moved copies into or out of a
    deck produces offers (queries.deck_sync_offers) that
    writes.sync_deck_lists applies to the deck's LIST without moving
    anything a second time.
"""
import sqlite3

import pytest

from dashboard_lib import formatting as fmt, queries as q, writes


def _log(conn):
    """The whole move log as plain tuples: (card, qty, from, to, reason)."""
    return [tuple(r) for r in conn.execute(
        "SELECT card_name, quantity, from_location, to_location, reason FROM collection_moves ORDER BY move_id")]


@pytest.fixture
def world(conn, add_card):
    """Sol Ring + Arcane Signet, two decks (Gisa, Tuvasa), nothing owned."""
    ring, signet = add_card("Sol Ring"), add_card("Arcane Signet")
    gisa = conn.execute("INSERT INTO decks (name) VALUES ('Gisa')").lastrowid
    tuvasa = conn.execute("INSERT INTO decks (name) VALUES ('Tuvasa')").lastrowid
    conn.commit()
    return {"ring": ring, "signet": signet, "gisa": gisa, "tuvasa": tuvasa}


# ------------------------------------------------------------------
# The log: one test per lot writer
# ------------------------------------------------------------------
def test_adding_a_lot_logs_a_new_copy_record(conn, world):
    writes.add_collection_lot(conn, world["ring"], quantity=2, location="Box")
    assert _log(conn) == [("Sol Ring", 2, None, "Box", "collection_edit")]


def test_adding_a_lot_with_no_location_logs_an_empty_location(conn, world):
    writes.add_collection_lot(conn, world["ring"], quantity=1, location=None)
    assert _log(conn) == [("Sol Ring", 1, None, "", "collection_edit")]


def test_a_caller_names_the_reason(conn, world):
    writes.add_collection_lot(conn, world["ring"], quantity=1, location="Box", reason="import")
    assert _log(conn)[0][4] == "import"


def test_an_unknown_reason_is_refused(conn, world):
    with pytest.raises(ValueError):
        writes.add_collection_lot(conn, world["ring"], quantity=1, location="Box", reason="whim")


def test_moving_a_whole_lot_logs_one_row(conn, world):
    lot = writes.add_collection_lot(conn, world["ring"], quantity=2, location="Box")
    writes.move_lot(conn, lot, "Gisa")
    assert _log(conn)[1:] == [("Sol Ring", 2, "Box", "Gisa", "collection_edit")]


def test_a_split_logs_the_moved_copies_against_the_new_lot(conn, world):
    lot = writes.add_collection_lot(conn, world["ring"], quantity=3, location="Box")
    new_lot = writes.move_lot(conn, lot, "Gisa", quantity=1)
    assert new_lot != lot
    rows = conn.execute("SELECT collection_id, quantity FROM collection_moves ORDER BY move_id").fetchall()
    assert [tuple(r) for r in rows][1:] == [(new_lot, 1)]
    assert _log(conn)[1:] == [("Sol Ring", 1, "Box", "Gisa", "collection_edit")]


def test_moving_a_lot_to_where_it_is_logs_nothing(conn, world):
    lot = writes.add_collection_lot(conn, world["ring"], quantity=1, location="Box")
    writes.move_lot(conn, lot, "Box")
    writes.move_lot(conn, lot, " Box ")
    assert len(_log(conn)) == 1


def test_editing_a_lots_quantity_up_logs_new_copies_at_its_location(conn, world):
    lot = writes.add_collection_lot(conn, world["ring"], quantity=1, location="Box")
    writes.update_collection_lot(conn, lot, quantity=3)
    assert _log(conn)[1:] == [("Sol Ring", 2, None, "Box", "collection_edit")]


def test_editing_a_lots_quantity_down_logs_the_copies_as_deleted(conn, world):
    lot = writes.add_collection_lot(conn, world["ring"], quantity=3, location="Box")
    writes.update_collection_lot(conn, lot, quantity=1)
    assert _log(conn)[1:] == [("Sol Ring", 2, "Box", None, "collection_edit")]


def test_a_location_and_quantity_edit_together_log_both_halves(conn, world):
    lot = writes.add_collection_lot(conn, world["ring"], quantity=2, location="Box")
    writes.update_collection_lot(conn, lot, location="Gisa", quantity=3)
    # The two copies it held moved; the third arrived where the lot now is.
    assert _log(conn)[1:] == [
        ("Sol Ring", 2, "Box", "Gisa", "collection_edit"),
        ("Sol Ring", 1, None, "Gisa", "collection_edit"),
    ]


def test_an_untracked_lot_logs_only_its_location_move(conn, world):
    lot = writes.add_collection_lot(conn, world["ring"], quantity=None, location="Box")
    writes.update_collection_lot(conn, lot, location="Gisa")
    assert _log(conn)[1:] == [("Sol Ring", None, "Box", "Gisa", "collection_edit")]


def test_edits_that_move_nothing_log_nothing(conn, world):
    lot = writes.add_collection_lot(conn, world["ring"], quantity=1, location="Box")
    writes.update_collection_lot(conn, lot, foil=True, price_paid=3.5, source="CK", location="Box")
    assert len(_log(conn)) == 1


def test_removing_a_lot_logs_it_as_deleted_and_the_history_outlives_it(conn, world):
    lot = writes.add_collection_lot(conn, world["ring"], quantity=2, location="Gisa")
    writes.remove_collection_lot(conn, lot)
    assert _log(conn)[1:] == [("Sol Ring", 2, "Gisa", None, "collection_edit")]
    assert conn.execute("SELECT COUNT(*) FROM collection").fetchone()[0] == 0
    assert [m["to_location"] for m in q.collection_moves_for_card(conn, "sol ring")] == [None, "Gisa"]


def test_removing_a_lot_that_isnt_there_logs_nothing(conn, world):
    writes.remove_collection_lot(conn, 999)
    assert _log(conn) == []


def test_bulk_update_logs_each_change_and_passes_the_reason(conn, world):
    a = writes.add_collection_lot(conn, world["ring"], quantity=1, location="Box")
    b = writes.add_collection_lot(conn, world["signet"], quantity=1, location="Box")
    c = writes.add_collection_lot(conn, world["signet"], quantity=1, location="Box")
    del_before = len(_log(conn))
    writes.bulk_update_collection(
        conn, {a: {"location": "Gisa"}, b: {"_delete": True}, c: {"location": "Box"}},
        reason="reconcile",
    )
    assert _log(conn)[del_before:] == [
        ("Sol Ring", 1, "Box", "Gisa", "reconcile"),
        ("Arcane Signet", 1, "Box", None, "reconcile"),
    ]


# ------------------------------------------------------------------
# Deck renames, deletes, prune
# ------------------------------------------------------------------
def test_renaming_a_deck_logs_nothing(conn, world):
    writes.add_collection_lot(conn, world["ring"], quantity=1, location="Gisa")
    before = _log(conn)
    ok, err = writes.rename_deck(conn, world["gisa"], "Gisa the Bold")
    assert ok and err is None
    assert _log(conn) == before
    assert conn.execute("SELECT location FROM collection").fetchone()[0] == "Gisa the Bold"


def test_deleting_a_deck_logs_each_sleeved_lot_going_to_storage(conn, world):
    writes.add_collection_lot(conn, world["ring"], quantity=2, location="Gisa")
    writes.add_collection_lot(conn, world["signet"], quantity=1, location="Box")
    writes.delete_deck(conn, world["gisa"])
    assert _log(conn)[2:] == [("Sol Ring", 2, "Gisa", "Box", "deck_delete")]


def test_deleting_a_deck_without_clearing_locations_logs_nothing_more(conn, world):
    writes.add_collection_lot(conn, world["ring"], quantity=2, location="Gisa")
    writes.delete_deck(conn, world["gisa"], clear_collection_locations=False)
    assert len(_log(conn)) == 1


def test_prune_logs_the_lots_it_deletes_by_name(conn, world):
    writes.add_collection_lot(conn, world["ring"], quantity=0, location=None)
    writes.add_collection_lot(conn, world["signet"], quantity=1, location="Box")
    pruned = writes.prune_collection(conn)
    assert [name for _, name in pruned] == ["Sol Ring"]
    assert _log(conn)[2:] == [("Sol Ring", 0, "", None, "prune")]


# ------------------------------------------------------------------
# Phase 1 deck edits reach the log, with their deck_changes row
# ------------------------------------------------------------------
def test_a_hand_edit_logs_its_moves_with_the_history_row(conn, world):
    writes.add_collection_lot(conn, world["ring"], quantity=1, location="Box")
    moves = writes.add_deck_card(conn, world["gisa"], world["ring"], 1)
    assert [m["kind"] for m in moves] == ["move"]
    row = conn.execute("SELECT card_name, quantity, from_location, to_location, reason, change_id"
                       " FROM collection_moves ORDER BY move_id DESC LIMIT 1").fetchone()
    applied = conn.execute("SELECT change_id FROM deck_changes WHERE status='applied'").fetchone()[0]
    assert tuple(row) == ("Sol Ring", 1, "Box", "Gisa", "deck_edit", applied)


def test_a_starter_lot_is_logged_as_a_new_copy_record(conn, world):
    writes.add_deck_card(conn, world["gisa"], world["signet"], 1)
    assert _log(conn) == [("Arcane Signet", 1, None, "Gisa", "deck_edit")]


def test_removing_a_card_logs_the_copy_going_to_box(conn, world):
    writes.add_collection_lot(conn, world["ring"], quantity=1, location="Box")
    writes.add_deck_card(conn, world["gisa"], world["ring"], 1)
    writes.remove_deck_card(conn, world["gisa"], world["ring"])
    assert _log(conn)[-1] == ("Sol Ring", 1, "Gisa", "Box", "deck_edit")


def test_applying_a_planned_change_logs_as_apply_with_its_change_id(conn, world):
    writes.add_collection_lot(conn, world["ring"], quantity=1, location="Box")
    conn.execute("INSERT INTO deck_cards (deck_id, scryfall_id, quantity) VALUES (?,?,1)",
                 (world["gisa"], world["signet"]))
    writes.add_collection_lot(conn, world["signet"], quantity=1, location="Gisa")
    change = writes.add_change(conn, world["gisa"], add_scryfall_id=world["ring"],
                               remove_scryfall_id=world["signet"], status="planned")
    writes.apply_change(conn, change)
    rows = conn.execute(
        "SELECT card_name, from_location, to_location, reason, change_id FROM collection_moves"
        " WHERE reason='apply' ORDER BY move_id").fetchall()
    assert [tuple(r) for r in rows] == [
        ("Arcane Signet", "Gisa", "Box", "apply", change),
        ("Sol Ring", "Box", "Gisa", "apply", change),
    ]


def test_reconcile_and_dismantle_moves_carry_their_reasons(conn, world):
    lot = writes.add_collection_lot(conn, world["ring"], quantity=1, location="Box")
    writes.move_copies(conn, lot, "Gisa")
    assert _log(conn)[-1][4] == "reconcile"
    writes.move_copies(conn, lot, "Box", reason="dismantle")
    assert _log(conn)[-1][4] == "dismantle"


def test_undo_logs_the_copy_going_back(conn, world):
    writes.add_collection_lot(conn, world["ring"], quantity=1, location="Box")
    moves = writes.add_deck_card(conn, world["gisa"], world["ring"], 1)
    writes.revert_moves(conn, moves)
    assert _log(conn)[-1] == ("Sol Ring", 1, "Gisa", "Box", "deck_edit")


# ------------------------------------------------------------------
# Reading the log
# ------------------------------------------------------------------
def test_the_log_for_a_card_spans_printings_and_ignores_other_cards(conn, world, add_card):
    other_printing = add_card("Sol Ring", set_code="c21", collector_number="2")
    writes.add_collection_lot(conn, world["ring"], quantity=1, location="Box")
    writes.add_collection_lot(conn, other_printing, quantity=1, location="Box")
    writes.add_collection_lot(conn, world["signet"], quantity=1, location="Box")
    moves = q.collection_moves_for_card(conn, "SOL RING")
    assert len(moves) == 2 and all(m["to_location"] == "Box" for m in moves)
    assert moves[0]["move_id"] > moves[1]["move_id"]


def test_card_lookup_carries_the_move_log(conn, world):
    writes.add_collection_lot(conn, world["ring"], quantity=1, location="Box")
    result = q.card_lookup(conn, "Sol Ring")
    assert [m["to_location"] for m in result["moves"]] == ["Box"]


def test_a_move_reads_as_a_sentence():
    moved = {"moved_at": "2026-10-13 10:00:00", "quantity": 2, "from_location": "Box",
             "to_location": "Gisa", "reason": "deck_edit"}
    assert fmt.format_collection_move(moved) == "2026-10-13  ×2 Box → Gisa (deck edit)"
    new = {**moved, "from_location": None, "reason": "import", "quantity": 1}
    assert fmt.format_collection_move(new) == "2026-10-13  ×1 new copy record → Gisa (order import)"
    gone = {**moved, "from_location": "", "to_location": None, "reason": "prune"}
    assert fmt.format_collection_move(gone) == "2026-10-13  ×2 no location → deleted (pruned)"


def test_a_database_from_before_the_log_gets_the_table(conn):
    conn.execute("DROP TABLE collection_moves")
    conn.commit()
    q.ensure_schema(conn)
    assert conn.execute("SELECT COUNT(*) FROM collection_moves").fetchone()[0] == 0


# ------------------------------------------------------------------
# Deck sync: offers
# ------------------------------------------------------------------
def _offers(conn, mark):
    return [(o["deck_name"], o["card"], o["action"], o["quantity"], o["new_quantity"])
            for o in q.deck_sync_offers(conn, mark)]


def test_moving_a_lot_into_a_deck_offers_to_add_the_card(conn, world):
    lot = writes.add_collection_lot(conn, world["ring"], quantity=1, location="Box")
    mark = q.last_move_id(conn)
    writes.update_collection_lot(conn, lot, location="Gisa")
    assert _offers(conn, mark) == [("Gisa", "Sol Ring", "add", 1, 1)]


def test_no_offer_when_the_list_already_runs_the_card(conn, world):
    conn.execute("INSERT INTO deck_cards (deck_id, scryfall_id, quantity) VALUES (?,?,1)",
                 (world["gisa"], world["ring"]))
    lot = writes.add_collection_lot(conn, world["ring"], quantity=1, location="Box")
    mark = q.last_move_id(conn)
    writes.update_collection_lot(conn, lot, location="Gisa")
    assert _offers(conn, mark) == []


def test_an_offer_never_exceeds_what_the_save_moved(conn, world):
    # Two copies were already sleeved and unlisted before this save; the save
    # moved one more in, so it offers one, not three.
    writes.add_collection_lot(conn, world["ring"], quantity=2, location="Gisa")
    lot = writes.add_collection_lot(conn, world["ring"], quantity=1, location="Box")
    mark = q.last_move_id(conn)
    writes.update_collection_lot(conn, lot, location="Gisa")
    assert _offers(conn, mark) == [("Gisa", "Sol Ring", "add", 1, 1)]


def test_an_offer_bumps_the_printing_already_on_the_list(conn, world, add_card):
    other = add_card("Sol Ring", set_code="c21", collector_number="2")
    conn.execute("INSERT INTO deck_cards (deck_id, scryfall_id, quantity) VALUES (?,?,1)",
                 (world["gisa"], world["ring"]))
    writes.add_collection_lot(conn, world["ring"], quantity=1, location="Gisa")
    lot = writes.add_collection_lot(conn, other, quantity=1, location="Box")
    mark = q.last_move_id(conn)
    writes.update_collection_lot(conn, lot, location="Gisa")
    [offer] = q.deck_sync_offers(conn, mark)
    assert (offer["scryfall_id"], offer["list_quantity"], offer["new_quantity"]) == (world["ring"], 1, 2)


def test_moving_a_lot_out_of_a_deck_offers_to_take_the_card_off(conn, world):
    conn.execute("INSERT INTO deck_cards (deck_id, scryfall_id, quantity) VALUES (?,?,2)",
                 (world["gisa"], world["ring"]))
    lot = writes.add_collection_lot(conn, world["ring"], quantity=2, location="Gisa")
    mark = q.last_move_id(conn)
    writes.move_lot(conn, lot, "Box", quantity=1)
    assert _offers(conn, mark) == [("Gisa", "Sol Ring", "remove", 1, 1)]


def test_deleting_a_sleeved_lot_offers_to_take_the_card_off(conn, world):
    conn.execute("INSERT INTO deck_cards (deck_id, scryfall_id, quantity) VALUES (?,?,1)",
                 (world["gisa"], world["ring"]))
    lot = writes.add_collection_lot(conn, world["ring"], quantity=1, location="Gisa")
    mark = q.last_move_id(conn)
    writes.bulk_update_collection(conn, {lot: {"_delete": True}})
    assert _offers(conn, mark) == [("Gisa", "Sol Ring", "remove", 1, 0)]


def test_shrinking_a_sleeved_lot_offers_the_difference(conn, world):
    conn.execute("INSERT INTO deck_cards (deck_id, scryfall_id, quantity) VALUES (?,?,3)",
                 (world["gisa"], world["ring"]))
    lot = writes.add_collection_lot(conn, world["ring"], quantity=3, location="Gisa")
    mark = q.last_move_id(conn)
    writes.update_collection_lot(conn, lot, quantity=1)
    assert _offers(conn, mark) == [("Gisa", "Sol Ring", "remove", 2, 1)]


def test_a_move_between_decks_offers_both_sides(conn, world):
    conn.execute("INSERT INTO deck_cards (deck_id, scryfall_id, quantity) VALUES (?,?,1)",
                 (world["gisa"], world["ring"]))
    lot = writes.add_collection_lot(conn, world["ring"], quantity=1, location="Gisa")
    mark = q.last_move_id(conn)
    writes.update_collection_lot(conn, lot, location="Tuvasa")
    assert _offers(conn, mark) == [
        ("Gisa", "Sol Ring", "remove", 1, 0),
        ("Tuvasa", "Sol Ring", "add", 1, 1),
    ]


def test_a_lot_leaving_a_deck_whose_list_never_ran_it_offers_nothing(conn, world):
    lot = writes.add_collection_lot(conn, world["ring"], quantity=1, location="Gisa")  # a stray
    mark = q.last_move_id(conn)
    writes.move_lot(conn, lot, "Box")
    assert _offers(conn, mark) == []


def test_storage_to_storage_offers_nothing(conn, world):
    lot = writes.add_collection_lot(conn, world["ring"], quantity=1, location="Box")
    mark = q.last_move_id(conn)
    writes.move_lot(conn, lot, "Lands Box")
    assert _offers(conn, mark) == []


def test_a_new_lot_straight_into_a_deck_offers_to_add_it(conn, world):
    mark = q.last_move_id(conn)
    writes.add_collection_lot(conn, world["signet"], quantity=2, location="Tuvasa", reason="import")
    assert _offers(conn, mark) == [("Tuvasa", "Arcane Signet", "add", 2, 2)]


def test_untracked_basic_lands_are_never_offered(conn, world):
    conn.execute("INSERT INTO cards (scryfall_id, name, set_code, collector_number, oracle_id, is_basic_land)"
                 " VALUES ('swamp', 'Swamp', 'cmd', '1', 'oracle-swamp', 1)")
    mark = q.last_move_id(conn)
    writes.add_collection_lot(conn, "swamp", quantity=None, location="Gisa")
    assert _offers(conn, mark) == []


# ------------------------------------------------------------------
# Deck sync: applying
# ------------------------------------------------------------------
def test_following_an_add_changes_the_list_and_history_but_moves_nothing_twice(conn, world):
    lot = writes.add_collection_lot(conn, world["ring"], quantity=1, location="Box")
    mark = q.last_move_id(conn)
    writes.update_collection_lot(conn, lot, location="Gisa")
    logged, lots = _log(conn), conn.execute("SELECT * FROM collection").fetchall()

    lines, moves = writes.sync_deck_lists(conn, q.deck_sync_offers(conn, mark))

    assert moves == [] and lines == ["Gisa: added Sol Ring ×1 to the list."]
    assert conn.execute("SELECT scryfall_id, quantity FROM deck_cards WHERE deck_id=?",
                        (world["gisa"],)).fetchall()[0][1] == 1
    assert _log(conn) == logged                      # no second move logged
    assert [tuple(r) for r in conn.execute("SELECT * FROM collection")] == [tuple(r) for r in lots]
    note = conn.execute("SELECT notes FROM deck_changes WHERE status='applied'").fetchone()[0]
    assert note == writes.SYNC_NOTE


def test_following_a_removal_takes_the_card_off_and_leaves_the_lot_alone(conn, world):
    conn.execute("INSERT INTO deck_cards (deck_id, scryfall_id, quantity) VALUES (?,?,1)",
                 (world["gisa"], world["ring"]))
    lot = writes.add_collection_lot(conn, world["ring"], quantity=1, location="Gisa")
    mark = q.last_move_id(conn)
    writes.move_lot(conn, lot, "Lands Box")
    lines, moves = writes.sync_deck_lists(conn, q.deck_sync_offers(conn, mark))
    assert moves == [] and lines == ["Gisa: took Sol Ring off the list."]
    assert conn.execute("SELECT COUNT(*) FROM deck_cards").fetchone()[0] == 0
    assert conn.execute("SELECT location FROM collection").fetchone()[0] == "Lands Box"


def test_following_a_partial_removal_lowers_the_quantity(conn, world):
    conn.execute("INSERT INTO deck_cards (deck_id, scryfall_id, quantity) VALUES (?,?,3)",
                 (world["gisa"], world["ring"]))
    lot = writes.add_collection_lot(conn, world["ring"], quantity=3, location="Gisa")
    mark = q.last_move_id(conn)
    writes.move_lot(conn, lot, "Box", quantity=1)
    lines, _ = writes.sync_deck_lists(conn, q.deck_sync_offers(conn, mark))
    assert lines == ["Gisa: Sol Ring down to ×2 on the list."]
    assert conn.execute("SELECT quantity FROM deck_cards").fetchone()[0] == 2
    assert sorted(r[0] for r in conn.execute("SELECT quantity FROM collection")) == [1, 2]


def test_the_offers_clear_once_the_list_follows(conn, world):
    lot = writes.add_collection_lot(conn, world["ring"], quantity=1, location="Box")
    mark = q.last_move_id(conn)
    writes.update_collection_lot(conn, lot, location="Gisa")
    writes.sync_deck_lists(conn, q.deck_sync_offers(conn, mark))
    assert q.deck_sync_offers(conn, mark) == []


def test_an_offer_for_a_deck_deleted_since_is_skipped(conn, world):
    lot = writes.add_collection_lot(conn, world["ring"], quantity=1, location="Box")
    mark = q.last_move_id(conn)
    writes.update_collection_lot(conn, lot, location="Gisa")
    offers = q.deck_sync_offers(conn, mark)
    writes.delete_deck(conn, world["gisa"])
    assert writes.sync_deck_lists(conn, offers) == ([], [])
