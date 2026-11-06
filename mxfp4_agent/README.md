# MXFP4 Meta-HDL Agent

Pipeline agentica che trasforma una richiesta in linguaggio naturale in una
**unità aritmetica MXFP4** descritta in **Chisel 6**, la verifica con
**Verilator** contro un **golden model Python** e ne misura area e timing con
**Yosys** e **nextpnr-ecp5** (place & route su Lattice ECP5 LFE5U-85F).

Gli LLM girano **solo in locale con Ollama** (nessuna API esterna); il core è
solo standard library Python.

Riferimento: *An agentic-driven approach for Meta-HDL* — coprocessore MXFP4/NVFP4
per RISC-V.

```
richiesta ─▶ PLANNER ─▶ CODER ─▶ REVIEWER ─▶ TESTER ──────────────────────────────┐
             piano JSON   modulo    review    lint statico → sbt/Chisel → Verilator │
             (porte dal   Chisel    statica   (testbench generato dal contratto)    │
             contratto)                       → Yosys → nextpnr (multi-seed)        │
                  ▲                                                                 │
                  └──────────── loop di fix (FIXER, max N round) ◀─────────────────┘
```

## Perché funziona

* **Il golden model è l'unica fonte di verità.** I valori attesi
  (`sim/test_vectors.h`) vengono da `mxfp4agent/knowledge/golden_model.py`;
  l'LLM non li vede né li può modificare.
* **L'LLM scrive solo il modulo Chisel.** Per i kernel supportati le porte sono
  fissate da un contratto (`knowledge/contracts.py`) e il testbench Verilator
  è generato in modo deterministico (`toolchain/tbgen.py`).
* **I modelli piccoli sono aiutati in modo deterministico:** few-shot col
  design di riferimento più vicino, lint Chisel statico prima di sbt con
  correzioni automatiche sicure, errori di compilazione riassunti in
  `file:riga` + contesto + suggerimento.
* **Numeri fisici riproducibili:** Fmax misurata su un wrapper a registri
  (registro → DUT → registro), su più seed di place & route, con mediana,
  minimo e massimo.

La specifica MXFP4 codificata (non modificabile): E2M1 con bias 1, subnormale
`0b0001` = 0.5, saturazione a ±6, E8M0 = 2^(X−127) con `0xFF` = NaN, elemento i
nei bit `[4i+3:4i]`.

## Installazione (server Linux, senza sudo)

```bash
bash scripts/setup_server.sh --models qwen2.5-coder:14b,qwen2.5-coder:32b
source ~/.local/mxfp4/env.sh          # da aggiungere a ~/.bashrc (o --add-to-bashrc)
python3 run.py --doctor
```

Lo script è idempotente e installa in `~/.local/mxfp4`:

| componente | come | note |
|---|---|---|
| JDK 17 + sbt 1.10.1 | Coursier (binario `cs` del repo) | versioni fissate |
| yosys, nextpnr-ecp5, verilator | OSS CAD Suite (build statica) | solo `oss-cad-suite/bin` va nel PATH |
| Chisel 6.6.0 + firtool 1.62.1 | warm-up: `run.py --selftest` | popola le cache: dopo funziona **offline** |
| modelli Ollama | `ollama pull` (opzione `--models`) | Ollama deve già girare (anche fuori dalla home) |

`--doctor` controlla Ollama e i modelli configurati (context length,
quantizzazione, digest), la GPU (`nvidia-smi`), Java, sbt, la cache di
Chisel/firtool, Verilator, g++, make, yosys e nextpnr-ecp5, e per ognuno che
manca stampa come installarlo **senza sudo**. Esce con 1 se manca un componente
obbligatorio.

**Lavorare offline.** Dopo il warm-up sbt trova Scala, Chisel, il plugin e
firtool nelle cache (`~/.cache/coursier`, `~/.ivy2`, `~/.cache/llvm-firtool`).
Per pre-scaricare su una macchina con rete e copiare: basta copiare queste
cartelle nella home del server. Ollama è l'unico servizio contattato durante una
run (`OLLAMA_HOST` o `--host`).

