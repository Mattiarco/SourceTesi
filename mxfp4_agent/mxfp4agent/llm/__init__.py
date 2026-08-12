"""LLM provider factory."""
from __future__ import annotations

from typing import Any

from .base import LLMError, LLMProvider, LLMResponse, Message

DEFAULT_MODELS = {
    "ollama": "qwen2.5-coder:14b",
    "claude": "claude-sonnet-5",
    "mock": "mock-1",
}


def build_provider(kind: str, model: str | None = None, **kw: Any) -> LLMProvider:
    kind = (kind or "ollama").lower()
    model = model or DEFAULT_MODELS.get(kind)
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


__all__ = ["build_provider", "LLMProvider", "LLMResponse", "Message", "LLMError", "DEFAULT_MODELS"]
