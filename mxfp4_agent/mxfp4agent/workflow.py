"""Orchestratore: Planner -> Coder -> Reviewer/Fixer -> Tester (con loop di fix)."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from .agents import CoderAgent, PlannerAgent, ReviewerAgent, TesterAgent
from .config import Config
from .llm import LLMError, build_provider
from .toolchain import format_tool_report, tool_report
from .utils import ExtractedFile, Log


@dataclass
class RunResult:
    ok: bool
    plan: dict | None = None
    files: list[ExtractedFile] = field(default_factory=list)
    workdir: Path | None = None
    rounds: int = 0
    message: str = ""
    trace: list[dict] = field(default_factory=list)
    stats: dict = field(default_factory=dict)


class Workflow:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.log = Log(cfg.verbose)
        self.providers = {}
        for role in ("planner", "coder", "reviewer", "tester"):
            self.providers[role] = build_provider(
                cfg.provider,
                model=cfg.agent_model(role),
                host=cfg.host,
                api_key=cfg.api_key,
                temperature=cfg.temperature,
                max_tokens=cfg.max_tokens,
                timeout=cfg.timeout,
            )
        self.trace: list[dict] = []

    # ------------------------------------------------------------------ util
    def _t(self, agent: str, event: str, **kw) -> None:
        self.trace.append({"t": round(time.time() - self.log.t0, 2), "agent": agent,
                           "event": event, **kw})

    def preflight(self) -> tuple[bool, str]:
        # live=False: la vera prova è la chiamata del Planner subito dopo, che
        # riporta comunque l'errore HTTP. Evitiamo una chiamata a pagamento in più.
        ok, detail = self.providers["planner"].health_check(live=False)
        self.log.stage("system", f"LLM: {detail}")
        rep = tool_report()
        self.log.info("toolchain:\n" + format_tool_report(rep))
        return ok, detail

    # ------------------------------------------------------------------- run
    def run(self) -> RunResult:
        cfg = self.cfg
        ok, detail = self.preflight()
        if not ok:
            return RunResult(False, message=f"Provider non pronto: {detail}")

        planner = PlannerAgent(self.providers["planner"], self.log)
        coder = CoderAgent(self.providers["coder"], self.log)
        reviewer = ReviewerAgent(self.providers["reviewer"], self.log)

        # ---------------------------------------------------------- PLANNER
        self.log.stage("planner", "analisi della richiesta e scelta del Meta-HDL…")
        try:
            pres = planner.run(cfg.request, cfg.target, cfg.block_size)
        except LLMError as e:
            return RunResult(False, message=str(e))
        if not pres.ok:
            self._t("planner", "failed", note=pres.notes)
            return RunResult(False, message=pres.notes, trace=self.trace)
        plan = pres.payload
        self.log.ok(f"{plan['module_name']} in {plan['meta_hdl']} "
                    f"({plan['clocking']}, K={plan['block_size']})")
        self.log.info(f"kernel di test: {plan['test_plan']['kernel']}")
        self._t("planner", "plan", module=plan["module_name"], hdl=plan["meta_hdl"])

        workdir = Path(cfg.outdir) / plan["module_name"]
        tester = TesterAgent(self.providers["tester"], self.log, workdir)

        compiled = PlannerAgent.compile_coder_prompt(plan, cfg.request)
        workdir.mkdir(parents=True, exist_ok=True)
        (workdir / "prompt_coder.md").write_text(compiled, encoding="utf-8")
        (workdir / "plan.json").write_text(json.dumps(plan, indent=2, ensure_ascii=False),
                                           encoding="utf-8")

        # ------------------------------------------------------------ CODER
        self.log.stage("coder", "generazione del design e del testbench…")
        try:
            cres = coder.run(plan, compiled, few_shot=cfg.few_shot)
        except LLMError as e:
            return RunResult(False, plan, message=str(e), trace=self.trace)
        if not cres.ok:
            self._t("coder", "failed", note=cres.notes)
            return RunResult(False, plan, cres.payload or [], workdir,
                             message=cres.notes, trace=self.trace)
        files: list[ExtractedFile] = cres.payload
        for f in files:
            self.log.ok(f"{f.path} ({len(f.content.splitlines())} righe)")
        self._t("coder", "files", paths=[f.path for f in files])

        # ---------------------------------------------- REVIEW STATICA (1x)
        issues: list[dict] = []
        if cfg.static_review:
            self.log.stage("reviewer", "ispezione statica prima della toolchain…")
            rres = reviewer.review(plan, files)
            data = rres.payload or {}
            issues = [i for i in data.get("issues", [])
                      if i.get("severity") in ("blocker", "major")]
            verdict = data.get("verdict", "approved")
            self.log.info(f"verdetto: {verdict} ({len(data.get('issues', []))} rilievi)")
            for i in issues[:6]:
                self.log.fail(f"[{i.get('severity')}] {i.get('file','')}: {i.get('problem','')}")
            self._t("reviewer", "review", verdict=verdict, issues=len(issues))
            if verdict == "changes_required" and issues:
                fres = reviewer.fix(plan, files, "static_review",
                                    "Rilievi della review statica (vedi elenco).", issues, 0)
                if fres.ok:
                    files = fres.payload
                    self.log.ok(f"patch statica applicata — {fres.notes}")

        # --------------------------------------- TESTER + LOOP DI RIPARAZIONE
        last_report = None
        for rnd in range(cfg.max_fix_rounds + 1):
            self.log.stage("tester", f"round {rnd}: scrittura progetto ed esecuzione toolchain…")
            tres = tester.run(plan, files, cfg.num_random_vectors, cfg.seed)
            last_report = tres.payload
            self._t("tester", "toolchain", round=rnd,
                    summary=last_report.summary() if last_report else "")

            if tres.ok:
                self.log.ok(f"VERIFICA SUPERATA — {tres.notes}")
                self._finalize(workdir, plan, files, True, tres.notes)
                return RunResult(True, plan, files, workdir, rnd,
                                 tres.notes, self.trace, self._stats())

            if tres.meta.get("blocked"):
                msg = ("Toolchain non installata (sbt e/o verilator): il codice è stato "
                       "generato ma non verificato. Installa i tool e rilancia con "
                       "`--resume` oppure `make run` nella cartella di output.")
                self.log.fail(msg)
                self._finalize(workdir, plan, files, False, msg)
                if not cfg.keep_going:
                    return RunResult(False, plan, files, workdir, rnd, msg,
                                     self.trace, self._stats())
                break

            if rnd == cfg.max_fix_rounds:
                break

            fail = last_report.first_failure
            self.log.stage("reviewer", f"fix round {rnd + 1} (fase: {fail.stage})…")
            if tres.notes:
                self.log.block("diagnosi del tester", tres.notes, 1200)
            try:
                fres = reviewer.fix(plan, files, fail.stage,
                                    f"{tres.notes}\n\n{fail.log}", issues, rnd + 1)
            except LLMError as e:
                return RunResult(False, plan, files, workdir, rnd, str(e), self.trace)
            if not fres.ok:
                self.log.fail("il fixer non ha prodotto correzioni; interrompo.")
                break
            files = fres.payload
            self.log.ok(fres.notes or "patch applicata")
            self._t("reviewer", "fix", round=rnd + 1, cause=fres.notes)

        msg = (f"Verifica non superata dopo {cfg.max_fix_rounds} round di fix. "
               f"Ultimo stato: {last_report.summary() if last_report else 'n/d'}")
        self.log.fail(msg)
        self._finalize(workdir, plan, files, False, msg)
        return RunResult(False, plan, files, workdir, cfg.max_fix_rounds, msg,
                         self.trace, self._stats())

    # -------------------------------------------------------------- finalize
    def _stats(self) -> dict:
        return {role: {"model": p.describe(), **p.stats} for role, p in self.providers.items()}

    def _finalize(self, workdir: Path, plan: dict, files, ok: bool, message: str) -> None:
        report = {
            "ok": ok,
            "message": message,
            "module": plan["module_name"],
            "meta_hdl": plan["meta_hdl"],
            "request": self.cfg.request,
            "config": self.cfg.to_dict(),
            "files": [f.path for f in files],
            "llm_stats": self._stats(),
            "trace": self.trace,
        }
        (workdir / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False),
                                             encoding="utf-8")
        (workdir / "README.md").write_text(self._readme(plan, files, ok, message),
                                           encoding="utf-8")

    @staticmethod
    def _readme(plan: dict, files, ok: bool, message: str) -> str:
        flist = "\n".join(f"- `{f.path}`" for f in files)
        cmd = ("sbt \"runMain mxfp4.Elaborate\"   # Chisel -> SystemVerilog\n"
               if plan["meta_hdl"] == "chisel" else "")
        return f"""# {plan['module_name']} — unità aritmetica MXFP4

Generato automaticamente dalla pipeline agentica (Planner → Coder → Reviewer → Tester).

**Stato verifica:** {'✅ superata' if ok else '❌ ' + message}

## Richiesta originale
> {plan.get('rationale', '')}

## File
{flist}
- `sim/test_vectors.h` — vettori attesi dal golden model Python (non modificare)
- `plan.json`, `prompt_coder.md`, `report.json` — tracciabilità della generazione

## Riprodurre la verifica
```bash
{cmd}make run                         # verilator: build + simulazione
```

## Interpretazione dell'uscita
Il risultato reale del dot-product è `accQ2 / 4 * 2^expOut`, dove `expOut =
(scaleA - 127) + (scaleB - 127)`. L'accumulo è **esatto**: nessun errore di
arrotondamento è introdotto dall'hardware.
"""
