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
    try:
        p = subprocess.run(cmd, cwd=str(cwd) if cwd else None, capture_output=True,
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


def format_tool_report(rep: dict[str, str | None]) -> str:
    return "\n".join(f"  {'✔' if v else '✘'} {k:<10} {v or 'NON TROVATO'}"
                     for k, v in rep.items())
