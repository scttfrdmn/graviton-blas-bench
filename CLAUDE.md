# CLAUDE.md — standing orders for graviton-blas-bench

This repo is a **measurement campaign**, not a library. Its output is a claim
about hardware, and a claim about hardware is only worth what its provenance is
worth. Read `README.md` §Measurement discipline and §Hazards before touching
anything that produces a number.

The question being answered: **is OpenBLAS kernel work on AWS Graviton worth
doing, and if so where?** A null result is a valid and publishable outcome. Do
not build tooling that assumes OpenBLAS is behind, and do not tune the analysis
until it finds something.

## Project facts

- Repo: `github.com/scttfrdmn/graviton-blas-bench`, public.
- Licence: MIT. `Copyright (c) 2026 Playground Logic LLC`.
- SemVer 2.0.0; Keep a Changelog 1.1.0.
- Binaries and env vars use the `gbb` / `GBB_` initialism.
- AWS: `AWS_PROFILE=aws` (account `942542972736`). **Always pass `--region`
  explicitly** — the profile defaults to `us-west-2`, but the campaign is pinned
  to **`us-east-1`, and within it to `us-east-1a`**. That is not a preference and
  there is no fallback: `hpc7g.16xlarge` exists in only three regions
  (`us-east-1`, `eu-west-1`, `ap-northeast-1`), one AZ each, and neither of the
  other two offers `c9g.metal-48xl` at all. `us-east-1a` is the only AZ on Earth
  where all five families can be placed together. Verified by
  `describe-instance-type-offerings`, 2026-08-19.

## The fourteen standing orders

1. **Measured peak, never theoretical.** The primary denominator is the best
   GFLOP/s any arm achieved on that host. `peak_fma` is a cross-check only; if
   it materially exceeds the best observed GEMM, every arm on that host is
   leaving headroom and *that gap* is the headline. Changing this policy
   requires asking Scott first.
2. **Do not reintroduce the optimizer hazard.** The first draft of
   `src/roofline.c` reported 927 TFLOP/s on one core because the FMA chain was
   folded away. Constants are read from volatile storage and `sanity_check()`
   hard-aborts above `IMPLAUSIBLE_GFLOPS_PER_CORE`. If you touch that file,
   re-verify the number is plausible before trusting anything downstream.
3. **Never claim a number you did not measure.** No filling gaps from vendor
   datasheets, and none from the published ArmPL comparisons. If an arm did not
   run, it did not run — it is a gap in the table, not an estimate.
4. **Correctness before speed.** A failed verification poisons the record. Do
   not relax the tolerance to make records pass; investigate instead.
5. **Every arm carries provenance.** OpenBLAS SHA, compiler version, MIDR,
   HWCAP, governor, NUMA topology, and what `DYNAMIC_ARCH` selected at runtime.
   A number without provenance is not admissible.
6. **The harness itself is identical across arms.** `-O2`, no `-march=native`,
   no `-O3`. Only the BLAS under test varies. This is checked by `gates/p0.sh`.
7. **Report costs before spending.** Estimate instance-hours per phase and put
   it in the umbrella issue before launching. Terminate on completion; nothing
   idles overnight.
8. **Stop and escalate** if `capture-env.sh` reports generic `ARMV8` selected on
   a host that *has* SVE, or `NO_SVE` in the build. That means SVE detection
   itself failed, and it outweighs every kernel question in this repo.
   An unrecognised MIDR is **not** that case, and the earlier version of this
   order had the direction backwards. `dynamic_arm64.c` falls through the
   implementer switch to an `HWCAP_SVE` test *before* `return NULL`, so an
   unrecognised SVE part gets `ARMV8SVE` and its 94 SVE kernels, while a
   recognised Neoverse V2/V3 gets `NEOVERSEV2`→`NEOVERSEN2` and 5. Being in the
   dispatch table is a downgrade. Record an unrecognised MIDR as an interesting
   and possibly *good* finding, and read it against the `ARMV8SVE` arm.
9. **Pin every arm identically, from outside the process.** Never equalise
   threading by rebuilding a library — `USE_OPENMP=1` changes the threading
   backend and therefore what is being measured, and pthread OpenBLAS is what
   the wheels ship. Bind with `taskset`/`numactl` around every arm regardless of
   backend, choose the NUMA policy from thread count and topology, apply it
   uniformly, and record both the policy and the threading backend per arm.
   The original runner set `OMP_PROC_BIND`/`OMP_PLACES` while OpenBLAS was built
   `USE_OPENMP=0`, so the only arm that obeyed them was the reference arm —
   a confound the exact size of the effect under study.
