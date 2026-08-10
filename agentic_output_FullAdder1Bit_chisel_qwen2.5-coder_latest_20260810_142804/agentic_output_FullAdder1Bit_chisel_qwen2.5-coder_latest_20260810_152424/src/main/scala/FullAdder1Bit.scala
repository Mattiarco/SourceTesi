import chisel3._
import mxfp4._

class FullAdder1Bit extends Module {
  val io = IO(new Bundle {
    val A = Input(new MXFP4)
    val B = Input(new MXFP4)
    val Cin = Input(new MXFP4)
    val Sum = Output(new MXFP4)
    val Cout = Output(new MXFP4)
  })

  def adder1bit(a: MXFP4, b: MXFP4, cin: MXFP4): (MXFP4, MXFP4) = {
    // Decode the inputs
    val aExp = a.exp.asUInt()
    val aMant = a.mant.asUInt()
    val bExp = b.exp.asUInt()
    val bMant = b.mant.asUInt()
    val cinExp = cin.exp.asUInt()
    val cinMant = cin.mant.asUInt()

    // Perform the addition
    val sumExp = Mux(aExp === bExp, aExp + 1.U, Mux(aExp > bExp, aExp, bExp))
    val sumMant = Mux(aExp === bExp, aMant + bMant, Mux(aExp > bExp, aMant << (aExp - bExp), bMant << (bExp - aExp)))

    // Handle carry
    val carry = Mux(sumMant >= (1.U << sumExp), 1.U, 0.U)

    // Encode the output
    val Sum = new MXFP4(sumExp.asSInt(), sumMant.asSInt())
    val Cout = new MXFP4(carry.asSInt(), 0.S)

    (Sum, Cout)
  }

  val (Sum, Cout) = adder1bit(io.A, io.B, io.Cin)

  // Assign outputs
  io.Sum := Sum
  io.Cout := Cout
}