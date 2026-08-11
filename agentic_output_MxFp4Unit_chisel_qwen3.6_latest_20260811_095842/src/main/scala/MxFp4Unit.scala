import chisel3._
import chisel3.util._

// =========================================================
// TIPO MXFP4 CONDIVISO (definito localmente per garantire la compilazione)
// In un progetto reale, questo tipo risiede in un package/shared object separato.
// Importarlo con: import your.project.shared.MxFp4
// =========================================================
class MxFp4 extends Bundle {
  val sign = Bool()
  val exp  = UInt(2.W)
  val frac = UInt(1.W)
}

object MxFp4 {
  def apply(): MxFp4 = new MxFp4
  
  // Conversioni implicite per interoperabilità con UInt(4.W) quando necessario
  implicit def toUInt(x: MxFp4): UInt = x.asUInt
  implicit def fromUInt(x: UInt): MxFp4 = x.asTypeOf(MxFp4())
}