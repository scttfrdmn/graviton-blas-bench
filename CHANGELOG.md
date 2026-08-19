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
- Records now carry `blas_sha`, `coretype`, `thread_backend` and `pin_policy`.
  `build` was the *gbb repo* SHA and was the only SHA in the record, so the
  identity of the library under test never reached `results/` at all — which made
  every record inadmissible under standing order 5.
- `src/coreprobe.c` and `make coreprobe`: reports what OpenBLAS actually
  selected. `OPENBLAS_CORETYPE` is a *request* — `force_coretype()` silently
  ignores a name it does not know, and a non-`DYNAMIC_ARCH` build ignores the
  variable entirely — so every coretype is now verified before its arm runs and
  the record carries what the library reported, not what was asked for.
- `results/census-<run_id>.ndjson`: one `arm_outcome` record per attempted arm,
  with `status` of `measured` / `build_failed` / `unrunnable` / `runtime_failed`
  / `skipped` and a stated `reason`. Without it the analysis cannot tell "V1 and
  V2 are at parity" from "the V1 arm never ran", and those support opposite
  conclusions. Required by gate P1.
- `results/topology-<run_id>.txt`: `numactl -H` and `lscpu` verbatim, which gate
  P2 requires.
- Incremental S3 shipping via `GBB_S3_URI`, after every arm rather than at the
  end of the sweep, plus on any trapped signal. Instances are terminated on
  completion and a spot reclaim can come sooner; a multi-hour sweep whose results
  only existed locally was spending instance-hours it could lose.
- `tests/run-matrix-stubs.sh`: 33 assertions covering the runner's decision
  logic against stub binaries — the refusal paths, coretype verification, the
  census, and the pinning arithmetic. None of it needs a Graviton, and all of it
  is what would be most expensive to get wrong.
- Instance-availability facts for the pinned region, from
  `describe-instance-type-offerings` and `describe-instance-types` rather than
  from documentation: `hpc7g.16xlarge` is offered in `us-east-1a` **only**, which
  pins the campaign to one AZ; it is also the only one of the five without spot,
  so it is the one host whose cost cannot be reduced. `DefaultThreadsPerCore` is
  1 on all five, at 64 vCPU on `c6g.metal`/`c7g.metal`/`hpc7g.16xlarge` and 192
  on the two `metal-48xl` sizes. `capture-env.sh` still verifies SMT per host: an
  API claim about an instance type is not a measurement of a host.
- `make openblas-omp`: links the `USE_OPENMP=1` OpenBLAS with `-lgomp` and
  *without* `-fopenmp`, so the harness compilation stays byte-identical across
  arms as standing order 6 requires.

### Changed — affects comparability of numbers

- **Pinning is now external and uniform, and this is the single most important
  change in the release.** The runner set `OMP_PROC_BIND=close`/`OMP_PLACES=cores`
  on every arm while OpenBLAS was built `USE_OPENMP=0`. Only OpenMP arms obey
  those, so ArmPL — the reference — was pinned and shipping pthread OpenBLAS was
  not. That is a systematic advantage to the reference of about the size of the
  deficit being investigated. Binding now happens outside the process with
  `numactl`/`taskset`, identically for every arm regardless of threading backend,
  and `OMP_PROC_BIND=false` is set so no arm gets a 1:1 pinning its competitors
  cannot have. What pinning is worth is measured by the new `DYNAMIC_OMP_BOUND`
  arm instead of being left in the comparison as a bias. Pinning was **not**
  equalised by rebuilding OpenBLAS with `USE_OPENMP=1`: that changes the
  threading backend and so what is under test, and pthreads is what the wheels
  ship.
- A uniform `numactl` memory policy also closes a second gap at no cost:
  `bench.c` first-touches its matrices serially and `roofline.c` in parallel, so
  on a multi-node host the denominator and the measurement used to land their
  pages on different nodes. Under one explicit `--membind`/`--interleave` policy
  they cannot.
- **The hardware × target cross is now a runtime `OPENBLAS_CORETYPE` sweep on one
  `DYNAMIC_ARCH` binary, not six separate `TARGET=` builds.** `TARGET=` is not
  only a kernel-table selection — it also sets the compiler flags applied to the
  *common* code (`Makefile.arm64` gives `NEOVERSEN2` `-march=armv8.5-a+sve+sve2+bf16`)
  so a `NEOVERSEV1`-vs-`NEOVERSEV2` comparison across two builds moved the kernel
  table and the codegen of every shared source file at once, with no way to
  attribute the difference afterwards. One binary, one set of common-code flags,
  only the kernel table varying is strictly less confounded. Two static `TARGET=`
  builds survive as controls — the host's native target and the cross target — to
  check that `DYNAMIC_ARCH` dispatch costs nothing measurable and that a forced
  coretype lands where a real `TARGET=` build does.
- **`OPENBLAS_REF` must now be an immutable commit SHA**, defaulting to the
  audited `cc3fc1e`. It defaulted to `develop`. The five hosts are built on
  different days, so a branch name meant `c6g` and `c9g` could silently get
  different libraries while the cross-host comparison that is the entire
  deliverable treated them as one. Override with `GBB_ALLOW_MUTABLE_REF=1`.
  Full SHAs are recorded, not `--short`: an abbreviated SHA does not identify a
  commit outside the repo that produced it.
- **`capture-env.sh`'s exit status now stops the sweep.** It was discarded, so
  the run-invalidating (3) and escalate (4) exit codes stopped nothing and a
  multi-hour sweep would start on a host already known to produce incomparable
  numbers. Exit 3 requires `GBB_FORCE_INVALID_HOST=1`; exit 4 requires
  `GBB_ESCALATION_ACK` with a note, which is recorded — standing order 8 says
  stop and escalate, so proceeding has to leave a trace.
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
- The failed-arm record omitted `instance`, so a failure could not be attributed
  to a host. Arms that were never built or were unrunnable produced no record at
  all and simply vanished from the results.
- A host where the native and cross control targets coincide — any NEON-only
  host, or one whose MIDR is unreadable — built the same `TARGET=` twice,
  installing over itself and emitting two identical manifest lines that the
  census would count as two arms. That inflated apparent coverage on exactly the
  hosts we know least about.
- The campaign's 192-vCPU hosts were named `c8g.48xlarge`/`c9g.48xlarge` in prose
  in README, `run-matrix.sh`, `bootstrap-github.sh` and KICKOFF. Those are real
  but *virtualized* sizes; the campaign runs `c8g.metal-48xl`/`c9g.metal-48xl`,
  and the one place the name appears operationally is a launch instruction.
- The build manifest was written to `$GBB_PREFIX`, outside `results/`, so the
  analysis could not reach it. It is now copied to
  `results/manifest-<run_id>.ndjson`, which is what the P1 expected-arm census
  reads.

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
