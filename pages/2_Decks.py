"""
LAYOUT (UI plan Phase 3): the branded header, cross-links, "Turn 0" stat grid
and a single Export expander (Moxfield text + printable sheet) sit above five
tabs, each built by a _render_<tab>(conn, deck_id, ...) function below:
  Overview     - win conditions/strengths/weaknesses, optimized mana, Game
                 Changers, Reserved List, Build Plan
  Mana & Curve - themes, type breakdown, mana curve, strategy tags
  Combos       - Commander Spellbook panel
  Cards        - Mainboard/Shortlist browser (its filters are in an expander
                 on the tab, not the sidebar) + top-10 most expensive
  History      - change log
"Compare all decks" moved to the Home page. st.tabs runs every tab body on
each rerun, so no tab does network work outside an explicit button press.
SPEED (UI plan Phase 4): the Export expander is built only while open
(on_change="rerun" + .open); the Combos tab and the Cards browser are
st.fragments, so their own controls rerun just that panel; and the header
image is an app/static/ URL, while the downloadable sheet embeds its art.
The notes below predate the split but still describe each section's content.

Decks page (renamed from "Decks & Maybeboard" in Prompt Pass 4 — the
deck's Shortlist (the old maybeboard: ideas plus the swaps staged from
them) is still browsable here via the Board toggle near the bottom, this
is just the section's display name/URL now).

Pick a deck (or land here already-selected, via a `deck_id` query param
set by the home page's deck-tile links, or on whatever deck you last
picked on another deck page: UI plan Phase 2's shared current deck, see
card_view.render_deck_picker) to see a branded header (with cross-links
to the same deck's Editor / Probability / Advisor views beneath it) (Prompt
Pass 10 — the deck's own Name, its resolved thumbnail, its short
descriptive tagline, and its color-identity mana symbols; see below),
then its "Turn 0" stat grid — commander/partner, colors, bracket,
interaction, tutors, when it was built, win/loss record, deck
value, and how many of its cards are physically sleeved in it right now
— followed by win conditions / strengths / weaknesses, then (Prompt Pass
4 reorder) optimized mana, Game Changers, and the Reserved List right
underneath that, then themes, mana curve, type breakdown, and strategy
tag breakdown, then the Commander Spellbook Combos panel (see below),
then Top 10 Most Expensive / Top 10 Saltiest.

Combos panel: reads the local deck_combos cache (never the network on page
load — the dashboard stays usable offline), split into "In this deck"
(every card present) and "One card away" (exactly one card short, grouped
by the missing card and cross-referenced against the collection so you can
see whether you already own it). "🔄 Check Spellbook" is the only thing
here that hits the network, and its result is flashed back as a message
naming the actual combos found (spellbook.combo_check_summary()) rather
than just a count, persisted across the rerun via session_state so it's
actually seen. This panel REPLACES the old hand-typed decks.combos field
— that free-text field and its Deck Editor input were removed outright
(it only ever held your own guess at what combos existed; this tells you
for real). decks.combos itself is left in the schema, untouched, per this
project's non-destructive column policy. See dashboard_lib/spellbook.py
for the API details and schema.sql's deck_combos comment for the cache
design.

Below that: the same table/grid card browser as the Collection page, for
the Mainboard, Shortlist, or both at once.

Proxy flags are intentionally NOT shown here yet — per the project state
doc, the Proxy convention in Location hasn't been confirmed with the user.

Phase 2 additions: an optional custom cover-image thumbnail next to the
deck header (set via the Deck Editor's Deck Info tab), and Top 10 Most
Expensive / Top 10 Saltiest card lists alongside the existing Game
Changers and Reserved List panels. Saltiest is hand-maintained EDHREC
data (Card Database -> Card Data -> Salt Scores), not a Scryfall field — this
panel itself was removed dashboard-wide in Prompt Pass 5, see below.

Prompt Pass 10 (prompt3.txt — branding/layout/color-identity/tag-list
pass) changes: the old generic "🃏 Decks" page title is gone — the page's
title is now the deck's own header block (card_view.render_deck_header())
built from decks.name (not the separate, user-editable "representative"
field the old per-deck header preferred), its resolved thumbnail (cover
image if set, else the commander's own card art via the now-public
card_view.deck_image_src(), "if available" rather than a placeholder),
its short descriptive tagline (decks.description — moved here from its
old spot below the Commander/Partner/.../Built stat grid), and a row of
the deck's own official Scryfall mana-symbol badges for its color
identity. A page-wide accent CSS pass (card_view.inject_deck_accent_css())
recolors this page's dividers/expander/sidebar edge with the deck's own
color-identity gradient/hex (dashboard_lib.formatting.deck_accent_gradient()/
deck_accent_hex()) so the page isn't flat black-and-white. The Game
Changers table's "Status" column (Scryfall-flag-vs-your-tag mismatch
indicator) is gone too — Card and Your tag only, per that prompt's
instruction 4; queries.deck_game_changers() itself still computes
`status` (a harmless, additive, page-content-only change — nothing else
in the codebase reads that column).
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import streamlit as st

from dashboard_lib import db, loaders, writes, formatting as fmt, moxfield_export, deck_printout
from dashboard_lib import queries as q, spellbook
from dashboard_lib import card_view as cv, nav

cv.setup_page("Decks · MTG Dashboard", "🃏")

db.require_db()
conn = db.get_connection()

MOXFIELD_EXPORT_DIR = os.path.join(db.BASE_DIR, "moxfield_exports")

# Prompt Pass 10: the old generic "🃏 Decks" st.title() is gone — the
# deck's own branded header (rendered further down, once a deck is
# chosen) is this page's title now. See card_view.render_deck_header().

st.sidebar.header("Deck")
db.refresh_data_button()

decks_df = loaders.load_decks_df(conn)
if decks_df.empty:
    nav.render_empty_state("No decks yet.", "Deck_Editor", "✏️ Create one in the Deck Editor")
    st.stop()

# Honor a `?deck_id=<id>` query param (set by the home page's deck-tile
# links, see dashboard_lib.card_view.render_deck_landing_grid) by seeding
# the shared current deck (Phase 2) before the picker is drawn. A param
# seeds only once per value per session (cv.seed_current_deck), and it's
# deleted right after reading so it can't keep forcing this deck back
# after the user picks another one.
if "deck_id" in st.query_params:
    cv.seed_current_deck(st.session_state, st.query_params.get("deck_id"))
    del st.query_params["deck_id"]

deck_id = cv.render_deck_picker(decks_df, key="deckpage_deck_select")

meta = loaders.load_deck_meta(conn, deck_id)
stats = loaders.load_deck_stats(conn, deck_id)
value = loaders.load_deck_value(conn, deck_id)
sleeved = loaders.load_in_deck_sleeved_count(conn, deck_id, meta.get("name"))

# ------------------------------------------------------------------
# Deck header + page-wide themed accents (Prompt Pass 10 / prompt3.txt)
# ------------------------------------------------------------------
deck_name = meta.get("name") or "Deck"
color_identity_raw = meta.get("color_identity")

# Reuse the exact same cover-image/commander-art row shape and fallback
# chain the home page's deck tiles already use (Prompt Pass 4), via the
# cached load_decks_with_covers() query, rather than resolving the
# thumbnail a third different way.
covers_df = loaders.load_decks_with_covers(conn)
cover_rows = covers_df[covers_df["deck_id"] == deck_id]
header_img_src = cv.deck_image_src(cover_rows.iloc[0]) if not cover_rows.empty else None

cv.inject_deck_accent_css(
    fmt.deck_accent_gradient(color_identity_raw), fmt.deck_accent_hex(color_identity_raw)
)
cv.render_deck_header(deck_name, meta.get("description"), header_img_src, color_identity_raw)
nav.render_deck_links("Decks")

# ------------------------------------------------------------------
# Turn 0 stat grid
# ------------------------------------------------------------------
def _out_of_5(value):
    """Bracket and Interaction are both 0-5 scores; show the scale so the number means something."""
    return "—" if value is None else f"{value} / 5"


meta_col1, meta_col2, meta_col3, meta_col4 = st.columns(4)
meta_col1.markdown(f"**Commander**\n\n{meta.get('commander') or '—'}")
meta_col2.markdown(f"**Partner**\n\n{meta.get('partner') or '—'}")
meta_col3.markdown(f"**Colors**\n\n{fmt.deck_color_identity_display(meta.get('color_identity'))}")
meta_col4.markdown(f"**Bracket**\n\n{_out_of_5(meta.get('bracket'))}")

meta_col5, meta_col6, meta_col7 = st.columns(3)
meta_col5.markdown(f"**Interaction**\n\n{_out_of_5(meta.get('interaction'))}")
meta_col6.markdown(f"**Tutors**\n\n{meta.get('tutors') or '—'}")
meta_col7.markdown(f"**Built**\n\n{meta.get('initially_built') or '—'}")
# (The old hand-typed "Combos" field that used to sit here was removed —
# see the Commander Spellbook Combos panel further down the page, which
# replaces it with real data instead of a guess. decks.combos itself is
# left in the schema untouched; see dashboard_lib/spellbook.py.)
# (decks.description now renders as the tagline directly beneath the
# deck name in the header above — Prompt Pass 10 relocated it from here.)

st.divider()

stat_col1, stat_col2, stat_col3, stat_col4, stat_col5 = st.columns(5)
stat_col1.metric("Games played", stats["games_played"])
stat_col2.metric("Wins", stats["wins"])
stat_col3.metric("Losses", stats["losses"])
stat_col4.metric("Win rate", f"{stats['win_rate']*100:.0f}%" if stats["win_rate"] is not None else "—")
deck_size = value.get("total_cards") or 0
sleeved_label = f"{int(sleeved)}/{deck_size}" if sleeved is not None else f"—/{deck_size}"
stat_col5.metric("Sleeved in deck", sleeved_label, help="Tracked collection rows whose Location matches this deck's name, plus the deck list's own basic lands (never individually tracked in the collection but physically sleeved all the same), vs. mainboard size.")

st.caption(f"Estimated deck value: {fmt.format_money(value.get('total_value'))}")


def _render_export(conn, deck_id, meta, stats, value, deck_name, color_identity_raw, cover_row):
    """Export expander body: Moxfield text + printable deck sheet. Only
    called while the expander is open (see below), because building the
    sheet loads every panel's data and base64-encodes its art."""
    st.markdown(
        "**📋 Moxfield**",
        help="Hover the text box and click the copy icon in its corner to copy it to your clipboard.",
    )
    export_include_mb = st.checkbox(
        "Include shortlist (as a separate '// Maybeboard' section)",
        key=f"deck_{deck_id}_moxfield_include_mb",
    )
    moxfield_text = moxfield_export.export_deck_text(conn, deck_id, include_maybeboard=export_include_mb)
    if not moxfield_text:
        st.caption("No mainboard cards to export yet.")
    else:
        st.code(moxfield_text, language=None)

        if st.button("💾 Save to file", key=f"deck_{deck_id}_moxfield_save"):
            os.makedirs(MOXFIELD_EXPORT_DIR, exist_ok=True)
            base_name = fmt.safe_filename(meta.get("name") or "deck")
            suffix = " (with maybeboard)" if export_include_mb else ""
            out_path = os.path.join(MOXFIELD_EXPORT_DIR, f"{base_name}{suffix}.txt")
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(moxfield_text + "\n")
            st.success(f"Saved to `{os.path.relpath(out_path, db.BASE_DIR)}`")

    st.divider()
    st.markdown(
        "**🖨️ Printable deck sheet**",
        help=(
            "A one-page, Letter-size sheet with stats, charts, art and the full decklist. "
            "Open the downloaded file in a browser and print (or save as PDF)."
        ),
    )
    _sheet_df = loaders.load_deck_cards_df(conn, deck_id)
    if _sheet_df.empty:
        st.caption("No mainboard cards to print yet.")
    else:
        _commander_names = {n for n in (meta.get("commander"), meta.get("partner")) if n}
        _priciest = (
            _sheet_df[~_sheet_df["name"].isin(_commander_names)]
            .assign(current_price_usd=lambda d: pd.to_numeric(d["current_price_usd"], errors="coerce"))
            .dropna(subset=["current_price_usd"])
            .nlargest(3, "current_price_usd")
        )
        # embed=True: the sheet is a downloaded file, so its images must be
        # data: URIs, not app/static/ URLs that only resolve inside the app.
        _showcase = [{"name": r["name"], "src": cv.card_image_src(r, embed=True)} for _, r in _priciest.iterrows()]
        sheet_img_src = cv.deck_image_src(cover_row, embed=True) if cover_row is not None else None
        sheet_html = deck_printout.build_deck_printout_html(
            meta, stats, value, _sheet_df, sheet_img_src,
            fmt.deck_color_identity_symbol_urls(color_identity_raw),
            fmt.deck_accent_gradient(color_identity_raw), fmt.deck_accent_hex(color_identity_raw),
            loaders.load_deck_rank_list(conn, deck_id, "win_conditions"),
            loaders.load_deck_rank_list(conn, deck_id, "strengths"),
            loaders.load_deck_rank_list(conn, deck_id, "weaknesses"),
            loaders.load_deck_themes(conn, deck_id),
            _showcase,
            game_changers=[
                {"name": r["card_name"], "tag": r["custom_tag"]}
                for _, r in loaders.load_deck_game_changers(conn, deck_id).iterrows()
            ],
            mana_tags=loaders.load_deck_mana_tag_summary(conn, deck_id),
            reserved=[
                {"name": r["card_name"], "price": r["price"]}
                for _, r in loaders.load_deck_reserved_list_cards(conn, deck_id).iterrows()
            ],
            # Turn 0 panel. Only the "included" bucket is printed — the
            # "almost" (one card away) bucket is a deck-building tool, not
            # something to read out at the table, so it's left off the
            # sheet entirely (not even a count). The stored newline columns
            # are split here so deck_printout stays free of this module's
            # storage convention.
            #
            # combos=None (never checked against Spellbook) omits the panel
            # rather than showing a misleading "no combos found"; a deck
            # that HAS been checked gets the list (possibly empty).
            combos=(
                [
                    {
                        "uses": spellbook.split_list(r["uses"]),
                        "produces": spellbook.split_list(r["produces"]),
                        "requires": spellbook.split_list(r["requires"]),
                        # clean_name() because an all-NULL column comes
                        # back from pandas as float64 NaN, which is truthy
                        # and would print "nan" on the sheet — see its
                        # docstring.
                        "bracket_tag": spellbook.clean_name(r["bracket_tag"]),
                        "mana_needed": spellbook.clean_name(r["mana_needed"]),
                    }
                    for _, r in loaders.load_deck_combos(conn, deck_id, "included").iterrows()
                ]
                if loaders.load_deck_combo_sync(conn, deck_id) else None
            ),
        )
        st.download_button(
            "⬇️ Download deck sheet (.html)",
            data=sheet_html.encode("utf-8"),
            file_name=f"{fmt.safe_filename(deck_name)} - deck sheet.html",
            mime="text/html",
            key=f"deck_{deck_id}_sheet_download",
        )


