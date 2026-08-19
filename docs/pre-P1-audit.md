# Pre-P1 adversarial audit — consolidated triage

Three independent adversarial reviews were run against the dropped-in harness
before any cloud spend: a measurement-contract audit of `src/bench.c`, a
provenance audit of `scripts/*.sh`, and a null-detection audit of
`analysis/decompose.py`. All three verified their claims by running code, not by
reading it.

**Verdict: the harness cannot currently produce an admissible answer, and the
errors are not random — they are signed.** Two of them push toward "publish the
negative result" and two push toward "the V1 kernel set wins." Those do not
cancel. Composed, the most probable write-up from the harness as dropped in is:

> *no broad OpenBLAS deficit, but the V1 kernel set beats the V2 kernel set on
> Graviton4 — so the N2 gap is worth closing.*

That is the most expensive possible wrong answer: it is the one that authorises
kernel work. It would also be unfalsifiable from the artifact, because the
provenance needed to re-derive it is not recorded.

Nothing here is a reason to abandon the campaign. Every finding is a harness
defect, not a finding about Graviton, and all of them are fixable before the
first instance-hour is spent. Finding them at $0 instead of at $7.66/hr is the
P1 gate doing exactly its job — earlier than KICKOFF.md scheduled it.

---

## 1. Findings that change the sign or size of the headline

Ordered by how directly they corrupt the conclusion, not by how hard they are to
fix.

### A. The library comparison is a thread-placement experiment
`scripts/run-matrix.sh` sets `OMP_PROC_BIND`/`OMP_PLACES`. ArmPL runs on
libgomp and obeys them. OpenBLAS is built `USE_OPENMP=0` and aarch64 OpenBLAS
ships no affinity implementation; BLIS uses `-t pthreads`. So **ArmPL threads are
pinned and OpenBLAS/BLIS threads float.**

The headline this campaign exists to decompose — "ArmPL 20–31% ahead" — is
precisely the magnitude that a pinned-vs-unpinned asymmetry produces on a
192-vCPU host. And `T=64`, designated in README §Hazards as *the one directly
comparable point across all five families*, is the most confounded point in the
matrix, not the cleanest.

Until every arm is pinned identically, the primary comparison measures
scheduling policy.

### B. NUMA first-touch mismatch manufactures a generational improvement
`bench.c` first-touches operands serially from one thread; `roofline.c`
first-touches in parallel. On a two-socket host every page lands on one socket
and half the threads read across the interconnect. `c8g.48xlarge` at 192 vCPU is
likely two sockets; `c9g.48xlarge` at 192 may be one.

So the harness would penalise `c8g` and not `c9g` **for an allocation policy**,
and report the difference as Graviton 4 → 5 progress. This is a fabricated
finding about the exact axis the campaign publishes on.

### C. Section 1 erases up to half of a real deficit
`decompose.py:119` takes `max()` per library per cell. In each cell `openblas`
contributes 6 targets × 5 sizes × 2 strides = 60 samples; `armpl` contributes
10. Max-of-N is upward-biased and the bias grows with N, so the arm with 6× the
samples is systematically flattered.

Measured over 300 trials with Gaussian run-to-run noise:

| true deficit | σ | reported | erased |
|---|---|---|---|
| 5% | 2% | +3.56% | 29% |
| 20% | 2% | +18.78% | 6% |
| 5% | 5% | +2.21% | **56%** |

Separately `winner = max(...)` floors the deficit at zero: if OpenBLAS *wins*,
the row prints `+0.0%`. 0 of 400 trials under true parity produced a negative
deficit. The report cannot express "OpenBLAS is ahead."

A "best of 6 kernel sets" OpenBLAS is also a configuration nobody ships.

### D. Section 2 promotes 2% noise to "V1 kernels win"
`--min-effect` is parsed, printed in the header, passed to `report_anomalies`,
and **never used**. The only threshold deciding anything is a hardcoded `* 1.02`
at `:158`. The header prints "parity threshold 5%" while the rows are decided at
2% — inside run-to-run variance on a shared host.

