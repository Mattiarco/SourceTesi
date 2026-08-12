"""Ollama backend (local models, no API key)."""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from .base import LLMError, LLMProvider, LLMResponse, Message


class OllamaProvider(LLMProvider):
    name = "ollama"

    def __init__(self, model: str = "qwen2.5-coder:14b",
                 host: str = "http://localhost:11434", num_ctx: int = 16384, **kw: Any) -> None:
        super().__init__(model, **kw)
        self.host = host.rstrip("/")
        self.num_ctx = num_ctx

    # ------------------------------------------------------------------ http
    def _post(self, path: str, payload: dict[str, Any], timeout: int | None = None) -> dict[str, Any]:
        req = urllib.request.Request(
            f"{self.host}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:  # pragma: no cover - network
            raise LLMError(f"Ollama HTTP {e.code}: {e.read().decode('utf-8', 'ignore')[:400]}") from e
        except urllib.error.URLError as e:  # pragma: no cover - network
            raise LLMError(
                f"Impossibile contattare Ollama su {self.host} ({e.reason}). "
                "Avvia il demone con `ollama serve`."
            ) from e

    # ------------------------------------------------------------------ chat
    def _chat(self, system: str, messages: list[Message], **overrides: Any) -> LLMResponse:
        payload = {
            "model": overrides.get("model", self.model),
            "messages": [{"role": "system", "content": system}] + [m.as_dict() for m in messages],
            "stream": False,
            "options": {
                "temperature": overrides.get("temperature", self.temperature),
                "num_ctx": self.num_ctx,
                "num_predict": overrides.get("max_tokens", self.max_tokens),
            },
        }
        data = self._post("/api/chat", payload)
        text = (data.get("message") or {}).get("content", "")
        if not text:
            raise LLMError(f"Risposta vuota da Ollama: {str(data)[:300]}")
        return LLMResponse(
            text=text,
            model=payload["model"],
            provider=self.name,
            input_tokens=data.get("prompt_eval_count", 0),
            output_tokens=data.get("eval_count", 0),
            raw=data,
        )

    # ---------------------------------------------------------------- health
    def health_check(self) -> tuple[bool, str]:
        try:
            with urllib.request.urlopen(f"{self.host}/api/tags", timeout=10) as r:
                tags = json.loads(r.read().decode("utf-8"))
        except Exception as e:  # pragma: no cover - network
            return False, f"Ollama non raggiungibile su {self.host}: {e}"
        names = [m["name"] for m in tags.get("models", [])]
        base = self.model.split(":")[0]
        if not any(n == self.model or n.split(":")[0] == base for n in names):
            return False, (f"Modello '{self.model}' non installato. "
                           f"Esegui `ollama pull {self.model}`. Disponibili: {', '.join(names) or 'nessuno'}")
        return True, f"Ollama ok ({len(names)} modelli, uso '{self.model}')"