# Built lazily (UI plan Phase 4): a collapsed st.expander still runs its
# body, so with on_change="rerun" its .open flag tells us whether to build
# the export at all. The key keeps it open across reruns and lets tests open it.
export_box = st.expander("📤 Export", key="deck_export_open", on_change="rerun")
if export_box.open:
    with export_box:
        _render_export(
            conn, deck_id, meta, stats, value, deck_name, color_identity_raw,
            cover_rows.iloc[0] if not cover_rows.empty else None,
        )

def _render_overview(conn, deck_id, meta, deck_name, color_identity_raw):
    """Overview tab: win conditions, optimized mana, Game Changers, Reserved List, build plan."""
    wc, st_, wk = st.columns(3)


    def _render_rank_list(container, title_text, rows):
        container.markdown(f"**{title_text}**")
        if not rows:
            container.caption("None recorded.")
            return
        for r in rows:
            desc = f" — {r['description']}" if r.get("description") else ""
            container.markdown(f"{r['rank']}. **{r['label']}**{desc}")


    _render_rank_list(wc, "Win Conditions", loaders.load_deck_rank_list(conn, deck_id, "win_conditions"))
    _render_rank_list(st_, "Strengths", loaders.load_deck_rank_list(conn, deck_id, "strengths"))
    _render_rank_list(wk, "Weaknesses", loaders.load_deck_rank_list(conn, deck_id, "weaknesses"))

    mana_tag_col, gc_col, reserved_col = st.columns(3)

    with mana_tag_col:
        st.markdown("**Optimized mana**")
        mana_summary = loaders.load_deck_mana_tag_summary(conn, deck_id)
        if mana_summary:
            st.dataframe(pd.DataFrame(mana_summary), hide_index=True, width="stretch")
        else:
            st.caption("No optimized-mana tags matched for this deck.")

    with gc_col:
        st.markdown("**Game Changers**", help="Cards flagged as Game Changers in this deck, with your own category tag.")
        gc_df = loaders.load_deck_game_changers(conn, deck_id)
        if not gc_df.empty:
            st.dataframe(
                # scryfall_flag was already omitted from display in Prompt
                # Pass 4 (every row here is already Game-Changer-flagged or
                # custom-tagged, so the raw Scryfall boolean only ever showed
                # a static "1"); Prompt Pass 10 (prompt3.txt instruction 4)
                # drops the derived "Status" column too, per that prompt's
                # "only display the Card Name and its associated Tag" —
                # queries.deck_game_changers() itself still computes status
                # (a harmless additive column nothing else reads), only this
                # page's own display was simplified.
                gc_df.rename(columns={"card_name": "Card", "custom_tag": "Your tag"})[["Card", "Your tag"]],
                hide_index=True, width="stretch",
            )
        else:
            st.caption("No Game Changers in this deck.")

    with reserved_col:
        st.markdown("**Reserved List**", help="Pulled live from Scryfall's `reserved` field.")
        reserved_df = loaders.load_deck_reserved_list_cards(conn, deck_id)
        if not reserved_df.empty:
            # Prompt Pass 4: shows current price instead of quantity run.
            st.dataframe(
                reserved_df.rename(columns={"card_name": "Card", "price": "Price"}),
                column_config={"Price": st.column_config.NumberColumn("Price", format="$%.2f")},
                hide_index=True, width="stretch",
            )
        else:
            st.caption("No Reserved List cards in this deck.")

    # ------------------------------------------------------------------
    # Build Plan (deck lifecycle, Phase 6) — where every card of a brewing
    # deck would come from, and what pulling it costs the deck it's in.
    #
    # Rendered as an expander rather than a tab because this page's top-level
    # sections already are expanders ("Compare all decks", "Export to
    # Moxfield", "Printable deck sheet"); a lone tab beside them would be the
    # odd one out. Expanded by default for a deck actually marked 'brewing',
    # collapsed otherwise — a built deck still gets the panel, since "where
    # are the 4 cards I'm missing" is a fair question for any deck.
    #
    # The plan is read live (queries.deck_sourcing_plan is deliberately
    # uncached) because sleeving one card changes every number here.
    # ------------------------------------------------------------------
    _brewing = meta.get("build_state") == "brewing"
    _sourcing = q.deck_sourcing_plan(conn, deck_id)

    if _sourcing and _sourcing["needed"]:
        _pct = _sourcing["pct"]
        _title = (
            f"🔨 Build Plan — {_sourcing['needed']} non-basics"
            + (f" · {_pct:.0f}% sleeved" if _pct is not None else "")
        )
        with st.expander(_title, expanded=_brewing):
            _counts = _sourcing["counts"]
            _to_pull = sum(_counts[b] for b in ("box", "unlocated", "other_deck"))

            if _brewing:
                st.caption("Brewing — completion updates itself as you sleeve the cards.")
            else:
                st.caption("Mark the deck as brewing in the Deck Editor to keep this panel open.")

            if _pct is not None:
                st.progress(
                    min(1.0, _pct / 100.0),
                    text=f"{_sourcing['located']} of {_sourcing['needed']} non-basics sleeved here",
                )

            # One row of counts, in the order you'd work them.
            _cols = st.columns(5)
            for _col, _bucket in zip(_cols, q.SOURCING_BUCKETS):
                _col.metric(q.bucket_label(_bucket), _counts.get(_bucket, 0))

            st.markdown("#### Sourcing")
            for _bucket in q.SOURCING_BUCKETS:
                _rows = _sourcing["buckets"].get(_bucket) or []
                if not _rows:
                    continue
                _n = _counts.get(_bucket, 0)
                _icon = {"sleeved": "✅", "box": "📦", "unlocated": "❓",
                         "other_deck": "🃏", "not_owned": "🛒"}[_bucket]

                if _bucket == "other_deck":
                    # Grouped by donor deck, because the clustering IS the
                    # finding: "building this means gutting Tuvasa" is a
                    # decision to make knowingly and up front, not to discover
                    # card by card at the kitchen table.
                    st.markdown(f"**{_icon} {q.bucket_label(_bucket)} — {_n}**")
                    for _d in _sourcing["donors"]:
                        _warn = (
                            f" · ⚠️ leaves {_d['deck_name']} {_d['shortfall']} short"
                            if _d["shortfall"] else " · leftover lots — costs it nothing"
                        )
                        with st.expander(f"{_d['deck_name']} — {_d['copies']}{_warn}"):
                            if _d["shortfall"]:
                                st.warning(
                                    f"Pulling these would leave **{_d['deck_name']}** "
                                    f"{_d['shortfall']} card(s) short of its own list. "
                                    "Either accept that, or buy the copy instead — the "
                                    "Workbench's buy list is where that goes."
                                )
                            st.dataframe(
                                pd.DataFrame(
                                    [{"Card": c["card"], "Copies": c["copies"]} for c in _d["cards"]]
                                ),
                                hide_index=True, width="stretch",
                            )
                    continue

                with st.expander(f"{_icon} {q.bucket_label(_bucket)} — {_n}"):
                    if _bucket == "unlocated":
                        nav.render_empty_state(
                            "Owned, but no box is recorded.", "Workbench", "🔧 Set locations in the Workbench",
                        )
                    elif _bucket == "not_owned":
                        nav.render_empty_state(
                            "Not owned — these feed the buy list.", "Workbench", "🔧 See the Workbench buy list",
                        )
                    _show_where = _bucket in ("box", "sleeved")
                    st.dataframe(
                        pd.DataFrame([
                            {
                                "Card": r["card"],
                                "Copies": r["copies"],
                                **({"Where": ", ".join(r["locations"])} if _show_where else {}),
                            }
                            for r in _rows
                        ]),
                        hide_index=True, width="stretch",
                    )

            st.divider()
            _sheet_col, _state_col = st.columns([2, 1])

            # The pull sheet is grouped by location rather than card type,
            # because you work the box in location order.
            _pull_html = deck_printout.build_pull_sheet_html(
                _sourcing,
                accent_gradient=fmt.deck_accent_gradient(color_identity_raw),
                meta=meta,
            )
            _sheet_col.download_button(
                f"🖨️ Printable pull sheet ({_to_pull} to pull)",
                data=_pull_html.encode("utf-8"),
                file_name=f"{fmt.safe_filename(deck_name)} - pull sheet.html",
                mime="text/html",
                key=f"deck_{deck_id}_pull_sheet",
                disabled=not _to_pull,
                help="A checklist grouped by where each card is, so one walk through the "
                     "boxes collects everything. Open it in a browser and print.",
            )
            if _brewing:
                if _state_col.button(
                    "✅ Mark deck as built", key=f"deck_{deck_id}_mark_built",
                    help="Clears the brewing flag. Completion stays derived from where the "
                         "cards are, so this doesn't claim the deck is 100% sleeved.",
                ):
                    writes.set_build_state(conn, deck_id, None)
                    loaders.invalidate_deck_caches(deck_id)
                    st.rerun()
            elif _state_col.button(
                "🔨 Mark as brewing", key=f"deck_{deck_id}_mark_brewing",
                help="For a deck whose list exists but whose cards are still elsewhere.",
            ):
                writes.set_build_state(conn, deck_id, "brewing")
                loaders.invalidate_deck_caches(deck_id)
                st.rerun()


