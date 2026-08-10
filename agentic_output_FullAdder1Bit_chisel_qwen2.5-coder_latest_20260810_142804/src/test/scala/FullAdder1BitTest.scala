import chisel3._
import chisel3.util._



class FullAdder1BitTest extends Module {
  val dut = Module(new FullAdder1Bit)

  // Input signals
  val a = IO(Input(UInt(1.W)))
  val b = IO(Input(UInt(1.W)))
  val cin = IO(Input(UInt(1.W)))

  // Output signals
  val sum = IO(Output(UInt(1.W)))
  val cout = IO(Output(UInt(1.W)))

  // Connect inputs to DUT
  dut.io.a := a
  dut.io.b := b
  dut.io.cin := cin

  // Connect outputs from DUT
  sum := dut.io.sum
  cout := dut.io.cout

  // Test cases
  val testCases = Seq(
    (0.U, 0.U, 0.U, 0.U, 0.U),
    (1.U, 0.U, 0.U, 1.U, 0.U),
    (0.U, 1.U, 0.U, 1.U, 0.U),
    (0.U, 0.U, 1.U, 1.U, 0.U),
    (1.U, 1.U, 0.U, 0.U, 1.U),
    (1.U, 0.U, 1.U, 0.U, 1.U),
    (0.U, 1.U, 1.U, 0.U, 1.U),
    (1.U, 1.U, 1.U, 1.U, 1.U)
  )

  for ((aVal, bVal, cinVal, expectedSum, expectedCout) <- testCases) {
    when(a === aVal && b === bVal && cin === cinVal) {
      assert(sum === expectedSum, s"Test case: a=$aVal, b=$bVal, cin=$cinVal failed")
      assert(cout === expectedCout, s"Test case: a=$aVal, b=$bVal, cin=$cinVal failed")
    }
  }
}

object FullAdder1BitTest extends App {
  chisel3.Driver.execute(args, () => new FullAdder1BitTest)
}