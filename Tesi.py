import os
import sys
import json
import re
import shutil
import shlex
import subprocess
import argparse
import datetime
import tempfile
import textwrap
import urllib.request
import urllib.error
from abc import ABC, abstractmethod
from pathlib import Path

# La console di Windows usa spesso una codepage legacy (cp1252) che non sa
# codificare i caratteri Unicode del banner/box-drawing (═, ║, ⟷...): senza
# questo il programma va in crash con UnicodeEncodeError prima ancora di
# stampare qualsiasi cosa, anche solo con --help.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

# ═══════════════════════════════════════════════════════════════════════
#  Configurazione generale.
# ═══════════════════════════════════════════════════════════════════════
DEFAULT_HOST   = "http://localhost:11434"
MAX_FIX_ITER   = 10    # Limite di iterazioni per evitare loop infiniti.
OLLAMA_TIMEOUT = 600

RECOMMENDED_MODELS = [
    "qwen2.5-coder",
    "codellama",
    "deepseek-coder",
    "deepseek-r1",
    "llama3.1",
    "llama3",
    "mistral",
    "phi4",
]

RESET, BOLD, CYAN  = "\033[0m", "\033[1m", "\033[36m"
GREEN, YELLOW, RED = "\033[32m", "\033[33m", "\033[31m"
DIM, BLUE, MAGENTA = "\033[2m", "\033[34m", "\033[35m"


def banner():
    print(f"""\n{CYAN}{BOLD}\
╔══════════════════════════════════════════════════════════════════════╗
║  Agentic Meta-HDL Generator — MXFP4  (Ollama, 100% locale)            ║
║  Selector → Planner → Coder → [Reviewer⟷Fixer] → Tester → [Test⟷Fixer]║
║  Linguaggi supportati: Chisel 3 (Scala) · Amaranth HDL (Python)       ║
╚══════════════════════════════════════════════════════════════════════╝{RESET}""")


def agent_step(agent_name: str, desc: str):
    print(f"\n{MAGENTA}{BOLD}[{agent_name.upper()}]{RESET} {desc}")


def step(n, msg: str):
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
    print(f"{DIM}{'─' * 72}{RESET}")


# Coda dell'output di un tool da riga di comando (sbt, python, verilator):
# questi strumenti stampano il boilerplate di avvio per primo e solo alla
# fine l'errore vero — troncare con [:N] mostrerebbe quasi sempre solo il
# boilerplate.
def tail(text: str, n: int) -> str:
    return text[-n:] if len(text) > n else text


# Isola dall'output grezzo del toolchain le righe che portano davvero un
# segnale (errori di compilazione/elaborazione, eccezioni Python, asserzioni
# fallite) invece del rumore di avvio. Versione "leggera" del waveform
# tracing AST-based di VerilogCoder: qui, non potendo introspezionare la
# forma d'onda della simulazione (fuori scala per un'unità combinatoria),
# si ottiene lo stesso risultato pratico filtrando per marcatori testuali
# noti, comuni sia a sbt/ScalaTest sia ai traceback Python.
FAILURE_MARKERS = (
    "[error]", "error:", "exception", "failed", "did not equal",
    "assertion", "expect(", "mismatch", "traceback", "raise ",
    "typeerror", "nameerror", "attributeerror", "syntaxerror",
    "risultato: fail",
)


def extract_failure_lines(output: str, max_lines: int = 25) -> str:
    seen: set[str] = set()
    hits: list[str] = []
    for line in output.splitlines():
        low = line.strip().lower()
        if not low or low in seen:
            continue
        if any(marker in low for marker in FAILURE_MARKERS):
            hits.append(line.strip())
            seen.add(low)
        if len(hits) >= max_lines:
            break
    return "\n".join(hits)


# Firma normalizzata di un errore, usata per rilevare loop senza progressi
# (meccanismo di "escape" di ReChisel): due errori con la stessa causa
# devono produrre la stessa firma anche se contengono dettagli che
# cambiano a ogni iterazione (path temporanei, timestamp, PID).
def error_signature(text: str) -> str:
    basis = extract_failure_lines(text) or tail(text, 200)
    basis = re.sub(r"(chisel_check|amaranth_check)_\S+", r"\1_X", basis)
    basis = re.sub(r"/mnt/\S+", "PATH", basis)
    basis = re.sub(r"[A-Za-z]:\\\S+", "PATH", basis)
    basis = re.sub(r"\d{4,}", "N", basis)
    basis = re.sub(r"\s+", " ", basis).strip()
    return basis[:300]


# ═══════════════════════════════════════════════════════════════════════
#  Bridge WSL: su Windows sia Verilator sia (in questo progetto) `make`
#  vivono quasi sempre solo dentro una distro WSL. Helper condivisi da
#  entrambi i backend per invocare un binario nativo se disponibile, o
#  altrimenti — se lo si trova dentro WSL — instradare il comando tramite
#  `wsl.exe`.
# ═══════════════════════════════════════════════════════════════════════
def wsl_which(binary: str) -> str | None:
    if shutil.which("wsl.exe") is None and shutil.which("wsl") is None:
        return None
    try:
        result = subprocess.run(
            ["wsl.exe", "-e", "which", binary],
            capture_output=True, text=True, timeout=15
        )
        path = result.stdout.strip()
        return path if result.returncode == 0 and path else None
    except Exception:
        return None


# Converte un path Windows nel corrispondente path visto da dentro WSL
# (C:\Users\... → /mnt/c/Users/...). Costruito a mano invece di chiamare
# 'wslpath' perché quest'ultimo, invocato da un processo Windows con un
# path contenente backslash, tronca l'argomento in modo inaffidabile.
def to_wsl_path(win_path: Path) -> str:
    p = str(win_path.resolve())
    drive, rest = p.split(":", 1)
    rest = rest.replace("\\", "/")
    return f"/mnt/{drive.lower()}{rest}"


def run_in_wsl(cmd_str: str, timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["wsl.exe", "-e", "bash", "-lc", cmd_str],
        capture_output=True, text=True, timeout=timeout
    )


