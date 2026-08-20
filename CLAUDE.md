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
   GFLOP/s any arm achieved on that host, **over the large-dgemm sizes that host
   ran at every thread count** — the intersection, not each thread point's own
   best rung. Decided with Scott 2026-08-19, once the thread-dependent large cap
   made the two differ: a per-rung max draws the 1-thread ceiling from a 3-rung
   ladder and the 192-thread ceiling from a 5-rung one, so the two efficiency
   columns in section 6 would be ratios against different quantities with nothing
   in the arithmetic to stop a reader comparing them. Annotating that was
   considered and rejected — it documents the inconsistency without removing it.
   It costs a little headroom wherever the unrestricted max sat above the common
   set; `decompose.py` prints that cost per row and never uses it. A thread point
   with **no** large dgemm drops out of the intersection rather than emptying it,
   and carries no denominator of its own. **The empirical ceiling stands alone, with
   no independent floor.** `peak_fma` was that floor and is **retired as a
   cross-check** (Scott, 2026-08-20). It was justified on the one case the measured
   peak cannot see by construction — every arm on a host being bad, which moves the
   ceiling down with the arms and leaves the efficiency columns looking fine — and it
   cannot detect that case. `roofline.c` declares `peak_fma` a *lower* bound on
   purpose, because whether its accumulator array vectorises into NEON or SVE is the
   compiler's decision and standing order 6 forbids `-march=native`; measured on
   `c8g.metal-48xl` at t=1 it is **4.22 GFLOP/s against a best large DGEMM of 18.16**,
   4.3× under the quantity it was bounding, so the flag could not fire at any
   threshold. It was not passing, it was absent while reading as protection. Building
   `roofline.c` alone at `-O3 -march=native` so the accumulators actually vectorise
   would make it discriminating and was **rejected**: it breaks standing order 6 and
   makes the campaign's only independent floor a function of gcc's vectoriser. Better
   no floor than that floor — and the retirement is stated in the report rather than
   left implicit, which is the whole point of taking this option. `peak_fma` is still
   measured and still printed in section 6 **as provenance, labelled not a
   cross-check**; the report raises no anomaly on it in either direction, and
   `peak-fma-retired` is the fixture that holds that silence. What is *not* retired is
   `IMPLAUSIBLE_GFLOPS_PER_CORE` and `sanity_check()`'s hard abort — see standing
   order 2; those guard the optimizer folding the chain away, which is worth guarding
   even when the number has no analytic use. Do not reintroduce a threshold on
   `peak_fma / best GEMM` without first making `peak_fma` a bound tight enough to be
   exceeded. Changing any of this requires asking Scott first.
2. **Do not reintroduce the optimizer hazard.** The first draft of
   `src/roofline.c` reported 927 TFLOP/s on one core because the FMA chain was
   folded away. Constants are read from volatile storage and `sanity_check()`
   hard-aborts above `IMPLAUSIBLE_GFLOPS_PER_CORE`. If you touch that file,
   re-verify the number is plausible before trusting anything downstream. **This
   survives `peak_fma`'s retirement as a cross-check** (standing order 1) and must
   not be removed alongside it: the abort guards against the chain being folded
   away, which is a different question from whether the resulting number bounds
   anything.
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
    **Known blind spot, recorded 2026-08-20 rather than discovered later:** the
    read-back cannot distinguish `NEOVERSEN2` from `NEOVERSEV2`. Both requests
    report `neoversev2`, because `gotoblas_corename()` tests `corename[12]` before
    `corename[13]` on what `dynamic_arm64.c` `#define`s to one pointer. It is blind
    *harmlessly*, and only for one reason: at `cc3fc1e`,
    `kernel/arm64/KERNEL.NEOVERSEV2` is a single 39-byte line,
    `include $(KERNELDIR)/KERNEL.NEOVERSEN2`, so there is exactly one kernel table
    and nothing for the read-back to fail to tell apart. That is why the `NEOVERSEN2`
    arm is `alias_duplicate` rather than a hole — the kernel set *was* measured, by
    the arm labelled `NEOVERSEV2`. **The harmlessness is a property of the pinned
    SHA, not of the mechanism.** If a future OpenBLAS gives V2 a table of its own or
    reorders `corename[]`, this pair becomes the one place a request can silently
    land on a different table than the label claims, and `alias_ok()`'s declaration
    turns from correct into exactly the plausible wrong answer this order exists to
    prevent. Re-derive it against the SHA under test; do not carry this paragraph
    forward as if it were about the mechanism.
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

**And sum it per rung, never per stream — but the per-rung costs are two terms, and
only one of them needs measuring per host.** A stream is not a unit of cost. The cost
model is `flat + large(t)`, and it is not an approximation of the measurements, it is
what they decompose into. Per stream, on `c8g.metal-48xl`:

