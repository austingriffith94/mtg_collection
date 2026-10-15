"""
Reusable card-browsing UI shared by the Collection page and the
Decks page:

  - add_derived_columns()   adds card_type / color_display / scryfall_url /
                             is_land (Phase 3, MDFC-aware)
  - render_filter_panel()   sidebar filters (search, facets, colors, price, CMC).
                             Color identity is a fixed WUBRG+C st.pills row;
                             Yes/No/All bool_toggles are st.segmented_control
                             (Phase 5) — both return None with nothing picked,
                             treated as "no filter"/"All".
  - render_browser()        the View toggle (Table / Image Grid, now
                             st.segmented_control) + "group by"
                             breakout controls + sort, then dispatches to...
                             (an st.fragment since UI plan Phase 4)
  - render_table()          st.dataframe with a clickable Scryfall link column
  - render_grid()           paginated image grid, each card image itself
                             links out to its Scryfall page. Local art is
                             linked as an app/static/ URL, not embedded
                             (card_image_src / deck_image_src, Phase 4)

Grouping ("layering of toggles") is implemented with ONE st.expander per
top-level group (Streamlit does not allow nesting expanders inside each
other) and plain markdown sub-headers for any additional grouping levels
selected underneath it.
"""
import base64
import html
import math
import os

import pandas as pd
import streamlit as st

from . import formatting as fmt
from .log import setup_logging

PAGE_SIZE_OPTIONS = [12, 24, 48, 96]
SORT_FIELDS = {
    "Name": "name",
    "Mana Value": "cmc",
    "Price": "current_price_usd",
    "Rarity": "rarity",
    "Set": "set_code",
}

def setup_page(page_title, page_icon, layout="wide"):
    """Logging + st.set_page_config() — every page calls this as its very
    first UI action. page_title sets the browser-tab title; the sidebar
    label comes from dashboard_lib/nav.py instead (uppercase titles there
    replaced the old nav-caps CSS in Phase 1 of UI_ACTION_PLAN.md). Each
    page still needs its own sys.path.insert(...) BEFORE this can even be
    imported, so that part stays per-file.

    Also shows any feedback a write stashed before its rerun (Phase 0 of
    EDIT_FLOW_PLAN.md) — see finish_edit()/show_pending_feedback() below."""
    setup_logging()
    st.set_page_config(page_title=page_title, page_icon=page_icon, layout=layout)
    show_pending_feedback()


# ------------------------------------------------------------------
# One ending for every write (EDIT_FLOW_PLAN.md Phase 0, rule 1): clears
# the right caches, stashes the outcome, and reruns — so no page hand-rolls
# its own invalidate_*() / st.toast() / st.rerun() triple after a write.
# Read-only notices (st.info hints, empty states) are unaffected; this is
# only for a page's own successful writes.
# ------------------------------------------------------------------
_PENDING_FEEDBACK_KEY = "_pending_feedback"


def finish_edit(result):
    """Call this instead of invalidate_*() / st.toast() / st.rerun() after a
    write. `result` (every key optional):

        message     str, or a list of str (one line per outcome)
        warnings    [str]  non-fatal notes, shown as ⚠️ toasts
        errors      [str]  per-item failures, shown as st.error
        moves       [...]  physical copies moved (writes' move dicts, Phase 1)
        deck_sync   [...]  list changes a collection-side save could make to
                           decks (queries.deck_sync_offers, Phase 6) — shown
                           as a dialog asking whether each deck's list follows
        collection  bool   also clear collection caches (implied by moves)
        reference   bool   also clear reference caches (catalogs, card data)
        games       bool   also clear game-log caches
        combos      bool   also clear the Spellbook combo caches

    Deck caches are always cleared. Ends in `st.rerun(scope="app")` — a
    full rerun even from inside a fragment, since a write can change what
    sections outside it show."""
    stash_edit(result)
    st.rerun(scope="app")


def stash_edit(result):
    """finish_edit() without the rerun, for a write made inside a widget
    callback (on_click / on_change), where st.rerun() is a no-op: the
    caller arranges the full-app rerun itself (deck_board's dirty flag +
    rerun_if_edited()), and setup_page() shows the feedback on that run."""
    # Local import: loaders imports card_view (for add_derived_columns),
    # so importing it at module scope here would be circular.
    from . import loaders

    moves = list(result.get("moves") or [])
    loaders.invalidate_deck_caches()
    if result.get("collection") or moves:
        loaders.invalidate_collection_caches()
    if result.get("reference"):
        loaders.invalidate_reference_caches()
    if result.get("games"):
        loaders.invalidate_game_tracking_caches()
    if result.get("combos"):
        loaders.invalidate_combo_caches()
    message = result.get("message")
    lines = [message] if isinstance(message, str) else list(message or [])
    st.session_state[_PENDING_FEEDBACK_KEY] = {
        "lines": [ln for ln in lines if ln],
        "warnings": list(result.get("warnings") or []),
        "errors": list(result.get("errors") or []),
        "moves": moves,
        "deck_sync": list(result.get("deck_sync") or []),
    }


def result_from_lines(lines, **flags):
    """A finish_edit() result from a list of "✅ ..." / "❌ ..." lines — the
    shape the Shortlist tab and the Workbench build for a batch (one line
    per change applied, promoted, dropped...). `flags` pass through
    (collection=True etc.)."""
    ok = [ln[1:].strip() for ln in lines if not ln.startswith("❌")]
    bad = [ln[1:].strip() for ln in lines if ln.startswith("❌")]
    return {"message": ok, "errors": bad, **flags}


# ------------------------------------------------------------------
# The moves pop-up (EDIT_FLOW_PLAN.md Phase 1): every physical copy a write
# moved, named — never summarised as a count. Moves are writes.py's dicts;
# their `kind` decides how each reads.
# ------------------------------------------------------------------
def _where(location):
    return (location or "").strip() or "no location"


def _card_qty(move):
    qty = move.get("quantity")
    return f"{move['card']} ×{qty}" if qty and qty > 1 else move["card"]


