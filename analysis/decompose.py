#!/usr/bin/env python3
"""graviton-blas-bench decompose — turn the raw sweep into a decision.

The question is not "is OpenBLAS slower than ArmPL on Graviton". That is known.
The question is WHERE the deficit lives, so the answer is either "here is a
tractable thing worth fixing" or "there is nothing here, publish the survey and
move on". Both are acceptable outcomes; this script is written so the second
one is as visible as the first.

Reports produced:

  1. deficit-by-routine   OpenBLAS vs best-available, per routine, per regime.
                          A routine that is at parity is not worth touching.
  2. target-cross         Same hardware, different OpenBLAS TARGET=. This is
                          the experiment: it separates "V2/V3 silicon is bad at
                          SVE" from "the N2 kernel set is worse than the V1 one".
  3. regime-profile       Small / medium / large. The N2 kernel set defines no
                          GEMM_SMALL_* entries at all, so if a deficit exists it
                          should be visible in small and absent in large.
  4. lda-penalty          Tight vs padded leading dimension, which isolates
                          packing-kernel quality from the inner kernel.
  5. scaling              GFLOP/s vs threads against the measured all-core peak.
  6. anomalies            Everything that should stop a conclusion: failed
                          verification, min/p50 divergence, unrecognised MIDR,
                          arms that fell back to generic ARMV8.

Usage:
    python3 decompose.py results/  [--min-effect 0.05]
"""

import argparse
import json
import pathlib
import sys
from collections import defaultdict


# ---- regime boundaries ----------------------------------------------------
# Deliberately coarse. The point is to separate "fits in cache and the fixed
# overheads dominate" from "streaming from DRAM", not to draw a precise line.
def regime(n: int) -> str:
    if n <= 256:
        return "small"
    if n <= 1536:
        return "medium"
    return "large"


def load(results_dir: pathlib.Path):
    bench, roof, envs = [], [], []
    for p in sorted(results_dir.glob("*.ndjson")):
        for line in p.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                print(f"warn: unparseable line in {p.name}", file=sys.stderr)
                continue
            if "metric" in r:
                roof.append(r)
            elif "routine" in r or r.get("failed"):
                bench.append(r)
    for p in sorted(results_dir.glob("env-*.json")):
        envs.append(json.loads(p.read_text()))
    return bench, roof, envs


def key_hw(r):
    return r.get("instance", "unknown")


def denominator(bench, roof, instance, threads):
    """Primary denominator: best GFLOP/s any arm achieved on this host at this
    thread count, over large dgemm. Empirical, so no compiler decision can
    inflate it. peak_fma is carried alongside as a cross-check -- if it
    materially exceeds the empirical ceiling, every arm on the host is bad and
    that gap is itself the headline."""
    obs = [r["gflops"] for r in bench
           if r.get("instance") == instance and r.get("threads") == threads
           and r.get("routine") == "dgemm" and regime(r.get("m", 0)) == "large"
           and r.get("verified", True)]
    empirical = max(obs) if obs else None
    pk = [r for r in roof if r.get("instance") == instance
          and r.get("threads") == threads
          and r.get("metric") in ("peak_fma", "peak_fma_allcore")]
    micro = max((r.get("gflops_f64", 0) for r in pk), default=None)
    return empirical, micro


def pct(a, b):
    if not a or not b:
        return None
    return (b - a) / b


def fmt_pct(x):
    return "  n/a " if x is None else f"{100*x:+6.1f}%"


def report_deficit_by_routine(bench, out):
    """For each (instance, threads, routine, regime): OpenBLAS's best arm vs
    the best arm overall. Positive deficit means OpenBLAS is behind."""
    out("\n" + "=" * 78)
    out("1. DEFICIT BY ROUTINE  — OpenBLAS best vs best-available")
    out("=" * 78)
    out("Positive = OpenBLAS behind. Anything inside noise is NOT worth working on.")

    g = defaultdict(lambda: defaultdict(list))
    for r in bench:
        if r.get("failed") or not r.get("verified", True):
            continue
        k = (r.get("instance"), r.get("threads"), r.get("routine"), regime(r.get("m", 0)))
        g[k][r.get("library")].append(r["gflops"])

    for (inst, thr, routine, reg) in sorted(g, key=lambda k: (str(k[0]), k[1] or 0, str(k[2]), str(k[3]))):
        libs = g[(inst, thr, routine, reg)]
        best_by_lib = {L: max(v) for L, v in libs.items()}
        if "openblas" not in best_by_lib or len(best_by_lib) < 2:
            continue
        ob = best_by_lib["openblas"]
        winner = max(best_by_lib, key=best_by_lib.get)
        best = best_by_lib[winner]
        out(f"  {inst!s:10s} t={thr:<4} {routine:6s} {reg:6s} "
            f"openblas={ob:9.2f}  best={best:9.2f} ({winner:8s})  "
            f"deficit={fmt_pct(pct(ob, best))}")


