"""
Binder candidates: stored cards that belong in the binder
(docs/BINDER_PLAN.md Phase 2). Reads only; the moves and dismissals go
through writes.py (move_lot, dismiss_binder_card, set_setting).

Which lots are looked at ("storage"):
  - not in a deck: a lot whose Location is exactly a deck name is ignored,
    whatever it is (a Reserved List card in a deck is not a candidate);
  - not already at the binder Location (queries.same_location, so case and
    spaces don't matter);
  - not at a Location the user unticked (binder.skip_locations). Lots with
    no Location are always scanned, and a new Location is scanned until
    it's unticked, because the setting stores the unticked ones;
  - not a basic land, not a blank lot (no quantity and no Location: not a
    copy at all), and not a card dismissed as "Not for binder".

A lot is a candidate when it passes ANY rule (RULES below):
  - Value: the copy's value is at least binder.min_value. A copy's value is
    its finish-aware price (copy_value_sql): the foil price for a foil copy,
    falling back to the non-foil price; the non-foil price otherwise. A card
    with no price never passes this rule.
  - Reserved List (cards.is_reserved), if binder.rule_reserved is on.
  - Game changer (cards.is_game_changer), if binder.rule_game_changer is on.
Nothing else is a rule: foil, showcase, rarity count only through the price.
There is no scoring and no price history; a card "went up" only in the
sense that its current price now meets the minimum.

A new rule is one function in RULES (row, settings) -> label or None, plus a
settings key if it can be switched off.
"""
import json
from dataclasses import dataclass, field

import pandas as pd

from . import queries as q
from . import writes as w

# app_settings keys and their defaults (stored as text).
DEFAULTS = {
    "binder.location": "Binder",
    "binder.capacity": "720",
    "binder.min_value": "2.00",
    "binder.rule_reserved": "1",
    "binder.rule_game_changer": "1",
    "binder.skip_locations": "[]",
}


@dataclass(frozen=True)
class BinderSettings:
    location: str = "Binder"
    capacity: int = 720
    min_value: float = 2.0
    rule_reserved: bool = True
    rule_game_changer: bool = True
    skip_locations: tuple = field(default_factory=tuple)


def _parse(raw, cast, default):
    try:
        return cast(raw)
    except (TypeError, ValueError):
        return cast(default)


def _parse_list(raw):
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return ()
    return tuple(str(v) for v in value) if isinstance(value, list) else ()


def binder_settings(conn):
    """The saved binder.* settings, each falling back to its default when
    missing or unreadable."""
    def get(key):
        return q.get_setting(conn, key, DEFAULTS[key])

    location = (get("binder.location") or "").strip() or DEFAULTS["binder.location"]
    return BinderSettings(
        location=location,
        capacity=_parse(get("binder.capacity"), int, DEFAULTS["binder.capacity"]),
        min_value=_parse(get("binder.min_value"), float, DEFAULTS["binder.min_value"]),
        rule_reserved=get("binder.rule_reserved") not in ("0", "false", "False", ""),
        rule_game_changer=get("binder.rule_game_changer") not in ("0", "false", "False", ""),
        skip_locations=_parse_list(get("binder.skip_locations")),
    )


def validate_settings(conn, settings):
    """Problems with `settings` as a list of messages (empty = fine): the
    binder Location must be named and can't be a deck (matched ignoring case
    and spaces, like every storage Location), the minimum value is 0 or
    more and the capacity 1 or more."""
    errors = []
    location = (settings.location or "").strip()
    if not location:
        errors.append("Name the binder's Location.")
    elif any(q.same_location(location, deck) for deck in q.deck_name_set(conn)):
        errors.append(f"“{location}” is a deck, not a storage Location.")
    if settings.min_value is None or settings.min_value < 0:
        errors.append("The minimum value can't be negative.")
    if settings.capacity is None or settings.capacity < 1:
        errors.append("The capacity must be at least 1.")
    return errors


