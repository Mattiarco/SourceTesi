"""`--doctor`: verifica dell'ambiente, con istruzioni di installazione SENZA sudo.

Ogni controllo produce (nome, ok, dettaglio, come installarlo, obbligatorio).
Obbligatori: Ollama con i modelli configurati, Java, sbt, Verilator, g++, make.
Facoltativi (le fasi corrispondenti vengono saltate): GPU, yosys, nextpnr-ecp5,
cache offline di Chisel/firtool.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from .toolchain.scaffold import CHISEL_VERSION, SBT_VERSION
from .toolchain.shell import check_path_sanity, run, which

FIRTOOL_VERSION = "1.62.1"      # quella con cui è pubblicato Chisel 6.6.0
SETUP = "bash scripts/setup_server.sh   # installa tutto in ~/.local/mxfp4, senza sudo"
OSS = ("OSS CAD Suite (yosys, nextpnr-ecp5, verilator), senza sudo:\n"
       "      curl -L -o oss.tgz https://github.com/YosysHQ/oss-cad-suite-build/releases/"
       "download/<data>/oss-cad-suite-linux-x64-<data>.tgz\n"
       "      tar -xzf oss.tgz -C ~/.local && export PATH=~/.local/oss-cad-suite/bin:$PATH\n"
       f"      oppure: {SETUP}")


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    hint: str = ""
    required: bool = True


def _version(cmd: list[str], pattern: str = r"(\d+\.\d+(?:\.\d+)?)") -> str | None:
    if not which(cmd[0]):
        return None
    r = run(cmd, timeout=60)
    out = (r.stdout + r.stderr).strip()
    m = re.search(pattern, out)
    if m:
        return m.group(1)
    return out.splitlines()[0][:60] if out else "?"


# ------------------------------------------------------------------ controlli
def check_ollama(host: str, models: list[str]) -> list[Check]:
    from .llm.ollama_provider import OllamaProvider, normalize_model_name

    prov = OllamaProvider(model=models[0] if models else "x", host=host, max_retries=0)
    try:
        installed = prov.installed_models()
        version = prov._get_json("/api/version").get("version", "?")
    except Exception as e:  # noqa: BLE001
        return [Check("ollama", False, f"non raggiungibile su {prov.host}: {str(e)[:120]}",
                      "senza sudo: scarica https://ollama.com/download/ollama-linux-amd64.tgz, "
                      "estrailo in ~/.local/ollama e avvia `~/.local/ollama/bin/ollama serve` "
                      "(in tmux/screen); se gira altrove imposta OLLAMA_HOST o --host")]
    out = [Check("ollama", True, f"{prov.host}, versione {version}, {len(installed)} modelli: "
                                 f"{', '.join(installed) or '-'}")]
    have = {normalize_model_name(n) for n in installed}
    for m in dict.fromkeys(models):
        ok = normalize_model_name(m) in have
        ctx = ""
        if ok:
            p = OllamaProvider(model=m, host=host, max_retries=0)
            info = p.model_info(refresh=True)
            ctx = (f"context {info.get('context_length')}, {info.get('parameter_size')} "
                   f"{info.get('quantization')}, digest {str(info.get('digest'))[:12]}")
        out.append(Check(f"modello {m}", ok, ctx or "non installato", f"ollama pull {m}"))
    return out


def check_gpu() -> Check:
    if not which("nvidia-smi"):
        return Check("GPU NVIDIA", False, "nvidia-smi assente: Ollama userà la CPU (molto lento)",
                     "serve il driver NVIDIA (lato amministratore); per Ollama non serve CUDA "
                     "installato a parte", required=False)
    r = run(["nvidia-smi", "--query-gpu=name,memory.total,memory.used,driver_version",
             "--format=csv,noheader"], timeout=30)
    gpus = [g.strip() for g in r.stdout.splitlines() if g.strip()]
    return Check("GPU NVIDIA", r.ok and bool(gpus), "; ".join(gpus) or r.log[-200:],
                 "controlla il driver con `nvidia-smi`", required=False)


def check_java() -> Check:
    if not which("java"):
        return Check("java", False, "non trovato",
                     f"./cs java-home --jvm temurin:17  (binario `cs` nel repo), poi "
                     f"export JAVA_HOME=$(./cs java-home --jvm temurin:17); oppure: {SETUP}")
    r = run(["java", "-version"], timeout=60)
    m = re.search(r'version "(\d+)(?:\.(\d+))?', r.stdout + r.stderr)
    major = int(m.group(1)) if m else 0
    major = int(m.group(2)) if major == 1 and m and m.group(2) else major   # "1.8"
    ok = 11 <= major <= 21
    detail = f"Java {major}" + ("" if major == 17 else " (consigliato 17)")
    return Check("java", ok, detail,
                 "Chisel 6 / Scala 2.13.14 sono testati con JDK 17: "
                 "export JAVA_HOME=$(./cs java-home --jvm temurin:17)")


def check_sbt() -> Check:
    v = _version(["sbt", "--script-version"]) if which("sbt") else None
    return Check("sbt", v is not None,
                 f"launcher {v}; il progetto fissa sbt {SBT_VERSION}" if v else "non trovato",
                 f"./cs install --install-dir ~/.local/bin sbt:{SBT_VERSION}  "
                 f"(binario `cs` nel repo); oppure: {SETUP}")


def check_chisel_cache() -> Check:
    """Chisel e firtool già in cache → l'elaborazione funziona anche offline."""
    home = Path.home()
    jar = f"chisel_2.13-{CHISEL_VERSION}.jar"
    cs_roots = [Path(os.environ["COURSIER_CACHE"])] if os.environ.get("COURSIER_CACHE") else []
    cs_roots += [home / ".cache" / "coursier", home / "Library" / "Caches" / "Coursier",
                 home / "AppData" / "Local" / "Coursier" / "Cache"]
    chisel = any(next(r.rglob(jar), None) for r in cs_roots if r.is_dir())
    fir_roots = [home / ".cache" / "llvm-firtool" / FIRTOOL_VERSION,
                 home / "AppData" / "Local" / "org.chipsalliance" / "llvm-firtool" / "cache" /
                 FIRTOOL_VERSION,
                 home / "Library" / "Caches" / "llvm-firtool" / FIRTOOL_VERSION]
    fir = os.environ.get("CHISEL_FIRTOOL_PATH") or next(
        (str(p) for p in fir_roots if p.is_dir()), None)
    ok = chisel and bool(fir)
    detail = (f"Chisel {CHISEL_VERSION} {'in cache' if chisel else 'NON in cache'}, "
              f"firtool {FIRTOOL_VERSION} {'in ' + fir if fir else 'NON in cache'}")
    return Check("Chisel/firtool", ok, detail,
                 "alla prima elaborazione sbt scarica Chisel e firtool (serve rete). Per "
                 "lavorare offline esegui una volta: python3 run.py --selftest "
                 f"(lo fa anche {SETUP.split('#')[0].strip()})", required=False)


