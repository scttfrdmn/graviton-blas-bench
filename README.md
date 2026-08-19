# graviton-blas-bench

A harness for deciding whether OpenBLAS kernel work on Graviton is worth doing,
and for producing a publishable decomposition either way.

Binaries and environment variables use the `gbb` / `GBB_` initialism for
brevity; the project, directory and repository are `graviton-blas-bench`.

The headline number already exists: on Graviton4, ArmPL runs roughly 20–31%
ahead of a source-built OpenBLAS. What does not exist publicly is the
**decomposition** — which routines, which size regimes, which thread counts,
and whether the deficit is ISA selection or microkernel quality. That
decomposition is the deliverable. A null result is a valid and reportable
outcome, and the analysis is written to make a null as visible as a hit.

## The experiment

The five instance families span three distinct vector regimes, and each lands
on a *different* OpenBLAS kernel set:

| instance | Graviton | core | vector | OpenBLAS target | SVE kernels |
|---|---|---|---|---|---|
| `c6g`   | 2  | Neoverse N1 | NEON only    | `NEOVERSEN1`        | 0  |
| `c7g`   | 3  | Neoverse V1 | SVE1, VL=256 | `NEOVERSEV1`        | 99 |
| `hpc7g` | 3E | Neoverse V1 | SVE1, VL=256 | `NEOVERSEV1`        | 99 |
| `c8g`   | 4  | Neoverse V2 | SVE2, VL=128 | `NEOVERSEV2`→`N2`   | 5  |
| `c9g`   | 5  | Neoverse V3 | SVE2, VL=128 | `NEOVERSEV2`→`N2`   | 5  |

Run naively this confounds silicon with kernel set. So the harness **crosses
them**: every OpenBLAS `TARGET=` is built on every host, including targets the
host is not. Running the SVE-rich `NEOVERSEV1` kernel set *on* Graviton4 and
Graviton5 is the measurement that separates "V2/V3 is bad at SVE" from "the N2
kernel set is worse than the V1 one." That single comparison decides whether the
90-operation N2 gap is worth closing — and closing it requires no new kernel
code, only kernel selection.

`ARMV8` (generic NEON) and `DYNAMIC_ARCH` are built as controls. `DYNAMIC_ARCH`
is what distro packages and NumPy wheels actually ship, so what it selects at
runtime on each host is a finding in its own right, not bookkeeping.

## Quickstart

Per host:

```bash
export GBB_PREFIX=$HOME/graviton-blas-bench-libs
export ARMPL_DIR=/opt/arm/armpl_24.10_gcc          # optional but wanted
bash scripts/build-libs.sh                          # ~40 min, builds 6 OpenBLAS variants
bash scripts/run-matrix.sh                          # writes results/*.ndjson
```

Then, with results from all hosts collected into one directory:

```bash
python3 analysis/decompose.py results/
```

## Layout

```
src/bench.c            routine sweep, Fortran BLAS ABI, NDJSON per measurement
src/roofline.c         measured peak FMA + triad bandwidth (the denominators)
scripts/build-libs.sh  builds the OpenBLAS target cross, ArmPL link, BLIS
scripts/capture-env.sh MIDR, HWCAP, NUMA, governor, OpenBLAS runtime selection
scripts/run-matrix.sh  orchestrates library x target x threads on one host
analysis/decompose.py  the five reports plus an anomaly section
```

## Measurement discipline

- **First call discarded.** OpenBLAS allocates its buffer pool lazily; ArmPL
  and BLIS have first-touch costs. Two warmup reps precede every timing loop.
- **Minimum, with p50 and p90 recorded.** Min is the statistic; the others are
  kept so the analysis can flag arms where min is unrepresentative. On a
  no-turbo, no-SMT host a wide min/p50 spread means a noisy neighbour.
- **Reps scale with problem size** so every measurement runs at least 0.3 s.
- **Correctness is verified**, not assumed. A 4×4 corner of every DGEMM result
  is recomputed by hand and compared. A failed check poisons the record rather
  than reporting a fast wrong answer. `decompose.py` surfaces these loudly.
- **Measured peak, not theoretical.** The primary denominator is the best
  GFLOP/s any arm achieved on that host — an empirical ceiling no compiler
  decision can inflate. `peak_fma` from the microbenchmark is a cross-check: if
  it materially exceeds the best observed GEMM, every arm on that host is
  leaving headroom, and *that gap* is the headline.
- **Thread control is set for every library every time.** `OPENBLAS_NUM_THREADS`,
  `OMP_NUM_THREADS` and `BLIS_NUM_THREADS` all get the same value. Setting only
  one silently leaves the others at their defaults.
- **The harness itself is built identically everywhere** — `-O2`, no
  `-march=native`. Only the BLAS under test varies.

## Hazards, learned the hard way

- **The FMA peak chain gets optimized away.** The first draft of `roofline.c`
  reported 927 TFLOP/s on one core. Constants are now read from volatile
  storage, and a hard sanity bound aborts the run rather than letting an
  inflated denominator propagate silently into every efficiency figure.
- **ArmPL ships a serial and an OpenMP build.** Linking `libarmpl` instead of
  `libarmpl_mp` produces flat scaling that looks like an ArmPL threading bug
  and is not. The Makefile links `-larmpl_mp`.
- **`c8g.48xlarge` at 192 vCPU is likely two sockets; `c9g.48xlarge` at 192 may
  be one.** Confirm with `numactl -H` on first boot. A NUMA boundary in the
  middle of one arm and not the other will dominate the multithreaded numbers.
  The thread ladder always includes 64 so there is one directly comparable
  point across all five families, since `c6g`/`c7g`/`hpc7g` stop there.
- **Memory generation moves every step** — DDR4 on Gv2 through DDR5-8800 with a
  much larger L3 on Gv5. Large-N DGEMM partly measures that rather than kernel
  quality, which is why the small and medium regimes are reported separately
  and the triad bandwidth is captured alongside.
- **An unrecognised MIDR is a result, not an error.** OpenBLAS dispatch falls
  back to generic `ARMV8` for any part not in its switch. If that happens on
  `c9g`, default NumPy on the newest Graviton is running plain NEON, which
  outweighs every kernel question in this repo. `capture-env.sh` warns on it and
  `decompose.py` promotes it to the top of the anomaly section.

## Practical notes

- **`us-east-1` and `us-east-2` are the only regions carrying all five
  families.** Pin the whole campaign to one of them.
- **`hpc7g` has no metal size.** It is the one arm where tenancy cannot be
  eliminated; run it repeatedly and lean on the p50/p90 spread.
- Graviton has **no SMT and no turbo**, so 1 vCPU is 1 core and the
  iso-frequency machinery needed on x86 hosts is unnecessary here.
- ArmPL is a download from developer.arm.com, not a build. Install it out of
  band and point `ARMPL_DIR` at the prefix; the arm is skipped cleanly if unset.

## What the output supports

`decompose.py` ends with a decision guide keyed to its own sections:

- V1-set beats V2-set on `c8g`/`c9g` → closing the N2 gap is justified and needs
  no new kernel code.
- Parity, or V2-set winning → the NEON choice was correct; publish the negative
  result and drop the SVE angle.
- Large leading-dimension penalty → packing kernels are the target, which is
  the one place an SVE2 argument (TBL2/TBX, FCVTLT/FCVTNT) actually holds up.
- Deficit concentrated in the small regime → the missing `GEMM_SMALL_*` path on
  the N2 target, the cheapest possible fix.
- A generic-`ARMV8` fallback anywhere → report that first.
