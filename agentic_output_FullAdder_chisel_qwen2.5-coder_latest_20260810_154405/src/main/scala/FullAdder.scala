import chisel3._
import mxfp4._

class FullAdder extends Module {
  val io = IO(new Bundle {
    val A = Input(new MXFP4)
    val B = Input(new MXFP4)
    val Cin = Input(Bool())
    val S = Output(new MXFP4)
    val Cout = Output(Bool())
  })

  // Helper function to decodify a MXFP4 value
  def decodify(input: MXFP4): UInt = {
    input match {
      case M0 => 0.U
      case M1 => 1.U
      case M2 => 2.U
      case M3 => 3.U
      // Add more cases as needed for your specific implementation
    }
  }

  // Helper function to align two MXFP4 values
  def align(a: MXFP4, b: MXFP4): (UInt, UInt) = {
    (decodify(a), decodify(b))
  }

  // Helper function to add two values and round-saturate
  def addWithSat(a: UInt, b: UInt, cin: Bool): (UInt, Bool) = {
    val sum = a + b + cin.asUInt
    val overflow = sum >= 4.U
    val result = Mux(overflow, 3.U, sum)
    (result, overflow)
  }

  // Align the two inputs
  val (alignedA, alignedB) = align(io.A, io.B)

  // Add and round-saturate
  val (sum, cout) = addWithSat(alignedA, alignedB, io.Cin)

  // Decode the sum to get the result MXFP4
  io.S := Mux(sum === 0.U, M0, Mux(sum === 1.U, M1, M2))

  // The carry out remains the same because it's a boolean
  io.Cout := cout
}