10. **An arm is labelled with what the library reported, never with what was
    requested.** `OPENBLAS_CORETYPE` is a request: `force_coretype()` silently
    ignores a name it does not know, and a non-`DYNAMIC_ARCH` build ignores the
    variable entirely. Verify with `gbb-coreprobe` before the arm runs and record
    `openblas_get_corename()`'s answer. If the request was not honoured, do not
    run the arm — a mislabelled arm is not a failed run, it is a plausible wrong
    answer, which is worse. Same rule for `TARGET=`, the MIDR, and the thread
    count: record the observed value, not the intended one.
11. **A gap in the results carries a reason.** Every arm the runner declines to
    run writes a census record saying why. Absent and null are different claims,
    and the analysis must be able to tell them apart: "V1 and V2 are at parity"
    and "the V1 arm never ran" support opposite conclusions.
12. **Ship results as they are produced.** Per-arm to S3, not at end of sweep.
    Instances terminate on completion and an instance can die before that —
    hardware fault, AZ event, a mistyped `terminate-instances`. On-demand removes
    reclaim, not loss. Results that exist only on the instance are instance-hours
    spent for nothing.
13. **Session-end status comment** on the active umbrella issue: what ran, what
    the gate said, what is blocked, what is needed from Scott.
14. **Long jobs run in the background.** `build-libs.sh` is ~40 minutes and the
    sweeps are longer. Launch them backgrounded and pick them up on completion;
    never block on `sleep` or a fixed timeout.

## Spend policy

Decided 2026-08-19, after costing the design as built, and revised twice the same
day: once when the matrix expansion on #2 was costed, and again when the P3 total
derived from that costing was withdrawn.

**There is no per-pass or whole-campaign figure in this file, and that is
deliberate.** Three have been struck, each for the same reason rather than for
three different ones:

- **~$96/pass** described the pre-expansion routine table.
- **30–37 instance-hours per pass** was `18.6 h × ~1.8`, and the 18.6 h was built
  on the `MAX_REPS`/156-measurements model that the timing audit (`6a8089f`)
  deleted. Scaling a dead number does not revive it.
- **$500–650 for three passes** was derived from the 30–37 h, so it went with it.

Do not reconcile a struck figure with a live one, and do not carry one forward
because it is the only number available: a stale cost estimate in the file that
authorises spend is followed later by someone who was not in this conversation.
The reason none of the three survives is that `MIN_SECONDS` targets a fixed amount
of *work* per measurement, so it buys as many calls as it takes; there is no
`MAX_REPS` capping the small end and no `MIN_REPS` flooring the large end.
"Densify below 2048 freely" rested entirely on that cap. The per-regime floor
(`MIN_SECONDS_SMALL = 0.05` below n=256), the conditional warmup and the
calibration reuse together take most of the small-end and large-end waste back,
but none of them restores the premise — the extra small and medium cases still add
close to linearly, and the multiplier is unknown until it is measured.

**The live basis is arithmetic over the current constants, and it covers one host
only.** The authorised P2 launch — one on-demand `c8g.metal-48xl`, 96 streams
across eight thread points — is **8–14 instance-hours, $61–107** at $7.65696/hr.
That is not a per-pass figure and must not be multiplied into one: P3 adds four
more hosts whose per-case cost this run has not measured. The whole point of the
run is to replace the arithmetic with a measurement.

Measure it on the **slowest** arm of the first P2 iteration, not a representative
one — see §Wall-clock is anti-correlated with arm quality for why a representative
arm extrapolates low.

- **Everything runs on-demand, including P2.** Spot was the earlier decision and is
  reversed: ~$100 of saving is not worth putting untested reclaim handling on the
  critical path, and a reclaim mid-sweep means arms *within one host's dataset* were
  measured on different physical hardware. P2 still runs on `c8g.metal-48xl`, and
  not on the cheap host, on purpose: `c8g` is where the central cross lives, and
  debugging P2 anywhere else would leave the most important analysis path untested
  until P3.
