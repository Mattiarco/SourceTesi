"""Testi di conoscenza iniettati nei prompt degli agenti.

Sono la parte "prompt engineering" del sistema: un LLM generico sbaglia quasi
sempre i dettagli di MXFP4 (bias, subnormali, saturazione, ordine dei nibble).
Fornirli esplicitamente sposta il modello dal "ricordare" al "applicare".
"""

MXFP4_SPEC = r"""
### FORMATO MXFP4 (OCP Microscaling, rif. NVIDIA MXFP4/NVFP4)

Un blocco MXFP4 = K elementi FP4 **E2M1** + 1 fattore di scala condiviso **E8M0**.
K standard = 32 (MXFP4). NVFP4 usa K = 16 e scala E4M3 — non confonderli.

**Elemento E2M1 (4 bit)**
  bit [3]   = segno
  bit [2:1] = esponente, bias = 1
  bit [0]   = mantissa
  Decodifica:
    exp == 0 && man == 0  ->  +/- 0
    exp == 0 && man == 1  ->  +/- 0.5        (SUBNORMALE, valore = 2^0 * 0.5)
    exp != 0              ->  +/- 2^(exp-1) * (1 + man/2)
  Valori rappresentabili (magnitudine): 0, 0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 6.0
  NON esistono Inf né NaN in E2M1: l'overflow SATURA a +/-6.0.
  Esiste lo zero negativo (0b1000).

**Scala E8M0 (8 bit, senza segno, senza mantissa)**
  X == 0xFF -> NaN (il blocco intero è NaN)
  altrimenti valore = 2^(X - 127);  X = 127 -> 1.0
  È sempre una potenza esatta di 2: in hardware si somma agli esponenti, non
  serve alcun moltiplicatore.

**Regola d'oro implementativa**
  Poiché ogni magnitudine E2M1 è un multiplo di 0.5, l'insieme
  {0,1,2,3,4,6,8,12} = magnitudine*2 è INTERO su 4 bit. Quindi:
    - un moltiplicatore E2M1 x E2M1 è una LUT/decodifica combinatoria a 3x3 bit
      (prodotto in unità 1/4, max 6*6 = 36 -> 8 bit con segno);
    - un dot-product su K elementi è una somma INTERA esatta, poi un unico
      allineamento con (scaleA - 127) + (scaleB - 127).
  Questo evita del tutto la logica di normalizzazione per elemento ed è
  l'approccio da preferire salvo richiesta esplicita contraria.

**Packing in memoria**
  2 elementi per byte. Convenzione usata da questo progetto: l'elemento di
  indice PARI sta nel nibble BASSO. Un vettore da 32 elementi = 128 bit, con
  l'elemento i nei bit [4*i+3 : 4*i].

**Quantizzazione FP32 -> blocco MXFP4 (OCP)**
  amax        = max |x_i|
  shared_exp  = floor(log2(amax)) - emax_elem,  con emax_elem = 2 per E2M1
  scale_E8M0  = clamp(shared_exp + 127, 0, 254)
  elemento_i  = round_to_nearest_even(x_i / 2^(scale-127)) con saturazione a +/-6
"""

CHISEL_RULES = r"""
### REGOLE CHISEL (Chisel 6.x / Scala 2.13) — vincolanti

1. Import minimi e corretti:
     import chisel3._
     import chisel3._; import chisel3.util._      // per Cat, Fill, MuxLookup, log2Ceil
   NON usare `chisel3.experimental._` né `Chisel._` (API deprecata).
2. Il modulo estende `Module`, l'IO è `val io = IO(new Bundle { ... })`.
   Ogni campo di output DEVE essere assegnato in ogni percorso (usa un default
   `io.out := 0.U` in testa) altrimenti l'elaborazione fallisce con
   "not fully initialized".
3. Larghezze SEMPRE esplicite: `UInt(8.W)`, `SInt(12.W)`, `0.U(4.W)`.
   Le operazioni SInt/UInt non si mischiano: converti con `.asSInt` / `.asUInt`.
4. `Vec` per gli array hardware: `Vec(32, UInt(4.W))`; per costanti software usa
   `VecInit(Seq(...))` o un `Seq` Scala + `MuxLookup`.
5. Firma corretta in Chisel 6: `MuxLookup(key, default)(Seq(k -> v, ...))`.
   `Cat` concatena con il PRIMO argomento nei bit più significativi.
6. Slicing: `x(hi, lo)` è inclusivo su entrambi gli estremi.
7. Registri: `RegInit(0.U(8.W))`, `RegNext(x, 0.U)`. Reset sincrono di default.
8. Somma di molti termini: usa `+&` (estende la larghezza, niente overflow
   silenzioso) dentro un albero bilanciato scritto a mano:
     def adderTree(xs: Seq[SInt]): SInt = xs match {
       case Seq(one) => one
       case _ => val (l, r) = xs.splitAt(xs.length / 2); adderTree(l) +& adderTree(r)
     }
   NON usare `Seq.reduceTree`: non esiste su `Seq`, solo su `Vec`.
9. Il main di elaborazione deve essere:
     object <Nome>Main extends App {
       println(circt.stage.ChiselStage.emitSystemVerilog(
         new <Nome>, firtoolOpts = Array("-disable-all-randomization",
                                          "-strip-debug-info")))
     }
   ma in questo progetto l'elaborazione è già gestita dallo scaffold: emetti
   SOLO il file del modulo, senza `object ...Main`.
10. Vietato: `printf` in logica sintetizzabile, `require` su valori hardware,
    `.litValue` su segnali non costanti, `when` senza `.otherwise` su output.
"""

