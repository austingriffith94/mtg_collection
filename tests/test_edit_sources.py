"""EDIT_FLOW_PLAN.md Phase 2: choosing where a card comes from, and where a
removed copy goes.

Built on the same small world as test_edit_flow.py: Gisa (the deck edited)
and Tuvasa (another deck), Sol Ring in whichever lots a test asks for, and
two storage Locations beside Box. The rules covered, in order:

  - the reads: storage_locations(), free_lots(), card_sources() and what
    Auto would pick
  - a chosen Location is used even when Box has the bigger lot; a Location
    that can't supply the copies refuses BEFORE anything is written
  - the choice is saved on the history row (source / dest), and only the
    direction it applies to
  - staged rows: add_change / update_change / apply_change carry it, and an
    apply whose Location has run dry refuses cleanly
  - the allocator: a pinned row claims only from its Location, falls to
    wrong_location / claimed_by when it runs short, and a brewing deck's
    pinned-but-unpulled cards claim first
  - brewing decks: the choice is saved, nothing moves
  - the Board: actions carry source / dest, Undo puts the copy back where
    it was, and the payload offers the options
"""
import os
import sqlite3

import pytest

from dashboard_lib import deck_board, formatting as fmt, queries as q, writes

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RING, BLADE, SWAMP = "Sol Ring", "Heirloom Blade", "Swamp"


@pytest.fixture
def c():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    with open(os.path.join(ROOT, "schema.sql"), encoding="utf-8") as f:
        conn.executescript(f.read())
    q.ensure_schema(conn)
    for place in ("Box", "Lands Box", "Binder"):
        writes.add_location_to_catalog(conn, place)
    for name in (RING, BLADE, SWAMP):
        conn.execute(
            "INSERT INTO cards (scryfall_id, name, set_code, collector_number, oracle_id, is_basic_land)"
            " VALUES (?,?,'cmd','1',?,?)", (name, name, f"oracle-{name}", 1 if name == SWAMP else 0))
    conn.execute("INSERT INTO decks (name) VALUES ('Gisa')")
    conn.execute("INSERT INTO decks (name) VALUES ('Tuvasa')")
    conn.commit()
    yield conn
    conn.close()


def _deck(c, name="Gisa"):
    return c.execute("SELECT deck_id FROM decks WHERE name=?", (name,)).fetchone()[0]


def _lot(c, card, qty, where):
    return writes.add_collection_lot(c, card, quantity=qty, location=where)


def _lots(c, card=RING):
    return sorted((r[0], r[1]) for r in c.execute(
        "SELECT location, quantity FROM collection WHERE scryfall_id=?", (card,)))


def _history(c, deck="Gisa"):
    return [dict(r) for r in c.execute(
        "SELECT * FROM deck_changes WHERE deck_id=? AND status='applied' ORDER BY change_id", (_deck(c, deck),))]


# ------------------------------------------------------------------
# The reads
# ------------------------------------------------------------------
def test_storage_locations_put_box_first_and_leave_decks_out(c):
    writes.add_location_to_catalog(c, "Tuvasa")   # a catalog entry that is also a deck name
    assert q.storage_locations(c) == ["Box", "Binder", "Lands Box"]


def test_box_is_always_offered_even_if_the_catalog_lost_it(c):
    writes.remove_location_from_catalog(c, "Box")
    assert q.storage_locations(c)[0] == "Box"


def test_free_lots_are_biggest_first_and_skip_decks(c):
    _lot(c, RING, 1, "Box")
    big = _lot(c, RING, 3, "Lands Box")
    _lot(c, RING, 2, "Tuvasa")
    assert [lot[0] for lot in q.free_lots(c, RING)][0] == big
    assert [lot[2] for lot in q.free_lots(c, RING)] == ["Lands Box", "Box"]
    assert [lot[2] for lot in q.free_lots(c, RING, location="lands box")] == ["Lands Box"]


