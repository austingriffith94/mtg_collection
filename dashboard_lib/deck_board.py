"""Moxfield-style "Stacks" deck board (VISUAL_DECK_PLAN.md, Phase A: read-only).

Shows a deck as overlapping card stacks grouped by type / mana value /
strategy tag / color, with a full-card hover preview. Grouping, hovering and
preview all happen in the browser (no Streamlit rerun); only the chosen
group-by is sent back (setStateValue) so it survives reruns.

Split in two so the logic stays testable:
  - board_payload(): PURE. Turns the deck dataframe into the list of plain
    card dicts the JS receives. No Streamlit, no I/O beyond reading a card's
    cached-image mtime (via card_view.card_image_src). Unit-tested.
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

from dashboard_lib import card_view, formatting as fmt

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


def render_deck_board(cards_df, deck_meta, owned_qty, *, key, editable=False):
    """Mount the Stacks board. `editable` is reserved for Phase B (the JS
    ignores it for now). `key` must be unique per deck: it also keys the
    saved group-by in session_state."""
    saved = st.session_state.get(key)
    group_by = (saved.get("group_by") if saved else None) or DEFAULT_GROUP_BY
    if group_by not in GROUP_BY_OPTIONS:
        group_by = DEFAULT_GROUP_BY

    return _BOARD(
        key=key,
        data={
            "cards": board_payload(cards_df, deck_meta, owned_qty),
            "group_by": group_by,
            "group_options": GROUP_BY_OPTIONS,
            "type_order": BOARD_TYPE_ORDER,
            "editable": editable,
        },
        # Registering the callback is what makes `group_by` a result field
        # and persists it across reruns; there's nothing to do on change.
        on_group_by_change=lambda: None,
    )


def payload_size_bytes(cards):
    """JSON size of a board payload — for the size check in the tests/notes."""
    return len(json.dumps(cards, separators=(",", ":")).encode("utf-8"))
