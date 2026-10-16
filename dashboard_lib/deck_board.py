"""Moxfield-style "Stacks" deck board (docs/archive/VISUAL_DECK_PLAN.md, Phases A-C).

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
    Phase C adds the Shortlist ops (plan / unpair / apply / drop / add) to it.
    docs/archive/EDIT_FLOW_PLAN.md Phase 1: every op also returns the copies it moved,
    and Undo puts them back (writes.revert_moves). Phase 2: an add (`add`,
    `apply`) may name a storage Location to take its copy from, and a
    removal (`dec`, `remove`, `to_shortlist`) the Location to put it in.
    Phase 3: an add may also name another deck — a take, which changes
    that deck's list too (writes.transfer_between_decks), so it has no Undo;
    `drop` on a take drops both halves.
  - leaving_payload(): PURE. Phase 3's "Leaving" rail group — this deck's
    cards another deck has a take of.
  - shortlist_payload() / planned_cuts(): PURE. Phase C's Shortlist column,
    with each row's availability verdict allocated across every deck.
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

from dashboard_lib import card_resolver, card_view, formatting as fmt, loaders, queries as q, writes

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


def board_payload(cards_df, deck_meta, owned_qty, cut_for=None, leaving=None, buying=None):
    """The mainboard as a list of card dicts for the component (one per
    deck_cards row — a card with quantity 30 is one dict with qty 30).

    cards_df   : loaders.load_deck_cards_df() (card columns + quantity +
                 strategy_tags). Derived columns are recomputed here from
                 type_line, so a raw queries.deck_cards_dataframe() also works.
    deck_meta  : loaders.load_deck_meta() — commander / partner names.
    owned_qty  : loaders.load_owned_card_quantities() — {front-face-lowercase
                 name: {...}}; a card with no entry is flagged not_owned
                 (basic lands never are: they're untracked by design).
    cut_for    : planned_cuts() — {scryfall_id: [incoming card names]}; each
                 card a planned swap would cut carries those names as
                 `cut_for` (empty list otherwise) for its "cut for Y" badge.
    leaving    : queries.deck_leaving() — {scryfall_id: [receiving decks]};
                 a card another deck has a take of carries those names as
                 `leaving` for its "→ Prosper" badge (docs/archive/EDIT_FLOW_PLAN.md
                 Phase 3).
    buying     : queries.deck_buying() — {scryfall_id: copies still to buy};
                 a card on the list from a Buy add with no copy sleeved yet
                 carries `flags.buying` for its "buying" badge (Phase 4) and
                 is NOT flagged not_owned: that badge means owned zero with no
                 decision made, this one means the decision is made.

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
    cut_for = cut_for or {}
    leaving = leaving or {}
    buying = buying or {}

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
            "cut_for": list(cut_for.get(row["scryfall_id"], [])),
            "leaving": list(leaving.get(row["scryfall_id"], [])),
            # Phase 7: a basic's copies never move, so its menu asks no "to…".
            "basic": is_basic,
            "flags": {
                "game_changer": bool(row.get("is_game_changer")) if pd.notna(row.get("is_game_changer")) else False,
                "reserved": bool(row.get("is_reserved")) if pd.notna(row.get("is_reserved")) else False,
                "not_owned": (not is_basic and _front_face(name) not in owned_qty
                              and row["scryfall_id"] not in buying),
                "buying": row["scryfall_id"] in buying,
                "commander": name.lower() in commander_names,
            },
        })

    cards.sort(key=lambda c: (not c["flags"]["commander"], c["cmc"], c["name"].lower()))
    return cards


# ------------------------------------------------------------------
# Phase C: the Shortlist column.
#
# Availability comes from queries.plan_availability() run over EVERY deck's
# open changes, not just this deck's: the allocator only sees a collision
# (two decks wanting your one copy) when both rows are in its input — the
# Workbench's view. The Deck Editor's Shortlist tab still allocates over one
# deck; the Board deliberately matches the Workbench instead.
# ------------------------------------------------------------------
# plan_availability verdict -> the badge's color class in the component.
# Three colors carry the decision (have it / unsleeve or settle a conflict /
# buy); the two verdicts that need no sourcing get a neutral badge.
VERDICT_BADGES = {
    "available": "available",
    "claimed_by": "elsewhere",
    "in_other_deck": "elsewhere",
    "wrong_location": "elsewhere",
    "not_owned": "buy",
    "buying": "buying",    # decided, so its own color rather than the "buy it" red
    "already_here": "neutral",
    "no_add": "neutral",
}
SHORTLIST_STATUS_ORDER = ("idea", "planned")   # the column's Ideas, then Planned