def test_card_sources_lists_locations_decks_and_the_auto_pick(c):
    _lot(c, RING, 1, "Box")
    _lot(c, RING, 2, "Lands Box")
    _lot(c, RING, 1, "Tuvasa")
    _lot(c, RING, 1, None)
    out = q.card_sources(c, RING, _deck(c))
    assert out["storage"] == [{"location": "Lands Box", "quantity": 2}, {"location": "Box", "quantity": 1}]
    assert out["unlocated"] == 1
    assert out["decks"] == [{"deck": "Tuvasa", "quantity": 1}]
    assert out["here"] == 0 and not out["bulk"]
    assert out["auto"] == {"kind": "storage", "location": "Lands Box"}   # the biggest lot, as Auto moves it


def test_card_sources_auto_kinds(c):
    gisa = _deck(c)
    assert q.card_sources(c, RING, gisa)["auto"]["kind"] == "new"          # not owned
    _lot(c, RING, 1, "Tuvasa")
    assert q.card_sources(c, RING, gisa)["auto"]["kind"] == "new"          # only in another deck
    _lot(c, RING, 1, "Gisa")
    assert q.card_sources(c, RING, gisa)["auto"] == {"kind": "here", "location": "Gisa"}
    c.execute("INSERT INTO collection (scryfall_id, quantity, location) VALUES (?, NULL, 'Lands Box')", (SWAMP,))
    assert q.card_sources(c, SWAMP, gisa)["auto"]["kind"] == "bulk"


def test_source_choice_labels(c):
    _lot(c, RING, 2, "Lands Box")
    sources = q.card_sources(c, RING, _deck(c))
    assert fmt.source_choices(sources, q.storage_locations(c)) == [
        ("Auto — Lands Box", None, None), ("Lands Box — 2 free", "storage", "Lands Box")]
    # No card yet: every storage Location, uncounted.
    assert [x[0] for x in fmt.source_choices(None, ["Box", "Binder"])] == ["Auto", "Box", "Binder"]
    assert fmt.format_change_route("storage", "Lands Box", "Binder") == "from Lands Box to Binder"
    assert fmt.format_change_route(None, None, float("nan")) == ""


# ------------------------------------------------------------------
# A chosen Location is used; one that can't supply refuses first
# ------------------------------------------------------------------
def test_a_chosen_location_wins_over_a_bigger_lot_in_box(c):
    _lot(c, RING, 3, "Box")
    _lot(c, RING, 1, "Lands Box")
    gisa = _deck(c)
    moves = writes.add_deck_card(c, gisa, RING, 1, source="storage", source_location="Lands Box")
    assert [(m["from"], m["to"], m["kind"]) for m in moves] == [("Lands Box", "Gisa", "move")]
    assert _lots(c) == [("Box", 3), ("Gisa", 1)]


def test_auto_still_takes_the_biggest_lot(c):
    _lot(c, RING, 3, "Box")
    _lot(c, RING, 1, "Lands Box")
    writes.add_deck_card(c, _deck(c), RING, 1)
    assert _lots(c) == [("Box", 2), ("Gisa", 1), ("Lands Box", 1)]


def test_location_spelling_is_matched_to_the_catalog(c):
    _lot(c, RING, 1, "Lands Box")
    writes.add_deck_card(c, _deck(c), RING, 1, source="storage", source_location=" lands box ")
    assert _history(c)[0]["source_location"] == "Lands Box"


def test_a_location_with_no_copy_refuses_and_writes_nothing(c):
    _lot(c, RING, 2, "Box")
    gisa = _deck(c)
    with pytest.raises(ValueError, match="Lands Box has 0 free"):
        writes.add_deck_card(c, gisa, RING, 1, source="storage", source_location="Lands Box")
    assert _history(c) == [] and _lots(c) == [("Box", 2)]
    assert c.execute("SELECT COUNT(*) FROM deck_cards").fetchone()[0] == 0


def test_a_location_short_by_one_refuses_even_when_box_has_plenty(c):
    _lot(c, RING, 5, "Box")
    _lot(c, RING, 1, "Lands Box")
    with pytest.raises(ValueError, match="Lands Box has 1 free copy, 2 needed"):
        writes.add_deck_card(c, _deck(c), RING, 2, source="storage", source_location="Lands Box")
    assert _lots(c) == [("Box", 5), ("Lands Box", 1)]


