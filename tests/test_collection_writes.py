"""writes.* collection-lot behavior — the code behind the Collection
page's Edit mode (bulk_update_collection) plus add/remove/prune."""
import os
import sqlite3

import pytest

from dashboard_lib import backup, queries, writes

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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


def test_prune_removes_every_blank_lot_even_on_a_deck_list(conn, add_card, raw_lot):
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
    assert removed == {stale, blank_loc, in_deck}
    remaining = {r[0] for r in conn.execute("SELECT collection_id FROM collection")}
    assert remaining == {located, owned}


def test_prune_keeps_a_blank_basic_land(conn, add_card, raw_lot):
    cid = raw_lot(_basic(conn, add_card))
    assert writes.prune_collection(conn) == []
    assert _lot(conn, cid) is not None


def test_blank_lots_names_what_holds_each_one(conn, add_card, raw_lot):
    listed, idea, loose = add_card("Listed"), add_card("Idea"), add_card("Loose")
    reprint = add_card("Listed", set_code="sld", collector_number="9")
    gisa = conn.execute("INSERT INTO decks (name) VALUES ('Gisa')").lastrowid
    tuvasa = conn.execute("INSERT INTO decks (name) VALUES ('Tuvasa')").lastrowid
    conn.execute("INSERT INTO deck_cards (deck_id, scryfall_id, quantity) VALUES (?,?,1)", (gisa, listed))
    writes.add_change(conn, tuvasa, add_scryfall_id=idea)
    writes.add_change(conn, tuvasa, add_scryfall_id=listed)
    for sid in (reprint, idea, loose):
        raw_lot(sid)
    raw_lot(listed, 1, "Box")                                # a real copy: not blank
    raw_lot(_basic(conn, add_card))                          # a basic: exempt
    frame = queries.blank_lots(conn)
    assert dict(zip(frame["card"], frame["held_by"])) == {
        "Idea": "idea: Tuvasa", "Listed": "Gisa; idea: Tuvasa", "Loose": ""}
    assert frame[frame["card"] == "Listed"]["printing"].tolist() == ["SLD #9"]
    assert len(queries.orphan_report(conn)["blank_lots"]) == 3


def test_a_zero_quantity_counts_like_no_quantity(conn, add_card, raw_lot):
    """With a Location, no quantity (blank or 0) is one copy; with neither it is no copy."""
    sid = add_card("Sol Ring")
    blank, zero, none = raw_lot(sid, 0, "Box"), raw_lot(sid, 0, "Gisa"), raw_lot(sid, None, "Binder")
    assert [(cid, qty) for cid, qty, _loc, _sid in queries.card_lots(conn, sid)] == [(blank, 1), (zero, 1), (none, 1)]
    assert [qty for _cid, qty, _loc in queries._lots_for_card(conn, scryfall_id=sid)] == [1, 1, 1]
    with pytest.raises(ValueError, match="quantity or a Location"):   # it can't lose its Location too
        writes.update_collection_lot(conn, zero, location=None)
    assert queries.lots_missing_a_field(conn, "quantity")["card"].tolist() == ["Sol Ring"] * 3


def test_the_clean_up_checks_list_lots_missing_one_field(conn, add_card, raw_lot):
    raw_lot(add_card("Counted"), 2, None)
    raw_lot(add_card("Placed"), None, "Gisa")
    raw_lot(add_card("Zero"), 0, "Box")
    raw_lot(add_card("Whole"), 1, "Box")
    raw_lot(add_card("Blank"))
    swamp = _basic(conn, add_card)                            # basics are exempt from both
    raw_lot(swamp, 5, None)
    raw_lot(swamp, None, "Lands")
    report = queries.orphan_report(conn)
    assert report["no_location"]["card"].tolist() == ["Counted"]
    assert report["no_quantity"]["card"].tolist() == ["Placed", "Zero"]
    assert report["no_quantity"]["location"].tolist() == ["Gisa", "Box"]
    assert report["blank_lots"]["card"].tolist() == ["Blank"]       # the blank lot is not in either


def test_delete_blank_lots_rechecks_each_lot_and_logs_nothing(conn, add_card, raw_lot):
    gone, filled = raw_lot(add_card("Gone")), raw_lot(add_card("Filled"))
    writes.update_collection_lot(conn, filled, location="Box")   # since the preview
    conn.execute("DELETE FROM collection_moves")
    conn.commit()
    assert writes.delete_blank_lots(conn, [gone, filled]) == 1
    assert [r[0] for r in conn.execute("SELECT collection_id FROM collection")] == [filled]
    assert conn.execute("SELECT COUNT(*) FROM collection_moves").fetchone()[0] == 0
    assert writes.delete_blank_lots(conn, []) == 0


def _file_db_with_a_blank_lot(tmp_path):
    path = str(tmp_path / "mtg_collection.db")
    c = sqlite3.connect(path)
    with open(os.path.join(ROOT, "schema.sql"), encoding="utf-8") as f:
        c.executescript(f.read())
    queries.ensure_schema(c)
    c.execute("INSERT INTO cards (scryfall_id, name, set_code, collector_number) VALUES ('a','A','cmd','1')")
    cid = c.execute("INSERT INTO collection (scryfall_id) VALUES ('a')").lastrowid
    c.commit()
    return path, c, cid


def test_delete_blank_lots_refuses_to_run_if_the_backup_fails(tmp_path, monkeypatch):
    path, c, cid = _file_db_with_a_blank_lot(tmp_path)
    monkeypatch.setattr(backup, "snapshot", lambda *a, **k: None)
    with pytest.raises(RuntimeError, match="backup failed"):
        writes.delete_blank_lots(c, [cid])
    assert c.execute("SELECT COUNT(*) FROM collection").fetchone()[0] == 1
    c.close()


def test_delete_blank_lots_writes_a_backup_of_the_lots_first(tmp_path):
    path, c, cid = _file_db_with_a_blank_lot(tmp_path)
    assert writes.delete_blank_lots(c, [cid]) == 1
    c.close()
    (saved,) = os.listdir(backup.backup_dir_for(path))
    with sqlite3.connect(os.path.join(backup.backup_dir_for(path), saved)) as old:
        assert old.execute("SELECT COUNT(*) FROM collection").fetchone()[0] == 1


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
