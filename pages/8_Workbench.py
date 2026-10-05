"""
Workbench page — every deck's pending shortlist work in one place
(Workbench rework, Phase 2; see IMPLEMENTATION_PLAN.md).

The Deck Editor's Shortlist tab answers "what's planned for THIS deck".
Nothing answered "what's planned at all", so 44 staged swaps across 10
decks sat invisible behind pick-a-deck for a month. This page is that
missing view, and it leads with conflicts — the rows that can't all
succeed — because those are the only ones needing a decision rather than
a click.

The inventory check here is queries.plan_availability(), which allocates
your actual copies across the whole queue in one pass. The per-row check
it replaces (card_inventory_status) told two planned swaps that each
independently wanted your single Blood Crypt that it was "available in
storage" — true of either alone, impossible together.

Applying happens through the same writes.apply_changes() the Shortlist tab
uses, so a swap applied here lands in history identically.

Phase 3 adds Contention (demand vs. supply per card, counting mainboard
usage too — not just the shortlist queue plan_availability() sees) and the
cross-deck Buy list. Phase 4 adds Reconcile: unlocated lots, each deck's
list-vs-physical diff, and per-deck build status.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import streamlit as st

from dashboard_lib import db, loaders, writes, card_resolver, queries as q
from dashboard_lib import card_view as cv
from dashboard_lib import moxfield_export as mox

cv.setup_page("Workbench · MTG Dashboard", "🔧")

db.require_db()
conn = db.get_connection()

st.title("🔧 Workbench")

st.sidebar.header("Workbench")
db.refresh_data_button()

# Bumped after every write. Each data_editor below keys on it: an editor
# keeps pending edits by ROW POSITION under a fixed key, so once an
# applied/dropped row leaves its table the next render would replay those
# edits onto whichever row slid into its slot (same reasoning as the
# Shortlist tab's `ver`).
VER_KEY = "workbench_ver"
RESULT_KEY = "workbench_result"
ver = st.session_state.get(VER_KEY, 0)


def _refresh(collection=False):
    st.session_state[VER_KEY] = ver + 1
    loaders.invalidate_deck_caches()
    if collection:
        loaders.invalidate_collection_caches()
    st.rerun()


def _flash():
    """Show and consume the ✅/❌ lines left by the write that caused this
    rerun. Consumed on display so they don't persist into the next one."""
    for line in st.session_state.pop(RESULT_KEY, []):
        (st.success if line.startswith("✅") else st.error)(line)


def _apply(change_ids, label="Applied"):
    results = writes.apply_changes(
        conn, change_ids,
        resolve_add=lambda name: card_resolver.resolve_or_fetch_card(conn, name=name),
    )
    lines = []
    for r in results:
        if r["ok"]:
            outcome = r["outcome"] or {}
            fetch = " (fetched fresh from Scryfall)" if r["was_new"] else ""
            if outcome.get("created_lot"):
                lot = " — created a new starter collection lot"
            elif outcome.get("assigned_lot_id"):
                lot = " — reassigned an existing owned copy"
            else:
                lot = ""
            lines.append(f"✅ {label} {r['add_name']}{fetch}, replaced {r['remove_name']}{lot}.")
        else:
            lines.append(f"❌ {r['add_name']}: {r['error']}")
    st.session_state[RESULT_KEY] = lines
    _refresh(collection=True)


# ------------------------------------------------------------------
# The queue. Loaded once, for every deck, and allocated in ONE pass —
# planned rows plus open ideas, because an idea competes for a physical
# card just as much as a staged swap does. plan_availability() walks them
# in (planned_at, change_id) order, so a planned swap staged last month
# outranks an idea jotted yesterday.
# ------------------------------------------------------------------
open_df = loaders.load_open_changes_df(conn, ("planned", "idea"))
planned_df = open_df[open_df["status"] == "planned"] if not open_df.empty else open_df
ideas_df = open_df[open_df["status"] == "idea"] if not open_df.empty else open_df

availability = q.plan_availability(conn, open_df)
conflicts = q.plan_conflicts(conn, open_df, availability)
unlocated = loaders.load_unlocated_lot_summary(conn)
# Not cached, like plan_availability above: contention counts mainboard
# usage too, so an apply or a hand edit made earlier in this same page run
# must be reflected immediately, not after the next cache-clearing rerun.
contention = q.contention_table(conn)
buy = q.buy_list(conn, contention=contention)

