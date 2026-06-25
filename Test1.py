"""
Full Adder — esempio di circuito Python per la conversione in Chisel MXFP4.

Un full adder somma tre bit (a, b, carry_in) e produce sum e carry_out.
Estensione: FullAdderN somma due interi N-bit usando una catena di full adder.
"""

def full_adder_1bit(a: int, b: int, cin: int) -> tuple[int, int]:
    """
    Full adder a 1 bit.
    
    Ingressi: a, b  ∈ {0,1}   — bit da sommare
              cin              — carry in
    Uscite:   sum              — bit di somma
              cout             — carry out
    """
    sum_bit  = a ^ b ^ cin          # XOR a tre vie
    cout     = (a & b) | (cin & (a ^ b))   # carry majority
    return sum_bit, cout


def ripple_carry_adder(a: int, b: int, n_bits: int = 8) -> tuple[int, int]:
    """
    Ripple-Carry Adder a N bit.
    Somma due interi non negativi a N bit e restituisce (risultato, overflow).
    """
    carry = 0
    result = 0

    for i in range(n_bits):
        bit_a = (a >> i) & 1
        bit_b = (b >> i) & 1
        s, carry = full_adder_1bit(bit_a, bit_b, carry)
        result |= (s << i)

    overflow = carry   # carry out dall'MSB
    return result, overflow


def carry_lookahead_adder(a: int, b: int, n_bits: int = 4) -> tuple[int, int]:
    """
    Carry-Lookahead Adder a N bit (versione semplificata).
    Calcola generate (G) e propagate (P) per anticipare i carry.
    """
    G = [(a >> i) & 1 & ((b >> i) & 1) for i in range(n_bits)]   # generate
    P = [((a >> i) & 1) ^ ((b >> i) & 1) for i in range(n_bits)] # propagate

    # Carry lookahead
    C = [0] * (n_bits + 1)
    C[0] = 0  # carry iniziale
    for i in range(n_bits):
        C[i+1] = G[i] | (P[i] & C[i])

    # Somma
    result = 0
    for i in range(n_bits):
        s = P[i] ^ C[i]
        result |= (s << i)

    return result, C[n_bits]


# ── Test di base ──
if __name__ == "__main__":
    print("=== Full Adder 1-bit ===")
    for a in range(2):
        for b in range(2):
            for cin in range(2):
                s, co = full_adder_1bit(a, b, cin)
                print(f"  a={a} b={b} cin={cin} → sum={s} cout={co}")

    print("\n=== Ripple-Carry Adder 8-bit ===")
    for (x, y) in [(15, 20), (100, 156), (255, 1)]:
        r, ov = ripple_carry_adder(x, y, 8)
        print(f"  {x} + {y} = {r}  (overflow={ov})  expected={(x+y) % 256}")

    print("\n=== Carry-Lookahead Adder 4-bit ===")
    for (x, y) in [(5, 3), (7, 9), (15, 1)]:
        r, ov = carry_lookahead_adder(x, y, 4)
        print(f"  {x} + {y} = {r}  (overflow={ov})  expected={(x+y) % 16}")