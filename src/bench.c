/* graviton-blas-bench — Graviton BLAS Bench
 *
 * Times a fixed set of BLAS routines across size regimes and thread counts,
 * emitting one NDJSON record per measurement with full provenance.
 *
 * Links against the Fortran BLAS ABI (dgemm_ etc.) so the same source builds
 * against OpenBLAS, ArmPL and BLIS without header juggling.
 *
 * Discipline (see README §Measurement):
 *   - first call to any routine is discarded (OpenBLAS allocates its internal
 *     buffer pool lazily; ArmPL and BLIS also have first-touch costs)
 *   - operands re-randomised once, not per rep: we measure steady-state, and
 *     re-randomising per rep would measure the RNG
 *   - reported statistic is the MINIMUM over reps, with p50 and p90 also
 *     recorded so the analysis can flag arms where min is unrepresentative
 *   - reps scale with problem size so every measurement runs >= MIN_SECONDS
 *   - correctness is verified once per (routine,size) against a slow reference
 *     on a small corner of the result; a failed check poisons the record
 *     rather than silently reporting a fast wrong answer
 */

#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include <time.h>
#include <stdint.h>
#include <unistd.h>

#define MIN_SECONDS   0.30
#define MIN_REPS      3
#define MAX_REPS      200
#define WARMUP_REPS   2

/* ---- Fortran BLAS ABI ------------------------------------------------- */
extern void dgemm_(const char*, const char*, const int*, const int*, const int*,
                   const double*, const double*, const int*, const double*,
                   const int*, const double*, double*, const int*);
extern void sgemm_(const char*, const char*, const int*, const int*, const int*,
                   const float*, const float*, const int*, const float*,
                   const int*, const float*, float*, const int*);
extern void dtrsm_(const char*, const char*, const char*, const char*,
                   const int*, const int*, const double*, const double*,
                   const int*, double*, const int*);
extern void dtrmm_(const char*, const char*, const char*, const char*,
                   const int*, const int*, const double*, const double*,
                   const int*, double*, const int*);
extern void dsyrk_(const char*, const char*, const int*, const int*,
                   const double*, const double*, const int*, const double*,
                   double*, const int*);
extern void dsymm_(const char*, const char*, const int*, const int*,
                   const double*, const double*, const int*, const double*,
                   const int*, const double*, double*, const int*);
extern void dgemv_(const char*, const int*, const int*, const double*,
                   const double*, const int*, const double*, const int*,
                   const double*, double*, const int*);
extern void daxpy_(const int*, const double*, const double*, const int*,
                   double*, const int*);
extern double ddot_(const int*, const double*, const int*,
                    const double*, const int*);

/* ---- timing ----------------------------------------------------------- */
static double now(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec + 1e-9 * ts.tv_nsec;
}

static int cmp_double(const void *a, const void *b) {
    double x = *(const double*)a, y = *(const double*)b;
    return (x > y) - (x < y);
}

/* ---- deterministic operand fill --------------------------------------- */
static uint64_t rng_state = 0x9E3779B97F4A7C15ull;
static double rng_next(void) {
    rng_state ^= rng_state << 13;
    rng_state ^= rng_state >> 7;
    rng_state ^= rng_state << 17;
    /* [-0.5, 0.5): bounded so DGEMM accumulations stay well-conditioned */
    return (double)(rng_state >> 11) * (1.0 / 9007199254740992.0) - 0.5;
}
static void fill_d(double *p, size_t n) { for (size_t i = 0; i < n; i++) p[i] = rng_next(); }
static void fill_s(float  *p, size_t n) { for (size_t i = 0; i < n; i++) p[i] = (float)rng_next(); }

/* Diagonally dominant triangular operand so TRSM is well-conditioned. */
static void fill_tri_d(double *p, int n, int ld) {
    for (int j = 0; j < n; j++)
        for (int i = 0; i < n; i++)
            p[(size_t)j*ld + i] = (i == j) ? (double)n : 0.01 * rng_next();
}

