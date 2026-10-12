"""Phase 4 of UI_ACTION_PLAN.md (speed): cached images served by URL from
static/ instead of base64 in the page, the one-time image_cache/ ->
static/image_cache/ move, and the lazily built export expanders.

Pure tests point formatting.STATIC_DIR at a temp folder; AppTest cases
reuse test_pages_smoke's temp-database fixture (each on its own DB copy,
since they set image paths the other smoke tests don't expect). Nothing
here touches the real static/ folder or mtg_collection.db.
"""
import os
import shutil
import sqlite3
import sys

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from dashboard_lib import card_view as cv
from dashboard_lib import formatting as fmt

from test_pages_smoke import PAGES_DIR, PROJECT_ROOT, smoke_db_path  # noqa: F401  (fixture)

sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts"))
from move_image_cache import move_legacy_image_cache  # noqa: E402

# Smallest valid PNG/JPEG-ish payloads — the code never decodes them.
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
JPG_BYTES = b"\xff\xd8\xff" + b"\x00" * 16


@pytest.fixture
def static_dir(tmp_path, monkeypatch):
    """A temp static/ with one cached card image and one deck cover."""
    root = tmp_path / "static"
    (root / "image_cache" / "deck_covers").mkdir(parents=True)
    (root / "image_cache" / "sol-ring-cmd-1.jpg").write_bytes(JPG_BYTES)
    (root / "image_cache" / "deck_covers" / "1.png").write_bytes(PNG_BYTES)
    monkeypatch.setattr(fmt, "STATIC_DIR", str(root))
    return root


# ------------------------------------------------------------------
# formatting.local_image_url / static_relative_path
# ------------------------------------------------------------------
def test_local_image_url_is_a_static_url(static_dir):
    url = fmt.local_image_url("image_cache/sol-ring-cmd-1.jpg")
    assert url.startswith("app/static/image_cache/sol-ring-cmd-1.jpg?v=")


def test_local_image_url_accepts_backslash_paths(static_dir):
    # sync_images.py used os.path.join before Phase 4, so Windows rows may
    # hold backslashes; the URL must still use forward slashes.
    url = fmt.local_image_url(os.path.join("image_cache", "deck_covers", "1.png"))
    assert url.startswith("app/static/image_cache/deck_covers/1.png?v=")


@pytest.mark.parametrize("value", [None, float("nan"), "", "image_cache/missing.jpg"])
def test_local_image_url_none_when_not_on_disk(static_dir, value):
    assert fmt.local_image_url(value) is None


def test_static_relative_path_uses_forward_slashes(static_dir):
    abs_path = os.path.join(str(static_dir), "image_cache", "deck_covers", "1.png")
    assert fmt.static_relative_path(abs_path) == "image_cache/deck_covers/1.png"


# ------------------------------------------------------------------
# card_view.card_image_src / deck_image_src
# ------------------------------------------------------------------
def test_card_image_src_url_on_screen_data_uri_when_embedded(static_dir):
    row = {"local_image_path": "image_cache/sol-ring-cmd-1.jpg", "image_uri": "https://x/sol.jpg"}
    assert cv.card_image_src(row).startswith("app/static/")
    assert cv.card_image_src(row, embed=True).startswith("data:image/jpeg;base64,")


def test_card_image_src_falls_back_to_remote(static_dir):
    row = {"local_image_path": "image_cache/missing.jpg", "image_uri": "https://x/sol.jpg"}
    assert cv.card_image_src(row) == "https://x/sol.jpg"
    assert cv.card_image_src(row, embed=True) == "https://x/sol.jpg"
    assert cv.card_image_src({"local_image_path": None, "image_uri": None}) is None


def test_deck_image_src_prefers_cover_then_commander_then_remote(static_dir):
    row = {
        "cover_image_path": "image_cache/deck_covers/1.png",
        "commander_local_image_path": "image_cache/sol-ring-cmd-1.jpg",
        "commander_image_uri": "https://x/cmd.jpg",
    }
    assert "deck_covers/1.png" in cv.deck_image_src(row)
    assert cv.deck_image_src(row, embed=True).startswith("data:image/png;base64,")
    row["cover_image_path"] = None
    assert "sol-ring-cmd-1.jpg" in cv.deck_image_src(row)
    row["commander_local_image_path"] = None
    assert cv.deck_image_src(row) == "https://x/cmd.jpg"


