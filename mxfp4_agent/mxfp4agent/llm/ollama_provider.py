"""Ollama backend (local models, no API key)."""
from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.request
from typing import Any

from .base import LLMError, LLMProvider, LLMResponse, Message


class OllamaProvider(LLMProvider):
    name = "ollama"

    def __init__(self, model: str = "qwen2.5-coder:14b",
                 host: str = "http://localhost:11434", num_ctx: int = 32768, **kw: Any) -> None:
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
        except (http.client.HTTPException, ConnectionError, OSError) as e:  # pragma: no cover
            # Tipicamente il server è stato ucciso mentre caricava il modello:
            # pesi + cache KV non stanno in memoria.
            raise LLMError(
                f"Ollama ha chiuso la connessione senza rispondere ({type(e).__name__}).\n"
                f"  Causa quasi certa: memoria insufficiente per '{self.model}' con "
                f"num_ctx={self.num_ctx} (il processo viene terminato dall'OOM killer).\n"
                f"  → prova un modello più piccolo, oppure --num-ctx 8192\n"
                f"  → verifica con:  free -h   e   dmesg | tail -20 | grep -i oom\n"
                f"  → in WSL la RAM si alza da C:\\Users\\<tu>\\.wslconfig ([wsl2] memory=…)"
            ) from e

    # ------------------------------------------------------------------ chat
    def _chat(self, system: str, messages: list[Message], **overrides: Any) -> LLMResponse:
        # Ollama tronca SILENZIOSAMENTE i prompt più lunghi di num_ctx, e lo fa
        # dall'inizio: sparirebbe proprio la specifica MXFP4 nel system prompt.
        approx = (len(system) + sum(len(m.content) for m in messages)) // 4
        budget = self.num_ctx - overrides.get("max_tokens", self.max_tokens)
        if approx > budget and self.on_notice:
            self.on_notice(
                f"prompt ~{approx} token contro una finestra utile di ~{budget} "
                f"(num_ctx={self.num_ctx}): Ollama troncherebbe la specifica MXFP4. "
                f"Rilancia con --num-ctx {2 ** (approx + 4096).bit_length()} "
                f"oppure --no-static-review.")

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

    # ------------------------------------------------------------ transienti
    def is_transient(self, err: Exception) -> bool:
        low = str(err).lower()
        return any(s in low for s in ("timed out", "timeout", "connection reset",
                                      "chiuso la connessione", "remotedisconnected",
                                      "http 500", "http 502", "http 503"))

    # ---------------------------------------------------------------- health
    def health_check(self, live: bool = True) -> tuple[bool, str]:
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