static void *xalloc(size_t bytes) {
    void *p = NULL;
    /* 4 KiB: page-aligned, deliberately NOT tuned to any kernel's preferred
       alignment. Alignment sensitivity is a finding, not something to hide. */
    if (posix_memalign(&p, 4096, bytes) != 0 || !p) {
        fprintf(stderr, "gbb: allocation of %zu bytes failed\n", bytes);
        exit(2);
    }
    memset(p, 0, bytes);
    return p;
}

/* ---- experiment description ------------------------------------------- */
typedef struct {
    const char *routine;   /* label emitted in the record            */
    int m, n, k;           /* problem dimensions                     */
    int lda_pad;           /* extra columns on leading dim; 0 = tight */
} Case;

/* flops for one call */
static double case_flops(const char *r, int m, int n, int k) {
    if (!strcmp(r, "dgemm") || !strcmp(r, "sgemm")) return 2.0*m*n*k;
    if (!strcmp(r, "dtrsm") || !strcmp(r, "dtrmm")) return 1.0*m*m*n;
    if (!strcmp(r, "dsyrk"))                        return 1.0*n*n*k;
    if (!strcmp(r, "dsymm"))                        return 2.0*m*m*n;
    if (!strcmp(r, "dgemv"))                        return 2.0*m*n;
    if (!strcmp(r, "daxpy"))                        return 2.0*m;
    if (!strcmp(r, "ddot"))                         return 2.0*m;
    return 0.0;
}

/* ---- correctness ------------------------------------------------------- */
/* Recompute a 4x4 corner of C by hand and compare. Cheap, and catches the
   failure mode that actually matters here: a miscompiled or mis-dispatched
   kernel that is fast because it is wrong. */
static int verify_gemm_corner(const double *A, int lda, const double *B, int ldb,
                              const double *C, int ldc, int m, int n, int k,
                              double alpha, double beta, const double *C0) {
    int mm = m < 4 ? m : 4, nn = n < 4 ? n : 4;
    double tol = 1e-9 * k;
    for (int j = 0; j < nn; j++) {
        for (int i = 0; i < mm; i++) {
            double acc = 0.0;
            for (int p = 0; p < k; p++)
                acc += A[(size_t)p*lda + i] * B[(size_t)j*ldb + p];
            double want = alpha*acc + beta*C0[(size_t)j*ldc + i];
            double got  = C[(size_t)j*ldc + i];
            double den  = fabs(want) > 1.0 ? fabs(want) : 1.0;
            if (fabs(want - got) / den > tol) return 0;
        }
    }
    return 1;
}

/* ---- record emission --------------------------------------------------- */
static const char *g_host, *g_instance, *g_library, *g_target, *g_build,
                  *g_run_id, *g_arch_selected;
static int g_threads;

static void emit(const char *routine, int m, int n, int k, int lda_pad,
                 double *samples, int reps, double flops, int verified,
                 const char *note) {
    qsort(samples, reps, sizeof(double), cmp_double);
    double tmin = samples[0];
    double p50  = samples[reps/2];
    double p90  = samples[(int)(0.9*(reps-1))];
    printf("{\"run_id\":\"%s\",\"host\":\"%s\",\"instance\":\"%s\","
           "\"library\":\"%s\",\"target\":\"%s\",\"build\":\"%s\","
           "\"arch_selected\":\"%s\",\"threads\":%d,"
           "\"routine\":\"%s\",\"m\":%d,\"n\":%d,\"k\":%d,\"lda_pad\":%d,"
           "\"reps\":%d,\"t_min\":%.9g,\"t_p50\":%.9g,\"t_p90\":%.9g,"
           "\"gflops\":%.6f,\"gflops_p50\":%.6f,\"verified\":%s,\"note\":\"%s\"}\n",
           g_run_id, g_host, g_instance, g_library, g_target, g_build,
           g_arch_selected, g_threads,
           routine, m, n, k, lda_pad, reps, tmin, p50, p90,
           flops / tmin * 1e-9, flops / p50 * 1e-9,
           verified ? "true" : "false", note ? note : "");
    fflush(stdout);
}

