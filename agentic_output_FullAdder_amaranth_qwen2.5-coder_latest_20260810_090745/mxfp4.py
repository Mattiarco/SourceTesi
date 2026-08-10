"""Formato MXFP4 E2M1 (OCP MX Specification v1.0): 4 bit totali.

  bit[3]   = segno      (0 = positivo, 1 = negativo)
  bit[2:1] = esponente a 2 bit (bias = 1)
  bit[0]   = mantissa a 1 bit

Valore: (-1)^segno * 2^(exp-1) * (1 + mant*0.5), tranne il caso subnormale
(exp == 0, include lo zero) dove NON c'e' bit implicito:
valore = (-1)^segno * mant*0.5.
"""
from amaranth.lib import data


class MXFP4Layout(data.Struct):
    # Ordine di dichiarazione = ordine dei bit da LSB a MSB in
    # amaranth.lib.data.Struct: mant (bit 0), exp (bit 2:1), sign (bit 3).
    mant: 1
    exp: 2
    sign: 1


def decode(bits: int) -> float:
    """Converte i 4 bit codificati (0..15) nel valore Double rappresentato."""
    sign = (bits >> 3) & 0x1
    exp  = (bits >> 1) & 0x3
    mant = bits & 0x1
    s = -1.0 if sign == 1 else 1.0
    if exp == 0:
        return s * mant * 0.5
    return s * (1.0 + mant * 0.5) * (2.0 ** (exp - 1))


def encode(value: float) -> int:
    """Converte un valore Double nella codifica MXFP4 (0..15) piu' vicina
    (round-to-nearest)."""
    best_bits, best_diff = 0, float("inf")
    for bits in range(16):
        diff = abs(decode(bits) - value)
        if diff < best_diff:
            best_diff, best_bits = diff, bits
    return best_bits
