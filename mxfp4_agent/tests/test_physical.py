"""Wrapper a registri, parsing del report nextpnr e flusso fisico multi-seed."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mxfp4agent.toolchain import run_synth, which  # noqa: E402
from mxfp4agent.toolchain.physical import (parse_nextpnr_report, render_markdown,  # noqa: E402
                                           run_physical, stats)
from mxfp4agent.toolchain.regwrap import (WRAPPER_NAME, dut_ports, io_pins,  # noqa: E402
                                          render_wrapper)

COMB_PORTS = [("clock", "input", 1), ("reset", "input", 1), ("io_a", "input", 128),
              ("io_scaleA", "input", 8), ("io_out", "output", 512), ("io_isNaN", "output", 1)]

# Estratto reale del report JSON di nextpnr-ecp5 0.11 (design pipelined di riferimento)
NEXTPNR_REPORT = {
    "fmax": {"$glbnet$clock$TRELLIS_IO_IN": {"achieved": 83.16699981689453, "constraint": 100}},
    "critical_paths": [
        {"from": "posedge $glbnet$clock$TRELLIS_IO_IN", "to": "posedge $glbnet$clock$TRELLIS_IO_IN",
         "path": [{"delay": 5.0}, {"delay": 7.024}]},
        {"from": "<async>", "to": "posedge $glbnet$clock$TRELLIS_IO_IN", "path": [{"delay": 18.792}]},
    ],
    "utilization": {"TRELLIS_IO": {"used": 323, "available": 365},
                    "DP16KD": {"used": 0, "available": 208}},
}


def test_wrapper_connects_every_port():
    v, info = render_wrapper("MXFP4ToBF16", COMB_PORTS)
    assert f"module {WRAPPER_NAME}" in v and "MXFP4ToBF16 dut (" in v
    assert ".clock(clk)" in v and ".reset(rst)" in v
    assert ".io_a(in_q[127:0])" in v and ".io_scaleA(in_q[135:128])" in v
    assert ".io_out(out_d[511:0])" in v and ".io_isNaN(out_d[512:512])" in v
    assert "in_q <= {in_q[134:0], sin};" in v and "assign sout = ^out_q;" in v
    assert info["in_bits"] == 136 and info["out_bits"] == 513 and info["pins"] == 4


def test_wrapper_edge_cases():
    v, info = render_wrapper("Tiny", [("x", "input", 1), ("y", "output", 1)])
    assert "in_q_r <= sin" in v and ".x(in_q[0:0])" in v and info["dut_clock"] is None
    v, _ = render_wrapper("Const", [("y", "output", 4)])
    assert "in_q" not in v
    try:
        render_wrapper("NoOut", [("x", "input", 3)])
    except ValueError:
        pass
    else:
        raise AssertionError


def test_ports_from_yosys_netlist():
    net = {"modules": {"Foo": {"ports": {
        "clock": {"direction": "input", "bits": [2]},
        "io_a": {"direction": "input", "bits": [3, 4, 5, 6]},
        "io_y": {"direction": "output", "bits": [7, 8]}}}}}
    ports = dut_ports(net, "Foo")
    assert ports == [("clock", "input", 1), ("io_a", "input", 4), ("io_y", "output", 2)]
    assert io_pins(ports) == 7


def test_nextpnr_report_parsing():
    r = parse_nextpnr_report(NEXTPNR_REPORT)
    assert r["clocks"] == {"clock": {"fmax_mhz": 83.17, "target_mhz": 100.0, "met": False}}
    assert r["pad_to_pad_ns"] is None            # nessun cammino <async> -> <async>
    assert list(r["utilization"]) == ["TRELLIS_IO"]
    comb = {"fmax": {}, "critical_paths": [
        {"from": "<async>", "to": "<async>", "path": [{"delay": 8.77}, {"delay": 31.886}]}]}
    assert parse_nextpnr_report(comb)["pad_to_pad_ns"] == 40.656


def test_stats():
    s = stats([80.0, 85.5, 83.1, 79.9, 90.0])
    assert (s["n"], s["median"], s["min"], s["max"]) == (5, 83.1, 79.9, 90.0)
    assert stats([]) == {"n": 0}


def test_markdown_table_states_what_each_number_is():
    p = {"device": "LFE5U-85F-6BG381", "target_mhz": 100.0, "seeds": [1, 2],
         "tools": {"yosys": "Yosys 0.70"},
         "design": {"module": "M", "clocking": "combinational", "kernel": "dot_product"},
         "area_ecp5": {"LUT4": 1592, "MULT18X18D": 32, "_total_cells": 2123},
         "area_generic": {"cells": 8000, "transistors_cmos": 58604},
         "fmax_mhz": {"what": "Fmax registro->DUT->registro", **stats([70.0, 72.0]),
                      "met_target": False},
         "direct": {"what": "ritardo pin->pin", "metric": "pad_to_pad_ns", **stats([40.0, 44.0])},
         "wrapper": {"in_bits": 272, "out_bits": 49}}
    md = render_markdown(p)
    assert "| LUT4 | 1592 |" in md and "| MULT18X18D | 32 |" in md
    assert "**71.0** MHz [70.0 – 72.0]" in md and "**42.0** ns [40.0 – 44.0]" in md
    assert "NON rispettato" in md and "registro → DUT → registro" in md
    assert "| 2 | 72.0 | 44.0 |" in md


def test_real_flow_if_tools_available(tmp_path):
    if not (which("yosys") and which("nextpnr-ecp5")):
        return   # strumenti fisici opzionali
    (tmp_path / "rtl").mkdir()
    rtl = tmp_path / "rtl" / "Mac.sv"
    rtl.write_text("module Mac(input clock, input reset, input [7:0] io_a, input [7:0] io_b,"
                   " output [15:0] io_y); assign io_y = io_a * io_b + io_a; endmodule\n")
    s = run_synth(tmp_path, "Mac", [rtl])
    assert s.ok, s.log
    p = run_physical(tmp_path, "Mac", [rtl], Path(s.detail["netlist"]), 100.0, seeds=2,
                     plan={"clocking": "combinational"})
    d = p.detail
    assert p.ok and d["fmax_mhz"]["n"] == 2 and d["fmax_mhz"]["median"] > 0
    assert d["direct"]["metric"] == "pad_to_pad_ns" and d["direct"]["n"] == 2
    assert d["area_generic"]["cells"] > 0 and d["wrapper"]["in_bits"] == 16
    assert json.loads((tmp_path / "physical.json").read_text())["device"]
    assert "Fmax" in (tmp_path / "physical.md").read_text(encoding="utf-8")


# ------------------------------------------------------------------ PPA loop
def test_ppa_adopts_only_verified_and_better_variants(tmp_path):
    from mxfp4agent.agents.base import AgentResult
    from mxfp4agent.config import Config
    from mxfp4agent.toolchain.runner import ToolchainReport
    from mxfp4agent.toolchain.stage import StageResult
    from mxfp4agent.utils import ExtractedFile
    from mxfp4agent.workflow import Workflow

    def phys(fmax, lut):
        return {"device": "x", "fmax_mhz": {"n": 5, "median": fmax}, "area_ecp5": {"LUT4": lut}}

    # round 1: più veloce ma NON verificata; round 2: corretta e migliore;
    # round 3: corretta ma peggiore -> si torna alla 2 e si riallinea il disco
    outcomes = {"v1": (False, phys(200, 100)), "v2": (True, phys(90, 1500)),
                "v3": (True, phys(85, 900))}

    class FakeCoder:
        n = 0

        def optimize(self, plan, compiled, files, physical, history):
            FakeCoder.n += 1
            return AgentResult(True, [ExtractedFile("src/main/scala/mxfp4/M.scala",
                                                    f"v{FakeCoder.n}")], "", f"idea {FakeCoder.n}")

    class FakeTester:
        runs = []

        def run(self, plan, files, *a, round_id=0, **k):
            tag = files[0].content
            FakeTester.runs.append((round_id, tag))
            ok, ph = outcomes.get(tag, (True, phys(80, 1600)))
            imp = StageResult("implement", True, "", detail=ph)
            sim = StageResult("simulate", ok, "")
            return AgentResult(ok, ToolchainReport([sim, imp]), "", "")

    wf = Workflow(Config(provider="mock", outdir=tmp_path, ppa_rounds=3, verbose=False))
    wf._physical = phys(80, 1600)
    original = [ExtractedFile("src/main/scala/mxfp4/M.scala", "v0")]
    best = wf._ppa_loop({"latency_cycles": 0}, "P", original, FakeCoder(), FakeTester())
    assert best[0].content == "v2"
    assert [h["outcome"].split(":")[0] for h in wf.ppa_history] == \
        ["verifica fallita", "adottata", "corretta ma non migliore"]
    assert FakeTester.runs[-1] == ("ppa_final", "v2")        # disco riallineato
    assert wf._physical["fmax_mhz"]["median"] == 90


def test_ppa_tie_on_fmax_prefers_smaller_area():
    from mxfp4agent.workflow import Workflow

    def phys(fmax, lut):
        return {"fmax_mhz": {"median": fmax}, "area_ecp5": {"LUT4": lut}}

    assert Workflow._better(phys(100.5, 900), phys(100.0, 1000))     # pari entro 1%, meno LUT
    assert not Workflow._better(phys(100.5, 1100), phys(100.0, 1000))
    assert Workflow._better(phys(120, 5000), phys(100, 1000))        # Fmax nettamente migliore