deck_count = int(planned_df["deck_id"].nunique()) if not planned_df.empty else 0
st.caption(
    f"**{len(planned_df)}** planned change(s) across **{deck_count}** deck(s) · "
    f"**{len(conflicts)}** conflict(s) · "
    f"**{len(contention)}** card(s) in contention · "
    f"**{unlocated['lots']}** lot(s) with no location (${unlocated['value']:,.0f})"
)
_flash()

# Contention can be non-empty with an EMPTY shortlist queue — a card run
# in 4 mainboards with 3 copies owned needs no idea/planned row to be real
# demand — so an empty open_df no longer stops the page outright, only
# the sections that actually depend on it.
if open_df.empty and not contention and not unlocated["lots"]:
    st.info("Nothing pending anywhere right now — no shortlist activity, no card "
            "in contention, and every lot has a location.")
    st.stop()

if open_df.empty:
    st.info(
        "Nothing on any shortlist. Add ideas from a deck's **Shortlist** tab in "
        "the Deck Editor, or from a card's tile on the **Decks** page."
    )


def _verdict_cell(change_id):
    """The Availability column: the allocator's one-line verdict, prefixed
    so the state is readable at a glance in a dense table."""
    verdict = availability.get(change_id, {})
    icon = {
        "available": "✅", "claimed_by": "⚠️", "in_other_deck": "📦",
        "not_owned": "🛒", "already_here": "↩️", "no_add": "—",
    }.get(verdict.get("verdict"), "")
    return f"{icon} {verdict.get('text', '')}".strip()


# ------------------------------------------------------------------
# Conflicts — first, because these are the rows that cannot all succeed.
# Everything below this is just a to-do list.
# ------------------------------------------------------------------
st.header(f"⚠️ Conflicts ({len(conflicts)})")
if not conflicts:
    st.success("No two plans want the same physical card. Everything below can be applied as it stands.")
else:
    st.caption(
        "Two or more open changes want one card, and between them want more copies than "
        "are free in storage. Applying them in order gives the card to the earliest; the "
        "rest will fail or buy a copy. Pick which deck keeps it and drop the others."
    )
    for group in conflicts:
        free = group["available_qty"] or 0
        with st.container(border=True):
            st.markdown(
                f"**{group['card']}** — {group['owned_qty']} owned, {free} free in storage, "
                f"{group['wanted_qty']} wanted (short {group['shortfall']})"
            )
            rows = [{
                "change_id": w["change_id"],
                "Deck": w["deck_name"],
                "Status": w["status"],
                "Qty": w["quantity"],
                "Gets it?": _verdict_cell(w["change_id"]),
                "Keep": False,
                "Drop": False,
            } for w in group["wanters"]]
            edited = st.data_editor(
                pd.DataFrame(rows),
                column_config={
                    "Deck": st.column_config.TextColumn("Deck", disabled=True),
                    "Status": st.column_config.TextColumn("Status", disabled=True),
                    "Qty": st.column_config.NumberColumn("Qty", disabled=True, width="small"),
                    "Gets it?": st.column_config.TextColumn("Gets it?", disabled=True),
                    "Keep": st.column_config.CheckboxColumn(
                        "Keep", width="small",
                        help="Tick the one deck that keeps this card, then press Resolve — "
                             "the others are dropped."),
                    "Drop": st.column_config.CheckboxColumn(
                        "Drop", width="small", help="Drop just this row, keeping the rest."),
                },
                column_order=["Deck", "Status", "Qty", "Gets it?", "Keep", "Drop"],
                hide_index=True, width="stretch",
                key=f"workbench_conflict_{group['card']}_{ver}",
            )
            if st.button("Resolve", key=f"workbench_conflict_resolve_{group['card']}_{ver}"):
                keep = [int(r["change_id"]) for _, r in edited.iterrows() if r["Keep"]]
                drop = [int(r["change_id"]) for _, r in edited.iterrows() if r["Drop"]]
                if len(keep) > 1:
                    st.warning("Tick **Keep** on at most one row — that's the point of resolving.")
                elif not keep and not drop:
                    st.warning("Tick **Keep** on the row that wins, or **Drop** on the ones that don't.")
                else:
                    # The keep-drops-the-rest rule lives in writes, not here
                    # — see writes.resolve_contention().
                    try:
                        outcome = writes.resolve_contention(
                            conn, [int(r["change_id"]) for _, r in edited.iterrows()],
                            keep_change_id=keep[0] if keep else None,
                            drop_change_ids=drop,
                        )
                    except ValueError as exc:
                        st.warning(str(exc))
                    else:
                        lines = [f"❌ change #{cid}: {msg}" for cid, msg in outcome["errors"]]
                        lines.insert(0, f"✅ Dropped {len(outcome['dropped'])} competing "
                                        f"row(s) for {group['card']}.")
                        st.session_state[RESULT_KEY] = lines
                        _refresh()