def report_target_cross(bench, out):
    """The experiment. Same silicon, different OpenBLAS TARGET=.

    If NEOVERSEV1 (99 SVE kernels) beats NEOVERSEV2 (5 SVE kernels) when both
    run on c8g/c9g, the N2 kernel set is leaving performance on the table and
    the gap-closing work is justified. If it loses, the N2 target's NEON choice
    was correct and the SVE angle is dead -- which is a publishable negative
    result and should be reported as one."""
    out("\n" + "=" * 78)
    out("2. TARGET CROSS  — same hardware, different OpenBLAS kernel set")
    out("=" * 78)
    out("NEOVERSEV1 = 99 SVE kernels.  NEOVERSEV2 -> KERNEL.NEOVERSEN2 = 5 SVE kernels.")
    out("If V1 > V2 on Graviton4/5 silicon, closing the N2 gap is worth doing.")

    g = defaultdict(dict)
    for r in bench:
        if r.get("failed") or r.get("library") != "openblas" or not r.get("verified", True):
            continue
        k = (r.get("instance"), r.get("threads"), r.get("routine"), regime(r.get("m", 0)))
        t = r.get("target")
        g[k][t] = max(g[k].get(t, 0.0), r["gflops"])

    for k in sorted(g, key=lambda x: (str(x[0]), x[1] or 0, str(x[2]), str(x[3]))):
        inst, thr, routine, reg = k
        tg = g[k]
        if "NEOVERSEV1" not in tg or "NEOVERSEV2" not in tg:
            continue
        v1, v2 = tg["NEOVERSEV1"], tg["NEOVERSEV2"]
        verdict = "V1 kernels win" if v1 > v2 * 1.02 else \
                  ("V2 kernels win" if v2 > v1 * 1.02 else "parity")
        out(f"  {inst!s:10s} t={thr:<4} {routine:6s} {reg:6s} "
            f"V1set={v1:9.2f}  V2set={v2:9.2f}  "
            f"delta={fmt_pct(pct(v2, v1))}  {verdict}")


def report_lda_penalty(bench, out):
    """Padded leading dimension isolates packing quality. A library whose
    packing kernels are good barely notices lda_pad; one that relies on the
    tight-stride fast path falls off a cliff."""
    out("\n" + "=" * 78)
    out("3. LEADING-DIMENSION PENALTY  — isolates packing from the inner kernel")
    out("=" * 78)
    g = defaultdict(dict)
    for r in bench:
        if r.get("failed") or r.get("routine") != "dgemm" or not r.get("verified", True):
            continue
        k = (r.get("instance"), r.get("library"), r.get("target"),
             r.get("threads"), r.get("m"))
        g[k][r.get("lda_pad", 0)] = r["gflops"]
    for k in sorted(g, key=lambda x: (str(x[0]), str(x[1]), str(x[2]), x[3] or 0, x[4] or 0)):
        d = g[k]
        if 0 not in d or 8 not in d:
            continue
        inst, lib, tgt, thr, m = k
        out(f"  {inst!s:10s} {lib!s:9s} {tgt!s:11s} t={thr:<4} n={m:<6} "
            f"tight={d[0]:9.2f}  padded={d[8]:9.2f}  penalty={fmt_pct(pct(d[8], d[0]))}")


def report_scaling(bench, roof, out):
    out("\n" + "=" * 78)
    out("4. SCALING  — against measured all-core peak, not theoretical")
    out("=" * 78)
    instances = sorted({r.get("instance") for r in bench if r.get("instance")})
    for inst in instances:
        threads = sorted({r.get("threads") for r in bench
                          if r.get("instance") == inst and r.get("threads")})
        for thr in threads:
            emp, micro = denominator(bench, roof, inst, thr)
            if emp is None:
                continue
            flag = ""
            if micro and micro > emp * 1.15:
                flag = f"  <-- microbench peak {micro:.1f} exceeds best GEMM by " \
                       f"{100*(micro-emp)/emp:.0f}%: every arm on this host may be leaving headroom"
            out(f"  {inst!s:10s} t={thr:<4} best_dgemm={emp:9.2f} GFLOP/s"
                f"  peak_fma={micro if micro else float('nan'):9.2f}{flag}")


