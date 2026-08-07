// ═══════════════════════════════════════════════════════════
//  Generato da: agentic_chisel_mxfp4_ollama.py
//  Modello Ollama: qwen2.5-coder:latest
//  Data: 2026-08-07T12:12:15
// ═══════════════════════════════════════════════════════════

import chisel3._
import chisel3.iotesters.{PeekPokeTester, Driver}

class FullAdder1BitUnitTester(c: FullAdder1Bit) extends PeekPokeTester(c) {
  // Test all possible combinations of inputs
  for (a <- 0 until 2; b <- 0 until 2; cin <- 0 until 2) {
    poke(c.io.A, a)
    poke(c.io.B, b)
    poke(c.io.Cin, cin)
    step(1)
    expect(c.io.S, (a + b + cin) % 2.U)
    expect(c.io.Cout, (a + b + cin) / 2.U)
  }
}

object FullAdder1BitTest extends App {
  Driver(() => new FullAdder1Bit(), "FullAdder1BitUnitTester") { c =>
    new FullAdder1BitUnitTester(c)
  }
}