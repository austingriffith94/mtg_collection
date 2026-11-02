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

Warnings are a separate list: true facts about what a swap would do (breaks
a combo, passes the bracket's Game Changer limit, adds a combo to a
low-bracket deck). The suggestion can still be right, so a warning neither
unverifies an item nor triggers the retry.

`check_tags` is the tag-proposal counterpart: name, tags, primary and quote
are checked per card, and a card sent but not returned is listed as missing.
The quote may come from the type line too, since a creature type is often
only there. A rejected tag is left off the row, which is otherwise kept and
marked unverified. Roles are not checked: the regexes miss too many real tags.

`check_chat_answer` is the chat-mode (Phase 3) counterpart: no cut/add
structure, no pre-filtered pool — name, quote and roles are all checked
against the whole database via context.lookup_card_text instead of one
deck's pool. Same caveat as analysis mode's role check: `mechanical_roles`
is a regex heuristic, not a card database, so an occasional disagreement is
our miss, not the model's.
"""
import re
from dataclasses import dataclass, field

from .context import EXCLUSIVE_LABELS, GAME_CHANGER_LIMITS, MAX_TAGS_PER_CARD, front_face, lookup_card_text, mechanical_roles


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
    warnings: list = field(default_factory=list)

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


def _combo_text(combo):
    result = ", ".join(combo["produces"][:2])
    return " + ".join(combo["uses"]) + (f" ({result})" if result else "")


def _add_warnings(ctx, items):
    """Attach warnings to checked items: combos a leaving card breaks, combos
    an added card brings into a low-bracket deck, and Game Changers past the
    bracket's limit. The Game Changer count runs across the adds in order,
    because several suggestions can only pass the limit together."""
    deck, cands = _index(ctx.cards), _index(ctx.candidates)
    bracket = ctx.stats.get("bracket")
    limit = GAME_CHANGER_LIMITS.get(bracket)
    game_changers = ctx.stats.get("game_changers", 0)
    for item in items:
        leaving = item.data.get("card" if item.kind == "cut" else "replaces")
        for combo in ctx.included_combos():
            if front_face(leaving) in combo["uses_keys"]:
                item.warnings.append(f"Breaks combo: {_combo_text(combo)}")
        if item.kind != "add":
            continue
        added = cands.get(front_face(item.data.get("card")))
        if added is None:
            continue
        if ctx.low_combo_bracket:
            for combo in added.completes_combos:
                item.warnings.append(f"Completes a combo in a bracket {bracket} deck: {_combo_text(combo)}")
        rep = deck.get(front_face(leaving))
        change = added.game_changer - bool(rep and rep.game_changer)
        game_changers += change
        # A one-for-one swap can't make an over-limit deck any worse.
        if change > 0 and limit is not None and game_changers > limit:
            item.warnings.append(f"{added.name} is a Game Changer: this makes {game_changers}, "
                                 f"and bracket {bracket} allows {limit}")


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
    _add_warnings(ctx, items)
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


@dataclass
class CheckedTagRow:
    data: dict                    # the model's raw row
    card: object = None           # CardFact; None if the name is unknown or repeated
    tags: list = field(default_factory=list)    # legal labels, primary first
    primary: str | None = None
    violations: list = field(default_factory=list)
    status: str = ""              # set by tagging.classify: new / same / conflict / none

    @property
    def verified(self):
        return not self.violations


@dataclass
class CheckedTags:
    rows: list                    # CheckedTagRow
    missing: list                 # names sent but not returned: not reviewed, unlike an empty `tags`

    @property
    def violations(self):
        return [(r, v) for r in self.rows for v in r.violations]

    def retry_keys(self):
        """front_face keys of the cards a retry should redo."""
        return {front_face(r.data.get("card")) for r in self.rows if r.violations} | {
            front_face(n) for n in self.missing}


def _check_tag_labels(row, labels):
    """Fill row.tags and row.primary with the legal labels, in their
    vocabulary spelling, and record what was wrong with the rest."""
    raw = row.data.get("tags")
    raw = raw if isinstance(raw, list) else []
    for t in raw:
        label = labels.get(str(t).strip().casefold())
        if label is None:
            row.violations.append(f"tag '{t}' is not in the allowed labels")
        elif label not in row.tags:
            row.tags.append(label)
    claimed = str(row.data.get("primary") or "").strip()
    primary = labels.get(claimed.casefold())
    if row.tags:
        if not claimed:
            row.violations.append("no primary tag")
        elif primary not in row.tags:
            row.violations.append(f"primary '{claimed}' is not one of the card's tags")
        row.primary = primary if primary in row.tags else row.tags[0]
        row.tags.sort(key=lambda t: t != row.primary)
    elif claimed and not raw:
        row.violations.append(f"primary '{claimed}' given with no tags")
    for pair in EXCLUSIVE_LABELS:
        both = [t for t in row.tags if t.casefold() in {p.casefold() for p in pair}]
        if len(both) == len(pair):
            # Keep the primary; the other label of the pair is the one dropped.
            drop = next(t for t in both if t != row.primary)
            row.violations.append(f"{' and '.join(pair)} are exclusive: a card is one or the other, "
                                  f"so '{drop}' was dropped")
            row.tags.remove(drop)
    if len(row.tags) > MAX_TAGS_PER_CARD:
        row.violations.append(f"{len(row.tags)} tags (at most {MAX_TAGS_PER_CARD})")
        del row.tags[MAX_TAGS_PER_CARD:]


def _check_tag_row(raw, pool, seen, labels):
    """One model row checked against the deck and vocabulary. `seen` holds the
    cards already answered for, and gains this one."""
    row = CheckedTagRow(raw)
    key = front_face(raw.get("card"))
    if key not in pool:
        row.violations.append(f"unknown card '{raw.get('card')}' (not in the deck)")
        return row
    if key in seen:
        row.violations.append(f"'{raw.get('card')}' was returned more than once")
        return row
    seen.add(key)
    row.card = card = pool[key]
    _check_tag_labels(row, labels)
    if not row.tags:
        return row
    quote = _norm(raw.get("evidence_quote"))
    if not quote:
        row.violations.append("missing evidence_quote")
    elif quote not in _norm(card.oracle_text) and quote not in _norm(card.type_line):
        row.violations.append(f"quote not found in {card.name}'s type line or oracle text: "
                              f"\"{raw.get('evidence_quote')}\"")
    return row


def check_tags(parsed, ctx):
    """Validate a parsed tag answer against a TagContext; never raises on bad
    model output."""
    parsed = parsed if isinstance(parsed, dict) else {}
    pool = _index(ctx.cards)
    labels = {label.casefold(): label for label in ctx.labels()}
    seen = set()
    rows = [_check_tag_row(raw, pool, seen, labels)
            for raw in parsed.get("cards") or [] if isinstance(raw, dict)]
    return CheckedTags(rows, [c.name for c in ctx.cards if front_face(c.name) not in seen])


def tag_retry_prompt(checked):
    """The one corrective follow-up. It asks only for the cards with a
    problem: a deck's whole answer is long and each retry is paid for."""
    lines = [f"- '{r.data.get('card')}': {v}" for r, v in checked.violations]
    lines += [f"- '{name}': not returned" for name in checked.missing]
    return ("Your answer had problems that I verified against the database:\n"
            + "\n".join(lines)
            + "\n\nReturn rows for these cards only, corrected. Use only the listed card names and labels, "
              "copy evidence quotes exactly from the type line or oracle text shown, and give every tagged "
              "card a primary. Return an empty tags list for a card you cannot support.")


def retry_prompt(checked):
    """The one corrective follow-up: list each violation, ask for a full
    corrected answer using only the allowed lists and exact quotes."""
    lines = [f"- {i.kind} '{i.data.get('card')}': {v}" for i, v in checked.violations]
    return ("Your answer had problems that I verified against the database:\n"
            + "\n".join(lines)
            + "\n\nReturn the complete corrected answer. Use only card names from the provided lists, "
              "copy evidence quotes exactly from the oracle text shown, and claim only roles the text supports. "
              "Drop any suggestion you cannot support.")
