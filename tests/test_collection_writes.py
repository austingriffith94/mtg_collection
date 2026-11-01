"""writes.* collection-lot behavior — the code behind the Collection
page's Edit mode (bulk_update_collection) plus add/remove/prune."""
import pytest

from dashboard_lib import writes


def _lot(conn, cid):
    return dict(conn.execute("SELECT * FROM collection WHERE collection_id=?", (cid,)).fetchone())


def test_add_lot_normalizes_foil(conn, add_card):
    cid = writes.add_collection_lot(conn, add_card("Sol Ring"), quantity=2, foil=True, location="Box")
    assert _lot(conn, cid)["foil"] == 1
    cid2 = writes.add_collection_lot(conn, add_card("Arcane Signet"), quantity=1)
    assert _lot(conn, cid2)["foil"] == 0


def test_bulk_update_changes_only_edited_fields(conn, add_card):
    cid = writes.add_collection_lot(conn, add_card("Sol Ring"), quantity=1, location="Box", source="CK")
    changed = writes.bulk_update_collection(conn, {cid: {
        "_delete": False, "quantity": 3, "foil": False, "location": "Box",
        "date_acquired": None, "price_paid": None, "source": "CK",
    }})
    assert changed == 1
    lot = _lot(conn, cid)
    assert lot["quantity"] == 3 and lot["location"] == "Box" and lot["source"] == "CK"


def test_bulk_update_unchanged_rows_not_counted(conn, add_card):
    cid = writes.add_collection_lot(conn, add_card("Sol Ring"), quantity=1, foil=True, location="Box")
    same = {"_delete": False, "quantity": 1, "foil": True, "location": "Box",
            "date_acquired": None, "price_paid": None, "source": None}
    assert writes.bulk_update_collection(conn, {cid: same}) == 0


def test_bulk_update_clears_location_to_null(conn, add_card):
    cid = writes.add_collection_lot(conn, add_card("Sol Ring"), quantity=1, location="Box")
    writes.bulk_update_collection(conn, {cid: {
        "_delete": False, "quantity": 1, "foil": False, "location": None,
        "date_acquired": None, "price_paid": None, "source": None,
    }})
    assert _lot(conn, cid)["location"] is None


def test_bulk_update_delete_removes_row_and_ignores_other_edits(conn, add_card):
    keep = writes.add_collection_lot(conn, add_card("Sol Ring"), quantity=1)
    gone = writes.add_collection_lot(conn, add_card("Arcane Signet"), quantity=1)
    changed = writes.bulk_update_collection(conn, {
        gone: {"_delete": True, "quantity": 99},
        keep: {"_delete": False, "quantity": 1, "foil": False, "location": writes.DEFAULT_LOCATION,
               "date_acquired": None, "price_paid": None, "source": None},
    })
    assert changed == 1
    assert conn.execute("SELECT collection_id FROM collection").fetchall()[0][0] == keep


def _basic(conn, add_card):
    sid = add_card("Swamp")
    conn.execute("UPDATE cards SET is_basic_land=1 WHERE scryfall_id=?", (sid,))
    conn.commit()
    return sid


@pytest.mark.parametrize("given,kept", [
    ({"quantity": 2}, (2, writes.DEFAULT_LOCATION)),
    ({"location": "Binder"}, (1, "Binder")),
    ({"quantity": 0, "location": "Binder"}, (1, "Binder")),
    ({"quantity": 2, "location": "  "}, (2, writes.DEFAULT_LOCATION)),
    ({"quantity": 2, "location": "Binder"}, (2, "Binder")),
], ids=["no-location", "no-quantity", "zero-quantity", "blank-location", "both"])
def test_a_new_lot_has_the_missing_field_filled_in(conn, add_card, given, kept):
    row = _lot(conn, writes.add_collection_lot(conn, add_card("Sol Ring"), **given))
    assert (row["quantity"], row["location"]) == kept


def test_fill_lot_fields_says_what_it_filled_in(conn, add_card):
    sid = add_card("Sol Ring")
    assert writes.fill_lot_fields(conn, sid, 2, "Binder") == (2, "Binder", [])
    assert writes.fill_lot_fields(conn, sid, None, "Binder") == (1, "Binder", ["quantity set to 1"])
    assert writes.fill_lot_fields(conn, sid, 2, None) == (2, "Box", ["Location set to Box"])


@pytest.mark.parametrize("given", [{}, {"quantity": 0}, {"quantity": None, "location": "  "}])
def test_a_new_lot_with_neither_field_is_refused(conn, add_card, given):
    with pytest.raises(ValueError, match="quantity or a Location"):
        writes.add_collection_lot(conn, add_card("Sol Ring"), **given)
    assert conn.execute("SELECT COUNT(*) FROM collection").fetchone()[0] == 0


def test_a_basic_land_lot_needs_neither_field(conn, add_card):
    row = _lot(conn, writes.add_collection_lot(conn, _basic(conn, add_card)))
    assert (row["quantity"], row["location"]) == (None, None)


def test_an_import_keeps_its_lot_unlocated(conn, add_card):
    row = _lot(conn, writes.add_collection_lot(conn, add_card("Sol Ring"), quantity=2, reason="import"))
    assert (row["quantity"], row["location"]) == (2, None)


