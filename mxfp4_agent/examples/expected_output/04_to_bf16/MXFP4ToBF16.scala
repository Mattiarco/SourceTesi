package mxfp4

import chisel3._
import chisel3.util._

/** Conversione di un blocco MXFP4 (k elementi E2M1 + scala E8M0) in k bfloat16.
  *
  * Pura ricomposizione di campi, nessun moltiplicatore: ogni magnitudine E2M1
  * non nulla e' 2^e * 1.f con e in {-1,0,1,2} e un solo bit di mantissa f,
  * e la scala e' una potenza di due. L'esponente BF16 (bias 127) e' quindi
  * Eb = scaleA + e, e il risultato e' sempre esatto:
  *
  *   Eb >= 255  -> overflow: +/-inf (0x7F80 / 0xFF80)
  *   Eb >= 1    -> normale:  segno | Eb | f 000000
  *   Eb == 0    -> subnormale 0.1f  * 2^-126
  *   Eb == -1   -> subnormale 0.01f * 2^-126  (es. 0.5 * 2^-127)
  *
  * Casi speciali: il codice 0b?000 resta zero con segno (-0 -> 0x8000);
  * scaleA == 0xFF rende NaN l'intero blocco (NaN canonico 0x7FC0).
  *
  * Uscita impacchettata: l'elemento i occupa i bit [16i+15 : 16i] di `out`.
  */
class MXFP4ToBF16(val k: Int = 32) extends Module {
  val io = IO(new Bundle {
    val a      = Input(UInt((4 * k).W))
    val scaleA = Input(UInt(8.W))
    val out    = Output(UInt((16 * k).W))
    val isNaN  = Output(Bool())
  })

  // e + 1 (sempre >= 0) e bit di mantissa, indicizzati dai 3 bit di magnitudine
  //            codice:  0  1(0.5) 2(1) 3(1.5) 4(2) 5(3) 6(4) 7(6)
  private val expP1 = VecInit(Seq(0, 0, 1, 1, 2, 2, 3, 3).map(_.U(2.W)))
  private val frac  = VecInit(Seq(0, 0, 0, 1, 0, 1, 0, 1).map(_.U(1.W)))

  private val nan = io.scaleA === 0xFF.U

  private def convert(nib: UInt): UInt = {
    val sign = nib(3)
    val m    = nib(2, 0)
    val f    = frac(m)
    val ebP1 = io.scaleA +& expP1(m)          // Eb + 1, in [0, 257]
    val body = Wire(UInt(15.W))               // esponente(8) ## mantissa(7)
    when(m === 0.U) {
      body := 0.U
    }.elsewhen(ebP1 >= 256.U) {
      body := Cat(0xFF.U(8.W), 0.U(7.W))
    }.elsewhen(ebP1 >= 2.U) {
      body := Cat((ebP1 - 1.U)(7, 0), f, 0.U(6.W))
    }.elsewhen(ebP1 === 1.U) {
      body := Cat(0.U(8.W), 1.U(1.W), f, 0.U(5.W))
    }.otherwise {
      body := Cat(0.U(9.W), 1.U(1.W), f, 0.U(4.W))
    }
    Mux(nan, 0x7FC0.U(16.W), Cat(sign, body))
  }

  private val results = Seq.tabulate(k)(i => convert(io.a(4 * i + 3, 4 * i)))
  io.out   := Cat(results.reverse)
  io.isNaN := nan
}
