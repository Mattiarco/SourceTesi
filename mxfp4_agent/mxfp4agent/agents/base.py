"""Classe base degli agenti."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ..llm import LLMProvider, Message
from ..utils import Log, truncate


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
        return resp.text

    def _trim_history(self, max_turns: int = 6) -> None:
        if len(self.history) > max_turns * 2:
            self.history = self.history[-max_turns * 2:]

    def reset(self) -> None:
        self.history.clear()

    # --------------------------------------------------------------- helpers
    @staticmethod
    def _fmt_json(obj: Any, limit: int = 4000) -> str:
        return truncate(json.dumps(obj, indent=2, ensure_ascii=False), limit)
