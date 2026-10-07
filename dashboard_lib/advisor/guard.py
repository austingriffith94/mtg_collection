"""
Grounding layer, part 2: validates model output before the user sees it.

Checks, per suggested card (cut or add):
  - name:     must be a real card in the allowed list (deck for cuts and
              `replaces`, candidate pool for adds), matched on front face so
              MDFCs resolve however the model spells them;
  - quote:    evidence_quote must be a verbatim substring of that card's
              stored oracle text (whitespace/case/curly-quote normalised).
              Proves the quote is real, not that the conclusion is right;
  - roles:    roles the model claims must be in the roles computed from the
              card's text (context.mechanical_roles).

Items with violations are kept but marked unverified; they are never
dropped, so the UI can show them collapsed under a badge. `retry_prompt`
turns the violations into the single corrective follow-up message.

`check_chat_answer` is the chat-mode (Phase 3) counterpart: no cut/add
structure, no pre-filtered pool — name, quote and roles are all checked
against the whole database via context.lookup_card_text instead of one
deck's pool. Same caveat as analysis mode's role check: `mechanical_roles`
is a regex heuristic, not a card database, so an occasional disagreement is
our miss, not the model's.
"""
import re
from dataclasses import dataclass, field

from .context import front_face, lookup_card_text, mechanical_roles


def _norm(text):
    """Normalise for substring comparison: curly quotes, dashes, whitespace, case."""
    t = (text or "").replace("’", "'").replace("‘", "'")
    t = t.replace("“", '"').replace("”", '"').replace("−", "-").replace("—", "-")
    return re.sub(r"\s+", " ", t).strip().casefold()


@dataclass
class CheckedItem:
    kind: str                     # "cut" | "add"
    data: dict                    # the model's raw item
    violations: list = field(default_factory=list)

    @property
    def verified(self):
        return not self.violations


@dataclass
class CheckedAnalysis:
    summary: str
    strengths: list
    weaknesses: list
    items: list                   # CheckedItem

    @property
    def violations(self):
        return [(i, v) for i in self.items for v in i.violations]


def _index(cards):
    return {front_face(c.name): c for c in cards}


def _check_item(kind, item, primary, deck):
    """Violations for one cut/add item. `primary` indexes the legal pool for
    item['card']; `deck` indexes mainboard names for item['replaces']."""
    v = []
    card = primary.get(front_face(item.get("card")))
    if card is None:
        v.append(f"unknown card '{item.get('card')}' (not in the allowed {'deck' if kind == 'cut' else 'candidate'} list)")
    if kind == "add":
        rep = deck.get(front_face(item.get("replaces")))
        if rep is None:
            v.append(f"'replaces' names unknown deck card '{item.get('replaces')}'")
        elif rep.is_commander:
            v.append("'replaces' names the commander, which cannot be cut")
    if card is None:
        return v
    quote = _norm(item.get("evidence_quote"))
    if not quote:
        v.append("missing evidence_quote")
    elif quote not in _norm(card.oracle_text):
        v.append(f"quote not found in {card.name}'s oracle text: \"{item.get('evidence_quote')}\"")
    claimed = set(item.get("roles") or [])
    extra = claimed - card.roles
    if extra:
        actual = ", ".join(sorted(card.roles)) or "none"
        v.append(f"claims role(s) {', '.join(sorted(extra))} for {card.name}, "
                 f"but its text computes to: {actual}")
    return v


def check_analysis(parsed, ctx):
    """Validate a parsed analysis dict against `ctx`; never raises on bad
    model output (a malformed section just contributes no items)."""
    parsed = parsed if isinstance(parsed, dict) else {}
    deck = _index(ctx.cards)
    # Commander cannot be cut: leave it out of the cut pool so it reads as unknown.
    cut_pool = {k: c for k, c in deck.items() if not c.is_commander}
    cands = _index(ctx.candidates)
    items = []
    for kind, pool, key in (("cut", cut_pool, "cuts"), ("add", cands, "adds")):
        for raw in parsed.get(key) or []:
            if isinstance(raw, dict):
                items.append(CheckedItem(kind, raw, _check_item(kind, raw, pool, deck)))
    return CheckedAnalysis(
        summary=str(parsed.get("summary") or ""),
        strengths=[str(s) for s in parsed.get("strengths") or []],
        weaknesses=[str(s) for s in parsed.get("weaknesses") or []],
        items=items,
    )


@dataclass
class CheckedMention:
    data: dict                    # {"card": ..., "evidence_quote": ...}
    violations: list = field(default_factory=list)

    @property
    def verified(self):
        return not self.violations


@dataclass
class CheckedChat:
    answer: str
    mentions: list                # CheckedMention

    @property
    def violations(self):
        return [(m, v) for m in self.mentions for v in m.violations]


def _check_mention(conn, item):
    """Violations for one restated chat mention: the name must resolve to a
    real card (front-face match, any card in the DB — chat has no
    pre-filtered pool); a given quote must be a real substring of its oracle
    text (empty quote allowed: not every mention is a textual claim); any
    claimed roles must be in context.mechanical_roles(oracle_text, type_line)
    for that card."""
    name = item.get("card")
    found = lookup_card_text(conn, name) if name else None
    if found is None:
        return [f"unknown card '{name}' (no matching printing in the database)"]
    v = []
    quote = _norm(item.get("evidence_quote"))
    if quote and quote not in _norm(found["oracle_text"]):
        v.append(f"quote not found in {found['name']}'s oracle text: \"{item.get('evidence_quote')}\"")
    claimed = set(item.get("roles") or [])
    actual_roles = mechanical_roles(found["oracle_text"], found["type_line"])
    extra = claimed - actual_roles
    if extra:
        actual = ", ".join(sorted(actual_roles)) or "none"
        v.append(f"claims role(s) {', '.join(sorted(extra))} for {found['name']}, "
                 f"but its text computes to: {actual}")
    return v


def check_chat_answer(conn, parsed):
    """Validate a chat turn's restated mentions against the database. Never
    raises on bad model output, same as check_analysis."""
    parsed = parsed if isinstance(parsed, dict) else {}
    mentions = [CheckedMention(raw, _check_mention(conn, raw))
                for raw in parsed.get("mentions") or [] if isinstance(raw, dict)]
    return CheckedChat(str(parsed.get("answer") or ""), mentions)


def retry_prompt(checked):
    """The one corrective follow-up: list each violation, ask for a full
    corrected answer using only the allowed lists and exact quotes."""
    lines = [f"- {i.kind} '{i.data.get('card')}': {v}" for i, v in checked.violations]
    return ("Your answer had problems that I verified against the database:\n"
            + "\n".join(lines)
            + "\n\nReturn the complete corrected answer. Use only card names from the provided lists, "
              "copy evidence quotes exactly from the oracle text shown, and claim only roles the text supports. "
              "Drop any suggestion you cannot support.")