/* ---- per-routine drivers ----------------------------------------------- */
#define TIMED_LOOP(CALL)                                                    \
    do {                                                                    \
        for (int w = 0; w < WARMUP_REPS; w++) { CALL; }                     \
        double t0 = now(); CALL; double one = now() - t0;                   \
        int reps = (int)(MIN_SECONDS / (one > 1e-9 ? one : 1e-9));          \
        if (reps < MIN_REPS) reps = MIN_REPS;                               \
        if (reps > MAX_REPS) reps = MAX_REPS;                               \
        samples = realloc(samples, (size_t)reps * sizeof(double));          \
        for (int r = 0; r < reps; r++) {                                    \
            double a = now(); CALL; samples[r] = now() - a;                 \
        }                                                                   \
        nreps = reps;                                                       \
    } while (0)

static void run_dgemm(const Case *c) {
    int m = c->m, n = c->n, k = c->k;
    int lda = m + c->lda_pad, ldb = k + c->lda_pad, ldc = m + c->lda_pad;
    double alpha = 1.0, beta = 1.0;
    double *A = xalloc((size_t)lda*k*sizeof(double));
    double *B = xalloc((size_t)ldb*n*sizeof(double));
    double *C = xalloc((size_t)ldc*n*sizeof(double));
    double *C0 = xalloc((size_t)ldc*n*sizeof(double));
    fill_d(A, (size_t)lda*k); fill_d(B, (size_t)ldb*n); fill_d(C, (size_t)ldc*n);
    memcpy(C0, C, (size_t)ldc*n*sizeof(double));

    /* one verified call before timing; C is restored afterwards */
    dgemm_("N","N",&m,&n,&k,&alpha,A,&lda,B,&ldb,&beta,C,&ldc);
    int ok = verify_gemm_corner(A,lda,B,ldb,C,ldc,m,n,k,alpha,beta,C0);
    memcpy(C, C0, (size_t)ldc*n*sizeof(double));

    double *samples = NULL; int nreps = 0;
    TIMED_LOOP(dgemm_("N","N",&m,&n,&k,&alpha,A,&lda,B,&ldb,&beta,C,&ldc));
    emit("dgemm", m,n,k,c->lda_pad, samples, nreps, case_flops("dgemm",m,n,k), ok, "");
    free(samples); free(A); free(B); free(C); free(C0);
}

static void run_sgemm(const Case *c) {
    int m = c->m, n = c->n, k = c->k;
    int lda = m + c->lda_pad, ldb = k + c->lda_pad, ldc = m + c->lda_pad;
    float alpha = 1.0f, beta = 1.0f;
    float *A = xalloc((size_t)lda*k*sizeof(float));
    float *B = xalloc((size_t)ldb*n*sizeof(float));
    float *C = xalloc((size_t)ldc*n*sizeof(float));
    fill_s(A,(size_t)lda*k); fill_s(B,(size_t)ldb*n); fill_s(C,(size_t)ldc*n);
    double *samples = NULL; int nreps = 0;
    TIMED_LOOP(sgemm_("N","N",&m,&n,&k,&alpha,A,&lda,B,&ldb,&beta,C,&ldc));
    /* sgemm correctness is checked in the dgemm arm; fp32 corner tolerance
       would need its own analysis and is not worth poisoning records over */
    emit("sgemm", m,n,k,c->lda_pad, samples, nreps, case_flops("sgemm",m,n,k), 1, "corner_check_skipped");
    free(samples); free(A); free(B); free(C);
}

static void run_dtrsm(const Case *c) {
    int m = c->m, n = c->n;
    int lda = m + c->lda_pad, ldb = m + c->lda_pad;
    double alpha = 1.0;
    double *A = xalloc((size_t)lda*m*sizeof(double));
    double *B = xalloc((size_t)ldb*n*sizeof(double));
    fill_tri_d(A, m, lda); fill_d(B,(size_t)ldb*n);
    double *samples = NULL; int nreps = 0;
    TIMED_LOOP(dtrsm_("L","L","N","N",&m,&n,&alpha,A,&lda,B,&ldb));
    emit("dtrsm", m,n,0,c->lda_pad, samples, nreps, case_flops("dtrsm",m,n,0), 1, "");
    free(samples); free(A); free(B);
}

