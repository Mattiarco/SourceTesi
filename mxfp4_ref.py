"""
mxfp4_ref.py
============
Modello di riferimento bit-accurato per il formato MXFP4 a livello di
SINGOLO ELEMENTO (E2M1: 1 segno, 2 bit esponente, 1 bit mantissa, bias=1).

Scopo
-----
Fornire una codifica/decodifica e delle operazioni aritmetiche (add, mul,
confronto) indipendenti dal codice Chisel generato dagli agenti LLM, cosi'
da poter validare il framework agentico contro un "golden model" invece
che confrontare il codice generato solo con se stesso (o con valori attesi
scritti a mano, potenzialmente affetti dallo stesso errore concettuale).

Nota sullo scope (IMPORTANTE per la tesi)
------------------------------------------
Questo modulo implementa SOLO il formato elemento E2M1. Il formato MXFP4
completo, come definito dalla OCP Microscaling (MX) Specification v1.0,
e' un formato block floating-point: un blocco di 32 elementi E2M1
condivide un fattore di scala a 8 bit in formato E8M0 (potenza di due).
Il framework agentico descritto nella tesi genera unita' aritmetiche a
livello di singolo elemento E2M1 (il "building block" di MXFP4), NON
unita' che gestiscono il block-scaling a 32 elementi. Questo limite di
scope va dichiarato esplicitamente nella tesi (capitolo su MXFP4 / stato
dell'arte) per evitare l'errore concettuale di presentare le unita'
generate come "acceleratori MXFF4 completi".

Tabella di riferimento E2M1 (bias = 1)
---------------------------------------
    bits   sign exp mant   valore
    0000    0   00   0      0.0
    0001    0   00   1      0.5   (subnormale: bit implicito = 0)
    0010    0   01   0      1.0
    0011    0   01   1      1.5
    0100    0   10   0      2.0
    0101    0   10   1      3.0
    0110    0   11   0      4.0
    0111    0   11   1      6.0
    1xxx    1   ..   .      -valore corrispondente

Formula:
    se exp == 0 (subnormale): valore = (-1)^sign * mant * 2^(1-bias)
    se exp != 0 (normale):    valore = (-1)^sign * (1 + mant/2) * 2^(exp-bias)

con bias = 1.
"""

from __future__ import annotations
from dataclasses import dataclass
from itertools import product

BIAS = 1
MAX_VALUE = 6.0   # 0b0111 = +6.0, il massimo rappresentabile in E2M1


@dataclass(frozen=True)
class Fields:
    sign: int   # 0 o 1
    exp: int    # 0..3
    mant: int   # 0 o 1


def fields_of(bits: int) -> Fields:
    """Scompone un codice a 4 bit (0..15) nei tre campi sign/exp/mant."""
    if not (0 <= bits <= 15):
        raise ValueError(f"bits fuori range 4 bit: {bits}")
    sign = (bits >> 3) & 0x1
    exp = (bits >> 1) & 0x3
    mant = bits & 0x1
    return Fields(sign, exp, mant)


def bits_of(f: Fields) -> int:
    """Ricompone i tre campi in un codice a 4 bit."""
    return ((f.sign & 1) << 3) | ((f.exp & 3) << 1) | (f.mant & 1)


def decode(bits: int) -> float:
    """Decodifica un codice E2M1 a 4 bit nel valore reale rappresentato.

    Gestisce ESPLICITAMENTE il caso subnormale (exp == 0): a differenza
    del codice Chisel originale della tesi, qui il bit implicito NON e'
    mai assunto pari a 1 quando l'esponente e' zero.
    """
    f = fields_of(bits)
    if f.exp == 0:
        magnitude = (f.mant / 2.0) * (2.0 ** (1 - BIAS))  # subnormale
    else:
        magnitude = (1.0 + f.mant / 2.0) * (2.0 ** (f.exp - BIAS))  # normale
    return -magnitude if f.sign else magnitude