# ------------------------------------------------------------------
# Planned changes, by deck — the same table the Shortlist tab shows,
# with the queue-aware verdict instead of the per-row one.
# ------------------------------------------------------------------
st.header(f"📋 Planned changes ({len(planned_df)})")
if planned_df.empty:
    st.caption("Nothing staged. Promote an idea on a deck's **Shortlist** tab to stage it.")
else:
    blocked = sum(
        1 for cid in planned_df["change_id"]
        if availability.get(int(cid), {}).get("verdict") in ("claimed_by", "not_owned", "in_other_deck")
    )
    if blocked:
        st.caption(
            f"{blocked} of these can't be sourced from storage right now — they'll buy a copy "
            "(creating a starter collection lot) or need a card unsleeved first."
        )
    for deck_id, deck_rows in planned_df.groupby("deck_id", sort=False):
        deck_name = deck_rows["deck_name"].iloc[0]
        with st.expander(f"{deck_name} ({len(deck_rows)})"):
            table = [{
                "change_id": int(r["change_id"]),
                "Add": r["add_card"] if isinstance(r["add_card"], str) else "",
                "Availability": _verdict_cell(int(r["change_id"])),
                "Replaces": r["remove_card"] if isinstance(r["remove_card"], str) else "",
                "Qty": int(r["quantity"]),
                "Apply": False,
                "← Idea": False,
                "Drop": False,
            } for _, r in deck_rows.iterrows()]
            edited = st.data_editor(
                pd.DataFrame(table),
                column_config={
                    "Add": st.column_config.TextColumn("Add", disabled=True),
                    "Availability": st.column_config.TextColumn("Availability", disabled=True, width="large"),
                    "Replaces": st.column_config.TextColumn("Replaces", disabled=True),
                    "Qty": st.column_config.NumberColumn("Qty", disabled=True, width="small"),
                    "Apply": st.column_config.CheckboxColumn("Apply", width="small"),
                    "← Idea": st.column_config.CheckboxColumn(
                        "← Idea", width="small", help="Un-stage it, keeping what it replaces."),
                    "Drop": st.column_config.CheckboxColumn("Drop", width="small"),
                },
                column_order=["Add", "Availability", "Replaces", "Qty", "Apply", "← Idea", "Drop"],
                hide_index=True, width="stretch",
                key=f"workbench_planned_{deck_id}_{ver}",
            )
            # Qty is read-only here on purpose: it changes how many cards
            # the swap cuts, which wants the Shortlist tab's mainboard
            # context (and its card-count warning) to decide safely.
            b1, b2, b3 = st.columns(3)
            if b1.button("✅ Apply checked", key=f"workbench_apply_checked_{deck_id}_{ver}"):
                checked = [int(r["change_id"]) for _, r in edited.iterrows() if r["Apply"]]
                if checked:
                    _apply(checked)
                else:
                    st.warning("Tick **Apply** on at least one row first.")
            if b2.button("✅ Apply all for this deck", key=f"workbench_apply_all_{deck_id}_{ver}"):
                _apply([int(r["change_id"]) for _, r in edited.iterrows()])
            if b3.button("💾 Run checked ← Idea / Drop", key=f"workbench_planned_save_{deck_id}_{ver}"):
                lines, demoted, dropped = [], 0, 0
                for _, r in edited.iterrows():
                    cid = int(r["change_id"])
                    try:
                        if r["Drop"]:
                            writes.drop_change(conn, cid)
                            dropped += 1
                        elif r["← Idea"]:
                            writes.demote_change(conn, cid)
                            demoted += 1
                    except ValueError as exc:
                        lines.append(f"❌ {r['Add']}: {exc}")
                lines.insert(0, f"✅ Moved {demoted} back to ideas, dropped {dropped}.")
                st.session_state[RESULT_KEY] = lines
                _refresh()

