"""writes.* collection-lot behavior — the code behind the Collection
page's Edit mode (bulk_update_collection) plus add/remove/prune."""
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
        keep: {"_delete": False, "quantity": 1, "foil": False, "location": None,
               "date_acquired": None, "price_paid": None, "source": None},
    })
    assert changed == 1
    assert conn.execute("SELECT collection_id FROM collection").fetchall()[0][0] == keep


def test_bulk_update_empty_and_unknown_ids(conn):
    assert writes.bulk_update_collection(conn, {}) == 0
    assert writes.bulk_update_collection(conn, {9999: {"_delete": True}}) == 0


def test_prune_removes_only_unhomed_zero_qty_lots_not_in_a_deck(conn, add_card):
    stale = writes.add_collection_lot(conn, add_card("Stale"), quantity=0)
    blank_loc = writes.add_collection_lot(conn, add_card("BlankLoc"), quantity=None, location="  ")
    located = writes.add_collection_lot(conn, add_card("Located"), quantity=0, location="Box")
    owned = writes.add_collection_lot(conn, add_card("Owned"), quantity=2)
    in_deck_sid = add_card("InDeck")
    in_deck = writes.add_collection_lot(conn, in_deck_sid, quantity=0)
    conn.execute("INSERT INTO decks (name) VALUES ('D')")
    deck_id = conn.execute("SELECT deck_id FROM decks").fetchone()[0]
    conn.execute("INSERT INTO deck_cards (deck_id, scryfall_id, quantity) VALUES (?,?,1)", (deck_id, in_deck_sid))
    conn.commit()

    removed = {cid for cid, _ in writes.prune_collection(conn)}
    assert removed == {stale, blank_loc}
    remaining = {r[0] for r in conn.execute("SELECT collection_id FROM collection")}
    assert remaining == {located, owned, in_deck}
