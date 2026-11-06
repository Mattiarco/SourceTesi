"""Test di parsing, generazione vettori e pipeline end-to-end con provider mock."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mxfp4agent.agents import CoderAgent, PlannerAgent, ReviewerAgent
from mxfp4agent.config import Config
from mxfp4agent.llm import build_provider
from mxfp4agent.toolchain.testvectors import build_vectors, render_header
from mxfp4agent.utils import Log, extract_files, extract_json
from mxfp4agent.workflow import Workflow


# ------------------------------------------------------------------ parsing
def test_extract_files_with_headers():
    text = """intro

### FILE: src/main/scala/mxfp4/Foo.scala
```scala
class Foo extends Module {}
```

### FILE: sim/tb_Foo.cpp
```cpp
int main() { return 0; }
```
"""
    files = extract_files(text)
    assert [f.path for f in files] == ["src/main/scala/mxfp4/Foo.scala", "sim/tb_Foo.cpp"]
    assert "class Foo" in files[0].content


def test_extract_files_fallback_without_headers():
    text = "```scala\nclass Bar extends Module {}\n```\n```cpp\n#include \"VBar.h\"\n```"
    files = extract_files(text)
    assert len(files) == 2
    assert files[0].path.endswith("Bar.scala")
    assert files[1].path == "sim/tb_Bar.cpp"


def test_extract_json_from_fence_and_bare():
    assert extract_json('```json\n{"a": 1}\n```')["a"] == 1
    assert extract_json('blabla {"b": {"c": 2}} coda')["b"]["c"] == 2


# ------------------------------------------------------------ test vectors
def test_vectors_cover_edge_cases():
    labels = {v.label for v in build_vectors(num_random=4)}
    for needed in ("all_zero", "all_max", "all_subnormal", "negative_zero", "scale_nan"):
        assert needed in labels


def test_header_is_valid_c():
    header = render_header(build_vectors(num_random=3))
    assert "#define NUM_VECTORS  13" in header
    assert header.count("{ {0x") == 13
    assert "mxfp4_vec_t MXFP4_VECTORS" in header
    # bilanciamento delle graffe
    assert header.count("{") == header.count("}")


def test_header_all_zero_vector_has_zero_accumulator():
    header = render_header(build_vectors(num_random=0))
    first = header.split("MXFP4_VECTORS[NUM_VECTORS] = {")[1].splitlines()[1]
    assert '"all_zero"' in first
    assert ", 0, " in first


# ------------------------------------------------- convenzione porte Chisel
def test_chisel_port_prefix_rule_is_in_the_prompt():
    """Chisel appiattisce `io` in `io_<nome>`: senza questa regola il tb non compila."""
    from mxfp4agent.knowledge import full_context

    chisel = full_context("chisel")
    assert "io_" in chisel and "appiattisce" in chisel
    assert "clock" in chisel and "reset" in chisel
    # per SystemVerilog scritto a mano la regola NON deve comparire: confonderebbe
    assert "appiattisce" not in full_context("systemverilog")


def test_reference_testbench_uses_io_prefix():
    """Guardia di regressione sull'errore reale visto con Verilator 5.032."""
    from mxfp4agent.knowledge.reference_design import REFERENCE_TB

    for port in ("io_a", "io_b", "io_scaleA", "io_scaleB",
                 "io_accQ2", "io_expOut", "io_isNaN"):
        assert f"dut->{port}" in REFERENCE_TB, port
    for wrong in ("dut->a[", "dut->b[", "dut->scaleA", "dut->accQ2", "dut->isNaN"):
        assert wrong not in REFERENCE_TB, wrong
    # clock e reset esistono sempre e NON hanno prefisso
    assert "dut->clock" in REFERENCE_TB and "dut->reset" in REFERENCE_TB


# ----------------------------------------------------------------- agents
def test_planner_parses_mock_plan():
    log = Log(verbose=False, color=False)
    planner = PlannerAgent(build_provider("mock"), log)
    res = planner.run("dot product mxfp4", block_size=32)
    assert res.ok
    assert res.payload["module_name"] == "MXFP4DotProduct"
    assert res.payload["meta_hdl"] == "chisel"
    assert res.payload["test_plan"]["kernel"] == "dot_product"


