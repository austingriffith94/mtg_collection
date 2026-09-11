"""
Streamlit-cached loaders. Thin wrappers around dashboard_lib.queries that
add st.cache_data + the shared derived columns (card_type / color_display /
scryfall_url) so every page gets the same enriched shape for free.

Leading-underscore `_conn` parameters are a Streamlit convention: it tells
st.cache_data to key its cache on the OTHER arguments only, not on the
(unhashable) sqlite3.Connection object itself.
"""
import streamlit as st

from . import queries as q
from . import card_view as cv
from . import writes as w


@st.cache_data(show_spinner="Loading collection…")
def load_collection_df(_conn):
    return cv.add_derived_columns(q.collection_dataframe(_conn))


@st.cache_data(show_spinner=False)
def load_collection_summary(_conn):
    return q.collection_summary(_conn)


@st.cache_data(show_spinner=False)
def load_dashboard_summary(_conn):
    return q.dashboard_summary(_conn)


@st.cache_data(show_spinner=False)
def load_decks_df(_conn, include_retired=True):
    return q.list_decks(_conn, include_retired=include_retired)


@st.cache_data(show_spinner="Loading decklist…")
def load_deck_cards_df(_conn, deck_id):
    return cv.add_derived_columns(q.deck_cards_dataframe(_conn, deck_id))


@st.cache_data(show_spinner="Loading shuffled library (mainboard minus commander)…")
def load_deck_library_df(_conn, deck_id):
    return cv.add_derived_columns(q.deck_library_dataframe(_conn, deck_id))


@st.cache_data(show_spinner="Loading maybeboard…")
def load_maybeboard_df(_conn, deck_id):
    return cv.add_derived_columns(q.maybeboard_dataframe(_conn, deck_id))


@st.cache_data(show_spinner=False)
def load_deck_meta(_conn, deck_id):
    return q.deck_meta(_conn, deck_id)


@st.cache_data(show_spinner=False)
def load_deck_stats(_conn, deck_id):
    return q.deck_stats_row(_conn, deck_id)


@st.cache_data(show_spinner=False)
def load_deck_value(_conn, deck_id):
    return q.deck_value_row(_conn, deck_id)


@st.cache_data(show_spinner=False)
def load_in_deck_sleeved_count(_conn, deck_name):
    return q.in_deck_sleeved_count(_conn, deck_name)


@st.cache_data(show_spinner=False)
def load_deck_rank_list(_conn, deck_id, kind):
    return q.deck_rank_list(_conn, deck_id, kind)


@st.cache_data(show_spinner=False)
def load_deck_themes(_conn, deck_id):
    return q.deck_themes_list(_conn, deck_id)


@st.cache_data(show_spinner=False)
def load_deck_mana_tag_summary(_conn, deck_id):
    return q.deck_mana_tag_summary(_conn, deck_id)


@st.cache_data(show_spinner=False)
def load_deck_game_changers(_conn, deck_id):
    return q.deck_game_changers(_conn, deck_id)


@st.cache_data(show_spinner=False)
def load_deck_reserved_list_cards(_conn, deck_id):
    return q.deck_reserved_list_cards(_conn, deck_id)


@st.cache_data(show_spinner=False)
def load_deck_strategy_tag_counts(_conn, deck_id):
    return q.deck_strategy_tag_counts(_conn, deck_id)


@st.cache_data(show_spinner=False)
def load_deck_mana_curve(_conn, deck_id):
    return q.deck_mana_curve(_conn, deck_id)


@st.cache_data(show_spinner=False)
def load_tag_types(_conn):
    return w.list_tag_types(_conn)


@st.cache_data(show_spinner=False)
def load_deck_tags(_conn, deck_id):
    return w.get_deck_tags(_conn, deck_id)


@st.cache_data(show_spinner=False)
def load_card_tags_by_type(_conn, deck_id, tag_type):
    return w.get_card_tags_by_type(_conn, deck_id, tag_type)


def invalidate_deck_caches(deck_id=None):
    """Clear every cached read a deck-scoped write (tags, deck metadata,
    mainboard, maybeboard, themes, retire/reactivate, create/rename) could
    have changed — including the deck list itself, since a rename/create/
    retire changes what every page's deck picker shows. deck_id is
    accepted for readability at call sites but unused: st.cache_data.clear()
    clears ALL cached calls of that function (every deck, every param
    combo) — there's no per-argument clear in the public API, and
    re-running the whole app is cheap enough for a personal-scale dataset
    that fully clearing is the simplest correct option."""
    load_decks_df.clear()
    load_deck_meta.clear()
    load_deck_cards_df.clear()
    load_deck_library_df.clear()
    load_deck_strategy_tag_counts.clear()
    load_deck_mana_curve.clear()
    load_deck_game_changers.clear()
    load_deck_reserved_list_cards.clear()
    load_deck_value.clear()
    load_deck_rank_list.clear()
    load_deck_themes.clear()
    load_tag_types.clear()
    load_deck_tags.clear()
    load_card_tags_by_type.clear()
    load_in_deck_sleeved_count.clear()
    load_dashboard_summary.clear()


def invalidate_collection_caches():
    """Clear cached collection reads after an add/edit/delete in the
    Editor's Collection tab. Also clears the sleeved-in-deck count and
    home-page summary, since both derive from collection contents."""
    load_collection_df.clear()
    load_collection_summary.clear()
    load_in_deck_sleeved_count.clear()
    load_dashboard_summary.clear()
