"""Tag layers on the deck board and in the counts (docs/TAG_SUGGESTIONS_PLAN.md
Phase 5): the payload carries every layer's tags, a tag edit writes the layer
the board is showing, and counts cover primary and secondary tags alike."""
import pandas as pd
import pytest

from dashboard_lib import deck_board, queries as q, writes

OWNED = {"sol ring": {"owned_qty": 1, "available_qty": 1}}
META = {"commander": None, "partner": None}


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


def test_untagged_deck_still_offers_strategy(conn, deck, ring):
    assert q.deck_tag_layers(conn, deck) == {"strategy": {}}


def test_layers_hold_each_types_tags_primary_first(conn, deck, ring):
    writes.set_card_tags(conn, deck, ring, "strategy", ["Artifacts"])
    writes.set_card_tags(conn, deck, ring, "function", ["Ramp", "Fixing"])
    assert q.deck_tag_layers(conn, deck) == {
        "strategy": {ring: ["Artifacts"]}, "function": {ring: ["Ramp", "Fixing"]}}


def test_payload_carries_a_layers_dict_per_card(conn, deck, ring):
    writes.set_card_tags(conn, deck, ring, "strategy", ["Artifacts"])
    writes.set_card_tags(conn, deck, ring, "function", ["Ramp", "Fixing"])
    df = q.deck_cards_dataframe(conn, deck)
    (card,) = deck_board.board_payload(df, META, OWNED, layer_tags=q.deck_tag_layers(conn, deck))
    assert card["tags"] == ["Artifacts"]
    assert card["layers"] == {"strategy": ["Artifacts"], "function": ["Ramp", "Fixing"]}


def test_payload_without_layer_data_has_empty_layers(conn, deck, ring):
    (card,) = deck_board.board_payload(q.deck_cards_dataframe(conn, deck), META, OWNED)
    assert card["layers"] == {}


def test_set_tags_writes_the_named_layer_and_undo_goes_back_to_it(conn, deck, ring):
    writes.set_card_tags(conn, deck, ring, "function", ["Ramp"])
    writes.set_card_tags(conn, deck, ring, "strategy", ["Artifacts"])
    result = deck_board.apply_board_action(
        conn, deck, {"op": "set_tags", "id": ring, "tags": ["Fixing", "Ramp"], "tag_type": "function"})
    assert writes.get_card_tags_by_type(conn, deck, "function")[ring] == ["Fixing", "Ramp"]
    assert writes.get_card_tags_by_type(conn, deck, "strategy")[ring] == ["Artifacts"]
    assert result["undo"]["tag_type"] == "function"
    deck_board.apply_board_action(conn, deck, result["undo"])
    assert writes.get_card_tags_by_type(conn, deck, "function")[ring] == ["Ramp"]


def test_set_tags_without_a_layer_still_means_strategy(conn, deck, ring):
    deck_board.apply_board_action(conn, deck, {"op": "set_tags", "id": ring, "tags": ["Artifacts"]})
    assert writes.get_card_tags_by_type(conn, deck, "strategy")[ring] == ["Artifacts"]


@pytest.mark.parametrize("layer", ["", "  ", 3])
def test_set_tags_refuses_a_bad_layer(conn, deck, ring, layer):
    with pytest.raises(ValueError, match="tag layer"):
        deck_board.apply_board_action(conn, deck, {"op": "set_tags", "id": ring, "tags": ["X"], "tag_type": layer})


def test_counts_cover_primary_and_secondary_and_follow_the_layer(conn, deck, ring, add_card):
    bolt = add_card("Lightning Bolt")
    conn.execute("INSERT INTO deck_cards (deck_id, scryfall_id, quantity) VALUES (?,?,1)", (deck, bolt))
    writes.set_card_tags(conn, deck, ring, "function", ["Ramp", "Fixing"])
    writes.set_card_tags(conn, deck, bolt, "function", ["Fixing"])
    writes.set_card_tags(conn, deck, ring, "strategy", ["Artifacts"])
    function = q.deck_tag_counts(conn, deck, "function").set_index("tag")["card_count"].to_dict()
    assert function == {"Fixing": 2, "Ramp": 1}
    strategy = q.deck_strategy_tag_counts(conn, deck)
    assert isinstance(strategy, pd.DataFrame) and list(strategy["tag"]) == ["Artifacts"]
