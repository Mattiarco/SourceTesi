"""Golden (reference) model for the OCP MXFP4 format.

MXFP4 = block of ``K`` elements in FP4 **E2M1** sharing one **E8M0** scale.

E2M1 element (4 bit)::

    [3]   sign
    [2:1] exponent (bias = 1)
    [0]   mantissa

    exp==0 & man==0  -> +/- 0
    exp==0 & man==1  -> +/- 0.5              (subnormale)
    exp!=0           -> +/- 2^(exp-1) * (1 + man/2)

    Insieme dei valori rappresentabili:
    {0, 0.5, 1, 1.5, 2, 3, 4, 6} con segno.  Nessun Inf/NaN.

E8M0 scale (8 bit, unsigned)::

    X == 255 -> NaN
    altrimenti valore = 2^(X - 127)      (range 2^-127 .. 2^127)

Questo modulo è la *fonte di verità* usata per generare i vettori di test dati
in pasto a Verilator: l'hardware prodotto dagli agenti deve corrispondere
bit-a-bit ai risultati calcolati qui.
"""
from __future__ import annotations

import math
import random
import struct
from dataclasses import dataclass, field

BLOCK_SIZE = 32
E2M1_BIAS = 1
E8M0_BIAS = 127
E8M0_NAN = 0xFF

#: valore assoluto per ognuno dei 8 pattern exp/mantissa (indice = codice a 3 bit)
E2M1_MAGNITUDE: tuple[float, ...] = (0.0, 0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 6.0)
E2M1_MAX = 6.0


# --------------------------------------------------------------------- E2M1
def e2m1_decode(code: int) -> float:
    """Decodifica un nibble E2M1 (0..15) nel suo valore reale."""
    code &= 0xF
    mag = E2M1_MAGNITUDE[code & 0x7]
    return -mag if code & 0x8 else mag


def e2m1_encode(value: float, mode: str = "rne") -> int:
    """Codifica un float in E2M1 con saturazione a +/-6.

    ``mode``: ``rne`` (round-to-nearest-even, default OCP) oppure ``rtz``.
    """
    if math.isnan(value):
        return 0b0111  # E2M1 non ha NaN: si satura al massimo (convenzione OCP)
    sign = 0x8 if (value < 0 or (value == 0 and math.copysign(1.0, value) < 0)) else 0x0
    a = abs(value)
    if math.isinf(a) or a >= E2M1_MAX:
        return sign | 0x7
    if mode == "rtz":
        best = 0
        for i in range(7, -1, -1):
            if E2M1_MAGNITUDE[i] <= a:
                best = i
                break
        return sign | best
    # round-to-nearest, ties-to-even (sul bit meno significativo del codice)
    best_i, best_d = 0, float("inf")
    for i, m in enumerate(E2M1_MAGNITUDE):
        d = abs(a - m)
        if d < best_d - 1e-12:
            best_i, best_d = i, d
        elif abs(d - best_d) <= 1e-12 and (i & 1) == 0:
            best_i = i  # tie -> codice pari
    return sign | best_i


# --------------------------------------------------------------------- E8M0
def e8m0_decode(code: int) -> float:
    code &= 0xFF
    if code == E8M0_NAN:
        return float("nan")
    return 2.0 ** (code - E8M0_BIAS)


def e8m0_encode_from_exp(exp: int) -> int:
    """Codifica 2^exp; satura nel range rappresentabile."""
    return max(0, min(254, exp + E8M0_BIAS))


def e8m0_encode(value: float) -> int:
    if value <= 0 or math.isnan(value) or math.isinf(value):
        return E8M0_NAN if math.isnan(value) else 0
    return e8m0_encode_from_exp(int(math.floor(math.log2(value))))