# ═══════════════════════════════════════════════════════════════════════
#  Api per Ollama.
# ═══════════════════════════════════════════════════════════════════════
def ollama_get(url: str, timeout: int = 5):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def ollama_chat(host: str, model: str, system_prompt: str,
                 history: list[dict], timeout: int = OLLAMA_TIMEOUT,
                 temperature: float = 0.1) -> str:
    payload = {
        "model":   model,
        "stream":  False,
        "system":  system_prompt,
        "options": {"temperature": temperature, "num_predict": 4096},
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


# Rappresenta un agente LLM con memoria conversazionale propria (così il
# Fixer "ricorda" le iterazioni precedenti e non ripete gli stessi errori).
# 'temperature' resta bassa (deterministica) per le correzioni in-place;
# viene alzata solo per i restart ad alta diversità del meccanismo di
# escape (idea di MAGE: campionamento ad alta temperatura per uscire da un
# candidato bloccato).
class Agent:
    def __init__(self, name: str, system_prompt: str, host: str, model: str):
        self.name          = name
        self.system_prompt = system_prompt
        self.host          = host
        self.model         = model
        self.history: list[dict] = []

    def run(self, user_message: str, temperature: float = 0.1) -> str:
        self.history.append({"role": "user", "content": user_message})
        info(f"Agente {BOLD}{self.name}{RESET} in elaborazione…")

        t_start  = datetime.datetime.now()
        response = ollama_chat(
            self.host, self.model, self.system_prompt, self.history,
            temperature=temperature
        )
        elapsed = (datetime.datetime.now() - t_start).total_seconds()

        self.history.append({"role": "assistant", "content": response})
        ok(f"{self.name} risposta in {elapsed:.1f}s  ({len(response)} caratteri)")
        return response

    def reset_history(self):
        self.history = []


# Estrae la riga "Diagnosi: ..." che i prompt SYSTEM_FIXER_* chiedono di
# anteporre al codice corretto (stile ReAct di RTLFixer: prima il
# ragionamento, poi l'azione). Solo per il log/report — l'estrazione del
# codice vero e proprio è compito di backend.extract_code().
def extract_diagnosis(text: str) -> str:
    m = re.search(r"Diagnosi:\s*(.+)", text)
    return m.group(1).strip() if m else ""


# ═══════════════════════════════════════════════════════════════════════
#  HDLBackend — astrazione che rende il resto della pipeline agnostico
#  rispetto al linguaggio Meta-HDL scelto dal Selector. Ogni backend
#  concreto fornisce: i quattro system prompt (Coder/Reviewer/Fixer/
#  Tester), un modo di estrarre codice dalla risposta grezza dell'LLM, un
#  controllo "compila/elabora davvero" e un controllo "i test passano
#  davvero" (entrambi non-LLM, eseguiti su un vero toolchain), una base di
#  conoscenza di errori noti, e il layout con cui salvare gli artefatti.
# ═══════════════════════════════════════════════════════════════════════
class HDLBackend(ABC):
    key: str
    display_name: str

    @property
    @abstractmethod
    def selector_blurb(self) -> str:
        """Descrizione del linguaggio mostrata al Selector per la scelta."""

    @property
    @abstractmethod
    def plan_type_vocab(self) -> str:
        """Vocabolario dei tipi da iniettare nello schema JSON del Planner."""

    @property
    @abstractmethod
    def critical_reminders(self) -> str:
        """Promemoria delle regole più violate, usato dal meccanismo di escape."""

    @property
    @abstractmethod
    def compile_check_label(self) -> str: ...

    @property
    @abstractmethod
    def test_check_label(self) -> str: ...

    @property
    @abstractmethod
    def file_manifest(self) -> list[tuple[str, str]]:
        """[(path relativo, descrizione)] dei file salvati da save(), per report/README."""

    @property
    @abstractmethod
    def run_instructions(self) -> str:
        """Comando/i per eseguire i test dell'output salvato su disco."""

    @abstractmethod
    def prompts(self) -> dict[str, str]:
        """{'coder':..., 'reviewer':..., 'fixer':..., 'tester':...}"""

    @abstractmethod
    def extract_code(self, text: str) -> str: ...

    @abstractmethod
    def strip_duplicate_module(self, text: str, module_name: str) -> str: ...

    @abstractmethod
    def check_uses_shared_types(self, code: str) -> bool: ...

    @abstractmethod
    def retrieve_hints(self, text: str) -> str: ...

    @abstractmethod
    def check_module(self, code: str, stem: str) -> tuple[bool, str]:
        """Equivalente di 'compila': vero controllo statico non-LLM."""

    @abstractmethod
    def run_tests(self, code: str, testbench: str, stem: str, plan: dict) -> tuple[bool, str]:
        """Esecuzione reale dei test (simulazione), non giudizio LLM."""

    @abstractmethod
    def toolchain_status(self) -> list[tuple[bool, str]]:
        """Righe di stato ('OK'/'WRN', messaggio) mostrate all'avvio."""

    @abstractmethod
    def save(self, out_dir: Path, stem: str, code: str, testbench: str) -> None: ...


# ═══════════════════════════════════════════════════════════════════════
#  BACKEND 1 — Chisel 3 (Scala), compilato/testato con sbt + ChiselTest,
#  simulazione reale tramite il backend Verilator (VerilatorBackendAnnotation).
#  Bundle MXFP4 e algoritmo di addizione scritti a mano e validati
#  esaustivamente (0 discrepanze su tutte le 256 combinazioni possibili di
#  operandi contro un modello di riferimento round-to-nearest) in sessioni
#  precedenti di questo progetto — vedi mxfp4_ref.py.
# ═══════════════════════════════════════════════════════════════════════

SYSTEM_CODER_CHISEL = """\
Sei un esperto di Chisel 3 (Scala) e di formati numerici a bassa precisione.
Devi implementare un'unità aritmetica hardware in Chisel 3 con supporto MXFP4.

REGOLE OBBLIGATORIE:
1. Prima riga: import chisel3._
   Seconda riga: import chisel3.util._
   Terza riga: import mxfp4._
2. Il Bundle MXFP4 (sign: Bool, exp: UInt(2.W), mant: UInt(1.W)) è GIÀ
   DEFINITO nel package mxfp4 fornito dal toolchain (MXFP4.scala).
   NON ridefinire mai "class MXFP4" o "object MXFP4". Esistono DUE forme
   distinte di "MXFP4(...)", NON intercambiabili:
     - "new MXFP4" o "MXFP4()" (senza argomenti) = TEMPLATE DI TIPO, usalo
       SOLO per dichiarare porte/segnali: "Input(new MXFP4)", "Wire(new MXFP4)".
     - "MXFP4(bits)" (un Int 0..15, i 4 bit codificati) = VALORE LETTERALE
       COSTANTE, usalo SOLO per assegnare/confrontare un valore concreto
       (es. "io.out := MXFP4(0xF)" per saturare a -6.0). MAI "Input(MXFP4(bits))"
       né "new MXFP4(bits)": non sono validi, causano errori di compilazione.
3. Ogni modulo Chisel estende Module e ha un val io = IO(new Bundle { ... })
4. Usa := per assegnazioni, non =
5. I segnali Wire si dichiarano con: val nome = Wire(UInt(N.W))
6. Usa nomi inglesi snake_case per segnali e moduli PascalCase
7. Commenta ogni blocco logico in italiano (utile per la tesi)
8. Nessuna libreria esterna oltre a chisel3 e al package mxfp4 fornito
9. Il codice deve essere COMPLETO e COMPILABILE
10. Le porte logiche elementari (XOR, AND, OR, NOT) su segnali UInt/Bool
    semplici si scrivono SEMPRE con gli operatori Chisel diretti (^, &, |, !),
    MAI istanziando "Module(new XOR)" ecc: queste classi non esistono.
    ATTENZIONE: questo vale SOLO per segnali UInt/Bool. Un segnale di tipo
    MXFP4 è un Bundle e NON ha operatori ^/&/| (causa l'errore di
    compilazione "value ^ is not a member of mxfp4.MXFP4"): per combinare
    due segnali MXFP4 usa SEMPRE l'algoritmo di addizione del punto 11, mai
    operatori bitwise diretti su un intero Bundle MXFP4.
11. ADDIZIONE MXFP4: quando la specifica chiede di sommare due valori MXFP4,
    NON usare XOR/AND/OR sui bit grezzi (non è un'addizione floating-point
    valida). Usa questo algoritmo, già validato (compila con sbt e produce
    lo stesso risultato di un modello di riferimento round-to-nearest su
    tutte le 256 combinazioni possibili di operandi MXFP4): decodifica in
    virgola fissa a scala "×2" (rappresenta esattamente gli 8 valori
    rappresentabili 0/0.5/1/1.5/2/3/4/6 come interi 0/1/2/3/4/6/8/12, senza
    perdita di precisione), somma con segno, poi arrotonda/satura e ricodifica:

      def magX2(exp: UInt, mant: UInt): UInt =
        Mux(exp === 0.U, mant, (2.U +& mant) << (exp - 1.U))

      val a_mag = magX2(io.a.exp, io.a.mant)
      val b_mag = magX2(io.b.exp, io.b.mant)
      val a_signed = Mux(io.a.sign, 0.S -& a_mag.zext, a_mag.zext)
      val b_signed = Mux(io.b.sign, 0.S -& b_mag.zext, b_mag.zext)
      val sum_signed = a_signed +& b_signed
      val out_sign = sum_signed < 0.S
      val sum_abs  = sum_signed.abs.asUInt

      val out_exp  = Wire(UInt(2.W))
      val out_mant = Wire(UInt(1.W))
      when (sum_abs >= 11.U) {
        out_exp := 3.U; out_mant := 1.U   // 6.0 (satura/arrotonda)
      }.elsewhen (sum_abs >= 8.U) {
        out_exp := 3.U; out_mant := 0.U   // 4.0
      }.elsewhen (sum_abs >= 6.U) {
        out_exp := 2.U; out_mant := 1.U   // 3.0
      }.elsewhen (sum_abs >= 4.U) {
        out_exp := 2.U; out_mant := 0.U   // 2.0
      }.elsewhen (sum_abs >= 3.U) {
        out_exp := 1.U; out_mant := 1.U   // 1.5
      }.elsewhen (sum_abs >= 2.U) {
        out_exp := 1.U; out_mant := 0.U   // 1.0
      }.elsewhen (sum_abs >= 1.U) {
        out_exp := 0.U; out_mant := 1.U   // 0.5
      }.otherwise {
        out_exp := 0.U; out_mant := 0.U   // 0
      }
      // <segnale_uscita>.sign := out_sign
      // <segnale_uscita>.exp  := out_exp
      // <segnale_uscita>.mant := out_mant

    Adatta i nomi dei segnali (io.a/io.b) alla specifica. Se la specifica ha
    più di due operandi MXFP4 (es. un "full adder" con A, B, Cin), applica
    l'algoritmo in sequenza: prima somma i primi due, poi somma il risultato
    con il terzo.

Rispondi con SOLO il codice Scala/Chisel.
Non usare markdown (no ```), nessun testo prima o dopo il codice.
"""

SYSTEM_REVIEWER_CHISEL = """\
Sei un revisore esperto di codice Chisel 3 per unità aritmetiche MXFP4.
Ricevi del codice Chisel 3 e devi identificare errori precisi.

CHECKLIST DA VERIFICARE:
  [ ] Import: chisel3._, chisel3.util._ e mxfp4._ presenti
  [ ] Il Bundle MXFP4 NON viene ridefinito (deve solo essere importato da mxfp4._,
      è già definito con: sign (Bool), exp (UInt(2.W)), mant (UInt(1.W)))
  [ ] Ogni modulo estende Module
  [ ] IO Bundle dichiarato con val io = IO(new Bundle { ... })
  [ ] Assegnazioni usano := non =
  [ ] Wire dichiarati prima dell'uso
  [ ] Parentesi graffe bilanciate
  [ ] Nessuna sintassi Scala non supportata in Chisel 3
  [ ] Nessun operatore ^/&/| usato direttamente su un segnale di tipo MXFP4
      (è un Bundle, non ha operatori bitwise — errore "value ^ is not a
      member of mxfp4.MXFP4"): l'addizione tra valori MXFP4 deve decodificare
      i campi, allineare, sommare con segno e ricodificare, non usare XOR/AND/OR
      sui bit grezzi
  [ ] "MXFP4(bits)" (con un Int) usato SOLO per valori letterali costanti, MAI
      per dichiarare porte/segnali ("Input(MXFP4(bits))" o "new MXFP4(bits)"
      sono entrambi errati — per le porte serve "new MXFP4"/"MXFP4()" senza argomenti)
  [ ] Nessun import o riferimento a librerie inesistenti (oltre a chisel3 e mxfp4)

REGOLA IMPORTANTE: la checklist sopra è la SOLA base per dire ISSUES. Se il
codice compila, rispetta ogni punto della checklist ed è funzionalmente
corretto, rispondi PASS anche se pensi che si potrebbe scrivere in modo più
chiaro, più efficiente, con nomi migliori o più commentato: suggerimenti di
stile, leggibilità o "best practice" NON sono un motivo valido per ISSUES.
Un ciclo di revisione che continua a proporre piccole riscritture su codice
già corretto non converge mai ed è un difetto, non una revisione accurata.

Se il codice supera tutti i controlli, rispondi ESATTAMENTE (solo questo):
PASS

Se (e solo se) manca un punto della checklist sopra, rispondi ESATTAMENTE in
questo formato:
ISSUES
- [riga o blocco] descrizione problema 1
- [riga o blocco] descrizione problema 2
...
"""

SYSTEM_FIXER_CHISEL = """\
Sei un esperto Chisel 3 che corregge codice hardware con errori.
Ricevi il codice difettoso e una lista di issues da risolvere.

REGOLE:
1. Correggi TUTTI gli errori elencati senza eccezioni
2. Non introdurre nuovi errori
3. Mantieni la stessa logica funzionale dell'originale
4. Il codice output deve essere completo (non troncare)
5. Rispetta le stesse regole del Coder:
   - import chisel3._, chisel3.util._ e mxfp4._
   - Bundle MXFP4 (sign/exp/mant) SOLO importato da mxfp4._, mai ridefinito
   - "new MXFP4"/"MXFP4()" per dichiarare porte/segnali (template di tipo),
     "MXFP4(bits)" con un Int SOLO per valori letterali costanti — mai
     "Input(MXFP4(bits))" né "new MXFP4(bits)"
   - := per assegnazioni
   - Commenti in italiano
   - Porte logiche elementari (XOR/AND/OR/NOT) con operatori diretti (^, &, |, !)
     SOLO su segnali UInt/Bool, MAI "Module(new XOR)" ecc (non esistono).
6. Un segnale MXFP4 è un Bundle: NON ha operatori ^/&/| (errore "value ^ is
   not a member of mxfp4.MXFP4"). Se il codice da correggere somma valori
   MXFP4 con XOR/AND/OR sui bit grezzi, è quello l'errore di fondo da
   correggere anche se non è nella lista di issues: sostituisci con questo
   algoritmo già validato (compila con sbt, 0 discrepanze contro un modello
   di riferimento round-to-nearest su tutte le 256 combinazioni possibili):

   def magX2(exp: UInt, mant: UInt): UInt =
     Mux(exp === 0.U, mant, (2.U +& mant) << (exp - 1.U))

   val a_mag = magX2(io.a.exp, io.a.mant)
   val b_mag = magX2(io.b.exp, io.b.mant)
   val a_signed = Mux(io.a.sign, 0.S -& a_mag.zext, a_mag.zext)
   val b_signed = Mux(io.b.sign, 0.S -& b_mag.zext, b_mag.zext)
   val sum_signed = a_signed +& b_signed
   val out_sign = sum_signed < 0.S
   val sum_abs  = sum_signed.abs.asUInt

   val out_exp  = Wire(UInt(2.W))
   val out_mant = Wire(UInt(1.W))
   when (sum_abs >= 11.U) {
     out_exp := 3.U; out_mant := 1.U   // 6.0
   }.elsewhen (sum_abs >= 8.U) {
     out_exp := 3.U; out_mant := 0.U   // 4.0
   }.elsewhen (sum_abs >= 6.U) {
     out_exp := 2.U; out_mant := 1.U   // 3.0
   }.elsewhen (sum_abs >= 4.U) {
     out_exp := 2.U; out_mant := 0.U   // 2.0
   }.elsewhen (sum_abs >= 3.U) {
     out_exp := 1.U; out_mant := 1.U   // 1.5
   }.elsewhen (sum_abs >= 2.U) {
     out_exp := 1.U; out_mant := 0.U   // 1.0
   }.elsewhen (sum_abs >= 1.U) {
     out_exp := 0.U; out_mant := 1.U   // 0.5
   }.otherwise {
     out_exp := 0.U; out_mant := 0.U   // 0
   }
   // <uscita>.sign := out_sign; <uscita>.exp := out_exp; <uscita>.mant := out_mant

   Adatta i nomi dei segnali (io.a/io.b) al modulo da correggere. Se ci sono
   più di due operandi MXFP4 (es. Cin), applica l'algoritmo in sequenza.
7. Se gli errori derivano da un'esecuzione di test falliti su Verilator
   (compilazione o simulazione), correggi la logica del modulo affinché il
   comportamento simulato corrisponda a quello atteso dal testbench, senza
   modificare l'interfaccia io se non strettamente necessario.

FORMATO OBBLIGATORIO DELLA RISPOSTA:
Prima riga: "Diagnosi: " seguito da UNA frase che spiega la causa radice
dell'errore (non un elenco, non una ripetizione della lista di issues).
Poi una riga vuota, poi SOLO il codice Chisel completo e corretto del modulo.
Nessun markdown (no ```), nessun altro testo oltre alla riga di diagnosi e al codice.
"""

SYSTEM_TESTER_CHISEL = """\
Sei un esperto di ChiselTest e ScalaTest per la verifica di circuiti hardware,
simulati tramite il backend Verilator (non Treadle).
Ricevi un modulo Chisel MXFP4 e devi generare un testbench completo che
esegue la simulazione REALE tramite Verilator.

STRUTTURA OBBLIGATORIA:
  import chisel3._
  import chiseltest._
  import chiseltest.simulator.VerilatorBackendAnnotation
  import mxfp4._
  import org.scalatest.flatspec.AnyFlatSpec

  class NomeModuloTest extends AnyFlatSpec with ChiselScalatestTester {
    behavior of "NomeModulo"

    it should "descrizione test" in {
      test(new NomeModulo).withAnnotations(Seq(VerilatorBackendAnnotation)) { dut =>
        // test cases
      }
    }
  }

REGOLE OBBLIGATORIE:
1. OGNI blocco "test(new NomeModulo)" DEVE avere
   ".withAnnotations(Seq(VerilatorBackendAnnotation))": i test devono
   girare su simulazione Verilator, non sul backend di default.
2. NON ridefinire "class MXFP4" o "object MXFP4": è già fornito da
   "import mxfp4._" (Bundle con sign/exp/mant). Per creare un valore MXFP4
   letterale nei test usa "MXFP4(bits)" con i 4 bit codificati (0..15), es.
   "dut.io.a.poke(MXFP4(3))" o "dut.io.sum.expect(MXFP4(5))" — NON
   "MXFP4(bits).U" e NON passare un Double: MXFP4(bits) prende già i bit
   codificati e ritorna un letterale del Bundle, pronto per poke/expect.
   MXFP4.encode(Double)/decode(Int) restano disponibili per calcolare a mano
   il valore atteso di un'operazione prima di passarlo a MXFP4(...).
3. Per le asserzioni usa SEMPRE il metodo nativo "dut.io.<segnale>.expect(...)"
   di ChiselTest (con "MXFP4(bits)" per segnali di tipo MXFP4, "valore.U" per
   segnali UInt semplici), MAI "assert(dut.io.<segnale>.peek().litValue() === ...)":
   "litValue" è un valore, non un metodo, e chiamarlo con le parentesi
   "litValue()" causa l'errore di compilazione "BigInt does not take
   parameters" — un pattern che genera sempre questo errore, quindi va evitato
   del tutto, non solo corretto togliendo le parentesi.
4. NON usare "chisel3.iotesters.PeekPokeTester" (API deprecata, rimossa da
   anni, incompatibile con questo progetto): SOLO ChiselTest/ScalaTest come
   nella struttura obbligatoria sopra (AnyFlatSpec + ChiselScalatestTester).
5. NON ridichiarare "class NomeModulo extends Module { ... }": il modulo
   esiste già in un file separato e ti viene passato come contesto, non va
   ripetuto nel testbench. Usalo solo referenziandolo in "test(new NomeModulo)".
   Ripeterne la definizione causa un errore di simbolo duplicato in
   compilazione (il modulo verrebbe definito due volte in due file diversi).

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

# Bundle MXFP4 canonico, scritto a mano (non generato dagli LLM) così la sua
# correttezza non dipende dal modello scelto. Coder/Fixer/Tester lo
# importano sempre con "import mxfp4._" invece di ridefinirlo ogni volta.
MXFP4_SCALA = """\
package mxfp4

import chisel3._
import chisel3.experimental.BundleLiterals._

// Formato MXFP4 E2M1 (OCP MX Specification v1.0): 4 bit totali.
//   bit[3]   = segno      (0 = positivo, 1 = negativo)
//   bit[2:1] = esponente a 2 bit (bias = 1)
//   bit[0]   = mantissa a 1 bit
// Valore: (-1)^sign * 2^(exp-1) * (1 + mant*0.5)
class MXFP4 extends Bundle {
  val sign = Bool()
  val exp  = UInt(2.W)
  val mant = UInt(1.W)
}

object MXFP4 {
  def apply(): MXFP4 = new MXFP4

  def apply(bits: Int): MXFP4 = {
    val s = (bits >> 3) & 0x1
    val e = (bits >> 1) & 0x3
    val m = bits & 0x1
    (new MXFP4).Lit(_.sign -> (s == 1).B, _.exp -> e.U(2.W), _.mant -> m.U(1.W))
  }

  def decode(bits: Int): Double = {
    val sign = (bits >> 3) & 0x1
    val exp  = (bits >> 1) & 0x3
    val mant = bits & 0x1
    val s = if (sign == 1) -1.0 else 1.0
    if (exp == 0) {
      s * mant * 0.5
    } else {
      s * (1.0 + mant * 0.5) * math.pow(2.0, exp - 1)
    }
  }

  def encode(value: Double): Int = {
    var bestBits = 0
    var bestDiff = Double.MaxValue
    for (bits <- 0 until 16) {
      val diff = math.abs(decode(bits) - value)
      if (diff < bestDiff) {
        bestDiff = diff
        bestBits = bits
      }
    }
    bestBits
  }
}
"""

BUILD_SBT = """\
scalaVersion := "2.13.12"

libraryDependencies ++= Seq(
  "org.chipsalliance" %% "chisel"     % "6.5.0",
  "edu.berkeley.cs"   %% "chiseltest" % "6.0.0" % "test",
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
"""

CRITICAL_REMINDERS_CHISEL = (
    "Promemoria (regole spesso dimenticate a questa temperatura più alta):\n"
    "- NON usare chisel3.Driver né 'object X extends App': non servono e non esistono più.\n"
    "- Usa SEMPRE 'import mxfp4._' e dichiara ingressi/uscite MXFP4 con 'new MXFP4', mai UInt semplice.\n"
    "- NON istanziare 'Module(new XOR/AND/OR)': non esistono.\n"
    "- Un segnale MXFP4 è un Bundle: NON ha operatori ^/&/|. Per sommare valori MXFP4 usa "
    "l'algoritmo decodifica/allinea/somma/arrotonda-satura descritto nelle regole, non XOR/AND/OR.\n\n"
)

# Base di conoscenza di errori Chisel/Scala osservati per davvero durante lo
# sviluppo di questo progetto (Progetto 2.0→5.0.py), con il suggerimento che
# li risolve — idea di RTLFixer (RAG su una KB di errori comuni) adattata in
# scala ridotta: niente embedding/vector store, solo matching per
# sottostringa, adeguato per una manciata di pattern noti in un dominio
# ristretto (Chisel + MXFP4).
CHISEL_KNOWLEDGE_BASE = [
    {
        "triggers": ["chisel3.driver", "object driver", "driver.execute"],
        "hint": ("chisel3.Driver è stato rimosso nelle versioni recenti di Chisel (6.x): "
                 "non serve per compilare un modulo, NON importarlo e NON aggiungere un "
                 "'object X extends App' con Driver.execute — un modulo Chisel di libreria "
                 "non ha bisogno di un entry point."),
    },
    {
        "triggers": ["class mxfp4", "object mxfp4", "trait mxfp4"],
        "hint": ("Il Bundle MXFP4 è già definito nel package mxfp4 (import mxfp4._): non "
                 "ridefinire 'class MXFP4' o 'object MXFP4' nel modulo, altrimenti si genera "
                 "un conflitto di simboli duplicati in compilazione."),
    },
    {
        "triggers": ["freespec", "not found: type anyflatspec", "object anyflatspec is not a member"],
        "hint": ("Il testbench deve estendere AnyFlatSpec (org.scalatest.flatspec.AnyFlatSpec) "
                 "con la sintassi 'it should \"...\" in { ... }', non FreeSpec con la sintassi "
                 "'\"...\" in { ... }': sono due stili di ScalaTest incompatibili tra loro."),
    },
    {
        "triggers": ["not found: type", "not found: value"],
        "hint": ("Il nome della classe del modulo usato nel testbench (es. 'test(new "
                 "NomeModulo)') deve corrispondere ESATTAMENTE al nome della classe che "
                 "estende Module nel file del modulo — controlla che non siano stati "
                 "rinominati in modo indipendente."),
    },
    {
        "triggers": ["reassignment to val", "value io is not a member"],
        "hint": ("In Chisel le assegnazioni ai segnali usano SEMPRE ':=', mai '=' (che in "
                 "Scala assegna una val, cosa non permessa) — controlla ogni riga che "
                 "assegna un segnale di io o un Wire."),
    },
    {
        "triggers": ["bigint does not take parameters", "litvalue()"],
        "hint": ("'.litValue()' con le parentesi causa SEMPRE 'BigInt does not take "
                 "parameters' (litValue è un valore, non un metodo, in Scala non si può "
                 "chiamare con parentesi un valore senza parametri). Non basta togliere le "
                 "parentesi: sostituisci ogni 'assert(dut.io.X.peek().litValue() === V)' con "
                 "'dut.io.X.expect(V.U)', il metodo idiomatico di ChiselTest per le asserzioni."),
    },
    {
        "triggers": ["not found: type xor", "not found: type and", "not found: type or",
                     "not found: type not", "module(new xor", "module(new and", "module(new or"],
        "hint": ("Le classi 'XOR'/'AND'/'OR'/'NOT' non esistono: NON istanziare porte logiche "
                 "elementari con 'Module(new XOR)' ecc. Sostituisci ogni istanza con gli "
                 "operatori Chisel diretti sugli operandi: 'a ^ b' per XOR, 'a & b' per AND, "
                 "'a | b' per OR, '!a' per NOT."),
    },
    {
        "triggers": ["not found: value e2m1", "not found: type e2m1", "e2m1()"],
        "hint": ("'E2M1' non è un tipo Chisel a sé: è solo il nome della codifica che il "
                 "Bundle MXFP4 già implementa. Sostituisci ogni 'Output(E2M1())' o "
                 "'Input(E2M1())' con 'Output(new MXFP4)' / 'Input(new MXFP4)'."),
    },
    {
        "triggers": ["iotesters", "peekpoketester", "object iotesters is not a member"],
        "hint": ("'chisel3.iotesters.PeekPokeTester' è un'API deprecata e non disponibile in "
                 "questo progetto (non è nelle dipendenze di build.sbt). Riscrivi il testbench "
                 "con ChiselTest/ScalaTest: 'class NomeTest extends AnyFlatSpec with "
                 "ChiselScalatestTester' e 'test(new NomeModulo).withAnnotations(Seq("
                 "VerilatorBackendAnnotation)) { dut => ... }', con 'dut.io.X.poke(...)' e "
                 "'dut.io.X.expect(...)'."),
    },
    {
        "triggers": ["value ^ is not a member of mxfp4", "value & is not a member of mxfp4",
                     "value | is not a member of mxfp4"],
        "hint": ("Un segnale di tipo MXFP4 è un Bundle: NON ha operatori bitwise ^/&/| "
                 "(quelli esistono solo su UInt/SInt). Per sommare due valori MXFP4 non si "
                 "usano XOR/AND/OR sui bit grezzi: bisogna decodificare i campi (exp/mant) in "
                 "virgola fissa a scala ×2, sommare con segno, poi arrotondare/saturare e "
                 "ricodificare — l'algoritmo completo è nel system prompt del Coder."),
    },
    {
        "triggers": ["no arguments allowed for nullary constructor mxfp4",
                     "not enough arguments for method apply"],
        "hint": ("'new MXFP4(bits)' non è valido: il costruttore della classe MXFP4 non prende "
                 "argomenti. Per un template di tipo (porte/segnali) usa 'new MXFP4' o 'MXFP4()' "
                 "senza argomenti; per un valore letterale costante usa 'MXFP4(bits)' — la "
                 "funzione apply(bits: Int) del companion object, non il costruttore della classe."),
    },
    {
        "triggers": ["cannot be applied to ()", "asuint()", "assint()", "asbool()"],
        "hint": ("In Chisel '.asUInt'/'.asSInt'/'.asBool' sono valori, non metodi: chiamarli con "
                 "le parentesi (es. '.asSInt()') causa 'cannot be applied to ()' — stesso problema "
                 "di '.litValue()'. Togli le parentesi: '.asUInt', '.asSInt', '.asBool' senza ()."),
    },
]


def retrieve_hints(text: str, kb: list[dict]) -> str:
    text_low = text.lower()
    matched = [entry["hint"] for entry in kb if any(t in text_low for t in entry["triggers"])]
    if not matched:
        return ""
    bullets = "\n".join(f"- {h}" for h in matched)
    return f"Suggerimenti noti (da errori osservati in precedenza su questo progetto):\n{bullets}\n"


# Estrae codice Scala da una risposta LLM che potrebbe non rispettare
# l'istruzione "solo codice, nessun markdown, nessun testo prima o dopo":
#   1. se c'è un blocco fenced ```scala/``` con codice Chisel riconoscibile,
#      usa quello (l'ultimo, se ce n'è più di uno);
#   2. altrimenti parte dalla prima riga "import chisel3" e taglia alla
#      chiusura bilanciata dell'ultima dichiarazione top-level.
def extract_scala_code(text: str) -> str:
    text = text.strip()

    fences = re.findall(r"```(?:scala)?\s*\n?(.*?)```", text, re.DOTALL)
    scala_fences = [
        f.strip() for f in fences
        if "import chisel3" in f or "extends Module" in f or "extends Bundle" in f
    ]
    if scala_fences:
        return scala_fences[-1]
    if fences:
        return fences[-1].strip()

    start = text.find("import chisel3")
    if start == -1:
        return text

    code = text[start:]
    top_level_kw = ("class ", "object ", "trait ", "import ", "package ")
    depth = 0
    seen_brace = False
    end = len(code)

    for i, ch in enumerate(code):
        if ch == "{":
            depth += 1
            seen_brace = True
        elif ch == "}":
            depth -= 1
            if depth <= 0 and seen_brace:
                rest = code[i + 1:].lstrip("\n\r \t")
                if not any(rest.startswith(kw) for kw in top_level_kw):
                    end = i + 1
                    break
                depth = 0
                seen_brace = False

    return code[:end].strip()


# Il Tester a volte ri-genera per intero il modulo insieme alla classe di
# test, producendo un simbolo duplicato in compilazione: il modulo esiste
# già nel suo file separato. Rimuove quella ridefinizione accidentale con la
# stessa logica di bilanciamento delle graffe di extract_scala_code.
def strip_duplicate_module_scala(text: str, module_name: str) -> str:
    if not module_name:
        return text
    pattern = re.compile(r"class\s+" + re.escape(module_name) + r"\b[^\n{]*\{")
    m = pattern.search(text)
    if not m:
        return text

    depth = 0
    i = m.end() - 1
    while i < len(text):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return (text[:m.start()] + text[i + 1:]).strip()
        i += 1
    return text


class ChiselBackend(HDLBackend):
    key = "chisel"
    display_name = "Chisel 3 (Scala)"

    def __init__(self):
        self.sbt_available = (shutil.which("sbt") is not None or
                               shutil.which("sbt.bat") is not None)
        self.verilator_available = shutil.which("verilator") is not None
        self.use_wsl_verilator   = False
        if not self.verilator_available:
            wsl_verilator = wsl_which("verilator")
            if wsl_verilator:
                self.verilator_available = True
                self.use_wsl_verilator   = True

    @property
    def selector_blurb(self) -> str:
        return (
            "genera FIRRTL/Verilog, è l'ecosistema nativo dei generatori RISC-V "
            "più diffusi (Rocket Chip/Chipyard), ha un type system rigido (Bundle) "
            "che intercetta a compile-time molti errori di bit-layout su un formato "
            "a 4 bit fisso come MXFP4, ed è verificato qui con simulazione "
            "cycle-accurate reale (ChiselTest + backend Verilator)."
        )

    @property
    def plan_type_vocab(self) -> str:
        return "MXFP4|UInt|SInt|Bool"

    @property
    def critical_reminders(self) -> str:
        return CRITICAL_REMINDERS_CHISEL

    @property
    def compile_check_label(self) -> str:
        return "sbt compile"

    @property
    def test_check_label(self) -> str:
        return "sbt test (Verilator)"

    @property
    def file_manifest(self) -> list[tuple[str, str]]:
        return [
            ("src/main/scala/mxfp4/MXFP4.scala", "Bundle MXFP4 condiviso (sign/exp/mant + encode/decode)"),
            ("src/main/scala/<Modulo>.scala",     "Modulo Chisel MXFP4"),
            ("src/test/scala/<Modulo>Test.scala", "Testbench ChiselTest (backend Verilator)"),
            ("build.sbt",                         "Progetto SBT"),
        ]

    @property
    def run_instructions(self) -> str:
        return "sbt test"

    def prompts(self) -> dict[str, str]:
        return {
            "coder": SYSTEM_CODER_CHISEL, "reviewer": SYSTEM_REVIEWER_CHISEL,
            "fixer": SYSTEM_FIXER_CHISEL, "tester": SYSTEM_TESTER_CHISEL,
        }

    def extract_code(self, text: str) -> str:
        return extract_scala_code(text)

    def strip_duplicate_module(self, text: str, module_name: str) -> str:
        return strip_duplicate_module_scala(text, module_name)

    # Verifica deterministica (non-LLM) che il codice usi davvero il Bundle
    # MXFP4, invece di fidarsi ciecamente del giudizio del Reviewer. Run
    # reali hanno mostrato il Coder generare un full adder UInt semplice
    # nonostante il piano richiedesse esplicitamente ingressi/uscite MXFP4,
    # senza che il Reviewer lo segnalasse.
    def check_uses_shared_types(self, code: str) -> bool:
        low = code.lower()
        return "mxfp4._" in low or "new mxfp4" in low

    def retrieve_hints(self, text: str) -> str:
        return retrieve_hints(text, CHISEL_KNOWLEDGE_BASE)

    def _new_project(self, stem: str) -> Path:
        tmp = Path(tempfile.gettempdir()) / \
            f"chisel_check_{stem}_{datetime.datetime.now().strftime('%H%M%S%f')}"
        tmp.mkdir(parents=True, exist_ok=True)
        (tmp / "build.sbt").write_text(BUILD_SBT, encoding="utf-8")
        proj_dir = tmp / "project"
        proj_dir.mkdir(exist_ok=True)
        (proj_dir / "build.properties").write_text("sbt.version=1.10.7\n", encoding="utf-8")
        mxfp4_dir = tmp / "src" / "main" / "scala" / "mxfp4"
        mxfp4_dir.mkdir(parents=True, exist_ok=True)
        (mxfp4_dir / "MXFP4.scala").write_text(MXFP4_SCALA, encoding="utf-8")
        (tmp / "src" / "main" / "scala").mkdir(parents=True, exist_ok=True)
        return tmp

    def _run_sbt(self, args: list[str], cwd: Path, timeout: int,
                 via_wsl: bool) -> subprocess.CompletedProcess:
        if via_wsl:
            wsl_dir = to_wsl_path(cwd)
            cmd_str = "cd " + shlex.quote(wsl_dir) + " && sbt " + " ".join(args)
            return run_in_wsl(cmd_str, timeout)
        return subprocess.run(
            ["sbt", *args], cwd=cwd,
            capture_output=True, text=True, timeout=timeout, shell=True
        )

    def check_module(self, code: str, stem: str) -> tuple[bool, str]:
        if not self.sbt_available:
            return True, "sbt non trovato — compilazione reale saltata"
        tmp = self._new_project(stem)
        try:
            (tmp / "src" / "main" / "scala" / f"{stem}.scala").write_text(code, encoding="utf-8")
            result = self._run_sbt(["compile"], tmp, timeout=180, via_wsl=False)
            output  = (result.stdout + result.stderr).strip()
            return result.returncode == 0, output
        except subprocess.TimeoutExpired:
            return False, "sbt compile timeout (>180s)"
        except Exception as e:
            return False, str(e)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def run_tests(self, code: str, testbench: str, stem: str, plan: dict) -> tuple[bool, str]:
        if not self.sbt_available:
            return True, "sbt non trovato — esecuzione test saltata"
        if not self.verilator_available:
            return True, ("verilator non trovato (né nativamente né in WSL) — esecuzione test "
                           "saltata (richiesto dal backend VerilatorBackendAnnotation del testbench)")
        tmp = self._new_project(stem)
        try:
            (tmp / "src" / "main" / "scala" / f"{stem}.scala").write_text(code, encoding="utf-8")
            test_dir = tmp / "src" / "test" / "scala"
            test_dir.mkdir(parents=True, exist_ok=True)
            (test_dir / f"{stem}Test.scala").write_text(testbench, encoding="utf-8")
            result = self._run_sbt(["test"], tmp, timeout=300, via_wsl=self.use_wsl_verilator)
            output  = (result.stdout + result.stderr).strip()
            return result.returncode == 0, output
        except subprocess.TimeoutExpired:
            return False, "sbt test (Verilator) timeout (>300s)"
        except Exception as e:
            return False, str(e)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def toolchain_status(self) -> list[tuple[bool, str]]:
        lines = []
        if self.sbt_available:
            lines.append((True, "sbt trovato → compilazione reale abilitata"))
        else:
            lines.append((False, "sbt non trovato → solo LLM review (installa sbt da https://www.scala-sbt.org)"))
        if self.sbt_available and self.verilator_available:
            where = "via bridge WSL" if self.use_wsl_verilator else "nativamente"
            lines.append((True, f"verilator trovato ({where}) → simulazione reale abilitata"))
        elif self.sbt_available:
            lines.append((False, "verilator non trovato (né nativamente né in WSL) → esecuzione test saltata"))
        return lines

    def save(self, out_dir: Path, stem: str, code: str, testbench: str) -> None:
        src_main  = out_dir / "src" / "main" / "scala"
        src_test  = out_dir / "src" / "test" / "scala"
        mxfp4_dir = src_main / "mxfp4"
        mxfp4_dir.mkdir(parents=True, exist_ok=True)
        src_test.mkdir(parents=True, exist_ok=True)
        (mxfp4_dir / "MXFP4.scala").write_text(MXFP4_SCALA, encoding="utf-8")
        (src_main / f"{stem}.scala").write_text(code, encoding="utf-8")
        (src_test / f"{stem}Test.scala").write_text(testbench, encoding="utf-8")
        (out_dir / "build.sbt").write_text(BUILD_SBT, encoding="utf-8")


# ═══════════════════════════════════════════════════════════════════════
#  BACKEND 2 — Amaranth HDL (Python). Nessun toolchain esterno obbligatorio:
#  l'elaborazione e la simulazione (pysim) girano nello stesso interprete
#  Python del progetto. Se 'amaranth-yosys' è installato, il Verilog
#  esportato viene anche passato a `verilator --lint-only` (nativo o via
#  WSL) come controllo aggiuntivo — un lint statico, non una simulazione
#  cycle-accurate: non richiede `make`, quindi resta disponibile anche
#  quando il toolchain di build di Verilator non è utilizzabile (vedi nota
#  su `make` mancante in WSL in run_diagnostics()/toolchain_status()).
#  Layout MXFP4Layout, decode/encode ed algoritmo di addizione ×2 validati
#  in questa sessione: 256/256 combinazioni corrispondono al modello
#  round-to-nearest, sia in Python puro sia in una simulazione Amaranth
#  reale (amaranth.sim.Simulator).
# ═══════════════════════════════════════════════════════════════════════

SYSTEM_CODER_AMARANTH = """\
Sei un esperto di Amaranth HDL (Python) e di formati numerici a bassa precisione.
Devi implementare un'unità aritmetica hardware in Amaranth con supporto MXFP4.

REGOLE OBBLIGATORIE:
1. Prima riga: from amaranth import Module, Signal, Elaboratable, Mux, signed
   Seconda riga: from mxfp4 import MXFP4Layout, decode, encode
2. Il layout MXFP4Layout (mant: 1 bit, exp: 2 bit, sign: 1 bit) è GIÀ
   DEFINITO nel modulo mxfp4 fornito dal toolchain. NON ridefinire mai
   "class MXFP4Layout". Per dichiarare una porta/segnale MXFP4 usa
   "Signal(MXFP4Layout)".
3. Il modulo è una classe che estende Elaboratable:
     - "__init__(self)" (SENZA argomenti oltre self) dichiara gli attributi
       Signal che sono le porte di I/O.
     - "elaborate(self, platform)" costruisce "m = Module()", aggiunge la
       logica con "m.d.comb += ..." (combinatoria) o "m.d.sync += ..."
       (sequenziale, clock/reset impliciti), e ritorna "m".
4. La classe DEVE definire un attributo di classe "PORTS: list[str]" con i
   nomi (stringa) di TUTTI gli attributi Signal che sono porte di I/O, nello
   stesso ordine della specifica — il toolchain lo usa per istanziare,
   testare ed esportare il modulo senza dover indovinare i nomi.
5. Le assegnazioni dentro "m.d.comb += ..." / "m.d.sync += ..." usano SEMPRE
   ".eq(valore)", mai "=" diretto su un Signal. Più assegnazioni nello
   stesso blocco: "m.d.comb += [a.eq(x), b.eq(y)]".
6. Per accedere ai campi di un segnale MXFP4Layout: "<segnale>.sign",
   "<segnale>.exp", "<segnale>.mant" (validi sia in lettura sia come target
   di ".eq(...)"). Per leggere/scrivere l'INTERO segnale come intero a 4 bit
   (0..15) invece dei singoli campi, usa "<segnale>.as_value()".
7. Nomi inglesi snake_case per segnali/variabili, PascalCase per la classe.
8. Commenta ogni blocco logico in italiano (utile per la tesi).
9. Nessuna libreria esterna oltre ad "amaranth" e al modulo "mxfp4" fornito.
10. Il codice deve essere COMPLETO ed ESEGUIBILE.
11. Porte logiche elementari (XOR, AND, OR, NOT) su Signal semplici si
    scrivono con gli operatori diretti (^, &, |, ~), MAI istanziando
    sotto-moduli per porte singole (non esistono classi apposite).
12. ATTENZIONE — ampiezza di shift: Amaranth richiede che l'ampiezza di uno
    shift "<<"/">>" sia un valore letterale o comunque tipizzato UNSIGNED.
    Un'espressione come "x << (exp - 1)" viene rifiutata a compile-time con
    "TypeError: Shift amount must be unsigned" anche se "exp - 1" è
    matematicamente sempre >= 0 su quel ramo, perché Amaranth tipizza
    staticamente il risultato di una sottrazione fra Signal come signed.
    SOLUZIONE: enumera i casi con Mux annidati usando ampiezze di shift
    LETTERALI (interi Python), mai il risultato di un'operazione fra Signal
    — vedi l'esempio del punto 13.
13. ADDIZIONE MXFP4: quando la specifica chiede di sommare due valori MXFP4,
    NON usare XOR/AND/OR sui bit grezzi. Usa questo algoritmo, già validato
    (0 discrepanze contro un modello di riferimento round-to-nearest su
    tutte le 256 combinazioni possibili di operandi MXFP4, verificato sia in
    Python puro sia in una simulazione Amaranth reale):

    def mag_x2(exp, mant):
        return Mux(exp == 0, mant,
               Mux(exp == 1, (2 + mant),
               Mux(exp == 2, (2 + mant) << 1,
                             (2 + mant) << 2)))

    a_mag = Signal(4); b_mag = Signal(4)
    m.d.comb += a_mag.eq(mag_x2(io_a.exp, io_a.mant))
    m.d.comb += b_mag.eq(mag_x2(io_b.exp, io_b.mant))

    a_signed = Signal(signed(6)); b_signed = Signal(signed(6))
    m.d.comb += a_signed.eq(Mux(io_a.sign, -a_mag, a_mag))
    m.d.comb += b_signed.eq(Mux(io_b.sign, -b_mag, b_mag))

    s = Signal(signed(7))
    m.d.comb += s.eq(a_signed + b_signed)

    s_abs = Signal(6)
    with m.If(s < 0):
        m.d.comb += s_abs.eq(-s)
    with m.Else():
        m.d.comb += s_abs.eq(s)

    with m.If(s < 0):
        m.d.comb += uscita.sign.eq(1)
    with m.Else():
        m.d.comb += uscita.sign.eq(0)

    with m.If(s_abs >= 11):
        m.d.comb += [uscita.exp.eq(3), uscita.mant.eq(1)]   # 6.0
    with m.Elif(s_abs >= 8):
        m.d.comb += [uscita.exp.eq(3), uscita.mant.eq(0)]   # 4.0
    with m.Elif(s_abs >= 6):
        m.d.comb += [uscita.exp.eq(2), uscita.mant.eq(1)]   # 3.0
    with m.Elif(s_abs >= 4):
        m.d.comb += [uscita.exp.eq(2), uscita.mant.eq(0)]   # 2.0
    with m.Elif(s_abs >= 3):
        m.d.comb += [uscita.exp.eq(1), uscita.mant.eq(1)]   # 1.5
    with m.Elif(s_abs >= 2):
        m.d.comb += [uscita.exp.eq(1), uscita.mant.eq(0)]   # 1.0
    with m.Elif(s_abs >= 1):
        m.d.comb += [uscita.exp.eq(0), uscita.mant.eq(1)]   # 0.5
    with m.Else():
        m.d.comb += [uscita.exp.eq(0), uscita.mant.eq(0)]   # 0

    Adatta i nomi (io_a/io_b/uscita) ai segnali reali della specifica. Se ci
    sono più di due operandi MXFP4, applica l'algoritmo in sequenza.

Rispondi con SOLO il codice Python. Non usare markdown (no ```), nessun
testo prima o dopo il codice.
"""

SYSTEM_REVIEWER_AMARANTH = """\
Sei un revisore esperto di codice Amaranth HDL per unità aritmetiche MXFP4.
Ricevi del codice Amaranth (Python) e devi identificare errori precisi.

CHECKLIST DA VERIFICARE:
  [ ] Import: "from amaranth import ..." e "from mxfp4 import MXFP4Layout,
      decode, encode" presenti
  [ ] "MXFP4Layout" NON viene ridefinito (deve solo essere importato da mxfp4)
  [ ] La classe del modulo estende Elaboratable, ha "__init__(self)" con i
      Signal di I/O e "elaborate(self, platform)" che ritorna "m"
  [ ] La classe definisce l'attributo di classe "PORTS" con i nomi di TUTTI
      i segnali di I/O
  [ ] Le assegnazioni dentro m.d.comb/m.d.sync usano ".eq(...)", mai "="
  [ ] Nessuno shift "<<"/">>" con ampiezza pari al risultato di una
      sottrazione fra Signal (causa "Shift amount must be unsigned") — solo
      ampiezze letterali o Mux fra ampiezze letterali
  [ ] L'addizione fra valori MXFP4 non usa XOR/AND/OR sui bit grezzi, ma
      l'algoritmo decodifica/allinea/somma/arrotonda-satura
  [ ] Nessun import o riferimento a librerie inesistenti (oltre ad amaranth
      e mxfp4)
  [ ] Sintassi/indentazione Python valide, parentesi bilanciate

REGOLA IMPORTANTE: la checklist sopra è la SOLA base per dire ISSUES. Se il
codice è sintatticamente valido, rispetta ogni punto della checklist ed è
funzionalmente corretto, rispondi PASS anche se pensi che si potrebbe
scrivere in modo più chiaro, più efficiente o più commentato: suggerimenti
di stile o "best practice" NON sono un motivo valido per ISSUES. Un ciclo di
revisione che continua a proporre piccole riscritture su codice già corretto
non converge mai ed è un difetto, non una revisione accurata.

Se il codice supera tutti i controlli, rispondi ESATTAMENTE (solo questo):
PASS

Se (e solo se) manca un punto della checklist sopra, rispondi ESATTAMENTE in
questo formato:
ISSUES
- [riga o blocco] descrizione problema 1
- [riga o blocco] descrizione problema 2
...
"""

SYSTEM_FIXER_AMARANTH = """\
Sei un esperto Amaranth HDL che corregge codice hardware Python con errori.
Ricevi il codice difettoso e una lista di issues da risolvere.

REGOLE:
1. Correggi TUTTI gli errori elencati senza eccezioni
2. Non introdurre nuovi errori
3. Mantieni la stessa logica funzionale dell'originale
4. Il codice output deve essere completo (non troncare)
5. Rispetta le stesse regole del Coder:
   - from amaranth import Module, Signal, Elaboratable, Mux, signed
   - from mxfp4 import MXFP4Layout, decode, encode
   - MXFP4Layout SOLO importato da mxfp4, mai ridefinito
   - La classe DEVE avere l'attributo di classe PORTS con i nomi dei Signal
     di I/O
   - ".eq(valore)" per le assegnazioni in m.d.comb/m.d.sync, mai "="
   - Commenti in italiano
6. Uno shift "<<"/">>" con ampiezza pari al risultato di una sottrazione fra
   Signal (es. "x << (exp - 1)") causa SEMPRE "TypeError: Shift amount must
   be unsigned", anche quando l'ampiezza è matematicamente sempre >= 0 su
   quel ramo. Se è quello l'errore, sostituisci con Mux annidati che usano
   ampiezze di shift letterali (interi Python), come nell'esempio del punto 7.
7. Se il codice da correggere somma valori MXFP4 con XOR/AND/OR sui bit
   grezzi, è quello l'errore di fondo da correggere anche se non è nella
   lista di issues: sostituisci con questo algoritmo già validato (0
   discrepanze contro un modello di riferimento round-to-nearest su tutte le
   256 combinazioni possibili, sia in Python puro sia in simulazione
   Amaranth reale):

   def mag_x2(exp, mant):
       return Mux(exp == 0, mant,
              Mux(exp == 1, (2 + mant),
              Mux(exp == 2, (2 + mant) << 1,
                            (2 + mant) << 2)))

   a_mag = Signal(4); b_mag = Signal(4)
   m.d.comb += a_mag.eq(mag_x2(io_a.exp, io_a.mant))
   m.d.comb += b_mag.eq(mag_x2(io_b.exp, io_b.mant))

   a_signed = Signal(signed(6)); b_signed = Signal(signed(6))
   m.d.comb += a_signed.eq(Mux(io_a.sign, -a_mag, a_mag))
   m.d.comb += b_signed.eq(Mux(io_b.sign, -b_mag, b_mag))

   s = Signal(signed(7))
   m.d.comb += s.eq(a_signed + b_signed)

   s_abs = Signal(6)
   with m.If(s < 0):
       m.d.comb += s_abs.eq(-s)
   with m.Else():
       m.d.comb += s_abs.eq(s)

   with m.If(s < 0):
       m.d.comb += uscita.sign.eq(1)
   with m.Else():
       m.d.comb += uscita.sign.eq(0)

   with m.If(s_abs >= 11):
       m.d.comb += [uscita.exp.eq(3), uscita.mant.eq(1)]   # 6.0
   with m.Elif(s_abs >= 8):
       m.d.comb += [uscita.exp.eq(3), uscita.mant.eq(0)]   # 4.0
   with m.Elif(s_abs >= 6):
       m.d.comb += [uscita.exp.eq(2), uscita.mant.eq(1)]   # 3.0
   with m.Elif(s_abs >= 4):
       m.d.comb += [uscita.exp.eq(2), uscita.mant.eq(0)]   # 2.0
   with m.Elif(s_abs >= 3):
       m.d.comb += [uscita.exp.eq(1), uscita.mant.eq(1)]   # 1.5
   with m.Elif(s_abs >= 2):
       m.d.comb += [uscita.exp.eq(1), uscita.mant.eq(0)]   # 1.0
   with m.Elif(s_abs >= 1):
       m.d.comb += [uscita.exp.eq(0), uscita.mant.eq(1)]   # 0.5
   with m.Else():
       m.d.comb += [uscita.exp.eq(0), uscita.mant.eq(0)]   # 0

   Adatta i nomi (io_a/io_b/uscita) al modulo da correggere. Se ci sono più
   di due operandi MXFP4, applica l'algoritmo in sequenza.
8. Se gli errori derivano da un'esecuzione di test falliti (simulazione
   pysim o lint Verilator), correggi la logica del modulo affinché il
   comportamento simulato corrisponda a quello atteso dal testbench, senza
   modificare gli attributi PORTS se non strettamente necessario.

FORMATO OBBLIGATORIO DELLA RISPOSTA:
Prima riga: "Diagnosi: " seguito da UNA frase che spiega la causa radice
dell'errore (non un elenco, non una ripetizione della lista di issues).
Poi una riga vuota, poi SOLO il codice Python completo e corretto del modulo.
Nessun markdown (no ```), nessun altro testo oltre alla riga di diagnosi e al codice.
"""

SYSTEM_TESTER_AMARANTH = """\
Sei un esperto di amaranth.sim per la verifica funzionale di circuiti
hardware descritti in Amaranth HDL.
Ricevi un modulo Amaranth MXFP4 e devi generare un testbench Python
completo che esegue una simulazione REALE con amaranth.sim.Simulator.

STRUTTURA OBBLIGATORIA:
  from amaranth.sim import Simulator
  from mxfp4 import decode, encode
  from module import NomeModulo

  dut = NomeModulo()
  sim = Simulator(dut)

  async def bench(ctx):
      ok = True
      # caso 1: descrizione
      ctx.set(dut.<porta>.as_value(), <bits 0..15>)
      got = int(ctx.get(dut.<uscita>.as_value()))
      atteso = encode(decode(<bitsA>) + decode(<bitsB>))   # esempio per un'addizione
      if got != atteso:
          ok = False
          print(f"FAIL caso 1: atteso {atteso}, ottenuto {got}")
      # ... altri casi ...
      if ok:
          print("RISULTATO: PASS")
      else:
          print("RISULTATO: FAIL")
          raise SystemExit(1)

  sim.add_testbench(bench)
  sim.run()

REGOLE OBBLIGATORIE:
1. Import SEMPRE "from module import NomeModulo" (il modulo esiste già in
   module.py, fornito come contesto — NON ridefinire/ridichiarare la classe
   del modulo nel testbench: causa un simbolo duplicato).
2. Per impostare/leggere un intero segnale MXFP4Layout come intero a 4 bit
   usa SEMPRE "ctx.set(dut.<segnale>.as_value(), bits)" e
   "ctx.get(dut.<segnale>.as_value())" — MAI "ctx.set(dut.<segnale>, bits)"
   direttamente su un segnale MXFP4Layout: fallisce con "'int' object is not
   iterable" perché il layout si aspetta un dict di campi, non un intero
   grezzo, se non si passa attraverso ".as_value()".
3. Usa SEMPRE "sim.add_testbench(bench)" con una funzione
   "async def bench(ctx): ...", MAI "sim.add_process" con uno "yield"
   semplice: quell'API è diversa e causa
   "TypeError: Received default command from process...".
4. Alla fine, se e solo se TUTTE le asserzioni sono passate, stampa
   ESATTAMENTE la riga "RISULTATO: PASS"; altrimenti stampa
   "RISULTATO: FAIL" e termina con "raise SystemExit(1)", così il processo
   ritorna un codice di uscita diverso da zero.
5. Usa "decode(bits)"/"encode(value)" (importati da "mxfp4") per calcolare a
   mano il valore Double atteso di un'operazione e i bit attesi da
   confrontare — NON assumere a mente il valore atteso senza calcolarlo con
   decode/encode.
6. Non ridefinire "MXFP4Layout", "decode" o "encode": sono già forniti da
   "from mxfp4 import ...".

CASI DA TESTARE:
  • Caso base (valori tipici)
  • Zero (0x0)
  • Valore massimo rappresentabile in MXFP4
  • Valori negativi (se il modulo li supporta)
  • Overflow/underflow (saturazione)
  • Simmetria (a op b == b op a per operazioni commutative)

Ricorda: in MXFP4 E2M1 il valore massimo è 0b0111 = +6.0

Rispondi con SOLO il codice Python del testbench. Nessun markdown, nessun
testo aggiuntivo.
"""

# Modulo mxfp4 canonico, scritto a mano (non generato dagli LLM) e validato
# esaustivamente in questa sessione: decode()/encode() riproducono la
# tabella OCP E2M1 (0/0.5/1/1.5/2/3/4/6) e l'algoritmo di addizione ×2
# combacia col modello round-to-nearest su tutte le 256 combinazioni,
# verificato sia in Python puro sia in una simulazione Amaranth reale.
MXFP4_PY = '''\
"""Formato MXFP4 E2M1 (OCP MX Specification v1.0): 4 bit totali.

  bit[3]   = segno      (0 = positivo, 1 = negativo)
  bit[2:1] = esponente a 2 bit (bias = 1)
  bit[0]   = mantissa a 1 bit

Valore: (-1)^segno * 2^(exp-1) * (1 + mant*0.5), tranne il caso subnormale
(exp == 0, include lo zero) dove NON c'e' bit implicito:
valore = (-1)^segno * mant*0.5.
"""
from amaranth.lib import data


class MXFP4Layout(data.Struct):
    # Ordine di dichiarazione = ordine dei bit da LSB a MSB in
    # amaranth.lib.data.Struct: mant (bit 0), exp (bit 2:1), sign (bit 3).
    mant: 1
    exp: 2
    sign: 1


def decode(bits: int) -> float:
    """Converte i 4 bit codificati (0..15) nel valore Double rappresentato."""
    sign = (bits >> 3) & 0x1
    exp  = (bits >> 1) & 0x3
    mant = bits & 0x1
    s = -1.0 if sign == 1 else 1.0
    if exp == 0:
        return s * mant * 0.5
    return s * (1.0 + mant * 0.5) * (2.0 ** (exp - 1))


def encode(value: float) -> int:
    """Converte un valore Double nella codifica MXFP4 (0..15) piu' vicina
    (round-to-nearest)."""
    best_bits, best_diff = 0, float("inf")
    for bits in range(16):
        diff = abs(decode(bits) - value)
        if diff < best_diff:
            best_diff, best_bits = diff, bits
    return best_bits
'''

CRITICAL_REMINDERS_AMARANTH = (
    "Promemoria (regole spesso dimenticate a questa temperatura più alta):\n"
    "- La classe DEVE avere l'attributo di classe PORTS con i nomi dei Signal di I/O.\n"
    "- Le assegnazioni in m.d.comb/m.d.sync usano SEMPRE '.eq(...)', mai '='.\n"
    "- Mai uno shift con ampiezza pari al risultato di una sottrazione fra Signal "
    "('x << (exp - 1)' fallisce con 'Shift amount must be unsigned'): usa Mux "
    "annidati con ampiezze letterali.\n"
    "- Per sommare valori MXFP4 usa l'algoritmo decodifica/allinea/somma/"
    "arrotonda-satura descritto nelle regole, non XOR/AND/OR sui bit grezzi.\n\n"
)

# Base di conoscenza per Amaranth: a differenza di CHISEL_KNOWLEDGE_BASE
# (costruita da molti run agentici reali), questa è seeded dai problemi reali
# di API incontrati verificando il backend in questa stessa sessione (vedi
# gli script di validazione) più alcuni pitfall documentati — non ancora da
# un ampio storico di run agentici come quella Chisel. È pensata per
# crescere allo stesso modo, aggiungendo pattern osservati nei run reali.
AMARANTH_KNOWLEDGE_BASE = [
    {
        "triggers": ["'int' object is not iterable", "shape.const"],
        "hint": ("ctx.set(<segnale>, bits) su un segnale MXFP4Layout fallisce perché il "
                 "layout si aspetta un dict di campi, non un intero grezzo. Usa "
                 "ctx.set(<segnale>.as_value(), bits) per impostare/leggere i 4 bit "
                 "codificati come intero."),
    },
    {
        "triggers": ["shift amount must be unsigned"],
        "hint": ("Uno shift '<<'/'>>' con ampiezza pari al risultato di una sottrazione fra "
                 "Signal (es. 'x << (exp - 1)') viene rifiutato perché Amaranth tipizza quel "
                 "risultato come signed anche quando è matematicamente sempre >= 0. Enumera i "
                 "casi con Mux annidati usando ampiezze di shift letterali (interi Python), mai "
                 "il risultato di un'operazione fra Signal."),
    },
    {
        "triggers": ["received default command", "did you mean to use tick"],
        "hint": ("sim.add_process con un 'yield' semplice non è il modo corretto di scrivere un "
                 "testbench in questa versione di Amaranth. Usa sim.add_testbench(bench) con "
                 "'async def bench(ctx): ...' e ctx.set(...)/ctx.get(...)."),
    },
    {
        "triggers": ["modulenotfounderror", "no module named", "importerror"],
        "hint": ("Non importare librerie oltre ad 'amaranth' e al modulo 'mxfp4' fornito: "
                 "MXFP4Layout/decode/encode sono già disponibili con 'from mxfp4 import "
                 "MXFP4Layout, decode, encode', non ridefinirli."),
    },
    {
        "triggers": ["attributeerror", "has no attribute 'ports'", "object has no attribute"],
        "hint": ("La classe del modulo deve definire l'attributo di classe PORTS (lista di "
                 "stringhe con i nomi dei Signal di I/O), altrimenti il toolchain non può "
                 "istanziare/esportare/testare il modulo in modo generico."),
    },
    {
        "triggers": ["takes 1 positional argument", "missing 1 required positional argument",
                     "__init__() missing"],
        "hint": ("Il costruttore della classe del modulo (__init__) non deve richiedere "
                 "argomenti oltre a 'self': il toolchain istanzia sempre il modulo con "
                 "'NomeModulo()', senza argomenti."),
    },
]


# Estrae codice Python da una risposta LLM che potrebbe non rispettare
# l'istruzione "solo codice, nessun markdown, nessun testo prima o dopo":
#   1. se c'è un blocco fenced ```python/``` con codice Amaranth
#      riconoscibile, usa quello (l'ultimo, se ce n'è più di uno);
#   2. altrimenti parte dalla prima riga "from amaranth" o "import amaranth"
#      e prende tutto il resto (il codice Python non ha un delimitatore di
#      fine dichiarazione bilanciato come le graffe Scala, quindi qui non
#      serve — a differenza di extract_scala_code — un bilanciamento
#      esplicito: eventuale prosa finale viene comunque scartata dal motivo
#      3 se preceduta da una riga vuota seguita da testo non-codice).
def extract_python_code(text: str) -> str:
    text = text.strip()

    fences = re.findall(r"```(?:python)?\s*\n?(.*?)```", text, re.DOTALL)
    py_fences = [
        f.strip() for f in fences
        if "amaranth" in f and ("import" in f or "from" in f)
    ]
    if py_fences:
        return py_fences[-1]
    if fences:
        return fences[-1].strip()

    start = -1
    for marker in ("from amaranth", "import amaranth"):
        idx = text.find(marker)
        if idx != -1 and (start == -1 or idx < start):
            start = idx
    if start == -1:
        return text
    return text[start:].strip()


CLASS_NAME_RE = re.compile(r"class\s+([A-Za-z_]\w*)\s*\([^)]*Elaboratable[^)]*\)")


def find_elaboratable_class_name(code: str) -> str | None:
    m = CLASS_NAME_RE.search(code)
    return m.group(1) if m else None


# Il Tester a volte ri-genera per intero il modulo insieme al testbench: lo
# stesso problema osservato sul lato Chisel, qui rimosso rimuovendo il primo
# blocco "class <NomeModulo>(Elaboratable): ..." (fino alla riga successiva
# non indentata) invece di ridefinirlo — causerebbe un simbolo duplicato in
# module.py/testbench.py se il testbench reimportasse se stesso, ed è comunque
# ridondante dato che il modulo è già fornito come contesto separato.
def strip_duplicate_module_python(text: str, module_name: str) -> str:
    if not module_name:
        return text
    pattern = re.compile(r"^class\s+" + re.escape(module_name) + r"\b.*:\s*$", re.MULTILINE)
    m = pattern.search(text)
    if not m:
        return text

    lines = text[m.end():].splitlines(keepends=True)
    end_offset = 0
    for line in lines:
        if line.strip() == "" or line.startswith((" ", "\t")):
            end_offset += len(line)
            continue
        break
    return (text[:m.start()] + text[m.end() + end_offset:]).strip()


class AmaranthBackend(HDLBackend):
    key = "amaranth"
    display_name = "Amaranth HDL (Python)"

    def __init__(self):
        try:
            import amaranth  # noqa: F401
            self.amaranth_available = True
        except ImportError:
            self.amaranth_available = False

        try:
            import amaranth_yosys  # noqa: F401
            self.yosys_available = True
        except ImportError:
            self.yosys_available = shutil.which("yosys") is not None

        self.verilator_available = shutil.which("verilator") is not None
        self.use_wsl_verilator   = False
        if not self.verilator_available:
            wsl_v = wsl_which("verilator")
            if wsl_v:
                self.verilator_available = True
                self.use_wsl_verilator   = True

    @property
    def selector_blurb(self) -> str:
        return (
            "elaborazione nativa in Python (nessun toolchain esterno "
            "obbligatorio), simulatore integrato (amaranth.sim) per iterazione "
            "rapida, stesso linguaggio del resto del progetto (compresi i "
            "modelli di riferimento golden come mxfp4_ref.py) — buona scelta "
            "per un prototipo algoritmico isolato o quando la specifica "
            "menziona esplicitamente Python, ma ecosistema di integrazione "
            "SoC/RISC-V meno maturo di Chisel/Chipyard."
        )

    @property
    def plan_type_vocab(self) -> str:
        return "MXFP4|unsigned|signed|Bool"

    @property
    def critical_reminders(self) -> str:
        return CRITICAL_REMINDERS_AMARANTH

    @property
    def compile_check_label(self) -> str:
        return "elaborazione Amaranth (Fragment.get)"

    @property
    def test_check_label(self) -> str:
        return "simulazione Amaranth (pysim) + lint Verilator"

    @property
    def file_manifest(self) -> list[tuple[str, str]]:
        return [
            ("mxfp4.py",      "Layout MXFP4 condiviso (MXFP4Layout + encode/decode)"),
            ("module.py",     "Modulo Amaranth MXFP4"),
            ("testbench.py",  "Testbench amaranth.sim"),
            ("requirements.txt", "Dipendenze pip"),
        ]

    @property
    def run_instructions(self) -> str:
        return "pip install -r requirements.txt && python testbench.py"

    def prompts(self) -> dict[str, str]:
        return {
            "coder": SYSTEM_CODER_AMARANTH, "reviewer": SYSTEM_REVIEWER_AMARANTH,
            "fixer": SYSTEM_FIXER_AMARANTH, "tester": SYSTEM_TESTER_AMARANTH,
        }

    def extract_code(self, text: str) -> str:
        return extract_python_code(text)

    def strip_duplicate_module(self, text: str, module_name: str) -> str:
        return strip_duplicate_module_python(text, module_name)

    def check_uses_shared_types(self, code: str) -> bool:
        low = code.lower()
        return "mxfp4layout" in low and "ports" in low

    def retrieve_hints(self, text: str) -> str:
        return retrieve_hints(text, AMARANTH_KNOWLEDGE_BASE)

    def _new_project(self, stem: str) -> Path:
        tmp = Path(tempfile.gettempdir()) / \
            f"amaranth_check_{stem}_{datetime.datetime.now().strftime('%H%M%S%f')}"
        tmp.mkdir(parents=True, exist_ok=True)
        (tmp / "mxfp4.py").write_text(MXFP4_PY, encoding="utf-8")
        return tmp

    def check_module(self, code: str, stem: str) -> tuple[bool, str]:
        if not self.amaranth_available:
            return True, "amaranth non installato (pip install amaranth) — controllo di elaborazione saltato"

        class_name = find_elaboratable_class_name(code)
        if not class_name:
            return False, ("ERRORE: nessuna classe 'class NomeModulo(Elaboratable):' trovata "
                            "nel codice generato")

        tmp = self._new_project(stem)
        try:
            (tmp / "module.py").write_text(code, encoding="utf-8")
            runner = textwrap.dedent(f"""\
                from module import {class_name}
                ports = getattr({class_name}, "PORTS", None)
                if not ports:
                    print("ERRORE: la classe non definisce l'attributo di classe PORTS")
                    raise SystemExit(1)
                dut = {class_name}()
                for p in ports:
                    if not hasattr(dut, p):
                        print(f"ERRORE: PORTS elenca '{{p}}' ma l'attributo non esiste sull'istanza")
                        raise SystemExit(1)
                from amaranth.hdl import Fragment
                Fragment.get(dut, platform=None)
                print("ELABORAZIONE OK")
            """)
            (tmp / "_check.py").write_text(runner, encoding="utf-8")
            result = subprocess.run(
                [sys.executable, "_check.py"],
                capture_output=True, text=True, timeout=60, cwd=tmp
            )
            output  = (result.stdout + result.stderr).strip()
            success = result.returncode == 0 and "ELABORAZIONE OK" in output
            return success, output
        except subprocess.TimeoutExpired:
            return False, "elaborazione Amaranth timeout (>60s)"
        except Exception as e:
            return False, str(e)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def _verilator_lint(self, tmp: Path, class_name: str, stem: str) -> tuple[bool, str]:
        export_script = textwrap.dedent(f"""\
            from module import {class_name}
            from amaranth.back import verilog
            dut = {class_name}()
            ports = [getattr(dut, p).as_value() for p in dut.PORTS]
            v = verilog.convert(dut, name="{stem}", ports=ports)
            with open("{stem}.v", "w", encoding="utf-8") as f:
                f.write(v)
            print("VERILOG OK")
        """)
        (tmp / "_export.py").write_text(export_script, encoding="utf-8")
        exp = subprocess.run(
            [sys.executable, "_export.py"],
            capture_output=True, text=True, timeout=60, cwd=tmp
        )
        if exp.returncode != 0 or "VERILOG OK" not in exp.stdout:
            return False, "Esportazione Verilog fallita:\n" + (exp.stdout + exp.stderr).strip()

        # -Wno-DECLFILENAME: il file temporaneo non si chiama come il modulo,
        #   irrilevante qui. -Wno-WIDTHTRUNC/-WIDTHEXPAND: confermato con un
        #   modulo di riferimento noto-corretto (validato contro il modello
        #   golden su tutte le 256 combinazioni) che Yosys, abbassando
        #   MXFP4Layout ad ampiezze di bit eterogenee (2 bit per exp, 1 per
        #   sign/mant), genera sempre confronti/somme a ampiezza mista che
        #   Amaranth stesso già risolve correttamente: sono rumore
        #   strutturale del netlist emesso da Yosys, non un indizio di bug
        #   funzionale, quindi non vanno lasciati bloccare il loop di fix.
        args = ["--lint-only", "-Wall", "-Wno-DECLFILENAME",
                "-Wno-WIDTHTRUNC", "-Wno-WIDTHEXPAND", f"{stem}.v"]
        try:
            if self.use_wsl_verilator:
                wsl_dir = to_wsl_path(tmp)
                cmd_str = "cd " + shlex.quote(wsl_dir) + " && verilator " + " ".join(args)
                result = run_in_wsl(cmd_str, timeout=60)
            else:
                result = subprocess.run(
                    ["verilator", *args], cwd=tmp,
                    capture_output=True, text=True, timeout=60, shell=True
                )
        except subprocess.TimeoutExpired:
            return False, "verilator --lint-only timeout (>60s)"
        output = (result.stdout + result.stderr).strip()
        return result.returncode == 0, output

    def run_tests(self, code: str, testbench: str, stem: str, plan: dict) -> tuple[bool, str]:
        if not self.amaranth_available:
            return True, "amaranth non installato — esecuzione test saltata"

        class_name = find_elaboratable_class_name(code) or plan.get("nome_modulo", stem)
        tmp = self._new_project(stem)
        try:
            (tmp / "module.py").write_text(code, encoding="utf-8")
            (tmp / "testbench.py").write_text(testbench, encoding="utf-8")

            result = subprocess.run(
                [sys.executable, "testbench.py"],
                capture_output=True, text=True, timeout=120, cwd=tmp
            )
            pysim_output = (result.stdout + result.stderr).strip()
            pysim_ok = result.returncode == 0 and "RISULTATO: PASS" in pysim_output
            report = f"=== Simulazione Amaranth (pysim) ===\n{pysim_output}\n"

# Il lint è SOLO informativo, non un gate su 'success': verificato con un
# modulo di riferimento noto-corretto (validato su tutte le 256 combinazioni
# contro il modello golden) che Verilator segnala comunque UNUSEDSIGNAL su
# 'a.exp'/'a.mant'/'a.sign' — wire di alias che Yosys emette per i campi di
# un amaranth.lib.data.Struct e poi ottimizza altrove nel netlist, non un
# segno di logica realmente scollegata. Con MXFP4Layout questo si presenta
# sistematicamente anche su codice perfetto: usarlo come gate renderebbe il
# meccanismo di escape inutile (bloccato su un errore che il Fixer non può
# correggere riscrivendo il modulo).
            if pysim_ok and self.verilator_available and self.yosys_available:
                lint_ok, lint_output = self._verilator_lint(tmp, class_name, stem)
                label = "pulito" if lint_ok else "warning presenti (non bloccanti)"
                report += (f"\n=== Verilator --lint-only (Verilog esportato via Yosys) "
                           f"[informativo: {label}] ===\n{lint_output}\n")
            elif pysim_ok and self.verilator_available:
                report += ("\n=== Verilator --lint-only ===\namaranth-yosys/yosys non "
                            "installato — export Verilog e lint saltati (resta valida la "
                            "simulazione pysim sopra)\n")
            elif pysim_ok:
                report += ("\n=== Verilator --lint-only ===\nverilator non trovato — lint "
                            "saltato (resta valida la simulazione pysim sopra)\n")

            return pysim_ok, report
        except subprocess.TimeoutExpired:
            return False, "simulazione Amaranth timeout (>120s)"
        except Exception as e:
            return False, str(e)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def toolchain_status(self) -> list[tuple[bool, str]]:
        lines = []
        if self.amaranth_available:
            lines.append((True, "amaranth installato → elaborazione e simulazione pysim reali abilitate"))
        else:
            lines.append((False, "amaranth non installato (pip install amaranth) → solo LLM review"))
        if self.amaranth_available and self.verilator_available and self.yosys_available:
            where = "via bridge WSL" if self.use_wsl_verilator else "nativamente"
            lines.append((True, f"verilator trovato ({where}) + amaranth-yosys → lint del Verilog esportato abilitato"))
        elif self.amaranth_available and self.verilator_available:
            lines.append((False, "verilator trovato ma amaranth-yosys/yosys assente → lint saltato (pip install amaranth-yosys)"))
        elif self.amaranth_available:
            lines.append((False, "verilator non trovato → lint del Verilog esportato saltato (resta la simulazione pysim)"))
        return lines

    def save(self, out_dir: Path, stem: str, code: str, testbench: str) -> None:
        (out_dir / "mxfp4.py").write_text(MXFP4_PY, encoding="utf-8")
        (out_dir / "module.py").write_text(code, encoding="utf-8")
        (out_dir / "testbench.py").write_text(testbench, encoding="utf-8")
        (out_dir / "requirements.txt").write_text(
            "amaranth>=0.5\n"
            "amaranth-yosys>=0.50  # opzionale: solo per l'export Verilog + lint Verilator\n",
            encoding="utf-8"
        )


# ═══════════════════════════════════════════════════════════════════════
#  SELECTOR — sceglie il linguaggio Meta-HDL prima che il piano venga
#  scritto (il requisito esplicito del progetto: "l'agente seleziona il
#  linguaggio Meta-HDL appropriato"). Il criterio principale è l'obiettivo
#  finale dichiarato del progetto (integrazione come coprocessore RISC-V),
#  che favorisce Chisel/Chipyard salvo indicazioni contrarie nella specifica.
# ═══════════════════════════════════════════════════════════════════════
SYSTEM_SELECTOR_TEMPLATE = """\
Sei un architetto hardware che deve scegliere il linguaggio Meta-HDL più
adatto per implementare una specifica unità aritmetica MXFP4, prima ancora
che il piano di implementazione venga scritto.

LINGUAGGI DISPONIBILI:

1. "chisel" — Chisel 3 (Scala): {chisel_blurb}

2. "amaranth" — Amaranth HDL (Python): {amaranth_blurb}

CRITERIO DECISIONALE PRINCIPALE: l'obiettivo finale del progetto è integrare
l'unità aritmetica come coprocessore in un processore RISC-V. Chisel è
l'ecosistema nativo di Rocket Chip/Chipyard (i generatori RISC-V più diffusi
che usano Chisel), quindi è la scelta di default più solida quando la
specifica menziona integrazione in un processore, un SoC, una pipeline o un
coprocessore RISC-V. Amaranth è preferibile quando la specifica è più vicina
a un prototipo algoritmico isolato, a iterazione rapida, o menziona
esplicitamente Python — casi in cui la maturità dell'integrazione SoC conta
meno della velocità di sviluppo e della verifica nativa in Python.

Se la specifica non dà indicazioni chiare, preferisci "chisel" (motivazione:
è il linguaggio più maturo per questo dominio in questo progetto e il target
dichiarato resta comunque un coprocessore RISC-V).

Rispondi SOLO con JSON valido, nessun testo prima o dopo, nessun markdown:
{{
  "linguaggio": "chisel|amaranth",
  "motivazione": "una frase che spiega la scelta in base alla specifica"
}}
"""

SYSTEM_PLANNER_TEMPLATE = """\
Sei un esperto di architetture hardware digitale e aritmetica a bassa precisione.
Ricevi una specifica testuale di un'unità aritmetica da implementare in
{lang_display} con formato numerico MXFP4.

CONTESTO MXFP4:
  • Formato E2M1: 4 bit totali
      bit[3]   = segno (0=positivo, 1=negativo)
      bit[2:1] = esponente (2 bit, bias=1)
      bit[0]   = mantissa (1 bit)
  • Shared exponent per blocchi di 32 elementi (OCP MX Specification v1.0)
  • Usato in acceleratori ML per ridurre area e banda

IMPORTANTE — se l'operazione richiesta è un'addizione tra valori MXFP4: NON
pianificare porte logiche elementari (XOR/AND/OR a livello di bit grezzi,
come per un full adder binario classico) come "components" — sommare due
codifiche MXFP4 non è la stessa cosa che sommare due numeri binari, perché
MXFP4 è una codifica floating-point-like (segno/esponente/mantissa). I passi
corretti sono: decodifica dei campi, allineamento in virgola fissa, somma con
segno, normalizzazione/saturazione e ricodifica — pianifica "passi_algoritmo"
in questi termini, non come porte XOR/AND/OR.

Il tuo compito è produrre un piano di implementazione strutturato in JSON.
Rispondi SOLO con il JSON valido. Nessun testo prima o dopo. Nessun markdown.

Schema JSON richiesto:
{{
  "nome_modulo": "NomeInPascalCase",
  "tipo": "combinatorio|sequenziale",
  "descrizione": "descrizione funzionale completa",
  "ingressi": [
    {{"nome": "a",   "tipo": "{type_vocab}", "bit": 4, "descrizione": "..."}}
  ],
  "uscite": [
    {{"nome": "sum", "tipo": "{type_vocab}", "bit": 4, "descrizione": "..."}}
  ],
  "segnali_interni": [
    {{"nome": "carry", "tipo": "{type_vocab}", "bit": 1, "descrizione": "..."}}
  ],
  "passi_algoritmo": [
    "1. Estrai segno, esponente e mantissa dagli ingressi",
    "2. Allinea gli esponenti",
    "..."
  ],
  "bundle_mxfp4_necessario": true,
  "note_mxfp4": "descrizione delle scelte architetturali MXFP4"
}}
"""


def run_selector(spec: str, host: str, model: str, backends: dict[str, HDLBackend]) -> dict:
    step("1", "Selezione del linguaggio Meta-HDL")
    prompt_text = SYSTEM_SELECTOR_TEMPLATE.format(
        chisel_blurb=backends["chisel"].selector_blurb,
        amaranth_blurb=backends["amaranth"].selector_blurb,
    )
    selector = Agent("Selector", prompt_text, host, model)
    raw = selector.run(
        f"Specifica dell'unità da implementare:\n\n{spec}\n\n"
        "Scegli il linguaggio e motiva la scelta in JSON."
    )
    clean = re.sub(r"```json|```", "", raw).strip()
    m = re.search(r"\{.*\}", clean, re.DOTALL)
    if m:
        clean = m.group(0)

    try:
        result = json.loads(clean)
        lang = str(result.get("linguaggio", "")).strip().lower()
        if lang not in backends:
            raise ValueError(f"linguaggio sconosciuto: {lang!r}")
        motivazione = str(result.get("motivazione", "")).strip()
        ok(f"Linguaggio scelto: {BOLD}{backends[lang].display_name}{RESET}")
        info(motivazione or "(nessuna motivazione fornita)")
        return {"linguaggio": lang, "motivazione": motivazione}
    except (json.JSONDecodeError, ValueError) as e:
        warn(f"Risposta del Selector non interpretabile ({e}) — uso 'chisel' come default")
        return {"linguaggio": "chisel",
                "motivazione": "Fallback: risposta del Selector non interpretabile come JSON valido."}


# Il Planner a volte ignora lo schema JSON richiesto e ne inventa uno
# proprio (osservato più volte: "unit_name"/"inputs"/"outputs"/"logic_steps"
# invece di "nome_modulo"/"ingressi"/"uscite"/"passi_algoritmo") — limite
# comune nei modelli locali più piccoli. Senza normalizzazione questo passa
# inosservato: i file vengono salvati con un nome che non corrisponde al
# modulo scritto dal Coder, e "Algoritmo pianificato" nel report resta
# vuoto. Qui si aggiungono le chiavi attese come alias di quelle trovate.
def _normalize_plan(plan: dict, spec: str) -> dict:
    aliases = {
        "nome_modulo":    ["unit_name", "module_name", "name"],
        "tipo":           ["type"],
        "descrizione":    ["description"],
        "ingressi":       ["inputs"],
        "uscite":         ["outputs"],
        "segnali_interni": ["internal_signals", "signals"],
        "passi_algoritmo": ["logic_steps", "algorithm_steps", "steps", "components", "operations"],
    }
    for expected, alts in aliases.items():
        if plan.get(expected):
            continue
        for alt in alts:
            if plan.get(alt):
                plan[expected] = plan[alt]
                break

    if not plan.get("nome_modulo"):
        plan["nome_modulo"] = "MxFp4Unit"
    if not plan.get("descrizione"):
        plan["descrizione"] = spec

    steps = plan.get("passi_algoritmo") or []
    if steps and isinstance(steps[0], dict):
        def _step_to_str(s: dict) -> str:
            label = s.get("step_name") or s.get("name") or "?"
            if s.get("description"):
                return f"{label}: {s['description']}"
            if s.get("type"):
                ins  = ", ".join(s.get("inputs", []))
                outs = ", ".join(s.get("outputs", []))
                return f"{label} ({s['type']}): {ins} → {outs}"
            return str(label)
        plan["passi_algoritmo"] = [_step_to_str(s) for s in steps]

# Il Planner a volte tratta "E2M1" come un tipo distinto da "MXFP4" per gli
# output — ma E2M1 è solo il nome della codifica che MXFP4 implementa, non
# un tipo a sé in nessuno dei due backend.
    for key in ("ingressi", "uscite", "segnali_interni"):
        for port in plan.get(key, []) or []:
            if isinstance(port, dict) and str(port.get("type", "")).strip().lower() == "e2m1":
                port["type"] = "MXFP4"

    return plan


def run_planner(spec: str, agent: Agent) -> dict:
    agent_step("PLANNER", "Analisi della specifica → piano di implementazione JSON")

    raw = agent.run(
        f"Specifica dell'unità da implementare:\n\n{spec}\n\n"
        "Crea il piano JSON completo."
    )

    clean = re.sub(r"```json|```", "", raw).strip()
    m = re.search(r"\{.*\}", clean, re.DOTALL)
    if m:
        clean = m.group(0)

    try:
        plan = json.loads(clean)
        plan = _normalize_plan(plan, spec)
        ok(f"Modulo: '{plan.get('nome_modulo', '?')}'  —  tipo: {plan.get('tipo', '?')}")
        ok(f"Ingressi: {len(plan.get('ingressi', []))}  |  Uscite: {len(plan.get('uscite', []))}")
        if plan.get("passi_algoritmo"):
            ok(f"Algoritmo: {len(plan['passi_algoritmo'])} passi pianificati")
        return plan
    except json.JSONDecodeError as e:
        warn(f"JSON non parsabile ({e}) — continuo con piano testuale")
        return {"nome_modulo": "MxFp4Unit", "tipo": "combinatorio",
                "descrizione": spec, "raw_plan": raw,
                "ingressi": [], "uscite": [], "passi_algoritmo": []}


# Il Coder riceve il piano JSON e genera il codice completo nel linguaggio
# scelto dal Selector. 'retry_note'/'temperature' sono usati dal meccanismo
# di escape (vedi run_review_fix_loop/run_test_fix_loop): quando il loop di
# fix è bloccato sullo stesso errore, invece di continuare a correggere
# in-place si richiama il Coder da zero, con temperatura più alta per un
# candidato davvero diverso (campionamento ad alta temperatura di MAGE).
def run_coder(plan: dict, spec: str, backend: HDLBackend, agent: Agent,
              retry_note: str = "", temperature: float = 0.1) -> str:
    agent_step("CODER", f"Generazione codice {backend.display_name}")

    plan_str = json.dumps(plan, ensure_ascii=False, indent=2)
    hints = backend.retrieve_hints(spec + "\n" + plan_str)
    prompt = f"Specifica originale:\n{spec}\n\nPiano di implementazione:\n{plan_str}\n\n"
    if hints:
        prompt += hints + "\n"
    if retry_note:
        prompt += (
            "Un tentativo precedente per questa stessa specifica è rimasto "
            f"bloccato sullo stesso errore per più iterazioni consecutive:\n{retry_note}\n\n"
            "Genera un'implementazione DIVERSA da zero (approccio o struttura "
            "del codice diversi), non una piccola variazione del tentativo "
            "precedente, evitando di ripetere lo stesso errore.\n\n"
        )
    prompt += (
        f"Genera il codice {backend.display_name} completo ed eseguibile/compilabile.\n"
        "Ricorda: no markdown fence, solo codice."
    )

    code = agent.run(prompt, temperature=temperature)
    code = backend.extract_code(code)
    ok(f"Codice generato: {len(code)} caratteri, {code.count(chr(10)) + 1} righe")
    return code


# Reviewer e Fixer lavorano in un loop. Integra tre idee dallo stato
# dell'arte per la generazione di HDL via LLM:
#   - RTLFixer: la base di conoscenza di errori noti (backend.retrieve_hints)
#     viene iniettata nel prompt del Fixer insieme alle righe di errore
#     isolate (extract_failure_lines), non solo l'output grezzo del toolchain.
#   - ReChisel: se la firma dell'errore (error_signature) resta identica per
#     2 iterazioni di fila, il Fixer è bloccato in un loop senza progressi —
#     "meccanismo di escape": invece di un altro fix in-place, si rigenera
#     il modulo da zero via run_coder.
#   - MAGE: la rigenerazione di escape usa temperatura più alta (0.7 invece
#     di 0.1) per ottenere un candidato davvero diverso.
def run_review_fix_loop(
    code: str, spec: str, plan: dict, backend: HDLBackend,
    reviewer: Agent, fixer: Agent, coder: Agent,
    stem: str, max_iter: int
) -> tuple[str, list[dict]]:
    agent_step("REVIEWER/FIXER", f"Loop review → fix (max {max_iter} iterazioni)")

    iteration_log: list[dict] = []
    last_signature: str | None = None
    stuck_count = 0

    for i in range(1, max_iter + 1):
        print(f"\n  {CYAN}── Iterazione {i}/{max_iter} ──{RESET}")

        reviewer.reset_history()
        review_result = reviewer.run(
            f"Specifica originale:\n{spec}\n\nCodice da revisionare:\n{code}"
        )
        passed_llm = review_result.strip().upper().startswith("PASS")
        if passed_llm:
            ok("LLM Reviewer: PASS")
        else:
            warn("LLM Reviewer: trovati problemi")
            print(f"  {DIM}{chr(10).join(review_result.splitlines()[:6])}{RESET}")

        compile_ok, compile_out = backend.check_module(code, stem)
        if compile_ok:
            ok(f"{backend.compile_check_label}: OK")
        else:
            warn(f"{backend.compile_check_label}: ERRORI")
            print(f"  {DIM}…{tail(compile_out, 300)}{RESET}")

        types_ok = backend.check_uses_shared_types(code)
        if not types_ok:
            warn(f"Codice non usa i tipi MXFP4 condivisi richiesti da {backend.display_name}")

        log_entry: dict = {
            "iterazione": i, "review_llm": review_result, "review_llm_pass": passed_llm,
            "compile_ok": compile_ok, "compile_output": tail(compile_out, 1200) if compile_out else "",
            "mxfp4_ok": types_ok, "fix_applicato": False, "escaped": False,
            "diagnosi": "", "esito": "",
        }

        tutto_ok = passed_llm and compile_ok and types_ok
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

        current_error_text = compile_out if not compile_ok else review_result
        signature = error_signature(current_error_text)
        if not types_ok:
            signature += "|TIPI_MXFP4_MANCANTI"
        stuck_count = stuck_count + 1 if signature == last_signature else 0
        last_signature = signature

        if stuck_count >= 2:
            agent_step("ESCAPE", f"Stesso errore per {stuck_count + 1} iterazioni: "
                                  f"rigenero il modulo da zero (iterazione {i})")
            reviewer.reset_history()
            fixer.reset_history()
            retry_note = backend.critical_reminders + tail(current_error_text, 800)
            if not types_ok:
                retry_note = (
                    f"Il tentativo precedente non usava affatto i tipi MXFP4 condivisi "
                    f"richiesti da {backend.display_name} — questo va corretto nel nuovo "
                    "tentativo.\n\n"
                ) + retry_note
            code = run_coder(plan, spec, backend, coder, retry_note=retry_note, temperature=0.7)
            log_entry["escaped"] = True
            log_entry["esito"] = "ESCAPED_RESTART"
            iteration_log.append(log_entry)
            stuck_count = 0
            last_signature = None
            continue

        agent_step("FIXER", f"Correzione automatica (iterazione {i})")
        hints = backend.retrieve_hints(current_error_text)
        fix_prompt = f"Codice con problemi:\n{code}\n\n"
        if not types_ok:
            fix_prompt += (
                f"PROBLEMA CRITICO: il codice non usa i tipi MXFP4 condivisi richiesti da "
                f"{backend.display_name}. Riscrivi il modulo usando quei tipi per gli "
                "ingressi/uscite indicati come MXFP4 nella specifica/piano.\n\n"
            )
        if not passed_llm:
            fix_prompt += f"Problemi rilevati da LLM Reviewer:\n{review_result}\n\n"
        if not compile_ok:
            failure_lines = extract_failure_lines(compile_out)
            if failure_lines:
                fix_prompt += f"Righe di errore rilevanti:\n{failure_lines}\n\n"
            fix_prompt += f"Errori di compilazione/elaborazione (coda dell'output):\n{tail(compile_out, 2500)}\n\n"
        if hints:
            fix_prompt += hints + "\n"
        fix_prompt += "Correggi TUTTI i problemi elencati e restituisci il codice completo e corretto."

        fix_response = fixer.run(fix_prompt)
        diagnosis = extract_diagnosis(fix_response)
        code = backend.extract_code(fix_response)
        if diagnosis:
            info(f"Diagnosi Fixer: {diagnosis}")
        ok(f"Codice corretto: {len(code)} caratteri")

        log_entry["fix_applicato"] = True
        log_entry["diagnosi"] = diagnosis
        log_entry["esito"] = "FIXED_CONTINUE"
        iteration_log.append(log_entry)

    return code, iteration_log


def run_tester(code: str, plan: dict, backend: HDLBackend, agent: Agent,
               temperature: float = 0.1) -> str:
    agent_step("TESTER", f"Generazione testbench ({backend.display_name})")

    plan_str = json.dumps(plan, ensure_ascii=False, indent=2)
    tb = agent.run(
        f"Piano del modulo:\n{plan_str}\n\nCodice del modulo:\n{code}\n\n"
        "Genera il testbench completo.\nRicorda: no markdown fence, solo codice.",
        temperature=temperature
    )
    tb = backend.extract_code(tb)
    tb_pulito = backend.strip_duplicate_module(tb, plan.get("nome_modulo", ""))
    if tb_pulito != tb:
        warn("Il Tester ha ridefinito il modulo nel testbench — rimossa la duplicazione")
        tb = tb_pulito
    ok(f"Testbench generato: {len(tb)} caratteri")
    return tb


# Esegue davvero il testbench in simulazione (backend.run_tests). Se un test
# fallisce (o il modulo non compila/elabora più insieme al testbench), il
# Fixer corregge il modulo e si ripete l'esecuzione, fino a max_iter
# iterazioni o al primo PASS. Stesso meccanismo di escape (ReChisel + MAGE)
# di run_review_fix_loop. Quando scatta l'escape si rigenerano insieme
# modulo E testbench da run_coder/run_tester, così restano coerenti tra
# loro invece di far indovinare al Fixer quale file è colpa.
def run_test_fix_loop(
    code: str, testbench: str, spec: str, plan: dict, backend: HDLBackend,
    coder: Agent, tester: Agent, fixer: Agent,
    stem: str, max_iter: int
) -> tuple[str, str, list[dict]]:
    agent_step("TEST", f"Esecuzione test in simulazione (max {max_iter} iterazioni)")

    iteration_log: list[dict] = []
    last_signature: str | None = None
    stuck_count = 0

    for i in range(1, max_iter + 1):
        print(f"\n  {CYAN}── Iterazione {i}/{max_iter} ──{RESET}")

        test_ok, test_out = backend.run_tests(code, testbench, stem, plan)
        if test_ok:
            ok(f"{backend.test_check_label}: OK")
        else:
            warn(f"{backend.test_check_label}: FALLITO")
            print(f"  {DIM}…{tail(test_out, 300)}{RESET}")

        types_ok = backend.check_uses_shared_types(code)
        if not types_ok:
            warn(f"Codice non usa i tipi MXFP4 condivisi richiesti da {backend.display_name}")

        log_entry: dict = {
            "iterazione": i, "test_ok": test_ok,
            "test_output": tail(test_out, 1200) if test_out else "",
            "mxfp4_ok": types_ok, "fix_applicato": False, "escaped": False,
            "diagnosi": "", "esito": "",
        }

        if test_ok and types_ok:
            ok(f"Test verificati all'iterazione {i}")
            log_entry["esito"] = "PASS"
            iteration_log.append(log_entry)
            break
        if i == max_iter:
            warn(f"Raggiunto limite iterazioni ({max_iter}) — uso l'ultimo codice")
            log_entry["esito"] = "MAX_ITER_REACHED"
            iteration_log.append(log_entry)
            break

        signature = error_signature(test_out)
        if not types_ok:
            signature += "|TIPI_MXFP4_MANCANTI"
        stuck_count = stuck_count + 1 if signature == last_signature else 0
        last_signature = signature

        if stuck_count >= 2:
            agent_step("ESCAPE", f"Stesso errore per {stuck_count + 1} iterazioni: "
                                  f"rigenero modulo e testbench da zero (iterazione {i})")
            fixer.reset_history()
            retry_note = backend.critical_reminders + tail(test_out, 800)
            if not types_ok:
                retry_note = (
                    f"Il tentativo precedente non usava affatto i tipi MXFP4 condivisi "
                    f"richiesti da {backend.display_name} — questo va corretto nel nuovo "
                    "tentativo.\n\n"
                ) + retry_note
            code = run_coder(plan, spec, backend, coder, retry_note=retry_note, temperature=0.7)
# Il Tester NON usa temperatura alta come il Coder: il testbench ha
# requisiti strutturali rigidi (API di simulazione specifiche, niente
# alternative). Diversità la vogliamo nel modulo, non nel modo in cui il
# testbench è strutturato — vedi CRITICAL_REMINDERS_*.
            testbench = run_tester(code, plan, backend, tester, temperature=0.2)
            log_entry["escaped"] = True
            log_entry["esito"] = "ESCAPED_RESTART"
            iteration_log.append(log_entry)
            stuck_count = 0
            last_signature = None
            continue

        agent_step("FIXER", f"Correzione automatica post-test (iterazione {i})")
        failure_lines = extract_failure_lines(test_out)
        hints = backend.retrieve_hints(test_out)
        fix_prompt = (
            f"Codice del modulo:\n{code}\n\n"
            f"Testbench (NON modificabile, deve restare compatibile con il modulo):\n{testbench}\n\n"
        )
        if not types_ok:
            fix_prompt += (
                f"PROBLEMA CRITICO: il modulo non usa i tipi MXFP4 condivisi richiesti da "
                f"{backend.display_name}. Riscrivi il modulo usando quei tipi per gli "
                "ingressi/uscite indicati come MXFP4 nella specifica/piano, mantenendo la "
                "compatibilità con il testbench.\n\n"
            )
        if failure_lines:
            fix_prompt += f"Righe di errore rilevanti:\n{failure_lines}\n\n"
        fix_prompt += f"Errore dall'esecuzione dei test (coda dell'output):\n{tail(test_out, 2500)}\n\n"
        if hints:
            fix_prompt += hints + "\n"
        fix_prompt += (
            "Correggi il modulo affinché compili/elabori insieme al testbench e i test "
            "passino. Restituisci SOLO il codice completo e corretto del modulo (non il testbench)."
        )
        fix_response = fixer.run(fix_prompt)
        diagnosis = extract_diagnosis(fix_response)
        code = backend.extract_code(fix_response)
        if diagnosis:
            info(f"Diagnosi Fixer: {diagnosis}")
        ok(f"Codice corretto: {len(code)} caratteri")

        log_entry["fix_applicato"] = True
        log_entry["diagnosi"] = diagnosis
        log_entry["esito"] = "FIXED_CONTINUE"
        iteration_log.append(log_entry)

    return code, testbench, iteration_log


# ═══════════════════════════════════════════════════════════════════════
#  Salvataggio degli artefatti in una directory timestamped: layout di
#  progetto specifico del backend scelto (backend.save) più un report
#  Markdown, un log JSON e un README condivisi e agnostici rispetto al
#  linguaggio.
# ═══════════════════════════════════════════════════════════════════════
def save_outputs(
    spec: str, plan: dict, code: str, testbench: str,
    iter_log: list[dict], test_log: list[dict],
    model: str, backend: HDLBackend, selector_result: dict, stem: str
) -> Path:
    step("6", "Salvataggio artefatti")

    msafe = model.replace(":", "_").replace("/", "_")
    ts    = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    out   = Path(f"agentic_output_{stem}_{backend.key}_{msafe}_{ts}")
    out.mkdir(parents=True, exist_ok=True)

    backend.save(out, stem, code, testbench)
    ok(f"File del modulo/testbench salvati → {out}/  (layout {backend.display_name})")

    n_fix      = sum(1 for it in iter_log if it.get("fix_applicato"))
    n_fix_t    = sum(1 for it in test_log if it.get("fix_applicato"))
    n_esc      = sum(1 for it in iter_log if it.get("escaped"))
    n_esc_t    = sum(1 for it in test_log if it.get("escaped"))

    iters_md = ""
    for it in iter_log:
        label = "ESCAPE (rigenerato da zero)" if it.get("escaped") else it["esito"]
        iters_md += (
            f"\n#### Iterazione {it['iterazione']} `{label}`\n\n"
            f"| Verifica | Risultato |\n|---|---|\n"
            f"| LLM Reviewer | `{'PASS' if it['review_llm_pass'] else 'ISSUES'}` |\n"
            f"| {backend.compile_check_label} | `{'OK' if it['compile_ok'] else 'FAIL'}` |\n"
            f"| Usa tipi MXFP4 | `{'SI' if it.get('mxfp4_ok', True) else 'NO'}` |\n"
            f"| Fix applicato | `{it['fix_applicato']}` |\n"
        )
        if it.get("diagnosi"):
            iters_md += f"\n**Diagnosi Fixer:** {it['diagnosi']}\n"
        if it.get("fix_applicato") and it.get("review_llm"):
            iters_md += f"\n**Issues rilevati:**\n```\n{it['review_llm'][:400]}\n```\n"

    test_md = ""
    for it in test_log:
        label = "ESCAPE (modulo+testbench rigenerati)" if it.get("escaped") else it["esito"]
        test_md += (
            f"\n#### Iterazione {it['iterazione']} `{label}`\n\n"
            f"| Verifica | Risultato |\n|---|---|\n"
            f"| {backend.test_check_label} | `{'OK' if it['test_ok'] else 'FAIL'}` |\n"
            f"| Usa tipi MXFP4 | `{'SI' if it.get('mxfp4_ok', True) else 'NO'}` |\n"
            f"| Fix applicato | `{it['fix_applicato']}` |\n"
        )
        if it.get("diagnosi"):
            test_md += f"\n**Diagnosi Fixer:** {it['diagnosi']}\n"
        if it.get("fix_applicato") and it.get("test_output"):
            test_md += f"\n**Output:**\n```\n{it['test_output'][:400]}\n```\n"
    if not test_md:
        test_md = "\n_Esecuzione test saltata (toolchain non disponibile)._\n"

    algo_md = "".join(f"- {p}\n" for p in plan.get("passi_algoritmo", []))
    manifest_md = "".join(f"| `{p}` | {d} |\n" for p, d in backend.file_manifest)
    toolchain_md = "".join(
        f"- {'✔' if okv else '⚠'} {msg}\n" for okv, msg in backend.toolchain_status()
    )

    (out / f"report_{stem}.md").write_text(
        f"# Report Agentico — {stem} ({backend.display_name})\n\n"
        f"| Campo | Valore |\n|---|---|\n"
        f"| **Modulo** | `{stem}` |\n"
        f"| **Linguaggio Meta-HDL** | {backend.display_name} |\n"
        f"| **Modello Ollama** | `{model}` |\n"
        f"| **Data** | {datetime.datetime.now().isoformat(timespec='seconds')} |\n"
        f"| **Agenti eseguiti** | Selector, Planner, Coder, Reviewer, Fixer, Tester |\n"
        f"| **Iterazioni review/fix** | {len(iter_log)} |\n"
        f"| **Fix automatici (review/fix)** | {n_fix} |\n"
        f"| **Escape (review/fix)** | {n_esc} |\n"
        f"| **Iterazioni test/fix** | {len(test_log)} |\n"
        f"| **Fix automatici (test/fix)** | {n_fix_t} |\n"
        f"| **Escape (test/fix)** | {n_esc_t} |\n\n"
        f"---\n\n"
        f"## Selezione del linguaggio (Selector Agent)\n\n"
        f"**Scelto:** {backend.display_name}\n\n"
        f"**Motivazione:** {selector_result.get('motivazione', 'N/D')}\n\n"
        f"### Stato toolchain\n\n{toolchain_md}\n"
        f"---\n\n"
        f"## Specifica Originale\n\n{spec}\n\n"
        f"---\n\n"
        f"## Piano di Implementazione (Planner Agent)\n\n"
        f"```json\n{json.dumps(plan, ensure_ascii=False, indent=2)}\n```\n\n"
        f"### Algoritmo pianificato\n\n{algo_md}\n"
        f"---\n\n"
        f"## Log Agentico — Review/Fix Loop ({backend.compile_check_label})\n{iters_md}\n"
        f"---\n\n"
        f"## Log Agentico — Verifica funzionale ({backend.test_check_label})\n{test_md}\n"
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
        f"## File generati\n\n"
        f"| File | Descrizione |\n|---|---|\n{manifest_md}\n"
        f"---\n\n"
        f"*Report generato automaticamente da `Tesi.py`*\n",
        encoding="utf-8"
    )
    ok(f"Report Markdown → {out}/report_{stem}.md")

    (out / "agent_log.json").write_text(
        json.dumps({
            "timestamp": ts, "model": model, "linguaggio": backend.key,
            "selector": selector_result, "spec": spec, "plan": plan,
            "stats": {
                "iterazioni_review_fix":   len(iter_log),
                "fix_applicati_review_fix": n_fix,
                "escape_review_fix":        n_esc,
                "esito_finale_review_fix":  iter_log[-1]["esito"] if iter_log else "N/A",
                "iterazioni_test":          len(test_log),
                "fix_applicati_test":       n_fix_t,
                "escape_test":              n_esc_t,
                "esito_finale_test":        test_log[-1]["esito"] if test_log else "N/A",
            },
            "iterations":      iter_log,
            "test_iterations": test_log,
        }, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    ok(f"Log JSON        → {out}/agent_log.json")

    (out / "README.md").write_text(
        f"# {stem} — {backend.display_name}\n\n"
        f"Generato da **Tesi.py** con modello Ollama `{model}`.\n"
        f"Linguaggio Meta-HDL scelto dal Selector agent: **{backend.display_name}** "
        f"— {selector_result.get('motivazione', '')}\n\n"
        f"## Esecuzione dei test\n\n```bash\n{backend.run_instructions}\n```\n\n"
        f"## File generati\n\n"
        f"| File | Descrizione |\n|---|---|\n{manifest_md}"
        f"| `report_{stem}.md` | Report completo per la tesi |\n"
        f"| `agent_log.json` | Log JSON del workflow agentico |\n",
        encoding="utf-8"
    )
    ok(f"README          → {out}/README.md")

    return out


# ═══════════════════════════════════════════════════════════════════════
#  Setup Ollama e acquisizione della specifica.
# ═══════════════════════════════════════════════════════════════════════
def check_ollama(host: str) -> list[str]:
    step("0", f"Verifica Ollama ({host})")
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
        is_rec = any(name.startswith(r) for r in ["codellama", "deepseek-coder", "qwen2.5-coder"])
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


def get_specification(file_arg: str | None, spec_arg: str | None) -> str:
    step("0", "Acquisizione della specifica")

    if spec_arg:
        ok(f"Specifica da argomento CLI ({len(spec_arg)} caratteri)")
        return spec_arg

    if file_arg:
        path = Path(file_arg)
        if not path.exists():
            err(f"File non trovato: {path}")
            sys.exit(1)
        source = path.read_text(encoding="utf-8")
        ok(f"File caricato come contesto: {path.name}  ({source.count(chr(10)) + 1} righe)")
        return (
            "Implementa in un linguaggio Meta-HDL, con formato MXFP4 (E2M1, 4 bit), "
            "un'unità aritmetica hardware funzionalmente equivalente al seguente codice "
            f"Python:\n\n```python\n{source}\n```\n\n"
            "Adatta ingressi, uscite e logica al dominio hardware/MXFP4."
        )

    print(f"""
  Descrivi l'unità aritmetica da implementare (formato MXFP4 E2M1).
  Il linguaggio Meta-HDL (Chisel o Amaranth) verrà scelto automaticamente.

  Esempi di specifiche:
    • "Implementa un full adder 1-bit con ingressi e uscite MXFP4 E2M1"
    • "Crea un moltiplicatore che moltiplica due numeri MXFP4 a 4 bit"
    • "ALU MXFP4 con addizione e sottrazione, gestione overflow, da integrare "
      "come coprocessore in un core RISC-V"
    • "Prototipo algoritmico rapido in Python per un ripple-carry adder MXFP4 4-bit"
""")
    spec = input(f"  {BOLD}Descrizione dell'unità:{RESET} ").strip()
    if not spec:
        err("Specifica vuota.")
        sys.exit(1)
    ok(f"Specifica acquisita ({len(spec)} caratteri)")
    return spec


# ═══════════════════════════════════════════════════════════════════════
#  Main.
# ═══════════════════════════════════════════════════════════════════════
def main():
    banner()

    parser = argparse.ArgumentParser(
        description="Agentic Meta-HDL MXFP4 generator — Ollama locale",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Esempi:
              python Tesi.py
              python Tesi.py --spec "full adder MXFP4 4 bit"
              python Tesi.py --file full_adder.py --model qwen2.5-coder
              python Tesi.py --spec "moltiplicatore MXFP4" --lang amaranth --iter 5 -v
              python Tesi.py --spec "ALU MXFP4 coprocessore RISC-V" --lang chisel
        """)
    )
    parser.add_argument("--spec",  "-s", help="Specifica testuale (es. 'full adder MXFP4')")
    parser.add_argument("--file",  "-f", help="File Python come contesto (backward compat)")
    parser.add_argument("--model", "-m", help="Modello Ollama (es. qwen2.5-coder, codellama)")
    parser.add_argument("--host",        default=DEFAULT_HOST, help=f"URL Ollama (default: {DEFAULT_HOST})")
    parser.add_argument("--iter", "-i",  type=int, default=MAX_FIX_ITER,
                         help=f"Max iterazioni review/fix e test/fix (default: {MAX_FIX_ITER})")
    parser.add_argument("--lang", "-l",  choices=["auto", "chisel", "amaranth"], default="auto",
                         help="Linguaggio Meta-HDL: 'auto' lascia scegliere il Selector agent (default)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Output dettagliato")
    args = parser.parse_args()

    available = check_ollama(args.host)
    model     = choose_model(available, args.model)
    spec      = get_specification(args.file, args.spec)

    backends: dict[str, HDLBackend] = {"chisel": ChiselBackend(), "amaranth": AmaranthBackend()}

    if args.lang != "auto":
        backend = backends[args.lang]
        selector_result = {
            "linguaggio": args.lang,
            "motivazione": "Scelto esplicitamente da riga di comando (--lang).",
        }
        step("1", "Selezione del linguaggio Meta-HDL")
        ok(f"Linguaggio forzato da CLI: {BOLD}{backend.display_name}{RESET}")
    else:
        selector_result = run_selector(spec, args.host, model, backends)
        backend = backends[selector_result["linguaggio"]]

    hr()
    print(f"\n  {BOLD}Stato toolchain — {backend.display_name}:{RESET}")
    for tc_ok, msg in backend.toolchain_status():
        (ok if tc_ok else info)(msg)

    hr()
    print(f"\n  {BOLD}Pipeline agentica:{RESET}  "
          f"Selector → Planner → Coder → [Reviewer⟷Fixer]×{args.iter} → "
          f"Tester → [Test⟷Fixer]×{args.iter}\n")

    planner  = Agent("Planner", SYSTEM_PLANNER_TEMPLATE.format(
        lang_display=backend.display_name, type_vocab=backend.plan_type_vocab
    ), args.host, model)
    prompts  = backend.prompts()
    coder    = Agent("Coder",    prompts["coder"],    args.host, model)
    reviewer = Agent("Reviewer", prompts["reviewer"], args.host, model)
    fixer    = Agent("Fixer",    prompts["fixer"],    args.host, model)
    tester   = Agent("Tester",   prompts["tester"],   args.host, model)

    t_global = datetime.datetime.now()

    plan  = run_planner(spec, planner)
    stem  = re.sub(r"[^a-zA-Z0-9_]", "_", plan.get("nome_modulo", "MxFp4Unit"))

    code  = run_coder(plan, spec, backend, coder)

    code, iter_log = run_review_fix_loop(
        code, spec, plan, backend, reviewer, fixer, coder, stem, args.iter
    )

    testbench = run_tester(code, plan, backend, tester)

    code, testbench, test_log = run_test_fix_loop(
        code, testbench, spec, plan, backend, coder, tester, fixer, stem, args.iter
    )

    out_dir = save_outputs(
        spec, plan, code, testbench, iter_log, test_log,
        model, backend, selector_result, stem
    )

    elapsed_total = (datetime.datetime.now() - t_global).total_seconds()

    hr()
    n_fix      = sum(1 for it in iter_log if it.get("fix_applicato"))
    n_fix_t    = sum(1 for it in test_log if it.get("fix_applicato"))
    n_esc      = sum(1 for it in iter_log if it.get("escaped"))
    n_esc_t    = sum(1 for it in test_log if it.get("escaped"))
    esito      = iter_log[-1]["esito"] if iter_log else "N/A"
    esito_s    = f"{GREEN}PASS{RESET}" if esito == "PASS" else f"{YELLOW}{esito}{RESET}"
    esito_t    = test_log[-1]["esito"] if test_log else "N/A"
    esito_t_s  = f"{GREEN}PASS{RESET}" if esito_t == "PASS" else f"{YELLOW}{esito_t}{RESET}"

    manifest_lines = "\n".join(f"      ├── {p:<40} ← {d}" for p, d in backend.file_manifest)

    print(f"""
{GREEN}{BOLD} Pipeline agentica completata in {elapsed_total:.0f}s!{RESET}

  {BOLD}Linguaggio scelto:{RESET}  {backend.display_name}
  {BOLD}Motivazione:{RESET}        {selector_result.get('motivazione', 'N/D')}

  {BOLD}Statistiche:{RESET}
      • Agenti eseguiti:        6  (Selector, Planner, Coder, Reviewer, Fixer, Tester)
      • Iterazioni review/fix:  {len(iter_log)}  (fix: {n_fix}, escape: {n_esc})  →  {esito_s}
      • Iterazioni test/fix:    {len(test_log)}  (fix: {n_fix_t}, escape: {n_esc_t})  →  {esito_t_s}

  {BOLD}Output:{RESET}  {BOLD}{out_dir}/{RESET}
{manifest_lines}
      ├── {'report_' + stem + '.md':<40} ← Report per la tesi
      └── {'agent_log.json':<40} ← Log JSON del workflow agentico

  {CYAN}Esegui i test:{RESET}
      cd {out_dir}
      {backend.run_instructions}
""")
    hr()


if __name__ == "__main__":
    main()
