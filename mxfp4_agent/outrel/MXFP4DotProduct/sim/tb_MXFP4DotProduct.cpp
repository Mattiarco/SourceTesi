// Testbench Verilator per MXFP4DotProduct.
//
// NOTA: Chisel appiattisce il Bundle `io`, quindi le porte si chiamano
// `io_<nome>` e non `<nome>`. Il modulo espone sempre anche clock/reset,
// inutilizzati perche' il design e' combinatorio.
#include "VMXFP4DotProduct.h"
#include "verilated.h"
#include "test_vectors.h"

#include <cstdio>
#include <cstdint>
#include <cstdlib>

int main(int argc, char** argv) {
    Verilated::commandArgs(argc, argv);
    VMXFP4DotProduct* dut = new VMXFP4DotProduct;

    dut->clock = 0;   // design combinatorio: clock/reset esistono ma non servono
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

        int32_t got_acc = (int32_t)dut->io_accQ2;
        int16_t got_exp = (int16_t)dut->io_expOut;
        uint8_t got_nan = (uint8_t)dut->io_isNaN;

        if (got_acc != v.exp_acc_q2 || got_exp != v.exp_shared || got_nan != v.nan) {
            if (failures < 10) {
                printf("FAIL [%d] acc exp=%d got=%d | exp2 exp=%d got=%d | nan exp=%u got=%u\n",
                       i, (int)v.exp_acc_q2, (int)got_acc,
                       (int)v.exp_shared, (int)got_exp,
                       (unsigned)v.nan, (unsigned)got_nan);
            }
            ++failures;
        }
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
