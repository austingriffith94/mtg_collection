"""
Offline test for Prompt Pass 6 changes: no Streamlit, no live network.

Covers:
  1. queries.search_card_names() / card_printings_by_name() — the
     Collection tab's auto-complete + printing-selector data source.
  2. writes.collection_has_card() / auto_add_to_collection() — Deck
     Building Auto-Add.
  3. dashboard_lib/moxfield_export.py's new Collection CSV exporter —
     header compliance against the real Moxfield sample export, per-
     printing/foil aggregation math, Tradelist Count / Purchase Price
     rules, and the "Owned NOT in a deck" subset.
  4. card_resolver.fetch_additional_printings() — a mocked Scryfall
     client standing in for live network access, same spirit as
     _test_migrate_offline.py's MockScryfallClient.

Run from anywhere:
    python scripts/_test_prompt_pass6_offline.py
"""
import csv
import io
import os
import sqlite3
import sys
import types

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# dashboard_lib.loaders has a real `import streamlit` (for @st.cache_data);
# streamlit isn't installed in this offline sandbox (see PROJECT_STATE.md's
# testing caveat), so register a minimal stub before importing anything
# that pulls loaders in transitively — same trick _test_prompt_pass5_offline.py
# uses.
if "streamlit" not in sys.modules:
    _fake_streamlit = types.ModuleType("streamlit")

    def _fake_cache_data(*args, **kwargs):
        def _decorator(func):
            func.clear = lambda: None
            return func
        return _decorator

    _fake_streamlit.cache_data = _fake_cache_data
    sys.modules["streamlit"] = _fake_streamlit

from dashboard_lib import queries as q
from dashboard_lib import writes as w
from dashboard_lib import moxfield_export as mox
from dashboard_lib import card_resolver
import scryfall_lookup


def check(label, cond):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        raise SystemExit(f"Test failed: {label}")


def fresh_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    with open(os.path.join(PROJECT_ROOT, "schema.sql")) as f:
        conn.executescript(f.read())
    return conn


