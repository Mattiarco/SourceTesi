# Meta-HDL FP4 Unit Report — MXFP4

Data generazione: 2026-06-03T07:51:46
Input file: `Test1.py`

## 1. Sintesi

Il framework ha analizzato il codice Python, generato una IR con 11 operazioni, costruito il DFG, stimato una latenza critica di 5 cicli e prodotto un backend PyMTL3 prototipale.

## 2. Matrice requisiti-risultati

| Requisito | Stato | Nota |
|---|---|---|
| Soluzione agentica Meta-HDL | Soddisfatto | CrewAI opzionale + analisi deterministica sempre disponibile |
| Parsing codice Python | Soddisfatto | AST Python |
| Intermediate Representation | Soddisfatto | IR con operazioni, input, output e latenze |
| Data Flow Graph | Soddisfatto | NetworkX DiGraph |
| Analisi HLS | Soddisfatto | Critical path, ASAP scheduling, stima risorse |
| Generazione Meta-HDL | Soddisfatto | Backend PyMTL3 prototipale |
| Supporto MXFP4/NVFP4 | Parziale ma migliorato | Golden model FP4 software + datapath a 4 bit |
| Validazione automatica | Soddisfatto | Test casuali e confronto float vs FP4 |
| Integrazione RISC-V | Futuro sviluppo | Non implementata nel prototipo attuale |

## 3. Codice Python sorgente

```python
def kernel(a, b, c, d):
    x = a * 1
    y = b * 2
    z = c * 3
    w = d * 4
    return x + y + z + w
```

## 4. Intermediate Representation

```json
[
  {
    "id": "op_1",
    "type": "const",
    "operation": "CONST",
    "inputs": [],
    "output": "op_1",
    "value": 1,
    "latency": 0
  },
  {
    "id": "op_2",
    "type": "binary_op",
    "operation": "Mult",
    "inputs": [
      "a",
      "op_1"
    ],
    "output": "op_2",
    "latency": 2
  },
  {
    "id": "op_3",
    "type": "const",
    "operation": "CONST",
    "inputs": [],
    "output": "op_3",
    "value": 2,
    "latency": 0
  },
  {
    "id": "op_4",
    "type": "binary_op",
    "operation": "Mult",
    "inputs": [
      "b",
      "op_3"
    ],
    "output": "op_4",
    "latency": 2
  },
  {
    "id": "op_5",
    "type": "const",
    "operation": "CONST",
    "inputs": [],
    "output": "op_5",
    "value": 3,
    "latency": 0
  },
  {
    "id": "op_6",
    "type": "binary_op",
    "operation": "Mult",
    "inputs": [
      "c",
      "op_5"
    ],
    "output": "op_6",
    "latency": 2
  },
  {
    "id": "op_7",
    "type": "const",
    "operation": "CONST",
    "inputs": [],
    "output": "op_7",
    "value": 4,
    "latency": 0
  },
  {
    "id": "op_8",
    "type": "binary_op",
    "operation": "Mult",
    "inputs": [
      "d",
      "op_7"
    ],
    "output": "op_8",
    "latency": 2
  },
  {
    "id": "op_9",
    "type": "binary_op",
    "operation": "Add",
    "inputs": [
      "op_2",
      "op_4"
    ],
    "output": "op_9",
    "latency": 1
  },
  {
    "id": "op_10",
    "type": "binary_op",
    "operation": "Add",
    "inputs": [
      "op_9",
      "op_6"
    ],
    "output": "op_10",
    "latency": 1
  },
  {
    "id": "op_11",
    "type": "binary_op",
    "operation": "Add",
    "inputs": [
      "op_10",
      "op_8"
    ],
    "output": "op_11",
    "latency": 1
  }
]
```

## 5. Variable Map

```json
{
  "x": "op_2",
  "y": "op_4",
  "z": "op_6",
  "w": "op_8"
}
```

Return value: `op_11`

## 6. Scheduling e Pipeline

### Schedule ASAP

