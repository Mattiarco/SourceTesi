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


# -------------------------------------------------------------- end-to-end
def test_end_to_end_mock_materializes_project(tmp_path):
    cfg = Config(request="dot product mxfp4 combinatorio", provider="mock",
                 outdir=tmp_path, max_fix_rounds=0, static_review=False,
                 num_random_vectors=8, keep_going=True, verbose=False)
    result = Workflow(cfg).run()
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
