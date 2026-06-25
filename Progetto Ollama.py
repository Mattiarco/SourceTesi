import os
import sys
import json
import argparse
import datetime
import textwrap
import urllib.request
import urllib.error
from pathlib import Path

# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURAZIONE
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_OLLAMA_HOST = "http://localhost:11434"

# Modelli consigliati (ordinati per preferenza per task hardware/codice)
RECOMMENDED_MODELS = [
    "codellama",
    "deepseek-coder",
    "deepseek-r1",
    "llama3.1",
    "llama3",
    "mistral",
    "qwen2.5-coder",
    "phi4",
]

# ─────────────────────────────────────────────────────────────────────────────
# PROMPT DI SISTEMA
# ─────────────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """\
Sei un esperto di architetture hardware e linguaggi di descrizione hardware (HDL).
Il tuo compito è convertire codice Python che descrive circuiti digitali in codice
Chisel 3 (Scala) con supporto al formato numerico MXFP4 (Microscaling Floating-Point 4-bit).

════════════════ CONTESTO MXFP4 ════════════════
MXFP4 è uno standard OCP (Open Compute Project) per aritmetica a bassa precisione:
  • 4 bit totali: 1 segno, 2 esponente, 1 mantissa (formato E2M1)
  • Scala per blocco condivisa (shared exponent per tile di 32 elementi)
  • Usato in acceleratori ML per ridurre larghezza di banda e area del silicio
  • Riferimento: OCP MX Specification v1.0

════════════════ REGOLE DI OUTPUT ════════════════
Quando ricevi codice Python di un circuito, devi rispondere ESATTAMENTE con queste
cinque sezioni (incluse le intestazioni in maiuscolo):

## [ANALISI]
- Tipo di circuito (combinatorio / sequenziale)
- Elenco ingressi/uscite con bit-width
- Descrizione dell'operazione logica/aritmetica

## [MAPPATURA MXFP4]
- Quali segnali usano MXFP4 vs UInt/SInt standard e perché
- Layout dei bit MXFP4: [3]=segno, [2:1]=esponente, [0]=mantissa
- Eventuali approssimazioni introdotte

## [CODICE CHISEL]
```scala
// Codice Chisel 3 completo con:
// - import chisel3._, chisel3.util._
// - Bundle MXFP4 riutilizzabile
// - Modulo con lo stesso comportamento del Python
// - Commenti didattici per la tesi
```

## [TESTBENCH]
```scala
// ChiselTest / ScalaTest per verificare il comportamento
```

## [NOTE]
- Approssimazioni e trade-off di MXFP4 rispetto alla precisione piena
- Possibili varianti (E3M0, E1M2, INT4…)
- Suggerimenti per misurazioni di area/potenza

Rispondi SOLO con queste cinque sezioni, niente altro prima o dopo.
"""

# ─────────────────────────────────────────────────────────────────────────────
# COLORI TERMINALE
# ─────────────────────────────────────────────────────────────────────────────

RESET  = "\033[0m"
BOLD   = "\033[1m"
CYAN   = "\033[36m"
GREEN  = "\033[32m"
YELLOW = "\033[33m"
RED    = "\033[31m"
DIM    = "\033[2m"
BLUE   = "\033[34m"

def banner():
    print(f"""
{CYAN}{BOLD}╔═════════════════════════════════════════════════════════════════╗
            ║   Convertitore Python -> Chisel MXFP4 (Ollama — 100% locale)    ║
            ║   Workflow — Architetture Hardware                              ║
            ╚═════════════════════════════════════════════════════════════════╝{RESET}
""")

def step(n: int, msg: str):
    print(f"\n{CYAN}{BOLD}[STEP {n}]{RESET} {msg}")

def ok(msg: str):
    print(f"  {GREEN}✔{RESET}  {msg}")

def warn(msg: str):
    print(f"  {YELLOW}⚠{RESET}  {msg}")

def err(msg: str):
    print(f"  {RED}✘{RESET}  {msg}", file=sys.stderr)

def info(msg: str):
    print(f"  {BLUE}ℹ{RESET}  {msg}")

def hr():
    print(f"{DIM}{'─'*68}{RESET}")

def ollama_get(url: str, timeout: int = 5):
    """GET su Ollama, restituisce il JSON o None."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None