def shortlist_availability(conn, open_changes_df):
    """{change_id: verdict} over every deck's open rows. `open_changes_df` is
    loaders.load_open_changes_df(conn, statuses=("idea", "planned")) with no
    deck filter (the loader's default status is planned only, which would
    hide ideas competing for the same copy). A thin wrapper so the page and
    the tests can't drift on which rows go in."""
    if open_changes_df is None or open_changes_df.empty:
        return {}
    return q.plan_availability(conn, open_changes_df)


def _source_fields(row, sources):
    """The payload fields for where an add's copy comes from (Phase 2): the
    row's saved choice, the storage Locations that could supply it (with
    their free counts) and what Auto would do — all the browser needs to
    offer the choice, with nothing it could use to name a lot."""
    saved = _clean_str(row.get("source")) if "source" in row else None
    location = _clean_str(row.get("source_location")) if "source_location" in row else None
    return {
        "source": saved,
        "source_location": location,
        "source_options": [{"location": o["location"], "quantity": o["quantity"]}
                           for o in (sources or {}).get("storage", [])],
        # Phase 3: other decks whose list runs the card. `label` says whether
        # one can hand a copy over right now; picking it also changes that
        # deck's list, which the board confirms first.
        "deck_options": [{"deck": o["deck"], "label": fmt.deck_choice_label(o)}
                         for o in (sources or {}).get("decks", []) if o.get("listed")],
        "auto_label": fmt.auto_source_label(sources),
        # Phase 4: Buy is always on offer (no Location, no lookup needed) —
        # except for a basic land (Phase 7), which has no source to choose.
        "can_buy": not (sources or {}).get("bulk"),
    }


def shortlist_payload(shortlist_df, availability, unlinked_df=None, sources=None):
    """The deck's open Shortlist as card dicts for the side column, ideas
    first then planned, each by name.

    shortlist_df : loaders.load_shortlist_df() (card columns + change_id,
                   status, quantity, replace_target, replace_id, notes).
    availability : shortlist_availability() — keyed by change_id; a row with
                   no verdict gets a neutral badge and no text.
    unlinked_df  : this deck's open rows whose add side is a typed NAME with
                   no printing (open_changes_dataframe() shape: change_id,
                   status, quantity, add_card, remove_card,
                   remove_scryfall_id, price). shortlist_df's INNER join
                   drops them, and they are most real planned swaps — every
                   swap backfilled from the old queue is one. They come out
                   with `unlinked: True`, no `id` and no image; `apply` links
                   the printing (deck_board.run_board_action passes a
                   resolver), the same as the Shortlist tab.

    sources      : {change_id: queries.card_sources()} for the choosable
                   source options (Phase 2); a row with none offers only Auto.

    `id` is the card's scryfall_id (for the image / preview); edits are keyed
    by `change_id`, since the same card can't sit on one deck's Shortlist
    twice but a change row is what the writes act on."""
    availability = availability or {}
    sources = sources or {}
    items = []
    if shortlist_df is None:
        shortlist_df = pd.DataFrame()
    for _, row in shortlist_df.iterrows():
        status = _clean_str(row.get("status"))
        if status not in SHORTLIST_STATUS_ORDER:
            continue
        change_id = int(row["change_id"])
        verdict = availability.get(change_id) or {}
        qty = row.get("quantity")
        price = row.get("current_price_usd")
        type_line = _clean_str(row.get("type_line")) or ""
        items.append({
            "change_id": change_id,
            "id": row["scryfall_id"],
            "name": _clean_str(row.get("name")) or "Unknown card",
            "status": status,
            "qty": int(qty) if pd.notna(qty) else 1,
            "img": card_view.card_image_src(row),
            "type_line": type_line,
            "type": fmt.derive_card_type(type_line),
            "price": float(price) if pd.notna(price) else None,
            "replaces": _clean_str(row.get("replace_target")),
            "replaces_id": _clean_str(row.get("replace_id")),
            "notes": _clean_str(row.get("notes")),
            "verdict": verdict.get("verdict"),
            "verdict_text": verdict.get("text") or "",
            "badge": VERDICT_BADGES.get(verdict.get("verdict"), "neutral"),
            "unlinked": False,
            **_source_fields(row, sources.get(change_id)),
        })
    if unlinked_df is not None:
        for _, row in unlinked_df.iterrows():
            status = _clean_str(row.get("status"))
            name = _clean_str(row.get("add_card"))
            if status not in SHORTLIST_STATUS_ORDER or not name:
                continue   # a pure removal has nothing to show in this column
            change_id = int(row["change_id"])
            verdict = availability.get(change_id) or {}
            qty = row.get("quantity")
            price = row.get("price")
            items.append({
                "change_id": change_id, "id": None, "name": name, "status": status,
                "qty": int(qty) if pd.notna(qty) else 1, "img": None,
                "type_line": "", "type": None,
                "price": float(price) if pd.notna(price) else None,
                "replaces": _clean_str(row.get("remove_card")),
                "replaces_id": _clean_str(row.get("remove_scryfall_id")),
                "notes": _clean_str(row.get("notes")),
                "verdict": verdict.get("verdict"),
                "verdict_text": verdict.get("text") or "",
                "badge": VERDICT_BADGES.get(verdict.get("verdict"), "neutral"),
                "unlinked": True,
                **_source_fields(row, None),
            })
    items.sort(key=lambda i: (SHORTLIST_STATUS_ORDER.index(i["status"]), i["name"].lower()))
    return items