```json
{
  "d": 0,
  "a": 0,
  "b": 0,
  "c": 0,
  "op_1": 0,
  "op_3": 0,
  "op_5": 0,
  "op_7": 0,
  "op_2": 2,
  "op_4": 2,
  "op_6": 2,
  "op_8": 2,
  "op_9": 3,
  "op_10": 4,
  "op_11": 5
}
```

### Pipeline stages

```json
{
  "0": [
    "d",
    "a",
    "b",
    "c",
    "op_1",
    "op_3",
    "op_5",
    "op_7"
  ],
  "2": [
    "op_2",
    "op_4",
    "op_6",
    "op_8"
  ],
  "3": [
    "op_9"
  ],
  "4": [
    "op_10"
  ],
  "5": [
    "op_11"
  ]
}
```

### Critical path

```json
{
  "d": 0,
  "a": 0,
  "b": 0,
  "c": 0,
  "op_1": 0,
  "op_3": 0,
  "op_5": 0,
  "op_7": 0,
  "op_2": 2,
  "op_4": 2,
  "op_6": 2,
  "op_8": 2,
  "op_9": 3,
  "op_10": 4,
  "op_11": 5
}
```

## 7. Stima risorse

```json
{
  "total_operations": {
    "adders": 3,
    "subtractors": 0,
    "multipliers": 4,
    "dividers": 0,
    "comparators": 0,
    "const_units": 4,
    "registers_estimated": 11
  },
  "peak_parallel_resources": {
    "peak_adders": 1,
    "peak_subtractors": 0,
    "peak_multipliers": 4,
    "peak_dividers": 0,
    "peak_comparators": 0
  },
  "per_stage": {
    "0": {
      "adders": 0,
      "subtractors": 0,
      "multipliers": 0,
      "dividers": 0,
      "comparators": 0
    },
    "2": {
      "adders": 0,
      "subtractors": 0,
      "multipliers": 4,
      "dividers": 0,
      "comparators": 0
    },
    "3": {
      "adders": 1,
      "subtractors": 0,
      "multipliers": 0,
      "dividers": 0,
      "comparators": 0
    },
    "4": {
      "adders": 1,
      "subtractors": 0,
      "multipliers": 0,
      "dividers": 0,
      "comparators": 0
    },
    "5": {
      "adders": 1,
      "subtractors": 0,
      "multipliers": 0,
      "dividers": 0,
      "comparators": 0
    }
  }
}
```

## 8. Validazione FP4

```json
{
  "fp_format": "MXFP4",
  "n_tests": 32,
  "mean_absolute_error": 5.515743312751415,
  "max_absolute_error": 15.28563209773774,
  "mean_relative_error": 0.5513623707453361,
  "max_relative_error": 2.2847418485056137
}
```

### Primi 5 test

```json
[
  {
    "inputs": {
      "a": 1.11541438766307,
      "b": -3.7999139582186645,
      "c": -1.799765453047046,
      "d": -2.214314094809418
    },
    "float_output": -20.74096626715307,
    "fp4_output": -12.0,
    "absolute_error": 8.74096626715307,
    "relative_error": 0.4214348624911693
  },
  {
    "inputs": {
      "a": 1.8917697133120992,
      "b": 1.4135958993832904,
      "c": 3.1374365416387633,
      "d": -3.304489338964671
    },
    "float_output": 0.9133137811362868,
    "fp4_output": 3.0,
    "absolute_error": 2.086686218863713,
    "relative_error": 2.2847418485056137
  },
  {
    "inputs": {
      "a": -0.6246254425178366,
      "b": -3.7616222444954373,
      "c": -2.250896201571173,
      "d": 0.0428423048268991
    },
    "float_output": -14.729189316914635,
    "fp4_output": -6.0,
    "absolute_error": 8.729189316914635,
    "relative_error": 0.5926456051656288
  },
  {
    "inputs": {
      "a": -3.787712242529091,
      "b": -2.409298794506812,
      "c": 1.1990755022361856,
      "d": 0.3595318448257334
    },
    "float_output": -3.5709559455312245,
    "fp4_output": 0.1875,
    "absolute_error": 3.7584559455312245,
    "relative_error": 1.0525069482254843
  },
  {
    "inputs": {
      "a": -2.2364750236744264,
      "b": 0.7141254710072698,
      "c": 2.475443653422613,
      "d": -3.948009922575512
    },
    "float_output": -9.173932811694094,
    "fp4_output": -1.5,
    "absolute_error": 7.673932811694094,
    "relative_error": 0.8364932432332151
  }
]
```

