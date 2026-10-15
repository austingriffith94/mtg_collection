"""EDIT_FLOW_PLAN.md Phase 5: "Build it" — writes.build_deck().

The world: Prosper is brewing; its list was built with each add's source
saved but nothing moved. Gisa (a normal deck) holds the one Sol Ring copy
Prosper has taken. Covered: the dry run writes nothing and lists the moves
the real run makes; storage / Auto / donor sources each land in the brew;
the donor is applied before the sleeve step (no starter lot for a transferred
card); a selection leaves the deck brewing; a full build clears the flag even
with Buy cards outstanding; a copy that is gone is reported as "to pull"."""
import os
import sqlite3

import pytest

from dashboard_lib import queries as q, writes

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RING, BLADE, SIGNET, TOWER = "Sol Ring", "Heirloom Blade", "Arcane Signet", "Command Tower"


@pytest.fixture
def c():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    with open(os.path.join(ROOT, "schema.sql"), encoding="utf-8") as f:
        conn.executescript(f.read())
    q.ensure_schema(conn)
    for place in ("Box", "Lands Box"):
        writes.add_location_to_catalog(conn, place)
    for name in (RING, BLADE, SIGNET, TOWER):
        conn.execute("INSERT INTO cards (scryfall_id, name, set_code, collector_number, oracle_id)"
                     " VALUES (?,?,'cmd','1',?)", (name, name, f"oracle-{name}"))
    for deck in ("Gisa", "Prosper"):
        conn.execute("INSERT INTO decks (name) VALUES (?)", (deck,))
    conn.commit()
    writes.set_build_state(conn, _deck(conn, "Prosper"), "brewing")
    yield conn
    conn.close()


def _deck(c, name):
    return c.execute("SELECT deck_id FROM decks WHERE name=?", (name,)).fetchone()[0]


def _lots(c):
    return sorted((r[0], r[1] or "", r[2]) for r in c.execute(
        "SELECT scryfall_id, location, quantity FROM collection"))


def _state(c):
    return (_lots(c), [tuple(r) for r in c.execute("SELECT * FROM deck_changes ORDER BY change_id")],
            [tuple(r) for r in c.execute("SELECT * FROM deck_cards ORDER BY deck_id, scryfall_id")],
            [tuple(r) for r in c.execute("SELECT deck_id, build_state FROM decks")])


def _sleeved(c, card, deck="Prosper"):
    return sum(r[0] or 0 for r in c.execute(
        "SELECT quantity FROM collection WHERE scryfall_id=? AND location=?", (card, deck)))


def _brew(c, card, **kw):
    return writes.add_deck_card(c, _deck(c, "Prosper"), card, **kw)


def _key(card):
    return f"oracle-{card}"


def test_storage_pick_beats_a_bigger_box_lot(c):
    writes.add_collection_lot(c, BLADE, quantity=3, location="Box")
    writes.add_collection_lot(c, BLADE, quantity=1, location="Lands Box")
    _brew(c, BLADE, source="storage", source_location="Lands Box")
    assert _sleeved(c, BLADE) == 0                      # a brew moves nothing
    result = writes.build_deck(c, _deck(c, "Prosper"))
    assert _sleeved(c, BLADE) == 1
    assert (BLADE, "Box", 3) in _lots(c)                # the Box lot is untouched
    assert result["pulled"][0]["source"] == "storage: Lands Box"
    assert result["cleared"] and q.deck_list_quantity(c, _deck(c, "Prosper"), BLADE) == 1


def test_auto_pulls_free_copies_and_never_makes_a_starter_lot(c):
    writes.add_collection_lot(c, SIGNET, quantity=1, location="Box")
    _brew(c, SIGNET)
    _brew(c, TOWER)                                     # owned nowhere
    result = writes.build_deck(c, _deck(c, "Prosper"))
    assert _sleeved(c, SIGNET) == 1
    assert _sleeved(c, TOWER) == 0 and not any(lot[0] == TOWER for lot in _lots(c))
    assert [r["card"] for r in result["to_pull"]] == [TOWER]
    assert result["to_pull"][0]["reason"] == "Not owned."
    assert not result["cleared"]
    assert q.deck_list_quantity(c, _deck(c, "Prosper"), TOWER) == 1   # still listed
    assert c.execute("SELECT build_state FROM decks WHERE name='Prosper'").fetchone()[0] == "brewing"