def test_copies_already_sleeved_here_count_before_the_chosen_location(c):
    gisa = _deck(c)
    _lot(c, RING, 1, "Gisa")
    _lot(c, RING, 1, "Lands Box")
    # List goes 0 -> 2: one is already sleeved here, one comes from Lands Box.
    writes.add_deck_card(c, gisa, RING, 2, source="storage", source_location="Lands Box")
    assert _lots(c) == [("Gisa", 1), ("Gisa", 1)]


def test_a_chosen_location_never_makes_a_starter_lot(c):
    gisa = _deck(c)
    with pytest.raises(ValueError):
        writes.add_deck_card(c, gisa, RING, 1, source="storage", source_location="Binder")
    assert _lots(c) == []


def test_bad_sources_and_destinations_are_refused_with_a_reason(c):
    gisa = _deck(c)
    for source, location, match in (
        ("storage", None, "Pick which storage Location"),
        ("storage", "Tuvasa", "is a deck"),
        ("deck", "Tuvasa", "isn't available yet"),
        ("buy", None, "isn't available yet"),
    ):
        with pytest.raises(ValueError, match=match):
            writes.add_deck_card(c, gisa, RING, 1, source=source, source_location=location)
    with pytest.raises(ValueError, match="is a deck"):
        writes.remove_deck_card(c, gisa, RING, dest="Tuvasa")
    with pytest.raises(ValueError, match="isn't one of your storage Locations"):
        writes.remove_deck_card(c, gisa, RING, dest="Garage")
    assert _history(c) == []


def test_auto_spellings_all_mean_auto(c):
    _lot(c, RING, 3, "Box")
    gisa = _deck(c)
    for spelling in (None, "", "auto"):
        assert writes.normalize_source(c, spelling, "Lands Box") == (None, None)
    writes.add_deck_card(c, gisa, RING, 1, source="auto")
    assert _history(c)[0]["source"] is None


def test_a_bulk_basic_ignores_the_choice(c):
    c.execute("INSERT INTO collection (scryfall_id, quantity, location) VALUES (?, NULL, 'Binder')", (SWAMP,))
    moves = writes.add_deck_card(c, _deck(c), SWAMP, 4, source="storage", source_location="Lands Box")
    assert [m["kind"] for m in moves] == ["bulk"]


# ------------------------------------------------------------------
# History records the choice — in the direction it applies to
# ------------------------------------------------------------------
def test_a_join_records_where_it_came_from(c):
    _lot(c, RING, 1, "Lands Box")
    writes.add_deck_card(c, _deck(c), RING, 1, source="storage", source_location="Lands Box")
    row = _history(c)[0]
    assert (row["source"], row["source_location"], row["dest_location"]) == ("storage", "Lands Box", None)


def test_a_bump_records_where_the_extra_copy_came_from(c):
    gisa = _deck(c)
    _lot(c, RING, 1, "Box")
    _lot(c, RING, 1, "Binder")
    writes.add_deck_card(c, gisa, RING, 1)
    writes.add_deck_card(c, gisa, RING, 2, source="storage", source_location="Binder")
    bump = _history(c)[1]
    assert (bump["quantity"], bump["source_location"], bump["notes"]) == (1, "Binder", "quantity 1 → 2")
    assert _lots(c) == [("Gisa", 1), ("Gisa", 1)]


def test_a_removal_goes_where_asked_and_says_so(c):
    gisa = _deck(c)
    _lot(c, RING, 1, "Box")
    writes.add_deck_card(c, gisa, RING, 1)
    moves = writes.remove_deck_card(c, gisa, RING, dest="lands box")
    assert [(m["from"], m["to"]) for m in moves] == [("Gisa", "Lands Box")]
    assert _history(c)[1]["dest_location"] == "Lands Box"
    assert _lots(c) == [("Lands Box", 1)]


def test_a_removal_with_no_choice_goes_to_box_and_records_nothing_extra(c):
    gisa = _deck(c)
    _lot(c, RING, 1, "Binder")
    writes.add_deck_card(c, gisa, RING, 1)
    writes.remove_deck_card(c, gisa, RING)
    assert _lots(c) == [("Box", 1)] and _history(c)[1]["dest_location"] is None


