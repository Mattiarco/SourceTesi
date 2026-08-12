"""Creazione della struttura di progetto (build.sbt, elaborazione, Makefile)."""
from __future__ import annotations

from pathlib import Path

CHISEL_VERSION = "6.6.0"
SCALA_VERSION = "2.13.14"
SBT_VERSION = "1.10.1"

BUILD_SBT = """// GENERATO AUTOMATICAMENTE
ThisBuild / scalaVersion := "{scala}"
ThisBuild / version      := "0.1.0"
ThisBuild / organization := "mxfp4.agent"

lazy val root = (project in file("."))
  .settings(
    name := "mxfp4-generated",
    libraryDependencies ++= Seq(
      "org.chipsalliance" %% "chisel" % "{chisel}",
      "edu.berkeley.cs"   %% "chiseltest" % "6.0.0" % "test"
    ),
    scalacOptions ++= Seq(
      "-language:reflectiveCalls",
      "-deprecation",
      "-feature",
      "-Xcheckinit",
      "-Ymacro-annotations"
    ),
    addCompilerPlugin("org.chipsalliance" % "chisel-plugin" % "{chisel}" cross CrossVersion.full)
  )
"""

ELABORATE_SCALA = """// GENERATO AUTOMATICAMENTE — non modificare.
package mxfp4

// `_root_.` è necessario: dentro `package mxfp4` il nome `circt` verrebbe
// risolto come `chisel3.util.circt` se il file importasse `chisel3.util._`.
import _root_.circt.stage.ChiselStage

object Elaborate extends App {{
  val outDir = sys.env.getOrElse("MXFP4_RTL_DIR", "rtl")
  ChiselStage.emitSystemVerilogFile(
    new {module}(),
    Array("--target-dir", outDir),
    Array("-disable-all-randomization", "-strip-debug-info", "--lowering-options=disallowLocalVariables")
  )
  println("[elaborate] SystemVerilog scritto in " + outDir)
}}
"""

MAKEFILE = """# GENERATO AUTOMATICAMENTE
MODULE   := {module}
RTL_DIR  := rtl
SIM_DIR  := sim
OBJ_DIR  := obj_dir
VERILATOR ?= verilator

.PHONY: all rtl sim run clean

all: run

rtl:
\tsbt "runMain mxfp4.Elaborate"

sim:
\t$(VERILATOR) --cc --exe --build -Wall -Wno-fatal -Wno-DECLFILENAME \\
\t  --top-module $(MODULE) --Mdir $(OBJ_DIR) -o sim_$(MODULE) \\
\t  -CFLAGS "-I$(CURDIR)/$(SIM_DIR) -O2" \\
\t  $(RTL_DIR)/*.sv $(SIM_DIR)/tb_$(MODULE).cpp

run: sim
\t./$(OBJ_DIR)/sim_$(MODULE)

clean:
\trm -rf $(OBJ_DIR) target project/target
"""

GITIGNORE = "obj_dir/\ntarget/\nproject/target/\nproject/project/\n*.vcd\n.bsp/\n"


def scaffold_chisel(root: Path, module: str) -> None:
    (root / "project").mkdir(parents=True, exist_ok=True)
    (root / "src/main/scala/mxfp4").mkdir(parents=True, exist_ok=True)
    (root / "sim").mkdir(parents=True, exist_ok=True)
    (root / "rtl").mkdir(parents=True, exist_ok=True)

    (root / "build.sbt").write_text(
        BUILD_SBT.format(scala=SCALA_VERSION, chisel=CHISEL_VERSION), encoding="utf-8")
    (root / "project/build.properties").write_text(f"sbt.version={SBT_VERSION}\n", encoding="utf-8")
    (root / "src/main/scala/mxfp4/Elaborate.scala").write_text(
        ELABORATE_SCALA.format(module=module), encoding="utf-8")
    (root / "Makefile").write_text(MAKEFILE.format(module=module), encoding="utf-8")
    (root / ".gitignore").write_text(GITIGNORE, encoding="utf-8")


def scaffold_verilog(root: Path, module: str) -> None:
    (root / "rtl").mkdir(parents=True, exist_ok=True)
    (root / "sim").mkdir(parents=True, exist_ok=True)
    mk = MAKEFILE.format(module=module).replace(
        'rtl:\n\tsbt "runMain mxfp4.Elaborate"', "rtl:\n\t@echo 'RTL scritto direttamente in rtl/'")
    (root / "Makefile").write_text(mk, encoding="utf-8")
    (root / ".gitignore").write_text(GITIGNORE, encoding="utf-8")


def scaffold(root: Path, module: str, meta_hdl: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    if meta_hdl == "chisel":
        scaffold_chisel(root, module)
    else:
        scaffold_verilog(root, module)