Worse, the comparison keys on `(instance, threads, routine, regime)` — **not
size, not `lda_pad`** — then maxes over the cell, so each target is represented
by its own best-case size and stride, chosen independently. Reproduced:

- V2set truly wins 4 of 5 sizes and the mean (97.2 vs 90.4) → printed
  `delta=+4.8% V1 kernels win`. The instrument reports the **opposite** of the
  truth on the campaign's central question.
- V1set is truly 5% faster but its tight-stride records are missing → printed
  `delta=-19.0% V2 kernels win`, because a padded number is compared against a
  tight one.

### E. The routines under study self-report as verified
Only `dgemm` had a correctness check. Seven drivers passed a hardcoded
`verified=1`, including `dtrsm`, `dtrmm` and `dsymm` — **exactly the operations
in the 90-kernel N2 gap.** The routines most likely to be mis-dispatched were the
ones asserting correctness on the strength of nothing, and `decompose.py` then
read them with `r.get("verified", True)` — fail-open.

*Fixed in `src/bench.c` (tri-state `verified`, JSON `null` where no check ran).
The analysis side still fails open.*

### F. TRSM/TRMM were timed on Inf and on exact zeros
TRSM/TRMM are destructive in place on `B` and `TIMED_LOOP` never restored it, so
the triangular operator's gain compounded once per rep. With a diagonal of `n`
that is a per-rep factor of ~`n`: at n=256 the operand reached `+Inf` (dtrmm) or
exact `0.0` (dtrsm) by roughly rep 128. Affects n ≲ 400 — the SMALL and
low-MEDIUM regimes the campaign calls its cheapest available fix.

*Fixed: unit diagonal, off-diagonals scaled by 1/n (strictly more diagonally
dominant than before at every n), plus `operand_finite()` to check the bound
rather than assert it.*

### G. Timer overhead biases the campaign toward the null
`TIMED_LOOP` brackets every call with `now()`. At n=8 the pair costs ~31 ns,
**27.9% of the sample**. Because it is a constant additive term it compresses
ratios: a real 20% difference in the small regime reads as ~14%. The bias points
at "no effect found" on the primary conclusion.

`MAX_REPS=200` also breaks the documented `MIN_SECONDS` floor at small n, and
`MIN_REPS=3` makes `t_p50 == t_p90` for every LARGE level-3 case, so the
dispersion signal README relies on does not exist where it is most needed.

### H. A hole in the experiment is indistinguishable from the publishable null
`decompose.py:155` `continue`s when either target is absent, so a missing arm
prints *nothing* while a true null prints one `parity` row. Three routes produce
no record anywhere in `results/`: build failure, `runnable:false` (which is **the
normal case** for the target cross), and the fact that
`$PREFIX/build-manifest.ndjson` lives outside `results/` and is never read. The
decisive V1-set-on-`c8g` cell can therefore vanish silently.

`pct()`'s falsy guards make this worse: two arms at `0.00` GFLOP/s print
`parity` — the word the decision guide maps to "publish the negative result."

### I. Nothing decides anything, so gate P1 is unimplementable
`decompose.py:285-297` is unconditional literal text; `main()` returns `0` on
every input. On a dataset with **zero comparisons**, `grep -q parity` passes
(it's in the header) and `grep -q "publish the negative result"` passes (static
guide text). There is no assertion `gates/p1.sh` could make that means what P1
requires. A computed verdict and `--json` output are prerequisites for the gate
existing at all, not polish.

Relatedly, `report_regime_profile` is promised in the docstring and **does not
exist** — no code anywhere compares regimes, yet P1 requires the planted
small-regime penalty be recovered.

