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
tools/synth.py         planted-effect datasets whose right answer is known: gate P1
tools/p2-mutate.py     plants one defect in a P2 fixture: gate P2's negative controls
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

- **First call discarded, while it is nearly free.** OpenBLAS allocates its buffer
  pool lazily; ArmPL and BLIS have first-touch costs. Two warmup reps precede a
  timing loop *while they cost under 2% of it* (`WARMUP_MAX_FRACTION`); above that
  they stop, and the record says so (`warmup_reps`). The pool is per **thread**, and
  OpenBLAS runs small problems single-threaded whatever the thread count says, so
  warming only the first case would move threads 2..N's allocation into a timed
  region mid-ladder. An explicit once-per-process `prime_threads()` at n=1024 pays
  that cost instead, outside every measurement, and writes a `thread_prime` record.
- **The calibration call is reused as the first sample** where it is the coldest
  comparable call in the case — unbatched, with no warmup after it (`cal_reused`).
  At the expensive end, where `ABS_MIN_SAMPLES = 3` is the real floor, warmup plus
  calibration were three of a case's seven calls.
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
- **The analysis is calibrated before it is trusted.** On campaign data the right
  answer is the thing being looked for, so there is nowhere to check the analysis
  against — which is what `tools/synth.py` is for. It writes complete result sets
  in which the effect is known by construction: a null, an effect in one regime
  only, an effect at one stride only, a leading-dimension penalty, a dead arm, a
  mislabelled arm, an arm that never ran, an arm that ran only half its sizes, a
  lucky duplicate sample, a flattered pass, two passes that disagree, three passes
  where two agree and the third lost its arms to a crash, three passes where the
  third lost an arm for no recorded reason, all nine
  routines with the effect on the three in the N2 gap, the same effect on those
  three while GEMM contributes four times the rows, an effect confined to one
  transpose, a reference library
  that is absent either entirely or for one routine, two reference libraries
  competing to be the named one, a host whose DYNAMIC_ARCH probe never ran, a host
  with no DYNAMIC build to probe in the first place, and a
  host whose topology fields are `lscpu` defaults rather than measurements.
  `gates/p1.sh` runs `decompose.py` over each and asserts the report says the
  planted thing. The instrument earns nothing on its own — it earns its place by
  having already found defects that would each have survived into published
  numbers, including one that raised a coverage-hole flag on a dataset with no hole
  in it, one that reported a timer-outrun record as a wrong answer, and an arm the
  runner declined to run without recording that it had.
- **The instrument is itself calibrated, by mutation.** A scenario that cannot
  fail is a decoration, so each is checked two ways: with its planted effect
  deleted it must go red, and with the `decompose.py` rule it claims to guard
  broken it must also go red. That is how two rules were found to be guarded by
  nothing — the minimum-within-a-run and median-across-runs aggregation, both of
  which exist so that the luckiest sample is not the one that survives, and neither
  of which any fixture could reach. It is also how the routine set was found to be
  a coverage gap in the instrument rather than in the campaign: every fixture ran
  three routines, so the analysis was certified on `dgemm` and `dgemv` and said
  nothing about `dtrsm`, `dtrmm` and `dsymm` — the operations the conclusion will
  rest on. Planting the effect there is what exposed the majority-of-cells hazard
  below.
- **The fixtures are audited against the producers, not only against the
  analysis.** A fixture that cannot emit a shape the scripts can write leaves that
  branch of the analysis to run for the first time on data that costs money. So
  `build-libs.sh`'s `arm_record` call sites and `run-matrix.sh`'s census statuses
  are read off the scripts and checked against the scenario set; four shapes had no
  fixture, including the OpenMP arm, the arm that measures the pinning delta, and a
  control target built but not runnable on its host. The same audit is why the
  exact wording of `capture-env.sh`'s no-topology warning is now pinned in a
  fixture: `decompose.py` matches it by substring, so a reword would not error — it
  would quietly stop flagging a defaulted field as a default.
