package mxfp4

import chisel3._
import chisel3.util._

/** Moltiplicatore element-wise MXFP4 -> 32 x FP32, combinatorio.
  *
  * Per ogni indice i calcola `a_i * b_i` e lo emette in IEEE-754 binary32.
  *
  * PERCHE' LA CONVERSIONE E' ESATTA E NON SERVE ARROTONDAMENTO:
  *   mag2 = magnitudine E2M1 * 2 appartiene a {0,1,2,3,4,6,8,12}
  *   q    = mag2(a) * mag2(b)  ->  intero in [0, 144], quindi 8 bit
  *   valore reale = q * 2^(scaleA + scaleB - 256)
  * Scrivendo q = 2^kk * (1 + f) con kk = indice del bit piu' significativo,
  *   valore = (1 + f) * 2^(kk + scaleA + scaleB - 256)
  * da cui direttamente:
  *   esponente FP32 (biased) = kk + scaleA + scaleB - 129
  *   mantissa                = (q - 2^kk) << (23 - kk)
  * Poiche' q - 2^kk < 2^kk e kk <= 7, la mantissa entra sempre nei 23 bit
  * senza perdere un solo bit: nessun arrotondamento, nessuno sticky bit.
  *
  * Casi speciali:
  *   q == 0            -> zero con segno (il segno si propaga: -0 * 1 = -0)
  *   scala E8M0 == 0xFF -> l'intero blocco e' NaN: si emette il NaN canonico
  *
  * Uscita impacchettata: l'elemento i occupa i bit [32i+31 : 32i] di `out`.
  * Un `Vec` in IO diventerebbe `io_out_0 ... io_out_31` in SystemVerilog,
  * scomodo da pilotare in C++; un unico segnale largo diventa invece un array
  * di parole a 32 bit, in cui la parola i e' esattamente l'elemento i.
  */
class MXFP4ElemMul(val k: Int = 32) extends Module {
  require(k > 0 && k % 2 == 0, "k deve essere pari e positivo")

  val io = IO(new Bundle {
    val a      = Input(UInt((4 * k).W))
    val b      = Input(UInt((4 * k).W))
    val scaleA = Input(UInt(8.W))
    val scaleB = Input(UInt(8.W))
    val out    = Output(UInt((32 * k).W))   // elemento i nei bit [32i+31 : 32i]
    val isNaN  = Output(Bool())
  })

  private val QNAN = "h7FC00000".U(32.W)

  /** magnitudine E2M1 * 2, indicizzata dal campo exp|mantissa (3 bit) */
  private val magTable = VecInit(Seq(0, 1, 2, 3, 4, 6, 8, 12).map(_.U(4.W)))

  private val nanScale = (io.scaleA === 0xFF.U) || (io.scaleB === 0xFF.U)

  /** esponente biased FP32 al netto di kk: scaleA + scaleB - 129 */
  private val expBase = (io.scaleA +& io.scaleB).zext - 129.S

  private def mulToF32(nibA: UInt, nibB: UInt): UInt = {
    val sign = nibA(3) ^ nibB(3)
    val q    = (magTable(nibA(2, 0)) * magTable(nibB(2, 0)))(7, 0)  // 0..144

    val kk     = Log2(q)                       // indice del MSB, 0..7
    val topBit = (1.U(8.W) << kk)(7, 0)        // 2^kk
    val frac   = q - topBit                    // < 2^kk
    val mant   = (frac << (23.U - kk))(22, 0)  // esatta: nessun bit perso

    val expAll   = (kk.zext + expBase).asUInt
    val expField = expAll(7, 0)

    val zero   = Cat(sign, 0.U(31.W))
    val normal = Cat(sign, expField, mant)
    Mux(nanScale, QNAN, Mux(q === 0.U, zero, normal))
  }

  private val results = Seq.tabulate(k) { i =>
    mulToF32(io.a(4 * i + 3, 4 * i), io.b(4 * i + 3, 4 * i))
  }

  // Cat mette il PRIMO argomento nei bit piu' significativi: invertendo la
  // sequenza l'elemento 0 finisce nei bit meno significativi, come richiesto.
  io.out   := Cat(results.reverse)
  io.isNaN := nanScale
}
