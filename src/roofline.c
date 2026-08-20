/* gbb roofline — measured peak, not theoretical peak.
 *
 * Emits NDJSON records for:
 *   peak_fma          : achieved FP64/FP32 FMA rate, single core
 *   peak_fma_allcore  : same chain on every thread (scaling check)
 *   bandwidth         : achieved triad bandwidth at a footprint that defeats L3
 *
 * These are the instrument-side numbers. Theoretical peak from clock x lanes x FMA
 * is not used anywhere in the analysis: it silently rescales every efficiency
 * number whenever the effective all-core clock differs from the datasheet value.
 * Nor is peak_fma the denominator -- see WHAT THIS NUMBER IS, AND IS NOT below.
 *
 * OPTIMIZER HAZARD (load-bearing):
 *   The obvious formulation -- a[i] = a[i]*b + c with b,c compile-time
 *   constants -- gets folded away entirely. The first draft of this file
 *   reported 927 TFLOP/s on a single core. b and c are therefore read from
 *   volatile storage once per outer iteration, which the compiler cannot hoist,
 *   so the chain must actually execute. Cost: two loads per ACC FMAs.
 *   sanity_check() hard-fails above a bound no shipping Arm core can reach, so
 *   a future compiler that defeats this is caught loudly rather than quietly
 *   inflating every efficiency figure downstream.
 *
 *   ACC must exceed FMA latency x issue width or this measures latency rather
 *   than throughput. 32 covers Neoverse N1 through V3. The value is emitted in
 *   the record so an anomalously low reading on a deeper-pipelined future core
 *   is identifiable after the fact.
 *
 * WHAT THIS NUMBER IS, AND IS NOT:
 *   Whether the accumulator array vectorises into NEON or SVE is entirely the
 *   compiler's decision, and it varies by -O level, -march, and gcc version.
 *   So peak_fma is a LOWER BOUND, never the denominator. The analysis uses
 *   max-observed-GFLOP/s across every (library, target) arm on that host as the
 *   primary denominator -- an empirical ceiling that no compiler decision can
 *   inflate. See analysis/decompose.py:compute_scaling().
 *
 *   peak_fma was ALSO a cross-check on that ceiling, and that role is RETIRED
 *   (2026-08-20, Scott's call). It was justified on the one case the empirical
 *   ceiling cannot see -- every arm on a host being bad, which moves the ceiling
 *   down with the arms -- and on this hardware it cannot detect that case. At -O2
 *   with no -march=native (standing order 6 applies to the whole harness,
 *   including this file) the chain does not vectorise into SVE, and on
 *   c8g.metal-48xl at one thread it measures 4.22 GFLOP/s against a best large
 *   DGEMM of 18.16 -- 4.3x under the quantity it was bounding. A bound that can
 *   never be exceeded is not a check that passes; it is an absent check reading as
 *   protection, so decompose.py now raises no anomaly on peak_fma in either
 *   direction and prints it in section 6 as provenance, labelled as such.
 *   Compiling this file alone at -O3 -march=native would make the check
 *   discriminating and was rejected: it breaks the identical-harness rule and
 *   makes the campaign's only independent floor a function of gcc's vectoriser.
 *
 *   The OPTIMIZER HAZARD block above is NOT retired with it. sanity_check()'s
 *   abort guards against the chain being folded away, which is a different
 *   question from whether the resulting number bounds anything, and it stays.
 */

#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <stdint.h>

#ifdef _OPENMP
#include <omp.h>
#endif

#define ACC 32

/* No shipping Arm core comes near this per-core. Anything above means the
   chain was optimized out, not that the core is fast. */
#define IMPLAUSIBLE_GFLOPS_PER_CORE 200.0

static double now(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec + 1e-9 * ts.tv_nsec;
}

static const char *env_or(const char *k, const char *d) {
    const char *v = getenv(k); return (v && *v) ? v : d;
}

static volatile double vb64 = 1.0000001, vc64 = 0.9999999;
static volatile float  vb32 = 1.0000001f, vc32 = 0.9999999f;
static volatile double g_sink = 0.0;

