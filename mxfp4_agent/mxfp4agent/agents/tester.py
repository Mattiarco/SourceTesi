"""Agente TESTER — materializza il progetto, genera i vettori, esegue la toolchain.

È l'unico agente che tocca il filesystem e i tool esterni. La parte LLM entra
solo alla fine, per trasformare un log grezzo in una diagnosi azionabile per il
Fixer (e per proporre test aggiuntivi quando tutto passa).
"""
from __future__ import annotations

from pathlib import Path

from ..toolchain import ToolchainReport, ToolchainRunner, scaffold, write_header
from ..toolchain.chisel_lint import lint_chisel
from ..toolchain.errsum import summarize_errors
from ..toolchain.stage import StageResult
from ..toolchain.tbgen import render_testbench
from ..toolchain.testvectors import HEADER_NAME
from ..utils import ExtractedFile, truncate
from .base import Agent, AgentResult

SYSTEM = """Sei il TESTER di un sistema agentico per Meta-HDL. Ricevi il log grezzo di
sbt/Verilator/simulazione/sintesi Yosys e lo trasformi in una diagnosi breve e operativa per
l'agente Fixer.

Rispondi con al massimo 12 righe:
CAUSA: <una frase>
FILE: <file più probabile da correggere>
AZIONE: <cosa cambiare, concretamente>
EVIDENZA: <le 1-3 righe di log decisive>

Se il log mostra vettori falliti, indica QUALE proprietà MXFP4 è violata
(bias, subnormale 0.5, saturazione a 6, ordine dei nibble, larghezza
dell'accumulatore, segno).

Se fallisce la fase `synth`, l'RTL non è sintetizzabile: cerca latch inferiti
(assegnazioni incomplete), anelli combinatori, driver multipli, costrutti non
supportati da Yosys.

Non riscrivere il codice: quello è compito del Fixer.
"""