def save_settings(conn, settings):
    """Validate, then store every binder.* key. Raises ValueError with the
    validate_settings() messages and stores nothing if any fail."""
    errors = validate_settings(conn, settings)
    if errors:
        raise ValueError(" ".join(errors))
    w.set_setting(conn, "binder.location", settings.location.strip())
    w.set_setting(conn, "binder.capacity", int(settings.capacity))
    w.set_setting(conn, "binder.min_value", f"{float(settings.min_value):.2f}")
    w.set_setting(conn, "binder.rule_reserved", int(bool(settings.rule_reserved)))
    w.set_setting(conn, "binder.rule_game_changer", int(bool(settings.rule_game_changer)))
    w.set_setting(conn, "binder.skip_locations", json.dumps(list(settings.skip_locations)))


def copy_value_sql(col="col", card="c"):
    """SQL for one copy's value, finish-aware: a foil copy is worth the foil
    price (falling back to the non-foil one when that's NULL), a non-foil
    copy the non-foil price. The one place this rule is written."""
    return (f"CASE WHEN {col}.foil THEN COALESCE({card}.current_price_usd_foil, {card}.current_price_usd)"
            f" ELSE {card}.current_price_usd END")


def card_key(oracle_id, name):
    """What a dismissal is keyed by: the oracle_id (every printing), or the
    casefolded name for a card row that has no oracle_id."""
    return oracle_id or f"name:{(name or '').strip().casefold()}"


# ------------------------------------------------------------------
# The rules. Each takes one lot row (dict) and the settings and returns the
# label shown in `reasons`, or None when the lot doesn't pass.
# ------------------------------------------------------------------
def _rule_value(row, s):
    value = row["copy_value"]
    return f"${value:.2f}" if value is not None and value >= s.min_value else None


def _rule_reserved(row, s):
    return "Reserved List" if s.rule_reserved and row["is_reserved"] else None


def _rule_game_changer(row, s):
    return "Game changer" if s.rule_game_changer and row["is_game_changer"] else None


RULES = (_rule_value, _rule_reserved, _rule_game_changer)


def _all_lots(conn):
    """Every non-basic, non-blank lot joined to its card, with its copy
    value. A NULL or 0 quantity is one copy (the collection's rule)."""
    cur = conn.execute(
        f"""SELECT col.collection_id, col.scryfall_id, c.name, c.oracle_id, c.set_code,
                   c.collector_number, COALESCE(col.foil, 0) AS foil,
                   COALESCE(NULLIF(col.quantity, 0), 1) AS quantity, col.location,
                   COALESCE(c.is_reserved, 0) AS is_reserved,
                   COALESCE(c.is_game_changer, 0) AS is_game_changer,
                   c.price_updated_at, {copy_value_sql()} AS copy_value
            FROM collection col JOIN cards c ON c.scryfall_id = col.scryfall_id
            WHERE COALESCE(c.is_basic_land, 0) = 0
              AND NOT ({q.BLANK_LOT_SQL})"""
    )
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _dismissed_keys(conn):
    return {r[0] for r in conn.execute("SELECT oracle_id FROM binder_dismissed").fetchall()}


def _in_scope(lot, settings, deck_names):
    place = (lot["location"] or "").strip()
    if place in deck_names:
        return False
    if q.same_location(place, settings.location):
        return False
    if place and any(q.same_location(place, skip) for skip in settings.skip_locations):
        return False
    return True


def _qualifying(conn, settings):
    """The candidate lots (dicts with `card_key` and `reasons` added), no
    planned_for. Shared by binder_candidates() and the cheap count."""
    deck_names = q.deck_name_set(conn)
    dismissed = _dismissed_keys(conn)
    out = []
    for lot in _all_lots(conn):
        if not _in_scope(lot, settings, deck_names):
            continue
        lot["card_key"] = card_key(lot["oracle_id"], lot["name"])
        if lot["card_key"] in dismissed:
            continue
        reasons = [label for label in (rule(lot, settings) for rule in RULES) if label]
        if reasons:
            lot["reasons"] = reasons
            out.append(lot)
    return out


def _key_for_sid(conn, scryfall_id, cache):
    if scryfall_id not in cache:
        row = conn.execute("SELECT oracle_id, name FROM cards WHERE scryfall_id = ?",
                           (scryfall_id,)).fetchone()
        cache[scryfall_id] = card_key(row[0], row[1]) if row else None
    return cache[scryfall_id]