## Uso

```bash
# caso base (criterio di accettazione)
python3 run.py "unità dot-product MXFP4 a 32 elementi, combinatoria"

# riproducibilità: seed del modello, seed dei vettori di test fisso
python3 run.py "..." --seed 3 --vector-seed 1234

# modelli diversi per agente da file JSON
cat > profiles.json <<'EOF'
{"planner":  {"model": "qwen2.5-coder:32b", "num_ctx": 16384},
 "coder":    {"model": "qwen2.5-coder:14b", "temperature": 0.1},
 "reviewer": {"model": "qwen2.5-coder:14b"}}
EOF
python3 run.py "..." --profiles profiles.json

# verifica più ricca: dump VCD e coverage Verilator
python3 run.py "..." --trace --coverage

# 5 seed di P&R (default) a 150 MHz, poi 2 varianti ottimizzate per Fmax/area
python3 run.py "..." --target-mhz 150 --ppa-rounds 2

# smoke test offline (provider mock, nessun LLM); --strict = toolchain obbligatoria
python3 run.py --selftest --strict

# rieseguire solo toolchain, sintesi e timing su un progetto generato
python3 run.py --resume out/MXFP4DotProduct
```

Opzioni principali:

| opzione | default | effetto |
|---|---|---|
| `--model`, `--planner-model`, … | `qwen2.5-coder:14b` | modello Ollama (globale o per agente) |
| `--profiles FILE` | – | impostazioni per agente: `model`, `num_ctx`, `temperature`, `max_tokens`, `keep_alive`, `seed` |
| `--num-ctx` | 32768 | ridotto automaticamente alla context length reale del modello (`/api/show`) |
| `--seed` | 1234 | `options.seed` di Ollama |
| `--keep-alive` | `30m` | quanto Ollama tiene il modello in memoria |
| `--timeout` / `--chunk-timeout` | 1800 / 600 s | tetto per chiamata / attesa massima fra due chunk dello stream |
| `--tb template\|llm` | `template` | testbench generato dal contratto o scritto dal Coder |
| `--no-few-shot`, `--no-lint`, `--no-static-review` | – | ablazioni |
| `--sbt-mode auto\|client\|batch` | `auto` | `client`: server sbt riusato fra i round (circa 6 volte più veloce) |
| `--max-fix-rounds` | 4 | round di riparazione |
| `--vectors` | 64 | vettori casuali oltre a quelli diretti ed esaustivi |
| `--pnr-seeds` | 5 | seed di place & route |
| `--no-synth` | – | salta Yosys e nextpnr |

**Memoria.** La cache KV cresce con `num_ctx`: con `qwen2.5-coder:14b` e
`num_ctx` 32768 Ollama occupa circa 15 GB e su una macchina da 16 GB senza GPU
l'OOM killer termina `llama-server` (osservato). In quel caso usa
`--num-ctx 16384`: i prompt della pipeline restano sotto i ~10k token, e se un
prompt rischia di essere troncato il provider lo segnala.

## Cosa produce una run

```
out/<Modulo>/
├── src/main/scala/mxfp4/<Modulo>.scala   ← unico file scritto dall'LLM
├── sim/tb_<Modulo>.cpp                   ← testbench generato dal contratto
├── sim/test_vectors.h                    ← vettori attesi dal golden model
├── sim/waves.vcd, sim/coverage.dat       ← con --trace / --coverage
├── rtl/*.sv                              ← SystemVerilog emesso da firtool
├── synth/                                ← log e netlist Yosys, report nextpnr per seed, regwrap.v
├── logs/r<N>_<fase>.log                  ← log completo di ogni fase per ogni round
├── plan.json, prompt_coder.md
├── report.json                           ← esito, LLM, round, tempi, metriche
├── physical.json, physical.md            ← area e timing
└── README.md
```

