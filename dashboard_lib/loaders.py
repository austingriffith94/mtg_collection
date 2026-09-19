"""
Streamlit-cached loaders. Thin wrappers around dashboard_lib.queries that
add st.cache_data + the shared derived columns (card_type / color_display /
scryfall_url / is_land) so every page gets the same enriched shape for free.

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
def load_decks_df(_conn):
    return q.list_decks(_conn)


@st.cache_data(show_spinner="Loading deck list…")
def load_decks_with_covers(_conn):
    return q.list_decks_with_covers(_conn)


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
def load_in_deck_sleeved_count(_conn, deck_id, deck_name):
    return q.in_deck_sleeved_count(_conn, deck_id, deck_name)


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


# ------------------------------------------------------------------
# Phase 3 additions
# ------------------------------------------------------------------
@st.cache_data(show_spinner=False)
def load_cards_by_name(_conn, names):
    """names must be passed as a tuple (hashable, so st.cache_data can key
    on it) — converted to a list here since q.cards_by_name just needs to
    iterate it for the SQL IN (...) clause."""
    return q.cards_by_name(_conn, list(names))


@st.cache_data(show_spinner=False)
def load_tag_types(_conn):
    return w.list_tag_types(_conn)


@st.cache_data(show_spinner=False)
def load_card_tags_by_type(_conn, deck_id, tag_type):
    return w.get_card_tags_by_type(_conn, deck_id, tag_type)


# ------------------------------------------------------------------
# Phase 2 additions
# ------------------------------------------------------------------
@st.cache_data(show_spinner=False)
def load_deck_price_top10(_conn, deck_id):
    return q.deck_price_top10(_conn, deck_id)


@st.cache_data(show_spinner=False)
def load_theme_catalog(_conn):
    return w.list_theme_catalog(_conn)


@st.cache_data(show_spinner=False)
def load_game_changer_categories(_conn):
    return w.list_game_changer_categories(_conn)


@st.cache_data(show_spinner="Loading Game Changers…")
def load_game_changers_overview(_conn):
    return q.game_changers_overview(_conn)


@st.cache_data(show_spinner=False)
def load_mana_tag_catalog(_conn):
    return w.list_mana_tag_catalog(_conn)


@st.cache_data(show_spinner=False)
def load_location_catalog(_conn):
    return w.list_location_catalog(_conn)


@st.cache_data(show_spinner=False)
def load_location_options(_conn):
    return q.location_options(_conn)


@st.cache_data(show_spinner="Loading mana tags…")
def load_mana_tags_overview(_conn):
    return q.mana_tags_overview(_conn)


@st.cache_data(show_spinner="Loading game history…")
def load_games_list(_conn):
    return q.games_list(_conn)


@st.cache_data(show_spinner=False)
def load_deck_win_rates(_conn):
    return q.deck_win_rates(_conn)


@st.cache_data(show_spinner=False)
def load_player_win_rates(_conn):
    return q.player_win_rates(_conn)


# ------------------------------------------------------------------
# Prompt Pass 7 additions (Commander Game Tracking: edit, autofill,
# ELO/head-to-head). Note game_detail() is NOT wrapped here — see its
# own docstring in queries.py for why; the edit form calls it directly.
# ------------------------------------------------------------------
@st.cache_data(show_spinner=False)
def load_known_player_names(_conn):
    return q.distinct_player_names(_conn)


@st.cache_data(show_spinner=False)
def load_known_untracked_deck_names(_conn):
    return q.distinct_untracked_deck_names(_conn)


@st.cache_data(show_spinner=False)
def load_player_elo_ratings(_conn):
    return q.player_elo_ratings(_conn)


@st.cache_data(show_spinner=False)
def load_player_head_to_head(_conn):
    return q.player_head_to_head_matrix(_conn)


def invalidate_reference_caches():
    """Clear caches for the name-keyed / global reference data touched by
    the Editor's Themes, Game Changers, and Mana Tags (Prompt Pass 12)
    management UIs (catalogs, category/tag assignments) — these aren't
    deck- or collection-scoped, so they don't belong in the other two
    invalidate_* functions."""
    load_theme_catalog.clear()
    load_game_changer_categories.clear()
    load_game_changers_overview.clear()
    load_mana_tag_catalog.clear()
    load_mana_tags_overview.clear()
    load_deck_mana_tag_summary.clear()  # Decks page's "Optimized mana" panel
    load_collection_df.clear()  # GC category can show in Collection later
    load_deck_cards_df.clear()
    load_deck_library_df.clear()
    load_deck_price_top10.clear()
    load_deck_game_changers.clear()
    load_location_catalog.clear()   # Prompt Pass 13
    load_location_options.clear()


def invalidate_game_tracking_caches():
    """Clear caches after logging, editing, or deleting a Commander
    game (Prompt Pass 7 added edit, plus the ELO/head-to-head/autofill
    caches, all of which shift whenever the game log changes)."""
    load_games_list.clear()
    load_deck_win_rates.clear()
    load_player_win_rates.clear()
    load_deck_stats.clear()
    load_dashboard_summary.clear()
    load_known_player_names.clear()
    load_known_untracked_deck_names.clear()
    load_player_elo_ratings.clear()
    load_player_head_to_head.clear()


def invalidate_deck_caches(deck_id=None):
    """Clear every cached read a deck-scoped write (card tags, deck
    metadata, mainboard, maybeboard, themes, create/rename/delete) could
    have changed — including the deck list itself, since a rename/create/
    delete changes what every page's deck picker shows. deck_id is
    accepted for readability at call sites but unused: st.cache_data.clear()
    clears ALL cached calls of that function (every deck, every param
    combo) — there's no per-argument clear in the public API, and
    re-running the whole app is cheap enough for a personal-scale dataset
    that fully clearing is the simplest correct option."""
    load_decks_df.clear()
    load_decks_with_covers.clear()
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
    load_card_tags_by_type.clear()
    load_in_deck_sleeved_count.clear()
    load_dashboard_summary.clear()
    load_deck_price_top10.clear()
    load_deck_win_rates.clear()
    load_game_changers_overview.clear()
    # A deleted deck's game_participants rows detach to deck_id=NULL
    # (writes.delete_deck()) rather than disappearing, so its name can
    # now show up in the "known opponent deck" autofill list too
    # (Prompt Pass 7).
    load_known_untracked_deck_names.clear()
    # A create/rename/delete changes the deck-name half of the Location
    # dropdown's option set (Prompt Pass 13).
    load_location_options.clear()


def invalidate_collection_caches():
    """Clear cached collection reads after an add/edit/delete in the
    Editor's Collection tab. Also clears the sleeved-in-deck count and
    home-page summary, since both derive from collection contents."""
    load_collection_df.clear()
    load_collection_summary.clear()
    load_in_deck_sleeved_count.clear()
    load_dashboard_summary.clear()
    load_game_changers_overview.clear()  # "owned qty" there is collection-derived