def seed_cards(conn):
    """Two distinct oracle cards, each with two printings, plus one
    unrelated card — enough to exercise name search, printing selection,
    and the not-quite-matching-substring case."""
    rows = [
        # Sol Ring — two printings, one plain, one owned as foil in
        # a different set, so foil-grouping in the CSV exporter can be
        # exercised too.
        ("sr-c21", None, "Sol Ring", "c21", "263", "Artifact", None, 0, "", None, "common", None, None, 0, 0, 0, 0, 0, 1, 2.00),
        ("sr-lea", None, "Sol Ring", "lea", "1", "Artifact", None, 0, "", None, "common", None, None, 0, 0, 0, 0, 0, 1, 500.00),
        # Solemn Simulacrum — substring-shares "Sol" prefix with Sol Ring,
        # used to test that search_card_names ranks prefix matches first
        # but still finds this one as a "contains" match too.
        ("solemn-1", None, "Solemn Simulacrum", "c21", "233", "Artifact Creature — Golem", None, 4, "", None, "uncommon", None, None, 0, 0, 0, 0, 0, 1, 3.50),
        # A card with a totally different name, not matching "sol" at all.
        ("counterspell-1", None, "Counterspell", "clb", "1", "Instant", None, 2, "U", None, "common", None, None, 0, 0, 0, 0, 0, 1, 1.00),
    ]
    for r in rows:
        conn.execute(
            """INSERT INTO cards (
                scryfall_id, oracle_id, name, set_code, collector_number, type_line,
                mana_cost, cmc, color_identity, oracle_text, rarity, image_uri,
                local_image_path, is_basic_land, is_game_changer, is_reserved,
                is_showcase, is_borderless, commander_legal, current_price_usd
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            r,
        )
    conn.commit()


def main():
    # ------------------------------------------------------------------
    # 1. search_card_names / card_printings_by_name
    # ------------------------------------------------------------------
    conn = fresh_conn()
    seed_cards(conn)

    matches = q.search_card_names(conn, "sol")
    check("search_card_names('sol') finds both Sol Ring and Solemn Simulacrum", set(matches) == {"Sol Ring", "Solemn Simulacrum"})
    check("search_card_names ranks the prefix match ('Sol...') ahead of the contains-only match", matches[0] == "Sol Ring")
    check("search_card_names('') returns nothing (no accidental full-table dump)", q.search_card_names(conn, "") == [])
    check("search_card_names('xyz-nonexistent') returns []", q.search_card_names(conn, "xyz-nonexistent") == [])

    printings = q.card_printings_by_name(conn, "Sol Ring")
    check("card_printings_by_name('Sol Ring') finds both printings", len(printings) == 2)
    check("printings are ordered by set code", [p["set_code"] for p in printings] == sorted(p["set_code"] for p in printings))
    check("each printing dict carries its own scryfall_id", {p["scryfall_id"] for p in printings} == {"sr-c21", "sr-lea"})
    check("card_printings_by_name is case-insensitive", len(q.card_printings_by_name(conn, "sol ring")) == 2)
    check("card_printings_by_name('') returns []", q.card_printings_by_name(conn, "") == [])
    check("a name not in the DB returns []", q.card_printings_by_name(conn, "Black Lotus") == [])

    # ------------------------------------------------------------------
    # 2. collection_has_card / auto_add_to_collection
    # ------------------------------------------------------------------
    check("collection_has_card is False before anything is added", w.collection_has_card(conn, "sr-c21") is False)

    new_id = w.auto_add_to_collection(conn, "sr-c21", quantity=1, location="Test Deck")
    check("auto_add_to_collection returns a new collection_id the first time", new_id is not None)
    check("collection_has_card is True after auto-adding", w.collection_has_card(conn, "sr-c21") is True)

    lot = conn.execute("SELECT quantity, location, foil, source FROM collection WHERE collection_id=?", (new_id,)).fetchone()
    check("auto-added lot has the requested quantity", lot["quantity"] == 1)
    check("auto-added lot's location defaults to the deck name passed in", lot["location"] == "Test Deck")
    check("auto-added lot is not foil", lot["foil"] == 0)
    check("auto-added lot is tagged with a distinct source", lot["source"] == "Auto-added (deck build)")

    again = w.auto_add_to_collection(conn, "sr-c21", quantity=5, location="Some Other Deck")
    check("auto_add_to_collection is a no-op (returns None) once a lot already exists for that printing", again is None)
    count_after = conn.execute("SELECT COUNT(*) FROM collection WHERE scryfall_id='sr-c21'").fetchone()[0]
    check("no duplicate/second lot was created", count_after == 1)

    # A different printing of the SAME card name (sr-lea) is a DIFFERENT
    # scryfall_id, so it should still get its own starter lot — auto-add
    # is scoped to the exact printing, not the card name.
    other_printing_id = w.auto_add_to_collection(conn, "sr-lea", quantity=1, location="Another Deck")
    check("a different printing of the same-named card still gets its own auto-add", other_printing_id is not None)

    conn.close()

    # ------------------------------------------------------------------
    # 3. Moxfield Collection CSV exporter
    # ------------------------------------------------------------------
    # 3a. Header compliance against the real Moxfield sample export.
    sample_path = os.path.join(PROJECT_ROOT, "moxfield_samples", "moxfield_collection.csv")
    with open(sample_path, newline="") as f:
        sample_header = next(csv.reader(f))
    check(
        "COLLECTION_CSV_HEADERS matches moxfield_samples/moxfield_collection.csv exactly",
        mox.COLLECTION_CSV_HEADERS == sample_header,
    )

    # 3b. A scratch DB exercising: two lots of the SAME printing (summed),
    # a foil lot of a DIFFERENT printing (kept separate), a card that's
    # run in a deck (Tradelist Count -> 0), a card that's not, and an
    # untracked bulk lot (quantity NULL -> excluded).
    conn = fresh_conn()
    seed_cards(conn)
    conn.execute("INSERT INTO decks (deck_id, name) VALUES (1, 'Test Deck')")
    conn.execute("INSERT INTO deck_cards (deck_id, scryfall_id, quantity) VALUES (1, 'sr-c21', 1)")
    # Two separate acquisition lots of the same non-foil printing (price
    # history kept apart) -> must be SUMMED into one CSV row.
    conn.execute(
        "INSERT INTO collection (scryfall_id, quantity, foil, price_paid) VALUES ('sr-c21', 2, 0, 5.00)"
    )
    conn.execute(
        "INSERT INTO collection (scryfall_id, quantity, foil, price_paid) VALUES ('sr-c21', 1, 0, 4.50)"
    )
    # A FOIL lot of the OTHER Sol Ring printing -> its own separate row.
    conn.execute(
        "INSERT INTO collection (scryfall_id, quantity, foil, price_paid) VALUES ('sr-lea', 1, 1, 450.00)"
    )
    # Solemn Simulacrum: owned, not run in any deck.
    conn.execute(
        "INSERT INTO collection (scryfall_id, quantity, foil, price_paid) VALUES ('solemn-1', 3, 0, NULL)"
    )
    # Counterspell: an untracked bulk lot (no counted quantity) -> must
    # be excluded from the export entirely.
    conn.execute(
        "INSERT INTO collection (scryfall_id, quantity, foil) VALUES ('counterspell-1', NULL, 0)"
    )
    conn.commit()

    full_rows = mox.collection_export_rows(conn, only_not_in_deck=False)
    by_key = {(r["name"], r["set_code"], r["foil"]): r for r in full_rows}

    check("untracked bulk lot (Counterspell, quantity NULL) is excluded from the export", "Counterspell" not in {r["name"] for r in full_rows})
    check("exactly 3 printing/foil rows in the full export (2x Sol Ring printings + Solemn)", len(full_rows) == 3)

    sr_c21_row = by_key[("Sol Ring", "c21", False)]
    check("the two sr-c21 lots (2 + 1) are summed into quantity 3", sr_c21_row["quantity"] == 3)
    check("the two sr-c21 lots' purchase prices (5.00 + 4.50) are summed", abs(sr_c21_row["purchase_price"] - 9.50) < 1e-9)
    check("sr-c21 is flagged in_deck (it's in Test Deck's mainboard)", sr_c21_row["in_deck"] is True)

    sr_lea_row = by_key[("Sol Ring", "lea", True)]
    check("the foil sr-lea printing is a SEPARATE row from the non-foil sr-c21 one", sr_lea_row["quantity"] == 1)
    check("sr-lea (a different printing) is NOT flagged in_deck even though 'Sol Ring' the c21 printing is", sr_lea_row["in_deck"] is False)

    solemn_row = by_key[("Solemn Simulacrum", "c21", False)]
    check("Solemn Simulacrum is not in_deck", solemn_row["in_deck"] is False)
    check("Solemn Simulacrum's purchase_price is None (never recorded)", solemn_row["purchase_price"] is None)

    # 3c. "Owned NOT in a deck" subset excludes anything in_deck at all.
    not_in_deck_rows = mox.collection_export_rows(conn, only_not_in_deck=True)
    check(
        "'Owned NOT in a deck' has exactly 2 rows (the foil lea printing + Solemn)",
        len(not_in_deck_rows) == 2,
    )
    not_in_deck_names_sets = {(r["name"], r["set_code"], r["foil"]) for r in not_in_deck_rows}
    check(
        "'Owned NOT in a deck' keeps the foil sr-lea Sol Ring printing (that exact printing isn't run)",
        ("Sol Ring", "lea", True) in not_in_deck_names_sets,
    )
    check(
        "'Owned NOT in a deck' drops the sr-c21 printing (that exact printing IS run in Test Deck)",
        ("Sol Ring", "c21", False) not in not_in_deck_names_sets,
    )
    check("'Owned NOT in a deck' keeps Solemn Simulacrum", ("Solemn Simulacrum", "c21", False) in not_in_deck_names_sets)

    # 3d. CSV row/field formatting.
    csv_row = mox.collection_csv_row(sr_c21_row, "2026-01-01 00:00:00")
    check("Count == quantity", csv_row["Count"] == 3)
    check("Tradelist Count is 0 for an in-deck printing", csv_row["Tradelist Count"] == 0)
    check("Edition is lowercased", csv_row["Edition"] == "c21")
    check("Condition is always 'Near Mint'", csv_row["Condition"] == "Near Mint")
    check("Language is always 'English'", csv_row["Language"] == "English")
    check("Foil is blank for a non-foil row", csv_row["Foil"] == "")
    check("Tags is always blank", csv_row["Tags"] == "")
    check("Alter is always 'FALSE'", csv_row["Alter"] == "FALSE")
    check("Proxy is always 'FALSE'", csv_row["Proxy"] == "FALSE")
    check("Purchase Price is formatted to 2 decimals", csv_row["Purchase Price"] == "9.50")

    foil_csv_row = mox.collection_csv_row(sr_lea_row, "2026-01-01 00:00:00")
    check("Foil is lowercase 'foil' for a foil row", foil_csv_row["Foil"] == "foil")
    check("Tradelist Count == Count for a NOT-in-deck printing", foil_csv_row["Tradelist Count"] == foil_csv_row["Count"] == 1)

    solemn_csv_row = mox.collection_csv_row(solemn_row, "2026-01-01 00:00:00")
    check("Purchase Price is blank when never recorded", solemn_csv_row["Purchase Price"] == "")

    # 3e. Full CSV text round-trips through csv.reader with the right
    # header and row count, using a fixed `now` for determinism.
    full_text = mox.export_collection_csv_text(conn, only_not_in_deck=False, now=__import__("datetime").datetime(2026, 1, 1))
    reader = list(csv.reader(io.StringIO(full_text)))
    check("CSV text header row matches COLLECTION_CSV_HEADERS", reader[0] == mox.COLLECTION_CSV_HEADERS)
    check("CSV text has one data row per exported printing/foil row (3)", len(reader) - 1 == 3)
    check("Last Modified stamp uses the injected `now`", reader[1][mox.COLLECTION_CSV_HEADERS.index("Last Modified")] == "2026-01-01 00:00:00")

    conn.close()

    # ------------------------------------------------------------------
    # 4. card_resolver.fetch_additional_printings (mocked Scryfall client)
    # ------------------------------------------------------------------
    class MockClient:
        def __init__(self, *a, **kw):
            pass

        def get_all_printings(self, name):
            if name.strip() == "Sol Ring":
                return [
                    {
                        "id": "sr-c21", "oracle_id": "sr-oracle", "name": "Sol Ring",
                        "set": "c21", "collector_number": "263", "type_line": "Artifact",
                        "mana_cost": "{1}", "cmc": 1, "color_identity": [],
                        "oracle_text": "{T}: Add {C}{C}.", "rarity": "common",
                        "image_uris": {"normal": "https://example.com/c21.jpg"},
                        "legalities": {"commander": "legal"}, "prices": {"usd": "2.00"},
                    },
                    {
                        # A printing NOT already in the local DB.
                        "id": "sr-m19", "oracle_id": "sr-oracle", "name": "Sol Ring",
                        "set": "m19", "collector_number": "222", "type_line": "Artifact",
                        "mana_cost": "{1}", "cmc": 1, "color_identity": [],
                        "oracle_text": "{T}: Add {C}{C}.", "rarity": "uncommon",
                        "image_uris": {"normal": "https://example.com/m19.jpg"},
                        "legalities": {"commander": "legal"}, "prices": {"usd": "1.50"},
                    },
                ]
            return []

    fake_module = types.ModuleType("scryfall_lookup")
    fake_module.ScryfallClient = MockClient
    fake_module.to_card_row = scryfall_lookup.to_card_row

    original_loader = card_resolver._load_scryfall_module
    card_resolver._load_scryfall_module = lambda: fake_module
    try:
        conn = fresh_conn()
        seed_cards(conn)  # already has sr-c21 and sr-lea locally

        added, err = card_resolver.fetch_additional_printings(conn, "Sol Ring")
        check("fetch_additional_printings returns no error for a known name", err is None)
        check("only the genuinely-new printing (m19) was added (c21 was already local)", added == 1)

        row = conn.execute("SELECT name, set_code FROM cards WHERE scryfall_id='sr-m19'").fetchone()
        check("the new m19 printing is now in the local cards table", row is not None and row["name"] == "Sol Ring")

        added_again, err_again = card_resolver.fetch_additional_printings(conn, "Sol Ring")
        check("running it again adds 0 (both known printings already present)", added_again == 0)
        check("re-running is not an error", err_again is None)

        added_none, err_none = card_resolver.fetch_additional_printings(conn, "Totally Fake Card Name")
        check("a name with no Scryfall results returns 0 added", added_none == 0)
        check("a name with no Scryfall results returns an error message", err_none is not None)

        conn.close()
    finally:
        card_resolver._load_scryfall_module = original_loader

    # Blank name is rejected before ever touching Scryfall.
    conn = fresh_conn()
    added_blank, err_blank = card_resolver.fetch_additional_printings(conn, "   ")
    check("a blank name is rejected with an error, not a network call", err_blank is not None and added_blank == 0)
    conn.close()

    print("\nAll Prompt Pass 6 offline checks passed.")


if __name__ == "__main__":
    main()
