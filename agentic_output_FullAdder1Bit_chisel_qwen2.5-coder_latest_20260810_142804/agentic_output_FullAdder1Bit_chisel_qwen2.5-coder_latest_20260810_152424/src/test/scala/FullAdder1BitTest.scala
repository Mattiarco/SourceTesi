import chisel3._
import mxfp4._

class FullAdder1BitTest extends Module {
  val dut = Module(new FullAdder1Bit)

  // Input signals
  val A = IO(Input(new MXFP4))
  val B = IO(Input(new MXFP4))
  val Cin = IO(Input(new MXFP4))

  // Output signals
  val Sum = IO(Output(new MXFP4))
  val Cout = IO(Output(new MXFP4))

  // Connect inputs to DUT
  dut.io.A := A
  dut.io.B := B
  dut.io.Cin := Cin

  // Connect outputs from DUT
  Sum := dut.io.Sum
  Cout := dut.io.Cout

  // Test cases
  val testCases = Seq(
    (0.U, 0.U, 0.U, 0.U),
    (1.U, 0.U, 0.U, 1.U),
    (0.U, 1.U, 0.U, 1.U),
    (1.U, 1.U, 0.U, 0.U),
    (0.U, 0.U, 1.U, 1.U),
    (1.U, 0.U, 1.U, 0.U),
    (0.U, 1.U, 1.U, 0.U),
    (1.U, 1.U, 1.U, 1.U)
  )

  // Testbench logic
  val testVector = Reg(Vec(testCases.length, Vec(4, UInt(1.W))))
  for (i <- testCases.indices) {
    testVector(i) := Vec(A, B, Cin, Sum)
  }

  // Clock signal
  val clock = RegInit(0.U(1.W))
  clock := ~clock

  // Testbench main loop
  when(clock === 0.U) {
    for (i <- testCases.indices) {
      A := testVector(i)(0).asMXFP4
      B := testVector(i)(1).asMXFP4
      Cin := testVector(i)(2).asMXFP4
      Sum.expect(testVector(i)(3).asMXFP4)
    }
  }

  // Run the testbench
  chisel3.Driver.execute(args, () => new FullAdder1BitTest)
}