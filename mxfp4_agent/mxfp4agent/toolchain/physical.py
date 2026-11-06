"""Metriche fisiche riproducibili: area (ECP5 e generica) e timing multi-seed.

Flusso, dopo che `run_synth` ha prodotto la netlist ECP5 del DUT:

1. **sintesi generica** (`synth -flatten` + `stat -tech cmos`): celle logiche
   elementari e transistor stimati, indipendenti dalla tecnologia, per
   confrontare design fra loro;
2. **wrapper a registri** (`regwrap.py`) sintetizzato con `synth_ecp5`;
3. **place & route** con nextpnr-ecp5 su N seed (default 5) del wrapper →
   Fmax registro→DUT→registro: mediana, minimo, massimo;
4. **DUT da solo** (se i pin bastano) sugli stessi seed → per un design
   combinatorio il ritardo pin→pin, per uno sequenziale la Fmax "nuda".

Risultati in `physical.json` (dati) e `physical.md` (tabella per la tesi).
Le metriche fisiche sono INFORMATIVE: non fanno mai fallire la pipeline.
"""
from __future__ import annotations

import json
import os
import re
import statistics
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .regwrap import WRAPPER_NAME, dut_ports, io_pins, render_wrapper
from .shell import run, which
from .stage import StageResult
from .synth import DEVICE_ARGS, DEVICE_NAME, parse_yosys_stat

DEVICE_IO = 365          # TRELLIS_IO disponibili su LFE5U-85F CABGA381 (report di nextpnr)


# ------------------------------------------------------------------ utilità
def tool_versions() -> dict:
    out = {}
    if which("yosys"):
        v = run(["yosys", "-V"], timeout=30).stdout.strip()
        m = re.search(r"Yosys\s+([\w.+-]+)", v)
        out["yosys"] = m.group(1) if m else v[:40]
    if which("nextpnr-ecp5"):
        r = run(["nextpnr-ecp5", "--version"], timeout=30)
        m = re.search(r"Version\s+([^\s)]+)", r.stdout + r.stderr)
        out["nextpnr-ecp5"] = m.group(1) if m else (r.stdout + r.stderr).strip()[:80]
    return out


def stats(values: list[float]) -> dict:
    if not values:
        return {"n": 0}
    return {"n": len(values), "median": round(statistics.median(values), 2),
            "min": round(min(values), 2), "max": round(max(values), 2),
            "values": [round(v, 2) for v in values]}


def _clean_clock(name: str) -> str:
    return re.sub(r"^\$glbnet\$|\$TRELLIS_IO_IN$", "", name)


def parse_nextpnr_report(rep: dict) -> dict:
    """Fmax per clock e ritardo massimo pin→pin dal report JSON di nextpnr (>= 0.4)."""
    fmax = {_clean_clock(k): {"fmax_mhz": round(float(v["achieved"]), 2),
                              "target_mhz": float(v.get("constraint", 0)),
                              "met": float(v["achieved"]) >= float(v.get("constraint", 0))}
            for k, v in (rep.get("fmax") or {}).items()}
    async_ns = [sum(float(x.get("delay", 0)) for x in cp.get("path", []))
                for cp in rep.get("critical_paths", [])
                if cp.get("from") == "<async>" and cp.get("to") == "<async>"]
    util = {k: v for k, v in (rep.get("utilization") or {}).items() if v.get("used")}
    return {"clocks": fmax, "pad_to_pad_ns": round(max(async_ns), 3) if async_ns else None,
            "utilization": util}