## 9. Codice PyMTL3 generato

```python
from pymtl3 import *

# Generated by Agentic Meta-HDL FP4 Compiler
# Format: MXFP4
# Datapath width: 4 bit
# Note: this is a prototype Meta-HDL datapath.
# Full FP4 numerical behavior is validated by the Python golden model.

class MXFP4ArithUnit(Component):
    def construct(s):
        s.in0 = InPort(4)
        s.in1 = InPort(4)
        s.in2 = InPort(4)
        s.in3 = InPort(4)
        s.out = OutPort(4)

        s.op_1 = Wire(4)
        s.op_2 = Wire(4)
        s.op_3 = Wire(4)
        s.op_4 = Wire(4)
        s.op_5 = Wire(4)
        s.op_6 = Wire(4)
        s.op_7 = Wire(4)
        s.op_8 = Wire(4)
        s.op_9 = Wire(4)
        s.op_10 = Wire(4)
        s.op_11 = Wire(4)

        @update
        def compute():
            s.op_1 @= 1
            s.op_2 @= (s.in0 * s.op_1) & 15
            s.op_3 @= 2
            s.op_4 @= (s.in1 * s.op_3) & 15
            s.op_5 @= 3
            s.op_6 @= (s.in2 * s.op_5) & 15
            s.op_7 @= 4
            s.op_8 @= (s.in3 * s.op_7) & 15
            s.op_9 @= (s.op_2 + s.op_4) & 15
            s.op_10 @= (s.op_9 + s.op_6) & 15
            s.op_11 @= (s.op_10 + s.op_8) & 15

            s.out @= s.op_11

# Pipeline note:
# Scheduling information is computed in the report.
# Automatic insertion of pipeline registers is a future backend extension.
```

## 10. Nodi non supportati o avvisi

Nessun nodo non supportato rilevato.

## 11. Analisi architetturale

# Architectural Review

Nota: Analisi deterministica usata. CrewAI non abilitato.

Il progetto genera una unità prototipale per formato MXFP4.
La IR contiene 11 operazioni.
La latenza stimata del critical path è pari a 5 cicli.

## Parallelismo individuato
- Picco adders paralleli: 1
- Picco subtractors paralleli: 0
- Picco multipliers paralleli: 4
- Picco dividers paralleli: 0

## Validazione numerica FP4
- Test eseguiti: 32
- Errore assoluto medio: 5.515743
- Errore assoluto massimo: 15.285632
- Errore relativo medio: 0.551362

## Limite tecnico dichiarato
Il backend PyMTL3 genera una rappresentazione a 4 bit del datapath, ma non implementa ancora una FPU completa con normalizzazione, rounding, saturazione, gestione dei casi speciali e scaling hardware completo.

## Miglioramento principale rispetto alla versione precedente
La presenza del golden model e dei test automatici rende il progetto più solido, perché permette di confrontare il comportamento numerico atteso con quello quantizzato.

## 12. Limiti dichiarati

- Il backend PyMTL3 è prototipale e usa operazioni a 4 bit, ma non implementa ancora una FPU MXFP4/NVFP4 completa.
- Il golden model FP4 è una modellazione software semplificata, utile per validazione sperimentale e confronto numerico.
- L'integrazione RISC-V non è implementata nel codice attuale, ma il modulo generato può essere usato come base per una futura interfaccia di coprocessore.