# -------------------------------------------------------------------- block
@dataclass
class MXBlock:
    """Un blocco MXFP4: ``scale`` E8M0 + ``K`` nibble E2M1."""

    scale: int  # 0..255
    elements: list[int] = field(default_factory=list)  # nibble 0..15

    def __post_init__(self) -> None:
        if not all(0 <= e <= 0xF for e in self.elements):
            raise ValueError("gli elementi E2M1 devono stare in 0..15")

    # -------------------------------------------------------------- helpers
    @property
    def k(self) -> int:
        return len(self.elements)

    def values(self) -> list[float]:
        s = e8m0_decode(self.scale)
        return [s * e2m1_decode(e) for e in self.elements]

    def packed(self) -> bytes:
        """Impacchetta gli elementi, 2 nibble per byte (low nibble = indice pari)."""
        out = bytearray()
        for i in range(0, self.k, 2):
            lo = self.elements[i]
            hi = self.elements[i + 1] if i + 1 < self.k else 0
            out.append((hi << 4) | lo)
        return bytes(out)

    def packed_hex(self) -> str:
        """Stringa esadecimale big-endian del vettore concatenato (elem 0 nei LSB)."""
        acc = 0
        for i, e in enumerate(self.elements):
            acc |= (e & 0xF) << (4 * i)
        return f"{acc:0{self.k}x}"

    # ----------------------------------------------------------- costruttori
    @classmethod
    def from_floats(cls, xs: list[float], mode: str = "rne") -> "MXBlock":
        """Quantizza una lista di float secondo l'algoritmo OCP MX.

        ``shared_exp = floor(log2(max|x|)) - emax_elem`` con ``emax_elem = 2``
        per E2M1 (l'esponente del valore massimo 6.0 è 2).
        """
        amax = max((abs(x) for x in xs if not math.isnan(x)), default=0.0)
        if amax == 0.0 or math.isinf(amax):
            shared = E8M0_BIAS if amax == 0.0 else 254
        else:
            shared = e8m0_encode_from_exp(int(math.floor(math.log2(amax))) - 2)
        s = e8m0_decode(shared)
        return cls(shared, [e2m1_encode(x / s, mode) for x in xs])

    @classmethod
    def random(cls, k: int = BLOCK_SIZE, rng: random.Random | None = None,
               exp_range: tuple[int, int] = (-8, 8)) -> "MXBlock":
        rng = rng or random.Random()
        return cls(e8m0_encode_from_exp(rng.randint(*exp_range)),
                   [rng.randrange(16) for _ in range(k)])


# ------------------------------------------------------------------ kernels
def dot_product(a: MXBlock, b: MXBlock) -> float:
    """Prodotto scalare esatto fra due blocchi (accumulo in FP64)."""
    if a.k != b.k:
        raise ValueError("blocchi di lunghezza diversa")
    sa, sb = e8m0_decode(a.scale), e8m0_decode(b.scale)
    acc = 0.0
    for x, y in zip(a.elements, b.elements):
        acc += e2m1_decode(x) * e2m1_decode(y)
    return acc * sa * sb


def elementwise_mul(a: MXBlock, b: MXBlock) -> list[float]:
    return [x * y for x, y in zip(a.values(), b.values())]


def elementwise_add(a: MXBlock, b: MXBlock) -> list[float]:
    return [x + y for x, y in zip(a.values(), b.values())]


def dot_product_exact_int(a: MXBlock, b: MXBlock) -> tuple[int, int]:
    """Versione intera del dot-product, utile per confronti bit-exact.

    Ogni magnitudine E2M1 è un multiplo di 0.5 -> la si rappresenta come intero
    ``mag*2``.  Il prodotto di due elementi è quindi un intero in unità 1/4.
    Ritorna ``(acc_q2, exp_totale)`` con valore reale = ``acc_q2 / 4 * 2^exp``.
    """
    q = [int(round(m * 2)) for m in E2M1_MAGNITUDE]
    acc = 0
    for x, y in zip(a.elements, b.elements):
        mx = q[x & 7] * (-1 if x & 8 else 1)
        my = q[y & 7] * (-1 if y & 8 else 1)
        acc += mx * my
    return acc, (a.scale - E8M0_BIAS) + (b.scale - E8M0_BIAS)


# ------------------------------------------------------------------ FP32/16
def f32_bits(x: float) -> int:
    return struct.unpack("<I", struct.pack("<f", x))[0]


def bits_f32(b: int) -> float:
    return struct.unpack("<f", struct.pack("<I", b & 0xFFFFFFFF))[0]


def bf16_bits(x: float) -> int:
    """Round-to-nearest-even su bfloat16."""
    b = f32_bits(x)
    lsb = (b >> 16) & 1
    return ((b + 0x7FFF + lsb) >> 16) & 0xFFFF


def summary() -> str:
    lines = ["code | bits | value", "-----+------+-------"]
    for c in range(16):
        lines.append(f" {c:>3} | {c:04b} | {e2m1_decode(c):>6}")
    return "\n".join(lines)


if __name__ == "__main__":  # pragma: no cover
    print(summary())
    rng = random.Random(0)
    a, b = MXBlock.random(rng=rng), MXBlock.random(rng=rng)
    print("scale a/b:", a.scale, b.scale, "dot =", dot_product(a, b))
