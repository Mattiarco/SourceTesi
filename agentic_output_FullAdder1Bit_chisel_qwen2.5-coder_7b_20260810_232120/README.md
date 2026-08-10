# FullAdder1Bit — Chisel 3 (Scala)

Generato da **Tesi.py** con modello Ollama `qwen2.5-coder:7b`.
Linguaggio Meta-HDL scelto dal Selector agent: **Chisel 3 (Scala)** — Fallback: risposta del Selector non interpretabile come JSON valido.

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
| `report_FullAdder1Bit.md` | Report completo per la tesi |
| `agent_log.json` | Log JSON del workflow agentico |
