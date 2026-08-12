"""Design di riferimento MXFP4 (usato dal provider `mock` e come few-shot).

Contiene un dot-product MXFP4 combinatorio *funzionante*: serve sia per fare
smoke test dell'intera pipeline senza LLM, sia come esempio few-shot da mettere
nel prompt del Coder quando il modello locale è piccolo.
"""

REFERENCE_CHISEL = r'''package mxfp4

import chisel3._
import chisel3.util._

/** Dot-product MXFP4: due blocchi da `k` elementi E2M1 con scala condivisa E8M0.
  *
  * Idea chiave: ogni magnitudine E2M1 e' un multiplo di 0.5, quindi
  * `mag * 2` appartiene a {0,1,2,3,4,6,8,12} ed e' un intero. Il prodotto di
  * due elementi e' allora un intero in unita' di 1/4 e l'accumulo e' esatto,
  * senza alcuna aritmetica floating point. Le scale, essendo potenze di due,
  * si combinano sommando gli esponenti.
  *
  * Risultato reale = accQ2 / 4 * 2^expOut.
  */
class MXFP4DotProduct(val k: Int = 32) extends Module {
  require(k > 0 && k % 2 == 0, "k deve essere pari e positivo")

  val io = IO(new Bundle {
    val a      = Input(UInt((4 * k).W))  // elemento i nei bit [4i+3 : 4i]
    val b      = Input(UInt((4 * k).W))
    val scaleA = Input(UInt(8.W))        // E8M0
    val scaleB = Input(UInt(8.W))        // E8M0
    val accQ2  = Output(SInt(32.W))      // accumulo intero in unita' 1/4
    val expOut = Output(SInt(16.W))      // (scaleA-127) + (scaleB-127)
    val isNaN  = Output(Bool())          // una delle due scale e' 0xFF
  })

  // magnitudine E2M1 moltiplicata per 2 -> intero su 5 bit con segno
  private val magTable = VecInit(Seq(0, 1, 2, 3, 4, 6, 8, 12).map(_.S(6.W)))

  private def decode(nib: UInt): SInt = {
    val mag = magTable(nib(2, 0))
    Mux(nib(3), (0.S(6.W) - mag), mag)
  }

  private val products = Seq.tabulate(k) { i =>
    decode(io.a(4 * i + 3, 4 * i)) * decode(io.b(4 * i + 3, 4 * i))
  }

  /** Albero di somma bilanciato: `+&` estende la larghezza a ogni livello,
    * quindi l'accumulo non puo' andare in overflow. */
  private def adderTree(xs: Seq[SInt]): SInt = xs match {
    case Seq(one) => one
    case _        =>
      val (l, r) = xs.splitAt(xs.length / 2)
      adderTree(l) +& adderTree(r)
  }

  io.accQ2  := adderTree(products)
  io.expOut := (io.scaleA +& io.scaleB).zext - 254.S
  io.isNaN  := (io.scaleA === 0xFF.U) || (io.scaleB === 0xFF.U)
}
'''

REFERENCE_TB = r'''// Testbench Verilator per MXFP4DotProduct.
#include "VMXFP4DotProduct.h"
#include "verilated.h"
#include "test_vectors.h"

#include <cstdio>
#include <cstdint>
#include <cstdlib>

int main(int argc, char** argv) {
    Verilated::commandArgs(argc, argv);
    VMXFP4DotProduct* dut = new VMXFP4DotProduct;

    int failures = 0;
    const int n = NUM_VECTORS;

    for (int i = 0; i < n; ++i) {
        const mxfp4_vec_t& v = MXFP4_VECTORS[i];
        for (int w = 0; w < 4; ++w) {
            dut->a[w] = v.a[w];
            dut->b[w] = v.b[w];
        }
        dut->scaleA = v.scale_a;
        dut->scaleB = v.scale_b;
        dut->eval();

        int32_t got_acc = (int32_t)dut->accQ2;
        int16_t got_exp = (int16_t)dut->expOut;
        uint8_t got_nan = (uint8_t)dut->isNaN;

        if (got_acc != v.exp_acc_q2 || got_exp != v.exp_shared || got_nan != v.nan) {
            if (failures < 10) {
                printf("FAIL [%d] acc exp=%d got=%d | exp2 exp=%d got=%d | nan exp=%u got=%u\n",
                       i, (int)v.exp_acc_q2, (int)got_acc,
                       (int)v.exp_shared, (int)got_exp,
                       (unsigned)v.nan, (unsigned)got_nan);
            }
            ++failures;
        }
    }

    dut->final();
    delete dut;

    if (failures) {
        printf("TEST FAILED (%d/%d)\n", failures, n);
        return 1;
    }
    printf("TEST PASSED (%d vectors)\n", n);
    return 0;
}
'''

MOCK_PLAN = """```json
{
  "module_name": "MXFP4DotProduct",
  "meta_hdl": "chisel",
  "rationale": "Chisel permette di parametrizzare K e di generare l'albero di somma con reduceTree; il datapath e' interamente intero quindi non serve libreria FP.",
  "clocking": "combinational",
  "parameters": [{"name": "k", "value": 32, "description": "elementi per blocco"}],
  "ports": [
    {"name": "a", "dir": "in", "width": 128, "type": "UInt", "description": "blocco A, elemento i nei bit [4i+3:4i]"},
    {"name": "b", "dir": "in", "width": 128, "type": "UInt", "description": "blocco B"},
    {"name": "scaleA", "dir": "in", "width": 8, "type": "UInt", "description": "scala E8M0 di A"},
    {"name": "scaleB", "dir": "in", "width": 8, "type": "UInt", "description": "scala E8M0 di B"},
    {"name": "accQ2", "dir": "out", "width": 32, "type": "SInt", "description": "accumulo intero in unita' 1/4"},
    {"name": "expOut", "dir": "out", "width": 16, "type": "SInt", "description": "esponente risultante"},
    {"name": "isNaN", "dir": "out", "width": 1, "type": "Bool", "description": "scala NaN"}
  ],
  "algorithm": [
    "Decodifica ogni nibble E2M1 in un intero con segno pari a mag*2 tramite LUT {0,1,2,3,4,6,8,12}.",
    "Moltiplica gli interi a coppie: il prodotto e' esatto in unita' 1/4.",
    "Somma con albero bilanciato usando +& per evitare overflow.",
    "Calcola expOut = scaleA + scaleB - 254.",
    "isNaN se una scala vale 0xFF."
  ],
  "test_plan": {
    "kernel": "dot_product",
    "num_random": 64,
    "directed": ["tutti zero", "tutti +6 (saturazione)", "subnormali 0.5", "zero negativo", "scala NaN"]
  },
  "risks": ["larghezza dell'accumulatore insufficiente", "ordine dei nibble invertito"]
}
```"""

MOCK_CODER_ANSWER = f"""Implementazione dell'unita' MXFP4.

### FILE: src/main/scala/mxfp4/MXFP4DotProduct.scala
```scala
{REFERENCE_CHISEL}```

### FILE: sim/tb_MXFP4DotProduct.cpp
```cpp
{REFERENCE_TB}```
"""

MOCK_REVIEW = """```json
{
  "verdict": "approved",
  "issues": [],
  "notes": "Design di riferimento gia' verificato: larghezze corrette, nessun latch, output tutti assegnati."
}
```"""