# ------------------------------------------------------------------
# Ideas flagged for review — the ones you marked "decide on this",
# gathered from every deck so the decisions happen in one sitting.
# ------------------------------------------------------------------
flagged = ideas_df[ideas_df["review_flag"] == 1] if not ideas_df.empty else ideas_df
st.header(f"🚩 Ideas needing a decision ({len(flagged)})")
if flagged.empty:
    st.caption(
        "No ideas are flagged. Tick **Review** on an idea in a deck's Shortlist tab to "
        "gather it here."
    )
else:
    st.caption(
        "Promoting and pairing happen on the deck's own Shortlist tab, where the mainboard "
        "is there to pick a card to cut from. Drop and un-flag can be done here."
    )
    table = [{
        "change_id": int(r["change_id"]),
        "Deck": r["deck_name"],
        "Card": r["add_card"] if isinstance(r["add_card"], str) else "",
        "Availability": _verdict_cell(int(r["change_id"])),
        "Notes": r["notes"] if isinstance(r["notes"], str) else "",
        "Un-flag": False,
        "Drop": False,
    } for _, r in flagged.sort_values(["deck_name", "add_card"]).iterrows()]
    flagged_edited = st.data_editor(
        pd.DataFrame(table),
        column_config={
            "Deck": st.column_config.TextColumn("Deck", disabled=True),
            "Card": st.column_config.TextColumn("Card", disabled=True),
            "Availability": st.column_config.TextColumn("Availability", disabled=True, width="large"),
            "Notes": st.column_config.TextColumn("Notes", disabled=True, width="medium"),
            "Un-flag": st.column_config.CheckboxColumn(
                "Un-flag", width="small", help="Clear the review flag, keeping the idea."),
            "Drop": st.column_config.CheckboxColumn("Drop", width="small"),
        },
        column_order=["Deck", "Card", "Availability", "Notes", "Un-flag", "Drop"],
        hide_index=True, width="stretch",
        key=f"workbench_flagged_{ver}",
    )
    if st.button("💾 Run checked actions", key=f"workbench_flagged_save_{ver}"):
        lines, cleared, dropped = [], 0, 0
        for _, r in flagged_edited.iterrows():
            cid = int(r["change_id"])
            try:
                if r["Drop"]:
                    writes.drop_change(conn, cid)
                    dropped += 1
                elif r["Un-flag"]:
                    writes.update_change(conn, cid, review_flag=0)
                    cleared += 1
            except ValueError as exc:
                lines.append(f"❌ {r['Card']}: {exc}")
        lines.insert(0, f"✅ Cleared {cleared} flag(s), dropped {dropped}.")
        st.session_state[RESULT_KEY] = lines
        _refresh()

# ------------------------------------------------------------------
# Contention (Phase 3) — demand vs. supply per card, counting mainboard
# usage in deck_cards as well as the shortlist queue, so a card with no
# idea or planned row at all (just over-subscribed mainboards, e.g. War
# Room in 4 decks on 3 copies) still surfaces here. The per-row shortlist
# check above can't see this; it only ever looks at open deck_changes.
# ------------------------------------------------------------------
st.header(f"📊 Contention ({len(contention)})")
if not contention:
    st.success("No card is shorter than its total demand across mainboards and shortlists.")
else:
    st.caption(
        "Demand = decks running this card mainboard + decks with an open idea/planned add "
        "for it. Shortfall = demand minus copies owned. Buying the shortfall is already "
        "reflected in the Buy list below; dropping it from a deck here removes it from that "
        "deck's mainboard immediately (logged to its history like any hand edit)."
    )
    for row in contention:
        with st.container(border=True):
            mb = ", ".join(row["mainboard_decks"]) or "—"
            sl = ", ".join(row["shortlist_decks"]) or "—"
            st.markdown(
                f"**{row['card']}** — {row['owned_qty']} owned, {row['demand']} wanted "
                f"(short {row['shortfall']})"
            )
            st.caption(f"Mainboard: {mb}  ·  Shortlist: {sl}")
            if row["mainboard_deck_ids"]:
                dc1, dc2 = st.columns([3, 1])
                drop_choice = dc1.selectbox(
                    "Drop from deck", list(row["mainboard_deck_ids"].values()),
                    key=f"workbench_contention_drop_pick_{row['oracle_id']}_{ver}",
                    label_visibility="collapsed",
                )
                if dc2.button("Drop", key=f"workbench_contention_drop_btn_{row['oracle_id']}_{ver}"):
                    deck_id = next(
                        d for d, n in row["mainboard_deck_ids"].items() if n == drop_choice
                    )
                    sid = q.deck_card_scryfall_id_for_oracle(conn, deck_id, row["oracle_id"])
                    if sid:
                        writes.remove_deck_card(conn, deck_id, sid)
                        st.session_state[RESULT_KEY] = [
                            f"✅ Dropped {row['card']} from {drop_choice}."
                        ]
                        _refresh()

