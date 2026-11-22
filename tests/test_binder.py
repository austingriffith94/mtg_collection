"""docs/archive/BINDER_PLAN.md Phase 2: app_settings, binder_dismissed and the
candidate rules in dashboard_lib/binder.py. One test per edge case listed in
the plan, on a small in-memory fixture (conftest's `conn`)."""
import os
import sys

import pytest

from dashboard_lib import binder, queries as q, refresh, writes

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import scryfall_lookup  # noqa: E402


@pytest.fixture
def card(conn, add_card):
    """card(name, price=None, foil_price=None, reserved=0, gc=0, basic=0, set_code='cmd')."""
    def _card(name, price=None, foil_price=None, reserved=0, gc=0, basic=0, set_code="cmd", number="1"):
        sid = add_card(name, set_code=set_code, collector_number=number)
        conn.execute(
            """UPDATE cards SET current_price_usd=?, current_price_usd_foil=?, is_reserved=?,
                   is_game_changer=?, is_basic_land=?, price_updated_at='2026-11-01'
               WHERE scryfall_id=?""",
            (price, foil_price, reserved, gc, basic, sid))
        conn.commit()
        return sid
    return _card


@pytest.fixture
def lot(conn):
    def _lot(sid, quantity=1, location="Box", foil=0):
        cur = conn.execute("INSERT INTO collection (scryfall_id, quantity, location, foil) VALUES (?,?,?,?)",
                           (sid, quantity, location, foil))
        conn.commit()
        return cur.lastrowid
    return _lot


def _names(conn, settings=None):
    return list(binder.binder_candidates(conn, settings)["name"])


def _deck(conn, name):
    deck_id, err = writes.create_deck(conn, name)
    assert err is None
    return deck_id


# ---------------------------------------------------------------- settings
def test_settings_defaults(conn):
    s = binder.binder_settings(conn)
    assert s == binder.BinderSettings("Binder", 720, 2.0, True, True, ())


def test_settings_round_trip(conn):
    s = binder.BinderSettings("Binder 2", 360, 3.5, False, True, ("Lands",))
    binder.save_settings(conn, s)
    assert binder.binder_settings(conn) == s
    assert q.get_setting(conn, "binder.min_value") == "3.50"


def test_unreadable_setting_falls_back(conn):
    writes.set_setting(conn, "binder.capacity", "lots")
    writes.set_setting(conn, "binder.skip_locations", "not json")
    s = binder.binder_settings(conn)
    assert s.capacity == 720 and s.skip_locations == ()


def test_binder_location_matching_a_deck_is_refused(conn):
    _deck(conn, "Gisa")
    with pytest.raises(ValueError, match="deck"):
        binder.save_settings(conn, binder.BinderSettings(location=" gisa "))
    assert q.get_setting(conn, "binder.location") is None          # nothing stored


@pytest.mark.parametrize("kwargs", [{"min_value": -1}, {"capacity": 0}, {"location": "  "}])
def test_invalid_settings_are_refused(conn, kwargs):
    assert binder.validate_settings(conn, binder.BinderSettings(**kwargs))


def test_binder_location_case_is_the_same_place(conn, card, lot):
    sid = card("Dystopia", price=44.88, reserved=1)
    lot(sid, 1, "Binder")
    binder.save_settings(conn, binder.BinderSettings(location="binder"))
    assert _names(conn) == []
    assert binder.binder_fill(conn) == (1, 720)


# ---------------------------------------------------------------- scope
def test_deck_lot_is_never_listed(conn, card, lot):
    _deck(conn, "Gisa")
    lot(card("Dystopia", price=44.88, reserved=1, gc=1), 1, "Gisa")
    assert _names(conn) == []


def test_lot_already_in_binder_is_not_listed(conn, card, lot):
    lot(card("Dystopia", price=44.88), 1, "Binder")
    assert _names(conn) == []


def test_basic_lands_are_never_listed(conn, card, lot):
    lot(card("Snow-Covered Island", price=9.0, basic=1), 1, "Lands")
    assert _names(conn) == []


def test_changing_binder_location_makes_old_binder_storage(conn, card, lot):
    lot(card("Dystopia", price=44.88), 1, "Binder")
    binder.save_settings(conn, binder.BinderSettings(location="Binder 2"))
    assert _names(conn) == ["Dystopia"]


def test_skip_locations_ignore_case_and_unlocated_lots_are_scanned(conn, card, lot):
    lot(card("Valuable Land", price=5.0), 1, "Lands")
    lot(card("Loose Card", price=5.0), 1, None)
    lot(card("New Box Card", price=5.0), 1, "Box 2")                # never ticked or unticked
    s = binder.BinderSettings(skip_locations=(" lands ",))
    assert sorted(_names(conn, s)) == ["Loose Card", "New Box Card"]


def test_blank_lot_is_not_a_copy(conn, card, raw_lot):
    raw_lot(card("Dystopia", price=44.88, reserved=1), None, None)
    assert _names(conn) == []


