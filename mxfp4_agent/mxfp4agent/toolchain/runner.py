"""Esecuzione della toolchain: Chisel -> SystemVerilog -> Verilator -> simulazione."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .shell import CmdResult, run, which

PASS_RE = re.compile(r"TEST\s+PASSED\s*\((\d+)", re.IGNORECASE)
FAIL_RE = re.compile(r"TEST\s+FAILED\s*\((\d+)\s*/\s*(\d+)", re.IGNORECASE)


@dataclass
class StageResult:
    stage: str          # elaborate | lint | build | simulate
    ok: bool
    log: str
    skipped: bool = False
    detail: dict = field(default_factory=dict)


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

    def add(self, s: StageResult) -> StageResult:
        self.stages.append(s)
        return s

    def summary(self) -> str:
        icons = {True: "✔", False: "✘"}
        return " | ".join(
            f"{'-' if s.skipped else icons[s.ok]} {s.stage}" for s in self.stages)


class ToolchainRunner:
    """Guida il progetto generato attraverso sbt e Verilator."""

    def __init__(self, root: Path, module: str, meta_hdl: str = "chisel",
                 sbt_timeout: int = 1800, verilator_timeout: int = 900,
                 sim_timeout: int = 300) -> None:
        self.root = Path(root)
        self.module = module
        self.meta_hdl = meta_hdl
        self.sbt_timeout = sbt_timeout
        self.verilator_timeout = verilator_timeout
        self.sim_timeout = sim_timeout

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
        if self.meta_hdl != "chisel":
            return StageResult("elaborate", True, "RTL scritto direttamente, nessuna elaborazione.",
                               skipped=True)
        if not which("sbt"):
            return StageResult("elaborate", False,
                               "`sbt` non trovato nel PATH. Installa sbt + JDK 17 "
                               "(https://www.scala-sbt.org/download).", skipped=True)
        self.rtl_dir.mkdir(parents=True, exist_ok=True)
        for f in self.rtl_files():
            f.unlink()
        r = run(["sbt", "-batch", "-Dsbt.color=false", "runMain mxfp4.Elaborate"],
                cwd=self.root, timeout=self.sbt_timeout,
                env={"MXFP4_RTL_DIR": "rtl", "JAVA_OPTS": "-Xmx2G"})
        produced = self.rtl_files()
        ok = r.ok and bool(produced)
        log = r.log if ok else self._clean_sbt(r)
        return StageResult("elaborate", ok, log,
                           detail={"rtl_files": [p.name for p in produced]})

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
        cmd = ["verilator", "--cc", "--exe", "--build", "-Wall", "-Wno-fatal",
               "-Wno-DECLFILENAME", "--top-module", self.module,
               "--Mdir", "obj_dir", "-o", f"sim_{self.module}",
               "-CFLAGS", f"-I{self.sim_dir.resolve()} -O2",
               *[str(f.relative_to(self.root)) for f in files],
               str(tb.relative_to(self.root))]
        r = run(cmd, cwd=self.root, timeout=self.verilator_timeout)
        exe = self.obj_dir / f"sim_{self.module}"
        ok = r.ok and exe.exists()
        return StageResult("build", ok, r.log, detail={"exe": str(exe)})

    # ------------------------------------------------------------ 4. simulate
    def simulate(self) -> StageResult:
        exe = self.obj_dir / f"sim_{self.module}"
        if not exe.exists():
            return StageResult("simulate", False, f"Eseguibile assente: {exe}", skipped=True)
        r = run([str(exe)], cwd=self.root, timeout=self.sim_timeout)
        out = r.stdout + r.stderr
        m_pass, m_fail = PASS_RE.search(out), FAIL_RE.search(out)
        if m_pass and not m_fail:
            return StageResult("simulate", True, r.log,
                               detail={"passed": int(m_pass.group(1)), "failed": 0})
        if m_fail:
            return StageResult("simulate", False, r.log,
                               detail={"failed": int(m_fail.group(1)),
                                       "total": int(m_fail.group(2))})
        return StageResult("simulate", False,
                           r.log + "\n\n[runner] Nessuna riga TEST PASSED/FAILED trovata.")

    # ------------------------------------------------------------------ tutto
    def run_all(self, stop_on_error: bool = True) -> ToolchainReport:
        rep = ToolchainReport()
        for fn in (self.elaborate, self.lint, self.build, self.simulate):
            s = rep.add(fn())
            if stop_on_error and not s.ok and not s.skipped:
                break
            if s.skipped and s.stage in ("elaborate", "build") and not s.ok:
                break
        return rep
