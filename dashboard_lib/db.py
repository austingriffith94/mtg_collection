"""
Streamlit-facing DB helpers: cached connection, "no DB yet" guard, and a
manual cache-clear used after re-running the migration.
"""
import os
import streamlit as st

from . import queries as q
from . import card_view as cv

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, "mtg_collection.db")
IMAGE_CACHE_DIR = os.path.join(BASE_DIR, "image_cache")


def db_exists():
    return os.path.exists(DB_PATH)


@st.cache_resource(show_spinner=False)
def get_connection():
    return q.get_connection(DB_PATH)


def require_db():
    """Stop the page with a friendly message if mtg_collection.db doesn't
    exist yet — mirrors the check run.py already does before offering to
    launch the dashboard."""
    if not db_exists():
        st.error(
            "No database found yet (`mtg_collection.db` is missing).\n\n"
            "Run the migration first: from a terminal in the project folder, "
            "run `python run.py` and choose **1) Run / refresh database "
            "migration**, then relaunch the dashboard."
        )
        st.stop()


def refresh_data_button(container=None):
    """Sidebar button to drop cached query results (and the cached
    connection) after re-running migrate.py while the app is already
    running — otherwise a rebuilt mtg_collection.db can be masked by a
    stale open file handle / cached data. Also resets every active
    sidebar filter (search text, facet selections, bool toggles,
    price/CMC range, and any bespoke filter a page registered) back to
    its default, on every page, so a refresh always starts from a clean
    slate rather than re-applying selections that may no longer match
    the rebuilt data."""
    c = container or st.sidebar
    if c.button(
        "🔄 Refresh data",
        help="Click after re-running the migration so the dashboard picks up the rebuilt database. Also resets all active sidebar filters.",
    ):
        cv.reset_filters()
        st.cache_data.clear()
        get_connection.clear()
        st.rerun()
