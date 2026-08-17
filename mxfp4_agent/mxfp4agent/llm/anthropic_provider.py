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


#: codici HTTP che vale la pena riprovare (sovraccarico, rate limit, guasti transitori)
TRANSIENT_CODES = ("429", "500", "502", "503", "504", "529")


class AnthropicProvider(LLMProvider):
    name = "claude"

    #: memoria CONDIVISA fra istanze: ogni agente ha il proprio provider, ma la
    #: scoperta "questo modello rifiuta temperature" va fatta una volta sola,
    #: altrimenti si paga un 400 inutile per ciascuno dei quattro agenti.
    _temperature_blocked: set[str] = set()

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
        body: dict[str, Any] = {
            "model": model,
            "max_tokens": overrides.get("max_tokens", self.max_tokens),
            "system": system,
            "messages": [m.as_dict() for m in messages],
        }
        temp = overrides.get("temperature", self.temperature)
        if self.send_temperature and temp is not None:
            body["temperature"] = temp

        try:
            return self._call(body)
        except LLMError as e:
            low = str(e).lower()
            if "credit balance" in low or "billing" in low:
                raise LLMError(
                    "Credito API Anthropic esaurito.\n"
                    "  → Ricarica su console.anthropic.com → Plans & Billing.\n"
                    "  → Attenzione: un abbonamento Claude Pro/Max NON include "
                    "credito API, sono due cose separate.\n"
                    "  → Alternativa gratuita: rilancia con `--provider ollama`."
                ) from e
            # "`temperature` is deprecated for this model" -> riprova senza,
            # e ricordalo per tutti gli agenti, non solo per questa istanza.
            if "temperature" in str(e).lower() and "temperature" in body:
                AnthropicProvider._temperature_blocked.add(model)
                body.pop("temperature")
                return self._call(body)
            raise

    @property
    def send_temperature(self) -> bool:
        return self.model not in AnthropicProvider._temperature_blocked

    # ------------------------------------------------------------ transienti
    def is_transient(self, err: Exception) -> bool:
        msg = str(err)
        if "Anthropic HTTP" not in msg and "SDK error" not in msg:
            return False
        low = msg.lower()
        if "overloaded" in low or "rate" in low or "timeout" in low:
            return True
        return any(f"HTTP {c}" in msg for c in TRANSIENT_CODES)

    # --------------------------------------------------------- diagnostica
    def _empty_response_error(self, blocks: list, stop_reason: str, budget: int) -> LLMError:
        """Messaggio utile quando la risposta non contiene testo.

        Il caso tipico: modelli con *extended thinking* che consumano l'intero
        budget `max_tokens` ragionando, senza mai arrivare al blocco di testo.
        """
        kinds = {b.get("type") if isinstance(b, dict) else getattr(b, "type", "?")
                 for b in blocks}
        if stop_reason == "max_tokens" or kinds == {"thinking"}:
            return LLMError(
                f"Il modello '{self.model}' ha esaurito il budget di {budget} token "
                f"prima di produrre testo (stop_reason={stop_reason or 'n/d'}, "
                f"blocchi={sorted(kinds) or 'nessuno'}).\n"
                f"È un modello con extended thinking: il ragionamento consuma "
                f"max_tokens.\nSoluzione: rilancia con `--max-tokens 32000` "
                f"(o superiore).")
        return LLMError(f"Risposta senza testo da Claude "
                        f"(stop_reason={stop_reason or 'n/d'}, blocchi={sorted(kinds)}).")

    # ------------------------------------------------------------- trasporto
    def _call(self, body: dict[str, Any]) -> LLMResponse:
        model = body["model"]
        budget = body.get("max_tokens", 0)
        if self._sdk is not None:
            try:
                r = self._sdk.messages.create(**body)
            except Exception as e:  # pragma: no cover - network
                raise LLMError(f"Anthropic SDK error: {e}") from e
            text = "".join(b.text for b in r.content if getattr(b, "type", "") == "text")
            stop = getattr(r, "stop_reason", "") or ""
            if not text.strip():
                raise self._empty_response_error(list(r.content), stop, budget)
            return LLMResponse(text, model, self.name, r.usage.input_tokens,
                               r.usage.output_tokens, raw=r, stop_reason=stop)

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

        blocks = data.get("content", [])
        text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        usage = data.get("usage", {})
        stop = data.get("stop_reason", "") or ""
        if not text.strip():
            raise self._empty_response_error(blocks, stop, budget)
        return LLMResponse(text, model, self.name, usage.get("input_tokens", 0),
                           usage.get("output_tokens", 0), raw=data, stop_reason=stop)

    # ---------------------------------------------------------------- health
    def health_check(self, live: bool = True) -> tuple[bool, str]:
        if not self.api_key:
            return False, "ANTHROPIC_API_KEY mancante"
        sdk = "SDK" if self._sdk else "urllib"
        if not live:
            return True, f"chiave presente (modello '{self.model}', {sdk})"
        # chiamata minima: verifica davvero chiave, modello e connettività
        try:
            self.chat("Rispondi con una sola parola.", [Message("user", "ping")],
                      max_tokens=4, temperature=0)
        except LLMError as e:
            msg = str(e)
            if "401" in msg or "authentication" in msg.lower():
                return False, "chiave rifiutata (401): rigenerala su console.anthropic.com"
            if "404" in msg or "not_found" in msg.lower():
                return False, f"modello '{self.model}' inesistente per questa chiave"
            if "credito api" in msg.lower() or "credit balance" in msg.lower():
                return False, "credito API esaurito (ricarica su console.anthropic.com)"
            if "429" in msg:
                return False, "rate limit (429): riprova fra poco"
            return False, f"chiamata fallita: {msg[:160]}"
        return True, f"Claude ok e raggiungibile (modello '{self.model}', {sdk})"