- **A gate written before its data exists runs for the first time on the data that
  cost money.** `gates/p2.sh` checks a dataset nobody has collected yet, so it also
  runs `--self-test`: `tools/synth.py`'s `p2-host` scenario builds a clean P2 pass,
  the gate must pass it, and then `tools/p2-mutate.py` plants one defect at a time —
  the `ARMV8` arm gone, that arm present but not at one thread, the floor-overlap
  band gone, the matrix stamp gone, one arm's ladder truncated, `env-*.json` gone,
  `topology-*.txt` gone, a second `instance_id`, `case_seconds` gone, the wrong
  instance type — and the gate must go red on each **and name the field it went red
  about**. The second half is not decoration: three mutants went red with an empty
  message, which is a gate that has stopped saying what is wrong, and one went red
  for the wrong reason. Both fixture and mutator are quarantined by construction
  rather than by care — the gate refuses a `synth-` matrix_id in real mode and
  requires one in self-test mode, and `p2-mutate.py` refuses any directory whose
  stamps are not `synth-`, because every mutation it performs writes a dataset that
  looks measured and is a lie.

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
  surface an unrecognised part as a finding to read against the `ARMV8SVE` arm,
  not as an alarm.
- **An adaptive parity band can absorb the bias it is supposed to expose.**
  `band_for()` widens the parity band to the dispersion actually observed, so a
  delta smaller than the spread that produced it is not published as a finding.
  But `run_spread` is computed from the same per-run values whose median becomes
  the cell value, so anything that biases the value toward one run's number widens
  the band by roughly the amount it moves the delta, and the row still reads
  `parity`. This is not hypothetical: replacing the median across runs with `max`
  is invisible to every verdict-level assertion in `gates/p1.sh`, and was caught
  only by asserting the pooled *number*. The general form — any band derived from
  the same samples as the statistic it bands can hide a bias in that statistic —
  is where the next blind spot of this kind will be, so aggregation rules are
  tested on numbers, and `within_spread`/`run_spread` are printed beside every
  verdict rather than folded into it.
- **A majority-of-cells verdict is decided partly by the size ladder.** The
  campaign verdict counts comparable cells, and the routine set does not
  contribute them evenly — padded and unpadded `dgemm` is 20 cells, most level-3
  routines 12, `dgemv` 8. So an effect confined to TRSM/TRMM/SYMM, which is the
  shape the 94-vs-5 kernel gap predicts, is about a third of the cells, and the
  parity cells clear the 60% majority on their own: the headline read
  `NULL … publish the negative result` over a coherent +22% on every cell of the
  three routines the campaign was built to price. The number of cells a routine
  contributes is a property of `bench.c`'s ladder, not of the hardware, so the
  NULL branch now requires that no coherent subset — by routine, regime or
  instance, in either direction — carries a direction of its own. The guard is
  asserted in both directions, because a rule that could manufacture a localised
  effect out of a genuine null would be worse than the false negative it fixes.
- **The fix by cell count is undone by the next table edit, so the counting is
  now per routine family.** The subset guard above counted rows, which means it
  inherits exactly the property it was written to defend against: whichever
  routine contributes the most rows decides whether anything is coherent. The #2
  matrix expansion makes that acute rather than theoretical — 31 sizes × 4
  transposes × 5 pads is DGEMM growing faster than every other routine, so a
  coherent TRSM/TRMM/SYMM effect would have been *harder* to see after the
  expansion than before the guard existed. `coherent_subsets()` therefore weights
  each row by `1/rows-in-its-family`: every family contributes one unit of weight
  regardless of how long its ladder is, and `dgemm`/`sgemm` are one family, not
  two. The `family-swamped` fixture plants the effect on TRSM/TRMM/SYMM with GEMM
  at parity across four transposes, which is the shape the 94-vs-5 kernel gap
  predicts and the shape raw row counting drowns.
