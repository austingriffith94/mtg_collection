# advisor/

Grounded MTG deck analysis with a swappable model. Design and phasing live in
[ADVISOR_PLAN.md](../../ADVISOR_PLAN.md); this file describes what is built.

**Status: Phase 1** — analysis mode (deck doctor) on Gemini. No chat mode, no
Ollama/Anthropic adapters yet. Read-only: nothing here writes to the database.

## How it works

Two layers keep guardrails identical for every model:

1. **Grounding (code).** `context.py` computes facts from the DB; `guard.py`
   validates every model answer before the user sees it.
2. **Provider (swappable).** `providers.py` hides the model behind
   `chat(messages, system, schema) -> ProviderResponse`.

Flow of `analysis.run_analysis`: build `DeckContext` → prompt (skill text +
computed facts + deck + candidates) → provider call with an enum-constrained
schema → `guard.check_analysis` → if violations, **one** retry that lists them
→ return the result, with anything still failing marked unverified (the page
collapses these under a badge; nothing is hidden or dropped).

## Files

| File | Role |
|---|---|
| `context.py` | `build_deck_context`: deck cards, stats (curve, pips, role tallies), candidate pool. `mechanical_roles` = oracle-text heuristics for ramp/draw/removal/wipe/tutor/counter. |
| `schemas.py` | `analysis_schema(ctx)`: Gemini-dialect response schema; card names are enums of the deck / candidate pool. |
| `guard.py` | Checks name (front-face match), evidence quote (substring of stored oracle text), claimed roles (vs computed). `retry_prompt` builds the corrective message. |
| `providers.py` | `GeminiProvider` (REST, `x-goog-api-key` header, backoff on 429/5xx), `FakeProvider` for tests. |
| `analysis.py` | Prompt building and the validate/retry loop. |
| `skills/deck_doctor.md` | System prompt for the deck-doctor skill. Add skills here and register in `analysis.SKILLS`. |

Page: `pages/10_Advisor.py`. Tests: `tests/test_advisor.py` (offline, includes
the starter eval cases).

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
- **Verified means the facts are real**, not that the advice is good.
- **Commander** cannot be cut or replaced; guard treats it as unknown for cuts.
- NULLs from pandas must go through `context._records` (NaN is truthy).
