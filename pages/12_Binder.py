"""
Binder page — stored cards that belong in the binder (docs/archive/BINDER_PLAN.md
Phase 3).

Lists the lots in storage (Box, Lands, no Location, any other non-deck
Location the user hasn't unticked) that pass a binder rule: worth at least
the minimum value (finish-aware: a foil copy uses its foil price), on the
Reserved List, or a game changer. The rules live in dashboard_lib/binder.py;
this page only shows them and acts on the ticked rows.

- Settings (expander): minimum value, the two rule switches, which storage
  Locations to scan, the binder's Location name and capacity. Saved through
  binder.save_settings(), which validates first (a deck name is refused).
- Price date: the oldest price among the scanned lots. Prices only change
  on a Card Data refresh, so an old date means an out-of-date list.
- Fill gauge: copies at the binder Location against its capacity, and what
  it would be after moving the ticked rows. Going over warns, never blocks.
- Candidate table: tick, art, card, copies to move (starts at the lot's
  quantity) and why it qualifies. Every row starts unticked: you decide.
  Select all ticks every row shown except ones a deck has promised itself
  (planned_for, a ⚠ note: moving them would leave that deck's plan pointing
  at the wrong box); Clear unticks everything.
- Move: writes.move_lot(..., reason="binder") per ticked row, so every move
  is in the move log and Card Lookup's History. A partial move leaves the
  rest behind as its own lot; a move merges into a matching Binder lot.
- Not for binder: dismisses the ticked rows' cards (every printing) for
  good; "Show dismissed" lists them with an Undo.
"""
import sys
import os
import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import streamlit as st
from streamlit.errors import StreamlitPageNotFoundError

from dashboard_lib import db, loaders, writes as w, queries as q, binder, nav
from dashboard_lib import card_view as cv

# A price date older than this is shown as a warning, not a caption.
STALE_PRICE_DAYS = 14
# Session flag: True after Select all, False after Clear / a move / a dismiss.
SELECT_ALL_KEY = "binder_select_all_on"

cv.setup_page("Binder · MTG Dashboard", "📒")

db.require_db()
conn = db.get_connection()

st.title("📒 Binder")
cv.how_it_works(
    "Stored cards that belong in the binder: worth at least the minimum value, on the Reserved "
    "List, or a game changer.",
    "Only storage is scanned — a copy sleeved in a deck is never listed, and neither are basic "
    "lands or copies already in the binder. A foil copy is judged on its foil price. Prices come "
    "from the last **Card Data** refresh (Card Database page), so a card that went up in value "
    "shows here once the prices are refreshed. Moving a row uses the same move as everywhere "
    "else, so it shows in Card Lookup's History.",
)
db.refresh_data_button()

settings = binder.binder_settings(conn)


# ------------------------------------------------------------------
# Settings
# ------------------------------------------------------------------
with st.expander("⚙️ Binder settings"):
    scan_options = binder.scan_location_options(conn, settings)
    with st.form("binder_settings_form", border=False):
        c1, c2 = st.columns(2)
        min_value = c1.number_input(
            "Minimum value ($)", min_value=0.0, step=0.5, format="%.2f",
            value=float(settings.min_value), key="binder_set_min_value",
            help="A copy worth at least this much qualifies. A foil copy uses its foil price.")
        rule_reserved = c1.toggle("Reserved List cards always qualify",
                                  value=settings.rule_reserved, key="binder_set_rule_reserved")
        rule_gc = c1.toggle("Game changers always qualify",
                            value=settings.rule_game_changer, key="binder_set_rule_gc")
        location = c2.text_input(
            "Binder Location", value=settings.location, key="binder_set_location",
            help="Renaming it makes the old binder plain storage: its cards come back as candidates.")
        capacity = c2.number_input("Capacity (copies)", min_value=1, step=1,
                                   value=int(settings.capacity), key="binder_set_capacity")
        st.markdown("**Locations to scan**")
        st.caption("Untick a box you keep as a working pile. A Location added later is scanned "
                   "until you untick it. Copies with no Location are always scanned.")
        scan_cols = st.columns(min(len(scan_options), 4) or 1)
        ticked = {}
        for i, place in enumerate(scan_options):
            ticked[place] = scan_cols[i % len(scan_cols)].checkbox(
                place, key=f"binder_set_scan_{place}",
                value=not any(q.same_location(place, s) for s in settings.skip_locations))
        saved = st.form_submit_button("Save settings", type="primary")
    if saved:
        # Keep unticked Locations that aren't offered right now (e.g. one
        # that's empty today), so saving never quietly re-ticks them.
        offered = set(scan_options)
        kept = [s for s in settings.skip_locations
                if not any(q.same_location(s, o) for o in offered)]
        new = binder.BinderSettings(
            location=location, capacity=int(capacity), min_value=float(min_value),
            rule_reserved=rule_reserved, rule_game_changer=rule_gc,
            skip_locations=tuple(kept + [p for p, on in ticked.items() if not on]),
        )
        try:
            binder.save_settings(conn, new)
        except ValueError as e:
            st.error(str(e))
        else:
            cv.finish_edit({"message": "Binder settings saved.", "collection": True})