def move_route(move):
    """The from → to half of a move, which is also its group in the dialog."""
    kind = move.get("kind")
    if kind == "new":
        return f"new copy record → {move['to']}"
    if kind == "delete":
        return f"{move['from']} → copy record removed"
    if kind == "bulk":
        return "bulk — not tracked"
    if kind == "brewing":
        return ("Brewing — pulled when you build the deck" if move.get("direction") == "in"
                else "Brewing — list only, the copy stays put")
    if kind == "dismantled":
        return "Dismantled — list only, nothing moved"
    if kind == "buy":
        return "to buy — no copy yet"
    return f"{_where(move['from'])} → {_where(move['to'])}"


def move_label(move):
    """One move as a sentence for a toast: "Sol Ring: Box → Prosper"."""
    kind = move.get("kind")
    if kind == "brewing" and move.get("direction") == "in":
        # Phase 2: a chosen Location is remembered with the add.
        place = f" from {move['source_location']}" if move.get("source_location") else ""
        return f"Brewing — {_card_qty(move)} will be pulled{place} when you build the deck."
    if kind == "buy":
        return f"On the list — {_card_qty(move)} still to buy (see the Workbench buy list)."
    if kind in ("brewing", "dismantled"):
        return f"{kind.capitalize()} — {_card_qty(move)}: list only, nothing moved."
    return f"{_card_qty(move)}: {move_route(move)}"


def _moves_dialog_body(moves):
    groups = {}
    for move in moves:
        groups.setdefault(move_route(move), []).append(move)
    for route, group in groups.items():
        st.markdown(f"**{route}**")
        st.markdown("\n".join(f"- {_card_qty(m)}" for m in group))
    if st.button("OK", key="_moves_dialog_ok", type="primary"):
        st.rerun()


# ------------------------------------------------------------------
# The source / destination pickers (EDIT_FLOW_PLAN.md Phase 2): one widget
# per question, so the add bar, the Shortlist form and the Mainboard save
# ask it the same way. They return the choice in the shape writes.py takes
# — (source, source_location) / dest — with None meaning Auto / Box.
# ------------------------------------------------------------------
def render_source_picker(choices, key, container=None, label="Comes from"):
    """`choices` is formatting.source_choices(): Auto first, then storage
    Locations, other decks and Buy. Returns (source, source_location);
    (None, None) is Auto, ("buy", None) a planned purchase."""
    container = container or st
    labels = [c[0] for c in choices]
    picked = container.selectbox(
        label, labels, key=key,
        help="Auto uses a copy already in this deck, then the biggest free lot in storage, "
             "then makes a new copy record. Pick a Location to take it from there instead, "
             "or Buy to put it on the list now and record the purchase later (no copy is made).",
    )
    _label, source, location = choices[labels.index(picked)] if picked in labels else choices[0]
    return source, location


def render_dest_picker(locations, key, container=None, label="Removed copies go to"):
    """Where copies a save puts away go. `locations` is queries.
    storage_locations() (Box first). Returns the Location, or None for the
    default (Box) so History stays quiet about the usual case."""
    container = container or st
    picked = container.selectbox(
        label, locations, key=key,
        help="Only matters when this save takes copies out of the deck.",
    )
    return None if picked == locations[0] else picked


def show_moves(moves):
    """One or two moves are toasts; more (a Mainboard table save, an apply
    batch) open a dialog listing every move, grouped by from → to."""
    if len(moves) <= 2:
        for move in moves:
            st.toast(move_label(move), icon="📦")
    else:
        # Decorated here rather than at import, the way pages declare their
        # dialogs on every run: the scripts/_test_*_offline.py harnesses
        # import this module under a Streamlit stub with no st.dialog.
        st.dialog("Copies moved", width="large")(_moves_dialog_body)(moves)


def show_pending_feedback():
    """Called once from setup_page(), on every page, every run: shows
    whatever the last write (if any) stashed via finish_edit() before its
    rerun, then clears it so it shows exactly once.

    One or two message lines are toasts; more (a batch apply) are one
    st.success list at the top of the page, so a batch is never cut off in
    a toast's few lines. Then the moves (show_moves)."""
    pending = st.session_state.pop(_PENDING_FEEDBACK_KEY, None)
    if not pending:
        return
    lines = pending.get("lines") or []
    if len(lines) <= 2:
        for line in lines:
            st.toast(line, icon="✅")
    else:
        st.success("\n".join(f"- {line}" for line in lines))
    offers = pending.get("deck_sync")
    if pending.get("moves"):
        # Streamlit opens one dialog per run, and the list question below
        # needs an answer, so a big moves list is spelled out as toasts
        # beside it rather than a second dialog.
        if offers:
            for move in pending["moves"]:
                st.toast(move_label(move), icon="📦")
        else:
            show_moves(pending["moves"])
    for warning in pending.get("warnings") or []:
        st.toast(warning, icon="⚠️")
    for err in pending.get("errors") or []:
        st.error(err)
    if offers:
        show_deck_sync(offers)


# ------------------------------------------------------------------
# "Also update Gisa's list?" (EDIT_FLOW_PLAN.md Phase 6): a Collection-side
# save that moved copies into or out of a deck has already happened — the
# lots are changed and in the move log. This asks, per deck, whether the
# deck's LIST should follow. There is no silent default: closing the dialog
# without answering leaves "just the collection", and Workbench Reconcile
# shows the gap as "to pull" / "to put away" as it always has.
#
# The offers wait in session state and the dialog body reads them from
# there, not from its arguments: a click inside a dialog reruns only the
# dialog, with the arguments it was first opened with, which would still
# list a deck that was already answered.
# ------------------------------------------------------------------
_DECK_SYNC_KEY = "_deck_sync_offers"
_DECK_SYNC_DONE_KEY = "_deck_sync_done_lines"


def deck_sync_line(offer):
    """One offer as a sentence: "Add Sol Ring ×1 (list ×2 → ×3)"."""
    delta = offer["quantity"] if offer["action"] == "add" else -offer["quantity"]
    verb = "Add" if offer["action"] == "add" else "Remove"
    return (f"{verb} {offer['card']} ×{offer['quantity']} "
            f"(list ×{offer['list_quantity']} → ×{offer['list_quantity'] + delta})")