# ------------------------------------------------------------------
# Buy list (Phase 3) — unowned ideas, deduplicated across decks, plus one
# row per contention shortfall above.
# ------------------------------------------------------------------
st.header(f"🛒 Buy list ({len(buy['rows'])}) — ${buy['total']:,.2f}")
if not buy["rows"]:
    st.caption("Nothing to buy — every open idea is already owned, and nothing is short.")
else:
    st.caption(
        "Ideas with zero owned copies (grouped across decks, priced once) plus contention "
        "rows where buying the shortfall resolves it."
    )
    buy_table = pd.DataFrame([{
        "Card": r["card"],
        "Qty": r["quantity"],
        "Price": r["price"],
        "Subtotal": r["subtotal"],
        "Why": "Idea (unowned)" if r["reason"] == "idea" else "Contention shortfall",
        "Decks": ", ".join(r["decks"]),
    } for r in buy["rows"]])
    st.dataframe(
        buy_table,
        column_config={
            "Price": st.column_config.NumberColumn("Price", format="$%.2f"),
            "Subtotal": st.column_config.NumberColumn("Subtotal", format="$%.2f"),
        },
        hide_index=True, width="stretch",
    )
    st.download_button(
        "⬇️ Export as text", data=mox.export_buy_list_text(buy),
        file_name="buy_list.txt", mime="text/plain",
        key=f"workbench_buy_list_export_{ver}",
    )

# ------------------------------------------------------------------
# Reconcile (Phase 4) — the physical gaps: lots with no location, and each
# deck's list vs. the lots actually sleeved in it. Uncached like the
# sections above, since every action here edits the collection.
# ------------------------------------------------------------------
st.header("🧹 Reconcile")
locations = q.location_options(conn)

# --- 1. Unlocated lots ---------------------------------------------
unloc = q.unlocated_lots(conn)
st.subheader(f"📍 Unlocated lots ({len(unloc)}) — ${unlocated['value']:,.2f}")
if not unloc:
    st.success("Every lot has a location.")
else:
    st.caption(
        "Lots with no Location, most valuable first. Pick a Location per row, or tick rows and "
        "assign them all at once — these likely came in as a batch."
    )
    unloc_df = pd.DataFrame([{
        "collection_id": r["collection_id"], "Card": r["card"], "Set": r["set_code"].upper(),
        "Qty": r["quantity"], "Foil": r["foil"], "Value": r["value"], "Source": r["source"],
        "Location": None, "Select": False,
    } for r in unloc])
    unloc_edited = st.data_editor(
        unloc_df,
        column_config={
            "Card": st.column_config.TextColumn("Card", disabled=True),
            "Set": st.column_config.TextColumn("Set", disabled=True, width="small"),
            "Qty": st.column_config.NumberColumn("Qty", disabled=True, width="small"),
            "Foil": st.column_config.CheckboxColumn("Foil", disabled=True, width="small"),
            "Value": st.column_config.NumberColumn("Value", format="$%.2f", disabled=True),
            "Source": st.column_config.TextColumn("Source", disabled=True),
            "Location": st.column_config.SelectboxColumn("Location", options=locations),
            "Select": st.column_config.CheckboxColumn("Select", width="small"),
        },
        column_order=["Card", "Set", "Qty", "Foil", "Value", "Source", "Location", "Select"],
        hide_index=True, width="stretch", height=400,
        key=f"workbench_unlocated_{ver}",
    )
    u1, u2, u3 = st.columns([2, 1, 1])
    bulk_loc = u1.selectbox("Assign selected to", locations, index=None,
                            placeholder="Choose a location…", key=f"workbench_unloc_bulk_{ver}")
    if u2.button("Assign selected", key=f"workbench_unloc_assign_{ver}"):
        picked = [int(r["collection_id"]) for _, r in unloc_edited.iterrows() if r["Select"]]
        if not picked or not bulk_loc:
            st.warning("Tick **Select** on at least one row and choose a location first.")
        else:
            writes.bulk_update_collection(conn, {cid: {"location": bulk_loc} for cid in picked})
            st.session_state[RESULT_KEY] = [f"✅ Assigned {len(picked)} lot(s) to {bulk_loc}."]
            _refresh(collection=True)
    if u3.button("💾 Save row locations", key=f"workbench_unloc_save_{ver}"):
        edits = {int(r["collection_id"]): {"location": r["Location"]}
                 for _, r in unloc_edited.iterrows() if isinstance(r["Location"], str) and r["Location"]}
        if not edits:
            st.warning("Choose a **Location** on at least one row first.")
        else:
            writes.bulk_update_collection(conn, edits)
            st.session_state[RESULT_KEY] = [f"✅ Located {len(edits)} lot(s)."]
            _refresh(collection=True)

