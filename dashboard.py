"""
MTG Collection Dashboard — main entry point and page router.

Launch via run.py ("1) Launch dashboard"), or directly with:
    streamlit run dashboard.py

This file only builds the sidebar navigation and runs the chosen page
(Phase 1 of UI_ACTION_PLAN.md). The page list, its sections (Browse /
Plan / Edit / Play), titles and URLs live in dashboard_lib/nav.py; the
Home page itself is pages/0_Home.py. Because st.navigation() is used,
Streamlit no longer auto-discovers pages/ — a page only appears if it's
in nav.PAGES.

Each page still calls cv.setup_page() (st.set_page_config) itself, which
sets its own browser-tab title; that's allowed under st.navigation.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import streamlit as st

from dashboard_lib import db, nav

pg = st.navigation(nav.build_navigation())
pg.run()

# Sidebar footer. Drawn after pg.run() so it sits below the page's own
# sidebar widgets; skipped on a page that st.stop()s (e.g. no database yet).
st.sidebar.caption(f"Reading from `{os.path.basename(db.DB_PATH)}`.")
