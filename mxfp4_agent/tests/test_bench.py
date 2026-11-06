"""bench.py: parsing delle richieste, estrazione delle metriche, CSV, ripresa."""
import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import bench  # noqa: E402

REPORT = {
    "ok": True, "message": "282 vettori superati", "module": "MXFP4ToBF16",
    "kernel": "to_bf16", "clocking": "combinational", "testbench": "template",
    "ports_overridden": ["out output", "in inputA"], "few_shot_reference": "04_to_bf16",
    "fix_rounds": 2, "first_failure_stage": "elaborate",
    "rounds": [{"round": 0, "summary": "✘ elaborate"}, {"round": 2, "summary": "✔ simulate"}],
    "timing_s": {"total": 900.5, "toolchain": 120.0, "planner": 300.0, "coder": 400.0,
                 "reviewer": 70.0, "tester": 10.5},
    "llm": {"coder": {"digest": "9ec8897f747e246e", "server_version": "0.34.3"}},
    "llm_stats": {"planner": {"calls": 1, "input_tokens": 1500, "output_tokens": 900},
                  "coder": {"calls": 3, "input_tokens": 9000, "output_tokens": 2500}},
    "physical": {"area_ecp5": {"LUT4": 700, "CCU2C": 0, "_total_cells": 900},
                 "area_generic": {"cells": 3000, "transistors_cmos": 20000},
                 "fmax_mhz": {"n": 5, "median": 210.5, "min": 200.1, "max": 215.0,
                              "met_target": True},
                 "direct": {"metric": "pad_to_pad_ns", "n": 5, "median": 30.2, "min": 28.0,
                            "max": 35.5}},
}


def test_parse_requests_from_the_examples_file():
    reqs = bench.parse_requests(ROOT / "examples" / "richieste.md")
    assert set(reqs) >= {1, 2, 3, 4}
    title, text = reqs[1]
    assert "combinatorio" in title.lower() and text.startswith("Unità di dot-product MXFP4")
    assert "\n" not in text and ">" not in text
    assert "BF16" in reqs[4][0]


def test_parse_seeds():
    assert bench.parse_seeds("3") == [1, 2, 3]
    assert bench.parse_seeds("2-4") == [2, 3, 4]
    assert bench.parse_seeds("1,5") == [1, 5]


def test_row_from_report():
    r = bench.row_from_report(REPORT)
    assert r["success"] and r["fix_rounds"] == 2 and r["first_failure_stage"] == "elaborate"
    assert (r["tokens_in"], r["tokens_out"], r["llm_calls"]) == (10500, 3400, 4)
    assert r["sim_vectors"] == 282 and r["ports_overridden"] == 2
    assert r["lut4"] == 700 and r["generic_cells"] == 3000
    assert (r["fmax_median_mhz"], r["pad_to_pad_median_ns"]) == (210.5, 30.2)
    assert r["t_coder_s"] == 400.0 and r["t_toolchain_s"] == 120.0
    assert r["model_digest"] == "9ec8897f747e" and r["final_stage"] == "✔ simulate"


def test_csv_summary_and_resume(tmp_path):
    rows = []
    for seed, ok in ((1, True), (2, False), (3, True)):
        d = tmp_path / "r4" / "qwen2.5-coder_14b" / f"s{seed}"
        d.mkdir(parents=True)
        row = {"request_id": 4, "request_title": "BF16", "model": "qwen2.5-coder:14b",
               "seed": seed, "status": "ok" if ok else "fail", "wall_s": 100.0 * seed,
               **bench.row_from_report({**REPORT, "ok": ok,
                                        "first_failure_stage": None if ok else "simulate"})}
        (d / "result.json").write_text(json.dumps(row), encoding="utf-8")
        rows.append(row)
    got = bench.refresh(tmp_path)
    assert [r["seed"] for r in got] == [1, 2, 3]
    with open(tmp_path / "bench.csv", encoding="utf-8") as fh:
        lines = list(csv.DictReader(fh))
    assert len(lines) == 3 and list(lines[0]) == bench.COLUMNS
    md = (tmp_path / "bench_summary.md").read_text(encoding="utf-8")
    assert "2/3 (67%)" in md and "| simulate | 1 |" in md

    # ripresa: con result.json presenti nessuna run viene rieseguita
    called = []
    orig = bench.run_one
    bench.run_one = lambda *a, **k: called.append(a) or {}
    try:
        rc = bench.main(["--requests", "4", "--models", "qwen2.5-coder:14b", "--seeds", "3",
                         "--out", str(tmp_path)])
    finally:
        bench.run_one = orig
    assert rc == 0 and called == []


def test_extra_args_after_double_dash(tmp_path):
    seen = {}
    orig = bench.run_one

    def fake(rid, title, text, model, seed, run_dir, extra, timeout, python):
        seen.update(rid=rid, model=model, seed=seed, extra=extra)
        row = {"request_id": rid, "request_title": title, "model": model, "seed": seed,
               "status": "ok", "success": True, "wall_s": 1.0}
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "result.json").write_text(json.dumps(row), encoding="utf-8")
        return row

    bench.run_one = fake
    try:
        bench.main(["--requests", "1", "--models", "m:1", "--seeds", "1", "--out", str(tmp_path),
                    "--", "--pnr-seeds", "3", "--no-static-review"])
    finally:
        bench.run_one = orig
    assert seen == {"rid": 1, "model": "m:1", "seed": 1,
                    "extra": ["--pnr-seeds", "3", "--no-static-review"]}
    assert (tmp_path / "r1" / "m_1" / "s1" / "result.json").exists()
    assert json.loads((tmp_path / "campaign.json").read_text(encoding="utf-8"))["seeds"] == [1]