def test_planner_target_hint_overrides():
    planner = PlannerAgent(build_provider("mock"), Log(False, False))
    res = planner.run("qualcosa", target_hint="systemverilog")
    assert res.payload["meta_hdl"] == "systemverilog"


def test_compiled_prompt_contains_domain_knowledge():
    planner = PlannerAgent(build_provider("mock"), Log(False, False))
    plan = planner.run("dot product").payload
    prompt = PlannerAgent.compile_coder_prompt(plan, "richiesta")
    for token in ("E2M1", "E8M0", "0.5", "bias = 1", "TEST PASSED"):
        assert token in prompt


def test_coder_produces_both_files():
    planner = PlannerAgent(build_provider("mock"), Log(False, False))
    plan = planner.run("dot product").payload
    coder = CoderAgent(build_provider("mock"), Log(False, False))
    res = coder.run(plan, PlannerAgent.compile_coder_prompt(plan, "x"))
    assert res.ok
    paths = {f.path for f in res.payload}
    assert "src/main/scala/mxfp4/MXFP4DotProduct.scala" in paths
    assert "sim/tb_MXFP4DotProduct.cpp" in paths


def test_reviewer_never_overwrites_golden_header():
    from mxfp4agent.utils import ExtractedFile

    old = [ExtractedFile("sim/test_vectors.h", "GOLDEN")]
    new = [ExtractedFile("sim/test_vectors.h", "HACKED")]
    merged = ReviewerAgent._merge(old, new)
    assert merged[0].content == "GOLDEN"


# ------------------------------------------------------------ robustezza LLM






def test_static_review_failure_does_not_kill_the_run(tmp_path):
    """Se il servizio è giù durante la review, Planner e Coder non vanno buttati."""
    from mxfp4agent.agents.reviewer import ReviewerAgent
    from mxfp4agent.agents.tester import TesterAgent
    from mxfp4agent.llm.base import LLMError
    from mxfp4agent.toolchain.runner import StageResult, ToolchainReport

    def boom(self, plan, files):
        raise LLMError("Ollama HTTP 503: server busy")

    def fake_toolchain(self, plan, round_id=0):
        rep = ToolchainReport()
        for st in ("elaborate", "lint", "build", "simulate"):
            rep.add(StageResult(st, True, "(simulata)", detail={"passed": 8}))
        return rep

    orig_r, orig_t = ReviewerAgent.review, TesterAgent.run_toolchain
    ReviewerAgent.review, TesterAgent.run_toolchain = boom, fake_toolchain
    try:
        cfg = Config(request="dot product", provider="mock", outdir=tmp_path,
                     max_fix_rounds=0, static_review=True, num_random_vectors=4,
                     verbose=False)
        result = Workflow(cfg).run()
    finally:
        ReviewerAgent.review, TesterAgent.run_toolchain = orig_r, orig_t

    assert result.ok, result.message
    assert any(e.get("event") == "skipped" for e in result.trace)








def test_truncated_response_is_flagged(capsys=None):
    """Se la risposta è tagliata a metà l'agente deve dirlo, non proseguire in silenzio."""
    from mxfp4agent.agents.base import Agent
    from mxfp4agent.llm.base import LLMProvider, LLMResponse

    class Trunc(LLMProvider):
        name = "trunc"

        def _chat(self, system, messages, **kw):
            return LLMResponse("meta file", self.model, self.name, 10, 99,
                               stop_reason="max_tokens")

        def health_check(self, live=True):
            return True, "ok"

    warnings = []
    log = Log(verbose=False, color=False)
    log.fail = warnings.append
    agent = Agent(Trunc("m", max_tokens=8192), log)
    agent.ask("dammi un file")
    assert warnings and "TRONCATA" in warnings[0]




