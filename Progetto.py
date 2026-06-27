import os
import sys
import json
import re
import shutil
import subprocess
import argparse
import datetime
import textwrap
import urllib.request
import urllib.error
from pathlib import Path

# Configurazione di ollama.
DEFAULT_HOST   = "http://localhost:11434"
MAX_FIX_ITER   = 10    # Metto un limite di interazioni per evitare loop infiniti.
OLLAMA_TIMEOUT = 600   # Metto un timeout per le chiamate ad ollama. 

# Modelli consigliati per il workflow. Do la possibiilità di scegliere il modello per avere più versatilità e per poter effettuare test con modelli diversi.
RECOMMENDED_MODELS = [
    "codellama",
    "deepseek-coder",
    "deepseek-r1",
    "qwen2.5-coder",
    "llama3.1",
    "llama3",
    "mistral",
    "phi4",
]

# Prepraro i prompt per i vari agenti. Questi prompt sono progettati per guidare il comportamento degli agenti LLM in modo coerente con il loro ruolo specifico nel workflow.
SYSTEM_PLANNER = """\
Sei un esperto di architetture hardware digitale e aritmetica a bassa precisione.
Ricevi una specifica testuale di un'unità aritmetica da implementare in Chisel 3
con formato numerico MXFP4.

CONTESTO MXFP4:
  • Formato E2M1: 4 bit totali
      bit[3]   = segno (0=positivo, 1=negativo)
      bit[2:1] = esponente (2 bit, bias=1)
      bit[0]   = mantissa (1 bit)
  • Shared exponent per blocchi di 32 elementi (OCP MX Specification v1.0)
  • Usato in acceleratori ML per ridurre area e banda

Il tuo compito è produrre un piano di implementazione strutturato in JSON.
Rispondi SOLO con il JSON valido. Nessun testo prima o dopo. Nessun markdown.

Schema JSON richiesto:
{
  "nome_modulo": "NomeInPascalCase",
  "tipo": "combinatorio|sequenziale",
  "descrizione": "descrizione funzionale completa",
  "ingressi": [
    {"nome": "a",   "tipo": "MXFP4|UInt|SInt|Bool", "bit": 4, "descrizione": "..."}
  ],
  "uscite": [
    {"nome": "sum", "tipo": "MXFP4|UInt|SInt|Bool", "bit": 4, "descrizione": "..."}
  ],
  "segnali_interni": [
    {"nome": "carry", "tipo": "UInt", "bit": 1, "descrizione": "..."}
  ],
  "passi_algoritmo": [
    "1. Estrai segno, esponente e mantissa dagli ingressi",
    "2. Allinea gli esponenti",
    "..."
  ],
  "bundle_mxfp4_necessario": true,
  "note_mxfp4": "descrizione delle scelte architetturali MXFP4"
}
"""

SYSTEM_CODER = """\
Sei un esperto di Chisel 3 (Scala) e di formati numerici a bassa precisione.
Devi implementare un'unità aritmetica hardware in Chisel 3 con supporto MXFP4.

REGOLE OBBLIGATORIE:
1. Prima riga: import chisel3._
   Seconda riga: import chisel3.util._
2. Definisci SEMPRE il Bundle MXFP4:
     class MXFP4 extends Bundle {
       val sign = Bool()
       val exp  = UInt(2.W)
       val mant = UInt(1.W)
     }
3. Ogni modulo Chisel estende Module e ha un val io = IO(new Bundle { ... })
4. Usa := per assegnazioni, non =
5. I segnali Wire si dichiarano con: val nome = Wire(UInt(N.W))
6. Usa nomi inglesi snake_case per segnali e moduli PascalCase
7. Commenta ogni blocco logico in italiano (utile per la tesi)
8. Nessuna libreria esterna oltre a chisel3 standard
9. Il codice deve essere COMPLETO e COMPILABILE

Rispondi con SOLO il codice Scala/Chisel.
Non usare markdown (no ```), nessun testo prima o dopo il codice.
"""

