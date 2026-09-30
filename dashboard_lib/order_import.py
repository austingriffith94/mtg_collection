"""
Import support for marketplace purchase-order CSV exports — e.g. Mana Pool's
"order_<id>_<date>.csv" downloads — into the collection.

Each line item names a specific printing via Set Code + Collector # — that
pair (not the file's own "Card Name" text) is what resolve_rows() uses to
look up the real card via card_resolver.resolve_exact_printing() (a local
exact match, else a live Scryfall lookup for that specific printing; unlike
the Editor's "Add a card" flow, this deliberately never substitutes a
different printing of the same card name that happens to already be in the
local `cards` table). The Scryfall/local-DB name that comes back is what
gets shown in the preview and used everywhere after — the CSV's Card Name
column is kept only as a fallback label for a row that fails to resolve at
all, since it's just the marketplace listing's own text and not to be
trusted over the actual printing data.

Not every export column has a home in the `collection` schema: Condition
and Language are parsed for the preview table but dropped on import (no
per-lot condition/language columns exist). Seller and Package # are folded
into the free-text `source` field instead of kept as their own columns.

Two integrity checks guard against a malformed or unexpected file silently
producing wrong data:
  - Finish values other than exactly "Foil"/"Non-Foil" (case-insensitive)
    are NOT assumed to be non-foil — they're flagged `finish_recognized =
    False` so the caller can surface them instead of silently mis-pricing
    a foil-etched/alt-finish card as a regular non-foil copy.
  - "Total Price" — otherwise redundant with Quantity x Unit Price (the
    per-card price paid, i.e. `collection.price_paid`) — is used as a
    cross-check: if it doesn't match Quantity x Unit Price within a cent
    of rounding tolerance, the row is flagged `price_mismatch = True`,
    which usually means a parsing assumption here is wrong for this
    particular export rather than that the marketplace's own math is off.

The caller (pd.read_csv) MUST pass dtype=str: without it, pandas silently
infers numeric dtypes per column, and a "Collector #" column that happens
to be all-digits in a given file gets read as an int, stripping any
leading zero (e.g. "007" -> 7) — which then either fails to match a local
printing that has the zero-padded form, or resolves the wrong Scryfall
printing entirely.
"""
import re

from dashboard_lib import card_resolver, writes

REQUIRED_COLUMNS = {"Card Name", "Set Code", "Collector #", "Quantity", "Unit Price", "Finish"}
FINISH_FOIL_MAP = {"foil": True, "non-foil": False}


def _parse_price(value):
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() == "nan":
        return None
    text = re.sub(r"[^0-9.\-]", "", text)
    try:
        return float(text) if text else None
    except ValueError:
        return None


def _parse_int(value, default):
    if value is None:
        return default
    text = str(value).strip()
    if not text or text.lower() == "nan":
        return default
    try:
        return int(float(text))
    except ValueError:
        return default


def parse_order_csv(df):
    """Normalizes a raw order DataFrame (read with dtype=str — see module
    docstring) into a list of dicts, one per line item, ready for
    resolve_rows()/import_rows(). Raises ValueError if the expected
    columns aren't present (i.e. this isn't a recognized order export)."""
    missing = REQUIRED_COLUMNS - set(df.columns)
    if missing:
        raise ValueError(
            f"Missing expected column(s): {', '.join(sorted(missing))}. "
            f"This doesn't look like a Mana Pool order export."
        )

    rows = []
    for _, r in df.iterrows():
        item_type = str(r.get("Item Type", "")).strip()
        quantity = _parse_int(r.get("Quantity"), default=1)
        unit_price = _parse_price(r.get("Unit Price"))
        total_price = _parse_price(r.get("Total Price"))

        price_mismatch = False
        if unit_price is not None and total_price is not None:
            expected_total = round(unit_price * quantity, 2)
            tolerance = 0.01 * quantity + 0.01
            price_mismatch = abs(expected_total - total_price) > tolerance

        finish_text = str(r.get("Finish", "")).strip()
        finish_key = finish_text.lower()
        finish_recognized = finish_key in FINISH_FOIL_MAP
        foil = FINISH_FOIL_MAP.get(finish_key, False)

        rows.append({
            "csv_name": str(r["Card Name"]).strip(),
            "set_code": str(r["Set Code"]).strip(),
            "collector_number": str(r["Collector #"]).strip(),
            "set_name": str(r.get("Set", "")).strip(),
            "condition": str(r.get("Condition", "")).strip(),
            "language": str(r.get("Language", "")).strip(),
            "foil": foil,
            "finish_raw": finish_text,
            "finish_recognized": finish_recognized,
            "rarity": str(r.get("Rarity", "")).strip(),
            "quantity": quantity,
            "unit_price": unit_price,
            "total_price": total_price,
            "price_mismatch": price_mismatch,
            "seller": str(r.get("Seller", "")).strip(),
            "package": str(r.get("Package #", "")).strip(),
            "item_type": item_type,
            "is_single": item_type.lower() == "single",
        })
    return rows


