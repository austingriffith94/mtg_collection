"""Finding cards and getting them into the collection: name search and
printing lookup, auto_add_to_collection, and fetching extra printings from
Scryfall (client mocked, no network).

Replaces sections 1, 2 and 4 of the old scripts/_test_prompt_pass6 harness.
"""
import os
import sys
import types

import pytest

from dashboard_lib import card_resolver
from dashboard_lib import queries as q
from dashboard_lib import writes as w

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import scryfall_lookup  # noqa: E402


# (scryfall_id, name, set_code, collector_number, type_line, cmc, color_identity)
CARDS = [
    ("sr-c21", "Sol Ring", "c21", "263", "Artifact", 1, ""),
    ("sr-lea", "Sol Ring", "lea", "1", "Artifact", 1, ""),
    # Shares the "Sol" prefix: a search for "sol" finds both, Sol Ring first.
    ("solemn-1", "Solemn Simulacrum", "c21", "233", "Artifact Creature — Golem", 4, ""),
    ("counterspell-1", "Counterspell", "clb", "1", "Instant", 2, "U"),
]


@pytest.fixture
def cards(conn):
    for sid, name, set_code, number, type_line, cmc, identity in CARDS:
        conn.execute("INSERT INTO cards (scryfall_id, name, set_code, collector_number, type_line, cmc, "
                     "color_identity, commander_legal) VALUES (?,?,?,?,?,?,?,1)",
                     (sid, name, set_code, number, type_line, cmc, identity))
    conn.commit()
    return conn


# ------------------------------------------------------------------
# search_card_names / card_printings_by_name
# ------------------------------------------------------------------
def test_search_finds_prefix_and_contains_matches_prefix_first(cards):
    matches = q.search_card_names(cards, "sol")
    assert set(matches) == {"Sol Ring", "Solemn Simulacrum"}
    assert matches[0] == "Sol Ring"


@pytest.mark.parametrize("text", ["", "xyz-nonexistent"])
def test_search_returns_nothing_for_blank_or_unknown(cards, text):
    assert q.search_card_names(cards, text) == []   # blank must not dump the table


def test_printings_by_name_lists_every_printing_ordered_by_set(cards):
    printings = q.card_printings_by_name(cards, "Sol Ring")
    assert {p["scryfall_id"] for p in printings} == {"sr-c21", "sr-lea"}
    assert [p["set_code"] for p in printings] == sorted(p["set_code"] for p in printings)


def test_printings_by_name_is_case_insensitive_and_empty_for_unknowns(cards):
    assert len(q.card_printings_by_name(cards, "sol ring")) == 2
    assert q.card_printings_by_name(cards, "") == []
    assert q.card_printings_by_name(cards, "Black Lotus") == []


# ------------------------------------------------------------------
# collection_has_card / auto_add_to_collection
# ------------------------------------------------------------------
def test_auto_add_creates_a_tagged_starter_lot_once_per_printing(cards):
    assert w.collection_has_card(cards, "sr-c21") is False
    lot_id = w.auto_add_to_collection(cards, "sr-c21", quantity=1, location="Test Deck")
    assert lot_id is not None and w.collection_has_card(cards, "sr-c21") is True

    lot = cards.execute("SELECT quantity, location, foil, source FROM collection WHERE collection_id=?",
                        (lot_id,)).fetchone()
    assert (lot["quantity"], lot["location"], lot["foil"]) == (1, "Test Deck", 0)
    assert lot["source"] == "Auto-added (deck build)"

    # A second call for the same printing is a no-op, whatever quantity or place.
    assert w.auto_add_to_collection(cards, "sr-c21", quantity=5, location="Some Other Deck") is None
    assert cards.execute("SELECT COUNT(*) FROM collection WHERE scryfall_id='sr-c21'").fetchone()[0] == 1


def test_auto_add_is_per_printing_not_per_name(cards):
    w.auto_add_to_collection(cards, "sr-c21", quantity=1, location="Test Deck")
    assert w.auto_add_to_collection(cards, "sr-lea", quantity=1, location="Another Deck") is not None


# ------------------------------------------------------------------
# card_resolver.fetch_additional_printings (Scryfall client mocked)
# ------------------------------------------------------------------
def scryfall_printing(sid, set_code, number, rarity, price):
    return {"id": sid, "oracle_id": "sr-oracle", "name": "Sol Ring", "set": set_code,
            "collector_number": number, "type_line": "Artifact", "mana_cost": "{1}", "cmc": 1,
            "color_identity": [], "oracle_text": "{T}: Add {C}{C}.", "rarity": rarity,
            "image_uris": {"normal": f"https://example.com/{set_code}.jpg"},
            "legalities": {"commander": "legal"}, "prices": {"usd": price}}


@pytest.fixture
def mock_scryfall(monkeypatch):
    class MockClient:
        def __init__(self, *a, **kw):
            pass

        def get_all_printings(self, name):
            if name.strip() == "Sol Ring":
                return [scryfall_printing("sr-c21", "c21", "263", "common", "2.00"),
                        scryfall_printing("sr-m19", "m19", "222", "uncommon", "1.50")]  # m19 is new
            return []

    module = types.ModuleType("scryfall_lookup")
    module.ScryfallClient = MockClient
    module.to_card_row = scryfall_lookup.to_card_row
    monkeypatch.setattr(card_resolver, "_load_scryfall_module", lambda: module)


def test_fetch_additional_printings_adds_only_the_new_one(cards, mock_scryfall):
    added, err = card_resolver.fetch_additional_printings(cards, "Sol Ring")
    assert err is None and added == 1          # c21 was already local
    row = cards.execute("SELECT name FROM cards WHERE scryfall_id='sr-m19'").fetchone()
    assert row is not None and row["name"] == "Sol Ring"
    # Second run: everything is known.
    assert card_resolver.fetch_additional_printings(cards, "Sol Ring") == (0, None)


def test_fetch_additional_printings_reports_an_unknown_name(cards, mock_scryfall):
    added, err = card_resolver.fetch_additional_printings(cards, "Totally Fake Card Name")
    assert added == 0 and err is not None


def test_fetch_additional_printings_rejects_a_blank_name_without_a_network_call(conn):
    # No mock installed: reaching Scryfall would fail loudly.
    added, err = card_resolver.fetch_additional_printings(conn, "   ")
    assert added == 0 and err is not None