def load_board_shortlist(conn, deck_id):
    """shortlist_payload() for one deck, verdicts allocated over every
    deck's ideas and planned rows. Cached reads (both cleared by
    loaders.invalidate_deck_caches); plan_availability itself reads the
    collection live, so a collection edit elsewhere shows on the next run."""
    open_df = loaders.load_open_changes_df(conn, statuses=SHORTLIST_STATUS_ORDER)
    unlinked = (open_df[(open_df["deck_id"] == deck_id) & open_df["add_scryfall_id"].isna()]
                if not open_df.empty else None)
    shortlist_df = loaders.load_shortlist_df(conn, deck_id)
    # Where each card could come from (Phase 2). Live reads, like the
    # allocator's: a lot moved on another page shows on the next run.
    sources = {int(r["change_id"]): q.card_sources(conn, r["scryfall_id"], deck_id)
               for _, r in shortlist_df.iterrows()}
    return shortlist_payload(shortlist_df, shortlist_availability(conn, open_df), unlinked, sources)


def load_board_leaving(conn, deck_id):
    """leaving_payload() for one deck. A live read, like the allocator's: a
    take staged on another deck's page shows here on the next run."""
    return leaving_payload(q.open_takes(conn, deck_id=deck_id))


def leaving_payload(takes):
    """This deck's cards other decks have a take of (queries.open_takes(conn,
    deck_id=...)), for the rail's "Leaving" group: [{change_id (the donor's
    removal, which `drop` acts on), id, name, qty, to_deck,
    receiver_status}]. Phase 3; text only — the card itself is on the
    board with its "→ Prosper" badge."""
    return [{"change_id": int(t["donor_change_id"]), "id": t["scryfall_id"], "name": t["card"],
             "qty": int(t["quantity"] or 1), "to_deck": t["receiver_deck_name"],
             "receiver_status": t["receiver_status"]}
            for t in takes]


def planned_cuts(shortlist_items):
    """{scryfall_id cut: [incoming names]} from shortlist_payload()'s planned
    rows — the other half of "→ replaces X": the mainboard card X gets a
    "cut for Y" badge. An idea's replace target is only a suggestion, so it
    doesn't mark the board."""
    cuts = {}
    for item in shortlist_items:
        if item["status"] == "planned" and item["replaces_id"]:
            cuts.setdefault(item["replaces_id"], []).append(item["name"])
    return cuts