- **The same defect, third appearance, this time on the regime axis — and it
  cuts both ways.** The campaign *verdict* still counted raw cells after
  `coherent_subsets()` stopped doing so. Before the #2 densification the three
  regimes contributed 20 cells each and the count was balanced by accident, so
  nothing showed; after it they contribute 160/110/20. Both failure directions
  then exist: an effect confined to small+medium clears a 60% majority on cell
  count alone and reads as a campaign-level `V1-SET-AHEAD`, while an effect
  confined to the large regime cannot reach 60% however large it is, because
  large is ~6% of the cells — and large is where the DDR generation and the L3
  step show, so that second failure would have silently removed the memory-side
  finding from the campaign's reach. The verdict majority is therefore over
  `(routine_family, regime)`-balanced weight: each group contributes one unit,
  divided among its own cells. Raw counts are still printed beside the balanced
  fraction.
- **A balanced majority is necessary for a directional headline and not
  sufficient.** Balancing stops the ladder voting, but it also means a family
  with 12 cells weighs as much as one with 240 — so an effect on three small
  families clears 60% of balanced weight while moving the dataset's median by
  +0.2%. Publishing `V1-SET-AHEAD, median +0.2%` off that would be the
  max-over-cell defect in its final form: a global claim sourced from a minority
  of the work. A directional verdict now additionally requires the median over
  *all* comparable cells to clear `--min-effect`, signed — a V1 majority whose
  global median runs the other way is not a V1 headline either. Below the floor
  the verdict is `MIXED` and says where the effect is instead. `family-swamped`
  asserts both halves on one dataset: the majority that must be believed, and
  the global median that must not be published as one.
- **A majority threshold can be decided by floating-point summation order.**
  Balanced weight is a sum of reciprocals — a 24-cell group is 24 × (1/24),
  which is not exactly 1.0 in binary. A dataset that lands exactly on the
  threshold by construction (five small-regime families, three of them
  one-sided: 3.0/5.0 = 0.60) then has its verdict decided by the order the
  weights happened to be added, and the two directions of one comparison can
  disagree with each other. `full-routine-set` is that dataset and it went from
  green to red on nothing but a ladder edit. Every majority comparison goes
  through `meets()`, which carries `MAJORITY_EPS = 1e-9` of slack: far below any
  difference the campaign could resolve, far above the accumulated error, and it
  settles the tie in favour of the hand-arithmetic answer, which is the one the
  policy is written in.
- **A wrong answer was blocked from the headline by a coverage threshold, which
  was never the guard — it only happened to be one.** `verify-fail` plants a V1
  arm that returns wrong `dgemm` results; standing order 4 says that poisons the
  record. The exclusion worked, the anomaly printed, and the *verdict* was
  refused only because the excluded cells pushed the non-comparable fraction over
  `--max-nodata-fraction`. The #2 densification took dgemm's total exclusion from
  40% of the cross down to 29%, under the 34% threshold, and the fixture went
  green on `NULL … publish the negative result` off a kernel returning wrong
  answers. `compute_verdict()` now refuses NULL while any routine stands excluded
  for a verification failure, on principle rather than on arithmetic: a wrong
  answer is not a slow answer, the comparison in that routine did not happen, and
  it is precisely where a kernel difference was most likely — a kernel that gets
  the answer wrong is a kernel doing something different. A null is a claim about
  the whole design, and the excluded part is the part that cannot support it.
- **A comparison key that omits an axis lets each target shop along it.** The
  same max-over-cell defect that size shopping produced returns for any axis the
  key does not name, and transposes are the next one: with `transa`/`transb`
  outside the key, NN and TN land in the same cell and each target is compared at
  whichever transpose flatters it. They are not interchangeable in the library
  either — NN goes through `gemm_ncopy_*` and TN through `gemm_tcopy_*`, so a
  packing-kernel difference is *exactly* a transpose-confined difference. The key
  is extended, and `trans` is also a subset axis, because the key alone still
  leaves a false negative: an effect confined to one transpose is confined to no
  routine, no regime and no instance, so it reads as a global null. The
  `transpose-shopping` fixture plants +35% on TN alone; without the subset axis
  the report says `NULL … publish the negative result`, with it `MIXED` and
  `trans TN: V1 set ahead in 20/20 cells`. `canon_trans()` defaults an absent
  field to `N`, so extending the key does not split data recorded before
  `bench.c` emitted one.
