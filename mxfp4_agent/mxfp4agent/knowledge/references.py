"""Design di riferimento verificati, usati come esempio few-shot per il Coder.

Con modelli locali da 7-14B un esempio concreto di Chisel corretto (import,
`IO(new Bundle…)`, `+&`, LUT con `VecInit`, registri con `RegInit`) riduce
molto gli errori di compilazione. L'esempio si sceglie DOPO il Planner, in
base al kernel di test e al clocking del piano: sono dati strutturati, più
affidabili delle parole chiave della richiesta.

I riferimenti vivono in ``examples/expected_output/<NN_nome>/`` (piano +
modulo Chisel + testbench). Se la cartella non c'è (pacchetto installato
altrove) si ripiega sul design incorporato in ``reference_design.py``.

Nota per la tesi: quando la richiesta coincide con un riferimento (es.
dot-product combinatorio), il Coder vede di fatto una soluzione. `report.json`
registra quale riferimento è stato usato, e ``--no-few-shot`` permette
l'ablazione.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .reference_design import REFERENCE_CHISEL

REF_DIR = Path(__file__).resolve().parents[2] / "examples" / "expected_output"


@dataclass
class Reference:
    key: str                 # nome della cartella, es. "02_pipelined"
    module: str
    scala: str
    plan: dict = field(default_factory=dict)

    @property
    def kernel(self) -> str:
        return (self.plan.get("test_plan") or {}).get("kernel", "dot_product")

    @property
    def clocking(self) -> str:
        return self.plan.get("clocking", "combinational")


BUILTIN = Reference("builtin_dot_product", "MXFP4DotProduct", REFERENCE_CHISEL,
                    {"clocking": "combinational", "test_plan": {"kernel": "dot_product"}})


def load_references(root: Path = REF_DIR) -> list[Reference]:
    refs: list[Reference] = []
    if not root.is_dir():
        return refs
    for d in sorted(p for p in root.iterdir() if p.is_dir()):
        plan_f = d / "plan.json"
        scalas = sorted(d.glob("*.scala"))
        if not plan_f.exists() or not scalas:
            continue
        try:
            plan = json.loads(plan_f.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        refs.append(Reference(d.name, plan.get("module_name", scalas[0].stem),
                              scalas[0].read_text(encoding="utf-8"), plan))
    return refs


def _kernel_family(kernel: str) -> str:
    return "elementwise" if kernel.startswith("elementwise") else kernel


def select_reference(plan: dict, refs: list[Reference] | None = None) -> Reference:
    """Il riferimento più vicino al piano: kernel uguale > famiglia > clocking."""
    refs = load_references() if refs is None else refs
    if not refs:
        return BUILTIN
    kernel = (plan.get("test_plan") or {}).get("kernel", "dot_product")
    clocking = plan.get("clocking", "combinational")

    def score(r: Reference) -> tuple[int, int, int]:
        return (int(r.kernel == kernel),
                int(_kernel_family(r.kernel) == _kernel_family(kernel)),
                int(r.clocking == clocking))

    # max() tiene il primo a parità: l'ordine delle cartelle (01, 02, …) è il tie-break
    return max(refs, key=score)