def _answer_deck_sync(deck_name, follow):
    """One deck answered: apply its offers when `follow`, drop them either
    way, and close the dialog (a full rerun) once none are left."""
    from . import db, writes  # local: neither is needed outside a click

    offers = st.session_state.get(_DECK_SYNC_KEY, [])
    mine = [o for o in offers if o["deck_name"] == deck_name]
    rest = [o for o in offers if o["deck_name"] != deck_name]
    st.session_state[_DECK_SYNC_KEY] = rest
    done = st.session_state.get(_DECK_SYNC_DONE_KEY, [])
    moves = []
    if follow:
        lines, moves = writes.sync_deck_lists(db.get_connection(), mine)
        done = done + lines
        st.session_state[_DECK_SYNC_DONE_KEY] = done
        # Caches are cleared per answer: the dialog may be dismissed before
        # the last deck is answered, and what's written is written.
        stash_edit({"message": done, "moves": moves})
    if not rest:
        st.session_state.pop(_DECK_SYNC_DONE_KEY, None)
        st.rerun(scope="app")
    # Bare st.rerun() is an app rerun, which would close the dialog with
    # decks still unanswered — rerun just the dialog to drop this deck's block.
    st.rerun(scope="fragment")


def _deck_sync_dialog_body():
    offers = st.session_state.get(_DECK_SYNC_KEY, [])
    st.caption("This save moved copies into or out of a deck. Should its list follow, "
               "or is this just a correction to the collection?")
    decks = {}
    for offer in offers:
        decks.setdefault(offer["deck_name"], []).append(offer)
    for deck_name, group in decks.items():
        st.markdown(f"**{deck_name}**")
        st.markdown("\n".join(f"- {deck_sync_line(o)}" for o in group))
        follow_col, keep_col = st.columns(2)
        if follow_col.button(f"Also update {deck_name}'s list", key=f"_deck_sync_yes_{deck_name}",
                             type="primary", width="stretch"):
            _answer_deck_sync(deck_name, True)
        if keep_col.button("Just the collection", key=f"_deck_sync_no_{deck_name}", width="stretch"):
            _answer_deck_sync(deck_name, False)


def show_deck_sync(offers):
    """Open the dialog for `offers` (queries.deck_sync_offers' shape)."""
    st.session_state[_DECK_SYNC_KEY] = list(offers)
    st.session_state.pop(_DECK_SYNC_DONE_KEY, None)
    # Declared when shown, like show_moves(): the offline test harnesses
    # import this module under a Streamlit stub with no st.dialog.
    st.dialog("Update deck lists?", width="large")(_deck_sync_dialog_body)()


# ------------------------------------------------------------------
# Sidebar filter reset (Prompt Pass 4) — every widget key created by
# render_filter_panel() (and any bespoke filter control a page adds
# outside it, via register_filter_key()) is tracked here so
# db.refresh_data_button() can clear them all on click, across every
# page/deck/board scope at once.
# ------------------------------------------------------------------
_FILTER_KEYS_STATE = "_active_filter_widget_keys"


def register_filter_key(key):
    """Track a sidebar-filter widget's session_state key so a "Refresh
    data" click can reset it. Called automatically by
    render_filter_panel() for every filter widget it builds; a page can
    also call this directly for a bespoke filter control that lives
    outside render_filter_panel (e.g. the Collection page's deck-status
    toggle) so it's reset the same way."""
    st.session_state.setdefault(_FILTER_KEYS_STATE, set())
    st.session_state[_FILTER_KEYS_STATE].add(key)


def reset_filters():
    """Delete every tracked filter widget's session_state entry so the
    next script run re-renders them at their defaults (no selection, no
    search text, full price/CMC range). Must be called BEFORE
    st.rerun() — Streamlit forbids setting/clearing a widget's
    session_state value after it has already been instantiated in the
    current run, but doing it here, inside the button-click handler and
    before the widgets are re-created on the next run, is the standard,
    safe pattern for a reset control."""
    for key in st.session_state.get(_FILTER_KEYS_STATE, set()):
        st.session_state.pop(key, None)
    st.session_state.pop(_FILTER_KEYS_STATE, None)


# ------------------------------------------------------------------
# Shared "current deck" (Phase 2 of UI_ACTION_PLAN.md). Decks, Land
# Probability, Deck Editor and the Advisor all pick one deck; the choice
# lives in ONE plain session key, CURRENT_DECK_KEY, so it follows you from
# page to page.
#
# Why not just share the selectbox's own key: Streamlit deletes a widget's
# session_state entry on any run where that widget isn't drawn, so a
# widget key can't carry a value across pages. Instead the picker copies
# CURRENT_DECK_KEY into its (per-page) widget key right before drawing,
# and its on_change writes the user's pick back.
#
# The value is the deck's *id*, never its name: a rename keeps the
# selection, and the Deck Editor's create flow can select the new deck by
# the id create_deck() returns.
# ------------------------------------------------------------------
CURRENT_DECK_KEY = "current_deck_id"
_CONSUMED_PARAM_KEY = "_current_deck_param_consumed"


def seed_current_deck(state, param_value):
    """Apply a `?deck_id=` query param to the shared current deck. The
    param seeds CURRENT_DECK_KEY only when it differs from the last param
    already consumed this session; after that the session key wins, so a
    leftover param can't override a later manual pick. Callers still
    delete the param after reading it. Returns True if it seeded.
    `state` is st.session_state or any dict (for tests)."""
    if param_value is None:
        return False
    raw = str(param_value)
    if state.get(_CONSUMED_PARAM_KEY) == raw:
        return False
    state[_CONSUMED_PARAM_KEY] = raw
    try:
        state[CURRENT_DECK_KEY] = int(raw)
    except ValueError:
        return False
    return True