# ---------------------------------------------------------------- rules
def test_value_rule_uses_the_minimum(conn, card, lot):
    lot(card("Cheap", price=1.99))
    lot(card("Exactly Two", price=2.00))
    df = binder.binder_candidates(conn)
    assert list(df["name"]) == ["Exactly Two"]
    assert df.loc[0, "reasons"] == ["$2.00"]


def test_foil_lot_uses_foil_price_and_falls_back(conn, card, lot):
    lot(card("Foil Gem", price=0.50, foil_price=6.00), foil=1)
    lot(card("Foil Unpriced", price=3.00, foil_price=None), foil=1)   # falls back to non-foil
    lot(card("Nonfoil Copy", price=0.50, foil_price=6.00), foil=0)   # ignores the foil price
    df = binder.binder_candidates(conn)
    assert list(df["name"]) == ["Foil Gem", "Foil Unpriced"]
    assert list(df["copy_value"]) == [6.00, 3.00]


def test_unpriced_card_passes_flag_rules_only(conn, card, lot):
    lot(card("Unpriced Reserved", reserved=1))
    lot(card("Unpriced Changer", gc=1))
    lot(card("Unpriced Plain"))
    df = binder.binder_candidates(conn)
    assert sorted(df["name"]) == ["Unpriced Changer", "Unpriced Reserved"]
    assert dict(zip(df["name"], df["reasons"])) == {
        "Unpriced Reserved": ["Reserved List"], "Unpriced Changer": ["Game changer"]}


def test_reasons_list_every_rule_passed_and_sort_by_value(conn, card, lot):
    lot(card("Conch Horn", price=3.54, reserved=1))
    lot(card("Dystopia", price=44.88, reserved=1))
    df = binder.binder_candidates(conn)
    assert list(df["name"]) == ["Dystopia", "Conch Horn"]
    assert df.loc[0, "reasons"] == ["$44.88", "Reserved List"]


def test_switches_off_and_high_minimum_give_empty_list(conn, card, lot):
    lot(card("Dystopia", price=44.88, reserved=1, gc=1))
    s = binder.BinderSettings(min_value=1000, rule_reserved=False, rule_game_changer=False)
    df = binder.binder_candidates(conn, s)
    assert df.empty and list(df.columns) == binder.CANDIDATE_COLUMNS
    assert binder.binder_candidate_count(conn, s) == 0


def test_null_quantity_lot_counts_as_one(conn, card, lot):
    lot(card("Dystopia", price=44.88), None, "Box")
    lot(card("Kediss", price=5.27), 2, "Box")
    df = binder.binder_candidates(conn)
    assert dict(zip(df["name"], df["quantity"])) == {"Dystopia": 1, "Kediss": 2}


def test_already_in_binder_counts_other_printings(conn, card, lot):
    a = card("Dystopia", price=44.88, set_code="all", number="1")
    b = card("Dystopia", price=40.00, set_code="me2", number="2")
    lot(a, 1, "Binder")
    lot(b, 1, "Box")
    df = binder.binder_candidates(conn)
    assert df.loc[0, "already_in_binder"] == 1


def test_count_is_distinct_cards(conn, card, lot):
    sid = card("Emergence Zone", price=3.0)
    lot(sid, 1, "Box")
    lot(sid, 1, "Lands")
    assert len(binder.binder_candidates(conn)) == 2
    assert binder.binder_candidate_count(conn) == 1


# ---------------------------------------------------------------- dismissals
def test_dismissed_card_hidden_in_every_printing_and_undo(conn, card, lot):
    a = card("Dystopia", price=44.88, set_code="all", number="1")
    b = card("Dystopia", price=40.00, set_code="me2", number="2")
    lot(a), lot(b)
    key = binder.binder_candidates(conn).loc[0, "card_key"]
    writes.dismiss_binder_card(conn, key, "Dystopia")
    assert _names(conn) == [] and binder.binder_candidate_count(conn) == 0
    assert [d["name"] for d in binder.dismissed_cards(conn)] == ["Dystopia"]
    writes.undo_binder_dismissal(conn, key)
    assert _names(conn) == ["Dystopia", "Dystopia"]


# ---------------------------------------------------------------- planned_for
def test_pinned_storage_row_shows_planned_for(conn, card, lot):
    gisa = _deck(conn, "Gisa")
    sid = card("Rhystic Study", price=30.0)
    lot(sid, 1, "Box")
    writes.add_change(conn, gisa, add_scryfall_id=sid, add_name="Rhystic Study",
                      source="storage", source_location="box")
    assert binder.binder_candidates(conn).loc[0, "planned_for"] == "Gisa"


def test_auto_row_is_not_a_claim(conn, card, lot):
    gisa = _deck(conn, "Gisa")
    sid = card("Rhystic Study", price=30.0)
    lot(sid, 1, "Box")
    writes.add_change(conn, gisa, add_scryfall_id=sid, add_name="Rhystic Study")
    assert binder.binder_candidates(conn).loc[0, "planned_for"] is None


