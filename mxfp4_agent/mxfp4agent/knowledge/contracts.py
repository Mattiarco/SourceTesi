"""Contratto canonico delle porte per ogni kernel supportato dal golden model.

Perché fissarlo: con nomi e larghezze noti a priori il testbench Verilator si
genera in modo deterministico (``toolchain/tbgen.py``) e l'LLM scrive SOLO il
modulo Chisel. È la parte del lavoro dove i modelli piccoli sbagliano di più
(prefisso `io_`, segnali larghi come array `WData`, segno delle uscite,
protocollo di `valid`), e non ha niente a che fare con l'aritmetica MXFP4.

Il Planner sceglie kernel, clocking e latenza; le porte le decide il contratto.
Per i kernel ``custom`` (fuori dal golden model) il contratto non esiste e il
testbench resta scritto dall'LLM.

Convenzioni (identiche ai design di riferimento in examples/expected_output):

* blocco di K elementi E2M1 impacchettato: elemento i nei bit [4i+3 : 4i];
* uscite vettoriali impacchettate: elemento i nei bit [W*i+W-1 : W*i];
* design sequenziali: in più `validIn`/`validOut`, latenza fissa
  ``latency_cycles``, un blocco per ciclo (throughput 1), reset sincrono.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PortSpec:
    name: str
    dir: str            # "in" | "out"
    type: str           # "UInt" | "SInt" | "Bool"
    width: str          # espressione in k, es. "4*k", "8", "1"
    description: str

    def width_for(self, k: int) -> int:
        """`"8"` → 8, `"4*k"` → 4k (le uniche due forme usate)."""
        if self.width.endswith("*k"):
            return int(self.width[:-2]) * k
        return int(self.width)

    def as_plan_port(self, k: int) -> dict:
        return {"name": self.name, "dir": self.dir, "type": self.type,
                "width": self.width_for(k), "description": self.description}


_A = PortSpec("a", "in", "UInt", "4*k", "blocco A: elemento E2M1 i nei bit [4i+3:4i]")
_B = PortSpec("b", "in", "UInt", "4*k", "blocco B, stesso packing di a")
_SA = PortSpec("scaleA", "in", "UInt", "8", "scala E8M0 di A: 2^(scaleA-127), 0xFF = NaN")
_SB = PortSpec("scaleB", "in", "UInt", "8", "scala E8M0 di B")
_NAN = PortSpec("isNaN", "out", "Bool", "1", "alto se una delle scale in ingresso vale 0xFF")

CONTRACTS: dict[str, tuple[PortSpec, ...]] = {
    "dot_product": (
        _A, _B, _SA, _SB,
        PortSpec("accQ2", "out", "SInt", "32",
                 "somma esatta dei prodotti in unita' di 1/4 (intero con segno)"),
        PortSpec("expOut", "out", "SInt", "16",
                 "(scaleA-127)+(scaleB-127); risultato reale = accQ2/4 * 2^expOut"),
        _NAN,
    ),
    "elementwise_mul": (
        _A, _B, _SA, _SB,
        PortSpec("out", "out", "UInt", "32*k",
                 "k risultati IEEE-754 binary32 di a_i*b_i (scale incluse); elemento i nei "
                 "bit [32i+31:32i]; scala NaN -> 0x7FC00000"),
        _NAN,
    ),
    "elementwise_add": (
        _A, _B, _SA, _SB,
        PortSpec("out", "out", "UInt", "32*k",
                 "k risultati IEEE-754 binary32 di a_i+b_i (scale incluse, RNE); elemento i "
                 "nei bit [32i+31:32i]; scala NaN -> 0x7FC00000"),
        _NAN,
    ),
    "to_bf16": (
        _A, _SA,
        PortSpec("out", "out", "UInt", "16*k",
                 "k valori bfloat16 di a_i * 2^(scaleA-127); elemento i nei bit [16i+15:16i]; "
                 "overflow -> +/-inf (0x7F80/0xFF80); scala 0xFF -> NaN canonico 0x7FC0; "
                 "-0 resta -0 (0x8000)"),
        PortSpec("isNaN", "out", "Bool", "1", "alto se scaleA vale 0xFF"),
    ),
}

SEQUENTIAL_PORTS = (
    PortSpec("validIn", "in", "Bool", "1", "il blocco in ingresso in questo ciclo e' valido"),
    PortSpec("validOut", "out", "Bool", "1",
             "validIn ritardato di latency_cycles: le uscite di questo ciclo sono valide"),
)

#: larghezza in bit di un elemento del vettore `out`, per kernel vettoriali
ELEMENT_BITS = {"elementwise_mul": 32, "elementwise_add": 32, "to_bf16": 16}

KERNELS = tuple(CONTRACTS)


def has_contract(kernel: str) -> bool:
    return kernel in CONTRACTS


def contract_ports(kernel: str, k: int = 32, clocking: str = "combinational") -> list[dict]:
    """Porte canoniche nel formato di `plan["ports"]` (vuoto se il kernel non ha contratto)."""
    if kernel not in CONTRACTS:
        return []
    specs = list(CONTRACTS[kernel])
    if clocking == "sequential":
        ins = [p for p in specs if p.dir == "in"]
        outs = [p for p in specs if p.dir == "out"]
        specs = [SEQUENTIAL_PORTS[0], *ins, SEQUENTIAL_PORTS[1], *outs]
    return [p.as_plan_port(k) for p in specs]


def contract_text(k: int = 32) -> str:
    """Contratto in forma leggibile, per il prompt del Planner."""
    lines = ["### KERNEL SUPPORTATI E CONTRATTO DELLE PORTE (vincolante)",
             "Il testbench e' generato automaticamente da questo contratto: per i kernel",
             "elencati le porte del modulo sono ESATTAMENTE queste (nome, direzione, tipo,",
             "larghezza). Tu scegli kernel, clocking, latenza e micro-architettura.", ""]
    for kernel, specs in CONTRACTS.items():
        lines.append(f"- `{kernel}`:")
        for p in specs:
            lines.append(f"    {p.dir:<3} {p.name:<8} {p.type}({p.width_for(k)})  {p.description}")
    lines += ["", "Se `clocking` = \"sequential\" si aggiungono `in validIn: Bool` e "
              "`out validOut: Bool`",
              "(validOut = validIn ritardato di latency_cycles, un blocco per ciclo, reset "
              "sincrono).",
              "Usa `custom` solo se nessun kernel corrisponde: in quel caso definisci tu le "
              "porte."]
    return "\n".join(lines)