- **Launch with truffle/spawn, not with new in-tree tooling.** Those already drive
  AWS instances for the keel host pool, so the lifecycle path — launch, tag, wait,
  terminate — is already exercised. Writing `scripts/launch.sh` for this campaign
  would put an unexercised lifecycle between the spend and the data, and the three
  P3 passes are exactly where a hand-driven or newly-written procedure drifts
  between launches. A drift between passes is indistinguishable from the effect the
  passes exist to test.
- **P3 runs three times.** Per-arm S3 shipping makes a lost pass's data survivable,
  not comparable. `us-east-1a` single-AZ bare metal on a new instance type is the
  thinnest capacity pool the campaign touches — on-demand does not change that, it
  only changes the failure from a mid-sweep reclaim to a launch that does not get
  the instance — and `c9g.metal-48xl` capacity is already the gating risk.
- **Three, because two passes have no breakdown point.** The median of two *is*
  the mean: one bad pass moves it, and there is no way to tell which pass was
  bad. Three passes reject one bad pass by majority. The campaign's whole output
  is a number that will be argued with, so the step from no protection to
  protection against one bad pass is not incremental.
- **The passes must be independent, which means three separate launches.** Three
  sweeps back-to-back on one launched instance are correlated samples — same
  physical box, same DRAM, same neighbours, same thermal history — and a median
  across them protects against nothing structural. Launch each pass separately,
  days apart, on a fresh instance, and terminate between passes. A loop inside
  one instance's lifetime is a repeat measurement, not a replicate, and must not
  be counted as one.
- **Keep the pass count uniform across all five hosts.** If budget pressure
  forces a cut, the third pass matters most on `c8g`/`c9g` (where the central
  cross lives) and least on `c6g` — but cut only under force: mixed pass counts
  mean the pooling rule has to handle them, and that is more code in the place
  you least want it.
- **A replicate needs no new field and must not be pooled.** Passes are
  identifiable as the same `instance_type` with different `instance_id`, both
  already recorded by `capture-env.sh`. This fails safe: a re-run on the *same*
  box carries the same `instance_id` and is correctly not counted as a replicate.
  `decompose.py` must compare passes rather than median across them — pooling
  would silently convert the campaign's strongest evidence into slightly tighter
  error bars. With three passes, agreement by majority is what the third pass was
  bought for, so section 8 reports `REPRODUCES-MAJORITY` when a majority carry
  one directional verdict and every dissenter is non-directional. Two passes
  disagreeing in *direction* is still a divergence at any pass count: no majority
  makes a contradiction publishable.
- **A pass that loses one arm is intersected, not discarded — and says so.**
  Sections 1–7 pool by median across passes, and equal N is required *within* a
  comparison, not globally: each comparison is restricted to the passes carrying
  both of its arms. Three conditions, and they are the policy, not an
  implementation detail. (a) A 2-of-3 intersection is back at median-of-2 = mean,
  so it carries `UNDER-REPLICATED` and cannot carry the headline. (b) Intersect
  only where the loss is explained by a census reason; an unexplained loss stays
  `INCONCLUSIVE`, because the missing records could have said anything and there
  is no record of them having said nothing. (c) Every number prints its own pass
  count (`passes=2of3`), so a partial comparison is never visually equal to a
  complete one.
- Budget: **not stated, on purpose** — see above for the three struck figures. The
  P3 number comes from the P2 pass's measured per-case cost on its slowest arm,
  extrapolated across the other four hosts, and it goes in the umbrella issue
  before P3 launches (standing order 7). Ask Scott with the arithmetic shown; do
  not carry a figure from this file. `hpc7g` is on-demand-only and has no metal
  size, so its tenancy stays inside the measurement; it gets the repeat runs and
  the p50/p90 spread is read accordingly.

## The matrix expansion — approved 2026-08-19, tracked on #2

Denser size ladders, `transa`/`transb`, complex types, more `lda_pad` values, and
BF16. Approved by Scott as a scope change, and it lands in this order because every
later item degrades the verdict without the first:

1. `coherent_subsets()` normalised per routine family, `transa`/`transb` in the
   comparison key, and the pass-intersection rule — **with fixtures**. Nothing else
   lands first. The expansion is not neutral with respect to the C11 false negative,
   it makes it worse: every addition multiplies GEMM's cell count faster than
   anything else's, so on raw counts an effect confined to TRSM/TRMM/SYMM would be
   *harder* to see after the expansion than before C11 was fixed.