static double peak_fma_f64(long iters) {
    double a[ACC];
    for (int i = 0; i < ACC; i++) a[i] = 1.0 + 0.001*i;
    double t0 = now();
    for (long it = 0; it < iters; it++) {
        double b = vb64, c = vc64;          /* volatile: cannot be hoisted */
        for (int i = 0; i < ACC; i++) a[i] = a[i]*b + c;
    }
    double t = now() - t0;
    double s = 0; for (int i = 0; i < ACC; i++) s += a[i];
    g_sink += s;
    return 2.0 * (double)ACC * (double)iters / t;
}

static double peak_fma_f32(long iters) {
    float a[ACC];
    for (int i = 0; i < ACC; i++) a[i] = 1.0f + 0.001f*i;
    double t0 = now();
    for (long it = 0; it < iters; it++) {
        float b = vb32, c = vc32;
        for (int i = 0; i < ACC; i++) a[i] = a[i]*b + c;
    }
    double t = now() - t0;
    float s = 0; for (int i = 0; i < ACC; i++) s += a[i];
    g_sink += (double)s;
    return 2.0 * (double)ACC * (double)iters / t;
}

static void sanity_check(const char *what, double gflops, int threads) {
    double per_core = gflops / (threads > 0 ? threads : 1);
    if (per_core > IMPLAUSIBLE_GFLOPS_PER_CORE) {
        fprintf(stderr,
            "gbb: FATAL: %s reported %.1f GFLOP/s (%.1f per core). Not "
            "achievable on any shipping Arm core -- the FMA chain was optimized "
            "away. Discard this run and check the compiler and -O level in the "
            "build manifest.\n", what, gflops, per_core);
        exit(3);
    }
}

static double triad(size_t n, int reps) {
    double *a = aligned_alloc(4096, n*sizeof(double));
    double *b = aligned_alloc(4096, n*sizeof(double));
    double *c = aligned_alloc(4096, n*sizeof(double));
    if (!a || !b || !c) { fprintf(stderr, "gbb: triad alloc failed\n"); exit(2); }

    /* First-touch under the same thread layout that will read it, so pages
       land on the right NUMA node on multi-socket instances. */
#ifdef _OPENMP
    #pragma omp parallel for schedule(static)
#endif
    for (size_t i = 0; i < n; i++) { a[i]=1.0; b[i]=2.0; c[i]=0.5; }

    double s = 1.5, best = 1e30;
    for (int r = 0; r < reps; r++) {
        double t0 = now();
#ifdef _OPENMP
        #pragma omp parallel for schedule(static)
#endif
        for (size_t i = 0; i < n; i++) a[i] = b[i] + s*c[i];
        double t = now() - t0;
        if (t < best) best = t;
    }
    g_sink += a[0] + a[n-1];
    double bytes = 3.0 * (double)n * sizeof(double);   /* 2 read + 1 write */
    free(a); free(b); free(c);
    return bytes / best;
}

