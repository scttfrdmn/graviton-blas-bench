/* graviton-blas-bench — ask a linked OpenBLAS what kernel set it actually selected.
 *
 * Prints "corename|config" on stdout. Exists because the OPENBLAS_CORETYPE axis
 * of this campaign is a *request*, and a request is not a measurement:
 * force_coretype() silently ignores a name it does not know, and a
 * non-DYNAMIC_ARCH build ignores the variable entirely. Labelling a record
 * coretype=NEOVERSEV1 because that is what we exported would be claiming a
 * number we did not measure (standing order 3), so run-matrix.sh runs this
 * first for every coretype and records what came back, not what it asked for.
 *
 * Built and linked exactly like the bench binaries so it resolves the same
 * libopenblas by rpath. Deliberately not merged into bench.c: bench.c links
 * against ArmPL and BLIS too, and neither exports these symbols.
 */
#include <stdio.h>

/* Declared rather than included: openblas_config.h is not installed by every
 * build variant, and these two symbols are stable across all of them. */
char *openblas_get_corename(void);
char *openblas_get_config(void);

int main(void) {
    const char *core = openblas_get_corename();
    const char *conf = openblas_get_config();
    printf("%s|%s\n", core ? core : "", conf ? conf : "");
    return 0;
}
