"""Scelta del design di riferimento per il few-shot."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mxfp4agent.agents import CoderAgent  # noqa: E402
from mxfp4agent.knowledge.references import (BUILTIN, load_references,  # noqa: E402
                                             select_reference)
from mxfp4agent.llm import build_provider  # noqa: E402
from mxfp4agent.utils import Log  # noqa: E402


def _plan(kernel, clocking):
    return {"module_name": "X", "clocking": clocking, "test_plan": {"kernel": kernel}}


def test_the_four_references_are_found():
    keys = [r.key for r in load_references()]
    assert keys == ["01_dot_product", "02_pipelined", "03_elementwise", "04_to_bf16"], keys


def test_selection_follows_kernel_then_clocking():
    assert select_reference(_plan("dot_product", "combinational")).key == "01_dot_product"
    assert select_reference(_plan("dot_product", "sequential")).key == "02_pipelined"
    assert select_reference(_plan("elementwise_mul", "combinational")).key == "03_elementwise"
    # stessa famiglia element-wise, kernel diverso
    assert select_reference(_plan("elementwise_add", "sequential")).key == "03_elementwise"
    assert select_reference(_plan("to_bf16", "combinational")).key == "04_to_bf16"
    # kernel sconosciuto: decide il clocking
    assert select_reference(_plan("custom", "sequential")).key == "02_pipelined"


def test_missing_reference_dir_falls_back_to_builtin(tmp_path):
    assert select_reference(_plan("dot_product", "combinational"),
                            load_references(tmp_path / "nope")) is BUILTIN


def test_coder_prompt_contains_the_reference_only_when_given():
    seen = []
    prov = build_provider("mock")
    orig = prov._chat
    prov._chat = lambda system, msgs, **kw: (seen.append(msgs[-1].content), orig(system, msgs, **kw))[1]
    coder = CoderAgent(prov, Log(False, False))
    plan = _plan("dot_product", "sequential") | {"module_name": "MXFP4DotProduct"}
    coder.run(plan, "PROMPT", reference=select_reference(plan))
    assert "DESIGN DI RIFERIMENTO VERIFICATO (MXFP4DotProductPipe" in seen[-1]
    coder.reset()
    coder.run(plan, "PROMPT", reference=None)
    assert "DESIGN DI RIFERIMENTO" not in seen[-1]