# ------------------------------------------------------------------
# scripts/move_image_cache.py
# ------------------------------------------------------------------
def test_move_legacy_image_cache_moves_nested_files_and_is_idempotent(tmp_path):
    legacy, new = tmp_path / "image_cache", tmp_path / "static" / "image_cache"
    (legacy / "deck_covers").mkdir(parents=True)
    (legacy / "a.jpg").write_bytes(JPG_BYTES)
    (legacy / "deck_covers" / "3.png").write_bytes(PNG_BYTES)

    assert move_legacy_image_cache(str(legacy), str(new)) == (2, 0)
    assert (new / "a.jpg").read_bytes() == JPG_BYTES
    assert (new / "deck_covers" / "3.png").read_bytes() == PNG_BYTES
    assert not legacy.exists()
    assert move_legacy_image_cache(str(legacy), str(new)) == (0, 0)


def test_move_legacy_image_cache_keeps_old_copy_when_destination_exists(tmp_path):
    legacy, new = tmp_path / "image_cache", tmp_path / "static" / "image_cache"
    legacy.mkdir()
    new.mkdir(parents=True)
    (legacy / "a.jpg").write_bytes(b"old")
    (new / "a.jpg").write_bytes(b"new")

    assert move_legacy_image_cache(str(legacy), str(new)) == (0, 1)
    assert (new / "a.jpg").read_bytes() == b"new"
    assert (legacy / "a.jpg").read_bytes() == b"old"


# ------------------------------------------------------------------
# Pages (AppTest)
# ------------------------------------------------------------------
@pytest.fixture
def image_db(smoke_db_path, tmp_path, monkeypatch, static_dir):  # noqa: F811
    """Own DB copy where Sol Ring has a cached image and deck 1 a cover."""
    from dashboard_lib import db

    path = str(tmp_path / "mtg_collection.db")
    shutil.copy(smoke_db_path, path)
    conn = sqlite3.connect(path)
    conn.execute(
        "UPDATE cards SET local_image_path = 'image_cache/sol-ring-cmd-1.jpg'"
        " WHERE scryfall_id = 'sol-ring-cmd-1'"
    )
    conn.execute("UPDATE decks SET cover_image_path = 'image_cache/deck_covers/1.png' WHERE deck_id = 1")
    conn.commit()
    conn.close()
    monkeypatch.setattr(db, "DB_PATH", path)
    st.cache_resource.clear()
    st.cache_data.clear()
    yield path
    st.cache_resource.clear()
    st.cache_data.clear()


def test_collection_grid_links_images_instead_of_embedding(image_db):
    at = AppTest.from_file(os.path.join(PAGES_DIR, "1_Collection.py"), default_timeout=15)
    at.run()
    at.radio(key="collection_view").set_value("Image Grid").run()
    assert not at.exception
    html = " ".join(m.value for m in at.markdown)
    assert "app/static/image_cache/sol-ring-cmd-1.jpg" in html
    assert "base64" not in html


def test_collection_export_is_built_only_when_open(image_db):
    at = AppTest.from_file(os.path.join(PAGES_DIR, "1_Collection.py"), default_timeout=15)
    at.run()
    assert "collection_moxfield_scope" not in [r.key for r in at.radio]
    at.session_state["collection_export_open"] = True
    at.run()
    assert not at.exception
    assert "collection_moxfield_scope" in [r.key for r in at.radio]


def test_deck_export_is_lazy_and_sheet_still_embeds_art(image_db, monkeypatch):
    from dashboard_lib import deck_printout

    seen = {}
    real_build = deck_printout.build_deck_printout_html

    def spy(meta, stats, value, main_df, image_src, *args, **kwargs):
        seen["image_src"] = image_src
        return real_build(meta, stats, value, main_df, image_src, *args, **kwargs)

    monkeypatch.setattr(deck_printout, "build_deck_printout_html", spy)

    at = AppTest.from_file(os.path.join(PAGES_DIR, "2_Decks.py"), default_timeout=15)
    at.session_state["current_deck_id"] = 1
    at.run()
    assert not at.exception
    assert "image_src" not in seen  # collapsed: nothing built
    # The on-screen header links the cover by URL.
    assert any("app/static/image_cache/deck_covers/1.png" in m.value for m in at.markdown)

    at.session_state["deck_export_open"] = True
    at.run()
    assert not at.exception
    # The downloaded sheet must work outside the app, so its art is a data: URI.
    assert seen["image_src"].startswith("data:image/png;base64,")
