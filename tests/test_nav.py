"""Page registry checks (Phase 1 of UI_ACTION_PLAN.md): dashboard_lib/nav.py
is the single list the sidebar router and the Home page's link cards are
both built from, so it has to match pages/ exactly."""
import os

from dashboard_lib import nav

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_every_registry_file_exists():
    for spec in nav.PAGES:
        assert os.path.isfile(os.path.join(PROJECT_ROOT, spec.path)), spec.path


def test_every_page_file_is_registered():
    on_disk = {
        f"pages/{f}" for f in os.listdir(os.path.join(PROJECT_ROOT, "pages"))
        if f.endswith(".py")
    }
    assert on_disk == {spec.path for spec in nav.PAGES}


def test_decks_url_path_is_decks():
    # Home-page deck tiles link to `Decks?deck_id=N`.
    decks = [p for p in nav.PAGES if p.path == "pages/2_Decks.py"]
    assert len(decks) == 1 and decks[0].url_path == "Decks"


def test_registry_shape():
    assert sum(p.default for p in nav.PAGES) == 1
    assert {p.section for p in nav.PAGES} <= set(nav.SECTIONS)
    url_paths = [p.url_path for p in nav.PAGES]
    assert len(url_paths) == len(set(url_paths))
    assert all(p.title == p.title.upper() for p in nav.PAGES)