| t | small | medium | level-1 | **flat** | large | rungs | stream |
|---|---|---|---|---|---|---|---|
| 1 | 0.24 | 2.30 | 0.08 | 2.62 | 4.60 | 3 | **7.22** |
| 8 | 0.25 | 1.02 | 0.08 | **1.35** | 5.20 | 5 | **6.55** |
| 16 | 0.25 | 0.98 | 0.08 | 1.31 | 2.87 | 5 | 4.18 |
| 32 | 0.25 | 0.96 | 0.08 | 1.29 | 1.72 | 5 | 3.00 |
| 64 | 0.25 | 0.94 | 0.07 | 1.27 | 1.22 | 5 | 2.48 |
| 96 | 0.25 | 0.91 | 0.07 | 1.23 | 1.04 | 5 | 2.27 |

**Read the `rungs` column before reading the `large` column.** t=8 costs more in the
large regime than t=1 (5.20 against 4.60) and that is **not a threading effect — it is
different work.** `LARGE_CAP_MIN_THREADS = 8` lifts the truncation, so t=8 measures
`n=6144` and `n=8192` and t=1 does not. Anyone who reads 5.20 against 4.60 without the
rung count will reach for a threading explanation and find none. The rungs are part of
the measurement; the two columns are not comparable without it.

The **flat** term is `MIN_SECONDS`-floored, so it is flat in thread count by
construction and measured so: **1.35 → 1.23 min/stream from t=8 to t=96, −9% across a
12× thread range.** Medium is ~75% of it, not small. It is flat only from t=8 up — at
t=1 medium has not yet flattened (2.30) and the flat term is double. So `flat × (arms ×
rungs)` is derivable from first principles for a host that has not been launched, and
only the **large** term, which is `ABS_MIN_SAMPLES = 3`-bound and scales with per-thread
call time, has to be measured there. On the 5-rung hosts (`c6g`, `hpc7g`) that puts most
of a pass's cost inside the derivable term. **One thing to check before relying on that
on a slower host:** `flat` is host-independent only where the `MIN_SECONDS` floor
actually binds, which is why it is flat above t=8 and not at t=1 — at t=1 the medium
cases still exceed the floor on their own. `c6g` is Graviton2 and several times slower
per core, so confirm medium is floored there before treating `flat` as derived rather
than measured. Where the floor binds, the term is a property of `MIN_SECONDS` and the
case count, not of the silicon.

So
`mean_stream_cost × stream_count` priced at the head of the thread ladder runs high —
it did, by ~1.4–1.8× on the first P2 pass, whose pre-launch band priced all eight
thread points at about the `t=8` cost and counted the roofline streams, which cost
seconds, as streams. The estimate is `Σ over thread points (per-rung stream cost
× streams at that rung)`, and the per-rung costs are measurements, one per rung.
The counter-intuitive term is the one that stops the total collapsing: **t=8 is only
9% cheaper than t=1, not half**, because the thread-dependent large cap lifts at
`LARGE_CAP_MIN_THREADS = 8` and the large ladder gains two rungs — large costs *more*
at t=8 (5.20 min) than at t=1 (4.60) — which is why the correction is ~1.4–1.8× and not
the 3–4× that "later rungs are cheaper" on its own implies. Report progress as
elapsed-time fraction, not as streams done: at 17 of 88 streams this pass was 19% by
stream count and 24–34% by time, an understatement, because what is left is the cheap
end of the ladder. Per-rung costs are host-dependent (`c6g`/`hpc7g` have no 192-thread
rung at all), so P3 re-derives them per host and does not scale c8g's.

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

### A comparison's reference must be invariant to the axis compared along

Section 1's reference arm is chosen **once per host** and named in the payload as
`reference_scope`. Not per comparison group, which is what it was: the group key
carries the regime, so on a host with two reference candidates whose coverage differs
by regime the choice could flip *inside* one comparison — a count-derived selection
moving a count-derived consequence, and the consequence is section 9's "deficit
concentrated in the small regime", which is the sentence that says which kernels to
fix. Section 4a keys on `reference_arm` (it must; rows measured against different
references are not one profile), so a flip splits the profile, nulls
`small_minus_large`, and presents as `MISSING: regimes` rather than as an error. Per
host is the only scope invariant to all four axes the report compares along — regime,
routine, thread count, and pad/transposes. Where the chosen reference produced
nothing, the row is an explicit NO DATA **naming it**, which is standing order 11's
answer: an absence with a reason beats a silent substitution. The tie-break is
coverage breadth, then conditions, then `arm_label` — deterministic, so it cannot
reorder between passes. `reference-regime-flip` is the fixture, mutation-validated
against the per-group selector, and it plants the coverage so that the
conditions-winner is *not* the alphabet-winner; otherwise it would pass against a
selector that read only the label.

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

