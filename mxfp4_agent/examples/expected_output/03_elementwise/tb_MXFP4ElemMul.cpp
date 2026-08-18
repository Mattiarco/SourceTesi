// Testbench Verilator per MXFP4ElemMul (element-wise -> 32 x FP32).
//
// Il confronto e' BIT-EXACT sul pattern IEEE-754: i valori attesi arrivano da
// `exp_elem[]`, calcolato dal golden model Python. Non si confrontano float
// con tolleranza, perche' la conversione MXFP4 -> FP32 e' esatta per
// costruzione e qualunque differenza sarebbe un bug.
//
// `io_out` e' largo 1024 bit: Verilator lo espone come array di parole a
// 32 bit, quindi la parola i coincide esattamente con l'elemento i.
#include "VMXFP4ElemMul.h"
#include "verilated.h"
#include "test_vectors.h"

#include <cstdio>
#include <cstdint>
#include <cstring>

static float as_float(uint32_t bits) {
    float f;
    std::memcpy(&f, &bits, sizeof f);
    return f;
}

int main(int argc, char** argv) {
    Verilated::commandArgs(argc, argv);
    VMXFP4ElemMul* dut = new VMXFP4ElemMul;

    dut->clock = 0;
    dut->reset = 0;

    int failures = 0;
    const int n = NUM_VECTORS;

    for (int i = 0; i < n; ++i) {
        const mxfp4_vec_t& v = MXFP4_VECTORS[i];

        for (int w = 0; w < MXFP4_WORDS; ++w) {
            dut->io_a[w] = v.a[w];
            dut->io_b[w] = v.b[w];
        }
        dut->io_scaleA = v.scale_a;
        dut->io_scaleB = v.scale_b;
        dut->eval();

        int bad_in_vector = 0;
        for (int e = 0; e < MXFP4_K; ++e) {
            const uint32_t got = dut->io_out[e];
            const uint32_t exp = v.exp_elem[e];
            if (got != exp) {
                if (failures < 10 && bad_in_vector < 3) {
                    printf("FAIL [%d] %-16s elem %2d: exp=0x%08x (%g) "
                           "got=0x%08x (%g)\n",
                           i, v.label, e, exp, (double)as_float(exp),
                           got, (double)as_float(got));
                }
                ++bad_in_vector;
            }
        }

        if ((uint8_t)dut->io_isNaN != v.nan) {
            if (failures < 10)
                printf("FAIL [%d] %-16s isNaN exp=%u got=%u\n",
                       i, v.label, (unsigned)v.nan, (unsigned)dut->io_isNaN);
            ++bad_in_vector;
        }
        if (bad_in_vector) ++failures;
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