def resolve_current_deck(state, deck_ids):
    """The deck the picker should show: CURRENT_DECK_KEY if it's still one
    of deck_ids (a non-empty list of ints), else the first deck (covers
    a first visit, a deleted deck, or a bad query param). Writes the
    answer back so every page agrees on it."""
    current = state.get(CURRENT_DECK_KEY)
    if current not in deck_ids:
        current = deck_ids[0]
    state[CURRENT_DECK_KEY] = current
    return current


def _sync_current_deck(widget_key):
    st.session_state[CURRENT_DECK_KEY] = st.session_state[widget_key]


def how_it_works(*paragraphs):
    """The page's single collapsed "ℹ️ How this works" expander (UI plan
    Phase 6). Long explanations live here or in `help=` tooltips;
    st.caption stays for short context and warnings."""
    with st.expander("ℹ️ How this works"):
        for p in paragraphs:
            st.markdown(p)


def render_deck_picker(decks_df, key, container=None, empty_message="No decks found.", label="Choose a deck"):
    """Render the deck selectbox (sidebar by default) and return the
    chosen deck_id, or None if decks_df is empty (empty_message is shown
    via st.info in that case). Callers that require a deck to proceed
    should st.stop() themselves on a None return; the Deck Editor page
    instead falls through to its "create a new deck" flow. `key` is this
    page's own widget key; the selection itself is the shared
    CURRENT_DECK_KEY (see above), so to pre-select a deck set that key,
    not the widget's."""
    c = container if container is not None else st.sidebar
    if decks_df.empty:
        c.info(empty_message)
        return None
    deck_ids = [int(i) for i in decks_df["deck_id"]]
    names = dict(zip(deck_ids, decks_df["name"]))
    st.session_state[key] = resolve_current_deck(st.session_state, deck_ids)
    c.selectbox(label, deck_ids, format_func=names.get, key=key,
                on_change=_sync_current_deck, args=(key,))
    return st.session_state[CURRENT_DECK_KEY]


# ------------------------------------------------------------------
# Dialog open-state (Phase 5). An st.dialog only keeps re-rendering on
# interactions INSIDE it (it inherits st.fragment's partial-rerun
# behavior); any OTHER full-script rerun — an unrelated widget elsewhere
# on the page, a page switch — happens because the call site that opened
# it ("if st.button(...): my_dialog()") isn't on that rerun's path, so the
# dialog simply isn't called and vanishes. A plain session_state flag
# fixes that: the opening button sets it, the call site checks it on
# every run (not just the click that set it), and the dialog clears it
# before any st.rerun() that should close it (a successful save, a
# cancel) — see each dialog's own call site in pages/.
# ------------------------------------------------------------------
def open_dialog(flag_key):
    st.session_state[flag_key] = True


def close_dialog(flag_key):
    st.session_state[flag_key] = False


def dialog_is_open(flag_key):
    return st.session_state.get(flag_key, False)


# ------------------------------------------------------------------
# Derived columns shared by every card-bearing dataframe
# ------------------------------------------------------------------
def add_derived_columns(df):
    df = df.copy()
    df["card_type"] = df["type_line"].fillna("").apply(fmt.derive_card_type)
    df["color_display"] = df["color_identity"].fillna("").apply(fmt.color_identity_display)
    df["scryfall_url"] = [
        fmt.scryfall_card_url(s, n)
        for s, n in zip(df.get("set_code"), df.get("collector_number"))
    ]
    # Phase 3: MDFC-aware land flag — True if EITHER face is a Land, unlike
    # card_type above which only looks at the front face for display
    # bucketing. Used by the mana curve and Land Probability page so a
    # card like "Instant // Land" is correctly treated as a land for
    # curve-exclusion, land-ratio, and draw-probability math.
    df["is_land"] = df["type_line"].fillna("").apply(fmt.has_land_face)
    return df


# ------------------------------------------------------------------
# Filters
# ------------------------------------------------------------------
def _multi_value_options(series, colorless_label=None):
    vals = set()
    for cell in series.dropna():
        for part in fmt.split_multi_value(cell):
            vals.add(part)
    options = sorted(vals)
    if colorless_label:
        options = options + [colorless_label]
    return options


def _multi_value_filter(df, col, selected, colorless_label=None):
    if not selected:
        return df
    target = {s for s in selected if s != colorless_label}
    want_colorless = colorless_label is not None and colorless_label in selected

    def matches(cell):
        parts = set(fmt.split_multi_value(cell))
        if not parts:
            return want_colorless
        return bool(parts & target) if target else False

    return df[df[col].apply(matches)]