def ollama_post(url: str, payload: dict, timeout: int = 300):
    """POST su Ollama, restituisce il JSON o lancia eccezione."""
    body = json.dumps(payload).encode("utf-8")
    req  = urllib.request.Request(url, data=body,
                                  headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))

# Verifica Ollama e lista modelli

def check_ollama(host: str) -> list[str]:
    """Controlla che Ollama sia up e ritorna i modelli disponibili."""
    step(1, f"Verifica Ollama  ({host})")

    data = ollama_get(f"{host}/api/tags")
    if data is None:
        err(f"Ollama non raggiungibile su {host}")
        sys.exit(1)

    models = [m["name"] for m in data.get("models", [])]
    if not models:
        err("Nessun modello installato in Ollama.")
        sys.exit(1)

    ok(f"Ollama online  —  {len(models)} modello/i trovato/i")
    return models


# Scelta del modello

def choose_model(available: list[str], model_arg: str | None) -> str:
    step(2, "Selezione del modello Ollama")

    if model_arg:
        matches = [m for m in available if m.startswith(model_arg)]
        if matches:
            ok(f"Modello scelto: {BOLD}{matches[0]}{RESET}")
            return matches[0]
        else:
            warn(f"Modello '{model_arg}' non trovato. Scegli tra quelli disponibili.")

    ordered = []
    for rec in RECOMMENDED_MODELS:
        found = [m for m in available if m.startswith(rec)]
        ordered.extend(found)
    others = [m for m in available if m not in ordered]
    ordered.extend(others)

    print(f"\n  {BOLD}Modelli disponibili:{RESET}")
    for i, name in enumerate(ordered, 1):
        tag = ""
        if any(name.startswith(r) for r in ["codellama", "deepseek-coder", "qwen2.5-coder"]):
            tag = f"  {GREEN}← consigliato per hardware/codice{RESET}"
        print(f"    {BOLD}{i:2}.{RESET} {name}{tag}")

    print()
    while True:
        raw = input(f"  {BOLD}Scegli numero o nome modello [1]:{RESET} ").strip()
        if raw == "":
            chosen = ordered[0]
            break
        if raw.isdigit():
            idx = int(raw) - 1
            if 0 <= idx < len(ordered):
                chosen = ordered[idx]
                break
            warn("Numero non valido.")
        elif raw in available:
            chosen = raw
            break
        else:
            # Cerca per prefisso
            matches = [m for m in available if m.startswith(raw)]
            if matches:
                chosen = matches[0]
                break
            warn(f"'{raw}' non trovato. Riprova.")

    ok(f"Modello selezionato: {BOLD}{chosen}{RESET}")
    return chosen

# Acquisizione file Python

def acquire_file(file_arg: str | None) -> tuple[Path, str]:
    step(3, "Acquisizione del file Python sorgente")

    if file_arg:
        path = Path(file_arg)
    else:
        print(f"""
  Inserisci il percorso del file Python che descrive il tuo circuito.
  Esempi supportati:
    • Full Adder (1-bit, N-bit ripple carry, carry-lookahead)
    • Moltiplicatore intero / in virgola mobile
    • ALU semplificata, shift register, comparatore, mux…
""")
        raw = input(f"  {BOLD}Percorso file Python:{RESET} ").strip()
        path = Path(raw)

    if not path.exists():
        err(f"File non trovato: {path}")
        sys.exit(1)
    if path.suffix.lower() != ".py":
        warn("Il file non ha estensione .py — procedo comunque.")

    source = path.read_text(encoding="utf-8")
    lines  = source.count("\n") + 1
    ok(f"File caricato: {BOLD}{path.name}{RESET}  ({len(source)} caratteri, {lines} righe)")
    return path, source

# Costruzione del prompt

def build_user_prompt(source: str, filename: str) -> str:
    return f"""\
Converti il seguente file Python ({filename}) in codice Chisel 3 con tipi MXFP4.

```python
{source}
```

Rispondi ESATTAMENTE con le cinque sezioni richieste:
## [ANALISI], ## [MAPPATURA MXFP4], ## [CODICE CHISEL], ## [TESTBENCH], ## [NOTE PER LA TESI]
"""

