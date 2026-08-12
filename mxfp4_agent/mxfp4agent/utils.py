"""Parsing dell'output dei modelli e logging colorato."""
from __future__ import annotations

import json
import re
import sys
import time
from dataclasses import dataclass

# ---------------------------------------------------------------- estrazione
_FILE_BLOCK = re.compile(
    r"^[ \t]*#{0,6}[ \t]*FILE:[ \t]*([^\n`]+?)[ \t]*\n+```[a-zA-Z0-9_+\-]*[ \t]*\n(.*?)^```",
    re.MULTILINE | re.DOTALL,
)
_ANY_FENCE = re.compile(r"```([a-zA-Z0-9_+\-]*)[ \t]*\n(.*?)^```", re.MULTILINE | re.DOTALL)

_EXT_BY_LANG = {"scala": ".scala", "chisel": ".scala", "systemverilog": ".sv",
                "verilog": ".v", "sv": ".sv", "cpp": ".cpp", "c++": ".cpp", "c": ".cpp"}


@dataclass
class ExtractedFile:
    path: str
    content: str

    @property
    def ext(self) -> str:
        return "." + self.path.rsplit(".", 1)[-1] if "." in self.path else ""


def extract_files(text: str) -> list[ExtractedFile]:
    """Estrae i blocchi ``### FILE: path`` + fence dal testo del modello.

    Se il modello dimentica le intestazioni, ricade su un'euristica basata sul
    linguaggio del fence.
    """
    out: list[ExtractedFile] = []
    seen: set[str] = set()
    for m in _FILE_BLOCK.finditer(text):
        path = m.group(1).strip().strip("`\"' ")
        if path and path not in seen:
            seen.add(path)
            out.append(ExtractedFile(path, m.group(2).rstrip() + "\n"))
    if out:
        return out

    # fallback: indovina dal linguaggio del fence
    for i, m in enumerate(_ANY_FENCE.finditer(text)):
        lang = (m.group(1) or "").lower()
        body = m.group(2)
        if lang == "json":
            continue
        ext = _EXT_BY_LANG.get(lang)
        if ext is None:
            continue
        name = _guess_name(body, ext) or f"generated_{i}{ext}"
        if name not in seen:
            seen.add(name)
            out.append(ExtractedFile(name, body.rstrip() + "\n"))
    return out


def _guess_name(body: str, ext: str) -> str | None:
    if ext == ".scala":
        m = re.search(r"class\s+([A-Za-z_][A-Za-z0-9_]*)\s*(?:\(|extends)", body)
        return f"src/main/scala/mxfp4/{m.group(1)}.scala" if m else None
    if ext in (".sv", ".v"):
        m = re.search(r"\bmodule\s+([A-Za-z_][A-Za-z0-9_]*)", body)
        return f"rtl/{m.group(1)}{ext}" if m else None
    if ext == ".cpp":
        m = re.search(r'#include\s+"V([A-Za-z_][A-Za-z0-9_]*)\.h"', body)
        return f"sim/tb_{m.group(1)}.cpp" if m else "sim/tb_main.cpp"
    return None


def extract_json(text: str) -> dict:
    """Recupera il primo oggetto JSON valido dal testo (fence o bilanciamento)."""
    for m in _ANY_FENCE.finditer(text):
        if (m.group(1) or "").lower() in ("json", ""):
            try:
                return json.loads(m.group(2))
            except json.JSONDecodeError:
                pass
    start = text.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            c = text[i]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
            elif c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except json.JSONDecodeError:
                        break
        start = text.find("{", start + 1)
    raise ValueError("Nessun JSON valido nella risposta del modello.")


def sanitize_identifier(name: str, default: str = "MXFP4Unit") -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_]", "", name or "")
    if not cleaned or not (cleaned[0].isalpha() or cleaned[0] == "_"):
        return default
    return cleaned


def truncate(text: str, limit: int = 6000, tail: int = 1500) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit - tail]}\n[... {len(text) - limit} caratteri omessi ...]\n{text[-tail:]}"


# ------------------------------------------------------------------ logging
class Log:
    COLORS = {"planner": "\033[95m", "coder": "\033[94m", "reviewer": "\033[93m",
              "tester": "\033[96m", "ok": "\033[92m", "err": "\033[91m", "dim": "\033[90m"}
    RESET = "\033[0m"

    def __init__(self, verbose: bool = True, color: bool | None = None) -> None:
        self.verbose = verbose
        self.color = sys.stdout.isatty() if color is None else color
        self.t0 = time.time()

    def _c(self, key: str, s: str) -> str:
        return f"{self.COLORS.get(key, '')}{s}{self.RESET}" if self.color else s

    def stage(self, agent: str, msg: str) -> None:
        tag = self._c(agent, f"[{agent.upper():<8}]")
        print(f"{tag} {self._c('dim', f'{time.time() - self.t0:6.1f}s')} {msg}", flush=True)

    def ok(self, msg: str) -> None:
        print(f"{self._c('ok', '  ✔')} {msg}", flush=True)

    def fail(self, msg: str) -> None:
        print(f"{self._c('err', '  ✘')} {msg}", flush=True)

    def info(self, msg: str) -> None:
        if self.verbose:
            print(f"    {self._c('dim', msg)}", flush=True)

    def block(self, title: str, body: str, limit: int = 2000) -> None:
        if not self.verbose:
            return
        print(self._c("dim", f"    ┌─ {title}"))
        for line in truncate(body, limit).splitlines()[:80]:
            print(self._c("dim", f"    │ {line}"))
        print(self._c("dim", "    └─"))