### J. Provenance: the OpenBLAS SHA never reaches the results
`OPENBLAS_REF` defaults to the mutable `develop`; the build manifest is written
outside `results/`; `GBB_BUILD` is the *gbb repo* SHA and will be misread as the
BLAS version; nothing compares SHAs across hosts. Two hosts can be built from
different OpenBLAS trees and the artifact cannot tell. Standing order 5 says a
number without provenance is not admissible — currently none of them are.

### K. Standing order 8 stops nothing
`capture-env.sh:139` is an unconditional `exit 0`. Warnings go to stderr, which
backgrounded runs send to unread scrollback. `run-matrix.sh` never inspects
`core_name` or governor before starting a multi-hour sweep. And the two warnings
that matter are keyed off `CORE_NAME`/`PART` — the MIDR — not off what OpenBLAS
*actually selected*, which is the thing standing order 8 is about.

The `DYNAMIC_ARCH`→generic-`ARMV8` detector at `decompose.py:220` **fails open**:
when `GBB_OPENBLAS_DYNAMIC_DIR` is unset or the probe fails to compile,
`capture-env.sh:85` leaves `OB_CORE="unknown"`, which is truthy and matches
neither substring, so no anomaly fires — silence precisely when the detection
broke. There is a MIDR-based backstop at `:217`, so this is HIGH rather than
critical.

One reviewer claim here does **not** survive checking. It was argued that the
test `"armv8" in sel and "sve" not in sel` fires on every host, on the grounds
that no Neoverse corename contains `sve` while the config string contains
`ARMV8`. Measured against a real OpenBLAS 0.3.30 `DYNAMIC_ARCH` build (see §3):
`openblas_get_config()` embeds the **corename**, not a literal `ARMV8` token, so
`sel` for a recognised Neoverse part contains no `armv8` substring at all and the
test correctly stays quiet. The test is sound; only the fail-open path is broken.

### L. Heterogeneous cores are not detected
`capture-env.sh:37` reads `cpu0`'s MIDR and labels the whole host. Verified on
`castor.local` (DGX Spark GB10): `cpu0` is a Cortex-A725 efficiency core, so the
box is labelled `Cortex-A725` at 2.81 GHz while half of it is Cortex-X925 at
3.90 GHz. The clusters are **interleaved, not contiguous** (X925 = cpus 5–9,
15–19), so `OMP_PLACES=cores` + `OMP_PROC_BIND=close` with 8 threads lands on 5
efficiency and 3 performance cores and reports the blend as one number.

No Graviton is heterogeneous today, so this is not a P3 blocker. It matters
because standing order 5 requires "all cores are identical" to be a **recorded
fact** rather than an assumption, and because the free dry-run host is
heterogeneous.

---

## 2. Durable results (new requirement)

Instances terminate on completion (standing order 7), so results currently live
only on the instance's disk until a run finishes cleanly. A crash, a spot
reclaim, or an early terminate loses the whole sweep — and a multi-hour `c9g`
sweep is ~$8/hr of unrecoverable work.

Ship results to S3 **incrementally as each arm completes**, not at the end:

- `s3://<bucket>/graviton-blas-bench/<run_id>/` — `run_id` carries a timestamp,
  keys are never overwritten, so the bucket is an append-only record.
- Upload after each `(library, target, threads)` arm, not after the sweep.
- Upload `env-*.json` **before** any timing starts, so a host that dies mid-sweep
  still leaves its provenance behind.
- Upload `build-manifest.ndjson` too. This also fixes finding H: the manifest is
  the expected-arm census that makes "absent" distinguishable from "parity."
- Enable bucket versioning; the record is evidence for a publication.

This is a P2 prerequisite, and it is the reason the manifest problem in H should
be fixed by *moving* the manifest into the uploaded set rather than by teaching
`decompose.py` to look outside `results/`.

---

## 3. A free measurement that inverts standing order 8's assumption