def test_donor_copy_moves_gisa_to_prosper_with_no_starter_lot(c):
    gisa = _deck(c, "Gisa")
    c.execute("INSERT INTO deck_cards (deck_id, scryfall_id, quantity) VALUES (?,?,1)", (gisa, RING))
    c.commit()
    writes.add_collection_lot(c, RING, quantity=1, location="Gisa")
    out = writes.transfer_between_decks(c, gisa, _deck(c, "Prosper"), RING)
    assert out["held"] and q.deck_list_quantity(c, gisa, RING) == 1   # Gisa waits
    result = writes.build_deck(c, _deck(c, "Prosper"))
    assert _lots(c) == [(RING, "Prosper", 1)]            # moved, not duplicated
    assert q.deck_list_quantity(c, gisa, RING) == 0
    assert result["pulled"][0]["source"] == "deck: Gisa"
    assert all(m["kind"] != "new" for m in result["moves"])
    assert result["cleared"]
    assert not q.open_takes(c)


def test_a_donor_with_no_sleeved_copy_is_reported_not_skipped(c):
    gisa = _deck(c, "Gisa")
    c.execute("INSERT INTO deck_cards (deck_id, scryfall_id, quantity) VALUES (?,?,1)", (gisa, RING))
    c.commit()                                           # listed, never sleeved
    writes.transfer_between_decks(c, gisa, _deck(c, "Prosper"), RING)
    result = writes.build_deck(c, _deck(c, "Prosper"))
    assert [r["card"] for r in result["to_pull"]] == [RING]
    assert "no copy" in result["to_pull"][0]["reason"]
    assert q.deck_list_quantity(c, gisa, RING) == 1     # Gisa untouched
    assert not result["cleared"]


def test_dry_run_writes_nothing_and_matches_the_real_run(c):
    writes.add_collection_lot(c, BLADE, quantity=1, location="Lands Box")
    writes.add_collection_lot(c, SIGNET, quantity=2, location="Box")
    _brew(c, BLADE, source="storage", source_location="Lands Box")
    _brew(c, SIGNET)
    _brew(c, TOWER, source="buy")
    before = _state(c)
    preview = writes.build_deck(c, _deck(c, "Prosper"), dry_run=True)
    assert _state(c) == before
    assert preview["dry_run"] and preview["cleared"]
    real = writes.build_deck(c, _deck(c, "Prosper"))
    assert _state(c) != before

    def shape(moves):
        return sorted((m["card"], m["quantity"], m["from"], m["to"], m["kind"]) for m in moves)
    assert shape(preview["moves"]) == shape(real["moves"])
    assert preview["to_pull"] == real["to_pull"] and preview["buying"] == real["buying"]


def test_a_selection_leaves_the_deck_brewing(c):
    writes.add_collection_lot(c, BLADE, quantity=1, location="Box")
    writes.add_collection_lot(c, SIGNET, quantity=1, location="Box")
    _brew(c, BLADE)
    _brew(c, SIGNET)
    result = writes.build_deck(c, _deck(c, "Prosper"), oracle_ids=[_key(BLADE)])
    assert _sleeved(c, BLADE) == 1 and _sleeved(c, SIGNET) == 0
    assert not result["cleared"]
    assert c.execute("SELECT build_state FROM decks WHERE name='Prosper'").fetchone()[0] == "brewing"
    rest = writes.build_deck(c, _deck(c, "Prosper"), oracle_ids=[_key(SIGNET)])
    assert rest["cleared"]                               # the last group finishes the job