### `report.json`

| campo | significato |
|---|---|
| `ok`, `message` | esito della verifica (es. `"282 vettori superati"`) |
| `kernel`, `clocking`, `testbench` | kernel del golden model, combinatorio/sequenziale, `template` o `llm` |
| `ports_overridden` | porte che il Planner aveva proposto diversamente dal contratto (corrette senza costare round) |
| `few_shot_reference` | design di riferimento mostrato al Coder |
| `fix_rounds` | round di fix usati (0 = corretto al primo colpo) |
| `first_failure_stage` | prima fase fallita (`chisel_lint`, `elaborate`, `lint`, `build`, `simulate`, `synth`) |
| `rounds` | esito sintetico di ogni round |
| `timing_s` | tempo totale, della toolchain e di ciascun agente LLM |
| `llm` | per agente: modello, **digest**, quantizzazione, context length, versione di Ollama, opzioni effettive (temperature, num_ctx, seed, num_predict) |
| `llm_stats` | chiamate, token in/out, secondi, retry per agente |
| `physical` | copia di `physical.json` |
| `ppa` | con `--ppa-rounds`: ogni variante tentata, esito, Fmax, LUT4 |
| `trace` | eventi con timestamp |

### `physical.json` / `physical.md`

| campo | come è misurato | come usarlo |
|---|---|---|
| `area_ecp5` | `synth_ecp5` sul solo DUT: LUT4, CCU2C (carry), PFUMX/L6MUX21, TRELLIS_FF, MULT18X18D, totale celle | area sul dispositivo target |
| `area_generic` | `synth -flatten -noabc` + `stat -tech cmos` (senza ABC: rapido e deterministico): celle logiche elementari per tipo e transistor stimati | confronto fra design indipendente dalla tecnologia |
| `fmax_mhz` | nextpnr sul **wrapper a registri** (ingressi da catena a scorrimento, uscite registrate ridotte a XOR, 4 pin), un valore per seed: `median`, `min`, `max`, `values` | **la Fmax del datapath**: registro → DUT → registro, indipendente dai pin |
| `direct` | DUT da solo con pin non vincolati (se i pin bastano: ≤ 365) — combinatorio: `pad_to_pad_ns`, ritardo pin→pin; sequenziale: Fmax senza wrapper | dipende dal piazzamento dei pin e varia con il seed: non usarlo per confrontare datapath |
| `wrapper` | bit registrati in ingresso/uscita, risorse del wrapper completo | overhead di misura (non incluso in `area_ecp5`) |
| `tools`, `seeds`, `target_mhz` | versioni di Yosys e nextpnr, seed usati, vincolo | riproducibilità |

`physical.md` è la stessa informazione in una tabella pronta per la tesi.
Le fasi fisiche sono **informative**: un vincolo di frequenza mancato è un
risultato, non un errore da riparare. Un RTL non sintetizzabile (latch, anelli
combinatori, driver multipli) invece fa fallire `synth` e passa al Fixer.

## Kernel, contratto delle porte e verifica