# ------------------------------------------------------------------
# Price date
# ------------------------------------------------------------------
as_of = binder.prices_as_of(conn, settings)
if as_of:
    as_of_day = str(as_of)[:10]
    try:
        age = (datetime.date.today() - datetime.date.fromisoformat(as_of_day)).days
    except ValueError:
        age = None
    note = f"Prices as of {as_of_day}."
    if age is not None and age > STALE_PRICE_DAYS:
        st.warning(f"{note} That's {age} days old — refresh Card Data on the Card Database page "
                   "to catch cards that have gone up.")
    else:
        st.caption(note)
else:
    st.caption("No price date yet — refresh Card Data on the Card Database page.")
try:
    st.page_link(nav.page_path("Card_Database"), label="Card Database → Card Data", icon="🗄️")
except StreamlitPageNotFoundError:
    pass


# ------------------------------------------------------------------
# Candidates
# ------------------------------------------------------------------
candidates = binder.binder_candidates(conn, settings)
filled, capacity = binder.binder_fill(conn, settings)

if candidates.empty:
    rules = [f"worth ${settings.min_value:.2f} or more"]
    if settings.rule_reserved:
        rules.append("on the Reserved List")
    if settings.rule_game_changer:
        rules.append("a game changer")
    st.progress(min(filled / capacity, 1.0), text=f"{filled} / {capacity} copies in {settings.location}")
    st.info("Nothing in storage passes the binder rules — no stored card is "
            + (", ".join(rules[:-1]) + " or " + rules[-1] if len(rules) > 1 else rules[0]) + ".")
