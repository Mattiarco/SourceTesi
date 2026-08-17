"""Configurazione della pipeline."""
from __future__ import annotations

import dataclasses
import json
from pathlib import Path


@dataclasses.dataclass
class Config:
    # --- richiesta
    request: str = ""
    target: str | None = None          # None = decide il Planner; "chisel" | "systemverilog"
    block_size: int = 32

    # --- LLM
    provider: str = "ollama"
    model: str | None = None
    planner_model: str | None = None   # override per singolo agente
    coder_model: str | None = None
    reviewer_model: str | None = None
    tester_model: str | None = None
    host: str = "http://localhost:11434"
    num_ctx: int = 32768             # finestra di contesto Ollama
    api_key: str | None = None
    temperature: float = 0.2
    max_tokens: int | None = None    # None = default del provider (vedi DEFAULT_MAX_TOKENS)
    timeout: int = 900

    # --- workflow
    outdir: Path = Path("out")
    max_fix_rounds: int = 4
    static_review: bool = True
    few_shot: bool = False
    num_random_vectors: int = 64
    seed: int = 1234
    keep_going: bool = False           # continua anche se la toolchain manca
    verbose: bool = True

    def agent_model(self, agent: str) -> str | None:
        return getattr(self, f"{agent}_model", None) or self.model

    def to_dict(self) -> dict:
        d = dataclasses.asdict(self)
        d["outdir"] = str(self.outdir)
        d.pop("api_key", None)
        return d

    @classmethod
    def from_file(cls, path: Path) -> "Config":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        data["outdir"] = Path(data.get("outdir", "out"))
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})
