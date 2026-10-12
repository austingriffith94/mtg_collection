"""Moxfield-style "Stacks" deck board (VISUAL_DECK_PLAN.md, Phases A-B).

Shows a deck as overlapping card stacks grouped by type / mana value /
strategy tag / color, with a full-card hover preview. Grouping, hovering and
preview all happen in the browser (no Streamlit rerun); only the chosen
group-by is sent back (setStateValue) so it survives reruns.

Split so the logic stays testable:
  - board_payload(): PURE. Turns the deck dataframe into the list of plain
    card dicts the JS receives. No Streamlit, no I/O beyond reading a card's
    cached-image mtime (via card_view.card_image_src). Unit-tested.
  - apply_board_action(): Phase B's edit handler. Applies one action the
    component sent back ({"op": "inc", "id": ...}) through the existing
    writes.* functions and returns a message plus the inverse action for
    one-level undo. No Streamlit and no SQL of its own; unit-tested.
  - render_deck_board(): the page-facing wrapper that mounts the component.

The JS and CSS live as real files under dashboard_lib/components/ (editor
highlighting, readable diffs) and are read at import time into multi-line
strings, which st.components.v2 treats as inline content. (A single-line,
path-like string would be read as a packaged-asset reference and error
without a component manifest; a file's contents are always multi-line.) No
npm / build step. Graduate to a packaged component only if the JS outgrows
one file.

The component is declared ONCE at import — re-declaring it per call confuses
CCv2.

The payload carries URLs only (card_view.card_image_src: the static/ URL,
else Scryfall's), never a filesystem path or base64, so it stays small
(UI plan Phase 4's rule).
"""
import json
import os

import pandas as pd
import streamlit as st

from dashboard_lib import card_view, formatting as fmt, loaders, queries as q, writes

_COMPONENT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "components")

# Order the stacks appear in when grouping by Type. Land last; anything the
# type buckets don't cover ("Other") is appended by the JS.
BOARD_TYPE_ORDER = [
    "Creature", "Instant", "Sorcery", "Artifact", "Enchantment",
    "Planeswalker", "Battle", "Land",
]
GROUP_BY_OPTIONS = ["Type", "Mana value", "Strategy tag", "Color"]
DEFAULT_GROUP_BY = "Type"


def _read_component_file(name):
    with open(os.path.join(_COMPONENT_DIR, name), encoding="utf-8") as f:
        return f.read()


_BOARD = st.components.v2.component(
    "mtg_deck_board",
    css=_read_component_file("deck_board.css"),
    js=_read_component_file("deck_board.js"),
)


