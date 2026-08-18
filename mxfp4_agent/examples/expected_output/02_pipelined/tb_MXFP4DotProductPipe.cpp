// Testbench Verilator per MXFP4DotProductPipe (2 stadi, latenza 2 cicli).
//
// Verifica DUE proprieta':
//   1. correttezza numerica di ogni risultato;
//   2. che il pipeline accetti davvero un blocco per ciclo — i vettori sono
//      immessi back-to-back e i risultati raccolti con offset fisso di 2.
//
// Porte: Chisel appiattisce `io` in `io_<nome>`; `clock` e `reset` no.
#include "VMXFP4DotProductPipe.h"
#include "verilated.h"
#include "test_vectors.h"

#include <cstdio>
#include <cstdint>

#define LATENCY 2

static VMXFP4DotProductPipe* dut;

static void tick() {
    dut->clock = 0;
    dut->eval();
    dut->clock = 1;
    dut->eval();
}

static void drive(const mxfp4_vec_t& v) {
    for (int w = 0; w < MXFP4_WORDS; ++w) {
        dut->io_a[w] = v.a[w];
        dut->io_b[w] = v.b[w];
    }
    dut->io_scaleA = v.scale_a;
    dut->io_scaleB = v.scale_b;
    dut->io_validIn = 1;
}

int main(int argc, char** argv) {
    Verilated::commandArgs(argc, argv);
    dut = new VMXFP4DotProductPipe;

    int failures = 0;
    const int n = NUM_VECTORS;

    // reset sincrono attivo alto, 2 cicli
    dut->reset = 1;
    dut->io_validIn = 0;
    tick();
    tick();
    dut->reset = 0;

    // Streaming: al ciclo i si immette il vettore i e si raccoglie il
    // risultato del vettore i-LATENCY.
    for (int i = 0; i < n + LATENCY; ++i) {
        if (i < n) {
            drive(MXFP4_VECTORS[i]);
        } else {
            dut->io_validIn = 0;
        }
        tick();

        // Il fronte al passo i cattura il vettore i nello stadio 1; il fronte
        // successivo lo porta in uscita. Quindi dopo tick() al passo i le
        // uscite valgono per il vettore i-1.
        const int out = i - (LATENCY - 1);
        if (out < 0 || out >= n) continue;

        const mxfp4_vec_t& v = MXFP4_VECTORS[out];
        const int32_t got_acc = (int32_t)dut->io_accQ2;
        const int16_t got_exp = (int16_t)dut->io_expOut;
        const uint8_t got_nan = (uint8_t)dut->io_isNaN;
        const uint8_t got_val = (uint8_t)dut->io_validOut;

        if (!got_val) {
            if (failures < 10)
                printf("FAIL [%d] %-16s validOut basso: il pipeline non "
                       "sostiene 1 blocco/ciclo\n", out, v.label);
            ++failures;
            continue;
        }
        if (got_acc != v.exp_acc_q2 || got_exp != v.exp_shared || got_nan != v.nan) {
            if (failures < 10) {
                printf("FAIL [%d] %-16s acc exp=%d got=%d | exp2 exp=%d got=%d | "
                       "nan exp=%u got=%u\n",
                       out, v.label,
                       (int)v.exp_acc_q2, (int)got_acc,
                       (int)v.exp_shared, (int)got_exp,
                       (unsigned)v.nan, (unsigned)got_nan);
            }
            ++failures;
        }
    }

    // dopo lo svuotamento validOut deve tornare basso
    tick();
    tick();
    if (dut->io_validOut) {
        printf("FAIL validOut resta alto dopo lo svuotamento del pipeline\n");
        ++failures;
    }

    dut->final();
    delete dut;

    if (failures) {
        printf("TEST FAILED (%d/%d)\n", failures, n);
        return 1;
    }
    printf("TEST PASSED (%d vectors)\n", n);
    return 0;
}
