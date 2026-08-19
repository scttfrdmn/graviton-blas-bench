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
#include <float.h>
#include <dlfcn.h>   /* RTLD_DEFAULT: see corename_fn() */

/* ---- timing contract --------------------------------------------------- */
/* A measurement is SAMPLES outer samples, each of which times a BATCH of
 * back-to-back calls and divides. Batching exists because bracketing every
 * individual call with now() cost ~31 ns per pair -- 27.9% of the sample at
 * n=8. A constant additive term does not merely add noise, it COMPRESSES
 * RATIOS: a true 20% difference in the small regime reads as ~14%. That biases
 * this campaign toward "no effect found" on its primary conclusion, in the one
 * size regime where the missing GEMM_SMALL_* path is expected to show. An
 * instrument whose error points at its own null hypothesis is not usable.
 *
 * With a 1 ms batch floor the same 31 ns is 0.003% of a sample.
 *
 * MIN_SAMPLES is 8 rather than the old MIN_REPS of 3 because at 3 samples
 * p50 = samples[1] and p90 = samples[(int)(0.9*2)] = samples[1] -- the same
 * element. The min/p50 spread that README §Measurement discipline relies on to
 * detect a noisy neighbour did not exist for any LARGE level-3 case, which is
 * exactly where each rep is expensive and a stolen timeslice hurts most.
 *
 * MAX_MEASURE_SECONDS bounds the wall-clock cost of one case, and
 * ABS_MIN_SAMPLES is the floor it may drive SAMPLES down to. The largest cases
 * (8192-cube DGEMM at one thread is ~37 s per call) still land on
 * ABS_MIN_SAMPLES, so they cost what they cost today; the added samples are
 * paid for only where a call is cheap.
 */
#define MIN_SECONDS          0.30      /* of real BLAS work per measurement  */
#define MIN_BATCH_SECONDS    1.0e-3    /* inner batch duration floor         */
#define CAL_MIN_SECONDS      1.0e-4    /* calibration interval floor         */
#define MAX_BATCH            1000000L  /* backstop, not normally reached     */
#define ABS_MIN_SAMPLES      3         /* fewest that admit any statistic    */
#define MIN_SAMPLES          8         /* fewest that admit p50 != p90       */
#define MAX_SAMPLES          1000
#define MAX_MEASURE_SECONDS  3.0       /* wall-clock cap for one case        */
#define WARMUP_REPS          2

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

/* Measured cost of the now() pair that brackets a timed region. Recorded in
   every record so a reader can check for themselves that batching made it
   negligible, rather than taking that on faith. */
static double g_timer_overhead = 0.0;

/* Smallest observable nonzero gap between two consecutive reads. This is a
   DIFFERENT quantity from the overhead above and the distinction is
   load-bearing: on Apple Silicon the mach timebase is 24 MHz, so the
   resolution is ~41.7 ns while the amortised cost of a read is ~30 ns. An
   averaged overhead measurement stays accurate under a coarse clock, so it
   cannot warn you about one -- see TIMED_LOOP stage 1 for what that cost. */
static double g_timer_res = 0.0;

