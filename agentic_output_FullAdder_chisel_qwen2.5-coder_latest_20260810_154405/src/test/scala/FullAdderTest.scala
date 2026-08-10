import chisel3._
import mxfp4._

class FullAdderTest extends Module {
  val io = IO(new Bundle {
    val A = Input(new MXFP4)
    val B = Input(new MXFP4)
    val Cin = Input(Bool())
    val S = Output(new MXFP4)
    val Cout = Output(Bool())
  })

  // Creazione di un'istanza del Full Adder
  val fullAdder = Module(new FullAdder)

  // Connessione dei segnali tra il testbench e il Full Adder
  fullAdder.io.A := io.A
  fullAdder.io.B := io.B
  fullAdder.io.Cin := io.Cin

  // Assegnazione dei segnali di output del Full Adder al testbench
  io.S := fullAdder.io.S
  io.Cout := fullAdder.io.Cout

  // Stimolo e verifica
  val A = RegInit(M0)
  val B = RegInit(M0)
  val Cin = RegInit(0.B)

  when (io.A === M3 && io.B === M3 && io.Cin === 1.B) {
    assert(io.S === M3, "Sum should be M3")
    assert(io.Cout === 1.B, "Cout should be 1")
  }.elsewhen (io.A === M0 && io.B === M0 && io.Cin === 0.B) {
    assert(io.S === M0, "Sum should be M0")
    assert(io.Cout === 0.B, "Cout should be 0")
  }.otherwise {
    assert(io.S === addWithSat(decodify(io.A), decodify(io.B), io.Cin)._1, "Sum is incorrect")
    assert(io.Cout === addWithSat(decodify(io.A), decodify(io.B), io.Cin)._2, "Cout is incorrect")
  }

  // Ciclo di clock
  val clk = RegInit(0.B)
  clk := ~clk

  // Stimolo dei segnali di ingresso
  when (clk === 1.B) {
    A := Mux(A === M3, M0, A + 1.U)
    B := Mux(B === M3, M0, B + 1.U)
    Cin := !Cin
  }
}

object FullAdderTestbench extends App {
  chisel3.Driver.execute(args, () => new FullAdderTest)
}