# Report Agentico — FullAdder (Chisel 3 (Scala))

| Campo | Valore |
|---|---|
| **Modulo** | `FullAdder` |
| **Linguaggio Meta-HDL** | Chisel 3 (Scala) |
| **Modello Ollama** | `qwen2.5-coder:latest` |
| **Data** | 2026-08-10T15:44:05 |
| **Agenti eseguiti** | Selector, Planner, Coder, Reviewer, Fixer, Tester |
| **Iterazioni review/fix** | 5 |
| **Fix automatici (review/fix)** | 4 |
| **Escape (review/fix)** | 0 |
| **Iterazioni test/fix** | 5 |
| **Fix automatici (test/fix)** | 3 |
| **Escape (test/fix)** | 1 |

---

## Selezione del linguaggio (Selector Agent)

**Scelto:** Chisel 3 (Scala)

**Motivazione:** Scelto esplicitamente da riga di comando (--lang).

### Stato toolchain

- ✔ sbt trovato → compilazione reale abilitata
- ✔ verilator trovato (via bridge WSL) → simulazione reale abilitata

---

## Specifica Originale

Implementa un full adder 1-bit con ingressi e uscite MXFP4 E2M1: somma due valori MXFP4 A e B con riporto in ingresso Cin (Bool), producendo la somma MXFP4 e un riporto in uscita

---

## Piano di Implementazione (Planner Agent)

```json
{
  "name": "FullAdder",
  "description": "Un full adder 1-bit che somma due valori A e B con riporto Cin, producendo la somma S e un riporto in uscita Cout.",
  "inputs": [
    {
      "name": "A",
      "type": "MXFP4"
    },
    {
      "name": "B",
      "type": "MXFP4"
    },
    {
      "name": "Cin",
      "type": "Bool"
    }
  ],
  "outputs": [
    {
      "name": "S",
      "type": "MXFP4"
    },
    {
      "name": "Cout",
      "type": "Bool"
    }
  ],
  "operations": [
    {
      "name": "XOR_A_B",
      "description": "Calcola A XOR B.",
      "inputs": [
        "A",
        "B"
      ],
      "output": "XOR_AB"
    },
    {
      "name": "AND_A_Cin",
      "description": "Calcola AND tra A e Cin.",
      "inputs": [
        "A",
        "Cin"
      ],
      "output": "AND_ACin"
    },
    {
      "name": "AND_B_Cin",
      "description": "Calcola AND tra B e Cin.",
      "inputs": [
        "B",
        "Cin"
      ],
      "output": "AND_BCin"
    },
    {
      "name": "OR_ANDs",
      "description": "Calcola OR tra AND_ACin e AND_BCin.",
      "inputs": [
        "AND_ACin",
        "AND_BCin"
      ],
      "output": "OR_ANDs"
    },
    {
      "name": "XOR_XOR_AB_Cout",
      "description": "Calcola XOR tra il risultato di XOR_A_B e OR_ANDs.",
      "inputs": [
        "XOR_AB",
        "OR_ANDs"
      ],
      "output": "S"
    },
    {
      "name": "AND_XOR_AB_OR_ANDs",
      "description": "Calcola AND tra XOR_AB e OR_ANDs.",
      "inputs": [
        "XOR_AB",
        "OR_ANDs"
      ],
      "output": "Cout"
    }
  ],
  "nome_modulo": "FullAdder",
  "descrizione": "Un full adder 1-bit che somma due valori A e B con riporto Cin, producendo la somma S e un riporto in uscita Cout.",
  "ingressi": [
    {
      "name": "A",
      "type": "MXFP4"
    },
    {
      "name": "B",
      "type": "MXFP4"
    },
    {
      "name": "Cin",
      "type": "Bool"
    }
  ],
  "uscite": [
    {
      "name": "S",
      "type": "MXFP4"
    },
    {
      "name": "Cout",
      "type": "Bool"
    }
  ],
  "passi_algoritmo": [
    "XOR_A_B: Calcola A XOR B.",
    "AND_A_Cin: Calcola AND tra A e Cin.",
    "AND_B_Cin: Calcola AND tra B e Cin.",
    "OR_ANDs: Calcola OR tra AND_ACin e AND_BCin.",
    "XOR_XOR_AB_Cout: Calcola XOR tra il risultato di XOR_A_B e OR_ANDs.",
    "AND_XOR_AB_OR_ANDs: Calcola AND tra XOR_AB e OR_ANDs."
  ]
}
```