class TesterAgent(Agent):
    name = "tester"
    system_prompt = SYSTEM

    def __init__(self, provider, log, workdir: Path, synth: bool = True,
                 target_mhz: float = 100.0, lint: bool = True,
                 sbt_mode: str = "auto", trace: bool = False, coverage: bool = False,
                 **kw) -> None:
        super().__init__(provider, log, **kw)
        self.workdir = Path(workdir)
        self.synth = synth
        self.target_mhz = target_mhz
        self.lint = lint
        self.sbt_mode = sbt_mode
        self.trace = trace
        self.coverage = coverage

    # ------------------------------------------------------------ filesystem
    def materialize(self, plan: dict, files: list[ExtractedFile],
                    num_random: int = 64, seed: int = 1234) -> int:
        """Scrive scaffold + file generati + header dei vettori. Ritorna #vettori."""
        scaffold(self.workdir, plan["module_name"])
        if plan.get("testbench") == "template":
            # testbench deterministico: si rimuove ogni .cpp precedente (es. di un
            # round con testbench LLM) e si scrive quello generato dal contratto
            sim = self.workdir / "sim"
            sim.mkdir(parents=True, exist_ok=True)
            for old in sim.glob("*.cpp"):
                old.unlink()
            (sim / f"tb_{plan['module_name']}.cpp").write_text(render_testbench(plan),
                                                               encoding="utf-8")
        for f in files:
            if plan.get("testbench") == "template" and f.ext in (".cpp", ".cc"):
                continue
            p = self.workdir / f.path
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(f.content, encoding="utf-8")
        n = write_header(self.workdir / "sim" / HEADER_NAME,
                         kernel=plan.get("test_plan", {}).get("kernel", "dot_product"),
                         num_random=num_random, k=plan.get("block_size", 32), seed=seed)
        self.log.info(f"progetto in {self.workdir} ({len(files)} file, {n} vettori)")
        return n

    # -------------------------------------------------------------- toolchain
    def _write_logs(self, rep: ToolchainReport, round_id: int) -> None:
        # i log delle fasi vanno sempre su disco: senza, l'errore di build si
        # perde e resta solo un "✘ build fallito" inutilizzabile.
        logdir = self.workdir / "logs"
        logdir.mkdir(parents=True, exist_ok=True)
        for s in rep.stages:
            (logdir / f"r{round_id}_{s.stage}.log").write_text(s.log, encoding="utf-8")

    def run_toolchain(self, plan: dict, round_id: int = 0,
                      pre: list[StageResult] | None = None) -> ToolchainReport:
        runner = ToolchainRunner(self.workdir, plan["module_name"],
                                 synth=self.synth, target_mhz=self.target_mhz,
                                 sbt_mode=self.sbt_mode, trace=self.trace,
                                 coverage=self.coverage)
        rep = runner.run_all()
        rep.stages[0:0] = pre or []
        self._write_logs(rep, round_id)

        for s in rep.stages:
            if s.skipped and not s.ok:
                self.log.fail(f"{s.stage}: saltato — {s.log.splitlines()[0]}")
            elif s.skipped:
                self.log.info(f"{s.stage}: non applicabile")
            elif s.ok and s.stage in ("synth", "implement"):
                self.log.ok(f"{s.stage}: {s.log.splitlines()[0]}")
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
        summary = summarize_errors(fail.log, self.workdir)
        evidence = f"{summary}\n\n{fail.log[-1500:]}" if summary else truncate(fail.log, 6000)
        prompt = f"""Modulo: {plan['module_name']} (Chisel)
Fase fallita: {fail.stage}
Kernel di test: {plan.get('test_plan', {}).get('kernel')}

LOG:
```
{evidence}
```

Produci la diagnosi nel formato richiesto."""
        text = self.ask(prompt, keep_history=False)
        return AgentResult(False, {"stage": fail.stage, "log": fail.log}, text, text)

    # ------------------------------------------------------------ lint statico
    def static_lint(self, plan: dict, files: list[ExtractedFile]) -> StageResult:
        """Lint Chisel deterministico. Applica IN PLACE le correzioni sicure ai file."""
        logs, warn_logs, errors, warnings, fixed = [], [], 0, 0, 0
        for f in files:
            if f.ext != ".scala":
                continue
            res = lint_chisel(f.content, plan)
            f.content = res.source
            errors += len(res.errors)
            warnings += len(res.warnings)
            fixed += len(res.fixed)
            if res.issues:
                logs.append(res.report(f.path))
            if res.warnings:
                warn_logs.append(res.report(f.path, only=("warning",)))
            for i in res.fixed:
                self.log.info(f"lint: corretto {f.path}:{i.line} — {i.message}")
        detail = {"errors": errors, "warnings": warnings, "fixed": fixed,
                  "warnings_text": "\n".join(warn_logs)}
        head = (f"chisel-lint: {errors} errori, {warnings} avvisi, {fixed} correzioni "
                f"automatiche")
        return StageResult("chisel_lint", errors == 0, "\n".join([head, *logs]), detail=detail)

    # ------------------------------------------------------- run + diagnose
    def run(self, plan: dict, files: list[ExtractedFile], num_random: int = 64,
            seed: int = 1234, diagnose: bool = True, round_id: int = 0) -> AgentResult:
        lint = self.static_lint(plan, files) if self.lint else None
        self.materialize(plan, files, num_random, seed)
        if lint is not None and not lint.ok:
            # Errore certo: inutile spendere un'elaborazione sbt. Il report del
            # lint è già azionabile, quindi niente diagnosi LLM del Tester.
            report = ToolchainReport([lint])
            self._write_logs(report, round_id)
            self.log.fail(f"chisel_lint: {lint.detail['errors']} errori bloccanti "
                          f"→ logs/r{round_id}_chisel_lint.log")
            self.log.block("chisel_lint", lint.log, 2500)
            return AgentResult(False, report, "", "", meta={"lint": True})
        report = self.run_toolchain(plan, round_id, pre=[lint] if lint else [])
        if report.blocked:
            return AgentResult(False, report, "",
                               "Toolchain incompleta: file generati ma non verificati.",
                               meta={"blocked": True})
        if report.ok:
            sim = next(s for s in report.stages if s.stage == "simulate")
            return AgentResult(True, report, "",
                               f"{sim.detail.get('passed', '?')} vettori superati")
        notes = ""
        if diagnose:
            try:
                notes = self.diagnose(plan, report).notes
            except Exception as e:
                # La diagnosi è un aiuto per il Fixer, non un requisito: il log
                # grezzo basta. Non deve far crollare il round.
                self.log.fail(f"diagnosi saltata ({str(e)[:100]})")
        return AgentResult(False, report, "", notes)