2. Size ladders and `lda_pad`.  3. Transposes.  4. Complex types.
5. BF16, as its own commit: OpenBLAS-vs-OpenBLAS only, weak-linked `sbgemm_`,
   HWCAP-gated, its absence explained rather than bit 8, its own report section.

Item 1 landed with the family normalisation, the key extension, the transpose axis
in the coherence guard, and the intersection rule; `transpose-shopping`,
`family-swamped`, `replicate-majority` and `replicate-loss-unexplained` are the
fixtures, each mutation-validated.

### The axis assignment — decided 2026-08-19, do not widen it

The cross is **pads × {NN, TN}**, not the full cross and not NN-only. Transposes
and pads probe the same hypothesis — packing quality — by different mechanisms, so
`pad=64 × TT` teaches nothing beyond `pad=64 × NN` plus `pad=0 × TT`. But NN-only
is a notch too thin: TN is the transposed-A path through `gemm_tcopy_*` rather than
`gemm_ncopy_*`, and alignment interacts differently with the two. Two
pad-carrying transposes is the smallest set covering both packing routines.

| axis | carried by | not carried by |
|---|---|---|
| transposes | `dgemm`, `sgemm` — NN/TN/NT/TT small+medium, NN/TN large | TRSM/TRMM, which already carry side/uplo/trans/diag; opening that space is a separate question the campaign does not need |
| `lda_pad` | `dgemm` at {NN, TN}; `dtrsm`, `dsymm` at their default setting | `sgemm`, `dsyrk`, `dtrmm`, `dgemv`, complex, BF16 |
| conjugate-transpose | `zgemm`/`cgemm` at **NN and CN** | everything real |
| neither | BF16 — it is a kernel-selection question | |

`zgemm` at NN only would be the same error as `dgemm` at NN only, one level down:
conjugate-transpose is a distinct code path and it is what most complex research
code actually issues.

**The "1005 cases per arm against 156 today (6.4×)" figure is struck**, and no
replacement projection is written here. It was computed against the 156-case table
that predates item 1 and item 2; the ladders and pads have since landed, so the
baseline it was a multiple *of* no longer exists, and its own breakdown (544 small)
now collides numerically with today's total. **Read the count off the producer, not
off this file**: every `gbb-bench` invocation folds the full design in its dry pass
and prints `gbb: matrix_id=… over N cases` to stderr before the first record —
**544 today** (288 small, 190 medium, 50 large, 16 level-1). It is the same number
the records carry as `matrix_cases`, so it cannot drift from what was measured, and
the fold is deliberately not subject to the thread-dependent large cap.

### Wall-clock is anti-correlated with arm quality

The cheap/expensive boundary is not a size. It is wherever ~6 calls exceed the
`MIN_SECONDS` floor — `t_call ≈ 0.1 s` — and that size moves with thread count and
with how fast the arm is: single-threaded around n≈800 for DGEMM, at 192 threads
closer to n≈2000. So the **slowest** arm is the most expensive one, and the generic
`ARMV8` arm at one thread on `c9g` is likely the single most expensive arm in the
campaign — and it is one the campaign specifically wants. Instrument per-case
wall-clock on the slowest arm of the first P2 iteration, never on a representative
one, or the extrapolation to P3 lands low.

### `MIN_SECONDS` is per regime, and 0.30 was never argued for

Checked before changing it, per Scott. **Nothing defends `MIN_SECONDS = 0.30`.** It
arrived unargued in the scaffold (`11677e2`, a bare `#define` with no comment) and
the timing audit (`6a8089f`) did not choose it — the audit's own message lists it
as a *contract that was documented but not met*: "an n=8 measurement ran ~12 us
against a documented 0.3 s floor, because `MAX_REPS=200` capped it." The audit made
the declared floor true; it never asked whether the declared floor was right.

The 31 ns `now()` bracketing is defended by **`MIN_BATCH_SECONDS = 1e-3`**, which
is independent of `MIN_SECONDS` and unchanged: the batch is still ~1 ms, so the
bracket is still 0.003% of a sample. Four other checks all clear:

- `MIN_SAMPLES = 8` still binds nothing — 0.05 s of 1 ms batches is ~51 samples.
- No turbo and no SMT on Graviton, so there is no frequency ramp needing a long
  window. (On x86 there would be, and this argument would not hold.)