# Inferenza con Ollama

def run_inference(host: str, model: str, user_prompt: str, verbose: bool) -> str:
    step(5, f"Inferenza locale con {BOLD}{model}{RESET}")

    payload = {
        "model":  model,
        "stream": False,
        "options": {
            "temperature": 0.2,     
            "num_predict": 4096,   
        },
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": user_prompt},
        ],
    }

    if verbose:
        print(f"  {DIM}Endpoint: {host}/api/chat{RESET}")
        print(f"  {DIM}Temperatura: {payload['options']['temperature']}  "
              f"| max tokens: {payload['options']['num_predict']}{RESET}")

    info("Elaborazione in corso (può richiedere qualche minuto su CPU)…")

    try:
        start  = datetime.datetime.now()
        data   = ollama_post(f"{host}/api/chat", payload, timeout=600)
        elapsed = (datetime.datetime.now() - start).total_seconds()
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        err(f"HTTP {e.code}: {body}")
        sys.exit(1)
    except urllib.error.URLError as e:
        err(f"Connessione persa: {e.reason}")
        sys.exit(1)
    except TimeoutError:
        err("Timeout: il modello ha impiegato troppo. Prova un modello più piccolo.")
        sys.exit(1)

    text = data.get("message", {}).get("content", "")

    eval_count  = data.get("eval_count", "?")
    prompt_eval = data.get("prompt_eval_count", "?")
    ok(f"Inferenza completata in {elapsed:.1f}s  —  "
       f"prompt tokens: {prompt_eval}  |  output tokens: {eval_count}")

    if not text.strip():
        err("Il modello ha restituito una risposta vuota.")
        sys.exit(1)

    return text

# Parsing della risposta

def extract_section(text: str, tag: str) -> str:
    import re
    pattern = rf"## \[{re.escape(tag)}\](.*?)(?=## \[|\Z)"
    m = re.search(pattern, text, re.DOTALL | re.IGNORECASE)
    return m.group(1).strip() if m else ""

