"""Esecuzione della toolchain: Chisel -> SystemVerilog -> Verilator -> simulazione."""
from __future__ import annotations

import re
import time
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .shell import CmdResult, run, which
from .stage import StageResult
from .physical import run_physical
from .synth import run_synth

PASS_RE = re.compile(r"TEST\s+PASSED\s*\((\d+)", re.IGNORECASE)
FAIL_RE = re.compile(r"TEST\s+FAILED\s*\((\d+)\s*/\s*(\d+)", re.IGNORECASE)
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07")


def strip_ansi(text: str) -> str:
    """Il client sbt colora l'output anche se rediretto: i codici ANSI rompono i parser."""
    return _ANSI.sub("", text or "")


def _looks_like_build_error(r: CmdResult) -> bool:
    """True se sbt ha davvero compilato/eseguito e fallito per colpa del design."""
    out = r.stdout + r.stderr
    return bool(re.search(r"\.scala:\d+|Exception|Compilation failed|error: |"
                          r"Nonzero exit code|FirtoolNonZeroExitCode", out))


def summarize_coverage(dat: str) -> dict:
    """Riepilogo di un `coverage.dat` di Verilator per tipo di punto.

    Ogni riga è ``C '<chiavi>' <conteggio>``; le chiavi sono coppie separate
    da \\x01 (chiave) e \\x02 (valore). `page` vale ad es. ``v_line/Mod``,
    ``v_toggle/Mod``, ``v_branch/Mod``: il tipo è la parte prima della barra.
    """
    per: dict[str, list[int]] = {}
    for line in dat.splitlines():
        m = re.match(r"C '(.*)' (\d+)\s*$", line)
        if not m:
            continue
        fields = dict(kv.split("\x02", 1) for kv in m.group(1).split("\x01") if "\x02" in kv)
        kind = fields.get("page", "v_other").split("/", 1)[0].removeprefix("v_") or "other"
        hit = per.setdefault(kind, [0, 0])
        hit[1] += 1
        hit[0] += int(m.group(2)) > 0
    out = {kind: {"covered": c, "total": t, "percent": round(100.0 * c / t, 1) if t else 0.0}
           for kind, (c, t) in sorted(per.items())}
    tot_c, tot_t = sum(c for c, _ in per.values()), sum(t for _, t in per.values())
    out["all"] = {"covered": tot_c, "total": tot_t,
                  "percent": round(100.0 * tot_c / tot_t, 1) if tot_t else 0.0}
    return out


def shutdown_sbt_server(root: Path, timeout: int = 60) -> bool:
    """Chiude il server sbt avviato da `sbt --client` nel progetto, se c'è.

    Senza, ogni run lascerebbe una JVM da ~1-2 GB in memoria: in una campagna
    di benchmark la macchina finirebbe la RAM.
    """
    root = Path(root).resolve()
    active = root / "project" / "target" / "active.json"
    if not active.exists() or not which("sbt"):
        return False
    ok = run(["sbt", "--client", "shutdown"], cwd=root, timeout=timeout).ok
    # La chiusura è asincrona: se nel frattempo si ricrea il progetto nella
    # stessa cartella, un nuovo client può agganciarsi al server morente e
    # restare appeso. Si attende che il server rimuova active.json.
    deadline = time.time() + 20
    while active.exists() and time.time() < deadline:
        time.sleep(0.25)
    return ok