# --- 2 + 3. Per-deck list vs. physical, with build status ----------
recon = q.deck_reconciliation(conn)
st.subheader("📦 Decks: list vs. physical")
st.caption(
    "**To pull** = in the mainboard but no copy is sleeved in the deck. **To put away** = a copy "
    "is sleeved in the deck that the mainboard no longer runs. Basics are ignored. Build % is the "
    "share of non-basic mainboard cards physically in the deck."
)
for deck in recon:
    b = deck["build"]
    pct = "—" if b["pct"] is None else f"{b['pct']:.0f}%"
    label = (f"{deck['deck_name']} — built {pct} ({b['located']}/{b['needed']}) · "
             f"{len(deck['to_pull'])} to pull · {len(deck['to_put_away'])} to put away")
    gaps = deck["to_pull"] or deck["to_put_away"]
    with st.expander(label, expanded=False):
        if not gaps:
            st.success("List and physical deck agree.")
            continue
        dk = deck["deck_id"]
        if deck["to_pull"]:
            st.markdown("**To pull**")
        for row in deck["to_pull"]:
            c1, c2, c3, c4 = st.columns([3, 4, 2, 2])
            c1.markdown(f"{row['card']} ×{row['short']}")
            opts = {
                f"#{c['collection_id']} · ×{c['quantity']} · "
                f"{c['location'] or '(no location)'}{' (in a deck)' if c['in_deck'] else ''}": c
                for c in row["candidates"]
            }
            if not opts:
                c2.caption("No owned copy — see the Buy list.")
                continue
            choice = c2.selectbox("Copy", list(opts), key=f"wb_pull_pick_{dk}_{row['oracle_id']}_{ver}",
                                  label_visibility="collapsed")
            if c3.button("Sleeved here", key=f"wb_pull_{dk}_{row['oracle_id']}_{ver}"):
                lot = opts[choice]
                writes.move_lot(conn, lot["collection_id"], deck["deck_name"],
                                quantity=min(row["short"], lot["quantity"]))
                st.session_state[RESULT_KEY] = [f"✅ {row['card']} located to {deck['deck_name']}."]
                _refresh(collection=True)
            c4.caption("It's elsewhere → set its Location on the Collection page.")
        if deck["to_put_away"]:
            st.markdown("**To put away**")
        for row in deck["to_put_away"]:
            c1, c2, c3 = st.columns([3, 4, 2])
            c1.markdown(f"{row['card']} ×{row['surplus']}")
            dest = c2.selectbox("Put away in", locations,
                                index=locations.index(writes.DEFAULT_LOCATION)
                                if writes.DEFAULT_LOCATION in locations else None,
                                key=f"wb_away_pick_{dk}_{row['oracle_id']}_{ver}",
                                label_visibility="collapsed")
            if c3.button("Put away", key=f"wb_away_{dk}_{row['oracle_id']}_{ver}") and dest:
                remaining = row["surplus"]
                for lot in row["lots"]:
                    if remaining <= 0:
                        break
                    take = min(remaining, lot["quantity"])
                    writes.move_lot(conn, lot["collection_id"], dest, quantity=take)
                    remaining -= take
                st.session_state[RESULT_KEY] = [f"✅ {row['card']} put away in {dest}."]
                _refresh(collection=True)
