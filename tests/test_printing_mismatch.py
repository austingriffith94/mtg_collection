"""queries.printing_mismatches / writes.use_sleeved_printing: a deck list that
runs a different printing than the one sleeved in the deck."""
import pytest

from dashboard_lib import queries as q, writes


@pytest.fixture
def deck(conn):
    conn.execute("INSERT INTO decks (name) VALUES ('Gisa')")
    conn.commit()
    return conn.execute("SELECT deck_id FROM decks WHERE name='Gisa'").fetchone()[0]


def _list(conn, deck, sid, qty=1):
    conn.execute("INSERT INTO deck_cards (deck_id, scryfall_id, quantity) VALUES (?,?,?)", (deck, sid, qty))
    conn.commit()


def _sleeve(conn, sid, location="Gisa", qty=1):
    conn.execute("INSERT INTO collection (scryfall_id, quantity, location) VALUES (?,?,?)", (sid, qty, location))
    conn.commit()


def test_flags_a_list_printing_that_differs_from_the_sleeved_one(conn, deck, add_card):
    listed, sleeved = add_card("Palm", "soa", "63"), add_card("Palm", "ktk", "173")
    _list(conn, deck, listed)
    _sleeve(conn, sleeved)
    [m] = q.printing_mismatches(conn)
    assert m["card"] == "Palm" and m["fixable"]
    assert m["listed"][0]["scryfall_id"] == listed and m["sleeved"][0]["scryfall_id"] == sleeved


def test_matching_printing_and_unsleeved_cards_are_not_flagged(conn, deck, add_card):
    a, b = add_card("A", "soa", "1"), add_card("B", "soa", "2")
    _list(conn, deck, a)
    _sleeve(conn, a)
    _list(conn, deck, b)  # listed but not sleeved: the Missing check's job
    assert q.printing_mismatches(conn) == []


def test_a_copy_in_the_box_is_not_a_sleeved_copy(conn, deck, add_card):
    listed, other = add_card("Palm", "soa", "63"), add_card("Palm", "ktk", "173")
    _list(conn, deck, listed)
    _sleeve(conn, other, location="Box")
    assert q.printing_mismatches(conn) == []


def test_split_printings_are_re_split_to_match_the_sleeves(conn, deck, add_card):
    a, b, c = add_card("Pet", "a", "1"), add_card("Pet", "b", "2"), add_card("Pet", "c", "3")
    _list(conn, deck, a, 5)
    _sleeve(conn, b, qty=2)
    _sleeve(conn, c, qty=2)
    [m] = q.printing_mismatches(conn)
    assert not m["fixable"]
    writes.use_sleeved_printing(conn, deck, [a], [(b, 2), (c, 2)])
    # Total stays 5: the unsleeved fifth copy stays on the list, on the first printing.
    assert sorted(tuple(r) for r in conn.execute("SELECT scryfall_id, quantity FROM deck_cards")) == [(b, 3), (c, 2)]


def test_keep_hides_it_until_a_side_changes(conn, deck, add_card):
    listed, sleeved = add_card("Palm", "soa", "63"), add_card("Palm", "ktk", "173")
    _list(conn, deck, listed)
    _sleeve(conn, sleeved)
    writes.dismiss_printing_mismatch(conn, q.printing_mismatches(conn)[0]["key"])
    assert q.printing_mismatches(conn) == []
    assert len(q.printing_mismatches(conn, include_dismissed=True)) == 1
    _sleeve(conn, add_card("Palm", "xyz", "9"))  # sleeved side changed
    assert len(q.printing_mismatches(conn)) == 1


def test_use_sleeved_printing_moves_quantity_and_tags(conn, deck, add_card):
    listed, sleeved = add_card("Palm", "soa", "63"), add_card("Palm", "ktk", "173")
    _list(conn, deck, listed, 2)
    _sleeve(conn, sleeved, qty=2)
    conn.execute("INSERT INTO tags (tag_type, label) VALUES ('strategy','Aikido')")
    tag = conn.execute("SELECT tag_id FROM tags").fetchone()[0]
    conn.execute("INSERT INTO card_tags (deck_id, scryfall_id, tag_id) VALUES (?,?,?)", (deck, listed, tag))
    conn.commit()
    writes.use_sleeved_printing(conn, deck, [listed], [(sleeved, 2)])
    assert [tuple(r) for r in conn.execute("SELECT scryfall_id, quantity FROM deck_cards")] == [(sleeved, 2)]
    assert [tuple(r) for r in conn.execute("SELECT scryfall_id FROM card_tags")] == [(sleeved,)]
    assert q.printing_mismatches(conn) == []
