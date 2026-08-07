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

SYSTEM_REVIEWER = """\
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

SYSTEM_FIXER = """\
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

SYSTEM_TESTER = """\
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
# correttezza non dipende dal modello scelto. Coder/Fixer/Tester lo importano
# sempre con "import mxfp4._" invece di ridefinirlo ogni volta.
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

// Utility di conversione tra MXFP4 e Double, pensate per i testbench
// (poke/expect) e per gli agenti che generano i test.
object MXFP4 {
  def apply(): MXFP4 = new MXFP4

  // Costruisce un letterale MXFP4 dai 4 bit codificati (0..15), per poke/expect
  // nei testbench: es. dut.io.a.poke(MXFP4(3)), dut.io.sum.expect(MXFP4(5)).
  def apply(bits: Int): MXFP4 = {
    val s = (bits >> 3) & 0x1
    val e = (bits >> 1) & 0x3
    val m = bits & 0x1
    (new MXFP4).Lit(_.sign -> (s == 1).B, _.exp -> e.U(2.W), _.mant -> m.U(1.W))
  }

  // Converte i 4 bit codificati (0..15) nel Double rappresentato.
  def decode(bits: Int): Double = {
    val sign = (bits >> 3) & 0x1
    val exp  = (bits >> 1) & 0x3
    val mant = bits & 0x1
    val s = if (sign == 1) -1.0 else 1.0
    if (exp == 0) {
      // Subnormale (include lo zero): NESSUN bit implicito, esponente
      // effettivo = 1 - bias = 0. (bits=0001 -> 0.5, non 0.75: senza
      // bit implicito la mantissa vale mant*0.5, non (1+mant*0.5)).
      s * mant * 0.5
    } else {
      // Normale: bit implicito 1, esponente effettivo = exp - bias.
      s * (1.0 + mant * 0.5) * math.pow(2.0, exp - 1)
    }
  }

  // Converte un Double nella codifica MXFP4 (0..15) più vicina (round-to-nearest).
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

# build.sbt condiviso sia dal Toolchain (compilazione/test di verifica in
# directory temporanea) sia dall'output finale salvato su disco, per non
# avere due definizioni che possono disallinearsi.
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
║  Planner → Coder → [Reviewer⟷Fixer] → Tester → [Verilator⟷Fixer] ║
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

# Ritorna la coda di un output di sbt, non la testa. sbt stampa sempre prima il
# boilerplate di avvio/risoluzione dipendenze (spesso migliaia di caratteri, in
# particolare la prima volta che gira in una cache WSL vuota) e solo alla fine
# gli "[error]" veri e il riepilogo del fallimento: troncare con [:N] mostra
# quindi quasi sempre solo "welcome to sbt... loading project..." e mai
# l'errore reale, sia nel report sia (peggio) nel prompt dato al Fixer.
def tail(text: str, n: int) -> str:
    return text[-n:] if len(text) > n else text

# Isola dall'output grezzo di sbt le righe che portano davvero un segnale
# (errori di compilazione, asserzioni fallite, eccezioni) invece del rumore di
# avvio/risoluzione dipendenze. Versione "leggera" del waveform tracing
# AST-based di VerilogCoder: lì si introspeziona la forma d'onda della
# simulazione per individuare il segnale che ha divergere; qui, non avendo
# accesso a quel livello (servirebbe parsare FIRRTL/VCD, fuori scala per una
# singola unità combinatoria), si ottiene lo stesso risultato pratico — dare
# al Fixer il segnale preciso invece del log intero — filtrando per marcatori
# testuali noti di ScalaTest/ChiselTest/sbt. Usata insieme a tail(): questa
# funzione dà il segnale specifico, tail() il contesto generico attorno.
FAILURE_MARKERS = (
    "[error]", "error:", "exception", "failed", "did not equal",
    "assertion", "expect(", "mismatch",
)

def extract_failure_lines(sbt_output: str, max_lines: int = 25) -> str:
    seen: set[str] = set()
    hits: list[str] = []
    for line in sbt_output.splitlines():
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
# (meccanismo di "escape" di ReChisel): due errori con la stessa causa devono
# produrre la stessa firma anche se contengono dettagli che cambiano a ogni
# iterazione (path della directory temporanea, timestamp), altrimenti il
# confronto fallirebbe sempre e l'escape non scatterebbe mai.
def error_signature(text: str) -> str:
    basis = extract_failure_lines(text) or tail(text, 200)
    basis = re.sub(r"chisel_check_\S+", "chisel_check_X", basis)
    basis = re.sub(r"/mnt/\S+", "PATH", basis)
    basis = re.sub(r"[A-Za-z]:\\\S+", "PATH", basis)
    basis = re.sub(r"\d{4,}", "N", basis)
    basis = re.sub(r"\s+", " ", basis).strip()
    return basis[:300]

# Promemoria delle regole più spesso violate, da anteporre alla nota di
# retry quando scatta l'escape: a temperatura alta (0.7, per ottenere un
# candidato davvero diverso — vedi run_coder) il modello segue le regole
# statiche del system prompt meno affidabilmente ed è stato osservato
# regredire a pattern già vietati (es. chisel3.Driver). Le regole del system
# prompt restano l'unica fonte di verità: questo è solo un richiamo mirato ai
# punti che si sono visti saltare più spesso, non una loro sostituzione.
CRITICAL_REMINDERS = (
    "Promemoria (regole spesso dimenticate a questa temperatura più alta):\n"
    "- NON usare chisel3.Driver né 'object X extends App': non servono e non esistono più.\n"
    "- Usa SEMPRE 'import mxfp4._' e dichiara ingressi/uscite MXFP4 con 'new MXFP4', mai UInt semplice.\n"
    "- NON istanziare 'Module(new XOR/AND/OR)': non esistono.\n"
    "- Un segnale MXFP4 è un Bundle: NON ha operatori ^/&/|. Per sommare valori MXFP4 usa "
    "l'algoritmo decodifica/allinea/somma/arrotonda-satura descritto nelle regole, non XOR/AND/OR.\n\n"
)

# Base di conoscenza di errori Chisel/Scala osservati per davvero durante lo
# sviluppo di questa pipeline, con il suggerimento che li risolve — idea di
# RTLFixer (RAG su un knowledge base di errori di sintassi comuni) adattata in
# scala ridotta: niente embedding/vector store, solo matching per sottostringa,
# adeguato per una manciata di pattern noti in un dominio ristretto (Chisel +
# MXFP4) invece del RAG generico su tutto VerilogEval che usa RTLFixer.
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
                 "ricodificare — l'algoritmo completo è descritto nel punto 11 delle regole."),
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
        "hint": ("In Chisel ').asUInt'/'.asSInt'/'.asBool' sono valori, non metodi: chiamarli con "
                 "le parentesi (es. '.asSInt()') causa 'cannot be applied to ()' — stesso problema "
                 "di '.litValue()'. Togli le parentesi: '.asUInt', '.asSInt', '.asBool' senza ()."),
    },
]

def retrieve_hints(text: str, kb: list[dict] = CHISEL_KNOWLEDGE_BASE) -> str:
    text_low = text.lower()
    matched = [
        entry["hint"] for entry in kb
        if any(trig in text_low for trig in entry["triggers"])
    ]
    if not matched:
        return ""
    bullets = "\n".join(f"- {h}" for h in matched)
    return f"Suggerimenti noti (da errori osservati in precedenza su questo progetto):\n{bullets}\n"

# Verifica deterministica (non-LLM) che il codice usi davvero il Bundle MXFP4,
# invece di fidarsi ciecamente del giudizio del Reviewer. Run reali hanno
# mostrato il Coder generare un full adder UInt semplice nonostante il piano
# richiedesse esplicitamente ingressi/uscite MXFP4, SENZA che il Reviewer lo
# segnalasse come ISSUES (il suo giudizio da solo non basta). Questo controllo
# fa da backstop indipendente: se manca, il codice non è considerato valido a
# prescindere da cosa dicono Reviewer/compilatore/test.
def check_mxfp4_usage(code: str) -> bool:
    low = code.lower()
    return "mxfp4._" in low or "new mxfp4" in low


# Api per ollama. Ollama_get() ritorna il JSON o None se non raggiungibile. Ollama_chat() gestisce la chat multi-turn con history e system prompt.
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
        "options": {
            "temperature": temperature,
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
# 'temperature' di default resta bassa (deterministica) per le correzioni "in place";
# viene alzata solo per i restart ad alta diversità del meccanismo di escape (vedi
# run_review_fix_loop/run_verilator_loop) — idea presa da MAGE (candidate sampling
# ad alta temperatura per uscire da un candidato bloccato).
    def run(self, user_message: str, temperature: float = 0.1) -> str:
        self.history.append({"role": "user", "content": user_message})
        info(f"Agente {BOLD}{self.name}{RESET} in elaborazione…")

        t_start  = datetime.datetime.now()
        response = ollama_chat(
            self.host, self.model, self.system_prompt, self.history,
            temperature=temperature
        )
        elapsed  = (datetime.datetime.now() - t_start).total_seconds()

        self.history.append({"role": "assistant", "content": response})
        ok(f"{self.name} risposta in {elapsed:.1f}s  "
           f"({len(response)} caratteri)")
        return response

    def reset_history(self):
        self.history = []


# Classe Toolchain: gestisce sia la compilazione (sbt compile) sia l'esecuzione
# reale dei test (sbt test) del codice Chisel generato. I test girano in
# simulazione tramite Verilator (annotazione VerilatorBackendAnnotation nel
# testbench), non con il backend di default di ChiselTest. In ogni progetto
# temporaneo viene sempre incluso il Bundle MXFP4 canonico (MXFP4_SCALA),
# così Coder/Fixer/Tester devono solo importarlo, non ridefinirlo.
class Toolchain:
    def __init__(self):
        self.sbt_available = shutil.which("sbt") is not None or \
                              shutil.which("sbt.bat") is not None

# Su Windows Verilator si installa quasi sempre solo dentro WSL (il supporto
# nativo Windows è fragile/poco mantenuto). Se non lo troviamo nel PATH nativo,
# proviamo a vedere se è raggiungibile dentro una distro WSL: in tal caso
# 'sbt test' (che deve invocare il binario verilator) viene eseguito tramite
# 'wsl.exe' invece che come processo nativo.
        self.verilator_available  = shutil.which("verilator") is not None
        self.use_wsl_verilator    = False

        if not self.verilator_available:
            wsl_verilator = self._wsl_which("verilator")
            if wsl_verilator:
                self.verilator_available = True
                self.use_wsl_verilator   = True

    @staticmethod
    def _wsl_which(binary: str) -> str | None:
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
# (es. C:\Users\... → /mnt/c/Users/...). Costruito a mano invece di chiamare
# 'wslpath' perché quest'ultimo, invocato da un processo Windows con un path
# contenente backslash, tronca l'argomento in modo inaffidabile (verificato:
# ritorna 'C:UsersmattiaAppData...' senza backslash ed exit code 1). Il mount
# automatico '/mnt/<drive minuscola>/...' è lo standard su WSL2 di default.
    @staticmethod
    def _to_wsl_path(win_path: Path) -> str:
        p = str(win_path.resolve())
        drive, rest = p.split(":", 1)
        rest = rest.replace("\\", "/")
        return f"/mnt/{drive.lower()}{rest}"

# Esegue 'sbt <args>' nella directory 'cwd': nativamente su Windows, oppure
# tramite 'wsl.exe' quando l'esecuzione dei test richiede il Verilator
# disponibile solo dentro WSL.
    def _run_sbt(self, args: list[str], cwd: Path, timeout: int,
                 via_wsl: bool) -> subprocess.CompletedProcess:
        if via_wsl:
            wsl_dir = self._to_wsl_path(cwd)
            cmd_str = "cd " + shlex.quote(wsl_dir) + " && sbt " + " ".join(args)
            return subprocess.run(
                ["wsl.exe", "-e", "bash", "-lc", cmd_str],
                capture_output=True, text=True, timeout=timeout
            )
        return subprocess.run(
            ["sbt", *args], cwd=cwd,
            capture_output=True, text=True, timeout=timeout, shell=True
        )

# Crea un progetto SBT minimale in una directory temporanea (cross-platform,
# a differenza del vecchio path hardcoded "/tmp" che non esiste su Windows),
# già popolato con build.sbt e il package mxfp4 condiviso.
    def _new_project(self, stem: str) -> Path:
        tmp = Path(tempfile.gettempdir()) / \
            f"chisel_check_{stem}_{datetime.datetime.now().strftime('%H%M%S%f')}"
        tmp.mkdir(parents=True, exist_ok=True)

        (tmp / "build.sbt").write_text(BUILD_SBT, encoding="utf-8")

        proj_dir = tmp / "project"
        proj_dir.mkdir(exist_ok=True)
        (proj_dir / "build.properties").write_text(
            "sbt.version=1.10.7\n", encoding="utf-8"
        )

        mxfp4_dir = tmp / "src" / "main" / "scala" / "mxfp4"
        mxfp4_dir.mkdir(parents=True, exist_ok=True)
        (mxfp4_dir / "MXFP4.scala").write_text(MXFP4_SCALA, encoding="utf-8")

        (tmp / "src" / "main" / "scala").mkdir(parents=True, exist_ok=True)
        return tmp

# Verifica solo che il modulo compili (sbt compile). Usato nel loop Reviewer/Fixer
# prima ancora che esista un testbench. Ritorna (successo, output). Se sbt non
# è disponibile, ritorna True con un messaggio di avviso (skip silenzioso).
    def compile_module(self, chisel_code: str, stem: str) -> tuple[bool, str]:
        if not self.sbt_available:
            return True, "sbt non trovato — compilazione reale saltata"

        tmp = self._new_project(stem)
        try:
            (tmp / "src" / "main" / "scala" / f"{stem}.scala").write_text(
                chisel_code, encoding="utf-8"
            )
            result = self._run_sbt(["compile"], tmp, timeout=180, via_wsl=False)
            output  = (result.stdout + result.stderr).strip()
            success = result.returncode == 0
            return success, output

        except subprocess.TimeoutExpired:
            return False, "sbt compile timeout (>180s)"
        except Exception as e:
            return False, str(e)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

# Esegue davvero il testbench con 'sbt test', simulando il modulo tramite
# Verilator. Ritorna (successo, output). Se sbt o verilator non sono
# disponibili, ritorna True con un messaggio di avviso (skip silenzioso),
# così il resto della pipeline continua comunque a funzionare.
    def run_tests(self, chisel_code: str, testbench_code: str, stem: str) -> tuple[bool, str]:
        if not self.sbt_available:
            return True, "sbt non trovato — esecuzione test saltata"
        if not self.verilator_available:
            return True, ("verilator non trovato (né nativamente né in WSL) — esecuzione test "
                           "saltata (richiesto dal backend VerilatorBackendAnnotation del testbench)")

        tmp = self._new_project(stem)
        try:
            (tmp / "src" / "main" / "scala" / f"{stem}.scala").write_text(
                chisel_code, encoding="utf-8"
            )
            test_dir = tmp / "src" / "test" / "scala"
            test_dir.mkdir(parents=True, exist_ok=True)
            (test_dir / f"{stem}Test.scala").write_text(
                testbench_code, encoding="utf-8"
            )

            result = self._run_sbt(
                ["test"], tmp, timeout=300, via_wsl=self.use_wsl_verilator
            )
            output  = (result.stdout + result.stderr).strip()
            success = result.returncode == 0
            return success, output

        except subprocess.TimeoutExpired:
            return False, "sbt test (Verilator) timeout (>300s)"
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

# Estrae la riga "Diagnosi: ..." che SYSTEM_FIXER chiede di anteporre al codice
# corretto (stile ReAct di RTLFixer: prima il ragionamento, poi l'azione). Non
# influisce sull'estrazione del codice — extract_scala_code() qui sotto parte
# comunque dalla prima occorrenza di "import chisel3", quindi la riga di
# diagnosi viene scartata automaticamente da quella funzione. Serve solo per
# mostrare la traccia di ragionamento del Fixer nel log/report.
def extract_diagnosis(text: str) -> str:
    m = re.search(r"Diagnosi:\s*(.+)", text)
    return m.group(1).strip() if m else ""

# Estrae codice Scala da una risposta LLM che potrebbe non rispettare l'istruzione
# "solo codice, nessun markdown, nessun testo prima o dopo" — un limite comune nei
# modelli locali più piccoli. Un semplice re.sub(r"```scala|```", "", txt) toglie
# solo i marcatori di fence e lascia la prosa attorno intatta (producendo un .scala
# non compilabile); questa funzione isola il codice vero e proprio:
#   1. se c'è un blocco fenced ```scala/``` che contiene codice Chisel riconoscibile,
#      usa quello (l'ultimo, se ce n'è più di uno);
#   2. altrimenti parte dalla prima riga "import chisel3" e taglia alla chiusura
#      bilanciata dell'ultima dichiarazione top-level (class/object/trait), così
#      eventuali spiegazioni testuali dopo il codice vengono scartate.
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

# Il Tester a volte ri-genera per intero il modulo insieme alla classe di test
# (nonostante SYSTEM_TESTER dica esplicitamente "rispondi con SOLO il
# testbench"), producendo un simbolo duplicato in compilazione: il modulo
# esiste già nel suo file separato (src/main/scala). Rimuove quella
# ridefinizione accidentale dal testo del testbench con la stessa logica di
# bilanciamento delle graffe di extract_scala_code, lasciando intatto il
# resto (import, classe di test).
def strip_duplicate_module(text: str, module_name: str) -> str:
    if not module_name:
        return text
    pattern = re.compile(r"class\s+" + re.escape(module_name) + r"\b[^\n{]*\{")
    m = pattern.search(text)
    if not m:
        return text

    depth = 0
    i = m.end() - 1  # indice della graffa di apertura
    while i < len(text):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return (text[:m.start()] + text[i + 1:]).strip()
        i += 1
    return text  # graffe non bilanciate: non tocco nulla per sicurezza

# Il Planner a volte ignora lo schema JSON richiesto e ne inventa uno proprio
# (osservato più volte: "unit_name"/"inputs"/"outputs"/"logic_steps" invece di
# "nome_modulo"/"ingressi"/"uscite"/"passi_algoritmo") — limite comune nei
# modelli locali più piccoli. Senza normalizzazione questo passa inosservato:
# 'plan.get("nome_modulo", "MxFp4Unit")' ricade silenziosamente sul default,
# quindi i file vengono salvati con un nome che non corrisponde al modulo
# scritto dal Coder, e "Algoritmo pianificato" nel report resta vuoto. Qui si
# aggiungono le chiavi attese come alias di quelle trovate, senza toccare il
# resto del piano (che arriva comunque per intero a Coder/Tester via JSON).
def _normalize_plan(plan: dict, spec: str) -> dict:
    aliases = {
        "nome_modulo":    ["unit_name", "module_name", "name"],
        "tipo":           ["type"],
        "descrizione":    ["description"],
        "ingressi":       ["inputs"],
        "uscite":         ["outputs"],
        "segnali_interni": ["internal_signals", "signals"],
        "passi_algoritmo": ["logic_steps", "algorithm_steps", "steps", "components"],
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

# I "passi" a volte arrivano come lista di dict invece che di stringhe, sia in
# forma {"step_name"/"name": ..., "description": ...} sia (via l'alias
# "components") in forma {"name": ..., "type": ..., "inputs": [...], "outputs": [...]}:
# il report si aspetta stringhe da elencare puntate.
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
# output (es. {"name": "Sum", "type": "E2M1"}) — ma E2M1 è solo il nome della
# codifica che il Bundle MXFP4 implementa, non un tipo Chisel a sé. Lasciato
# così, il Coder genera "Output(E2M1())", che non esiste da nessuna parte
# (osservato più volte: sempre "not found: value E2M1" in compilazione).
    for key in ("ingressi", "uscite", "segnali_interni"):
        for port in plan.get(key, []) or []:
            if isinstance(port, dict) and str(port.get("type", "")).strip().lower() == "e2m1":
                port["type"] = "MXFP4"

    return plan

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
        plan = _normalize_plan(plan, spec)
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
# 'retry_note' e 'temperature' sono usati dal meccanismo di escape (vedi
# run_review_fix_loop/run_verilator_loop): quando il loop di fix è bloccato
# sullo stesso errore, invece di continuare a correggere in-place si richiama
# il Coder da zero, con una nota su cosa ha bloccato il tentativo precedente
# e una temperatura più alta per ottenere un candidato davvero diverso
# (campionamento ad alta temperatura di MAGE), non solo il default 0.1 usato
# per le correzioni deterministiche in-place.
def run_coder(plan: dict, spec: str, agent: Agent,
               retry_note: str = "", temperature: float = 0.1) -> str:
    agent_step("CODER", "Generazione codice Chisel 3 MXFP4")

    plan_str = json.dumps(plan, ensure_ascii=False, indent=2)
    hints = retrieve_hints(spec + "\n" + plan_str)
    prompt = (
        f"Specifica originale:\n{spec}\n\n"
        f"Piano di implementazione:\n{plan_str}\n\n"
    )
    if hints:
        prompt += hints + "\n"
    if retry_note:
        prompt += (
            f"Un tentativo precedente per questa stessa specifica è rimasto "
            f"bloccato sullo stesso errore per più iterazioni consecutive:\n"
            f"{retry_note}\n\n"
            "Genera un'implementazione DIVERSA da zero (approccio o struttura "
            "del codice diversi), non una piccola variazione del tentativo "
            "precedente, evitando di ripetere lo stesso errore.\n\n"
        )
    prompt += (
        "Genera il codice Chisel 3 completo e compilabile.\n"
        "Ricorda: no markdown fence, solo codice Scala."
    )

    code = agent.run(prompt, temperature=temperature)
    code = extract_scala_code(code)

    ok(f"Codice generato: {len(code)} caratteri, "
       f"{code.count(chr(10))+1} righe")
    return code

# Terzo e quarto agente: Reviewer e Fixer lavorano in un loop.
# Il Reviewer valuta il codice generato, se ci sono problemi il Fixer li corregge. Questo ciclo si ripete fino a quando il codice passa la revisione o si raggiunge il numero massimo di iterazioni.
# Rispetto alla versione precedente, il loop integra tre idee da lavori sullo
# stato dell'arte per la generazione di HDL via LLM:
#   - RTLFixer: la base di conoscenza di errori noti (retrieve_hints) viene
#     iniettata nel prompt del Fixer insieme alle righe di errore isolate
#     (extract_failure_lines), non solo l'output grezzo di sbt.
#   - ReChisel: se la firma dell'errore (error_signature) resta identica per
#     2 iterazioni di fila, il Fixer è bloccato in un loop senza progressi —
#     "meccanismo di escape": invece di un altro fix in-place, si rigenera il
#     modulo da zero via run_coder.
#   - MAGE: la rigenerazione di escape usa temperatura più alta (0.7 invece
#     di 0.1) per ottenere un candidato davvero diverso, non una variazione
#     minima dello stesso tentativo bloccato.
def run_review_fix_loop(
    code:      str,
    spec:      str,
    plan:      dict,
    reviewer:  Agent,
    fixer:     Agent,
    coder:     Agent,
    toolchain: Toolchain,
    stem:      str,
    max_iter:  int
) -> tuple[str, list[dict]]:
    agent_step("REVIEWER/FIXER", f"Loop review → fix (max {max_iter} iterazioni)")

    iteration_log: list[dict] = []
    last_signature: str | None = None
    stuck_count = 0

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
        compile_ok, compile_out = toolchain.compile_module(code, stem)
        if toolchain.sbt_available:
            if compile_ok:
                ok("sbt compile: OK")
            else:
                warn("sbt compile: ERRORI")
                print(f"  {DIM}…{tail(compile_out, 300)}{RESET}")
        else:
            info("sbt non disponibile — solo revisione LLM")

# Verifica non-LLM che il codice usi davvero il Bundle MXFP4: run reali hanno
# mostrato il Reviewer accettare codice UInt semplice senza segnalarlo, quindi
# non ci si può fidare solo del suo giudizio per questo requisito.
        mxfp4_ok = check_mxfp4_usage(code)
        if not mxfp4_ok:
            warn("Codice non usa il Bundle MXFP4 (import mxfp4._ assente)")

# Log iterazione.
        log_entry: dict = {
            "iterazione":      i,
            "review_llm":      review_result,
            "review_llm_pass": passed_llm,
            "compile_ok":      compile_ok,
            "compile_output":  tail(compile_out, 1200) if compile_out else "",
            "mxfp4_ok":        mxfp4_ok,
            "fix_applicato":   False,
            "escaped":         False,
            "diagnosi":        "",
            "esito":           "",
        }

# Esito.
        tutto_ok = passed_llm and compile_ok and mxfp4_ok

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

# Rilevazione loop senza progressi (ReChisel): stessa firma d'errore 2 volte di fila.
# Il marcatore MXFP4_MANCANTE è aggiunto direttamente alla firma (non al testo
# sorgente) perché extract_failure_lines/tail potrebbero altrimenti tagliarlo
# via prima che arrivi a error_signature.
        current_error_text = compile_out if not compile_ok else review_result
        signature = error_signature(current_error_text)
        if not mxfp4_ok:
            signature += "|MXFP4_MANCANTE"
        stuck_count = stuck_count + 1 if signature == last_signature else 0
        last_signature = signature

        if stuck_count >= 2:
            agent_step("ESCAPE", f"Stesso errore per {stuck_count + 1} iterazioni: "
                                  f"rigenero il modulo da zero (iterazione {i})")
            reviewer.reset_history()
            fixer.reset_history()
            retry_note = CRITICAL_REMINDERS + tail(current_error_text, 800)
            if not mxfp4_ok:
                retry_note = (
                    "Il tentativo precedente NON usava affatto il Bundle MXFP4 "
                    "(nessun 'import mxfp4._', ingressi/uscite dichiarati come UInt "
                    "semplici invece che MXFP4) — questo va corretto nel nuovo tentativo.\n\n"
                ) + retry_note
            code = run_coder(
                plan, spec, coder,
                retry_note=retry_note,
                temperature=0.7
            )
            log_entry["escaped"] = True
            log_entry["esito"]   = "ESCAPED_RESTART"
            iteration_log.append(log_entry)
            stuck_count = 0
            last_signature = None
            continue

# Fix
        agent_step("FIXER", f"Correzione automatica (iterazione {i})")

        hints = retrieve_hints(current_error_text)
        fix_prompt = f"Codice con problemi:\n{code}\n\n"
        if not mxfp4_ok:
            fix_prompt += (
                "PROBLEMA CRITICO: il codice NON usa affatto il formato MXFP4 "
                "richiesto dalla specifica — manca 'import mxfp4._' e gli ingressi/"
                "uscite sono dichiarati come UInt semplici invece che come MXFP4 "
                "('Input(new MXFP4)'/'Output(new MXFP4)'). Riscrivi il modulo "
                "usando il Bundle MXFP4 per gli ingressi/uscite indicati come "
                "MXFP4 nella specifica/piano.\n\n"
            )
        if not passed_llm:
            fix_prompt += f"Problemi rilevati da LLM Reviewer:\n{review_result}\n\n"
        if not compile_ok and toolchain.sbt_available:
            failure_lines = extract_failure_lines(compile_out)
            if failure_lines:
                fix_prompt += f"Righe di errore rilevanti:\n{failure_lines}\n\n"
            fix_prompt += (
                f"Errori di compilazione sbt (coda dell'output, dove sbt "
                f"stampa gli '[error]' veri):\n{tail(compile_out, 2500)}\n\n"
            )
        if hints:
            fix_prompt += hints + "\n"
        fix_prompt += (
            "Correggi TUTTI i problemi elencati e restituisci "
            "il codice Chisel completo e corretto."
        )

        fix_response = fixer.run(fix_prompt)
        diagnosis    = extract_diagnosis(fix_response)
        code         = extract_scala_code(fix_response)
        if diagnosis:
            info(f"Diagnosi Fixer: {diagnosis}")
        ok(f"Codice corretto: {len(code)} caratteri")

        log_entry["fix_applicato"] = True
        log_entry["diagnosi"]      = diagnosis
        log_entry["esito"]         = "FIXED_CONTINUE"
        iteration_log.append(log_entry)

    return code, iteration_log

# Quinto agente: il Tester genera un testbench ChiselTest/ScalaTest completo, coprendo casi base, zero, massimo, overflow e simmetria.
# 'temperature' propagata come per run_coder: usata più alta quando il Tester
# viene richiamato dal meccanismo di escape per rigenerare una coppia
# modulo+testbench coerente da zero (vedi run_verilator_loop).
def run_tester(code: str, plan: dict, agent: Agent, temperature: float = 0.1) -> str:
    agent_step("TESTER", "Generazione testbench ChiselTest")

    plan_str = json.dumps(plan, ensure_ascii=False, indent=2)
    tb = agent.run(
        f"Piano del modulo:\n{plan_str}\n\n"
        f"Codice Chisel del modulo:\n{code}\n\n"
        "Genera il testbench ChiselTest completo.\n"
        "Ricorda: no markdown fence, solo codice Scala.",
        temperature=temperature
    )
    tb = extract_scala_code(tb)
    tb_pulito = strip_duplicate_module(tb, plan.get("nome_modulo", ""))
    if tb_pulito != tb:
        warn("Il Tester ha ridefinito il modulo nel testbench — rimossa la duplicazione")
        tb = tb_pulito
    ok(f"Testbench generato: {len(tb)} caratteri")
    return tb

# Sesto step: esegue davvero il testbench in simulazione tramite Verilator
# (sbt test + VerilatorBackendAnnotation). Se un test fallisce (o il modulo
# non compila più insieme al testbench), il Fixer corregge il modulo e si
# ripete l'esecuzione, fino a max_iter iterazioni o al primo PASS.
#
# Come run_review_fix_loop, integra il meccanismo di escape (ReChisel) con
# restart ad alta temperatura (MAGE) e la base di conoscenza errori (RTLFixer).
# Una differenza importante rispetto a prima: il Fixer NON perde più la
# memoria a ogni iterazione (era un reset_history() qui ma non nell'altro
# loop — inconsistenza che vanificava il principio "impara dai tentativi
# precedenti" di ReChisel). L'altra differenza: in precedenza il testbench era
# trattato come fisso e "non modificabile" per tutta la durata del loop, ma
# l'ultima run reale ha mostrato il modulo finale collassare in una copia del
# testbench — segno che a volte l'errore origina nel testbench, non nel
# modulo. Invece di far indovinare al Fixer quale file è colpa (euristica
# fragile), quando scatta l'escape si rigenerano insieme modulo E testbench
# da run_coder/run_tester, così restano garantiti coerenti tra loro.
def run_verilator_loop(
    code:      str,
    testbench: str,
    spec:      str,
    plan:      dict,
    coder:     Agent,
    tester:    Agent,
    fixer:     Agent,
    toolchain: Toolchain,
    stem:      str,
    max_iter:  int
) -> tuple[str, str, list[dict]]:
    agent_step("VERILATOR", f"Esecuzione test in simulazione (max {max_iter} iterazioni)")

    if not toolchain.sbt_available:
        info("sbt non disponibile — esecuzione test su Verilator saltata")
        return code, testbench, []
    if not toolchain.verilator_available:
        info("verilator non trovato (né nativamente né in WSL) — esecuzione test saltata "
             "(installa Verilator per la verifica funzionale reale)")
        return code, testbench, []

    iteration_log: list[dict] = []
    last_signature: str | None = None
    stuck_count = 0

    for i in range(1, max_iter + 1):
        print(f"\n  {CYAN}── Iterazione {i}/{max_iter} ──{RESET}")

        test_ok, test_out = toolchain.run_tests(code, testbench, stem)
        if test_ok:
            ok("sbt test (Verilator): OK")
        else:
            warn("sbt test (Verilator): FALLITO")
            print(f"  {DIM}…{tail(test_out, 300)}{RESET}")

        mxfp4_ok = check_mxfp4_usage(code)
        if not mxfp4_ok:
            warn("Codice non usa il Bundle MXFP4 (import mxfp4._ assente)")

        log_entry: dict = {
            "iterazione":        i,
            "verilator_ok":      test_ok,
            "verilator_output":  tail(test_out, 1200) if test_out else "",
            "mxfp4_ok":          mxfp4_ok,
            "fix_applicato":     False,
            "escaped":           False,
            "diagnosi":          "",
            "esito":             "",
        }

        if test_ok and mxfp4_ok:
            ok(f"Test verificati su Verilator all'iterazione {i}")
            log_entry["esito"] = "PASS"
            iteration_log.append(log_entry)
            break

        if i == max_iter:
            warn(f"Raggiunto limite iterazioni ({max_iter}) — uso l'ultimo codice")
            log_entry["esito"] = "MAX_ITER_REACHED"
            iteration_log.append(log_entry)
            break

# Rilevazione loop senza progressi (ReChisel): stessa firma d'errore 2 volte di fila.
        signature = error_signature(test_out)
        if not mxfp4_ok:
            signature += "|MXFP4_MANCANTE"
        stuck_count = stuck_count + 1 if signature == last_signature else 0
        last_signature = signature

        if stuck_count >= 2:
            agent_step("ESCAPE", f"Stesso errore per {stuck_count + 1} iterazioni: "
                                  f"rigenero modulo e testbench da zero (iterazione {i})")
            fixer.reset_history()
            retry_note = CRITICAL_REMINDERS + tail(test_out, 800)
            if not mxfp4_ok:
                retry_note = (
                    "Il tentativo precedente NON usava affatto il Bundle MXFP4 "
                    "(nessun 'import mxfp4._', ingressi/uscite dichiarati come UInt "
                    "semplici invece che MXFP4) — questo va corretto nel nuovo tentativo.\n\n"
                ) + retry_note
            code = run_coder(plan, spec, coder, retry_note=retry_note, temperature=0.7)
# Il Tester NON usa temperatura alta come il Coder: il testbench ha requisiti
# strutturali rigidi (ChiselTest + VerilatorBackendAnnotation, niente API
# alternative). Una run reale con temperature=0.7 ha fatto derivare il Tester
# verso 'chisel3.iotesters.PeekPokeTester', un'API deprecata e incompatibile
# con l'intero toolchain — qui vogliamo diversità nel modulo, non nel modo in
# cui il testbench è strutturato.
            testbench = run_tester(code, plan, tester, temperature=0.2)
            log_entry["escaped"] = True
            log_entry["esito"]   = "ESCAPED_RESTART"
            iteration_log.append(log_entry)
            stuck_count = 0
            last_signature = None
            continue

        agent_step("FIXER", f"Correzione automatica post-Verilator (iterazione {i})")

        failure_lines = extract_failure_lines(test_out)
        hints         = retrieve_hints(test_out)
        fix_prompt = (
            f"Codice del modulo:\n{code}\n\n"
            f"Testbench (NON modificabile, deve restare compatibile con l'io del modulo):\n"
            f"{testbench}\n\n"
        )
        if not mxfp4_ok:
            fix_prompt += (
                "PROBLEMA CRITICO: il modulo NON usa affatto il formato MXFP4 "
                "richiesto dalla specifica — manca 'import mxfp4._' e gli ingressi/"
                "uscite sono dichiarati come UInt semplici invece che come MXFP4. "
                "Riscrivi il modulo usando il Bundle MXFP4 per gli ingressi/uscite "
                "indicati come MXFP4 nella specifica/piano, mantenendo la "
                "compatibilità con il testbench.\n\n"
            )
        if failure_lines:
            fix_prompt += f"Righe di errore rilevanti:\n{failure_lines}\n\n"
        fix_prompt += (
            f"Errore da 'sbt test' in simulazione Verilator (coda dell'output, "
            f"dove sbt stampa gli '[error]' veri e il riepilogo dei test "
            f"falliti):\n{tail(test_out, 2500)}\n\n"
        )
        if hints:
            fix_prompt += hints + "\n"
        fix_prompt += (
            "Correggi il modulo Chisel affinché compili insieme al testbench "
            "e la simulazione su Verilator passi. Restituisci SOLO il codice "
            "completo e corretto del modulo (non il testbench)."
        )
        fix_response = fixer.run(fix_prompt)
        diagnosis    = extract_diagnosis(fix_response)
        code         = extract_scala_code(fix_response)
        if diagnosis:
            info(f"Diagnosi Fixer: {diagnosis}")
        ok(f"Codice corretto: {len(code)} caratteri")

        log_entry["fix_applicato"] = True
        log_entry["diagnosi"]      = diagnosis
        log_entry["esito"]         = "FIXED_CONTINUE"
        iteration_log.append(log_entry)

    return code, testbench, iteration_log

# Salvataggio di tutti gli outputs in una directory timestamped, con un vero
# layout di progetto SBT (src/main/scala, src/test/scala) così "sbt test" nella
# directory di output funziona davvero. Include codice Chisel, il Bundle MXFP4
# condiviso, testbench, report Markdown, log JSON, build.sbt e README.
def save_outputs(
    spec:               str,
    plan:               dict,
    code:               str,
    testbench:          str,
    iter_log:           list[dict],
    verilator_log:      list[dict],
    model:              str,
    sbt_available:      bool,
    verilator_available: bool
) -> Path:
    step(6, "Salvataggio artefatti")

    stem  = re.sub(r"[^a-zA-Z0-9_]", "_",
                   plan.get("nome_modulo", "MxFp4Unit"))
    msafe = model.replace(":", "_").replace("/", "_")
    ts    = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    out   = Path(f"chisel_output_{stem}_{msafe}_{ts}")

    src_main = out / "src" / "main" / "scala"
    src_test = out / "src" / "test" / "scala"
    mxfp4_dir = src_main / "mxfp4"
    mxfp4_dir.mkdir(parents=True, exist_ok=True)
    src_test.mkdir(parents=True, exist_ok=True)

    hdr = (
        "// ═══════════════════════════════════════════════════════════\n"
        "//  Generato da: agentic_chisel_mxfp4_ollama.py\n"
        f"//  Modello Ollama: {model}\n"
        f"//  Data: {datetime.datetime.now().isoformat(timespec='seconds')}\n"
        "// ═══════════════════════════════════════════════════════════\n\n"
    )

# Bundle MXFP4 condiviso
    (mxfp4_dir / "MXFP4.scala").write_text(MXFP4_SCALA, encoding="utf-8")
    ok(f"Bundle MXFP4    → {out}/src/main/scala/mxfp4/MXFP4.scala")

# Modulo Chisel
    (src_main / f"{stem}.scala").write_text(hdr + code, encoding="utf-8")
    ok(f"Modulo Chisel   → {out}/src/main/scala/{stem}.scala")

# Testbench
    (src_test / f"{stem}Test.scala").write_text(hdr + testbench, encoding="utf-8")
    ok(f"Testbench       → {out}/src/test/scala/{stem}Test.scala")

# build.sbt
    (out / "build.sbt").write_text(BUILD_SBT, encoding="utf-8")
    ok(f"build.sbt       → {out}/build.sbt")

# Report Markdown
    n_fix       = sum(1 for it in iter_log if it.get("fix_applicato"))
    n_fix_veri  = sum(1 for it in verilator_log if it.get("fix_applicato"))

    n_escape_review = sum(1 for it in iter_log if it.get("escaped"))
    n_escape_veri   = sum(1 for it in verilator_log if it.get("escaped"))

    iters_md = ""
    for it in iter_log:
        label = "ESCAPE (rigenerato da zero)" if it.get("escaped") else it['esito']
        iters_md += (
            f"\n#### Iterazione {it['iterazione']} "
            f"`{label}`\n\n"
            f"| Verifica | Risultato |\n|---|---|\n"
            f"| LLM Reviewer | `{'PASS' if it['review_llm_pass'] else 'ISSUES'}` |\n"
            f"| sbt compile  | `{'OK' if it['compile_ok'] else 'FAIL'}` |\n"
            f"| Usa MXFP4    | `{'SI' if it.get('mxfp4_ok', True) else 'NO'}` |\n"
            f"| Fix applicato | `{it['fix_applicato']}` |\n"
        )
        if it.get("diagnosi"):
            iters_md += f"\n**Diagnosi Fixer:** {it['diagnosi']}\n"
        if it.get("fix_applicato") and it.get("review_llm"):
            excerpt = it["review_llm"][:400]
            iters_md += f"\n**Issues rilevati:**\n```\n{excerpt}\n```\n"

    veri_md = ""
    for it in verilator_log:
        label = "ESCAPE (modulo+testbench rigenerati)" if it.get("escaped") else it['esito']
        veri_md += (
            f"\n#### Iterazione {it['iterazione']} "
            f"`{label}`\n\n"
            f"| Verifica | Risultato |\n|---|---|\n"
            f"| sbt test (Verilator) | `{'OK' if it['verilator_ok'] else 'FAIL'}` |\n"
            f"| Usa MXFP4    | `{'SI' if it.get('mxfp4_ok', True) else 'NO'}` |\n"
            f"| Fix applicato | `{it['fix_applicato']}` |\n"
        )
        if it.get("diagnosi"):
            veri_md += f"\n**Diagnosi Fixer:** {it['diagnosi']}\n"
        if it.get("fix_applicato") and it.get("verilator_output"):
            excerpt = it["verilator_output"][:400]
            veri_md += f"\n**Output Verilator:**\n```\n{excerpt}\n```\n"
    if not veri_md:
        veri_md = "\n_Esecuzione test su Verilator saltata (sbt e/o verilator non disponibili)._\n"

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
        f"| **Fix automatici applicati (review/fix)** | {n_fix} |\n"
        f"| **Escape (rigenerazioni da zero, review/fix)** | {n_escape_review} |\n"
        f"| **Iterazioni test/fix (Verilator)** | {len(verilator_log)} |\n"
        f"| **Fix automatici applicati (Verilator)** | {n_fix_veri} |\n"
        f"| **Escape (rigenerazioni da zero, Verilator)** | {n_escape_veri} |\n"
        f"| **Compilazione sbt** | "
        f"{'Abilitata' if sbt_available else 'Non disponibile (solo LLM review)'} |\n"
        f"| **Simulazione test (Verilator)** | "
        f"{'Abilitata' if (sbt_available and verilator_available) else 'Non disponibile'} |\n\n"
        f"---\n\n"
        f"## Specifica Originale\n\n{spec}\n\n"
        f"---\n\n"
        f"## Piano di Implementazione (Planner Agent)\n\n"
        f"```json\n{json.dumps(plan, ensure_ascii=False, indent=2)}\n```\n\n"
        f"### Algoritmo pianificato\n\n{algo_md}\n"
        f"---\n\n"
        f"## Log Agentico — Review/Fix Loop (sbt compile)\n{iters_md}\n"
        f"---\n\n"
        f"## Log Agentico — Verifica funzionale (sbt test + Verilator)\n{veri_md}\n"
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
                "iterazioni_review_fix":      len(iter_log),
                "fix_applicati_review_fix":   n_fix,
                "escape_review_fix":          n_escape_review,
                "esito_finale_review_fix":    iter_log[-1]["esito"] if iter_log else "N/A",
                "iterazioni_verilator":       len(verilator_log),
                "fix_applicati_verilator":    n_fix_veri,
                "escape_verilator":           n_escape_veri,
                "esito_finale_verilator":     verilator_log[-1]["esito"] if verilator_log else "N/A",
            },
            "iterations":          iter_log,
            "verilator_iterations": verilator_log,
        }, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    ok(f"Log JSON        → {out}/agent_log.json")

# README
    (out / "README.md").write_text(
        f"# {stem} — Chisel MXFP4\n\n"
        f"Generato da **agentic_chisel_mxfp4_ollama.py** con modello `{model}`.\n\n"
        f"## Compilazione e test\n\n"
        f"I test sono scritti con ChiselTest e girano in simulazione tramite "
        f"**Verilator** (annotazione `VerilatorBackendAnnotation`): assicurati "
        f"che `verilator` sia installato e nel PATH prima di lanciare `sbt test` "
        f"(su Windows: via WSL o MSYS2/Cygwin).\n\n"
        f"```bash\nsbt test\n```\n\n"
        f"## File generati\n\n"
        f"| File | Descrizione |\n|---|---|\n"
        f"| `src/main/scala/mxfp4/MXFP4.scala` | Bundle MXFP4 condiviso (sign/exp/mant + encode/decode) |\n"
        f"| `src/main/scala/{stem}.scala` | Modulo Chisel MXFP4 |\n"
        f"| `src/test/scala/{stem}Test.scala` | Testbench ChiselTest (backend Verilator) |\n"
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
                                         help=f"Max iterazioni review/fix e test/fix (default: {MAX_FIX_ITER})")
    parser.add_argument("--verbose", "-v", action="store_true", help="Output dettagliato")
    args = parser.parse_args()

# Setup iniziale: verifica Ollama, scegli modello, acquisisci specifica e inizializza il toolchain (sbt + verilator).
    available = check_ollama(args.host)
    model     = choose_model(available, args.model)
    spec      = get_specification(args.file, args.spec)
    toolchain = Toolchain()

    if toolchain.sbt_available:
        ok("sbt trovato → compilazione reale abilitata nel loop")
    else:
        info("sbt non trovato → solo LLM review nel loop  "
             "(installa sbt da https://www.scala-sbt.org per compilazione reale)")

    if toolchain.sbt_available and toolchain.verilator_available:
        if toolchain.use_wsl_verilator:
            ok("verilator trovato in WSL → test eseguiti tramite bridge wsl.exe (sbt test)")
        else:
            ok("verilator trovato → esecuzione reale dei test in simulazione abilitata")
    elif toolchain.sbt_available:
        info("verilator non trovato (né nativamente né in WSL) → esecuzione test saltata  "
             "(su Windows installalo tramite WSL o MSYS2/Cygwin)")

    hr()
    print(f"\n  {BOLD}Pipeline agentica:{RESET}  "
          f"Planner → Coder → [Reviewer ⟷ Fixer]×{args.iter} → Tester → "
          f"[Verilator ⟷ Fixer]×{args.iter}\n")

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
        code, spec, plan, reviewer, fixer, coder,
        toolchain, stem_safe, args.iter
    )

    testbench = run_tester(code, plan, tester)

    code, testbench, verilator_log = run_verilator_loop(
        code, testbench, spec, plan, coder, tester, fixer,
        toolchain, stem_safe, args.iter
    )

    out_dir   = save_outputs(
        spec, plan, code, testbench,
        iter_log, verilator_log, model,
        toolchain.sbt_available, toolchain.verilator_available
    )

    elapsed_total = (datetime.datetime.now() - t_global).total_seconds()

# Output finale e statistiche.
    hr()
    n_fix       = sum(1 for it in iter_log if it.get("fix_applicato"))
    n_fix_veri  = sum(1 for it in verilator_log if it.get("fix_applicato"))
    n_esc       = sum(1 for it in iter_log if it.get("escaped"))
    n_esc_veri  = sum(1 for it in verilator_log if it.get("escaped"))
    esito       = iter_log[-1]["esito"] if iter_log else "N/A"
    esito_s     = f"{GREEN}PASS{RESET}" if esito == "PASS" else f"{YELLOW}{esito}{RESET}"
    esito_veri  = verilator_log[-1]["esito"] if verilator_log else "N/A"
    esito_veri_s = f"{GREEN}PASS{RESET}" if esito_veri == "PASS" else f"{YELLOW}{esito_veri}{RESET}"

    print(f"""
{GREEN}{BOLD} Pipeline agentica completata in {elapsed_total:.0f}s!{RESET}

  {BOLD}Statistiche:{RESET}
      • Agenti eseguiti:        5  (Planner, Coder, Reviewer, Fixer, Tester)
      • Iterazioni review/fix:  {len(iter_log)}  (fix: {n_fix}, escape: {n_esc})  →  {esito_s}
      • Iterazioni test/fix:    {len(verilator_log)}  (fix: {n_fix_veri}, escape: {n_esc_veri})  →  {esito_veri_s}
      • sbt compilazione:       {'✔ abilitata' if toolchain.sbt_available else '⚠ non disponibile'}
      • Simulazione Verilator:  {'✔ abilitata' if (toolchain.sbt_available and toolchain.verilator_available) else '⚠ non disponibile'}

  {BOLD}Output:{RESET}  {BOLD}{out_dir}/{RESET}
      ├── src/main/scala/mxfp4/MXFP4.scala   ← Bundle MXFP4 condiviso
      ├── src/main/scala/{stem_safe}.scala   ← Modulo Chisel MXFP4
      ├── src/test/scala/{stem_safe}Test.scala  ← Testbench ChiselTest (backend Verilator)
      ├── report_{stem_safe}.md              ← Report per la tesi
      ├── agent_log.json                     ← Log JSON del workflow agentico
      ├── build.sbt                          ← Progetto SBT
      └── README.md

  {CYAN}Compila e testa (richiede verilator nel PATH):{RESET}
      cd {out_dir}
      sbt test
""")
    hr()


if __name__ == "__main__":
    main()