# ------------------------------------------------------------------
# Phase B: edits from the board.
#
# The component's card menu sends one of BOARD_OPS. UNDO_OPS are never sent
# by the menu: they only exist as the inverse of a forward op, because some
# inverses need something the forward ops can't express (putting a removed
# card back at its old quantity; un-shortlisting a card).
#
# History (deck_changes) follows from the write functions used, by design:
#   inc / dec (staying >= 1)  one `applied` row for the difference (Phase 0)
#   dec to 0 / remove         one `applied` removal row
#   to_shortlist              one `applied` removal row + an open `idea`
#   set_qty onto the board    one `applied` add row
#   from_shortlist            the idea -> `dropped`, one `applied` add row
#   set_tags                  tags aren't history -> no row
# Undo never deletes history: undoing a remove logs the re-add as its own
# applied row, so history records the mistake and the fix (plan decision).
#
# Physical copies (docs/archive/EDIT_FLOW_PLAN.md Phase 1) follow the list through
# writes.sleeve_into_deck / unsleeve_from_deck, and every op returns the
# moves. Undo does NOT re-run that Auto rule: it changes the list only and
# hands the forward edit's moves to writes.revert_moves(), so each copy
# goes back to exactly where it came from.
#
# Phase C adds CHANGE_OPS, sent from the Shortlist column and keyed by
# `change_id` (plus `cut_id`, the mainboard card picked, for `plan`). They
# move a deck_changes row through the same writes.* transitions the Deck
# Editor's Shortlist tab and the Workbench use:
#   plan     idea -> planned, paired with cut_id      undo: unpair
#   unpair   planned -> idea, pairing kept            undo: plan (same cut)
#   apply    planned -> applied via apply_changes()   no undo: it moves
#            collection lots, so the component confirms before sending it
#   drop     idea/planned -> dropped (row kept)       undo: reopen
#   add      idea -> the card joins the mainboard     undo: to_shortlist
#            with no cut (add_deck_card); the idea is retired
#   reopen   dropped -> idea; only ever an undo (the menu never sends it)
# ------------------------------------------------------------------
BOARD_OPS = ("inc", "dec", "remove", "to_shortlist", "set_tags")
UNDO_OPS = ("set_qty", "from_shortlist")
CHANGE_OPS = ("plan", "unpair", "apply", "drop", "add", "reopen")
STRATEGY_TAG_TYPE = "strategy"


def _qty_label(name, qty):
    return f"{name} ×{qty}" if qty and qty > 1 else name


def _positive_int(value, what):
    # JSON numbers from the browser may arrive as floats (2.0); bool is an
    # int subclass in Python and must not pass as 1.
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != int(value) or value < 1:
        raise ValueError(f"That edit had an invalid {what}.")
    return int(value)


def _validate_route(action):
    """Phase 2's optional `source` / `source_location` / `dest`: strings
    (or absent) only. Whether a Location exists or has stock is the write
    functions' call (writes.normalize_source / normalize_dest) — this just
    keeps the browser from sending anything that isn't plain text."""
    for field in ("source", "source_location", "dest"):
        value = action.get(field)
        if value is not None and not isinstance(value, str):
            raise ValueError("That edit had an invalid Location — nothing changed.")


def _validate_action(action):
    """(op, scryfall_id) for a well-formed action, else ValueError with a
    message fit for a toast. Op-specific fields are checked here too, so
    the handler below never half-applies a malformed payload."""
    if not isinstance(action, dict):
        raise ValueError("That edit didn't come through properly — nothing changed.")
    op = action.get("op")
    if op not in BOARD_OPS + UNDO_OPS + CHANGE_OPS:
        raise ValueError(f"Unknown board action {op!r} — nothing changed.")
    if op in CHANGE_OPS:
        # Keyed by the Shortlist row, not a card; `id` is not needed.
        _positive_int(action.get("change_id"), "Shortlist entry")
        _validate_route(action)
        cut_id = action.get("cut_id")
        if op == "plan" and (not isinstance(cut_id, str) or not cut_id.strip()):
            raise ValueError("That swap didn't say which card to cut — nothing changed.")
        return op, None
    scryfall_id = action.get("id")
    if not isinstance(scryfall_id, str) or not scryfall_id.strip():
        raise ValueError("That edit didn't say which card it was for — nothing changed.")
    if op == "set_tags":
        tags = action.get("tags")
        if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
            raise ValueError("That tag edit had no list of tags — nothing changed.")
    _validate_route(action)
    if op in UNDO_OPS:
        _positive_int(action.get("qty"), "quantity")
    if op == "from_shortlist":
        _positive_int(action.get("change_id"), "Shortlist entry")
    return op, scryfall_id.strip()


