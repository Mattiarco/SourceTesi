# MXFP4 Meta-HDL Agent

Pipeline agentica che trasforma una richiesta in linguaggio naturale in una
**unità aritmetica MXFP4** descritta in **Chisel** (o SystemVerilog), completa di
**testbench Verilator** e verificata contro un **golden model Python**.

Riferimento: *An agentic-driven approach for Meta-HDL* — coprocessore MXFP4/NVFP4
per RISC-V.

```
richiesta ──▶ PLANNER ──▶ CODER ──▶ REVIEWER/FIXER ──▶ TESTER ──┐
              (piano +      (Chisel +     (review statica       │
               prompt)       tb C++)       + patch sui log)     │
                    ▲                                           │
                    └────────────── loop di fix ◀───────────────┘
                              (max N round, default 4)
```

## Perché funziona

Un LLM lasciato a sé sbaglia quasi sempre i dettagli di MXFP4. Il sistema
inietta in ogni prompt la specifica esatta (bias E2M1 = 1, subnormale `0b0001` =
0.5, saturazione a ±6, scala E8M0 = 2^(X−127), ordine dei nibble) e soprattutto
**non si fida del modello per la correttezza**: i valori attesi vengono da un
golden model Python indipendente (`mxfp4agent/knowledge/golden_model.py`), non
dall'LLM.

L'osservazione architetturale che il Planner sfrutta: le magnitudini E2M1 sono
multipli di 0.5 e le scale E8M0 sono potenze esatte di due — quindi un
dot-product MXFP4 è **aritmetica intera esatta** più una somma di esponenti.
Niente moltiplicatori FP, niente arrotondamento.

## Installazione

```bash
python -m pip install -r requirements.txt      # opzionale: solo pytest e anthropic
```

Il core non ha dipendenze esterne obbligatorie (usa `urllib`).

**Toolchain** (per la verifica; il codice viene generato anche senza):

| tool | serve per | installazione |
|---|---|---|
| `verilator` | simulazione | `apt install verilator` / `brew install verilator` |
| `sbt` + JDK 17 | Chisel → SystemVerilog | <https://www.scala-sbt.org/download> |

**LLM** — uno dei due:

```bash
ollama pull qwen2.5-coder:14b        # locale
export ANTHROPIC_API_KEY=sk-ant-...  # Claude
```

Controlla tutto con:

```bash
python run.py --doctor
```

## Uso

```bash
# Ollama locale (default)
python run.py "unità dot-product MXFP4 a 32 elementi, combinatoria"

# Claude API
python run.py "MAC MXFP4 pipelined a 2 stadi, uscita FP32" --provider claude

# forzare il Meta-HDL e la dimensione del blocco
python run.py "moltiplicatore element-wise MXFP4" --target systemverilog -k 32

# modelli diversi per agente (planner grande, coder specializzato)
python run.py "..." --provider ollama \
  --planner-model llama3.1:70b --coder-model qwen2.5-coder:32b

# smoke test offline, senza LLM né rete
python run.py --selftest

# rieseguire solo la toolchain su un progetto già generato
python run.py --resume out/MXFP4DotProduct
```

Opzioni utili: `--few-shot` (aggiunge un design di riferimento al prompt, utile
con modelli piccoli), `--max-fix-rounds N`, `--vectors N`, `--no-static-review`,
`--keep-going`.

## Output

```
out/<Modulo>/
├── src/main/scala/mxfp4/<Modulo>.scala   ← design generato
├── src/main/scala/mxfp4/Elaborate.scala  ← elaborazione Chisel → SystemVerilog
├── sim/tb_<Modulo>.cpp                   ← testbench Verilator generato
├── sim/test_vectors.h                    ← vettori attesi dal golden model
├── rtl/*.sv                              ← SystemVerilog prodotto
├── build.sbt, project/, Makefile
├── plan.json          ← piano del Planner
├── prompt_coder.md    ← prompt compilato dato al Coder
├── report.json        ← esito, statistiche token, trace degli agenti
└── README.md
```

Riprodurre a mano la verifica:

```bash
cd out/<Modulo>
sbt "runMain mxfp4.Elaborate"    # Chisel → rtl/*.sv
make run                         # verilator: build + simulazione
```

## Gli agenti

| agente | input | output | note |
|---|---|---|---|
| **Planner** | richiesta utente + spec MXFP4 | `plan.json` + **prompt compilato** per il Coder | sceglie il Meta-HDL, dimensiona l'accumulatore, definisce il piano di test |
| **Coder** | prompt compilato + contratto dell'header | design + testbench C++ | emette sempre file interi, mai diff |
| **Reviewer/Fixer** | codice, log di sbt/Verilator | review JSON o file corretti | non può toccare `test_vectors.h` |
| **Tester** | piano + file | progetto su disco, esito toolchain, diagnosi | genera i vettori, esegue elaborate → lint → build → simulate |

Il loop di fix riparte dal fallimento più a monte: un errore di elaborazione non
arriva mai a Verilator.

## Vettori di test

Generati da `mxfp4agent/toolchain/testvectors.py`. Oltre ai casi casuali
(seed deterministico), coprono sempre i bug tipici:

`all_zero`, `all_one`, `all_max` (saturazione), `max_negative`,
`all_subnormal` (il celebre 0.5 dimenticato), `negative_zero`, `sweep_codes`
(tutti e 16 i codici), `extreme_scales`, `scale_nan`, `scale_overflow`.

## Test

```bash
python run_tests.py            # funziona anche senza pytest (shim interno)
python run_tests.py golden     # solo il golden model
```

## Estensioni naturali

- **NVFP4**: K = 16 con scala E4M3 — aggiungere il formato in `golden_model.py`
  e `mxfp4_spec.py`; gli agenti non cambiano.
- **Integrazione RISC-V**: avvolgere il modulo in un'interfaccia CV-X-IF /
  Rocket RoCC; il Planner può generare direttamente il wrapper.
- **PPA loop**: aggiungere uno stadio di sintesi (yosys) e retroazionare
  area/timing al Planner.
