"""Testbench Verilator deterministico, generato dal contratto delle porte.

Il testbench non contiene alcuna semantica MXFP4: legge ingressi e valori
attesi da `test_vectors.h` (golden model Python) e confronta bit a bit. Tutto
ciò che sa del modulo viene dal contratto (`knowledge/contracts.py`):

* combinatorio: per ogni vettore pilota gli ingressi, `eval()`, confronta;
* sequenziale: reset sincrono per 2 cicli, poi un vettore per ciclo con
  `validIn=1` (throughput 1); i risultati si raccolgono quando `validOut` è
  alto e devono arrivare in ordine e con latenza esattamente `latency_cycles`.

Alla prima discrepanza stampa ingressi, atteso e ottenuto; le successive (fino
a 10) in forma compatta. Con `--trace` (macro VM_TRACE di Verilator) scrive
`sim/waves.vcd`; con `--coverage` (VM_COVERAGE) scrive `sim/coverage.dat`.
L'ultima riga è sempre `TEST PASSED (n vectors)` o `TEST FAILED (f/n)`.

Rappresentazione Verilator delle porte: <=32 bit `IData`/`CData`/`SData`
(intero), 33..64 bit `QData` (uint64), >64 bit `VlWide` (array di parole a 32
bit, parola 0 = bit meno significativi). Gli accessori generati coprono i tre
casi, così il testbench vale per qualunque K.
"""
from __future__ import annotations

from ..knowledge.contracts import ELEMENT_BITS, contract_ports, has_contract


def _write_words(port: str, src: str, width: int) -> str:
    """Assegna la porta `io_<port>` dalle parole a 32 bit `src[]` (LSB in src[0])."""
    if width <= 32:
        return f"    dut->io_{port} = {src}[0];"
    if width <= 64:
        return f"    dut->io_{port} = ((uint64_t){src}[1] << 32) | {src}[0];"
    return f"    for (int w = 0; w < {(width + 31) // 32}; ++w) dut->io_{port}[w] = {src}[w];"


def _read_word(port: str, width: int) -> str:
    """Corpo di una funzione C che ritorna la parola `w` (32 bit) della porta."""
    if width <= 32:
        return f"return w == 0 ? (uint32_t)dut->io_{port} : 0u;"
    if width <= 64:
        return f"return (uint32_t)((uint64_t)dut->io_{port} >> (32 * w));"
    return f"return dut->io_{port}[w];"


