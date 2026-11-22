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


def _dup_lot(conn, sid, quantity, location, *, foil=0, date=None, source=None, paid=None):
    """A row inserted straight past the writers: the only way to get two rows
    at one place, as the live database has from before the merge rule."""
    cur = conn.execute(
        "INSERT INTO collection (scryfall_id, quantity, foil, location, date_acquired, source, price_paid)"
        " VALUES (?,?,?,?,?,?,?)", (sid, quantity, foil, location, date, source, paid))
    conn.commit()
    return cur.lastrowid


# ------------------------------------------------------------------
# The one-time merge (docs/LOT_CONSOLIDATION_PLAN.md Phase 2). A database
# from before the merge rule has no index and can hold rows sharing a
# place; `legacy` drops the index to get one.
# ------------------------------------------------------------------
def _has_place_index(conn):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE name='idx_collection_place'").fetchone() is not None


@pytest.fixture
def legacy(conn):
    conn.execute("DROP INDEX idx_collection_place")
    return conn


def test_consolidate_merges_rows_sharing_a_place_whatever_their_source(legacy, add_card):
    conn = legacy
    sid = add_card("Sol Ring")
    a = _dup_lot(conn, sid, 1, "Box", date="2022-02-27", source="CK", paid=1.0)
    _dup_lot(conn, sid, 2, "Box ", date="2021-12-03", source="CS", paid=0.5)
    foil = _dup_lot(conn, sid, 1, "Box", foil=1)
    other = _dup_lot(conn, sid, 1, "Binder")
    assert writes.consolidate_lots(conn) == 1
    lot = _lot(conn, a)
    assert (lot["quantity"], lot["price_paid"]) == (3, 1.5)
    assert (lot["date_acquired"], lot["source"]) == ("2021-12-03", "CS")   # the older purchase
    assert {foil, other} <= {r[0] for r in conn.execute("SELECT collection_id FROM collection")}
    assert _has_place_index(conn)
    assert writes.consolidate_lots(conn) == 0


def test_consolidate_keeps_every_copy_and_dollar_across_the_live_shapes(legacy, add_card):
    """The shapes of the live database's 8 groups (plan, Data findings):
    identical triples, differing prices, sources and dates, a counted row
    beside a NULL price, a multi-copy row, plus uncounted basics."""
    conn = legacy
    stone, ring, orchard, seer = (add_card(n) for n in
                                  ("Fellwar Stone", "Sol Ring", "Exotic Orchard", "Viscera Seer"))
    conn.execute("UPDATE cards SET is_basic_land=1 WHERE scryfall_id=?", (forest := add_card("Forest"),))
    for _ in range(3):
        _dup_lot(conn, stone, 1, "Box", date="2022-05-18", source="TCG", paid=0.58)
    _dup_lot(conn, ring, 1, "Box", date="2022-02-27", source="CK", paid=2.29)
    _dup_lot(conn, ring, 1, "Box", date="2022-02-27", source="CK", paid=1.99)
    _dup_lot(conn, orchard, 1, "Lands", date="2022-12-27", source="TCG", paid=0.24)
    _dup_lot(conn, orchard, 1, "Lands", date="2021-12-14", source="CS", paid=0.49)
    _dup_lot(conn, seer, 1, "Box", date="2021-12-31", source="CK", paid=0.39)
    _dup_lot(conn, seer, 3, "Box", date="2022-03-26", source="CMR 1")
    _dup_lot(conn, forest, None, "Lands")
    _dup_lot(conn, forest, None, "Lands")
    _dup_lot(conn, seer, 1, "Binder")                      # alone at its place: untouched

    def totals():
        return conn.execute("SELECT COUNT(*), SUM(COALESCE(NULLIF(quantity,0),0)),"
                            " ROUND(SUM(COALESCE(price_paid,0)),2) FROM collection").fetchone()
    rows, copies, paid = totals()
    assert queries.place_duplicate_count(conn) == 6

    assert writes.consolidate_lots(conn) == 6
    assert tuple(totals()) == (rows - 6, copies, paid)
    by_card = {(r["scryfall_id"], r["location"]): r for r in _rows_all(conn)}
    assert by_card[(stone, "Box")]["quantity"] == 3
    assert (by_card[(orchard, "Lands")]["source"], by_card[(orchard, "Lands")]["date_acquired"]) \
        == ("CS", "2021-12-14")                            # the older purchase
    assert (by_card[(seer, "Box")]["quantity"], by_card[(seer, "Box")]["source"]) == (4, "CK")
    assert by_card[(forest, "Lands")]["quantity"] is None  # basics stay a pile
    assert queries.place_duplicate_count(conn) == 0 and _has_place_index(conn)


