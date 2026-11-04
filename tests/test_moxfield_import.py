"""Moxfield list import: parsing pasted text, and building a deck whose cards
all sit on the Shortlist. Only cards already in the throwaway database are
used, so nothing here reaches Scryfall."""
import pytest

from dashboard_lib import card_resolver, moxfield_import, queries as q, writes


def test_parse_reads_printing_quantity_and_finish_marker():
    rows = moxfield_import.parse_list(
        "1 Combustible Gearhulk (C21) 163\n"
        "2x Sol Ring\n"
        "1 Birgi, God of Storytelling // Harnfel, Horn of Bounty (KHM) 129 *F*\n"
    )
    assert [(r["name"], r["set_code"], r["number"], r["quantity"]) for r in rows] == [
        ("Combustible Gearhulk", "c21", "163", 1),
        ("Sol Ring", None, None, 2),
        ("Birgi, God of Storytelling // Harnfel, Horn of Bounty", "khm", "129", 1),
    ]


def test_parse_tracks_sections_and_skips_comments_and_blanks():
    rows = moxfield_import.parse_list(
        "// my deck\nCommander:\n1 Atraxa (C16) 28\n\nMainboard\n1 Sol Ring\nSIDEBOARD\n1 Mana Crypt\n")
    assert [(r["name"], r["section"]) for r in rows] == [
        ("Atraxa", "Commander"), ("Sol Ring", "Mainboard"), ("Mana Crypt", "Sideboard")]


def test_parse_merges_the_same_printing_listed_twice():
    rows = moxfield_import.parse_list("1 Forest (SLD) 5\n3 Forest (SLD) 5\n1 Forest (SLD) 6")
    assert [(r["number"], r["quantity"]) for r in rows] == [("5", 4), ("6", 1)]


def test_import_builds_a_deck_with_everything_on_the_shortlist(conn, add_card):
    ring = add_card("Sol Ring", "c21", "263")
    atraxa = add_card("Atraxa", "c16", "28")
    deck_id, summary, err = moxfield_import.import_list(
        conn, "Imported", "Commander\n1 Atraxa (C16) 28\nMainboard\n2 Sol Ring (C21) 263\n")
    assert err is None
    assert summary == {"added": 2, "cards": 3, "failed": []}
    deck = conn.execute("SELECT commander FROM decks WHERE deck_id=?", (deck_id,)).fetchone()
    assert deck[0] == "Atraxa"
    changes = {c["add_scryfall_id"]: c for c in writes.list_changes(conn, deck_id)}
    assert set(changes) == {ring, atraxa}
    assert changes[ring]["quantity"] == 2
    assert q.deck_list_quantity(conn, deck_id, ring) == 0  # the deck's real list is untouched


def test_import_reports_lines_it_could_not_resolve(conn, add_card, monkeypatch):
    add_card("Sol Ring", "c21", "263")
    monkeypatch.setattr(card_resolver, "_load_scryfall_module", lambda: (_ for _ in ()).throw(ImportError()))
    _id, summary, err = moxfield_import.import_list(conn, "Imported", "1 Sol Ring\n1 Not A Real Card\n")
    assert err is None
    assert summary["added"] == 1
    assert [line for line, _why in summary["failed"]] == ["1 Not A Real Card"]


@pytest.mark.parametrize("name, text, fragment", [
    ("Deck", "   \n// nothing\n", "Paste a list"),
    ("", "1 Sol Ring", "can't be empty"),
])
def test_import_refuses_an_empty_paste_or_name(conn, name, text, fragment):
    deck_id, summary, err = moxfield_import.import_list(conn, name, text)
    assert deck_id is None and fragment in err
    assert conn.execute("SELECT COUNT(*) FROM decks").fetchone()[0] == 0


def test_import_refuses_a_taken_deck_name(conn, add_card):
    add_card("Sol Ring")
    writes.create_deck(conn, "Taken")
    _id, _summary, err = moxfield_import.import_list(conn, "Taken", "1 Sol Ring")
    assert "already exists" in err
