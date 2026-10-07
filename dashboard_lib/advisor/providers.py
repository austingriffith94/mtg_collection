"""
Provider layer: one interface, so the grounding code never knows which model
answered. Phase 1 ships Gemini plus a scripted FakeProvider for tests; the
Ollama and Anthropic adapters arrive in later phases behind the same
`chat(messages, system=None, schema=None) -> ProviderResponse` call.

Messages are [{"role": "user"|"assistant", "content": str}]. When a schema is
given, the adapter must return JSON text conforming to it (parsed into
`ProviderResponse.parsed`, None if it still isn't valid JSON).
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


class ProviderError(RuntimeError):
    """Provider unreachable, rejected the request, or returned no usable text."""


@dataclass
class ProviderResponse:
    text: str
    parsed: dict | None = None
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
    capabilities = Capabilities(tools=False, structured_output=True)
    URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

    def __init__(self, api_key=None, model=None, timeout=120, attempts=3):
        secrets = load_advisor_secrets() if not (api_key and model) else {}
        self.api_key = api_key or os.environ.get("GOOGLE_API_KEY") or secrets.get("google_api_key")
        self.model = model or secrets.get("gemini_model") or DEFAULT_GEMINI_MODEL
        if not self.api_key:
            raise ProviderError("No Gemini key: set [advisor] google_api_key in .streamlit/secrets.toml")
        self.timeout, self.attempts = timeout, attempts

    def chat(self, messages, system=None, schema=None):
        body = {"contents": [
            {"role": "model" if m["role"] == "assistant" else "user", "parts": [{"text": m["content"]}]}
            for m in messages]}
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        if schema:
            body["generationConfig"] = {"responseMimeType": "application/json", "responseSchema": schema}
        data = self._post(body)
        try:
            parts = data["candidates"][0]["content"]["parts"]
            text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        except (KeyError, IndexError, TypeError):
            raise ProviderError(f"Gemini returned no text: {str(data)[:300]}")
        u = data.get("usageMetadata", {})
        return ProviderResponse(
            text=text, parsed=_parse_json(text) if schema else None,
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


@dataclass
class FakeProvider:
    """Scripted provider for offline tests: returns each queued reply in order
    (dicts are serialised as JSON) and records every call it received."""
    replies: list
    name: str = "fake"
    capabilities: Capabilities = field(default_factory=Capabilities)
    calls: list = field(default_factory=list)

    def chat(self, messages, system=None, schema=None):
        self.calls.append({"messages": list(messages), "system": system, "schema": schema})
        if not self.replies:
            raise ProviderError("FakeProvider has no more scripted replies")
        reply = self.replies.pop(0)
        text = reply if isinstance(reply, str) else json.dumps(reply)
        return ProviderResponse(text=text, parsed=_parse_json(text) if schema else None,
                                input_tokens=100, output_tokens=50)
