"""
Chat mode: free-form conversation with read-only tool calls (ADVISOR_PLAN.md
Phase 3). Gated to providers that declare `capabilities.tools`.

run_chat_turn drives one turn: loop the provider against `tools.TOOL_SPECS`
until it stops calling tools (or the iteration/token budget runs out), then
makes one more call asking it to restate the answer with its card mentions
in schema-constrained JSON, so guard.py can check each one against the
database. That restate step is a separate request (never combined with
`tools` in the same call — see providers.GeminiProvider.chat) because chat
has no small pre-filtered pool to enum card names against the way analysis
mode does (ADVISOR_PLAN.md guardrail #2), so the check happens after the
fact against the whole database instead.

If the restate call fails or returns unusable JSON, the answer is still
shown — it just carries no mention check, rather than being thrown away.

The restate step depends on the model choosing to list every card it
mentioned, which a live check showed is not reliable (a card quoted plainly
in the answer came back with an empty `mentions` list). As a second,
model-independent source of truth, every card actually resolved via a
successful `get_card` tool call this turn is folded into the checked
mentions regardless of what the restate step reports — that lookup already
came straight from the database, so it needs no further checking.
"""
from dataclasses import dataclass

from . import guard, tools
from .guard import CheckedMention
from .providers import ProviderError
from .schemas import chat_answer_schema

MAX_TOOL_ITERATIONS = 6
TOKEN_CEILING = 30_000   # guardrail #8: a budget per message, not per session

RESTATE_PROMPT = (
    "Restate your last answer to the user, unchanged, and list every card you named or made a "
    "textual or mechanical claim about. For each, give its exact name; if you quoted or described "
    "its printed text, the exact substring of that text you relied on (empty string if not); and "
    "any roles (ramp/draw/removal/wipe/tutor/counter) you said or implied it fills (empty list if "
    "none)."
)


@dataclass
class ChatTurnResult:
    checked: guard.CheckedChat | None
    tool_log: list            # [{"name", "args", "result"}] — provenance for the UI
    input_tokens: int
    output_tokens: int
    error: str = ""


def _tool_resolved_cards(tool_log):
    """Names successfully looked up via get_card this turn: ground truth
    from the database, independent of whether the model's restate step
    remembered to list them."""
    return {t["args"].get("name") for t in tool_log if t["name"] == "get_card" and "error" not in t["result"]}


def load_system_prompt():
    import os
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "skills", "chat.md")
    with open(path, encoding="utf-8") as f:
        return f.read()


def run_chat_turn(provider, conn, messages):
    """`messages` is the visible history ending in the new user turn: plain
    {"role": "user"|"assistant", "content": str} pairs. Tool-call/tool-result
    turns live only inside this one call (built on a local copy) and are not
    returned — the next turn can always look things up again, which keeps
    the caller's history simple and provider-agnostic."""
    if not provider.capabilities.tools:
        return ChatTurnResult(None, [], 0, 0,
                               f"{provider.name} does not support tool calling; chat mode is off for it.")
    system = load_system_prompt()
    tool_log = []
    tokens_in = tokens_out = 0
    msgs = list(messages)

    def ask(schema=None, use_tools=True):
        nonlocal tokens_in, tokens_out
        resp = provider.chat(msgs, system=system, schema=schema,
                              tools=tools.TOOL_SPECS if use_tools else None)
        tokens_in += resp.input_tokens
        tokens_out += resp.output_tokens
        return resp

    def result(checked, error=""):
        return ChatTurnResult(checked, tool_log, tokens_in, tokens_out, error)

    final_text = None
    for _ in range(MAX_TOOL_ITERATIONS):
        if tokens_in + tokens_out > TOKEN_CEILING:
            return result(None, "Token budget exceeded for this turn.")
        try:
            resp = ask()
        except ProviderError as e:
            return result(None, str(e))
        if not resp.tool_calls:
            final_text = resp.text
            break
        msgs.append({"role": "assistant", "tool_calls": resp.tool_calls})
        for call in resp.tool_calls:
            outcome = tools.dispatch(conn, call["name"], call.get("args") or {})
            tool_log.append({"name": call["name"], "args": call.get("args") or {}, "result": outcome})
            msgs.append({"role": "tool", "name": call["name"], "content": outcome, "call_id": call.get("id")})
    if final_text is None:
        return result(None, f"Gave up after {MAX_TOOL_ITERATIONS} tool-call rounds without a final answer.")

    msgs.append({"role": "assistant", "content": final_text})
    msgs.append({"role": "user", "content": RESTATE_PROMPT})
    try:
        structured = ask(schema=chat_answer_schema(), use_tools=False)
        parsed = structured.parsed
    except ProviderError:
        parsed = None
    checked = guard.check_chat_answer(conn, parsed) if parsed is not None else guard.CheckedChat(final_text, [])
    checked.answer = final_text   # the restated text is only scaffolding for the mention check

    already = {m.data.get("card") for m in checked.mentions}
    for name in _tool_resolved_cards(tool_log) - already:
        checked.mentions.append(CheckedMention({"card": name, "evidence_quote": "", "roles": []}, []))
    return result(checked)
