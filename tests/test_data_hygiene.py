"""UI plan Phase 8 — reference-list overviews, orphan report/cleanup writes,
image pruning and the CSV export, against the in-memory `conn` fixture."""
import os
import sys
import types

from dashboard_lib import queries, writes

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts"))


def _tag(conn, table, name, tag):
    conn.execute(f"INSERT INTO {table} (card_name, tag) VALUES (?,?)", (name, tag))
    conn.commit()


# ------------------------------ overviews ------------------------------
def test_tagged_name_without_card_row_still_listed(conn):
    _tag(conn, "game_changer_tags", "Ghost Card", "Combo")
    df = queries.game_changers_overview(conn)
    row = df[df["name"] == "Ghost Card"].iloc[0]
    assert not row["has_card"]
    assert row["scryfall_status"] == "unknown (no card data)"
    assert row["categories"] == "Combo"
    assert row["needs_attention"]
    assert row["ownership"] == "Not owned"


def test_untagged_scryfall_flagged_card_listed_and_tagged_unflagged_marked(conn, add_card):
    flagged = add_card("Flagged Card")
    conn.execute("UPDATE cards SET is_game_changer = 1 WHERE scryfall_id=?", (flagged,))
    add_card("Yuriko")  # is_game_changer defaults to 0 -> Scryfall no longer flags it
    _tag(conn, "game_changer_tags", "Yuriko", "Power")
    df = queries.game_changers_overview(conn).set_index("name")
    assert df.loc["Flagged Card", "scryfall_status"] == "flagged"
    assert df.loc["Flagged Card", "categories"] == ""
    assert df.loc["Flagged Card", "needs_attention"]  # flagged, nothing categorised it
    assert df.loc["Yuriko", "scryfall_status"] == "not flagged"
    assert df.loc["Yuriko", "needs_attention"]        # tagged, Scryfall disagrees


def test_overview_counts_and_ownership(conn, add_card):
    sid = add_card("Sol Ring")
    conn.execute("INSERT INTO collection (scryfall_id, quantity) VALUES (?, 2)", (sid,))
    _tag(conn, "mana_tags", "Sol Ring", "Fast")
    _tag(conn, "mana_tags", "Sol Ring", "Rock")
    df = queries.mana_tags_overview(conn)
    assert len(df) == 1
    assert df.iloc[0]["owned_qty"] == 2 and df.iloc[0]["ownership"] == "Owned"
    assert set(df.iloc[0]["tags"].split(",")) == {"Fast", "Rock"}


def test_empty_overviews_have_columns(conn):
    assert queries.game_changers_overview(conn).empty
    assert queries.mana_tags_overview(conn).empty


# ------------------------------ list deletes ------------------------------
def test_delete_names_removes_only_that_name(conn):
    _tag(conn, "mana_tags", "A", "Fast")
    _tag(conn, "mana_tags", "A", "Dual")
    _tag(conn, "mana_tags", "B", "Fast")
    assert writes.delete_mana_tag_names(conn, ["A", "Nope"]) == 1
    assert writes.get_mana_tags_map(conn) == {"B": ["Fast"]}


def test_set_tags_for_name_with_no_card(conn):
    writes.set_mana_tags(conn, "Unknown Card", ["Fast"])
    assert queries.mana_tags_overview(conn).iloc[0]["name"] == "Unknown Card"


# ------------------------------ orphans ------------------------------
def _make_deck(conn, deck_id=1):
    conn.execute(
        "INSERT INTO decks (deck_id, name, commander, color_identity) VALUES (?, 'D', NULL, '')",
        (deck_id,),
    )
    conn.commit()