### Wall-clock is anti-correlated with arm quality — but weakly, and measured now

The cheap/expensive boundary is not a size. It is wherever ~6 calls exceed the
`MIN_SECONDS` floor — `t_call ≈ 0.1 s` — and that size moves with thread count and
with how fast the arm is: single-threaded around n≈800 for DGEMM, at 192 threads
closer to n≈2000. So the **slowest** arm is the most expensive one, and the
extrapolation to P3 must be instrumented on the slowest arm of the first P2
iteration, never on a representative one, or it lands low. That much holds.

**The rest of what this section used to say was a prediction, and the first P2 pass
contradicted it.** It named the generic `ARMV8` arm at one thread on `c9g` as
"likely the single most expensive arm in the campaign". Measured on
`c8g.metal-48xl` at t=1 (run `20260820T031023Z-ip-172-31-36-19`, `case_seconds`
summed per stream), the six DYNAMIC coretype arms come out:

| arm at t=1 | minutes | | arm at t=1 | minutes |
|---|---|---|---|---|
| `ARMV8SVE` | **7.59** | | `ARMV8` | 7.09 |
| `NEOVERSEV1` | 7.44 | | `NEOVERSEV2` | 7.09 |
| `unforced` | 7.09 | | `NEOVERSEN1` | 7.00 |

`ARMV8SVE` is the slowest, generic `ARMV8` is tied for third, and the whole spread
is **8.4%**. Scott's reading of that, and it is the right one: at fixed thread count
wall clock is essentially **flat** across arms, which is the *opposite* of the model
the P3 extrapolation was built on — so the falsification is more useful than the
prediction was. The residual instruction survives for a different reason than it was
written for: instrument the slowest arm because you cannot tell in advance which arm
that is, not because picking it moves the estimate much. Do not assume. On this
host the two SVE-kernel arms are the expensive ones, because at 1 thread they are
the *slower* arms in the large regime (see §The expensive end): that is where
`ABS_MIN_SAMPLES = 3` binds, so per-call time passes straight through to wall clock.
Large is 63–65% of a t=1 stream and 79% of a t=8 one; small is 3–4% and level-1 is
1%. Everywhere else `MIN_SECONDS` targets fixed *work*, so a slower arm buys fewer
calls rather than more seconds, which is why the anti-correlation is weak rather
than strong. Re-read this on `c9g`: the same argument predicts nothing about which
arm is slowest there either.

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

**This touched standing order 1's denominator input set at low thread counts**, and
it was Scott's call, not the harness's: the measured peak at 1 thread is taken over a
3-rung large ladder rather than 5. DGEMM is flat-to-declining above n=2048 so the max
was not expected to move, but "not expected to" is not the standard this repo holds
denominators to. **Resolved 2026-08-19 by changing the policy rather than annotating
it** — see standing order 1: the denominator is now the max over the sizes present at
*every* thread count, so both thread points divide by a ceiling drawn from the same
input set and the cap costs no comparability. The annotation stays regardless
(`best_large_dgemm=… at n=4096 of 3 size(s)`) because it is good provenance either
way, and a max sitting at the top of a truncated ladder is still the case to read
twice; alongside it, `decompose.py` prints what the restriction cost and which size
it declined to use. `denominator-intersection` and `denominator-thread-point-dark`
are the fixtures, each mutation-validated — the second one because the obvious
implementation (intersect over the host's thread points rather than over the thread
points that *have* large dgemm) turns one dark rung into a host-wide loss of
comparability.

**`LARGE_CAP_MIN_THREADS = 8` is now validated by measurement, not by argument, and it
must not be raised.** Asked by Scott 2026-08-20: if a 1-thread `n=8192` DGEMM answers no
hypothesis anyone will cite, does the same argument apply at 8 of 192 cores, and would
raising the threshold to 16 or 32 cut a large fraction of P3? Checked against the first
P2 pass (`ARMV8` arm, spread across the large ladder at each thread point). The answer
is **no, on all three of the grounds it could have been yes on:**

- **The premise holds for GEMM and fails for everything else.** At t=8, `dgemm`/`sgemm`/
  `dsymm` are flat across `n=2048…8192` to **0.4–0.7%** — those cells genuinely are
  redundant, exactly as the question supposed. But `dsyrk` already spreads **7.3%** with
  `n=8192` the maximum, `dtrsm` 2.4%, `dtrmm` 1.0%, and `dgemv` **14.0%**. Worse, the
  `>4096`-versus-`≤4096` lift grows monotonically with thread count for every non-GEMM
  routine — `dsyrk` +4.2% at t=8, +6.6% at t=32, **+20.6% at t=96**; `dtrsm` +1.2% →
  +11.0% — so the rungs the cap would remove are where those routines are still climbing
  to their asymptote. **That is the family the campaign's most likely finding lives in**:
  the C11 false negative was an effect confined to TRSM/TRMM/SYMM, which is why item 1 of
  the #2 expansion landed before anything else. Truncating them to save instance-hours
  would be tuning the matrix until the effect is harder to see.
