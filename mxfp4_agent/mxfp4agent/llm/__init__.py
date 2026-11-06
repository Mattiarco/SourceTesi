"""LLM provider factory."""
from __future__ import annotations

from typing import Any

from .base import LLMError, LLMProvider, LLMResponse, Message

DEFAULT_MODELS = {
    "ollama": "qwen2.5-coder:14b",
    "mock": "mock-1",
}

#: budget di output per provider.
#: Nota per Ollama: max_tokens è sottratto da num_ctx per calcolare lo spazio
#: disponibile al prompt. Un budget di output generoso restringe la finestra e
#: fa troncare il prompt (perdendo la specifica MXFP4). 4096 basta ampiamente
#: per due file HDL.
DEFAULT_MAX_TOKENS = {
    "ollama": 4096,
    "mock": 4096,
}


def build_provider(kind: str, model: str | None = None, **kw: Any) -> LLMProvider:
    kind = (kind or "ollama").lower()
    model = model or DEFAULT_MODELS.get(kind)
    if kw.get("max_tokens") is None:
        kw["max_tokens"] = DEFAULT_MAX_TOKENS.get(kind, 8192)
    if kind == "ollama":
        from .ollama_provider import OllamaProvider

        return OllamaProvider(model=model, **kw)
    if kind == "mock":
        from .mock_provider import MockProvider

        return MockProvider(model=model, **kw)
    raise ValueError(f"Provider sconosciuto: {kind!r} (usa ollama | mock)")


__all__ = ["build_provider", "LLMProvider", "LLMResponse", "Message", "LLMError",
           "DEFAULT_MODELS", "DEFAULT_MAX_TOKENS"]
