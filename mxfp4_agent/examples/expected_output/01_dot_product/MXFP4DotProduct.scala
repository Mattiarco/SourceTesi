package mxfp4

import chisel3._
import chisel3.util._

/** Dot-product MXFP4 combinatorio.
  *
  * Due blocchi da `k` elementi E2M1 con scala condivisa E8M0.
  *
  * IDEA CHIAVE — perche' non serve aritmetica floating point:
  *   - ogni magnitudine E2M1 appartiene a {0, .5, 1, 1.5, 2, 3, 4, 6}, quindi
  *     `mag * 2` appartiene a {0,1,2,3,4,6,8,12} ed e' un INTERO su 4 bit;
  *   - il prodotto di due elementi e' allora un intero in unita' di 1/4;
  *   - la somma di k prodotti resta intera ed ESATTA: nessun arrotondamento;
  *   - le scale E8M0 sono potenze esatte di due, quindi si combinano SOMMANDO
  *     gli esponenti, senza alcun moltiplicatore.
  *
  * Contratto di uscita:
  *   risultato reale = accQ2 / 4 * 2^expOut
  *
  * Deliberatamente NON si applica lo shift internamente: farlo richiederebbe
  * un barrel shifter a 254 posizioni e introdurrebbe overflow/underflow su un
  * risultato che qui e' esatto. La normalizzazione, se serve, e' compito dello
  * stadio a valle.
  */
class MXFP4DotProduct(val k: Int = 32) extends Module {
  require(k > 0 && k % 2 == 0, "k deve essere pari e positivo")

  val io = IO(new Bundle {
    val a      = Input(UInt((4 * k).W))  // elemento i nei bit [4i+3 : 4i]
    val b      = Input(UInt((4 * k).W))
    val scaleA = Input(UInt(8.W))        // E8M0: valore = 2^(scaleA-127)
    val scaleB = Input(UInt(8.W))
    val accQ2  = Output(SInt(32.W))      // accumulo intero, unita' 1/4
    val expOut = Output(SInt(16.W))      // (scaleA-127) + (scaleB-127)
    val isNaN  = Output(Bool())          // una delle due scale vale 0xFF
  })

  /** magnitudine E2M1 * 2 -> intero. L'indice e' il campo exp|mantissa (3 bit).
    * Nota: l'indice 1 (exp=0, man=1) vale 0.5 -> 1, NON zero: e' il subnormale.
    * Si usa `(-n).S(w)` e non `-n.S(w)`: quest'ultimo in Scala e' la negazione
    * hardware, che allarga la larghezza di un bit.
    */
  private val magTable = VecInit(Seq(0, 1, 2, 3, 4, 6, 8, 12).map(_.S(6.W)))

  private def decode(nib: UInt): SInt = {
    val mag = magTable(nib(2, 0))
    Mux(nib(3), 0.S(6.W) - mag, mag)      // bit 3 = segno; gestisce anche -0
  }

  private val products = Seq.tabulate(k) { i =>
    decode(io.a(4 * i + 3, 4 * i)) * decode(io.b(4 * i + 3, 4 * i))
  }

  /** Albero di somma bilanciato. `+&` estende la larghezza a ogni livello,
    * quindi l'accumulo non puo' andare in overflow: con k=32 il massimo e'
    * 32 * 144 = 4608, che sta in 14 bit piu' il segno.
    * Non si usa `reduceTree`: esiste su `Vec`, non su `Seq`.
    */
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
