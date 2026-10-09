"""
Provider layer: one interface, so the grounding code never knows which model
answered. Phase 1 shipped Gemini, Phase 5 adds Anthropic (Claude Sonnet /
Opus), plus a scripted FakeProvider for tests. Ollama (Phase 2) is skipped
for now (user decision; see ADVISOR_PLAN.md). All adapters share one call:
`chat(messages, system=None, schema=None, tools=None) -> ProviderResponse`.

Messages are normally [{"role": "user"|"assistant", "content": str}]. Chat
mode (Phase 3) adds two more shapes: an assistant turn with `tool_calls`
instead of `content` (the model asking to run tools — each call dict may
carry provider-opaque round-trip data such as Gemini's `id`/
`thoughtSignature`, which chat.py must pass back unmodified), and a
`{"role": "tool", "name": str, "content": dict, "call_id": str|None}` turn
carrying one tool's result back. When a
schema is given, the adapter must return JSON text conforming to it (parsed
into `ProviderResponse.parsed`, None if it still isn't valid JSON). When
`tools` is given (Gemini-dialect function declarations, see
advisor/tools.py), the model may return `ProviderResponse.tool_calls` instead
of text.
"""
import json
import os
import time
import tomllib
from dataclasses import dataclass, field

import requests

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SECRETS_PATH = os.path.join(BASE_DIR, ".streamlit", "secrets.toml")
DEFAULT_GEMINI_MODEL = "gemini-3.5-flash"
DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-5-5"
ANTHROPIC_VERSION = "2023-06-01"
ANTHROPIC_MODELS = {"Claude Sonnet 5.5": "claude-sonnet-5-5", "Claude Opus 5.5": "claude-opus-5-5"}


class ProviderError(RuntimeError):
    """Provider unreachable, rejected the request, or returned no usable text."""


@dataclass
class ProviderResponse:
    text: str
    parsed: dict | None = None
    tool_calls: list = field(default_factory=list)   # [{"name": str, "args": dict}]
    input_tokens: int = 0
    output_tokens: int = 0     # includes hidden "thinking" tokens where reported


@dataclass
class Capabilities:
    tools: bool = False
    structured_output: bool = True


def _parse_json(text):
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


def _to_gemini_contents(messages):
    """Generic messages -> Gemini's `contents`. An assistant turn carries
    either `content` (text) or `tool_calls` (functionCall parts); a `tool`
    turn carries the result of one call back as a functionResponse part,
    role "function" (Gemini's dialect, not OpenAI's "tool").

    `id` and `thoughtSignature` on a tool_call are opaque, Gemini-specific
    round-trip data (not produced by FakeProvider): a thinking model 400s on
    the next request if a prior functionCall part is replayed without its
    thoughtSignature, so chat.py's loop must carry whatever the provider
    attached to each call straight back through, unmodified."""
    contents = []
    for m in messages:
        role = m["role"]
        if role == "tool":
            response = m["content"] if isinstance(m["content"], dict) else {"result": m["content"]}
            func_response = {"name": m["name"], "response": response}
            if m.get("call_id"):
                func_response["id"] = m["call_id"]
            contents.append({"role": "function", "parts": [{"functionResponse": func_response}]})
        elif role == "assistant" and m.get("tool_calls"):
            parts = []
            for c in m["tool_calls"]:
                call = {"name": c["name"], "args": c.get("args") or {}}
                if c.get("id"):
                    call["id"] = c["id"]
                part = {"functionCall": call}
                if c.get("thoughtSignature"):
                    part["thoughtSignature"] = c["thoughtSignature"]
                parts.append(part)
            contents.append({"role": "model", "parts": parts})
        else:
            contents.append({"role": "model" if role == "assistant" else "user",
                              "parts": [{"text": m.get("content", "")}]})
    return contents


def load_advisor_secrets(path=SECRETS_PATH):
    """The [advisor] table of .streamlit/secrets.toml ({} if absent). Read
    directly (not via st.secrets) so tests and scripts work without Streamlit."""
    try:
        with open(path, "rb") as f:
            return tomllib.load(f).get("advisor", {})
    except FileNotFoundError:
        return {}


