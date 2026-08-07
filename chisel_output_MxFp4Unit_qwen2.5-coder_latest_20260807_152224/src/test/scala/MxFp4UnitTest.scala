// ═══════════════════════════════════════════════════════════
//  Generato da: agentic_chisel_mxfp4_ollama.py
//  Modello Ollama: qwen2.5-coder:latest
//  Data: 2026-08-07T15:22:24
// ═══════════════════════════════════════════════════════════

import chisel3._
import chiseltest._

class FullAdder1BitTest extends FreeSpec with ChiselScalatestTester {
  "Full Adder 1-bit" in {
    test(new FullAdder1Bit) { dut =>
      // Test all possible inputs
      Seq(
        (0.U, 0.U, 0.U),
        (0.U, 0.U, 1.U),
        (0.U, 1.U, 0.U),
        (0.U, 1.U, 1.U),
        (1.U, 0.U, 0.U),
        (1.U, 0.U, 1.U),
        (1.U, 1.U, 0.U),
        (1.U, 1.U, 1.U)
      ).foreach { case (A, B, C_in) =>
        dut.io.A.poke(A)
        dut.io.B.poke(B)
        dut.io.C_in.poke(C_in)

        dut.clock.step(1)

        assert(dut.io.Sum.peek().litValue() === (A + B + C_in)(0).litValue(), s"Sum mismatch for inputs ($A, $B, $C_in)")
        assert(dut.io.C_out.peek().litValue() === (A + B + C_in)(1).litValue(), s"C_out mismatch for inputs ($A, $B, $C_in)")
      }
    }
  }
}