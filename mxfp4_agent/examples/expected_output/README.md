# Output atteso — tre esempi di riferimento

Questi sono i deliverable che la pipeline **deve** produrre: per ogni esempio il
piano del Planner, il modulo Chisel del Coder e il testbench Verilator, cioè
esattamente i tre artefatti che compaiono in `out/<Modulo>/` a fine run.

Servono a tre scopi: metro di paragone per giudicare l'output degli agenti,
materiale per la tesi, e riferimento per capire cosa significa "corretto" su
MXFP4.

| # | Modulo | Stile | Kernel | Cosa dimostra |
|---|--------|-------|--------|----------------|
| 1 | `MXFP4DotProduct` | combinatorio | `dot_product` | accumulo intero esatto, caso base |
| 2 | `MXFP4DotProductPipe` | sequenziale, 2 stadi | `dot_product` | pipeline, `valid`, reset, throughput 1/ciclo |
| 3 | `MXFP4ElemMul` | combinatorio | `elementwise_mul` | conversione esatta MXFP4 → IEEE-754 FP32 |

## Stato di verifica

I tre testbench compilano con `g++ -Wall -Wextra` **senza un solo warning** e
passano tutti i 58 vettori del golden model, confrontati contro
un'implementazione C++ indipendente della semantica attesa:

```
01 dot-product   : TEST PASSED (58 vectors)
02 pipelined     : TEST PASSED (58 vectors)
03 element-wise  : TEST PASSED (58 vectors)
```

Più significativo del passaggio è la **controprova**: introducendo di proposito
bug classici, i testbench li catturano tutti.

| bug iniettato | esito |
|---|---|
| subnormale 0.5 trattato come zero | `TEST FAILED (50/58)` |
| bias E8M0 sbagliato (127 invece di 254) | `TEST FAILED (58/58)` |
| esponente FP32 sfasato di 1 | `TEST FAILED (55/58)` |
| `validOut` mai alzato (pipeline rotto) | `TEST FAILED (58/58)` |

Un testbench che passa sempre non verifica niente: questa tabella è la prova
che quelli generati discriminano davvero.

**Avvertenza onesta:** i moduli Chisel non sono stati elaborati con `sbt` in
questo ambiente — non era disponibile. Sono stati verificati per semantica e
per conformità alle regole del progetto, e i testbench sono stati compilati
contro stub che replicano fedelmente i nomi di porta e il comportamento che
Chisel genera. Per la conferma definitiva esegui `make rtl && make run` sulla
tua macchina.

## Il filo conduttore

Tutti e tre partono dalla stessa osservazione, che è il vero contenuto tecnico
del progetto:

> Le magnitudini E2M1 appartengono a `{0, .5, 1, 1.5, 2, 3, 4, 6}`, quindi
> `mag × 2` è un intero su 4 bit. Le scale E8M0 sono potenze esatte di due.
> Ne segue che **l'aritmetica MXFP4 è aritmetica intera**, e le scale si
> combinano sommando esponenti.

Conseguenza pratica: niente moltiplicatori floating point, niente
arrotondamento, nessun errore numerico introdotto dall'hardware. L'esempio 1 si
ferma al risultato intero; il 3 mostra che anche la conversione a FP32 è una
pura ricomposizione di campi.

## Riprodurre

```bash
cd out/<Modulo>
sbt "runMain mxfp4.Elaborate"   # Chisel -> rtl/*.sv
make run                        # verilator: build + simulazione
```

Per generare a mano l'header dei vettori di un kernel:

```python
from mxfp4agent.toolchain.testvectors import build_vectors, render_header
open("test_vectors.h", "w").write(
    render_header(build_vectors("elementwise_mul", 48), "elementwise_mul"))
```

## Dettagli che fanno inciampare gli LLM

Ognuno di questi è costato almeno un round di fix in un run reale, ed è ora
documentato nei prompt (`mxfp4agent/knowledge/mxfp4_spec.py`):

- Chisel appiattisce il Bundle `io`: in Verilator le porte sono `dut->io_a`,
  non `dut->a`. `clock` e `reset` invece non hanno prefisso ed esistono sempre,
  anche in un modulo combinatorio.
- Un `Vec` in IO diventa `io_out_0 … io_out_31`: usare un unico segnale largo
  rende il testbench molto più semplice (esempio 3).
- `-3.S(5.W)` in Scala è `-(3.S(5.W))`, cioè negazione hardware che allarga di
  un bit. Il letterale a larghezza fissa è `(-3).S(5.W)`.
- L'ammontare di uno shift dev'essere `UInt` o `Int`, mai `SInt`; e `.asUInt`
  su un esponente negativo produce uno shift enorme.
- `+` tronca alla larghezza massima degli operandi: negli alberi di somma serve
  `+&`.
- `reduceTree` esiste su `Vec`, non su `Seq`.
