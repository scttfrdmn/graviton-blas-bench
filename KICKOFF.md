# Claude Code kickoff — graviton-blas-bench

Paste this as the first message of a fresh Claude Code session in an empty
`~/src/graviton-blas-bench`. The harness sources referenced below are already
written and should be dropped into the directory before you start; your job is
to productionise them, stand up the repo, and run the campaign.

---

## Context you need before doing anything

We are answering one question: **is OpenBLAS kernel work on AWS Graviton worth
doing, and if so where?** A null result is a valid and publishable outcome. Do
not build tooling that assumes OpenBLAS is behind, and do not tune the analysis
until it finds something.

What is already established, from a source audit of OpenBLAS `develop` @
`cc3fc1e` (2026-08-18):

- `kernel/arm64/KERNEL.NEOVERSEN2` contains **zero `include` directives**. It
  does not inherit `KERNEL.ARMV8SVE`. `KERNEL.NEOVERSEV2` is a one-line include
  of it, and 0.3.32 maps Neoverse V3 onto the V2 target. So Graviton 4 and 5 run
  a NEON-based kernel set: **5 SVE kernels, all `gemv`**, versus 99 on
  `NEOVERSEV1`.
- **Runtime dispatch does not fall through to generic `ARMV8` for an SVE part.**
  When the implementer/part switch in `kernel/arm64/dynamic_arm64.c` finds
  nothing it checks SME, then `HWCAP_SVE`, and returns `gotoblas_ARMV8SVE`
  before it ever reaches `return NULL`. Generic `ARMV8` requires a part that is
  unrecognised *and* SVE-less. Confirmed empirically: an OpenBLAS 0.3.30
  `DYNAMIC_ARCH` build on Cortex-X925/A725 (parts `0xd85`/`0xd87`, neither in
  the switch, both SVE2-capable) reports
  `openblas_get_corename() -> armv8sve`. So an unrecognised SVE part inherits
  the full SVE kernel set — 94 kernels — while a **recognised** Graviton 4/5
  gets 5. Being present in the dispatch table is a **downgrade** for Neoverse
  V2/V3, and a NumPy wheel that does *not* recognise the chip may be faster than
  one that does. This inverts what standing order 8 below used to say: the
  earlier prediction that an unrecognised MIDR drops the newest Graviton to
  plain NEON was not merely unproven, it was backwards.
- **`ARMV8SVE` is an experimental arm, not a control.** `KERNEL.ARMV8SVE` is the
  file `KERNEL.NEOVERSEN2` does not include, which makes it the closest thing
  in-tree to "what Graviton 4 would get if the N2 kernel-selection gap were
  closed." It runs on every host.
- **90 operations** have working SVE implementations in-tree that N2/V2/V3 do
  not select — all four `TRSM` kernels per type (they fall to
  `../generic/trsm_kernel_*.c`), the `TRMM`/`SYMM`/`HEMM` copy kernels, and the
  entire `GEMM_SMALL_*` fast path, which `NEOVERSEN2` does not define at all.
- SVE2 is **already enabled in the compiler flags** for `NEOVERSEN2`
  (`Makefile.arm64:184`, `-march=armv8.5-a+sve+sve2+bf16`). No source file in
  the tree uses an SVE2-only instruction. Runtime dispatch checks `HWCAP_SVE`
  and `HWCAP2_SME` but never `HWCAP2_SVE2`.
- Published comparisons put ArmPL roughly 20–31% ahead of a source-built
  OpenBLAS on Graviton4. Arm's own figures used `TARGET=NEOVERSEV1` on V2
  hardware and OpenBLAS still lost badly — which is the strongest hint that the
  deficit is microkernel quality, not ISA selection. **Treat the null as a live
  possibility throughout.**

**The experiment** is the hardware × target cross: build every OpenBLAS
`TARGET=` on every instance family, including targets the host is not, and run
the SVE-rich `NEOVERSEV1` kernel set *on* Graviton4 and Graviton5. That is what
separates "V2/V3 silicon is bad at SVE" from "the N2 kernel set is worse."

Instance families, one per vector regime:

| instance | Graviton | core | vector | OpenBLAS target | SVE kernels |
|---|---|---|---|---|---|
| `c6g`   | 2  | Neoverse N1 | NEON only    | `NEOVERSEN1`      | 0  |
| `c7g`   | 3  | Neoverse V1 | SVE1, VL=256 | `NEOVERSEV1`      | 99 |
| `hpc7g` | 3E | Neoverse V1 | SVE1, VL=256 | `NEOVERSEV1`      | 99 |
| `c8g`   | 4  | Neoverse V2 | SVE2, VL=128 | `NEOVERSEV2`→`N2` | 5  |
| `c9g`   | 5  | Neoverse V3 | SVE2, VL=128 | `NEOVERSEV2`→`N2` | 5  |

`us-east-1` and `us-east-2` are the only regions carrying all five. Pin the
campaign to one of them.

---

## Repo conventions

Mirror the keel repo's conventions:

- Module/repo `github.com/scttfrdmn/graviton-blas-bench`, public.
- Apache-2.0, `Copyright 2026 Scott Friedman`.
- SemVer 2.0.0, Keep a Changelog.
- Project management lives in GitHub: milestones P0–P4 below, one umbrella issue
  per phase, labels, and a Projects board created by
  `scripts/bootstrap-github.sh`.