def test_a_decrease_uses_dest_and_an_increase_ignores_it(c):
    gisa = _deck(c)
    _lot(c, RING, 3, "Box")
    writes.add_deck_card(c, gisa, RING, 2)
    writes.add_deck_card(c, gisa, RING, 3, dest="Binder")        # growing: dest is ignored
    assert _history(c)[-1]["dest_location"] is None
    writes.add_deck_card(c, gisa, RING, 2, dest="Binder")        # shrinking: it applies
    assert _history(c)[-1]["dest_location"] == "Binder"
    assert _lots(c) == [("Binder", 1), ("Gisa", 2)]


def test_a_source_is_ignored_when_the_list_does_not_grow(c):
    gisa = _deck(c)
    _lot(c, RING, 2, "Box")
    writes.add_deck_card(c, gisa, RING, 2)
    # Shrinking with a source named: nothing to take, nothing validated.
    writes.add_deck_card(c, gisa, RING, 1, source="storage", source_location="Binder")
    assert _history(c)[-1]["source"] is None


def test_bulk_update_sends_every_put_away_copy_to_one_place(c):
    gisa = _deck(c)
    _lot(c, RING, 1, "Box")
    _lot(c, BLADE, 1, "Box")
    writes.add_deck_card(c, gisa, RING, 1)
    writes.add_deck_card(c, gisa, BLADE, 1)
    changed, moves = writes.bulk_update_deck_cards(c, gisa, {RING: 0, BLADE: None}, dest="Lands Box")
    assert changed == 2 and {m["to"] for m in moves} == {"Lands Box"}
    assert sorted(r[0] for r in c.execute("SELECT location FROM collection")) == ["Lands Box", "Lands Box"]


# ------------------------------------------------------------------
# Staged rows
# ------------------------------------------------------------------
def test_an_idea_keeps_its_source_and_applying_it_uses_it(c):
    gisa = _deck(c)
    _lot(c, RING, 3, "Box")
    _lot(c, RING, 1, "Lands Box")
    _lot(c, BLADE, 1, "Gisa")
    c.execute("INSERT INTO deck_cards (deck_id, scryfall_id, quantity) VALUES (?,?,1)", (gisa, BLADE))
    cid = writes.add_change(c, gisa, add_scryfall_id=RING, add_name=RING, remove_scryfall_id=BLADE,
                            remove_name=BLADE, status="planned", source="storage",
                            source_location="lands box", dest_location="Binder")
    row = writes.get_change(c, cid)
    assert (row["source"], row["source_location"], row["dest_location"]) == ("storage", "Lands Box", "Binder")
    outcome = writes.apply_change(c, cid)
    assert {(m["from"], m["to"]) for m in outcome["moves"]} == {("Lands Box", "Gisa"), ("Gisa", "Binder")}
    # The applied row IS the idea's row, so History keeps the choice with no extra write.
    applied = writes.get_change(c, cid)
    assert applied["status"] == "applied" and applied["source_location"] == "Lands Box"


def test_apply_refuses_cleanly_when_the_chosen_location_ran_dry(c):
    gisa = _deck(c)
    _lot(c, RING, 1, "Box")
    cid = writes.add_change(c, gisa, add_scryfall_id=RING, add_name=RING, source="storage",
                            source_location="Lands Box")
    c.execute("UPDATE deck_changes SET status='planned', planned_at=CURRENT_TIMESTAMP WHERE change_id=?", (cid,))
    c.commit()
    with pytest.raises(ValueError, match="Lands Box has 0 free"):
        writes.apply_change(c, cid)
    assert writes.get_change(c, cid)["status"] == "planned"
    assert c.execute("SELECT COUNT(*) FROM deck_cards").fetchone()[0] == 0
    assert _lots(c) == [("Box", 1)]


def test_a_swap_for_another_printing_checks_stock_against_what_stays(c):
    # Cutting one printing for another of the SAME card moves nothing, so a
    # chosen Location with no copy must not refuse it.
    gisa = _deck(c)
    other = "Sol Ring-sld-2"
    c.execute("INSERT INTO cards (scryfall_id, name, set_code, collector_number, oracle_id) VALUES (?,?,?,?,?)",
              (other, RING, "sld", "2", f"oracle-{RING}"))
    c.execute("INSERT INTO deck_cards (deck_id, scryfall_id, quantity) VALUES (?,?,1)", (gisa, RING))
    _lot(c, RING, 1, "Gisa")
    cid = writes.add_change(c, gisa, add_scryfall_id=other, add_name=RING, remove_scryfall_id=RING,
                            remove_name=RING, status="planned", source="storage", source_location="Binder")
    outcome = writes.apply_change(c, cid)
    assert outcome["moves"] == []
    assert _lots(c) == [("Gisa", 1)]