static void calibrate_timer(void) {
    enum { N = 4096 };
    double t0 = now();
    for (int i = 0; i < N; i++) { double a = now(); (void)a; }
    /* two clock reads per timed region */
    g_timer_overhead = 2.0 * (now() - t0) / (double)N;

    double best = 1.0;
    for (int i = 0; i < 65536; i++) {
        double a = now(), b = now();
        double d = b - a;
        if (d > 0.0 && d < best) best = d;
    }
    g_timer_res = (best < 1.0) ? best : 0.0;
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

/* Diagonally dominant triangular operand so TRSM is well-conditioned.
 *
 * MEASUREMENT HAZARD (load-bearing -- do not "simplify" the 1/n scaling out):
 *   TRSM and TRMM are DESTRUCTIVE and operate in place on B, and TIMED_LOOP
 *   repeats the same call up to MAX_REPS times without restoring the operand.
 *   So whatever gain the triangular operator has is applied ONCE PER REP,
 *   geometrically.
 *
 *   The original fill used a diagonal of (double)n, giving a per-rep gain of
 *   about n. dtrmm then multiplied B by ~n every rep and dtrsm divided by ~n:
 *   at n=256 the operand overflowed to +Inf by around rep 128, and dtrsm
 *   underflowed to exactly 0.0 over the same span. A large share of every
 *   dtrsm/dtrmm sample was therefore timed on Inf or on exact zeros -- values
 *   some kernels special-case -- and because those two drivers hardcode
 *   verified=1, nothing flagged it. That silently corrupted the SMALL and
 *   low-MEDIUM regimes, which is exactly where the campaign expects to find
 *   the missing GEMM_SMALL_* / generic-TRSM deficit.
 *
 *   Fix: unit diagonal, and off-diagonals scaled by TRI_OFFDIAG/n so the
 *   worst-case row sum is <= TRI_OFFDIAG/2 against a diagonal of 1.0 -- MORE
 *   diagonally dominant than before, at every n, while the operator gain is
 *   1 + TRI_OFFDIAG/2 instead of ~n.
 *
 *   TRI_OFFDIAG must be chosen against the TOTAL call count, not the sample
 *   count. TIMED_LOOP batches, so the calls per measurement are
 *   MAX_SAMPLES * MAX_BATCH = 1e9 in the worst case, not the 200 the first
 *   version of this fix assumed. At 1e-9 the bound over 1e9 calls is
 *   (1 + 5e-10)^1e9 = e^0.5 ~ 1.65 -- and that is the pessimistic monotone
 *   bound; the off-diagonals are signed, so the real drift is a random walk.
 *
 *   Off-diagonals are then ~1e-12 at n=1000. Still normal FP64 by 296 orders
 *   of magnitude, so no subnormal arithmetic is introduced -- which would have
 *   been its own timing artifact.
 *
 *   alpha stays 1.0 deliberately: scaling by alpha would also fix the growth,
 *   but many libraries have an alpha==1 fast path, and taking a different code
 *   path than production callers would make the measurement unrepresentative.
 *
 *   Flop count and memory access pattern are unchanged by operand values, so
 *   this costs nothing in what the benchmark measures.
 */
#define TRI_OFFDIAG 1.0e-9
static void fill_tri_d(double *p, int n, int ld) {
    const double off = TRI_OFFDIAG / (double)n;
    for (int j = 0; j < n; j++)
        for (int i = 0; i < n; i++)
            p[(size_t)j*ld + i] = (i == j) ? 1.0 : off * rng_next();
}

/* Strided finiteness probe for in-place operands.
 *
 * The detector for the hazard documented above: if a destructive routine ever
 * drives its operand out of range again, the record must say so rather than
 * reporting a fast number computed on Inf. Blow-up is global, not local, so a
 * strided sample is sufficient and stays cheap on the 8192-square cases.
 */
static int operand_finite(const double *p, size_t n) {
    size_t stride = n / 1024;
    if (stride < 1) stride = 1;
    for (size_t i = 0; i < n; i += stride)
        if (!isfinite(p[i])) return 0;
    return isfinite(p[0]) && isfinite(p[n - 1]);
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
    /* Was 1e-9 * k, which is about 4.5e6 times looser than the error a correct
     * FP64 dot product can actually accumulate. At k=1024 that admitted a
     * relative error of 1e-6: a kernel wrong in the 7th significant digit --
     * one that had silently dropped to FP32 accumulation, or was skipping a
     * tail block -- passed cleanly. That is precisely the "fast because it is
     * wrong" failure this check exists to catch (standing order 4).
     *
     * The forward error bound for a k-length dot product is k*eps; the factor
     * of 8 is headroom for the different summation orders that blocked and
     * SIMD-reduced kernels legitimately produce. Tightening, not relaxing, so
     * it needs no sign-off -- but it MUST be validated against real OpenBLAS,
     * ArmPL and BLIS builds before P2, because a false positive poisons records
     * just as badly as a false negative. If it does fire spuriously, raise the
     * factor with the measured evidence attached, never to make records pass.
     */
    double tol = 8.0 * (double)k * DBL_EPSILON;
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

/* Verification state is tri-state on the wire, not boolean.
 *
 * Only dgemm has a correctness check. Every other driver used to pass a
 * hardcoded 1, so seven routines -- including dtrsm, dtrmm and dsymm, which
 * are exactly the operations in the 90-kernel N2 gap this campaign exists to
 * study -- asserted "verified" on the strength of nothing. The routines most
 * likely to be mis-dispatched were the ones self-reporting as correct.
 *
 * "not checked" and "checked and passed" are different claims, and standing
 * order 3 does not let us print the second when we mean the first. UNCHECKED
 * emits JSON null so the analysis can fail closed on it.
 */
#define VERIFIED_UNCHECKED (-1)
#define VERIFIED_FAIL       (0)
#define VERIFIED_PASS       (1)

static const char *g_host, *g_instance, *g_library, *g_target, *g_build,
                  *g_run_id, *g_arch_selected;
/* Provenance that was previously unrecorded, and whose absence made numbers
 * inadmissible under standing order 5:
 *   g_blas_sha       the SHA of the BLAS under test. g_build is the *gbb repo*
 *                    SHA and was being misread as the library version.
 *   g_coretype       what OPENBLAS_CORETYPE was forced to at runtime, which is
 *                    a different claim from the build-time TARGET= in g_target
 *                    and from the auto-detected g_arch_selected.
 *   g_thread_backend pthreads vs openmp. Not cosmetic: it decides whether the
 *                    arm obeys OMP_PROC_BIND at all, which was the confound.
 *   g_pin_policy     the exact external binding applied to this arm.
 *   g_role           "campaign" or "instrument". Local instrument checks
 *                    (castor/pollux) must not be mixable with campaign data by
 *                    accident, so the runner derives the role from evidence it
 *                    cannot fake and stamps it here. The default is "unknown",
 *                    not "campaign": a record produced by running this binary by
 *                    hand is not campaign data, and the analysis excludes
 *                    anything that does not say campaign.
 */
static const char *g_blas_sha, *g_coretype, *g_thread_backend, *g_pin_policy,
                  *g_role;
static int g_threads;

/* Looked up at runtime rather than linked, for two reasons.
 *
 * One: bench.c is compiled with identical flags for every arm -- standing order 6
 * forbids otherwise and gates/p0.sh checks it -- and this symbol exists only in
 * the OpenBLAS-linked binaries. A -D would make the harness differ across arms.
 * A weak declaration would avoid that on ELF but needs weak_import on Mach-O, so
 * it would trade a per-arm difference for a per-platform one. dlsym needs
 * neither: the same source, the same flags, and the answer comes from whatever
 * was actually loaded.
 *
 * Two, and the real point: the runner probes the coretype in a SEPARATE PROCESS
 * before the arm runs, and a separate process is not the artifact that produces
 * the numbers -- it can resolve a different libopenblas by rpath, or be handed a
 * different environment. RTLD_DEFAULT searches this process's own loaded images,
 * so what comes back is a property of the library about to do the work rather
 * than a claim inherited over an environment variable. Same principle as
 * measured-peak-over-theoretical, and as reading SVE kernel symbols out of the
 * installed archive instead of trusting the variables passed to the build:
 * verify the artifact, not the intent. */
static char *(*corename_fn(void))(void) {
    return (char *(*)(void))dlsym(RTLD_DEFAULT, "openblas_get_corename");
}

/* Sentinels the runner passes when the question does not apply or could not be
 * answered. Never assert against these: they are not corenames. */
static int is_sentinel(const char *s) {
    return !strcmp(s, "n/a") || !strcmp(s, "unknown") || !strcmp(s, "unprobed");
}
static long g_batch = 1;   /* set by TIMED_LOOP */

/* Element stride, emitted in every record. Level 3 is always unit stride; only
   run_level1 varies it, and it does so by running the SAME (routine, m, n, k,
   lda_pad) twice. Without this field on the record the two strides are
   indistinguishable downstream, and decompose.py's condition key -- which is
   exactly (instance, threads, routine, m, n, k, lda_pad) -- collapsed them into
   one cell and kept the slower of the two. That silently deleted the incx axis,
   which the campaign singles out as where the arm64 tree is weakest. Set by
   run_level1 and reset by it, on the same file-scope-global convention as
   g_batch. */
static int g_incx = 1;

static void emit(const char *routine, int m, int n, int k, int lda_pad,
                 double *samples, int reps, double flops, int verified,
                 const char *note) {
    qsort(samples, reps, sizeof(double), cmp_double);
    double tmin = samples[0];
    double p50  = samples[reps/2];
    double p90  = samples[(int)(0.9*(reps-1))];

    /* A zero t_min makes flops/tmin print as the bare token `inf`, which is not
     * valid JSON -- Python's json module rejects it (it accepts `Infinity`, not
     * `inf`). decompose.py would then drop the whole record with a one-line
     * stderr warning and under-count silently in its header. t_min can legally
     * be zero whenever a single call is shorter than the clock granularity:
     * CLOCK_MONOTONIC resolves to 1 ns on Linux arm64 but 1000 ns on macOS, and
     * coarse clocksources do occur under virtualisation.
     *
     * Emit a valid record that says the timer was outrun, rather than an
     * invalid one that disappears. gflops of 0 is unambiguous here because a
     * real measurement can never be 0. */
    int timer_outrun = !(tmin > 0.0) || !(p50 > 0.0);
    double gf     = timer_outrun ? 0.0 : flops / tmin * 1e-9;
    double gf_p50 = timer_outrun ? 0.0 : flops / p50  * 1e-9;
    if (timer_outrun) {
        note = "timer_resolution_outrun";
        verified = VERIFIED_FAIL;
    }

    const char *vstr = verified > 0 ? "true" : verified == 0 ? "false" : "null";

    printf("{\"run_id\":\"%s\",\"host\":\"%s\",\"instance\":\"%s\","
           "\"library\":\"%s\",\"target\":\"%s\",\"build\":\"%s\","
           "\"blas_sha\":\"%s\",\"coretype\":\"%s\","
           "\"thread_backend\":\"%s\",\"pin_policy\":\"%s\","
           "\"arch_selected\":\"%s\",\"role\":\"%s\",\"threads\":%d,"
           "\"routine\":\"%s\",\"m\":%d,\"n\":%d,\"k\":%d,\"lda_pad\":%d,"
           "\"incx\":%d,"
           "\"reps\":%d,\"batch\":%ld,\"calls\":%ld,"
           "\"timer_overhead_ns\":%.3f,\"timer_res_ns\":%.3f,"
           "\"t_min\":%.9g,\"t_p50\":%.9g,\"t_p90\":%.9g,"
           "\"gflops\":%.6f,\"gflops_p50\":%.6f,\"verified\":%s,\"note\":\"%s\"}\n",
           g_run_id, g_host, g_instance, g_library, g_target, g_build,
           g_blas_sha, g_coretype, g_thread_backend, g_pin_policy,
           g_arch_selected, g_role, g_threads,
           routine, m, n, k, lda_pad,
           g_incx,
           reps, g_batch, (long)reps * g_batch,
           g_timer_overhead * 1e9, g_timer_res * 1e9,
           tmin, p50, p90,
           gf, gf_p50,
           vstr, note ? note : "");
    fflush(stdout);
}

/* ---- per-routine drivers ----------------------------------------------- */
/* Sets g_batch as a side effect, read by emit(). A macro cannot return two
   values and the alternative is editing all nine call sites; this file already
   passes provenance through file-scope globals, so it is consistent. */
#define TIMED_LOOP(CALL)                                                      \
    do {                                                                      \
        for (int w = 0; w < WARMUP_REPS; w++) { CALL; }                       \
        /* Stage 1: grow a batch until the interval is long enough that the    \
           clock's RESOLUTION is not what we are measuring. Timing ONE call to \
           size the batch does not work: at n=8 a dgemm call is ~58 ns, below  \
           the 41.7 ns tick on some hosts, so it reads as 0, gets clamped, and \
           the batch is sized from garbage. Measured, not hypothetical -- it   \
           overshot by 58x and turned a 0.3 s measurement into 17.6 s. */      \
        long _b = 1; double _cal = 0.0;                                        \
        for (;;) {                                                             \
            double _c0 = now();                                                \
            for (long _i = 0; _i < _b; _i++) { CALL; }                          \
            _cal = now() - _c0;                                                \
            if (_cal >= CAL_MIN_SECONDS || _b >= MAX_BATCH) break;              \
            _b *= 4;                                                           \
        }                                                                      \
        double _one = _cal / (double)_b;                                        \
        if (!(_one > 0.0)) _one = CAL_MIN_SECONDS / (double)_b;                 \
        /* Stage 2: batch so the now() pair is noise; sample so p50 != p90. */ \
        long _bs = (long)(MIN_BATCH_SECONDS / _one) + 1;                        \
        if (_bs > MAX_BATCH) _bs = MAX_BATCH;                                   \
        double _per = (double)_bs * _one;                                       \
        int _ns = (int)(MIN_SECONDS / _per) + 1;                                \
        if (_ns < MIN_SAMPLES) _ns = MIN_SAMPLES;                               \
        if (_ns > MAX_SAMPLES) _ns = MAX_SAMPLES;                               \
        int _fit = (int)(MAX_MEASURE_SECONDS / _per);                            \
        if (_ns > _fit) _ns = (_fit > ABS_MIN_SAMPLES) ? _fit : ABS_MIN_SAMPLES; \
        double *_p = realloc(samples, (size_t)_ns * sizeof(double));            \
        if (!_p) { fprintf(stderr, "gbb: sample realloc failed\n"); exit(2); }  \
        samples = _p;                                                          \
        for (int r = 0; r < _ns; r++) {                                          \
            double _a = now();                                                 \
            for (long _i = 0; _i < _bs; _i++) { CALL; }                         \
            samples[r] = (now() - _a) / (double)_bs;                             \
        }                                                                       \
        nreps = _ns; g_batch = _bs;                                             \
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
    emit("sgemm", m,n,k,c->lda_pad, samples, nreps, case_flops("sgemm",m,n,k), VERIFIED_UNCHECKED, "corner_check_absent_fp32");
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
    /* dtrsm is destructive and TIMED_LOOP never restores B, so every rep feeds
       on the previous rep's output. fill_tri_d bounds the per-rep gain, but the
       bound is an argument, not a measurement -- check it. */
    int fin = operand_finite(B, (size_t)ldb*n);
    emit("dtrsm", m,n,0,c->lda_pad, samples, nreps, case_flops("dtrsm",m,n,0),
         fin ? VERIFIED_UNCHECKED : VERIFIED_FAIL,
         fin ? "corner_check_absent" : "operand_left_finite_range");
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
    /* destructive and unrestored, exactly as dtrsm above */
    int fin = operand_finite(B, (size_t)ldb*n);
    emit("dtrmm", m,n,0,c->lda_pad, samples, nreps, case_flops("dtrmm",m,n,0),
         fin ? VERIFIED_UNCHECKED : VERIFIED_FAIL,
         fin ? "corner_check_absent" : "operand_left_finite_range");
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
    emit("dsyrk", n,n,k,c->lda_pad, samples, nreps, case_flops("dsyrk",0,n,k), VERIFIED_UNCHECKED, "corner_check_absent");
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
    emit("dsymm", m,n,0,c->lda_pad, samples, nreps, case_flops("dsymm",m,n,0), VERIFIED_UNCHECKED, "corner_check_absent");
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
    emit("dgemv", m,n,0,c->lda_pad, samples, nreps, case_flops("dgemv",m,n,0), VERIFIED_UNCHECKED, "corner_check_absent");
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
    g_incx = incx;
    emit(which, n,0,0,0, samples, nreps, case_flops(which,n,0,0),
         VERIFIED_UNCHECKED, note);
    g_incx = 1;
    free(samples); free(x); free(y);
}

/* ---- size regimes ------------------------------------------------------ */
/* Three regimes chosen so the analysis can separate kernel quality from
   memory behaviour. These boundaries are compile-time constants and are the
   same on every host: nothing re-derives them from measured cache sizes, and
   no environment variable overrides them. That is deliberate -- per-host size
   ladders would mean the cross-host comparison that is the whole deliverable
   was comparing different problem sets. The consequence is that "small" is a
   fixed number of elements, not a fixed fraction of L1, so a given regime label
   sits at a different cache level on Gv2 than on Gv5. The measured L1d/L2/L3 of
   each host is recorded in results/env-<run_id>.json by capture-env.sh, which is
   what a reader needs to see where these boundaries actually fell on that host.
   Changing these requires asking Scott (CLAUDE.md, "Ask before"). */
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
    /* Verify the artifact, not the intent. OPENBLAS_CORETYPE is a request that
       force_coretype() may silently ignore, and GBB_ARCH_SELECTED is the
       runner's answer measured elsewhere. Where the library under test can be
       asked directly, its answer wins and the runner's is checked against it.
       A disagreement is fatal: it means the records this process is about to
       write would be labelled with a property of some other library or some
       other environment, and standing order 10 makes a mislabelled arm worse
       than a missing one -- a failed run is a gap, a plausible wrong answer is
       not. */
    const char *claimed = env_or("GBB_ARCH_SELECTED", "unknown");
    char *(*get_corename)(void) = corename_fn();
    if (get_corename) {
        const char *actual = get_corename();
        g_arch_selected = (actual && *actual) ? actual : "unknown";
        if (!is_sentinel(claimed) && strcmp(claimed, g_arch_selected) != 0) {
            fprintf(stderr,
                "gbb: FATAL: arch_selected disagrees between the runner's probe and this "
                "process. The runner passed GBB_ARCH_SELECTED=%s; the libopenblas actually "
                "linked here reports %s. One of them describes a different library or a "
                "different environment, so every record this arm would write is "
                "mislabelled. Refusing to measure. Check that gbb-coreprobe-%s and this "
                "binary resolve the same libopenblas by rpath, and that OPENBLAS_CORETYPE "
                "was exported to both.\n",
                claimed, g_arch_selected, g_target ? g_target : "?");
            exit(4);
        }
    } else {
        /* No OpenBLAS linked, so the question does not apply to this arm. The
           runner passes "n/a" for ArmPL and BLIS; anything else here would be a
           label with nothing behind it. */
        g_arch_selected = claimed;
    }
    g_blas_sha       = env_or("GBB_BLAS_SHA", "unknown");
    g_coretype       = env_or("GBB_CORETYPE", "unforced");
    g_thread_backend = env_or("GBB_THREAD_BACKEND", "unknown");
    g_pin_policy     = env_or("GBB_PIN_POLICY", "none");
    g_role           = env_or("GBB_ROLE", "unknown");
    g_threads       = atoi(env_or("GBB_THREADS", "1"));

    calibrate_timer();

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
