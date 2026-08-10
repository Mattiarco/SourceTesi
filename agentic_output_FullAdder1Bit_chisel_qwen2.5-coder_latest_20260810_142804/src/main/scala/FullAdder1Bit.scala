import chisel3._
import chisel3.util._

class FullAdder1Bit extends Module {
  val io = IO(new Bundle {
    val a = Input(UInt(1.W))
    val b = Input(UInt(1.W))
    val cin = Input(UInt(1.W))
    val sum = Output(UInt(1.W))
    val cout = Output(UInt(1.W))
  })

  // Implementazione del Full Adder
  io.sum := io.a ^ io.b ^ io.cin
  io.cout := (io.a & io.b) | (io.a & io.cin) | (io.b & io.cin)
}

object FullAdder1BitTest extends App {
  chisel3.Driver.execute(args, () => new FullAdder1Bit)
}