def test_update_change_validates_the_pair_together(c):
    gisa = _deck(c)
    cid = writes.add_change(c, gisa, add_scryfall_id=RING, add_name=RING)
    writes.update_change(c, cid, source="storage", source_location="Binder")
    assert writes.get_change(c, cid)["source_location"] == "Binder"
    writes.update_change(c, cid, source_location="Lands Box")       # half the pair: the other is kept
    assert writes.get_change(c, cid)["source"] == "storage"
    writes.update_change(c, cid, source=None, source_location=None)  # back to Auto
    assert writes.get_change(c, cid)["source_location"] is None
    with pytest.raises(ValueError, match="is a deck"):
        writes.update_change(c, cid, source="storage", source_location="Tuvasa")
    with pytest.raises(ValueError, match="is a deck"):
        writes.update_change(c, cid, dest_location="Tuvasa")
    writes.update_change(c, cid, dest_location="binder")
    assert writes.get_change(c, cid)["dest_location"] == "Binder"


def test_bulk_update_changes_can_set_a_source(c):
    gisa = _deck(c)
    cid = writes.add_change(c, gisa, add_scryfall_id=RING, add_name=RING)
    n = writes.bulk_update_changes(c, gisa, {cid: {"source": "storage", "source_location": "Binder"}})
    assert n == 1 and writes.get_change(c, cid)["source_location"] == "Binder"


def test_a_rename_follows_a_deck_source_but_not_a_storage_one(c):
    gisa = _deck(c)
    storage = writes.add_change(c, gisa, add_scryfall_id=RING, add_name=RING, source="storage",
                                source_location="Binder")
    deck_row = writes.add_change(c, gisa, add_scryfall_id=BLADE, add_name=BLADE)
    # Phase 3 will write source='deck'; the cascade is in place ahead of it.
    c.execute("UPDATE deck_changes SET source='deck', source_location='Tuvasa' WHERE change_id=?", (deck_row,))
    c.execute("UPDATE deck_changes SET source_location='Tuvasa' WHERE change_id=?", (storage,))
    c.commit()
    writes.rename_deck(c, _deck(c, "Tuvasa"), "Tuvasa Prime")
    assert writes.get_change(c, deck_row)["source_location"] == "Tuvasa Prime"
    assert writes.get_change(c, storage)["source_location"] == "Tuvasa"      # storage text untouched


# ------------------------------------------------------------------
# The allocator
# ------------------------------------------------------------------
def _open_rows(c):
    return q.open_changes_dataframe(c, ("idea", "planned"))


def test_a_pinned_row_claims_only_from_its_location(c):
    gisa = _deck(c)
    _lot(c, RING, 3, "Box")
    _lot(c, RING, 1, "Lands Box")
    cid = writes.add_change(c, gisa, add_scryfall_id=RING, add_name=RING, source="storage",
                            source_location="Lands Box")
    verdict = q.plan_availability(c, _open_rows(c))[cid]
    assert verdict["verdict"] == "available" and verdict["locations"] == ["Lands Box"]
    assert verdict["text"] == "In storage: Lands Box (your pick)"


def test_a_pinned_row_short_in_its_location_says_where_the_copy_is(c):
    gisa = _deck(c)
    _lot(c, RING, 3, "Box")
    cid = writes.add_change(c, gisa, add_scryfall_id=RING, add_name=RING, source="storage",
                            source_location="Lands Box")
    verdict = q.plan_availability(c, _open_rows(c))[cid]
    assert verdict["verdict"] == "wrong_location"
    assert verdict["text"] == "None free in Lands Box — free in Box"


def test_a_pinned_row_with_nothing_free_anywhere_falls_back_to_the_usual_verdicts(c):
    gisa = _deck(c)
    cid = writes.add_change(c, gisa, add_scryfall_id=RING, add_name=RING, source="storage",
                            source_location="Lands Box")
    assert q.plan_availability(c, _open_rows(c))[cid]["verdict"] == "not_owned"