def _clean_str(value):
    """None for NaN/None/blank, else the stripped string — a pandas column
    that is all-NULL comes back as float NaN, which is truthy."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip()
    return text or None


def _split_csv(value):
    text = _clean_str(value)
    return [p.strip() for p in text.split(",") if p.strip()] if text else []


def _front_face(name):
    return (name or "").split(" // ")[0].strip().lower()


def board_payload(cards_df, deck_meta, owned_qty):
    """The mainboard as a list of card dicts for the component (one per
    deck_cards row — a card with quantity 30 is one dict with qty 30).

    cards_df   : loaders.load_deck_cards_df() (card columns + quantity +
                 strategy_tags). Derived columns are recomputed here from
                 type_line, so a raw queries.deck_cards_dataframe() also works.
    deck_meta  : loaders.load_deck_meta() — commander / partner names.
    owned_qty  : loaders.load_owned_card_quantities() — {front-face-lowercase
                 name: {...}}; a card with no entry is flagged not_owned
                 (basic lands never are: they're untracked by design).

    Sorted commander(s) first, then mana value, then name; the JS relies on
    this order within each stack.
    """
    if cards_df is None or cards_df.empty:
        return []

    commander_names = {
        n.strip().lower()
        for n in (deck_meta.get("commander"), deck_meta.get("partner"))
        if _clean_str(n)
    }
    owned_qty = owned_qty or {}

    cards = []
    for _, row in cards_df.iterrows():
        name = _clean_str(row.get("name")) or "Unknown card"
        type_line = _clean_str(row.get("type_line")) or ""
        qty = row.get("quantity")
        cmc = row.get("cmc")
        price = row.get("current_price_usd")
        is_basic = bool(row.get("is_basic_land")) if pd.notna(row.get("is_basic_land")) else False
        cards.append({
            "id": row["scryfall_id"],
            "name": name,
            "qty": int(qty) if pd.notna(qty) else 1,
            "cmc": float(cmc) if pd.notna(cmc) else 0.0,
            "mana_cost": _clean_str(row.get("mana_cost")) or "",
            "type_line": type_line,
            "img": card_view.card_image_src(row),
            "type": fmt.derive_card_type(type_line),
            "is_land": fmt.has_land_face(type_line),
            "colors": _split_csv(row.get("color_identity")),
            "tags": _split_csv(row.get("strategy_tags")),
            "price": float(price) if pd.notna(price) else None,
            "flags": {
                "game_changer": bool(row.get("is_game_changer")) if pd.notna(row.get("is_game_changer")) else False,
                "reserved": bool(row.get("is_reserved")) if pd.notna(row.get("is_reserved")) else False,
                "not_owned": not is_basic and _front_face(name) not in owned_qty,
                "commander": name.lower() in commander_names,
            },
        })

    cards.sort(key=lambda c: (not c["flags"]["commander"], c["cmc"], c["name"].lower()))
    return cards


# ------------------------------------------------------------------
# Phase B: edits from the board.
#
# The component's card menu sends one of BOARD_OPS. UNDO_OPS are never sent
# by the menu: they only exist as the inverse of a forward op, because some
# inverses need something the forward ops can't express (putting a removed
# card back at its old quantity; un-shortlisting a card).
#
# History (deck_changes) follows from the write functions used, by design:
#   inc / dec (staying >= 1)  quantity bump -> no history row
#   dec to 0 / remove         one `applied` removal row
#   to_shortlist              one `applied` removal row + an open `idea`
#   set_qty onto the board    one `applied` add row
#   from_shortlist            the idea -> `dropped`, one `applied` add row
#   set_tags                  tags aren't history -> no row
# Undo never deletes history: undoing a remove logs the re-add as its own
# applied row, so history records the mistake and the fix (plan decision).
# ------------------------------------------------------------------
BOARD_OPS = ("inc", "dec", "remove", "to_shortlist", "set_tags")
UNDO_OPS = ("set_qty", "from_shortlist")
STRATEGY_TAG_TYPE = "strategy"


def _qty_label(name, qty):
    return f"{name} ×{qty}" if qty and qty > 1 else name


def _positive_int(value, what):
    # JSON numbers from the browser may arrive as floats (2.0); bool is an
    # int subclass in Python and must not pass as 1.
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != int(value) or value < 1:
        raise ValueError(f"That edit had an invalid {what}.")
    return int(value)


def _validate_action(action):
    """(op, scryfall_id) for a well-formed action, else ValueError with a
    message fit for a toast. Op-specific fields are checked here too, so
    the handler below never half-applies a malformed payload."""
    if not isinstance(action, dict):
        raise ValueError("That edit didn't come through properly — nothing changed.")
    op = action.get("op")
    if op not in BOARD_OPS + UNDO_OPS:
        raise ValueError(f"Unknown board action {op!r} — nothing changed.")
    scryfall_id = action.get("id")
    if not isinstance(scryfall_id, str) or not scryfall_id.strip():
        raise ValueError("That edit didn't say which card it was for — nothing changed.")
    if op == "set_tags":
        tags = action.get("tags")
        if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
            raise ValueError("That tag edit had no list of tags — nothing changed.")
    if op in UNDO_OPS:
        _positive_int(action.get("qty"), "quantity")
    if op == "from_shortlist":
        _positive_int(action.get("change_id"), "Shortlist entry")
    return op, scryfall_id.strip()


def apply_board_action(conn, deck_id, action):
    """Apply one board edit and return {"message", "undo"}.

    `message` is a short past-tense line for a toast / the Undo button
    ("Removed Sol Ring"). `undo` is the action that reverses this one —
    itself a valid input here, so the page just feeds it back — or None
    when nothing changed.

    Quantities are read from the database, never from the action: the
    browser's payload can be a rerun stale, and a double-click on −1 must be
    two decrements, not two writes of the same stale number.

    Raises ValueError (user-facing message, nothing written) for a
    malformed action, a card that isn't in the deck any more, or a write
    function's own refusal (e.g. the card is already on the Shortlist)."""
    op, sid = _validate_action(action)
    entry = q.deck_card_entry(conn, deck_id, sid)
    if entry is None:
        raise ValueError("That card isn't in the card database any more — nothing changed.")
    name, qty = entry["name"], entry["quantity"]
    if qty is None and op in BOARD_OPS:
        raise ValueError(f"{name} isn't in this deck any more — the board was out of date, nothing changed.")

    if op == "inc":
        writes.bulk_update_deck_cards(conn, deck_id, {sid: qty + 1})
        return {"message": f"{name} → ×{qty + 1}", "undo": {"op": "dec", "id": sid}}

    if op == "dec":
        if qty <= 1:
            # bulk_update_deck_cards treats 0 as a removal (and logs it);
            # the inverse has to put the card back, which `inc` can't.
            writes.bulk_update_deck_cards(conn, deck_id, {sid: 0})
            return {"message": f"Removed {name}", "undo": {"op": "set_qty", "id": sid, "qty": qty}}
        writes.bulk_update_deck_cards(conn, deck_id, {sid: qty - 1})
        return {"message": f"{name} → ×{qty - 1}", "undo": {"op": "inc", "id": sid}}

    if op == "remove":
        writes.remove_deck_card(conn, deck_id, sid)
        return {"message": f"Removed {_qty_label(name, qty)}",
                "undo": {"op": "set_qty", "id": sid, "qty": qty}}

    if op == "to_shortlist":
        # Shortlist first: add_change refuses a duplicate open row, and
        # failing there leaves the card on the board untouched. The idea
        # keeps the old quantity so it can come back as it was.
        change_id = writes.add_change(conn, deck_id, add_scryfall_id=sid, add_name=name, quantity=qty)
        writes.remove_deck_card(conn, deck_id, sid)
        return {"message": f"Moved {_qty_label(name, qty)} to the Shortlist",
                "undo": {"op": "from_shortlist", "id": sid, "qty": qty, "change_id": change_id}}

    if op == "set_tags":
        old = writes.get_card_tags_by_type(conn, deck_id, STRATEGY_TAG_TYPE).get(sid, [])
        new = action["tags"]
        if {t.strip().lower() for t in old} == {t.strip().lower() for t in new if t.strip()}:
            return {"message": f"No tag changes on {name}", "undo": None}
        writes.set_card_tags(conn, deck_id, sid, STRATEGY_TAG_TYPE, new)
        labels = ", ".join(t.strip() for t in new if t.strip())
        return {"message": f"Tagged {name}: {labels}" if labels else f"Cleared tags on {name}",
                "undo": {"op": "set_tags", "id": sid, "tags": list(old)}}

    target = _positive_int(action["qty"], "quantity")

    if op == "set_qty":
        if qty == target:
            return {"message": f"{_qty_label(name, qty)} is already in the deck", "undo": None}
        if qty is None:
            # A genuine re-add: add_deck_card logs the applied add row.
            writes.add_deck_card(conn, deck_id, sid, target)
            return {"message": f"Put back {_qty_label(name, target)}", "undo": {"op": "remove", "id": sid}}
        writes.bulk_update_deck_cards(conn, deck_id, {sid: target})
        return {"message": f"{name} → ×{target}", "undo": {"op": "set_qty", "id": sid, "qty": qty}}

    # from_shortlist: retire the idea to_shortlist created, then re-add.
    # Dropped (not deleted) — history keeps it. Done before the re-add so
    # add_deck_card's auto-drop doesn't also stamp it "added by hand". An
    # idea already applied/dropped/moved since is left alone.
    change_id = int(action["change_id"])
    change = writes.get_change(conn, change_id)
    if (change and change["deck_id"] == deck_id and change["add_scryfall_id"] == sid
            and change["status"] in writes.OPEN_CHANGE_STATUSES):
        writes.drop_change(conn, change_id)
    if qty is None:
        writes.add_deck_card(conn, deck_id, sid, target)
    return {"message": f"Moved {_qty_label(name, target)} back from the Shortlist",
            "undo": {"op": "to_shortlist", "id": sid}}


# ------------------------------------------------------------------
# Stats strip + the Streamlit side of editing (callback, undo state).
# ------------------------------------------------------------------
DECK_SIZE = 100            # Commander; the strip compares the card count to it
CURVE_BUCKETS = 8          # mana values 0..6 and a "7+" bucket
# Scope of the rerun after a board edit. "app" follows UI plan Phase 4's rule
# (a write inside a fragment ends in a full rerun) and matters here: the
# Mainboard tab's data_editor would otherwise keep stale quantities and its
# Save would write them back over the edit. See VISUAL_DECK_PLAN.md.
BOARD_RERUN_SCOPE = "app"


def board_stats(cards_df):
    """Numbers for the strip above the board, from the same dataframe the
    board uses (no extra query): total cards (quantity-weighted, commander
    included, as deck_cards stores it), lands, average mana value of
    nonlands, and the nonland curve as CURVE_BUCKETS counts.

    "Land" is any card with a land face (MDFC-aware, like the payload's
    is_land and queries.deck_mana_curve), so a spell//land counts as a land
    here and stays out of the curve and the average."""
    curve = [0] * CURVE_BUCKETS
    total = lands = 0
    mv_sum = nonland = 0.0
    if cards_df is not None and not cards_df.empty:
        for _, row in cards_df.iterrows():
            qty = int(row["quantity"]) if pd.notna(row.get("quantity")) else 1
            total += qty
            if fmt.has_land_face(_clean_str(row.get("type_line")) or ""):
                lands += qty
                continue
            cmc = float(row["cmc"]) if pd.notna(row.get("cmc")) else 0.0
            nonland += qty
            mv_sum += qty * cmc
            curve[min(int(cmc), CURVE_BUCKETS - 1)] += qty
    return {
        "total": total, "lands": lands, "nonlands": int(nonland),
        "avg_mv": (mv_sum / nonland) if nonland else None,
        "curve": curve,
    }


def _undo_key(key):
    return f"{key}__undo"


def _dirty_key(key):
    return f"{key}__edited"


def run_board_action(conn, deck_id, key, action, *, is_undo=False):
    """Apply one action for the page: toast the outcome (or the refusal),
    remember the inverse for the Undo button, drop stale caches and flag the
    page to rerun. Returns True if something was applied.

    An undo itself leaves nothing to undo (one level); a failed or no-op
    edit leaves the previous Undo in place."""
    try:
        result = apply_board_action(conn, deck_id, action)
    except ValueError as exc:
        st.toast(str(exc), icon="⚠️")
        return False
    st.toast(result["message"], icon="↩️" if is_undo else "✅")
    if is_undo:
        st.session_state.pop(_undo_key(key), None)
    elif result["undo"]:
        st.session_state[_undo_key(key)] = {"label": result["message"], "action": result["undo"]}
    else:
        return True  # no-op: nothing changed, so no cache clear or rerun
    loaders.invalidate_deck_caches(deck_id)
    st.session_state[_dirty_key(key)] = True
    return True


def clear_board_undo(key):
    """Forget the pending undo — for edits made elsewhere on the page (the
    add bar), which the one-level undo can't reverse."""
    st.session_state.pop(_undo_key(key), None)


def rerun_if_edited(key):
    """Call at the top of the board's fragment: a callback flagged an edit,
    so end the run with the full-page rerun before drawing anything stale."""
    if st.session_state.pop(_dirty_key(key), False):
        st.rerun(scope=BOARD_RERUN_SCOPE)


def render_undo_button(conn, deck_id, key):
    """"↶ Undo: …" for the last board edit, until the next one."""
    pending = st.session_state.get(_undo_key(key))
    if not pending:
        return
    st.button(
        f"↶ Undo: {pending['label']}", key=f"{key}__undo_btn",
        help="Reverses the last edit made on the board. History keeps both rows.",
        on_click=run_board_action, args=(conn, deck_id, key, pending["action"]),
        kwargs={"is_undo": True},
    )


def render_deck_board(cards_df, deck_meta, owned_qty, *, key, editable=False, conn=None, deck_id=None):
    """Mount the Stacks board. `key` must be unique per deck: it also keys the
    saved group-by in session_state.

    editable=True (needs conn + deck_id) lets the card menu send edits back;
    each one runs run_board_action() in the component callback — before the
    script body, so the board below is drawn from fresh data."""
    if editable and (conn is None or deck_id is None):
        raise ValueError("An editable board needs conn and deck_id.")
    saved = st.session_state.get(key)
    group_by = (saved.get("group_by") if saved else None) or DEFAULT_GROUP_BY
    if group_by not in GROUP_BY_OPTIONS:
        group_by = DEFAULT_GROUP_BY

    def _on_action():
        action = (st.session_state.get(key) or {}).get("action")
        if action:
            run_board_action(conn, deck_id, key, action)

    callbacks = {"on_group_by_change": lambda: None}
    if editable:
        callbacks["on_action_change"] = _on_action
    return _BOARD(
        key=key,
        data={
            "cards": board_payload(cards_df, deck_meta, owned_qty),
            "group_by": group_by,
            "group_options": GROUP_BY_OPTIONS,
            "type_order": BOARD_TYPE_ORDER,
            "editable": editable,
        },
        # Registering a callback is what makes its field a result field.
        # group_by's does nothing: it only has to persist across reruns.
        **callbacks,
    )


def payload_size_bytes(cards):
    """JSON size of a board payload — for the size check in the tests/notes."""
    return len(json.dumps(cards, separators=(",", ":")).encode("utf-8"))