def extract_code_block(text: str, lang: str = "scala") -> str:
    import re
    pattern = rf"```{lang}(.*?)```"
    m = re.search(pattern, text, re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return text.strip()

def parse_response(raw: str) -> dict:
    step(6, "Parsing e validazione della risposta")

    sections = {
        "analisi":   extract_section(raw, "ANALISI"),
        "mappatura": extract_section(raw, "MAPPATURA MXFP4"),
        "codice":    extract_code_block(extract_section(raw, "CODICE CHISEL")),
        "testbench": extract_code_block(extract_section(raw, "TESTBENCH")),
        "note":      extract_section(raw, "NOTE"),
    }

    for name, val in sections.items():
        if val:
            ok(f"Sezione [{name}] estratta  ({len(val)} caratteri)")
        else:
            warn(f"Sezione [{name}] mancante — controlla raw_response.txt")

    return sections

# STEP 7 — Salvataggio output

def save_outputs(source_path: Path, model: str,
                 sections: dict, raw_response: str) -> Path:
    step(7, "Salvataggio dei file di output")

    ts      = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    stem    = source_path.stem
    # nome sicuro per il modello (rimuove ":" e tag di versione per il path)
    msafe   = model.replace(":", "_").replace("/", "_")
    out_dir = Path(f"chisel_output_{stem}_{msafe}_{ts}")
    out_dir.mkdir(exist_ok=True)

    header = (
        f"// ══════════════════════════════════════════════════════════\n"
        f"//  Generato da: python_to_chisel_mxfp4_ollama.py\n"
        f"//  Sorgente:     {source_path.name}\n"
        f"//  Modello:      {model}\n"
        f"//  Data:         {datetime.datetime.now().isoformat(timespec='seconds')}\n"
        f"// ══════════════════════════════════════════════════════════\n\n"
    )

    # ── Codice Chisel ──
    chisel_file = out_dir / f"{stem}_mxfp4.scala"
    chisel_file.write_text(header + sections["codice"], encoding="utf-8")
    ok(f"Codice Chisel  → {chisel_file}")

    # ── Testbench ──
    tb_file = out_dir / f"{stem}_tb.scala"
    tb_file.write_text(header + sections["testbench"], encoding="utf-8")
    ok(f"Testbench      → {tb_file}")

    # ── Report Markdown (per la tesi) ──
    report_file = out_dir / f"report_{stem}.md"
    report_file.write_text(f"""\
# Report: Conversione Python → Chisel MXFP4

| Campo | Valore |
|---|---|
| **Sorgente** | `{source_path.name}` |
| **Modello Ollama** | `{model}` |
| **Data** | {datetime.datetime.now().isoformat(timespec='seconds')} |

---

## Analisi del Circuito

{sections['analisi']}

---

## Mappatura su MXFP4

{sections['mappatura']}

---

## Note

{sections['note']}

---

*Report generato automaticamente — python_to_chisel_mxfp4_ollama.py*
""", encoding="utf-8")
    ok(f"Report Markdown→ {report_file}")

    # ── Risposta grezza (debug) ──
    log_file = out_dir / "raw_response.txt"
    log_file.write_text(raw_response, encoding="utf-8")
    ok(f"Raw log        → {log_file}")

    # ── build.sbt ──
    sbt_file = out_dir / "build.sbt"
    sbt_file.write_text("""\
scalaVersion := "2.13.12"

libraryDependencies ++= Seq(
  "org.chipsalliance" %% "chisel"      % "6.5.0",
  "edu.berkeley.cs"   %% "chiseltest"  % "6.0.0" % "test",
)

addCompilerPlugin(
  "org.chipsalliance" % "chisel-plugin" % "6.5.0" cross CrossVersion.full
)

scalacOptions ++= Seq(
  "-language:reflectiveCalls",
  "-deprecation",
  "-feature",
  "-Xcheckinit",
)
""", encoding="utf-8")
    ok(f"build.sbt      → {sbt_file}")

    readme = out_dir / "README.md"
    readme.write_text(f"""\
# {stem} — Chisel MXFP4

Generato automaticamente da `python_to_chisel_mxfp4_ollama.py` con modello **{model}**.

## File

| File | Descrizione |
|---|---|
| `{stem}_mxfp4.scala` | Modulo Chisel principale con Bundle MXFP4 |
| `{stem}_tb.scala` | Testbench ChiselTest |
| `report_{stem}.md` | Analisi e note per la tesi |
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
""", encoding="utf-8")
    ok(f"README         → {readme}")

    return out_dir

# MAIN

def main():
    banner()

    parser = argparse.ArgumentParser(
        description="Converte un circuito Python in Chisel MXFP4 via Ollama (locale)",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--file",    "-f",  help="Percorso del file Python da convertire")
    parser.add_argument("--model",   "-m",  help="Nome del modello Ollama (es. codellama, llama3.1)")
    parser.add_argument("--host",           default=DEFAULT_OLLAMA_HOST,
                                            help=f"URL Ollama (default: {DEFAULT_OLLAMA_HOST})")
    parser.add_argument("--verbose", "-v",  action="store_true", help="Output dettagliato")
    args = parser.parse_args()

    available_models = check_ollama(args.host)

    model = choose_model(available_models, args.model)

    source_path, source_code = acquire_file(args.file)

    step(4, "Costruzione del prompt")
    user_prompt = build_user_prompt(source_code, source_path.name)
    ok(f"Prompt costruito ({len(user_prompt)} caratteri)")
    if args.verbose:
        print(f"\n{DIM}{'─'*60}")
        print(textwrap.indent(user_prompt[:600] + "…", "  "))
        print(f"{'─'*60}{RESET}")

    raw_response = run_inference(args.host, model, user_prompt, args.verbose)

    if args.verbose:
        print(f"\n{DIM}{'─'*60}")
        print(textwrap.indent(raw_response[:800] + "…", "  "))
        print(f"{'─'*60}{RESET}")

    sections = parse_response(raw_response)

    out_dir = save_outputs(source_path, model, sections, raw_response)

    # ── RIEPILOGO ──
    hr()
    stem = source_path.stem
    msafe = model.replace(":", "_").replace("/", "_")
    print(f"""
{GREEN}{BOLD}Conversione completata.{RESET}""")
    hr()


if __name__ == "__main__":
    main()