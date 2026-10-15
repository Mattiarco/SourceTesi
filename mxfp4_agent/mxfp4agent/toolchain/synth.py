"""Fasi fisiche: sintesi (Yosys) e implementation con timing (nextpnr-ecp5).

La simulazione Verilator è puramente funzionale: dice che l'RTL calcola il
risultato giusto, non che sia sintetizzabile, né quanto costi, né a che
frequenza giri. Queste due fasi chiudono il buco:

* ``synth``     Yosys (``synth_ecp5``). Fallisce se l'RTL non è sintetizzabile
                (errore di parsing, latch inferito, anello combinatorio,
                più driver). Estrae le risorse mappate: LUT4, FF, CCU2C
                (catene di carry), moltiplicatori MULT18X18D, RAM.
* ``implement`` nextpnr-ecp5 (place & route) sul netlist di Yosys. Estrae
                Fmax o ritardo massimo e l'utilizzo post-P&R. È
                INFORMATIVA: un vincolo di frequenza mancato o un design che
                non entra nel dispositivo non è un bug da riparare, è un
                risultato da riportare. Non fa mai fallire la catena.

Dispositivo di riferimento: Lattice LFE5U-85F (CABGA381, speed grade 6). È un
FPGA open-source-friendly con LUT4, carry chain e DSP 18x18, abbastanza
grande per le unità MXFP4 a 32 elementi.

Avvertenza sui design puramente combinatori: non c'è un clock, quindi non
esiste una Fmax. Si riporta il ritardo massimo ingresso→uscita, che nextpnr
calcola includendo il routing dai pad (non vincolati, quindi piazzati a
discrezione del tool): è una stima pessimistica e dipende dal seed.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .shell import run, which
from .stage import StageResult

DEVICE_ARGS = ["--85k", "--package", "CABGA381", "--speed", "6"]
DEVICE_NAME = "LFE5U-85F-6BG381"

# Celle ECP5 che contano come risorse, nell'ordine in cui le riportiamo.
RESOURCE_CELLS = ("LUT4", "PFUMX", "L6MUX21", "CCU2C", "TRELLIS_FF", "TRELLIS_DPR16X4",
                  "DP16KD", "MULT18X18D", "ALU54B")

_PACKED_ARRAY_HINT = (
    "\n[synth] Suggerimento: il frontend Verilog di Yosys non legge gli array "
    "impacchettati (`wire [7:0][3:0] x`) emessi da firtool. Se l'RTL è generato "
    "da Chisel, lo scaffold passa `--lowering-options=disallowPackedArrays`; se "
    "è scritto a mano, usa vettori piatti con indicizzazione `x[i*4 +: 4]`.")


def _clean_yosys(log: str, limit: int = 5000) -> str:
    """Tiene le righe utili di un log Yosys (ERROR, Warning, Latch, loop)."""
    keep = [ln for ln in log.splitlines()
            if re.search(r"ERROR|Warning|[Ll]atch inferred|[Ll]ogic loop|multiple conflicting"
                         r"|drivers|problems", ln)]
    out = "\n".join(dict.fromkeys(keep)) or log
    return out[-limit:]


def parse_yosys_stat(stat_json: dict, top: str) -> dict:
    """Estrae il conteggio delle celle del modulo top da `stat -json`."""
    mods = stat_json.get("modules", {})
    mod = mods.get(top) or mods.get(f"\\{top}") or (next(iter(mods.values())) if mods else {})
    # Il nome della chiave cambia tra versioni di Yosys (num_cells_by_type in 0.33).
    cells = mod.get("num_cells_by_type") or mod.get("cells_by_type") or {}
    res = {c: int(cells.get(c, 0)) for c in RESOURCE_CELLS if cells.get(c, 0)}
    res["_total_cells"] = int(mod.get("num_cells", sum(cells.values()) if cells else 0))
    return res


def parse_nextpnr_log(log: str) -> dict:
    """Estrae Fmax e ritardi massimi dall'ultimo report (post-routing) di nextpnr."""
    # Le righe compaiono sia dopo il placement sia dopo il routing: l'ultima
    # occorrenza per ogni dominio è il valore finale.
    fmax: dict[str, dict] = {}
    for m in re.finditer(
            r"Max frequency for clock\s+'([^']+)':\s*([\d.]+)\s*MHz\s*\((PASS|FAIL) at ([\d.]+) MHz\)",
            log):
        name = re.sub(r"^\$glbnet\$(.+?)\$TRELLIS_IO_IN$", r"\1", m.group(1))
        fmax[name] = {"fmax_mhz": float(m.group(2)),
                            "target_mhz": float(m.group(4)),
                            "met": m.group(3) == "PASS"}
    delays: dict[str, float] = {}
    for m in re.finditer(r"Max delay\s+(\S+)\s+->\s+(\S+):\s*([\d.]+)\s*ns", log):
        delays[f"{m.group(1)} -> {m.group(2)}"] = float(m.group(3))
    return {"clocks": fmax, "max_delays_ns": delays}


