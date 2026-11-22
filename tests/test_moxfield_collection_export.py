"""The Moxfield collection CSV exporter (dashboard_lib/moxfield_export.py):
header compliance, summing lots per printing and finish, the in-deck flag,
the "owned NOT in a deck" subset, and per-field CSV formatting.

Replaces section 3 of the old scripts/_test_prompt_pass6 harness. (The
list-paste *import* has its own tests in test_moxfield_import.py.)
"""
import csv
import datetime
import io

import pytest

from dashboard_lib import moxfield_export as mox

# Moxfield's real sample export header (the sample file itself is gone).
SAMPLE_HEADER = ["Count", "Tradelist Count", "Name", "Edition", "Condition", "Language",
                 "Foil", "Tags", "Last Modified", "Collector Number", "Alter", "Proxy",
                 "Purchase Price"]
STAMP = "2026-01-01 00:00:00"


def test_csv_headers_match_moxfields_sample_exactly():
    assert mox.COLLECTION_CSV_HEADERS == SAMPLE_HEADER


@pytest.fixture
def exported(conn):
    """Two lots of one printing, a foil lot of another printing, an owned
    card in no deck, and an untracked bulk lot."""
    for sid, name, set_code, number in [("sr-c21", "Sol Ring", "c21", "263"),
                                        ("sr-lea", "Sol Ring", "lea", "1"),
                                        ("solemn-1", "Solemn Simulacrum", "c21", "233"),
                                        ("counterspell-1", "Counterspell", "clb", "1")]:
        conn.execute("INSERT INTO cards (scryfall_id, name, set_code, collector_number) VALUES (?,?,?,?)",
                     (sid, name, set_code, number))
    conn.execute("INSERT INTO decks (deck_id, name) VALUES (1, 'Test Deck')")
    conn.execute("INSERT INTO deck_cards (deck_id, scryfall_id, quantity) VALUES (1, 'sr-c21', 1)")
    # Two Locations: one place holds one row (docs/archive/LOT_CONSOLIDATION_PLAN.md).
    for sid, qty, foil, paid, loc in [("sr-c21", 2, 0, 5.00, "Box"), ("sr-c21", 1, 0, 4.50, "Binder"),
                                      ("sr-lea", 1, 1, 450.00, None), ("solemn-1", 3, 0, None, None),
                                      ("counterspell-1", None, 0, None, None)]:
        conn.execute("INSERT INTO collection (scryfall_id, quantity, foil, price_paid, location) "
                     "VALUES (?,?,?,?,?)", (sid, qty, foil, paid, loc))
    conn.commit()
    return conn


def rows_by_key(rows):
    return {(r["name"], r["set_code"], r["foil"]): r for r in rows}


def test_full_export_sums_lots_per_printing_and_finish(exported):
    rows = mox.collection_export_rows(exported, only_not_in_deck=False)
    by_key = rows_by_key(rows)
    assert "Counterspell" not in {r["name"] for r in rows}     # untracked bulk lot (quantity NULL)
    assert len(rows) == 3

    sol_c21 = by_key[("Sol Ring", "c21", False)]
    assert sol_c21["quantity"] == 3                            # 2 + 1
    assert sol_c21["purchase_price"] == pytest.approx(9.50)    # 5.00 + 4.50
    assert sol_c21["in_deck"] is True

    sol_lea = by_key[("Sol Ring", "lea", True)]               # foil: its own row
    assert sol_lea["quantity"] == 1
    # in_deck is per exact printing, not per card name.
    assert sol_lea["in_deck"] is False

    solemn = by_key[("Solemn Simulacrum", "c21", False)]
    assert solemn["in_deck"] is False and solemn["purchase_price"] is None


def test_not_in_a_deck_subset_drops_printings_that_are_run(exported):
    keys = set(rows_by_key(mox.collection_export_rows(exported, only_not_in_deck=True)))
    assert keys == {("Sol Ring", "lea", True), ("Solemn Simulacrum", "c21", False)}


def test_csv_row_fields(exported):
    by_key = rows_by_key(mox.collection_export_rows(exported, only_not_in_deck=False))
    row = mox.collection_csv_row(by_key[("Sol Ring", "c21", False)], STAMP)
    assert row["Count"] == 3
    assert row["Tradelist Count"] == 0                         # in a deck: not for trade
    assert row["Edition"] == "c21"
    assert (row["Condition"], row["Language"]) == ("Near Mint", "English")
    assert row["Foil"] == "" and row["Tags"] == ""
    assert (row["Alter"], row["Proxy"]) == ("FALSE", "FALSE")
    assert row["Purchase Price"] == "9.50"


def test_csv_row_foil_tradelist_and_missing_price(exported):
    by_key = rows_by_key(mox.collection_export_rows(exported, only_not_in_deck=False))
    foil = mox.collection_csv_row(by_key[("Sol Ring", "lea", True)], STAMP)
    assert foil["Foil"] == "foil"
    assert foil["Tradelist Count"] == foil["Count"] == 1        # not in a deck: all tradable
    assert mox.collection_csv_row(by_key[("Solemn Simulacrum", "c21", False)], STAMP)["Purchase Price"] == ""


def test_csv_text_round_trips(exported):
    text = mox.export_collection_csv_text(exported, only_not_in_deck=False,
                                          now=datetime.datetime(2026, 1, 1))
    reader = list(csv.reader(io.StringIO(text)))
    assert reader[0] == mox.COLLECTION_CSV_HEADERS
    assert len(reader) - 1 == 3
    assert reader[1][mox.COLLECTION_CSV_HEADERS.index("Last Modified")] == STAMP
