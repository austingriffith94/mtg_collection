"""
Offline test for Prompt Pass 12 changes: no Streamlit, no live network.

Prompt Pass 12 ("prompt5.txt" — mana-source color mapping + Optimized
Mana Flags management):

  1. dashboard_lib/formatting.py — new MANA_SOURCE_COLOR_HEX dict for the
     Land Probability page's "Weighted mana-source availability" graph,
     aligned with the Decks page's existing WUBRG palette
     (MANA_CURVE_COLOR_HEX) instead of Streamlit's auto-assigned default
     categorical colors, plus a new dedicated color for the "Rocks/Dorks"
     (nonland mana source) category that doesn't overlap W/U/B/R/G.
     pages/3_Land_Probability.py now passes `color=` into the chart's
     st.bar_chart() call built from this mapping.
  2. mana_tags (the "optimized mana flags" table) had NO in-dashboard
     editor at all before this pass (CSV-only, per PROJECT_STATE.md and
     README.md's own "Known open items"). A new `mana_tag_catalog` table
     (schema.sql + queries.ensure_schema() additive upgrade, seeded from
     existing distinct mana_tags.tag values) backs a new Editor "Mana
     Tags" tab, mirroring the existing Game Changers tab's catalog-
     management + bulk data_editor pattern, plus a "tag a card" search
     section (mana_tags has no Scryfall-derived flag the way
     game_changer_tags does, so nothing seeds a first-ever tag without a
     dedicated search-and-add path).

Covers:
  A. formatting.MANA_SOURCE_COLOR_HEX — every probability.MANA_SOURCE_
     CATEGORIES key present; W/U/B/R/G pulled from MANA_CURVE_COLOR_HEX
     (single source of truth, no drift); Colorless/Any Color reuse that
     same dict's Colorless/Multi-color; Rocks/Dorks is a real hex color
     distinct from every WUBRG hex.
  B. Source-text check confirming pages/3_Land_Probability.py's weighted
     graph passes `color=` built from MANA_SOURCE_COLOR_HEX into
     st.bar_chart().
  C. schema.sql — a fresh database already has mana_tag_catalog (empty,
     no seed rows needed on a from-scratch build since mana_tags starts
     empty too).
  D. queries.ensure_schema() upgrade path — a database missing
     mana_tag_catalog (simulating a pre-Prompt-Pass-12 database) gets it
     created AND seeded from whatever distinct mana_tags.tag values
     already existed, on the next connect.
  E. writes.py catalog CRUD (list/add/remove_mana_tag_*) and tag
     assignment (get_mana_tags_map/set_mana_tags/bulk_set_mana_tags,
     including the "new tag auto-registers in the catalog" behavor and
     the changed-count return value).
  F. queries.mana_tags_overview() against a real scratch schema.sql
     database — only tagged cards appear, tags GROUP_CONCAT correctly,
     owned/deck-count merge correctly, empty-database case returns an
     empty DataFrame without erroring.
  G. Source-text checks confirming the Editor page actually wires in the
     new "Mana Tags" tab (tab list, tab body, save buttons) rather than
     just having the backing functions exist in isolation.

Run from anywhere:
    python scripts/_test_prompt_pass12_offline.py
"""
import os
import sqlite3
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from dashboard_lib import formatting as fmt
from dashboard_lib import probability as prob
from dashboard_lib import queries as q
from dashboard_lib import writes as w


def check(label, cond):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        raise SystemExit(f"Test failed: {label}")


def build_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    with open(os.path.join(PROJECT_ROOT, "schema.sql")) as f:
        conn.executescript(f.read())
    return conn


