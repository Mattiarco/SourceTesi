# MxFp4Unit — Chisel MXFP4

Generato da **agentic_chisel_mxfp4_ollama.py** con modello `qwen2.5-coder:latest`.

## Compilazione e test

I test sono scritti con ChiselTest e girano in simulazione tramite **Verilator** (annotazione `VerilatorBackendAnnotation`): assicurati che `verilator` sia installato e nel PATH prima di lanciare `sbt test` (su Windows: via WSL o MSYS2/Cygwin).

```bash
sbt test
```

## File generati

| File | Descrizione |
|---|---|
| `src/main/scala/mxfp4/MXFP4.scala` | Bundle MXFP4 condiviso (sign/exp/mant + encode/decode) |
| `src/main/scala/MxFp4Unit.scala` | Modulo Chisel MXFP4 |
| `src/test/scala/MxFp4UnitTest.scala` | Testbench ChiselTest (backend Verilator) |
| `report_MxFp4Unit.md` | Report completo per la tesi |
| `agent_log.json` | Log JSON del workflow agentico |
| `build.sbt` | Progetto SBT |
