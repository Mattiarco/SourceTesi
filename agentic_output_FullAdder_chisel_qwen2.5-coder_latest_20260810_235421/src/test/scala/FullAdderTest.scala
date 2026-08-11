import chisel3._
import mxfp4._

class FullAdderTestbench extends Module {
  val dut = Module(new FullAdder)

  // Input signals
  val A = RegInit(0.U(4.W))
  val B = RegInit(0.U(4.W))
  val Cin = RegInit(0.U(4.W))

  // Output signals
  val S = dut.io.S
  val Cout = dut.io.Cout

  // Clock signal
  val clock = RegInit(0.B)
  when(clock) {
    clock := ~clock
  }

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

  val testCaseIndex = RegInit(0.U(log2Ceil(testCases.length).W))

  // Connect inputs to test cases
  A := testCases(testCaseIndex)(0)
  B := testCases(testCaseIndex)(1)
  Cin := testCases(testCaseIndex)(2)

  // Monitor outputs
  printf(p"Test Case ${testCaseIndex}: A=${A}, B=${B}, Cin=${Cin}, S=${S}, Cout=${Cout}\n")

  // Increment test case index on each clock cycle
  when(clock) {
    testCaseIndex := (testCaseIndex + 1.U) % testCases.length.U
  }

  // Stop simulation after all test cases are executed
  when(testCaseIndex === (testCases.length - 1).U) {
    stop()
  }
}

object FullAdderTestbench extends App {
  chisel3.Driver.execute(args, () => new FullAdderTestbench)
}