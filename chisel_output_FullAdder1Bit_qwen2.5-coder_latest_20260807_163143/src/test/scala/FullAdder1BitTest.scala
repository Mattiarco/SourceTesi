// ═══════════════════════════════════════════════════════════
//  Generato da: agentic_chisel_mxfp4_ollama.py
//  Modello Ollama: qwen2.5-coder:latest
//  Data: 2026-08-07T16:31:43
// ═══════════════════════════════════════════════════════════

import chisel3._
import chiseltest._
import org.scalatest.freespec.AnyFreeSpec

class FullAdder1BitTest extends AnyFreeSpec with ChiselScalatestTester {
  "FullAdder1Bit" in {
    test(new FullAdder1Bit) { dut =>
      // Test case 1: A = 0, B = 0, Cin = 0
      dut.io.A.poke(0.U)
      dut.io.B.poke(0.U)
      dut.io.Cin.poke(0.U)
      dut.clock.step()
      assert(dut.io.Sum.peek().litValue() === 0)
      assert(dut.io.Cout.peek().litValue() === 0)

      // Test case 2: A = 1, B = 0, Cin = 0
      dut.io.A.poke(1.U)
      dut.io.B.poke(0.U)
      dut.io.Cin.poke(0.U)
      dut.clock.step()
      assert(dut.io.Sum.peek().litValue() === 1)
      assert(dut.io.Cout.peek().litValue() === 0)

      // Test case 3: A = 0, B = 1, Cin = 0
      dut.io.A.poke(0.U)
      dut.io.B.poke(1.U)
      dut.io.Cin.poke(0.U)
      dut.clock.step()
      assert(dut.io.Sum.peek().litValue() === 1)
      assert(dut.io.Cout.peek().litValue() === 0)

      // Test case 4: A = 1, B = 1, Cin = 0
      dut.io.A.poke(1.U)
      dut.io.B.poke(1.U)
      dut.io.Cin.poke(0.U)
      dut.clock.step()
      assert(dut.io.Sum.peek().litValue() === 0)
      assert(dut.io.Cout.peek().litValue() === 1)

      // Test case 5: A = 0, B = 0, Cin = 1
      dut.io.A.poke(0.U)
      dut.io.B.poke(0.U)
      dut.io.Cin.poke(1.U)
      dut.clock.step()
      assert(dut.io.Sum.peek().litValue() === 1)
      assert(dut.io.Cout.peek().litValue() === 0)

      // Test case 6: A = 1, B = 0, Cin = 1
      dut.io.A.poke(1.U)
      dut.io.B.poke(0.U)
      dut.io.Cin.poke(1.U)
      dut.clock.step()
      assert(dut.io.Sum.peek().litValue() === 0)
      assert(dut.io.Cout.peek().litValue() === 1)

      // Test case 7: A = 0, B = 1, Cin = 1
      dut.io.A.poke(0.U)
      dut.io.B.poke(1.U)
      dut.io.Cin.poke(1.U)
      dut.clock.step()
      assert(dut.io.Sum.peek().litValue() === 0)
      assert(dut.io.Cout.peek().litValue() === 1)

      // Test case 8: A = 1, B = 1, Cin = 1
      dut.io.A.poke(1.U)
      dut.io.B.poke(1.U)
      dut.io.Cin.poke(1.U)
      dut.clock.step()
      assert(dut.io.Sum.peek().litValue() === 0)
      assert(dut.io.Cout.peek().litValue() === 2)

    }
  }
}