def test_orphan_report_buckets(conn, add_card, tmp_path):
    used = add_card("Used")
    unused = add_card("Unused")
    listed = add_card("Listed")
    _make_deck(conn)
    conn.execute("INSERT INTO deck_cards (deck_id, scryfall_id, quantity) VALUES (1, ?, 1)", (used,))
    _tag(conn, "mana_tags", "Listed", "Fast")
    # a deck tag for a card that is not in the deck
    conn.execute("INSERT INTO tags (tag_type, label) VALUES ('strategy', 'Ramp')")
    conn.execute("INSERT INTO card_tags (deck_id, scryfall_id, tag_id) VALUES (1, ?, 1)", (unused,))
    conn.commit()
    image_dir = tmp_path / "image_cache"
    image_dir.mkdir()
    (image_dir / "no-such-card.jpg").write_bytes(b"x")
    (image_dir / "deck_covers").mkdir()
    conn.execute("UPDATE cards SET local_image_path='image_cache/gone.jpg' WHERE scryfall_id=?", (listed,))

    rep = queries.orphan_report(conn, image_dir=str(image_dir))
    # "Unused" has a stale tag row, so it is referenced; "Listed" by a tag list.
    assert rep["unreferenced_cards"].empty
    assert rep["stale_card_tags"]["card"].tolist() == ["Unused"]
    assert rep["orphan_image_files"]["file"].tolist() == ["no-such-card.jpg"]
    assert rep["missing_image_files"]["scryfall_id"].tolist() == [listed]


def test_delete_orphan_cards_refuses_referenced(conn, add_card):
    lonely = add_card("Lonely")
    in_deck = add_card("InDeck")
    listed = add_card("Listed")
    _make_deck(conn)
    conn.execute("INSERT INTO deck_cards (deck_id, scryfall_id, quantity) VALUES (1, ?, 1)", (in_deck,))
    _tag(conn, "game_changer_tags", "Listed", "Combo")
    assert queries.orphan_report(conn)["unreferenced_cards"]["scryfall_id"].tolist() == [lonely]
    deleted = writes.delete_orphan_cards(conn, [lonely, in_deck, listed])
    assert deleted == [(lonely, "Lonely")]
    left = {r[0] for r in conn.execute("SELECT scryfall_id FROM cards")}
    assert left == {in_deck, listed}


def test_delete_orphan_card_tags(conn, add_card):
    a, b = add_card("A"), add_card("B")
    _make_deck(conn)
    conn.execute("INSERT INTO deck_cards (deck_id, scryfall_id, quantity) VALUES (1, ?, 1)", (a,))
    conn.execute("INSERT INTO tags (tag_type, label) VALUES ('strategy', 'Ramp')")
    conn.execute("INSERT INTO card_tags (deck_id, scryfall_id, tag_id) VALUES (1, ?, 1)", (a,))
    conn.execute("INSERT INTO card_tags (deck_id, scryfall_id, tag_id) VALUES (1, ?, 1)", (b,))
    conn.commit()
    assert writes.delete_orphan_card_tags(conn, [(1, a), (1, b)]) == 1  # (1, a) still in the deck
    assert [r[0] for r in conn.execute("SELECT scryfall_id FROM card_tags")] == [a]


def test_delete_orphan_image_files_and_missing_paths(conn, add_card, tmp_path):
    known = add_card("Known")
    (tmp_path / "stray.jpg").write_bytes(b"x")
    (tmp_path / f"{known}.jpg").write_bytes(b"x")
    done = writes.delete_orphan_image_files(conn, ["stray.jpg", f"{known}.jpg"], str(tmp_path))
    assert done == ["stray.jpg"]
    assert (tmp_path / f"{known}.jpg").exists()
    conn.execute("UPDATE cards SET local_image_path='image_cache/x.jpg'")
    writes.clear_missing_image_paths(conn, [known])
    assert conn.execute("SELECT local_image_path FROM cards").fetchone()[0] is None