- **A reason recorded is not a reason reported.** Standing order 11 says every gap
  in the results carries a reason, and every gap did — in `census-*.ndjson`. The
  analysis read those reasons, used them to classify each absence, and then printed
  only the ones it had classified as holes. So a reader of the report saw
  `build_failed=12` against ArmPL and could not tell from the report whether the
  licence had failed, the download was missing or the link had broken. Absent and
  null being different claims is a property the *artifact* has to have, not just the
  input files, so section 7 now prints the reason beside every explained absence.
  The same gap exists one level down: section 7's listing is pooled across passes,
  so an arm that failed on exactly one pass had its reason recorded and printed
  nowhere, which is why section 8 now lists each pass's own losses.
- **One arm lost on one pass can turn the pooled headline into a false
  `INCONCLUSIVE`.** Sections 1–7 pool by median across `run_id`s and refuse any
  cell whose two sides have unequal N, which is right — a median over three passes
  compared against a median over two is a comparison between different experiments.
  But it means a single crashed arm on one of three passes makes *every* pooled
  cell non-comparable, and the report then says `INCONCLUSIVE` while section 8
  shows two passes agreeing on a 22% effect. Pooled `INCONCLUSIVE` is not parity,
  and the verdict now carries a caveat saying so and pointing at the per-pass
  verdicts. The remaining question — whether to intersect instead of refusing —
  was an aggregation-policy call rather than a bug fix, and it was escalated
  rather than fixed in place. The answer: **intersect per comparison, on three
  conditions.** Equal N is needed *within* a comparison, not globally, so each
  cell uses the passes on which both sides ran. But an intersection down to two
  passes is median-of-2, which is the mean, with breakdown point zero — so a
  2-of-3 cell carries `UNDER-REPLICATED` and is barred from the headline
  (`headline_eligible` is false unless every contributing cell is full, or the
  verdict has no direction to over-claim). Intersecting is licensed by the loss
  being *explained*: a pass whose missing arm has a census reason is intersected,
  a pass whose arm is missing for no stated reason stays `INCONCLUSIVE` — absent
  and unexplained-absent are different claims one level up, exactly as they are in
  section 7. And the per-comparison pass count prints beside every number, so
  `2of3` is never visually equal to `3of3`. The `replicate-majority` and
  `replicate-loss-unexplained` fixtures hold the two branches apart; refusing
  wholesale kills both, intersecting unconditionally kills only the second, and
  dropping the `UNDER-REPLICATED` label kills only the first.
- **An exit bit that fires routinely costs you the bit.** Bit 8 says the
  provenance is incomplete, which is a claim worth reading — but only if it fires
  when the evidence *should* have existed and does not. A missing DYNAMIC_ARCH
  probe on a host where the DYNAMIC build failed, or `sve_kernels: unknown` on an
  arm that has no archive because it never built, are both structurally
  inapplicable rather than incomplete, and raising them would have set bit 8 on
  routine data until nobody read the exit code. Both cases now go to section 7's
  explained-absence machinery as notes. The severity of a bit is worth less than
  its signal-to-noise.
- **Two builds on one host collide silently, and the damage is a build that
  succeeds.** `$GBB_PREFIX` and `$GBB_SRC` are fixed paths, so two concurrent
  `build-libs.sh` runs share one install tree and one source tree. The failure to
  worry about is not the one that errors — it is the run that finishes and writes a
  manifest describing libraries the *other* run installed, which is standing order
  10's mislabelled arm arriving via the builder. Giving each run a PID-suffixed
  path is the wrong remedy, because `run-matrix.sh` finds libraries by name under
  the prefix; the right one is mutual exclusion. Both scripts now take a `mkdir`
  lock on each tree they write, `run-matrix.sh` refuses to sweep against a prefix a
  build is holding, and every refusal names the holder's pid, host and start time
  so a stale lock is diagnosable rather than merely annoying
  (`GBB_FORCE_UNLOCK=1`, `GBB_IGNORE_BUILD_LOCK=1`).
