"""Agente REVIEWER & FIXER.

Due modalità:
* ``review``  — ispezione statica prima di toccare la toolchain (a costo zero
  rispetto a un ciclo sbt+verilator, che può durare minuti);
* ``fix``     — riparazione guidata dai log reali di sbt/Verilator/simulazione.
"""
from __future__ import annotations

from ..knowledge import full_context
from ..toolchain.testvectors import HEADER_CONTRACT
from ..utils import ExtractedFile, extract_files, extract_json, truncate
from .base import Agent, AgentResult

SYSTEM = """Sei il REVIEWER & FIXER di un sistema agentico per Meta-HDL, esperto di
Chisel, SystemVerilog, Verilator e formati microscaling (MXFP4/NVFP4).

Hai due modalita', indicate dal prompt:

[REVIEW] Ispezione statica. Rispondi SOLO con un blocco ```json:
{
  "verdict": "approved" | "changes_required",
  "issues": [{"severity": "blocker"|"major"|"minor",
              "file": "...", "where": "riferimento nel codice",
              "problem": "...", "fix": "..."}],
  "notes": "sintesi in una riga"
}
Sii severo su: larghezze, output non assegnati, latch, bias E2M1/E8M0 sbagliati,
subnormale 0.5 ignorato, overflow dell'accumulatore, ordine dei nibble, API
Chisel deprecate, accesso errato ai segnali larghi in Verilator.
Non segnalare questioni di stile.

[FIX] Riparazione. Ricevi i log di errore reali. Rispondi con:
1. una riga `CAUSA: <diagnosi in una frase>`;
2. i file corretti, COMPLETI, nel formato:

### FILE: percorso/del/file.ext
```linguaggio
<contenuto integrale>
```

Riemetti solo i file che cambiano, ma per intero. Correggi la causa, non il
sintomo: se un vettore fallisce, e' quasi sempre la semantica MXFP4 ad essere
sbagliata, non il testbench. Non modificare mai `test_vectors.h` (e' il golden
model, e' per definizione corretto).
"""


class ReviewerAgent(Agent):
    name = "reviewer"
    system_prompt = SYSTEM

    # ------------------------------------------------------------- review
    def review(self, plan: dict, files: list[ExtractedFile]) -> AgentResult:
        prompt = f"""[REVIEW]

{full_context(plan['meta_hdl'])}

### PIANO
{self._fmt_json({k: v for k, v in plan.items() if k != 'coder_prompt'}, 3000)}

{HEADER_CONTRACT}

### CODICE DA REVISIONARE
{self._dump(files)}

Emetti il JSON di review."""
        text = self.ask(prompt, keep_history=False)
        try:
            data = extract_json(text)
        except ValueError:
            return AgentResult(True, {"verdict": "approved", "issues": []}, text,
                               "review non parsabile, proseguo")
        return AgentResult(True, data, text)

    # ---------------------------------------------------------------- fix
    def fix(self, plan: dict, files: list[ExtractedFile], stage: str, log: str,
            issues: list[dict] | None = None, attempt: int = 1,
            previous_attempts: list[str] | None = None) -> AgentResult:
        issue_txt = ""
        if issues:
            issue_txt = "\n### PROBLEMI SEGNALATI IN REVIEW\n" + "\n".join(
                f"- [{i.get('severity','?')}] {i.get('file','')}: {i.get('problem','')} "
                f"-> {i.get('fix','')}" for i in issues)

        # Riassunto COMPATTO dei tentativi precedenti al posto della cronologia
        # completa: quest'ultima cresce senza limite e, su modelli locali con
        # finestra piccola, viene troncata dall'inizio facendo sparire proprio
        # la specifica MXFP4 dal system prompt.
        history_txt = ""
        if previous_attempts:
            history_txt = ("\n### TENTATIVI GIA' FALLITI (non riproporli)\n" +
                           "\n".join(f"- tentativo {i + 1}: {truncate(c, 300)}"
                                     for i, c in enumerate(previous_attempts)) +
                           "\nSe la tua diagnosi coincide con una di queste, e' SBAGLIATA: "
                           "l'errore e' identico dopo quelle correzioni. Cerca altrove.")

        prompt = f"""[FIX] Tentativo {attempt}. Fase fallita: **{stage}**.

{full_context(plan['meta_hdl'])}

{HEADER_CONTRACT}
{issue_txt}{history_txt}

### LOG DI ERRORE ({stage})
```
{truncate(log, 5000)}
```

### CODICE ATTUALE
{self._dump(files)}

Diagnostica la causa e riemetti i file corretti per intero."""
        # keep_history=False: il contesto necessario e' tutto nel prompt qui sopra.
        text = self.ask(prompt, keep_history=False)
        new_files = extract_files(text)
        cause = self._cause(text)
        if not new_files:
            return AgentResult(False, files, text, f"Nessun file riemesso. {cause}")
        merged = self._merge(files, new_files)
        return AgentResult(True, merged, text, cause)

    # ------------------------------------------------------------- helpers
    @staticmethod
    def _cause(text: str) -> str:
        for line in text.splitlines():
            if line.strip().upper().startswith("CAUSA"):
                return line.strip()
        return ""

    @staticmethod
    def _merge(old: list[ExtractedFile], new: list[ExtractedFile]) -> list[ExtractedFile]:
        by_path = {f.path: f for f in old}
        by_name = {f.path.rsplit("/", 1)[-1]: f.path for f in old}
        for f in new:
            name = f.path.rsplit("/", 1)[-1]
            if name == "test_vectors.h":
                continue  # il golden model non si tocca
            target = by_name.get(name, f.path)
            by_path[target] = ExtractedFile(target, f.content)
        return list(by_path.values())

    @staticmethod
    def _dump(files: list[ExtractedFile], limit: int = 6000) -> str:
        chunks = []
        for f in files:
            lang = {".scala": "scala", ".sv": "systemverilog", ".v": "verilog",
                    ".cpp": "cpp"}.get(f.ext, "")
            chunks.append(f"### FILE: {f.path}\n```{lang}\n{truncate(f.content, limit)}```")
        return "\n\n".join(chunks)
