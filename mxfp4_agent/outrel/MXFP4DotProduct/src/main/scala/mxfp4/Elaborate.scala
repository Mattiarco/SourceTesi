// GENERATO AUTOMATICAMENTE — non modificare.
package mxfp4

// `_root_.` è necessario: dentro `package mxfp4` il nome `circt` verrebbe
// risolto come `chisel3.util.circt` se il file importasse `chisel3.util._`.
import _root_.circt.stage.ChiselStage

object Elaborate extends App {
  val outDir = sys.env.getOrElse("MXFP4_RTL_DIR", "rtl")
  ChiselStage.emitSystemVerilogFile(
    new MXFP4DotProduct(),
    Array("--target-dir", outDir),
    Array("-disable-all-randomization", "-strip-debug-info", "--lowering-options=disallowLocalVariables")
  )
  println("[elaborate] SystemVerilog scritto in " + outDir)
}
