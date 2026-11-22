"""Storage Locations and live inventory: the location_catalog table (fresh
schema and the upgrade path for an older database), its add/remove writes,
location_options(), and card_inventory_status().

Replaces sections A-E of the old scripts/_test_prompt_pass13 harness.
"""
from dashboard_lib import queries as q
from dashboard_lib import writes as w


def locations(conn):
    return {r[0] for r in conn.execute("SELECT location FROM location_catalog")}


def table_names(conn):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


# ------------------------------------------------------------------
# location_catalog: fresh schema and the upgrade path
# ------------------------------------------------------------------
def test_a_fresh_database_has_an_empty_location_catalog(conn):
    assert "location_catalog" in table_names(conn)
    assert locations(conn) == set()


def test_ensure_schema_recreates_and_seeds_the_catalog_on_an_old_database(conn):
    conn.execute("DROP TABLE location_catalog")
    conn.execute("INSERT INTO decks (deck_id, name) VALUES (1, 'Atraxa Superfriends')")
    conn.execute("INSERT INTO cards (scryfall_id, name, set_code, collector_number) VALUES ('c1','Sol Ring','TST','1')")
    conn.execute("INSERT INTO cards (scryfall_id, name, set_code, collector_number) VALUES ('c2','Forest','TST','2')")
    conn.execute("INSERT INTO collection (scryfall_id, quantity, location) VALUES ('c1', 1, 'Atraxa Superfriends')")
    conn.execute("INSERT INTO collection (scryfall_id, quantity, location) VALUES ('c2', 20, 'Lands Box')")
    conn.execute("INSERT INTO collection (scryfall_id, quantity, location) VALUES ('c2', 5, 'Trade Binder')")
    conn.commit()
    assert "location_catalog" not in table_names(conn)

    q.ensure_schema(conn)

    assert "location_catalog" in table_names(conn)
    seeded = locations(conn)
    assert {"Box", "Lands Box"} <= seeded         # the two standard locations
    assert "Trade Binder" in seeded               # existing non-deck locations
    # A deck's own name is already offered by location_options(); seeding it
    # would duplicate that.
    assert "Atraxa Superfriends" not in seeded

    q.ensure_schema(conn)                         # re-running is a no-op
    assert locations(conn) == seeded


# ------------------------------------------------------------------
# Catalog writes
# ------------------------------------------------------------------
def test_catalog_add_remove_and_listing(conn):
    assert w.list_location_catalog(conn) == []
    w.add_location_to_catalog(conn, "Box")
    w.add_location_to_catalog(conn, "Binder A")
    assert w.list_location_catalog(conn) == ["Binder A", "Box"]    # alphabetical
    w.add_location_to_catalog(conn, "Box")                         # duplicate: no-op
    w.add_location_to_catalog(conn, "   ")                         # blank: ignored
    assert w.list_location_catalog(conn) == ["Binder A", "Box"]
    w.remove_location_from_catalog(conn, "Binder A")
    assert w.list_location_catalog(conn) == ["Box"]


def test_location_options_unions_catalog_and_deck_names_case_insensitively_sorted(conn):
    w.add_location_to_catalog(conn, "Box")
    w.add_location_to_catalog(conn, "Lands Box")
    conn.execute("INSERT INTO decks (deck_id, name) VALUES (1, 'zzz Aristocrats')")
    conn.execute("INSERT INTO decks (deck_id, name) VALUES (2, 'Atraxa Superfriends')")
    conn.commit()
    options = q.location_options(conn)
    assert set(options) == {"Box", "Lands Box", "zzz Aristocrats", "Atraxa Superfriends"}
    assert options.index("Atraxa Superfriends") < options.index("zzz Aristocrats")


# ------------------------------------------------------------------
# card_inventory_status: owned copies split into this deck / other decks /
# available in storage
# ------------------------------------------------------------------
def bolt_inventory(conn):
    conn.execute("INSERT INTO decks (deck_id, name) VALUES (1, 'Deck A')")
    conn.execute("INSERT INTO decks (deck_id, name) VALUES (2, 'Deck B')")
    conn.execute("INSERT INTO cards (scryfall_id, name, set_code, collector_number) "
                 "VALUES ('c1','Lightning Bolt','TST','1')")
    conn.execute("INSERT INTO collection (scryfall_id, quantity, location) VALUES ('c1', 1, 'Deck A')")
    conn.execute("INSERT INTO collection (scryfall_id, quantity, location) VALUES ('c1', 1, 'Box')")
    conn.execute("INSERT INTO collection (scryfall_id, quantity, location) VALUES ('c1', 2, NULL)")
    conn.commit()
    return conn


def test_inventory_status_splits_copies_for_a_deck_that_does_not_run_it(conn):
    status = q.card_inventory_status(bolt_inventory(conn), "Lightning Bolt", deck_name="Deck B")
    assert status["owned_qty"] == 4
    assert status["in_other_decks"] == {"Deck A": 1}
    assert status["in_this_deck_qty"] == 0
    assert {lot["location"] for lot in status["available_lots"]} == {"Box", None}
    assert status["available_lots"][0]["quantity"] == 2            # largest lot first


def test_inventory_status_counts_the_passed_deck_as_this_deck_not_available(conn):
    status = q.card_inventory_status(bolt_inventory(conn), "Lightning Bolt", deck_name="Deck A")
    assert status["in_this_deck_qty"] == 1 and status["in_other_decks"] == {}


def test_inventory_status_for_unowned_and_blank_names(conn):
    bolt_inventory(conn)
    unowned = q.card_inventory_status(conn, "Counterspell", deck_name="Deck A")
    assert unowned["owned_qty"] == 0 and unowned["available_lots"] == []
    assert q.card_inventory_status(conn, "   ", deck_name="Deck A")["owned_qty"] == 0
