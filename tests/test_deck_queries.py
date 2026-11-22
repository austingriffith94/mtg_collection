"""Read queries behind the home page, Decks page and Collection page:
list_decks_with_covers, deck_reserved_list_cards, deck_price_top10,
collection_dataframe's in_deck flag, in_deck_sleeved_count, and the
retired EDHREC salt column.

Replaces the query checks of the old scripts/_test_phase4, _pass5 and
_pass11 harnesses.
"""
import pytest

from dashboard_lib import loaders
from dashboard_lib import queries as q
from dashboard_lib import writes as w


def add_cards(conn, rows):
    """rows: (scryfall_id, name, set_code, number, color_identity, price, reserved, image_uri)."""
    for sid, name, set_code, number, identity, price, reserved, image in rows:
        conn.execute(
            "INSERT INTO cards (scryfall_id, name, set_code, collector_number, color_identity, "
            "current_price_usd, is_reserved, image_uri) VALUES (?,?,?,?,?,?,?,?)",
            (sid, name, set_code, number, identity, price, reserved, image))


@pytest.fixture
def seeded(conn):
    """Two decks sharing a commander name across two printings, a reserved-list
    card, and three collection lots in different states."""
    conn.execute("INSERT INTO decks (deck_id, name, commander, representative, cover_image_path) "
                 "VALUES (1, 'Goblin Storm', 'Krenko, Mob Boss', 'Krenko, Mob Boss', NULL)")
    conn.execute("INSERT INTO decks (deck_id, name, commander, representative, cover_image_path) "
                 "VALUES (2, 'Reserved Test', 'Krenko, Mob Boss', 'Krenko, Mob Boss', "
                 "'image_cache/deck_covers/2.png')")
    add_cards(conn, [
        ("krenko-a", "Krenko, Mob Boss", "M13", "191", "R", 12.50, 0, "https://example.com/krenko-a.jpg"),
        ("krenko-b", "Krenko, Mob Boss", "CMD", "100", "R", 8.00, 0, "https://example.com/krenko-b.jpg"),
        ("goblin-1", "Goblin Chieftain", "M15", "1", "R", 3.00, 0, None),
        ("mox-emerald", "Mox Emerald", "LEA", "263", "G", 4000.00, 1, None),
        ("sol-ring", "Sol Ring", "C21", "263", "", 2.00, 0, None),
        ("unowned-card", "Lightning Bolt", "M11", "146", "R", 1.00, 0, None),
    ])
    for deck, sid in [(1, "krenko-a"), (1, "mox-emerald"), (1, "sol-ring"), (2, "krenko-b"), (2, "goblin-1")]:
        conn.execute("INSERT INTO deck_cards (deck_id, scryfall_id, quantity) VALUES (?, ?, 1)", (deck, sid))
    # Mox: sleeved in deck 1, price paid logged. Bolt: owned, in no deck, no
    # location. Sol Ring: run in deck 1 but kept in a binder.
    conn.execute("INSERT INTO collection (scryfall_id, quantity, location, price_paid) "
                 "VALUES ('mox-emerald', 1, 'Goblin Storm', 350.00)")
    conn.execute("INSERT INTO collection (scryfall_id, quantity, location, price_paid) "
                 "VALUES ('unowned-card', 4, NULL, NULL)")
    conn.execute("INSERT INTO collection (scryfall_id, quantity, location, price_paid) "
                 "VALUES ('sol-ring', 1, 'Binder A', 1.50)")
    conn.commit()
    return conn


# ------------------------------------------------------------------
# list_decks_with_covers
# ------------------------------------------------------------------
def test_covers_resolve_the_commander_printing_run_in_each_deck(seeded):
    covers = {int(r["deck_id"]): r for _, r in q.list_decks_with_covers(seeded).iterrows()}
    assert len(covers) == 2
    # Same commander name, two printings: each deck gets ITS printing's art.
    assert covers[1]["commander_image_uri"] == "https://example.com/krenko-a.jpg"
    assert covers[2]["commander_image_uri"] == "https://example.com/krenko-b.jpg"
    # An uploaded cover is kept so the UI can prefer it over commander art.
    assert covers[2]["cover_image_path"] == "image_cache/deck_covers/2.png"


# ------------------------------------------------------------------
# deck_reserved_list_cards / deck_price_top10
# ------------------------------------------------------------------
def test_reserved_list_shows_price_not_quantity(seeded):
    reserved = q.deck_reserved_list_cards(seeded, 1)
    assert len(reserved) == 1
    assert "price" in reserved.columns and "quantity" not in reserved.columns
    assert float(reserved.iloc[0]["price"]) == 4000.00


def test_top10_is_sorted_and_joins_the_purchase_price(seeded):
    top10 = q.deck_price_top10(seeded, 1)
    assert "purchase_price" in top10.columns and "quantity" not in top10.columns
    assert list(top10["price"]) == sorted(top10["price"], reverse=True)
    by_name = {r["card_name"]: r for _, r in top10.iterrows()}
    assert float(by_name["Mox Emerald"]["purchase_price"]) == 350.00
    # No lot of Sol Ring is located at "Goblin Storm": NULL (None or NaN).
    paid = by_name["Sol Ring"]["purchase_price"]
    assert paid is None or paid != paid


