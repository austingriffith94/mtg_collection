"""Tag vocabularies and the one-time copy into `function`
(docs/TAG_SUGGESTIONS_PLAN.md Phase 1b)."""
import pytest

from dashboard_lib import queries as q, tag_vocab, writes


@pytest.fixture
def deck(conn):
    cur = conn.execute("INSERT INTO decks (name) VALUES ('Raktres')")
    conn.commit()
    return cur.lastrowid


@pytest.fixture
def listed(conn, deck, add_card):
    """listed('Sol Ring') -> scryfall_id of a card on the deck's list."""
    def _add(name):
        sid = add_card(name)
        conn.execute("INSERT INTO deck_cards (deck_id, scryfall_id, quantity) VALUES (?,?,1)", (deck, sid))
        conn.commit()
        return sid
    return _add


def _vocab(conn, deck, tag_type, **kwargs):
    return {v["label"]: v["definition"] for v in q.deck_tag_vocabulary(conn, deck, tag_type, **kwargs)}


def test_every_menu_label_has_a_definition_and_the_copied_labels_are_on_the_menu():
    assert all(definition.strip() for definition in tag_vocab.FUNCTION_MENU.values())
    assert set(tag_vocab.COPIED_FUNCTION_LABELS) <= set(tag_vocab.FUNCTION_MENU)


def test_removal_and_interaction_definitions_keep_the_agreed_line():
    removal, interaction = tag_vocab.FUNCTION_MENU["Removal"], tag_vocab.FUNCTION_MENU["Interaction"]
    assert "Bounce" in removal
    for word in ("counterspells", "tapping down", "goad", "stealing", "taxing", "pingers"):
        assert word in interaction


def test_vocabulary_is_active_labels_plus_labels_on_cards(conn, deck, listed):
    sol = listed("Sol Ring")
    writes.set_card_tags(conn, deck, sol, "function", ["Ramp"])
    writes.set_tag_vocab_label(conn, deck, "function", "Tutor")
    assert list(_vocab(conn, deck, "function")) == ["Ramp", "Tutor"]
    assert q.active_tag_vocab(conn, deck, "function") == {"Tutor": None}


def test_function_labels_default_to_the_menu_and_a_saved_definition_wins(conn, deck):
    writes.set_tag_vocab_label(conn, deck, "function", "Ramp")
    writes.set_tag_vocab_label(conn, deck, "function", "Tutor", "  Finds combo pieces.  ")
    assert _vocab(conn, deck, "function") == {
        "Ramp": tag_vocab.FUNCTION_MENU["Ramp"], "Tutor": "Finds combo pieces."}


def test_a_label_with_no_definition_is_not_offered_to_the_model(conn, deck, listed):
    sol = listed("Sol Ring")
    writes.set_card_tags(conn, deck, sol, "strategy", ["Rakdos Target", "Any"])
    writes.set_tag_vocab_label(conn, deck, "strategy", "Any", "Fits every creature type in the deck.")
    assert _vocab(conn, deck, "strategy") == {
        "Any": "Fits every creature type in the deck.", "Rakdos Target": None}
    assert list(_vocab(conn, deck, "strategy", defined_only=True)) == ["Any"]


def test_vocabulary_is_per_deck_and_per_layer(conn, deck):
    other = conn.execute("INSERT INTO decks (name) VALUES ('Gisa')").lastrowid
    writes.set_tag_vocab_label(conn, deck, "function", "Ramp")
    assert _vocab(conn, other, "function") == {} and _vocab(conn, deck, "strategy") == {}


def test_a_label_differing_only_in_case_is_one_entry(conn, deck, listed):
    sol = listed("Sol Ring")
    writes.set_card_tags(conn, deck, sol, "strategy", ["ramp"])
    writes.set_tag_vocab_label(conn, deck, "strategy", "Ramp", "Old.")
    writes.set_tag_vocab_label(conn, deck, "strategy", "RAMP", "New.")
    assert _vocab(conn, deck, "strategy") == {"RAMP": "New."}


