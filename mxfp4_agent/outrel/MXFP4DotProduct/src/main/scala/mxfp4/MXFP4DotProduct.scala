package mxfp4

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
