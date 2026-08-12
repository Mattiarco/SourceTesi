"""Verifica del golden model MXFP4 — è la fonte di verità, deve essere impeccabile."""
import math
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mxfp4agent.knowledge.golden_model import (BLOCK_SIZE, E8M0_BIAS, E8M0_NAN, MXBlock,
                                               bf16_bits, dot_product,
                                               dot_product_exact_int, e2m1_decode,
                                               e2m1_encode, e8m0_decode,
                                               e8m0_encode_from_exp, elementwise_mul)

EXPECTED = {0: 0.0, 1: 0.5, 2: 1.0, 3: 1.5, 4: 2.0, 5: 3.0, 6: 4.0, 7: 6.0}


def test_e2m1_decode_all_16_codes():
    for c, v in EXPECTED.items():
        assert e2m1_decode(c) == v
        assert e2m1_decode(c | 0x8) == -v


def test_e2m1_subnormal_is_half():
    """Il bug più comune: trattare 0b0001 come zero."""
    assert e2m1_decode(0b0001) == 0.5
    assert e2m1_decode(0b1001) == -0.5


def test_e2m1_roundtrip_encode_decode():
    for c in range(16):
        v = e2m1_decode(c)
        # -0 si ricodifica come -0 (0b1000), non come 0b0000
        assert e2m1_decode(e2m1_encode(v)) == v


def test_e2m1_saturates_instead_of_inf():
    assert e2m1_decode(e2m1_encode(1e9)) == 6.0
    assert e2m1_decode(e2m1_encode(-1e9)) == -6.0
    assert e2m1_decode(e2m1_encode(float("inf"))) == 6.0


@pytest.mark.parametrize("x,expected", [
    (0.24, 0.0), (0.26, 0.5), (0.74, 0.5), (0.76, 1.0),
    (1.24, 1.0), (1.26, 1.5), (2.4, 2.0), (2.6, 3.0), (5.5, 6.0),
    # tie esatti -> mantissa pari (round-to-nearest-even, come da spec OCP)
    (0.25, 0.0), (5.0, 4.0), (2.5, 2.0),
])
def test_e2m1_rounding_nearest(x, expected):
    assert e2m1_decode(e2m1_encode(x)) == expected


def test_e2m1_round_toward_zero_mode():
    assert e2m1_decode(e2m1_encode(5.9, "rtz")) == 4.0
    assert e2m1_decode(e2m1_encode(0.9, "rtz")) == 0.5


def test_e8m0_bias_and_nan():
    assert e8m0_decode(127) == 1.0
    assert e8m0_decode(128) == 2.0
    assert e8m0_decode(126) == 0.5
    assert math.isnan(e8m0_decode(E8M0_NAN))
    assert e8m0_encode_from_exp(0) == E8M0_BIAS


def test_e8m0_is_always_power_of_two():
    for c in range(255):
        v = e8m0_decode(c)
        assert v > 0 and math.log2(v) == int(math.log2(v))


def test_block_packing_nibble_order():
    b = MXBlock(E8M0_BIAS, list(range(16)) + list(range(16)))
    packed = b.packed()
    assert len(packed) == 16
    # elemento pari nel nibble basso
    assert packed[0] & 0xF == 0
    assert packed[0] >> 4 == 1


def test_block_values_apply_scale():
    b = MXBlock(e8m0_encode_from_exp(3), [0b0010] * 4)  # 1.0 * 2^3
    assert b.values() == [8.0] * 4


def test_dot_product_matches_integer_version():
    rng = random.Random(7)
    for _ in range(200):
        a = MXBlock.random(BLOCK_SIZE, rng)
        b = MXBlock.random(BLOCK_SIZE, rng)
        acc_q2, shared = dot_product_exact_int(a, b)
        assert dot_product(a, b) == pytest.approx(acc_q2 / 4.0 * 2.0 ** shared, rel=1e-12)


def test_accumulator_width_bound():
    """Il massimo assoluto dell'accumulatore giustifica la larghezza usata nell'RTL."""
    a = MXBlock(E8M0_BIAS, [0b0111] * BLOCK_SIZE)   # +6
    b = MXBlock(E8M0_BIAS, [0b1111] * BLOCK_SIZE)   # -6
    acc, _ = dot_product_exact_int(a, b)
    assert acc == -BLOCK_SIZE * 144          # 6*2=12 -> 12*12=144
    assert abs(acc) < 2 ** 14                 # 14 bit + segno bastano


def test_quantization_from_floats_ocp():
    xs = [0.0, 1.0, -2.0, 6.0, 3.7, -0.1]
    blk = MXBlock.from_floats(xs)
    vals = blk.values()
    assert vals[0] == 0.0
    assert vals[3] == pytest.approx(6.0, rel=0.2)
    for x, v in zip(xs, vals):
        assert abs(v - x) <= max(abs(x) * 0.5, 0.5)


def test_elementwise_mul_length():
    rng = random.Random(3)
    a, b = MXBlock.random(rng=rng), MXBlock.random(rng=rng)
    assert len(elementwise_mul(a, b)) == BLOCK_SIZE


def test_bf16_round_to_nearest_even():
    assert bf16_bits(1.0) == 0x3F80
    assert bf16_bits(-1.0) == 0xBF80
    assert bf16_bits(0.0) == 0x0000
