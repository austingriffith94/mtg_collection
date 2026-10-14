"""
Card Database page — whole-database, card-name-keyed reference data that
isn't scoped to any one deck: Game Changer categories, optimized-mana
tags, and a Scryfall data refresh across every card already tracked.

The Game Changers and Mana Tags tabs show the WHOLE hand-maintained list
(the tag tables), not just names that happen to have a `cards` row. Each
has ownership / tag / "needs attention" filters, a Delete column, an "add by
name" form, a "fetch card data" button for names with no card row, and an
"Export lists to CSV" backup (migrate.py rebuilds the DB from CSVs and would
otherwise discard these lists). The Card Data tab also has a "Clean up" panel
listing stale rows (unreferenced cards, tags for cards no longer in a deck,
orphaned image files) with a confirm step before anything is deleted.

The Card Data tab also carries the bulk "Check all decks" Commander
Spellbook combo refresh — the per-deck version lives on the Decks page,
where the results are actually read; this is the "I've been editing
decklists, re-check everything" path, sitting next to the Scryfall refresh
for the same reason. See dashboard_lib/spellbook.py.

Split out from the original combined Editor page — deck-level editing
lives in Deck Editor, and collection/inventory management lives in
Collection Editor.

Migration is one-way (CSV -> DB): re-running scripts/migrate.py wipes and
rebuilds the whole database from the CSVs, discarding anything edited
here since the last migration. Use the CSVs + migrate.py for your initial
import, then manage everything here from then on — see the README.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import streamlit as st

from dashboard_lib import db, loaders, writes, refresh, card_resolver, formatting as fmt, queries as q
from dashboard_lib import spellbook
from dashboard_lib import card_view as cv

cv.setup_page("Card Database · MTG Dashboard", "🗄️")

db.require_db()
conn = db.get_connection()

st.title("🗄️ Card Database")
cv.how_it_works(
    "Everything here writes straight to the database — no CSV editing or re-migration needed.",
    "**Game Changers** lists every name on your custom list plus every card Scryfall flags as a "
    "Game Changer — whether or not it's in your collection. *Scryfall says* flags entries that "
    "Scryfall no longer lists. *Owned* sums collection quantity across all printings; *In decks* "
    "counts how many of your decks currently run the card.",
)

db.refresh_data_button()

ORPHAN_LABELS = {
    "unreferenced_cards": "unreferenced card(s)",
    "stale_card_tags": "stale deck tag(s)",
    "orphan_image_files": "orphan image file(s)",
    "missing_image_files": "missing image file(s)",
}

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
OWNERSHIP_FILTERS = ["All", "Owned", "In a deck", "Not owned"]


def _split_labels(raw):
    return [] if pd.isna(raw) or not str(raw).strip() else [x.strip() for x in str(raw).split(",")]


def _render_reference_list(cfg):
    """Shared body of the Game Changers / Mana Tags tabs: the tag table is
    the list, `cards` only decorates it (see queries._tag_list_overview)."""
    pre = cfg["prefix"]
    col_label, tags_col = cfg["noun"], cfg["tags_col"]
    df = cfg["load"](conn)
    unlinked = df.loc[~df["has_card"], "name"].tolist()

    st.caption(
        f"{len(df)} name(s) on this list · {len(unlinked)} with no card data yet "
        f"(no art" + (" or Scryfall status" if cfg["has_flag"] else "") + ")."
    )

    with st.expander("➕ Add a card by name"):
        st.caption(
            "Works for cards that aren't in your database. The name is looked up on Scryfall so "
            "it's spelled the way Scryfall spells it; offline, it's kept exactly as typed."
        )
        add_name = st.text_input("Card name", key=f"{pre}_add_name")
        add_labels = st.text_input(f"{col_label} (comma-separated)", key=f"{pre}_add_labels")
        if st.button("Add to list", key=f"{pre}_add_btn"):
            labels = _split_labels(add_labels)
            if not add_name.strip() or not labels:
                st.warning("Enter a card name and at least one entry.")
            else:
                name, error = card_resolver.canonical_card_name(conn, add_name)
                cfg["set_tags"](conn, name, cfg["get_map"](conn).get(name, []) + labels)
                cv.finish_edit({
                    "message": f"Added '{name}'.",
                    "warnings": ["Couldn't look it up on Scryfall, so it was kept as typed."] if error else [],
                    "reference": True,
                })

    if unlinked:
        if not card_resolver.scryfall_available():
            st.caption("Install the `requests` package to fetch card data for the unlinked names.")
        elif st.button(f"🌐 Fetch card data for {len(unlinked)} unlinked name(s)", key=f"{pre}_fetch_btn",
                       help="Looks each name up on Scryfall (network) so it gets art and a Game Changer "
                            "flag. Only runs when you press this."):
            bar = st.progress(0.0)
            fetched, failed = card_resolver.fetch_missing_names(
                conn, unlinked, progress_callback=lambda i, n, _name: bar.progress(i / n))
            if failed:
                st.session_state[f"{pre}_fetch_failed"] = failed
            cv.finish_edit({
                "message": f"Fetched {len(fetched)} card(s)" + (f"; {len(failed)} not found." if failed else "."),
                "reference": True,
            })
    if st.session_state.get(f"{pre}_fetch_failed"):
        st.warning("Not found on Scryfall: " + ", ".join(st.session_state[f"{pre}_fetch_failed"]))

    if df.empty:
        st.info("This list is empty — add a card by name above.")
    else:
        f1, f2, f3 = st.columns([2, 2, 3])
        search = f1.text_input("Search by card name", key=f"{pre}_search")
        own = f2.selectbox("Ownership", OWNERSHIP_FILTERS, key=f"{pre}_f_own")
        all_labels = sorted({t for v in df[tags_col] for t in v.split(",") if t}, key=str.lower)
        picked = f3.multiselect(col_label, all_labels, key=f"{pre}_f_tags")
        attn = st.checkbox(
            "Needs attention only", key=f"{pre}_f_attn",
            help=cfg["attn_help"],
        )

        scoped = df
        if search:
            scoped = scoped[scoped["name"].str.contains(search, case=False, na=False, regex=False)]
        if own == "Owned":
            scoped = scoped[scoped["owned_qty"] > 0]
        elif own == "In a deck":
            scoped = scoped[scoped["deck_count"] > 0]
        elif own == "Not owned":
            scoped = scoped[scoped["owned_qty"] == 0]
        if picked:
            scoped = scoped[scoped[tags_col].apply(lambda v: bool(set(v.split(",")) & set(picked)))]
        if attn:
            scoped = scoped[scoped["needs_attention"]]
        st.caption(f"Showing {len(scoped)} of {len(df)}.")

        rows = []
        for _, r in scoped.iterrows():
            local = r["local_image_path"] if pd.notna(r["local_image_path"]) else None
            row = {
                "image_uri": (fmt.local_image_url(local) if local else None)
                             or (r["image_uri"] if pd.notna(r["image_uri"]) else None),
                "Card": r["name"],
            }
            if cfg["has_flag"]:
                row["Scryfall says"] = r["scryfall_status"]
            row[col_label] = r[tags_col]
            row["Status"] = r["ownership"]
            row["Owned"] = int(r["owned_qty"])
            row["In decks"] = int(r["deck_count"])
            row["Delete"] = False
            rows.append(row)
        order = ["image_uri", "Card"] + (["Scryfall says"] if cfg["has_flag"] else []) + [
            col_label, "Status", "Owned", "In decks", "Delete"]
        edited = st.data_editor(
            pd.DataFrame(rows),
            column_config={
                "image_uri": st.column_config.ImageColumn("Art", width="small"),
                "Card": st.column_config.TextColumn("Card", disabled=True),
                "Scryfall says": st.column_config.TextColumn("Scryfall says", disabled=True),
                col_label: st.column_config.TextColumn(f"{col_label} (comma-separated)"),
                "Status": st.column_config.TextColumn("Status", disabled=True),
                "Owned": st.column_config.NumberColumn("Owned", disabled=True),
                "In decks": st.column_config.NumberColumn("In decks", disabled=True),
                "Delete": st.column_config.CheckboxColumn(
                    "Delete", help="Tick, then save, to remove this name from the list entirely."),
            },
            column_order=order, hide_index=True, width="stretch",
            # Keyed by every filter (not a static key) so changing one gives a
            # genuinely fresh widget instead of Streamlit reapplying this
            # editor's in-progress edits to a different set of rows by position.
            key=f"{pre}_editor_{hash((search, own, tuple(picked), attn))}",
        )
        if st.button("💾 Save changes", key=f"{pre}_save"):
            to_delete = [row["Card"] for _, row in edited.iterrows() if row["Delete"]]
            edits = {row["Card"]: _split_labels(row[col_label])
                     for _, row in edited.iterrows() if not row["Delete"]}
            removed = cfg["delete_names"](conn, to_delete)
            changed = cfg["bulk_set"](conn, edits)
            cv.finish_edit({
                "message": f"Updated {changed} card(s)" + (f", removed {removed} from the list." if removed else "."),
                "reference": True,
            })

    st.divider()
    if st.button("💾 Export lists to CSV", key=f"{pre}_export",
                 help="Writes data/game_changers.csv and data/mana_tags.csv from the database, "
                      "so re-running the migration can't discard your edits to these lists."):
        counts = writes.export_reference_lists(conn, DATA_DIR)
        st.toast("Wrote " + ", ".join(f"data/{f} ({n} rows)" for f, n in counts.items()), icon="✅")


GC_CFG = {
    "prefix": "editor_gc", "noun": "Categories", "tags_col": "categories", "has_flag": True,
    "load": loaders.load_game_changers_overview,
    "set_tags": writes.set_game_changer_tags, "get_map": writes.get_game_changer_tags_map,
    "bulk_set": writes.bulk_set_game_changer_tags, "delete_names": writes.delete_game_changer_names,
    "attn_help": "No card data, tagged but Scryfall no longer flags it, or Scryfall flags it but "
                 "it has no category.",
}
MT_CFG = {
    "prefix": "editor_mt", "noun": "Tags", "tags_col": "tags", "has_flag": False,
    "load": loaders.load_mana_tags_overview,
    "set_tags": writes.set_mana_tags, "get_map": writes.get_mana_tags_map,
    "bulk_set": writes.bulk_set_mana_tags, "delete_names": writes.delete_mana_tag_names,
    "attn_help": "Names with no card data (no art yet).",
}

tab_gc, tab_manatags, tab_carddata = st.tabs(["Game Changers", "Mana Tags", "Card Data"])

# ------------------------------------------------------------------
# Game Changers — card_name-keyed (NOT deck-scoped), across every card
# in the database that's either Scryfall-flagged or already carries a
# custom category. Categories come from a master catalog (add/remove
# below), same pattern as the Themes catalog above.
# ------------------------------------------------------------------
with tab_gc:
    @st.dialog("⚙️ Manage the master category list")
    def _manage_gc_catalog_dialog():
        """Phase 5: was an inline st.expander."""
        st.caption(
            "Adding/removing here only changes what's offered below — it never touches any "
            "card's existing category assignments."
        )
        gc_catalog = loaders.load_game_changer_categories(conn)
        new_gc_category = st.text_input("New category name", key="editor_gc_catalog_add")
        if st.button("➕ Add to master list", key="editor_gc_catalog_add_btn"):
            if new_gc_category.strip():
                writes.add_game_changer_category(conn, new_gc_category.strip())
                cv.finish_edit({"message": f"Added '{new_gc_category.strip()}' to the master list.",
                                "reference": True})
            else:
                st.warning("Enter a category name.")

        if gc_catalog:
            gc_catalog_remove = st.selectbox(
                "Remove from master list", gc_catalog, key="editor_gc_catalog_remove"
            )
            if st.button("🗑️ Remove from master list", key="editor_gc_catalog_remove_btn"):
                writes.remove_game_changer_category(conn, gc_catalog_remove)
                cv.finish_edit({"message": f"Removed '{gc_catalog_remove}' from the master list.",
                                "reference": True})
        else:
            st.caption("Master category list is empty.")

        if st.button("Close", key="editor_gc_catalog_close"):
            cv.close_dialog("editor_gc_catalog_dialog")
            st.rerun()

    if st.button("⚙️ Manage the master category list", key="editor_gc_catalog_open"):
        cv.open_dialog("editor_gc_catalog_dialog")
    if cv.dialog_is_open("editor_gc_catalog_dialog"):
        _manage_gc_catalog_dialog()

    st.divider()

    _render_reference_list(GC_CFG)

# ------------------------------------------------------------------
# Mana Tags (Prompt Pass 12 / prompt5.txt) — card_name-keyed (NOT
# deck-scoped), across every card in the database, same pattern as Game
# Changers above. Unlike Game Changers, mana_tags has no Scryfall-derived
# flag to seed candidates from (it's 100% hand-curated — Fast, Dual,
# Shockland, Fetch, Ritual, Mana Doubler, Medallion, Moxen, etc.), so a
# dedicated "tag a card" search comes first, letting you assign a tag to
# ANY card in the database (not just ones that already have one); the
# overview table below it only ever shows cards that already carry at
# least one tag, exactly like Game Changers' overview does for its own
# candidate set.
# ------------------------------------------------------------------
with tab_manatags:
    st.caption(
        "Optimized-mana categories (Fast, Dual, Shockland, Fetch, Ritual, …), shown per deck on the Decks page.",
        help=(
            "Reconstructs the old 'Optimized Mana' footnote. Card-name-keyed across your "
            "whole database, not deck-scoped, same as Game Changers."
        ),
    )

    @st.dialog("⚙️ Manage the master tag list")
    def _manage_mt_catalog_dialog():
        """Phase 5: was an inline st.expander."""
        st.caption(
            "Adding/removing here only changes what's offered below — it never touches any "
            "card's existing tag assignments."
        )
        mt_catalog = loaders.load_mana_tag_catalog(conn)
        new_mt_tag = st.text_input("New tag name", key="editor_mt_catalog_add")
        if st.button("➕ Add to master list", key="editor_mt_catalog_add_btn"):
            if new_mt_tag.strip():
                writes.add_mana_tag_to_catalog(conn, new_mt_tag.strip())
                cv.finish_edit({"message": f"Added '{new_mt_tag.strip()}' to the master list.",
                                "reference": True})
            else:
                st.warning("Enter a tag name.")

        if mt_catalog:
            mt_catalog_remove = st.selectbox(
                "Remove from master list", mt_catalog, key="editor_mt_catalog_remove"
            )
            if st.button("🗑️ Remove from master list", key="editor_mt_catalog_remove_btn"):
                writes.remove_mana_tag_from_catalog(conn, mt_catalog_remove)
                cv.finish_edit({"message": f"Removed '{mt_catalog_remove}' from the master list.",
                                "reference": True})
        else:
            st.caption("Master tag list is empty.")

        if st.button("Close", key="editor_mt_catalog_close"):
            cv.close_dialog("editor_mt_catalog_dialog")
            st.rerun()

    if st.button("⚙️ Manage the master tag list", key="editor_mt_catalog_open"):
        cv.open_dialog("editor_mt_catalog_dialog")
    if cv.dialog_is_open("editor_mt_catalog_dialog"):
        _manage_mt_catalog_dialog()

    st.divider()

    st.markdown(
        "**Tag a card**",
        help=(
            "Search any card in your database and assign its optimized-mana tags — this is the "
            "only way to give a card its FIRST tag, since (unlike Game Changers) there's no "
            "Scryfall flag to seed the list below from."
        ),
    )
    mt_search = st.text_input("Search for a card by name", key="editor_mt_search_add")
    mt_matches = q.search_card_names(conn, mt_search) if mt_search.strip() else []
    if mt_search.strip() and not mt_matches:
        st.caption("No cards found matching that name.")
    elif mt_matches:
        mt_pick = st.selectbox("Card", mt_matches, key="editor_mt_pick")
        mt_current_tags = writes.get_mana_tags_map(conn).get(mt_pick, [])
        mt_tags_input = st.text_input(
            "Tags (comma-separated)",
            value=", ".join(mt_current_tags),
            # Keyed by the picked card (not a static key) so switching
            # cards resets this box to THAT card's own saved tags —
            # Streamlit ignores `value=` on rerender for a key it has
            # already instantiated, so a static key would carry over
            # whatever text was typed for the previous card.
            key=f"editor_mt_tags_input_{mt_pick}",
        )
        if st.button("💾 Save tags for this card", key="editor_mt_save_one"):
            labels = [x.strip() for x in mt_tags_input.split(",") if x.strip()]
            writes.set_mana_tags(conn, mt_pick, labels)
            cv.finish_edit({"message": f"Updated mana tags on '{mt_pick}'.", "reference": True})

    st.divider()

    _render_reference_list(MT_CFG)

# ------------------------------------------------------------------
# Card Data — refresh Reserved List / Game Changer / legality / price
# straight from Scryfall for every card already in the database
# ------------------------------------------------------------------
with tab_carddata:
    st.markdown(
        "**Refresh Scryfall data**",
        help=(
            "Re-fetches every card already in your database by its permanent Scryfall ID and "
            "updates its Reserved List flag, Game Changer flag, Showcase/Borderless flags, "
            "Commander legality, and current price. Doesn't touch your decklists or tags — safe "
            "to run anytime. Needs network access and the `requests` package. Afterward, it also "
            "automatically prunes your collection: any lot with no assigned location, a quantity "
            "of 0/none, and not present in any deck's mainboard or shortlist is deleted."
        ),
    )
    total_cards = conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0]
    st.caption(f"{total_cards} card(s) in your database.")

    if not card_resolver.scryfall_available():
        st.warning("The `requests` package isn't installed — run `pip install requests` to enable this.")
    elif st.button("🔄 Refresh now", key="editor_refresh_cards_btn"):
        progress_bar = st.progress(0.0)
        status = st.empty()

        def _progress(done, total, name):
            progress_bar.progress(done / total if total else 1.0)
            status.caption(f"{done}/{total} — {name}")

        summary = refresh.refresh_all_cards(conn, progress_callback=_progress)
        lines = [f"Checked {summary['checked']} card(s), updated {summary['updated']}."]
        if summary["newly_reserved"]:
            lines.append("Newly flagged Reserved List: " + ", ".join(summary["newly_reserved"]))
        if summary["newly_game_changer"]:
            lines.append("Newly flagged Game Changer: " + ", ".join(summary["newly_game_changer"]))
        if summary["pruned_count"]:
            lines.append(
                f"Pruned {summary['pruned_count']} collection lot(s) with no location, no "
                f"quantity, and not in any deck's mainboard/shortlist: "
                + ", ".join(summary["pruned_cards"])
            )
        _orphans = q.orphan_report(conn)
        lines.append(
            "Stale data left (see **Clean up** below): "
            + ", ".join(f"{len(_orphans[k])} {label}" for k, label in ORPHAN_LABELS.items())
        )
        warnings = []
        if summary["unresolved"]:
            warnings.append(f"Couldn't refresh {len(summary['unresolved'])} card(s): "
                            + ", ".join(summary["unresolved"]))
        cv.finish_edit({"message": lines, "warnings": warnings, "collection": True, "reference": True})

    st.divider()

    st.divider()

    # ------------------------------------------------------------------
    # Clean up (UI plan Phase 8): read-only report of stale rows; each bucket
    # deletes only after its confirm box is ticked, and the delete helpers
    # re-check the reference rules themselves.
    # ------------------------------------------------------------------
    st.markdown(
        "**Clean up**",
        help=(
            "Stale rows nothing uses. Hidden by default and never deleted automatically — each "
            "group lists exactly what will go and needs its confirm box ticked first. A card used "
            "by a lot, deck, shortlist, tag or one of your Game Changer / Mana Tag lists is never "
            "listed."
        ),
    )
    orphans = q.orphan_report(conn)
    image_dir = os.path.join(fmt.STATIC_DIR, "image_cache")
    if not any(len(df) for df in orphans.values()):
        st.caption("Nothing stale — every card, tag and image is accounted for.")

    def _cleanup_bucket(key, title, note, cols, action):
        frame = orphans[key]
        if frame.empty:
            return
        with st.expander(f"{title} ({len(frame)})"):
            st.caption(note)
            st.dataframe(frame[cols], hide_index=True, width="stretch")
            ok = st.checkbox(f"Yes, delete these {len(frame)}", key=f"carddb_clean_{key}_ok")
            if st.button("🗑️ Delete", key=f"carddb_clean_{key}_btn", disabled=not ok):
                n = action(frame)
                cv.finish_edit({"message": f"Deleted {n}.", "reference": True, "collection": True})

    _cleanup_bucket(
        "unreferenced_cards", "Unreferenced cards",
        "Card rows with no lot, deck, shortlist, tag or list entry. Cheap to keep and re-fetchable, "
        "so this is opt-in.",
        ["name", "set_code", "collector_number"],
        lambda f: len(writes.delete_orphan_cards(conn, f["scryfall_id"].tolist())),
    )
    _cleanup_bucket(
        "stale_card_tags", "Deck tags for cards no longer in that deck",
        "Strategy/rule-0 tags left behind when a card left its deck.",
        ["deck", "card", "tag"],
        lambda f: writes.delete_orphan_card_tags(
            conn, list(f[["deck_id", "scryfall_id"]].drop_duplicates().itertuples(index=False, name=None))),
    )
    _cleanup_bucket(
        "orphan_image_files", "Image files with no card row",
        "Files in static/image_cache/ that no card refers to.",
        ["file"],
        lambda f: len(writes.delete_orphan_image_files(conn, f["file"].tolist(), image_dir)),
    )
    _cleanup_bucket(
        "missing_image_files", "Cards pointing at a missing image file",
        "Clears the dead path; the next image sync re-downloads it.",
        ["name", "local_image_path"],
        lambda f: writes.clear_missing_image_paths(conn, f["scryfall_id"].tolist()),
    )

    # ------------------------------------------------------------------
    # Commander Spellbook combos — all decks at once. The per-deck version
    # of this lives on the Decks page (where the results are actually
    # read); this is the "I've been editing decklists, re-check everything"
    # path, alongside the Scryfall refresh above for the same reason.
    # ------------------------------------------------------------------
    st.markdown(
        "**Refresh Commander Spellbook combos**",
        help=(
            "Re-checks every deck against commanderspellbook.com's public combo database: which "
            "combos are fully assembled in each deck right now, and which are exactly one card "
            "short. Results are cached and shown per deck on the Decks page — this only refreshes "
            "them in bulk. One request per deck; never touches your decklists or tags. Needs "
            "network access and the `requests` package."
        ),
    )

    combo_sync_df = loaders.load_deck_combo_sync_all(conn)
    never_synced = int(combo_sync_df["fetched_at"].isna().sum())
    st.caption(
        f"{len(combo_sync_df)} deck(s) tracked · {never_synced} never checked."
        if never_synced
        else f"All {len(combo_sync_df)} tracked deck(s) have been checked at least once."
    )

    if not card_resolver.scryfall_available():
        # Same `requests` dependency as the Scryfall refresh above, so the
        # same availability check answers for both.
        st.warning("The `requests` package isn't installed — run `pip install requests` to enable this.")
    elif st.button("🔗 Check all decks", key="carddb_refresh_combos_btn"):
        combo_progress = st.progress(0.0)
        combo_status = st.empty()

        def _combo_progress(done, total, deck_name):
            combo_progress.progress(done / total if total else 1.0)
            combo_status.caption(f"{done}/{total} — {deck_name}")

        deck_inputs = [
            (row["deck_id"], row["name"], row["commander"], row["partner"],
             q.deck_combo_card_rows(conn, row["deck_id"]))
            for _, row in loaders.load_decks_df(conn).iterrows()
        ]

        combo_totals = {"included": 0, "almost": 0}
        combo_failures = []
        # Per-deck findings (what was actually found, not just a count) —
        # shown below the overview table so "Check all decks" answers
        # "what are they", the same as the Decks page's own per-deck check.
        combo_findings = []
        for deck_id, deck_name, result, error in spellbook.fetch_many(
            deck_inputs, progress_callback=_combo_progress
        ):
            if error:
                combo_failures.append(f"{deck_name} ({error})")
                continue
            n_inc, n_alm = writes.save_deck_combos(conn, deck_id, result)
            combo_totals["included"] += n_inc
            combo_totals["almost"] += n_alm
            if n_inc:
                combo_findings.append((deck_name, spellbook.combo_list_text(result["included"], limit=6)))

        lines = [
            f"Checked {len(deck_inputs)} deck(s): {combo_totals['included']} assembled combo(s) "
            f"found, {combo_totals['almost']} one card away."
        ] + [f"**{deck_name}** — {line}" for deck_name, line in combo_findings]
        cv.finish_edit({
            "message": lines,
            "warnings": ["Couldn't check: " + "; ".join(combo_failures)] if combo_failures else [],
            "combos": True,
        })

    combo_overview = combo_sync_df.rename(columns={
        "deck_name": "Deck", "fetched_at": "Last checked",
        "included_count": "Combos in deck", "almost_count": "One card away",
    })
    st.dataframe(
        combo_overview[["Deck", "Last checked", "Combos in deck", "One card away"]],
        hide_index=True, width="stretch",
    )

    # The "Salt Scores (EDHREC)" sub-editor that used to live here was
    # removed dashboard-wide in Prompt Pass 5: no lightweight public API
    # exposes EDHREC salt scores, and the only alternative data source
    # (MTGJSON's edhrecSaltiness field) only ships inside its full
    # AllPrintings-scale exports — far too heavy an ongoing dependency
    # for one hand-sized field in a personal-scale tool. See
    # PROJECT_STATE.md for the full writeup. cards.edhrec_salt itself is
    # left in the schema untouched (additive policy), so any values
    # entered before this pass aren't lost, there's just no editor for it
    # anymore.