else:
    locations = sorted({(loc or "").strip() for loc in candidates["location"]}, key=str.casefold)
    labels = {loc: loc or "(no Location)" for loc in locations}
    shown_locs = st.multiselect(
        "Show copies in", locations, default=locations, key="binder_loc_filter",
        format_func=labels.get)
    view = candidates[candidates["location"].fillna("").str.strip().isin(shown_locs)]

    # Select all / Clear rebuild the editor (the flag is part of its key)
    # with every row ticked or none. Set in on_click so it's in place before
    # the table is drawn on the rerun.
    def _set_select_all(value):
        st.session_state[SELECT_ALL_KEY] = value

    select_all = st.session_state.get(SELECT_ALL_KEY, False)
    sel_col, clear_col, _ = st.columns([1, 1, 4])
    sel_col.button("☑️ Select all", key="binder_select_all", width="stretch",
                   on_click=_set_select_all, args=(True,),
                   help="Tick every row shown. ⚠ rows (planned for a deck) stay unticked.")
    clear_col.button("⬜ Clear", key="binder_select_none", width="stretch",
                     on_click=_set_select_all, args=(False,), help="Untick every row.")

    art = {r[0]: r[1] for r in conn.execute(
        "SELECT scryfall_id, image_uri FROM cards WHERE scryfall_id IN (%s)"
        % ",".join("?" * len(view)), tuple(view["scryfall_id"])).fetchall()} if len(view) else {}
    rows = [{
        "collection_id": int(r["collection_id"]),
        "card_key": r["card_key"],
        # Nothing is ticked until you choose; Select all ticks every row
        # shown except ones a deck has planned (those stay a choice per row).
        "Move": select_all and not r["planned_for"],
        "Art": art.get(r["scryfall_id"]),
        "Card": r["name"],
        "Set": f"{(r['set_code'] or '').upper()} #{r['collector_number'] or ''}".strip(),
        "Foil": bool(r["foil"]),
        "Qty": int(r["quantity"]),
        "To move": int(r["quantity"]),
        "Location": (r["location"] or "").strip() or "(no Location)",
        "Value": None if pd.isna(r["copy_value"]) else float(r["copy_value"]),
        "Why": " · ".join(r["reasons"]),
        "In binder": int(r["already_in_binder"]),
        "Planned for": f"⚠ {r['planned_for']}" if r["planned_for"] else "",
    } for _, r in view.iterrows()]

    st.caption(f"{len(view)} lot(s), {int(view['quantity'].sum()) if len(view) else 0} copies, "
               f"{view['card_key'].nunique()} card(s). Tick the rows to act on; lower **To move** "
               "to leave copies behind. ⚠ rows are promised to a deck.")
    # Keyed by the visible lot ids so a new filter or a finished move gives
    # a fresh editor instead of replaying old ticks onto different rows,
    # and by the Select all flag so those buttons reset every tick.
    editor_key = f"binder_table_{select_all}_{hash(tuple(r['collection_id'] for r in rows))}"
    edited = st.data_editor(
        pd.DataFrame(rows, columns=["collection_id", "card_key", "Move", "Art", "Card", "Set", "Foil",
                                    "Qty", "To move", "Location", "Value", "Why", "In binder",
                                    "Planned for"]),
        column_config={
            "Move": st.column_config.CheckboxColumn("Move", width="small"),
            "Art": st.column_config.ImageColumn("Art", width="small"),
            "Card": st.column_config.TextColumn("Card", width="medium"),
            "Set": st.column_config.TextColumn("Set", width="small"),
            "Foil": st.column_config.CheckboxColumn("Foil", width="small"),
            "Qty": st.column_config.NumberColumn("Qty", width="small"),
            "To move": st.column_config.NumberColumn(
                "To move", min_value=1, step=1,
                help="Copies of this lot to move. Anything left stays behind as its own lot."),
            "Value": st.column_config.NumberColumn("Value", format="$%.2f", width="small",
                                                   help="One copy's value (foil price for a foil)."),
            "Why": st.column_config.TextColumn("Why", width="medium"),
            "In binder": st.column_config.NumberColumn(
                "In binder", width="small", help="Copies of this card already in the binder."),
            "Planned for": st.column_config.TextColumn(
                "Planned for", help="A deck has promised itself this copy from this box."),
        },
        column_order=["Move", "Art", "Card", "Set", "Foil", "Qty", "To move", "Location", "Value",
                      "Why", "In binder", "Planned for"],
        disabled=["Art", "Card", "Set", "Foil", "Qty", "Location", "Value", "Why", "In binder",
                  "Planned for"],
        hide_index=True, width="stretch", key=editor_key,
    )

    picked = edited[edited["Move"]].copy()
    # A lowered count can't go past the lot or below one copy.
    picked["copies"] = [max(1, min(int(n) if pd.notna(n) else qty, qty))
                        for n, qty in zip(picked["To move"], picked["Qty"])]
    moving = int(picked["copies"].sum())
    after = filled + moving
    st.progress(min(filled / capacity, 1.0),
                text=f"{filled} / {capacity} copies in {settings.location}"
                     + (f" · {after} / {capacity} after moving {moving}" if moving else ""))
    if after > capacity:
        st.warning(f"Moving these puts {after} copies in a {capacity}-copy binder "
                   f"({after - capacity} over). The move still goes ahead if you want it.")

    move_col, dismiss_col = st.columns(2)
    do_move = move_col.button(
        f"📒 Move {moving} cop{'y' if moving == 1 else 'ies'} to {settings.location}" if moving
        else f"📒 Move to {settings.location} (tick rows first)",
        type="primary", disabled=not moving, key="binder_move", width="stretch")
    n_cards = picked["card_key"].nunique()
    do_dismiss = dismiss_col.button(
        f"🚫 Not for binder ({n_cards} card{'' if n_cards == 1 else 's'})" if n_cards
        else "🚫 Not for binder", disabled=not n_cards,
        key="binder_dismiss", width="stretch",
        help="Hide these cards (every printing) from the list for good. Undo under Show dismissed.")

    if do_move or do_dismiss:
        # The next list starts unticked again, whatever was selected.
        st.session_state[SELECT_ALL_KEY] = False

    if do_move:
        moves, errors = [], []
        # The binder's Location joins the master Location list on the first
        # move (not on page load — viewing the page writes nothing), so the
        # Collection Editor's dropdowns offer it from then on. A different
        # spelling already in the catalog counts as present.
        if not any(q.same_location(loc, settings.location) for loc in w.list_location_catalog(conn)):
            w.add_location_to_catalog(conn, settings.location)
        for _, r in picked.iterrows():
            try:
                w.move_lot(conn, int(r["collection_id"]), settings.location,
                           quantity=int(r["copies"]), reason="binder")
            except ValueError as e:
                errors.append(f"{r['Card']}: {e}")
                continue
            moves.append({"card": r["Card"], "quantity": int(r["copies"]),
                          "from": "" if r["Location"] == "(no Location)" else r["Location"],
                          "to": settings.location})
        loaders.load_location_options.clear()
        total = sum(m["quantity"] for m in moves)
        cv.finish_edit({"message": f"Moved {total} cop{'y' if total == 1 else 'ies'} to {settings.location}.",
                        "moves": moves, "errors": errors})

    if do_dismiss:
        names = {}
        for _, r in picked.iterrows():
            names.setdefault(r["card_key"], r["Card"])
        for key, name in names.items():
            w.dismiss_binder_card(conn, key, name)
        cv.finish_edit({"message": f"Not for binder: {', '.join(sorted(names.values(), key=str.casefold))}."})


# ------------------------------------------------------------------
# Dismissed
# ------------------------------------------------------------------
dismissed = binder.dismissed_cards(conn)
if st.toggle(f"Show dismissed ({len(dismissed)})", key="binder_show_dismissed", disabled=not dismissed):
    st.caption("Hidden from the list in every printing, whatever their price. Undo brings a card back.")
    for d in dismissed:
        name_col, date_col, undo_col = st.columns([4, 2, 1])
        name_col.write(d["name"] or d["card_key"])
        date_col.caption(f"Dismissed {d['dismissed_at'] or '—'}")
        if undo_col.button("Undo", key=f"binder_undo_{d['card_key']}"):
            w.undo_binder_dismissal(conn, d["card_key"])
            cv.finish_edit({"message": f"{d['name']} is back on the binder list."})
