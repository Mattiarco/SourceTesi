from amaranth import Module, Signal, Elaboratable, ClockDomain, ResetSignal
from amaranth.sim import Simulator

class FullAdderTestbench(Elaboratable):
    def __init__(self):
        self.A = Signal()
        self.B = Signal()
        self.Cin = Signal()
        self.S = Signal()
        self.Cout = Signal()

    def elaborate(self, platform):
        m = Module()
        m.submodules.full_adder = full_adder = FullAdder()

        # Connect signals
        m.d.comb += [
            full_adder.A.eq(self.A),
            full_adder.B.eq(self.B),
            full_adder.Cin.eq(self.Cin),
            self.S.eq(full_adder.S),
            self.Cout.eq(full_adder.Cout)
        ]

        return m

def test_full_adder():
    tb = FullAdderTestbench()
    sim = Simulator(tb)

    def process():
        for a, b, cin in [(0, 0, 0), (0, 0, 1), (0, 1, 0), (0, 1, 1),
                          (1, 0, 0), (1, 0, 1), (1, 1, 0), (1, 1, 1)]:
            yield tb.A.eq(a)
            yield tb.B.eq(b)
            yield tb.Cin.eq(cin)
            yield
            yield from sim.sleep(1)

    sim.add_process(process)
    with sim.run_async() as run_obj:
        while not run_obj.done():
            pass

test_full_adder()