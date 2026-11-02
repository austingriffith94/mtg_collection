"""
Tag proposals: the model reads a deck's cards and proposes tags in one layer.

run_tag_proposals follows analysis.run_analysis: build the prompt from a
TagContext, ask for schema-constrained output, validate with guard.py, retry
once, and return whatever still fails marked unverified. The retry asks only
for the cards with a problem and its rows are merged over the first answer, so
it costs far less than a second full answer and can't lose good rows.

Nothing here writes to the database. A result is a proposal: each row is
classified against the card's current tags (`classify`), and `focus_report`
says which labels would end up thin or broad. Applying rows is the page's job.
"""
import os
from collections import Counter
from dataclasses import dataclass

from . import guard
from .analysis import SKILLS_DIR
from .context import TagContext, front_face
from .providers import ProviderError
from .schemas import tag_schema

SKILL_FILE = "tag_suggester.md"
# One row per card, each with a quote and a reason, so a full deck's answer is
# several times the usual length and takes minutes to come back.
MAX_OUTPUT_TOKENS = 16000
TIMEOUT_SECONDS = 300
# Already-tagged cards shown per label as examples of how the user uses it.
EXAMPLES_PER_LABEL = 2
# A label on fewer cards than this isn't worth having; one on more than this
# share of the deck says nothing. Starting values, to tune after the backtest.
MIN_LABEL_CARDS = 3
BROAD_LABEL_SHARE = 0.40


@dataclass
class FocusReport:
    counts: dict              # {label: copies carrying it, primary or secondary}
    tags_per_card: float      # average over the cards with at least one tag
    thin: list                # labels on 1 to MIN_LABEL_CARDS - 1 copies
    broad: list               # labels on more than BROAD_LABEL_SHARE of the deck


@dataclass
class TagResult:
    checked: guard.CheckedTags | None
    focus: FocusReport | None     # if every verified `new` row were applied
    retried: bool
    input_tokens: int
    output_tokens: int
    error: str = ""


def pick_examples(ctx: TagContext, per_label=EXAMPLES_PER_LABEL):
    """{label: [card name, ...]} from the deck's tagged cards, for labels the
    model is offered."""
    wanted = {label.casefold(): label for label in ctx.labels()}
    examples = {}
    for c in ctx.cards:
        for tag in ctx.current.get(c.name, []):
            label = wanted.get(tag.casefold())
            if label and len(examples.setdefault(label, [])) < per_label:
                examples[label].append(c.name)
    return examples


def _card_line(c, current):
    """One card for the prompt. `current` is None when the card's tags are
    being hidden, and [] when it has none."""
    flags = ["COMMANDER"] if c.is_commander else []
    if current is not None:
        flags.append("current tags: " + (", ".join(current) or "none"))
    tail = f" [{'; '.join(flags)}]" if flags else ""
    return f"- {c.name} | {c.mana_cost or '-'} | {c.type_line}{tail}\n  {c.oracle_text.replace(chr(10), ' / ')}"


def build_prompt(ctx: TagContext, hide_current=False, examples=None):
    """User message: deck, labels, examples, cards. `hide_current` leaves out
    the cards' tags, which is what a backtest needs; `examples` then has to
    come from cards it doesn't score."""
    m = ctx.meta
    if examples is None:
        examples = {} if hide_current else pick_examples(ctx)
    labels = "\n".join(f"- {v['label']}: {v['definition']}" for v in ctx.vocabulary)
    shown = "\n".join(f"- {label}: {', '.join(names)}" for label, names in examples.items() if names)
    cards = "\n".join(_card_line(c, None if hide_current else ctx.current.get(c.name, [])) for c in ctx.cards)
    return (
        f"DECK: {m.get('name')} (commander: {m.get('commander') or 'unknown'}"
        f"{', partner: ' + m['partner'] if m.get('partner') else ''})\n"
        f"Stated plan: {m.get('description') or 'not recorded'}\n\n"
        f"LABELS (tag layer '{ctx.tag_type}'):\n{labels}\n\n"
        + (f"EXAMPLES OF HOW THE USER USES THESE LABELS:\n{shown}\n\n" if shown else "")
        + f"CARDS ({len(ctx.cards)}):\n{cards}"
    )


