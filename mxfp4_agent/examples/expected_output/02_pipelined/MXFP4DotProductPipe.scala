package mxfp4

import chisel3._
import chisel3.util._

/** Dot-product MXFP4 pipelined a 2 stadi, throughput 1 blocco/ciclo.
  *
  *   stadio 1: decodifica E2M1 + moltiplicazione dei k prodotti
  *   stadio 2: albero di somma + combinazione degli esponenti
  *
  * Latenza fissa 2 cicli. `validOut` e' `validIn` ritardato di 2, cosi' il
  * consumatore sa quali cicli portano un risultato valido senza handshake.
  *
  * Contratto identico alla versione combinatoria:
  *   risultato reale = accQ2 / 4 * 2^expOut
  *
  * Il taglio del pipeline e' scelto dove il ritardo combinatorio e' massimo:
  * i moltiplicatori 6x6 bit e l'albero di somma a 5 livelli finiscono in
  * stadi diversi.
  */
class MXFP4DotProductPipe(val k: Int = 32) extends Module {
  require(k > 0 && k % 2 == 0, "k deve essere pari e positivo")

  val io = IO(new Bundle {
    val validIn  = Input(Bool())
    val a        = Input(UInt((4 * k).W))
    val b        = Input(UInt((4 * k).W))
    val scaleA   = Input(UInt(8.W))
    val scaleB   = Input(UInt(8.W))
    val validOut = Output(Bool())
    val accQ2    = Output(SInt(32.W))
    val expOut   = Output(SInt(16.W))
    val isNaN    = Output(Bool())
  })

  private val magTable = VecInit(Seq(0, 1, 2, 3, 4, 6, 8, 12).map(_.S(6.W)))

  private def decode(nib: UInt): SInt = {
    val mag = magTable(nib(2, 0))
    Mux(nib(3), 0.S(6.W) - mag, mag)
  }

  private def adderTree(xs: Seq[SInt]): SInt = xs match {
    case Seq(one) => one
    case _        =>
      val (l, r) = xs.splitAt(xs.length / 2)
      adderTree(l) +& adderTree(r)
  }

  // ------------------------------------------------------------- stadio 1
  // k prodotti da 12 bit con segno (6x6). Registrati alla fine del ciclo.
  private val s1Prod   = Reg(Vec(k, SInt(12.W)))
  private val s1Exp    = RegInit(0.S(16.W))
  private val s1NaN    = RegInit(false.B)
  private val s1Valid  = RegInit(false.B)

  for (i <- 0 until k) {
    s1Prod(i) := decode(io.a(4 * i + 3, 4 * i)) * decode(io.b(4 * i + 3, 4 * i))
  }
  s1Exp   := (io.scaleA +& io.scaleB).zext - 254.S
  s1NaN   := (io.scaleA === 0xFF.U) || (io.scaleB === 0xFF.U)
  s1Valid := io.validIn

  // ------------------------------------------------------------- stadio 2
  private val s2Acc   = RegInit(0.S(32.W))
  private val s2Exp   = RegInit(0.S(16.W))
  private val s2NaN   = RegInit(false.B)
  private val s2Valid = RegInit(false.B)

  s2Acc   := adderTree(Seq.tabulate(k)(i => s1Prod(i)))
  s2Exp   := s1Exp
  s2NaN   := s1NaN
  s2Valid := s1Valid

  // --------------------------------------------------------------- uscite
  io.accQ2    := s2Acc
  io.expOut   := s2Exp
  io.isNaN    := s2NaN
  io.validOut := s2Valid
}