def apply_board_action(conn, deck_id, action, resolve_add=None, undo_moves=None):
    """Apply one board edit and return {"message", "undo", "moves"}.

    `message` is a short past-tense line for a toast / the Undo button
    ("Removed Sol Ring"). `undo` is the action that reverses this one —
    itself a valid input here, so the page just feeds it back — or None
    when there is none (nothing changed, or an `apply`, which can't be
    undone). A no-op also carries "changed": False; anything else changed
    the database. `moves` is every physical copy the edit moved
    (writes.sleeve_into_deck / unsleeve_from_deck), for the pop-up and for
    the undo.

    `undo_moves` is set only when `action` is an Undo: the forward edit's
    `moves`. The list then changes without the Auto rule moving anything,
    and writes.revert_moves() puts those copies back where they came from;
    the result's `moves` are the reverting ones, and `warnings` names any
    copy that had moved on since and was left alone. It is a separate
    argument rather than part of `action` because actions also arrive from
    the browser, which must never be able to name lots to move.

    Quantities are read from the database, never from the action: the
    browser's payload can be a rerun stale, and a double-click on −1 must be
    two decrements, not two writes of the same stale number.

    Raises ValueError (user-facing message, nothing written) for a
    malformed action, a card that isn't in the deck any more, or a write
    function's own refusal (e.g. the card is already on the Shortlist).

    `resolve_add(name) -> (scryfall_id, was_new, error)` links a name-only
    Shortlist row's printing on `apply` (writes.apply_changes' hook); without
    it such a row is refused with apply_change's own message. Tests leave it
    out to stay offline; the page passes card_resolver's."""
    op, sid = _validate_action(action)
    with_moves = undo_moves is None
    if op in CHANGE_OPS:
        result = _apply_change_action(conn, deck_id, op, action, resolve_add, with_moves)
    else:
        result = _apply_card_action(conn, deck_id, op, sid, action, with_moves)
    result.setdefault("moves", [])
    if undo_moves is not None and result.get("changed", True):
        result["moves"], result["warnings"] = writes.revert_moves(conn, undo_moves)
    return result


def _apply_card_action(conn, deck_id, op, sid, action, with_moves):
    """BOARD_OPS + UNDO_OPS for apply_board_action(). `with_moves=False`
    (an Undo) changes the list and history only."""
    entry = q.deck_card_entry(conn, deck_id, sid)
    if entry is None:
        raise ValueError("That card isn't in the card database any more — nothing changed.")
    name, qty = entry["name"], entry["quantity"]
    if qty is None and op in BOARD_OPS:
        raise ValueError(f"{name} isn't in this deck any more — the board was out of date, nothing changed.")

    # Where a removal's copy goes (Phase 2); an Undo never names one.
    dest = (action.get("dest") or None) if with_moves else None

    def set_qty(new_qty):
        return writes.bulk_update_deck_cards(conn, deck_id, {sid: new_qty}, with_moves=with_moves,
                                             dest=dest)[1]

    if op == "inc":
        moves = set_qty(qty + 1)
        return {"message": f"{name} → ×{qty + 1}", "undo": {"op": "dec", "id": sid}, "moves": moves}

    if op == "dec":
        if qty <= 1:
            # bulk_update_deck_cards treats 0 as a removal (and logs it);
            # the inverse has to put the card back, which `inc` can't.
            moves = set_qty(0)
            return {"message": f"Removed {name}", "undo": {"op": "set_qty", "id": sid, "qty": qty},
                    "moves": moves}
        moves = set_qty(qty - 1)
        return {"message": f"{name} → ×{qty - 1}", "undo": {"op": "inc", "id": sid}, "moves": moves}

    if op == "remove":
        moves = writes.remove_deck_card(conn, deck_id, sid, with_moves=with_moves, dest=dest)
        return {"message": f"Removed {_qty_label(name, qty)}",
                "undo": {"op": "set_qty", "id": sid, "qty": qty}, "moves": moves}

    if op == "to_shortlist":
        # Shortlist first: add_change refuses a duplicate open row, and
        # failing there leaves the card on the board untouched. The idea
        # keeps the old quantity so it can come back as it was.
        change_id = writes.add_change(conn, deck_id, add_scryfall_id=sid, add_name=name, quantity=qty)
        moves = writes.remove_deck_card(conn, deck_id, sid, with_moves=with_moves, dest=dest)
        return {"message": f"Moved {_qty_label(name, qty)} to the Shortlist",
                "undo": {"op": "from_shortlist", "id": sid, "qty": qty, "change_id": change_id},
                "moves": moves}

    if op == "set_tags":
        old = writes.get_card_tags_by_type(conn, deck_id, STRATEGY_TAG_TYPE).get(sid, [])
        new = action["tags"]
        if {t.strip().lower() for t in old} == {t.strip().lower() for t in new if t.strip()}:
            return {"message": f"No tag changes on {name}", "undo": None, "changed": False}
        writes.set_card_tags(conn, deck_id, sid, STRATEGY_TAG_TYPE, new)
        labels = ", ".join(t.strip() for t in new if t.strip())
        return {"message": f"Tagged {name}: {labels}" if labels else f"Cleared tags on {name}",
                "undo": {"op": "set_tags", "id": sid, "tags": list(old)}}

    target = _positive_int(action["qty"], "quantity")

    if op == "set_qty":
        if qty == target:
            return {"message": f"{_qty_label(name, qty)} is already in the deck", "undo": None, "changed": False}
        if qty is None:
            # A genuine re-add: add_deck_card logs the applied add row.
            moves = writes.add_deck_card(conn, deck_id, sid, target, with_moves=with_moves)
            return {"message": f"Put back {_qty_label(name, target)}", "undo": {"op": "remove", "id": sid},
                    "moves": moves}
        moves = set_qty(target)
        return {"message": f"{name} → ×{target}", "undo": {"op": "set_qty", "id": sid, "qty": qty},
                "moves": moves}

    # from_shortlist: retire the idea to_shortlist created, then re-add.
    # Dropped (not deleted) — history keeps it. Done before the re-add so
    # add_deck_card's auto-drop doesn't also stamp it "added by hand". An
    # idea already applied/dropped/moved since is left alone.
    change_id = int(action["change_id"])
    change = writes.get_change(conn, change_id)
    if (change and change["deck_id"] == deck_id and change["add_scryfall_id"] == sid
            and change["status"] in writes.OPEN_CHANGE_STATUSES):
        writes.drop_change(conn, change_id)
    moves = writes.add_deck_card(conn, deck_id, sid, target, with_moves=with_moves) if qty is None else []
    return {"message": f"Moved {_qty_label(name, target)} back from the Shortlist",
            "undo": {"op": "to_shortlist", "id": sid}, "moves": moves}


