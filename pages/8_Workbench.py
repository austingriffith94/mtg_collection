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

Layout: one section selector (a segmented control, so only the chosen
section renders, and it survives the rerun after a write because its
values are stable keys while its labels carry live counts):

  📋 Shortlist      Conflicts, planned changes by deck, flagged ideas.
  🛒 Supply & buy   Contention (demand vs. supply, counting mainboard
                    usage too — not just the queue) and the Buy list.
  📦 Physical       Unlocated lots, then every deck's list-vs-physical gap
                    in ONE cross-deck table you fix from directly
                    (queries.location_mismatches over deck_reconciliation;
                    writes.fix_location_mismatch), with build status per
                    deck. This replaced the per-deck Reconcile expanders,
                    which showed the same gaps one deck at a time.
  🏷️ Tags           Untagged mainboard cards, one deck at a time, with a
                    dropdown of the tags that deck already uses.

UI plan Phase 4: each deck's planned-changes block is an st.fragment
(_planned_block), so ticking its checkboxes doesn't recompute the whole
page; every write still ends in a full-app rerun via _refresh().

docs/archive/EDIT_FLOW_PLAN.md Phase 1: applying, the contention "Drop", and
the Physical fixes all report the copies they moved through the shared
pop-up (card_view.show_moves).

docs/archive/EDIT_FLOW_PLAN.md Phase 6: assigning unlocated lots to a Location can put
copies into a deck's sleeves, so those saves ask whether the deck's list
should follow (card_view.show_deck_sync). The Physical fixes are logged as
"reconcile" in the move log.

docs/archive/EDIT_FLOW_PLAN.md Phase 3: a card taken from another deck is a linked pair
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
    """Bump the editor version, then finish the write (docs/archive/EDIT_FLOW_PLAN.md
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
# Everything the section counts need, loaded once. The queue is planned
# rows plus open ideas, allocated in ONE pass, because an idea competes
# for a physical card just as much as a staged swap does.
# plan_availability() walks them in (planned_at, change_id) order, so a
# planned swap staged last month outranks an idea jotted yesterday. None
# of this is cached: contention and the physical diff count mainboard and
# lot state, so a write earlier in this run must show immediately.
# ------------------------------------------------------------------
open_df = loaders.load_open_changes_df(conn, ("planned", "idea"))
planned_df = open_df[open_df["status"] == "planned"] if not open_df.empty else open_df
ideas_df = open_df[open_df["status"] == "idea"] if not open_df.empty else open_df
flagged = ideas_df[ideas_df["review_flag"] == 1] if not ideas_df.empty else ideas_df

availability = q.plan_availability(conn, open_df)
conflicts = q.plan_conflicts(conn, open_df, availability)
# One entry per planned change, a take from another deck (a linked pair of
# rows) counting once — see queries.collapse_linked_pairs().
planned_entries = q.collapse_linked_pairs(planned_df) if not planned_df.empty else []
unlocated = loaders.load_unlocated_lot_summary(conn)
contention = q.contention_table(conn)
buy = q.buy_list(conn, contention=contention)
recon = q.deck_reconciliation(conn)
mismatches = q.location_mismatches(conn, recon)
untagged = q.untagged_deck_cards(conn)

# ------------------------------------------------------------------
# Section selector. Values are stable keys and the counts live in
# format_func, so the chosen section survives a write's rerun even though
# its label changes.
# ------------------------------------------------------------------
SECTIONS = {
    "plan": lambda: (f"📋 Shortlist · {len(planned_entries) + len(flagged)}"
                     + (f" · ⚠️ {len(conflicts)}" if conflicts else "")),
    "supply": lambda: f"🛒 Supply & buy · {len(buy['rows'])}",
    "physical": lambda: f"📦 Physical · {unlocated['lots'] + len(mismatches)}",
    "tags": lambda: f"🏷️ Tags · {len(untagged)}",
}
view = st.segmented_control(
    "Section", list(SECTIONS), format_func=lambda k: SECTIONS[k](), default="plan",
    required=True, key="workbench_view", label_visibility="collapsed",
)


def _verdict_cell(change_id):
    """The Availability column: the allocator's one-line verdict, prefixed
    so the state is readable at a glance in a dense table."""
    verdict = availability.get(change_id, {})
    icon = {
        "available": "✅", "claimed_by": "⚠️", "in_other_deck": "📦", "wrong_location": "📍",
        "not_owned": "🛒", "buying": "💳", "already_here": "↩️", "no_add": "—",
    }.get(verdict.get("verdict"), "")
    return f"{icon} {verdict.get('text', '')}".strip()


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
    half, and writes applies / drops the pair (docs/archive/EDIT_FLOW_PLAN.md Phase 3)."""
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


