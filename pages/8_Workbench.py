"""
Workbench page — every deck's pending shortlist work in one place
(Workbench rework, Phase 2; see docs/archive/IMPLEMENTATION_PLAN.md).

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

UI plan Phase 4: each deck's planned-changes block is an st.fragment
(_planned_block), so ticking its checkboxes doesn't recompute the whole
page; every write still ends in a full-app rerun via _refresh().

EDIT_FLOW_PLAN.md Phase 1: applying, the contention "Drop", and
Reconcile's "Sleeved here" / "Put away" all report the copies they moved
through the shared pop-up (card_view.show_moves).

EDIT_FLOW_PLAN.md Phase 6: assigning unlocated lots to a Location can put
copies into a deck's sleeves, so those saves ask whether the deck's list
should follow (card_view.show_deck_sync). Reconcile's own moves are logged
as "reconcile" in the move log.

EDIT_FLOW_PLAN.md Phase 3: a card taken from another deck is a linked pair
(a removal on the donor, an add on the receiver). Planned changes shows it
as ONE row — "Sol Ring: Gisa → Prosper", under the receiving deck — counted
once in the header, applied or dropped together
(queries.collapse_linked_pairs). A Conflicts group counts a take's promised
copy as supply.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import streamlit as st

from dashboard_lib import db, loaders, writes, card_resolver, formatting as fmt, queries as q
from dashboard_lib import card_view as cv, nav
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
ver = st.session_state.get(VER_KEY, 0)


def _refresh(lines, collection=False, moves=None, deck_sync=None):
    """Bump the editor version, then finish the write (EDIT_FLOW_PLAN.md
    Phase 0): `lines` are "✅ ..." / "❌ ..." outcomes; `moves` are the
    copies the write moved (Phase 1), named in the pop-up; `deck_sync` are
    the list changes a collection-side save could make (Phase 6) —
    finish_edit() asks about them. It reruns the whole app even from a
    fragment (_planned_block), since a write changes sections outside it."""
    st.session_state[VER_KEY] = ver + 1
    cv.finish_edit(cv.result_from_lines(lines, collection=collection, moves=moves or [],
                                        deck_sync=deck_sync or []))


def _apply(change_ids, label="Applied"):
    results = writes.apply_changes(
        conn, change_ids,
        resolve_add=lambda name: card_resolver.resolve_or_fetch_card(conn, name=name),
    )
    lines = [fmt.apply_result_line(r, label) for r in results]
    _refresh(lines, moves=[m for r in results for m in r["moves"]])


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
# One entry per planned change, a take from another deck (a linked pair of
# rows) counting once — see queries.collapse_linked_pairs().
planned_entries = q.collapse_linked_pairs(planned_df) if not planned_df.empty else []
unlocated = loaders.load_unlocated_lot_summary(conn)
# Not cached, like plan_availability above: contention counts mainboard
# usage too, so an apply or a hand edit made earlier in this same page run
# must be reflected immediately, not after the next cache-clearing rerun.
contention = q.contention_table(conn)
buy = q.buy_list(conn, contention=contention)

deck_count = len({e["group_deck_id"] for e in planned_entries})
st.caption(
    f"**{len(planned_entries)}** planned change(s) across **{deck_count}** deck(s) · "
    f"**{len(conflicts)}** conflict(s) · "
    f"**{len(contention)}** card(s) in contention · "
    f"**{unlocated['lots']}** lot(s) with no location (${unlocated['value']:,.0f})"
)

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
        "available": "✅", "claimed_by": "⚠️", "in_other_deck": "📦", "wrong_location": "📍",
        "not_owned": "🛒", "already_here": "↩️", "no_add": "—",
    }.get(verdict.get("verdict"), "")
    return f"{icon} {verdict.get('text', '')}".strip()


# ------------------------------------------------------------------
# Conflicts — first, because these are the rows that cannot all succeed.
# Everything below this is just a to-do list.
# ------------------------------------------------------------------
st.header(
    f"⚠️ Conflicts ({len(conflicts)})",
    help=(
        "Two or more open changes want one card, and between them want more copies than "
        "are free in storage. Applying them in order gives the card to the earliest; the "
        "rest will fail or buy a copy. Pick which deck keeps it and drop the others."
    ),
)
if not conflicts:
    st.success("No two plans want the same physical card. Everything below can be applied as it stands.")
else:
    for group in conflicts:
        free = group["available_qty"] or 0
        with st.container(border=True):
            promised = (f", {group['promised_qty']} promised from another deck"
                        if group.get("promised_qty") else "")
            st.markdown(
                f"**{group['card']}** — {group['owned_qty']} owned, {free} free in storage{promised}, "
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
                        _refresh(lines)

def _entry_cells(entry):
    """(Add, Availability, Replaces) for one planned_entries item. A take
    from another deck reads "Sol Ring: Gisa → Prosper", with the receiving
    row's verdict — or, when that row isn't planned, why Apply waits or
    what it will do."""
    r, pair = entry["row"], entry["pair"]
    replaces = r["remove_card"] if isinstance(r.get("remove_card"), str) and not pair else ""
    if not pair:
        return (r["add_card"] if isinstance(r.get("add_card"), str) else "",
                _verdict_cell(entry["change_id"]), replaces)
    label = f"{pair['card']}: {pair['from_deck']} → {pair['to_deck']}"
    if q.is_donor_row(r):
        if pair["receiver_status"] == "idea":
            return label, f"💡 Still an idea on {pair['to_deck']} — plan it there to apply", ""
        return label, (f"📦 On {pair['to_deck']}'s list already — applying moves {pair['from_deck']}'s "
                       "copy over"), ""
    cut = r["remove_card"] if isinstance(r.get("remove_card"), str) else ""
    return label, _verdict_cell(entry["change_id"]), cut


@st.fragment
def _planned_block(deck_id, deck_name, entries):
    """One deck's planned changes: editor + Apply / ← Idea / Drop buttons.
    A fragment (UI plan Phase 4), so ticking checkboxes reruns only this
    block instead of the whole page (which recomputes the allocator,
    contention and the buy list). Every write ends in _refresh(), a
    full-app rerun, since it changes the conflicts, counts and other
    decks' verdicts outside this block.

    `entries` are queries.collapse_linked_pairs() items grouped under this
    deck: a take from another deck is one row whose actions name either
    half, and writes applies / drops the pair (EDIT_FLOW_PLAN.md Phase 3)."""
    with st.expander(f"{deck_name} ({len(entries)})"):
        table = []
        for e in entries:
            add, availability_text, replaces = _entry_cells(e)
            table.append({
                "change_id": e["change_id"],
                "Add": add,
                "Availability": availability_text,
                "Replaces": replaces,
                "Qty": int(e["row"]["quantity"]),
                "Apply": False,
                "← Idea": False,
                "Drop": False,
            })
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
                "Drop": st.column_config.CheckboxColumn(
                    "Drop", width="small", help="A take from another deck drops both sides."),
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
                        lines += [f"✅ {line}" for line in writes.drop_change(conn, cid)]
                        dropped += 1
                    elif r["← Idea"]:
                        writes.demote_change(conn, cid)
                        demoted += 1
                except ValueError as exc:
                    lines.append(f"❌ {r['Add']}: {exc}")
            lines.insert(0, f"✅ Moved {demoted} back to ideas, dropped {dropped}.")
            _refresh(lines)


# ------------------------------------------------------------------
# Planned changes, by deck — the same table the Shortlist tab shows,
# with the queue-aware verdict instead of the per-row one. A take from
# another deck is one row, under the deck receiving the card.
# ------------------------------------------------------------------
st.header(f"📋 Planned changes ({len(planned_entries)})")
if not planned_entries:
    nav.render_empty_state(
        "Nothing staged yet. Promote an idea on a deck's **Shortlist** tab to stage it.",
        "Deck_Editor", "✏️ Open the Deck Editor",
    )
else:
    blocked = sum(
        1 for e in planned_entries
        if availability.get(e["change_id"], {}).get("verdict") in (
            "claimed_by", "not_owned", "in_other_deck", "wrong_location")
    )
    if blocked:
        st.caption(
            f"{blocked} of these can't be sourced from storage right now — they'll buy a copy "
            "(creating a starter collection lot) or need a card unsleeved first."
        )
    by_deck = {}
    for e in planned_entries:
        by_deck.setdefault((e["group_deck_id"], e["group_deck_name"]), []).append(e)
    for (group_id, group_name), entries in by_deck.items():
        _planned_block(group_id, group_name, entries)

# ------------------------------------------------------------------
# Ideas flagged for review — the ones you marked "decide on this",
# gathered from every deck so the decisions happen in one sitting.
# ------------------------------------------------------------------
flagged = ideas_df[ideas_df["review_flag"] == 1] if not ideas_df.empty else ideas_df
st.header(f"🚩 Ideas needing a decision ({len(flagged)})")
if flagged.empty:
    nav.render_empty_state(
        "No ideas are flagged. Tick **Review** on an idea in a deck's Shortlist tab to gather it here.",
        "Deck_Editor", "✏️ Open the Deck Editor",
    )
else:
    st.caption("Promote and pair on the deck's own Shortlist tab; drop and un-flag work here.")
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
        _refresh(lines)

# ------------------------------------------------------------------
# Contention (Phase 3) — demand vs. supply per card, counting mainboard
# usage in deck_cards as well as the shortlist queue, so a card with no
# idea or planned row at all (just over-subscribed mainboards, e.g. War
# Room in 4 decks on 3 copies) still surfaces here. The per-row shortlist
# check above can't see this; it only ever looks at open deck_changes.
# ------------------------------------------------------------------
st.header(
    f"📊 Contention ({len(contention)})",
    help=(
        "Demand = decks running this card mainboard + decks with an open idea/planned add "
        "for it. Shortfall = demand minus copies owned. Buying the shortfall is already "
        "reflected in the Buy list below; dropping it from a deck here removes it from that "
        "deck's mainboard immediately (logged to its history like any hand edit) and puts its "
        f"sleeved copy away in {writes.DEFAULT_LOCATION}."
    ),
)
if not contention:
    st.success("No card is shorter than its total demand across mainboards and shortlists.")
else:
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
                        moves = writes.remove_deck_card(conn, deck_id, sid)
                        _refresh([f"✅ Dropped {row['card']} from {drop_choice}."], moves=moves)

# ------------------------------------------------------------------
# Buy list (Phase 3) — unowned ideas, deduplicated across decks, plus one
# row per contention shortfall above.
# ------------------------------------------------------------------
st.header(
    f"🛒 Buy list ({len(buy['rows'])}) — ${buy['total']:,.2f}",
    help=(
        "Ideas with zero owned copies (grouped across decks, priced once) plus contention "
        "rows where buying the shortfall resolves it."
    ),
)
if not buy["rows"]:
    st.caption("Nothing to buy — every open idea is already owned, and nothing is short.")
else:
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
st.subheader(
    f"📍 Unlocated lots ({len(unloc)}) — ${unlocated['value']:,.2f}",
    help=(
        "Lots with no Location, most valuable first. Pick a Location per row, or tick rows and "
        "assign them all at once — these likely came in as a batch."
    ),
)
if not unloc:
    st.success("Every lot has a location.")
else:
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
            mark = q.last_move_id(conn)
            writes.bulk_update_collection(conn, {cid: {"location": bulk_loc} for cid in picked})
            _refresh([f"✅ Assigned {len(picked)} lot(s) to {bulk_loc}."], collection=True,
                     deck_sync=q.deck_sync_offers(conn, mark))
    if u3.button("💾 Save row locations", key=f"workbench_unloc_save_{ver}"):
        edits = {int(r["collection_id"]): {"location": r["Location"]}
                 for _, r in unloc_edited.iterrows() if isinstance(r["Location"], str) and r["Location"]}
        if not edits:
            st.warning("Choose a **Location** on at least one row first.")
        else:
            mark = q.last_move_id(conn)
            writes.bulk_update_collection(conn, edits)
            _refresh([f"✅ Located {len(edits)} lot(s)."], collection=True,
                     deck_sync=q.deck_sync_offers(conn, mark))

# --- 2 + 3. Per-deck list vs. physical, with build status ----------
recon = q.deck_reconciliation(conn)
st.subheader(
    "📦 Decks: list vs. physical",
    help=(
        "**To pull** = in the mainboard but no copy is sleeved in the deck. **To put away** = a copy "
        "is sleeved in the deck that the mainboard no longer runs. Basics are ignored. Build % is the "
        "share of non-basic mainboard cards physically in the deck."
    ),
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
                moves = writes.move_copies(conn, lot["collection_id"], deck["deck_name"],
                                           quantity=min(row["short"], lot["quantity"]))
                _refresh([f"✅ {row['card']} located to {deck['deck_name']}."], moves=moves)
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
                remaining, moves = row["surplus"], []
                for lot in row["lots"]:
                    if remaining <= 0:
                        break
                    take = min(remaining, lot["quantity"])
                    moves += writes.move_copies(conn, lot["collection_id"], dest, quantity=take)
                    remaining -= take
                _refresh([f"✅ {row['card']} put away in {dest}."], moves=moves)
