"""Agente TESTER — materializza il progetto, genera i vettori, esegue la toolchain.

È l'unico agente che tocca il filesystem e i tool esterni. La parte LLM entra
solo alla fine, per trasformare un log grezzo in una diagnosi azionabile per il
Fixer (e per proporre test aggiuntivi quando tutto passa).
"""
from __future__ import annotations

from pathlib import Path

from ..toolchain import ToolchainReport, ToolchainRunner, scaffold, write_header
from ..toolchain.testvectors import HEADER_NAME
from ..utils import ExtractedFile, truncate
from .base import Agent, AgentResult

SYSTEM = """Sei il TESTER di un sistema agentico per Meta-HDL. Ricevi il log grezzo di
sbt/Verilator/simulazione e lo trasformi in una diagnosi breve e operativa per
l'agente Fixer.

Rispondi con al massimo 12 righe:
CAUSA: <una frase>
FILE: <file più probabile da correggere>
AZIONE: <cosa cambiare, concretamente>
EVIDENZA: <le 1-3 righe di log decisive>

Se il log mostra vettori falliti, indica QUALE proprietà MXFP4 è violata
(bias, subnormale 0.5, saturazione a 6, ordine dei nibble, larghezza
dell'accumulatore, segno). Non riscrivere il codice: quello è compito del Fixer.
"""


class TesterAgent(Agent):
    name = "tester"
    system_prompt = SYSTEM

    def __init__(self, provider, log, workdir: Path, **kw) -> None:
        super().__init__(provider, log, **kw)
        self.workdir = Path(workdir)

    # ------------------------------------------------------------ filesystem
    def materialize(self, plan: dict, files: list[ExtractedFile],
                    num_random: int = 64, seed: int = 1234) -> int:
        """Scrive scaffold + file generati + header dei vettori. Ritorna #vettori."""
        module, hdl = plan["module_name"], plan["meta_hdl"]
        scaffold(self.workdir, module, hdl)
        for f in files:
            p = self.workdir / f.path
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(f.content, encoding="utf-8")
        n = write_header(self.workdir / "sim" / HEADER_NAME,
                         kernel=plan.get("test_plan", {}).get("kernel", "dot_product"),
                         num_random=num_random, k=plan.get("block_size", 32), seed=seed)
        self.log.info(f"progetto in {self.workdir} ({len(files)} file, {n} vettori)")
        return n

    # -------------------------------------------------------------- toolchain
    def run_toolchain(self, plan: dict, round_id: int = 0) -> ToolchainReport:
        runner = ToolchainRunner(self.workdir, plan["module_name"], plan["meta_hdl"])
        rep = runner.run_all()

        # i log delle fasi vanno sempre su disco: senza, l'errore di build si
        # perde e resta solo un "✘ build fallito" inutilizzabile.
        logdir = self.workdir / "logs"
        logdir.mkdir(parents=True, exist_ok=True)
        for s in rep.stages:
            (logdir / f"r{round_id}_{s.stage}.log").write_text(s.log, encoding="utf-8")

        for s in rep.stages:
            if s.skipped and not s.ok:
                self.log.fail(f"{s.stage}: saltato — {s.log.splitlines()[0]}")
            elif s.skipped:
                self.log.info(f"{s.stage}: non applicabile")
            elif s.ok:
                self.log.ok(f"{s.stage} ok")
            else:
                self.log.fail(f"{s.stage} fallito → logs/r{round_id}_{s.stage}.log")
                self.log.block(f"{s.stage} (ultime righe)", "\n".join(
                    s.log.splitlines()[-25:]), 2500)
        return rep

    # -------------------------------------------------------------- diagnosi
    def diagnose(self, plan: dict, report: ToolchainReport) -> AgentResult:
        fail = report.first_failure
        if fail is None:
            return AgentResult(True, None, "", "nessun fallimento da diagnosticare")
        prompt = f"""Modulo: {plan['module_name']} ({plan['meta_hdl']})
Fase fallita: {fail.stage}
Kernel di test: {plan.get('test_plan', {}).get('kernel')}

LOG:
```
{truncate(fail.log, 6000)}
```

Produci la diagnosi nel formato richiesto."""
        text = self.ask(prompt, keep_history=False)
        return AgentResult(False, {"stage": fail.stage, "log": fail.log}, text, text)

    # ------------------------------------------------------- run + diagnose
    def run(self, plan: dict, files: list[ExtractedFile], num_random: int = 64,
            seed: int = 1234, diagnose: bool = True, round_id: int = 0) -> AgentResult:
        self.materialize(plan, files, num_random, seed)
        report = self.run_toolchain(plan, round_id)
        if report.blocked:
            return AgentResult(False, report, "",
                               "Toolchain incompleta: file generati ma non verificati.",
                               meta={"blocked": True})
        if report.ok:
            sim = next(s for s in report.stages if s.stage == "simulate")
            return AgentResult(True, report, "",
                               f"{sim.detail.get('passed', '?')} vettori superati")
        notes = self.diagnose(plan, report).notes if diagnose else ""
        return AgentResult(False, report, "", notes)
