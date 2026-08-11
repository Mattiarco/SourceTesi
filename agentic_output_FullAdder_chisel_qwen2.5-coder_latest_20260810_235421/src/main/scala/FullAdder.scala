import chisel3._
import mxfp4._

class FullAdder extends Module {
  val io = IO(new Bundle {
    val A = Input(new MXFP4)
    val B = Input(new MXFP4)
    val Cin = Input(new MXFP4)
    val S = Output(new MXFP4)
    val Cout = Output(new MXFP4)
  })

  // Decode inputs
  val A_dec = io.A.decode()
  val B_dec = io.B.decode()
  val Cin_dec = io.Cin.decode()

  // Perform addition
  val (S_dec, Cout_dec) = FloatingPoint.add(A_dec, B_dec + Cin_dec)

  // Encode outputs
  io.S := S_dec.encode()
  io.Cout := Cout_dec.encode()
}

object FullAdder extends App {
  chisel3.Driver.execute(args, () => new FullAdder)
}