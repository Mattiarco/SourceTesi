"""Generazione dei vettori di test dal golden model -> header C per Verilator."""
from __future__ import annotations

import random
from dataclasses import dataclass

from ..knowledge.golden_model import (BLOCK_SIZE, E8M0_BIAS, E8M0_NAN, MXBlock,
                                      dot_product, dot_product_exact_int,
                                      e8m0_encode_from_exp, elementwise_add,
                                      elementwise_mul, f32_bits)

HEADER_NAME = "test_vectors.h"

#: contratto mostrato al Coder: il testbench DEVE usare esattamente questi nomi.
HEADER_CONTRACT = r"""
### CONTRATTO DELL'HEADER `test_vectors.h` (generato automaticamente, NON scriverlo tu)

```c
#define MXFP4_K       32          // elementi per blocco
#define MXFP4_WORDS    4          // parole a 32 bit per blocco (K/8)
#define NUM_VECTORS  <N>

typedef struct {
    uint32_t a[MXFP4_WORDS];   // blocco A impacchettato: elemento i nei bit [4i+3:4i]
    uint32_t b[MXFP4_WORDS];   // blocco B
    uint8_t  scale_a;          // E8M0
    uint8_t  scale_b;          // E8M0
    int32_t  exp_acc_q2;       // dot-product atteso, intero in unita' 1/4
    int16_t  exp_shared;       // (scale_a-127)+(scale_b-127)
    uint8_t  nan;              // 1 se una delle scale e' 0xFF
    double   exp_real;         // valore reale atteso (per diagnostica)
    uint32_t exp_elem[MXFP4_K];// risultato element-wise atteso, bit FP32
    const char* label;         // nome del caso ("random", "all_max", ...)
} mxfp4_vec_t;

static const mxfp4_vec_t MXFP4_VECTORS[NUM_VECTORS];
```
Usa `#include "test_vectors.h"` e itera su `NUM_VECTORS`. Confronta solo i campi
pertinenti al kernel implementato.
"""


@dataclass
class Vector:
    a: MXBlock
    b: MXBlock
    label: str

    def words(self, blk: MXBlock) -> list[int]:
        acc = 0
        for i, e in enumerate(blk.elements):
            acc |= (e & 0xF) << (4 * i)
        n = (blk.k + 7) // 8
        return [(acc >> (32 * w)) & 0xFFFFFFFF for w in range(n)]


# --------------------------------------------------------------- generazione
def build_vectors(kernel: str = "dot_product", num_random: int = 64,
                  k: int = BLOCK_SIZE, seed: int = 1234) -> list[Vector]:
    rng = random.Random(seed)
    vecs: list[Vector] = []

    def blk(elems: list[int], scale_exp: int = 0) -> MXBlock:
        return MXBlock(e8m0_encode_from_exp(scale_exp), elems)

    one = 0b0010   # +1.0
    half = 0b0001  # +0.5 (subnormale)
    six = 0b0111   # +6.0
    nsix = 0b1111  # -6.0
    nzero = 0b1000 # -0
    zero = 0b0000

    # --- casi diretti (coprono i bug tipici)
    vecs.append(Vector(blk([zero] * k), blk([zero] * k), "all_zero"))
    vecs.append(Vector(blk([one] * k), blk([one] * k), "all_one"))
    vecs.append(Vector(blk([six] * k), blk([six] * k), "all_max"))
    vecs.append(Vector(blk([nsix] * k), blk([six] * k), "max_negative"))
    vecs.append(Vector(blk([half] * k), blk([half] * k), "all_subnormal"))
    vecs.append(Vector(blk([nzero] * k), blk([one] * k), "negative_zero"))
    vecs.append(Vector(blk([i % 16 for i in range(k)]),
                       blk([(15 - i) % 16 for i in range(k)]), "sweep_codes"))
    vecs.append(Vector(blk([one] * k, -127 + 0), blk([one] * k, 120), "extreme_scales"))
    nan_a = MXBlock(E8M0_NAN, [one] * k)
    vecs.append(Vector(nan_a, blk([one] * k), "scale_nan"))
    vecs.append(Vector(blk([six] * k, 100), blk([six] * k, 100), "scale_overflow"))

    # --- casi casuali
    for i in range(max(0, num_random)):
        vecs.append(Vector(MXBlock.random(k, rng), MXBlock.random(k, rng), f"random_{i}"))
    return vecs


def _c_double(x: float) -> str:
    if x != x:
        return "(0.0/0.0)"
    if x in (float("inf"), float("-inf")):
        return "1e400" if x > 0 else "-1e400"
    return repr(x)


def render_header(vectors: list[Vector], kernel: str = "dot_product",
                  k: int = BLOCK_SIZE) -> str:
    """Genera il contenuto di ``test_vectors.h``."""
    rows: list[str] = []
    for v in vectors:
        acc_q2, shared = dot_product_exact_int(v.a, v.b)
        is_nan = int(v.a.scale == E8M0_NAN or v.b.scale == E8M0_NAN)
        if kernel == "elementwise_mul":
            elems = elementwise_mul(v.a, v.b)
        elif kernel == "elementwise_add":
            elems = elementwise_add(v.a, v.b)
        else:
            elems = [0.0] * k
        real = dot_product(v.a, v.b) if kernel == "dot_product" else 0.0
        aw = ", ".join(f"0x{w:08x}u" for w in v.words(v.a))
        bw = ", ".join(f"0x{w:08x}u" for w in v.words(v.b))
        ew = ", ".join(f"0x{f32_bits(e):08x}u" for e in elems)
        rows.append(
            f"  {{ {{{aw}}}, {{{bw}}}, {v.a.scale}u, {v.b.scale}u, "
            f"{acc_q2}, {shared}, {is_nan}u, {_c_double(real)}, "
            f"{{{ew}}}, \"{v.label}\" }}"
        )

    body = ",\n".join(rows)
    return f"""// GENERATO AUTOMATICAMENTE dal golden model Python — non modificare a mano.
// kernel = {kernel}, K = {k}, vettori = {len(vectors)}
#ifndef MXFP4_TEST_VECTORS_H
#define MXFP4_TEST_VECTORS_H

#include <stdint.h>

#define MXFP4_K      {k}
#define MXFP4_WORDS  {(k + 7) // 8}
#define NUM_VECTORS  {len(vectors)}
#define MXFP4_KERNEL "{kernel}"
#define E8M0_BIAS    {E8M0_BIAS}

typedef struct {{
    uint32_t    a[MXFP4_WORDS];
    uint32_t    b[MXFP4_WORDS];
    uint8_t     scale_a;
    uint8_t     scale_b;
    int32_t     exp_acc_q2;
    int16_t     exp_shared;
    uint8_t     nan;
    double      exp_real;
    uint32_t    exp_elem[MXFP4_K];
    const char* label;
}} mxfp4_vec_t;

static const mxfp4_vec_t MXFP4_VECTORS[NUM_VECTORS] = {{
{body}
}};

#endif // MXFP4_TEST_VECTORS_H
"""


def write_header(path, kernel: str = "dot_product", num_random: int = 64,
                 k: int = BLOCK_SIZE, seed: int = 1234) -> int:
    vecs = build_vectors(kernel, num_random, k, seed)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_header(vecs, kernel, k), encoding="utf-8")
    return len(vecs)