def _route_of(action, change):
    """(source, source_location) for an add: the action's when it names
    one (the browser's "auto" or absent-with-key means Auto), else the
    change row's saved choice."""
    if "source" in action:
        source = action.get("source")
        return (None, None) if source in (None, "", "auto") else (source, action.get("source_location"))
    return change.get("source"), change.get("source_location")


def _card_label(conn, deck_id, scryfall_id, fallback):
    entry = q.deck_card_entry(conn, deck_id, scryfall_id) if scryfall_id else None
    return (entry and entry["name"]) or fallback or "that card"


def _apply_change_action(conn, deck_id, op, action, resolve_add=None, with_moves=True):
    """CHANGE_OPS for apply_board_action(). The row is re-read here and its
    state checked before writing, so a click from a stale board (the row was
    applied or dropped in another tab) gets a plain-English refusal instead
    of the write functions' terser one. Only `add` and `apply` move copies;
    `with_moves` reaches `add` (`apply` is never an Undo)."""
    change_id = int(action["change_id"])
    change = writes.get_change(conn, change_id)
    if change is None or change["deck_id"] != deck_id:
        raise ValueError("That Shortlist entry isn't on this deck any more — the board was out of date, nothing changed.")
    status = change["status"]
    add_name = _card_label(conn, deck_id, change["add_scryfall_id"], change["add_name"])
    wanted = {"plan": ("idea",), "add": ("idea",), "unpair": ("planned",), "apply": ("planned",),
              "drop": ("idea", "planned"), "reopen": ("dropped",)}[op]
    if status not in wanted:
        now = {"idea": "an idea", "planned": "already planned", "dropped": "already dropped",
               "applied": "already applied"}.get(status, status)
        raise ValueError(f"{add_name} is {now} — the board was out of date, nothing changed.")

    if op == "drop":
        donor_side = q.is_donor_row(change)
        partner_lines = writes.drop_change(conn, change_id)
        if donor_side:
            # The "Leaving" rail's Drop: this deck keeps the card. The other
            # deck's half went with it, and can only be reopened over there,
            # so there is no Undo here.
            card = _card_label(conn, deck_id, change["remove_scryfall_id"], change["remove_name"])
            return {"message": " ".join([f"Kept {card} — the take is cancelled.", *partner_lines]),
                    "undo": None}
        return {"message": " ".join([f"Dropped {add_name} from the Shortlist", *partner_lines]),
                "undo": {"op": "reopen", "change_id": change_id}}

    if op == "reopen":
        # A dropped swap comes back as an idea (its pairing is kept), the same
        # as the Shortlist tab's reopen; a take re-promises the donor's copy.
        lines = writes.reopen_change(conn, change_id) or []
        return {"message": " ".join([f"Brought {add_name} back to the Shortlist", *lines]),
                "undo": {"op": "drop", "change_id": change_id}}

    if op == "add":
        sid = change["add_scryfall_id"]
        if not sid:
            # A typed name with no printing yet: link it the way apply does.
            if resolve_add is None:
                raise ValueError(f"{add_name} isn't linked to a printing yet.")
            sid, _, err = resolve_add(change["add_name"])
            if err:
                raise ValueError(f"Couldn't find {add_name}: {err}")
        entry = q.deck_card_entry(conn, deck_id, sid)
        if entry and entry["quantity"] is not None:
            # add_deck_card would quietly bump the quantity instead.
            raise ValueError(f"{add_name} is already in the deck — drop the idea instead. Nothing changed.")
        qty = change["quantity"] or 1
        # A choice made in the menu wins; otherwise the idea's saved one.
        source, location = _route_of(action, change)
        moves = writes.add_deck_card(conn, deck_id, sid, qty, with_moves=with_moves,
                                     source=source, source_location=location)
        # add_deck_card retires an idea it can match by printing/oracle; a
        # typed-name idea it can't, so finish the job here.
        again = writes.get_change(conn, change_id)
        if again and again["status"] in writes.OPEN_CHANGE_STATUSES:
            writes.drop_change(conn, change_id)
        if source == "deck":
            # Phase 3: the card also left the other deck's list. Undoing
            # that is an edit to another deck, which a one-level Undo here
            # can't do — so, like apply, there is none.
            held = q.deck_meta(conn, deck_id).get("build_state") == "brewing"
            tail = (f" — {location} keeps its copy until you build this deck" if held
                    else f", off {location}'s list")
            return {"message": f"Took {_qty_label(add_name, qty)} from {location}{tail}",
                    "undo": None, "moves": moves}
        where = f" (from {location})" if source and location else ""
        if source == "buy":
            where = " — to buy, so it shows on the Workbench buy list"
        return {"message": f"Added {_qty_label(add_name, qty)} to the deck{where}",
                "undo": {"op": "to_shortlist", "id": sid}, "moves": moves}

    if op == "plan":
        cut_id = action["cut_id"].strip()
        cut = q.deck_card_entry(conn, deck_id, cut_id)
        if cut is None or cut["quantity"] is None:
            raise ValueError("The card picked to cut isn't in this deck any more — nothing changed.")
        if cut_id == change["add_scryfall_id"]:
            raise ValueError(f"{add_name} can't replace itself — pick a different card to cut.")
        writes.promote_change(conn, change_id, remove_scryfall_id=cut_id)
        return {"message": f"Planned: {add_name} in for {cut['name']}",
                "undo": {"op": "unpair", "change_id": change_id}}

    cut_name = _card_label(conn, deck_id, change["remove_scryfall_id"], change["remove_name"])
    if op == "unpair":
        # demote_change keeps the pairing, so the undo is the same plan.
        writes.demote_change(conn, change_id)
        undo = ({"op": "plan", "change_id": change_id, "cut_id": change["remove_scryfall_id"]}
                if change["remove_scryfall_id"] else None)
        return {"message": f"Unpaired {add_name} from {cut_name} — back to an idea", "undo": undo}

    # apply: the same path as the Shortlist tab and the Workbench, so history
    # and collection locations move identically — including linking a
    # name-only row's printing through resolve_add. A source picked in the
    # menu is saved on the row first, so History records what was applied.
    if "source" in action:
        source, location = _route_of(action, change)
        writes.update_change(conn, change_id, source=source, source_location=location)
    (result,) = writes.apply_changes(conn, [change_id], resolve_add=resolve_add)
    if not result["ok"]:
        raise ValueError(f"Couldn't apply {add_name}: {result['error']}")
    if result["pair"]:
        # Phase 3: a take, applied as a pair (or, on a brewing deck, just
        # this deck's half).
        return {"message": fmt.apply_result_line(result)[2:], "undo": None,
                "moves": result["moves"]}
    outcome = result["outcome"] or {}
    if outcome.get("created_lot"):
        lot = " (not owned — added a starter collection lot)"
    elif outcome.get("assigned_lot_id"):
        lot = " (owned copy moved into the deck)"
    else:
        lot = ""
    return {"message": f"Applied: {add_name} in, {cut_name} out{lot}", "undo": None,
            "moves": result["moves"]}