def _render_mana_curve(conn, deck_id):
    """Mana & Curve tab: themes, type breakdown, mana curve, strategy tags."""
    main_df_full = loaders.load_deck_cards_df(conn, deck_id)

    theme_col, curve_col = st.columns(2)

    with theme_col:
        st.markdown("**Themes**")
        themes = loaders.load_deck_themes(conn, deck_id)
        mains = [t["theme"] for t in themes if t["role"] == "main"]
        subs = [t["theme"] for t in themes if t["role"] == "sub"]
        st.markdown(f"- Main: {', '.join(mains) if mains else '—'}")
        st.markdown(f"- Sub: {', '.join(subs) if subs else '—'}")

        st.markdown("**Type breakdown**")
        if not main_df_full.empty:
            type_counts = main_df_full.groupby("card_type")["quantity"].sum().reindex(fmt.ALL_CARD_TYPES).fillna(0)
            st.bar_chart(type_counts, color=fmt.CATEGORY_COLORS[0])
        else:
            st.caption("No mainboard cards loaded yet.")

    with curve_col:
        st.markdown(
            "**Mana curve** (non-land, MDFC-aware)",
            help=(
                "Cards with a land on either face (e.g. an MDFC land) are excluded here, same as "
                "the rest of this dashboard's mana-curve and land-ratio math (Land Probability page)."
            ),
        )
        if not main_df_full.empty:
            nonland_df = main_df_full[~main_df_full["is_land"]]
        else:
            nonland_df = main_df_full
        if nonland_df.empty:
            st.caption("No non-land cards to chart yet.")
        else:
            breakdown_by = st.segmented_control(
                "Stack by", ["Color", "Type"], key=f"curve_stackby_{deck_id}"
            ) or "Color"
            chart_colors = None
            if breakdown_by == "Color":
                # Prompt Pass 4: fixed W/U/B/R/G/Multi-color/Colorless
                # grouping+order with custom mana-colored bars, instead of an
                # alphabetical-with-Colorless-last fallback order.
                bucketed = nonland_df.assign(
                    mana_color_bucket=nonland_df["color_display"].apply(fmt.mana_curve_color_bucket)
                )
                group_col = "mana_color_bucket"
                pivot = (
                    bucketed.groupby(["cmc", group_col])["quantity"]
                    .sum()
                    .unstack(group_col)
                    .fillna(0)
                    .sort_index()
                )
                ordered_cols = [c for c in fmt.MANA_CURVE_COLOR_ORDER if c in pivot.columns]
                pivot = pivot[ordered_cols]
                chart_colors = [fmt.MANA_CURVE_COLOR_HEX[c] for c in ordered_cols]
            else:
                group_col = "card_type"
                pivot = (
                    nonland_df.groupby(["cmc", group_col])["quantity"]
                    .sum()
                    .unstack(group_col)
                    .fillna(0)
                    .sort_index()
                )
                ordered_cols = [c for c in fmt.ALL_CARD_TYPES if c in pivot.columns]
                ordered_cols += [c for c in pivot.columns if c not in ordered_cols]
                pivot = pivot[ordered_cols]
                chart_colors = fmt.category_colors(len(ordered_cols))
            st.bar_chart(pivot, color=chart_colors)

        st.markdown("**Strategy tag breakdown**")
        tag_counts = loaders.load_deck_strategy_tag_counts(conn, deck_id)
        if not tag_counts.empty:
            st.dataframe(tag_counts, hide_index=True, width="stretch")
        else:
            st.caption("No strategy tags recorded for this deck.")