- **The small end of the ladder is not cheap, and the cost model said it was.**
  The spend policy's "densify below 2048 freely" rested on a reps cap bounding
  the small end — and `bench.c` has no reps cap. `MIN_SECONDS` targets a fixed
  amount of *real BLAS work* per measurement, so it buys as many calls as it
  takes: an `n=8` case cost the same wall clock as an `n=1024` one, and the
  "~4× the cases, ~1.6–2× the wall clock" step did not survive contact with that.
  The `~$96/pass` figure it produced is retired rather than rescaled. The floor is
  now per regime — `MIN_SECONDS_SMALL = 0.05` below n=256, still ~500k calls at
  n=8, which is four orders of magnitude clear of the ~31 ns `now()` bracket the
  original 0.30 was assumed to be defending (it was not: `MIN_BATCH_SECONDS` does
  that, and 0.30 entered the file uncommented). Every record carries the
  `min_seconds` it was measured under, so a mixed dataset is detectable rather
  than merely inconsistent. The corollary is that **wall-clock is anti-correlated
  with arm quality**: the cheap/expensive boundary is wherever 6 calls exceed the
  floor, which moves with thread count and with how fast the arm is, so the
  generic `ARMV8` arm at 1 thread will be the single most expensive arm in the
  campaign. Instrument the *slowest* arm of the first P2 iteration, not a
  representative one, or the extrapolation lands low.
- **"Instrument the slowest arm" was an instruction with nothing to read.** The
  cost model's one remaining unknown is the wall-clock multiplier of the expanded
  matrix, and no record carried wall clock: `reps`, `batch` and `calls` describe
  the timing loop, not the case, and the sum over an arm was recoverable only from
  a log timestamp that the S3 shipping path does not preserve. Every record now
  carries **`case_seconds`**, the interval from the previous record's emission to
  this one's, so summing an arm's records reconstructs its sweep wall clock
  exactly — allocation, fill, verification, `TIMED_LOOP` calibration and samples
  all included, because all of them are cost the campaign pays. Three choices in
  it are load-bearing. The clock is read **before** the `printf`, not after the
  `fflush`: `run-matrix.sh` consumes stdout through a pipe, and a value taken
  after the flush charges the consumer's backpressure to the case, which would
  make the cost model track how fast S3 was that day. The accounting starts
  **after** the dry pass and the timer calibration, so the first case is a case
  and not this process's launch. And the field is per *record*, not per *arm*,
  which is what makes it answer the question the spend policy actually asks —
  which sizes cost the multiplier, not just what the total was.
- **Three of a large case's seven calls were overhead, and the cap that was
  supposed to bound them does not.** Verify 1 + warmup 2 + calibration 1 + samples
  3, and at that end it is `ABS_MIN_SAMPLES = 3` that floors the case, not
  `MAX_MEASURE_SECONDS = 3.0` — so the overhead is 43% of the case and the cap
  never sees it. Warmup's stated justification is a lazy buffer-pool allocation
  that happens *once per process*, and it was being paid per case for several
  hundred cases after the pool went warm. It now decays to zero above
  `WARMUP_MAX_FRACTION = 0.02` of the measurement it precedes, which is
  self-scaling and so adds no size threshold to drift out of step with the regimes.
  The naive version of this fix corrupts data and must not be reintroduced: the
  pool is per **thread**, OpenBLAS runs small problems single-threaded regardless
  of `OPENBLAS_NUM_THREADS`, and the first case (n=8) recruits one thread — so
  "warm only the first case" moves threads 2..N's allocation *into* a timed region
  in the middle of the ladder. Hence an explicit `prime_threads()` at `PRIME_N =
  1024` with its own `thread_prime` record, which `gates/p2.sh` requires per
  stream. The calibration call is **reused** as `samples[0]` rather than predicted
  from the previous ladder rung: same saving, no cross-case history dependence
  (prediction breaks wherever a rung is skipped, which the large cap now does), and
  because `_cal` is the coldest call of the case, reuse can only raise `t_min` —
  never flatter an arm. Its three preconditions (`batch == 1`, unit batch size, no
  warmup) are checked per record, because reuse outside them is a flattered reading
  that nothing else in the record looks wrong about. Measured on an Apple M-series
  host against the pre-change binary: 58% of sweep wall clock removed, and on the
  136 cases common to both, GFLOP/s moved −0.05% median against a same-binary
  run-to-run spread of [−5.2%, +1.6%].