def _planned_claims(conn):
    """[(card_key, location, deck_name), ...]: stored copies a deck has
    promised itself from a named storage Location. Two sources, both read
    through the functions the Workbench uses so they can't drift: a
    brewing deck's saved storage choice (queries.brewing_claims) and an
    open shortlist row pinned to storage (source='storage'). An Auto row
    claims nothing — Auto takes from any storage, the Binder included."""
    cache, claims = {}, []
    for claim in q.brewing_claims(conn):
        claims.append((_key_for_sid(conn, claim["scryfall_id"], cache),
                       claim["location"], claim["deck_name"]))
    deck_names = {r[0]: r[1] for r in conn.execute("SELECT deck_id, name FROM decks").fetchall()}
    for row in w.list_changes(conn):
        location = (row.get("source_location") or "").strip()
        if row.get("source") != "storage" or not location or not row.get("add_scryfall_id"):
            continue
        claims.append((_key_for_sid(conn, row["add_scryfall_id"], cache),
                       location, deck_names.get(row["deck_id"], "?")))
    return claims


CANDIDATE_COLUMNS = [
    "collection_id", "scryfall_id", "card_key", "name", "set_code", "collector_number",
    "foil", "quantity", "location", "copy_value", "reasons", "already_in_binder", "planned_for",
]


def binder_candidates(conn, settings=None):
    """One row per qualifying lot, highest copy value first (no price last):
    CANDIDATE_COLUMNS. `reasons` is the list of rule labels passed;
    `already_in_binder` the copies of the same card already at the binder
    Location; `planned_for` the deck(s) that promised themselves this copy
    (", "-joined), or None. A planned lot is still listed — the page warns
    and leaves it unticked."""
    settings = settings or binder_settings(conn)
    lots = _qualifying(conn, settings)
    if not lots:
        return pd.DataFrame(columns=CANDIDATE_COLUMNS)

    in_binder = {}
    for lot in _all_lots(conn):
        if q.same_location(lot["location"], settings.location):
            key = card_key(lot["oracle_id"], lot["name"])
            in_binder[key] = in_binder.get(key, 0) + lot["quantity"]

    claims = _planned_claims(conn)
    for lot in lots:
        lot["already_in_binder"] = in_binder.get(lot["card_key"], 0)
        decks = sorted({deck for key, location, deck in claims
                        if key == lot["card_key"] and q.same_location(location, lot["location"])},
                       key=str.casefold)
        lot["planned_for"] = ", ".join(decks) or None

    df = pd.DataFrame(lots)[CANDIDATE_COLUMNS]
    df["foil"] = df["foil"].astype(bool)
    df = (df.sort_values(["copy_value", "name"], ascending=[False, True], na_position="last",
                         key=lambda s: s.str.casefold() if s.name == "name" else s)
            .reset_index(drop=True))
    # pandas turns a None among strings into NaN; keep "no plan" as None.
    df["planned_for"] = df["planned_for"].astype(object).where(df["planned_for"].notna(), None)
    return df


def binder_candidate_names(conn, settings=None):
    """The distinct card names that currently qualify, without the
    planned_for work. What the refresh summary compares before and after."""
    settings = settings or binder_settings(conn)
    return {lot["name"] for lot in _qualifying(conn, settings)}


def binder_candidate_count(conn, settings=None):
    """How many distinct cards in storage could go in the binder — Home's
    count. Cheap: skips planned_for, which pulls in the allocator's reads."""
    settings = settings or binder_settings(conn)
    return len({lot["card_key"] for lot in _qualifying(conn, settings)})


def binder_fill(conn, settings=None):
    """(copies at the binder Location, capacity) for the fill gauge."""
    settings = settings or binder_settings(conn)
    # Every lot, basics included: a basic filed in the binder still takes a slot.
    copies = sum(n for loc, n in conn.execute(
        "SELECT location, COALESCE(NULLIF(quantity, 0), 1) FROM collection").fetchall()
        if q.same_location(loc, settings.location))
    return copies, settings.capacity


def dismissed_cards(conn):
    """The "Not for binder" list, newest first: [{card_key, name, dismissed_at}]."""
    return [{"card_key": r[0], "name": r[1], "dismissed_at": r[2]} for r in conn.execute(
        "SELECT oracle_id, name, dismissed_at FROM binder_dismissed"
        " ORDER BY dismissed_at DESC, name COLLATE NOCASE"
    ).fetchall()]
