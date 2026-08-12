"""Claude (Anthropic API) backend.

Uses the official ``anthropic`` SDK when installed, otherwise falls back to a
dependency-free urllib call against the Messages API.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

from .base import LLMError, LLMProvider, LLMResponse, Message

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"


class AnthropicProvider(LLMProvider):
    name = "claude"

    def __init__(self, model: str = "claude-sonnet-5", api_key: str | None = None, **kw: Any) -> None:
        super().__init__(model, **kw)
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self._sdk = None
        try:  # optional fast path
            import anthropic  # type: ignore

            if self.api_key:
                self._sdk = anthropic.Anthropic(api_key=self.api_key, timeout=self.timeout)
        except Exception:
            self._sdk = None

    # ------------------------------------------------------------------ chat
    def _chat(self, system: str, messages: list[Message], **overrides: Any) -> LLMResponse:
        if not self.api_key:
            raise LLMError("ANTHROPIC_API_KEY non impostata (export ANTHROPIC_API_KEY=sk-ant-...).")
        model = overrides.get("model", self.model)
        body = {
            "model": model,
            "max_tokens": overrides.get("max_tokens", self.max_tokens),
            "temperature": overrides.get("temperature", self.temperature),
            "system": system,
            "messages": [m.as_dict() for m in messages],
        }

        if self._sdk is not None:
            try:
                r = self._sdk.messages.create(**body)
            except Exception as e:  # pragma: no cover - network
                raise LLMError(f"Anthropic SDK error: {e}") from e
            text = "".join(b.text for b in r.content if getattr(b, "type", "") == "text")
            return LLMResponse(text, model, self.name, r.usage.input_tokens,
                               r.usage.output_tokens, raw=r)

        req = urllib.request.Request(
            API_URL,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "content-type": "application/json",
                "x-api-key": self.api_key,
                "anthropic-version": API_VERSION,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:  # pragma: no cover - network
            raise LLMError(f"Anthropic HTTP {e.code}: {e.read().decode('utf-8', 'ignore')[:400]}") from e
        except urllib.error.URLError as e:  # pragma: no cover - network
            raise LLMError(f"Rete non disponibile per l'API Anthropic: {e.reason}") from e

        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        usage = data.get("usage", {})
        if not text:
            raise LLMError(f"Risposta vuota da Claude: {str(data)[:300]}")
        return LLMResponse(text, model, self.name, usage.get("input_tokens", 0),
                           usage.get("output_tokens", 0), raw=data)

    # ---------------------------------------------------------------- health
    def health_check(self) -> tuple[bool, str]:
        if not self.api_key:
            return False, "ANTHROPIC_API_KEY mancante"
        return True, f"Claude ok (modello '{self.model}', SDK={'sì' if self._sdk else 'no, urllib'})"