@dataclass
class ToolchainReport:
    stages: list[StageResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True solo se la simulazione è stata eseguita e ha superato i test."""
        return (not self.blocked
                and all(s.ok or s.skipped for s in self.stages)
                and any(s.stage == "simulate" and s.ok for s in self.stages))

    @property
    def blocked(self) -> bool:
        """True se un tool esterno manca: non è colpa del codice generato."""
        return any(s.skipped and not s.ok for s in self.stages)

    @property
    def first_failure(self) -> StageResult | None:
        return next((s for s in self.stages if not s.ok and not s.skipped), None)

    @property
    def physical(self) -> dict:
        """Metriche fisiche (vedi physical.py); solo area ECP5 se il P&R non è stato fatto."""
        impl = next((s for s in self.stages if s.stage == "implement" and not s.skipped), None)
        if impl is not None and impl.detail.get("device"):
            return impl.detail
        syn = next((s for s in self.stages if s.stage == "synth" and s.ok and not s.skipped), None)
        return {"area_ecp5": syn.detail.get("resources_yosys", {})} if syn else {}

    def add(self, s: StageResult) -> StageResult:
        self.stages.append(s)
        return s

    def summary(self) -> str:
        icons = {True: "✔", False: "✘"}
        return " | ".join(
            f"{'-' if s.skipped else icons[s.ok]} {s.stage}" for s in self.stages)


def resolve_sbt_mode(mode: str) -> str:
    """`auto` → client su Linux/macOS, batch su Windows.

    Su Windows sbtn passa a sbt.bat un path con `%20` (es. `Program%20Files`)
    e l'avvio del server resta appeso: verificato con sbt 1.10.
    """
    if mode not in ("auto", "client", "batch"):
        raise ValueError(f"sbt_mode non valido: {mode!r} (auto | client | batch)")
    if ToolchainRunner._client_broken:
        return "batch"
    if mode == "auto":
        return "batch" if sys.platform.startswith("win") else "client"
    return mode


class ToolchainRunner:
    """Guida il progetto generato attraverso sbt e Verilator."""

    #: diventa True dopo un fallimento "di infrastruttura" del client sbt
    _client_broken = False

    def __init__(self, root: Path, module: str,
                 sbt_timeout: int = 1800, verilator_timeout: int = 900,
                 sim_timeout: int = 300, synth: bool = True,
                 target_mhz: float = 100.0, synth_timeout: int = 900,
                 sbt_mode: str = "auto", sbt_client_timeout: int = 420,
                 trace: bool = False, coverage: bool = False, pnr_seeds: int = 5,
                 plan: dict | None = None) -> None:
        self.pnr_seeds = pnr_seeds
        self.plan = plan or {}
        self.sbt_mode = sbt_mode
        self.sbt_client_timeout = sbt_client_timeout
        self.trace = trace            # dump VCD in sim/waves.vcd
        self.coverage = coverage      # coverage Verilator in sim/coverage.dat
        # SEMPRE assoluto: i comandi girano con cwd=self.root, quindi un path
        # relativo verrebbe risolto due volte ("out/X/out/X/rtl/...") e Verilator
        # non troverebbe l'RTL appena generato.
        self.root = Path(root).resolve()
        self.module = module
        self.sbt_timeout = sbt_timeout
        self.verilator_timeout = verilator_timeout
        self.sim_timeout = sim_timeout
        self.synth_enabled = synth
        self.target_mhz = target_mhz
        self.synth_timeout = synth_timeout
        self._netlist: Path | None = None

    # -------------------------------------------------------------- percorsi
    @property
    def rtl_dir(self) -> Path:
        return self.root / "rtl"

    @property
    def sim_dir(self) -> Path:
        return self.root / "sim"

    @property
    def obj_dir(self) -> Path:
        return self.root / "obj_dir"

    def rtl_files(self) -> list[Path]:
        return sorted(list(self.rtl_dir.glob("*.sv")) + list(self.rtl_dir.glob("*.v")))

    # ------------------------------------------------------------- 1. elabora
    def elaborate(self) -> StageResult:
        if not which("sbt"):
            return StageResult("elaborate", False,
                               "`sbt` non trovato nel PATH. Installa sbt + JDK 17 "
                               "(https://www.scala-sbt.org/download).", skipped=True)
        self.rtl_dir.mkdir(parents=True, exist_ok=True)
        for f in self.rtl_files():
            f.unlink()
        mode = resolve_sbt_mode(self.sbt_mode)
        r = self._sbt("runMain mxfp4.Elaborate", mode)
        if mode == "client" and not r.ok and (r.timed_out or not _looks_like_build_error(r)):
            # Il client non è riuscito a parlare col server (socket, lock in
            # ~/.sbt, sbt troppo vecchio, avvio appeso): non è un errore del
            # design. Si ripiega su -batch e non si riprova più in questo processo.
            ToolchainRunner._client_broken = True
            shutdown_sbt_server(self.root, timeout=30)
            mode = "batch"
            r = self._sbt("runMain mxfp4.Elaborate", mode)
        produced = self.rtl_files()
        ok = r.ok and bool(produced)
        log = r.log if ok else self._clean_sbt(r)
        return StageResult("elaborate", ok, log,
                           detail={"rtl_files": [p.name for p in produced], "sbt_mode": mode})

    def _sbt(self, task: str, mode: str) -> CmdResult:
        env = {"MXFP4_RTL_DIR": "rtl", "JAVA_OPTS": "-Xmx2G", "NO_COLOR": "1"}
        timeout = self.sbt_timeout
        if mode == "client":
            # Il primo comando avvia un server sbt per QUESTO progetto; i round
            # successivi lo riusano (JVM calda, build già caricato, compilazione
            # incrementale). Il server si chiude con shutdown_sbt_server().
            # Timeout più corto: un client appeso deve ripiegare presto su -batch.
            cmd = ["sbt", "--client", task]
            timeout = min(timeout, self.sbt_client_timeout)
        else:
            cmd = ["sbt", "-batch", "-Dsbt.color=false", "-Dsbt.supershell=false", task]
        r = run(cmd, cwd=self.root, timeout=timeout, env=env)
        r.stdout, r.stderr = strip_ansi(r.stdout), strip_ansi(r.stderr)
        return r

    @staticmethod
    def _clean_sbt(r: CmdResult) -> str:
        """Tiene solo le righe utili dei log sbt (sono verbosissimi)."""
        keep, lines = [], (r.stdout + "\n" + r.stderr).splitlines()
        for i, ln in enumerate(lines):
            if re.search(r"\[error\]|\[warn\].*(deprecated|not found)|Exception|error:", ln):
                keep.extend(lines[max(0, i - 1):i + 3])
        out = "\n".join(dict.fromkeys(keep)) or r.log
        return f"$ {' '.join(r.cmd)}\n{out[-7000:]}"

    # ---------------------------------------------------------------- 2. lint
    def lint(self) -> StageResult:
        if not which("verilator"):
            return StageResult("lint", False,
                               "`verilator` non trovato nel PATH "
                               "(apt install verilator / brew install verilator).", skipped=True)
        files = self.rtl_files()
        if not files:
            return StageResult("lint", False, "Nessun file RTL in rtl/.")
        # path ASSOLUTI: Verilator non deve mai dover calcolare percorsi relativi
        # (fallisce se la directory contiene caratteri non ASCII).
        r = run(["verilator", "--lint-only", "-Wall", "-Wno-DECLFILENAME",
                 "-Wno-UNUSEDSIGNAL", "--top-module", self.module,
                 *[str(f) for f in files]],
                cwd=self.root, timeout=self.verilator_timeout)
        return StageResult("lint", r.ok, r.log)

    # --------------------------------------------------------------- 3. build
    def build(self) -> StageResult:
        if not which("verilator"):
            return StageResult("build", False, "`verilator` non trovato nel PATH.", skipped=True)
        tb = self.sim_dir / f"tb_{self.module}.cpp"
        if not tb.exists():
            cands = list(self.sim_dir.glob("*.cpp"))
            if not cands:
                return StageResult("build", False, f"Testbench mancante: {tb}")
            tb = cands[0]
        files = self.rtl_files()
        if not files:
            return StageResult("build", False, "Nessun file RTL da compilare.")
        # UNUSEDSIGNAL: un Module Chisel espone sempre clock/reset, inutilizzati
        # nei design combinatori. Il warning è atteso, non va segnalato al Fixer.
        # Tutto ASSOLUTO, --Mdir compreso. Con path relativi Verilator deve
        # calcolare il prefisso "../" del VPATH rispetto a obj_dir, e su
        # directory con caratteri non ASCII (es. "Università") sbaglia: emette
        # "sim/tb_X.cpp" invece di "../sim/tb_X.cpp" e make non trova il file.
        cmd = ["verilator", "--cc", "--exe", "--build", "-Wall", "-Wno-fatal",
               "-Wno-DECLFILENAME", "-Wno-UNUSEDSIGNAL", "--top-module", self.module,
               "--Mdir", str(self.obj_dir), "-o", f"sim_{self.module}",
               "-CFLAGS", f"-I{self.sim_dir} -O2",
               *(["--trace"] if self.trace else []),
               *(["--coverage"] if self.coverage else []),
               *[str(f) for f in files], str(tb)]
        r = run(cmd, cwd=self.root, timeout=self.verilator_timeout)
        exe = self.obj_dir / f"sim_{self.module}"
        ok = r.ok and exe.exists()
        return StageResult("build", ok, r.log, detail={"exe": str(exe)})

    # ------------------------------------------------------------ 4. simulate
    def simulate(self) -> StageResult:
        exe = self.obj_dir / f"sim_{self.module}"
        if not exe.exists():
            return StageResult("simulate", False, f"Eseguibile assente: {exe}", skipped=True)
        cov_file = self.sim_dir / "coverage.dat"
        cov_file.unlink(missing_ok=True)
        r = run([str(exe)], cwd=self.root, timeout=self.sim_timeout)
        out = r.stdout + r.stderr
        m_pass, m_fail = PASS_RE.search(out), FAIL_RE.search(out)
        extra: dict = {}
        if self.coverage and cov_file.exists():
            extra["coverage"] = summarize_coverage(cov_file.read_text(errors="replace"))
        if self.trace and (self.sim_dir / "waves.vcd").exists():
            extra["vcd"] = str(self.sim_dir / "waves.vcd")
        if m_pass and not m_fail:
            return StageResult("simulate", True, r.log,
                               detail={"passed": int(m_pass.group(1)), "failed": 0, **extra})
        if m_fail:
            return StageResult("simulate", False, r.log,
                               detail={"failed": int(m_fail.group(1)),
                                       "total": int(m_fail.group(2)), **extra})
        return StageResult("simulate", False,
                           r.log + "\n\n[runner] Nessuna riga TEST PASSED/FAILED trovata.")

    # ------------------------------------------------------------- 5. sintesi
    def synth(self) -> StageResult:
        """Sintesi Yosys: l'RTL deve essere sintetizzabile, e si estraggono le risorse."""
        s = run_synth(self.root, self.module, self.rtl_files(), self.synth_timeout)
        self._netlist = Path(s.detail["netlist"]) if s.ok and s.detail.get("netlist") else None
        return s

    # ------------------------------------------------------- 6. implementation
    def implement(self) -> StageResult:
        """Place & route multi-seed con wrapper a registri (informativa): physical.json/.md."""
        return run_physical(self.root, self.module, self.rtl_files(), self._netlist,
                            self.target_mhz, self.pnr_seeds, self.plan,
                            timeout=self.synth_timeout)

    # ------------------------------------------------------------------ tutto
    def run_all(self, stop_on_error: bool = True) -> ToolchainReport:
        rep = ToolchainReport()
        for fn in (self.elaborate, self.lint, self.build, self.simulate):
            s = rep.add(fn())
            if stop_on_error and not s.ok and not s.skipped:
                break
            if s.skipped and s.stage in ("elaborate", "build") and not s.ok:
                break
        # Le fasi fisiche hanno senso solo su un design funzionalmente corretto:
        # prima si ripara la funzione, poi si guardano sintesi e timing.
        sim = next((s for s in rep.stages if s.stage == "simulate"), None)
        if self.synth_enabled and sim is not None and sim.ok:
            syn = rep.add(self.synth())
            if syn.ok and not syn.skipped:
                rep.add(self.implement())
        return rep