@st.fragment
def _render_combos(conn, deck_id, meta):
    """Combos tab: Commander Spellbook panel (cached; network only on button press).
    A fragment (UI plan Phase 4), so opening combos or moving the "how many
    to show" slider reruns only this tab. A Spellbook check ends in a
    full-app rerun because it changes the cached combos other parts read."""
    st.subheader("🔗 Combos")

    combo_flash_key = f"deck_{deck_id}_combo_flash"
    combo_flash = st.session_state.pop(combo_flash_key, None)
    if combo_flash:
        st.success(combo_flash)

    combo_sync = loaders.load_deck_combo_sync(conn, deck_id)

    sync_col, button_col = st.columns([3, 1])
    with sync_col:
        if combo_sync:
            st.caption(
                f"From Commander Spellbook · last checked {combo_sync['fetched_at']} · "
                f"color identity {combo_sync['identity'] or '—'}. "
                "Re-check after changing the decklist."
            )
        else:
            st.caption("Not looked up yet — “Check Spellbook” sends this decklist to commanderspellbook.com.")

    with button_col:
        check_combos = st.button(
            "🔄 Check Spellbook",
            key=f"deck_{deck_id}_combo_refresh",
            width="stretch",
            help="One request to commanderspellbook.com's public find-my-combos API. Needs network access.",
        )

    if check_combos:
        combo_result = combo_error = None
        try:
            with st.spinner("Asking Commander Spellbook…"):
                combo_result, combo_error = spellbook.fetch_deck_combos(
                    q.deck_combo_card_rows(conn, deck_id),
                    meta.get("commander"),
                    meta.get("partner"),
                )
        except ImportError:
            # spellbook.py imports `requests` lazily (inside SpellbookClient)
            # precisely so this page still loads without it — the failure
            # surfaces here, on the button press, instead of at import time.
            combo_error = "Combo lookup needs the `requests` package installed."

        if combo_error:
            st.error(combo_error)
        elif combo_result is not None:
            writes.save_deck_combos(conn, deck_id, combo_result)
            loaders.invalidate_combo_caches()
            st.session_state[combo_flash_key] = spellbook.combo_check_summary(combo_result)
            st.rerun(scope="app")

    if combo_sync:
        included_df = loaders.load_deck_combos(conn, deck_id, "included")
        almost_df = loaders.load_deck_combos(conn, deck_id, "almost")
        owned = loaders.load_owned_card_quantities(conn)

        in_deck_tab, one_away_tab = st.tabs(
            [f"In this deck ({len(included_df)})", f"One card away ({len(almost_df)})"]
        )

        # Shared with the printable deck sheet, which decodes the same stored
        # convention — see spellbook.split_list().
        _lines = spellbook.split_list

        def _render_combo_detail(row):
            """Shared detail body for a single combo, used by both tabs."""
            meta_bits = []
            if row["bracket_tag"]:
                meta_bits.append(f"**{row['bracket_tag']}**")
            if row["mana_needed"]:
                meta_bits.append(f"mana needed {row['mana_needed']}")
            if row["popularity"]:
                meta_bits.append(f"{int(row['popularity']):,} decks run it")
            if not row["commander_legal"]:
                meta_bits.append("⚠️ not Commander-legal")
            if meta_bits:
                st.caption(" · ".join(meta_bits))

            produces = _lines(row["produces"])
            if produces:
                st.markdown("**Produces:** " + ", ".join(produces))

            st.markdown("**Cards:** " + ", ".join(_lines(row["uses"])))

            # Template requirements can't be matched to a specific card, so
            # they're called out rather than folded into the card list — a
            # combo listed as "in this deck" may still hinge on one of these.
            requires = _lines(row["requires"])
            if requires:
                st.markdown("**Also needs:** " + "; ".join(requires))

            prereqs = _lines(row["prerequisites"])
            if prereqs:
                st.markdown("**Prerequisites:**")
                for p in prereqs:
                    st.markdown(f"- {p}")

            steps = _lines(row["description"])
            if steps:
                st.markdown("**Steps:**")
                for i, step in enumerate(steps, start=1):
                    st.markdown(f"{i}. {step}")

            st.markdown(
                f"[Full writeup on Commander Spellbook]({spellbook.combo_url(row['combo_id'])})"
            )

        with in_deck_tab:
            if included_df.empty:
                st.caption(
                    "No complete combos known to Spellbook — it only tracks submitted combos, "
                    "so your own synergies won't appear."
                )
            else:
                st.caption("Every card of these combos is in the mainboard right now.")
                for _, row in included_df.iterrows():
                    label = " + ".join(_lines(row["uses"]))
                    produces_short = ", ".join(_lines(row["produces"])[:2])
                    with st.expander(f"{label} → {produces_short}"):
                        _render_combo_detail(row)

        with one_away_tab:
            if almost_df.empty:
                st.caption("Nothing is exactly one card away from a known combo.")
            else:
                # Grouping by the MISSING card is the actionable view: "adding
                # Freed from the Real completes 3 combos" is a shopping-list
                # answer, where the flat per-combo list below is reference.
                missing_rows = {}
                for _, row in almost_df.iterrows():
                    for card in _lines(row["missing"]):
                        key = card.split(" // ")[0].strip().lower()
                        entry = missing_rows.setdefault(
                            key, {"card": card, "combos": 0, "produces": set(), "best_pop": 0}
                        )
                        entry["combos"] += 1
                        entry["produces"].update(_lines(row["produces"])[:2])
                        entry["best_pop"] = max(entry["best_pop"], int(row["popularity"] or 0))

                summary = []
                for key, entry in missing_rows.items():
                    have = owned.get(key)
                    if have and have["available_qty"] > 0:
                        own_label = "✅ Own it (free)"
                    elif have and have["owned_qty"] > 0:
                        own_label = "🔶 Own it (in another deck)"
                    else:
                        own_label = "— Don't own"
                    summary.append({
                        "Add this card": entry["card"],
                        "In collection": own_label,
                        "Combos it completes": entry["combos"],
                        "Would produce": ", ".join(sorted(entry["produces"])[:3]),
                        "Popularity": entry["best_pop"],
                    })

                summary_df = pd.DataFrame(summary).sort_values(
                    ["Combos it completes", "Popularity"], ascending=False
                )

                st.caption(
                    "One card short of a known combo, ranked by combos completed, then popularity.",
                    help="“In collection” counts a copy as free unless it's sleeved in another deck.",
                )
                st.dataframe(
                    summary_df,
                    column_config={
                        "Popularity": st.column_config.NumberColumn(
                            "Popularity", format="%d",
                            help="Decks on Spellbook running the most popular combo this card would complete.",
                        ),
                    },
                    hide_index=True, width="stretch",
                )

                with st.expander(f"Every one-card-away combo ({len(almost_df)})"):
                    # Capped because a deck like a well-supported mono-black one
                    # can be one card away from 150+ combos — past the first
                    # couple of dozen by popularity it stops being useful.
                    show_n = st.slider(
                        "How many to show", min_value=5,
                        max_value=int(len(almost_df)), value=min(15, int(len(almost_df))),
                        key=f"deck_{deck_id}_combo_almost_n",
                    ) if len(almost_df) > 5 else len(almost_df)
                    for _, row in almost_df.head(int(show_n)).iterrows():
                        missing_label = ", ".join(_lines(row["missing"]))
                        produces_short = ", ".join(_lines(row["produces"])[:2])
                        with st.expander(f"Add {missing_label} → {produces_short}"):
                            _render_combo_detail(row)


