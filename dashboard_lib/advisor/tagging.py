"""
Tag proposals: the model reads a deck's cards and proposes tags in one layer.

run_tag_proposals follows analysis.run_analysis: build the prompt from a
TagContext, ask for schema-constrained output, validate with guard.py, retry
once, and return whatever still fails marked unverified. The retry asks only
for the cards with a problem and its rows are merged over the first answer, so
it costs far less than a second full answer and can't lose good rows.

Large decks go out in batches of BATCH_SIZE cards. Nothing here writes to the database. A result is a proposal: each row is
classified against the card's current tags (`classify`), and `focus_report`
says which labels would end up thin or broad. Applying rows is the page's job.

`draft_definitions` is the second kind of proposal: one line for each label
with no definition, written from a sample of the cards carrying it. The user
edits and saves those in the vocabulary editor; nothing is saved here.
"""
import dataclasses
import logging
import os
from collections import Counter
from dataclasses import dataclass

from . import guard
from .analysis import SKILLS_DIR
from .context import TagContext, front_face
from .providers import ProviderError
from .schemas import definition_schema, tag_schema

logger = logging.getLogger(__name__)

SKILL_FILE = "tag_suggester.md"
DEFINITION_SKILL_FILE = "tag_definitions.md"
# Cards per request. A whole deck's answer came to 10,000-13,000 tokens against a
# 16,000 limit in the backtest, and a cut-off answer fails to parse.
BATCH_SIZE = 50
# One row per card, each with a quote and a reason, so a full deck's answer is
# several times the usual length and takes minutes to come back.
MAX_OUTPUT_TOKENS = 16000
TIMEOUT_SECONDS = 300
# Already-tagged cards shown per label as examples of how the user uses it.
EXAMPLES_PER_LABEL = 2
# Cards sent per label when drafting a definition.
DEFINITION_SAMPLE_CARDS = 6
# For the note before a Suggest. The backtest's decks came to about 130 tokens
# out per card, and the schema adds about 700 tokens in.
OUTPUT_TOKENS_PER_CARD = 140
SCHEMA_TOKENS = 700
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


@dataclass
class DefinitionDrafts:
    drafts: dict              # {label: drafted definition}
    one_card: list            # drafted labels that rest on a single card
    no_cards: list            # labels with no card to draft from
    missing: list             # labels the model gave no usable line for
    input_tokens: int = 0
    output_tokens: int = 0
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


def focus_report(ctx: TagContext, rows, replaced=()):
    """Each vocabulary label's count if `rows` were applied on top of the
    deck's current tags, and which labels would be thin or broad. A row is
    additive unless its card name is in `replaced`. Counts are copies, and a
    card counts for each of its tags, so they overlap and don't add up to the
    deck."""
    tags = {c.name: {t.casefold() for t in ctx.current.get(c.name, [])} for c in ctx.cards}
    for r in rows:
        if r.card is not None:
            proposed = {t.casefold() for t in r.tags}
            tags[r.card.name] = proposed if r.card.name in replaced else tags[r.card.name] | proposed
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


def starts_ticked(ctx: TagContext, rows):
    """Which rows the review panel ticks at the start. Returns ({card names},
    [thin labels]). A verified row starts ticked when it gives a card its first
    tag or a new primary; a row that only adds a secondary tag, or that adds a
    label which would stay thin, starts unticked, as do unverified and
    `conflict` rows."""
    def have(r):
        return {t.casefold() for t in ctx.current.get(r.card.name, [])}

    picked = [r for r in rows if r.card and r.verified and r.status == "new"
              and (not have(r) or r.primary.casefold() not in have(r))]
    thin = focus_report(ctx, picked).thin
    folded = {label.casefold() for label in thin}
    return {r.card.name for r in picked if not ({t.casefold() for t in r.tags} - have(r)) & folded}, thin