# ------------------------------------------------------ invocazione toolchain
def test_runner_root_is_absolute_and_paths_are_relative(tmp_path):
    """Bug reale: con outdir relativo, verilator cercava out/X/out/X/rtl/... ."""
    import os

    from mxfp4agent.toolchain.runner import ToolchainRunner

    proj = tmp_path / "out" / "MyMod"
    (proj / "rtl").mkdir(parents=True)
    (proj / "rtl" / "MyMod.sv").write_text("module MyMod(); endmodule\n")

    (proj / "sim").mkdir()
    (proj / "sim" / "tb_MyMod.cpp").write_text("int main(){}\n")

    from mxfp4agent.toolchain import runner as R

    captured: list[list[str]] = []
    orig_run, orig_which = R.run, R.which
    R.run = lambda cmd, cwd=None, timeout=900, env=None: (
        captured.append(cmd) or R.CmdResult(cmd, 0, "", ""))
    R.which = lambda n: "/usr/bin/" + n

    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        r = ToolchainRunner("out/MyMod", "MyMod")          # path RELATIVO
        assert r.root.is_absolute()
        assert len(r.rtl_files()) == 1
        r.lint()
        r.build()
    finally:
        os.chdir(cwd)
        R.run, R.which = orig_run, orig_which

    assert captured, "nessun comando eseguito"
    for cmd in captured:
        for arg in cmd:
            # nessun path relativo: né "out/..." (che verrebbe risolto due volte
            # rispetto a cwd=root) né "sim/..." (che rompe il VPATH di obj_dir)
            assert not arg.startswith(("out/", "rtl/", "sim/")), f"path relativo: {arg} in {cmd}"
    build_cmd = captured[-1]
    assert str(proj / "sim" / "tb_MyMod.cpp") in build_cmd
    assert str(proj / "obj_dir") in build_cmd          # anche --Mdir assoluto


def test_non_ascii_path_is_flagged(tmp_path):
    """`.../Università/...` rompe il calcolo del VPATH di Verilator."""
    from mxfp4agent.toolchain import check_path_sanity

    accented = tmp_path / "Università" / "out"
    accented.mkdir(parents=True)
    warns = check_path_sanity(accented)
    assert warns and "non ASCII" in warns[0] and "à" in warns[0]

    clean = tmp_path / "Universita" / "out"
    clean.mkdir(parents=True)
    assert not any("non ASCII" in w for w in check_path_sanity(clean))


def test_fix_prompt_does_not_grow_with_history():
    """La cronologia illimitata faceva troncare il prompt e sparire la spec MXFP4."""
    from mxfp4agent.agents.reviewer import ReviewerAgent
    from mxfp4agent.utils import ExtractedFile

    rev = ReviewerAgent(build_provider("mock"), Log(False, False))
    plan = PlannerAgent(build_provider("mock"), Log(False, False)).run("dot").payload
    files = [ExtractedFile("src/main/scala/mxfp4/M.scala", "class M extends Module {}\n")]

    sizes = []
    for i in range(4):
        rev.fix(plan, files, "elaborate", "errore " * 50, attempt=i + 1,
                previous_attempts=[f"causa {j}" for j in range(i)])
        sizes.append(len(rev.history))
    assert sizes == [0, 0, 0, 0], f"la cronologia cresce: {sizes}"


def test_fix_prompt_warns_against_repeating_a_failed_diagnosis():
    from mxfp4agent.agents.reviewer import ReviewerAgent
    from mxfp4agent.utils import ExtractedFile

    captured = {}

    class Spy(ReviewerAgent):
        def ask(self, prompt, keep_history=True, **kw):
            captured["p"] = prompt
            return "CAUSA: x\n### FILE: a.scala\n```scala\nclass A\n```"

    rev = Spy(build_provider("mock"), Log(False, False))
    plan = PlannerAgent(build_provider("mock"), Log(False, False)).run("dot").payload
    rev.fix(plan, [ExtractedFile("a.scala", "x")], "elaborate", "boom",
            previous_attempts=["convertire accQ2 a UInt", "convertire expCombined"])
    p = captured["p"]
    assert "TENTATIVI GIA' FALLITI" in p
    assert "convertire accQ2 a UInt" in p
    assert "e' SBAGLIATA" in p


