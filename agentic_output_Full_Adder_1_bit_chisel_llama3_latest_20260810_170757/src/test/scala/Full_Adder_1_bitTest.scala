import chisel3._
import chiseltest._

class MXFP4(width: Int) extends Bundle {
  val bits = UInt(width.W)
}

class FullAdder1bit extends Module {
  val io = IO(new Bundle {
    val A = Input(new MXFP4(1))
    val B = Input(new MXFP4(1))
    val Cin = Input(new MXFP4(1))
    val S = Output(new MXFP4(1))
    val Cout = Output(new MXFP4(1))
  })

  val s = (io.A.bits ^ io.B.bits) ^ io.Cin.bits
  val cout = (io.A.bits & io.B.bits) | (io.A.bits & io.Cin.bits) | (io.B.bits & io.Cin.bits)

  io.S := new MXFP4(1)(s)
  io.Cout := new MXFP4(1)(cout)
}

class FullAdder1bitTest extends AnyFlatSpec with ChiselTest {
  behavior of "Full Adder 1-bit"

  it should "pass" in {
    val dut = new FullAdder1bit

    // Test 0 + 0 + 0
    poke(dut.io.A.bits, 0.U)
    poke(dut.io.B.bits, 0.U)
    poke(dut.io.Cin.bits, 0.U)
    expect(dut.io.S.bits, 0.U)
    expect(dut.io.Cout.bits, 0.U)

    // Test 0 + 1 + 0
    poke(dut.io.A.bits, 0.U)
    poke(dut.io.B.bits, 1.U)
    poke(dut.io.Cin.bits, 0.U)
    expect(dut.io.S.bits, 1.U)
    expect(dut.io.Cout.bits, 0.U)

    // Test 1 + 0 + 0
    poke(dut.io.A.bits, 1.U)
    poke(dut.io.B.bits, 0.U)
    poke(dut.io.Cin.bits, 0.U)
    expect(dut.io.S.bits, 1.U)
    expect(dut.io.Cout.bits, 0.U)

    // Test 1 + 1 + 0
    poke(dut.io.A.bits, 1.U)
    poke(dut.io.B.bits, 1.U)
    poke(dut.io.Cin.bits, 0.U)
    expect(dut.io.S.bits, 0.U)
    expect(dut.io.Cout.bits, 1.U)

    // Test 1 + 1 + 1
    poke(dut.io.A.bits, 1.U)
    poke(dut.io.B.bits, 1.U)
    poke(dut.io.Cin.bits, 1.U)
    expect(dut.io.S.bits, 0.U)
    expect(dut.io.Cout.bits, 1.U)
  }
}

object FullAdder1bit {
  def main(args: Array[String]): Unit = {
    chiseltest.Runner().execute(classOf[FullAdder1bit], Array("--backend", "verilog"))
  }
}