def resolve_rows(conn, rows):
    """Resolves every Single row's exact printing up front (local match,
    else a live Scryfall fetch) so the preview can show the actual card
    name for that Set Code + Collector #, instead of the file's own Card
    Name text. Mutates and returns `rows`, adding to each Single row:
      - scryfall_id: resolved printing, or None if resolution failed
      - resolved_name: the name from `cards` (falls back to csv_name only
        if resolution failed outright, so there's still something to show)
      - was_new: True if this printing had to be fetched from Scryfall
      - error: a message if resolution failed, else None
    Non-Single rows are left untouched. Safe to call again on the same
    rows — already-resolved rows (scryfall_id already set) are skipped, so
    re-running this after a Streamlit rerun doesn't refetch anything or
    repeat a failed lookup pointlessly."""
    for row in rows:
        if not row["is_single"] or "scryfall_id" in row:
            continue
        sid, was_new, err = card_resolver.resolve_exact_printing(
            conn, row["set_code"], row["collector_number"],
        )
        row["scryfall_id"] = sid
        row["was_new"] = was_new
        row["error"] = err
        if sid:
            found = conn.execute("SELECT name FROM cards WHERE scryfall_id=?", (sid,)).fetchone()
            row["resolved_name"] = found[0] if found else row["csv_name"]
        else:
            row["resolved_name"] = row["csv_name"]
    return rows


def mark_existing_lots(conn, rows):
    """Flags each resolved Single row with `already_in_collection`: True if
    a collection lot already exists for this exact printing + foil status
    (quantity/location/price aren't compared — this is just "do I already
    own (a lot of) this specific card", not an exact-duplicate check).
    Meant to catch re-uploading a file you've already imported once, so the
    caller can warn before adding brand-new duplicate lots. Call after
    resolve_rows() (rows without a resolved scryfall_id are marked False,
    same as an unresolvable row can't be a known duplicate)."""
    for row in rows:
        if not row["is_single"] or not row.get("scryfall_id"):
            row["already_in_collection"] = False
            continue
        found = conn.execute(
            "SELECT 1 FROM collection WHERE scryfall_id=? AND foil=? LIMIT 1",
            (row["scryfall_id"], 1 if row["foil"] else 0),
        ).fetchone()
        row["already_in_collection"] = bool(found)
    return rows


def import_rows(conn, rows, *, location=None, date_acquired=None, source_prefix="Mana Pool",
                 append_seller=True):
    """Adds a collection lot for each already-resolved Single row (see
    resolve_rows() — call it first; a row with no "scryfall_id" key yet is
    treated as an error here rather than resolved on the fly, so preview
    and import always agree on what a row resolved to). Returns a list of
    {row, status, message} dicts, status one of "added", "added_new_card",
    "skipped", "error"."""
    results = []
    for row in rows:
        if not row["is_single"]:
            results.append({
                "row": row, "status": "skipped",
                "message": f"Item type '{row['item_type']}' isn't a single card — skipped.",
            })
            continue

        sid = row.get("scryfall_id")
        if not sid:
            results.append({
                "row": row, "status": "error",
                "message": row.get("error") or "This row was never resolved to a printing.",
            })
            continue

        source = source_prefix or None
        if append_seller and row["seller"]:
            source = f"{source_prefix} ({row['seller']})" if source_prefix else row["seller"]

        writes.add_collection_lot(
            conn, sid,
            quantity=row["quantity"],
            foil=row["foil"],
            location=location,
            date_acquired=date_acquired,
            price_paid=row["unit_price"],
            source=source,
        )
        results.append({
            "row": row,
            "status": "added_new_card" if row.get("was_new") else "added",
            "message": "Fetched from Scryfall and added." if row.get("was_new") else "Added.",
        })
    return results