def _render_cards(conn, deck_id):
    """Cards tab: Mainboard/Shortlist browser (filters live in an expander here, not the sidebar), then top-10 prices."""
    main_df_full = loaders.load_deck_cards_df(conn, deck_id)


    board_choice = st.segmented_control(
        "Board", ["Mainboard", "Shortlist", "Both"], key=f"deck_{deck_id}_board_choice"
    ) or "Mainboard"

    shortlist_df = loaders.load_shortlist_df(conn, deck_id)

    if board_choice == "Mainboard":
        working_df = main_df_full
    elif board_choice == "Shortlist":
        working_df = shortlist_df
    else:
        m = main_df_full.copy()
        m["board"] = "Mainboard"
        b = shortlist_df.copy()
        b["board"] = "Shortlist"
        for col in ("review_flag", "replace_target", "notes", "status"):
            if col not in m.columns:
                m[col] = None
        for col in ("quantity", "strategy_tags"):
            if col not in b.columns:
                b[col] = None
        working_df = pd.concat([m, b], ignore_index=True, sort=False)

    # Widget keys are namespaced by deck_id + board_choice so switching decks
    # or boards never tries to restore a filter/group selection that no longer
    # matches the available options for the new dataframe.
    scope_key = f"deck_{deck_id}_{board_choice}"

    if working_df.empty:
        st.info(f"No cards in the {board_choice.lower()} for this deck.")
    else:
        filter_box = st.expander("🔍 Filters")
        multi_facets = [("color_identity", "Color Identity", "Colorless")]
        if "strategy_tags" in working_df.columns:
            multi_facets.append(("strategy_tags", "Strategy Tag", None))

        single_facets = [("card_type", "Card Type"), ("rarity", "Rarity"), ("set_code", "Set")]
        if "board" in working_df.columns:
            single_facets.append(("board", "Board"))

        bool_toggles = [("is_game_changer", "Game Changer")]
        if "review_flag" in working_df.columns:
            bool_toggles.append(("review_flag", "Flagged for review"))

        filtered = cv.render_filter_panel(
            working_df,
            key_prefix=scope_key,
            container=filter_box,
            multi_facets=multi_facets,
            single_facets=single_facets,
            bool_toggles=bool_toggles,
        )

        column_config = cv.base_column_config()
        extra_cols = []
        if "quantity" in working_df.columns:
            column_config["quantity"] = st.column_config.NumberColumn("Qty", width="small")
            extra_cols.append("quantity")
        if "strategy_tags" in working_df.columns:
            column_config["strategy_tags"] = st.column_config.TextColumn("Strategy Tag", width="small")
            extra_cols.append("strategy_tags")
        if "board" in working_df.columns:
            column_config["board"] = st.column_config.TextColumn("Board", width="small")
            extra_cols.append("board")
        if "status" in working_df.columns:
            column_config["status"] = st.column_config.TextColumn("Status", width="small")
            extra_cols.append("status")
        if "review_flag" in working_df.columns:
            column_config["review_flag"] = st.column_config.CheckboxColumn("Review?", width="small")
            extra_cols.append("review_flag")
        if "replace_target" in working_df.columns:
            column_config["replace_target"] = st.column_config.TextColumn("Replaces", width="small")
            extra_cols.append("replace_target")
        if "notes" in working_df.columns:
            column_config["notes"] = st.column_config.TextColumn("Notes", width="large")
            extra_cols.append("notes")

        group_options = [("card_type", "Type"), ("color_display", "Color Identity"), ("rarity", "Rarity")]
        if "strategy_tags" in working_df.columns:
            group_options.append(("strategy_tags", "Strategy Tag"))
        if "board" in working_df.columns:
            group_options.append(("board", "Board"))

        cv.render_browser(
            filtered,
            key_prefix=scope_key,
            group_options=group_options,
            column_config=column_config,
            extra_table_cols=extra_cols,
        )


    st.markdown("**Top 10 most expensive cards**", help="By cards.current_price_usd, refreshed via Card Database -> Card Data. Single-card (unit) pricing — quantity isn't shown here.")
    price_df = loaders.load_deck_price_top10(conn, deck_id)
    if not price_df.empty:
        # Prompt Pass 4: quantity column hidden (unit pricing only); a
        # "purchase price / cost paid" column added, sourced from
        # collection.price_paid for the lot(s) sleeved in this deck.
        st.dataframe(
            price_df.rename(columns={
                "card_name": "Card", "price": "Price", "purchase_price": "Purchase price / Cost paid",
            }),
            column_config={
                "Price": st.column_config.NumberColumn("Price", format="$%.2f"),
                "Purchase price / Cost paid": st.column_config.NumberColumn(
                    "Purchase price / Cost paid", format="$%.2f",
                ),
            },
            hide_index=True, width="stretch",
        )
    else:
        st.caption("No priced cards in this deck yet.")