def test_a_full_build_clears_the_flag_with_buy_cards_outstanding(c):
    writes.add_collection_lot(c, BLADE, quantity=1, location="Box")
    _brew(c, BLADE)
    _brew(c, TOWER, source="buy")
    result = writes.build_deck(c, _deck(c, "Prosper"))
    assert result["cleared"] and result["buying"] == [{"card": TOWER, "quantity": 1, "oracle_id": _key(TOWER)}]
    assert c.execute("SELECT build_state FROM decks WHERE name='Prosper'").fetchone()[0] is None
    assert q.deck_list_quantity(c, _deck(c, "Prosper"), TOWER) == 1


def test_a_picked_location_that_ran_dry_is_reported(c):
    lot = writes.add_collection_lot(c, BLADE, quantity=1, location="Lands Box")
    _brew(c, BLADE, source="storage", source_location="Lands Box")
    writes.move_lot(c, lot, "Box")                       # another plan took it since
    result = writes.build_deck(c, _deck(c, "Prosper"))
    assert [r["card"] for r in result["to_pull"]] == [BLADE]
    assert "Lands Box" in result["to_pull"][0]["reason"]
    assert _sleeved(c, BLADE) == 0 and not result["cleared"]


def test_build_refuses_a_deck_that_is_not_brewing(c):
    with pytest.raises(ValueError, match="isn't brewing"):
        writes.build_deck(c, _deck(c, "Gisa"))
    writes.set_build_state(c, _deck(c, "Gisa"), "dismantled")
    with pytest.raises(ValueError, match="dismantled"):
        writes.build_deck(c, _deck(c, "Gisa"), dry_run=True)


def test_the_move_log_says_build(c):
    writes.add_collection_lot(c, BLADE, quantity=1, location="Box")
    _brew(c, BLADE)
    writes.build_deck(c, _deck(c, "Prosper"))
    reasons = [r[0] for r in c.execute("SELECT reason FROM collection_moves WHERE scryfall_id=?", (BLADE,))]
    assert "build" in reasons


# ------------------------------------------------------------------
# The Advisor's "Add to Shortlist" (writes.add_advisor_idea)
# ------------------------------------------------------------------
def test_advisor_idea_is_an_idea_with_the_cut_filled_in_and_touches_no_list(c):
    gisa = _deck(c, "Gisa")
    c.execute("INSERT INTO deck_cards (deck_id, scryfall_id, quantity) VALUES (?,?,1)", (gisa, BLADE))
    c.commit()
    change_id = writes.add_advisor_idea(c, gisa, "sol ring", replaces=BLADE, skill="Deck doctor")
    row = writes.get_change(c, change_id)
    assert (row["status"], row["add_scryfall_id"], row["remove_scryfall_id"]) == ("idea", RING, BLADE)
    assert row["notes"] == "Advisor: Deck doctor"
    assert q.deck_list_quantity(c, gisa, RING) == 0 and q.deck_list_quantity(c, gisa, BLADE) == 1
    assert _lots(c) == []


def test_advisor_idea_refuses_unknown_cards_cards_in_the_deck_and_duplicates(c):
    gisa = _deck(c, "Gisa")
    with pytest.raises(ValueError, match="isn't in the card database"):
        writes.add_advisor_idea(c, gisa, "Not A Card")
    c.execute("INSERT INTO deck_cards (deck_id, scryfall_id, quantity) VALUES (?,?,1)", (gisa, TOWER))
    c.commit()
    with pytest.raises(ValueError, match="already in this deck"):
        writes.add_advisor_idea(c, gisa, TOWER)
    writes.add_advisor_idea(c, gisa, SIGNET)
    with pytest.raises(ValueError, match="already on this deck's shortlist"):
        writes.add_advisor_idea(c, gisa, SIGNET)


def test_a_replaces_that_is_not_on_the_list_is_left_blank(c):
    gisa = _deck(c, "Gisa")
    row = writes.get_change(c, writes.add_advisor_idea(c, gisa, RING, replaces=BLADE))
    assert row["remove_scryfall_id"] is None