def test_an_auto_row_cannot_take_the_copy_a_pinned_row_was_promised(c):
    # The pinned row is older, so it walks first and holds the only Lands Box
    # copy; a later Auto row wanting two copies is claimed_by it.
    gisa, tuvasa = _deck(c), _deck(c, "Tuvasa")
    _lot(c, RING, 1, "Lands Box")
    first = writes.add_change(c, gisa, add_scryfall_id=RING, add_name=RING, source="storage",
                              source_location="Lands Box")
    second = writes.add_change(c, tuvasa, add_scryfall_id=RING, add_name=RING)
    out = q.plan_availability(c, _open_rows(c))
    assert out[first]["verdict"] == "available"
    assert out[second]["verdict"] == "claimed_by"
    assert [x["change_id"] for x in out[second]["claimed_by"]] == [first]


def test_a_pinned_row_behind_an_earlier_claimant_is_claimed_by(c):
    gisa, tuvasa = _deck(c), _deck(c, "Tuvasa")
    _lot(c, RING, 1, "Lands Box")
    first = writes.add_change(c, gisa, add_scryfall_id=RING, add_name=RING)           # Auto takes it
    second = writes.add_change(c, tuvasa, add_scryfall_id=RING, add_name=RING, source="storage",
                               source_location="Lands Box")
    assert q.plan_availability(c, _open_rows(c))[second]["verdict"] == "claimed_by"
    assert first


def test_a_brewing_deck_holds_its_pinned_copy_against_other_plans(c):
    brew, tuvasa = _deck(c), _deck(c, "Tuvasa")
    c.execute("UPDATE decks SET build_state='brewing' WHERE deck_id=?", (brew,))
    c.commit()
    _lot(c, RING, 1, "Lands Box")
    moves = writes.add_deck_card(c, brew, RING, 1, source="storage", source_location="Lands Box")
    assert [m["kind"] for m in moves] == ["brewing"] and moves[0]["source_location"] == "Lands Box"
    assert _lots(c) == [("Lands Box", 1)]                       # nothing moved
    assert q.brewing_claims(c) == [{"deck_id": brew, "deck_name": "Gisa", "scryfall_id": RING,
                                    "card": RING, "quantity": 1, "location": "Lands Box"}]
    idea = writes.add_change(c, tuvasa, add_scryfall_id=RING, add_name=RING)
    verdict = q.plan_availability(c, _open_rows(c))[idea]
    assert verdict["verdict"] == "claimed_by" and "Gisa (brewing)" in verdict["text"]
    assert verdict["claimed_by"][0]["change_id"] is None


def test_a_brewing_card_left_on_auto_claims_nothing(c):
    brew, tuvasa = _deck(c), _deck(c, "Tuvasa")
    c.execute("UPDATE decks SET build_state='brewing' WHERE deck_id=?", (brew,))
    c.commit()
    _lot(c, RING, 1, "Lands Box")
    writes.add_deck_card(c, brew, RING, 1)
    assert q.brewing_claims(c) == []
    idea = writes.add_change(c, tuvasa, add_scryfall_id=RING, add_name=RING)
    assert q.plan_availability(c, _open_rows(c))[idea]["verdict"] == "available"


def test_a_pulled_brewing_card_stops_claiming(c):
    brew = _deck(c)
    c.execute("UPDATE decks SET build_state='brewing' WHERE deck_id=?", (brew,))
    c.commit()
    _lot(c, RING, 1, "Lands Box")
    writes.add_deck_card(c, brew, RING, 1, source="storage", source_location="Lands Box")
    writes.sleeve_into_deck(c, brew, RING, 1, source="storage", source_location="Lands Box", force=True)
    assert q.brewing_claims(c) == []


def test_removing_a_brewing_card_drops_its_claim(c):
    brew = _deck(c)
    c.execute("UPDATE decks SET build_state='brewing' WHERE deck_id=?", (brew,))
    c.commit()
    _lot(c, RING, 1, "Lands Box")
    writes.add_deck_card(c, brew, RING, 1, source="storage", source_location="Lands Box")
    writes.remove_deck_card(c, brew, RING)
    assert q.brewing_claims(c) == []


