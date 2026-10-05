"""Contention table, buy list, and Card Lookup — Phase 3 of the Workbench
rework (see IMPLEMENTATION_PLAN.md).

Contention answers "demand vs. supply per card", counting both mainboard
usage (deck_cards) and open shortlist demand (deck_changes idea/planned),
which plan_availability() alone can't: it only ever sees the shortlist
queue, so a card run in 4 mainboards with 3 copies owned (no shortlist
activity at all) would never surface there.
"""
import pytest

from dashboard_lib import queries as q
from dashboard_lib import moxfield_export as mox


def _deck(conn, name):
    cur = conn.execute("INSERT INTO decks (name) VALUES (?)", (name,))
    conn.commit()
    return cur.lastrowid


def _lot(conn, scryfall_id, location=None, quantity=1):
    conn.execute(
        "INSERT INTO collection (scryfall_id, quantity, location) VALUES (?,?,?)",
        (scryfall_id, quantity, location),
    )
    conn.commit()


def _mainboard(conn, deck_id, scryfall_id, quantity=1):
    conn.execute(
        "INSERT INTO deck_cards (deck_id, scryfall_id, quantity) VALUES (?,?,?)",
        (deck_id, scryfall_id, quantity),
    )
    conn.commit()


def _idea(conn, deck_id, *, add_scryfall_id=None, add_name=None, quantity=1, status="idea"):
    cur = conn.execute(
        """INSERT INTO deck_changes (deck_id, status, add_scryfall_id, add_name, quantity)
           VALUES (?,?,?,?,?)""",
        (deck_id, status, add_scryfall_id, add_name, quantity),
    )
    conn.commit()
    return cur.lastrowid


# ------------------------------------------------------------------
# Contention math
# ------------------------------------------------------------------

def test_contention_two_mainboards_one_idea_one_owned_shortfall_is_two(conn, add_card):
    sid = add_card("Blood Crypt")
    _lot(conn, sid, location="Box", quantity=1)
    d1, d2, d3 = _deck(conn, "Raktres"), _deck(conn, "Marchesa"), _deck(conn, "Prosper's")
    _mainboard(conn, d1, sid)
    _mainboard(conn, d2, sid)
    _idea(conn, d3, add_scryfall_id=sid)

    rows = {r["card"]: r for r in q.contention_table(conn)}
    assert rows["Blood Crypt"]["demand"] == 3
    assert rows["Blood Crypt"]["owned_qty"] == 1
    assert rows["Blood Crypt"]["shortfall"] == 2
    assert set(rows["Blood Crypt"]["mainboard_decks"]) == {"Raktres", "Marchesa"}
    assert rows["Blood Crypt"]["shortlist_decks"] == ["Prosper's"]


def test_contention_basics_never_appear(conn, add_card):
    swamp = conn.execute(
        "INSERT INTO cards (scryfall_id, name, set_code, collector_number, oracle_id, is_basic_land)"
        " VALUES ('swamp-1', 'Swamp', 'cmd', '1', 'oracle-swamp', 1)"
    )
    conn.commit()
    sid = "swamp-1"
    for i in range(9):
        deck_id = _deck(conn, f"Deck {i}")
        _mainboard(conn, deck_id, sid, quantity=1)
    # Nothing owned at all for Swamp (bulk basics aren't logged as lots).
    rows = q.contention_table(conn)
    assert all(r["card"] != "Swamp" for r in rows)


def test_contention_no_shortfall_when_owned_covers_demand(conn, add_card):
    sid = add_card("War Room")
    for _ in range(4):
        _lot(conn, sid, location="Box", quantity=1)
    deck_id = _deck(conn, "Only Deck")
    _mainboard(conn, deck_id, sid)
    rows = q.contention_table(conn)
    assert all(r["card"] != "War Room" for r in rows)


def test_contention_unlinked_idea_still_resolves_by_name(conn, add_card):
    """A backfilled swap stores only a typed name (no add_scryfall_id) —
    it must still contend for the right oracle card, not be invisible."""
    sid = add_card("Duelist's Heritage")
    deck1 = _deck(conn, "Vaevictis")
    deck2 = _deck(conn, "Wulfgar")
    _mainboard(conn, deck1, sid)
    _idea(conn, deck2, add_name="duelist's heritage")  # lowercase, unlinked
    rows = {r["card"]: r for r in q.contention_table(conn)}
    assert rows["Duelist's Heritage"]["demand"] == 2
    assert rows["Duelist's Heritage"]["shortfall"] == 2


# ------------------------------------------------------------------
# Buy list
# ------------------------------------------------------------------

