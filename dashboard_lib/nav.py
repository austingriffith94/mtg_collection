"""
Page registry — the single list of every page in the app (Phase 1 of
UI_ACTION_PLAN.md).

dashboard.py builds its grouped st.navigation() sidebar from PAGES, and the
Home page builds its page-link cards from the same list, so the sidebar and
the Home guide can't drift apart. Adding a page means adding one PageSpec
here; tests/test_nav.py fails if a file in pages/ is missing from it.

Titles are uppercase on purpose: the sidebar's ALL-CAPS look (a Prompt
Pass 8 request) used to come from CSS aimed at Streamlit's internal nav
DOM, which broke easily on upgrades. A plain uppercase title does the same
job with no CSS. Each page's own st.set_page_config(page_title=...) still
sets the browser-tab title.

url_path is explicit for every page. "Decks" must stay "Decks": the Home
page's deck tiles link to `Decks?deck_id=N`
(card_view.render_deck_landing_grid).

DECK_LINKS (Phase 2) are the cross-links shown under the deck header on
each per-deck page. They carry no deck in the URL: the shared
card_view.CURRENT_DECK_KEY session value is what tells the target page
which deck to show.
"""
from dataclasses import dataclass

import streamlit as st
from streamlit.errors import StreamlitPageNotFoundError


@dataclass(frozen=True)
class PageSpec:
    path: str        # relative to the project root (dashboard.py's folder)
    title: str       # sidebar label, uppercase
    icon: str
    url_path: str
    section: str
    blurb: str       # one-line purpose, shown on the Home page's link cards
    default: bool = False


SECTIONS = ["Browse", "Plan", "Edit", "Play"]

PAGES = [
    PageSpec("pages/0_Home.py", "HOME", "🏠", "Home", "Browse",
             "Collection totals, pending work, and a tile for every deck.", default=True),
    PageSpec("pages/1_Collection.py", "COLLECTION", "📦", "Collection", "Browse",
             "Search and filter every card you own; edit lots in place."),
    PageSpec("pages/2_Decks.py", "DECKS", "🃏", "Decks", "Browse",
             "One deck's summary, mana, combos, prices, history and cards."),
    PageSpec("pages/9_Card_Lookup.py", "CARD LOOKUP", "🔍", "Card_Lookup", "Browse",
             "Everything about one card: printings, lots, decks, plans."),
    PageSpec("pages/8_Workbench.py", "WORKBENCH", "🔧", "Workbench", "Plan",
             "Pending shortlist work across every deck, conflicts first."),
    PageSpec("pages/10_Advisor.py", "ADVISOR", "🧠", "Advisor", "Plan",
             "Database-grounded deck analysis and chat with an AI model."),
    PageSpec("pages/3_Land_Probability.py", "LAND & COLOR PROBABILITY", "🎲", "Land_Probability", "Plan",
             "Odds of lands, specific cards, and each color in the early turns."),
    PageSpec("pages/4_Deck_Editor.py", "DECK EDITOR", "✏️", "Deck_Editor", "Edit",
             "Deck metadata, themes, tags, mainboard and shortlist."),
    PageSpec("pages/5_Collection_Editor.py", "COLLECTION EDITOR", "📥", "Collection_Editor", "Edit",
             "Import purchase orders and add card lots by hand."),
    PageSpec("pages/6_Card_Database.py", "CARD DATABASE", "🗄️", "Card_Database", "Edit",
             "Game Changers, mana tags, and Scryfall/Spellbook refreshes."),
    PageSpec("pages/7_Commander_Game_Tracking.py", "COMMANDER GAME TRACKING", "🏆", "Commander_Game_Tracking", "Play",
             "Log games, then see win rates, ELO and head-to-head."),
]


def build_navigation():
    """{section: [st.Page, ...]} in SECTIONS order, ready for st.navigation()."""
    return {
        section: [
            st.Page(p.path, title=p.title, icon=p.icon, url_path=p.url_path, default=p.default)
            for p in PAGES if p.section == section
        ]
        for section in SECTIONS
    }


# (url_path, link label) for every per-deck view, in display order.
DECK_LINKS = [
    ("Decks", "← Decks"),
    ("Deck_Editor", "✏️ Edit deck"),
    ("Land_Probability", "🎲 Probabilities"),
    ("Advisor", "🧠 Ask the Advisor"),
]


def page_path(url_path):
    """The registry file path for a url_path (what st.page_link wants)."""
    return next(p.path for p in PAGES if p.url_path == url_path)


def render_empty_state(missing, url_path=None, link_label=None, container=None):
    """The one "nothing here yet" pattern (UI plan Phase 6): what's
    missing, plus where to add it. `missing` is a full sentence ("No decks
    yet."); when url_path is given it's followed by a page_link to that
    page. Standalone pages (no router) get the sentence alone, same
    fallback as render_deck_links."""
    c = container if container is not None else st
    c.caption(missing)
    if url_path is None:
        return
    try:
        c.page_link(page_path(url_path), label=link_label or "Go there →")
    except StreamlitPageNotFoundError:
        pass


def render_deck_links(current_url_path):
    """One row of st.page_link cross-links to the other per-deck views,
    skipping the page we're on. When a page runs on its own instead of
    under dashboard.py's router, st.page_link can't resolve registry
    paths (only st.navigation knows them), so the row is skipped there."""
    links = [(u, label) for u, label in DECK_LINKS if u != current_url_path]
    cols = st.columns(len(links))
    try:
        for col, (url_path, label) in zip(cols, links):
            col.page_link(page_path(url_path), label=label)
    except StreamlitPageNotFoundError:
        pass