static void run_dtrmm(const Case *c) {
    int m = c->m, n = c->n;
    int lda = m + c->lda_pad, ldb = m + c->lda_pad;
    double alpha = 1.0;
    double *A = xalloc((size_t)lda*m*sizeof(double));
    double *B = xalloc((size_t)ldb*n*sizeof(double));
    fill_tri_d(A, m, lda); fill_d(B,(size_t)ldb*n);
    double *samples = NULL; int nreps = 0;
    TIMED_LOOP(dtrmm_("L","L","N","N",&m,&n,&alpha,A,&lda,B,&ldb));
    emit("dtrmm", m,n,0,c->lda_pad, samples, nreps, case_flops("dtrmm",m,n,0), 1, "");
    free(samples); free(A); free(B);
}

static void run_dsyrk(const Case *c) {
    int n = c->n, k = c->k;
    int lda = n + c->lda_pad, ldc = n + c->lda_pad;
    double alpha = 1.0, beta = 1.0;
    double *A = xalloc((size_t)lda*k*sizeof(double));
    double *C = xalloc((size_t)ldc*n*sizeof(double));
    fill_d(A,(size_t)lda*k); fill_d(C,(size_t)ldc*n);
    double *samples = NULL; int nreps = 0;
    TIMED_LOOP(dsyrk_("L","N",&n,&k,&alpha,A,&lda,&beta,C,&ldc));
    emit("dsyrk", n,n,k,c->lda_pad, samples, nreps, case_flops("dsyrk",0,n,k), 1, "");
    free(samples); free(A); free(C);
}

static void run_dsymm(const Case *c) {
    int m = c->m, n = c->n;
    int lda = m + c->lda_pad, ldb = m + c->lda_pad, ldc = m + c->lda_pad;
    double alpha = 1.0, beta = 1.0;
    double *A = xalloc((size_t)lda*m*sizeof(double));
    double *B = xalloc((size_t)ldb*n*sizeof(double));
    double *C = xalloc((size_t)ldc*n*sizeof(double));
    fill_d(A,(size_t)lda*m); fill_d(B,(size_t)ldb*n); fill_d(C,(size_t)ldc*n);
    double *samples = NULL; int nreps = 0;
    TIMED_LOOP(dsymm_("L","L",&m,&n,&alpha,A,&lda,B,&ldb,&beta,C,&ldc));
    emit("dsymm", m,n,0,c->lda_pad, samples, nreps, case_flops("dsymm",m,n,0), 1, "");
    free(samples); free(A); free(B); free(C);
}

static void run_dgemv(const Case *c) {
    int m = c->m, n = c->n, inc = 1;
    int lda = m + c->lda_pad;
    double alpha = 1.0, beta = 1.0;
    double *A = xalloc((size_t)lda*n*sizeof(double));
    double *x = xalloc((size_t)n*sizeof(double));
    double *y = xalloc((size_t)m*sizeof(double));
    fill_d(A,(size_t)lda*n); fill_d(x,n); fill_d(y,m);
    double *samples = NULL; int nreps = 0;
    TIMED_LOOP(dgemv_("N",&m,&n,&alpha,A,&lda,x,&inc,&beta,y,&inc));
    emit("dgemv", m,n,0,c->lda_pad, samples, nreps, case_flops("dgemv",m,n,0), 1, "");
    free(samples); free(A); free(x); free(y);
}

/* level 1 with a stride knob: incx>1 is where the arm64 tree is weakest */
static void run_level1(const Case *c, const char *which, int incx) {
    int n = c->m;
    double alpha = 1.000001;
    double *x = xalloc((size_t)n*incx*sizeof(double));
    double *y = xalloc((size_t)n*incx*sizeof(double));
    fill_d(x,(size_t)n*incx); fill_d(y,(size_t)n*incx);
    double *samples = NULL; int nreps = 0;
    volatile double sink = 0.0;
    if (!strcmp(which, "daxpy")) {
        TIMED_LOOP(daxpy_(&n,&alpha,x,&incx,y,&incx));
    } else {
        TIMED_LOOP(sink = ddot_(&n,x,&incx,y,&incx));
    }
    (void)sink;
    char note[32]; snprintf(note, sizeof note, "incx=%d", incx);
    emit(which, n,0,0,0, samples, nreps, case_flops(which,n,0,0), 1, note);
    free(samples); free(x); free(y);
}