# ==================================================================
# 📋 Shortlist — conflicts, planned changes, flagged ideas
# ==================================================================
def _render_plan():
    # Conflicts first: the only rows that can't all succeed. Shown only
    # when there are some — an empty "no conflicts" box is just noise.
    if conflicts:
        st.subheader(
            f"⚠️ Conflicts ({len(conflicts)})",
            help=(
                "Two or more open changes want one card, and between them want more copies than "
                "are free in storage. Applying them in order gives the card to the earliest; the "
                "rest will fail or buy a copy. Pick which deck keeps it and drop the others."
            ),
        )
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

    # Planned changes, by deck — the Shortlist tab's table with the
    # queue-aware verdict. A take from another deck is one row, under the
    # deck receiving the card.
    st.subheader(f"📋 Planned changes ({len(planned_entries)})")
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

    # Ideas flagged for review, gathered from every deck so the decisions
    # happen in one sitting.
    st.subheader(f"🚩 Ideas needing a decision ({len(flagged)})")
    if flagged.empty:
        st.caption("None flagged. Tick **Review** on an idea in a deck's Shortlist tab to gather it here.")
        return
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


# ==================================================================
# 🛒 Supply & buy — contention, then the buy list it feeds
# ==================================================================
def _render_supply():
    # Contention (Phase 3) counts mainboard usage in deck_cards as well as
    # the shortlist queue, so an over-subscribed card with no idea or
    # planned row at all (War Room in 4 decks on 3 copies) still shows.
    st.subheader(
        f"📊 Contention ({len(contention)})",
        help=(
            "Demand = decks running this card mainboard + decks with an open idea/planned add "
            "for it. Shortfall = demand minus copies owned. Buying the shortfall is already "
            "reflected in the Buy list below; dropping it from a deck here removes it from that "
            "deck's mainboard immediately (logged to its history like any hand edit) and puts its "
            f"sleeved copy away in {writes.DEFAULT_LOCATION} ({writes.LANDS_LOCATION} for a land)."
        ),
    )
    if not contention:
        st.caption("✅ No card is shorter than its total demand across mainboards and shortlists.")
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

    # Buy list (Phase 3, Phase 4) — one row per card, grouped across decks
    # and priced once: cards already on a deck's list from a Buy add
    # (awaiting), open rows marked Buy (chosen), contention shortfalls, and
    # unowned ideas. A card with several reasons shows once, under the most
    # specific.
    st.subheader(
        f"🛒 Buy list ({len(buy['rows'])}) — ${buy['total']:,.2f}",
        help=(
            "Cards on a deck's list that you chose to buy and don't have a copy of yet, shortlist rows "
            "marked Buy, ideas with zero owned copies (grouped across decks, priced once), and contention "
            "rows where buying the shortfall resolves it."
        ),
    )
    if not buy["rows"]:
        st.caption("Nothing to buy — nothing is marked Buy, every open idea is already owned, and nothing is short.")
        return
    buy_table = pd.DataFrame([{
        "Card": r["card"],
        "Qty": r["quantity"],
        "Price": r["price"],
        "Subtotal": r["subtotal"],
        "Why": fmt.BUY_REASON_LABELS[r["reason"]],
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

    # "Bought": the copy arrived — sleeve it straight into the deck that
    # wanted it (a contention shortfall belongs to no one deck, so it has no
    # button: mark one of its ideas Buy first). For an idea that isn't on its
    # deck's list yet, the copy sits sleeved there until the idea is applied.
    purchases = [(r, t) for r in buy["rows"] for t in r["targets"]]
    if purchases:
        with st.expander(f"✅ Bought something? Sleeve it into its deck ({len(purchases)})"):
            st.caption(
                "Enter what you paid per copy (optional) and press Bought: a collection copy is made in "
                "that deck's sleeves. An order import can do this for you — see Collection Editor."
            )
            for r, t in purchases:
                tkey = f"{t['deck_id']}_{t['scryfall_id']}_{t['change_id']}_{ver}"
                c_label, c_qty, c_price, c_btn = st.columns([4, 1, 1, 1])
                c_label.markdown(f"**{r['card']}** → {t['deck_name']} · {fmt.BUY_REASON_LABELS[t['kind']].lower()}")
                got = c_qty.number_input("Qty", min_value=1, value=int(t["quantity"]), step=1,
                                         key=f"workbench_bought_qty_{tkey}", label_visibility="collapsed")
                paid = c_price.number_input("Paid each", min_value=0.0, value=float(r["price"] or 0.0), step=0.25,
                                            format="%.2f", key=f"workbench_bought_price_{tkey}",
                                            label_visibility="collapsed")
                if c_btn.button("Bought", key=f"workbench_bought_btn_{tkey}"):
                    try:
                        moves = writes.record_purchase(conn, t["deck_id"], t["scryfall_id"], int(got),
                                                       price_paid=paid or None, change_id=t["change_id"])
                    except ValueError as exc:
                        st.error(str(exc))
                    else:
                        _refresh([f"✅ Sleeved {r['card']} ×{int(got)} into {t['deck_name']}."], moves=moves)


# ==================================================================
# 📦 Physical — unlocated lots, then every deck's list vs. its sleeves
# ==================================================================
def _render_physical():
    locations = q.location_options(conn)

    # --- Unlocated lots ------------------------------------------------
    unloc = q.unlocated_lots(conn)
    st.subheader(
        f"📍 Unlocated lots ({len(unloc)}) — ${unlocated['value']:,.2f}",
        help=(
            "Lots with no Location, most valuable first. Pick a Location per row, and/or tick rows "
            "and choose one Location for all of them — these likely came in as a batch."
        ),
    )
    if not unloc:
        st.caption("✅ Every lot has a location.")
    else:
        unloc_edited = st.data_editor(
            pd.DataFrame([{
                "collection_id": r["collection_id"], "Card": r["card"], "Set": r["set_code"].upper(),
                "Qty": r["quantity"], "Foil": r["foil"], "Value": r["value"], "Source": r["source"],
                "Location": None, "Select": False,
            } for r in unloc]),
            column_config={
                "Card": st.column_config.TextColumn("Card", disabled=True),
                "Set": st.column_config.TextColumn("Set", disabled=True, width="small"),
                "Qty": st.column_config.NumberColumn("Qty", disabled=True, width="small"),
                "Foil": st.column_config.CheckboxColumn("Foil", disabled=True, width="small"),
                "Value": st.column_config.NumberColumn("Value", format="$%.2f", disabled=True),
                "Source": st.column_config.TextColumn("Source", disabled=True),
                "Location": st.column_config.SelectboxColumn("Location", options=locations),
                "Select": st.column_config.CheckboxColumn(
                    "Select", width="small", help="Gets the location chosen below the table."),
            },
            column_order=["Card", "Set", "Qty", "Foil", "Value", "Source", "Location", "Select"],
            hide_index=True, width="stretch", height=350,
            key=f"workbench_unlocated_{ver}",
        )
        u1, u2 = st.columns([3, 1], vertical_alignment="bottom")
        bulk_loc = u1.selectbox("Selected rows go to", locations, index=None,
                                placeholder="Choose a location…", key=f"workbench_unloc_bulk_{ver}")
        if u2.button("💾 Save locations", key=f"workbench_unloc_save_{ver}", width="stretch"):
            # A row's own Location wins; a ticked row without one takes the bulk pick.
            edits = {}
            for _, r in unloc_edited.iterrows():
                loc = r["Location"] if isinstance(r["Location"], str) and r["Location"] else (
                    bulk_loc if r["Select"] else None)
                if loc:
                    edits[int(r["collection_id"])] = {"location": loc}
            if not edits:
                st.warning("Pick a **Location** on a row, or tick **Select** and choose one above.")
            else:
                mark = q.last_move_id(conn)
                writes.bulk_update_collection(conn, edits)
                _refresh([f"✅ Located {len(edits)} lot(s)."], collection=True,
                         deck_sync=q.deck_sync_offers(conn, mark))

    # --- Decks: list vs. physical --------------------------------------
    st.subheader(
        f"📦 Decks: list vs. physical ({len(mismatches)})",
        help=(
            "**Missing** = in the mainboard but no copy is sleeved in the deck. **Extra** = a copy is "
            "sleeved in the deck that the mainboard doesn't run. Basics are ignored. Fixing moves "
            "copies only — no deck list changes."
        ),
    )
    with st.expander("🏗️ Build status by deck"):
        st.dataframe(
            pd.DataFrame([{
                "Deck": d["deck_name"],
                "Built": d["build"]["pct"],
                "Sleeved": f"{d['build']['located']}/{d['build']['needed']}",
                "Missing": len(d["to_pull"]),
                "Extra": len(d["to_put_away"]),
            } for d in sorted(recon, key=lambda d: (d["build"]["pct"] is None, d["build"]["pct"] or 0))]),
            column_config={
                "Built": st.column_config.ProgressColumn("Built", format="%.0f%%", min_value=0, max_value=100),
            },
            hide_index=True, width="stretch",
        )
    if not mismatches:
        st.caption("✅ Every deck's list matches what's sleeved in it.")
        return

    f1, f2 = st.columns([3, 1], vertical_alignment="bottom")
    deck_pick = f1.multiselect("Decks", sorted({m["deck_name"] for m in mismatches}, key=str.casefold),
                               placeholder="All decks", key="workbench_mismatch_decks")
    show_inactive = f2.toggle("Brewing / dismantled", value=False, key="workbench_mismatch_inactive",
                              help="Their cards are expected to be elsewhere, so they're hidden by default.")
    rows = [m for m in mismatches if (show_inactive or not m["build_state"])
            and (not deck_pick or m["deck_name"] in deck_pick)]
    if not rows:
        st.caption("Nothing for these decks.")
        return
    st.caption("Tick **Fix** and save. *Missing* pulls copies in the order listed (storage before "
               "another deck — taking one leaves that deck missing it); *Extra* goes to **Put away in** "
               f"— blank picks Lands for lands, else {writes.DEFAULT_LOCATION}. To use a specific copy, "
               "set its Location on the Collection page.")
    deck_names = q.deck_name_set(conn)
    # The editor keeps ticks by ROW POSITION, so the filter is part of its
    # key: changing it must start a fresh table, not replay ticks onto the
    # rows that slid into those positions.
    filter_key = abs(hash((tuple(sorted(deck_pick)), show_inactive)))
    mismatch_edited = st.data_editor(
        pd.DataFrame([{
            "i": i, "Deck": m["deck_name"], "Card": m["card"],
            "Issue": "❓ Missing" if m["kind"] == "missing" else "📤 Extra",
            "Qty": m["qty"], "Copies are": m["where"], "Put away in": None, "Fix": False,
        } for i, m in enumerate(rows)]),
        column_config={
            "Deck": st.column_config.TextColumn("Deck", disabled=True),
            "Card": st.column_config.TextColumn("Card", disabled=True),
            "Issue": st.column_config.TextColumn("Issue", disabled=True, width="small"),
            "Qty": st.column_config.NumberColumn("Qty", disabled=True, width="small"),
            "Copies are": st.column_config.TextColumn("Copies are", disabled=True, width="large"),
            "Put away in": st.column_config.SelectboxColumn(
                "Put away in", options=[l for l in locations if l not in deck_names],
                help="Extra rows only. Blank = Lands for a land, otherwise Box."),
            "Fix": st.column_config.CheckboxColumn("Fix", width="small"),
        },
        column_order=["Deck", "Card", "Issue", "Qty", "Copies are", "Put away in", "Fix"],
        hide_index=True, width="stretch", height=400,
        key=f"workbench_mismatch_{ver}" + (f"_{filter_key}" if deck_pick or show_inactive else ""),
    )
    if st.button("💾 Fix checked", key=f"workbench_mismatch_fix_{ver}"):
        lines, moves = [], []
        for _, r in mismatch_edited.iterrows():
            if not r["Fix"]:
                continue
            m = rows[int(r["i"])]
            dest = r["Put away in"] if isinstance(r["Put away in"], str) and r["Put away in"] else None
            try:
                moved = writes.fix_location_mismatch(conn, m, dest)
            except ValueError as exc:
                lines.append(f"❌ {exc}")
                continue
            moves += moved
            # Taking from another deck fixes this row but opens one there — say so.
            raided = sorted({mv["from"] for mv in moved if mv.get("from") in deck_names})
            if m["kind"] == "extra":
                lines.append(f"✅ {m['card']} put away from {m['deck_name']}.")
            elif raided:
                lines.append(f"✅ {m['card']} sleeved into {m['deck_name']} — taken from "
                             f"{', '.join(raided)}, which is now missing it.")
            else:
                lines.append(f"✅ {m['card']} sleeved into {m['deck_name']}.")
        if not lines:
            st.warning("Tick **Fix** on at least one row first.")
        else:
            _refresh(lines, moves=moves)


# ==================================================================
# 🏷️ Tags — untagged mainboard cards, one deck at a time
# ==================================================================
def _render_tags():
    st.subheader(
        f"🏷️ Untagged cards ({len(untagged)})",
        help="Non-basic mainboard cards with no card tag of any type in that deck.",
    )
    if not untagged:
        st.caption("✅ Every mainboard card carries at least one tag.")
        return
    per_deck = {}
    for r in untagged:
        per_deck.setdefault((r["deck_id"], r["deck_name"]), []).append(r)
    # Most untagged first: that's where the work is.
    decks = sorted(per_deck, key=lambda k: (-len(per_deck[k]), k[1].casefold()))
    tag_types = sorted(set(writes.list_tag_types(conn)) | {"strategy"})

    t1, t2 = st.columns([3, 1])
    deck_key = t1.selectbox("Deck", decks, format_func=lambda k: f"{k[1]} — {len(per_deck[k])} untagged",
                            key="workbench_tags_deck")
    # One tag type is the norm; the picker only appears once there are more.
    tag_type = (t2.selectbox("Tag type", tag_types, index=tag_types.index("strategy"),
                             key="workbench_tags_type")
                if len(tag_types) > 1 else "strategy")
    deck_id, deck_name = deck_key
    options = q.deck_tag_labels(conn, tag_type).get(deck_id, [])
    st.caption((f"{len(options)} {tag_type} tag(s) in use in {deck_name} — pick from the list, "
                "or type a new one." if options else
                f"No {tag_type} tags in {deck_name} yet — type new ones in the Tags cell."))
    rows = per_deck[deck_key]
    edited = st.data_editor(
        pd.DataFrame([{"scryfall_id": r["scryfall_id"], "Card": r["card"], "Type": r["type_line"],
                       "Tags": []} for r in rows]),
        column_config={
            "Card": st.column_config.TextColumn("Card", disabled=True),
            "Type": st.column_config.TextColumn("Type", disabled=True),
            "Tags": st.column_config.MultiselectColumn(
                f"{tag_type.capitalize()} tags", options=options, accept_new_options=True, width="large"),
        },
        column_order=["Card", "Type", "Tags"],
        hide_index=True, width="stretch", height=min(400, 38 + 35 * len(rows)),
        key=f"workbench_untagged_{deck_id}_{tag_type}_{ver}",
    )
    if st.button("💾 Save tags", key=f"workbench_untagged_save_{ver}"):
        edits = {r["scryfall_id"]: [str(t) for t in r["Tags"]]
                 for _, r in edited.iterrows() if isinstance(r["Tags"], list) and r["Tags"]}
        if not edits:
            st.warning("Pick a tag on at least one row first.")
        else:
            tagged = writes.bulk_set_card_tags(conn, deck_id, tag_type, edits)
            _refresh([f"✅ Tagged {tagged} card(s) in {deck_name}."])


{"plan": _render_plan, "supply": _render_supply,
 "physical": _render_physical, "tags": _render_tags}[view]()
