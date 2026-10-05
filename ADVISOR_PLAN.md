# Advisor Plan — Grounded MTG Chat with a Swappable Model

Status: **planned, not started.** Nothing in this file is built yet.

## Goal

An AI advisor inside the dashboard that can analyse decks, suggest upgrades
from the collection, and chat — without making up Magic cards. The model is
swappable: a free local model, a free hosted model (Gemini), and paid models
(Claude Sonnet / Opus) later.

## Core idea

Two layers keep the chat flexible while keeping the guardrails identical for
every model.

1. **Grounding layer (code, model-independent).** Facts come from the DB,
   and every model output is validated before the user sees it.
2. **Provider layer (swappable).** Ollama, Gemini and Anthropic sit behind
   one interface.

Guardrails live in layer 1, so a weaker local model gets the same safety net
as Opus; it just trips it more often.

## Architecture

```
pages/10_Advisor.py          chat UI + provider toggle
dashboard_lib/advisor/
  providers.py               Provider interface + Ollama / Gemini / Anthropic adapters
  context.py                 builds grounded context from the DB (deck, stats, candidates)
  tools.py                   read-only tools wrapping queries.py / loaders.py
  guard.py                   validators: name check, evidence-quote check, retry/flag
  schemas.py                 structured-output schemas (analysis, chat answer)
  skills/*.md                deck doctor, upgrade finder, build from collection, cut advisor
  chat.py                    conversation loop, budgets, mode selection
  README.md
```

## Two modes, one UI

**Analysis mode (single-shot, strongest guardrails).** The user picks a deck
and a skill. Code builds the context (oracle text, computed stats, candidate
pool). The model returns structured output, `guard.py` validates it, and the
UI shows computed numbers separately from model opinion. Works on every
provider and needs no tool calling.

**Chat mode (flexible, tool-based).** Free-form conversation; the model calls
read-only tools. Guardrails: every card name the model mentions is checked
against the DB and unknown names are flagged inline; the system prompt
requires card text to come from `get_card` results; quoted card text gets the
same substring check; tool-call and iteration caps apply. Depends on tool
calling reliability, which varies by provider, so it is gated per provider.

## Provider layer

One interface: `chat(messages, tools=None, schema=None) -> normalized response`.
Each adapter declares capabilities (tools, structured output, both) and the
app picks the mode accordingly.

| Provider | Role | Notes |
|---|---|---|
| Ollama (local) | Free, offline, private | Hardware: Ryzen 7 5800X3D, 32 GB RAM, RX 6800 XT 16 GB VRAM (ROCm). Default: Qwen3 14B at 4-bit. Stretch: Mistral Small ~24B at 4-bit with a short context. Confirm exact tags in Ollama's library when installing. |
| Gemini 2.5 Flash | Free hosted | Default for quality at no cost. Key stored in `.streamlit/secrets.toml` (gitignored). Free tier: tight rate limits, and Google may use prompts to improve products. |
| Claude Sonnet 5.5 / Opus 5.5 | Paid, later | Adapter stubbed; enable with a key. Add prompt caching at that point. |

Sidebar toggle stores the choice in session state, shows a token/cost
counter, and warns when a provider lacks a feature. Local models often need
a "JSON-only" prompt fallback plus a retry for structured output.

## Guardrails (shared by all providers)

1. **Compute facts in Python.** Counts, curve, colour pips, ramp/draw/removal
   tallies, prices, availability and combos come from the DB; the model only
   interprets and prioritises.
2. **Constrain names.** Schema enums in analysis mode; post-hoc name
   validation in chat mode. Match by `oracle_id` (as `card_lookup` does) so
   MDFCs and multiple printings resolve correctly.
3. **Evidence quotes verified by substring match** against stored oracle text.
4. **One retry with the specific violation**, then show the result with the
   failing items marked "unverified".
5. **Read-only everywhere** in v1. No writes to the collection.
6. **Visible provenance.** UI labels computed vs model-generated content.
7. **Budgets.** Max tool iterations and a token ceiling per message.

## Phases

**Phase 0 — Smoke test (~15 min).** One Gemini call with the stored key.
Confirms the key works (its `AQ.` prefix is not the usual `AIza`) and how
Gemini handles schema output.

**Phase 1 — Grounding core + analysis mode on Gemini.** `context.py`,
`guard.py`, `schemas.py`, the deck-doctor skill, a minimal page, and tests
with a fake provider.

**Phase 2 — Provider layer + Ollama.** Extract the interface, add the Ollama
adapter and the toggle. Install Ollama with ROCm support and pull the model.
Compare Gemini vs local on the same deck.

**Phase 3 — Chat mode.** Tools, name flagging, conversation loop. Evaluate
tool-call reliability per provider and gate chat mode where a provider is too
flaky.

**Phase 4 — More skills + eval set.** Upgrade finder, build-from-collection,
cut advisor (can use game-tracking win rates). Add ~10 fixed test questions
that score each provider on fabricated names and misquoted text.

**Phase 5 — Claude adapter.** Add the key, enable Sonnet and Opus, add prompt
caching, compare cost against Gemini.

**Later / optional.** Suggested changes written to `deck_changes` as `idea`
entries with explicit confirmation. A notes table for user preferences.

## Risks

| Risk | Mitigation |
|---|---|
| Local 14B model is unreliable at tool calls | Analysis mode needs no tools; chat mode gated per provider |
| Gemini free-tier limits or key issues | Phase 0 test, retry with backoff, Ollama fallback |
| Providers differ in structured-output / tool support | Capability flags per adapter; JSON-prompt + retry fallback |
| Name matching misses MDFC / split cards | Reuse existing `oracle_id` matching |
| Model quotes correctly but concludes wrongly | Expected; UI labels it as opinion, eval set compares providers |
| Key exposure | Key lives only in gitignored `.streamlit/secrets.toml`; rotate in Google AI Studio if exposed (the current key was pasted into a chat once) |

## Housekeeping done

- `.streamlit/secrets.toml` added to `.gitignore`; key saved under
  `[advisor] google_api_key`.
- Scratch sample from Gemini removed from the repo root.

## Conventions

Follow the project's documentation standards: header comment per module,
inline comments for non-obvious logic, and a README.md for the new
`dashboard_lib/advisor/` sub-area. Add offline tests under `tests/` using a
fake provider, matching the existing pytest layout.
