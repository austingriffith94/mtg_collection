# Advisor Plan — Grounded MTG Chat with a Swappable Model

Status: **Phase 0, 1, 3, 4 and 5 done** (analysis mode with four skills, and
tool-based chat, on Gemini or Anthropic via a sidebar toggle; page
`pages/10_Advisor.py`; see `dashboard_lib/advisor/README.md`). Phase 2
(Ollama) is skipped for now.

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
| Gemini 3.5 Flash (2.5 Flash retired for this key) | Free hosted | Default for quality at no cost. Key stored in `.streamlit/secrets.toml` (gitignored). Free tier: tight rate limits, and Google may use prompts to improve products. |
| Claude Sonnet 5.5 / Opus 5.5 | Paid, later | Adapter stubbed; enable with a key. Add prompt caching at that point. |

Sidebar toggle stores the choice in session state, shows a basic token
counter (cost display waits until a paid provider is enabled), and warns when a provider lacks a feature. Local models often need
a "JSON-only" prompt fallback plus a retry for structured output.

## Guardrails (shared by all providers)

1. **Compute facts in Python.** Counts, curve, colour pips, ramp/draw/removal
   tallies, prices, availability and combos come from the DB; the model only
   interprets and prioritises.
2. **Constrain names.** Schema enums in analysis mode; post-hoc name
   validation in chat mode. Match by `oracle_id` (as `card_lookup` does) so
   MDFCs and multiple printings resolve correctly. Enums only work on a small
   pool: `context.py` pre-filters candidates in code (colour identity,
   legality, owned or not) and caps the pool size. If the pool is still too
   large for the schema or prompt, fall back to post-hoc name validation.
3. **Evidence quotes verified by substring match** against stored oracle text.
   This proves a quote is real, not that the conclusion is right.
4. **Mechanical claims cross-checked against computed tags.** If the model
   calls a card ramp, removal or card draw, the code compares that with the
   tags it already computes and flags disagreements.
5. **One retry with the specific violation**, then show the result with the
   failing items marked "unverified". Unverified items are collapsed under
   the verified ones with a visible badge, never hidden and never mixed in.
6. **Read-only everywhere** in v1. No writes to the collection.
7. **Visible provenance.** UI labels computed vs model-generated content.
8. **Budgets.** Max tool iterations and a token ceiling per message.

## Phase 0 results (2026-10-06)

- **Key works.** The `AQ.` key authenticates via the `x-goog-api-key` header
  (it is rejected as `Authorization: Bearer`, so adapters must use the header).
- **`gemini-2.5-flash` is gone for this key** (404, "no longer available to
  new users"; same for 2.5-flash-lite). Use `gemini-3.5-flash` (default; thinks,
  ~400 hidden thought tokens per call) or `gemini-3.1-flash-lite` (fast, cheap).
  `gemini-3.8-flash` and `gemini-flash-latest` timed out at 60 s. Retest later.
  Make the model name a config value, not a constant.
- **Structured output works** with `responseMimeType` + `responseSchema`,
  including a string `enum` on a field; both models returned valid JSON that
  obeyed the enum.
- **Ollama is not installed**; the RX 6800 XT is visible to Windows
  (driver 32.0.21045.5002) but ROCm/Ollama GPU support is still unverified.
  **Ollama is skipped for now** (user decision); revisit Phase 2 only if a
  local model becomes desirable, starting with an `ollama ps` GPU check.
- **Key rotation still pending** (user action in Google AI Studio, then update
  `secrets.toml`).

## Phases

**Phase 0 — Smoke test (~15 min).** One Gemini call with the stored key.
Confirms the key works (its `AQ.` prefix is not the usual `AIza`, so check
this first) and how Gemini handles schema output. The key was pasted into a
chat once, so rotate it in Google AI Studio and update `secrets.toml` before
or during this phase. Also check that Ollama can see the RX 6800 XT on
Windows (ROCm support is hit-or-miss); the result decides how realistic
Phase 2 is.

**Phase 1 — Grounding core + analysis mode on Gemini.** `context.py`,
`guard.py`, `schemas.py`, the deck-doctor skill, a minimal page, and tests
with a fake provider. Includes a starter eval set of 3-5 fixed questions
(fabricated names, misquoted text, wrong mechanical tags) so the guard layer
can be judged and providers compared from the start.

**Phase 2 — Provider layer + Ollama.** Extract the interface, add the Ollama
adapter and the toggle. Install Ollama with ROCm support and pull the model
(skip or fall back to Vulkan/CPU only if Phase 0 shows the GPU is unusable;
a 14B model on CPU will be slow). Compare Gemini vs local on the same deck
using the starter eval set.

**Phase 3 — Chat mode. Done (2026-10-06).** `tools.py` (get_card, search_cards,
list_decks, get_deck — read-only, dispatch never raises), `chat.py` (tool-call
loop, iteration + token budget, then one schema-only restate call so
`guard.check_chat_answer` can check every mention against the whole
database — chat has no small enough pool to enum names against up front, so
the check happens after the fact). Gemini's adapter now implements function
calling (`capabilities.tools = True`); the page gates the Chat tab off for
any provider that doesn't declare it. Tool-call reliability hasn't been
evaluated against a second provider yet (Gemini is still the only live
one) — revisit the gate once Ollama or Anthropic lands.