def run_synth(root: Path, module: str, rtl_files: list[Path], timeout: int = 900) -> StageResult:
    """Sintesi Yosys. Scrive netlist.json, yosys_stat.json e yosys.log in synth/."""
    if not which("yosys"):
        return StageResult("synth", True,
                           "`yosys` non trovato nel PATH: sintesi non eseguita "
                           "(apt install yosys | brew install yosys).", skipped=True)
    if not rtl_files:
        return StageResult("synth", False, "Nessun file RTL da sintetizzare.")
    out = Path(root) / "synth"
    out.mkdir(parents=True, exist_ok=True)
    netlist, stat_json, ylog = out / "netlist.json", out / "yosys_stat.json", out / "yosys.log"
    for f in (netlist, stat_json, ylog):
        f.unlink(missing_ok=True)

    script = "; ".join([
        "read_verilog -sv " + " ".join(f'"{f}"' for f in rtl_files),
        f"hierarchy -check -top {module}",
        f"synth_ecp5 -top {module} -json {netlist}",
        "check",
        f"tee -q -o {stat_json} stat -json -top {module}",
        f"tee -o {out / 'yosys_stat.txt'} stat -top {module}",
    ])
    r = run(["yosys", "-l", str(ylog), "-p", script], cwd=root, timeout=timeout)
    full = ylog.read_text(encoding="utf-8", errors="replace") if ylog.exists() else r.log

    if not r.ok or not netlist.exists():
        hint = _PACKED_ARRAY_HINT if "unexpected '['" in r.log + full else ""
        return StageResult("synth", False,
                           f"$ yosys -p \"{script[:200]}…\"\n{_clean_yosys(r.log + chr(10) + full)}{hint}",
                           detail={"error": "yosys"})

    # Problemi di sintetizzabilità che Yosys segnala come warning, non come errore.
    problems = []
    if re.search(r"[Ll]atch inferred", full):
        problems.append("latch inferito: un process combinatorio non assegna tutte le uscite "
                        "in tutti i rami (o manca l'`else`).")
    m = re.search(r"[Ff]ound and reported\s+(\d+)\s+problems?", full)
    if m and int(m.group(1)) > 0:
        problems.append(f"`check` di Yosys ha riportato {m.group(1)} problemi "
                        f"(anello combinatorio, driver multipli o segnali non guidati).")
    if problems:
        return StageResult("synth", False,
                           "RTL non sintetizzabile in modo pulito:\n- " + "\n- ".join(problems)
                           + "\n\n" + _clean_yosys(full),
                           detail={"error": "synthesizability"})

    try:
        resources = parse_yosys_stat(json.loads(stat_json.read_text(encoding="utf-8")), module)
    except Exception as e:  # stat illeggibile: la sintesi è ok, mancano solo i numeri
        resources = {"_parse_error": str(e)[:200]}
    return StageResult("synth", True, f"yosys synth_ecp5 ok — {resources}",
                       detail={"resources_yosys": resources, "netlist": str(netlist)})


def run_implement(root: Path, module: str, netlist: Path | None, target_mhz: float = 100.0,
                  seed: int = 1, timeout: int = 900) -> StageResult:
    """Place & route con nextpnr-ecp5: Fmax / ritardo massimo e utilizzo.

    Informativa: ritorna sempre ok=True (vedi docstring del modulo).
    """
    if not which("nextpnr-ecp5"):
        return StageResult("implement", True,
                           "`nextpnr-ecp5` non trovato nel PATH: timing non calcolato "
                           "(apt install nextpnr-ecp5).", skipped=True)
    if netlist is None or not Path(netlist).exists():
        return StageResult("implement", True, "Netlist Yosys assente.", skipped=True)

    out = Path(root) / "synth"
    plog, prep = out / "nextpnr.log", out / "nextpnr_report.json"
    for f in (plog, prep):
        f.unlink(missing_ok=True)
    cmd = ["nextpnr-ecp5", *DEVICE_ARGS, "--json", str(netlist), "--freq", f"{target_mhz:g}",
           "--seed", str(seed), "--lpf-allow-unconstrained", "--timing-allow-fail",
           "--report", str(prep), "-l", str(plog)]
    r = run(cmd, cwd=root, timeout=timeout)
    log = plog.read_text(encoding="utf-8", errors="replace") if plog.exists() else r.log

    if not r.ok or not prep.exists():
        # Tipicamente: il design non entra nel dispositivo, o timeout. È un dato.
        tail = "\n".join((r.log + "\n" + log).splitlines()[-15:])
        return StageResult("implement", True, f"place&route non completato:\n{tail}",
                           detail={"status": "failed", "device": DEVICE_NAME})

    timing = parse_nextpnr_log(log)
    util = {}
    try:
        rep = json.loads(prep.read_text(encoding="utf-8"))
        util = {k: v for k, v in rep.get("utilization", {}).items() if v.get("used")}
    except Exception:
        pass
    sequential = bool(timing["clocks"])
    summary = (", ".join(f"{c}: {d['fmax_mhz']:.1f} MHz ({'ok' if d['met'] else 'NO'} a "
                         f"{d['target_mhz']:g})" for c, d in timing["clocks"].items())
               if sequential else
               "; ".join(f"{k}: {v:.2f} ns" for k, v in timing["max_delays_ns"].items())
               or "nessun cammino temporizzato")
    return StageResult("implement", True, f"nextpnr-ecp5 {DEVICE_NAME} — {summary}",
                       detail={"status": "ok", "device": DEVICE_NAME, "seed": seed,
                               "target_mhz": target_mhz, "sequential": sequential,
                               "timing": timing, "utilization": util})
