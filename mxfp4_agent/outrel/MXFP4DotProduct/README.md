# MXFP4DotProduct — unità aritmetica MXFP4

Generato automaticamente dalla pipeline agentica (Planner → Coder → Reviewer → Tester).

**Stato verifica:** ❌ Verifica non superata dopo 0 round di fix. Ultimo stato: - elaborate

## Richiesta originale
> Chisel permette di parametrizzare K e di generare l'albero di somma con reduceTree; il datapath e' interamente intero quindi non serve libreria FP.

## File
- `src/main/scala/mxfp4/MXFP4DotProduct.scala`
- `sim/tb_MXFP4DotProduct.cpp`
- `sim/test_vectors.h` — vettori attesi dal golden model Python (non modificare)
- `plan.json`, `prompt_coder.md`, `report.json` — tracciabilità della generazione

## Riprodurre la verifica
```bash
sbt "runMain mxfp4.Elaborate"   # Chisel -> SystemVerilog
make run                         # verilator: build + simulazione
```

## Interpretazione dell'uscita
Il risultato reale del dot-product è `accQ2 / 4 * 2^expOut`, dove `expOut =
(scaleA - 127) + (scaleB - 127)`. L'accumulo è **esatto**: nessun errore di
arrotondamento è introdotto dall'hardware.
