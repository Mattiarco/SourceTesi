"""Classe base degli agenti."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ..llm import LLMError, LLMProvider, Message
from ..utils import Log, parse_json_response, truncate


@dataclass
class AgentResult:
    ok: bool
    payload: Any = None
    raw: str = ""
    notes: str = ""
    meta: dict = field(default_factory=dict)


class Agent:
    """Agente conversazionale con memoria locale e retry sul parsing."""

    name = "agent"
    system_prompt = ""

    def __init__(self, provider: LLMProvider, log: Log, temperature: float | None = None,
                 max_parse_retries: int = 2) -> None:
        self.provider = provider
        self.log = log
        self.temperature = temperature
        self.max_parse_retries = max_parse_retries
        self.history: list[Message] = []

    # ------------------------------------------------------------------ core
    def ask(self, prompt: str, keep_history: bool = True, **overrides: Any) -> str:
        msgs = list(self.history) + [Message("user", prompt)]
        if self.temperature is not None:
            overrides.setdefault("temperature", self.temperature)
        resp = self.provider.chat(self.system_prompt, msgs, **overrides)
        if keep_history:
            self.history.append(Message("user", prompt))
            self.history.append(Message("assistant", resp.text))
            self._trim_history()
        self.log.info(f"{self.name}: {resp.output_tokens} tok out in {resp.latency_s:.1f}s")
        if resp.stop_reason == "max_tokens":
            # risposta tagliata a metà: quasi certamente un file HDL incompleto
            self.log.fail(f"{self.name}: risposta TRONCATA a {resp.output_tokens} token. "
                          f"Rilancia con --max-tokens più alto (attuale: {self.provider.max_tokens}).")
        return resp.text

    def ask_json(self, prompt: str, schema: dict | None, keep_history: bool = True,
                 **overrides: Any) -> tuple[dict | None, str, str]:
        """Chiede un oggetto JSON con output strutturato.

        Ritorna ``(dati | None, testo grezzo, errore di parsing)``. Se il server
        rifiuta il campo `format` (Ollama < 0.5) ripiega sul testo libero.
        """
        try:
            text = self.ask(prompt, keep_history, format=schema, **overrides) if schema                 else self.ask(prompt, keep_history, **overrides)
        except LLMError as e:
            if not schema or "format" not in str(e).lower():
                raise
            self.log.fail(f"{self.name}: output strutturato non supportato ({str(e)[:80]}); "
                          "ripiego sul testo libero")
            text = self.ask(prompt, keep_history, **overrides)
        try:
            return parse_json_response(text), text, ""
        except ValueError as e:
            return None, text, str(e)

    def _trim_history(self, max_turns: int = 6) -> None:
        if len(self.history) > max_turns * 2:
            self.history = self.history[-max_turns * 2:]

    def reset(self) -> None:
        self.history.clear()

    # --------------------------------------------------------------- helpers
    @staticmethod
    def _fmt_json(obj: Any, limit: int = 4000) -> str:
        return truncate(json.dumps(obj, indent=2, ensure_ascii=False), limit)
