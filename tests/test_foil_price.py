"""docs/BINDER_PLAN.md Phase 1: cards.current_price_usd_foil.

Covers the Scryfall parser (usd_foil, then usd_etched, then NULL), the Card Data
refresh UPDATE, the new-card INSERT, and the additive upgrade of an older
database. Offline: no network, the Scryfall client is faked."""
import os
import sqlite3
import sys

import pytest

from dashboard_lib import card_resolver, queries as q, refresh

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import scryfall_lookup  # noqa: E402


def _scryfall(prices, sid="sid-1"):
    """Smallest payload to_card_row() accepts."""
    return {"id": sid, "oracle_id": "o-1", "name": "Test Card", "set": "tst",
            "collector_number": "1", "type_line": "Artifact", "prices": prices}


@pytest.mark.parametrize("prices, nonfoil, foil", [
    ({"usd": "1.50", "usd_foil": "4.00", "usd_etched": "9.00"}, 1.50, 4.00),  # foil beats etched
    ({"usd": "1.50", "usd_foil": None, "usd_etched": "9.00"}, 1.50, 9.00),    # etched fallback
    ({"usd": "1.50", "usd_foil": None, "usd_etched": None}, 1.50, None),      # no foil price
    ({"usd": None, "usd_foil": "4.00"}, 4.00, 4.00),                          # foil-only printing
    ({}, None, None),
])
def test_parser_foil_price(prices, nonfoil, foil):
    row = scryfall_lookup.to_card_row(_scryfall(prices))
    assert row["current_price_usd"] == nonfoil
    assert row["current_price_usd_foil"] == foil


def test_new_card_insert_stores_foil_price(conn):
    row = scryfall_lookup.to_card_row(_scryfall({"usd": "1.00", "usd_foil": "3.25"}))
    row["price_updated_at"] = row["last_fetched_at"] = "2026-11-21"
    card_resolver._insert_card_row(conn, row)
    got = conn.execute("SELECT current_price_usd, current_price_usd_foil FROM cards").fetchone()
    assert tuple(got) == (1.00, 3.25)


def test_refresh_writes_foil_price(conn, add_card, monkeypatch):
    sid = add_card("Test Card")

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def get_by_id(self, scryfall_id):
            return _scryfall({"usd": "2.00", "usd_foil": "7.50"}, sid=scryfall_id)

    class FakeModule:
        ScryfallClient = FakeClient
        to_card_row = staticmethod(scryfall_lookup.to_card_row)

    monkeypatch.setattr(refresh, "_load_scryfall_module", lambda: FakeModule)
    refresh.refresh_all_cards(conn)
    got = conn.execute("SELECT current_price_usd, current_price_usd_foil FROM cards WHERE scryfall_id=?",
                       (sid,)).fetchone()
    assert tuple(got) == (2.00, 7.50)


def test_older_database_gains_the_column():
    old = sqlite3.connect(":memory:")
    old.execute("CREATE TABLE cards (scryfall_id TEXT PRIMARY KEY, current_price_usd REAL)")
    old.execute("INSERT INTO cards VALUES ('a', 1.0)")
    ddl = next(d for t, c, d in q._SCHEMA_UPGRADES if (t, c) == ("cards", "current_price_usd_foil"))
    old.execute(ddl)
    assert old.execute("SELECT current_price_usd_foil FROM cards").fetchone()[0] is None
    old.close()


def test_fresh_schema_has_the_column(conn):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(cards)")}
    assert "current_price_usd_foil" in cols