class GeminiProvider:
    """Google Gemini over the REST API. The key goes in the x-goog-api-key
    header (the AQ.-style keys are rejected as Bearer tokens)."""
    name = "gemini"
    capabilities = Capabilities(tools=True, structured_output=True)
    URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

    def __init__(self, api_key=None, model=None, timeout=120, attempts=3):
        secrets = load_advisor_secrets() if not (api_key and model) else {}
        self.api_key = api_key or os.environ.get("GOOGLE_API_KEY") or secrets.get("google_api_key")
        self.model = model or secrets.get("gemini_model") or DEFAULT_GEMINI_MODEL
        if not self.api_key:
            raise ProviderError("No Gemini key: set [advisor] google_api_key in .streamlit/secrets.toml")
        self.timeout, self.attempts = timeout, attempts

    def chat(self, messages, system=None, schema=None, tools=None):
        """`tools` is a list of Gemini-dialect function declarations (see
        advisor/tools.py's TOOL_SPECS). Not combined with `schema` in one
        request here — chat.py uses tools first, then a separate
        schema-only call once the model is done calling them, so each
        request only exercises one capability."""
        body = {"contents": _to_gemini_contents(messages)}
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        if schema:
            body["generationConfig"] = {"responseMimeType": "application/json", "responseSchema": schema}
        if tools:
            body["tools"] = [{"functionDeclarations": tools}]
        data = self._post(body)
        try:
            parts = data["candidates"][0]["content"]["parts"]
        except (KeyError, IndexError, TypeError):
            raise ProviderError(f"Gemini returned no content: {str(data)[:300]}")
        text = "".join(p.get("text", "") for p in parts if not p.get("thought") and "text" in p)
        calls = []
        for p in parts:
            if "functionCall" not in p:
                continue
            call = {"name": p["functionCall"]["name"], "args": p["functionCall"].get("args") or {}}
            if "id" in p["functionCall"]:
                call["id"] = p["functionCall"]["id"]
            if "thoughtSignature" in p:        # thinking models require this echoed back verbatim
                call["thoughtSignature"] = p["thoughtSignature"]
            calls.append(call)
        if not text and not calls:
            raise ProviderError(f"Gemini returned no text or tool call: {str(data)[:300]}")
        u = data.get("usageMetadata", {})
        return ProviderResponse(
            text=text, parsed=_parse_json(text) if (schema and text) else None, tool_calls=calls,
            input_tokens=u.get("promptTokenCount", 0),
            output_tokens=u.get("candidatesTokenCount", 0) + u.get("thoughtsTokenCount", 0))

    def _post(self, body):
        """POST with backoff on rate limits / 5xx; a bad key or model fails fast."""
        url = self.URL.format(model=self.model)
        err = ""
        for attempt in range(self.attempts):
            try:
                r = requests.post(url, json=body, headers={"x-goog-api-key": self.api_key}, timeout=self.timeout)
            except requests.RequestException as e:
                err = str(e)
            else:
                if r.ok:
                    return r.json()
                err = f"HTTP {r.status_code}: {r.text[:300]}"
                if r.status_code not in (429, 500, 502, 503, 504):
                    break
            time.sleep(2 ** attempt)
        raise ProviderError(f"Gemini ({self.model}) failed: {err}")


def _gemini_schema_to_json_schema(node):
    """Translate one node of tools.py/schemas.py's Gemini-dialect schema
    (uppercase type names: OBJECT/STRING/ARRAY) into standard JSON Schema
    (lowercase), recursively, for Claude's `input_schema`."""
    if not isinstance(node, dict):
        return node
    out = {}
    for k, v in node.items():
        if k == "type" and isinstance(v, str):
            out[k] = v.lower()
        elif k == "properties" and isinstance(v, dict):
            out[k] = {pk: _gemini_schema_to_json_schema(pv) for pk, pv in v.items()}
        elif k == "items":
            out[k] = _gemini_schema_to_json_schema(v)
        else:
            out[k] = v
    return out


def _to_anthropic_messages(messages):
    """Generic messages -> Anthropic's `messages` (system is passed
    separately, as a `system` top-level field). Anthropic has no separate
    "tool" role: a `tool` turn becomes a `tool_result` content block inside
    a user turn, keyed by `tool_use_id`. An assistant `tool_calls` turn
    becomes `tool_use` content blocks. `call_id` round-trips the `id`
    Anthropic always assigns its `tool_use` blocks, which it requires back
    verbatim on the matching `tool_result` (not produced by FakeProvider,
    which never goes through this adapter)."""
    out = []
    for m in messages:
        role = m["role"]
        if role == "tool":
            content = m["content"]
            text = content if isinstance(content, str) else json.dumps(content)
            out.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": m.get("call_id"), "content": text}]})
        elif role == "assistant" and m.get("tool_calls"):
            blocks = [{"type": "tool_use", "id": c.get("id"), "name": c["name"], "input": c.get("args") or {}}
                      for c in m["tool_calls"]]
            out.append({"role": "assistant", "content": blocks})
        else:
            out.append({"role": "assistant" if role == "assistant" else "user",
                        "content": m.get("content", "")})
    return out


