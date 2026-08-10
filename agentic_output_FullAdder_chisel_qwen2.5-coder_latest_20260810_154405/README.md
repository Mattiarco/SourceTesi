# FullAdder — Chisel 3 (Scala)

Generato da **Tesi.py** con modello Ollama `qwen2.5-coder:latest`.
Linguaggio Meta-HDL scelto dal Selector agent: **Chisel 3 (Scala)** — Scelto esplicitamente da riga di comando (--lang).

## Esecuzione dei test

```bash
sbt test
```

## File generati

| File | Descrizione |
|---|---|
| `src/main/scala/mxfp4/MXFP4.scala` | Bundle MXFP4 condiviso (sign/exp/mant + encode/decode) |
| `src/main/scala/<Modulo>.scala` | Modulo Chisel MXFP4 |
| `src/test/scala/<Modulo>Test.scala` | Testbench ChiselTest (backend Verilator) |
| `build.sbt` | Progetto SBT |
| `report_FullAdder.md` | Report completo per la tesi |
| `agent_log.json` | Log JSON del workflow agentico |
