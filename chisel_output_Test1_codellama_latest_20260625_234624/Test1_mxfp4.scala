// ══════════════════════════════════════════════════════════
//  Generato da: python_to_chisel_mxfp4_ollama.py
//  Sorgente:     Test1.py
//  Modello:      codellama:latest
//  Data:         2026-06-25T23:46:24
// ══════════════════════════════════════════════════════════

import chisel3._
import chisel3.util._

class FullAdderMXFP4 extends Module {
    val io = IO(new Bundle {
        val a = Input(UInt(4.W))
        val b = Input(SInt(4.W))
        val cin = Input(UInt(4.W))
        val sum = Output(UInt(4.W))
        val cout = Output(UInt(4.W))
    })

    val s = io.a ^ io.b ^ io.cin
    val cout = (io.a & io.b) | (io.cin & (io.a ^ io.b))
    io.sum := s
    io.cout := cout
}