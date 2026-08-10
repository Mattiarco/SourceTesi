from amaranth import Module, Signal

class FullAdder(Module):
    def __init__(self):
        self.A = Signal()
        self.B = Signal()
        self.Cin = Signal()
        self.S = Signal()
        self.Cout = Signal()

        # XOR_A_B
        xor_ab = Signal()
        self.comb += xor_ab.eq(self.A ^ self.B)

        # AND_A_Cin
        and_acin = Signal()
        self.comb += and_acin.eq(self.A & self.Cin)

        # AND_B_Cin
        and_bcin = Signal()
        self.comb += and_bcin.eq(self.B & self.Cin)

        # OR_ANDs
        or_ands = Signal()
        self.comb += or_ands.eq(and_acin | and_bcin)

        # XOR_XOR_AB_Cout
        xor_ab_cout = Signal()
        self.comb += xor_ab_cout.eq(xor_ab ^ or_ands)

        # AND_XOR_AB_OR_ANDs
        and_xor_ab_or_ands = Signal()
        self.comb += and_xor_ab_or_ands.eq(xor_ab & or_ands)

        # Assign outputs
        self.comb += [
            self.S.eq(xor_ab_cout),
            self.Cout.eq(and_xor_ab_or_ands)
        ]