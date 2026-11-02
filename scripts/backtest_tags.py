"""
Backtest of the tag suggester (docs/TAG_SUGGESTIONS_PLAN.md Phase 3). Hand-run,
uses the real API and costs money; it never writes to the database.

For each deck the model re-tags the `function` layer from the deck's own label
vocabulary, with the cards' current tags hidden from the prompt. A few tagged
cards per label are held out as the prompt's examples and are not scored, so the
model is never shown the answer to a card it is graded on.

    python scripts/backtest_tags.py --deck 12 --deck 2          # estimate, ask, run
    python scripts/backtest_tags.py --deck 12 --yes             # skip the go-ahead
    python scripts/backtest_tags.py --deck 7 --layer strategy --card Mutavault
    python scripts/backtest_tags.py --deck 12 --model Gemini --dry   # estimate only

Scores, over scored cards the user has tagged: exact (same set of labels), any
(shares at least one label), and primary (the model's primary is the user's
first tag). Cards the user left untagged are not scored; the model's tags on
them are counted as extras. Disagreements are listed per deck.
"""
import argparse
import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, "mtg_collection.db")

sys.path.insert(0, BASE_DIR)
from dashboard_lib import queries as q
from dashboard_lib.advisor import tagging
from dashboard_lib.advisor.context import build_tag_context
from dashboard_lib.advisor.providers import PROVIDER_CHOICES, ProviderError, make_provider

CHARS_PER_TOKEN = 4          # the same rough estimate the plan used
SCHEMA_TOKENS = 700          # schema size measured in the Phase 2 dry run
HELD_OUT_PER_LABEL = 2


def hold_out(ctx):
    """({label: [example card names]}, set of those names). The first tagged
    cards by name for each label the model is offered; deterministic so reruns
    compare."""
    examples, held = {}, set()
    wanted = {label.casefold(): label for label in ctx.labels()}
    for c in sorted(ctx.cards, key=lambda c: c.name):
        for tag in ctx.current.get(c.name, [])[:1]:   # a card shows under its primary only
            label = wanted.get(tag.casefold())
            if label and len(examples.setdefault(label, [])) < HELD_OUT_PER_LABEL and c.name not in held:
                examples[label].append(c.name)
                held.add(c.name)
    return examples, held


def estimate(ctx, examples):
    """Rough input tokens: system prompt, user prompt and schema."""
    with open(os.path.join(tagging.SKILLS_DIR, tagging.SKILL_FILE), encoding="utf-8") as f:
        system = f.read()
    prompt = tagging.build_prompt(ctx, hide_current=True, examples=examples)
    return (len(system) + len(prompt)) // CHARS_PER_TOKEN + SCHEMA_TOKENS


def score(ctx, result, held):
    """Compare the model's rows to the user's tags. Returns (stats, disagreements,
    extras), where extras are the model's tags on cards the user left untagged."""
    stats = dict(scored=0, exact=0, any=0, primary=0, extras=0, unverified=0, untagged=0)
    disagreements = []
    extras = []
    for row in result.checked.rows:
        if row.card is None:
            continue
        name = row.card.name
        if not row.verified:
            stats["unverified"] += 1
        key = ctx.current.get(name, [])
        if name in held:
            continue
        if not key:
            stats["untagged"] += 1
            if row.tags:
                stats["extras"] += 1
                extras.append((name, row.tags, row.data.get("reason", "")))
            continue
        stats["scored"] += 1
        mine, theirs = {t.casefold() for t in row.tags}, {t.casefold() for t in key}
        stats["exact"] += mine == theirs
        stats["any"] += bool(mine & theirs)
        stats["primary"] += bool(row.primary) and row.primary.casefold() == key[0].casefold()
        if mine != theirs:
            disagreements.append((name, key, row.tags, row.data.get("reason", "")))
    return stats, disagreements, extras


def pct(n, total):
    return f"{n}/{total} ({100 * n / total:.0f}%)" if total else "n/a"


def report(ctx, result, held, cards_of_interest):
    stats, disagreements, extras = score(ctx, result, held)
    n = stats["scored"]
    print(f"\n=== {ctx.meta.get('name')} ({ctx.tag_type}) ===")
    print(f"Labels: {', '.join(ctx.labels())}")
    print(f"Tokens: {result.input_tokens} in, {result.output_tokens} out"
          f"{', retried' if result.retried else ''}")
    print(f"Scored cards (tagged by you, not held out): {n}")
    print(f"  exact match:   {pct(stats['exact'], n)}")
    print(f"  any match:     {pct(stats['any'], n)}")
    print(f"  primary match: {pct(stats['primary'], n)}")
    print(f"  extras (model tagged a card you left untagged): {pct(stats['extras'], stats['untagged'])}")
    print(f"  unverified rows: {stats['unverified']}; not returned: {len(result.checked.missing)}")
    f = result.focus
    if f:
        print(f"  tags per tagged card: {f.tags_per_card}; thin: {f.thin or 'none'}; broad: {f.broad or 'none'}")
        print(f"  label counts if applied: {f.counts}")
    if disagreements:
        print(f"\nDisagreements ({len(disagreements)}): card | yours -> model's | reason")
        for name, key, mine, reason in sorted(disagreements):
            print(f"  {name} | {', '.join(key)} -> {', '.join(mine) or 'none'} | {reason}")
    if extras:
        print(f"\nExtras ({len(extras)}): card | model's | reason")
        for name, mine, reason in sorted(extras):
            print(f"  {name} | {', '.join(mine)} | {reason}")
    for row in result.checked.rows:
        if row.card and any(w.casefold() in row.card.name.casefold() for w in cards_of_interest):
            print(f"\n[{row.card.name}] yours: {ctx.current.get(row.card.name, [])} "
                  f"model: {row.tags} ({row.data.get('evidence_quote')!r}) {row.data.get('reason', '')}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--deck", type=int, action="append", required=True, help="deck id; repeat for several")
    ap.add_argument("--layer", default="function")
    ap.add_argument("--model", default="Gemini", choices=PROVIDER_CHOICES)
    ap.add_argument("--card", action="append", default=[], help="print the full row for cards matching this name")
    ap.add_argument("--yes", action="store_true", help="skip the go-ahead prompt")
    ap.add_argument("--dry", action="store_true", help="print the estimate and stop")
    args = ap.parse_args()

    if not os.path.exists(DB_PATH):
        sys.exit(f"No database found at {DB_PATH}.")
    conn = q.open_connection(DB_PATH)
    runs = []
    for deck_id in args.deck:
        ctx = build_tag_context(conn, deck_id, args.layer)
        examples, held = hold_out(ctx)
        if not ctx.vocabulary or not any(ctx.current.values()):
            print(f"Skipping {ctx.meta.get('name')}: no {args.layer} labels or no existing tags to score against.")
            continue
        runs.append((ctx, examples, held, estimate(ctx, examples)))
        print(f"{ctx.meta.get('name')}: {len(ctx.cards)} cards, {len(held)} held out as examples, "
              f"about {runs[-1][3]} input tokens")
    if not runs or args.dry:
        return
    if not args.yes and input(f"Run {len(runs)} request(s) on {args.model}? [y/N] ").strip().lower() != "y":
        return
    try:
        provider = make_provider(args.model)
    except ProviderError as e:
        sys.exit(str(e))
    for ctx, examples, held, _ in runs:
        result = tagging.run_tag_proposals(provider, ctx, hide_current=True, examples=examples)
        if result.error:
            print(f"\n{ctx.meta.get('name')}: failed: {result.error}")
            continue
        report(ctx, result, held, args.card)


if __name__ == "__main__":
    main()