def _rows_all(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM collection ORDER BY collection_id")]


def test_ensure_schema_skips_the_index_while_duplicates_remain(legacy, add_card, caplog):
    conn = legacy
    sid = add_card("Sol Ring")
    _dup_lot(conn, sid, 1, "Box")
    _dup_lot(conn, sid, 1, "Box")
    queries.ensure_schema(conn)                            # no raise: every page still opens
    assert not _has_place_index(conn)
    assert "idx_collection_place not created" in caplog.text
    writes.consolidate_lots(conn)
    queries.ensure_schema(conn)
    assert _has_place_index(conn)


def test_consolidate_merges_nothing_when_the_backup_fails(legacy, add_card, monkeypatch):
    conn = legacy
    sid = add_card("Sol Ring")
    _dup_lot(conn, sid, 1, "Box")
    _dup_lot(conn, sid, 1, "Box")

    def failed(_conn):
        raise RuntimeError("The database backup failed, so nothing was changed. See the log.")
    monkeypatch.setattr(backup, "required_snapshot", failed)
    with pytest.raises(RuntimeError):
        writes.consolidate_lots(conn)
    assert len(_rows(conn, sid)) == 2 and not _has_place_index(conn)


def test_consolidate_takes_no_backup_when_there_is_nothing_to_merge(conn, add_card, monkeypatch):
    writes.add_collection_lot(conn, add_card("Sol Ring"), quantity=1, location="Box")
    monkeypatch.setattr(backup, "required_snapshot", lambda _c: pytest.fail("backed up for nothing"))
    assert writes.consolidate_lots(conn) == 0


def test_move_lot_merges_into_existing_lot_at_destination(conn, add_card):
    sid = add_card("Sol Ring")
    src = writes.add_collection_lot(conn, sid, quantity=3, location="Box")
    dest = writes.add_collection_lot(conn, sid, quantity=1, location="Binder")
    assert writes.move_lot(conn, src, "Binder", quantity=2) == dest
    assert _lot(conn, dest)["quantity"] == 3 and _lot(conn, src)["quantity"] == 1


# ------------------------------------------------------------------
# One row per printing, finish and Location (docs/LOT_CONSOLIDATION_PLAN.md)
# ------------------------------------------------------------------
def _rows(conn, sid):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM collection WHERE scryfall_id=? ORDER BY collection_id", (sid,))]


def _deck(conn, name):
    cur = conn.execute("INSERT INTO decks (name) VALUES (?)", (name,))
    conn.commit()
    return cur.lastrowid


def _list(conn, deck_id, sid, quantity):
    conn.execute("INSERT INTO deck_cards (deck_id, scryfall_id, quantity) VALUES (?,?,?)",
                 (deck_id, sid, quantity))
    conn.commit()


def _log(conn):
    return [tuple(r) for r in conn.execute(
        "SELECT collection_id, quantity, from_location, to_location, reason "
        "FROM collection_moves ORDER BY move_id")]


def test_moving_a_whole_lot_onto_a_twin_sums_it_and_keeps_the_older_purchase(conn, add_card):
    sid = add_card("Sol Ring")
    box = writes.add_collection_lot(conn, sid, quantity=1, location="Box", price_paid=2.29,
                                    date_acquired="2022-02-27", source="CK")
    gisa = writes.add_collection_lot(conn, sid, quantity=2, location="Gisa", price_paid=1.99,
                                     date_acquired="2021-12-31", source="TCG")
    assert writes.move_lot(conn, gisa, "Box") == box          # the lower id survives
    (row,) = _rows(conn, sid)
    assert (row["collection_id"], row["quantity"], row["price_paid"]) == (box, 3, 4.28)
    assert (row["date_acquired"], row["source"], row["location"]) == ("2021-12-31", "TCG", "Box")
    assert _log(conn)[-1] == (box, 2, "Gisa", "Box", "collection_edit")


def test_the_moved_row_survives_when_its_id_is_lower(conn, add_card):
    sid = add_card("Sol Ring")
    first = writes.add_collection_lot(conn, sid, quantity=1, location="Gisa")
    writes.add_collection_lot(conn, sid, quantity=1, location="Box ")
    assert writes.move_lot(conn, first, "Box") == first
    (row,) = _rows(conn, sid)
    assert (row["collection_id"], row["quantity"], row["location"]) == (first, 2, "Box")


def test_a_partial_move_leaves_the_rest_and_merges_the_moved_part(conn, add_card):
    sid = add_card("Sol Ring")
    box = writes.add_collection_lot(conn, sid, quantity=3, location="Box", price_paid=3.0)
    gisa = writes.add_collection_lot(conn, sid, quantity=1, location="Gisa", price_paid=1.0)
    assert writes.move_lot(conn, box, "Gisa", quantity=2) == gisa
    rows = {r["location"]: (r["quantity"], r["price_paid"]) for r in _rows(conn, sid)}
    assert rows == {"Box": (1, 1.0), "Gisa": (3, 3.0)}


def test_other_finishes_and_printings_never_merge(conn, add_card):
    sid, reprint = add_card("Sol Ring"), add_card("Sol Ring", set_code="sld", collector_number="9")
    writes.add_collection_lot(conn, sid, quantity=1, location="Box")
    foil = writes.add_collection_lot(conn, sid, quantity=1, foil=True, location="Gisa")
    other = writes.add_collection_lot(conn, reprint, quantity=1, location="Gisa")
    writes.move_lot(conn, foil, "Box")
    writes.move_lot(conn, other, "Box")
    assert conn.execute("SELECT COUNT(*) FROM collection WHERE location='Box'").fetchone()[0] == 3


def test_adding_copies_to_an_occupied_place_adds_to_that_row(conn, add_card):
    sid = add_card("Sol Ring")
    first = writes.add_collection_lot(conn, sid, quantity=1, location="Box", price_paid=1.0)
    again = writes.add_collection_lot(conn, sid, quantity=2, location=" Box", price_paid=0.5)
    assert again == first
    (row,) = _rows(conn, sid)
    assert (row["quantity"], row["price_paid"]) == (3, 1.5)
    assert _log(conn) == [(first, 1, None, "Box", "collection_edit"),
                          (first, 2, None, "Box", "collection_edit")]


def test_an_import_merges_with_the_card_already_unlocated(conn, add_card):
    sid = add_card("Sol Ring")
    first = writes.add_collection_lot(conn, sid, quantity=1, location=None, reason="import")
    assert writes.add_collection_lot(conn, sid, quantity=1, location="", reason="import") == first
    assert [r["quantity"] for r in _rows(conn, sid)] == [2]


def test_tied_dates_keep_the_lower_ids_source_and_no_date_counts_as_newest(conn, add_card):
    sid, other = add_card("Sol Ring"), add_card("Arcane Signet")
    writes.add_collection_lot(conn, sid, quantity=1, location="Box", date_acquired="2022-01-01", source="A")
    writes.add_collection_lot(conn, sid, quantity=1, location="Box", date_acquired="2022-01-01", source="B")
    assert _rows(conn, sid)[0]["source"] == "A"
    writes.add_collection_lot(conn, other, quantity=1, location="Box", source="undated")
    writes.add_collection_lot(conn, other, quantity=1, location="Box", date_acquired="2026-01-01",
                              source="dated")
    (row,) = _rows(conn, other)
    assert (row["date_acquired"], row["source"]) == ("2026-01-01", "dated")


def test_an_uncounted_basic_pile_absorbs_counted_copies(conn, add_card, raw_lot):
    swamp = _basic(conn, add_card)
    pile = raw_lot(swamp, None, "Lands")
    counted = writes.add_collection_lot(conn, swamp, quantity=4, location="Gisa", price_paid=0.4)
    assert writes.move_lot(conn, counted, "Lands") == pile
    (row,) = _rows(conn, swamp)
    assert (row["quantity"], row["price_paid"]) == (None, 0.4)


def test_a_zero_count_basic_is_uncounted_too(conn, add_card, raw_lot):
    swamp = _basic(conn, add_card)
    zero = raw_lot(swamp, 0, "Lands")
    assert writes.add_collection_lot(conn, swamp, quantity=3, location="Lands") == zero
    assert _lot(conn, zero)["quantity"] is None


def test_an_uncounted_non_basic_row_merges_as_one_copy(conn, add_card, raw_lot):
    sid = add_card("Sol Ring")
    loose = raw_lot(sid, 0, "Box")
    assert writes.add_collection_lot(conn, sid, quantity=3, location="Box") == loose
    assert _lot(conn, loose)["quantity"] == 4


def test_deleting_a_deck_merges_its_cards_into_box_and_logs_each_move(conn, add_card):
    sid = add_card("Sol Ring")
    gisa = _deck(conn, "Gisa")
    box = writes.add_collection_lot(conn, sid, quantity=1, location="Box")
    writes.add_collection_lot(conn, sid, quantity=1, location="Gisa")
    _name, err, _r, _d = writes.delete_deck(conn, gisa)
    assert err is None
    assert [(r["collection_id"], r["quantity"], r["location"]) for r in _rows(conn, sid)] == [(box, 2, "Box")]
    assert _log(conn)[-1] == (box, 1, "Gisa", "Box", "deck_delete")


def test_renaming_a_deck_onto_a_storage_name_merges(conn, add_card):
    sid = add_card("Sol Ring")
    gisa = _deck(conn, "Gisa")
    writes.add_collection_lot(conn, sid, quantity=1, location="Shelf")
    writes.add_collection_lot(conn, sid, quantity=2, location="Gisa")
    ok, err = writes.rename_deck(conn, gisa, "Shelf")
    assert ok and err is None
    assert [(r["quantity"], r["location"]) for r in _rows(conn, sid)] == [(3, "Shelf")]


def test_a_grid_location_edit_onto_an_occupied_place_merges(conn, add_card):
    sid = add_card("Sol Ring")
    box = writes.add_collection_lot(conn, sid, quantity=1, location="Box")
    gisa = writes.add_collection_lot(conn, sid, quantity=1, location="Gisa")
    assert writes.bulk_update_collection(conn, {gisa: {"location": "Box"}}) == 1
    assert [(r["collection_id"], r["quantity"]) for r in _rows(conn, sid)] == [(box, 2)]


def test_a_printing_change_onto_one_already_there_merges(conn, add_card):
    sid, reprint = add_card("Sol Ring"), add_card("Sol Ring", set_code="sld", collector_number="9")
    there = writes.add_collection_lot(conn, reprint, quantity=1, location="Box")
    wrong = writes.add_collection_lot(conn, sid, quantity=1, location="Box")
    assert writes.set_lot_printing(conn, wrong, reprint) is True
    assert _rows(conn, sid) == [] and _lot(conn, there)["quantity"] == 2


def test_a_save_changing_printing_and_quantity_keeps_both(conn, add_card):
    sid, reprint = add_card("Sol Ring"), add_card("Sol Ring", set_code="sld", collector_number="9")
    there = writes.add_collection_lot(conn, reprint, quantity=1, location="Box")
    wrong = writes.add_collection_lot(conn, sid, quantity=1, location="Box")
    writes.bulk_update_collection(conn, {wrong: {"quantity": 3}}, printings={wrong: reprint})
    assert [(r["collection_id"], r["quantity"]) for r in _rows(conn, reprint)] == [(there, 4)]
    assert _rows(conn, sid) == []


def test_a_save_moving_a_onto_b_and_editing_b_applies_b_first(conn, add_card):
    sid = add_card("Sol Ring")
    b = writes.add_collection_lot(conn, sid, quantity=1, location="Box", price_paid=1.0)
    a = writes.add_collection_lot(conn, sid, quantity=2, location="Gisa", price_paid=2.0)
    writes.bulk_update_collection(conn, {a: {"location": "Box"}, b: {"quantity": 5, "price_paid": 4.0}})
    (row,) = _rows(conn, sid)
    assert (row["collection_id"], row["quantity"], row["price_paid"]) == (b, 7, 6.0)


def test_a_save_that_vacates_a_place_before_filling_it_merges_nothing(conn, add_card):
    sid = add_card("Sol Ring")
    a = writes.add_collection_lot(conn, sid, quantity=1, location="Box")
    b = writes.add_collection_lot(conn, sid, quantity=2, location="Gisa")
    # b is listed first but waits for a to leave Box
    writes.bulk_update_collection(conn, {b: {"location": "Box"}, a: {"location": "Binder"}})
    assert {r["location"]: r["quantity"] for r in _rows(conn, sid)} == {"Binder": 1, "Box": 2}


def test_a_save_where_two_lots_swap_places_is_refused_whole(conn, add_card):
    sid, other = add_card("Sol Ring"), add_card("Arcane Signet")
    a = writes.add_collection_lot(conn, sid, quantity=1, location="Box")
    b = writes.add_collection_lot(conn, sid, quantity=2, location="Gisa")
    c = writes.add_collection_lot(conn, other, quantity=1, location="Box")
    with pytest.raises(ValueError, match="Sol Ring: these lots trade places"):
        writes.bulk_update_collection(conn, {c: {"quantity": 9}, a: {"location": "Gisa"},
                                             b: {"location": "Box"}})
    assert _lot(conn, c)["quantity"] == 1
    assert {r["location"]: r["quantity"] for r in _rows(conn, sid)} == {"Box": 1, "Gisa": 2}


def test_sleeving_from_two_lots_merges_into_the_deck_row(conn, add_card):
    sid = add_card("Sol Ring")
    gisa = _deck(conn, "Gisa")
    for loc in ("Gisa", "Box", "Binder"):
        writes.add_collection_lot(conn, sid, quantity=1, location=loc)
    _list(conn, gisa, sid, 3)
    writes.sleeve_into_deck(conn, gisa, sid, 2)
    assert [(r["quantity"], r["location"]) for r in _rows(conn, sid)] == [(3, "Gisa")]


def test_unsleeving_two_printings_into_box_merges_each_with_its_twin(conn, add_card):
    sid, reprint = add_card("Sol Ring"), add_card("Sol Ring", set_code="sld", collector_number="9")
    gisa = _deck(conn, "Gisa")
    for card in (sid, reprint):
        writes.add_collection_lot(conn, card, quantity=1, location="Box")
        writes.add_collection_lot(conn, card, quantity=1, location="Gisa")
    moves = writes.unsleeve_from_deck(conn, gisa, sid, 2)
    assert sum(m["quantity"] for m in moves) == 2
    assert {(r["scryfall_id"], r["quantity"], r["location"])
            for r in conn.execute("SELECT * FROM collection")} == {(sid, 2, "Box"), (reprint, 2, "Box")}


def test_a_binder_style_loop_of_moves_lands_in_one_row(conn, add_card):
    sid = add_card("Sol Ring")
    lots = [writes.add_collection_lot(conn, sid, quantity=1, location=loc) for loc in ("Box", "Shelf", "Gisa")]
    for lot in lots:
        writes.move_lot(conn, lot, "Binder", quantity=1, reason="binder")
    assert [(r["quantity"], r["location"]) for r in _rows(conn, sid)] == [(3, "Binder")]


def test_two_sessions_placing_one_card_leave_one_row(conn, add_card, monkeypatch):
    sid = add_card("Sol Ring")
    theirs = writes.add_collection_lot(conn, sid, quantity=1, location="Box")
    real, calls = writes._occupant, []

    def blind_once(*args, **kwargs):   # this session looked before the other one wrote
        calls.append(1)
        return None if len(calls) == 1 else real(*args, **kwargs)
    monkeypatch.setattr(writes, "_occupant", blind_once)
    assert writes.add_collection_lot(conn, sid, quantity=2, location="Box") == theirs
    assert [r["quantity"] for r in _rows(conn, sid)] == [3]


def test_a_raw_duplicate_insert_fails_under_the_index(conn, add_card, raw_lot):
    sid = add_card("Sol Ring")
    raw_lot(sid, 1, "Box")
    with pytest.raises(sqlite3.IntegrityError):
        raw_lot(sid, 1, " Box ")


def test_undoing_a_move_that_merged_merges_back_at_the_origin(conn, add_card):
    sid = add_card("Sol Ring")
    gisa = _deck(conn, "Gisa")
    box = writes.add_collection_lot(conn, sid, quantity=2, location="Box")
    writes.add_collection_lot(conn, sid, quantity=1, location="Gisa")
    _list(conn, gisa, sid, 1)
    moves = writes.add_deck_card(conn, gisa, sid, 2)
    assert {r["location"]: r["quantity"] for r in _rows(conn, sid)} == {"Box": 1, "Gisa": 2}
    _done, skipped = writes.revert_moves(conn, moves)
    assert skipped == []
    assert {r["location"]: r["quantity"] for r in _rows(conn, sid)} == {"Box": 2, "Gisa": 1}
    assert _lot(conn, box)["quantity"] == 2


def test_undoing_a_purchase_that_merged_takes_its_copies_and_price_back_off(conn, add_card):
    sid = add_card("Sol Ring")
    gisa = _deck(conn, "Gisa")
    held = writes.add_collection_lot(conn, sid, quantity=1, location="Gisa", price_paid=1.0)
    _list(conn, gisa, sid, 3)
    # The row's price is a total and the subtraction proportional, so with
    # mixed prices it's an average (accepted in the plan); same price here.
    moves = writes.record_purchase(conn, gisa, sid, quantity=2, price_paid=2.0)
    assert moves[0]["collection_id"] == held and _lot(conn, held)["quantity"] == 3
    done, skipped = writes.revert_moves(conn, moves)
    assert skipped == [] and done[0]["kind"] == "delete"
    assert (_lot(conn, held)["quantity"], _lot(conn, held)["price_paid"]) == (1, 1.0)
    assert _log(conn)[-1] == (held, 2, "Gisa", None, "deck_edit")


def test_undoing_a_purchase_whose_row_shrank_or_is_uncounted_is_skipped(conn, add_card):
    sid = add_card("Sol Ring")
    gisa = _deck(conn, "Gisa")
    _list(conn, gisa, sid, 3)
    moves = writes.record_purchase(conn, gisa, sid, quantity=2)
    writes.update_collection_lot(conn, moves[0]["collection_id"], quantity=1)
    assert "edited since" in writes.revert_moves(conn, moves)[1][0]
    conn.execute("UPDATE collection SET quantity=NULL")
    conn.commit()
    assert "uncounted pile" in writes.revert_moves(conn, moves)[1][0]