# ------------------------------------------------------------------
# The Board
# ------------------------------------------------------------------
def _idea(c, deck, card=RING, **kw):
    return writes.add_change(c, deck, add_scryfall_id=card, add_name=card, **kw)


def test_the_board_adds_from_the_location_picked_in_the_menu(c):
    gisa = _deck(c)
    _lot(c, RING, 3, "Box")
    _lot(c, RING, 1, "Binder")
    cid = _idea(c, gisa)
    result = deck_board.apply_board_action(
        c, gisa, {"op": "add", "change_id": cid, "source": "storage", "source_location": "Binder"})
    assert result["message"] == "Added Sol Ring to the deck (from Binder)"
    assert [(m["from"], m["to"]) for m in result["moves"]] == [("Binder", "Gisa")]
    assert _history(c)[0]["source_location"] == "Binder"


def test_the_board_add_uses_the_ideas_saved_source_when_the_menu_names_none(c):
    gisa = _deck(c)
    _lot(c, RING, 3, "Box")
    _lot(c, RING, 1, "Binder")
    cid = _idea(c, gisa, source="storage", source_location="Binder")
    result = deck_board.apply_board_action(c, gisa, {"op": "add", "change_id": cid})
    assert [m["from"] for m in result["moves"]] == ["Binder"]


def test_choosing_auto_in_the_menu_overrides_a_saved_source(c):
    gisa = _deck(c)
    _lot(c, RING, 3, "Box")
    _lot(c, RING, 1, "Binder")
    cid = _idea(c, gisa, source="storage", source_location="Binder")
    result = deck_board.apply_board_action(c, gisa, {"op": "add", "change_id": cid, "source": "auto"})
    assert [m["from"] for m in result["moves"]] == ["Box"]
    assert _history(c)[0]["source"] is None


def test_the_board_refuses_a_dry_location_with_nothing_changed(c):
    gisa = _deck(c)
    _lot(c, RING, 3, "Box")
    cid = _idea(c, gisa)
    with pytest.raises(ValueError, match="Binder has 0 free"):
        deck_board.apply_board_action(
            c, gisa, {"op": "add", "change_id": cid, "source": "storage", "source_location": "Binder"})
    assert writes.get_change(c, cid)["status"] == "idea" and _lots(c) == [("Box", 3)]


def test_the_board_apply_saves_the_picked_source_then_applies(c):
    gisa = _deck(c)
    _lot(c, RING, 3, "Box")
    _lot(c, RING, 1, "Binder")
    c.execute("INSERT INTO deck_cards (deck_id, scryfall_id, quantity) VALUES (?,?,1)", (gisa, BLADE))
    _lot(c, BLADE, 1, "Gisa")
    cid = writes.add_change(c, gisa, add_scryfall_id=RING, add_name=RING, remove_scryfall_id=BLADE,
                            remove_name=BLADE, status="planned")
    deck_board.apply_board_action(
        c, gisa, {"op": "apply", "change_id": cid, "source": "storage", "source_location": "Binder"})
    row = writes.get_change(c, cid)
    assert row["status"] == "applied" and row["source_location"] == "Binder"
    assert ("Gisa", 1) in _lots(c) and ("Box", 3) in _lots(c)


def test_the_board_remove_puts_the_copy_where_asked_and_undo_returns_it(c):
    gisa = _deck(c)
    _lot(c, RING, 1, "Box")
    writes.add_deck_card(c, gisa, RING, 1)
    result = deck_board.apply_board_action(c, gisa, {"op": "remove", "id": RING, "dest": "Binder"})
    assert _lots(c) == [("Binder", 1)]
    assert _history(c)[-1]["dest_location"] == "Binder"
    undone = deck_board.apply_board_action(c, gisa, result["undo"], undo_moves=result["moves"])
    # Undo reverts the recorded move, so the copy returns to Gisa from Binder.
    assert _lots(c) == [("Gisa", 1)]
    assert [(m["from"], m["to"]) for m in undone["moves"]] == [("Binder", "Gisa")]


def test_the_board_minus_one_goes_where_asked(c):
    gisa = _deck(c)
    _lot(c, RING, 2, "Box")
    writes.add_deck_card(c, gisa, RING, 2)
    result = deck_board.apply_board_action(c, gisa, {"op": "dec", "id": RING, "dest": "Lands Box"})
    assert sorted(_lots(c)) == [("Gisa", 1), ("Lands Box", 1)]
    assert result["undo"] == {"op": "inc", "id": RING}


