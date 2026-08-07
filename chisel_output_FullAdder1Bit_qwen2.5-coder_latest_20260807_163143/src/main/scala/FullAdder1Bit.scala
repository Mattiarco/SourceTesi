// ═══════════════════════════════════════════════════════════
//  Generato da: agentic_chisel_mxfp4_ollama.py
//  Modello Ollama: qwen2.5-coder:latest
//  Data: 2026-08-07T16:31:43
// ═══════════════════════════════════════════════════════════

import chisel3._

class FullAdder1Bit extends Module {
  val io = IO(new Bundle {
    val A = Input(UInt(4.W))
    val B = Input(UInt(4.W))
    val Cin = Input(UInt(1.W)) // Changed to UInt(1.W) as it's a single bit
    val Sum = Output(UInt(1.W))
    val Cout = Output(UInt(1.W)) // Changed to UInt(1.W) as it's a single bit
  })

  // XOR_A_B (XOR): A, B → Sum_XOR
  val Sum_XOR = io.A ^ io.B

  // AND_A_B (AND): A, B → Carry_AND
  val Carry_AND = io.A & io.B

  // XOR_Sum_XOR_Cin (XOR): Sum_XOR, Cin → Sum
  val Sum = Sum_XOR ^ io.Cin

  // AND_Sum_XOR_Cin (AND): Sum_XOR, Cin → Carry_AND2
  val Carry_AND2 = Sum_XOR & io.Cin

  // OR_Carry_AND_Carry_AND2 (OR): Carry_AND, Carry_AND2 → Cout
  val Cout = Carry_AND | Carry_AND2

  io.Sum := Sum(0)
  io.Cout := Cout // Assign the carry output to the correct port
}