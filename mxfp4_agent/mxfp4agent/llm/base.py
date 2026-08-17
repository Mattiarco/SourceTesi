"""Abstract LLM provider layer.

Every backend (Ollama, Anthropic, mock) implements :class:`LLMProvider`, so the
agents never care which engine is behind them.
"""
from __future__ import annotations

import abc
import dataclasses
import random
import time
from typing import Any, Callable, Iterable


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
    stop_reason: str = ""

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class LLMError(RuntimeError):
    """Raised when a backend fails in a way the workflow should surface."""


class LLMProvider(abc.ABC):
    """Minimal chat interface shared by all backends."""

    name: str = "abstract"

    def __init__(self, model: str, temperature: float = 0.2, max_tokens: int = 8192,
                 timeout: int = 600, max_retries: int = 4, retry_backoff: float = 3.0,
                 **kwargs: Any) -> None:
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_backoff = retry_backoff
        #: hook opzionali (msg: str) -> None, usati dal workflow per il logging
        self.on_retry: Callable[[str], None] | None = None
        self.on_notice: Callable[[str], None] | None = None
        self.extra = kwargs
        self.stats = {"calls": 0, "input_tokens": 0, "output_tokens": 0,
                      "seconds": 0.0, "retries": 0}

    # ---------------------------------------------------------------- public
    def chat(self, system: str, messages: Iterable[Message] | Iterable[dict[str, str]],
             **overrides: Any) -> LLMResponse:
        norm: list[Message] = []
        for m in messages:
            norm.append(m if isinstance(m, Message) else Message(m["role"], m["content"]))
        start = time.time()

        # Un sovraccarico temporaneo del servizio non deve buttare via minuti di
        # lavoro degli agenti precedenti: si riprova con backoff esponenziale.
        for attempt in range(self.max_retries + 1):
            try:
                resp = self._chat(system, norm, **overrides)
                break
            except LLMError as e:
                if attempt >= self.max_retries or not self.is_transient(e):
                    raise
                delay = self.retry_backoff * (2 ** attempt) + random.uniform(0, 1.5)
                self.stats["retries"] += 1
                msg = (f"errore temporaneo ({str(e)[:90]}…) — "
                       f"riprovo fra {delay:.0f}s [{attempt + 1}/{self.max_retries}]")
                if self.on_retry:
                    self.on_retry(msg)
                time.sleep(delay)

        resp.latency_s = time.time() - start
        self.stats["calls"] += 1
        self.stats["input_tokens"] += resp.input_tokens
        self.stats["output_tokens"] += resp.output_tokens
        self.stats["seconds"] += resp.latency_s
        return resp

    def complete(self, system: str, prompt: str, **overrides: Any) -> str:
        return self.chat(system, [Message("user", prompt)], **overrides).text

    def is_transient(self, err: Exception) -> bool:
        """True se l'errore è passeggero e ha senso riprovare."""
        return False

    # -------------------------------------------------------------- abstract
    @abc.abstractmethod
    def _chat(self, system: str, messages: list[Message], **overrides: Any) -> LLMResponse:
        ...

    @abc.abstractmethod
    def health_check(self, live: bool = True) -> tuple[bool, str]:
        """Return (ok, human readable detail).

        ``live=False`` chiede un controllo economico (nessuna chiamata a pagamento).
        """

    def describe(self) -> str:
        return f"{self.name}:{self.model}"