def report_anomalies(bench, envs, out, min_effect):
    out("\n" + "=" * 78)
    out("5. ANOMALIES  — read this before trusting anything above")
    out("=" * 78)
    n = 0

    for e in envs:
        if e.get("core_name") == "UNRECOGNISED":
            n += 1
            out(f"  ! {e.get('instance_type')}: MIDR part {e.get('midr_part')} not in "
                f"OpenBLAS dispatch. DYNAMIC_ARCH falls back to generic ARMV8 here.")
        sel = (e.get("openblas_dynamic_selection") or "").lower()
        if sel and "armv8" in sel and "sve" not in sel:
            n += 1
            out(f"  ! {e.get('instance_type')}: DYNAMIC_ARCH selected '{sel}'. "
                f"Default NumPy/R/Julia on this instance run generic NEON.")
        if e.get("threads_per_core", 1) != 1:
            n += 1
            out(f"  ! {e.get('instance_type')}: SMT is on. Not a Graviton host.")
        gov = e.get("cpufreq_governor")
        if gov not in (None, "", "none", "performance"):
            n += 1
            out(f"  ! {e.get('instance_type')}: cpufreq governor '{gov}'. Timings not comparable.")
        if (e.get("numa_nodes") or 1) > 1:
            out(f"  . {e.get('instance_type')}: {e['numa_nodes']} NUMA nodes — "
                f"multithreaded arms cross a socket boundary; compare per-socket too.")

    failed = [r for r in bench if r.get("failed")]
    for r in failed:
        n += 1
        note = " (SIGILL: target needs ISA this host lacks)" if r.get("exit_code") == 132 else ""
        out(f"  ! arm failed: {r.get('library')}/{r.get('target')} "
            f"threads={r.get('threads')} exit={r.get('exit_code')}{note}")

    unver = [r for r in bench if r.get("verified") is False]
    for r in unver[:20]:
        n += 1
        out(f"  !! WRONG ANSWER: {r.get('library')}/{r.get('target')} {r.get('routine')} "
            f"n={r.get('m')} threads={r.get('threads')} — fast but incorrect, discard")
    if len(unver) > 20:
        out(f"  !! ... and {len(unver)-20} more failed verifications")

    noisy = [r for r in bench
             if r.get("t_min") and r.get("t_p50")
             and r["t_p50"] > r["t_min"] * 1.25]
    if noisy:
        out(f"  . {len(noisy)} measurements where p50 exceeds min by >25%. "
            f"On a no-turbo, no-SMT host this suggests a noisy neighbour; "
            f"prefer the metal sizes for those arms.")

    if n == 0:
        out("  none")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("results", type=pathlib.Path)
    ap.add_argument("--min-effect", type=float, default=0.05,
                    help="deficits below this fraction are treated as parity")
    args = ap.parse_args()

    bench, roof, envs = load(args.results)
    if not bench:
        print(f"no benchmark records found under {args.results}", file=sys.stderr)
        return 1

    lines = []
    out = lines.append
    out(f"graviton-blas-bench decomposition — {len(bench)} measurements, {len(envs)} hosts, "
        f"parity threshold {100*args.min_effect:.0f}%")

    report_deficit_by_routine(bench, out)
    report_target_cross(bench, out)
    report_lda_penalty(bench, out)
    report_scaling(bench, roof, out)
    report_anomalies(bench, envs, out, args.min_effect)

    out("\n" + "=" * 78)
    out("DECISION GUIDE")
    out("=" * 78)
    out("  Section 2 shows V1-set > V2-set on c8g/c9g   -> closing the N2 gap is justified;")
    out("                                                  it needs no new kernel code.")
    out("  Section 2 shows parity or V2-set winning     -> the NEON choice was right;")
    out("                                                  publish the negative result.")
    out("  Section 3 shows a large lda penalty          -> packing kernels are the target")
    out("                                                  (this is where SVE2 TBL2/TBX would apply).")
    out("  Section 1 deficit concentrated in 'small'    -> the missing GEMM_SMALL_* path;")
    out("                                                  cheapest possible fix.")
    out("  Section 5 flags a generic-ARMV8 fallback     -> stop and report that first;")
    out("                                                  it outweighs every kernel question.")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