def test_move_to_shortlist_takes_a_destination_too(c):
    gisa = _deck(c)
    _lot(c, RING, 1, "Box")
    writes.add_deck_card(c, gisa, RING, 1)
    deck_board.apply_board_action(c, gisa, {"op": "to_shortlist", "id": RING, "dest": "Binder"})
    assert _lots(c) == [("Binder", 1)]


def test_a_dest_that_is_a_deck_is_refused_by_the_board(c):
    gisa = _deck(c)
    _lot(c, RING, 1, "Box")
    writes.add_deck_card(c, gisa, RING, 1)
    with pytest.raises(ValueError, match="is a deck"):
        deck_board.apply_board_action(c, gisa, {"op": "remove", "id": RING, "dest": "Tuvasa"})
    assert _lots(c) == [("Gisa", 1)]


def test_non_text_locations_from_the_browser_are_refused(c):
    gisa = _deck(c)
    cid = _idea(c, gisa)
    for action in ({"op": "remove", "id": RING, "dest": 5},
                   {"op": "add", "change_id": cid, "source": ["storage"]},
                   {"op": "apply", "change_id": cid, "source_location": {"x": 1}}):
        with pytest.raises(ValueError, match="invalid Location"):
            deck_board.apply_board_action(c, gisa, action)


def test_a_brewing_deck_removal_names_no_copy_to_move(c):
    brew = _deck(c)
    c.execute("UPDATE decks SET build_state='brewing' WHERE deck_id=?", (brew,))
    c.commit()
    writes.add_deck_card(c, brew, RING, 1)
    result = deck_board.apply_board_action(c, brew, {"op": "remove", "id": RING, "dest": "Binder"})
    assert [m["kind"] for m in result["moves"]] == []   # nothing was sleeved, so nothing to put away


def test_the_shortlist_payload_offers_each_cards_sources(c):
    gisa = _deck(c)
    _lot(c, RING, 2, "Lands Box")
    _lot(c, RING, 1, "Box")
    _idea(c, gisa, source="storage", source_location="Box")
    sources = {}
    shortlist = q.shortlist_dataframe(c, gisa)
    for _, r in shortlist.iterrows():
        sources[int(r["change_id"])] = q.card_sources(c, r["scryfall_id"], gisa)
    items = deck_board.shortlist_payload(shortlist, {}, None, sources)
    assert len(items) == 1
    item = items[0]
    assert item["source"] == "storage" and item["source_location"] == "Box"
    assert item["source_options"] == [{"location": "Lands Box", "quantity": 2}, {"location": "Box", "quantity": 1}]
    assert item["auto_label"] == "Auto — Lands Box"


def test_the_shortlist_payload_without_sources_offers_only_auto(c):
    gisa = _deck(c)
    _idea(c, gisa)
    (item,) = deck_board.shortlist_payload(q.shortlist_dataframe(c, gisa), {}, None)
    assert item["source_options"] == [] and item["source"] is None and item["auto_label"] == "Auto"


# ------------------------------------------------------------------
# History readers carry the choice
# ------------------------------------------------------------------
def test_history_readers_include_source_and_dest(c):
    gisa = _deck(c)
    _lot(c, RING, 1, "Binder")
    writes.add_deck_card(c, gisa, RING, 1, source="storage", source_location="Binder")
    writes.remove_deck_card(c, gisa, RING, dest="Lands Box")
    timeline = [row for row in q.deck_change_history(c, gisa) if row["kind"] == "change"]
    routes = [fmt.format_change_route(r["source"], r["source_location"], r["dest_location"]) for r in timeline]
    assert routes == ["to Lands Box", "from Binder"]                 # newest first
    df = q.deck_changes_dataframe(c, gisa, ("applied",))
    assert {"source", "source_location", "dest_location"} <= set(df.columns)
    lookup = q.card_lookup(c, RING)
    assert [fmt.format_change_route(h["source"], h["source_location"], h["dest_location"])
            for h in lookup["history"]] == ["to Lands Box", "from Binder"]