def estimate_tokens(ctx: TagContext):
    """(input, output) tokens a Suggest should cost, at about 4 characters per
    token, for the note shown before spending anything."""
    with open(os.path.join(SKILLS_DIR, SKILL_FILE), encoding="utf-8") as f:
        system = f.read()
    return ((len(system) + len(build_prompt(ctx))) // 4 + SCHEMA_TOKENS, len(ctx.cards) * OUTPUT_TOKENS_PER_CARD)


def _merge_retry(first, second, keys):
    """The first answer's rows, with the retry's in place for the cards in
    `keys`. A flagged card the retry skipped keeps its row, still flagged."""
    rows = second.get("cards") if isinstance(second, dict) else None
    redone = {front_face(r.get("card")): r for r in rows or [] if isinstance(r, dict)}
    redone = {k: r for k, r in redone.items() if k in keys}
    kept = [r for r in first.get("cards") or []
            if not isinstance(r, dict) or front_face(r.get("card")) not in redone]
    return {"cards": kept + list(redone.values())}


def _batches(cards, size=None):
    """`cards` in the fewest even chunks of at most `size` (BATCH_SIZE)."""
    size = size or BATCH_SIZE
    n = -(-len(cards) // size)
    step = -(-len(cards) // n)
    return [cards[i:i + step] for i in range(0, len(cards), step)]


def run_tag_proposals(provider, ctx: TagContext, hide_current=False, examples=None):
    """Ask for tags for every card in `ctx`, BATCH_SIZE cards per request so a
    full answer stays inside the output limit. Returns a TagResult. If a batch
    fails, `error` says which and why, `checked` holds the batches that
    finished (None if none did), and the failed batch's cards read as missing."""
    if not ctx.cards:
        return TagResult(None, None, False, 0, 0, "This deck has no cards to tag.")
    if not ctx.vocabulary:
        return TagResult(None, None, False, 0, 0, f"No labels with definitions in the '{ctx.tag_type}' layer.")
    with open(os.path.join(SKILLS_DIR, SKILL_FILE), encoding="utf-8") as f:
        system = f.read()
    if examples is None:
        examples = {} if hide_current else pick_examples(ctx)   # from the whole deck, not one batch
    tokens = [0, 0]
    batches = _batches(ctx.cards)
    rows, missing, retried, error = [], [], False, ""
    done = 0

    for number, cards in enumerate(batches, 1):
        part = dataclasses.replace(ctx, cards=cards)
        where = f"batch {number} of {len(batches)}" if len(batches) > 1 else "the request"
        checked, was_retried, why = _run_batch(provider, part, system, hide_current, examples, tokens)
        if checked is None:
            logger.warning("Tag proposals for deck %s (%s): %s failed: %s", ctx.deck_id, ctx.tag_type, where, why)
            error = (f"The model call for {where} ({len(cards)} cards: {cards[0].name} to {cards[-1].name}) "
                     f"failed: {why}")
            if done:
                error += f" {done} of {len(batches)} batches finished and are kept; the rest were not reviewed."
            missing += [c.name for b in batches[number - 1:] for c in b]
            break
        done += 1
        retried |= was_retried
        rows += checked.rows
        missing += checked.missing
        logger.info("Tag proposals for deck %s (%s): %s done, %d violations%s", ctx.deck_id, ctx.tag_type,
                    where, len(checked.violations), ", retried" if was_retried else "")
    if not done:
        return TagResult(None, None, False, tokens[0], tokens[1], error)

    checked = guard.CheckedTags(rows, missing)
    for r in rows:
        r.status = classify(r.tags, ctx.current.get(r.card.name, []) if r.card else [])
    focus = focus_report(ctx, [r for r in rows if r.verified and r.status == "new"])
    return TagResult(checked, focus, retried, tokens[0], tokens[1], error)


def _run_batch(provider, part, system, hide_current, examples, tokens):
    """One request plus its one retry, for the cards in `part`. Returns
    (CheckedTags, retried, "") or (None, False, why) when the model call or its
    JSON failed. `tokens` is [in, out] and is added to in place."""
    schema = tag_schema(part)
    messages = [{"role": "user", "content": build_prompt(part, hide_current, examples)}]

    def ask():
        resp = provider.chat(messages, system=system, schema=schema,
                             max_tokens=MAX_OUTPUT_TOKENS, timeout=TIMEOUT_SECONDS)
        tokens[0] += resp.input_tokens
        tokens[1] += resp.output_tokens
        return resp

    try:
        resp = ask()
    except ProviderError as e:
        return None, False, str(e)
    if not isinstance(resp.parsed, dict):
        return None, False, "the model did not return valid JSON (the answer may have been cut off)."
    checked = guard.check_tags(resp.parsed, part)
    if not checked.violations and not checked.missing:
        return checked, False, ""

    # One retry that names each problem; keep the first answer if it fails.
    messages += [{"role": "assistant", "content": resp.text},
                 {"role": "user", "content": guard.tag_retry_prompt(checked)}]
    try:
        retry = ask()
    except ProviderError as e:
        logger.warning("Tag retry failed, keeping the first answer: %s", e)
        return checked, True, ""
    if retry.parsed is None:
        return checked, True, ""
    return guard.check_tags(_merge_retry(resp.parsed, retry.parsed, checked.retry_keys()), part), True, ""


def _definition_prompt(ctx: TagContext, samples):
    """User message for draft_definitions: the deck, then each label's cards."""
    m = ctx.meta
    blocks = [f"LABEL: {label}\n" + "\n".join(_card_line(c, None) for c in cards) for label, cards in samples.items()]
    return (f"DECK: {m.get('name')} (commander: {m.get('commander') or 'unknown'})\n"
            f"Stated plan: {m.get('description') or 'not recorded'}\n"
            f"Tag layer: {ctx.tag_type}\n\n" + "\n\n".join(blocks))


def draft_definitions(provider, ctx: TagContext, labels):
    """Ask for one line defining each of `labels`, from the deck's cards that
    carry it. Returns a DefinitionDrafts. No retry: a label left out is reported
    in `missing`, and it can be drafted again or written by hand."""
    samples, one_card = {}, []
    for label in labels:
        carrying = [c for c in ctx.cards
                    if label.casefold() in {t.casefold() for t in ctx.current.get(c.name, [])}]
        if carrying:
            samples[label] = carrying[:DEFINITION_SAMPLE_CARDS]
            if len(carrying) == 1:
                one_card.append(label)
    no_cards = [label for label in labels if label not in samples]
    if not samples:
        return DefinitionDrafts({}, [], no_cards, [])
    with open(os.path.join(SKILLS_DIR, DEFINITION_SKILL_FILE), encoding="utf-8") as f:
        system = f.read()
    try:
        resp = provider.chat([{"role": "user", "content": _definition_prompt(ctx, samples)}], system=system,
                             schema=definition_schema(list(samples)), timeout=TIMEOUT_SECONDS)
    except ProviderError as e:
        logger.warning("Definition drafts for deck %s (%s) failed: %s", ctx.deck_id, ctx.tag_type, e)
        return DefinitionDrafts({}, [], no_cards, list(samples), error=f"The model call failed: {e}")
    drafts, missing = guard.check_definitions(resp.parsed, list(samples))
    return DefinitionDrafts(drafts, [label for label in one_card if label in drafts], no_cards, missing,
                            resp.input_tokens, resp.output_tokens)