SYSTEM_REVIEWER = """\
Sei un revisore esperto di codice Chisel 3 per unità aritmetiche MXFP4.
Ricevi del codice Chisel 3 e devi identificare errori precisi.

CHECKLIST DA VERIFICARE:
  [ ] Import: chisel3._ e chisel3.util._ presenti
  [ ] Bundle MXFP4 definito con: sign (Bool), exp (UInt(2.W)), mant (UInt(1.W))
  [ ] Ogni modulo estende Module
  [ ] IO Bundle dichiarato con val io = IO(new Bundle { ... })
  [ ] Assegnazioni usano := non =
  [ ] Wire dichiarati prima dell'uso
  [ ] Parentesi graffe bilanciate
  [ ] Nessuna sintassi Scala non supportata in Chisel 3
  [ ] Logica MXFP4 corretta (estrazione bit, allineamento esponenti, ecc.)
  [ ] Nessun import o riferimento a librerie inesistenti

Se il codice supera tutti i controlli, rispondi ESATTAMENTE (solo questo):
PASS

Se ci sono problemi, rispondi ESATTAMENTE in questo formato:
ISSUES
- [riga o blocco] descrizione problema 1
- [riga o blocco] descrizione problema 2
...
"""

SYSTEM_FIXER = """\
Sei un esperto Chisel 3 che corregge codice hardware con errori.
Ricevi il codice difettoso e una lista di issues da risolvere.

REGOLE:
1. Correggi TUTTI gli errori elencati senza eccezioni
2. Non introdurre nuovi errori
3. Mantieni la stessa logica funzionale dell'originale
4. Il codice output deve essere completo (non troncare)
5. Rispetta le stesse regole del Coder:
   - import chisel3._ e chisel3.util._
   - Bundle MXFP4 con sign/exp/mant
   - := per assegnazioni
   - Commenti in italiano

non scrivere mai codice nella tua risposta.
Scrivi SOLO "PASS" oppure "ISSUES" seguito dalla lista.
Nessun markdown, nessun testo aggiuntivo.
"""

SYSTEM_TESTER = """\
Sei un esperto di ChiselTest e ScalaTest per la verifica di circuiti hardware.
Ricevi un modulo Chisel MXFP4 e devi generare un testbench completo.

STRUTTURA OBBLIGATORIA:
  import chisel3._
  import chiseltest._
  import org.scalatest.flatspec.AnyFlatSpec

  class NomeModuloTest extends AnyFlatSpec with ChiselScalatestTester {
    behavior of "NomeModulo"

    it should "descrizione test" in {
      test(new NomeModulo) { dut =>
        // test cases
      }
    }
  }

CASI DA TESTARE:
  • Caso base (valori tipici)
  • Zero (0x0)
  • Valore massimo rappresentabile in MXFP4
  • Valori negativi (se il modulo li supporta)
  • Overflow/underflow
  • Simmetria (a op b == b op a per operazioni commutative)

Ricorda: in MXFP4 E2M1 il valore massimo è 0b0111 = +6.0

Rispondi con SOLO il codice Scala del testbench.
Nessun markdown, nessun testo aggiuntivo.
"""

# Set di colori per i messaggi in console.
RESET  = "\033[0m"
BOLD   = "\033[1m"
CYAN   = "\033[36m"
GREEN  = "\033[32m"
YELLOW = "\033[33m"
RED    = "\033[31m"
DIM    = "\033[2m"
BLUE   = "\033[34m"
MAGENTA = "\033[35m"

# Messaggi di log e banner per l'interfaccia utente.
def banner():
    print(f"""\n{CYAN}{BOLD}\
╔══════════════════════════════════════════════════════════════════╗
║  Agentic Chisel MXFP4 Generator  (Ollama — 100% locale)          ║
║  Planner → Coder → [Reviewer ⟷ Fixer]* → Tester → Output        ║
╚══════════════════════════════════════════════════════════════════╝{RESET}""")

def agent_step(agent_name: str, desc: str):
    print(f"\n{MAGENTA}{BOLD}[{agent_name.upper()}]{RESET} {desc}")

def step(n: int, msg: str):
    print(f"\n{CYAN}{BOLD}[STEP {n}]{RESET} {msg}")

def ok(msg: str):
    print(f"  {GREEN}OK{RESET}  {msg}")

def warn(msg: str):
    print(f"  {YELLOW}WRN{RESET}  {msg}")

def err(msg: str):
    print(f"  {RED}KO{RESET}  {msg}", file=sys.stderr)

def info(msg: str):
    print(f"  {BLUE}Info{RESET}  {msg}")

def hr():
    print(f"{DIM}{'─' * 68}{RESET}")


