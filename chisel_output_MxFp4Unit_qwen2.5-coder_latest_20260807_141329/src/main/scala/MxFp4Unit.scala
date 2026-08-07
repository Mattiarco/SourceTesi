// ═══════════════════════════════════════════════════════════
//  Generato da: agentic_chisel_mxfp4_ollama.py
//  Modello Ollama: qwen2.5-coder:latest
//  Data: 2026-08-07T14:13:29
// ═══════════════════════════════════════════════════════════

import chisel3._
import chisel3.Driver

class FullAdder extends Module {
  val io = IO(new Bundle {
    // Ingressi
    val a = Input(UInt(1.W)) // Primo bit di input
    val b = Input(UInt(1.W)) // Secondo bit di input
    val cin = Input(UInt(1.W)) // Bit di carry in

    // Uscite
    val sum = Output(UInt(1.W)) // Somma del Full Adder
    val cout = Output(UInt(1.W)) // Carry out del Full Adder
  })

  // Implementazione del Full Adder
  // XOR tra a e b per calcolare la somma senza carry
  val xor_ab = io.a ^ io.b

  // AND tra a e b per calcolare il primo bit di carry
  val and_ab = io.a & io.b

  // AND tra a e cin per calcolare il secondo bit di carry
  val and_acin = io.a & io.cin

  // AND tra b e cin per calcolare il terzo bit di carry
  val and_bcin = io.b & io.cin

  // Somma finale: XOR tra xor_ab e cin, che considera anche il carry in
  io.sum := xor_ab ^ io.cin

  // Carry out: OR tra tutti i possibili bit di carry generati
  io.cout := (and_ab | and_acin | and_bcin)
}

object FullAdder extends App {
  chisel3.Driver.execute(args, () => new FullAdder())
}