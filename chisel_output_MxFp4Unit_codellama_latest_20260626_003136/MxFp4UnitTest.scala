// ═══════════════════════════════════════════════════════════
//  Generato da: agentic_chisel_mxfp4_ollama.py
//  Modello Ollama: codellama:latest
//  Data: 2026-06-26T00:31:36
// ═══════════════════════════════════════════════════════════

import chisel3._
import chisel3.util._
import chisel3.testers._

class FullAdderTest(implicit config: Config) extends ChiselFlatSpec {
  behavior of "FullAdder"

  it should "add two bits correctly" in {
    val adder = Module(new FullAdder)
    val io = new Bundle {
      val a = Input(Bool())
      val b = Input(Bool())
      val sum = Output(Bool())
      val carry = Output(Bool())
    }

    // Connect the inputs and outputs of the adder
    io.a := adder.io.a
    io.b := adder.io.b
    io.sum := adder.io.sum
    io.carry := adder.io.carry

    // Create a tester object to drive the circuit
    val tester = new Tester(adder)

    // Test the circuit with different inputs and outputs
    for (i <- 0 until 2) {
      for (j <- 0 until 2) {
        io.a := i.U
        io.b := j.U
        tester.step()
        assert(io.sum === (i + j).U)
        assert(io.carry === ((i + j) / 2).U)
      }
    }
  }
}

This code defines a Chisel testbench named `FullAdderTest` that tests the correctness of the `FullAdder` module. The testbench uses the `ChiselFlatSpec` trait to define a behavior specification for the `FullAdder` module.

The `behavior` method is used to specify the behavior of the `FullAdderTest` testbench. In this case, we use it to specify that the testbench should test the correctness of the `FullAdder` module.

The `it should "add two bits correctly"` method is used to define a test case for the `FullAdder` module. The test case uses the `for` loop to iterate over all possible combinations of input values (`i` and `j`) and checks that the output values (`sum` and `carry`) are correct.

The `io` object is used to define the input and output ports of the `FullAdderTest` testbench. The `a`, `b`, `sum`, and `carry` fields are used to specify the names and types of the inputs and outputs, respectively. In this case, we have two inputs (`a` and `b`) and two outputs (`sum` and `carry`).

The `adder` object is a reference to an instance of the `FullAdder` module. We use the `Module` constructor to create an instance of the `FullAdder` module and assign it to the `adder` variable. The `io` field of the `adder` object is used to connect the input ports of the `FullAdder` module to the input ports of the `FullAdderTest` testbench.

The `sum` and `carry` outputs are connected to the output ports of the `FullAdder` module using the `io` field of the `adder` object.

The `tester` object is a reference to an instance of the `Tester` class, which is used to drive the circuit. We use the `step()` method of the `tester` object to advance the simulation by one clock cycle and check that the output values (`sum` and `carry`) are correct.

The `assert` statement is used to check that the output values (`sum` and `carry`) are correct. If the output values are not correct, the testbench will throw an exception and indicate that the test failed.