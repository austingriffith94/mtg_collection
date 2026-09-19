"""
Offline test for Prompt Pass 13 changes: no Streamlit, no live network.

Prompt Pass 13 ("prompt6.txt" — Collection Location dropdown + cascade
deletion, and a new Deck Swap/Upgrade Manager tool):

  1. Collection Location is now a controlled dropdown (standard-location
     catalog, new `location_catalog` table, unioned with every currently
     tracked deck's own name — queries.location_options()) instead of a
     free-text field, in both the Editor Collection tab's "Add a card"
     form and its bulk lot editor. Deleting a deck now REASSIGNS any
     collection.location rows that matched its name back to
     writes.DEFAULT_LOCATION ("Box") instead of clearing them to NULL.
  2. A new Editor "Swap Manager" tab: queue up planned "Add Card A ->
     Replace Card B" swaps, see live inventory availability for each
     candidate (queries.card_inventory_status()), then apply the whole
     queue at once (writes.execute_swap()) — updates the deck's
     mainboard AND reconciles collection.location for both cards.

Covers:
  A. schema.sql — a fresh database already has location_catalog (empty,
     no seed rows needed on a from-scratch schema-only build).
  B. queries.ensure_schema() upgrade path — a database missing
     location_catalog (simulating a pre-Prompt-Pass-13 database) gets it
     created AND seeded with 'Box'/'Lands Box' plus whatever non-deck
     Location text collection rows already used, EXCLUDING any location
     that happens to match a tracked deck's own name.
  C. writes.py location catalog CRUD (list/add/remove_location_*).
  D. queries.location_options() — catalog ∪ deck names, sorted
     case-insensitively, deduped.
  E. queries.card_inventory_status() — owned_qty aggregation,
     available_lots (largest-quantity-first, excluding deck-sleeved
     lots), in_this_deck_qty, in_other_decks, and the not-owned/blank
     edge cases.
  F. writes.delete_deck() now reassigns matching collection.location
     rows to DEFAULT_LOCATION ("Box") instead of clearing to NULL.
  G. writes.execute_swap() — the three inventory scenarios (not owned;
     owned and available; owned but every copy is in another deck),
     plus the removed card's location moving back to storage and a
     no-matching-lot removal being a harmless no-op.
  H. Source-text checks confirming pages/4_Editor.py actually wires in
     the Swap Manager tab and the Location dropdowns, not just having
     the backing functions exist in isolation.
  I. loaders.py — invalidate_reference_caches()/invalidate_deck_caches()
     clear the new location caches.

Run from anywhere:
    python scripts/_test_prompt_pass13_offline.py
"""
import os
import sqlite3
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from dashboard_lib import queries as q
from dashboard_lib import writes as w


def check(label, cond):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        raise SystemExit(f"Test failed: {label}")


def build_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    with open(os.path.join(PROJECT_ROOT, "schema.sql")) as f:
        conn.executescript(f.read())
    return conn


