"""Orchestratore: Planner -> Coder -> Reviewer/Fixer -> Tester (con loop di fix)."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from .agents import CoderAgent, PlannerAgent, ReviewerAgent, TesterAgent
from .config import AGENTS, Config
from .knowledge.contracts import has_contract
from .knowledge.references import select_reference
from .llm import LLMError, build_provider
from .toolchain import check_path_sanity, format_tool_report, tool_report
from .toolchain.errsum import summarize_errors
from .toolchain.runner import shutdown_sbt_server
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
    blocked: bool = False      # toolchain esterna assente: generato ma non verificato


class Workflow:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.log = Log(cfg.verbose)
        self.providers = {}
        for role in AGENTS:
            s = cfg.agent_settings(role)
            self.providers[role] = build_provider(
                cfg.provider,
                model=s["model"],
                host=cfg.host,
                num_ctx=s["num_ctx"],
                temperature=s["temperature"],
                max_tokens=s["max_tokens"],
                seed=s["seed"],
                keep_alive=s["keep_alive"],
                timeout=cfg.timeout,
                chunk_timeout=cfg.chunk_timeout,
            )
        self.trace: list[dict] = []
        for role, prov in self.providers.items():
            prov.on_retry = (lambda msg, r=role: self.log.fail(f"{r}: {msg}"))
            prov.on_notice = (lambda msg, r=role: self.log.fail(f"{r}: {msg}"))

    # ------------------------------------------------------------------ util
    def _t(self, agent: str, event: str, **kw) -> None:
        self.trace.append({"t": round(time.time() - self.log.t0, 2), "agent": agent,
                           "event": event, **kw})

    def preflight(self) -> tuple[bool, str]:
        # Ogni agente può usare un modello diverso: vanno controllati tutti,
        # altrimenti un modello mancante emerge solo dopo minuti di lavoro.
        ok, detail = True, ""
        for role, prov in self.providers.items():
            r_ok, r_detail = prov.health_check()
            if not r_ok:
                return False, f"{role}: {r_detail}"
            detail = r_detail
            for note in prov.prepare():
                self.log.fail(f"{role}: {note}")
        self.log.stage("system", f"LLM: {detail}")
        for role, prov in self.providers.items():
            m = prov.meta()
            self.log.info(f"{role:<8} {m.get('model')} digest={str(m.get('digest'))[:12]} "
                          f"ctx={m.get('options', {}).get('num_ctx')} "
                          f"seed={m.get('options', {}).get('seed')}")
        rep = tool_report()
        self.log.info("toolchain:\n" + format_tool_report(rep))
        for warn in check_path_sanity(self.cfg.outdir):
            self.log.fail(f"ATTENZIONE sul percorso di output: {warn}")
        return ok, detail

    # ------------------------------------------------------------------- run
    def run(self) -> RunResult:
        self._workdir: Path | None = None
        # metriche per la campagna sperimentale (bench.py legge report.json)
        self.rounds_used = 0
        self.first_failure_stage: str | None = None
        self.round_history: list[dict] = []
        self.toolchain_seconds = 0.0
        # causa di un'interruzione non imputabile al design (es. Ollama caduto):
        # bench.py la conta come errore di infrastruttura, non come fallimento
        self.aborted: str | None = None
        try:
            return self._run()
        finally:
            # il server di `sbt --client` occupa 1-2 GB: non deve sopravvivere alla run
            if self._workdir is not None and shutdown_sbt_server(self._workdir):
                self.log.info("server sbt chiuso")

    def _run(self) -> RunResult:
        cfg = self.cfg
        ok, detail = self.preflight()
        if not ok:
            return RunResult(False, message=f"Provider non pronto: {detail}")

        planner = PlannerAgent(self.providers["planner"], self.log)
        coder = CoderAgent(self.providers["coder"], self.log)
        reviewer = ReviewerAgent(self.providers["reviewer"], self.log)

        # ---------------------------------------------------------- PLANNER
        self.log.stage("planner", "analisi della richiesta e micro-architettura…")
        try:
            pres = planner.run(cfg.request, cfg.block_size)
        except LLMError as e:
            return RunResult(False, message=str(e))
        if not pres.ok:
            self._t("planner", "failed", note=pres.notes)
            return RunResult(False, message=pres.notes, trace=self.trace)
        plan = pres.payload
        # Testbench: generato dal contratto quando il kernel ne ha uno (default),
        # altrimenti scritto dall'LLM come prima.
        tb_template = cfg.tb_mode == "template" and has_contract(plan["test_plan"]["kernel"])
        plan["testbench"] = "template" if tb_template else "llm"
        if cfg.tb_mode == "template" and not tb_template:
            self.log.info("kernel senza contratto di porte: testbench scritto dall'LLM")
        if plan.get("ports_overridden"):
            self.log.info("porte imposte dal contratto (il piano proponeva diversamente): "
                          + ", ".join(plan["ports_overridden"]))
        self.log.ok(f"{plan['module_name']} "
                    f"({plan['clocking']}, K={plan['block_size']})")
        self.log.info(f"kernel di test: {plan['test_plan']['kernel']}")
        self._t("planner", "plan", module=plan["module_name"])

        workdir = Path(cfg.outdir) / plan["module_name"]
        self._workdir = workdir
        tester = TesterAgent(self.providers["tester"], self.log, workdir,
                             synth=cfg.synth, target_mhz=cfg.target_mhz,
                             lint=cfg.chisel_lint, sbt_mode=cfg.sbt_mode,
                             trace=cfg.trace, coverage=cfg.coverage,
                             pnr_seeds=cfg.pnr_seeds)

        compiled = PlannerAgent.compile_coder_prompt(plan, cfg.request)
        workdir.mkdir(parents=True, exist_ok=True)
        (workdir / "prompt_coder.md").write_text(compiled, encoding="utf-8")
        (workdir / "plan.json").write_text(json.dumps(plan, indent=2, ensure_ascii=False),
                                           encoding="utf-8")

        # ------------------------------------------------------------ CODER
        self.log.stage("coder", "generazione del design e del testbench…")
        reference = select_reference(plan) if cfg.few_shot else None
        self.few_shot_reference = reference.key if reference else None
        if reference:
            self.log.info(f"few-shot: riferimento {reference.key} ({reference.module})")
        try:
            cres = coder.run(plan, compiled, reference=reference)
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
            try:
                rres = reviewer.review(plan, files)
            except LLMError as e:
                # La review statica è un di più: se il servizio è giù non deve
                # buttare via il lavoro di Planner e Coder. Si prosegue al Tester.
                self.log.fail(f"review statica saltata ({str(e)[:120]}) — proseguo")
                self._t("reviewer", "skipped", reason=str(e)[:200])
                rres = None
            data = (rres.payload if rres else None) or {}
            issues = [i for i in data.get("issues", [])
                      if i.get("severity") in ("blocker", "major")]
            verdict = data.get("verdict", "approved")
            self.log.info(f"verdetto: {verdict} ({len(data.get('issues', []))} rilievi)")
            for i in issues[:6]:
                self.log.fail(f"[{i.get('severity')}] {i.get('file','')}: {i.get('problem','')}")
            self._t("reviewer", "review", verdict=verdict, issues=len(issues))
            if verdict == "changes_required" and issues:
                try:
                    fres = reviewer.fix(plan, files, "static_review",
                                        "Rilievi della review statica (vedi elenco).", issues, 0)
                except LLMError as e:
                    # come per la review: la patch statica è facoltativa, il
                    # codice del Coder va comunque alla toolchain
                    self.log.fail(f"patch statica saltata ({str(e)[:120]}) — proseguo")
                    self._t("reviewer", "static_fix_skipped", reason=str(e)[:200])
                    fres = None
                if fres is not None and fres.ok:
                    files = fres.payload
                    self.log.ok(f"patch statica applicata — {fres.notes}")

        # --------------------------------------- TESTER + LOOP DI RIPARAZIONE
        last_report = None
        prev_signature: str | None = None
        causes: list[str] = []
        for rnd in range(cfg.max_fix_rounds + 1):
            self.log.stage("tester", f"round {rnd}: scrittura progetto ed esecuzione toolchain…")
            self.rounds_used = rnd
            t_tc = time.time()
            try:
                tres = tester.run(plan, files, cfg.num_random_vectors, cfg.vector_seed, round_id=rnd)
            except LLMError as e:
                msg = (f"Il provider LLM è caduto durante il round {rnd}: {e}\n"
                       f"I file generati sono salvati; riprendi con "
                       f"`--resume {workdir}` quando il servizio torna disponibile.")
                self.log.fail(msg)
                self.aborted = "llm_unavailable"
                self._finalize(workdir, plan, files, False, msg)
                return RunResult(False, plan, files, workdir, rnd, msg,
                                 self.trace, self._stats())
            # il tempo del Tester include la sua eventuale diagnosi LLM, già
            # contata in llm_stats: qui interessa il totale per round
            self.toolchain_seconds += time.time() - t_tc
            last_report = tres.payload
            self._physical = last_report.physical if last_report else {}
            if last_report is not None:
                fail = last_report.first_failure
                self.round_history.append({"round": rnd, "summary": last_report.summary(),
                                           "failed_stage": fail.stage if fail else None})
                if fail is not None and self.first_failure_stage is None:
                    self.first_failure_stage = fail.stage
            self._t("tester", "toolchain", round=rnd,
                    summary=last_report.summary() if last_report else "")

            if tres.ok:
                self.log.ok(f"VERIFICA SUPERATA — {tres.notes}")
                if cfg.ppa_rounds > 0:
                    files = self._ppa_loop(plan, compiled, files, coder, tester)
                self._finalize(workdir, plan, files, True, tres.notes)
                return RunResult(True, plan, files, workdir, rnd,
                                 tres.notes, self.trace, self._stats())

            if tres.meta.get("blocked"):
                msg = ("Toolchain non installata (sbt e/o verilator): il codice è stato "
                       "generato ma non verificato. Installa i tool e rilancia con "
                       "`--resume` oppure `make run` nella cartella di output.")
                self.log.fail(msg)
                self._finalize(workdir, plan, files, False, msg)
                return RunResult(False, plan, files, workdir, rnd, msg,
                                 self.trace, self._stats(), blocked=True)

            if rnd == cfg.max_fix_rounds:
                break

            fail = last_report.first_failure

            # Se il codice cambia ma l'errore resta identico, il problema non è
            # nel codice: è nel modo in cui invochiamo la toolchain. Continuare
            # a "riparare" brucia round e token inseguendo un fantasma.
            # (Il lint statico è escluso: lì un errore ripetuto è un fix mancato
            # del modello, non un problema di invocazione della toolchain.)
            signature = self._signature(fail)
            if signature == prev_signature and fail.stage != "chisel_lint":
                msg = (f"Errore IDENTICO a quello del round precedente nella fase "
                       f"'{fail.stage}', nonostante il codice sia cambiato.\n"
                       f"Non è un bug del design generato: è l'invocazione della "
                       f"toolchain a essere sospetta.\nControlla "
                       f"{workdir}/logs/r{rnd}_{fail.stage}.log e riproduci il "
                       f"comando a mano prima di spendere altri round.")
                self.log.fail(msg)
                self._t("workflow", "loop_detected", stage=fail.stage, round=rnd)
                self._finalize(workdir, plan, files, False, msg)
                return RunResult(False, plan, files, workdir, rnd, msg,
                                 self.trace, self._stats())
            prev_signature = signature

            self.log.stage("reviewer", f"fix round {rnd + 1} (fase: {fail.stage})…")
            if tres.notes:
                self.log.block("diagnosi del tester", tres.notes, 1200)
            evidence = self.fixer_evidence(fail, workdir, last_report)
            try:
                fres = reviewer.fix(plan, files, fail.stage,
                                    f"{tres.notes}\n\n{evidence}", issues, rnd + 1,
                                    previous_attempts=causes)
            except LLMError as e:
                msg = (f"Il provider LLM è caduto durante il fix del round {rnd + 1}: {e}\n"
                       f"Riprendi con `--resume {workdir}`.")
                self.log.fail(msg)
                self.aborted = "llm_unavailable"
                self._finalize(workdir, plan, files, False, msg)
                return RunResult(False, plan, files, workdir, rnd, msg,
                                 self.trace, self._stats())
            if not fres.ok:
                self.log.fail("il fixer non ha prodotto correzioni; interrompo.")
                break
            files = fres.payload
            if fres.notes:
                causes.append(fres.notes)
            self.log.ok(fres.notes or "patch applicata")
            self._t("reviewer", "fix", round=rnd + 1, cause=fres.notes)

        msg = (f"Verifica non superata dopo {cfg.max_fix_rounds} round di fix. "
               f"Ultimo stato: {last_report.summary() if last_report else 'n/d'}")
        self.log.fail(msg)
        self._finalize(workdir, plan, files, False, msg)
        return RunResult(False, plan, files, workdir, cfg.max_fix_rounds, msg,
                         self.trace, self._stats())

    # ------------------------------------------------------------------- PPA
    @staticmethod
    def _ppa_key(phys: dict) -> tuple[float, int]:
        """Ordinamento delle varianti: Fmax mediana più alta, poi meno LUT4."""
        fm = (phys.get("fmax_mhz") or {}).get("median") or 0.0
        lut = (phys.get("area_ecp5") or {}).get("LUT4") or 10 ** 9
        return fm, -lut

    @classmethod
    def _better(cls, new: dict, old: dict) -> bool:
        (fn, ln), (fo, lo) = cls._ppa_key(new), cls._ppa_key(old)
        if fo and abs(fn - fo) <= 0.01 * fo:          # Fmax pari entro l'1%: decide l'area
            return ln > lo
        return fn > fo

    def _ppa_loop(self, plan: dict, compiled: str, files, coder, tester):
        """--ppa-rounds: varianti ottimizzate che devono ripassare TUTTA la verifica."""
        cfg = self.cfg
        best_files, best_phys = files, dict(self._physical or {})
        if not (best_phys.get("fmax_mhz") or {}).get("n"):
            self.log.fail("PPA saltato: nessuna Fmax misurata (yosys/nextpnr assenti?)")
            return files
        self.ppa_history: list[dict] = []
        on_disk_is_best = True        # il progetto su disco corrisponde a best_files?
        for p in range(1, cfg.ppa_rounds + 1):
            self.log.stage("coder", f"PPA round {p}: variante ottimizzata…")
            try:
                ores = coder.optimize(plan, compiled, best_files, best_phys, self.ppa_history)
            except LLMError as e:
                self.log.fail(f"PPA interrotto: {e}")
                break
            entry = {"round": p, "idea": ores.notes}
            if not ores.ok:
                entry["outcome"] = "nessun codice"
                self.ppa_history.append(entry)
                continue
            try:
                tres = tester.run(plan, ores.payload, cfg.num_random_vectors, cfg.vector_seed,
                                  diagnose=False, round_id=f"ppa{p}")
            except LLMError:
                tres = None
            on_disk_is_best = False
            phys = tres.payload.physical if tres is not None and tres.payload else {}
            entry.update({"fmax_median": (phys.get("fmax_mhz") or {}).get("median"),
                          "lut4": (phys.get("area_ecp5") or {}).get("LUT4")})
            if tres is None or not tres.ok:
                entry["outcome"] = ("verifica fallita: " +
                                    (tres.payload.summary() if tres and tres.payload else "n/d"))
            elif self._better(phys, best_phys):
                entry["outcome"] = "adottata"
                best_files, best_phys = ores.payload, phys
                on_disk_is_best = True
            else:
                entry["outcome"] = "corretta ma non migliore"
            self.log.info(f"PPA round {p}: {entry['outcome']} — Fmax {entry['fmax_median']} "
                          f"MHz, LUT4 {entry['lut4']}")
            self.ppa_history.append(entry)
            self._t("coder", "ppa", **entry)

        if not on_disk_is_best:
            # Gli artefatti su disco (RTL, physical.json…) devono corrispondere al
            # design scelto: si riesegue la toolchain sulla variante migliore.
            tester.run(plan, best_files, cfg.num_random_vectors, cfg.vector_seed,
                       diagnose=False, round_id="ppa_final")
        self._physical = best_phys
        return best_files

    # -------------------------------------------------------------- finalize
    @staticmethod
    def fixer_evidence(fail, workdir: Path, report=None, tail_chars: int = 1500) -> str:
        """Log per il Fixer: riassunto file:riga + contesto, più la coda del log grezzo.

        Il riassunto è ciò su cui il modello deve agire; la coda resta perché
        un formato di errore non riconosciuto non deve far perdere informazione.
        Gli avvisi del lint statico si aggiungono quando a fallire è una fase
        successiva: spesso ne sono la causa (es. `+` che tronca → vettori errati).
        """
        summary = summarize_errors(fail.log, workdir)
        if summary:
            text = (f"RIASSUNTO DEGLI ERRORI (posizioni nel tuo codice):\n{summary}\n\n"
                    f"--- coda del log grezzo ---\n{fail.log[-tail_chars:]}")
        else:
            text = fail.log
        lint = next((s for s in (report.stages if report else [])
                     if s.stage == "chisel_lint" and s is not fail), None)
        if lint is not None and lint.detail.get("warnings_text"):
            text += ("\n\n--- AVVISI DEL LINT STATICO (possibili cause) ---\n"
                     + lint.detail["warnings_text"])
        return text

    @staticmethod
    def _signature(fail) -> str:
        """Impronta di un fallimento, insensibile a path assoluti e timestamp."""
        import hashlib
        import re

        norm = re.sub(r"/[^\s:]+/", "/…/", fail.log)          # path
        norm = re.sub(r"\d{2}:\d{2}:\d{2}|\b\d+[.,]\d+ s\b", "", norm)  # tempi
        return f"{fail.stage}:{hashlib.sha1(norm.encode()).hexdigest()[:16]}"

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
            "kernel": plan.get("test_plan", {}).get("kernel"),
            "clocking": plan.get("clocking"),
            "testbench": plan.get("testbench"),
            "ports_overridden": plan.get("ports_overridden", []),
            "few_shot_reference": getattr(self, "few_shot_reference", None),
            # round 0 = prima verifica; N = numero di round di fix effettivamente usati
            "fix_rounds": getattr(self, "rounds_used", 0),
            "first_failure_stage": getattr(self, "first_failure_stage", None),
            "rounds": getattr(self, "round_history", []),
            "timing_s": {"total": round(time.time() - self.log.t0, 1),
                         "toolchain": round(getattr(self, "toolchain_seconds", 0.0), 1),
                         **{role: round(p.stats["seconds"], 1)
                            for role, p in self.providers.items()}},
            "llm": {role: p.meta() for role, p in self.providers.items()},
            "llm_stats": self._stats(),
            "physical": getattr(self, "_physical", {}),
            "ppa": getattr(self, "ppa_history", None),
            "aborted": getattr(self, "aborted", None),
            "trace": self.trace,
        }
        (workdir / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False),
                                             encoding="utf-8")
        (workdir / "README.md").write_text(self._readme(plan, files, ok, message),
                                           encoding="utf-8")

    @staticmethod
    def _readme(plan: dict, files, ok: bool, message: str) -> str:
        flist = "\n".join(f"- `{f.path}`" for f in files)
        cmd = "sbt \"runMain mxfp4.Elaborate\"   # Chisel -> SystemVerilog\n"
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
mxfp4-agent --resume .           # + sintesi Yosys e timing nextpnr -> physical.json
```

## Interpretazione dell'uscita
Il risultato reale del dot-product è `accQ2 / 4 * 2^expOut`, dove `expOut =
(scaleA - 127) + (scaleB - 127)`. L'accumulo è **esatto**: nessun errore di
arrotondamento è introdotto dall'hardware.
"""
