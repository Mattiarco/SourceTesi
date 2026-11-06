"""Contratto delle porte, testbench da template e vettori esaustivi."""
import json
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mxfp4agent.agents import CoderAgent, PlannerAgent, ReviewerAgent  # noqa: E402
from mxfp4agent.agents.tester import TesterAgent  # noqa: E402
from mxfp4agent.knowledge import full_context  # noqa: E402
from mxfp4agent.knowledge.contracts import CONTRACTS, contract_ports  # noqa: E402
from mxfp4agent.knowledge.golden_model import f32_bits  # noqa: E402
from mxfp4agent.knowledge.references import load_references  # noqa: E402
from mxfp4agent.llm import build_provider  # noqa: E402
from mxfp4agent.toolchain.runner import summarize_coverage  # noqa: E402
from mxfp4agent.toolchain.tbgen import render_testbench  # noqa: E402
from mxfp4agent.utils import ExtractedFile, Log  # noqa: E402


def _plan(kernel="dot_product", clocking="combinational", k=32, latency=0, name="Dut"):
    return {"module_name": name, "clocking": clocking, "latency_cycles": latency,
            "block_size": k, "test_plan": {"kernel": kernel}, "testbench": "template",
            "ports": contract_ports(kernel, k, clocking)}


# --------------------------------------------------------------- contratto
def test_reference_designs_follow_the_contract():
    for ref in load_references():
        kernel, clk = ref.kernel, ref.clocking
        want = {(p["name"], p["dir"], p["type"], p["width"])
                for p in contract_ports(kernel, 32, clk)}
        got = {(p["name"], p["dir"], p["type"], p["width"]) for p in ref.plan["ports"]}
        assert got == want, (ref.key, got ^ want)


def test_sequential_contract_adds_valid_ports():
    names = [p["name"] for p in contract_ports("dot_product", 32, "sequential")]
    assert names[0] == "validIn" and "validOut" in names
    assert "validIn" not in [p["name"] for p in contract_ports("dot_product", 32)]
    assert contract_ports("custom") == []


def test_widths_scale_with_k():
    ports = {p["name"]: p["width"] for p in contract_ports("to_bf16", 8)}
    assert ports == {"a": 32, "scaleA": 8, "out": 128, "isNaN": 1}


def test_planner_enforces_the_contract_and_records_differences():
    plan = PlannerAgent._normalize({
        "module_name": "X", "clocking": "combinational",
        "test_plan": {"kernel": "dot_product"},
        "ports": [{"name": "inputA", "dir": "in", "width": 128, "type": "UInt"},
                  {"name": "output", "dir": "out", "width": 16, "type": "SInt"}]}, 32)
    assert [p["name"] for p in plan["ports"]] == \
        ["a", "b", "scaleA", "scaleB", "accQ2", "expOut", "isNaN"]
    assert plan["ports_source"] == "contract"
    assert "in inputA" in plan["ports_overridden"] and "out accQ2" in plan["ports_overridden"]


def test_planner_keeps_custom_ports_and_fixes_latency():
    plan = PlannerAgent._normalize({
        "module_name": "Q", "clocking": "sequential", "latency_cycles": 0,
        "test_plan": {"kernel": "custom"},
        "ports": [{"name": "x", "dir": "in", "width": 8, "type": "UInt"}]}, 32)
    assert plan["ports_source"] == "planner" and plan["ports"][0]["name"] == "x"
    assert plan["latency_cycles"] == 1
    comb = PlannerAgent._normalize({"module_name": "C", "clocking": "combinational",
                                    "latency_cycles": 3}, 32)
    assert comb["latency_cycles"] == 0












# -------------------------------------------------------------- testbench
def test_testbench_shapes():
    tb = render_testbench(_plan())
    assert '#include "VDut.h"' in tb and "dut->io_accQ2" in tb and "LATENCY 0" in tb
    assert "for (int w = 0; w < 4; ++w) dut->io_a[w] = v.a[w];" in tb
    assert "TEST PASSED (%d vectors)" in tb and "prima discrepanza" in tb
    seq = render_testbench(_plan(clocking="sequential", latency=2))
    assert "LATENCY 2" in seq and "dut->io_validIn = 1;" in seq and "dut->reset = 1;" in seq
    ew = render_testbench(_plan("elementwise_mul"))
    assert "out_elem" in ew and "0xFFFFFFFFu" in ew and "dut->io_b" in ew
    bf = render_testbench(_plan("to_bf16"))
    assert "0xFFFFu" in bf and "dut->io_b" not in bf and "dut->io_scaleB" not in bf


def test_testbench_handles_narrow_ports():
    tb8 = render_testbench(_plan("to_bf16", k=8))          # a: 32 bit, out: 128 bit
    assert "dut->io_a = v.a[0];" in tb8
    tb16 = render_testbench(_plan("dot_product", k=16))    # a: 64 bit
    assert "dut->io_a = ((uint64_t)v.a[1] << 32) | v.a[0];" in tb16


def test_testbench_refuses_custom_kernels():
    try:
        render_testbench(_plan("custom"))
    except ValueError:
        return
    raise AssertionError