Two issues surfaced by a live call against the real API, not caught by the
offline `FakeProvider` tests, both fixed before calling the phase done:
- `gemini-3.5-flash` is a thinking model and 400s on the turn after a tool
  call if the prior `functionCall` part is replayed without the
  `thoughtSignature` Gemini attached to it. `providers.py` now round-trips
  `id`/`thoughtSignature` on each tool call unmodified through chat.py's
  loop (opaque to everything except the Gemini adapter).
- The restate step (ask the model to list every card it mentioned, so
  `guard.check_chat_answer` can check it) is not reliable: one live answer
  quoted Sol Ring's oracle text directly but came back with an empty
  `mentions` list. `chat.py` now also folds in every card actually resolved
  via a successful `get_card` call this turn, regardless of what the
  restate step reports — that lookup is already ground truth from the
  database, so it needs no model cooperation to trust.

**Phase 4 — More skills + eval set. Done (2026-10-06).** Three new skills —
`upgrade_finder.md` (assume the deck's plan is fine, find single-for-single
power upgrades only), `build_from_collection.md` (maximise using only owned
cards, weighted toward thin computed roles), `cut_advisor.md` (cuts-first,
reads `games_played`/`win_rate` already in COMPUTED FACTS, falls back to
curve/roles when there are fewer than 5 recorded games) — registered in
`analysis.SKILLS`. No code changes needed beyond that: every skill is just a
system prompt sharing `run_analysis`, `analysis_schema` and
`check_analysis`, and the page's skill picker already lists whatever is in
`analysis.SKILLS`. Grew the guard-level eval set from 5 to 11 cases (fabricated
name / misquoted text / wrong role on both cuts and adds, bad `replaces`,
commander-cut, multi-violation-in-one-answer, and a check that every skill
file still states the core guardrail rules). This eval set checks the guard,
not model quality — scoring live providers against the same questions is
still a manual comparison, deferred to Phase 5 when there is a second
provider to compare against.

**Phase 5 — Claude adapter. Done (2026-10-08).** `AnthropicProvider` in
`providers.py`: Messages API, `x-api-key`/`anthropic-version` headers,
backoff on 429/5xx like Gemini. Claude has no native structured-output
parameter, so a `schema` request is sent as a single forced tool call named
"respond" whose `input_schema` is the schema translated out of Gemini's
dialect (`_gemini_schema_to_json_schema`); its `input` becomes
`ProviderResponse.parsed`. Regular tools (`TOOL_SPECS`) translate the same
way and go through Claude's normal `tool_use`/`tool_result` protocol
(`_to_anthropic_messages`), with the `id` Claude assigns each `tool_use`
block round-tripped via `call_id` exactly like Gemini's `thoughtSignature`.
Prompt caching (`cache_control: {"type": "ephemeral"}`) marks the system
prompt and the last tool definition, since both repeat unchanged across a
session. The page sidebar gained a model toggle (`providers.ANTHROPIC_MODELS`:
Claude Sonnet 5.5 / Opus 5.5) alongside Gemini. Cost-vs-Gemini comparison is
manual — the sidebar's existing token counter is enough to eyeball it; no
pricing table is hardcoded since prices and model IDs change. Key goes in
`.streamlit/secrets.toml` under `[advisor] anthropic_api_key` (or
`ANTHROPIC_API_KEY`), same pattern as Gemini's key.

**Later / optional.** Suggested changes written to `deck_changes` as `idea`
entries with explicit confirmation. A notes table for user preferences.

## Risks

| Risk | Mitigation |
|---|---|
| Local 14B model is unreliable at tool calls | Analysis mode needs no tools; chat mode gated per provider |
| Gemini free-tier limits or key issues | Phase 0 test, retry with backoff, Ollama fallback |
| Providers differ in structured-output / tool support | Capability flags per adapter; JSON-prompt + retry fallback |
| Name matching misses MDFC / split cards | Reuse existing `oracle_id` matching |
| Model quotes correctly but concludes wrongly | Expected; UI labels it as opinion, mechanical claims are cross-checked against computed tags, eval set compares providers |
| Candidate pool too big for enum / prompt | Pre-filter and cap in `context.py`; fall back to post-hoc name validation |
| Ollama can't use the GPU on Windows | Phase 0 check; fall back to Gemini-only or a smaller model |
| Free-tier Gemini may use prompts for training | Fine for deck lists; don't send personal notes or preferences to it |
| Key exposure | Key lives only in gitignored `.streamlit/secrets.toml`; the current key was pasted into a chat once, so rotate it in Google AI Studio in Phase 0 |

## Housekeeping done

- `.streamlit/secrets.toml` added to `.gitignore`; key saved under
  `[advisor] google_api_key`.
- Scratch sample from Gemini removed from the repo root.

## Conventions

Follow the project's documentation standards: header comment per module,
inline comments for non-obvious logic, and a README.md for the new
`dashboard_lib/advisor/` sub-area. Add offline tests under `tests/` using a
fake provider, matching the existing pytest layout.
