#!/usr/bin/env python3
"""Campagna sperimentale: {richieste} x {modelli Ollama} x {seed} -> CSV + riepilogo.

Ogni combinazione è una run completa di `run.py` in un sottoprocesso con il
proprio timeout e la propria cartella:

    <out>/r<id>/<modello>/s<seed>/
        run.log            stdout+stderr della run
        <Modulo>/...       progetto generato, report.json, physical.json/md
        result.json        riga del CSV (marcatore di run completata)

È riprendibile: una combinazione con `result.json` viene saltata (usa
--rerun-failed per ripetere quelle fallite per errore o timeout). Dopo ogni
run si riscrivono `bench.csv` e `bench_summary.md`, quindi un'interruzione non
perde niente.

    python bench.py --models qwen2.5-coder:14b --seeds 3
    python bench.py --requests 1,3,4 --models qwen2.5-coder:14b,qwen2.5-coder:32b --seeds 1-5 \\
                    --out bench_out -- --pnr-seeds 3 --max-fix-rounds 4
    python bench.py --summary-only --out bench_out

Gli argomenti dopo `--` passano invariati a run.py.

Prima di ogni run si aspetta che Ollama risponda (fino a --wait-ollama
secondi): se il demone è stato ucciso (OOM) e systemd lo sta riavviando, la
campagna attende invece di bruciare le run successive in errori da 0,1 s; se
non torna, la campagna si ferma e si riprende rilanciando lo stesso comando
con --rerun-failed. Una run interrotta perché il provider è caduto ha stato
`error` (infrastruttura), non `fail` (risultato del modello).
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from mxfp4agent.utils import force_utf8_stdio  # noqa: E402

#: richieste interamente verificabili dal golden model (vedi README, "Estensioni naturali")
DEFAULT_REQUESTS = "1,3,4"

COLUMNS = [
    "request_id", "request_title", "model", "seed", "status", "success", "module", "kernel",
    "clocking", "testbench", "fix_rounds", "first_failure_stage", "final_stage",
    "ports_overridden", "few_shot_reference",
    "tokens_in", "tokens_out", "llm_calls",
    "t_total_s", "t_planner_s", "t_coder_s", "t_reviewer_s", "t_tester_s", "t_toolchain_s",
    "sim_vectors", "lut4", "ccu2c", "pfumx", "l6mux21", "trellis_ff", "mult18x18d",
    "ecp5_cells", "generic_cells", "transistors_cmos",
    "fmax_median_mhz", "fmax_min_mhz", "fmax_max_mhz", "fmax_met",
    "pad_to_pad_median_ns", "pad_to_pad_min_ns", "pad_to_pad_max_ns",
    "model_digest", "ollama_version", "wall_s", "aborted", "error",
]


# ------------------------------------------------------------------ richieste
def parse_requests(path: Path) -> dict[int, tuple[str, str]]:
    """`## N. Titolo` seguito da righe `> testo` → {N: (titolo, testo)}."""
    out: dict[int, tuple[str, str]] = {}
    cur: int | None = None
    title = ""
    lines: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines() + ["## 0. fine"]:
        m = re.match(r"^##\s+(\d+)\.\s+(.+?)\s*$", line)
        if m:
            if cur is not None and lines:
                out[cur] = (title, " ".join(lines))
            cur, title, lines = int(m.group(1)), m.group(2), []
        elif cur is not None and line.startswith(">"):
            lines.append(line.lstrip("> ").strip())
    return out


def parse_seeds(spec: str) -> list[int]:
    """`3` → 1..3; `2-5` → 2..5; `1,4,7` → [1,4,7]."""
    if re.fullmatch(r"\d+", spec):
        return list(range(1, int(spec) + 1))
    if re.fullmatch(r"\d+-\d+", spec):
        a, b = map(int, spec.split("-"))
        return list(range(a, b + 1))
    return [int(x) for x in spec.split(",") if x.strip()]


def model_dir(model: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", model)


# ------------------------------------------------------------------ ollama
def ollama_host(extra: list[str]) -> str:
    """Host di Ollama come lo vedrà run.py: --host dopo `--`, poi OLLAMA_HOST."""
    for i, a in enumerate(extra):
        if a == "--host" and i + 1 < len(extra):
            return extra[i + 1]
        if a.startswith("--host="):
            return a.split("=", 1)[1]
    host = os.environ.get("OLLAMA_HOST") or "http://localhost:11434"
    return host if "://" in host else f"http://{host}"


def uses_ollama(extra: list[str]) -> bool:
    """False se gli argomenti per run.py scelgono il provider mock."""
    for i, x in enumerate(extra):
        if x == "--provider" and i + 1 < len(extra):
            return extra[i + 1] != "mock"
        if x.startswith("--provider="):
            return x.split("=", 1)[1] != "mock"
    return True


def ollama_up(host: str, timeout: float = 5.0) -> bool:
    try:
        with urllib.request.urlopen(host.rstrip("/") + "/api/version", timeout=timeout) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def wait_for_ollama(host: str, max_wait: float, poll: float = 5.0,
                    probe=None, sleep=time.sleep) -> bool:
    """True appena Ollama risponde; False se non risponde entro `max_wait` secondi."""
    probe = probe or ollama_up
    waited = 0.0
    while True:
        if probe(host):
            return True
        if waited >= max_wait:
            return False
        if waited == 0:
            print(f"    Ollama non risponde su {host}: attendo fino a {max_wait:.0f}s…",
                  flush=True)
        sleep(poll)
        waited += poll


# ------------------------------------------------------------- estrazione
def row_from_report(rep: dict) -> dict:
    """Colonne del CSV da un report.json della pipeline."""
    phys = rep.get("physical") or {}
    a = phys.get("area_ecp5") or {}
    g = phys.get("area_generic") or {}
    f = phys.get("fmax_mhz") or {}
    d = phys.get("direct") or {}
    stats = rep.get("llm_stats") or {}
    t = rep.get("timing_s") or {}
    rounds = rep.get("rounds") or []
    m = re.search(r"(\d+) vettori superati", rep.get("message") or "")
    sim_vectors = int(m.group(1)) if m else None
    llm = rep.get("llm") or {}
    coder = llm.get("coder") or next(iter(llm.values()), {})
    pad = d if d.get("metric") == "pad_to_pad_ns" else {}
    return {
        "success": bool(rep.get("ok")),
        "module": rep.get("module"),
        "kernel": rep.get("kernel"),
        "clocking": rep.get("clocking"),
        "testbench": rep.get("testbench"),
        "fix_rounds": rep.get("fix_rounds"),
        "first_failure_stage": rep.get("first_failure_stage") or "",
        "final_stage": rounds[-1]["summary"] if rounds else "",
        "ports_overridden": len(rep.get("ports_overridden") or []),
        "few_shot_reference": rep.get("few_shot_reference") or "",
        "tokens_in": sum(s.get("input_tokens", 0) for s in stats.values()),
        "tokens_out": sum(s.get("output_tokens", 0) for s in stats.values()),
        "llm_calls": sum(s.get("calls", 0) for s in stats.values()),
        "t_total_s": t.get("total"), "t_planner_s": t.get("planner"),
        "t_coder_s": t.get("coder"), "t_reviewer_s": t.get("reviewer"),
        "t_tester_s": t.get("tester"), "t_toolchain_s": t.get("toolchain"),
        "sim_vectors": sim_vectors,
        "lut4": a.get("LUT4"), "ccu2c": a.get("CCU2C"), "pfumx": a.get("PFUMX"),
        "l6mux21": a.get("L6MUX21"), "trellis_ff": a.get("TRELLIS_FF"),
        "mult18x18d": a.get("MULT18X18D"), "ecp5_cells": a.get("_total_cells"),
        "generic_cells": g.get("cells"), "transistors_cmos": g.get("transistors_cmos"),
        "fmax_median_mhz": f.get("median"), "fmax_min_mhz": f.get("min"),
        "fmax_max_mhz": f.get("max"), "fmax_met": f.get("met_target"),
        "pad_to_pad_median_ns": pad.get("median"), "pad_to_pad_min_ns": pad.get("min"),
        "pad_to_pad_max_ns": pad.get("max"),
        "model_digest": (coder.get("digest") or "")[:12],
        "ollama_version": coder.get("server_version") or "",
        "aborted": rep.get("aborted") or "",
    }


def run_one(rid: int, title: str, text: str, model: str, seed: int, run_dir: Path,
            extra: list[str], timeout: int, python: str) -> dict:
    run_dir.mkdir(parents=True, exist_ok=True)
    cmd = [python, str(ROOT / "run.py"), text, "--model", model, "--seed", str(seed),
           "-o", str(run_dir), *extra]
    row = {"request_id": rid, "request_title": title, "model": model, "seed": seed}
    t0 = time.time()
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    with open(run_dir / "run.log", "w", encoding="utf-8") as log:
        log.write("$ " + " ".join(cmd) + "\n\n")
        log.flush()
        try:
            p = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT, timeout=timeout,
                               env=env, cwd=ROOT)
            status = "ok" if p.returncode == 0 else "fail"
        except subprocess.TimeoutExpired:
            status = "timeout"
    row["wall_s"] = round(time.time() - t0, 1)
    reports = sorted(run_dir.glob("*/report.json"), key=lambda q: q.stat().st_mtime)
    if reports:
        row.update(row_from_report(json.loads(reports[-1].read_text(encoding="utf-8"))))
        row["status"] = status if status != "ok" or row["success"] else "fail"
        if row["aborted"] and status != "timeout":
            row["status"] = "error"          # infrastruttura, non risultato del modello
            row["error"] = row["aborted"]
    else:
        tail = (run_dir / "run.log").read_text(encoding="utf-8", errors="replace")[-600:]
        row.update({"status": "timeout" if status == "timeout" else "error", "success": False,
                    "error": " ".join(tail.split())[-300:]})
    if status == "timeout":
        row["error"] = f"timeout dopo {timeout}s"
    (run_dir / "result.json").write_text(json.dumps(row, indent=2, ensure_ascii=False),
                                         encoding="utf-8")
    return row


# ---------------------------------------------------------------- report
def collect(out: Path) -> list[dict]:
    rows = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(out.glob("r*/*/s*/result.json"))]
    return sorted(rows, key=lambda r: (r["request_id"], r["model"], r["seed"]))


def write_csv(rows: list[dict], path: Path) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in COLUMNS})


def _med(vals):
    vals = [v for v in vals if isinstance(v, (int, float))]
    return round(statistics.median(vals), 1) if vals else None


def summarize(rows: list[dict]) -> str:
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        groups[(r["request_id"], r.get("request_title", ""), r["model"])].append(r)
    lines = ["# Riepilogo della campagna", "",
             f"{len(rows)} run, {sum(1 for r in rows if r.get('success'))} superate.", "",
             "| richiesta | modello | run | successo | round di fix (mediana, ok) | "
             "token in/out (mediana) | tempo (mediana, s) | LUT4 (mediana, ok) | "
             "Fmax MHz (mediana, ok) |",
             "|---|---|---|---|---|---|---|---|---|"]
    for (rid, title, model), rs in sorted(groups.items()):
        ok = [r for r in rs if r.get("success")]
        rate = f"{len(ok)}/{len(rs)} ({100 * len(ok) / len(rs):.0f}%)"
        lines.append(
            f"| {rid}. {title} | {model} | {len(rs)} | {rate} | "
            f"{_med(r.get('fix_rounds') for r in ok) if ok else '–'} | "
            f"{_med(r.get('tokens_in') for r in rs)}/{_med(r.get('tokens_out') for r in rs)} | "
            f"{_med(r.get('wall_s') for r in rs)} | "
            f"{_med(r.get('lut4') for r in ok) if ok else '–'} | "
            f"{_med(r.get('fmax_median_mhz') for r in ok) if ok else '–'} |")

    by_model: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_model[r["model"]].append(r)
    lines += ["", "## Per modello", "",
              "| modello | run | successo | round di fix (mediana, ok) | tempo per run (mediana, s) |",
              "|---|---|---|---|---|"]
    for model, rs in sorted(by_model.items()):
        ok = [r for r in rs if r.get("success")]
        lines.append(f"| {model} | {len(rs)} | {len(ok)}/{len(rs)} "
                     f"({100 * len(ok) / len(rs):.0f}%) | "
                     f"{_med(r.get('fix_rounds') for r in ok) if ok else '–'} | "
                     f"{_med(r.get('wall_s') for r in rs)} |")

    first = Counter(r.get("first_failure_stage") or "(nessuno)" for r in rows)
    lines += ["", "## Fase del primo fallimento", "",
              "| fase | run |", "|---|---|"]
    lines += [f"| {k} | {v} |" for k, v in first.most_common()]
    status = Counter(r.get("status") for r in rows)
    lines += ["", "Esiti: " + ", ".join(f"{k} {v}" for k, v in status.most_common()), ""]
    return "\n".join(lines)


def refresh(out: Path) -> list[dict]:
    rows = collect(out)
    write_csv(rows, out / "bench.csv")
    (out / "bench_summary.md").write_text(summarize(rows), encoding="utf-8")
    return rows


# ------------------------------------------------------------------- main
def main(argv: list[str] | None = None) -> int:
    force_utf8_stdio()
    argv = list(sys.argv[1:] if argv is None else argv)
    extra: list[str] = []
    if "--" in argv:
        i = argv.index("--")
        argv, extra = argv[:i], argv[i + 1:]
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--requests", default=DEFAULT_REQUESTS,
                    help=f"id delle richieste in examples/richieste.md (default {DEFAULT_REQUESTS})")
    ap.add_argument("--requests-file", type=Path, default=ROOT / "examples" / "richieste.md")
    ap.add_argument("--models", default="qwen2.5-coder:14b", help="lista separata da virgole")
    ap.add_argument("--seeds", default="3", help="N (=1..N), A-B oppure lista: 1,4,7")
    ap.add_argument("--out", type=Path, default=Path("bench_out"))
    ap.add_argument("--timeout", type=int, default=4 * 3600, help="timeout per run (s)")
    ap.add_argument("--rerun-failed", action="store_true",
                    help="ripeti le run concluse con errore o timeout (non quelle fallite "
                         "in verifica: sono un risultato)")
    ap.add_argument("--wait-ollama", type=float, default=600,
                    help="secondi di attesa di Ollama prima di ogni run; se non risponde "
                         "la campagna si ferma (default 600)")
    ap.add_argument("--summary-only", action="store_true", help="rigenera CSV e riepilogo")
    ap.add_argument("--dry-run", action="store_true", help="elenca le run senza eseguirle")
    args = ap.parse_args(argv)

    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    if args.summary_only:
        rows = refresh(out)
        print((out / "bench_summary.md").read_text(encoding="utf-8"))
        return 0 if rows else 1

    reqs = parse_requests(args.requests_file)
    ids = [int(x) for x in args.requests.split(",") if x.strip()]
    missing = [i for i in ids if i not in reqs]
    if missing:
        print(f"richieste inesistenti: {missing} (disponibili: {sorted(reqs)})", file=sys.stderr)
        return 2
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    seeds = parse_seeds(args.seeds)
    plan = [(rid, m, s) for rid in ids for m in models for s in seeds]
    (out / "campaign.json").write_text(json.dumps(
        {"requests": {i: reqs[i][0] for i in ids}, "models": models, "seeds": seeds,
         "extra_args": extra, "timeout": args.timeout}, indent=2, ensure_ascii=False),
        encoding="utf-8")

    print(f"campagna: {len(ids)} richieste x {len(models)} modelli x {len(seeds)} seed "
          f"= {len(plan)} run -> {out}")
    done = 0
    host = ollama_host(extra)
    stopped = False
    for n, (rid, model, seed) in enumerate(plan, 1):
        run_dir = out / f"r{rid}" / model_dir(model) / f"s{seed}"
        res = run_dir / "result.json"
        if res.exists():
            prev = json.loads(res.read_text(encoding="utf-8"))
            if not (args.rerun_failed and prev.get("status") in ("error", "timeout")):
                print(f"[{n}/{len(plan)}] r{rid} {model} s{seed}: già fatta "
                      f"({prev.get('status')}) — salto")
                continue
        if args.dry_run:
            print(f"[{n}/{len(plan)}] r{rid} {model} s{seed}: da eseguire")
            continue
        if uses_ollama(extra) and not wait_for_ollama(host, args.wait_ollama):
            print(f"[{n}/{len(plan)}] Ollama non raggiungibile su {host} dopo "
                  f"{args.wait_ollama:.0f}s: campagna sospesa. Rilancia lo stesso comando "
                  f"con --rerun-failed quando il servizio è di nuovo attivo.", flush=True)
            stopped = True
            break
        title, text = reqs[rid]
        print(f"[{n}/{len(plan)}] r{rid} {model} s{seed}: avvio…", flush=True)
        row = run_one(rid, title, text, model, seed, run_dir, extra, args.timeout,
                      sys.executable)
        done += 1
        print(f"    -> {row['status']} (fix {row.get('fix_rounds')}, primo fallimento "
              f"{row.get('first_failure_stage') or '-'}, {row['wall_s']}s, "
              f"Fmax {row.get('fmax_median_mhz')} MHz)", flush=True)
        refresh(out)

    rows = refresh(out)
    print(f"\n{done} run eseguite ora, {len(rows)} totali. CSV: {out / 'bench.csv'}")
    print((out / "bench_summary.md").read_text(encoding="utf-8"))
    return 3 if stopped else 0


if __name__ == "__main__":
    raise SystemExit(main())