# ----------------------------------------------------------- sintesi generica
def run_generic_synth(root: Path, module: str, rtl_files: list[Path], timeout: int = 900) -> dict:
    """`synth -flatten -noabc` indipendente dalla tecnologia: celle per tipo e transistor stimati.

    Senza ABC: con la minimizzazione logica di ABC il dot-product a 32 elementi
    richiede oltre 10 minuti (contro ~1 s) e il risultato dipende dalla versione
    di ABC. Il conteggio è quello delle porte dopo le ottimizzazioni di Yosys
    (opt, techmap): un indicatore di area coerente fra design, non un minimo.
    """
    out = Path(root) / "synth"
    out.mkdir(parents=True, exist_ok=True)
    stat_json = out / "generic_stat.json"
    stat_json.unlink(missing_ok=True)
    script = "; ".join([
        "read_verilog -sv " + " ".join(f'"{f}"' for f in rtl_files),
        f"synth -flatten -noabc -top {module}",
        f"tee -q -o {stat_json} stat -json -tech cmos",
    ])
    r = run(["yosys", "-q", "-l", str(out / "generic.log"), "-p", script], cwd=root,
            timeout=timeout)
    if not r.ok or not stat_json.exists():
        return {"error": (r.stderr or r.stdout)[-400:]}
    j = json.loads(stat_json.read_text(encoding="utf-8"))
    mods = j.get("modules", {})
    mod = mods.get(f"\\{module}") or mods.get(module) or next(iter(mods.values()), {})
    tr = str(mod.get("estimated_num_transistors", "")).rstrip("+")
    return {"cells": int(mod.get("num_cells", 0)),
            "by_type": dict(sorted((mod.get("num_cells_by_type") or {}).items())),
            "transistors_cmos": int(tr) if tr.isdigit() else None,
            "flow": "yosys synth -flatten -noabc; stat -tech cmos"}


# ------------------------------------------------------------------ P&R
def run_pnr(netlist: Path, out_dir: Path, tag: str, seed: int, target_mhz: float,
            timeout: int = 900) -> dict:
    log, rep = out_dir / f"pnr_{tag}_s{seed}.log", out_dir / f"pnr_{tag}_s{seed}.json"
    for f in (log, rep):
        f.unlink(missing_ok=True)
    r = run(["nextpnr-ecp5", *DEVICE_ARGS, "--json", str(netlist), "--freq", f"{target_mhz:g}",
             "--seed", str(seed), "--lpf-allow-unconstrained", "--timing-allow-fail",
             "--report", str(rep), "-l", str(log)], cwd=out_dir, timeout=timeout)
    if not r.ok or not rep.exists():
        tail = "\n".join((r.log).splitlines()[-8:])
        return {"seed": seed, "ok": False, "error": tail[-600:]}
    return {"seed": seed, "ok": True,
            **parse_nextpnr_report(json.loads(rep.read_text(encoding="utf-8")))}


def _pnr_many(netlist: Path, out_dir: Path, tag: str, seeds: list[int], target_mhz: float,
              timeout: int) -> list[dict]:
    workers = max(1, min(len(seeds), (os.cpu_count() or 2) // 2))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(lambda s: run_pnr(netlist, out_dir, tag, s, target_mhz, timeout),
                           seeds))


def _clock_fmax(r: dict, prefer: tuple[str, ...]) -> float | None:
    clocks = r.get("clocks") or {}
    for name in prefer:
        if name in clocks:
            return clocks[name]["fmax_mhz"]
    return next((c["fmax_mhz"] for c in clocks.values()), None)