def _render_history(conn, deck_id):
    """History tab: applied/dropped changes with the game record between them."""
    history = loaders.load_deck_change_history(conn, deck_id)
    n_applied = sum(1 for h in history if h["kind"] == "change" and h["status"] == "applied")
    with st.expander(f"🕓 Changes ({n_applied} applied)"):
        if not history:
            st.caption("No changes recorded for this deck yet. Applying a swap or editing the mainboard starts the log.")
        else:
            for h in history:
                if h["kind"] == "games":
                    record = f"{h['wins']}-{h['losses']}" if h["games"] else "no games"
                    plural = "game" if h["games"] == 1 else "games"
                    st.caption(f"── {h['games']} {plural} {h['label']} · {record} ──")
                    continue
                parts = []
                if h["add_card"]:
                    parts.append(f"**+ {h['add_card']}**")
                if h["remove_card"]:
                    parts.append(f"− {h['remove_card']}")
                line = f"`{h['date']}`  " + "   ".join(parts)
                if h["quantity"] and h["quantity"] > 1:
                    line += f"  ×{h['quantity']}"
                if h["status"] == "dropped":
                    line = f"~~{line}~~  *(dropped)*"
                if h["notes"]:
                    line += f"  — {h['notes']}"
                st.markdown(line)
            st.caption(
                "A few games per version is a signal, not evidence.",
                help="Games are counted by day; a game on the day of a change counts toward the older list.",
            )


tab_overview, tab_mana, tab_combos, tab_cards, tab_history = st.tabs(
    ["Overview", "Mana & Curve", "Combos", "Cards", "History"]
)
with tab_overview:
    _render_overview(conn, deck_id, meta, deck_name, color_identity_raw)
with tab_mana:
    _render_mana_curve(conn, deck_id)
with tab_combos:
    _render_combos(conn, deck_id, meta)
with tab_cards:
    _render_cards(conn, deck_id)
with tab_history:
    _render_history(conn, deck_id)