def test_deleting_a_label_keeps_card_tags(conn, deck, listed):
    sol = listed("Sol Ring")
    writes.set_card_tags(conn, deck, sol, "strategy", ["Burn"])
    writes.set_tag_vocab_label(conn, deck, "strategy", "Burn", "Direct damage.")
    writes.delete_tag_vocab_label(conn, deck, "strategy", "burn")
    assert _vocab(conn, deck, "strategy") == {"Burn": None}
    assert writes.get_card_tags_by_type(conn, deck, "strategy") == {sol: ["Burn"]}


def test_blank_label_is_ignored(conn, deck):
    writes.set_tag_vocab_label(conn, deck, "function", "  ")
    assert q.active_tag_vocab(conn, deck, "function") == {}


def test_delete_deck_clears_its_vocabulary(conn, deck):
    writes.set_tag_vocab_label(conn, deck, "function", "Ramp")
    assert writes.delete_deck(conn, deck)[1] is None
    assert conn.execute("SELECT COUNT(*) FROM deck_tag_vocab").fetchone()[0] == 0


def test_copy_adds_functional_labels_and_leaves_strategy_alone(conn, deck, listed):
    sol, bolt, goblin = listed("Sol Ring"), listed("Lightning Bolt"), listed("Goblin")
    writes.set_card_tags(conn, deck, sol, "strategy", ["Ramp"])
    writes.set_card_tags(conn, deck, bolt, "strategy", ["Burn", "Removal"])
    writes.set_card_tags(conn, deck, goblin, "strategy", ["Fodder"])
    assert len(q.pending_function_tag_copy(conn)) == 2
    assert writes.copy_strategy_tags_to_function(conn) == (None, 2)
    assert writes.get_card_tags_by_type(conn, deck, "function") == {sol: ["Ramp"], bolt: ["Removal"]}
    assert writes.get_card_tags_by_type(conn, deck, "strategy") == {
        sol: ["Ramp"], bolt: ["Burn", "Removal"], goblin: ["Fodder"]}


def test_copy_keeps_the_strategy_order_so_the_primary_carries_over(conn, deck, listed):
    sol = listed("Sol Ring")
    writes.set_card_tags(conn, deck, sol, "strategy", ["Removal", "Ramp"])
    writes.copy_strategy_tags_to_function(conn)
    assert writes.get_card_tags_by_type(conn, deck, "function")[sol] == ["Removal", "Ramp"]


def test_copy_goes_after_an_existing_function_primary(conn, deck, listed):
    sol = listed("Sol Ring")
    writes.set_card_tags(conn, deck, sol, "function", ["Tutor"])
    writes.set_card_tags(conn, deck, sol, "strategy", ["Ramp"])
    writes.copy_strategy_tags_to_function(conn)
    assert writes.get_card_tags_by_type(conn, deck, "function")[sol] == ["Tutor", "Ramp"]


def test_copy_is_safe_to_rerun_and_picks_up_new_tags(conn, deck, listed):
    sol, bolt = listed("Sol Ring"), listed("Lightning Bolt")
    writes.set_card_tags(conn, deck, sol, "strategy", ["Ramp"])
    writes.copy_strategy_tags_to_function(conn)
    assert q.pending_function_tag_copy(conn) == []
    assert writes.copy_strategy_tags_to_function(conn) == (None, 0)
    writes.set_card_tags(conn, deck, bolt, "strategy", ["Removal"])
    assert writes.copy_strategy_tags_to_function(conn)[1] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM card_tags ct JOIN tags t USING (tag_id) WHERE t.tag_type = 'function'"
    ).fetchone()[0] == 2


def test_copy_skips_a_stale_tag_on_a_card_off_the_list(conn, deck, add_card):
    writes.set_card_tags(conn, deck, add_card("Sol Ring"), "strategy", ["Ramp"])
    assert q.pending_function_tag_copy(conn) == []


def test_copy_refuses_to_run_without_a_backup(conn, deck, listed, monkeypatch):
    sol = listed("Sol Ring")
    writes.set_card_tags(conn, deck, sol, "strategy", ["Ramp"])

    def fail(_conn):
        raise RuntimeError("The database backup failed, so nothing was changed. See the log.")

    monkeypatch.setattr(writes.backup, "required_snapshot", fail)
    with pytest.raises(RuntimeError):
        writes.copy_strategy_tags_to_function(conn)
    assert writes.get_card_tags_by_type(conn, deck, "function") == {}