# --------------------------------------------------------------- flusso
def run_physical(root: Path, module: str, rtl_files: list[Path], dut_netlist: Path | None,
                 target_mhz: float = 100.0, seeds: int = 5, plan: dict | None = None,
                 timeout: int = 900) -> StageResult:
    root = Path(root)
    if not which("nextpnr-ecp5") or not which("yosys"):
        return StageResult("implement", True,
                           "`nextpnr-ecp5`/`yosys` non trovati nel PATH: timing non calcolato.",
                           skipped=True)
    if dut_netlist is None or not Path(dut_netlist).exists():
        return StageResult("implement", True, "Netlist Yosys assente.", skipped=True)
    out = root / "synth"
    plan = plan or {}
    seed_list = list(range(1, max(1, seeds) + 1))
    phys: dict = {"device": DEVICE_NAME, "target_mhz": target_mhz, "seeds": seed_list,
                  "tools": tool_versions(),
                  "design": {"module": module, "clocking": plan.get("clocking"),
                             "latency_cycles": plan.get("latency_cycles"),
                             "kernel": (plan.get("test_plan") or {}).get("kernel")}}

    # 1. area: ECP5 (dalla sintesi del DUT già fatta) e generica
    try:
        phys["area_ecp5"] = parse_yosys_stat(
            json.loads((out / "yosys_stat.json").read_text(encoding="utf-8")), module)
    except Exception as e:  # noqa: BLE001
        phys["area_ecp5"] = {"_parse_error": str(e)[:200]}
    phys["area_ecp5"]["flow"] = "yosys synth_ecp5 (solo DUT)"
    phys["area_generic"] = run_generic_synth(root, module, rtl_files, timeout)

    # 2. wrapper a registri
    ports = dut_ports(json.loads(Path(dut_netlist).read_text(encoding="utf-8")), module)
    wrapper_v, winfo = render_wrapper(module, ports)
    (out / "regwrap.v").write_text(wrapper_v, encoding="utf-8")
    wnet, wstat = out / "netlist_wrap.json", out / "wrap_stat.json"
    for f in (wnet, wstat):
        f.unlink(missing_ok=True)
    script = "; ".join([
        "read_verilog -sv " + " ".join(f'"{f}"' for f in [*rtl_files, out / "regwrap.v"]),
        f"synth_ecp5 -top {WRAPPER_NAME} -json {wnet}",
        f"tee -q -o {wstat} stat -json -top {WRAPPER_NAME}",
    ])
    r = run(["yosys", "-q", "-l", str(out / "yosys_wrap.log"), "-p", script], cwd=root,
            timeout=timeout)
    if r.ok and wnet.exists():
        try:
            winfo["area_ecp5"] = parse_yosys_stat(
                json.loads(wstat.read_text(encoding="utf-8")), WRAPPER_NAME)
        except Exception:  # noqa: BLE001
            pass
        runs = _pnr_many(wnet, out, "wrap", seed_list, target_mhz, timeout)
        fm = [f for f in (_clock_fmax(x, ("clk",)) for x in runs if x["ok"]) if f is not None]
        phys["fmax_mhz"] = {
            "what": "Fmax registro->DUT->registro (wrapper con ingressi e uscite registrati, "
                    "4 pin): ritardo intrinseco del datapath",
            **stats(fm), "met_target": bool(fm) and statistics.median(fm) >= target_mhz,
            "failed_seeds": [x["seed"] for x in runs if not x["ok"]]}
        best = next((x for x in runs if x["ok"]), None)
        phys["utilization"] = best.get("utilization", {}) if best else {}
        phys["runs_wrapper"] = runs
    else:
        phys["fmax_mhz"] = {"what": "Fmax con wrapper", "n": 0,
                            "error": "sintesi del wrapper fallita: " + r.log[-400:]}
    phys["wrapper"] = winfo

    # 3. DUT da solo (pin non vincolati), se i pin bastano
    pins = io_pins(ports)
    comb = plan.get("clocking", "combinational") != "sequential"
    if pins > DEVICE_IO:
        phys["direct"] = {"skipped": f"il DUT richiede {pins} pin, il dispositivo ne ha "
                                     f"{DEVICE_IO}: misura possibile solo con il wrapper"}
    else:
        runs = _pnr_many(Path(dut_netlist), out, "dut", seed_list, target_mhz, timeout)
        ok = [x for x in runs if x["ok"]]
        if comb:
            vals = [x["pad_to_pad_ns"] for x in ok if x.get("pad_to_pad_ns") is not None]
            phys["direct"] = {"what": "ritardo combinatorio pin->pin del DUT senza registri; "
                                      "pin non vincolati, quindi dipende dal piazzamento",
                              "metric": "pad_to_pad_ns", "pins": pins, **stats(vals)}
        else:
            vals = [f for f in (_clock_fmax(x, ("clock", "clk")) for x in ok) if f is not None]
            phys["direct"] = {"what": "Fmax del DUT sequenziale piazzato da solo (i cammini "
                                      "pin->registro non sono vincolati)",
                              "metric": "fmax_mhz", "pins": pins, **stats(vals)}
        phys["direct"]["failed_seeds"] = [x["seed"] for x in runs if not x["ok"]]

    write_physical(root, phys)
    return StageResult("implement", True, summary_line(phys), detail=phys)


# -------------------------------------------------------------- report
def summary_line(p: dict) -> str:
    parts = [f"nextpnr-ecp5 {p['device']}, {len(p['seeds'])} seed"]
    f = p.get("fmax_mhz", {})
    if f.get("n"):
        parts.append(f"Fmax (wrapper) mediana {f['median']} MHz [{f['min']}–{f['max']}]")
    d = p.get("direct", {})
    if d.get("n") and d.get("metric") == "pad_to_pad_ns":
        parts.append(f"pin→pin {d['median']} ns [{d['min']}–{d['max']}]")
    elif d.get("n"):
        parts.append(f"Fmax DUT da solo {d['median']} MHz")
    return " — ".join(parts)


