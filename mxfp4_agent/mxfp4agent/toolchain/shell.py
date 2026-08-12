"""Esecuzione di comandi esterni con timeout e cattura del log."""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class CmdResult:
    cmd: list[str]
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    @property
    def log(self) -> str:
        parts = [f"$ {' '.join(self.cmd)}"]
        if self.stdout.strip():
            parts.append(self.stdout.rstrip())
        if self.stderr.strip():
            parts.append("--- stderr ---\n" + self.stderr.rstrip())
        if self.timed_out:
            parts.append("!!! TIMEOUT !!!")
        return "\n".join(parts)


def run(cmd: list[str], cwd: Path | str | None = None, timeout: int = 900,
        env: dict | None = None) -> CmdResult:
    merged = {**os.environ, **(env or {})}
    # Su Windows CreateProcess non applica PATHEXT: "sbt" non risolve a "sbt.BAT".
    # Risolviamo noi l'eseguibile prima di lanciarlo.
    resolved = list(cmd)
    if not os.path.isabs(resolved[0]) and os.sep not in resolved[0]:
        full = shutil.which(resolved[0])
        if full:
            resolved[0] = full
    try:
        p = subprocess.run(resolved, cwd=str(cwd) if cwd else None, capture_output=True,
                           text=True, timeout=timeout, env=merged, errors="replace")
        return CmdResult(cmd, p.returncode, p.stdout, p.stderr)
    except subprocess.TimeoutExpired as e:
        return CmdResult(cmd, 124, e.stdout or "", e.stderr or "", timed_out=True)
    except FileNotFoundError:
        return CmdResult(cmd, 127, "", f"comando non trovato: {cmd[0]}")


def which(name: str) -> str | None:
    return shutil.which(name)


def tool_report() -> dict[str, str | None]:
    """Stato della toolchain sulla macchina corrente."""
    out: dict[str, str | None] = {}
    for tool in ("sbt", "verilator", "java", "g++", "make"):
        path = which(tool)
        out[tool] = path
    return out


def check_path_sanity(path: Path | str) -> list[str]:
    """Problemi noti dei percorsi che mandano in crisi Verilator/sbt/make.

    Non sono paranoie: un accento nel path (es. ``.../Università/...``) fa
    sbagliare a Verilator il calcolo del VPATH relativo a ``obj_dir``, e make
    fallisce con "No rule to make target 'sim/tb_X.cpp'".
    """
    p = str(Path(path).resolve())
    problems: list[str] = []
    non_ascii = sorted({c for c in p if ord(c) > 127})
    if non_ascii:
        problems.append(
            f"il percorso contiene caratteri non ASCII ({' '.join(non_ascii)}): "
            f"Verilator e make possono sbagliare i path relativi. "
            f"Consiglio: sposta il progetto in una cartella senza accenti.")
    if " " in p:
        problems.append("il percorso contiene spazi: alcuni Makefile generati non li "
                        "gestiscono correttamente.")
    return problems


def format_tool_report(rep: dict[str, str | None]) -> str:
    return "\n".join(f"  {'✔' if v else '✘'} {k:<10} {v or 'NON TROVATO'}"
                     for k, v in rep.items())