- **`n=8192` single-threaded is arithmetic nobody will cite, and it was the most
  expensive arithmetic in the campaign.** The large regime answers bandwidth and
  blocking questions; at 1 thread `n=4096` answers them. The ladder therefore stops
  at `LARGE_CAP_LOW = 4096` below `LARGE_CAP_MIN_THREADS = 8`. What makes that
  principled rather than economising is that the omitted cells carry no hypothesis
  *and* say so: each writes a `case_skipped` record with a reason, because a cell
  absent from every arm at a thread point produces no cell at all and is therefore
  invisible to a census derived from the data — the record is the only thing
  separating a decision from a hole, and `gates/p2.sh` checks `measured + declined
  == matrix_cases` and rejects an empty reason. The dry pass is **not** capped, so
  `matrix_id` describes the design rather than one process's share of it and a
  1-thread stream still pools with a 192-thread one. The cap lives inside `sweep()`,
  which is what keeps it off the level-1 cases: applied on `m` alone it truncates
  `ddot` at `n=4194304`, a 32 MB vector rather than a 512 MB working set, and the
  `incx-axis` fixture caught exactly that by losing its non-unit-stride axis. It
  does touch standing order 1's denominator at low thread counts, so `decompose.py`
  reports which size the peak came from and out of how many — `at n=4096 of 3
  size(s)` — and the policy question is Scott's, asked rather than assumed.
- **The per-regime floor put a change of instrument at n=256, which is where the
  effect is expected to be.** `GEMM_SMALL_*`'s crossover is hypothesised to sit at
  about the same size as the 0.05/0.30 transition, so a step in the section 4
  profile at n=256 is ambiguous between "the fast path ends here" and "the
  measurement window changed here" — and the ambiguity lands on the one section
  whose whole job is locating the effect in the size range. Moving the transition
  to n=512 would separate them by assumption; measuring an overlap band separates
  them by evidence, and costs about 1.75 s per arm. `run_floor_overlap()` therefore
  re-measures `dgemm` at n ∈ {192, 224, 256, 320, 384} at *both* floors, tagging
  those records `probe: floor-overlap` so `split_floor_probe()` partitions them out
  before the cross is built — a probe record is the same condition as a matrix
  record bar the floor, and left in the cross min-within-run would silently keep
  whichever read faster. Section 9 then pairs them and reports `AGREES`,
  `AGREES-WITH-BIAS`, `DISAGREES`, `ORDER-CONFOUNDED` or `INCOMPLETE`; anything but
  the first two sets **exit bit 32** and bars section 4 from being read across
  n=256. Three details are load-bearing. The band must *straddle* the transition,
  or every pair compares a floor with itself and `AGREES` confirms nothing.
  `bench.c` **alternates which floor runs first** by size index, because with a
  fixed order "the first one reads high" and "the short floor reads high" are the
  same dataset — that alternation is the only thing that makes `ORDER-CONFOUNDED`
  reachable rather than a drift reported as a floor bias. And a *signed* bias above
  `--min-effect` is `DISAGREES` even when every pair sits inside its own parity
  band, because `band_for()` widens on a dispersed cell and would otherwise pass a
  consistent bias large enough to move a verdict. `ABSENT` deliberately does not
  set bit 32: every result set produced before the probe existed is `ABSENT`, and
  requiring the probe to be *present* is `gates/p2.sh`'s job.