def render_filter_panel(
    df,
    key_prefix,
    container=None,
    single_facets=None,
    multi_facets=None,
    bool_toggles=None,
    show_price_slider=True,
    show_cmc_slider=True,
    disabled=False,
):
    """
    single_facets: [(col, label), ...] -> plain "is in selection" multiselect
    multi_facets:  [(col, label, colorless_label_or_None), ...] -> comma-
                   separated values, "any selected value present" matching
                   (used for color_identity and strategy_tags)
    bool_toggles:  [(col, label), ...] -> All / Yes / No segmented_control on
                   a 0/1 column
    disabled:      grays out every widget here without clearing its value
                   (Phase 5 — the Collection page's Edit mode uses this so a
                   filter change can't silently discard unsaved table edits).
    Returns the filtered dataframe. All widget keys are namespaced with
    key_prefix so the Collection and Decks pages don't collide.
    """
    c = container if container is not None else st.sidebar
    filtered = df

    search_key = f"{key_prefix}_search"
    register_filter_key(search_key)
    search = c.text_input(
        "Search card name", key=search_key, placeholder="e.g. Sol Ring", disabled=disabled
    )
    if search:
        filtered = filtered[filtered["name"].str.contains(search, case=False, na=False, regex=False)]

    for col, label, colorless_label in multi_facets or []:
        if col not in df.columns:
            continue
        multi_key = f"{key_prefix}_{col}_multi"
        register_filter_key(multi_key)
        if col == "color_identity":
            # Phase 5: a fixed WUBRG+C pill row, not a multiselect built from
            # whatever colors happen to appear in df — the point is a stable,
            # recognizable control, not a dynamic option list.
            options = list(fmt.WUBRG_ORDER) + ([colorless_label] if colorless_label else [])
            selected = c.pills(label, options, selection_mode="multi", key=multi_key, disabled=disabled) or []
        else:
            options = _multi_value_options(df[col], colorless_label)
            if not options:
                continue
            selected = c.multiselect(label, options, key=multi_key, disabled=disabled)
        filtered = _multi_value_filter(filtered, col, selected, colorless_label)

    for col, label in single_facets or []:
        if col not in df.columns:
            continue
        options = sorted(v for v in df[col].dropna().unique() if str(v).strip())
        if not options:
            continue
        single_key = f"{key_prefix}_{col}_single"
        register_filter_key(single_key)
        selected = c.multiselect(label, options, key=single_key, disabled=disabled)
        if selected:
            filtered = filtered[filtered[col].isin(selected)]

    for col, label in bool_toggles or []:
        if col not in df.columns:
            continue
        bool_key = f"{key_prefix}_{col}_bool"
        register_filter_key(bool_key)
        # st.segmented_control returns None with nothing selected (unlike the
        # old selectbox, which always had something picked) — treat that the
        # same as "All" (Phase 5).
        choice = c.segmented_control(label, ["All", "Yes", "No"], key=bool_key, disabled=disabled) or "All"
        if choice == "Yes":
            filtered = filtered[filtered[col] == 1]
        elif choice == "No":
            filtered = filtered[filtered[col] == 0]

    if show_price_slider and "current_price_usd" in df.columns and df["current_price_usd"].notna().any():
        max_price = float(df["current_price_usd"].max(skipna=True) or 0)
        if max_price > 0:
            price_key = f"{key_prefix}_price"
            register_filter_key(price_key)
            lo, hi = c.slider(
                "Price (USD)", 0.0, round(max_price + 0.5, 2), (0.0, round(max_price + 0.5, 2)),
                key=price_key, disabled=disabled,
            )
            filtered = filtered[
                filtered["current_price_usd"].isna()
                | filtered["current_price_usd"].between(lo, hi)
            ]

    if show_cmc_slider and "cmc" in df.columns and df["cmc"].notna().any():
        max_cmc = int(df["cmc"].max(skipna=True) or 0)
        if max_cmc > 0:
            cmc_key = f"{key_prefix}_cmc"
            register_filter_key(cmc_key)
            lo, hi = c.slider("Mana Value", 0, max_cmc, (0, max_cmc), key=cmc_key, disabled=disabled)
            filtered = filtered[filtered["cmc"].isna() | filtered["cmc"].between(lo, hi)]

    return filtered


# ------------------------------------------------------------------
# View toggle + group-by breakout + sort, then dispatch to table/grid
# ------------------------------------------------------------------
@st.fragment
def render_browser(df, key_prefix, group_options, column_config, extra_table_cols=None, columns_per_row=5,
                   stacks_renderer=None):
    """
    A fragment (UI plan Phase 4): changing the view, grouping, sort, page
    size or page reruns only this browser, not the whole page. A fragment
    rerun reuses the arguments from the last full run, so callers pass the
    already-filtered `df` and keep their filter widgets outside it — a
    filter change is still a full rerun, which re-calls this with new data.
    Don't draw into the sidebar or other outside containers from in here.

    group_options: [(col, label), ...] offered in the "Group / breakout by"
                   multiselect, applied in the order the user picks them
                   (layered breakouts).
    column_config: ordered dict {col: st.column_config...} defining the
                   Table view's columns (order + rendering, incl. the
                   clickable Scryfall LinkColumn).
    stacks_renderer: optional callable(df) that draws the deck's Stacks board
                   (VISUAL_DECK_PLAN.md Phase A). Passing it adds a third
                   "Stacks" View option; that view has its own group-by, so
                   the browser's group / sort controls are skipped for it,
                   but the (already-applied) filters still narrow `df`. It's
                   a callable, not an import, because deck_board imports this
                   module. Opt-in so the Collection page is unchanged.
    """
    if df.empty:
        st.info("No cards match the current filters.")
        return

    ctrl1, ctrl2, ctrl3, ctrl4 = st.columns([1.3, 2, 1.3, 1])
    views = ["Table", "Image Grid"] + (["Stacks"] if stacks_renderer else [])
    view = ctrl1.segmented_control("View", views, key=f"{key_prefix}_view") or "Table"

    if view == "Stacks":
        st.caption(f"{len(df)} card row(s) shown")
        stacks_renderer(df)
        return

    label_by_col = dict(group_options)
    col_by_label = {label: col for col, label in group_options}
    chosen_labels = ctrl2.multiselect(
        "Group / breakout by", list(col_by_label.keys()), key=f"{key_prefix}_groupby",
        help="Layer multiple breakouts — each pick nests inside the previous one.",
    )
    group_cols = [col_by_label[lbl] for lbl in chosen_labels]

    sort_label = ctrl3.selectbox("Sort by", list(SORT_FIELDS.keys()), key=f"{key_prefix}_sort")
    descending = ctrl4.toggle("Desc.", key=f"{key_prefix}_desc")

    sort_col = SORT_FIELDS[sort_label]
    if sort_col in df.columns:
        df = df.sort_values(by=sort_col, ascending=not descending, na_position="last", kind="stable")

    st.caption(f"{len(df)} card row(s) shown")

    def leaf_renderer(sub_df):
        if view == "Table":
            render_table(sub_df, column_config, extra_table_cols)
        else:
            # sub_df.index values are unique in the parent dataframe and
            # partitioned disjointly across groups, so the first index value
            # is a stable, collision-free widget-key suffix per leaf group.
            leaf_key = f"{key_prefix}_g{sub_df.index[0]}"
            render_grid(sub_df, key_prefix=leaf_key, columns_per_row=columns_per_row)

    if not group_cols:
        leaf_renderer(df)
    else:
        _render_grouped(df, group_cols, label_by_col, leaf_renderer, depth=0)


