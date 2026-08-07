// ═══════════════════════════════════════════════════════════
//  Generato da: agentic_chisel_mxfp4_ollama.py
//  Modello Ollama: qwen2.5-coder:latest
//  Data: 2026-08-07T14:13:29
// ═══════════════════════════════════════════════════════════

import chisel3.iotesters.{PeekPokeTester, Driver}

class FullAdderTests(c: FullAdder) extends PeekPokeTester(c) {
  // Test case 1: a = 0, b = 0, cin = 0
  poke(c.io.a, 0.U)
  poke(c.io.b, 0.U)
  poke(c.io.cin, 0.U)
  step(1)
  expect(c.io.sum, 0.U)
  expect(c.io.cout, 0.U)

  // Test case 2: a = 0, b = 0, cin = 1
  poke(c.io.a, 0.U)
  poke(c.io.b, 0.U)
  poke(c.io.cin, 1.U)
  step(1)
  expect(c.io.sum, 1.U)
  expect(c.io.cout, 0.U)

  // Test case 3: a = 0, b = 1, cin = 0
  poke(c.io.a, 0.U)
  poke(c.io.b, 1.U)
  poke(c.io.cin, 0.U)
  step(1)
  expect(c.io.sum, 1.U)
  expect(c.io.cout, 0.U)

  // Test case 4: a = 0, b = 1, cin = 1
  poke(c.io.a, 0.U)
  poke(c.io.b, 1.U)
  poke(c.io.cin, 1.U)
  step(1)
  expect(c.io.sum, 0.U)
  expect(c.io.cout, 1.U)

  // Test case 5: a = 1, b = 0, cin = 0
  poke(c.io.a, 1.U)
  poke(c.io.b, 0.U)
  poke(c.io.cin, 0.U)
  step(1)
  expect(c.io.sum, 1.U)
  expect(c.io.cout, 0.U)

  // Test case 6: a = 1, b = 0, cin = 1
  poke(c.io.a, 1.U)
  poke(c.io.b, 0.U)
  poke(c.io.cin, 1.U)
  step(1)
  expect(c.io.sum, 0.U)
  expect(c.io.cout, 1.U)

  // Test case 7: a = 1, b = 1, cin = 0
  poke(c.io.a, 1.U)
  poke(c.io.b, 1.U)
  poke(c.io.cin, 0.U)
  step(1)
  expect(c.io.sum, 0.U)
  expect(c.io.cout, 1.U)

  // Test case 8: a = 1, b = 1, cin = 1
  poke(c.io.a, 1.U)
  poke(c.io.b, 1.U)
  poke(c.io.cin, 1.U)
  step(1)
  expect(c.io.sum, 1.U)
  expect(c.io.cout, 1.U)
}

object FullAdderTester extends App {
  Driver.execute(args, () => new FullAdderTests(_))
}