/* gbb roofline — measured peak, not theoretical peak.
 *
 * Emits NDJSON records for:
 *   peak_fma          : achieved FP64/FP32 FMA rate, single core
 *   peak_fma_allcore  : same chain on every thread (scaling check)
 *   bandwidth         : achieved triad bandwidth at a footprint that defeats L3
 *
 * These are the denominators. Theoretical peak from clock x lanes x FMA is not
 * used anywhere in the analysis: it silently rescales every efficiency number
 * whenever the effective all-core clock differs from the datasheet value.
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
 *   So peak_fma is a LOWER BOUND and a cross-check, not the denominator.
 *   The analysis uses max-observed-GFLOP/s across every (library, target) arm
 *   on that host as the primary denominator -- an empirical ceiling that no
 *   compiler decision can inflate. peak_fma exists to catch the case where
 *   every arm on a host is bad and the empirical ceiling is therefore too low
 *   to notice. If peak_fma materially exceeds the best GEMM result, that gap
 *   is the finding. See analysis/decompose.py:denominator().
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
    int threads = atoi(env_or("GBB_THREADS","1"));

    long iters = 4000000;

    double f64_1 = peak_fma_f64(iters);
    double f32_1 = peak_fma_f32(iters);
    sanity_check("peak_fma_f64", f64_1*1e-9, 1);
    sanity_check("peak_fma_f32", f32_1*1e-9, 1);

    printf("{\"run_id\":\"%s\",\"host\":\"%s\",\"instance\":\"%s\",\"build\":\"%s\","
           "\"threads\":1,\"metric\":\"peak_fma\",\"accumulators\":%d,"
           "\"gflops_f64\":%.4f,\"gflops_f32\":%.4f}\n",
           run_id, host, instance, build, ACC, f64_1*1e-9, f32_1*1e-9);

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
        printf("{\"run_id\":\"%s\",\"host\":\"%s\",\"instance\":\"%s\",\"build\":\"%s\","
               "\"threads\":%d,\"metric\":\"peak_fma_allcore\",\"accumulators\":%d,"
               "\"gflops_f64\":%.4f,\"scaling_efficiency\":%.4f}\n",
               run_id, host, instance, build, threads, ACC,
               all*1e-9, all / (f64_1 * threads));
    }
#endif

    /* 512 MiB per array defeats any L3 currently shipping on Graviton,
       including Graviton5's enlarged cache. */
    size_t n = (size_t)64*1024*1024;
    double bw = triad(n, 5);
    printf("{\"run_id\":\"%s\",\"host\":\"%s\",\"instance\":\"%s\",\"build\":\"%s\","
           "\"threads\":%d,\"metric\":\"bandwidth\",\"array_bytes\":%zu,"
           "\"triad_gbs\":%.4f}\n",
           run_id, host, instance, build, threads, n*sizeof(double), bw*1e-9);

    if (g_sink == 1234.5678) fputs("", stderr);   /* keep results live */
    return 0;
}