- The destructive-operand bound on TRSM/TRMM gets *tighter*, not looser: fewer
  calls per measurement is the safe direction for `TRI_OFFDIAG`'s monotone bound.
- Sample count already varies 100× across the ladder (3 at `n=8192`, ~301 at
  `n=8`), so a per-regime floor does not introduce a non-uniformity that was not
  there. What must stay uniform is the *comparison*, and it does: the same case on
  two arms is batched to the same ~1 ms sample, so both arms get the same count.

So the small regime takes **0.05 s** and medium/large keep 0.30 s. That is ~136 s
back per (arm × thread point) at 544 small cases, and it stops spending three
million calls at `n=8` measuring harness dispatch.

### The expensive end: three of seven calls were overhead

Decided 2026-08-19, and it is the change P2 was held for — measuring the cost basis
against a configuration known to be wasteful would mean measuring it twice. A large
case used to cost seven calls: verify 1 + warmup 2 + calibration 1 + samples 3. The
floor at that end is `ABS_MIN_SAMPLES = 3`, not `MAX_MEASURE_SECONDS`, so **the cap
does not cap** and the three overhead calls are 43% of the case.

- **Warmup is per-process, not per-case, and now decays to zero.** Its stated
  justification is OpenBLAS's lazy buffer-pool allocation, which happens once.
  `WARMUP_MAX_FRACTION = 0.02` runs warmup only while it costs under 2% of the
  measurement it precedes — self-scaling, so it adds no size threshold to keep in
  step with the regimes.
- **"Warm only the first case" would corrupt data, and must not be reintroduced.**
  The buffer pool is per *thread*, and OpenBLAS runs small problems
  single-threaded whatever `OPENBLAS_NUM_THREADS` says. The first case (n=8)
  recruits one thread; threads 2..N would then allocate mid-ladder, inside a timed
  region. That is why there is an explicit once-per-process `prime_threads()` at
  `PRIME_N = 1024`, emitting a `thread_prime` record, and why `gates/p2.sh`
  requires one per stream.
- **Calibration is reused, not predicted.** Scott's suggestion was to predict the
  next rung's call time from the previous rung's rate. Implemented as reuse
  instead — `_cal` becomes `samples[0]` when `batch == 1 && batch_size == 1 &&
  warmup_reps == 0` — for the same saving without a cross-case history dependence
  (prediction makes case N's configuration depend on case N−1 and breaks wherever
  a rung is skipped, which the large cap now does), and because `_cal` is the
  *coldest* call of the case: reuse can only raise `t_min`/p50/p90, never flatter
  an arm. All three conditions are load-bearing; the gate checks them per record.
- **Measured effect** (Apple M-series, Accelerate, dgemm ladder ×2 runs): 58% of
  sweep wall clock removed, of which the cap is most; on the 136 cases common to
  both binaries, 7.9% saved and the GFLOP/s median moved −0.05% overall and −0.12%
  on the reuse path, against a same-binary run-to-run spread of [−5.2%, +1.6%] on
  those same cases. Inside the noise, and in the conservative direction.

### The large ladder is thread-dependent

An `n=8192` single-threaded DGEMM is not a number anyone will cite: the large
regime answers bandwidth and blocking questions, and at 1 thread `n=4096` answers
them. `LARGE_CAP_LOW = 4096` below `LARGE_CAP_MIN_THREADS = 8`. Three properties
make this principled rather than economising, and all three are asserted:

- Every omission emits a `case_skipped` record carrying a reason — standing order 11
  at case granularity. A cell absent from *every* arm at a thread point produces no
  cell at all, so a data-derived census cannot see it; the record is the only thing
  separating policy from a hole. `gates/p2.sh` checks `measured + declined ==
  matrix_cases` and rejects an empty reason.
- The **dry pass is not capped**, so `matrix_id` is unchanged (`7c371fee324b7304`
  over 544 cases, verified before and after). A 1-thread stream and a 192-thread
  stream carry the same stamp and stay poolable.
- The cap lives inside `sweep()`, so it cannot reach the level-1 cases, which are
  built directly in `main()`. Applying it on `m` alone would truncate `ddot` at
  `n=4194304` — a 32 MB vector, not a 512 MB working set — and did, until the
  `incx-axis` fixture lost its non-unit-stride axis and said so.