def render_markdown(p: dict) -> str:
    a, g, f, d, w = (p.get("area_ecp5", {}), p.get("area_generic", {}), p.get("fmax_mhz", {}),
                     p.get("direct", {}), p.get("wrapper", {}))
    des = p.get("design", {})
    tools = ", ".join(f"{k} {v}" for k, v in p.get("tools", {}).items())
    rows = []
    for cell in ("LUT4", "PFUMX", "L6MUX21", "CCU2C", "TRELLIS_FF", "MULT18X18D", "DP16KD",
                 "TRELLIS_DPR16X4", "ALU54B"):
        if a.get(cell):
            rows.append(f"| {cell} | {a[cell]} | `synth_ecp5`, solo DUT |")
    if a.get("_total_cells") is not None:
        rows.append(f"| Celle ECP5 totali | {a['_total_cells']} | `synth_ecp5`, solo DUT |")
    if g.get("cells") is not None:
        rows.append(f"| Celle logiche generiche | {g['cells']} | `synth -flatten -noabc`, "
                    "indipendente dalla tecnologia |")
    if g.get("transistors_cmos"):
        rows.append(f"| Transistor stimati | {g['transistors_cmos']} | `stat -tech cmos` |")

    def rng(s, unit):
        return (f"**{s['median']}** {unit} [{s['min']} – {s['max']}]" if s.get("n")
                else s.get("skipped") or s.get("error") or "n/d")

    rows.append(f"| Fmax (mediana [min – max]) | {rng(f, 'MHz')} | {f.get('what', '')} |")
    if d:
        unit = "ns" if d.get("metric") == "pad_to_pad_ns" else "MHz"
        label = ("Ritardo pin→pin (mediana [min – max])" if unit == "ns"
                 else "Fmax DUT senza wrapper (mediana [min – max])")
        rows.append(f"| {label} | {rng(d, unit)} | {d.get('what', '')} |")

    per_seed = ["| seed | Fmax wrapper (MHz) | " + ("pin→pin (ns)" if d.get("metric") == "pad_to_pad_ns"
                                                     else "Fmax DUT (MHz)") + " |", "|---|---|---|"]
    dv = dict(zip(p.get("seeds", []), d.get("values", []))) if d.get("n") == len(p.get("seeds", [])) else {}
    fv = dict(zip(p.get("seeds", []), f.get("values", []))) if f.get("n") == len(p.get("seeds", [])) else {}
    for s in p.get("seeds", []):
        per_seed.append(f"| {s} | {fv.get(s, '–')} | {dv.get(s, '–')} |")

    met = f.get("met_target")
    return f"""# Metriche fisiche — {des.get('module')}

Dispositivo **{p['device']}** · {tools} · vincolo {p['target_mhz']:g} MHz · {len(p.get('seeds', []))} seed di place & route.
Design: {des.get('kernel')}, {des.get('clocking')}{f", latenza {des.get('latency_cycles')} cicli" if des.get('clocking') == 'sequential' else ''}.

| Metrica | Valore | Come è misurata |
|---|---|---|
{chr(10).join(rows)}

Vincolo di {p['target_mhz']:g} MHz {'rispettato' if met else 'NON rispettato'} (sulla mediana).

**Come leggere i due numeri di timing.** La *Fmax* viene dal design racchiuso in un
wrapper generato automaticamente: ingressi caricati da una catena a scorrimento di
{w.get('in_bits', '?')} flip-flop, uscite registrate ({w.get('out_bits', '?')} flip-flop) e ridotte a XOR, 4 pin in
tutto. Il cammino critico è quindi registro → DUT → registro e non dipende dai pin.
Il *ritardo pin→pin* è quello del DUT piazzato da solo con pin non vincolati: include
il routing verso i pad scelti dal placer e varia molto con il seed. Per confrontare
datapath diversi usare la Fmax.

### Dettaglio per seed

{chr(10).join(per_seed)}
"""


def write_physical(root: Path, phys: dict) -> None:
    root = Path(root)
    (root / "physical.json").write_text(json.dumps(phys, indent=2, ensure_ascii=False),
                                        encoding="utf-8")
    (root / "physical.md").write_text(render_markdown(phys), encoding="utf-8")
