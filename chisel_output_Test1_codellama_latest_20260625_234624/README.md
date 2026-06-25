# Test1 — Chisel MXFP4

Generato automaticamente da `python_to_chisel_mxfp4_ollama.py` con modello **codellama:latest**.

## File

| File | Descrizione |
|---|---|
| `Test1_mxfp4.scala` | Modulo Chisel principale con Bundle MXFP4 |
| `Test1_tb.scala` | Testbench ChiselTest |
| `report_Test1.md` | Analisi e note per la tesi |
| `build.sbt` | Configurazione SBT |
| `raw_response.txt` | Output grezzo del modello |

## Compilazione e test

```bash
sbt test
```

## Struttura MXFP4 (E2M1)

```
Bit 3   → segno (0=+, 1=−)
Bit 2:1 → esponente (bias=1)
Bit 0   → mantissa
```