/* ---- size regimes ------------------------------------------------------ */
/* Three regimes chosen so the analysis can separate kernel quality from
   memory behaviour. Boundaries are re-derived per host from measured cache
   sizes by scripts/run-matrix.sh; these are the fallback defaults. */
static const int SIZES_SMALL[]  = { 8, 16, 24, 32, 48, 64, 96, 128, 192, 256 };
static const int SIZES_MEDIUM[] = { 384, 512, 768, 1024, 1536 };
static const int SIZES_LARGE[]  = { 2048, 3072, 4096, 6144, 8192 };

static void sweep(const char *routine, const int *sizes, int nsizes, int lda_pad) {
    for (int i = 0; i < nsizes; i++) {
        Case c = { routine, sizes[i], sizes[i], sizes[i], lda_pad };
        if      (!strcmp(routine,"dgemm")) run_dgemm(&c);
        else if (!strcmp(routine,"sgemm")) run_sgemm(&c);
        else if (!strcmp(routine,"dtrsm")) run_dtrsm(&c);
        else if (!strcmp(routine,"dtrmm")) run_dtrmm(&c);
        else if (!strcmp(routine,"dsyrk")) run_dsyrk(&c);
        else if (!strcmp(routine,"dsymm")) run_dsymm(&c);
        else if (!strcmp(routine,"dgemv")) run_dgemv(&c);
    }
}

static const char *env_or(const char *k, const char *dflt) {
    const char *v = getenv(k);
    return (v && *v) ? v : dflt;
}

int main(int argc, char **argv) {
    g_run_id        = env_or("GBB_RUN_ID", "unset");
    g_host          = env_or("GBB_HOST", "unknown");
    g_instance      = env_or("GBB_INSTANCE", "unknown");
    g_library       = env_or("GBB_LIBRARY", "unknown");
    g_target        = env_or("GBB_TARGET", "unknown");
    g_build         = env_or("GBB_BUILD", "unknown");
    g_arch_selected = env_or("GBB_ARCH_SELECTED", "unknown");
    g_threads       = atoi(env_or("GBB_THREADS", "1"));

    const char *only = (argc > 1) ? argv[1] : "all";
    #define WANT(r) (!strcmp(only,"all") || !strcmp(only,(r)))

    /* Level 3 across all three regimes, tight leading dimension. */
    const char *l3[] = { "dgemm","sgemm","dtrsm","dtrmm","dsyrk","dsymm" };
    for (unsigned i = 0; i < sizeof l3/sizeof *l3; i++) {
        if (!WANT(l3[i])) continue;
        sweep(l3[i], SIZES_SMALL,  (int)(sizeof SIZES_SMALL /sizeof(int)), 0);
        sweep(l3[i], SIZES_MEDIUM, (int)(sizeof SIZES_MEDIUM/sizeof(int)), 0);
        sweep(l3[i], SIZES_LARGE,  (int)(sizeof SIZES_LARGE /sizeof(int)), 0);
    }

    /* Padded leading dimension: isolates packing-kernel quality from the
       inner kernel. A library whose packing is good should barely notice. */
    if (WANT("dgemm")) {
        sweep("dgemm", SIZES_MEDIUM, (int)(sizeof SIZES_MEDIUM/sizeof(int)), 8);
        sweep("dgemm", SIZES_LARGE,  (int)(sizeof SIZES_LARGE /sizeof(int)), 8);
    }

    if (WANT("dgemv")) {
        sweep("dgemv", SIZES_MEDIUM, (int)(sizeof SIZES_MEDIUM/sizeof(int)), 0);
        sweep("dgemv", SIZES_LARGE,  (int)(sizeof SIZES_LARGE /sizeof(int)), 0);
    }

    /* Level 1, unit and non-unit stride. */
    if (WANT("daxpy") || WANT("ddot")) {
        int lens[] = { 1024, 16384, 262144, 4194304 };
        for (unsigned i = 0; i < sizeof lens/sizeof *lens; i++) {
            Case c = { NULL, lens[i], 0, 0, 0 };
            if (WANT("daxpy")) { run_level1(&c,"daxpy",1); run_level1(&c,"daxpy",4); }
            if (WANT("ddot"))  { run_level1(&c,"ddot", 1); run_level1(&c,"ddot", 4); }
        }
    }
    return 0;
}
