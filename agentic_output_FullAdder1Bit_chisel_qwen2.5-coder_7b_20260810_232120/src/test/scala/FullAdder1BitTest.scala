import chisel3._
import mxfp4._

class FullAdder1BitTest extends Module {
  val dut = Module(new FullAdder1Bit)

  // IO signals
  val ioA = Reg(new MXFP4)
  val ioB = Reg(new MXFP4)
  val ioCin = Reg(new MXFP4)
  val ioSum = Wire(new MXFP4)
  val ioCout = Wire(new MXFP4)

  // Connect dut inputs to IO signals
  dut.io.A := ioA
  dut.io.B := ioB
  dut.io.Cin := ioCin

  // Connect dut outputs to IO signals
  ioSum := dut.io.Sum
  ioCout := dut.io.Cout

  // Test cases
  val testCases = Seq(
    (0.U, 0.U, 0.U, 0.U, 0.U),
    (1.U, 0.U, 0.U, 1.U, 0.U),
    (0.U, 1.U, 0.U, 1.U, 0.U),
    (1.U, 1.U, 0.U, 0.U, 1.U),
    (0.U, 0.U, 1.U, 1.U, 0.U),
    (1.U, 0.U, 1.U, 0.U, 1.U),
    (0.U, 1.U, 1.U, 0.U, 1.U),
    (1.U, 1.U, 1.U, 1.U, 1.U)
  )

  // Testbench logic
  val testVector = RegInit(0.U(testCases.length.W))
  when (testVector < testCases.length.U) {
    ioA := new MXFP4(decode(testCases(testVector)(0)))
    ioB := new MXFP4(decode(testCases(testVector)(1)))
    ioCin := new MXFP4(decode(testCases(testVector)(2)))

    // Check outputs
    assert(ioSum.data === encode(decode(testCases(testVector)(3))), "Sum mismatch")
    assert(ioCout.data === encode(decode(testCases(testVector)(4))), "Carry out mismatch")

    // Move to the next test case
    testVector := testVector + 1.U
  }

  // Stop simulation after all test cases are done
  when (testVector === testCases.length.U) {
    stop()
  }
}