def test_pinned_row_only_claims_its_location(conn, card, lot):
    gisa = _deck(conn, "Gisa")
    sid = card("Emergence Zone", price=3.0)
    lot(sid, 1, "Box")
    lot(sid, 1, "Lands")
    writes.add_change(conn, gisa, add_scryfall_id=sid, add_name="Emergence Zone",
                      source="storage", source_location="Lands")
    df = binder.binder_candidates(conn)
    assert dict(zip(df["location"], df["planned_for"])) == {"Box": None, "Lands": "Gisa"}


def test_brewing_saved_choice_shows_planned_for(conn, card, lot):
    brew = _deck(conn, "Gisa")
    conn.execute("UPDATE decks SET build_state='brewing' WHERE deck_id=?", (brew,))
    conn.commit()
    sid = card("Sol Ring", price=2.5)
    lot(sid, 1, "Box")
    writes.add_deck_card(conn, brew, sid, 1, source="storage", source_location="Box")
    assert binder.binder_candidates(conn).loc[0, "planned_for"] == "Gisa"


# ---------------------------------------------------------------- moving
def test_moving_one_of_two_leaves_one_behind_and_logs(conn, card, lot):
    cid = lot(card("Kediss", price=5.27), 2, "Box")
    writes.move_lot(conn, cid, "Binder", quantity=1)
    df = binder.binder_candidates(conn)
    assert list(zip(df["location"], df["quantity"])) == [("Box", 1)]
    assert df.loc[0, "already_in_binder"] == 1
    assert binder.binder_fill(conn) == (1, 720)
    moves = conn.execute("SELECT quantity, from_location, to_location, reason FROM collection_moves").fetchall()
    assert [tuple(m) for m in moves] == [(1, "Box", "Binder", "collection_edit")]


# ---------------------------------------------------------------- refresh summary
def _fake_scryfall(monkeypatch, prices):
    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def get_by_id(self, scryfall_id):
            name = scryfall_id.split("-")[0]
            return {"id": scryfall_id, "oracle_id": f"oracle-{name.casefold()}", "name": name,
                    "set": "cmd", "collector_number": "1", "type_line": "Artifact",
                    "prices": {"usd": prices.get(name)}}

    class FakeModule:
        ScryfallClient = FakeClient
        to_card_row = staticmethod(scryfall_lookup.to_card_row)

    monkeypatch.setattr(refresh, "_load_scryfall_module", lambda: FakeModule)


def test_refresh_summary_names_newly_qualifying_cards(conn, card, lot, monkeypatch):
    lot(card("Riser", price=1.00))
    lot(card("Already", price=5.00))
    _fake_scryfall(monkeypatch, {"Riser": "2.50", "Already": "5.00"})
    assert refresh.refresh_all_cards(conn)["binder_new"] == ["Riser"]
    assert refresh.refresh_all_cards(conn)["binder_new"] == []       # second run: nothing new


# ---------------------------------------------------------------- schema
def test_fresh_schema_has_the_tables(conn):
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"app_settings", "binder_dismissed"} <= tables


def test_older_database_gains_the_tables(conn):
    conn.execute("DROP TABLE app_settings")
    conn.execute("DROP TABLE binder_dismissed")
    q.ensure_schema(conn)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"app_settings", "binder_dismissed"} <= tables


# ---------------------------------------------------------------- Phase 4: new-lot notice
def test_new_lot_notice_names_the_rules_passed(conn, card):
    sid = card("Dystopia", price=44.88, reserved=1)
    assert binder.reasons_for_new_lot(conn, sid, False, "Box") == ["$44.88", "Reserved List"]


def test_new_lot_notice_is_empty_when_out_of_scope(conn, card):
    sid = card("Dystopia", price=44.88, reserved=1)
    _deck(conn, "Doom Deck")
    assert binder.reasons_for_new_lot(conn, sid, False, "Doom Deck") == []      # in a deck
    assert binder.reasons_for_new_lot(conn, sid, False, "binder") == []         # already the Binder
    assert binder.reasons_for_new_lot(conn, card("Forest", price=5, basic=1, number="2"), False, "Box") == []
    assert binder.reasons_for_new_lot(conn, card("Cheap", price=0.2, number="3"), False, "Box") == []


def test_new_lot_notice_uses_the_foil_price_for_foils(conn, card):
    sid = card("Shiny", price=0.5, foil_price=9.0)
    assert binder.reasons_for_new_lot(conn, sid, True, "Box") == ["$9.00"]
    assert binder.reasons_for_new_lot(conn, sid, False, "Box") == []


# ---------------------------------------------------------------- Decks tickbox
def test_deck_lots_listed_only_when_asked(conn, card, lot):
    _deck(conn, "Doom Deck")
    lot(card("Dystopia", price=44.88, reserved=1), location="Doom Deck")
    lot(card("Gem", price=5, number="2"), location="Box")
    assert _names(conn) == ["Gem"]
    assert list(binder.binder_candidates(conn, include_decks=True)["name"]) == ["Dystopia", "Gem"]
