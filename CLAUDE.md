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
    Instances terminate on completion and a spot reclaim comes sooner; results
    that exist only on the instance are instance-hours spent for nothing.
13. **Session-end status comment** on the active umbrella issue: what ran, what
    the gate said, what is blocked, what is needed from Scott.
14. **Long jobs run in the background.** `build-libs.sh` is ~40 minutes and the
    sweeps are longer. Launch them backgrounded and pick them up on completion;
    never block on `sleep` or a fixed timeout.

## Spend policy

Decided 2026-08-19, after costing the design as built. One clean pass across all
five hosts is ~18.6 instance-hours and ~$96 on-demand, which is low enough to
change the question: the right use of the next $200 is **two further independent
passes**, not a 3× discount on the first.

- **P2 runs on spot, on `c8g.metal-48xl`.** Several iterations, throwaway data,
  harness debugging. A reclaim costs a restart, not integrity, and the
  iteration-count uncertainty lives here. ~$47 at four iterations, against ~$148
  on-demand. The host is not the cheap one on purpose: `c8g` is where the central
  cross lives, and debugging P2 anywhere else would leave the most important
  analysis path untested until P3.
- **P3 runs on-demand, three times.** A spot reclaim mid-sweep means arms
  *within one host's dataset* were measured on different physical hardware — a
  between-host variable smuggled inside what the analysis treats as one host.
  Per-arm S3 shipping makes that data survivable, not comparable. `us-east-1a`
  single-AZ bare metal on a new instance type is also the thinnest spot pool
  obtainable, and `c9g.metal-48xl` capacity is already the campaign's gating risk.
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
- Budget: P2 spot ~$47 + P3 on-demand ×3 ~$288 = **~$335**, ~$503 at 1.5×
  contingency. `hpc7g` is on-demand-only and has no metal size, so its tenancy
  stays inside the measurement; it gets the repeat runs and the p50/p90 spread is
  read accordingly.

## Ask before

- Launching **any** EC2 instance.
- Changing the size regimes or the routine set.
- Altering the denominator policy in standing order 1.
- Relaxing a verification tolerance.

## Phase gates

Do not start a phase until the previous gate is green. Each gate is a script
under `gates/` that exits 0/1 and prints its evidence.

| gate | requires |
|---|---|
| `gates/p0.sh` | CI green on a clean clone; `make roofline` builds; `bash -n` clean |
| `gates/p1.sh` | expected-arm census present and read from `manifest-*.ndjson` + `census-*.ndjson`; every planted effect recovered; the planted **null** reported as a null, not a weak hit, and distinguishable from a missing arm |
| `gates/p2.sh` | complete NDJSON set from one `c8g.metal-48xl` spot host; `decompose.py` clean bar genuine findings; `topology-*.txt` recorded; every arm in `census-*.ndjson` either `measured` or carrying a stated reason — zero `MISSING-UNEXPLAINED` |
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
