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

import mxfp4_ref  # Modello di riferimento bit-accurato per MXFP4 E2M1 (golden model)

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
per il formato numerico elemento MXFP4 (E2M1).

CONTESTO MXFP4 — SCOPE DICHIARATO:
  Questo framework genera unità che operano sul formato ELEMENTO E2M1
  (1 valore a 4 bit), che è il "building block" del formato MXFP4 completo.
  Il formato MXFP4 completo, definito dalla OCP Microscaling (MX)
  Specification v1.0, è un formato block floating-point: un blocco di 32
  elementi E2M1 condivide un fattore di scala a 8 bit E8M0. Questo
  framework NON implementa il block-scaling a 32 elementi: genera
  aritmetica a livello di singolo elemento E2M1. Questo va sempre
  dichiarato nel campo "note_mxfp4" del piano.

FORMATO E2M1 (4 bit, bias = 1):
  bit[3]   = segno (0=positivo, 1=negativo)
  bit[2:1] = esponente (2 bit)
  bit[0]   = mantissa (1 bit)

  Regola di decodifica (obbligatoria, NON confondere i due casi):
    • se esponente == 0 (SUBNORMALE): valore = (-1)^segno * mantissa * 0.5
      (bit implicito = 0, NON 1)
    • se esponente != 0 (NORMALE):    valore = (-1)^segno * (1 + mantissa*0.5) * 2^(esponente-1)
      (bit implicito = 1)

  Valori rappresentabili: 0, 0.5, 1, 1.5, 2, 3, 4, 6 (e i corrispondenti negativi).
  Massimo rappresentabile: 0b0111 = +6.0 (usato come valore di saturazione in overflow).

  Per la MOLTIPLICAZIONE l'esponente del risultato prima della normalizzazione
  è exp_a + exp_b - bias (bias=1): il bias va sempre sottratto una volta,
  altrimenti il risultato è concettualmente errato.

  Ogni piano che coinvolga somma/allineamento di mantisse deve includere
  esplicitamente un passo di ARROTONDAMENTO (anche solo round-to-nearest
  con 1 bit di guardia) prima della normalizzazione finale a 1 bit di
  mantissa: il solo troncamento introduce un bias sistematico e va evitato
  o quantomeno dichiarato esplicitamente come approssimazione voluta.

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
  "gestione_subnormali": "descrivi come viene trattato il caso esponente==0",
  "sottrazione_bias": "true|false|non_applicabile — obbligatorio true per moltiplicatori",
  "note_mxfp4": "OBBLIGATORIO: includi la dichiarazione di scope (solo elemento E2M1, nessun block-scaling a 32 elementi/E8M0) più le scelte su subnormali/bias/arrotondamento"
}

