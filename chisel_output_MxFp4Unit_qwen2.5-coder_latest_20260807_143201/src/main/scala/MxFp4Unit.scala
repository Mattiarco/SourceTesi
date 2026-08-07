// ═══════════════════════════════════════════════════════════
//  Generato da: agentic_chisel_mxfp4_ollama.py
//  Modello Ollama: qwen2.5-coder:latest
//  Data: 2026-08-07T14:32:01
// ═══════════════════════════════════════════════════════════

import chisel3._

class FullAdder1Bit extends Module {
  val io = IO(new Bundle {
    // Ingressi
    val a = Input(UInt(1.W)) // Primo bit di ingresso
    val b = Input(UInt(1.W)) // Secondo bit di ingresso
    val cin = Input(UInt(1.W)) // Bit di carry in

    // Uscite
    val sum = Output(UInt(1.W)) // Somma del bit
    val cout = Output(UInt(1.W)) // Carry out
  })

  // XOR gate per calcolare la somma senza carry
  val sumWithoutCarry = io.a ^ io.b

  // AND gate per calcolare il primo bit di carry
  val firstCarryBit = io.a & io.b

  // Secondo XOR gate per combinare la somma senza carry e il cin
  val finalSum = sumWithoutCarry ^ io.cin

  // OR gate per calcolare il carry out
  val finalCarryOut = firstCarryBit | (sumWithoutCarry & io.cin)

  // Assegna le uscite
  io.sum := finalSum
  io.cout := finalCarryOut
}

object FullAdder1Bit extends App {
  chisel3.Driver.execute(args, () => new FullAdder1Bit)
}