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

  // XOR gate
  val XOR1 = Module(new XOR)
  XOR1.io.A := io.A
  XOR1.io.B := io.B
  val SumXOR = XOR1.io.Out

  // AND gates
  val AND1 = Module(new AND)
  AND1.io.A := io.A
  AND1.io.B := io.B
  val CarryAND1 = AND1.io.Out

  val AND2 = Module(new AND)
  AND2.io.A := SumXOR
  AND2.io.B := io.Cin
  val CarryAND2 = AND2.io.Out

  // OR gate
  val OR1 = Module(new OR)
  OR1.io.A := CarryAND1
  OR1.io.B := CarryAND2
  val Cout = OR1.io.Out

  // Assign outputs
  io.Sum := encode(decode(SumXOR))
  io.Cout := encode(decode(Cout))
}

class XOR extends Module {
  val io = IO(new Bundle {
    val A = Input(new MXFP4)
    val B = Input(new MXFP4)
    val Out = Output(new MXFP4)
  })

  // Implement XOR logic for MXFP4
  val a_dec = decode(io.A.data)
  val b_dec = decode(io.B.data)
  val sum_dec = (a_dec ^ b_dec) % 16
  io.Out := encode(sum_dec)
}

class AND extends Module {
  val io = IO(new Bundle {
    val A = Input(new MXFP4)
    val B = Input(new MXFP4)
    val Out = Output(new MXFP4)
  })

  // Implement AND logic for MXFP4
  val a_dec = decode(io.A.data)
  val b_dec = decode(io.B.data)
  val and_dec = a_dec & b_dec
  io.Out := encode(and_dec)
}

class OR extends Module {
  val io = IO(new Bundle {
    val A = Input(new MXFP4)
    val B = Input(new MXFP4)
    val Out = Output(new MXFP4)
  })

  // Implement OR logic for MXFP4
  val a_dec = decode(io.A.data)
  val b_dec = decode(io.B.data)
  val or_dec = a_dec | b_dec
  io.Out := encode(or_dec)
}

// Helper functions to decode and encode MXFP4 values
def decode(data: UInt): Int = {
  data.asUInt.toInt
}

def encode(dec: Int): UInt = {
  dec.U(4.W)
}