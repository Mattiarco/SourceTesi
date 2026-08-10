class FullAdder1bit(width: Int) extends Module {
  val io = IO(new Bundle {
    val A = Input(new MXFP4(width))
    val B = Input(new MXFP4(width))
    val Cin = Input(new MXFP4(1))
    val S = Output(new MXFP4(width))
    val Cout = Output(new MXFP4(1))
  })

  val s = (io.A.bits ^ io.B.bits) ^ io.Cin.bits
  val cout = (io.A.bits & io.B.bits) | (io.A.bits & io.Cin.bits) | (io.B.bits & io.Cin.bits)

  io.S := new MXFP4(width)(s)
  io.Cout := new MXFP4(1)(cout)
}