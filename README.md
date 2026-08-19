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
them**: the SVE-rich `NEOVERSEV1` kernel set is run *on* Graviton4 and Graviton5.
That is the measurement separating "V2/V3 is bad at SVE" from "the N2 kernel set
is worse than the V1 one," and it decides whether the 90-operation N2 gap is
worth closing — closing it requires no new kernel code, only kernel selection.

**The cross is a runtime sweep, not a set of builds.** This is worth being
precise about, because the obvious way to do it is wrong. `TARGET=` is not only a
kernel-table selection: `Makefile.arm64` also uses it to set the compiler flags
applied to the *common* code, so comparing a `TARGET=NEOVERSEV1` build against a
`TARGET=NEOVERSEV2` build moves the kernel table **and** the codegen of every
shared source file at once, with no way to attribute the difference afterwards.

OpenBLAS's own `force_coretype()` makes every target in its switch reachable by
name at runtime via `OPENBLAS_CORETYPE`, so one `DYNAMIC_ARCH` binary can be
swept over `{ARMV8, ARMV8SVE, NEOVERSEN1, NEOVERSEV1, NEOVERSEV2, NEOVERSEN2}`
with one set of common-code flags and only the kernel table varying. Two static
`TARGET=` builds are kept per host — the host's native target and the cross
target — purely as controls on the mechanism: that `DYNAMIC_ARCH` dispatch costs
nothing measurable, and that a forced coretype lands where a real `TARGET=` build
does.

`OPENBLAS_CORETYPE` is a **request**. `force_coretype()` ignores a name it does
not know, and a non-`DYNAMIC_ARCH` build ignores the variable entirely, so
`src/coreprobe.c` verifies every coretype against `openblas_get_corename()`
before its arm runs. An arm whose request was not honoured is not run at all,
because labelling its records `coretype=NEOVERSEV1` would be claiming a number
we did not measure.

`DYNAMIC_ARCH` unforced is its own arm: it is what distro packages and NumPy
wheels actually ship, so what it selects on each host is a finding rather than
bookkeeping.

`ARMV8SVE` is **not** a control. `KERNEL.ARMV8SVE` is the file
`KERNEL.NEOVERSEN2` conspicuously does not include, so the `ARMV8SVE` arm is
the closest thing in-tree to "what Graviton 4 would get if the N2
kernel-selection gap were closed." It is a first-class experimental arm.

Two further arms exist to measure confounds rather than leave them in the
comparison. `DYNAMIC_OMP` is the same OpenBLAS built `USE_OPENMP=1`, so the
threading backend can be attributed; `DYNAMIC_OMP_BOUND` is that binary with
`OMP_PROC_BIND=close`, which measures what thread pinning is worth. See
§Measurement discipline for why those are arms and not fixes.

## Quickstart

Per host:

```bash
export GBB_PREFIX=$HOME/graviton-blas-bench-libs
export ARMPL_DIR=/opt/arm/armpl_24.10_gcc     # optional but wanted
export GBB_S3_URI=s3://your-bucket/gbb        # strongly advised, see below
export GBB_AWS_REGION=us-east-1
bash scripts/build-libs.sh                     # ~40 min
bash scripts/run-matrix.sh                     # writes results/*.ndjson
```

`GBB_S3_URI` is not optional in practice. Instances are terminated on completion
and a spot reclaim can come sooner, so results are shipped after every arm rather
than at the end of the sweep. Without it a multi-hour run exists only on a host
that is about to be destroyed.

`build-libs.sh` requires `OPENBLAS_REF` to be an immutable commit SHA — it
defaults to the audited `cc3fc1e`. The five hosts are built on different days,
and a branch name would let them silently get different libraries while the
cross-host comparison treats them as one. `GBB_ALLOW_MUTABLE_REF=1` overrides it.

`run-matrix.sh` refuses to start if `capture-env.sh` exits non-zero: 3 means this
host's timings would not be comparable, 4 means stop and escalate per standing
order 8. `GBB_FORCE_INVALID_HOST=1` and `GBB_ESCALATION_ACK="<note>"` override
them respectively, and the override is recorded in the census.

Then, with results from all hosts collected into one directory:

```bash
python3 analysis/decompose.py results/
```

## Layout