def test_chisel_shift_rule_is_documented():
    """Il 14B ha bruciato 4 round su `SInt >> SInt`."""
    from mxfp4agent.knowledge import full_context

    ctx = full_context("chisel")
    assert "MAI `SInt`" in ctx
    assert "cannot be applied to (chisel3.SInt)" in ctx
    assert "Mux(sh < 0.S" in ctx


def test_ollama_output_budget_leaves_room_for_the_prompt():
    from mxfp4agent.llm import DEFAULT_MAX_TOKENS, build_provider

    p = build_provider("ollama")
    assert DEFAULT_MAX_TOKENS["ollama"] <= 4096
    assert p.num_ctx - p.max_tokens >= 24000, "finestra utile troppo stretta per i prompt"


def test_repeated_identical_failure_is_detected():
    """Quattro round identici sono un bug della toolchain, non del design."""
    from mxfp4agent.toolchain.runner import StageResult
    from mxfp4agent.workflow import Workflow

    a = StageResult("lint", False, "%Error: Cannot find file /home/x/out/M/rtl/M.sv")
    b = StageResult("lint", False, "%Error: Cannot find file /other/path/out/M/rtl/M.sv")
    c = StageResult("lint", False, "%Error: sintassi diversa")
    # stesso errore a meno del path assoluto -> stessa impronta
    assert Workflow._signature(a) == Workflow._signature(b)
    assert Workflow._signature(a) != Workflow._signature(c)
    # fasi diverse non collidono mai
    assert Workflow._signature(StageResult("build", False, a.log)) != Workflow._signature(a)


def test_coder_is_told_not_to_write_an_elaboration_entrypoint():
    """Il Fixer aveva aggiunto un `object ...Main` seguendo una regola ambigua."""
    from mxfp4agent.knowledge import full_context

    ctx = full_context("chisel")
    assert "NON scrivere alcun entry-point" in ctx
    assert "_root_.circt.stage.ChiselStage" in ctx
    assert "(-3).S(5.W)" in ctx      # trappola dei letterali negativi


# -------------------------------------------------------------- end-to-end
def test_end_to_end_mock_materializes_project(tmp_path):
    """Verifica lo scaffolding, NON la toolchain esterna.

    sbt/verilator sono volutamente esclusi: il test deve dare lo stesso esito su
    qualunque macchina, e un giro completo di sbt richiederebbe minuti.
    """
    from mxfp4agent.agents.tester import TesterAgent
    from mxfp4agent.toolchain.runner import StageResult, ToolchainReport

    def fake_toolchain(self, plan, round_id=0):
        rep = ToolchainReport()
        for st in ("elaborate", "lint", "build", "simulate"):
            rep.add(StageResult(st, True, "(toolchain simulata nel test)",
                                detail={"passed": 8}))
        return rep

    original = TesterAgent.run_toolchain
    TesterAgent.run_toolchain = fake_toolchain
    try:
        cfg = Config(request="dot product mxfp4 combinatorio", provider="mock",
                     outdir=tmp_path, max_fix_rounds=0, static_review=False,
                     num_random_vectors=8, verbose=False)
        result = Workflow(cfg).run()
    finally:
        TesterAgent.run_toolchain = original

    assert result.ok, result.message
    root = tmp_path / "MXFP4DotProduct"
    assert root.exists()
    for rel in ("build.sbt", "Makefile", "project/build.properties",
                "src/main/scala/mxfp4/MXFP4DotProduct.scala",
                "src/main/scala/mxfp4/Elaborate.scala",
                "sim/tb_MXFP4DotProduct.cpp", "sim/test_vectors.h",
                "plan.json", "prompt_coder.md", "report.json", "README.md"):
        assert (root / rel).exists(), rel
    assert result.plan["module_name"] == "MXFP4DotProduct"
    # senza sbt/verilator installati il risultato non può essere "ok"
    assert result.workdir == root