int main(void) {
    const char *run_id   = env_or("GBB_RUN_ID","unset");
    const char *host     = env_or("GBB_HOST","unknown");
    const char *instance = env_or("GBB_INSTANCE","unknown");
    const char *build    = env_or("GBB_BUILD","unknown");
    /* Campaign data and instrument checks must not be mixable by accident. The
       runner decides the role from evidence it cannot fake -- a host with no
       IMDS instance type is never "campaign" -- and stamps it into every record
       here. The default is "unknown", not "campaign": a record produced by
       running this binary by hand is not campaign data, and the analysis
       excludes anything that does not say campaign. */
    const char *role     = env_or("GBB_ROLE","unknown");
    /* Standing order 9 says record the binding policy per arm, and until
       2026-08-20 these records did not: bench.c emitted `pin_policy` and roofline
       did not, so for the one instrument that showed the t>=128 efficiency cliff
       most starkly -- peak_fma_allcore fell from 94% to 53% per core between t=96
       and t=128 -- the applied policy was not in the record. The runner sets the
       same GBB_PIN_POLICY for both binaries; only the printf was missing. */
    const char *pin_policy = env_or("GBB_PIN_POLICY","none");
    int threads = atoi(env_or("GBB_THREADS","1"));

    /* Shared provenance, built once. It carries the OpenMP PLACE MAP, which is
     * here to eliminate a whole class of explanation for that cliff before an
     * instance is launched to chase it. peak_fma_allcore does no DRAM traffic, so
     * page placement cannot touch it -- but if OMP_PLACES enumerates fewer places
     * than there are threads, threads double up on cores and per-core efficiency
     * falls for a reason that has nothing to do with NUMA at all. That is invisible
     * without the field and obvious with it.
     *
     * `omp_place_procs_total` is not redundant with the other two: places may be
     * heterogeneous, so the count of places and the size of place 0 together do
     * not answer "are there fewer hardware threads in the map than threads asked
     * for", and that sum is the number that does.
     *
     * null, not -1 or 0, when the binary has no OpenMP: absent and zero are
     * different claims, and `make roofline` already warns loudly about the build
     * that produces the absence. Zero places is a real and interesting value --
     * the runtime exposing no place list at all -- so it must not share an
     * encoding with "this binary cannot answer". */
    char prov[640];
#ifdef _OPENMP
    int nplaces = omp_get_num_places();
    int p0procs = nplaces > 0 ? omp_get_place_num_procs(0) : 0;
    int ptotal = 0;
    for (int p = 0; p < nplaces; p++) ptotal += omp_get_place_num_procs(p);
    snprintf(prov, sizeof prov,
             "\"run_id\":\"%s\",\"host\":\"%s\",\"instance\":\"%s\",\"build\":\"%s\","
             "\"role\":\"%s\",\"pin_policy\":\"%s\","
             "\"omp_places\":%d,\"omp_place_procs\":%d,\"omp_place_procs_total\":%d",
             run_id, host, instance, build, role, pin_policy,
             nplaces, p0procs, ptotal);
#else
    snprintf(prov, sizeof prov,
             "\"run_id\":\"%s\",\"host\":\"%s\",\"instance\":\"%s\",\"build\":\"%s\","
             "\"role\":\"%s\",\"pin_policy\":\"%s\","
             "\"omp_places\":null,\"omp_place_procs\":null,"
             "\"omp_place_procs_total\":null",
             run_id, host, instance, build, role, pin_policy);
#endif

    long iters = 4000000;

    double f64_1 = peak_fma_f64(iters);
    double f32_1 = peak_fma_f32(iters);
    sanity_check("peak_fma_f64", f64_1*1e-9, 1);
    sanity_check("peak_fma_f32", f32_1*1e-9, 1);

    printf("{%s,"
           "\"threads\":1,\"metric\":\"peak_fma\",\"accumulators\":%d,"
           "\"gflops_f64\":%.4f,\"gflops_f32\":%.4f}\n",
           prov, ACC, f64_1*1e-9, f32_1*1e-9);

    /* All-core peak. Graviton has no SMT and no turbo, so this should be
       threads x the single-core figure. A shortfall is itself a finding
       (mesh, power, or vCPU oversubscription); the analysis flags it rather
       than assuming linearity. */
#ifdef _OPENMP
    if (threads > 1) {
        double t0 = now();
        #pragma omp parallel num_threads(threads)
        { peak_fma_f64(iters); }
        double t = now() - t0;
        double all = 2.0*(double)ACC*(double)iters*threads / t;
        sanity_check("peak_fma_f64_allcore", all*1e-9, threads);
        printf("{%s,"
               "\"threads\":%d,\"metric\":\"peak_fma_allcore\",\"accumulators\":%d,"
               "\"gflops_f64\":%.4f,\"scaling_efficiency\":%.4f}\n",
               prov, threads, ACC, all*1e-9, all / (f64_1 * threads));
    }
#endif

    /* 512 MiB per array defeats any L3 currently shipping on Graviton,
       including Graviton5's enlarged cache. */
    size_t n = (size_t)64*1024*1024;
    double bw = triad(n, 5);
    printf("{%s,"
           "\"threads\":%d,\"metric\":\"bandwidth\",\"array_bytes\":%zu,"
           "\"triad_gbs\":%.4f}\n",
           prov, threads, n*sizeof(double), bw*1e-9);

    if (g_sink == 1234.5678) fputs("", stderr);   /* keep results live */
    return 0;
}