### Algoritmo pianificato

- XOR_A_B: Calcola A XOR B.
- AND_A_Cin: Calcola AND tra A e Cin.
- AND_B_Cin: Calcola AND tra B e Cin.
- OR_ANDs: Calcola OR tra AND_ACin e AND_BCin.
- XOR_XOR_AB_Cout: Calcola XOR tra il risultato di XOR_A_B e OR_ANDs.
- AND_XOR_AB_OR_ANDs: Calcola AND tra XOR_AB e OR_ANDs.

---

## Log Agentico — Review/Fix Loop (sbt compile)

#### Iterazione 1 `FIXED_CONTINUE`

| Verifica | Risultato |
|---|---|
| LLM Reviewer | `ISSUES` |
| sbt compile | `FAIL` |
| Usa tipi MXFP4 | `NO` |
| Fix applicato | `True` |

**Issues rilevati:**
```
Il codice che hai fornito implementa correttamente un full adder 1-bit in Chisel. Tuttavia, per migliorare la leggibilità e la manutenzione del codice, posso suggerirti alcune modifiche:

1. **Nomi delle variabili**: Assicurati che i nomi delle variabili siano significativi e coerenti con le operazioni che eseguono.
2. **Documentazione**: Aggiungi commenti per descrivere il funzionamento di ciascu
```

#### Iterazione 2 `FIXED_CONTINUE`

| Verifica | Risultato |
|---|---|
| LLM Reviewer | `ISSUES` |
| sbt compile | `OK` |
| Usa tipi MXFP4 | `NO` |
| Fix applicato | `True` |

**Issues rilevati:**
```
Il codice che hai fornito implementa un generatore di reset a 100 MHz, ma non un full adder 1-bit. Per implementare un full adder 1-bit in Chisel3, dovresti creare una classe che rappresenti il circuito logico del full adder e definire i segnali di ingresso e uscita correttamente.

Ecco come potrebbe essere l'implementazione di un full adder 1-bit:

```scala
import chisel3._

class FullAdder exten
```

#### Iterazione 3 `FIXED_CONTINUE`

| Verifica | Risultato |
|---|---|
| LLM Reviewer | `ISSUES` |
| sbt compile | `FAIL` |
| Usa tipi MXFP4 | `NO` |
| Fix applicato | `True` |

**Issues rilevati:**
```
Il codice che hai fornito è una buona implementazione di un modulo Chisel per un Full Adder 1-bit. Tuttavia, ci sono alcune modifiche e miglioramenti che puoi apportare per assicurarti che il modulo funzioni correttamente e sia conforme alle specifiche originali.

Ecco le modifiche suggerite:

1. **Nomi delle porte**: Assicurati che i nomi delle porte siano coerenti con la documentazione fornita.

```

#### Iterazione 4 `FIXED_CONTINUE`

| Verifica | Risultato |
|---|---|
| LLM Reviewer | `ISSUES` |
| sbt compile | `OK` |
| Usa tipi MXFP4 | `NO` |
| Fix applicato | `True` |

**Issues rilevati:**
```
Il codice che hai fornito è una buona implementazione di un Full Adder 1-bit in Chisel3. Tuttavia, ci sono alcune piccole modifiche e miglioramenti che potrebbero essere fatte per assicurare la correttezza e la leggibilità del codice.

### Modifiche e Miglioramenti:

