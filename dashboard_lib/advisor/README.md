# advisor/

Grounded MTG deck analysis and chat with a swappable model. Design and
phasing live in [ADVISOR_PLAN.md](../../ADVISOR_PLAN.md); this file
describes what is built.

**Status: Phase 3** — analysis mode (deck doctor) and tool-based chat, both
on Gemini. No Ollama/Anthropic adapters yet. Read-only: nothing here writes
to the database.

## How it works

Two layers keep guardrails identical for every model:

1. **Grounding (code).** `context.py` computes facts from the DB; `guard.py`
   validates every model answer before the user sees it.
2. **Provider (swappable).** `providers.py` hides the model behind
   `chat(messages, system, schema, tools) -> ProviderResponse`.

**Analysis mode** (`analysis.run_analysis`): build `DeckContext` → prompt
(skill text + computed facts + deck + candidates) → provider call with an
enum-constrained schema → `guard.check_analysis` → if violations, **one**
retry that lists them → return the result, with anything still failing
marked unverified (the page collapses these under a badge; nothing is hidden
or dropped).

**Chat mode** (`chat.run_chat_turn`): loop the provider against
`tools.TOOL_SPECS` (get_card, search_cards, list_decks, get_deck) until it
stops calling tools or hits the iteration/token budget, then one more
schema-only call asks it to restate its answer with every card it mentioned
— `guard.check_chat_answer` checks each one (name, quote, claimed roles)
against the *whole* database, since chat has no small pre-filtered pool to
enum names against the way analysis mode does. If that restate call fails,
the answer is still shown, just without a mention check. The restate step
isn't fully trusted either — a live check showed the model can quote a
card's text in its answer and still omit it from the restated list — so
every card actually resolved via a successful `get_card` call that turn is
folded into the checked mentions regardless of what the model self-reports.
Gated off per provider: the page only offers the Chat tab when
`provider.capabilities.tools` is true.

## Files

| File | Role |
|---|---|
| `context.py` | `build_deck_context`: deck cards, stats (curve, pips, role tallies), candidate pool. `mechanical_roles` = oracle-text heuristics for ramp/draw/removal/wipe/tutor/counter. `lookup_card_text`: front-face name resolution shared by `tools.get_card` and `guard.check_chat_answer`. |
| `schemas.py` | `analysis_schema(ctx)`: enum-constrained to one deck's pool. `chat_answer_schema()`: no enum, for the chat restate step. Both in Gemini's response-schema dialect. |
| `guard.py` | `check_analysis`: name (front-face match)/quote (oracle-text substring)/role checks for analysis mode, `retry_prompt` for the corrective follow-up. `check_chat_answer`: the same name/quote/role checks for chat mentions, against the whole database instead of one deck's pool. |
| `tools.py` | Chat-mode tools (`get_card`, `search_cards`, `list_decks`, `get_deck`) and `dispatch`, which never raises — a bad call becomes an `{"error": ...}` result the model sees. `TOOL_SPECS` is the Gemini-dialect function-declaration list. |
| `providers.py` | `GeminiProvider` (REST, `x-goog-api-key` header, backoff on 429/5xx, function calling), `FakeProvider` for tests. |
| `analysis.py` | Analysis mode: prompt building and the validate/retry loop. |
| `chat.py` | Chat mode: the tool-call loop, its iteration/token budget, and the restate-then-check step. |
| `skills/deck_doctor.md` | System prompt for the deck-doctor analysis skill. Add skills here and register in `analysis.SKILLS`. |
| `skills/chat.md` | System prompt for chat mode (tool-first, no card facts from memory, read-only). |

Page: `pages/10_Advisor.py` (Analysis and Chat tabs). Tests:
`tests/test_advisor.py` (offline, includes the starter eval cases).

## Configuration

`.streamlit/secrets.toml` (gitignored):

```toml
[advisor]
google_api_key = "..."
gemini_model = "gemini-3.5-flash"   # optional; this is the default
```

`GOOGLE_API_KEY` in the environment also works. `gemini-2.5-flash` is retired
for this key; see the Phase 0 notes in the plan.

## Things to know

- **Candidate pool** = owned, commander-legal, colour-identity-legal cards not
  already in the deck, free copies first, capped at 60 (the cap keeps the enum
  and prompt small). Candidates sleeved in another deck are flagged, not hidden.
- **Roles are heuristics.** A disagreement means "the card's text does not
  match the claimed role by our regexes", which can occasionally be our miss.
  Tune the regexes in `context.py`, not the guard.
- **Chat budgets.** `chat.MAX_TOOL_ITERATIONS` (6) caps tool-call rounds per
  turn; `chat.TOKEN_CEILING` (30k) caps total tokens per turn. Hitting either
  ends the turn with an error rather than a silent partial answer.
- **Chat history is answers only.** The page only persists `{"role",
  "content"}` turns across messages; a turn's tool calls and the restate
  round-trip live only inside that one `run_chat_turn` call. The model can
  always look a card up again next turn, so nothing is lost by this.
- **Verified means the facts are real**, not that the advice is good.
- **Commander** cannot be cut or replaced; guard treats it as unknown for cuts.
- NULLs from pandas must go through `context._records` (NaN is truthy).