Checking finding K needed a real `DYNAMIC_ARCH` OpenBLAS on an SVE2 host.
`castor.local` (NVIDIA DGX Spark, GB10) has one — the `scipy_openblas64` bundled
with NumPy. Its MIDR parts are `0xd85` (Cortex-X925) and `0xd87` (Cortex-A725),
neither of which is in OpenBLAS's dispatch switch, so this host exercises exactly
the unrecognised-part path standing order 8 exists to catch:

```
openblas_get_corename() -> armv8sve
openblas_get_config()   -> OpenBLAS 0.3.30  USE64BITINT DYNAMIC_ARCH
                           NO_AFFINITY armv8sve MAX_THREADS=64
```

The fallback for "has `HWCAP_SVE`, part unknown" is **`ARMV8SVE`** — not generic
`ARMV8`. And `KERNEL.ARMV8SVE` is the file `KERNEL.NEOVERSEN2` conspicuously does
*not* include. So on this host an **unrecognised** SVE part receives the full SVE
kernel set, while a **recognised** Graviton 4 receives `NEOVERSEV2` →
`NEOVERSEN2` and its 5 SVE kernels.

If that holds on Graviton, being in OpenBLAS's dispatch table is *worse* than
being absent from it, and standing order 8's premise — that an unrecognised MIDR
is the alarming outcome — is backwards for any SVE-capable part. It would also
mean the `ARMV8SVE` arm, currently specified as a mere control, is a
first-class experimental arm: it is the closest thing in-tree to "what Graviton 4
would get if the N2 gap were closed."

**This is a hypothesis, not a finding** (standing order 3). It was measured on
Cortex-X925/A725 under 0.3.30, not on Neoverse V2/V3, and KICKOFF.md records that
0.3.32 maps V3 onto the V2 target — so V3 is recognised there and would not take
this path. What it is: a cheap, specific, falsifiable prediction to test in P3,
and a reason to keep `ARMV8SVE` in the target cross on every host rather than
treating it as optional.

`castor.local` is not a Graviton substitute — Cortex ≠ Neoverse, and the box is
heterogeneous (finding L). It is a zero-cost host for exactly this kind of
contract check, and for the end-to-end dry run proposed as P1.5.

---

## 4. Fix order

Grouped so that each group is independently verifiable.

1. **`bench.c` measurement contract** — done: operand blow-up, tolerance,
   tri-state `verified`, `inf` guard, `realloc` guard. Still open: timer overhead
   (G) via batched timing, `MAX_REPS`/`MIN_REPS` contract, parallel first-touch
   to match `roofline.c` (B).
2. **Pin every arm identically** (A). Highest-value single fix; without it no
   library comparison means anything.
3. **Provenance and stop conditions** (J, K, L) — pin `OPENBLAS_REF` to a SHA,
   move the manifest into `results/`, make `capture-env.sh` exit non-zero and
   `run-matrix.sh` refuse to start, record runtime SVE VL and per-CPU MIDR.
4. **S3 shipping** (§2).
5. **`decompose.py` as an instrument** (C, D, E, H, I) — compare like against
   like size by size, aggregate across repeats with a dispersion-based parity
   band, coverage census, computed verdict plus `--json`, meaningful exit code,
   implement `report_regime_profile`, fail closed on `verified`.
6. **`tools/synth.py` and `gates/p1.sh`** — written *last*, against the fixed
   analysis, and deliberately including the false-positive datasets these audits
   used: V2-truly-wins-but-V1-reported, a missing arm, two arms at 0.00, and a
   3%-apart pair that must read as parity. The synthetic suite's job is to keep
   these specific regressions dead.

`decompose.py` is deliberately last. It is the component whose correctness is
hardest to establish by inspection, and `synth.py` is the thing that establishes
it — so writing the analysis fixes and their test together, after the data
contract is settled, is cheaper than fixing it twice.

No EC2 instance should be launched until items 1–4 are complete. Items 5–6 are
gate P1 itself.