- `CLAUDE.md` carries your standing orders, including a session-end status
  comment on the active umbrella issue as the back-and-forth channel with Scott.

---

## Phases and gates

Do not start a phase until the previous gate is green. Each gate is a script
under `gates/` that exits 0/1 and prints its evidence.

### P0 — repo hygiene
Drop in the existing sources (`src/bench.c`, `src/roofline.c`, `Makefile`,
`scripts/*.sh`, `analysis/decompose.py`, `README.md`). Add LICENSE, CHANGELOG,
`.gitignore`, `CLAUDE.md`, `scripts/bootstrap-github.sh`. CI on GitHub Actions:
compile both C sources, `bash -n` every script, `ruff` + `python -m py_compile`
on the analysis. Tag `v0.0.1`.

**Gate P0:** CI green on a clean clone; `make roofline` builds; `bash -n` clean.

### P1 — synthetic instrument check
Before any cloud spend, prove the analysis reports what it should. Write
`tools/synth.py` that emits NDJSON with *planted* effects — a small-regime
penalty, a leading-dimension penalty, a `DYNAMIC_ARCH`→generic-`ARMV8`
selection *on an SVE-capable host*, a failed verification, a noisy-neighbour
p50/min spread — and assert that `decompose.py` surfaces each one. Also plant a
**null**: a dataset where
V1-set and V2-set are at parity, and assert the decision guide reads as
"publish the negative result." An instrument that can only find hits is not an
instrument.

**Gate P1:** every planted effect recovered; the planted null is reported as a
null, not as a weak hit.

### P2 — single-host end-to-end
One `c8g.metal-48xl` in the pinned region. Run `build-libs.sh` then
`run-matrix.sh` then `decompose.py`. Expect and resolve: OpenBLAS builds for
targets the host lacks ISA for (mark unrunnable, do not crash), ArmPL install
(it is a download from developer.arm.com, not a build), BLIS config selection.

**Gate P2:** a complete NDJSON set from one host, `decompose.py` runs on it
without warnings other than genuine findings, and `numactl -H` output is
recorded — confirm whether `c8g.48xlarge` at 192 vCPU is one socket or two.

### P3 — full matrix
All five families. Metal sizes where they exist; `hpc7g` has none, so run it
repeatedly and lean on the p50/p90 spread. Capture `numactl -H` and
`capture-env.sh` on every host before any timing.

**Gate P3:** five hosts' results collected, every host's `env-*.json` present,
zero unresolved anomalies in `decompose.py` section 5 other than genuine
findings.

### P4 — decomposition and write-up
Produce the artifact that does not currently exist publicly: the deficit broken
down by routine, size regime, and thread count, with the hardware × target cross
resolved.

**Gate P4:** the report states a clear answer to "is the N2 gap worth closing,"
supported by section 2 of the decomposition, and states it as a null if that is
what the data says.

---

## Standing orders

1. **Measured peak, never theoretical.** The primary denominator is the best
   GFLOP/s any arm achieved on that host. `peak_fma` is a cross-check only; if
   it materially exceeds the best observed GEMM, every arm on that host is
   leaving headroom and *that gap* is the headline.
2. **Do not reintroduce the optimizer hazard.** The first draft of
   `roofline.c` reported 927 TFLOP/s on one core because the FMA chain was
   folded away. Constants are read from volatile storage and a hard sanity
   bound aborts the run. If you touch that file, re-verify the number is
   plausible before trusting anything downstream.
3. **Never claim a number you did not measure.** No filling gaps from vendor
   datasheets or from the published comparisons quoted above. If an arm did not
   run, it did not run.
4. **Correctness before speed.** A failed verification poisons the record. Do
   not relax the tolerance to make records pass; investigate instead.
5. **Every arm carries provenance.** OpenBLAS SHA, compiler version, MIDR,
   HWCAP, governor, NUMA topology, and what `DYNAMIC_ARCH` selected at runtime.
   A number without provenance is not admissible.
6. **The harness itself is identical across arms.** `-O2`, no `-march=native`.
   Only the BLAS under test varies.
7. **Report costs before spending.** Estimate instance-hours per phase and put
   it in the umbrella issue before launching. Terminate on completion; nothing
   idles overnight.
8. **Stop and escalate** if `capture-env.sh` reports generic `ARMV8` selected on
   a host that *has* SVE — which would mean the SVE detection itself failed — or
   `NO_SVE` set at build time. That finding outweighs every kernel question in
   the repo and changes what gets published first. An unrecognised MIDR is not
   that case: an unrecognised SVE part gets `ARMV8SVE`, which is *more* SVE
   kernels than a recognised Graviton 4/5 gets. Record it as an interesting and
   possibly good finding, not an alarm.
9. **Session-end status comment** on the active umbrella issue: what ran, what
   the gate said, what is blocked, what you need from Scott.

---

## First actions

1. Read the dropped-in `README.md` end to end, especially §Measurement
   discipline and §Hazards.
2. Scaffold P0 and open the five umbrella issues.
3. Report back with: the P0 gate result, your instance-hour estimate for P2 and
   P3, and any disagreement you have with the phase structure above. Do not
   start P1 until Scott confirms.

Ask before: launching any instance, changing the size regimes or the routine
set, or altering the denominator policy in standing order 1.