- **The campaign will put two different case matrices in one bucket, and the way
  it happens is one `aws s3 sync`.** P2 runs pre-expansion and P3 runs after items
  3–5 land, so the two passes sweep different case sets; pooled, cells present in
  one and absent in the other drop out of every intersection silently and what
  survives is whatever the two matrices happen to share — a number that looks like
  every other number in the report. `bench.c` therefore stamps every record with
  `matrix_id`, a digest folded over the same size/pad/incx/routine/floor tables the
  sweep itself walks, in a **dry pass that runs before any measurement**, plus the
  `matrix_cases` count beside it. A digest and not a version number because a
  version number records what someone remembered to bump; the digest moves whether
  or not anyone noticed, and two of the five case-set changes tested during
  development left `matrix_cases` unchanged while moving the id. Four consequences
  worth knowing. The dry pass **ignores the `--routine` filter**, so one arm's
  partial run carries the same id as the full sweep — the id describes the matrix
  the binary sweeps, not what this invocation measured. A routine in a sweep list
  that `sweep()` cannot dispatch is **fatal** (`exit(5)`, censused
  `harness_invalid`), because the dry pass would otherwise fold cases the real pass
  skips and the id would claim measurements never taken. More than one id in a
  results directory is **exit bit 64, returned alone**: `decompose.py` refuses
  before section 0 and computes nothing, because a refusal that still printed a
  cross would hand over the pooled table it exists to prevent. And a record with no
  `matrix_id` is one group, `unstamped`, not a wildcard — an old dataset still
  analyses, but mixing stamped and unstamped records is refused, since whether the
  two swept the same cases is precisely what no record says.
- **A penalty is a property of the stride, so the pad axis has to be attributed
  per pad.** With one extra pad value, "tight versus padded" was one comparison
  and pooling was unobservable. With `LDA_PADS_EXTRA = {1, 4, 8, 64}` a section 3
  that averaged every padded stride against pad 0 would report one penalty per
  size and lose the only thing the four pads were added to see — an arm can hurt
  at pad 8 and be flat at pad 64, and which one it is *is* the packing finding.
  `lda-penalty` therefore plants a penalty at pads 1/4/8 and leaves pad 64 flat on
  the same arm, and asserts both. Relatedly, pad 0 must never appear in an
  extra-pad table: the base sweep already emits every routine at pad 0, so a 0
  there would emit a second record for the same condition in the same run, which
  min-within-run would then silently resolve — a duplicated case masquerading as a
  quietly different sample. `gates/p1.sh` checks both tables on both sides.
- **The alarming dispatch outcome is narrower than the above.** Generic `ARMV8`
  selected on a host that *has* SVE would mean the SVE detection itself failed;
  `NO_SVE` set at build time would mean the SVE kernels were never compiled in.
  Either of those outweighs every kernel question in this repo. Unrecognised
  *with* SVE does not — that is an interesting and possibly good finding, not an
  alarm.

## Practical notes

- **`us-east-1a` is the only availability zone where all five families can be
  placed**, and there is no fallback region. `hpc7g.16xlarge` is offered in
  exactly three regions — `us-east-1`, `eu-west-1`, `ap-northeast-1` — in one AZ
  each, and neither of the other two offers `c9g.metal-48xl` at all; `us-east-2`
  has no `hpc7g` either. Within `us-east-1`, `hpc7g` is `1a` only, the four metal
  types are in `1a`–`1d`, and `c6g.metal`/`c7g.metal`/`c8g.metal-48xl`
  additionally in `1f`. So the campaign is pinned to `us-east-1a` by
  availability, not by preference. From `describe-instance-type-offerings`,
  2026-08-19.
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

`decompose.py` prints ten numbered sections and then a `DECISION` block. The
sections are: 0 hosts and admissibility, 1 deficit by routine, 2 the target
cross, 3 the leading-dimension penalty, 4 the regime profile, 5 anomalies,
6 thread scaling, 7 the coverage census, 8 replicate agreement, 9 the
timing-floor overlap band. Section 9 gates section 4: read it first, because it
is what says whether a step at n=256 is the effect or the instrument.

