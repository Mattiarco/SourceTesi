"""Agente CODER — implementa il piano in Chisel (+ testbench C++ se non è da template)."""
from __future__ import annotations

from ..knowledge.references import Reference
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


def template_testbench(plan: dict) -> bool:
    """True se il testbench è generato dal contratto (l'LLM scrive solo il modulo)."""
    return plan.get("testbench") == "template"


TEMPLATE_TB_NOTE = """### TESTBENCH
Il testbench Verilator e' GENERATO AUTOMATICAMENTE dal contratto delle porte e
confronta le uscite con il golden model. NON scriverlo e non modificarlo:
il tuo unico compito e' il modulo Chisel, con le porte ESATTAMENTE come nel piano
(stessi nomi, direzioni, tipi e larghezze, dentro `val io = IO(new Bundle {...})`)."""


class CoderAgent(Agent):
    name = "coder"
    system_prompt = SYSTEM

    def run(self, plan: dict, compiled_prompt: str,
            reference: Reference | None = None) -> AgentResult:
        module = plan["module_name"]
        src_path = f"src/main/scala/mxfp4/{module}.scala"
        layout = (f"`{src_path}` — package `mxfp4`, classe `class {module}(...) extends Module`.\n"
                  f"   NON includere `object {module}Main`: l'elaborazione e' gia' predisposta\n"
                  f"   e istanzia il modulo come `new {module}()`, quindi TUTTI i parametri\n"
                  f"   del costruttore devono avere un valore di default.")
        template = template_testbench(plan)

        example = ""
        if reference is not None:
            example = (f"\n### DESIGN DI RIFERIMENTO VERIFICATO ({reference.module}, "
                       f"{reference.clocking}, kernel {reference.kernel})\n"
                       "Compila e supera i test: riusane lo stile, gli import e le idiomatiche\n"
                       "Chisel. Adatta nomi, porte e algoritmo al PIANO APPROVATO, che prevale\n"
                       "sempre sull'esempio.\n"
                       f"```scala\n{reference.scala.rstrip()}\n```\n")

        if template:
            files_section = f"""{TEMPLATE_TB_NOTE}

### FILE DA PRODURRE (esattamente uno)
1. {layout}"""
            closing = "Scrivi ora il file nel formato richiesto (fence `scala`)."
            retry = "Riemetti il file Scala COMPLETO nel formato richiesto."
        else:
            tb_path = f"sim/tb_{module}.cpp"
            files_section = f"""{HEADER_CONTRACT}

### FILE DA PRODURRE (esattamente due)
1. {layout}
2. `{tb_path}` — testbench C++ per Verilator del modulo `{module}`
   (`#include "V{module}.h"`, `#include "test_vectors.h"`).

Il testbench deve terminare stampando `TEST PASSED (<n> vectors)` oppure
`TEST FAILED (<f>/<n>)` e ritornare 0/1 di conseguenza."""
            closing = "Scrivi ora i due file nel formato richiesto (linguaggio del primo fence: `scala`)."
            retry = "Riemetti entrambi i file COMPLETI nel formato richiesto."

        prompt = f"""{compiled_prompt}

{files_section}
{example}
{closing}"""

        text = self.ask(prompt)
        files = extract_files(text)
        if not files:
            text = self.ask(f"Non ho trovato alcun blocco `### FILE: ...`. {retry}")
            files = extract_files(text)
        if not files:
            return AgentResult(False, None, text, "Il Coder non ha prodotto file estraibili.")

        files = self._retarget(files)
        if template:
            # un testbench scritto comunque dal modello verrebbe ignorato: meglio
            # scartarlo subito, così non finisce nei prompt del Fixer
            files = [f for f in files if f.ext not in (".cpp", ".cc", ".h")]
        missing = self._missing(files, need_tb=not template)
        if missing:
            return AgentResult(False, files, text, f"File mancanti: {', '.join(missing)}")
        return AgentResult(True, files, text)

    # ------------------------------------------------------------- normalize
    @staticmethod
    def _retarget(files):
        """Riporta i percorsi nella struttura attesa dal build system."""
        for f in files:
            p = f.path.replace("\\", "/").lstrip("./")
            if p.endswith(".scala") and not p.startswith("src/main/scala/"):
                p = f"src/main/scala/mxfp4/{p.rsplit('/', 1)[-1]}"
            elif p.endswith((".cpp", ".cc")) and not p.startswith("sim/"):
                p = f"sim/{p.rsplit('/', 1)[-1]}"
            f.path = p
        return files

    @staticmethod
    def _missing(files, need_tb: bool = True) -> list[str]:
        exts = {f.ext for f in files}
        missing = []
        if ".scala" not in exts:
            missing.append("sorgente .scala")
        if need_tb and ".cpp" not in exts:
            missing.append("testbench .cpp")
        return missing
