"""
Home page — the app's default page under dashboard.py's st.navigation()
router (Phase 1 of UI_ACTION_PLAN.md; this content used to live in
dashboard.py itself).

Top to bottom:
  - metrics row (decks, printings, lots, games)
  - page guide: one st.page_link card per page, grouped by sidebar section,
    built from dashboard_lib.nav.PAGES so it always matches the sidebar
  - Workbench summary line (pending work across every deck)
  - deck tiles: clicking one opens the Decks page at `Decks?deck_id=N`,
    which pre-selects that deck

Phase 3 adds a collapsed "📊 Compare all decks" expander under the tiles.

Router-only: the st.page_link paths are relative to dashboard.py, so this
file doesn't run on its own the way the other pages do.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import streamlit as st

from dashboard_lib import db, loaders, nav
from dashboard_lib import card_view as cv

cv.setup_page("MTG Collection Dashboard", "🃏")

st.title("🃏 MTG Collection Dashboard")

db.refresh_data_button()
db.require_db()

conn = db.get_connection()
summary = loaders.load_dashboard_summary(conn)

c1, c2, c3, c4 = st.columns(4)
c1.metric("Decks", summary["total_decks"])
c2.metric("Unique printings tracked", summary["unique_printings"])
c3.metric("Collection lots", summary["collection_lots"])
c4.metric("Games logged", summary["games_logged"])

st.divider()

# Page guide: one row of cards per sidebar section. Four columns fits the
# largest section (Browse, minus Home itself) on one row.
for section in nav.SECTIONS:
    specs = [p for p in nav.PAGES if p.section == section and not p.default]
    if not specs:
        continue
    st.markdown(f"**{section}**")
    cols = st.columns(4)
    for col, spec in zip(cols, specs):
        with col.container(border=True):
            st.page_link(spec.path, label=spec.title.title(), icon=spec.icon)
            st.caption(spec.blurb)

st.divider()

st.subheader("Your decks")
st.caption("Click a deck to jump straight to its page.")

comparison = loaders.load_deck_comparison(conn)

# Workbench summary above the grid (Phase 5): pending work visible from the
# landing page rather than three clicks deep. Counts come from the same
# comparison rows as the tile strips, so the two can't disagree.
planned_total = int(comparison["planned"].sum())
decks_with_plans = int((comparison["planned"] > 0).sum())
unlocated = loaders.load_unlocated_lot_summary(conn)
workbench_bits = []
if planned_total:
    workbench_bits.append(f"**{planned_total}** planned change(s) across {decks_with_plans} deck(s)")
if summary["idea_rows"]:
    workbench_bits.append(f"**{summary['idea_rows']}** shortlist idea(s)")
if unlocated["lots"]:
    workbench_bits.append(f"**{unlocated['lots']}** lot(s) with no location")
if workbench_bits:
    st.markdown("🔧 " + " · ".join(workbench_bits))
    st.page_link("pages/8_Workbench.py", label="Open the Workbench", icon="🔧")

decks_with_covers = loaders.load_decks_with_covers(conn)
cv.render_deck_landing_grid(decks_with_covers, status_df=comparison)