def test_buy_list_sums_unowned_ideas_deduplicated_across_decks(conn, add_card):
    sid = add_card("Jeweled Lotus")
    conn.execute("UPDATE cards SET current_price_usd = 42.0 WHERE scryfall_id = ?", (sid,))
    conn.commit()
    d1, d2 = _deck(conn, "Vilis"), _deck(conn, "Gisa")
    _idea(conn, d1, add_scryfall_id=sid)
    _idea(conn, d2, add_scryfall_id=sid)

    result = q.buy_list(conn)
    jeweled = [r for r in result["rows"] if r["card"] == "Jeweled Lotus"][0]
    assert jeweled["quantity"] == 2
    assert jeweled["price"] == 42.0
    assert jeweled["subtotal"] == 84.0
    assert set(jeweled["decks"]) == {"Vilis", "Gisa"}
    assert result["total"] == pytest.approx(84.0)


def test_buy_list_excludes_an_idea_already_owned(conn, add_card):
    sid = add_card("Sol Ring")
    _lot(conn, sid, location="Box", quantity=1)
    deck_id = _deck(conn, "Vilis")
    _idea(conn, deck_id, add_scryfall_id=sid)

    result = q.buy_list(conn)
    assert all(r["card"] != "Sol Ring" for r in result["rows"])


def test_buy_list_includes_contention_shortfall_rows(conn, add_card):
    sid = add_card("Blood Crypt")
    conn.execute("UPDATE cards SET current_price_usd = 6.0 WHERE scryfall_id = ?", (sid,))
    _lot(conn, sid, location="Box", quantity=1)
    conn.commit()
    d1, d2, d3 = _deck(conn, "Raktres"), _deck(conn, "Marchesa"), _deck(conn, "Prosper's")
    _mainboard(conn, d1, sid)
    _mainboard(conn, d2, sid)
    _mainboard(conn, d3, sid)

    result = q.buy_list(conn)
    contention_rows = [r for r in result["rows"] if r["reason"] == "contention"]
    assert len(contention_rows) == 1
    assert contention_rows[0]["card"] == "Blood Crypt"
    assert contention_rows[0]["quantity"] == 2
    assert contention_rows[0]["subtotal"] == pytest.approx(12.0)


# ------------------------------------------------------------------
# Card Lookup
# ------------------------------------------------------------------

def test_card_lookup_rolls_two_printings_into_one_result(conn, add_card):
    sid1 = add_card("Sulfurous Springs", set_code="ice", collector_number="331")
    sid2 = add_card("Sulfurous Springs", set_code="one", collector_number="123")
    conn.execute("UPDATE cards SET current_price_usd = 1.5 WHERE scryfall_id = ?", (sid1,))
    conn.execute("UPDATE cards SET current_price_usd = 6.4 WHERE scryfall_id = ?", (sid2,))
    conn.commit()
    _lot(conn, sid1, location="Box", quantity=1)
    _lot(conn, sid2, location=None, quantity=1)
    deck_id = _deck(conn, "Marchesa Thatcher")
    _mainboard(conn, deck_id, sid1)

    result = q.card_lookup(conn, "Sulfurous Springs")
    assert result["name"] == "Sulfurous Springs"
    assert result["owned_qty"] == 2
    assert len(result["lots"]) == 2
    assert result["mainboard_in"] == [
        {"deck_id": deck_id, "deck_name": "Marchesa Thatcher", "quantity": 1}
    ]
    # Representative printing picked by highest price.
    assert result["card"]["current_price_usd"] == 6.4


def test_card_lookup_unknown_name_returns_none(conn):
    assert q.card_lookup(conn, "Not A Real Card") is None


def test_buy_list_text_export_includes_total(conn, add_card):
    sid = add_card("Jeweled Lotus")
    conn.execute("UPDATE cards SET current_price_usd = 42.0 WHERE scryfall_id = ?", (sid,))
    conn.commit()
    deck_id = _deck(conn, "Vilis")
    _idea(conn, deck_id, add_scryfall_id=sid)

    text = mox.export_buy_list_text(q.buy_list(conn))
    assert "Jeweled Lotus" in text
    assert "Total: $42.00" in text


def test_card_lookup_shows_shortlist_and_demand(conn, add_card):
    sid = add_card("Ripples of Undeath")
    deck_id = _deck(conn, "Prosper's Payday Loans")
    change_id = _idea(conn, deck_id, add_scryfall_id=sid)

    result = q.card_lookup(conn, "Ripples of Undeath")
    assert result["owned_qty"] == 0
    assert result["shortlisted_in"] == [
        {"change_id": change_id, "deck_id": deck_id,
         "deck_name": "Prosper's Payday Loans", "status": "idea"}
    ]
    assert result["demand"] == 1
    assert result["shortfall"] == 1