def encode(value: float) -> int:
    """Codifica un valore reale nel codice E2M1 a 4 bit piu' vicino
    (round-to-nearest, pareggio verso il codice con mantissa pari;
    saturazione a +-6.0 in caso di overflow, come nel caso di studio
    del moltiplicatore MXFP4 della tesi)."""
    if value != value:  # NaN non rappresentabile in questo formato minimale
        raise ValueError("NaN non gestito da questo modello di riferimento")

    saturated = max(-MAX_VALUE, min(MAX_VALUE, value))

    best_bits, best_err, best_mant = 0, None, None
    for bits in range(16):
        candidate = decode(bits)
        err = abs(candidate - saturated)
        f = fields_of(bits)
        if best_err is None or err < best_err or (
            err == best_err and f.mant < best_mant
        ):
            best_bits, best_err, best_mant = bits, err, f.mant
    return best_bits


# ---------------------------------------------------------------------
# Operazioni aritmetiche di riferimento (a livello di singolo elemento)
# ---------------------------------------------------------------------

def ref_add(a_bits: int, b_bits: int) -> int:
    """Somma di riferimento: decodifica, somma in floating point Python,
    ricodifica con saturazione. E' la definizione "per costruzione"
    corretta contro cui confrontare l'addizionatore Chisel generato."""
    return encode(decode(a_bits) + decode(b_bits))


def ref_mul(a_bits: int, b_bits: int) -> int:
    """Prodotto di riferimento, stessa logica di ref_add."""
    return encode(decode(a_bits) * decode(b_bits))


def ref_gt(a_bits: int, b_bits: int) -> bool:
    """Confronto 'maggiore' di riferimento."""
    return decode(a_bits) > decode(b_bits)


# ---------------------------------------------------------------------
# Generazione di vettori di test esaustivi (tutte le 16x16 combinazioni)
# ---------------------------------------------------------------------

def exhaustive_binary_vectors(op: str) -> list[dict]:
    """Genera i vettori di test per TUTTE le 256 combinazioni di due
    ingressi MXFP4 (16 x 16), con l'uscita attesa calcolata dal modello
    di riferimento. op in {"add", "mul", "cmp"}."""
    if op not in ("add", "mul", "cmp"):
        raise ValueError(f"operazione non supportata: {op}")

    vectors = []
    for a_bits, b_bits in product(range(16), range(16)):
        if op == "add":
            expected = ref_add(a_bits, b_bits)
            ef = fields_of(expected)
            vectors.append({
                "a": a_bits, "b": b_bits,
                "expected_sign": ef.sign, "expected_exp": ef.exp,
                "expected_mant": ef.mant,
            })
        elif op == "mul":
            expected = ref_mul(a_bits, b_bits)
            ef = fields_of(expected)
            vectors.append({
                "a": a_bits, "b": b_bits,
                "expected_sign": ef.sign, "expected_exp": ef.exp,
                "expected_mant": ef.mant,
            })
        else:  # cmp
            vectors.append({
                "a": a_bits, "b": b_bits,
                "expected_gt": ref_gt(a_bits, b_bits),
            })
    return vectors


def self_check() -> None:
    """Verifica il modello di riferimento contro la tabella E2M1
    documentata nella tesi (Tabella 4.1). Va eseguito una volta come
    controllo di non-regressione del golden model stesso."""
    known = {
        0b0000: 0.0, 0b0001: 0.5, 0b0010: 1.0, 0b0011: 1.5,
        0b0100: 2.0, 0b0101: 3.0, 0b0110: 4.0, 0b0111: 6.0,
    }
    for bits, expected in known.items():
        got = decode(bits)
        assert got == expected, f"bits={bits:04b}: atteso {expected}, ottenuto {got}"
        neg_bits = bits | 0b1000
        assert decode(neg_bits) == -expected, f"segno negativo errato per {bits:04b}"
    for bits in range(16):
        if bits == 0b1000:
            continue
        assert encode(decode(bits)) == bits, f"round-trip fallito per bits={bits:04b}"
    assert encode(decode(0b1000)) == 0b0000, "zero negativo non canonicalizzato a zero positivo"
    print("mxfp4_ref: self-check superato (16/16 codici validati contro Tabella 4.1).")


if __name__ == "__main__":
    self_check()
    print(f"\nEsempio: 1.0 + 1.0 -> bits {ref_add(0b0010, 0b0010):04b} "
          f"({decode(ref_add(0b0010, 0b0010))})")
    print(f"Esempio: 4.0 * 2.0 -> bits {ref_mul(0b0110, 0b0100):04b} "
          f"({decode(ref_mul(0b0110, 0b0100))})  [atteso: saturazione a 6.0]")
