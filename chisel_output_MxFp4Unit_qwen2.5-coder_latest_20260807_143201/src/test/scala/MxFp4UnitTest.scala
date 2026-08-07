// ═══════════════════════════════════════════════════════════
//  Generato da: agentic_chisel_mxfp4_ollama.py
//  Modello Ollama: qwen2.5-coder:latest
//  Data: 2026-08-07T14:32:01
// ═══════════════════════════════════════════════════════════

import chisel3._
import chiseltest._
import org.scalatest.freespec.AnyFreeSpec

class FullAdder1BitTest extends AnyFreeSpec with ChiselScalatestTester {
  "FullAdder1Bit" should {
    "correctly add two bits and carry" in {
      test(new FullAdder1Bit) { dut =>
        // Test case 1: a = 0, b = 0, cin = 0
        dut.io.a.poke(0.U)
        dut.io.b.poke(0.U)
        dut.io.cin.poke(0.U)
        dut.clock.step()
        assert(dut.io.sum === 0.U)
        assert(dut.io.cout === 0.U)

        // Test case 2: a = 1, b = 0, cin = 0
        dut.io.a.poke(1.U)
        dut.io.b.poke(0.U)
        dut.io.cin.poke(0.U)
        dut.clock.step()
        assert(dut.io.sum === 1.U)
        assert(dut.io.cout === 0.U)

        // Test case 3: a = 0, b = 1, cin = 0
        dut.io.a.poke(0.U)
        dut.io.b.poke(1.U)
        dut.io.cin.poke(0.U)
        dut.clock.step()
        assert(dut.io.sum === 1.U)
        assert(dut.io.cout === 0.U)

        // Test case 4: a = 1, b = 1, cin = 0
        dut.io.a.poke(1.U)
        dut.io.b.poke(1.U)
        dut.io.cin.poke(0.U)
        dut.clock.step()
        assert(dut.io.sum === 0.U)
        assert(dut.io.cout === 1.U)

        // Test case 5: a = 0, b = 0, cin = 1
        dut.io.a.poke(0.U)
        dut.io.b.poke(0.U)
        dut.io.cin.poke(1.U)
        dut.clock.step()
        assert(dut.io.sum === 1.U)
        assert(dut.io.cout === 0.U)

        // Test case 6: a = 1, b = 0, cin = 1
        dut.io.a.poke(1.U)
        dut.io.b.poke(0.U)
        dut.io.cin.poke(1.U)
        dut.clock.step()
        assert(dut.io.sum === 0.U)
        assert(dut.io.cout === 1.U)

        // Test case 7: a = 0, b = 1, cin = 1
        dut.io.a.poke(0.U)
        dut.io.b.poke(1.U)
        dut.io.cin.poke(1.U)
        dut.clock.step()
        assert(dut.io.sum === 0.U)
        assert(dut.io.cout === 1.U)

        // Test case 8: a = 1, b = 1, cin = 1
        dut.io.a.poke(1.U)
        dut.io.b.poke(1.U)
        dut.io.cin.poke(1.U)
        dut.clock.step()
        assert(dut.io.sum === 1.U)
        assert(dut.io.cout === 1.U)
      }
    }
  }
}