def render_testbench(plan: dict) -> str:
    module = plan["module_name"]
    kernel = (plan.get("test_plan") or {}).get("kernel", "dot_product")
    if not has_contract(kernel):
        raise ValueError(f"nessun contratto di porte per il kernel {kernel!r}")
    k = int(plan.get("block_size", 32))
    seq = plan.get("clocking") == "sequential"
    latency = max(1, int(plan.get("latency_cycles") or 1)) if seq else 0
    ports = {p["name"]: p for p in contract_ports(kernel, k, "sequential" if seq else "comb")}
    two_inputs = "b" in ports
    elem_bits = ELEMENT_BITS.get(kernel)

    # ------------------------------------------------------------ pilotaggio
    drive = [_write_words("a", "v.a", ports["a"]["width"])]
    if two_inputs:
        drive.append(_write_words("b", "v.b", ports["b"]["width"]))
    drive.append("    dut->io_scaleA = v.scale_a;")
    if two_inputs:
        drive.append("    dut->io_scaleB = v.scale_b;")
    if seq:
        drive.append("    dut->io_validIn = 1;")
    drive_code = "\n".join(drive)

    # ------------------------------------------------------------- confronto
    # mode: 0 = silenzioso, 1 = dettaglio (prima discrepanza), 2 = compatto
    if kernel == "dot_product":
        helpers = ""
        check = """    const int32_t got_acc = sext(dut->io_accQ2, 32);
    const int32_t got_exp = sext(dut->io_expOut, 16);
    const unsigned got_nan = dut->io_isNaN & 1u;
    if (got_acc == v.exp_acc_q2 && got_exp == v.exp_shared && got_nan == v.nan) return true;
    if (mode == 1) {
        printf("  atteso  : accQ2=%d expOut=%d isNaN=%u   (valore reale %.17g)\\n",
               (int)v.exp_acc_q2, (int)v.exp_shared, (unsigned)v.nan, v.exp_real);
        printf("  ottenuto: accQ2=%d expOut=%d isNaN=%u\\n", got_acc, got_exp, got_nan);
    } else if (mode == 2) {
        printf("  accQ2 %d/%d  expOut %d/%d  isNaN %u/%u  (atteso/ottenuto)\\n",
               (int)v.exp_acc_q2, got_acc, (int)v.exp_shared, got_exp, (unsigned)v.nan, got_nan);
    }
    return false;"""
    else:
        per_word = 32 // elem_bits
        mask = "0xFFFFFFFFu" if elem_bits == 32 else f"0x{(1 << elem_bits) - 1:X}u"
        fmt = "0x%08x" if elem_bits == 32 else "0x%04x"
        helpers = f"""static uint32_t out_word(int w) {{
    {_read_word("out", ports["out"]["width"])}
}}

static uint32_t out_elem(int i) {{
    return (out_word(i / {per_word}) >> ({elem_bits} * (i % {per_word}))) & {mask};
}}
"""
        b_code = ("(v.b[i / 8] >> (4 * (i % 8))) & 0xFu" if two_inputs else "0u")
        check = f"""    const unsigned got_nan = dut->io_isNaN & 1u;
    int bad = -1, nbad = 0;
    for (int i = 0; i < MXFP4_K; ++i)
        if (out_elem(i) != (v.exp_elem[i] & {mask})) {{ if (bad < 0) bad = i; ++nbad; }}
    if (bad < 0 && got_nan == v.nan) return true;
    if (mode != 0 && bad >= 0) {{
        const int i = bad;
        const unsigned ca = (v.a[i / 8] >> (4 * (i % 8))) & 0xFu;
        const unsigned cb = {b_code};
        printf("  elemento %d (%d errati su %d): codice a=0x%x{' b=0x%x' if two_inputs else ''}  "
               "atteso={fmt} ottenuto={fmt}\\n", i, nbad, MXFP4_K, ca{', cb' if two_inputs else ''},
               (unsigned)(v.exp_elem[i] & {mask}), (unsigned)out_elem(i));
        (void)cb;
    }}
    if (mode != 0 && got_nan != v.nan)
        printf("  isNaN atteso=%u ottenuto=%u\\n", (unsigned)v.nan, got_nan);
    return false;"""

    if seq:
        body = """    // reset sincrono attivo alto per 2 cicli
    dut->reset = 1;
    dut->io_validIn = 0;
    tick();
    tick();
    dut->reset = 0;

    // Streaming: un vettore per ciclo. Dopo il tick() del ciclo c il vettore
    // c-(LATENCY-1) deve presentarsi in uscita con validOut alto.
    int next_out = 0;
    for (int c = 0; c < n + LATENCY + 4; ++c) {
        if (c < n) drive(MXFP4_VECTORS[c]);
        else dut->io_validIn = 0;
        tick();
        if (!(dut->io_validOut & 1u)) continue;
        if (next_out >= n) {
            printf("FAIL validOut alto al ciclo %d ma tutti i %d risultati sono gia' usciti\\n", c, n);
            ++failures;
            break;
        }
        const int lat = c - next_out + 1;
        if (lat != LATENCY) {
            printf("FAIL [%d] %s: risultato uscito con latenza %d cicli, attesa %d "
                   "(latency_cycles del piano)\\n", next_out, MXFP4_VECTORS[next_out].label,
                   lat, LATENCY);
            ++failures;
            break;
        }
        report(next_out, failures);
        ++next_out;
    }
    if (next_out < n && failures == 0) {
        printf("FAIL usciti %d risultati su %d: validOut deve essere validIn ritardato di %d "
               "cicli\\n", next_out, n, LATENCY);
        failures += n - next_out;
    }"""
    else:
        body = """    dut->clock = 0;
    dut->reset = 0;
    for (int i = 0; i < n; ++i) {
        drive(MXFP4_VECTORS[i]);
        dut->eval();
        dump();
        report(i, failures);
    }"""

    if two_inputs:
        print_b = """    printf("  b =");
    for (int w = MXFP4_WORDS - 1; w >= 0; --w) printf(" %08x", (unsigned)v.b[w]);
    printf("\\n");
"""
        scales = ('    printf("  ingresso: scaleA=0x%02x scaleB=0x%02x\\n", '
                  '(unsigned)v.scale_a, (unsigned)v.scale_b);')
    else:
        print_b = ""
        scales = '    printf("  ingresso: scaleA=0x%02x\\n", (unsigned)v.scale_a);'
    kind = f"sequenziale, latenza {latency}" if seq else "combinatorio"

    return f"""// GENERATO AUTOMATICAMENTE da mxfp4agent/toolchain/tbgen.py — non modificare.
// Testbench Verilator per {module}: kernel {kernel}, {kind}, K={k}.
// I valori attesi vengono da test_vectors.h (golden model Python).
#include "V{module}.h"
#include "verilated.h"
#if VM_TRACE
#include "verilated_vcd_c.h"
#endif
#if VM_COVERAGE
#include "verilated_cov.h"
#endif
#include "test_vectors.h"

#include <cstdint>
#include <cstdio>

#define LATENCY {latency}

static V{module}* dut;
#if VM_TRACE
static VerilatedVcdC* tfp = nullptr;
#endif
static uint64_t sim_time = 0;

static inline int32_t sext(uint64_t v, int width) {{
    const uint64_t m = 1ull << (width - 1);
    v &= (1ull << width) - 1;
    return (int32_t)(int64_t)((v ^ m) - m);
}}

static void dump() {{
#if VM_TRACE
    tfp->dump(sim_time);
#endif
    ++sim_time;
}}

static void tick() {{
    dut->clock = 0;
    dut->eval();
    dump();
    dut->clock = 1;
    dut->eval();
    dump();
}}

static void drive(const mxfp4_vec_t& v) {{
{drive_code}
}}

{helpers}
static void print_inputs(const mxfp4_vec_t& v) {{
{scales}
    printf("  a =");
    for (int w = MXFP4_WORDS - 1; w >= 0; --w) printf(" %08x", (unsigned)v.a[w]);
    printf("  (elemento 0 nei bit meno significativi)\\n");
{print_b}}}

// true se le uscite correnti corrispondono al vettore atteso
static bool check(const mxfp4_vec_t& v, int mode) {{
{check}
}}

static void report(int i, int& failures) {{
    const mxfp4_vec_t& v = MXFP4_VECTORS[i];
    if (check(v, 0)) return;
    ++failures;
    if (failures == 1) {{
        printf("FAIL [%d] %s — prima discrepanza:\\n", i, v.label);
        print_inputs(v);
        check(v, 1);
    }} else if (failures <= 10) {{
        printf("FAIL [%d] %s\\n", i, v.label);
        check(v, 2);
    }}
}}

int main(int argc, char** argv) {{
    Verilated::commandArgs(argc, argv);
#if VM_TRACE
    Verilated::traceEverOn(true);   // prima di costruire il modello
#endif
    dut = new V{module};
#if VM_TRACE
    tfp = new VerilatedVcdC;
    dut->trace(tfp, 99);
    tfp->open("sim/waves.vcd");
#endif
    int failures = 0;
    const int n = NUM_VECTORS;

{body}

#if VM_TRACE
    tfp->close();
#endif
#if VM_COVERAGE
    Verilated::defaultContextp()->coveragep()->write("sim/coverage.dat");
#endif
    dut->final();
    delete dut;
    if (failures) {{
        printf("TEST FAILED (%d/%d)\\n", failures, n);
        return 1;
    }}
    printf("TEST PASSED (%d vectors)\\n", n);
    return 0;
}}
"""
