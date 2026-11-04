"""
Goldfish page — play out a deck's first turns alone, by clicking
(docs/GOLDFISH_PLAN.md).

Pick a deck, choose play or draw, mulligan or keep, then click hand cards
to play them and battlefield cards to tap them. The whole game runs in the
browser (dashboard_lib/components/goldfish.js): Python sends the deck once
through goldfish.render_goldfish() and never hears back, so shuffling,
drawing and tapping cause no rerun and nothing is saved. Changing deck or
leaving the page ends the game.

Read-only: this page never writes to the database. The deck picker shares
its selection with the other per-deck pages (card_view.CURRENT_DECK_KEY),
and the header carries cross-links to them.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import streamlit as st

from dashboard_lib import db, goldfish, loaders
from dashboard_lib import card_view as cv, nav

cv.setup_page("Goldfish · MTG Dashboard", "🐟")

db.require_db()
conn = db.get_connection()

st.title("🐟 Goldfish")

st.sidebar.header("Deck")
db.refresh_data_button()

decks_df = loaders.load_decks_df(conn)
deck_id = cv.render_deck_picker(decks_df, key="goldfish_deck_select")
if deck_id is None:
    st.stop()

meta = loaders.load_deck_meta(conn, deck_id)
cards_df = loaders.load_deck_cards_df(conn, deck_id)

st.header(meta.get("representative") or meta.get("name") or "Deck")
nav.render_deck_links("Goldfish")

cv.how_it_works(
    "Choose **on the play** or **on the draw** to draw 7, then **Mulligan** or **Keep**. "
    "Mulligans are London mulligans: after Keep you click one card per mulligan to put on "
    "the bottom, and Commander's free first mulligan is a checkbox (on by default).",
    "Click a card in your hand to play it: a land uses your land drop, a creature or other "
    "permanent goes to the battlefield, and an instant or sorcery goes to the graveyard. A card "
    "with two faces asks which one. Click a card on the battlefield to tap or untap it. "
    "The **⋯** on a card moves it to your hand, graveyard, exile or the top or bottom of the library.",
    "**Next turn** untaps everything, clears summoning sickness, resets the land drop and draws. "
    "**Undo** steps back one action at a time, **New hand** starts over with a fresh 7, "
    "and **Reset** goes back to choosing play or draw.",
    "Rules are shown, not enforced: a second land in a turn warns but is allowed, and "
    "**Mana available** is what your untapped sources could make (`?` for a source whose output "
    "can't be read from its text). You tap your own sources; nothing is paid for you.",
    "The game lives in your browser and is never saved. Changing deck or leaving the page ends it.",
)

if cards_df.empty:
    st.warning("No mainboard cards loaded for this deck yet — nothing to play.")
    st.stop()

deck_size = int(cards_df["quantity"].sum())
if deck_size != 100:
    st.caption(f"This deck has {deck_size} cards in its mainboard (a Commander deck has 100).")

goldfish.render_goldfish(cards_df, meta, deck_id, layer_tags=loaders.load_deck_tag_layers(conn, deck_id))