**This touches standing order 1's denominator input set at low thread counts**, and
that is Scott's call, not the harness's: the measured peak at 1 thread is now taken
over a 3-rung large ladder rather than 5. DGEMM is flat-to-declining above n=2048 so
the max is not expected to move, but "not expected to" is not the standard this repo
holds denominators to. `decompose.py` therefore reports **which** size the max came
from and out of how many (`best_large_dgemm=… at n=4096 of 3 size(s)`), and a max
sitting at the top of a truncated ladder is the case to read twice.

## Ask before

- Launching **any** EC2 instance.
- Changing the size regimes or the routine set (the #2 expansion above is already
  approved; anything beyond it is not).
- Altering the denominator policy in standing order 1. **Open, awaiting Scott:**
  whether the thread-dependent large cap's effect on the 1-thread denominator is
  acceptable, or whether `n=6144`/`8192` should run at low thread counts purely to
  keep the peak's input set uniform across thread points.
- Relaxing a verification tolerance.

## Phase gates

Do not start a phase until the previous gate is green. Each gate is a script
under `gates/` that exits 0/1 and prints its evidence.

| gate | requires |
|---|---|
| `gates/p0.sh` | CI green on a clean clone; `make roofline` builds; `bash -n` clean |
| `gates/p1.sh` | expected-arm census present and read from `manifest-*.ndjson` + `census-*.ndjson`; every planted effect recovered; the planted **null** reported as a null, not a weak hit, and distinguishable from a missing arm |
| `gates/p2.sh` | complete NDJSON set from one **on-demand** `c8g.metal-48xl` host (spot was reversed; the gate itself asserts nothing about tenancy, and that is correct — tenancy is a spend decision, not an admissibility one); `decompose.py` clean bar genuine findings; `topology-*.txt` recorded; every arm in `census-*.ndjson` either `measured` or carrying a stated reason — zero `MISSING-UNEXPLAINED` |
| `gates/p3.sh` | five hosts collected **three times**, each pass on a different `instance_id` from a separately launched instance; every `env-*.json` present; `blas_sha` identical across hosts and passes; the headline reproduces across passes (`REPRODUCES` or `REPRODUCES-MAJORITY`, never `DIVERGES-*`); no unresolved section-5 anomalies |
| `gates/p4.sh` | report answers "is the N2 gap worth closing", supported by section 2, stated as a null if that is what the data says |

P1 is implemented by `tools/synth.py`, which writes complete result sets whose
answer is known by construction, and `gates/p1.sh`, which runs `decompose.py`
over each and checks the report. Two rules about it:

- **Each scenario owns its expectations; the gate owns none.** Adding a scenario
  must not require editing `gates/p1.sh`. An unknown expectation kind FAILs rather
  than being skipped.
- **The fixture must stay faithful to the producers.** synth.py hand-copies
  `bench.c`'s size ladders and the census/manifest vocabulary from
  `run-matrix.sh`, because there is nothing to import from a shell script or a C
  file. A copy that drifts turns every scenario into a rigorous test of the wrong
  experiment, and it does so silently — so the ladders are asserted against
  `bench.c` in gate section 2. If you change a ladder, a record field, or a census
  status, change synth.py in the same commit. Fixtures go to a scratch dir, never
  to `results/`: they are not measurements.

## Working conventions

- Project management lives in GitHub: milestones P0–P4, one umbrella issue per
  phase, labels, and a Projects board created by `scripts/bootstrap-github.sh`.
- Conventional Commits. Update `CHANGELOG.md` under `## [Unreleased]`.
- Delegate separable work to concurrent subagents — read-only audits, per-host
  analysis, boilerplate. Keep measurement-policy judgment in the main loop.
- **`castor.local`/`pollux.local` are instrument checks, never data.** They are
  NVIDIA DGX Spark (GB10) — Cortex-X925 + Cortex-A725, heterogeneous,
  SVE2 at VL=128. Real SVE2 silicon and free, so they exercise the whole
  pipeline including the S3 path, but they are not Neoverse and not Graviton.
  Quarantine them **by construction, not by discipline**: a distinct `run_id`
  namespace and a separate output directory, so nothing from them can reach the
  published dataset even by accident.
- `results/` and `bin/` are gitignored. Collected campaign results are published
  as release artifacts, not committed.