def classify(tags, current):
    """How a proposal relates to a card's current tags: `none` proposes
    nothing, `same` adds no label, `conflict` shares no label with tags the
    card already has, and `new` adds a label without that disagreement."""
    proposed = {t.casefold() for t in tags}
    have = {t.casefold() for t in current}
    if not proposed:
        return "none"
    if proposed <= have:
        return "same"
    return "conflict" if have and not proposed & have else "new"


def focus_report(ctx: TagContext, rows):
    """Each vocabulary label's count if `rows` were applied on top of the
    deck's current tags, additively, and which labels would be thin or broad.
    Counts are copies, and a card counts for each of its tags, so they
    overlap and don't add up to the deck."""
    tags = {c.name: {t.casefold() for t in ctx.current.get(c.name, [])} for c in ctx.cards}
    for r in rows:
        if r.card is not None:
            tags[r.card.name] |= {t.casefold() for t in r.tags}
    quantity = {c.name: c.quantity for c in ctx.cards}
    counts = Counter()
    for name, labels in tags.items():
        for label in labels:
            counts[label] += quantity[name]
    counts = {label: counts[label.casefold()] for label in ctx.labels() if counts[label.casefold()]}
    tagged = [len(labels) for labels in tags.values() if labels]
    deck_size = sum(quantity.values())
    return FocusReport(
        counts=counts,
        tags_per_card=round(sum(tagged) / len(tagged), 2) if tagged else 0.0,
        thin=[label for label, n in counts.items() if n < MIN_LABEL_CARDS],
        broad=[label for label, n in counts.items() if n > BROAD_LABEL_SHARE * deck_size],
    )


def _merge_retry(first, second, keys):
    """The first answer's rows, with the retry's in place for the cards in
    `keys`. A flagged card the retry skipped keeps its row, still flagged."""
    rows = second.get("cards") if isinstance(second, dict) else None
    redone = {front_face(r.get("card")): r for r in rows or [] if isinstance(r, dict)}
    redone = {k: r for k, r in redone.items() if k in keys}
    kept = [r for r in first.get("cards") or []
            if not isinstance(r, dict) or front_face(r.get("card")) not in redone]
    return {"cards": kept + list(redone.values())}


def run_tag_proposals(provider, ctx: TagContext, hide_current=False, examples=None):
    """Ask for tags for every card in `ctx`. Returns a TagResult; on a provider
    failure `error` is set and nothing else is usable."""
    if not ctx.cards:
        return TagResult(None, None, False, 0, 0, "This deck has no cards to tag.")
    if not ctx.vocabulary:
        return TagResult(None, None, False, 0, 0, f"No labels with definitions in the '{ctx.tag_type}' layer.")
    with open(os.path.join(SKILLS_DIR, SKILL_FILE), encoding="utf-8") as f:
        system = f.read()
    schema = tag_schema(ctx)
    messages = [{"role": "user", "content": build_prompt(ctx, hide_current, examples)}]
    tokens_in = tokens_out = 0

    def ask():
        nonlocal tokens_in, tokens_out
        resp = provider.chat(messages, system=system, schema=schema,
                             max_tokens=MAX_OUTPUT_TOKENS, timeout=TIMEOUT_SECONDS)
        tokens_in += resp.input_tokens
        tokens_out += resp.output_tokens
        return resp

    def result(checked, retried):
        for r in checked.rows:
            r.status = classify(r.tags, ctx.current.get(r.card.name, []) if r.card else [])
        focus = focus_report(ctx, [r for r in checked.rows if r.verified and r.status == "new"])
        return TagResult(checked, focus, retried, tokens_in, tokens_out)

    try:
        resp = ask()
    except ProviderError as e:
        return TagResult(None, None, False, tokens_in, tokens_out, str(e))
    if not isinstance(resp.parsed, dict):
        return TagResult(None, None, False, tokens_in, tokens_out, "The model did not return valid JSON.")
    checked = guard.check_tags(resp.parsed, ctx)
    if not checked.violations and not checked.missing:
        return result(checked, False)

    # One retry that names each problem; keep the first answer if it fails.
    messages += [{"role": "assistant", "content": resp.text},
                 {"role": "user", "content": guard.tag_retry_prompt(checked)}]
    try:
        retry = ask()
    except ProviderError:
        return result(checked, True)
    if retry.parsed is None:
        return result(checked, True)
    return result(guard.check_tags(_merge_retry(resp.parsed, retry.parsed, checked.retry_keys()), ctx), True)