def main():
    # ------------------------------------------------------------------
    # A. schema.sql — fresh database already has location_catalog
    # ------------------------------------------------------------------
    conn = build_db()
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    check("a fresh schema.sql database already has location_catalog", "location_catalog" in tables)
    check(
        "location_catalog starts empty on a fresh schema-only build",
        conn.execute("SELECT COUNT(*) FROM location_catalog").fetchone()[0] == 0,
    )
    conn.close()

    # ------------------------------------------------------------------
    # B. queries.ensure_schema() upgrade path — simulated pre-Pass-13 DB
    # ------------------------------------------------------------------
    conn = build_db()
    conn.execute("DROP TABLE location_catalog")
    conn.execute("INSERT INTO decks (deck_id, name) VALUES (1, 'Atraxa Superfriends')")
    conn.execute("INSERT INTO cards (scryfall_id, name, set_code, collector_number) VALUES ('c1','Sol Ring','TST','1')")
    conn.execute("INSERT INTO cards (scryfall_id, name, set_code, collector_number) VALUES ('c2','Forest','TST','2')")
    # A location matching a tracked deck's own name must NOT be seeded
    # into the catalog (it's already covered by location_options()'s
    # deck-name half, and seeding it would be a redundant duplicate of
    # what the dropdown already offers via decks.name).
    conn.execute("INSERT INTO collection (scryfall_id, quantity, location) VALUES ('c1', 1, 'Atraxa Superfriends')")
    conn.execute("INSERT INTO collection (scryfall_id, quantity, location) VALUES ('c2', 20, 'Lands Box')")
    conn.execute("INSERT INTO collection (scryfall_id, quantity, location) VALUES ('c2', 5, 'Trade Binder')")
    conn.commit()
    tables_before = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    check("location_catalog was actually dropped for this simulated-old-DB test", "location_catalog" not in tables_before)

    q.ensure_schema(conn)

    tables_after = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    check("ensure_schema() recreates location_catalog on an upgraded database", "location_catalog" in tables_after)
    seeded = {r[0] for r in conn.execute("SELECT location FROM location_catalog").fetchall()}
    check("ensure_schema() seeds the two standard locations", {"Box", "Lands Box"} <= seeded)
    check("ensure_schema() seeds existing non-deck collection locations too", "Trade Binder" in seeded)
    check(
        "ensure_schema() does NOT seed a location that duplicates a tracked deck's own name",
        "Atraxa Superfriends" not in seeded,
    )

    q.ensure_schema(conn)  # re-run should be a no-op, not error or duplicate
    check(
        "re-running ensure_schema() doesn't touch an already-upgraded location_catalog",
        {r[0] for r in conn.execute("SELECT location FROM location_catalog").fetchall()} == seeded,
    )
    conn.close()

    # ------------------------------------------------------------------
    # C. writes.py — location catalog CRUD
    # ------------------------------------------------------------------
    conn = build_db()
    check("catalog starts empty", w.list_location_catalog(conn) == [])
    w.add_location_to_catalog(conn, "Box")
    w.add_location_to_catalog(conn, "Binder A")
    check("catalog list is alphabetical, case-insensitive", w.list_location_catalog(conn) == ["Binder A", "Box"])
    w.add_location_to_catalog(conn, "Box")  # duplicate, should be a no-op
    check("adding a duplicate catalog entry doesn't create a second row", w.list_location_catalog(conn) == ["Binder A", "Box"])
    w.add_location_to_catalog(conn, "   ")  # blank, should be ignored
    check("adding a blank/whitespace-only catalog entry is ignored", w.list_location_catalog(conn) == ["Binder A", "Box"])
    w.remove_location_from_catalog(conn, "Binder A")
    check("removing a catalog entry works", w.list_location_catalog(conn) == ["Box"])
    conn.close()

    # ------------------------------------------------------------------
    # D. queries.location_options()
    # ------------------------------------------------------------------
    conn = build_db()
    w.add_location_to_catalog(conn, "Box")
    w.add_location_to_catalog(conn, "Lands Box")
    conn.execute("INSERT INTO decks (deck_id, name) VALUES (1, 'zzz Aristocrats')")
    conn.execute("INSERT INTO decks (deck_id, name) VALUES (2, 'Atraxa Superfriends')")
    conn.commit()
    options = q.location_options(conn)
    check(
        "location_options() unions the catalog and every deck's own name",
        set(options) == {"Box", "Lands Box", "zzz Aristocrats", "Atraxa Superfriends"},
    )
    check(
        "location_options() is sorted case-insensitively (Atraxa before zzz)",
        options.index("Atraxa Superfriends") < options.index("zzz Aristocrats"),
    )
    conn.close()

    # ------------------------------------------------------------------
    # E. queries.card_inventory_status()
    # ------------------------------------------------------------------
    conn = build_db()
    conn.execute("INSERT INTO decks (deck_id, name) VALUES (1, 'Deck A')")
    conn.execute("INSERT INTO decks (deck_id, name) VALUES (2, 'Deck B')")
    conn.execute("INSERT INTO cards (scryfall_id, name, set_code, collector_number) VALUES ('c1','Lightning Bolt','TST','1')")
    conn.execute("INSERT INTO collection (scryfall_id, quantity, location) VALUES ('c1', 1, 'Deck A')")
    conn.execute("INSERT INTO collection (scryfall_id, quantity, location) VALUES ('c1', 1, 'Box')")
    conn.execute("INSERT INTO collection (scryfall_id, quantity, location) VALUES ('c1', 2, NULL)")
    conn.commit()

    status = q.card_inventory_status(conn, "Lightning Bolt", deck_name="Deck B")
    check("card_inventory_status() sums owned_qty across all lots", status["owned_qty"] == 4)
    check("card_inventory_status() flags the lot sleeved in another deck", status["in_other_decks"] == {"Deck A": 1})
    check("card_inventory_status() finds no copies already in THIS deck (Deck B)", status["in_this_deck_qty"] == 0)
    check(
        "card_inventory_status() lists the Box + blank-location lots as available",
        {lot["location"] for lot in status["available_lots"]} == {"Box", None},
    )
    check(
        "card_inventory_status() sorts available_lots largest-quantity-first",
        status["available_lots"][0]["quantity"] == 2,
    )

    status_this_deck = q.card_inventory_status(conn, "Lightning Bolt", deck_name="Deck A")
    check(
        "card_inventory_status() reports the lot sleeved in the deck you pass as 'in this deck', not 'available'",
        status_this_deck["in_this_deck_qty"] == 1 and status_this_deck["in_other_decks"] == {},
    )

    not_owned = q.card_inventory_status(conn, "Counterspell", deck_name="Deck A")
    check("card_inventory_status() on a card with zero collection rows returns owned_qty=0", not_owned["owned_qty"] == 0)
    check("card_inventory_status() on an unowned card returns no available lots", not_owned["available_lots"] == [])

    blank = q.card_inventory_status(conn, "   ", deck_name="Deck A")
    check("card_inventory_status() on a blank name returns all-zero without erroring", blank["owned_qty"] == 0)
    conn.close()

    # ------------------------------------------------------------------
    # F. writes.delete_deck() reassigns Location to DEFAULT_LOCATION
    # ------------------------------------------------------------------
    conn = build_db()
    conn.execute("INSERT INTO decks (deck_id, name) VALUES (1, 'Doomed Deck')")
    conn.execute("INSERT INTO cards (scryfall_id, name, set_code, collector_number) VALUES ('c1','Sol Ring','TST','1')")
    conn.execute("INSERT INTO collection (collection_id, scryfall_id, quantity, location) VALUES (1, 'c1', 1, 'Doomed Deck')")
    conn.commit()
    check("DEFAULT_LOCATION is 'Box' per the prompt spec", w.DEFAULT_LOCATION == "Box")
    w.delete_deck(conn, 1, clear_collection_locations=True)
    new_loc = conn.execute("SELECT location FROM collection WHERE collection_id=1").fetchone()[0]
    check("delete_deck() reassigns matching collection.location to 'Box', not NULL", new_loc == "Box")
    conn.close()

    conn = build_db()
    conn.execute("INSERT INTO decks (deck_id, name) VALUES (1, 'Doomed Deck 2')")
    conn.execute("INSERT INTO cards (scryfall_id, name, set_code, collector_number) VALUES ('c1','Sol Ring','TST','1')")
    conn.execute("INSERT INTO collection (collection_id, scryfall_id, quantity, location) VALUES (1, 'c1', 1, 'Doomed Deck 2')")
    conn.commit()
    w.delete_deck(conn, 1, clear_collection_locations=False)
    kept_loc = conn.execute("SELECT location FROM collection WHERE collection_id=1").fetchone()[0]
    check("delete_deck(clear_collection_locations=False) leaves the Location text untouched", kept_loc == "Doomed Deck 2")
    conn.close()

    # ------------------------------------------------------------------
    # G. writes.execute_swap() — the Swap Manager's core write path
    # ------------------------------------------------------------------
    def fresh_swap_db():
        conn = build_db()
        conn.execute("INSERT INTO decks (deck_id, name) VALUES (1, 'My Deck')")
        conn.execute("INSERT INTO decks (deck_id, name) VALUES (2, 'Other Deck')")
        conn.execute("INSERT INTO cards (scryfall_id, name, set_code, collector_number) VALUES ('bad','Bad Card','TST','1')")
        conn.execute("INSERT INTO cards (scryfall_id, name, set_code, collector_number) VALUES ('good','Good Card','TST','2')")
        conn.execute("INSERT INTO deck_cards (deck_id, scryfall_id, quantity) VALUES (1, 'bad', 1)")
        conn.commit()
        return conn

    # G1: candidate not owned at all -> a brand-new starter lot is created
    conn = fresh_swap_db()
    conn.execute("INSERT INTO collection (scryfall_id, quantity, location) VALUES ('bad', 1, 'My Deck')")
    conn.commit()
    outcome = w.execute_swap(
        conn, deck_id=1, deck_name="My Deck",
        remove_scryfall_id="bad", remove_card_name="Bad Card",
        add_scryfall_id="good", add_card_name="Good Card", quantity=1,
    )
    deck_cards = dict(conn.execute("SELECT scryfall_id, quantity FROM deck_cards WHERE deck_id=1").fetchall())
    check("execute_swap() removes the replaced card from the mainboard", "bad" not in deck_cards)
    check("execute_swap() adds the new card to the mainboard", deck_cards.get("good") == 1)
    check("execute_swap() (not owned) creates a brand-new starter lot", outcome["created_lot"] is True)
    good_lot = conn.execute(
        "SELECT location, source FROM collection WHERE scryfall_id='good'"
    ).fetchone()
    check("the new starter lot is sleeved in the deck", good_lot[0] == "My Deck")
    check("the new starter lot is tagged as Swap Manager auto-added", good_lot[1] == "Auto-added (swap manager)")
    bad_loc = conn.execute("SELECT location FROM collection WHERE scryfall_id='bad'").fetchone()[0]
    check("execute_swap() moves the replaced card's lot back to storage (Box)", bad_loc == "Box")
    check("execute_swap() reports moved_out=True when a matching lot existed", outcome["moved_out"] is True)
    conn.close()

    # G2: candidate already owned and available in storage -> reassigned, not duplicated
    conn = fresh_swap_db()
    conn.execute("INSERT INTO collection (collection_id, scryfall_id, quantity, location) VALUES (10, 'good', 1, 'Box')")
    conn.commit()
    outcome = w.execute_swap(
        conn, deck_id=1, deck_name="My Deck",
        remove_scryfall_id="bad", remove_card_name="Bad Card",
        add_scryfall_id="good", add_card_name="Good Card", quantity=1,
    )
    check("execute_swap() (available in storage) reassigns the existing lot, doesn't create a new one", outcome["created_lot"] is False)
    check("execute_swap() reassigns the exact existing lot id", outcome["assigned_lot_id"] == 10)
    lot_count = conn.execute("SELECT COUNT(*) FROM collection WHERE scryfall_id='good'").fetchone()[0]
    check("exactly one lot exists for the added card afterward (no duplicate created)", lot_count == 1)
    reassigned_loc = conn.execute("SELECT location FROM collection WHERE collection_id=10").fetchone()[0]
    check("the reassigned lot's Location now matches the deck", reassigned_loc == "My Deck")
    conn.close()

    # G3: candidate owned but every copy is already in ANOTHER tracked deck
    # -> a fresh starter lot is created rather than stealing the other
    # deck's physical copy.
    conn = fresh_swap_db()
    conn.execute("INSERT INTO collection (collection_id, scryfall_id, quantity, location) VALUES (20, 'good', 1, 'Other Deck')")
    conn.commit()
    outcome = w.execute_swap(
        conn, deck_id=1, deck_name="My Deck",
        remove_scryfall_id="bad", remove_card_name="Bad Card",
        add_scryfall_id="good", add_card_name="Good Card", quantity=1,
    )
    check("execute_swap() (all copies elsewhere) creates a new starter lot instead of stealing one", outcome["created_lot"] is True)
    other_deck_loc = conn.execute("SELECT location FROM collection WHERE collection_id=20").fetchone()[0]
    check("execute_swap() never touches another deck's own lot", other_deck_loc == "Other Deck")
    conn.close()

    # G4: removed card has no collection lot sleeved in this deck at all
    # -> moved_out stays False, no error.
    conn = fresh_swap_db()
    conn.commit()
    outcome = w.execute_swap(
        conn, deck_id=1, deck_name="My Deck",
        remove_scryfall_id="bad", remove_card_name="Bad Card",
        add_scryfall_id="good", add_card_name="Good Card", quantity=1,
    )
    check("execute_swap() with no matching removed-card lot is a harmless no-op on that side", outcome["moved_out"] is False)
    deck_cards2 = dict(conn.execute("SELECT scryfall_id, quantity FROM deck_cards WHERE deck_id=1").fetchall())
    check("execute_swap() still performs the mainboard swap even with nothing to move in storage", deck_cards2 == {"good": 1})
    conn.close()

    # ------------------------------------------------------------------
    # H. Source-text checks — pages/4_Editor.py wiring
    # ------------------------------------------------------------------
    editor_path = os.path.join(PROJECT_ROOT, "pages", "4_Editor.py")
    with open(editor_path, encoding="utf-8") as f:
        editor_text = f.read()

    check("Editor's tab tuple now includes tab_swap", "tab_swap" in editor_text and "st.tabs(" in editor_text)
    check("Editor's tab labels now include 'Swap Manager'", '"Swap Manager"' in editor_text)
    check("Editor has a 'with tab_swap:' block", "with tab_swap:" in editor_text)
    check("Swap Manager tab checks live inventory via card_inventory_status", "q.card_inventory_status(conn" in editor_text)
    check("Swap Manager tab executes queued swaps via writes.execute_swap", "writes.execute_swap(" in editor_text)
    check("Swap Manager tab keeps its queue in st.session_state (not written until confirmed)", "queue_key" in editor_text and "st.session_state[queue_key]" in editor_text)

    check(
        "Editor's Collection tab uses a Location dropdown (loaders.load_location_options), not free text",
        "loaders.load_location_options(conn)" in editor_text,
    )
    check(
        "Editor's bulk collection editor uses a SelectboxColumn for Location",
        'st.column_config.SelectboxColumn("Location"' in editor_text,
    )
    check(
        "Editor manages a master Location catalog (add/remove)",
        "writes.add_location_to_catalog(conn" in editor_text and "writes.remove_location_from_catalog(conn" in editor_text,
    )
    check(
        "Deck Info's delete-deck checkbox now references writes.DEFAULT_LOCATION rather than 'clear'",
        "writes.DEFAULT_LOCATION" in editor_text,
    )

    loaders_path = os.path.join(PROJECT_ROOT, "dashboard_lib", "loaders.py")
    with open(loaders_path, encoding="utf-8") as f:
        loaders_text = f.read()
    check(
        "invalidate_reference_caches() clears the new location caches",
        "load_location_catalog.clear()" in loaders_text and "load_location_options.clear()" in loaders_text,
    )
    check(
        "invalidate_deck_caches() also clears load_location_options (deck names feed its options)",
        loaders_text.index("def invalidate_deck_caches")
        < loaders_text.index("load_location_options.clear()", loaders_text.index("def invalidate_deck_caches")),
    )

    migrate_path = os.path.join(PROJECT_ROOT, "scripts", "migrate.py")
    with open(migrate_path, encoding="utf-8") as f:
        migrate_text = f.read()
    check(
        "migrate.py seeds location_catalog on a fresh CSV import (schema.sql already has the empty table)",
        "INSERT OR IGNORE INTO location_catalog" in migrate_text,
    )

    print("\nAll Prompt Pass 13 offline checks passed.")


if __name__ == "__main__":
    main()