- **The money is not there.** Raising the threshold to 32 saves **70.5 min per host per
  pass, ~$9 on `c8g`** — against the existing t<8 cap, which saves **~4.5 h and ~$34**,
  five times more. `n=6144` and `n=8192` at one thread cost ~24 min/stream against a
  whole t=1 stream's 7.22, and that asymmetry is the entire reason the cap earns its
  keep at t=1 and stops earning it immediately above.
- **t=1 is the only rung where truncation is free, and the data says so.** At t=1 every
  large routine is flat: worst spread 3.2% (`dgemv`), 1.5% (`dtrsm`), ≤0.5% for the rest.
  At t=8 it already is not. The threshold sits exactly on the boundary.

A **routine-aware** cap would recover most of the saving without the data loss —
`gemm/symm` is **70% of the `>4096` cost at t=8** and is the provably flat part, so
truncating only those routines below t=32 recovers ~71% of the blunt cap's saving and
removes no cell that carries a trend. **Not implemented, and not to be implemented
without asking**: it changes the routine/size design, so `matrix_id` moves and this
pass's `7c371fee324b7304` stops pooling with P3's — a real cost for ~$9 a host.

One observed consequence of the denominator policy, read off `decompose.py`'s own
`denom_restriction_cost` on the partial pass rather than computed by hand: the
intersection is `{2048, 3072, 4096}` (set by t=1), and what it costs **rises with thread
count but stays about 1%** — 0.000% at t=1, 0.022% at t=8, 0.181% at t=16, 0.000% at
t=32, 0.461% at t=64, **1.240% at t=96**, where the unrestricted max would have been
1708.28 at `n=6144` against the restricted 1687.35 at `n=3072`. That is standing order 1
paying a little headroom to keep two efficiency columns commensurable, which is what it
was changed to do, and the price is now known rather than assumed.

### Reference arms are a P3 prerequisite, not a P3 discovery

- **ArmPL absent is admissible for P2 and not for P3.** Standing order 1's
  denominator is measured peak, so the P2 gate stands without it and the census
  recorded the absence honestly (`ARMPL_DIR unset or not a directory`). But the
  published framing is OpenBLAS against what the silicon can do, and a manual,
  registration-gated download discovered at launch time is discovered on five hosts
  across three passes. `scripts/install-armpl.sh` is the reproducible path: version
  and **sha256 pinned per package family** (the Arm CDN permalink is stable by name
  and says nothing about what it returns), the EULA accepted by a human via
  `GBB_ARMPL_ACCEPT_EULA=1` and never by the script, and `ARMPL_DIR` **discovered**
  after install rather than guessed — the directory name carries the GCC version and
  is part of what `build-libs.sh` records. Verified end to end on aarch64 Linux
  2026-08-19, through `make armpl` and `ldd`.
- **`BLIS_REF` is pinned — done 2026-08-20.** It was `master`, and a mutable ref means
  two hosts, or two passes days apart, can build different trees, so the cross-host
  reference comparison would be comparing two libraries and nothing in the report
  would say so. Exactly the failure a mutable `OPENBLAS_REF` would be, one library
  over, and the P3 gate's `blas_sha` check is about OpenBLAS and does not cover it.
  The pin is `061c2ebef87eda9189e6cdf38af4ea3d4a8efe7b`, **read off the running P2
  host's manifest, not chosen** — that is what its `master` resolved to, so the pin
  is a no-op against the P2 dataset by construction and P3 stays comparable to P2.
  An explicit `BLIS_REF=master` still only warns: the reference arm is not the
  subject, so an override is a recorded fact rather than a refusal. What is covered
  after the fact is `blas_sha_conflict`, which keys on `(library, target)` and so
  flags `blis/auto` carrying two SHAs without needing a new check.

## Ask before

- Launching **any** EC2 instance.
- Changing the size regimes or the routine set (the #2 expansion above is already
  approved; anything beyond it is not).
- Altering the denominator policy in standing order 1. **Closed 2026-08-19:** the
  question was whether the thread-dependent large cap's effect on the 1-thread
  denominator was acceptable, or whether `n=6144`/`8192` should run at low thread
  counts purely to keep the peak's input set uniform. Neither — the input set is
  made uniform in the *analysis* instead, by intersecting the sizes across thread
  points, so no instance-hours are spent on rungs nobody would cite. The cap
  stays.
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