def _stub_headers(d: Path, module: str, ports: list[dict]) -> None:
    """Header Verilator fittizi: bastano per un controllo di sintassi con g++."""
    (d / "verilated.h").write_text(
        "#pragma once\n#include <cstdint>\nstruct VerilatedCov{void write(const char*){}};\n"
        "struct VerilatedContext{VerilatedCov* coveragep(){return nullptr;}};\n"
        "struct Verilated{static void commandArgs(int,char**){}"
        " static void traceEverOn(bool){}"
        " static VerilatedContext* defaultContextp(){return nullptr;}};\n")

    def ctype(w):
        return ("uint8_t" if w <= 8 else "uint16_t" if w <= 16 else "uint32_t" if w <= 32
                else "uint64_t" if w <= 64 else f"uint32_t[{(w + 31) // 32}]")

    members = "".join(
        f"  {ctype(p['width']).split('[')[0]} io_{p['name']}"
        f"{('[' + ctype(p['width']).split('[')[1]) if '[' in ctype(p['width']) else ''};\n"
        for p in ports)
    (d / f"V{module}.h").write_text(
        f"#pragma once\n#include <cstdint>\nstruct V{module} {{\n  uint8_t clock, reset;\n"
        f"{members}  void eval(){{}}\n  void final(){{}}\n}};\n")


def test_generated_testbench_compiles_if_gpp_available(tmp_path):
    gpp = shutil.which("g++")
    if not gpp:
        return   # niente g++ (es. Windows): la compilazione vera avviene in Verilator
    from mxfp4agent.toolchain.testvectors import write_header

    cases = [("dot_product", "combinational", 32), ("dot_product", "sequential", 32),
             ("elementwise_add", "combinational", 32), ("to_bf16", "sequential", 8),
             ("dot_product", "combinational", 16)]
    for kernel, clk, k in cases:
        d = tmp_path / f"{kernel}_{clk}_{k}"
        d.mkdir()
        plan = _plan(kernel, clk, k, latency=2 if clk == "sequential" else 0)
        _stub_headers(d, "Dut", plan["ports"])
        write_header(d / "test_vectors.h", kernel, 2, k, 1)
        (d / "tb.cpp").write_text(render_testbench(plan))
        r = subprocess.run([gpp, "-std=c++17", "-fsyntax-only", "-Wall", "-Wextra", "-Werror",
                            "-I", str(d), str(d / "tb.cpp")], capture_output=True, text=True)
        assert r.returncode == 0, f"{kernel}/{clk}/{k}:\n{r.stderr[:2000]}"


# --------------------------------------------------------- agenti/tester
def test_template_mode_shrinks_prompts():
    assert "REGOLE TESTBENCH" in full_context(True)
    assert "REGOLE TESTBENCH" not in full_context(False)
    assert "io_<nome>" not in full_context(False)


def test_coder_in_template_mode_writes_only_scala():
    seen = []
    prov = build_provider("mock")
    orig = prov._chat
    prov._chat = lambda s, m, **kw: (seen.append(m[-1].content), orig(s, m, **kw))[1]
    res = CoderAgent(prov, Log(False, False)).run(_plan(name="MXFP4DotProduct"), "PROMPT")
    assert res.ok and [f.ext for f in res.payload] == [".scala"]   # il .cpp del mock scartato
    assert "GENERATO AUTOMATICAMENTE" in seen[-1] and "esattamente uno" in seen[-1]
    assert "CONTRATTO DELL'HEADER" not in seen[-1]


def test_fixer_cannot_replace_the_generated_testbench():
    from mxfp4agent.knowledge.reference_design import MOCK_CODER_ANSWER
    from mxfp4agent.llm.base import LLMResponse

    prov = build_provider("mock")
    # il "fixer" riemette sia il modulo sia un testbench suo
    prov._chat = lambda s, m, **kw: LLMResponse("CAUSA: x\n" + MOCK_CODER_ANSWER, "m", "mock")
    files = [ExtractedFile("src/main/scala/mxfp4/MXFP4DotProduct.scala", "old")]
    res = ReviewerAgent(prov, Log(False, False)).fix(_plan(name="MXFP4DotProduct"), files,
                                                      "simulate", "FAIL")
    assert res.ok and all(f.ext == ".scala" for f in res.payload)


def test_tester_writes_the_generated_testbench(tmp_path):
    t = TesterAgent(build_provider("mock"), Log(False, False), tmp_path)
    (tmp_path / "sim").mkdir()
    (tmp_path / "sim" / "tb_vecchio.cpp").write_text("// round precedente")
    plan = _plan(name="Foo")
    t.materialize(plan, [ExtractedFile("src/main/scala/mxfp4/Foo.scala", "x"),
                         ExtractedFile("sim/tb_Foo.cpp", "// scritto dall'LLM")], 2)
    tb = (tmp_path / "sim" / "tb_Foo.cpp").read_text()
    assert tb.startswith("// GENERATO AUTOMATICAMENTE")
    assert not (tmp_path / "sim" / "tb_vecchio.cpp").exists()


def test_coverage_summary():
    dat = ("# SystemC::Coverage-3\n"
           "C '\x01t\x02line\x01page\x02v_line/Foo\x01f\x02Foo.sv\x01l\x0210' 4\n"
           "C '\x01t\x02line\x01page\x02v_line/Foo\x01f\x02Foo.sv\x01l\x0211' 0\n"
           "C '\x01t\x02toggle\x01page\x02v_toggle/Foo\x01o\x02io_a[0]' 7\n")
    s = summarize_coverage(dat)
    assert s["line"] == {"covered": 1, "total": 2, "percent": 50.0}
    assert s["toggle"]["percent"] == 100.0 and s["all"]["total"] == 3


def test_cli_flags():
    from mxfp4agent.cli import build_parser

    a = build_parser().parse_args(["x"])
    assert a.tb_mode == "template" and not a.trace and not a.coverage
    a = build_parser().parse_args(["x", "--tb", "llm", "--trace", "--coverage"])
    assert a.tb_mode == "llm" and a.trace and a.coverage


def test_reference_plans_are_valid_json():
    for d in (Path(__file__).resolve().parents[1] / "examples/expected_output").iterdir():
        if d.is_dir():
            json.loads((d / "plan.json").read_text(encoding="utf-8"))


def test_f32_helper_sanity():
    assert f32_bits(1.0) == 0x3F800000
