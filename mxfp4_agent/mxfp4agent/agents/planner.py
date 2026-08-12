"""Agente PLANNER — dalla richiesta in linguaggio naturale al piano + prompt.

Fa due cose:
1. sceglie il Meta-HDL e produce una micro-architettura in JSON;
2. **compila il prompt di implementazione** che verra' dato al Coder — è questo
   il "prompt engineering automatico" richiesto dal progetto.
"""
from __future__ import annotations

from ..knowledge import full_context
from ..knowledge.mxfp4_spec import MXFP4_SPEC
from ..utils import extract_json, sanitize_identifier
from .base import Agent, AgentResult

SYSTEM = """Sei il PLANNER di un sistema agentico per Meta-HDL, specializzato in unita'
aritmetiche in formato microscaling MXFP4 destinate a un coprocessore RISC-V.

Il tuo compito NON e' scrivere HDL. Devi produrre una specifica di progetto
completa e non ambigua, tale che un secondo agente possa implementarla senza
dover indovinare nulla: nomi, larghezze, semantica di ogni porta, latenza,
algoritmo passo-passo, casi limite.

Rispondi SEMPRE e SOLO con un singolo oggetto JSON in un blocco ```json.
Nessun testo prima o dopo.

Schema richiesto:
{
  "module_name": "IdentificatoreCamelCase",
  "meta_hdl": "chisel" | "systemverilog",
  "rationale": "perche' questo Meta-HDL e questa micro-architettura",
  "clocking": "combinational" | "sequential",
  "latency_cycles": <int, 0 se combinatorio>,
  "parameters": [{"name": "...", "value": <int>, "description": "..."}],
  "ports": [{"name": "...", "dir": "in"|"out", "width": <int>,
             "type": "UInt"|"SInt"|"Bool", "description": "semantica esatta"}],
  "algorithm": ["passo 1", "passo 2", "..."],
  "numerics": {"accumulator_width": <int>, "rounding": "...",
               "saturation": "...", "special_cases": ["..."]},
  "test_plan": {"kernel": "dot_product"|"elementwise_mul"|"elementwise_add"|"custom",
                "num_random": <int>, "directed": ["caso 1", "..."]},
  "risks": ["..."],
  "coder_prompt": "Istruzioni operative dettagliate per l'agente Coder, in italiano, che riprendono numericamente ogni larghezza e ogni caso limite."
}

Regole di pianificazione:
- Preferisci datapath INTERI: le magnitudini E2M1 sono multipli di 0.5 e le scale
  E8M0 sono potenze di due, quindi non serve aritmetica floating point.
- Dimensiona l'accumulatore con margine e giustificalo in "numerics".
- Se la richiesta e' ambigua, scegli la soluzione piu' semplice e dichiarala in
  "rationale" invece di chiedere chiarimenti.
- Il campo "test_plan.kernel" deve essere uno dei kernel supportati dal golden
  model Python; usa "custom" solo se davvero nessuno e' adatto.
"""


class PlannerAgent(Agent):
    name = "planner"
    system_prompt = SYSTEM

    def run(self, user_request: str, target_hint: str | None = None,
            block_size: int = 32) -> AgentResult:
        hint = f"\nMeta-HDL imposto dall'utente: {target_hint}." if target_hint else ""
        prompt = f"""{MXFP4_SPEC}

### RICHIESTA UTENTE
{user_request}

### VINCOLI DI PROGETTO
- Dimensione blocco K = {block_size}.
- Il modulo sara' verificato con Verilator contro un golden model Python.
- Packing: elemento i nei bit [4*i+3 : 4*i] del vettore di ingresso.
- Il testbench legge i vettori attesi da un header C generato automaticamente,
  quindi le porte devono essere poche, larghe e con semantica esplicita.{hint}

Produci ora il piano JSON."""

        last_err = ""
        for attempt in range(self.max_parse_retries + 1):
            text = self.ask(prompt if attempt == 0 else
                            f"Il tuo output non era JSON valido ({last_err}). "
                            "Rispondi SOLO con il blocco ```json richiesto.")
            try:
                plan = extract_json(text)
            except ValueError as e:
                last_err = str(e)
                self.log.fail(f"planner: JSON non valido (tentativo {attempt + 1})")
                continue
            plan = self._normalize(plan, block_size, target_hint)
            return AgentResult(True, plan, text)
        return AgentResult(False, None, "", f"Piano non parsabile: {last_err}")

    # ------------------------------------------------------------- normalize
    @staticmethod
    def _normalize(plan: dict, block_size: int, target_hint: str | None) -> dict:
        plan["module_name"] = sanitize_identifier(plan.get("module_name", ""))
        hdl = (target_hint or plan.get("meta_hdl") or "chisel").lower()
        plan["meta_hdl"] = "chisel" if hdl.startswith("chi") else "systemverilog"
        plan.setdefault("clocking", "combinational")
        plan.setdefault("latency_cycles", 0 if plan["clocking"] == "combinational" else 1)
        plan.setdefault("ports", [])
        plan.setdefault("algorithm", [])
        tp = plan.setdefault("test_plan", {})
        tp.setdefault("kernel", "dot_product")
        tp["num_random"] = int(tp.get("num_random", 64) or 64)
        tp.setdefault("directed", [])
        plan["block_size"] = block_size
        plan.setdefault("coder_prompt", "")
        return plan

    # ------------------------------------------------------ prompt compilato
    @staticmethod
    def compile_coder_prompt(plan: dict, user_request: str) -> str:
        """Prompt finale per il Coder: piano + conoscenza di dominio + contratti."""
        ports = "\n".join(
            f"  - {p.get('dir','?'):<3} {p.get('name','?'):<10} "
            f"{p.get('type','UInt')}({p.get('width','?')} bit) : {p.get('description','')}"
            for p in plan.get("ports", [])
        )
        steps = "\n".join(f"  {i + 1}. {s}" for i, s in enumerate(plan.get("algorithm", [])))
        num = plan.get("numerics", {})
        return f"""{full_context(plan['meta_hdl'])}

### RICHIESTA ORIGINALE DELL'UTENTE
{user_request}

### PIANO APPROVATO (da implementare alla lettera)
Modulo        : {plan['module_name']}
Meta-HDL      : {plan['meta_hdl']}
Clocking      : {plan['clocking']} (latenza {plan.get('latency_cycles', 0)} cicli)
Block size K  : {plan['block_size']}
Motivazione   : {plan.get('rationale', '')}

Porte:
{ports or '  (nessuna specificata: deducile dall algoritmo)'}

Algoritmo:
{steps or '  (nessuno specificato)'}

Numerica:
  accumulatore : {num.get('accumulator_width', 'da dimensionare')} bit
  arrotondamento: {num.get('rounding', 'esatto, nessun arrotondamento')}
  saturazione  : {num.get('saturation', 'satura a +/-6 in E2M1')}
  casi speciali: {', '.join(num.get('special_cases', [])) or 'zero negativo, subnormale 0.5, scala NaN'}

### ISTRUZIONI SPECIFICHE DEL PLANNER
{plan.get('coder_prompt', '(nessuna)')}
"""
