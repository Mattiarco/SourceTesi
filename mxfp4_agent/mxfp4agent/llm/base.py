"""Abstract LLM provider layer.

Every backend (Ollama, Anthropic, mock) implements :class:`LLMProvider`, so the
agents never care which engine is behind them.
"""
from __future__ import annotations

import abc
import dataclasses
import time
from typing import Any, Iterable


@dataclasses.dataclass
class Message:
    role: str  # "user" | "assistant"
    content: str

    def as_dict(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


@dataclasses.dataclass
class LLMResponse:
    text: str
    model: str
    provider: str
    input_tokens: int = 0
    output_tokens: int = 0
    latency_s: float = 0.0
    raw: Any = None

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class LLMError(RuntimeError):
    """Raised when a backend fails in a way the workflow should surface."""


class LLMProvider(abc.ABC):
    """Minimal chat interface shared by all backends."""

    name: str = "abstract"

    def __init__(self, model: str, temperature: float = 0.2, max_tokens: int = 8192,
                 timeout: int = 600, **kwargs: Any) -> None:
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.extra = kwargs
        self.stats = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "seconds": 0.0}

    # ---------------------------------------------------------------- public
    def chat(self, system: str, messages: Iterable[Message] | Iterable[dict[str, str]],
             **overrides: Any) -> LLMResponse:
        norm: list[Message] = []
        for m in messages:
            norm.append(m if isinstance(m, Message) else Message(m["role"], m["content"]))
        start = time.time()
        resp = self._chat(system, norm, **overrides)
        resp.latency_s = time.time() - start
        self.stats["calls"] += 1
        self.stats["input_tokens"] += resp.input_tokens
        self.stats["output_tokens"] += resp.output_tokens
        self.stats["seconds"] += resp.latency_s
        return resp

    def complete(self, system: str, prompt: str, **overrides: Any) -> str:
        return self.chat(system, [Message("user", prompt)], **overrides).text

    # -------------------------------------------------------------- abstract
    @abc.abstractmethod
    def _chat(self, system: str, messages: list[Message], **overrides: Any) -> LLMResponse:
        ...

    @abc.abstractmethod
    def health_check(self) -> tuple[bool, str]:
        """Return (ok, human readable detail)."""

    def describe(self) -> str:
        return f"{self.name}:{self.model}"
