"""Primary and secondary tags (docs/archive/TAG_SUGGESTIONS_PLAN.md Phase 1a): a card's
tags keep the order they were given, the first one is its primary, and every
path that reads, compares or rewrites tags respects that."""
import sqlite3

import pytest

from dashboard_lib import deck_board, queries as q, writes
from dashboard_lib import formatting as fmt


@pytest.fixture
def deck(conn):
    cur = conn.execute("INSERT INTO decks (name) VALUES ('Raktres')")
    conn.commit()
    return cur.lastrowid


@pytest.fixture
def ring(conn, deck, add_card):
    sid = add_card("Sol Ring")
    conn.execute("INSERT INTO deck_cards (deck_id, scryfall_id, quantity) VALUES (?,?,1)", (deck, sid))
    conn.commit()
    return sid


def _strategy(conn, deck, sid):
    return writes.get_card_tags_by_type(conn, deck, "strategy").get(sid, [])


def test_set_card_tags_keeps_the_order_it_is_given(conn, deck, ring):
    writes.set_card_tags(conn, deck, ring, "strategy", ["Zebra", " ", "Apple", "zebra"])
    assert _strategy(conn, deck, ring) == ["Zebra", "Apple"]
    assert writes.get_all_card_tags(conn, deck, ring) == {"strategy": ["Zebra", "Apple"]}
    writes.set_card_tags(conn, deck, ring, "strategy", ["Apple", "Zebra"])
    assert _strategy(conn, deck, ring) == ["Apple", "Zebra"]


def test_each_tag_type_has_its_own_order(conn, deck, ring):
    writes.set_card_tags(conn, deck, ring, "strategy", ["Zebra", "Apple"])
    writes.set_card_tags(conn, deck, ring, "function", ["Ramp", "Fixing"])
    assert writes.get_all_card_tags(conn, deck, ring) == {
        "function": ["Ramp", "Fixing"], "strategy": ["Zebra", "Apple"]}


def test_deck_dataframe_lists_the_primary_first_and_only_strategy_tags(conn, deck, ring, add_card):
    bolt = add_card("Lightning Bolt")
    conn.execute("INSERT INTO deck_cards (deck_id, scryfall_id, quantity) VALUES (?,?,1)", (deck, bolt))
    writes.set_card_tags(conn, deck, ring, "strategy", ["Zebra", "Apple", "Mango"])
    writes.set_card_tags(conn, deck, ring, "function", ["Ramp"])
    df = q.deck_cards_dataframe(conn, deck).set_index("name")
    assert df.loc["Sol Ring", "strategy_tags"] == "Zebra,Apple,Mango"
    assert fmt.primary_tag(df.loc["Lightning Bolt", "strategy_tags"]) is None
    assert len(df) == 2


def test_a_reorder_is_a_change_but_a_case_difference_is_not(conn, deck, ring):
    writes.set_card_tags(conn, deck, ring, "strategy", ["Ramp", "Combo"])
    assert writes.bulk_set_card_tags(conn, deck, "strategy", {ring: ["ramp", " combo"]}) == 0
    assert writes.bulk_set_card_tags(conn, deck, "strategy", {ring: ["Combo", "Ramp"]}) == 1
    assert _strategy(conn, deck, ring) == ["Combo", "Ramp"]


def test_board_saves_a_reorder_and_undo_restores_the_old_order(conn, deck, ring):
    writes.set_card_tags(conn, deck, ring, "strategy", ["Ramp", "Combo"])
    result = deck_board.apply_board_action(conn, deck, {"op": "set_tags", "id": ring, "tags": ["Combo", "Ramp"]})
    assert _strategy(conn, deck, ring) == ["Combo", "Ramp"]
    deck_board.apply_board_action(conn, deck, result["undo"])
    assert _strategy(conn, deck, ring) == ["Ramp", "Combo"]


def test_undoing_a_removal_restores_the_tag_order(conn, deck, ring):
    writes.set_card_tags(conn, deck, ring, "strategy", ["Zebra", "Apple"])
    result = deck_board.apply_board_action(conn, deck, {"op": "remove", "id": ring})
    assert _strategy(conn, deck, ring) == []
    deck_board.apply_board_action(conn, deck, result["undo"])
    assert _strategy(conn, deck, ring) == ["Zebra", "Apple"]


def test_printing_fix_carries_the_tag_order(conn, deck, ring, add_card):
    sleeved = add_card("Sol Ring", "ktk", "2")
    writes.set_card_tags(conn, deck, ring, "strategy", ["Zebra", "Apple"])
    writes.use_sleeved_printing(conn, deck, [ring], [(sleeved, 1)])
    assert _strategy(conn, deck, sleeved) == ["Zebra", "Apple"]


def test_printing_fix_merges_two_printings_at_the_earlier_position(conn, deck, ring, add_card):
    other, sleeved = add_card("Sol Ring", "lea", "3"), add_card("Sol Ring", "ktk", "2")
    conn.execute("INSERT INTO deck_cards (deck_id, scryfall_id, quantity) VALUES (?,?,1)", (deck, other))
    writes.set_card_tags(conn, deck, ring, "strategy", ["Zebra", "Apple"])
    writes.set_card_tags(conn, deck, other, "strategy", ["Mango", "Zebra"])
    writes.use_sleeved_printing(conn, deck, [ring, other], [(sleeved, 2)])
    # Zebra and Mango both sat first somewhere; the shared position sorts by label.
    assert _strategy(conn, deck, sleeved) == ["Mango", "Zebra", "Apple"]


def test_position_upgrade_makes_an_existing_tag_primary():
    old = sqlite3.connect(":memory:")
    old.executescript(
        """CREATE TABLE tags (tag_id INTEGER PRIMARY KEY, tag_type TEXT, label TEXT);
           CREATE TABLE card_tags (deck_id INT, scryfall_id TEXT, tag_id INT);
           INSERT INTO card_tags VALUES (1, 'a', 1);""")
    old.execute(next(ddl for table, col, ddl in q._SCHEMA_UPGRADES if (table, col) == ("card_tags", "position")))
    assert old.execute("SELECT position FROM card_tags").fetchone()[0] == 0
    old.close()


@pytest.mark.parametrize("tags, primary", [
    ("Ramp,Removal", "Ramp"), ("Ramp", "Ramp"), (None, None), (float("nan"), None), ("", None),
])
def test_primary_tag(tags, primary):
    assert fmt.primary_tag(tags) == primary