ESEMPIO COMPLETO (per un moltiplicatore — segui ESATTAMENTE questa forma,
in particolare "tipo": "MXFP4" per ogni ingresso/uscita in formato E2M1;
NON inventare schemi alternativi come campi "mantissa"/"exponent" separati
con larghezze a piacere, NON usare "tipo": "binary" — il formato MXFP4
E2M1 è SEMPRE un singolo segnale a 4 bit con i tre campi sign/exp/mant,
mai due segnali separati con larghezze diverse):
{
  "nome_modulo": "MXFP4Multiplier",
  "tipo": "combinatorio",
  "descrizione": "Moltiplicatore MXFP4 E2M1: prodotto di due valori a 4 bit con sottrazione del bias e saturazione in overflow",
  "ingressi": [
    {"nome": "a", "tipo": "MXFP4", "bit": 4, "descrizione": "primo operando E2M1"},
    {"nome": "b", "tipo": "MXFP4", "bit": 4, "descrizione": "secondo operando E2M1"}
  ],
  "uscite": [
    {"nome": "prod", "tipo": "MXFP4", "bit": 4, "descrizione": "prodotto E2M1, saturato a +-6.0 in overflow"}
  ],
  "segnali_interni": [
    {"nome": "mant_prod", "tipo": "UInt", "bit": 4, "descrizione": "prodotto delle mantisse estese col bit implicito"},
    {"nome": "exp_sum", "tipo": "UInt", "bit": 3, "descrizione": "somma degli esponenti prima della sottrazione del bias"}
  ],
  "passi_algoritmo": [
    "1. Estrai segno (XOR), esponente e mantissa di a e b",
    "2. Bit implicito: 0 se esponente==0 (subnormale), 1 altrimenti",
    "3. Moltiplica le mantisse estese col bit implicito",
    "4. Somma gli esponenti e sottrai il bias (1)",
    "5. Normalizza il prodotto e arrotonda a 1 bit di mantissa",
    "6. Satura a +-6.0 (0b0111/0b1111) se l'esponente normalizzato eccede il massimo rappresentabile"
  ],
  "bundle_mxfp4_necessario": true,
  "gestione_subnormali": "bit implicito 0 quando exp==0 su entrambi gli operandi",
  "sottrazione_bias": "true",
  "note_mxfp4": "Implementazione a livello di singolo elemento E2M1; nessun block-scaling E8M0/32 elementi (fuori scope)."
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
   Aggiungi SEMPRE un commento sopra il Bundle che dichiari lo scope:
     "// Elemento E2M1 (building block di MXFP4). Nessun block-scaling
     //  a 32 elementi / E8M0: si veda OCP MX Specification v1.0."
3. Ogni modulo Chisel estende Module e ha un val io = IO(new Bundle { ... })
4. Usa := per assegnazioni, non =
5. I segnali Wire si dichiarano con: val nome = Wire(UInt(N.W))
6. Usa nomi inglesi snake_case per segnali e moduli PascalCase
7. Commenta ogni blocco logico in italiano (utile per la tesi)
8. Nessuna libreria esterna oltre a chisel3 standard
9. Il codice deve essere COMPLETO e COMPILABILE
10. GESTIONE SUBNORMALI (OBBLIGATORIA): il bit implicito della mantissa
    NON è sempre 1. Devi discriminare esplicitamente il caso esponente==0:
      val bit_implicito = Mux(x.exp === 0.U, 0.U(1.W), 1.U(1.W))
      val mantissa_estesa = Cat(bit_implicito, x.mant)
    Usare sempre Cat(1.U(1.W), x.mant) senza il Mux è un ERRORE.
11. SOTTRAZIONE DEL BIAS (OBBLIGATORIA per moltiplicatori/operazioni che
    sommano esponenti): l'esponente combinato è
      val exp_combinato = (x.exp +& y.exp) - 1.U   // bias = 1, va sottratto
    Sommare gli esponenti senza sottrarre il bias è un ERRORE concettuale.
12. ARROTONDAMENTO: dopo un'addizione o normalizzazione che produce più
    bit di mantissa di quanti ne siano disponibili in uscita (1 bit),
    non troncare silenziosamente: implementa almeno un arrotondamento
    round-to-nearest con bit di guardia, oppure commenta esplicitamente
    "// troncamento (non arrotondamento) - approssimazione dichiarata"
    se scegli di semplificare.
13. I nomi dei segnali di ingresso/uscita nell'IO Bundle DEVONO corrispondere
    ESATTAMENTE (stesso nome) ai campi "nome" di ingressi/uscite nel piano
    JSON ricevuto, per permettere la validazione automatica esterna.
14. NON aggiungere un "object NomeModulo extends App" con chiamate a
    "Driver.execute(...)" o simili: non serve né per la compilazione con
    sbt compile né per i test ChiselTest, e le API "chisel3.Driver" /
    "chisel3.iotesters.Driver" sono deprecate o rimosse a seconda della
    versione di Chisel, causando errori di compilazione imprevedibili.
    Il file deve contenere SOLO la (o le) classi Chisel (Bundle/Module),
    nessun punto di ingresso eseguibile.

Rispondi con SOLO il codice Scala/Chisel.
Non usare markdown (no ```), nessun testo prima o dopo il codice.
"""

SYSTEM_REVIEWER = """\
Sei un revisore esperto di codice Chisel 3 per unità aritmetiche MXFP4.
Ricevi del codice Chisel 3 e devi identificare errori precisi.

AMBITO (importante): rivedi SOLO il modulo hardware (Bundle/Module) che
implementa la specifica. NON proporre, richiedere o scrivere un testbench,
test case, logica di verifica, contatori di stato per iterare su vettori
di test, o segnali di clock/reset gestiti manualmente: quello è compito di
un agente diverso (il Tester), eseguito in una fase successiva e separata.
Se il codice che ricevi non contiene un testbench, questo NON è un
problema da segnalare: è corretto che non ce l'abbia. Il modulo sotto
revisione deve rimanere un modulo Chisel sintetizzabile, senza logica di
test al suo interno.

CHECKLIST DA VERIFICARE:
  [ ] Import: chisel3._ e chisel3.util._ presenti
  [ ] Bundle MXFP4 definito con: sign (Bool), exp (UInt(2.W)), mant (UInt(1.W))
  [ ] Ogni modulo estende Module
  [ ] IO Bundle dichiarato con val io = IO(new Bundle { ... })
  [ ] Assegnazioni usano := non =
  [ ] Wire dichiarati prima dell'uso
  [ ] Parentesi graffe bilanciate
  [ ] Nessuna sintassi Scala non supportata in Chisel 3
  [ ] Nessun import o riferimento a librerie inesistenti
  [ ] SUBNORMALI: il bit implicito NON è sempre 1. Deve esistere un Mux
      (o equivalente) che usa bit implicito 0 quando exp === 0.U e
      bit implicito 1 altrimenti. Cat(1.U, mant) incondizionato è un ERRORE.
  [ ] BIAS: se il modulo somma esponenti (es. moltiplicatore), il bias (1)
      deve essere sottratto una volta dal risultato della somma. La sola
      somma "a.exp +& b.exp" senza sottrazione del bias è un ERRORE.
  [ ] ARROTONDAMENTO: se la mantissa intermedia ha più bit di quella
      d'uscita, deve esserci un arrotondamento esplicito oppure un
      commento che dichiari il troncamento come scelta consapevole.
  [ ] NOMI PORTE: i nomi dei segnali IO corrispondono ai nomi del piano JSON.
  [ ] Nessun "object ... extends App" / "Driver.execute(...)": non serve e
      può causare errori di compilazione legati alla versione di Chisel.

IMPORTANTE — criterio PASS vs ISSUES:
Rispondi PASS se e solo se TUTTI i punti della checklist sopra sono
soddisfatti, anche se il codice potrebbe essere migliorato dal punto di
vista stilistico (nomi più chiari, refactoring, sintassi più concisa,
ecc.). Suggerimenti di stile, leggibilità o "best practice" opzionali
NON sono motivo per rispondere ISSUES: se la checklist è soddisfatta,
rispondi comunque PASS e ignora le tue considerazioni stilistiche.
Usa ISSUES solo quando un punto della checklist NON è soddisfatto
(codice che non compila, bit implicito sempre 1, bias non sottratto,
nessun arrotondamento né dichiarazione di troncamento, nomi porte
diversi dal piano, ecc.).

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

AMBITO (importante): il file che correggi è SOLO il modulo hardware
(Bundle/Module) sotto test, non un testbench. Anche se tra i problemi
segnalati compare un suggerimento di aggiungere test, verifica, contatori
di stato per iterare su vettori, o gestione manuale di clock/reset,
IGNORA quel suggerimento: non è pertinente a questo file. Non aggiungere
mai logica di test all'interno del modulo, e non rinominare la classe del
modulo (deve restare quella del piano originale).

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
   - Bit implicito 0 quando exp===0.U (subnormale), 1 altrimenti — MAI
     Cat(1.U, mant) incondizionato
   - Bias (1) sottratto una volta quando si sommano esponenti
   - Arrotondamento esplicito (o troncamento dichiarato) prima della
     normalizzazione finale della mantissa
   - Nomi delle porte IO identici ai nomi nel piano JSON
   - NESSUN "object ... extends App" con Driver.execute(...): non serve e
     rischia di rompere la compilazione tra versioni diverse di Chisel

Rispondi con SOLO il codice Scala/Chisel completo e corretto.
Nessuna spiegazione, nessun markdown, nessun testo prima o dopo il codice:
la tua risposta deve iniziare con "import chisel3._" e finire con l'ultima
graffa di chiusura del codice.
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

CASI DA TESTARE (qualitativi — casi mirati, non esaustivi):
  • Caso base (valori tipici)
  • Zero (0x0)
  • Un valore SUBNORMALE come ingresso (exp=00, mant=1 → 0.5): verifica
    che il bit implicito usato sia 0 e non 1
  • Valore massimo rappresentabile in MXFP4 (0b0111 = +6.0)
  • Valori negativi (se il modulo li supporta)
  • Overflow/underflow e saturazione
  • Simmetria (a op b == b op a per operazioni commutative)

Ricorda: in MXFP4 E2M1 il valore massimo è 0b0111 = +6.0

IMPORTANTE: NON è necessario generare test esaustivi su tutte le
combinazioni di ingresso: per la copertura esaustiva (tutte le 256
combinazioni a 4x4 bit, calcolate con un modello di riferimento Python
indipendente) il framework aggiunge automaticamente un metodo di test
dopo la tua risposta. Concentrati sui casi qualitativi elencati sopra,
con commenti chiari in italiano sul significato di ciascun caso.

Rispondi con SOLO il codice Scala del testbench (la classe test completa,
graffa di chiusura inclusa). Nessun markdown, nessun testo aggiuntivo.
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
        proj_dir = tmp / "project"
        proj_dir.mkdir(exist_ok=True)
        (proj_dir / "build.properties").write_text(
            "sbt.version=1.10.7\n", encoding="utf-8"
        )
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
# Estrazione robusta di un oggetto JSON dalla risposta di un LLM. La vecchia
# regex "\{.*\}" con DOTALL è greedy: se il modello scrive testo con altre
# graffe prima/dopo il JSON (spiegazioni, frammenti di codice), finisce per
# catturare dalla prima alla ultima graffa di TUTTO il testo, producendo un
# blob non valido o valido-ma-sbagliato. Qui invece si cerca la prima graffa
# aperta e si segue la profondità delle graffe (ignorando quelle dentro le
# stringhe) fino a trovare la graffa di chiusura corrispondente: un solo
# oggetto JSON, delimitato correttamente.
# Estrazione robusta di codice Scala dalla risposta di un LLM. La vecchia
# pulizia (rimuovere solo i marcatori ```scala/```) lascia intatta qualunque
# prosa che il modello scriva prima o dopo il codice quando non usa i
# fence markdown (es. spiegazioni, intestazioni "### Note", istruzioni
# per l'utente) — prosa che finisce dentro il file .scala e ne impedisce
# la compilazione. Qui si individua l'inizio del codice cercando la prima
# riga "import chisel3" (o, in mancanza, la prima "class "/"object ") e si
# taglia la coda subito dopo l'ultima graffa che chiude un blocco di primo
# livello, scartando qualunque testo scritto dopo.
def extract_scala_code(text: str) -> str:
    text = re.sub(r"```scala|```", "", text)
    lines = text.splitlines()

# Se il modello scrive più tentativi in sequenza (spiega, mostra codice,
# spiega di nuovo, riscrive tutto il codice corretto) — osservato in
# pratica — ogni tentativo tende a ripartire da "import chisel3". In quel
# caso l'ultima occorrenza è la versione finale/corretta: le precedenti
# sono bozze superate e vanno scartate, non concatenate.
# Ancora specifica: "import chisel3._" esatto (non "import chisel3.util._",
# che è il secondo import dello stesso blocco, non l'inizio di un nuovo
# tentativo). Cercare "import\s+chisel3" in modo generico avrebbe incluso
# anche "import chisel3.util._" tra le occorrenze, rompendo il caso
# normale di un unico file con entrambi gli import.
    import_idxs = [i for i, line in enumerate(lines)
                   if re.match(r"^\s*import\s+chisel3\._\s*$", line)]
    start_idx = import_idxs[-1] if import_idxs else None

    if start_idx is None:
        for i, line in enumerate(lines):
            if re.match(r"^\s*(class|object|trait)\s+\w+", line):
                start_idx = i
                break
    if start_idx is None:
        return text.strip()  # nessun'ancora riconosciuta: meglio non alterare nulla

    code = "\n".join(lines[start_idx:])

    depth = 0
    in_string = False
    escape = False
    started = False
    last_valid_end = None
    for i, ch in enumerate(code):
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
            started = True
        elif ch == "}":
            depth -= 1
            if started and depth == 0:
                last_valid_end = i  # aggiornato ad ogni blocco top-level chiuso

    if last_valid_end is not None:
        code = code[:last_valid_end + 1]
    return strip_app_entrypoint(code.strip())


# Rimuove eventuali "object NomeModulo extends App { ... }" (entry point
# eseguibile con Driver.execute o simili). Non serve né per "sbt compile"
# né per i test ChiselTest, e le API Driver cambiano/spariscono tra
# versioni di Chisel — una causa di errore ricorrente e del tutto evitabile
# rimuovendola direttamente in Python, invece di fare affidamento solo
# sull'obbedienza del modello alla regola corrispondente nel prompt.
def strip_app_entrypoint(code: str) -> str:
    pattern = re.compile(r"object\s+\w+\s+extends\s+App\s*\{")
    result = code
    while True:
        m = pattern.search(result)
        if not m:
            break
        start = m.start()
        depth = 0
        end = None
        for i in range(m.end() - 1, len(result)):
            ch = result[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        if end is None:
            break  # graffe non bilanciate: meglio non toccare nulla
        result = result[:start] + result[end + 1:]

    # Rimuove anche l'import residuo (es. "import chisel3.Driver" o
    # "import chisel3.iotesters.Driver"), che da solo causerebbe un errore
    # di compilazione nelle versioni di Chisel dove quell'oggetto non esiste
    # più, anche dopo aver tolto il blocco "object ... extends App".
    result = re.sub(r"(?m)^\s*import\s+[\w.]*\bDriver\b\s*$\n?", "", result)
    return result.strip()


def extract_json_object(text: str) -> str | None:
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None  # graffe non bilanciate: nessun oggetto completo trovato


# Riparazioni "leggere" e sicure per errori comuni nel JSON generato da LLM
# (virgole finali, commenti stile Python/JS, virgolette tipografiche).
# Non e' un parser tollerante generico: se dopo queste riparazioni il JSON
# resta invalido, si fallisce esplicitamente invece di indovinare oltre.
def repair_json(text: str) -> str:
    text = text.replace("“", '"').replace("”", '"').replace("’", "'")
    text = re.sub(r"//.*?$", "", text, flags=re.MULTILINE)          # commenti //
    text = re.sub(r"(?m)^\s*#.*?$", "", text)                        # commenti #
    text = re.sub(r",\s*([}\]])", r"\1", text)                       # virgole finali
    return text


# Alias per campi che alcuni modelli restituiscono in inglese invece che
# nei nomi italiani dello schema richiesto. Applicato dopo il parsing,
# senza toccare i valori — solo per non perdere un piano altrimenti valido
# a causa di nomi di chiave diversi.
_TOP_LEVEL_ALIASES = {
    "module_name": "nome_modulo", "name": "nome_modulo",
    "type": "tipo", "description": "descrizione",
    "inputs": "ingressi", "outputs": "uscite",
    "internal_signals": "segnali_interni", "signals": "segnali_interni",
    "algorithm_steps": "passi_algoritmo", "steps": "passi_algoritmo",
    "notes": "note_mxfp4",
}
_FIELD_ALIASES = {
    "name": "nome", "type": "tipo", "bits": "bit",
    "description": "descrizione",
}


def normalize_plan_keys(plan: dict) -> dict:
    if not isinstance(plan, dict):
        return plan
    normalized = {}
    for k, v in plan.items():
        key = _TOP_LEVEL_ALIASES.get(k, k)
        normalized[key] = v
    for campo in ("ingressi", "uscite", "segnali_interni"):
        if isinstance(normalized.get(campo), list):
            normalized[campo] = [
                {_FIELD_ALIASES.get(k, k): v for k, v in item.items()}
                if isinstance(item, dict) else item
                for item in normalized[campo]
            ]
    return normalized


def run_planner(spec: str, agent: Agent) -> dict:
    agent_step("PLANNER", "Analisi della specifica → piano di implementazione JSON")

    raw = agent.run(
        f"Specifica dell'unità da implementare:\n\n{spec}\n\n"
        "Crea il piano JSON completo."
    )

# Estrazione del JSON dalla risposta dell'agente, con riparazioni leggere
# e normalizzazione dei nomi dei campi (vedi funzioni sopra).
    clean = re.sub(r"```json|```", "", raw).strip()
    candidate = extract_json_object(clean) or clean

    plan = None
    for tentativo in (candidate, repair_json(candidate)):
        try:
            plan = json.loads(tentativo)
            break
        except json.JSONDecodeError:
            continue

    if plan is not None:
        plan = normalize_plan_keys(plan)
        ok(f"Modulo: '{plan.get('nome_modulo', '?')}'  —  "
           f"tipo: {plan.get('tipo', '?')}")
        ok(f"Ingressi: {len(plan.get('ingressi', []))}  |  "
           f"Uscite: {len(plan.get('uscite', []))}")
        if plan.get("passi_algoritmo"):
            ok(f"Algoritmo: {len(plan['passi_algoritmo'])} passi pianificati")
        if not plan.get("ingressi") or not plan.get("uscite") or not _plan_usa_mxfp4(plan):
            _save_planner_debug(raw)
        return plan

    warn("JSON non parsabile — continuo con piano vuoto (vedi file di debug)")
    _save_planner_debug(raw)
    return {"nome_modulo": "MxFp4Unit", "tipo": "combinatorio",
            "descrizione": spec, "raw_plan": raw,
            "ingressi": [], "uscite": [], "passi_algoritmo": []}


def _plan_usa_mxfp4(plan: dict) -> bool:
    """Controlla che almeno un ingresso o un'uscita dichiari esplicitamente
    tipo MXFP4. Si è osservato in pratica che alcuni modelli, pur avendo
    ingressi/uscite non vuoti, inventano uno schema alternativo (es. campi
    "mantissa"/"exponent" separati con tipo "binary" e larghezze a
    piacere) invece di seguire il formato E2M1 richiesto: un piano del
    genere è vuoto ai fini pratici quanto uno con ingressi/uscite assenti,
    e va intercettato allo stesso modo invece di lasciarlo proseguire."""
    campi = list(plan.get("ingressi") or []) + list(plan.get("uscite") or [])
    return any(isinstance(c, dict) and str(c.get("tipo", "")).upper() == "MXFP4"
               for c in campi)


def _save_planner_debug(raw: str) -> None:
    """Salva SEMPRE la risposta grezza del Planner quando il piano risulta
    vuoto o non parsabile, cosi' l'errore è ispezionabile invece che
    nascosto dietro 'Ingressi: 0 | Uscite: 0'."""
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    debug_path = Path(f"planner_debug_{ts}.txt")
    debug_path.write_text(raw, encoding="utf-8")
    warn(f"Risposta grezza del Planner salvata in: {debug_path}  "
         f"(controllala per capire cosa ha risposto il modello)")
    preview = raw[:400].replace("\n", " ")
    info(f"Anteprima: {preview}{'…' if len(raw) > 400 else ''}")

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
    code = extract_scala_code(code)

    ok(f"Codice generato: {len(code)} caratteri, "
       f"{code.count(chr(10))+1} righe")
    return code

# Terzo e quarto agente: Reviewer e Fixer lavorano in un loop. 
# Il Reviewer valuta il codice generato, se ci sono problemi il Fixer li corregge. Questo ciclo si ripete fino a quando il codice passa la revisione o si raggiunge il numero massimo di iterazioni.
# Controllo strutturale deterministico (non dipende dall'LLM): se il piano
# richiede segnali di tipo MXFP4, il codice DEVE definire il Bundle MXFP4
# con i tre campi sign/exp/mant. È il requisito più fondamentale di tutti
# (senza non si sta generando un'unità MXFP4, punto), quindi non può
# dipendere dal fatto che un Reviewer LLM se ne accorga o meno — si è
# osservato in pratica che alcuni modelli continuano a dare feedback
# stilistico anche quando questo requisito di base manca del tutto.
def mxfp4_bundle_mancante(plan: dict, code: str) -> bool:
    richiede_mxfp4 = any(
        isinstance(x, dict) and str(x.get("tipo", "")).upper() == "MXFP4"
        for x in list(plan.get("ingressi") or []) + list(plan.get("uscite") or [])
    )
    if not richiede_mxfp4:
        return False
    has_bundle = bool(re.search(r"class\s+MXFP4\s+extends\s+Bundle", code))
    has_fields = all(re.search(rf"\b{f}\b", code) for f in ("sign", "exp", "mant"))
    # Non basta che il Bundle sia DEFINITO: deve essere anche USATO per
    # almeno una porta IO (Input/Output(new MXFP4)). Si è osservato in
    # pratica un caso in cui "class MXFP4 extends Bundle" era presente ma
    # mai referenziato nell'IO reale, ancora basato su UInt semplici.
    has_usage = bool(re.search(r"(Input|Output)\s*\(\s*new\s+MXFP4\b", code))
    return not (has_bundle and has_fields and has_usage)


def run_review_fix_loop(
    code:      str,
    spec:      str,
    plan:      dict,
    reviewer:  Agent,
    fixer:     Agent,
    compiler:  SbtCompiler,
    stem:      str,
    max_iter:  int
) -> tuple[str, list[dict]]:
    agent_step("REVIEWER/FIXER", f"Loop review → fix (max {max_iter} iterazioni)")

    iteration_log: list[dict] = []
    compile_ok_streak = 0  # iterazioni consecutive con sbt compile riuscito

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
        compile_ok_streak = compile_ok_streak + 1 if compile_ok else 0
        if compiler.available:
            if compile_ok:
                ok("sbt compile: OK")
            else:
                warn("sbt compile: ERRORI")
                print(f"  {DIM}{compile_out[:1500]}…{RESET}")
        else:
            info("sbt non disponibile — solo revisione LLM")

# Log iterazione. 
        log_entry: dict = {
            "iterazione":      i,
            "review_llm":      review_result,
            "review_llm_pass": passed_llm,
            "compile_ok":      compile_ok,
            "compile_output":  compile_out if compile_out else "",
            "fix_applicato":   False,
            "esito":           "",
        }

# Esito. Oltre al PASS esplicito del Reviewer, si accetta anche il caso in
# cui sbt compili con successo per 2 iterazioni consecutive: alcuni modelli
# locali (osservato con qwen2.5-coder) non rispettano il contratto di
# risposta rigido (PASS / ISSUES) e continuano a fornire solo suggerimenti
# stilistici anche quando il codice compila correttamente da tempo. sbt è
# un segnale deterministico di correttezza sintattica/di tipo — più
# affidabile, su questo aspetto, di un LLM che non rispetta il formato
# richiesto — e viene usato qui come rete di sicurezza per non sprecare
# iterazioni su un codice già valido dal punto di vista Chisel/Scala.
#
# ATTENZIONE: sbt compila con successo anche codice che ha abbandonato del
# tutto MXFP4 (es. un full adder booleano con porte UInt semplici) — la
# compilazione riuscita prova solo la correttezza sintattica Scala/Chisel,
# non la conformità al formato. Per questo, indipendentemente da PASS del
# Reviewer o dalla compilazione, il Bundle MXFP4 deve essere presente ogni
# volta che il piano lo richiede: è un controllo deterministico che non
# dipende dal fatto che un Reviewer LLM se ne accorga.
        bundle_ok = not mxfp4_bundle_mancante(plan, code)
        if not bundle_ok:
            warn("Controllo strutturale: il piano richiede segnali MXFP4 ma "
                 "il codice non definisce il Bundle MXFP4 (sign/exp/mant) — "
                 "blocco indipendente dal Reviewer/da sbt")

        accettato_per_compilazione_stabile = (
            not passed_llm and compiler.available
            and compile_ok and compile_ok_streak >= 2 and bundle_ok
        )
        tutto_ok = (passed_llm and bundle_ok) or accettato_per_compilazione_stabile

        if tutto_ok:
            if accettato_per_compilazione_stabile:
                ok(f"Codice accettato all'iterazione {i}: sbt compila da "
                   f"{compile_ok_streak} iterazioni consecutive (il Reviewer "
                   f"non ha risposto PASS in formato stretto, ma non ha "
                   f"segnalato difetti bloccanti — vedi report per dettagli)")
                log_entry["esito"] = "PASS_COMPILE_STABLE"
            else:
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
        if not bundle_ok:
            fix_prompt += (
                "PROBLEMA BLOCCANTE (controllo automatico, non del Reviewer): "
                "il piano richiede segnali di tipo MXFP4 ma il codice non "
                "definisce il Bundle MXFP4 con i campi sign (Bool), "
                "exp (UInt(2.W)), mant (UInt(1.W)), oppure non lo usa per "
                "gli ingressi/uscite dichiarati MXFP4. Aggiungilo e usalo: "
                "un'unità che manipola solo UInt/Bool grezzi non è "
                "un'unità MXFP4.\n\n"
            )
        if not passed_llm:
            fix_prompt += f"Problemi rilevati da LLM Reviewer:\n{review_result}\n\n"
        if not compile_ok and compiler.available:
            fix_prompt += (
                f"Errori di compilazione sbt:\n{compile_out[:2500]}\n\n"
            )
        fix_prompt += (
            "Correggi TUTTI i problemi elencati e restituisci "
            "il codice Chisel completo e corretto."
        )

        code = fixer.run(fix_prompt)
        code = extract_scala_code(code)
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
    tb = extract_scala_code(tb)
    ok(f"Testbench generato (casi qualitativi): {len(tb)} caratteri")

# Validazione esaustiva contro il golden model Python (mxfp4_ref).
# Non dipende dall'LLM: i valori attesi sono calcolati deterministicamente,
# quindi non possono soffrire dello stesso errore concettuale del codice
# generato (elimina il rischio di "validazione circolare").
    op = detect_operation(plan)
    if op:
        exhaustive_method = build_exhaustive_test_method(plan, op)
        if exhaustive_method:
            tb = insert_method_in_test_class(tb, exhaustive_method)
            ok(f"Test esaustivo aggiunto: 256 combinazioni contro mxfp4_ref.py "
               f"(operazione rilevata: '{op}')")
        else:
            warn("Operazione rilevata ma non è stato possibile determinare "
                 "i nomi delle porte IO dal piano — test esaustivo saltato")
    else:
        info("Operazione non riconosciuta automaticamente (add/mul/cmp) — "
             "nessun test esaustivo aggiunto, solo i casi qualitativi del Tester")

    return tb


def detect_operation(plan: dict) -> str | None:
    """Individua euristicamente il tipo di operazione a partire dal piano,
    per decidere se e quale test esaustivo generare con mxfp4_ref.
    Riconosce solo il caso più semplice e comune: due ingressi MXFP4 e
    una singola uscita (MXFP4 per add/mul, Bool per cmp).

    Difensivo per costruzione: alcuni modelli restituiscono "ingressi"/
    "uscite" come liste di stringhe invece che di oggetti {"nome":...,
    "tipo":...}. In quel caso non possiamo determinare i tipi in modo
    affidabile, quindi si rinuncia al test esaustivo (return None) invece
    di andare in errore.
    """
    testo = " ".join(str(x) for x in [
        plan.get("nome_modulo", ""),
        plan.get("descrizione", ""),
        " ".join(str(p) for p in plan.get("passi_algoritmo", []) or []),
    ]).lower()

    ingressi = [i for i in (plan.get("ingressi") or []) if isinstance(i, dict)]
    uscite = [o for o in (plan.get("uscite") or []) if isinstance(o, dict)]
    ingressi_mxfp4 = [i for i in ingressi if str(i.get("tipo", "")).upper() == "MXFP4"]
    if len(ingressi_mxfp4) != 2 or len(uscite) < 1:
        return None  # supportiamo solo il caso a due operandi MXFP4

    if any(k in testo for k in ("moltiplic", "mult", "prod")):
        return "mul"
    if any(k in testo for k in ("somm", "add", "sum", "adder")):
        return "add"
    if any(k in testo for k in ("confront", "compar", "maggiore", "cmp")):
        return "cmp"
    return None


def build_exhaustive_test_method(plan: dict, op: str) -> str | None:
    """Costruisce (in Python, senza passare dall'LLM) un metodo ScalaTest
    che verifica TUTTE le 256 combinazioni di ingresso contro il modello
    di riferimento mxfp4_ref. I nomi delle porte sono presi dal piano."""
    ingressi_mxfp4 = [i for i in (plan.get("ingressi") or [])
                       if isinstance(i, dict) and str(i.get("tipo", "")).upper() == "MXFP4"]
    if len(ingressi_mxfp4) != 2:
        return None
    a_name, b_name = ingressi_mxfp4[0]["nome"], ingressi_mxfp4[1]["nome"]

    uscite = [o for o in (plan.get("uscite") or []) if isinstance(o, dict)]
    if op in ("add", "mul"):
        uscite_mxfp4 = [o for o in uscite if str(o.get("tipo", "")).upper() == "MXFP4"]
        if not uscite_mxfp4:
            return None
        out_name = uscite_mxfp4[0]["nome"]
        vectors = mxfp4_ref.exhaustive_binary_vectors(op)
        righe = ",\n      ".join(
            f'({v["a"]}, {v["b"]}, {str(bool(v["expected_sign"])).lower()}, '
            f'{v["expected_exp"]}, {v["expected_mant"]})'
            for v in vectors
        )
        modulo_name = plan.get("nome_modulo", "Modulo")
        return f"""
  // ==========================================================================
  // Test ESAUSTIVO generato automaticamente da mxfp4_ref.py (Python), NON
  // dall'LLM: copre tutte le 256 combinazioni di ingresso a 4x4 bit e
  // confronta l'uscita del modulo Chisel con il modello di riferimento
  // bit-accurato del formato E2M1 (golden model, validato contro la
  // Tabella 4.1 della tesi in mxfp4_ref.self_check()).
  // ==========================================================================
  it should "rispettare il modello di riferimento Python su tutte le 256 combinazioni ({op})" in {{
    test(new {modulo_name}) {{ dut =>
      val vettori = Seq(
      {righe}
      )
      for ((aBits, bBits, eSign, eExp, eMant) <- vettori) {{
        dut.io.{a_name}.sign.poke((((aBits >> 3) & 1) == 1).B)
        dut.io.{a_name}.exp.poke(((aBits >> 1) & 3).U)
        dut.io.{a_name}.mant.poke((aBits & 1).U)
        dut.io.{b_name}.sign.poke((((bBits >> 3) & 1) == 1).B)
        dut.io.{b_name}.exp.poke(((bBits >> 1) & 3).U)
        dut.io.{b_name}.mant.poke((bBits & 1).U)
        dut.clock.step(1)
        dut.io.{out_name}.sign.expect(eSign.B,
          s"a=$aBits b=$bBits: segno atteso da mxfp4_ref=$eSign")
        dut.io.{out_name}.exp.expect(eExp.U,
          s"a=$aBits b=$bBits: esponente atteso da mxfp4_ref=$eExp")
        dut.io.{out_name}.mant.expect(eMant.U,
          s"a=$aBits b=$bBits: mantissa attesa da mxfp4_ref=$eMant")
      }}
    }}
  }}
"""
    else:  # cmp
        uscite_bool = [o for o in uscite if str(o.get("tipo", "")).upper() == "BOOL"]
        if not uscite_bool:
            return None
        out_name = uscite_bool[0]["nome"]
        vectors = mxfp4_ref.exhaustive_binary_vectors("cmp")
        righe = ",\n      ".join(
            f'({v["a"]}, {v["b"]}, {str(v["expected_gt"]).lower()})'
            for v in vectors
        )
        modulo_name = plan.get("nome_modulo", "Modulo")
        return f"""
  // ==========================================================================
  // Test ESAUSTIVO generato automaticamente da mxfp4_ref.py (Python):
  // tutte le 256 combinazioni di confronto contro il golden model.
  // ==========================================================================
  it should "rispettare il modello di riferimento Python su tutte le 256 combinazioni (cmp)" in {{
    test(new {modulo_name}) {{ dut =>
      val vettori = Seq(
      {righe}
      )
      for ((aBits, bBits, eGt) <- vettori) {{
        dut.io.{a_name}.sign.poke((((aBits >> 3) & 1) == 1).B)
        dut.io.{a_name}.exp.poke(((aBits >> 1) & 3).U)
        dut.io.{a_name}.mant.poke((aBits & 1).U)
        dut.io.{b_name}.sign.poke((((bBits >> 3) & 1) == 1).B)
        dut.io.{b_name}.exp.poke(((bBits >> 1) & 3).U)
        dut.io.{b_name}.mant.poke((bBits & 1).U)
        dut.clock.step(1)
        dut.io.{out_name}.expect(eGt.B,
          s"a=$aBits b=$bBits: confronto atteso da mxfp4_ref=$eGt")
      }}
    }}
  }}
"""


def insert_method_in_test_class(testbench: str, method_code: str) -> str:
    """Inserisce il metodo di test esaustivo appena prima dell'ultima
    graffa di chiusura della classe di test generata dall'LLM. Se non
    riesce a individuarla in modo affidabile, appende il metodo in coda
    (fuori dalla classe) e lo segnala con un commento, cosi' l'errore
    è visibile e correggibile a mano invece che silenzioso."""
    last_brace = testbench.rfind("}")
    if last_brace == -1:
        return testbench + "\n// ATTENZIONE: impossibile individuare la chiusura della classe;\n" \
                            "// il metodo esaustivo seguente va incollato manualmente nella classe:\n" + method_code
    return testbench[:last_brace] + method_code + "\n" + testbench[last_brace:]

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

# Golden model Python (copiato per tracciabilità: i valori attesi nel test
# esaustivo derivano da questo file, non da stime manuali).
    ref_src = Path(__file__).parent / "mxfp4_ref.py"
    if ref_src.exists():
        shutil.copy(ref_src, out / "mxfp4_ref.py")
        ok(f"Golden model    → {out}/mxfp4_ref.py")

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
        f"## Formato MXFP4 (E2M1) — e scope della validazione\n\n"
        f"```\n"
        f"bit[3]   = segno  (0=+, 1=−)\n"
        f"bit[2:1] = esponente a 2 bit (bias=1)\n"
        f"bit[0]   = mantissa a 1 bit\n\n"
        f"Normale   (exp!=0): (−1)^sign × 2^(exp−1) × (1 + mant×0.5)\n"
        f"Subnormale(exp==0): (−1)^sign × mant × 0.5   (bit implicito = 0)\n"
        f"Valori rappresentabili: 0, 0.5, 1, 1.5, 2, 3, 4, 6 (e negativi)\n"
        f"```\n\n"
        f"**Nota di scope.** Questo framework genera unità aritmetiche a "
        f"livello di *singolo elemento* E2M1, il building block del formato "
        f"MXFP4. Il formato MXFP4 completo (OCP Microscaling/MX Specification "
        f"v1.0) prevede inoltre un fattore di scala condiviso a 8 bit (E8M0) "
        f"per blocchi di 32 elementi, che qui NON è implementato: è "
        f"un'estensione lasciata a sviluppi futuri.\n\n"
        f"**Metodologia di validazione.** Oltre alla revisione LLM e alla "
        f"compilazione sbt, il codice generato viene verificato con un "
        f"test esaustivo (256 combinazioni, se l'operazione è "
        f"riconosciuta automaticamente) generato deterministicamente da un "
        f"modello di riferimento Python indipendente (`mxfp4_ref.py`, "
        f"incluso tra gli artefatti), validato a sua volta contro la "
        f"tabella dei valori E2M1 della tesi. I valori attesi non sono "
        f"quindi stimati manualmente, ma calcolati da un golden model "
        f"separato dal codice sotto test.\n\n"
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
        f"| `{stem}Test.scala` | Testbench ChiselTest (casi qualitativi LLM "
        f"+ test esaustivo 256 combinazioni contro il golden model) |\n"
        f"| `mxfp4_ref.py` | Golden model Python indipendente (E2M1): "
        f"usato per calcolare i valori attesi del test esaustivo |\n"
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

# Guardia di validità: se il Planner non ha prodotto un piano MXFP4
# minimamente coerente (nessun ingresso/uscita), ci si ferma qui invece
# di far proseguire Coder/Reviewer/Fixer per max_iter iterazioni su un
# piano vuoto — capita con modelli che non rispettano bene il formato
# JSON richiesto (es. modelli generalisti piccoli).
    if not plan.get("ingressi") or not plan.get("uscite"):
        err("Il Planner non ha prodotto un piano JSON valido "
            "(ingressi/uscite vuoti). Controlla il file "
            "planner_debug_*.txt appena salvato nella cartella corrente "
            "per vedere esattamente cosa ha risposto il modello.")
        info("Suggerimento: riprova con --model deepseek-coder oppure "
             "--model qwen2.5-coder (seguono meglio le istruzioni di "
             "formato rispetto a modelli generalisti più piccoli).")
        sys.exit(1)

    if not _plan_usa_mxfp4(plan):
        err("Il piano non contiene alcun ingresso/uscita con tipo "
            "\"MXFP4\": il modello ha probabilmente inventato uno schema "
            "diverso (es. campi 'mantissa'/'exponent' separati con "
            "larghezze a piacere) invece del formato E2M1 richiesto. "
            "Controlla planner_debug_*.txt.")
        info("Suggerimento: riformula la specifica menzionando "
             "esplicitamente 'formato MXFP4 E2M1 (segno, 2 bit esponente, "
             "1 bit mantissa)' invece di descrivere l'operazione in "
             "astratto, e/o riprova con un altro modello.")
        sys.exit(1)

    stem_safe = re.sub(r"[^a-zA-Z0-9_]", "_",
                       plan.get("nome_modulo", "MxFp4Unit"))

    code      = run_coder(plan, spec, coder)

    code, iter_log = run_review_fix_loop(
        code, spec, plan, reviewer, fixer,
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
    esito_s = (f"{GREEN}{esito}{RESET}" if esito in ("PASS", "PASS_COMPILE_STABLE")
               else f"{YELLOW}{esito}{RESET}")

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