1. **Nomi delle Classi**: Assicurati che i nomi delle classi siano coerenti con le convenzioni di denominazione in Chisel3.
2. **Doc
```

#### Iterazione 5 `MAX_ITER_REACHED`

| Verifica | Risultato |
|---|---|
| LLM Reviewer | `ISSUES` |
| sbt compile | `OK` |
| Usa tipi MXFP4 | `NO` |
| Fix applicato | `False` |

---

## Log Agentico — Verifica funzionale (sbt test (Verilator))

#### Iterazione 1 `FIXED_CONTINUE`

| Verifica | Risultato |
|---|---|
| sbt test (Verilator) | `OK` |
| Usa tipi MXFP4 | `NO` |
| Fix applicato | `True` |

**Output:**
```
[info] welcome to sbt 1.10.7 (Ubuntu Java 25.0.3-ea)
[info] loading project definition from /mnt/c/Users/mattia/AppData/Local/Temp/chisel_check_FullAdder_154047483345/project
[info] loading settings for project chisel_check_fulladder_154047483345 from build.sbt...
[info] set current project to chisel_check_fulladder_154047483345 (in build file:/mnt/c/Users/mattia/AppData/Local/Temp/chisel_check_Fu
```

#### Iterazione 2 `FIXED_CONTINUE`

| Verifica | Risultato |
|---|---|
| sbt test (Verilator) | `OK` |
| Usa tipi MXFP4 | `NO` |
| Fix applicato | `True` |

**Output:**
```
[info] welcome to sbt 1.10.7 (Ubuntu Java 25.0.3-ea)
[info] loading project definition from /mnt/c/Users/mattia/AppData/Local/Temp/chisel_check_FullAdder_154135117635/project
[info] loading settings for project chisel_check_fulladder_154135117635 from build.sbt...
[info] set current project to chisel_check_fulladder_154135117635 (in build file:/mnt/c/Users/mattia/AppData/Local/Temp/chisel_check_Fu
```

#### Iterazione 3 `ESCAPE (modulo+testbench rigenerati)`

| Verifica | Risultato |
|---|---|
| sbt test (Verilator) | `OK` |
| Usa tipi MXFP4 | `NO` |
| Fix applicato | `False` |

#### Iterazione 4 `FIXED_CONTINUE`

| Verifica | Risultato |
|---|---|
| sbt test (Verilator) | `FAIL` |
| Usa tipi MXFP4 | `SI` |
| Fix applicato | `True` |

**Output:**
```
ase M3 => 3.U
[error]            ^
[error] /mnt/c/Users/mattia/AppData/Local/Temp/chisel_check_FullAdder_154322179839/src/main/scala/FullAdder.scala:44:28: not found: value M0
[error]   io.S := Mux(sum === 0.U, M0, Mux(sum === 1.U, M1, M2))
[error]                            ^
[error] /mnt/c/Users/mattia/AppData/Local/Temp/chisel_check_FullAdder_154322179839/src/main/scala/FullAdder.scala:44:49: n
```

#### Iterazione 5 `MAX_ITER_REACHED`

| Verifica | Risultato |
|---|---|
| sbt test (Verilator) | `FAIL` |
| Usa tipi MXFP4 | `SI` |
| Fix applicato | `False` |

---

## Formato MXFP4 (E2M1)

```
bit[3]   = segno  (0=+, 1=−)
bit[2:1] = esponente a 2 bit (bias=1)
bit[0]   = mantissa a 1 bit

Valore: (−1)^sign × 2^(exp−1) × (1 + mant×0.5)
Valori speciali: 0b0000=0, 0b0111=+6.0, 0b1111=−6.0
```

---

## File generati

| File | Descrizione |
|---|---|
| `src/main/scala/mxfp4/MXFP4.scala` | Bundle MXFP4 condiviso (sign/exp/mant + encode/decode) |
| `src/main/scala/<Modulo>.scala` | Modulo Chisel MXFP4 |
| `src/test/scala/<Modulo>Test.scala` | Testbench ChiselTest (backend Verilator) |
| `build.sbt` | Progetto SBT |

---

*Report generato automaticamente da `Tesi.py`*