# ------------------------------------------------------------------
# Stats strip + the Streamlit side of editing (callback, undo state).
# ------------------------------------------------------------------
DECK_SIZE = 100            # Commander; the strip compares the card count to it
CURVE_BUCKETS = 8          # mana values 0..6 and a "7+" bucket
# Scope of the rerun after a board edit. "app" follows UI plan Phase 4's rule
# (a write inside a fragment ends in a full rerun) and matters here: the
# Mainboard tab's data_editor would otherwise keep stale quantities and its
# Save would write them back over the edit. See docs/archive/VISUAL_DECK_PLAN.md.
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


def run_board_action(conn, deck_id, key, action, *, is_undo=False, undo_moves=None):
    """Apply one action for the page: remember the inverse (and the moves it
    must put back) for the Undo button, stash the outcome and drop stale
    caches (card_view.stash_edit — docs/archive/EDIT_FLOW_PLAN.md rule 1; any moves also
    clear the collection caches and reach the pop-up), and flag the page to
    rerun. Returns True if something was applied.

    This runs in a widget callback, where st.rerun() is a no-op, hence
    stash_edit() rather than finish_edit(): rerun_if_edited() does the
    full-app rerun, and setup_page() shows the message on it. A refusal or
    a no-op writes nothing and reruns nothing, so it toasts directly.

    An undo itself leaves nothing to undo (one level); a failed or no-op
    edit leaves the previous Undo in place."""
    try:
        result = apply_board_action(
            conn, deck_id, action,
            resolve_add=lambda name: card_resolver.resolve_or_fetch_card(conn, name=name),
            # An Undo always reverts by the recorded moves, even an empty
            # list (the forward edit moved nothing, so nothing goes back).
            undo_moves=(undo_moves or []) if is_undo else None,
        )
    except ValueError as exc:
        st.toast(str(exc), icon="⚠️")
        return False
    if is_undo:
        st.session_state.pop(_undo_key(key), None)
    elif not result.get("changed", True):
        st.toast(result["message"], icon="✅")
        return True  # no-op: nothing changed, so no cache clear or rerun
    elif result["undo"]:
        st.session_state[_undo_key(key)] = {"label": result["message"], "action": result["undo"],
                                            "moves": result["moves"]}
    else:
        # A change with no inverse (apply): an older Undo would now act on
        # a deck that has moved on, so it goes.
        st.session_state.pop(_undo_key(key), None)
    card_view.stash_edit({
        "message": f"Undid: {result['message']}" if is_undo else result["message"],
        "moves": result["moves"],
        "warnings": result.get("warnings") or [],
    })
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
        help="Reverses the last edit made on the board, putting any copies it moved back where "
             "they were. History keeps both rows.",
        on_click=run_board_action, args=(conn, deck_id, key, pending["action"]),
        kwargs={"is_undo": True, "undo_moves": pending.get("moves")},
    )


