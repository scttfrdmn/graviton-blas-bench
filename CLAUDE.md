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
  to `us-east-1`/`us-east-2`, the only regions carrying all five families.

## The ten standing orders

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
8. **Stop and escalate** if `capture-env.sh` reports an unrecognised MIDR or a
   `DYNAMIC_ARCH`→generic-`ARMV8` fallback on any host. That finding outweighs
   every kernel question in this repo and changes what gets published first.
9. **Session-end status comment** on the active umbrella issue: what ran, what
   the gate said, what is blocked, what is needed from Scott.
10. **Long jobs run in the background.** `build-libs.sh` is ~40 minutes and the
    sweeps are longer. Launch them backgrounded and pick them up on completion;
    never block on `sleep` or a fixed timeout.

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
| `gates/p1.sh` | every planted effect recovered; the planted **null** reported as a null, not a weak hit |
| `gates/p2.sh` | complete NDJSON set from one host; `decompose.py` clean bar genuine findings; `numactl -H` recorded |
| `gates/p3.sh` | five hosts collected; every `env-*.json` present; no unresolved section-5 anomalies |
| `gates/p4.sh` | report answers "is the N2 gap worth closing", supported by section 2, stated as a null if that is what the data says |

## Working conventions

- Project management lives in GitHub: milestones P0–P4, one umbrella issue per
  phase, labels, and a Projects board created by `scripts/bootstrap-github.sh`.
- Conventional Commits. Update `CHANGELOG.md` under `## [Unreleased]`.
- Delegate separable work to concurrent subagents — read-only audits, per-host
  analysis, boilerplate. Keep measurement-policy judgment in the main loop.
- `results/` and `bin/` are gitignored. Collected campaign results are published
  as release artifacts, not committed.
