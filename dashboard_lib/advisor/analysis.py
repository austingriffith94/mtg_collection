"""
Analysis mode: single-shot, strongest guardrails, no tool calling.

run_analysis builds the prompt from a DeckContext, asks the provider for
schema-constrained output, validates it with guard.py, and retries ONCE with
the specific violations. Whatever still fails is returned marked unverified,
never hidden. Works with any provider that implements `chat`.
"""
import os
from dataclasses import dataclass

from . import guard
from .context import DeckContext
from .providers import ProviderError
from .schemas import analysis_schema

SKILLS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "skills")
# Combos spelled out in the prompt, per list, most popular first.
PROMPT_COMBO_CAP = 15

SKILLS = {
    "Deck doctor": "deck_doctor.md",
    "Upgrade finder": "upgrade_finder.md",
    "Build from collection": "build_from_collection.md",
    "Cut advisor": "cut_advisor.md",
}


@dataclass
class AnalysisResult:
    checked: guard.CheckedAnalysis | None
    computed: dict            # stats from the DB, shown separately from model opinion
    retried: bool
    input_tokens: int
    output_tokens: int
    error: str = ""


def load_skill(skill):
    with open(os.path.join(SKILLS_DIR, SKILLS[skill]), encoding="utf-8") as f:
        return f.read()


def _card_line(c, with_availability=False):
    qty = f"{c.quantity}x " if c.quantity > 1 else ""
    flags = []
    if c.is_commander:
        flags.append("COMMANDER")
    if with_availability:
        flags.append("available" if c.available else "sleeved in: " + ", ".join(sorted(set(c.locations))))
    if c.game_changer:
        flags.append("Game Changer")
    tail = f" [{'; '.join(flags)}]" if flags else ""
    return f"- {qty}{c.name} | {c.mana_cost or '-'} | {c.type_line}{tail}\n  {c.oracle_text.replace(chr(10), ' / ')}"


def _notes_section(goals):
    """The user's notes on the deck, or "" if none are recorded."""
    parts = [(title, goals.get(key)) for title, key in (
        ("Win conditions", "win_conditions"), ("Strengths", "strengths"), ("Weaknesses", "weaknesses"),
        ("Main themes", "main_themes"), ("Sub themes", "sub_themes"))]
    lines = [f"- {title}: " + "; ".join(items) for title, items in parts if items]
    if not lines:
        return ""
    return "THE USER'S NOTES ON THIS DECK (their intent, not verified facts):\n" + "\n".join(lines) + "\n\n"


def _combo_line(combo):
    tag = f" [{combo['bracket_tag']}]" if combo["bracket_tag"] else ""
    return f"- {' + '.join(combo['uses'])}: {', '.join(combo['produces'][:3])}{tag}"


def _combos_section(ctx):
    """Combos the deck has, and (unless the bracket is low) combos a listed
    candidate would finish. Only cards already in the lists appear, so the
    "use only listed cards" rule holds."""
    included = ctx.included_combos()[:PROMPT_COMBO_CAP]
    out = ""
    if included:
        out += ("COMBOS ALREADY IN THE DECK (cutting any piece loses that combo):\n"
                + "\n".join(_combo_line(c) for c in included) + "\n\n")
    if not ctx.low_combo_bracket:
        almost = sorted({id(x): x for c in ctx.candidates for x in c.completes_combos}.values(),
                        key=lambda x: -x["popularity"])[:PROMPT_COMBO_CAP]
        if almost:
            out += ("COMBOS ONE CANDIDATE AWAY (optional; weigh against the user's notes and bracket):\n"
                    + "\n".join(_combo_line(c) for c in almost) + "\n\n")
    return out


def build_prompt(ctx: DeckContext):
    """User message: deck meta, the user's notes, computed facts, the deck,
    its combos, the candidate pool."""
    m, s = ctx.meta, ctx.stats
    facts = "\n".join(f"- {k}: {v}" for k, v in s.items() if v not in (None, "", {}))
    return (
        f"DECK: {m.get('name')} (commander: {m.get('commander') or 'unknown'}"
        f"{', partner: ' + m['partner'] if m.get('partner') else ''})\n"
        f"Stated plan: {m.get('description') or 'not recorded'}\n\n"
        + _notes_section(ctx.goals) +
        f"COMPUTED FACTS (from the database; authoritative):\n{facts}\n\n"
        f"DECK ({len(ctx.cards)} distinct cards):\n" + "\n".join(_card_line(c) for c in ctx.cards)
        + "\n\n" + _combos_section(ctx)
        + f"CANDIDATES ({len(ctx.candidates)} owned cards that could be added):\n"
        + ("\n".join(_card_line(c, True) for c in ctx.candidates) or "(none)")
    )


def run_analysis(provider, ctx: DeckContext, skill="Deck doctor"):
    system = load_skill(skill)
    schema = analysis_schema(ctx)
    messages = [{"role": "user", "content": build_prompt(ctx)}]
    tokens_in = tokens_out = 0

    def ask():
        nonlocal tokens_in, tokens_out
        resp = provider.chat(messages, system=system, schema=schema)
        tokens_in += resp.input_tokens
        tokens_out += resp.output_tokens
        return resp

    def result(checked, retried, error=""):
        return AnalysisResult(checked, ctx.stats, retried, tokens_in, tokens_out, error)

    try:
        resp = ask()
    except ProviderError as e:
        return result(None, False, str(e))
    if resp.parsed is None:
        return result(None, False, "The model did not return valid JSON.")
    checked = guard.check_analysis(resp.parsed, ctx)
    if not checked.violations:
        return result(checked, False)

    # One retry that names each violation; keep the first answer if it fails.
    messages += [{"role": "assistant", "content": resp.text},
                 {"role": "user", "content": guard.retry_prompt(checked)}]
    try:
        retry = ask()
    except ProviderError:
        return result(checked, True)
    if retry.parsed is None:
        return result(checked, True)
    return result(guard.check_analysis(retry.parsed, ctx), True)