def check_tool(name: str, cmd: list[str], hint: str, required: bool = True,
               pattern: str = r"(\d+\.\d+(?:\.\d+)?)") -> Check:
    v = _version(cmd, pattern)
    return Check(name, v is not None, f"{v}  ({which(cmd[0])})" if v else "non trovato", hint,
                 required)


def run_checks(host: str, models: list[str], outdir: Path | None = None) -> list[Check]:
    checks = check_ollama(host, models)
    checks += [check_gpu(), check_java(), check_sbt(), check_chisel_cache(),
               check_tool("verilator", ["verilator", "--version"], OSS,
                          pattern=r"Verilator (\d+\.\d+)"),
               check_tool("g++", ["g++", "--version"],
                          "compilatore C++ per Verilator: di solito già presente; "
                          "altrimenti `conda install -c conda-forge gxx` senza sudo"),
               check_tool("make", ["make", "--version"],
                          "`conda install -c conda-forge make` senza sudo"),
               check_tool("yosys", ["yosys", "-V"], OSS, required=False,
                          pattern=r"Yosys ([\d.+]+)"),
               check_tool("nextpnr-ecp5", ["nextpnr-ecp5", "--version"], OSS, required=False,
                          pattern=r"Version ([^\s)]+)")]
    v = next((c for c in checks if c.name == "verilator" and c.ok), None)
    if v and re.match(r"[0-4]\.", v.detail):
        v.ok, v.hint = False, "serve Verilator >= 5 per il testbench generato. " + OSS
    if outdir is not None:
        problems = check_path_sanity(outdir)
        checks.append(Check("percorso di output", not problems,
                            "; ".join(problems) or str(Path(outdir).resolve()),
                            "usa una cartella senza accenti né spazi (-o ...)", required=False))
    return checks


def format_checks(checks: list[Check]) -> str:
    lines = []
    for c in checks:
        mark = "✔" if c.ok else ("✘" if c.required else "!")
        lines.append(f"  {mark} {c.name:<22} {c.detail}")
        if not c.ok and c.hint:
            lines.append(f"      → {c.hint}")
    return "\n".join(lines)