def render_deck_board(cards_df, deck_meta, owned_qty, *, key, editable=False, conn=None, deck_id=None,
                      shortlist=None, leaving=None):
    """Mount the Stacks board. `key` must be unique per deck: it also keys the
    saved group-by in session_state.

    `shortlist` (load_board_shortlist()) feeds the Shortlist column and the
    "cut for Y" badges; None leaves both out (the read-only Decks page).
    `leaving` (load_board_leaving(), Phase 3) feeds the rail's "Leaving"
    group and the "→ Prosper" badges on cards other decks are taking.

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
    leaving = leaving or []
    leaving_by_card = {}
    for item in leaving:
        leaving_by_card.setdefault(item["id"], []).append(item["to_deck"])
    return _BOARD(
        key=key,
        data={
            "cards": board_payload(cards_df, deck_meta, owned_qty, cut_for=planned_cuts(shortlist or []),
                                   leaving=leaving_by_card,
                                   buying=q.deck_buying(conn, deck_id) if conn is not None and deck_id is not None else None),
            "shortlist": shortlist or [],
            "leaving": leaving,
            "group_by": group_by,
            "group_options": GROUP_BY_OPTIONS,
            "type_order": BOARD_TYPE_ORDER,
            "editable": editable,
            # Phase 2: where a removed copy can be put, and whether copies
            # move at all (a brewing deck's edits are list-only, so the
            # menu leaves the question out).
            "locations": q.storage_locations(conn) if editable else [],
            "copies_move": editable and not (loaders.load_deck_meta(conn, deck_id).get("build_state")),
        },
        # Registering a callback is what makes its field a result field.
        # group_by's does nothing: it only has to persist across reruns.
        **callbacks,
    )


def payload_size_bytes(cards):
    """JSON size of a board payload — for the size check in the tests/notes."""
    return len(json.dumps(cards, separators=(",", ":")).encode("utf-8"))
