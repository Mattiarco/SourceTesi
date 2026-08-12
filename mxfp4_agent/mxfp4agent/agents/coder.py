"""Agente CODER — implementa il piano in Chisel/SystemVerilog + testbench C++."""
from __future__ import annotations

from ..knowledge.reference_design import REFERENCE_CHISEL
from ..toolchain.testvectors import HEADER_CONTRACT
from ..utils import extract_files
from .base import Agent, AgentResult

SYSTEM = """Sei il CODER di un sistema agentico per Meta-HDL. Ricevi una specifica gia'
approvata e la implementi. Non discuti le scelte architetturali: le esegui.

FORMATO DI RISPOSTA OBBLIGATORIO — un blocco per ogni file, esattamente cosi':

### FILE: percorso/relativo/del/file.scala
```scala
<contenuto completo del file>
```

Regole ferree:
- Emetti SEMPRE il contenuto INTEGRALE di ogni file, mai diff o frammenti.
- Nessun commento del tipo "// resto invariato".
- Niente testo fuori dai blocchi, a parte due righe di riepilogo iniziali.
- I percorsi devono rispettare la struttura richiesta nel prompt.
- Il codice deve compilare al primo colpo: larghezze esplicite, output sempre
  assegnati, import corretti.
"""


class CoderAgent(Agent):
    name = "coder"
    system_prompt = SYSTEM

    def run(self, plan: dict, compiled_prompt: str, few_shot: bool = False) -> AgentResult:
        module = plan["module_name"]
        if plan["meta_hdl"] == "chisel":
            src_path = f"src/main/scala/mxfp4/{module}.scala"
            lang = "scala"
            layout = (f"1. `{src_path}` — package `mxfp4`, classe `class {module}(...) extends Module`.\n"
                      f"   NON includere `object {module}Main`: l'elaborazione e' gia' predisposta\n"
                      f"   e istanzia il modulo come `new {module}()`, quindi TUTTI i parametri\n"
                      f"   del costruttore devono avere un valore di default.")
        else:
            src_path = f"rtl/{module}.sv"
            lang = "systemverilog"
            layout = f"1. `{src_path}` — `module {module} ( ... ); ... endmodule`."

        tb_path = f"sim/tb_{module}.cpp"
        example = ""
        if few_shot:
            example = ("\n### ESEMPIO DI STILE ATTESO (design diverso, NON copiarlo alla lettera)\n"
                       f"```scala\n{REFERENCE_CHISEL}```\n")

        prompt = f"""{compiled_prompt}

{HEADER_CONTRACT}

### FILE DA PRODURRE (esattamente due)
{layout}
2. `{tb_path}` — testbench C++ per Verilator del modulo `{module}`
   (`#include "V{module}.h"`, `#include "test_vectors.h"`).

Il testbench deve terminare stampando `TEST PASSED (<n> vectors)` oppure
`TEST FAILED (<f>/<n>)` e ritornare 0/1 di conseguenza.
{example}
Scrivi ora i due file nel formato richiesto (linguaggio del primo fence: `{lang}`)."""

        text = self.ask(prompt)
        files = extract_files(text)
        if not files:
            text = self.ask("Non ho trovato alcun blocco `### FILE: ...`. Riemetti "
                            "entrambi i file COMPLETI nel formato richiesto.")
            files = extract_files(text)
        if not files:
            return AgentResult(False, None, text, "Il Coder non ha prodotto file estraibili.")

        files = self._retarget(files, module, plan["meta_hdl"])
        missing = self._missing(files, plan["meta_hdl"])
        if missing:
            return AgentResult(False, files, text, f"File mancanti: {', '.join(missing)}")
        return AgentResult(True, files, text)

    # ------------------------------------------------------------- normalize
    @staticmethod
    def _retarget(files, module: str, hdl: str):
        """Riporta i percorsi nella struttura attesa dal build system."""
        for f in files:
            p = f.path.replace("\\", "/").lstrip("./")
            if p.endswith(".scala") and not p.startswith("src/main/scala/"):
                p = f"src/main/scala/mxfp4/{p.rsplit('/', 1)[-1]}"
            elif p.endswith((".sv", ".v")) and not p.startswith("rtl/"):
                p = f"rtl/{p.rsplit('/', 1)[-1]}"
            elif p.endswith((".cpp", ".cc")) and not p.startswith("sim/"):
                p = f"sim/{p.rsplit('/', 1)[-1]}"
            f.path = p
        return files

    @staticmethod
    def _missing(files, hdl: str) -> list[str]:
        exts = {f.ext for f in files}
        need_src = ".scala" if hdl == "chisel" else ".sv"
        missing = []
        if need_src not in exts and not (need_src == ".sv" and ".v" in exts):
            missing.append(f"sorgente {need_src}")
        if ".cpp" not in exts:
            missing.append("testbench .cpp")
        return missing