def main():
    # ------------------------------------------------------------------
    # A. formatting.MANA_SOURCE_COLOR_HEX
    # ------------------------------------------------------------------
    for cat in prob.MANA_SOURCE_CATEGORIES:
        check(f"MANA_SOURCE_COLOR_HEX has an entry for '{cat}'", cat in fmt.MANA_SOURCE_COLOR_HEX)

    for letter in fmt.WUBRG_ORDER:
        check(
            f"MANA_SOURCE_COLOR_HEX['{letter}'] matches the Decks page's MANA_CURVE_COLOR_HEX exactly",
            fmt.MANA_SOURCE_COLOR_HEX[letter] == fmt.MANA_CURVE_COLOR_HEX[letter],
        )

    check(
        "Colorless reuses MANA_CURVE_COLOR_HEX's Colorless grey",
        fmt.MANA_SOURCE_COLOR_HEX["Colorless"] == fmt.MANA_CURVE_COLOR_HEX["Colorless"],
    )
    check(
        "Any Color reuses MANA_CURVE_COLOR_HEX's Multi-color goldenrod",
        fmt.MANA_SOURCE_COLOR_HEX["Any Color"] == fmt.MANA_CURVE_COLOR_HEX["Multi-color"],
    )

    wubrg_hexes = {fmt.MANA_SOURCE_COLOR_HEX[l].lower() for l in fmt.WUBRG_ORDER}
    rocks_hex = fmt.MANA_SOURCE_COLOR_HEX["Rocks/Dorks"].lower()
    check("Rocks/Dorks is a real hex color string", rocks_hex.startswith("#") and len(rocks_hex) == 7)
    check(
        "Rocks/Dorks color does not overlap White/Blue/Black/Red/Green",
        rocks_hex not in wubrg_hexes,
    )

    # ------------------------------------------------------------------
    # B. Source-text check — pages/3_Land_Probability.py chart wiring
    # ------------------------------------------------------------------
    landprob_path = os.path.join(PROJECT_ROOT, "pages", "3_Land_Probability.py")
    with open(landprob_path, encoding="utf-8") as f:
        landprob_text = f.read()
    check(
        "Land Probability page builds chart_colors from fmt.MANA_SOURCE_COLOR_HEX",
        "fmt.MANA_SOURCE_COLOR_HEX[c] for c in nonzero_cols" in landprob_text,
    )
    check(
        "Land Probability page's weighted-graph st.bar_chart() call passes color=chart_colors",
        "st.bar_chart(weighted_df[nonzero_cols], color=chart_colors)" in landprob_text,
    )

    # ------------------------------------------------------------------
    # C. schema.sql — fresh database already has mana_tag_catalog
    # ------------------------------------------------------------------
    conn = build_db()
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    check("a fresh schema.sql database already has mana_tag_catalog", "mana_tag_catalog" in tables)
    check(
        "mana_tag_catalog starts empty on a fresh install (mana_tags itself starts empty)",
        conn.execute("SELECT COUNT(*) FROM mana_tag_catalog").fetchone()[0] == 0,
    )
    conn.close()

    # ------------------------------------------------------------------
    # D. queries.ensure_schema() upgrade path — simulated pre-Pass-12 DB
    # ------------------------------------------------------------------
    conn = build_db()
    conn.execute("DROP TABLE mana_tag_catalog")
    conn.execute("INSERT INTO cards (scryfall_id, name, set_code, collector_number) VALUES ('c1','Sol Ring','TST','1')")
    conn.execute("INSERT INTO cards (scryfall_id, name, set_code, collector_number) VALUES ('c2','Cabal Coffers','TST','2')")
    conn.execute("INSERT INTO mana_tags (card_name, tag) VALUES ('Sol Ring', 'Fast')")
    conn.execute("INSERT INTO mana_tags (card_name, tag) VALUES ('Cabal Coffers', 'Mana Doubler')")
    conn.commit()
    tables_before = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    check("mana_tag_catalog was actually dropped for this simulated-old-DB test", "mana_tag_catalog" not in tables_before)

    q.ensure_schema(conn)

    tables_after = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    check("ensure_schema() recreates mana_tag_catalog on an upgraded database", "mana_tag_catalog" in tables_after)
    seeded = {r[0] for r in conn.execute("SELECT tag FROM mana_tag_catalog").fetchall()}
    check(
        "ensure_schema() seeds mana_tag_catalog from the existing mana_tags' distinct tags",
        seeded == {"Fast", "Mana Doubler"},
    )

    # Re-running ensure_schema() is a no-op (table already exists) — confirm
    # it doesn't error and doesn't duplicate/clear anything.
    q.ensure_schema(conn)
    check(
        "re-running ensure_schema() doesn't touch an already-upgraded mana_tag_catalog",
        {r[0] for r in conn.execute("SELECT tag FROM mana_tag_catalog").fetchall()} == {"Fast", "Mana Doubler"},
    )
    conn.close()

    # ------------------------------------------------------------------
    # E. writes.py — catalog CRUD + tag assignment
    # ------------------------------------------------------------------
    conn = build_db()
    conn.execute("INSERT INTO cards (scryfall_id, name, set_code, collector_number) VALUES ('c1','Sol Ring','TST','1')")
    conn.execute("INSERT INTO cards (scryfall_id, name, set_code, collector_number) VALUES ('c2','Dark Ritual','TST','2')")
    conn.commit()

    check("catalog starts empty", w.list_mana_tag_catalog(conn) == [])
    w.add_mana_tag_to_catalog(conn, "Fast")
    w.add_mana_tag_to_catalog(conn, "Ritual")
    check("catalog list is alphabetical, case-insensitive", w.list_mana_tag_catalog(conn) == ["Fast", "Ritual"])
    w.add_mana_tag_to_catalog(conn, "Fast")  # duplicate, should be a no-op
    check("adding a duplicate catalog entry doesn't create a second row", w.list_mana_tag_catalog(conn) == ["Fast", "Ritual"])
    w.add_mana_tag_to_catalog(conn, "   ")  # blank, should be ignored
    check("adding a blank/whitespace-only catalog entry is ignored", w.list_mana_tag_catalog(conn) == ["Fast", "Ritual"])
    w.remove_mana_tag_from_catalog(conn, "Ritual")
    check("removing a catalog entry works", w.list_mana_tag_catalog(conn) == ["Fast"])

    check("no card has any mana tags yet", w.get_mana_tags_map(conn) == {})
    w.set_mana_tags(conn, "Sol Ring", ["Fast", "Moxen"])
    check(
        "set_mana_tags() assigns exactly the given tags to the named card",
        set(w.get_mana_tags_map(conn)["Sol Ring"]) == {"Fast", "Moxen"},
    )
    check(
        "set_mana_tags() auto-registers a brand-new tag into the master catalog",
        "Moxen" in w.list_mana_tag_catalog(conn),
    )
    w.set_mana_tags(conn, "Sol Ring", ["Fast"])
    check("set_mana_tags() replaces (not merges) — Moxen removed from Sol Ring", w.get_mana_tags_map(conn)["Sol Ring"] == ["Fast"])
    w.set_mana_tags(conn, "Sol Ring", [])
    check("set_mana_tags([]) clears every tag on the card", "Sol Ring" not in w.get_mana_tags_map(conn))

    changed = w.bulk_set_mana_tags(conn, {
        "Sol Ring": ["Fast"],
        "Dark Ritual": ["Ritual"],
    })
    check("bulk_set_mana_tags() returns the count of cards that actually changed", changed == 2)
    changed_again = w.bulk_set_mana_tags(conn, {
        "Sol Ring": ["Fast"],       # unchanged
        "Dark Ritual": ["Ritual", "Combo"],  # actually changed
    })
    check("bulk_set_mana_tags() re-run only counts cards whose tag set actually changed", changed_again == 1)
    conn.close()

    # ------------------------------------------------------------------
    # F. queries.mana_tags_overview()
    # ------------------------------------------------------------------
    conn = build_db()
    check("mana_tags_overview() on an empty database returns an empty DataFrame", q.mana_tags_overview(conn).empty)

    conn.execute("INSERT INTO cards (scryfall_id, name, set_code, collector_number) VALUES ('c1','Sol Ring','TST','1')")
    conn.execute("INSERT INTO cards (scryfall_id, name, set_code, collector_number) VALUES ('c2','Dark Ritual','TST','2')")
    conn.execute("INSERT INTO cards (scryfall_id, name, set_code, collector_number) VALUES ('c3','Forest','TST','3')")
    conn.execute("INSERT INTO decks (deck_id, name) VALUES (1, 'Test Deck')")
    conn.execute("INSERT INTO deck_cards (deck_id, scryfall_id, quantity) VALUES (1, 'c1', 1)")
    conn.execute("INSERT INTO collection (scryfall_id, quantity, location) VALUES ('c1', 2, 'Test Deck')")
    conn.execute("INSERT INTO mana_tags (card_name, tag) VALUES ('Sol Ring', 'Fast')")
    conn.execute("INSERT INTO mana_tags (card_name, tag) VALUES ('Sol Ring', 'Moxen')")
    conn.execute("INSERT INTO mana_tags (card_name, tag) VALUES ('Dark Ritual', 'Ritual')")
    conn.commit()

    overview = q.mana_tags_overview(conn)
    check("mana_tags_overview() only includes cards with at least one mana tag (Forest excluded)", set(overview["name"]) == {"Sol Ring", "Dark Ritual"})
    sol_row = overview[overview["name"] == "Sol Ring"].iloc[0]
    check("Sol Ring's tags are comma-concatenated (order-independent)", set(sol_row["tags"].split(",")) == {"Fast", "Moxen"})
    check("Sol Ring's owned_qty reflects its collection row", int(sol_row["owned_qty"]) == 2)
    check("Sol Ring's deck_count reflects its one deck_cards row", int(sol_row["deck_count"]) == 1)
    ritual_row = overview[overview["name"] == "Dark Ritual"].iloc[0]
    check("Dark Ritual (untracked, not in any deck) shows owned_qty=0", int(ritual_row["owned_qty"]) == 0)
    check("Dark Ritual shows deck_count=0", int(ritual_row["deck_count"]) == 0)
    conn.close()

    # ------------------------------------------------------------------
    # G. Source-text checks — Editor page wiring
    # ------------------------------------------------------------------
    editor_path = os.path.join(PROJECT_ROOT, "pages", "4_Editor.py")
    with open(editor_path, encoding="utf-8") as f:
        editor_text = f.read()

    check("Editor's tab tuple now includes tab_manatags", "tab_manatags" in editor_text and "st.tabs(" in editor_text)
    check("Editor's tab labels now include 'Mana Tags'", '"Mana Tags"' in editor_text)
    check("Editor has a 'with tab_manatags:' block", "with tab_manatags:" in editor_text)
    check("Mana Tags tab manages the master tag catalog", "writes.add_mana_tag_to_catalog(conn" in editor_text and "writes.remove_mana_tag_from_catalog(conn" in editor_text)
    check("Mana Tags tab has a search-and-tag-a-card flow", "q.search_card_names(conn, mt_search)" in editor_text and "writes.set_mana_tags(conn" in editor_text)
    check("Mana Tags tab has a bulk data_editor save path", "writes.bulk_set_mana_tags(conn, mt_edits)" in editor_text)
    check("Mana Tags tab reads from loaders.load_mana_tags_overview", "loaders.load_mana_tags_overview(conn)" in editor_text)

    loaders_path = os.path.join(PROJECT_ROOT, "dashboard_lib", "loaders.py")
    with open(loaders_path, encoding="utf-8") as f:
        loaders_text = f.read()
    check(
        "invalidate_reference_caches() clears the new Mana Tags caches",
        "load_mana_tag_catalog.clear()" in loaders_text
        and "load_mana_tags_overview.clear()" in loaders_text
        and "load_deck_mana_tag_summary.clear()" in loaders_text,
    )

    print("\nAll Prompt Pass 12 offline checks passed.")


if __name__ == "__main__":
    main()
