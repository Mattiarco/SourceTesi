"""Risultato di una fase della toolchain (modulo a parte per evitare import circolari)."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class StageResult:
    stage: str          # elaborate | lint | build | simulate | synth | implement
    ok: bool
    log: str
    skipped: bool = False
    detail: dict = field(default_factory=dict)
