// GENERATO AUTOMATICAMENTE
ThisBuild / scalaVersion := "2.13.14"
ThisBuild / version      := "0.1.0"
ThisBuild / organization := "mxfp4.agent"

lazy val root = (project in file("."))
  .settings(
    name := "mxfp4-generated",
    libraryDependencies ++= Seq(
      "org.chipsalliance" %% "chisel" % "6.6.0",
      "edu.berkeley.cs"   %% "chiseltest" % "6.0.0" % "test"
    ),
    scalacOptions ++= Seq(
      "-language:reflectiveCalls",
      "-deprecation",
      "-feature",
      "-Xcheckinit",
      "-Ymacro-annotations"
    ),
    addCompilerPlugin("org.chipsalliance" % "chisel-plugin" % "6.6.0" cross CrossVersion.full)
  )
