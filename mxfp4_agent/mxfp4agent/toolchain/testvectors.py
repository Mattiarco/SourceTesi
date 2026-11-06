"""Generazione dei vettori di test dal golden model -> header C per Verilator."""
from __future__ import annotations

import random
from dataclasses import dataclass

from ..knowledge.golden_model import (BLOCK_SIZE, E8M0_BIAS, E8M0_NAN, MXBlock,
                                      bf16_bits, dot_product, dot_product_exact_int,
                                      e8m0_encode_from_exp, elementwise_add,
                                      elementwise_mul, f32_bits)

#: Coppie (esponente scala A, esponente scala B) per il test esaustivo element-wise.
#: Scelte perché ogni risultato resti un binary32 NORMALE (prodotti: |ea+eb| <= 120;
#: magnitudini dei prodotti E2M1 in [2^-2, 36]) e perché le somme siano esatte in
#: double prima dell'unico arrotondamento a binary32 (|ea-eb| <= 20).
EXHAUSTIVE_SCALES = {
    "elementwise_mul": [(0, 0), (1, -1), (-1, -2), (5, 3), (-60, -60), (60, 60),
                        (100, -100), (-63, 0)],
    "elementwise_add": [(0, 0), (1, 0), (0, 3), (-5, -5), (20, 0), (-60, -58), (60, 61)],
}

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

    # I kernel element-wise producono FP32: con scale estreme il risultato
    # uscirebbe dal range di binary32 e il confronto bit-exact perderebbe senso.
    # Per il dot-product il problema non esiste (accumulo intero + esponente).
    elementwise = kernel.startswith("elementwise")
    lo, hi = (-50, 50) if elementwise else (-127, 120)
    rand_range = (-50, 50) if elementwise else (-127, 127) if kernel == "to_bf16" else (-8, 8)

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
    vecs.append(Vector(blk([one] * k, lo), blk([one] * k, hi), "extreme_scales"))
    nan_a = MXBlock(E8M0_NAN, [one] * k)
    vecs.append(Vector(nan_a, blk([one] * k), "scale_nan"))
    vecs.append(Vector(blk([six] * k, hi), blk([six] * k, hi), "scale_large"))

    # --- casi esaustivi (unità element-wise e conversioni)
    vecs.extend(exhaustive_vectors(kernel, k))

    # --- casi casuali
    for i in range(max(0, num_random)):
        vecs.append(Vector(MXBlock.random(k, rng, rand_range),
                           MXBlock.random(k, rng, rand_range), f"random_{i}"))
    return vecs


def exhaustive_vectors(kernel: str, k: int = BLOCK_SIZE) -> list[Vector]:
    """Copertura completa dello spazio degli ingressi per elemento.

    * element-wise: tutte le 16x16 coppie di codici E2M1 (256 coppie = 256/k
      blocchi) per ogni coppia di scale in EXHAUSTIVE_SCALES, più la scala NaN
      su ciascun operando;
    * to_bf16: tutti i 16 codici per TUTTE le 256 scale E8M0 (NaN compresa):
      è l'intero dominio della conversione di un elemento.
    Per il dot-product l'esaustivo non ha senso (16^64 combinazioni per blocco):
    restano i casi diretti e quelli casuali.
    """
    out: list[Vector] = []
    if kernel in EXHAUSTIVE_SCALES:
        pairs = [(p // 16, p % 16) for p in range(256)]
        blocks = [pairs[i:i + k] for i in range(0, 256, k)]
        for ea, eb in EXHAUSTIVE_SCALES[kernel]:
            for j, blk in enumerate(blocks):
                ca = [c for c, _ in blk] + [0] * (k - len(blk))
                cb = [c for _, c in blk] + [0] * (k - len(blk))
                out.append(Vector(MXBlock(e8m0_encode_from_exp(ea), ca),
                                  MXBlock(e8m0_encode_from_exp(eb), cb),
                                  f"exh_s{ea}_{eb}_b{j}"))
        ca = [i % 16 for i in range(k)]
        cb = [(15 - i) % 16 for i in range(k)]
        out.append(Vector(MXBlock(E8M0_NAN, ca), MXBlock(E8M0_BIAS, cb), "exh_nan_a"))
        out.append(Vector(MXBlock(E8M0_BIAS, ca), MXBlock(E8M0_NAN, cb), "exh_nan_b"))
    elif kernel == "to_bf16":
        codes = [i % 16 for i in range(k)]
        zero_b = MXBlock(E8M0_BIAS, [0] * k)
        for s in range(256):
            out.append(Vector(MXBlock(s, codes), zero_b, f"exh_scale{s}"))
    return out


def expected_elements(v: Vector, kernel: str, k: int = BLOCK_SIZE) -> list[int]:
    """Bit attesi di ogni elemento del vettore `out` (0 per i kernel scalari)."""
    if kernel == "elementwise_mul":
        return [f32_bits(e) for e in elementwise_mul(v.a, v.b)]
    if kernel == "elementwise_add":
        return [f32_bits(e) for e in elementwise_add(v.a, v.b)]
    if kernel == "to_bf16":
        return [bf16_bits(x) for x in v.a.values()]
    return [0] * k


def expected_nan(v: Vector, kernel: str) -> int:
    if kernel == "to_bf16":            # unico operando: conta solo la scala di A
        return int(v.a.scale == E8M0_NAN)
    return int(v.a.scale == E8M0_NAN or v.b.scale == E8M0_NAN)


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
        is_nan = expected_nan(v, kernel)
        real = dot_product(v.a, v.b) if kernel == "dot_product" else 0.0
        aw = ", ".join(f"0x{w:08x}u" for w in v.words(v.a))
        bw = ", ".join(f"0x{w:08x}u" for w in v.words(v.b))
        ew = ", ".join(f"0x{e:08x}u" for e in expected_elements(v, kernel, k))
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
