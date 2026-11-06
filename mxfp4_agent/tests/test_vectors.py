"""Vettori di test: casi esaustivi per element-wise e conversione BF16."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mxfp4agent.knowledge.golden_model import MXBlock, bf16_bits, e2m1_decode  # noqa: E402
from mxfp4agent.toolchain.testvectors import (EXHAUSTIVE_SCALES, build_vectors,  # noqa: E402
                                              expected_elements, exhaustive_vectors)


def test_elementwise_exhaustive_covers_all_code_pairs():
    for kernel, scales in EXHAUSTIVE_SCALES.items():
        vecs = exhaustive_vectors(kernel, 32)
        assert len(vecs) == len(scales) * 8 + 2
        a = vecs[0].a.elements + [x for v in vecs[1:8] for x in v.a.elements]
        b = vecs[0].b.elements + [x for v in vecs[1:8] for x in v.b.elements]
        assert set(zip(a, b)) == {(i, j) for i in range(16) for j in range(16)}


def test_exhaustive_fp32_results_are_normal_or_zero():
    """Le scale scelte devono evitare overflow/underflow di binary32 (bit-exact sensato)."""
    for kernel in EXHAUSTIVE_SCALES:
        for v in exhaustive_vectors(kernel, 32):
            if "nan" in v.label:
                continue
            for bits in expected_elements(v, kernel):
                exp = (bits >> 23) & 0xFF
                assert exp != 0xFF and (exp != 0 or bits & 0x7FFFFF == 0), (v.label, hex(bits))


def test_bf16_exhaustive_covers_every_scale_and_code():
    vecs = exhaustive_vectors("to_bf16", 32)
    assert sorted(v.a.scale for v in vecs) == list(range(256))
    assert all(set(v.a.elements) == set(range(16)) for v in vecs)
    by_scale = {v.a.scale: expected_elements(v, "to_bf16") for v in vecs}
    assert by_scale[127][:8] == [0x0000, 0x3F00, 0x3F80, 0x3FC0, 0x4000, 0x4040, 0x4080, 0x40C0]
    assert by_scale[0][1] == 0x0020          # 0.5 * 2^-127 = 2^-128: subnormale BF16 esatto
    assert by_scale[254][7] == 0x7F80        # 6 * 2^127: overflow -> +inf
    assert by_scale[254][15] == 0xFF80       # -6 * 2^127: -inf
    assert set(by_scale[255]) == {0x7FC0}    # scala NaN
    assert by_scale[127][8] == 0x8000        # -0


def test_bf16_expected_values_match_the_golden_model():
    v = exhaustive_vectors("to_bf16", 32)[130]
    s = 2.0 ** (v.a.scale - 127)
    assert expected_elements(v, "to_bf16") == [bf16_bits(e2m1_decode(c) * s) for c in v.a.elements]


def test_to_bf16_nan_flag_ignores_operand_b():
    v = exhaustive_vectors("to_bf16", 32)[0]
    from mxfp4agent.toolchain.testvectors import expected_nan
    v.b = MXBlock(0xFF, [0] * 32)
    assert expected_nan(v, "to_bf16") == 0


def test_dot_product_vectors_unchanged_by_exhaustive_mode():
    assert exhaustive_vectors("dot_product") == []
    assert len(build_vectors("dot_product", 4)) == 14
