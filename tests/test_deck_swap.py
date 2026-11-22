"""The deck-swap write path: what delete_deck does with the deck's collection
lots, and what execute_swap does to the list and the physical copies.

Replaces sections F and G of the old scripts/_test_prompt_pass13 harness.
Deeper coverage of execute_swap lives in test_deck_changes_writes.py and
test_edit_flow.py; these are the basic cases.
"""
import pytest

from dashboard_lib import writes as w


# ------------------------------------------------------------------
# delete_deck puts located lots back in storage
# ------------------------------------------------------------------
@pytest.fixture
def doomed(conn):
    conn.execute("INSERT INTO decks (deck_id, name) VALUES (1, 'Doomed Deck')")
    conn.execute("INSERT INTO cards (scryfall_id, name, set_code, collector_number) VALUES ('c1','Sol Ring','TST','1')")
    conn.execute("INSERT INTO collection (collection_id, scryfall_id, quantity, location) "
                 "VALUES (1, 'c1', 1, 'Doomed Deck')")
    conn.commit()
    return conn


def lot_location(conn):
    return conn.execute("SELECT location FROM collection WHERE collection_id=1").fetchone()[0]


def test_default_location_is_box():
    assert w.DEFAULT_LOCATION == "Box"


def test_delete_deck_moves_its_lots_to_the_default_location(doomed):
    w.delete_deck(doomed, 1, clear_collection_locations=True)
    assert lot_location(doomed) == "Box"          # not NULL


def test_delete_deck_can_leave_the_location_text_alone(doomed):
    w.delete_deck(doomed, 1, clear_collection_locations=False)
    assert lot_location(doomed) == "Doomed Deck"


# ------------------------------------------------------------------
# execute_swap: cut "bad", add "good" in deck 1. A card you do not own has
# no collection row, so Auto puts it on the buy list instead of making a lot
# (docs/archive/UNOWNED_LOTS_PLAN.md).
# ------------------------------------------------------------------
@pytest.fixture
def swap_db(conn):
    conn.execute("INSERT INTO decks (deck_id, name) VALUES (1, 'My Deck')")
    conn.execute("INSERT INTO decks (deck_id, name) VALUES (2, 'Other Deck')")
    conn.execute("INSERT INTO cards (scryfall_id, name, set_code, collector_number) VALUES ('bad','Bad Card','TST','1')")
    conn.execute("INSERT INTO cards (scryfall_id, name, set_code, collector_number) VALUES ('good','Good Card','TST','2')")
    conn.execute("INSERT INTO deck_cards (deck_id, scryfall_id, quantity) VALUES (1, 'bad', 1)")
    conn.commit()
    return conn


def swap(conn):
    return w.execute_swap(conn, deck_id=1, deck_name="My Deck",
                          remove_scryfall_id="bad", remove_card_name="Bad Card",
                          add_scryfall_id="good", add_card_name="Good Card", quantity=1)


def mainboard(conn):
    return dict(conn.execute("SELECT scryfall_id, quantity FROM deck_cards WHERE deck_id=1").fetchall())


def test_swap_of_an_unowned_card_goes_on_the_buy_list_and_frees_the_cut_card(swap_db):
    swap_db.execute("INSERT INTO collection (scryfall_id, quantity, location) VALUES ('bad', 1, 'My Deck')")
    swap_db.commit()
    outcome = swap(swap_db)
    assert mainboard(swap_db) == {"good": 1}
    assert outcome["to_buy"] is True and outcome["assigned_lot_id"] is None
    assert swap_db.execute("SELECT COUNT(*) FROM collection WHERE scryfall_id='good'").fetchone()[0] == 0
    # The cut card's lot goes back to storage.
    assert swap_db.execute("SELECT location FROM collection WHERE scryfall_id='bad'").fetchone()[0] == "Box"
    assert outcome["moved_out"] is True


def test_swap_reassigns_a_free_lot_in_storage_instead_of_buying(swap_db):
    swap_db.execute("INSERT INTO collection (collection_id, scryfall_id, quantity, location) "
                    "VALUES (10, 'good', 1, 'Box')")
    swap_db.commit()
    outcome = swap(swap_db)
    assert outcome["to_buy"] is False and outcome["assigned_lot_id"] == 10
    assert swap_db.execute("SELECT COUNT(*) FROM collection WHERE scryfall_id='good'").fetchone()[0] == 1
    assert swap_db.execute("SELECT location FROM collection WHERE collection_id=10").fetchone()[0] == "My Deck"


def test_swap_never_takes_a_copy_out_of_another_deck(swap_db):
    swap_db.execute("INSERT INTO collection (collection_id, scryfall_id, quantity, location) "
                    "VALUES (20, 'good', 1, 'Other Deck')")
    swap_db.commit()
    assert swap(swap_db)["to_buy"] is True
    assert swap_db.execute("SELECT location FROM collection WHERE collection_id=20").fetchone()[0] == "Other Deck"


def test_swap_with_no_lot_for_the_cut_card_still_swaps_the_list(swap_db):
    outcome = swap(swap_db)
    assert outcome["moved_out"] is False
    assert mainboard(swap_db) == {"good": 1}
