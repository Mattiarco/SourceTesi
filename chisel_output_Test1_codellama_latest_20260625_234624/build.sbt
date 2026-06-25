scalaVersion := "2.13.12"

libraryDependencies ++= Seq(
  "org.chipsalliance" %% "chisel"      % "6.5.0",
  "edu.berkeley.cs"   %% "chiseltest"  % "6.0.0" % "test",
)

addCompilerPlugin(
  "org.chipsalliance" % "chisel-plugin" % "6.5.0" cross CrossVersion.full
)

scalacOptions ++= Seq(
  "-language:reflectiveCalls",
  "-deprecation",
  "-feature",
  "-Xcheckinit",
)