# ------------------------------------------------------------------
# collection_dataframe's in_deck flag and the "unassigned" filter
# ------------------------------------------------------------------
def test_in_deck_flag_follows_the_deck_list_not_the_location(seeded):
    coll = {r["name"]: r for _, r in q.collection_dataframe(seeded).iterrows()}
    assert int(coll["Mox Emerald"]["in_deck"]) == 1
    assert int(coll["Sol Ring"]["in_deck"]) == 1     # run in deck 1 though kept in a binder
    assert int(coll["Lightning Bolt"]["in_deck"]) == 0


def test_unassigned_means_in_no_deck_and_no_location(seeded):
    # The Collection page's "Not in a deck OR another location" filter.
    coll = q.collection_dataframe(seeded)
    blank = coll["location"].isna() | (coll["location"].astype(str).str.strip() == "")
    unassigned = set(coll[(coll["in_deck"] == 0) & blank]["name"])
    assert unassigned == {"Lightning Bolt"}


# ------------------------------------------------------------------
# in_deck_sleeved_count: collection lots located at the deck, plus the
# deck list's basic lands (basics never get their own collection row)
# ------------------------------------------------------------------
@pytest.fixture
def sleeve_db(conn):
    for deck_id, name in [(1, "Basics Only"), (2, "Collection Only"), (3, "Mixed Deck"), (4, "Empty Deck")]:
        conn.execute("INSERT INTO decks (deck_id, name, commander) VALUES (?, ?, 'Some Commander')",
                     (deck_id, name))
    for sid, name, type_line, basic in [("forest-1", "Forest", "Basic Land — Forest", 1),
                                        ("island-1", "Island", "Basic Land — Island", 1),
                                        ("sol-ring", "Sol Ring", "Artifact", 0),
                                        ("goblin-1", "Goblin Chieftain", "Creature — Goblin", 0)]:
        conn.execute("INSERT INTO cards (scryfall_id, name, set_code, collector_number, type_line, "
                     "is_basic_land) VALUES (?,?,'TST','1',?,?)", (sid, name, type_line, basic))
    for deck, sid, qty in [(1, "forest-1", 10), (1, "island-1", 5), (2, "sol-ring", 1),
                           (3, "forest-1", 7), (3, "goblin-1", 1)]:
        conn.execute("INSERT INTO deck_cards (deck_id, scryfall_id, quantity) VALUES (?,?,?)", (deck, sid, qty))
    conn.execute("INSERT INTO collection (scryfall_id, quantity, location) VALUES ('sol-ring', 1, 'Collection Only')")
    conn.execute("INSERT INTO collection (scryfall_id, quantity, location) VALUES ('goblin-1', 1, 'Mixed Deck')")
    conn.commit()
    return conn


@pytest.mark.parametrize("deck_id, name, expected", [
    (1, "Basics Only", 15),         # 10 Forest + 5 Island, no collection rows
    (2, "Collection Only", 1),
    (3, "Mixed Deck", 8),           # 7 untracked basics + 1 tracked nonland
    (4, "Empty Deck", None),        # None, not 0: nothing to count
    (999, "Nonexistent Deck", None),
])
def test_in_deck_sleeved_count(sleeve_db, deck_id, name, expected):
    assert q.in_deck_sleeved_count(sleeve_db, deck_id, name) == expected


# ------------------------------------------------------------------
# EDHREC salt score is retired from the app but the column stays (the
# schema is additive: nothing is ever dropped)
# ------------------------------------------------------------------
def test_salt_code_is_gone():
    assert "edhrec_salt" not in q.CARD_COLUMNS
    assert not hasattr(q, "deck_salt_top10")
    assert not hasattr(w, "set_card_salt_score")
    assert not hasattr(w, "bulk_set_salt_scores")
    assert not hasattr(loaders, "load_deck_salt_top10")


def test_salt_column_stays_in_the_schema_but_is_never_read_back(conn):
    cols = {row[1] for row in conn.execute("PRAGMA table_info(cards)")}
    assert "edhrec_salt" in cols
    conn.execute("INSERT INTO cards (scryfall_id, name, set_code, collector_number, edhrec_salt) "
                 "VALUES ('t1', 'Test Card', 'TST', '1', 4.2)")
    conn.execute("INSERT INTO decks (deck_id, name) VALUES (1, 'Test Deck')")
    conn.execute("INSERT INTO deck_cards (deck_id, scryfall_id, quantity) VALUES (1, 't1', 1)")
    conn.commit()
    assert "edhrec_salt" not in q.deck_cards_dataframe(conn, 1).columns
    assert "edhrec_salt" not in q.collection_dataframe(conn).columns