def _render_grouped(df, group_cols, label_by_col, leaf_renderer, depth):
    if not group_cols:
        leaf_renderer(df)
        return

    col = group_cols[0]
    label = label_by_col.get(col, col)
    working = df.copy()
    working["_group_val"] = working[col].fillna("—").astype(str)
    working.loc[working["_group_val"].str.strip() == "", "_group_val"] = "—"

    for val in sorted(working["_group_val"].unique()):
        sub = working[working["_group_val"] == val].drop(columns=["_group_val"])
        if sub.empty:
            continue
        header = f"{label}: {val}  ·  {len(sub)} card{'s' if len(sub) != 1 else ''}"
        if depth == 0:
            # Only ONE level of st.expander is ever used per branch —
            # Streamlit does not allow nesting an expander inside another.
            with st.expander(header, expanded=(working["_group_val"].nunique() <= 4)):
                _render_grouped(sub, group_cols[1:], label_by_col, leaf_renderer, depth + 1)
        else:
            heading = "####" if depth == 1 else "#####"
            st.markdown(f"{heading} {header}")
            _render_grouped(sub, group_cols[1:], label_by_col, leaf_renderer, depth + 1)
            st.divider()


# ------------------------------------------------------------------
# Table view
# ------------------------------------------------------------------
def render_table(df, column_config, extra_cols=None):
    cols = [c for c in column_config.keys() if c in df.columns]
    for c in extra_cols or []:
        if c in df.columns and c not in cols:
            cols.append(c)
    st.dataframe(
        df[cols],
        column_config=column_config,
        hide_index=True,
        width="stretch",
    )


def base_column_config(price=True):
    cfg = {
        "image_uri": st.column_config.ImageColumn("Art", width="small"),
        "name": st.column_config.TextColumn("Name", width="medium"),
        "set_code": st.column_config.TextColumn("Set", width="small"),
        "collector_number": st.column_config.TextColumn("#", width="small"),
        "rarity": st.column_config.TextColumn("Rarity", width="small"),
        "type_line": st.column_config.TextColumn("Type", width="large"),
        "mana_cost": st.column_config.TextColumn("Cost", width="small"),
        "cmc": st.column_config.NumberColumn("MV", width="small", format="%.0f"),
        "color_display": st.column_config.TextColumn("Colors", width="small"),
        "scryfall_url": st.column_config.LinkColumn("Scryfall", display_text="View ↗", width="small"),
    }
    if price:
        cfg["current_price_usd"] = st.column_config.NumberColumn("Price", format="$%.2f", width="small")
    return cfg


# ------------------------------------------------------------------
# Image grid view — each card image itself links out to Scryfall
# ------------------------------------------------------------------
@st.cache_data(show_spinner=False, max_entries=500)
def _encode_local_image_b64(abs_path, mtime):
    # `mtime` is unused in the body but is part of Streamlit's cache key: without
    # it, replacing the file on disk at the same `abs_path` would keep serving the
    # stale cached bytes forever. Callers must pass the file's current mtime.
    with open(abs_path, "rb") as f:
        return base64.b64encode(f.read()).decode("ascii")


def _local_src(local_image_path, embed):
    """src for a cached image under static/, or None if it isn't on disk.

    On screen (embed=False) this is a static-serving URL, so the browser
    fetches and caches each image once instead of the page carrying every
    image's bytes on every rerun (Phase 4 of UI_ACTION_PLAN.md: a 48-card
    grid used to re-send several MB of base64 per filter change). embed=True
    returns a self-contained data: URI, for output that has to work outside
    the app — the downloadable deck sheet — or for st.image, which reads a
    plain string as a file path rather than a relative URL."""
    if not embed:
        return fmt.local_image_url(local_image_path)
    abs_path = fmt.resolve_local_image(local_image_path)
    if not abs_path:
        return None
    try:
        b64 = _encode_local_image_b64(abs_path, os.path.getmtime(abs_path))
    except OSError:
        return None
    ext = os.path.splitext(abs_path)[1].lstrip(".").lower() or "jpg"
    ext = "jpeg" if ext == "jpg" else ext
    return f"data:image/{ext};base64,{b64}"


def card_image_src(row, embed=False):
    """Best image src for a card row: the local cache (see _local_src for
    what `embed` changes), else Scryfall's remote image_uri, else None."""
    src = _local_src(row.get("local_image_path"), embed)
    if src:
        return src
    image_uri = row.get("image_uri")
    if isinstance(image_uri, str) and image_uri.startswith("http"):
        return image_uri
    return None


def _render_card_tile(row):
    name = html.escape(str(row.get("name") or "Unknown card"))
    url = row.get("scryfall_url")
    src = card_image_src(row)
    qty = row.get("quantity")
    qty_badge = f" ×{int(qty)}" if pd.notna(qty) else ""

    if src:
        img_html = f'<img src="{src}" style="width:100%;border-radius:8px;display:block;box-shadow:0 1px 4px rgba(0,0,0,0.35);" alt="{name}"/>'
    else:
        img_html = (
            '<div style="width:100%;aspect-ratio:5/7;background:#2b2b2b;border-radius:8px;'
            'display:flex;align-items:center;justify-content:center;color:#999;'
            'font-size:0.75rem;text-align:center;padding:8px;">No cached image</div>'
        )

    if url:
        tile = (
            f'<a href="{url}" target="_blank" rel="noopener" style="text-decoration:none;color:inherit;">'
            f"{img_html}"
            f'<div style="text-align:center;font-size:0.8rem;margin-top:4px;line-height:1.2;">{name}{qty_badge}</div>'
            f'<div style="text-align:center;font-size:0.72rem;color:#4da3ff;">🔗 Scryfall</div>'
            f"</a>"
        )
    else:
        tile = (
            f"{img_html}"
            f'<div style="text-align:center;font-size:0.8rem;margin-top:4px;line-height:1.2;">{name}{qty_badge}</div>'
        )

    st.markdown(tile, unsafe_allow_html=True)


