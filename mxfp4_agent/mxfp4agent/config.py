"""Configurazione della pipeline."""
from __future__ import annotations

import dataclasses
import json
from pathlib import Path

AGENTS = ("planner", "coder", "reviewer", "tester")

#: impostazioni LLM che un profilo può ridefinire per un singolo agente
PROFILE_KEYS = ("model", "num_ctx", "temperature", "max_tokens", "keep_alive", "seed")


@dataclasses.dataclass
class Config:
    # --- richiesta
    request: str = ""
    block_size: int = 32

    # --- LLM
    provider: str = "ollama"
    model: str | None = None
    planner_model: str | None = None   # override per singolo agente
    coder_model: str | None = None
    reviewer_model: str | None = None
    tester_model: str | None = None
    #: profili per agente, es. {"planner": {"model": "qwen2.5-coder:32b", "num_ctx": 16384}}.
    #: Precedenza: --<agente>-model > profilo dell'agente > valori globali.
    profiles: dict = dataclasses.field(default_factory=dict)
    host: str = "http://localhost:11434"
    num_ctx: int = 32768             # finestra di contesto Ollama
    temperature: float = 0.2
    max_tokens: int | None = None    # None = default del provider (vedi DEFAULT_MAX_TOKENS)
    timeout: int = 1800              # tetto totale per singola chiamata LLM (s)
    chunk_timeout: int = 600         # attesa massima fra due chunk dello stream (s)
    keep_alive: str | None = "30m"   # quanto Ollama tiene il modello in VRAM dopo l'uso

    # --- workflow
    outdir: Path = Path("out")
    max_fix_rounds: int = 4
    static_review: bool = True
    chisel_lint: bool = True         # lint statico deterministico prima di sbt
    sbt_mode: str = "auto"           # auto | client (server sbt riusato) | batch
    tb_mode: str = "template"        # template (da contratto) | llm
    trace: bool = False              # dump VCD della simulazione
    coverage: bool = False           # coverage Verilator nel report
    few_shot: bool = True            # esempio Chisel verificato più vicino al piano
    num_random_vectors: int = 64
    seed: int = 1234                   # seed del campionamento LLM (options.seed)
    vector_seed: int = 1234            # seed dei vettori casuali: fisso fra le run di un bench
    synth: bool = True                 # sintesi Yosys + P&R nextpnr dopo la simulazione
    target_mhz: float = 100.0          # vincolo di frequenza per il timing (nextpnr)
    pnr_seeds: int = 5                 # seed di place & route (mediana/min/max della Fmax)
    ppa_rounds: int = 0                # varianti ottimizzate per area/Fmax dopo la verifica
    verbose: bool = True

    def agent_model(self, agent: str) -> str | None:
        return self.agent_settings(agent).get("model")

    def agent_settings(self, agent: str) -> dict:
        """Impostazioni LLM effettive di un agente (globali + profilo + override CLI)."""
        out = {"model": self.model, "num_ctx": self.num_ctx, "temperature": self.temperature,
               "max_tokens": self.max_tokens, "keep_alive": self.keep_alive, "seed": self.seed}
        prof = (self.profiles or {}).get(agent) or {}
        unknown = set(prof) - set(PROFILE_KEYS)
        if unknown:
            raise ValueError(f"profilo '{agent}': chiavi non riconosciute {sorted(unknown)} "
                             f"(ammesse: {', '.join(PROFILE_KEYS)})")
        out.update({k: v for k, v in prof.items() if v is not None})
        cli_model = getattr(self, f"{agent}_model", None)
        if cli_model:
            out["model"] = cli_model
        return out

    def to_dict(self) -> dict:
        d = dataclasses.asdict(self)
        d["outdir"] = str(self.outdir)
        return d

    @classmethod
    def from_file(cls, path: Path) -> "Config":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        data["outdir"] = Path(data.get("outdir", "out"))
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


def load_profiles(path: Path) -> dict:
    """Legge un file di profili per agente e lo valida.

    Formato::

        {"planner": {"model": "qwen2.5-coder:32b", "num_ctx": 16384},
         "coder":   {"model": "qwen2.5-coder:14b", "temperature": 0.1}}

    È accettata anche la forma ``{"profiles": {...}}``.
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    data = data.get("profiles", data)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: atteso un oggetto JSON agente -> impostazioni")
    bad = set(data) - set(AGENTS)
    if bad:
        raise ValueError(f"{path}: agenti sconosciuti {sorted(bad)} (ammessi: {', '.join(AGENTS)})")
    for a in AGENTS:
        Config(profiles=data).agent_settings(a)   # valida le chiavi
    return data