@pytest.mark.parametrize("start,edit", [
    ({"quantity": 1, "location": "Box"}, {"quantity": None, "location": None}),
    ({"quantity": 1, "location": "Box"}, {"quantity": 0, "location": ""}),
    ({"quantity": None, "location": "Box"}, {"location": None}),
    ({"quantity": 1, "location": None}, {"quantity": None}),
], ids=["both", "zero-and-empty", "location-of-untracked", "quantity-of-unlocated"])
def test_an_edit_that_blanks_a_lot_is_refused(conn, add_card, raw_lot, start, edit):
    cid = raw_lot(add_card("Sol Ring"), start["quantity"], start["location"])
    with pytest.raises(ValueError, match="quantity or a Location"):
        writes.update_collection_lot(conn, cid, **edit)
    with pytest.raises(ValueError, match="quantity or a Location"):
        writes.bulk_update_collection(conn, {cid: edit})
    row = _lot(conn, cid)
    assert (row["quantity"], row["location"]) == (start["quantity"], start["location"])


def test_a_refused_bulk_save_writes_none_of_its_rows(conn, add_card):
    fine = writes.add_collection_lot(conn, add_card("Sol Ring"), quantity=1, location="Box")
    bad = writes.add_collection_lot(conn, add_card("Arcane Signet"), quantity=1, location="Box")
    with pytest.raises(ValueError, match="Arcane Signet"):
        writes.bulk_update_collection(conn, {fine: {"quantity": 5}, bad: {"quantity": None, "location": None}})
    assert _lot(conn, fine)["quantity"] == 1


def test_clearing_one_field_is_allowed(conn, add_card):
    cid = writes.add_collection_lot(conn, add_card("Sol Ring"), quantity=1, location="Box")
    writes.update_collection_lot(conn, cid, location=None)
    assert _lot(conn, cid)["location"] is None


def test_a_lot_that_is_already_blank_can_still_be_edited(conn, add_card, raw_lot):
    cid = raw_lot(add_card("Sol Ring"))
    writes.update_collection_lot(conn, cid, price_paid=2.5)
    assert _lot(conn, cid)["price_paid"] == 2.5


def test_a_basic_land_lot_can_be_blanked(conn, add_card):
    cid = writes.add_collection_lot(conn, _basic(conn, add_card), quantity=4, location="Lands")
    writes.update_collection_lot(conn, cid, quantity=None, location=None)
    assert _lot(conn, cid)["location"] is None


def test_moving_an_untracked_lot_to_no_location_is_not_an_edit(conn, add_card, raw_lot):
    # What Undo does for a lot that Auto pulled in from no Location.
    cid = raw_lot(add_card("Sol Ring"), None, "Gisa")
    assert writes.move_lot(conn, cid, None) == cid
    assert _lot(conn, cid)["location"] is None


def test_bulk_update_empty_and_unknown_ids(conn):
    assert writes.bulk_update_collection(conn, {}) == 0
    assert writes.bulk_update_collection(conn, {9999: {"_delete": True}}) == 0


def test_prune_removes_only_unhomed_zero_qty_lots_not_in_a_deck(conn, add_card, raw_lot):
    stale = raw_lot(add_card("Stale"), 0)
    blank_loc = raw_lot(add_card("BlankLoc"), None, "  ")
    located = raw_lot(add_card("Located"), 0, "Box")
    owned = writes.add_collection_lot(conn, add_card("Owned"), quantity=2)
    in_deck_sid = add_card("InDeck")
    in_deck = raw_lot(in_deck_sid, 0)
    conn.execute("INSERT INTO decks (name) VALUES ('D')")
    deck_id = conn.execute("SELECT deck_id FROM decks").fetchone()[0]
    conn.execute("INSERT INTO deck_cards (deck_id, scryfall_id, quantity) VALUES (?,?,1)", (deck_id, in_deck_sid))
    conn.commit()

    removed = {cid for cid, _ in writes.prune_collection(conn)}
    assert removed == {stale, blank_loc}
    remaining = {r[0] for r in conn.execute("SELECT collection_id FROM collection")}
    assert remaining == {located, owned, in_deck}


def test_consolidate_merges_interchangeable_lots_only(conn, add_card):
    sid = add_card("Sol Ring")
    a = writes.add_collection_lot(conn, sid, quantity=1, location="Box", price_paid=1.0)
    writes.add_collection_lot(conn, sid, quantity=2, location="Box", price_paid=0.5)
    foil = writes.add_collection_lot(conn, sid, quantity=1, foil=True, location="Box")
    other = writes.add_collection_lot(conn, sid, quantity=1, location="Binder")
    assert writes.consolidate_lots(conn) == 1
    assert _lot(conn, a)["quantity"] == 3 and _lot(conn, a)["price_paid"] == 1.5
    assert {foil, other} <= {r[0] for r in conn.execute("SELECT collection_id FROM collection")}
    assert writes.consolidate_lots(conn) == 0


def test_move_lot_merges_into_existing_lot_at_destination(conn, add_card):
    sid = add_card("Sol Ring")
    src = writes.add_collection_lot(conn, sid, quantity=3, location="Box")
    dest = writes.add_collection_lot(conn, sid, quantity=1, location="Binder")
    assert writes.move_lot(conn, src, "Binder", quantity=2) == dest
    assert _lot(conn, dest)["quantity"] == 3 and _lot(conn, src)["quantity"] == 1
