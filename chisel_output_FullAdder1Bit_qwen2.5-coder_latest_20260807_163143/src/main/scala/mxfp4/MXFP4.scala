package mxfp4

import chisel3._

// Formato MXFP4 E2M1 (OCP MX Specification v1.0): 4 bit totali.
//   bit[3]   = segno      (0 = positivo, 1 = negativo)
//   bit[2:1] = esponente a 2 bit (bias = 1)
//   bit[0]   = mantissa a 1 bit
// Valore: (-1)^sign * 2^(exp-1) * (1 + mant*0.5)
class MXFP4 extends Bundle {
  val sign = Bool()
  val exp  = UInt(2.W)
  val mant = UInt(1.W)
}

// Utility di conversione tra MXFP4 e Double, pensate per i testbench
// (poke/expect) e per gli agenti che generano i test.
object MXFP4 {
  def apply(): MXFP4 = new MXFP4

  // Converte i 4 bit codificati (0..15) nel Double rappresentato.
  def decode(bits: Int): Double = {
    val sign = (bits >> 3) & 0x1
    val exp  = (bits >> 1) & 0x3
    val mant = bits & 0x1
    if (exp == 0 && mant == 0) {
      0.0
    } else {
      val s = if (sign == 1) -1.0 else 1.0
      val m = 1.0 + mant * 0.5
      s * m * math.pow(2.0, exp - 1)
    }
  }

  // Converte un Double nella codifica MXFP4 (0..15) più vicina (round-to-nearest).
  def encode(value: Double): Int = {
    var bestBits = 0
    var bestDiff = Double.MaxValue
    for (bits <- 0 until 16) {
      val diff = math.abs(decode(bits) - value)
      if (diff < bestDiff) {
        bestDiff = diff
        bestBits = bits
      }
    }
    bestBits
  }
}