```
src/bench.c            routine sweep, Fortran BLAS ABI, NDJSON per measurement
src/roofline.c         measured peak FMA + triad bandwidth (the denominators)
src/coreprobe.c        what OpenBLAS actually selected, per OPENBLAS_CORETYPE
scripts/build-libs.sh  DYNAMIC_ARCH + control builds, ArmPL link, BLIS
scripts/capture-env.sh MIDR per core, HWCAP, NUMA, cgroups, governor, dispatch
scripts/run-matrix.sh  orchestrates arm x coretype x threads on one host
analysis/decompose.py  the reports, the coverage census, and an anomaly section
tests/                 stub-based regression suite for the runner's decisions
gates/                 one script per phase gate; each exits 0/1 with evidence
```

Each run writes, per host:

```
results/env-<run_id>.json        provenance; capture-env.sh's exit code gates the run
results/manifest-<run_id>.ndjson what was built, and why anything was not
results/census-<run_id>.ndjson   one record per attempted arm, with an outcome
results/topology-<run_id>.txt    numactl -H and lscpu, verbatim
results/roofline-<run_id>.ndjson measured peak FMA and triad bandwidth
results/bench-<run_id>.ndjson    the measurements
```

The census is load-bearing, not bookkeeping: without it the analysis cannot
distinguish "V1 and V2 are at parity" from "the V1 arm never ran," and those two
support opposite conclusions.

## Measurement discipline

- **First call discarded.** OpenBLAS allocates its buffer pool lazily; ArmPL
  and BLIS have first-touch costs. Two warmup reps precede every timing loop.
- **Minimum, with p50 and p90 recorded.** Min is the statistic; the others are
  kept so the analysis can flag arms where min is unrepresentative. On a
  no-turbo, no-SMT host a wide min/p50 spread means a noisy neighbour.
- **Calls are batched, and the batch is calibrated in two stages.** Bracketing
  every call with `now()` cost ~31 ns per pair — 27.9% of the sample at n=8 — and
  a constant additive term compresses ratios, biasing the campaign toward "no
  effect found" in the one regime where the missing `GEMM_SMALL_*` path should
  show. Sizing the batch from a single timed call does not work either: clock
  *resolution* and clock *overhead* are different quantities, and a coarse
  clocksource (1 µs is common under virtualisation, which `hpc7g` is) reads a
  58 ns call as zero. `timer_res_ns` is recorded in every record so a reader can
  check rather than trust this.
- **Reps scale with problem size** so every measurement runs at least 0.3 s.
- **Pinning is external and uniform.** Every arm is bound with `numactl`
  (falling back to `taskset`) to the same CPU set and memory policy, chosen from
  the real per-node CPU lists in `numactl -H` rather than an assumption that node
  N owns a contiguous range. `OMP_PROC_BIND` is explicitly **disabled** during
  the sweep. That is not an omission: it used to be set to `close` on every arm,
  and only OpenMP arms obey it — so ArmPL, the reference, was pinned and shipping
  pthread OpenBLAS was not, a systematic advantage to the reference of roughly the
  size of the deficit under investigation. What pinning is worth is measured by
  the `DYNAMIC_OMP_BOUND` arm instead of being left in the comparison as a bias.
  It is deliberately **not** equalised by rebuilding OpenBLAS with
  `USE_OPENMP=1`: that changes the threading backend and therefore what is under
  test, and pthreads is what the wheels ship.
- **One memory policy for the denominator and the measurement.** `bench.c`
  first-touches its matrices serially and `roofline.c` in parallel, so on a
  multi-node host the two used to land their pages on different nodes — making
  the standing-order-1 cross-check compare different machines. A single explicit
  `--membind`/`--interleave` policy for both removes that.
- **Every arm the runner declines to run is recorded with a reason.** Build
  failure, ISA the host lacks, a coretype the library ignored: each writes a
  census record. An unexplained gap in the results is then a detectable defect
  rather than an invisible one.
