"""Offline provider used for smoke tests and CI (no network, no GPU).

It returns a hard-coded but *syntactically valid* MXFP4 dot-product design so
the whole pipeline (Chisel elaboration + Verilator) can be exercised without an
LLM.  Enable with ``--provider mock``.
"""
from __future__ import annotations

from typing import Any

from .base import LLMProvider, LLMResponse, Message
from ..knowledge.reference_design import MOCK_CODER_ANSWER, MOCK_PLAN, MOCK_REVIEW


class MockProvider(LLMProvider):
    name = "mock"

    def __init__(self, model: str = "mock-1", **kw: Any) -> None:
        super().__init__(model, **kw)

    def _chat(self, system: str, messages: list[Message], **overrides: Any) -> LLMResponse:
        # il routing guarda SOLO il system prompt: il prompt utente cita tutti i
        # ruoli e produrrebbe falsi positivi.
        role = system.lower()
        if "sei il planner" in role:
            text = MOCK_PLAN
        elif "sei il reviewer" in role:
            text = MOCK_REVIEW
        elif "sei il tester" in role:
            text = "CAUSA: n/d\nFILE: n/d\nAZIONE: n/d\nEVIDENZA: (mock provider)"
        else:
            text = MOCK_CODER_ANSWER
        n_in = sum(len(m.content) for m in messages) // 4
        return LLMResponse(text, self.model, self.name, n_in, len(text) // 4)

    def health_check(self) -> tuple[bool, str]:
        return True, "Mock provider (nessuna rete, output deterministico)"