def render_grid(df, key_prefix, columns_per_row=5):
    if df.empty:
        st.info("No cards match the current filters.")
        return

    total = len(df)
    ctrl1, ctrl2 = st.columns([1, 1])
    # Default is 48 (index 2 of [12, 24, 48, 96]), per Phase 2.
    page_size = ctrl1.selectbox("Cards per page", PAGE_SIZE_OPTIONS, index=2, key=f"{key_prefix}_pagesize")
    n_pages = max(1, math.ceil(total / page_size))
    page = ctrl2.number_input("Page", min_value=1, max_value=n_pages, value=1, step=1, key=f"{key_prefix}_page")

    start = (page - 1) * page_size
    page_df = df.iloc[start : start + page_size]
    st.caption(f"Showing {start + 1}–{min(start + page_size, total)} of {total}")

    cols = st.columns(columns_per_row)
    for i, (_, row) in enumerate(page_df.iterrows()):
        with cols[i % columns_per_row]:
            _render_card_tile(row)


# ------------------------------------------------------------------
# Home-page deck landing grid (Prompt Pass 4) — image-backed tiles that
# navigate straight to the Decks page with a deck pre-selected.
# ------------------------------------------------------------------
def deck_image_src(row, embed=False):
    """Resolve the best available thumbnail for a deck: a user-set cover
    image first (Editor -> Deck Info -> Cover image), then the
    commander's own card art (local cache, else Scryfall's remote URL),
    else None so the caller renders a placeholder (or, on the Decks
    page's header as of Prompt Pass 10, simply omits the image entirely
    — "if available" per that prompt, not a placeholder box). Public
    (no leading underscore) since Prompt Pass 10 made this a second
    caller alongside render_deck_landing_grid() below: the Decks page's
    own header now reuses this exact fallback chain instead of
    duplicating it a third time — was previously private/single-caller.
    `row` needs the same shape queries.list_decks_with_covers() returns
    (cover_image_path, commander_local_image_path,
    commander_image_uri) — a dict or a single pandas Series row both
    work, since both support .get(). `embed` works as in _local_src():
    a static URL on screen, a data: URI for the downloadable deck sheet."""
    src = _local_src(row.get("cover_image_path"), embed) or _local_src(
        row.get("commander_local_image_path"), embed
    )
    if src:
        return src
    remote = row.get("commander_image_uri")
    if isinstance(remote, str) and remote.startswith("http"):
        return remote
    return None


def deck_status_strip(row):
    """One-line status for a deck tile from a deck_comparison() row:
    "🔨 brewing · 9 planned · 18 ideas · 1-2 · 4% built". Parts that don't
    apply are left out (no "0 planned"), the record shows only once a game
    is logged, and build status only while the deck isn't fully sleeved — a
    finished deck's 100% would be noise on 11 of 12 tiles. Empty string when
    nothing applies.

    The lifecycle flag (Phase 6) leads, because it changes how to read
    everything after it: 4% built is alarming on a deck you thought was
    finished and expected on one you're still assembling."""
    def n(key):
        v = row.get(key)
        return 0 if v is None or pd.isna(v) else int(v)

    parts = []
    state = row.get("build_state")
    if isinstance(state, str) and state:
        parts.append({"brewing": "🔨 brewing", "dismantled": "🔧 dismantled"}.get(state, state))
    if n("planned"):
        parts.append(f"{n('planned')} planned")
    if n("ideas"):
        parts.append(f"{n('ideas')} idea{'s' if n('ideas') != 1 else ''}")
    if n("games"):
        parts.append(f"{n('wins')}-{n('losses')}")
    pct = row.get("build_pct")
    if pct is not None and not pd.isna(pct) and pct < 100:
        parts.append(f"{pct:.0f}% built")
    return " · ".join(parts)


def render_deck_landing_grid(decks_df, deck_page_path="Decks", columns_per_row=4, status_df=None):
    """Home-page landing grid: one image-backed tile per deck. Clicking
    a tile navigates (plain <a> href, same tab) straight to the Decks
    page with `?deck_id=<id>` in the URL, which that page reads on load
    to pre-select the deck — see pages/2_Decks.py. Mirrors the existing
    Scryfall-link tile pattern in render_grid()/_render_card_tile()
    above, just pointed at an internal page instead of an external URL."""
    if decks_df is None or decks_df.empty:
        st.info("No decks found yet.")
        return

    # Phase 5: optional per-deck status line (queries.deck_comparison()
    # rows) under each name; tiles render exactly as before without it.
    strips = {}
    if status_df is not None and not status_df.empty:
        strips = {r["deck_id"]: deck_status_strip(r) for _, r in status_df.iterrows()}

    cols = st.columns(columns_per_row)
    for i, (_, row) in enumerate(decks_df.iterrows()):
        name = html.escape(str(row.get("name") or "Deck"))
        deck_id = row.get("deck_id")
        strip = html.escape(strips.get(deck_id, ""))
        strip_html = (
            f'<div style="text-align:center;font-size:0.8rem;opacity:0.7;line-height:1.3;">{strip}</div>'
            if strip else ""
        )
        href = f"{deck_page_path}?deck_id={deck_id}"
        src = deck_image_src(row)

        if src:
            img_html = (
                f'<img src="{src}" style="width:100%;border-radius:10px;display:block;'
                f'box-shadow:0 1px 4px rgba(0,0,0,0.35);" alt="{name}"/>'
            )
        else:
            img_html = (
                '<div style="width:100%;aspect-ratio:5/7;background:#2b2b2b;border-radius:10px;'
                'display:flex;align-items:center;justify-content:center;color:#999;'
                'font-size:0.85rem;text-align:center;padding:8px;">No cover image</div>'
            )

        tile = (
            f'<a href="{href}" target="_self" style="text-decoration:none;color:inherit;">'
            f"{img_html}"
            f'<div style="text-align:center;font-size:0.95rem;margin-top:6px;line-height:1.3;'
            f'font-weight:600;">{name}</div>'
            f"{strip_html}"
            f"</a>"
        )
        with cols[i % columns_per_row]:
            st.markdown(tile, unsafe_allow_html=True)


