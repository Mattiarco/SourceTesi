# FullAdder — Amaranth HDL (Python)

Generato da **Tesi.py** con modello Ollama `qwen2.5-coder:latest`.
Linguaggio Meta-HDL scelto dal Selector agent: **Amaranth HDL (Python)** — Scelto esplicitamente da riga di comando (--lang).

## Esecuzione dei test

```bash
pip install -r requirements.txt && python testbench.py
```

## File generati

| File | Descrizione |
|---|---|
| `mxfp4.py` | Layout MXFP4 condiviso (MXFP4Layout + encode/decode) |
| `module.py` | Modulo Amaranth MXFP4 |
| `testbench.py` | Testbench amaranth.sim |
| `requirements.txt` | Dipendenze pip |
| `report_FullAdder.md` | Report completo per la tesi |
| `agent_log.json` | Log JSON del workflow agentico |