class AnthropicProvider:
    """Anthropic Claude over the Messages API (ADVISOR_PLAN.md Phase 5).
    Claude has no native "respond with this JSON schema" parameter the way
    Gemini does, so a `schema` request is sent as a single tool named
    "respond" with `tool_choice` forced to it; the tool's `input` becomes
    `ProviderResponse.parsed`. Prompt caching marks the system prompt and
    the tool definitions (the parts of the request that repeat unchanged
    turn to turn) with `cache_control`."""
    name = "anthropic"
    capabilities = Capabilities(tools=True, structured_output=True)
    URL = "https://api.anthropic.com/v1/messages"

    def __init__(self, api_key=None, model=None, timeout=120, attempts=3):
        secrets = load_advisor_secrets() if not (api_key and model) else {}
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY") or secrets.get("anthropic_api_key")
        self.model = model or secrets.get("anthropic_model") or DEFAULT_ANTHROPIC_MODEL
        if not self.api_key:
            raise ProviderError("No Anthropic key: set [advisor] anthropic_api_key in .streamlit/secrets.toml")
        self.timeout, self.attempts = timeout, attempts

    def chat(self, messages, system=None, schema=None, tools=None):
        body = {"model": self.model, "max_tokens": 4096, "messages": _to_anthropic_messages(messages)}
        if system:
            # A cached system prompt is re-sent unchanged on every call in a
            # session (same skill or chat.md), so it is the cheapest, safest
            # thing to mark cacheable.
            body["system"] = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]

        claude_tools = [
            {"name": t["name"], "description": t.get("description", ""),
             "input_schema": _gemini_schema_to_json_schema(
                 t.get("parameters") or {"type": "OBJECT", "properties": {}})}
            for t in (tools or [])]
        if schema:
            claude_tools.append({"name": "respond", "description": "Submit your structured answer.",
                                  "input_schema": _gemini_schema_to_json_schema(schema)})
            body["tool_choice"] = {"type": "tool", "name": "respond"}
        if claude_tools:
            claude_tools[-1] = {**claude_tools[-1], "cache_control": {"type": "ephemeral"}}
            body["tools"] = claude_tools

        data = self._post(body)
        blocks = data.get("content") or []
        text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        parsed = None
        calls = []
        for b in blocks:
            if b.get("type") != "tool_use":
                continue
            if schema and b.get("name") == "respond":
                parsed = b.get("input")
            else:
                calls.append({"id": b.get("id"), "name": b["name"], "args": b.get("input") or {}})
        if not text and parsed is None and not calls:
            raise ProviderError(f"Claude returned no content: {str(data)[:300]}")
        u = data.get("usage", {})
        return ProviderResponse(
            text=text or (json.dumps(parsed) if parsed is not None else ""), parsed=parsed, tool_calls=calls,
            input_tokens=u.get("input_tokens", 0), output_tokens=u.get("output_tokens", 0))

    def _post(self, body):
        """POST with backoff on rate limits / 5xx; a bad key or model fails fast."""
        err = ""
        for attempt in range(self.attempts):
            try:
                r = requests.post(self.URL, json=body, timeout=self.timeout, headers={
                    "x-api-key": self.api_key, "anthropic-version": ANTHROPIC_VERSION,
                    "content-type": "application/json"})
            except requests.RequestException as e:
                err = str(e)
            else:
                if r.ok:
                    return r.json()
                err = f"HTTP {r.status_code}: {r.text[:300]}"
                if r.status_code not in (429, 500, 502, 503, 504):
                    break
            time.sleep(2 ** attempt)
        raise ProviderError(f"Claude ({self.model}) failed: {err}")


@dataclass
class FakeProvider:
    """Scripted provider for offline tests: returns each queued reply in order
    (dicts are serialised as JSON) and records every call it received."""
    replies: list
    name: str = "fake"
    capabilities: Capabilities = field(default_factory=Capabilities)
    calls: list = field(default_factory=list)

    def chat(self, messages, system=None, schema=None, tools=None):
        """A scripted reply that is already a ProviderResponse (e.g. one
        carrying `tool_calls`) is returned as-is, so tests can script a
        tool-call round without reconstructing the dataclass; any other
        reply is serialised the way Phase 1 tests already rely on."""
        self.calls.append({"messages": list(messages), "system": system, "schema": schema, "tools": tools})
        if not self.replies:
            raise ProviderError("FakeProvider has no more scripted replies")
        reply = self.replies.pop(0)
        if isinstance(reply, ProviderResponse):
            return reply
        text = reply if isinstance(reply, str) else json.dumps(reply)
        return ProviderResponse(text=text, parsed=_parse_json(text) if schema else None,
                                input_tokens=100, output_tokens=50)
