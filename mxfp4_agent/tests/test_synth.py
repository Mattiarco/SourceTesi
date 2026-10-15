"""Fasi fisiche: parser dei log e comportamento della sintesi Yosys."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mxfp4agent.toolchain import parse_nextpnr_log, parse_yosys_stat, run_synth, which

NEXTPNR_SEQ = """
Info: Max frequency for clock '$glbnet$clock$TRELLIS_IO_IN': 120.50 MHz (PASS at 100.00 MHz)
Info: Max frequency for clock '$glbnet$clock$TRELLIS_IO_IN': 337.95 MHz (PASS at 100.00 MHz)
"""
NEXTPNR_COMB = """
Info: Max delay <async> -> <async>: 58.41 ns
Info: Max delay <async> -> <async>: 38.83 ns
"""


def test_nextpnr_sequential_takes_last_report_and_cleans_clock_name():
    t = parse_nextpnr_log(NEXTPNR_SEQ)
    assert list(t["clocks"]) == ["clock"]
    assert t["clocks"]["clock"]["fmax_mhz"] == 337.95 and t["clocks"]["clock"]["met"]


def test_nextpnr_failed_timing_is_reported_not_raised():
    t = parse_nextpnr_log("Info: Max frequency for clock 'c': 80.00 MHz (FAIL at 100.00 MHz)")
    assert t["clocks"]["c"]["met"] is False


def test_nextpnr_combinational_delay():
    t = parse_nextpnr_log(NEXTPNR_COMB)
    assert t["clocks"] == {} and t["max_delays_ns"]["<async> -> <async>"] == 38.83


def test_yosys_stat_parsing():
    j = {"modules": {"\\Top": {"num_cells": 50, "num_cells_by_type":
                               {"LUT4": 40, "TRELLIS_FF": 8, "MULT18X18D": 1, "$scopeinfo": 1}}}}
    r = parse_yosys_stat(j, "Top")
    assert r["LUT4"] == 40 and r["TRELLIS_FF"] == 8 and r["MULT18X18D"] == 1
    assert "$scopeinfo" not in r and r["_total_cells"] == 50


def test_synth_flags_latch_and_packed_arrays(tmp_path):
    if not which("yosys"):
        return  # sintesi opzionale: senza yosys il test non ha oggetto
    (tmp_path / "rtl").mkdir()
    ok = tmp_path / "rtl" / "A.sv"
    ok.write_text("module A(input [3:0] a, input [3:0] b, output [4:0] y);"
                  " assign y = a + b; endmodule")
    s = run_synth(tmp_path, "A", [ok])
    assert s.ok and s.detail["resources_yosys"]["_total_cells"] > 0

    lat = tmp_path / "rtl" / "L.sv"
    lat.write_text("module L(input [3:0] a, input en, output reg [3:0] y);"
                   " always @* if (en) y = a; endmodule")
    s = run_synth(tmp_path, "L", [lat])
    assert not s.ok and "latch" in s.log.lower()

    pk = tmp_path / "rtl" / "P.sv"
    pk.write_text("module P(input [1:0] i, output [3:0] y);\n"
                  "  wire [3:0][3:0] g = {4'h1, 4'h2, 4'h3, 4'h4};\n"
                  "  assign y = g[i];\nendmodule")
    s = run_synth(tmp_path, "P", [pk])
    assert not s.ok and "disallowPackedArrays" in s.log
