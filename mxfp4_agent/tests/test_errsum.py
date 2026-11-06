"""Riassunto degli errori di toolchain (log reali in tests/fixtures/)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mxfp4agent.toolchain.errsum import hint_for, parse_errors, summarize_errors  # noqa: E402
from mxfp4agent.toolchain.stage import StageResult  # noqa: E402
from mxfp4agent.workflow import Workflow  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures"


def _log(name: str) -> str:
    return (FIX / f"sbt_{name}.log").read_text(encoding="utf-8")


def _project(tmp_path: Path, body: str) -> Path:
    src = tmp_path / "src/main/scala/mxfp4"
    src.mkdir(parents=True)
    (src / "Broken.scala").write_text(body, encoding="utf-8")
    return tmp_path


BROKEN = "\n".join(f"// riga {i}" for i in range(1, 10)) + "\n" + \
    "  val s: SInt = io.a + 1.U\n  val y = Cat(io.a, io.a)\n  io.out := io.a\n  // 13\n"


def test_scalac_not_found():
    (e,) = parse_errors(_log("scalac"))
    assert (e.tool, e.line, e.col) == ("scalac", 11, 11)
    assert e.file.endswith("src/main/scala/mxfp4/Broken.scala")
    assert e.message == "not found: value Cat"
    assert "chisel3.util._" in hint_for(e.message)


def test_scalac_type_mismatch_keeps_found_and_required():
    (e,) = parse_errors(_log("mismatch"))
    assert (e.line, e.col) == (10, 22)
    assert "found : chisel3.UInt" in e.message and "required: chisel3.SInt" in e.message
    assert ".asSInt" in hint_for(e.message)


def test_firtool_error_is_deduplicated_and_path_normalized():
    errs = parse_errors(_log("elab"))
    assert len(errs) == 1, errs      # firtool lo stampa due volte
    e = errs[0]
    assert e.tool == "firtool" and e.line == 11
    assert e.file == "/src/main/scala/mxfp4/Broken.scala"
    assert "not fully initialized" in e.message and "WireDefault" in hint_for(e.message)


def test_chisel_exception_points_to_user_code():
    (e,) = parse_errors(_log("runtime"))
    assert e.tool == "chisel" and e.file == "Broken.scala" and e.line == 11
    assert e.message.startswith("ChiselException: Connection between")


def test_summary_has_context_from_the_real_source(tmp_path):
    root = _project(tmp_path, BROKEN)
    s = summarize_errors(_log("scalac"), root)
    assert "[scalac] src/main/scala/mxfp4/Broken.scala:11:11  not found: value Cat" in s
    assert ">   11 |   val y = Cat(io.a, io.a)" in s
    assert "   10 |" in s and "   12 |" in s           # 3 righe di contesto
    assert "suggerimento:" in s


def test_summary_resolves_a_bare_file_name(tmp_path):
    root = _project(tmp_path, BROKEN)
    s = summarize_errors(_log("runtime"), root)
    assert "src/main/scala/mxfp4/Broken.scala:11" in s and "io.out := io.a" in s


def test_verilator_and_gpp_formats():
    log = """%Warning-WIDTH: /x/rtl/Foo.sv:5:7: Operator ASSIGN expects 8 bits
%Error: /x/rtl/Foo.sv:12:3: Cannot find file containing module: 'Bar'
%Error: Exiting due to 1 error(s)
/x/sim/tb_Foo.cpp:23:10: error: 'class VFoo' has no member named 'accQ2'
/x/sim/tb_Foo.cpp:24:1: warning: unused variable 'k'
"""
    errs = parse_errors(log)
    assert [(e.tool, e.severity, e.line) for e in errs] == [
        ("verilator", "warning", 5), ("verilator", "error", 12),
        ("g++", "error", 23), ("g++", "warning", 24)]
    s = summarize_errors(log)
    assert "WIDTH" not in s and "unused" not in s       # i warning si escludono
    assert "io_<nome>" in s                             # suggerimento sul prefisso io_


def test_unrecognized_log_gives_empty_summary():
    assert summarize_errors("qualcosa e' andato storto, nessuna posizione") == ""


def test_fixer_gets_summary_plus_raw_tail(tmp_path):
    root = _project(tmp_path, BROKEN)
    fail = StageResult("elaborate", False, _log("mismatch"))
    ev = Workflow.fixer_evidence(fail, root)
    assert ev.startswith("RIASSUNTO DEGLI ERRORI")
    assert "coda del log grezzo" in ev and "Compilation failed" in ev
    raw = StageResult("simulate", False, "FAIL [3] all_max acc exp=4608 got=0")
    assert Workflow.fixer_evidence(raw, root) == raw.log    # niente da riassumere