Section 8 exists because the docs call for repeated `hpc7g` runs, and the
tempting thing to do with a second box is pool it with the first. Pooling turns
the campaign's strongest available evidence — that the headline reproduces on
different silicon of the same type — into slightly tighter error bars on a single
number. So passes are **compared, never pooled**: each `(instance_type,
instance_id)` pair is analysed independently and the resulting verdict codes are
set against each other. If they disagree in direction, that is exit bit 16 and a
`VERDICT-CAVEAT:` line, because a headline that does not reproduce is not a
headline. With three passes, agreement by majority is what the third pass was
bought for, so `REPRODUCES-MAJORITY` is reported when a majority carry one
directional verdict and every dissenter is non-directional — but two passes
disagreeing in *direction* is a divergence at any pass count.

Sections 1–7 do pool, and they pool by **intersection per comparison**: each cell
uses the passes on which both sides ran, prints its own pass count, and is marked
`UNDER-REPLICATED` and barred from the headline if that count is short of the
full set. A pass whose arm is missing without a census reason is not intersected
away — that comparison is `INCONCLUSIVE`. See the hazard note on one lost arm.

The `DECISION` block is **computed, not a guide to be read against**. It emits
exactly one machine-greppable `VERDICT:` line, so `gates/p4.sh` can assert on it
rather than on a human's reading:

| `VERDICT:` | means |
|---|---|
| `V1-SET-AHEAD` | the V1 kernel set wins on `c8g`/`c9g` → closing the N2 gap is justified and needs no new kernel code, only kernel selection |
| `V2-SET-AHEAD` | the N2 mapping was the right call → publish the negative result and drop the SVE angle |
| `NULL` | the two sets are at parity within `--min-effect` → also a negative result, and a reportable one |
| `MIXED` | no `(family, regime)`-balanced majority, **or** a balanced majority whose global median is inside `--min-effect` → the answer is routine-, regime- or transpose-specific, not global, and the `MIXED` line says which |
| `INCONCLUSIVE` | too few cells are comparable to support any of the above |
| `NO-DATA` | the cross never ran; nothing in this dataset answers the question |

`NULL` and `NO-DATA` are deliberately distinct verdicts. "The kernel sets are
equivalent" and "we never measured them against each other" are the two claims
easiest to confuse and they support opposite conclusions.

The majority behind a directional verdict is over `(routine_family, regime)`
**balanced** weight, not raw cells — the number of cells a routine or a regime
contributes is a property of `bench.c`'s ladder, not of the hardware — and a
directional verdict additionally requires the median over all comparable cells to
clear `--min-effect` with the right sign. Both requirements have to hold, and
either one failing gives `MIXED` with the effect *located*. `NULL` also carries a
second bar: it is refused while any routine stands excluded for a failed
verification, because a wrong answer means that routine never compared. See the
hazard notes.

Anything that should qualify the verdict is printed beneath it as a
`VERDICT-CAVEAT:` line — contributing cells that rest on `verified=null`
records, unexplained holes in section 7, hard anomalies in section 5 — and any
consequence the data actually shows is printed as a `CONSEQUENCE:` line, for
instance a real leading-dimension penalty pointing at the packing kernels, or a
deficit concentrated in the small regime pointing at the absent `GEMM_SMALL_*`
path on the N2 target. Consequences are conditional on the finding: the guide
this replaced stated all of them unconditionally, which is exactly why no gate
could assert on any of them.

Two outcomes bypass the verdict entirely and are reported first, via exit
bit 2: generic `ARMV8` selected on a host that *has* SVE, or `NO_SVE` in the
build. Either means the measurement apparatus, not the kernel set, is what the
run discovered. An unrecognised part landing on `ARMV8SVE` is **not** in that
class — read it against the `ARMV8SVE` arm.

One outcome bypasses the report entirely, via exit bit 64: more than one
`matrix_id` in the directory. Nothing is aggregated and no section is printed —
`decompose.py` prints the breakdown by id, with each id's run and instance ids, on
stderr and stops. Separate the directory by `matrix_id` and analyse each on its
own; do not merge the reports, because two matrices are two experiments.