- **Correctness is verified where it is verified, and `null` where it is not.**
  A 4×4 corner of every DGEMM result is recomputed by hand and compared at
  `8 * k * DBL_EPSILON` — validated against a real optimised multithreaded BLAS at
  every k from 8 to 8192 with zero false positives. A failed check poisons the
  record rather than reporting a fast wrong answer. `verified` is **tri-state**:
  `true`, `false`, or `null` where no check for that routine exists. Seven of the
  eight drivers used to hardcode `verified=1` — including `dtrsm`, `dtrmm` and
  `dsymm`, precisely the operations in the 90-kernel N2 gap under study — so a
  fast-and-wrong generic TRSM would have produced a clean win. The analysis fails
  closed on this: only `true` counts as verified, and any verdict resting on
  `null` records is marked unverified.
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
- **`c8g.metal-48xl` at 192 vCPU is likely two sockets; `c9g.metal-48xl` at 192
  may be one.** Confirm with `numactl -H` on first boot. A NUMA boundary in the
  middle of one arm and not the other will dominate the multithreaded numbers.
  The thread ladder always includes 64 so there is one directly comparable
  point across all five families, since `c6g`/`c7g`/`hpc7g` stop there.
- **Memory generation moves every step** — DDR4 on Gv2 through DDR5-8800 with a
  much larger L3 on Gv5. Large-N DGEMM partly measures that rather than kernel
  quality, which is why the small and medium regimes are reported separately
  and the triad bandwidth is captured alongside.
- **An unrecognised MIDR is a result, and the direction of that result was
  predicted backwards here.** This file used to say that OpenBLAS dispatch
  falls back to generic `ARMV8` for any part not in its switch, and that a
  newer NumPy wheel on `c9g` would therefore be running plain NEON. That
  prediction was not merely unproven, it was inverted. When the implementer/part
  switch in `kernel/arm64/dynamic_arm64.c` falls through it checks SME, then
  `HWCAP_SVE`, and returns `gotoblas_ARMV8SVE` before it ever reaches
  `return NULL`. Generic `ARMV8` is reachable only for a part that is
  unrecognised *and* has no SVE at all. Confirmed empirically: an OpenBLAS
  0.3.30 `DYNAMIC_ARCH` build on Cortex-X925/A725 (parts `0xd85`/`0xd87`,
  neither in the switch, both SVE2-capable) reports
  `openblas_get_corename() -> armv8sve`. So an unrecognised SVE part inherits
  the full SVE kernel set — 94 kernels — while a recognised Graviton 4/5 maps
  `NEOVERSEV2`→`NEOVERSEN2` and gets 5. Being present in the dispatch table is a
  **downgrade** for Neoverse V2/V3, and a NumPy wheel that does *not* recognise
  the chip may be faster than one that does. `capture-env.sh` and `decompose.py`
  still surface an unrecognised part; their wording follows the old prediction
  and is being corrected with it.
- **The alarming dispatch outcome is narrower than the above.** Generic `ARMV8`
  selected on a host that *has* SVE would mean the SVE detection itself failed;
  `NO_SVE` set at build time would mean the SVE kernels were never compiled in.
  Either of those outweighs every kernel question in this repo. Unrecognised
  *with* SVE does not — that is an interesting and possibly good finding, not an
  alarm.

## Practical notes

- **`us-east-1` and `us-east-2` are the only regions carrying all five
  families.** Pin the whole campaign to one of them.
- **In `us-east-1`, `hpc7g.16xlarge` is offered in `us-east-1a` only**, so the
  whole campaign is pinned to that AZ if the hosts are to be comparable in
  placement as well as in region. The four metal types are offered in `1a`–`1d`,
  and `c7g.metal`/`c6g.metal`/`c8g.metal-48xl` additionally in `1f`. From
  `describe-instance-type-offerings`, 2026-08-19.
- **`hpc7g` has no metal size and no spot.** It is the one arm where tenancy
  cannot be eliminated; run it repeatedly and lean on the p50/p90 spread. It is
  also on-demand only, while all four metal types support spot — so it is the one
  host whose cost cannot be reduced and the one whose run cannot be reclaimed.
- Graviton has **no SMT and no turbo**, so 1 vCPU is 1 core and the
  iso-frequency machinery needed on x86 hosts is unnecessary here. Confirmed for
  all five types: `DefaultThreadsPerCore` is 1, at 64 vCPU on
  `c6g.metal`/`c7g.metal`/`hpc7g.16xlarge` and 192 on the two metal-48xl sizes.
  `capture-env.sh` still checks it per host and exits 3 if SMT is on, because an
  API claim about an instance type is not a measurement of the host.
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
- Generic `ARMV8` selected on a host that has SVE, or `NO_SVE` in the build →
  report that first. An unrecognised part landing on `ARMV8SVE` is a different
  thing: read it against the `ARMV8SVE` arm, not as an alarm.