| kernel | porte (K = 32) | vettori |
|---|---|---|
| `dot_product` | `a`,`b` (128), `scaleA`,`scaleB` (8) → `accQ2` SInt(32), `expOut` SInt(16), `isNaN` | 10 casi diretti (zero, ±6, subnormali, −0, scale estreme, NaN…) + casuali |
| `elementwise_mul` / `_add` | `a`,`b`,`scaleA`,`scaleB` → `out` (1024 = 32 × FP32), `isNaN` | **esaustivo**: 256 coppie di codici × 8 (7) coppie di scale + NaN, più diretti e casuali |
| `to_bf16` | `a`, `scaleA` → `out` (512 = 32 × BF16), `isNaN` | **esaustivo**: 16 codici × tutte le 256 scale (l'intero dominio) |
| `custom` | decise dal Planner | testbench scritto dall'LLM |

Con `clocking = sequential` si aggiungono `validIn`/`validOut`: il testbench
immette un blocco per ciclo e controlla ordine e **latenza esatta**
(`latency_cycles`). Alla prima discrepanza stampa ingressi, atteso e ottenuto.

I design di riferimento verificati sono in `examples/expected_output/` (dot
product combinatorio e pipelined, moltiplicatore element-wise, conversione
BF16), con i risultati della controprova a bug iniettati.

## Campagna sperimentale

```bash
python3 bench.py --requests 1,3,4 --models qwen2.5-coder:14b --seeds 3 --out bench_out
python3 bench.py --models qwen2.5-coder:14b,qwen2.5-coder:32b --seeds 1-5 -- --pnr-seeds 3
python3 bench.py --summary-only --out bench_out
```

Ogni combinazione {richiesta di `examples/richieste.md`} × {modello} × {seed}
è una run completa in un sottoprocesso, nella propria cartella
`bench_out/r<id>/<modello>/s<seed>/`. La campagna è **riprendibile** (le run con
`result.json` vengono saltate; `--rerun-failed` ripete quelle finite in errore o
timeout). Dopo ogni run si aggiornano:

* `bench.csv` — una riga per run: esito, round di fix, fase del primo
  fallimento, token in/out, tempo per agente e della toolchain, risorse Yosys,
  Fmax e ritardo pin→pin, digest del modello e versione di Ollama;
* `bench_summary.md` — tasso di successo, mediana dei round, dei token, dei
  tempi, di LUT4 e Fmax per richiesta × modello e per modello, distribuzione
  della fase del primo fallimento.

Gli argomenti dopo `--` passano a `run.py`. Il seed LLM varia, il seed dei
vettori resta fisso: tutte le run sono verificate sullo stesso insieme di test.

## Test

```bash
python3 run_tests.py            # funziona anche senza pytest (shim interno)
python3 run_tests.py tbgen      # solo i file il cui nome contiene "tbgen"
```

I test che richiedono g++, yosys o nextpnr vengono saltati se gli strumenti
mancano. Con la toolchain installata (dopo `setup_server.sh`) girano tutti,
compresi un controllo di sintassi g++ del testbench generato e un place &
route reale con il wrapper.

## Struttura del codice

| percorso | contenuto |
|---|---|
| `mxfp4agent/workflow.py` | orchestrazione, loop di fix, rilevamento di errori ripetuti, loop PPA, `report.json` |
| `mxfp4agent/agents/` | Planner (JSON schema), Coder, Reviewer/Fixer, Tester |
| `mxfp4agent/llm/ollama_provider.py` | streaming, `format`, seed, keep_alive, `<think>`, metadati del modello |
| `mxfp4agent/knowledge/` | specifica MXFP4, golden model, contratti, design di riferimento |
| `mxfp4agent/toolchain/` | scaffold sbt, lint Chisel, riassunto errori, runner, vettori, testbench, sintesi, wrapper, metriche fisiche |
| `mxfp4agent/doctor.py` | `--doctor` |
| `bench.py`, `scripts/setup_server.sh` | campagna sperimentale, installazione |

## Estensioni naturali

- **NVFP4**: K = 16 con scala E4M3 — nuovo formato in `golden_model.py`,
  `mxfp4_spec.py` e un contratto in `contracts.py`.
- **Dot-product con uscita FP32** (richiesta 2), **quantizzatore** (5) e
  **accumulatore multi-blocco** (6): il golden model non ha ancora questi
  kernel. Oggi la richiesta 2 viene ricondotta a `dot_product` sequenziale
  (uscita intera esatta + esponente: la normalizzazione FP32 non viene
  verificata), le altre a `custom`. Per questo `bench.py` usa di default le
  richieste 1, 3 e 4, interamente verificabili.
- **Integrazione RISC-V**: wrapper CV-X-IF / RoCC generato dal Planner.