# ------------------------------ image pruning ------------------------------
def test_prune_orphaned_keeps_tag_listed_and_dry_run(conn, add_card, tmp_path, monkeypatch):
    import sync_images

    monkeypatch.setattr(sync_images, "IMAGE_CACHE_DIR", str(tmp_path))
    listed = add_card("Listed")
    plain = add_card("Plain")
    _tag(conn, "mana_tags", "Listed", "Fast")
    for sid in (listed, plain):
        (tmp_path / f"{sid}.jpg").write_bytes(b"x")

    would = sync_images.prune_orphaned(conn, verbose=False, dry_run=True)
    assert would == [(plain, "Plain")]
    assert (tmp_path / f"{plain}.jpg").exists()  # dry run deletes nothing

    removed = sync_images.prune_orphaned(conn, verbose=False)
    assert removed == [(plain, "Plain")]
    assert not (tmp_path / f"{plain}.jpg").exists()
    assert (tmp_path / f"{listed}.jpg").exists()


# ------------------------------ CSV export ------------------------------
def test_export_round_trips_through_migrate_loader(conn, tmp_path, monkeypatch):
    import migrate

    _tag(conn, "game_changer_tags", "Ad Nauseam", "Combo")
    _tag(conn, "game_changer_tags", "Rhystic Study", "Value")
    _tag(conn, "mana_tags", "Sol Ring", "Fast")
    counts = writes.export_reference_lists(conn, str(tmp_path))
    assert counts == {"game_changers.csv": 2, "mana_tags.csv": 1}

    monkeypatch.setattr(migrate, "DATA_DIR", str(tmp_path))
    stub = types.SimpleNamespace(conn=conn, log=lambda *_: None)
    conn.execute("DELETE FROM game_changer_tags")
    conn.execute("DELETE FROM mana_tags")
    migrate.Migrator.load_static_reference_lists(stub)
    assert writes.get_game_changer_tags_map(conn) == {"Ad Nauseam": ["Combo"], "Rhystic Study": ["Value"]}
    assert writes.get_mana_tags_map(conn) == {"Sol Ring": ["Fast"]}


# ------------------------------ page (AppTest) ------------------------------
def test_card_database_page_lists_unlinked_names_and_cleans_up(tmp_path, monkeypatch):
    import sqlite3

    import streamlit as st
    from streamlit.testing.v1 import AppTest

    from dashboard_lib import db

    path = str(tmp_path / "mtg_collection.db")
    c = sqlite3.connect(path)
    with open(os.path.join(PROJECT_ROOT, "schema.sql"), encoding="utf-8") as f:
        c.executescript(f.read())
    c.execute("INSERT INTO cards (scryfall_id, name, set_code, collector_number) VALUES ('s1','Stray','x','1')")
    c.execute("INSERT INTO game_changer_tags (card_name, tag) VALUES ('Ghost', 'Combo')")
    c.execute("INSERT INTO mana_tags (card_name, tag) VALUES ('Ghost Rock', 'Fast')")
    c.commit()
    c.close()
    monkeypatch.setattr(db, "DB_PATH", path)
    st.cache_resource.clear()
    st.cache_data.clear()
    try:
        at = AppTest.from_file(os.path.join(PROJECT_ROOT, "pages", "6_Card_Database.py"), default_timeout=30)
        at.run()
        assert not at.exception
        # Each list tab counts its one tagged-but-cardless name.
        assert sum("1 name(s) on this list · 1 with no card data" in c.value for c in at.caption) == 2
        # Clean up: the stray card is listed; delete needs the confirm box.
        assert at.button(key="carddb_clean_unreferenced_cards_btn").disabled
        at.checkbox(key="carddb_clean_unreferenced_cards_ok").set_value(True).run()
        at.button(key="carddb_clean_unreferenced_cards_btn").click().run()
        assert not at.exception
        assert sqlite3.connect(path).execute("SELECT COUNT(*) FROM cards").fetchone()[0] == 0
        # Tag lists untouched by the cleanup.
        assert sqlite3.connect(path).execute("SELECT COUNT(*) FROM game_changer_tags").fetchone()[0] == 1
    finally:
        st.cache_resource.clear()
        st.cache_data.clear()
