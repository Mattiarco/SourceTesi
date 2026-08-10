package mxfp4

import chisel3._
import chisel3.experimental.BundleLiterals._

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

object MXFP4 {
  def apply(): MXFP4 = new MXFP4

  def apply(bits: Int): MXFP4 = {
    val s = (bits >> 3) & 0x1
    val e = (bits >> 1) & 0x3
    val m = bits & 0x1
    (new MXFP4).Lit(_.sign -> (s == 1).B, _.exp -> e.U(2.W), _.mant -> m.U(1.W))
  }

  def decode(bits: Int): Double = {
    val sign = (bits >> 3) & 0x1
    val exp  = (bits >> 1) & 0x3
    val mant = bits & 0x1
    val s = if (sign == 1) -1.0 else 1.0
    if (exp == 0) {
      s * mant * 0.5
    } else {
      s * (1.0 + mant * 0.5) * math.pow(2.0, exp - 1)
    }
  }

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
