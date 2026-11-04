"""
Moxfield plain-text LIST import — the reverse of moxfield_export.py's deck
paste format. Paste a list copied from Moxfield (or anywhere that writes the
same lines) and it becomes a new deck whose cards all sit on the Shortlist as
ideas:

    1 Combustible Gearhulk (C21) 163
    1x Sol Ring
    1 Birgi, God of Storytelling // Harnfel, Horn of Bounty (KHM) 129 *F*

Section headings (Commander / Mainboard / Sideboard / Maybeboard, with or
without a trailing colon) are read as labels: the Commander section fills the
new deck's commander and partner fields, and every card's note records the
section it came from. `//` comment lines and blank lines are ignored.

Nothing is added to the collection or to the deck's real list. Shortlist
ideas for cards you don't own show up as buys on the Workbench, and ones you
do own show where the copy is, so you can see what the list would take before
committing to it. The deck is created even if some lines can't be resolved
(offline, misspelled); those lines are returned for the caller to show.

No Streamlit dependency — plain sqlite3 plus card_resolver/writes, so it is
unit-testable directly.
"""
import re

from dashboard_lib import card_resolver, writes

SECTIONS = {
    "commander": "Commander", "commanders": "Commander",
    "companion": "Companion",
    "mainboard": "Mainboard", "main": "Mainboard", "deck": "Mainboard",
    "sideboard": "Sideboard",
    "maybeboard": "Maybeboard", "maybe": "Maybeboard",
}

# "1 Name (SET) 123" with an optional "*F*"/"*E*" finish marker after it.
_WITH_PRINTING = re.compile(r"^(\d+)\s*x?\s+(.+?)\s+\(([A-Za-z0-9]+)\)\s+(\S+?)(?:\s+\*\w+\*)*\s*$")
_NAME_ONLY = re.compile(r"^(\d+)\s*x?\s+(.+?)(?:\s+\*\w+\*)*\s*$")


def parse_list(text):
    """Parse pasted text into a list of {name, set_code, number, quantity,
    section} dicts, in paste order. A card listed twice with the same
    printing is merged into one row (quantities added); the same card in
    two different sections stays two rows."""
    rows, index = [], {}
    section = "Mainboard"
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("//"):
            continue
        heading = SECTIONS.get(line.rstrip(":").strip().lower())
        if heading:
            section = heading
            continue
        m = _WITH_PRINTING.match(line)
        if m:
            qty, name, set_code, number = int(m[1]), m[2], m[3].lower(), m[4]
        else:
            m = _NAME_ONLY.match(line)
            if m:
                qty, name, set_code, number = int(m[1]), m[2], None, None
            else:
                qty, name, set_code, number = 1, line, None, None
        if qty < 1:
            continue
        key = (name.casefold(), set_code, number, section)
        if key in index:
            index[key]["quantity"] += qty
        else:
            index[key] = {"name": name, "set_code": set_code, "number": number,
                          "quantity": qty, "section": section}
            rows.append(index[key])
    return rows


def _resolve(conn, row, client):
    """(scryfall_id, error) for a parsed row: its exact printing when the
    paste named one (a local row, else Scryfall), else — or if that printing
    can't be found — the card by name."""
    if row["set_code"] and row["number"]:
        sid, _new, err = card_resolver.resolve_or_fetch_card(
            conn, set_code=row["set_code"], collector_number=row["number"], client=client)
        if sid:
            return sid, None
    sid, _new, err = card_resolver.resolve_or_fetch_card(conn, name=row["name"], client=client)
    return sid, err


def import_list(conn, deck_name, text, progress_callback=None):
    """Create a deck named `deck_name` from pasted `text`, every card
    staged as a Shortlist idea. Returns (deck_id, summary, error). error is
    set (and nothing created) for a blank name, a taken name or an empty
    paste. summary is {added: int, cards: int, failed: [(line, reason)]}
    where `cards` counts copies."""
    rows = parse_list(text)
    if not rows:
        return None, None, "Paste a list first — nothing in it looked like a card."
    commanders = [r["name"] for r in rows if r["section"] == "Commander"]
    deck_id, err = writes.create_deck(
        conn, deck_name,
        commander=commanders[0] if commanders else None,
        partner=commanders[1] if len(commanders) > 1 else None,
    )
    if err:
        return None, None, err

    client = None
    try:
        client = card_resolver._load_scryfall_module().ScryfallClient(verbose=False)  # one client: shared pacing
    except ImportError:
        pass  # each unresolved card reports it

    added = copies = 0
    failed = []
    for i, row in enumerate(rows, start=1):
        label = f"{row['quantity']} {row['name']}"
        sid, reason = _resolve(conn, row, client)
        if not sid:
            failed.append((label, reason or "not found"))
        else:
            card_name = conn.execute("SELECT name FROM cards WHERE scryfall_id=?", (sid,)).fetchone()[0]
            try:
                writes.add_change(conn, deck_id, add_scryfall_id=sid, add_name=card_name,
                                  quantity=row["quantity"], notes=f"Moxfield import: {row['section']}")
                added += 1
                copies += row["quantity"]
            except ValueError as exc:
                failed.append((label, str(exc)))
        if progress_callback:
            progress_callback(i, len(rows), row["name"])
    return deck_id, {"added": added, "cards": copies, "failed": failed}, None
