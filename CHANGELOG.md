# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning 2.0.0](https://semver.org/spec/v2.0.0.html).

Because this repository is a measurement campaign, entries that change how a
number is produced — timing loop, warmup policy, denominator choice, size
regimes, routine set, thread ladder, harness compiler flags — are called out
explicitly, since they determine whether results collected before and after the
change can be compared.

## [Unreleased]

### Added

- Initial harness, scripts and analysis are in tree and heading toward `v0.0.1`.
- `docs/pre-P1-audit.md` — consolidated triage of three adversarial reviews run
  against the harness before any cloud spend.
- Records now carry `batch`, `calls`, `timer_overhead_ns` and `timer_res_ns`, so
  a reader can check the timing contract held rather than assume it.

### Changed — affects comparability of numbers

- **Timing loop is now batched.** Each sample times a batch of back-to-back
  calls and divides, instead of bracketing every call with `now()`. The old
  scheme cost ~31 ns per call pair, 27.9% of the sample at n=8, and a constant
  additive term compresses ratios — biasing the campaign toward "no effect
  found" in the one regime where the missing `GEMM_SMALL_*` path should show.
- **Batch size is calibrated in two stages.** Sizing the batch from a single
  timed call does not work: measured `CLOCK_MONOTONIC` resolution is 1 µs on
  macOS, so an n=8 DGEMM call (58 ns) reads as zero, and the batch was sized
  from a clamped floor. Overshot by 58x, turning a 0.3 s measurement into 17.6 s.
  Coarse clocksources also occur under virtualisation, which is what `hpc7g` is.
- **`MIN_SAMPLES` is 8, was `MIN_REPS` 3.** At 3 samples `p50` and `p90` index
  the same element, so the min/p50 spread README relies on to detect a noisy
  neighbour did not exist for any LARGE level-3 case. Largest cases still land on
  `ABS_MIN_SAMPLES`, so their cost is unchanged.
- **`MIN_SECONDS` is now actually honoured.** The old `MAX_REPS=200` cap meant an
  n=8 measurement ran for ~12 µs against a documented 0.3 s floor. Verified: work
  per measurement is now 0.28–0.30 s from n=8 to n=2048.

### Fixed

- **TRSM/TRMM were timed on `Inf` and on exact zeros.** Both are destructive in
  place on `B` and the timing loop never restored it, so the triangular
  operator's gain compounded once per rep; with a diagonal of `n` the operand
  overflowed (`dtrmm`) or underflowed to zero (`dtrsm`) by around rep 128 at
  n=256. Affected n ≲ 400 — the SMALL and low-MEDIUM regimes. Now a unit
  diagonal with off-diagonals scaled by `1e-9/n`, sized against the batched call
  count rather than the sample count, plus an `operand_finite()` check that
  poisons the record instead of asserting the bound.
- **DGEMM corner tolerance was ~4.5e6x too loose** (`1e-9 * k`); at k=1024 it
  admitted a relative error of 1e-6, so a kernel that had silently dropped to
  FP32 accumulation passed. Now `8 * k * DBL_EPSILON`. Validated against a real
  optimised multithreaded BLAS (Apple Accelerate) at every k from 8 to 8192 with
  zero false positives.
- **Seven of eight drivers claimed verification they never performed**, passing a
  hardcoded `verified=1` — including `dtrsm`, `dtrmm` and `dsymm`, precisely the
  operations in the 90-kernel N2 gap under study. `verified` is now tri-state and
  emits JSON `null` where no check ran.
- A zero `t_min` made `gflops` print as the bare token `inf`, which is invalid
  JSON; `decompose.py` dropped such records with a one-line warning and
  under-counted silently. Now emits a valid record noting the timer was outrun.
- Unchecked `realloc` in the timing loop.

### Corrected

- **The predicted `DYNAMIC_ARCH` fallback was documented backwards.** README and
  KICKOFF claimed an unrecognised MIDR falls back to generic `ARMV8` — plain NEON,
  zero SVE. `dynamic_arm64.c` tests `HWCAP_SVE` before `return NULL`, so an
  unrecognised SVE part gets `ARMV8SVE` and its 94 SVE kernels, while a
  *recognised* Neoverse V2/V3 gets `NEOVERSEV2`→`NEOVERSEN2` and 5. Being in the
  dispatch table is a downgrade, and a wheel that does not recognise the chip may
  be faster than one that does. Confirmed empirically: OpenBLAS 0.3.30
  `DYNAMIC_ARCH` on Cortex-X925/A725 reports `armv8sve`. Standing order 8 and the
  `ARMV8SVE` arm's status are corrected accordingly.

[Unreleased]: https://github.com/scttfrdmn/graviton-blas-bench/commits/main
