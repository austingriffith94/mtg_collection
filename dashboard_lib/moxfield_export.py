"""
Moxfield plain-text import format:

    1 Combustible Gearhulk (C21) 163
    1 Birgi, God of Storytelling // Harnfel, Horn of Bounty (KHM) 129

Shared by scripts/export_moxfield.py (CLI) and the Decks & Maybeboard
page's in-dashboard export section, so both always produce identical
output. No Streamlit dependency — plain sqlite3, unit-testable directly.

Uses the exact printing stored in deck_cards/maybeboard, which migrate.py
(and the Editor page's card_resolver) prefer your OWNED collection
printing for wherever you have a copy — that's what makes the pasted-in
art match what you actually have.
"""


def format_line(name, set_code, collector_number, quantity=1):
    qty = quantity if quantity else 1
    return f"{qty} {name} ({set_code.upper()}) {collector_number}"


def export_deck_lines(conn, deck_id, include_maybeboard=False):
    """List of text lines in Moxfield's paste format for the deck's
    mainboard, optionally followed by a '// Maybeboard' section."""
    lines = []
    rows = conn.execute(
        """SELECT c.name, c.set_code, c.collector_number, dc.quantity
           FROM deck_cards dc JOIN cards c ON c.scryfall_id = dc.scryfall_id
           WHERE dc.deck_id = ?
           ORDER BY c.name""",
        (deck_id,),
    ).fetchall()
    for name, set_code, collector_number, qty in rows:
        lines.append(format_line(name, set_code, collector_number, qty))

    if include_maybeboard:
        mb_rows = conn.execute(
            """SELECT c.name, c.set_code, c.collector_number
               FROM maybeboard mb JOIN cards c ON c.scryfall_id = mb.scryfall_id
               WHERE mb.deck_id = ?
               ORDER BY c.name""",
            (deck_id,),
        ).fetchall()
        if mb_rows:
            lines.append("")
            lines.append("// Maybeboard")
            for name, set_code, collector_number in mb_rows:
                lines.append(format_line(name, set_code, collector_number, 1))

    return lines


def export_deck_text(conn, deck_id, include_maybeboard=False):
    return "\n".join(export_deck_lines(conn, deck_id, include_maybeboard))
