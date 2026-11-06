"""Lint Chisel statico: correzioni sicure, errori certi, avvisi, nessun falso positivo."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mxfp4agent.agents.tester import TesterAgent  # noqa: E402
from mxfp4agent.config import Config  # noqa: E402
from mxfp4agent.knowledge.references import BUILTIN, load_references  # noqa: E402
from mxfp4agent.llm import build_provider  # noqa: E402
from mxfp4agent.toolchain.chisel_lint import (blank_comments_and_strings,  # noqa: E402
                                              lint_chisel)
from mxfp4agent.utils import ExtractedFile, Log  # noqa: E402
from mxfp4agent.workflow import Workflow  # noqa: E402

PORTS = {"ports": [
    {"name": "a", "dir": "in", "type": "UInt", "width": 8},
    {"name": "out", "dir": "out", "type": "SInt", "width": 16},
]}

GOOD = """package mxfp4

import chisel3._
import chisel3.util._

class Foo extends Module {
  val io = IO(new Bundle {
    val a   = Input(UInt(8.W))
    val out = Output(SInt(16.W))
  })
  val w = Wire(SInt(16.W))
  w := Cat(0.U(8.W), io.a).asSInt
  io.out := w
}
"""


def _rules(res, sev=None):
    return [i.rule for i in res.issues if sev is None or i.severity == sev]


def test_reference_designs_are_clean():
    for ref in load_references() + [BUILTIN]:
        res = lint_chisel(ref.scala, ref.plan if ref.plan.get("ports") else None)
        assert res.issues == [], (ref.key, res.report("x"))
        assert res.source == ref.scala


def test_good_module_is_untouched():
    res = lint_chisel(GOOD, PORTS)
    assert res.issues == [] and res.source == GOOD


def test_missing_package_and_imports_are_fixed():
    src = GOOD.replace("package mxfp4\n\n", "").replace("import chisel3._\n", "") \
              .replace("import chisel3.util._\n", "")
    res = lint_chisel(src, PORTS)
    assert sorted(_rules(res, "fixed")) == ["imports", "imports", "package"]
    assert res.source.startswith("package mxfp4\n")
    assert "import chisel3._\nimport chisel3.util._" in res.source
    assert lint_chisel(res.source, PORTS).issues == []      # idempotente


def test_wrong_package_is_renamed():
    res = lint_chisel(GOOD.replace("package mxfp4", "package foo.bar"), PORTS)
    assert "package mxfp4" in res.source and _rules(res) == ["package"]


def test_named_util_import_counts():
    src = GOOD.replace("import chisel3.util._", "import chisel3.util.Cat")
    assert lint_chisel(src, PORTS).issues == []


def test_symbols_in_comments_and_strings_are_ignored():
    src = GOOD.replace("import chisel3.util._\n", "") \
              .replace("w := Cat(0.U(8.W), io.a).asSInt", "w := io.a.zext // non Cat(x)")
    src = src.replace('val w', 'val msg = "usa Cat(a, b)"\n  val w')
    assert "imports" not in _rules(lint_chisel(src, PORTS))
    assert blank_comments_and_strings('a // Cat(\n"Cat(" b').count("Cat") == 0


def test_negative_literal_is_rewritten():
    res = lint_chisel(GOOD.replace("io.out := w", "io.out := w + -3.S(16.W)"), PORTS)
    assert "(-3).S(16.W)" in res.source and _rules(res) == ["neg-literal"]
    res = lint_chisel(GOOD.replace("io.out := w", "io.out := Mux(io.a(0), -1.S(16.W), w)"), PORTS)
    assert "Mux(io.a(0), (-1).S(16.W), w)" in res.source
    # sottrazioni binarie: non vanno toccate (riscriverle romperebbe il codice)
    for expr in ("w -3.S(16.W)", "w - 3.S(16.W)", "(w) -3.S(16.W)", "v(0) -3.S(16.W)"):
        res = lint_chisel(GOOD.replace("io.out := w", f"val v = VecInit(w)\n  io.out := {expr}"),
                          PORTS)
        assert expr in res.source and "neg-literal" not in _rules(res), expr


def test_uninitialized_wire_is_an_error():
    res = lint_chisel(GOOD.replace("  w := Cat(0.U(8.W), io.a).asSInt\n", "")
                      .replace("io.out := w", "io.out := io.a.zext"), PORTS)
    assert _rules(res, "error") == ["wire-uninit"]


def test_vec_wire_assigned_by_index_is_fine():
    src = GOOD.replace("val w = Wire(SInt(16.W))", "val v = Wire(Vec(2, SInt(16.W)))\n"
                       "  for (i <- 0 until 2) { v(i) := 0.S }\n  val w = Wire(SInt(16.W))")
    assert "wire-uninit" not in _rules(lint_chisel(src, PORTS))


def test_port_contract_mismatch_is_an_error():
    src = GOOD.replace("val a   = Input", "val inA = Input").replace("io.a", "io.inA")
    res = lint_chisel(src, PORTS)
    (e,) = res.errors
    assert e.rule == "ports" and "mancano a" in e.message and "in più inA" in e.message
    assert "in a: UInt(8)" in e.hint
    res = lint_chisel(GOOD.replace("Output(SInt(16.W))", "Output(UInt(16.W))"), PORTS)
    assert "tipo errato out (UInt≠SInt)" in res.errors[0].message


def test_entrypoint_and_old_api_are_errors():
    res = lint_chisel(GOOD + "\nobject FooMain extends App {\n  ChiselStage.emitSystemVerilog(new Foo)\n}\n",
                      PORTS)
    assert _rules(res, "error") == ["entrypoint"]
    res = lint_chisel(GOOD.replace("import chisel3._", "import Chisel._"), PORTS)
    assert "deprecated-api" in _rules(res, "error")


def test_warnings():
    src = GOOD.replace("io.out := w", """val s = io.a.asSInt
  val validReg = Reg(Bool())
  val ok = RegNext(io.a, 0.U)
  val sh: SInt = w
  val sum = Seq(w, w).reduce(_ + _)
  val c = Cat(s, io.a)
  io.out := (w >> sh)""")
    res = lint_chisel(src, PORTS)
    assert sorted(_rules(res, "warning")) == sorted(
        ["assint-input", "reg-noreset", "plus-truncates", "cat-sint", "shift-sint"]), res.report("x")
    assert not res.errors
    assert "io.a.zext" in next(i.hint for i in res.issues if i.rule == "assint-input")


# ------------------------------------------------------------ integrazione
def test_lint_error_skips_the_toolchain(tmp_path):
    called = []
    t = TesterAgent(build_provider("mock"), Log(False, False), tmp_path)
    t.run_toolchain = lambda *a, **k: called.append(1)
    plan = {"module_name": "Foo", "block_size": 32, "test_plan": {"kernel": "dot_product"},
            **PORTS}
    bad = GOOD.replace("val a   = Input", "val x = Input").replace("io.a", "io.x")
    files = [ExtractedFile("src/main/scala/mxfp4/Foo.scala", bad)]
    res = t.run(plan, files, num_random=2)
    assert not res.ok and res.meta.get("lint") and not called
    assert res.payload.first_failure.stage == "chisel_lint"
    assert (tmp_path / "logs/r0_chisel_lint.log").exists()


def test_autofix_reaches_the_files_seen_by_the_fixer(tmp_path):
    t = TesterAgent(build_provider("mock"), Log(False, False), tmp_path)
    plan = {"module_name": "Foo", **PORTS}
    f = ExtractedFile("src/main/scala/mxfp4/Foo.scala", GOOD.replace("package mxfp4\n", ""))
    stage = t.static_lint(plan, [f])
    assert stage.ok and f.content.startswith("package mxfp4") and stage.detail["fixed"] == 1


def test_lint_warnings_reach_the_fixer_when_simulation_fails(tmp_path):
    from mxfp4agent.toolchain.runner import ToolchainReport
    from mxfp4agent.toolchain.stage import StageResult

    lint = StageResult("chisel_lint", True, "...", detail={"warnings_text": "[chisel-lint:plus-truncates] WARNING"})
    sim = StageResult("simulate", False, "FAIL [2] all_max")
    ev = Workflow.fixer_evidence(sim, tmp_path, ToolchainReport([lint, sim]))
    assert "AVVISI DEL LINT STATICO" in ev and "plus-truncates" in ev


def test_no_lint_flag():
    from mxfp4agent.cli import build_parser

    assert build_parser().parse_args(["x", "--no-lint"]).no_lint
    assert Config().chisel_lint is True