# ------------------------------------------------------------------
# Deck page header + themed accents (Prompt Pass 10 / prompt3.txt) —
# replaces the Decks page's old generic "🃏 Decks" page title and its
# separate st.header(representative)/st.caption(description) blocks
# with one branded header, plus page-wide color-identity CSS accents.
# ------------------------------------------------------------------
def render_deck_header(deck_name, tagline, image_src, color_identity_str):
    """The Decks page's branded header (Prompt Pass 10): the deck's own
    NAME (decks.name) as the actual page title — not the separate,
    user-editable "representative" field the old header showed instead
    — next to its resolved thumbnail (image_src — see deck_image_src()
    above; "if available" per the prompt, so no image at all rather than
    a placeholder box when nothing resolves, unlike the home-page grid
    which always needs SOME tile art to keep its layout from looking
    broken), the deck's short descriptive tagline directly beneath the
    name (moved here in this pass from its old spot below the
    Commander/Partner/.../Built stat grid), and a row of the deck's own
    official Scryfall mana-symbol badges for its color identity."""
    name_html = html.escape(str(deck_name or "Deck"))
    tagline_html = (
        f'<div style="opacity:0.75;font-size:1.05rem;margin-top:2px;">{html.escape(str(tagline))}</div>'
        if tagline else ""
    )
    symbol_urls = fmt.deck_color_identity_symbol_urls(color_identity_str)
    symbols_html = "".join(
        f'<img src="{u}" width="26" height="26" style="margin-right:4px;vertical-align:middle;" '
        f'alt="mana symbol"/>'
        for u in symbol_urls
    )
    symbols_row = f'<div style="margin-top:8px;">{symbols_html}</div>' if symbols_html else ""

    name_block = (
        f'<div style="font-size:2.1rem;font-weight:700;line-height:1.2;">{name_html}</div>'
        f"{tagline_html}{symbols_row}"
    )

    if image_src:
        img_html = (
            f'<img src="{image_src}" style="width:96px;border-radius:10px;display:block;'
            f'box-shadow:0 1px 4px rgba(0,0,0,0.35);" alt="{name_html}"/>'
        )
        block = (
            '<div style="display:flex;align-items:center;gap:18px;">'
            f"{img_html}<div>{name_block}</div>"
            "</div>"
        )
    else:
        block = name_block

    st.markdown(block, unsafe_allow_html=True)


def inject_deck_accent_css(gradient_css, accent_hex):
    """Subtle per-deck themed accents (Prompt Pass 10 / prompt3.txt) so
    the Decks page isn't flat black-and-white: recolors this page's
    st.divider() rules (plain <hr> elements) with the deck's own
    color-identity gradient (fmt.deck_accent_gradient()), and gives this
    page's expander (the Moxfield-export panel) and the sidebar a
    colored accent edge in the deck's dominant color
    (fmt.deck_accent_hex()). Call once per page render, AFTER the
    current deck (and therefore its accent colors) is known — it's
    deck-specific and must be (re-)called every rerun once the deck picker
    has settled, so switching decks in the sidebar re-themes the page.
    Caveat: `hr`/stExpander/stSidebar are
    Streamlit's own rendered/internal DOM structure, not all guaranteed
    stable public API across versions — this hasn't been checked against
    a real running Streamlit (no live Streamlit in this project's build
    sandbox, per every prior phase's own testing caveat), so a future
    Streamlit upgrade is the first place to check if these accents ever
    stop appearing."""
    st.markdown(
        f"""
        <style>
        hr {{
            height: 4px;
            border: none;
            border-radius: 2px;
            background: {gradient_css};
            opacity: 0.9;
        }}
        div[data-testid="stExpander"] details {{
            border-left: 4px solid {accent_hex};
            border-radius: 4px;
        }}
        section[data-testid="stSidebar"] {{
            border-right: 3px solid {accent_hex};
        }}
        </style>
        """,
        unsafe_allow_html=True,
    )


# ------------------------------------------------------------------
# Win-rate conditional formatting (Prompt Pass 9 / prompt2.txt) — shared
# by the Commander Game Tracking page's Win-by-Deck summary, Win-by-
# Player summary, and Head-to-Head matrix, so all three read the same
# color scale off one function rather than three separate st.dataframe
# call sites each rolling their own. This is the one spot in the app
# that hands st.dataframe() a pandas Styler instead of a plain
# DataFrame — everywhere else (render_table(), the two summary/matrix
# call sites before this pass) just passes a DataFrame straight through.
# ------------------------------------------------------------------
def style_win_rate_percentages(
    df, columns=None, low=fmt.WIN_RATE_LOW_THRESHOLD, high=fmt.WIN_RATE_HIGH_THRESHOLD, na_rep="\u2014"
):
    """Build a pandas Styler over `df` that renders each cell in
    `columns` (win-rate floats, 0.0-1.0) as a whole-percent string and
    colors its background on fmt.win_rate_background_color()'s bucketed
    red/amber/green scale (below `low` red, above `high` green, in
    between amber). `columns=None` (the default) styles every column —
    the Head-to-Head matrix, where every cell is itself a win rate;
    pass an explicit list (e.g. ["Win %"]) for a table like the
    Win-by-Deck/Win-by-Player summaries, which also
    carry non-win-rate columns (deck/player name, games played, ...)
    that must pass through untouched. NaN cells (e.g. the Head-to-Head
    diagonal, or a deck/player with no recorded result) render as
    `na_rep` and stay unstyled, same as fmt.win_rate_background_color()
    returning None for them.

    Pandas 2.1 renamed Styler.applymap() to Styler.map() and pandas 3.0
    removed applymap() outright; this project's floor is pandas>=2.0
    (which only has applymap()), so this picks whichever the installed
    pandas actually offers rather than hardcoding one."""
    cols = list(columns) if columns is not None else list(df.columns)
    styler = df.style.format(
        lambda v: f"{v * 100:.0f}%" if pd.notna(v) else na_rep, subset=cols
    )
    elementwise = styler.map if hasattr(styler, "map") else styler.applymap
    return elementwise(lambda v: fmt.win_rate_cell_style(v, low, high), subset=cols)