# Api per ollama. Ollama_get() ritorna il JSON o None se non raggiungibile. Ollama_chat() gestisce la chat multi-turn con history e system prompt.
def ollama_get(url: str, timeout: int = 5):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def ollama_chat(host: str, model: str, system_prompt: str,
                history: list[dict], timeout: int = OLLAMA_TIMEOUT) -> str:
    payload = {
        "model":   model,
        "stream":  False,
        "system":  system_prompt,
        "options": {
            "temperature": 0.1,     
            "num_predict": 4096,
        },
        "messages": history,
    }
    body = json.dumps(payload).encode("utf-8")
    req  = urllib.request.Request(
        f"{host}/api/chat", data=body,
        headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body_err = e.read().decode("utf-8", errors="replace")
        err(f"HTTP {e.code}: {body_err}")
        sys.exit(1)
    except urllib.error.URLError as e:
        err(f"Connessione persa: {e.reason}")
        sys.exit(1)

    return data.get("message", {}).get("content", "").strip()

# Classe Agent: rappresenta un agente LLM con memoria conversazionale. Ogni agente mantiene la propria history separata, quindi Fixer "ricorda" le iterazioni precedenti e non ripete gli stessi errori. 
# Il system prompt è fisso e descrive il ruolo dell'agente.
class Agent:
    def __init__(self, name: str, system_prompt: str, host: str, model: str):
        self.name          = name
        self.system_prompt = system_prompt
        self.host          = host
        self.model         = model
        self.history: list[dict] = []   # memoria conversazionale

# Invia un messaggio all'agente e ottiene la risposta. La risposta viene aggiunta alla history per il contesto multi-turn.
    def run(self, user_message: str) -> str:
        self.history.append({"role": "user", "content": user_message})
        info(f"Agente {BOLD}{self.name}{RESET} in elaborazione…")

        t_start  = datetime.datetime.now()
        response = ollama_chat(
            self.host, self.model, self.system_prompt, self.history
        )
        elapsed  = (datetime.datetime.now() - t_start).total_seconds()

        self.history.append({"role": "assistant", "content": response})
        ok(f"{self.name} risposta in {elapsed:.1f}s  "
           f"({len(response)} caratteri)")
        return response

    def reset_history(self):
        self.history = []


# Classe SbtCompiler: gestisce la compilazione reale del codice Chisel generato. Scrive il codice in una directory temporanea, esegue 'sbt compile' e cattura eventuali errori. 
class SbtCompiler:
    def __init__(self):
        self.available = shutil.which("sbt") is not None or \
                 shutil.which("sbt.bat") is not None

# Compila il codice Chisel in una directory temporanea. Ritorna (successo, output). Se sbt non è disponibile, ritorna True con un messaggio di avviso.
    def compile(self, chisel_code: str, stem: str) -> tuple[bool, str]:
        if not self.available:
            return True, "sbt non trovato — compilazione reale saltata"

        tmp = Path(f"/tmp/chisel_check_{stem}_{datetime.datetime.now().strftime('%H%M%S%f')}")
        tmp.mkdir(parents=True, exist_ok=True)

        # Progetto SBT minimale
        (tmp / "build.sbt").write_text(
            'scalaVersion := "2.13.12"\n'
            'libraryDependencies += "org.chipsalliance" %% "chisel" % "6.5.0"\n'
            'addCompilerPlugin('
            '"org.chipsalliance" % "chisel-plugin" % "6.5.0" cross CrossVersion.full)\n',
            encoding="utf-8"
        )
        src = tmp / "src" / "main" / "scala"
        src.mkdir(parents=True, exist_ok=True)
        (src / f"{stem}.scala").write_text(chisel_code, encoding="utf-8")

        try:
            result = subprocess.run(
                ["sbt", "compile"],
                cwd=tmp, capture_output=True, text=True, timeout=180, shell=True
            )
            output  = (result.stdout + result.stderr).strip()
            success = result.returncode == 0
            return success, output

        except subprocess.TimeoutExpired:
            return False, "sbt compile timeout (>180s)"
        except Exception as e:
            return False, str(e)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

# Input della specifica: può essere fornita come argomento CLI (--spec), come file Python (--file) o in modalità interattiva. 
# La funzione get_specification gestisce queste tre modalità e ritorna la specifica testuale da usare nel workflow.
def get_specification(file_arg: str | None, spec_arg: str | None) -> str:
    step(0, "Acquisizione della specifica")

    if spec_arg:
        ok(f"Specifica da argomento CLI ({len(spec_arg)} caratteri)")
        return spec_arg

    if file_arg:
        path = Path(file_arg)
        if not path.exists():
            err(f"File non trovato: {path}")
            sys.exit(1)
        source = path.read_text(encoding="utf-8")
        ok(f"File caricato come contesto: {path.name}  "
           f"({source.count(chr(10))+1} righe)")
        return (
            "Implementa in Chisel 3 con formato MXFP4 (E2M1, 4 bit) "
            "un'unità aritmetica hardware funzionalmente equivalente "
            f"al seguente codice Python:\n\n```python\n{source}\n```\n\n"
            "Adatta ingressi, uscite e logica al dominio hardware/MXFP4."
        )

    # Modalità interattiva
    print(f"""
  Descrivi l'unità aritmetica da implementare in Chisel + MXFP4.

  Esempi di specifiche:
    • "Implementa un full adder 1-bit con ingressi e uscite MXFP4 E2M1"
    • "Crea un moltiplicatore che moltiplica due numeri MXFP4 a 4 bit"
    • "ALU MXFP4 con addizione e sottrazione, gestione overflow"
    • "Ripple-carry adder 4-bit con rappresentazione MXFP4"
""")
    spec = input(f"  {BOLD}Descrizione dell'unità:{RESET} ").strip()
    if not spec:
        err("Specifica vuota.")
        sys.exit(1)
    ok(f"Specifica acquisita ({len(spec)} caratteri)")
    return spec

# Primo agente: il Planner analizza la specifica e produce un piano JSON strutturato. 
# Il piano include nome modulo, ingressi/uscite, algoritmo e segnali interni. Questo piano è poi usato dal Coder come base per la generazione del codice Chisel.
def run_planner(spec: str, agent: Agent) -> dict:
    agent_step("PLANNER", "Analisi della specifica → piano di implementazione JSON")

    raw = agent.run(
        f"Specifica dell'unità da implementare:\n\n{spec}\n\n"
        "Crea il piano JSON completo."
    )

# Estrazione del JSON dalla risposta dell'agente. 
    clean = re.sub(r"```json|```", "", raw).strip()
    m = re.search(r"\{.*\}", clean, re.DOTALL)
    if m:
        clean = m.group(0)

    try:
        plan = json.loads(clean)
        ok(f"Modulo: '{plan.get('nome_modulo', '?')}'  —  "
           f"tipo: {plan.get('tipo', '?')}")
        ok(f"Ingressi: {len(plan.get('ingressi', []))}  |  "
           f"Uscite: {len(plan.get('uscite', []))}")
        if plan.get("passi_algoritmo"):
            ok(f"Algoritmo: {len(plan['passi_algoritmo'])} passi pianificati")
        return plan
    except json.JSONDecodeError as e:
        warn(f"JSON non parsabile ({e}) — continuo con piano testuale")
        return {"nome_modulo": "MxFp4Unit", "tipo": "combinatorio",
                "descrizione": spec, "raw_plan": raw,
                "ingressi": [], "uscite": [], "passi_algoritmo": []}

# Secondo agente: il Coder riceve il piano JSON e genera il codice Chisel 3 completo.
def run_coder(plan: dict, spec: str, agent: Agent) -> str:
    agent_step("CODER", "Generazione codice Chisel 3 MXFP4")

    plan_str = json.dumps(plan, ensure_ascii=False, indent=2)
    prompt = (
        f"Specifica originale:\n{spec}\n\n"
        f"Piano di implementazione:\n{plan_str}\n\n"
        "Genera il codice Chisel 3 completo e compilabile.\n"
        "Ricorda: no markdown fence, solo codice Scala."
    )

    code = agent.run(prompt)
    code = re.sub(r"```scala|```", "", code).strip()

    ok(f"Codice generato: {len(code)} caratteri, "
       f"{code.count(chr(10))+1} righe")
    return code

# Terzo e quarto agente: Reviewer e Fixer lavorano in un loop. 
# Il Reviewer valuta il codice generato, se ci sono problemi il Fixer li corregge. Questo ciclo si ripete fino a quando il codice passa la revisione o si raggiunge il numero massimo di iterazioni.
def run_review_fix_loop(
    code:      str,
    spec:      str,
    reviewer:  Agent,
    fixer:     Agent,
    compiler:  SbtCompiler,
    stem:      str,
    max_iter:  int
) -> tuple[str, list[dict]]:
    agent_step("REVIEWER/FIXER", f"Loop review → fix (max {max_iter} iterazioni)")

    iteration_log: list[dict] = []

    for i in range(1, max_iter + 1):
        print(f"\n  {CYAN}── Iterazione {i}/{max_iter} ──{RESET}")

# Revisione LLM 
        reviewer.reset_history()  
        review_result = reviewer.run(
            f"Specifica originale:\n{spec}\n\n"
            f"Codice Chisel da revisionare:\n{code}"
        )
        passed_llm = review_result.strip().upper().startswith("PASS")

        if passed_llm:
            ok("LLM Reviewer: PASS")
        else:
            warn("LLM Reviewer: trovati problemi")
            issues_preview = "\n".join(review_result.splitlines()[:6])
            print(f"  {DIM}{issues_preview}{RESET}")

# Compilazione con sbt. 
        compile_ok, compile_out = compiler.compile(code, stem)
        if compiler.available:
            if compile_ok:
                ok("sbt compile: OK")
            else:
                warn("sbt compile: ERRORI")
                print(f"  {DIM}{compile_out[:300]}…{RESET}")
        else:
            info("sbt non disponibile — solo revisione LLM")

# Log iterazione. 
        log_entry: dict = {
            "iterazione":      i,
            "review_llm":      review_result,
            "review_llm_pass": passed_llm,
            "compile_ok":      compile_ok,
            "compile_output":  compile_out[:600] if compile_out else "",
            "fix_applicato":   False,
            "esito":           "",
        }

# Esito.
        tutto_ok = passed_llm and compile_ok

        if tutto_ok:
            ok(f"Codice validato all'iterazione {i}")
            log_entry["esito"] = "PASS"
            iteration_log.append(log_entry)
            break

        if i == max_iter:
            warn(f"Raggiunto limite iterazioni ({max_iter}) — uso l'ultimo codice")
            log_entry["esito"] = "MAX_ITER_REACHED"
            iteration_log.append(log_entry)
            break

# Fix
        agent_step("FIXER", f"Correzione automatica (iterazione {i})")

        fix_prompt = f"Codice con problemi:\n{code}\n\n"
        if not passed_llm:
            fix_prompt += f"Problemi rilevati da LLM Reviewer:\n{review_result}\n\n"
        if not compile_ok and compiler.available:
            fix_prompt += (
                f"Errori di compilazione sbt:\n{compile_out[:1000]}\n\n"
            )
        fix_prompt += (
            "Correggi TUTTI i problemi elencati e restituisci "
            "il codice Chisel completo e corretto."
        )

        code = fixer.run(fix_prompt)
        code = re.sub(r"```scala|```", "", code).strip()
        ok(f"Codice corretto: {len(code)} caratteri")

        log_entry["fix_applicato"] = True
        log_entry["esito"]         = "FIXED_CONTINUE"
        iteration_log.append(log_entry)

    return code, iteration_log

# Quinto agente: il Tester genera un testbench ChiselTest/ScalaTest completo, coprendo casi base, zero, massimo, overflow e simmetria.
def run_tester(code: str, plan: dict, agent: Agent) -> str:
    agent_step("TESTER", "Generazione testbench ChiselTest")

    plan_str = json.dumps(plan, ensure_ascii=False, indent=2)
    tb = agent.run(
        f"Piano del modulo:\n{plan_str}\n\n"
        f"Codice Chisel del modulo:\n{code}\n\n"
        "Genera il testbench ChiselTest completo.\n"
        "Ricorda: no markdown fence, solo codice Scala."
    )
    tb = re.sub(r"```scala|```", "", tb).strip()
    ok(f"Testbench generato: {len(tb)} caratteri")
    return tb

# Salvataggio di tutti gli outputs in una directory timestamped. Include codice Chisel, testbench, report Markdown, log JSON, build.sbt e README.
def save_outputs(
    spec:         str,
    plan:         dict,
    code:         str,
    testbench:    str,
    iter_log:     list[dict],
    model:        str,
    compiler_avail: bool
) -> Path:
    step(6, "Salvataggio artefatti")

    stem  = re.sub(r"[^a-zA-Z0-9_]", "_",
                   plan.get("nome_modulo", "MxFp4Unit"))
    msafe = model.replace(":", "_").replace("/", "_")
    ts    = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    out   = Path(f"chisel_output_{stem}_{msafe}_{ts}")
    out.mkdir(exist_ok=True)

    hdr = (
        "// ═══════════════════════════════════════════════════════════\n"
        "//  Generato da: agentic_chisel_mxfp4_ollama.py\n"
        f"//  Modello Ollama: {model}\n"
        f"//  Data: {datetime.datetime.now().isoformat(timespec='seconds')}\n"
        "// ═══════════════════════════════════════════════════════════\n\n"
    )

# Modulo Chisel
    (out / f"{stem}.scala").write_text(hdr + code, encoding="utf-8")
    ok(f"Modulo Chisel   → {out}/{stem}.scala")

# Testbench
    (out / f"{stem}Test.scala").write_text(hdr + testbench, encoding="utf-8")
    ok(f"Testbench       → {out}/{stem}Test.scala")

# build.sbt
    (out / "build.sbt").write_text(
        'scalaVersion := "2.13.12"\n\n'
        'libraryDependencies ++= Seq(\n'
        '  "org.chipsalliance" %% "chisel"     % "6.5.0",\n'
        '  "edu.berkeley.cs"   %% "chiseltest" % "6.0.0" % "test",\n'
        ')\n\n'
        'addCompilerPlugin(\n'
        '  "org.chipsalliance" % "chisel-plugin" % "6.5.0" cross CrossVersion.full\n'
        ')\n\n'
        'scalacOptions ++= Seq(\n'
        '  "-language:reflectiveCalls",\n'
        '  "-deprecation",\n'
        '  "-feature",\n'
        '  "-Xcheckinit",\n'
        ')\n',
        encoding="utf-8"
    )
    ok(f"build.sbt       → {out}/build.sbt")

# Report Markdown
    n_fix  = sum(1 for it in iter_log if it.get("fix_applicato"))
    n_pass = sum(1 for it in iter_log if it.get("esito") == "PASS")

    iters_md = ""
    for it in iter_log:
        iters_md += (
            f"\n#### Iterazione {it['iterazione']} "
            f"`{it['esito']}`\n\n"
            f"| Verifica | Risultato |\n|---|---|\n"
            f"| LLM Reviewer | `{'PASS' if it['review_llm_pass'] else 'ISSUES'}` |\n"
            f"| sbt compile  | `{'OK' if it['compile_ok'] else 'FAIL'}` |\n"
            f"| Fix applicato | `{it['fix_applicato']}` |\n"
        )
        if it.get("fix_applicato") and it.get("review_llm"):
            excerpt = it["review_llm"][:400]
            iters_md += f"\n**Issues rilevati:**\n```\n{excerpt}\n```\n"

    algo_md = ""
    for p in plan.get("passi_algoritmo", []):
        algo_md += f"- {p}\n"

    (out / f"report_{stem}.md").write_text(
        f"# Report Agentico — {stem} Chisel MXFP4\n\n"
        f"| Campo | Valore |\n|---|---|\n"
        f"| **Modulo** | `{stem}` |\n"
        f"| **Modello Ollama** | `{model}` |\n"
        f"| **Data** | {datetime.datetime.now().isoformat(timespec='seconds')} |\n"
        f"| **Agenti eseguiti** | Planner, Coder, Reviewer, Fixer, Tester |\n"
        f"| **Iterazioni review/fix** | {len(iter_log)} |\n"
        f"| **Fix automatici applicati** | {n_fix} |\n"
        f"| **Compilazione sbt** | "
        f"{'Abilitata' if compiler_avail else 'Non disponibile (solo LLM review)'} |\n\n"
        f"---\n\n"
        f"## Specifica Originale\n\n{spec}\n\n"
        f"---\n\n"
        f"## Piano di Implementazione (Planner Agent)\n\n"
        f"```json\n{json.dumps(plan, ensure_ascii=False, indent=2)}\n```\n\n"
        f"### Algoritmo pianificato\n\n{algo_md}\n"
        f"---\n\n"
        f"## Log Agentico — Review/Fix Loop\n{iters_md}\n"
        f"---\n\n"
        f"## Formato MXFP4 (E2M1)\n\n"
        f"```\n"
        f"bit[3]   = segno  (0=+, 1=−)\n"
        f"bit[2:1] = esponente a 2 bit (bias=1)\n"
        f"bit[0]   = mantissa a 1 bit\n\n"
        f"Valore: (−1)^sign × 2^(exp−1) × (1 + mant×0.5)\n"
        f"Valori speciali: 0b0000=0, 0b0111=+6.0, 0b1111=−6.0\n"
        f"```\n\n"
        f"---\n\n"
        f"*Report generato automaticamente da `agentic_chisel_mxfp4_ollama.py`*\n",
        encoding="utf-8"
    )
    ok(f"Report Markdown → {out}/report_{stem}.md")

# Log JSON completo
    (out / "agent_log.json").write_text(
        json.dumps({
            "timestamp": ts,
            "model":     model,
            "spec":      spec,
            "plan":      plan,
            "stats": {
                "iterazioni":    len(iter_log),
                "fix_applicati": n_fix,
                "esito_finale":  iter_log[-1]["esito"] if iter_log else "N/A",
            },
            "iterations": iter_log,
        }, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    ok(f"Log JSON        → {out}/agent_log.json")

# README
    (out / "README.md").write_text(
        f"# {stem} — Chisel MXFP4\n\n"
        f"Generato da **agentic_chisel_mxfp4_ollama.py** con modello `{model}`.\n\n"
        f"## Compilazione e test\n\n"
        f"```bash\nsbt test\n```\n\n"
        f"## File generati\n\n"
        f"| File | Descrizione |\n|---|---|\n"
        f"| `{stem}.scala` | Modulo Chisel MXFP4 |\n"
        f"| `{stem}Test.scala` | Testbench ChiselTest |\n"
        f"| `report_{stem}.md` | Report completo per la tesi |\n"
        f"| `agent_log.json` | Log JSON del workflow agentico |\n"
        f"| `build.sbt` | Progetto SBT |\n",
        encoding="utf-8"
    )
    ok(f"README          → {out}/README.md")

    return out

# Setup iniziale: verifica che Ollama sia raggiungibile e che ci siano modelli disponibili. Se non ci sono modelli, fornisce istruzioni per installarne uno.
def check_ollama(host: str) -> list[str]:
    step(0, f"Verifica Ollama ({host})")
    data = ollama_get(f"{host}/api/tags")
    if data is None:
        err(f"Ollama non raggiungibile su {host}")
        print(f"""
  {YELLOW}Soluzioni:{RESET}
    1. Avvia Ollama:       {BOLD}ollama serve{RESET}
    2. Installa un modello:{BOLD}ollama pull codellama{RESET}
    3. Host diverso:       {BOLD}--host http://IP:11434{RESET}
""")
        sys.exit(1)
    models = [m["name"] for m in data.get("models", [])]
    if not models:
        err("Nessun modello installato. Esegui: ollama pull codellama")
        sys.exit(1)
    ok(f"Ollama online — {len(models)} modello/i disponibile/i")
    return models


def choose_model(available: list[str], model_arg: str | None) -> str:
    if model_arg:
        matches = [m for m in available if m.startswith(model_arg)]
        if matches:
            ok(f"Modello: {BOLD}{matches[0]}{RESET}")
            return matches[0]
        warn(f"'{model_arg}' non trovato — scegli dalla lista")

    ordered = []
    for rec in RECOMMENDED_MODELS:
        ordered.extend(m for m in available if m.startswith(rec))
    ordered.extend(m for m in available if m not in ordered)

    print(f"\n  {BOLD}Modelli disponibili:{RESET}")
    for i, name in enumerate(ordered, 1):
        is_rec = any(name.startswith(r)
                     for r in ["codellama", "deepseek-coder", "qwen2.5-coder"])
        tag = f"  {GREEN}← consigliato per HDL/codice{RESET}" if is_rec else ""
        print(f"    {BOLD}{i:2}.{RESET} {name}{tag}")

    while True:
        raw = input(f"\n  {BOLD}Scegli numero o nome [1]:{RESET} ").strip() or "1"
        if raw.isdigit() and 0 < int(raw) <= len(ordered):
            chosen = ordered[int(raw) - 1]
            break
        matches = [m for m in available if m.startswith(raw)]
        if matches:
            chosen = matches[0]
            break
        warn("Scelta non valida, riprova.")

    ok(f"Modello selezionato: {BOLD}{chosen}{RESET}")
    return chosen

# Main.
def main():
    banner()

    parser = argparse.ArgumentParser(
        description="Agentic Chisel MXFP4 generator — Ollama locale",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Esempi:
              python agentic_chisel_mxfp4_ollama.py
              python agentic_chisel_mxfp4_ollama.py --spec "full adder MXFP4 4 bit"
              python agentic_chisel_mxfp4_ollama.py --file full_adder.py --model codellama
              python agentic_chisel_mxfp4_ollama.py --spec "moltiplicatore MXFP4" --iter 5 --verbose
        """)
    )
    parser.add_argument("--spec",  "-s", help="Specifica testuale (es. 'full adder MXFP4')")
    parser.add_argument("--file",  "-f", help="File Python come contesto (backward compat)")
    parser.add_argument("--model", "-m", help="Modello Ollama (es. codellama, deepseek-coder)")
    parser.add_argument("--host",        default=DEFAULT_HOST,
                                         help=f"URL Ollama (default: {DEFAULT_HOST})")
    parser.add_argument("--iter", "-i",  type=int, default=MAX_FIX_ITER,
                                         help=f"Max iterazioni review/fix (default: {MAX_FIX_ITER})")
    parser.add_argument("--verbose", "-v", action="store_true", help="Output dettagliato")
    args = parser.parse_args()

# Setup iniziale: verifica Ollama, scegli modello, acquisisci specifica e inizializza compiler.
    available = check_ollama(args.host)
    model     = choose_model(available, args.model)
    spec      = get_specification(args.file, args.spec)
    compiler  = SbtCompiler()

    if compiler.available:
        ok("sbt trovato → compilazione reale abilitata nel loop")
    else:
        info("sbt non trovato → solo LLM review nel loop  "
             "(installa sbt da https://www.scala-sbt.org per compilazione reale)")

    hr()
    print(f"\n  {BOLD}Pipeline agentica:{RESET}  "
          f"Planner → Coder → [Reviewer ⟷ Fixer]×{args.iter} → Tester\n")

# Inizializzo i 5 agenti.
    planner  = Agent("Planner",  SYSTEM_PLANNER,  args.host, model)
    coder    = Agent("Coder",    SYSTEM_CODER,    args.host, model)
    reviewer = Agent("Reviewer", SYSTEM_REVIEWER, args.host, model)
    fixer    = Agent("Fixer",    SYSTEM_FIXER,    args.host, model)
    tester   = Agent("Tester",   SYSTEM_TESTER,   args.host, model)

    t_global = datetime.datetime.now()

# Eseguo il workflow.
    plan      = run_planner(spec, planner)
    stem_safe = re.sub(r"[^a-zA-Z0-9_]", "_",
                       plan.get("nome_modulo", "MxFp4Unit"))

    code      = run_coder(plan, spec, coder)

    code, iter_log = run_review_fix_loop(
        code, spec, reviewer, fixer,
        compiler, stem_safe, args.iter
    )

    testbench = run_tester(code, plan, tester)

    out_dir   = save_outputs(
        spec, plan, code, testbench,
        iter_log, model, compiler.available
    )

    elapsed_total = (datetime.datetime.now() - t_global).total_seconds()

# Output finale e statistiche.
    hr()
    n_fix   = sum(1 for it in iter_log if it.get("fix_applicato"))
    esito   = iter_log[-1]["esito"] if iter_log else "N/A"
    esito_s = f"{GREEN}PASS{RESET}" if esito == "PASS" else f"{YELLOW}{esito}{RESET}"

    print(f"""
{GREEN}{BOLD} Pipeline agentica completata in {elapsed_total:.0f}s!{RESET}

  {BOLD}Statistiche:{RESET}
      • Agenti eseguiti:      5  (Planner, Coder, Reviewer, Fixer, Tester)
      • Iterazioni review/fix: {len(iter_log)}
      • Fix automatici:        {n_fix}
      • Esito finale:          {esito_s}
      • sbt compilazione:      {'✔ abilitata' if compiler.available else '⚠ non disponibile'}

  {BOLD}Output:{RESET}  {BOLD}{out_dir}/{RESET}
      ├── {stem_safe}.scala           ← Modulo Chisel MXFP4
      ├── {stem_safe}Test.scala       ← Testbench ChiselTest
      ├── report_{stem_safe}.md       ← Report per la tesi
      ├── agent_log.json              ← Log JSON del workflow agentico
      ├── build.sbt                   ← Progetto SBT
      └── README.md

  {CYAN}Compila e testa:{RESET}
      cd {out_dir}
      sbt test
""")
    hr()


if __name__ == "__main__":
    main()