VERILOG_RULES = r"""
### REGOLE SYSTEMVERILOG — vincolanti
1. `module <Nome> ( ... );` con porte ANSI e tipi espliciti (`logic`).
2. Sempre `always_comb` / `always_ff @(posedge clk)`; mai `always @(*)` misto.
3. Nessun latch: in `always_comb` assegna un default a tutti gli output.
4. Reset attivo alto sincrono `rst` salvo diversa indicazione.
5. Niente costrutti non supportati da Verilator (`#delay` nella logica,
   `initial` in RTL sintetizzabile, `real`).
6. Larghezze esplicite ovunque; niente conversioni implicite firmate/non firmate.
"""

VERILATOR_TB_RULES = r"""
### REGOLE TESTBENCH C++ PER VERILATOR — vincolanti
1. File singolo `tb_<Nome>.cpp` con:
     #include "V<Nome>.h"
     #include "verilated.h"
     #include <cstdio>, <cstdint>, <cstdlib>, <vector>
2. `int main(int argc, char** argv)`:
     Verilated::commandArgs(argc, argv);
     auto* dut = new V<Nome>;
   Alla fine: `dut->final(); delete dut; return failures ? 1 : 0;`
3. Se il DUT è sequenziale definisci un helper:
     auto tick = [&]{ dut->clk = 0; dut->eval(); dut->clk = 1; dut->eval(); };
   e applica il reset per almeno 2 cicli prima dei vettori.
   Se è puramente combinatorio NON pilotare clk/rst: basta `dut->eval()`.
4. I vettori di test attesi arrivano dal golden model Python e sono inclusi come
   `#include "test_vectors.h"` (array C generato automaticamente). NON
   ricalcolare la semantica MXFP4 in C++: usa i valori attesi forniti.
5. Ogni confronto stampa su fallimento:
     printf("FAIL [%d] exp=0x%llx got=0x%llx\n", i, (unsigned long long)exp,
            (unsigned long long)got);
6. In fondo stampa ESATTAMENTE una di queste righe (il tester le cerca):
     printf("TEST PASSED (%d vectors)\n", n);
     printf("TEST FAILED (%d/%d)\n", failures, n);
7. Segnali > 64 bit in Verilator sono array `WData` a 32 bit: accedi con
   `dut->porta[k]`. Segnali <= 32 bit sono `uint32_t`, <= 64 bit `uint64_t`.
8. Niente `std::cout` con `std::endl` in loop stretti; usa printf.
"""

COMMON_PITFALLS = r"""
### ERRORI RICORRENTI DA EVITARE (osservati su LLM in MXFP4/HDL)
- Confondere il bias E2M1 (=1) con quello FP16 (=15) o E8M0 (=127).
- Dimenticare il subnormale 0b0001 = 0.5: se lo si tratta come 0 tutti i
  vettori di test falliscono in modo sottile.
- Trattare E2M1 come se avesse Inf/NaN: NON li ha, si satura a 6.
- Moltiplicare per la scala con un moltiplicatore FP invece di sommare
  esponenti (spreco enorme di area, e introduce errori di arrotondamento).
- Invertire l'ordine dei nibble nel packing.
- Sommare i prodotti in una larghezza troppo stretta: con K=32, max |acc| in
  unità 1/4 è 32*144 = 4608 -> servono 14 bit + segno.
- In Chisel, `+` tronca alla larghezza massima degli operandi: usa `+&`.
- Nel testbench, leggere un output combinatorio senza `eval()` dopo aver
  scritto gli input.
"""


def full_context(target: str = "chisel") -> str:
    """Blocco di conoscenza completo da iniettare nei prompt."""
    hdl = CHISEL_RULES if target == "chisel" else VERILOG_RULES
    return "\n".join([MXFP4_SPEC, hdl, VERILATOR_TB_RULES, COMMON_PITFALLS])
