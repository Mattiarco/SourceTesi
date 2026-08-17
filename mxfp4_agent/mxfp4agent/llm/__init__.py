"""LLM provider factory."""
from __future__ import annotations

from typing import Any

from .base import LLMError, LLMProvider, LLMResponse, Message

DEFAULT_MODELS = {
    "ollama": "qwen2.5-coder:14b",
    "claude": "claude-sonnet-5",
    "mock": "mock-1",
}

#: budget di output per provider. I modelli Claude recenti usano l'extended
#: thinking, che consuma `max_tokens`: con 8k si esaurisce il budget ragionando
#: e la risposta arriva senza alcun blocco di testo.
#: Nota per Ollama: max_tokens è sottratto da num_ctx per calcolare lo spazio
#: disponibile al prompt. Un budget di output generoso restringe la finestra e
#: fa troncare il prompt (perdendo la specifica MXFP4). 4096 basta ampiamente
#: per due file HDL.
DEFAULT_MAX_TOKENS = {
    "ollama": 4096,
    "claude": 32000,
    "anthropic": 32000,
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
    if kind in ("claude", "anthropic"):
        from .anthropic_provider import AnthropicProvider

        return AnthropicProvider(model=model, **kw)
    if kind == "mock":
        from .mock_provider import MockProvider

        return MockProvider(model=model, **kw)
    raise ValueError(f"Provider sconosciuto: {kind!r} (usa ollama | claude | mock)")


__all__ = ["build_provider", "LLMProvider", "LLMResponse", "Message", "LLMError",
           "DEFAULT_MODELS", "DEFAULT_MAX_TOKENS"]
