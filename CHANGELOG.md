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

### Changed — affects how the timing-floor band is measured and read; no matrix number changes

- **The overlap band is replicated four times per size, because at two pairs a cell it
  could not answer the question it was bought to ask.** On the first P2 pass the band
  came back `2 of 390` pairs outside their own band, with **56% floor sign consistency
  against a 3% order control**. Read as adjudication that is a null; read honestly it
  is *n=2 per cell*, which is not evidence for a floor effect and equally not evidence
  against one. Scott's ruling, 2026-08-20: **don't adjudicate it, power it** — raise the
  replication so the question is answerable on P3, and leave section 4's small-regime
  block in place until it is.

  `src/bench.c` gains `#define OVERLAP_REPS 4`; `run_floor_overlap()` loops it and
  alternates the floor order on `(i + r) % 2`, so position is balanced *within* a cell
  rather than only across the five sizes. Every bench record gains **`probe_rep`**,
  printed from both `emit()` and `emit_prefix()` immediately after `probe`; matrix
  records carry `0`. Cost is ~6 s per stream against a 2.27 min cheapest rung, and
  `matrix_id` does not move — the band was already excluded from the dry pass, so
  `7c371fee324b7304` over 544 cases still stamps a replicated stream.

  **`OVERLAP_REPS` must be even**, and `gates/p1.sh` asserts it: with alternation on
  `(i + r) % 2` an odd count hands each size one position once more often than the
  other, reintroducing exactly the order asymmetry the alternation exists to remove.

  Four decisions in the analysis, each mutation-validated:

  - **`probe_rep` is in the comparison key, not merely in the record.** It is
    `cond[11]`, and the pair key deliberately drops only `cond[10]` (the floor) into
    the inner dict. Without the rep in the key all four reps of a cell land in one
    slot and the last write wins — three quarters of the replication silently
    discarded, reported as one pair. Dropping it fails all four band fixtures.
  - **The sign tests changed unit from pair to cell**, over each cell's median delta,
    where a cell is `(instance, arm, threads, size)`. This is a fix, not a
    recalibration: **unanimity is anti-monotone in sample size**, so replicating the
    band made a planted bias *harder* to report — the 2% `floor-band-biased` fixture
    went from 5 unanimous pairs to 240 pairs at 92.5% consistency and came back
    `AGREES`. A binomial test at α = 1/16 was considered and rejected: it reproduces
    unanimity exactly at n=5, but the 240 pairs are correlated within cells and
    assuming independence would overstate the significance of whatever it found. The
    median-per-cell unit is a **no-op at `OVERLAP_REPS = 1`**, so no pre-replication
    dataset changes its answer. `MIN_FOR_SIGN = 5` now counts cells, which is why it
    is unaffected in either direction by the rep count.
  - **`ORDER-CONFOUNDED` is checked before `outside`, guarded by `not persistent`.**
    A 3% order effect plus jitter puts a handful of 240 pairs outside band, and with
    the band test first the status became unreachable: the report would have said
    `DISAGREES` and sent someone to change `MIN_SECONDS` over a drift. This unblocks
    **nothing** — `confirmed` is false and bit 32 fires under either status, and
    section 4 stays blocked under either — the only thing that changes is which cause
    the reader is sent after. The fixture now asserts `outside_band > 0` *and*
    `n_persistent_cells == 0`, so the precedence itself is under test rather than the
    arithmetic that happens to reach it.
  - **Three reproducibility buckets, not two.** An out-of-band cell is `persistent`
    (majority of its reps out, all one sign), `unreproduced` (reps disagree), or
    `unreplicated` (one pair, so neither). Calling a single-rep cell `persistent`
    manufactures a reproduction; calling it `unreproduced` dismisses it on no
    evidence. **Persistence is still not a precondition for `DISAGREES`** — gating on
    it would answer the question the replication was bought to ask, in the direction
    of finding nothing, on a threshold nobody argued for. Failing toward the block is
    the safe direction.

  `report_floor_overlap()` grows a `rep` column, a `replication: N cell(s) x M rep(s)
  = P pairs` line, a `NO REPLICATION` branch, per-cell PERSISTENT and not-reproduced
  listings capped by `--max-listed`, and a closing note that the status does not
  discount an unreproduced cell. The reported worst pair is now the largest by
  magnitude rather than the first one enumerated.

  **`gates/p2.sh` section 7 now requires `reps_per_cell >= 2` whenever the band ran at
  all**, so a pass built from a pre-replication binary cannot report `AGREES` and read
  as stronger evidence than it is; new `probe-unreplicated` mutant holds it. New
  fixture **`floor-band-unreplicated`** carries the legacy single-rep shape — which is
  the shape the first P2 pass has — and asserts `DISAGREES` with
  `n_unreplicated_cells == 60`, bit 32 set, bits 4/8/16 clear. `gates/p1.sh` section 2b
  additionally pins the alternation expression, the `probe_rep` printf position in both
  emit paths, and `OVERLAP_REPS` against `synth.py`'s copy of it.

  **One behavioural consequence to state plainly: `DISAGREES` now fires more readily on
  noise**, because four reps give a dispersed cell four chances to stray outside its
  band. That is deliberate and it is the safe direction — it fails toward section 4's
  block, never away from it — but it means a band status on P3 must be read with the
  `persistent` / `unreproduced` split, not off the headline alone.

### Added — affects what the record can certify; no measured number changes

- **Every routine now carries a correctness check. Eight of nine had none.** Before
  this change only `dgemm` was verified, so `31,723 of 42,743` cells on the first P2
  pass carried `verified: null` — and the nulls were not spread evenly. `dtrsm`,
  `dtrmm`, `dsymm`, `dsyrk`, `sgemm`, `dgemv`, `daxpy` and `ddot` were at **0%
  coverage**, which means TRSM/TRMM/SYMM — the family the 90-operation N2 gap lives
  in, and the family the C11 false negative was confined to — was precisely the part
  of the matrix the campaign could not certify had produced correct answers. The
  likely headline was about routines with no check behind them. Standing order 4 puts
  correctness before speed; a number whose correctness was never tested is not a
  slower number, it is not a result.

  This traces back to the original timing audit. Making `verified` tri-state and
  emitting `null` where no check ran was the right fix for a hardcoded `verified=1`,
  which was a lie — but reporting a gap honestly is not closing it, and the matrix
  has since grown roughly fivefold around it.

  Each check recomputes a **4×4 top-left corner** by hand, from one untimed call
  before the timed loop, and then restores the operand so the timed loop starts from a
  bit-identical input. The corner is not an economy for its own sake: for a
  lower-triangular product the top-left corner closes over itself — row *i* touches
  only columns `0..i` — so `dtrsm`/`dtrmm` verify in ≤4·4·4 flops at any *n*.

  - `dtrsm` is checked as a **residual**: recompute `A·X` from the solved `X` and
    compare against the saved `alpha·B0`. Re-solving would be both expensive and
    self-confirming if the defect were in the solve's own dispatch.
  - `dtrmm` verifies the product directly, same structure in reverse.
  - `dsyrk` checks **only `i >= j`**. A correct kernel leaves the upper triangle
    untouched, so checking it would fail a correct kernel.
  - `dsymm` reads A **only from its lower triangle**, via a `sym_lo()` accessor,
    because `dsymm("L","L")` does and `fill_d()` put unrelated noise in the upper one.
  - `sgemm` accumulates its reference in FP64 but scales tolerance by `FLT_EPSILON`:
    a *correct* fp32 kernel carries ~`k·FLT_EPSILON` of error.
  - `ddot` is the one routine with **no corner** — the result *is* the full reduction,
    so the reference costs a whole untimed pass. At `n=4194304` the tolerance is
    7.4e-9 relative, which still rejects a kernel that dropped a single element
    (~1e-4 out) or mishandled `incx`.

  **The triangular tolerance is scaled by the corner's reduction length, not by `m`,
  and that distinction is load-bearing.** `fill_tri_d` sets a unit diagonal and
  off-diagonals of `TRI_OFFDIAG/n = 1e-9/n`, so the entire off-diagonal contribution
  to `X[i]` is ~`1e-9/m` ≈ 4e-13 at `m=8192`. An `m`-scaled tolerance
  (`8·8192·eps` = 1.4e-11) is **30× larger than the whole signal being checked**, so
  it would certify a kernel that ignored the off-diagonal update path entirely. The
  corner length (`mm ≤ 4`, tol ≈ 7e-15) puts that path two orders above tolerance.
  This is not a hypothetical: it is what the first draft did, and the
  `trsm-m-scaled-tolerance-is-blind` mutation exists because of it.

  **No measured number moves and `matrix_id` does not move.** Operands are restored
  before every timed loop, so the timed work is unchanged, and the design is
  untouched: `7c371fee324b7304` over 544 cases, before and after. The P2 dry-run
  dataset keeps its nulls — a shipped record is not edited — so `verified: null`
  remains reachable for archived data, for `case_skipped` records, and for any newly
  added routine before its check lands, and the report's caveat machinery stays.

  Validated two ways, because the failure mode here is a check that cannot fire:
  end-to-end against a real BLAS, where all nine routines report `verified: true` and
  **zero false**; and by **`tests/verify-corner-mutation.sh`**, new and wired into CI
  and `gates/p0.sh`, which breaks each routine's result deliberately and requires the
  check to notice. Eleven mutations across three expectation kinds — `fires`,
  `silent`, `blind_large`. `syrk-upper-triangle-garbage` is a `silent` case: garbage
  in the untouched upper triangle must **not** fail a correct kernel, so a check that
  over-fires is caught as well as one that under-fires. The suite fails loudly rather
  than skipping when it can find no BLAS to link, and asserts its own baseline
  (`91 true, 0 false, 0 null, over 9 routines`) so that a mutation "passing" against a
  broken fixture is not possible.

### Fixed — gate coverage; affects no measured number

- **The instrument-host quarantine could be defeated by a test hook on any Neoverse
  host, including the campaign hosts themselves.** Campaign role requires two
  conditions — a campaign instance type from IMDS, and a cpu0 MIDR part in
  `GRAVITON_PARTS` — and `GBB_TEST_IMDS_TYPE` forges the first so the stub suite can
  reach the code path without EC2. Its comment claimed the second condition still had
  to hold and that "on any machine a test runs on it does not". **That was an
  assumption about the test host, not a construction, and it is false.** CI found it:
  gate p0 runs on `ubuntu-24.04-arm`, whose cpu0 MIDR part *is* in the list, so the
  forged type promoted the runner to campaign role, `bench-instr-L.ndjson` was never
  written, and the suite's own anti-forgery assertion failed — while the same suite
  passed on the x86 runner and on darwin, where the MIDR is unreadable. The sharpest
  case is the one nobody would have run into by accident and which would have mattered
  most: running the stub suite on the `c8g` itself would have written
  campaign-namespace records from a test. CLAUDE.md's rule for these boxes is
  quarantine *by construction, not by discipline*, and this was discipline wearing
  construction's clothes.
  - A forged instance type now refuses campaign role outright, whatever the silicon
    says. The check is placed **last**, so the two evidence-based refusals still fire
    wherever they apply — a laptop, castor, an x86 runner — and are still what the
    suite exercises there.
  - `GBB_TEST_MIDR_PART` was added so both legs can be pinned deterministically on
    every platform instead of only on whichever runner happens to carry a Neoverse
    part — the same host-dependence that hid the hole. **It can only demote:**
    promotion needs a campaign instance type from real IMDS, which it does not touch,
    and on a real campaign host the true part is already in the list.
  - Section L of `tests/run-matrix-stubs.sh` lost its silicon premise and gained L2
    (forged type + castor's `0xd85` → refused on the MIDR) and L3 (forged type + a
    *real* Graviton part `0xd4f` → still instrument, refused on the forgery).
    Mutation-validated: with the new condition removed, L3's three assertions fail and
    campaign-namespace records appear — reproducing the CI failure on darwin, where
    the old assertion could not see it.

- **A whitespace failure had been silently skipping the P1 calibration gate.**
  `ruff format --check` failed from `b59ae7b` onwards over three sites in
  `analysis/decompose.py`, `tools/p2-mutate.py` and `tools/synth.py`. Because
  `gate-p1` declared `needs: [python]` and the `python` job bundled
  `ruff format --check` with `ruff check` and `py_compile`, GitHub reported
  `gate p1: skipped` — not failed — on **every push** across the window in which
  `42c6369` rewrote section 1's reference-arm scope and the denominator input set
  and `04f4732` retired the `peak_fma` cross-check. `gate-p0` was skipped the same
  way. `gate-p1`'s own comment states its purpose as catching `synth.py` drift
  "before the drift is discovered by a dataset that cost real instance-hours"; it
  was dark while the first P2 pass spent them. Nothing was mis-measured — the
  reformat is AST-identical on all three files and the suite passes 65 scenarios
  and 36 checks unchanged — but for that window the calibration was unverified
  rather than verified.
  - Formatting is now its own `python-format` job. `gate-p1` depends on semantics
    (`ruff check`, `py_compile`) only; `gate-p0` still depends on formatting,
    because its stated requirement really is "CI green on a clean clone".
  - `ruff` is **pinned** to `0.16.3`, for the same reason `BLIS_REF` is pinned: an
    unpinned `pipx install ruff` is a mutable ref, so a formatter release can turn
    an unchanged tree red and take the gates down with it.
- **`gates/check-build-flags.sh` could not fail when its probe broke.** The
  real-compile-line half of standing order 6 ran
  `DRYRUN="$(make -n roofline 2>/dev/null || true)"` guarded by
  `[ -n "$DRYRUN" ]`, so any `make` failure yielded an empty string and skipped
  both the forbidden-flag and the `-O2` check in silence, printing "harness build
  flags conform to standing order 6". Same shape as the `sve_kernels()` bug below:
  a probe failure collapsing into a substantive answer. An unverifiable compile
  line is now a `FAIL`, verified against a fixture on which the old form exits 0.
- **`tests/arch-selected-assert.sh` was vacuous on ELF — the platform the campaign
  runs on — and green on the dev host.** It linked its stub `openblas_get_corename`
  into the *executable*. `bench.c` finds that symbol with
  `dlsym(RTLD_DEFAULT, ...)`, and on ELF an executable-resident symbol is absent
  from `.dynsym` unless the link passes `-rdynamic`, so the lookup returned NULL.
  `bench.c` then behaved perfectly correctly — no OpenBLAS in the image, label the
  arm `n/a` — and every assertion about **refusing a disagreeing label passed
  without exercising the refusal**. Measured on aarch64 Linux against the old form:
  `openblas_get_corename` in `.dynsym` = **0**, `arch_selected` recorded as
  `unprobed` rather than `neoversen2`, and a deliberately disagreeing label exited
  **0 instead of 4**. Mach-O exports executable globals by default, which is why it
  passed locally. The suite that guards standing order 10's fatal check — *"a
  mislabelled arm is not a failed run, it is a plausible wrong answer"* — could not
  fail on the only platform that matters.

  Fixed by putting the stub where the real symbol lives: a **shared library**, linked
  with an rpath. `-rdynamic` would have fixed the symptom while leaving the fixture
  testing a topology that does not exist — in the campaign the corename comes out of
  `libopenblas.so`. Same rule as P1's "the fixture must stay faithful to the
  producers", applied to link topology rather than to size ladders.

  The suite now **proves it is non-vacuous before asserting anything**: it reads back
  `arch_selected` from the stub-linked binary and aborts if it is not `neoversen2`,
  rather than reporting a green it has not earned. And a build failure is now a
  `FAIL`, not a `SKIP` — only a wholly absent compiler skips. `SKIP` on a broken build
  is the same vacuous-pass shape as `check-build-flags.sh` above: a renamed source or
  a new dependency in `bench.c` would have printed SKIP and passed, which is how the
  broken-build case was found. Verified on aarch64 Linux (10/10, and 10/10 on Mach-O),
  and mutation-validated on two mutations — a stub that answers nothing trips the
  vacuity guard, and an unbuildable source exits 1.

- **The `shell` job's shellcheck step had been hiding four test suites, which is how
  the above survived.** Steps within a job are sequential, so shellcheck failing from
  `b59ae7b` meant `run-matrix-stubs.sh`, `arch-selected-assert.sh`,
  `sve-probe-assert.sh` and `workload-preflight.sh` **did not run at all** for that
  whole window — the identical defect to `ruff format` taking down `gate-p1`, one
  language over, and it was live at the same time. Lint is now its own `shell-lint`
  job, and the remaining suites carry `if: ${{ !cancelled() }}` so that four
  independent questions get four answers instead of stopping at the first no.
  `gate-p0` requires both jobs, since its bar is "CI green on a clean clone".

- **`tests/sve-probe-assert.sh` could not reproduce the bug it exists to guard, on
  CI, because SIGPIPE is ignored there — and unmasking the `shell` job is what
  revealed it.** The suite's central claim is that its fixture still reproduces the
  SIGPIPE bug: it asserts the *old* `nm | grep -q` form returns `no`, precisely so
  that section 3 cannot pass against a broken implementation. On the x86 runner it
  returned `yes`.

  **The bug needs `nm` to be KILLED by SIGPIPE.** If SIGPIPE is ignored, `nm`'s write
  returns `EPIPE` instead, `nm` exits 0, the pipeline succeeds, and the old form
  returns `yes` — at *any* fixture size. And a signal's disposition is inherited
  across `fork` and `exec`, so whether this suite can reproduce the bug depends on
  **who launched it**: GitHub Actions runs each step from a Node.js process, and Node
  sets SIGPIPE to `SIG_IGN`. Measured on aarch64 Linux, same fixture, same bytes:

  | parent | `/proc/self/status` `SigIgn` | `PIPESTATUS` | status | probe answers |
  |---|---|---|---|---|
  | interactive bash | `0000000000000000` | `141 0` | 141 | `no` — bug reproduces |
  | SIGPIPE ignored | `0000000001001000` (bit 13) | `0 0` | 0 | `yes` — **cannot reproduce** |

  The suite now **measures that precondition and restores it**: a `yes | head -1`
  probe reads `PIPESTATUS[0]`, and if it is not 141 the suite re-execs itself once
  through `python3` with `SIGPIPE` set to `SIG_DFL` — which survives `exec`, whereas
  `SIG_IGN` is what persists by default and is the whole problem. If it cannot be
  restored the suite **FAILS loudly**; it never skips, because a skip here is exactly
  the vacuous pass the file exists to prevent. Verified 13/13 under both dispositions,
  and mutation-validated by suppressing the re-exec under an ignored SIGPIPE: the
  guard fires, exits 1, and reports the observed producer status of 1 rather than 141,
  which is the `EPIPE` path confirming the mechanism.

  **This was first misdiagnosed as a fixture-size race, and the wrong diagnosis is
  recorded rather than quietly replaced.** The story was that 6×500 short-named
  symbols gave ~93 KB of `nm` output against a 64 KiB pipe buffer, so `nm` could
  finish during `grep`'s first scan; it fit the platform split exactly and it was
  wrong. Enlarging the fixture 12× to **1,152 KB** left x86 CI failing with *both*
  size preconditions passing and a byte count identical to the host where it passed.
  Size was never the variable. The falsification is the reason the two deterministic
  size assertions are **kept**: the first matching symbol is within the first tenth of
  `nm`'s lines, and the output exceeds 8× a 64 KiB buffer. A precondition that can be
  checked separately is what lets "the fixture degraded" be told apart from "the
  environment differs" — which is how the size hypothesis was killed rather than
  argued about. The enlargement is kept on its own merits (93 KB against 64 KiB is a
  thin margin, and margin costs ~1 s), explicitly *not* as the fix.

  Writing those assertions reproduced the original defect in miniature:
  `nm | grep -nE -m1 | cut` under the suite's own `pipefail` killed the suite on the
  measurement line. `nm`'s output is now captured to a file once and every count taken
  from there — the same fix the probe itself got.

- **Two more live instances of that defect class, found by auditing for it.** Neither
  is a measurement bug; both are environment-dependent failures on paths that have to
  behave identically across five hosts and three passes.
  - `install-armpl.sh` located the installer and the licence text with
    `find … | head -1` under `set -euo pipefail`. `head` exits after one line, `find`
    is killed by SIGPIPE on its next write, `pipefail` reports 141, the command
    substitution fails, and `set -e` **aborts the install with no message,
    immediately after a ~1 GB download and extraction** — but only when `find` still
    had output buffered *and* SIGPIPE is deliverable, so it would fail on an
    interactive host and succeed under a parent that ignores SIGPIPE. Now
    `find … -print -quit`, which stops `find` itself: no pipe, no early-exit
    consumer, and the exit status means what it appears to mean. The adjacent
    `[ -n "$LICENCE" ] && log …` is now an `if` — checked rather than assumed, since
    bash's `set -e` *exempts* a non-final element of an `&&` list and the old form
    did **not** abort; it is changed because it leaves a non-zero status behind.
  - `bootstrap-github.sh` tested for existing labels, umbrella issues and the project
    board with `gh … | grep -q`. A SIGPIPE-killed `gh` reports 141, so an **existing**
    label reads as absent and the script tries to create it — silently inverting the
    idempotency this file's own comments claim. All three now capture first and match
    a `printf`, whose small builtin write cannot induce SIGPIPE in its producer.

  `gates/p2.sh`'s `grep … | head -6 | sed` is the same shape and was left alone
  deliberately: the script has no `-e`, and the pipeline's status is discarded because
  it is a diagnostic print, so a 141 there changes nothing.

- Tree-wide `shellcheck --severity=warning` is clean, which the `shell` job has
  been failing on: five `cd` without `|| exit` (these scripts run `set -uo
  pipefail`, no `-e`, so a failed `cd` really did continue), two dead variables,
  and four `ls | grep -c` quarantine counts replaced by a glob helper. The three
  remaining warnings are suppressed individually with a stated reason.

### Fixed — affects provenance, not any measured number

- **`build-libs.sh`'s SVE probe could only ever answer `no`.** `sve_kernels()` ran
  `nm --defined-only "$lib" | grep -qE '(ARMV8SVE|_sve|sve_)'` under the script's
  `set -euo pipefail`. `grep -q` exits on its first match, `nm` then dies on
  SIGPIPE, and `pipefail` makes the pipeline report 141 — so the `if` took the else
  branch. Both outcomes printed `no`:

  | SVE in the archive | pipeline status | printed |
  |---|---|---|
  | present | 141 (SIGPIPE) | `no` |
  | absent | 1 (grep found nothing) | `no` |

  It was a constant function. It could not pass and it could not fail; it could only
  be believed. Because `no` is what `decompose.py` turns into standing order 8 —
  *"every SVE-coretype arm on this host measures the NEON path under an SVE label.
  Stop and escalate"* — the campaign's single most outweighing alarm was wired live
  from the moment it was written, and it fired on all four OpenBLAS builds of the
  first P2 pass (`20260820T031023Z-ip-172-31-36-19`) while SVE was demonstrably
  present: 1,092 matching defined symbols including `dgemm_kernel_ARMV8SVE`, 135,312
  SVE instructions in the `.so`, and a measured `GEMM_SMALL` effect that cannot
  exist without SVE kernels. **The equally serious half is the converse**: on a
  genuinely `NO_SVE` build the probe would have been just as unable to stay silent,
  so it carried no information in either direction.

  `nm`'s output is now captured whole, its exit status kept, and the count taken with
  `grep -c`, which consumes all of its input and cannot induce SIGPIPE in its
  producer. A failed `nm` or an empty symbol table now yields `unknown` rather than
  `no` — `decompose.py` already treats those as different claims (provenance gap vs
  escalation) and was correct throughout; only the producer was wrong. A truncated
  archive previously read as a *confirmed absence* of SVE. The probe also now logs
  its answer and the matching-symbol count via `log` (stderr, folded into
  `build.log`), because the bug survived a whole pass for want of anyone reading its
  output before `decompose.py` did, hours later and on another machine.

  **No measured number changes and no pass needs re-running.** The libraries were
  built correctly; one provenance field was recorded wrongly. The P2 manifest is left
  exactly as shipped — editing a shipped record is worse than either alternative — so
  that dataset's `sve_kernels` field is invalid by construction and carries no
  information; SVE presence for that pass is established out-of-band by the three
  witnesses above.

  New: `tests/sve-probe-assert.sh`, wired into CI and `gates/p0.sh` §5e. It runs under
  `set -euo pipefail`, because a bare `bash x.sh` does not inherit it and the bug is
  invisible without it — forty standalone runs of the broken function returned `yes`
  during the investigation, which is exactly how it survived review. It also proves
  its own fixture: a pipeline only induces SIGPIPE while the producer is still
  writing, so the suite asserts that the **old** form still returns `no` on that
  archive, and fails loudly if it does not rather than reporting a green it did not
  earn. Mutation-validated against the original implementation, which it fails on two
  assertions.

- **`roofline-*.ndjson` records now carry `pin_policy`, which standing order 9
  requires and they did not.** `bench.c` emitted it; `roofline.c` did not, though the
  runner sets the same `GBB_PIN_POLICY` for both. Only the `printf` was missing. That
  gap sat on the one instrument showing the t≥128 efficiency cliff most starkly —
  `peak_fma_allcore` per-core falls from **94.3% at t=96 to 53.1% at t=128 and 43.9%
  at t=192** — so for those records the applied binding policy was not in the record
  at all.

- **And the OpenMP place map, to kill a whole hypothesis class before an instance is
  launched to chase that cliff.** New: `omp_places`, `omp_place_procs`,
  `omp_place_procs_total`, from `omp_get_num_places()` / `omp_get_place_num_procs()`.
  `peak_fma_allcore` does no DRAM traffic, so page placement cannot explain its
  collapse — but if `OMP_PLACES=cores` enumerates **fewer places than there are
  threads**, threads double up on cores and per-core efficiency falls for a reason
  with nothing to do with NUMA. Invisible without the field, obvious with it, and it
  costs two lines. `omp_place_procs_total` is not redundant with the other two: places
  may be heterogeneous, so the count of places and the size of place 0 together do not
  answer "are there fewer hardware threads in the map than threads requested", and
  that sum is the number that does. All three are JSON `null`, not `0` or `-1`, in a
  non-OpenMP build: **zero places is a real and interesting value** — the runtime
  exposing no place list — and must not share an encoding with "this binary cannot
  answer".

- **A BLIS arm's `target` was a request with no read-back, which is the defect class
  standing order 10 exists for.** The first P2 pass shipped `target: "auto"` with
  nothing recording what `auto` resolved to, and that arm ran single-threaded large
  DGEMM at **0.35× OpenBLAS** — a figure that means misconfigured, not slow, because
  no threading is involved at one thread. `configure auto` on Neoverse V2 falling back
  to a generic arm64 sub-config is exactly a request landing somewhere other than
  where the label claims, and the manifest could not express it. Three changes:
  - `build-libs.sh` **chooses** the config from the host rather than deferring to
    `auto`: `armsve` where HWCAP reports SVE, `altra` for PART `0xd0c`, else the
    `arm64` family — each verified to exist as `config/<name>` in the checked-out tree
    before it is used, with a warning and a fall back to `auto` if not. `BLIS_CONFIG`
    overrides it explicitly and the override is logged.
  - `arm_record()` gains **`target_effective`**, filled from an actual runtime query
    (`bli_arch_string(bli_arch_query_id())`) by a probe compiled and run against the
    installed library. It defaults to JSON `null` and **never to a copy of `target`**:
    standing order 10's failure mode is a request echoed as if it were an observation,
    so defaulting the field to the request would build the mistake into the record. A
    probe that runs and cannot answer writes `"unknown"` plus a reason.
  - `decompose.py` section 5 reads it. `unknown` raises `target_readback_failed` — a
    read-back was attempted and failed, so the label is unverifiable. A value that
    merely *differs* from `target` raises `target_resolved_elsewhere`, which is **not
    a fault**: a family config resolving to a member is the normal thing and the whole
    reason the field exists, so the anomaly names the resolved config and says to read
    any deficit against that kernel set. Both are warning-level and neither sets an
    exit bit — the reference arm is not the campaign's subject, and a check that made
    an ordinary resolution fatal would be removed within a week, leaving the original
    defect undetectable again.

  Fixtures are a **pair**, because the interesting failure is a check that fires on
  everything: new scenario `target-readback` plants both cases and requires both
  anomalies by name, while `manifest-shapes` — whose BLIS arm carries `null`, the
  pre-existing shape and the state of four of the five libraries — asserts **silence**.
  Mutation-validated on four mutations: disabling the loop, killing either branch, and
  dropping the null guard. That last one is what the silent half buys: without it,
  `target_resolved_elsewhere` fires on every scenario in the suite.

  **Both halves were then run against real SVE silicon, and both were wrong in ways
  no fixture could have caught** — the probe is compiled and executed on the build
  host, so nothing short of building BLIS tests it. On `castor.local` (Cortex-X925,
  SVE2 at VL=128; instrument check, never data):

  - The probe **did not compile**, and the read-back therefore recorded
    `target_effective: "unknown"` — a failure reported honestly, and caused entirely
    by the flags used to ask the question. `blis.h` includes `bli_pthread.h`, which
    declares `pthread_barrier_t`; glibc hides that GNU extension behind
    `__USE_XOPEN2K`, which `-std=c11` switches off by defining `__STRICT_ANSI__`. Now
    `-std=gnu11 -D_GNU_SOURCE`, with `<blis.h>` and an rpath. This does **not** touch
    standing order 6: that order constrains the harness so the library under test is
    the only thing that varies, and this throwaway prints a string and exits — it is
    not measured and is not linked into any timed binary. With the fix the read-back
    answers **`armsve`**, confirming the chosen config is the one the library reports.
  - The run also **exposed a defect in the `target_effective` commit itself.** The
    final admissibility check for the DYNAMIC arm was one substring spanning four
    keys — `'"target":"DYNAMIC","coretype":null,"blas_sha":"[0-9a-f]*","built":true'`
    — so it was really an assertion about how `arm_record()` formats a line. Inserting
    `target_effective` *between* `target` and `coretype` made it stop matching, and a
    healthy build died with **"the DYNAMIC_ARCH arm did not build"** while the manifest
    one line above said `"built":true`. It fails closed, which is the survivable
    direction and exactly why it was worth fixing rather than tolerating: a fatal that
    fires on good builds is a fatal someone deletes rather than debugs, and this one
    guards the arm carrying the entire `OPENBLAS_CORETYPE` sweep. Now matched per
    field, which also lets the **count** be asserted — `grep -q` is satisfied by one
    line and says nothing about there being exactly one. Mutation-validated against
    the shipped manifest on five cases: the real one passes, `built:false` dies, a
    removed record dies as `found 0`, a duplicated record dies as `found 2`, and
    `DYNAMIC_OMP` alone does not satisfy it.

### Removed — affects what the report claims

- **BLIS is dropped from P3.** One config attempt was authorised and it is spent.
  On the first P2 pass (`c8g.metal-48xl`, Neoverse V2) BLIS from `configure auto` ran
  large single-threaded DGEMM at **6.1 GFLOP/s against OpenBLAS's 17.7 — 0.35×, at one
  thread**, then scaled perfectly linearly at a flat 1.33 GFLOP/s per core to t=96.
  Perfect scaling at a 4.6×-bad constant is a kernel deficit, not oversubscription, so
  the hypothesis was that `auto` had landed on a generic arm64 sub-config with no
  Neoverse kernels — and nothing in the record said which, because the config was
  never read back.

  The attempt: choose the config from the host and read it back at runtime
  (`blis_config_choose()` plus `bli_arch_query_id()`, both kept). The result, measured
  on an **instrument-check host and labelled as one** — `castor.local`, Cortex-X925
  SVE2 at VL=128, not Neoverse and not campaign data, quoted only because it is what
  tested the hypothesis: with `BLIS_CONFIG=armsve` and the read-back confirming
  `armsve`, so the request demonstrably landed, BLIS still came in at **0.228×
  OpenBLAS median over 136 paired t=1 dgemm cases** (0.230× large, range 0.197–0.257;
  best large dgemm 16.70 against 65.34 GFLOP/s). *Worse* than `auto` on c8g, not
  better. The misconfiguration hypothesis is tested and rejected as the explanation.

  Why removal rather than a second iteration. Section 1 measures every deficit
  against the reference arm, so **a misconfigured reference manufactures a deficit in
  every row** — worse than an absent one, and every one of those rows also read
  `UNVERIFIED`, since BLIS records carried `verified: null` before this release's
  verification work. With ArmPL present as the ceiling reference (mandatory for P3,
  enforced by `workload.sh`) BLIS's marginal analytical value is low, and it is also
  the cost lever: it ran **5–25× every other arm**, and the $2,942-versus-$591 P3
  spread *is* BLIS.

  Mechanically it is a declined arm, not a deleted one: `GBB_PHASE=p3` skips the
  clone and build and writes a manifest arm record with `built=false`, `runnable=true`
  and a stated reason (standing order 11 — absent and null are different claims). The
  P2 dataset still needs the config-read-back tooling to be read, so none of it is
  removed, and `GBB_BLIS=on` builds it anyway with the override logged. `gates/p3.sh`
  never referenced BLIS, so no gate changes.

- **Standing order 1's `peak_fma` headroom cross-check is retired.** Not weakened,
  not rethresholded: removed, and the report now says the empirical ceiling stands
  alone with no independent floor. The check existed for the one case the measured
  peak cannot see by construction — *every* arm on a host being bad, which moves the
  ceiling down with the arms and leaves both efficiency columns looking healthy — and
  on this hardware it cannot detect that case. `src/roofline.c` declares `peak_fma` a
  **lower** bound on purpose, because whether its accumulator array vectorises into
  NEON or SVE is the compiler's decision and standing order 6 forbids `-march=native`
  anywhere in the harness. Measured on `c8g.metal-48xl` at t=1: **4.22 GFLOP/s against
  a best large DGEMM of 18.16**, a ratio of 0.232. The flag fires above 1.15, so it
  could not fire at any plausible threshold — it was not a check that kept passing, it
  was an absent check reading as protection, and the case it was justified on was
  passing silently.

  **The alternative was considered and rejected.** Building `roofline.c` alone with
  `-O3 -march=native` would let the accumulators vectorise into SVE and make the bound
  tight enough to be exceeded. That breaks "the harness is compiled identically across
  every arm" (standing order 6, enforced by `gates/check-build-flags.sh`) and makes the
  campaign's only independent floor a function of gcc's vectoriser, in a campaign that
  has spent considerable effort removing exactly that class of dependency. Better no
  floor than that floor — and better a retirement stated in the output than a check
  quietly incapable of firing.

  Gone: `DEFAULT_HEADROOM_FACTOR`, `--headroom-factor`, `params.headroom_factor`, the
  `headroom` and `peak_fma_absent` anomalies, the section-6 `<-- headroom` flag, and
  the `headroom_ratio` / `peak_fma_status` fields — a `peak_fma_status` of `ok` asserts
  a check ran. Kept: `peak_fma` and `peak_fma_allcore` are still measured, still
  shipped in `roofline-*.ndjson`, still required as a file family by `gates/p2.sh`, and
  still printed in section 6 — now under a header saying they are provenance and **not**
  a cross-check, so a reader cannot mistake the surviving number for a surviving check.
  Also kept, and explicitly not retired with it: `IMPLAUSIBLE_GFLOPS_PER_CORE` and
  `sanity_check()`'s hard abort. Those guard standing order 2's optimizer hazard (927
  TFLOP/s on one core from a folded FMA chain), which is a different question from
  whether the number bounds anything. `roofline.c` was rebuilt and re-run after the
  comment change per standing order 2: 25.13 GFLOP/s f64 single-core at `-O2`, plausible
  and far below the abort bound. **That re-run was on the local dev host** — `arm64`,
  Apple clang 21, darwin 25.6.0 — **not** the campaign host, which is aarch64 gcc 11.5.0
  on Amazon Linux 2023 and produced the 4.22. So it verifies the chain is not folded
  (standing order 2's letter) without exercising the path that produced the number under
  discussion, and that is worth stating rather than leaving to be inferred. The 6× spread
  between the two is the closing argument for the retirement, not a caveat on it — and a
  *stronger* argument than an x86-vs-Arm gap would have been, because both hosts are
  aarch64, so the whole 6× is compiler and `-O` level. That is precisely the dependency
  that makes the bound worthless as a floor.

  Fixtures: `headroom` is replaced by **`peak-fma-retired`**, which plants the case
  that used to be the headline (`peak_fma` 1.5× the best GEMM, which no Graviton host
  at `-O2` produces) and asserts the report stays silent about it — no anomaly, no
  published `headroom_factor`, and `peak_fma` still printed and still labelled. A
  retirement can only be fixtured negatively, so the assertion *is* the silence.
  `peak-absent` is inverted the same way: absence is a provenance gap section 6 prints,
  not a check reported as "not performed". Both mutation-validated — reinstating the
  constant, the two anomalies and the payload field fails 3 of `peak-fma-retired`'s 6
  assertions and 2 of `peak-absent`'s 3, and nothing else in the suite. `HostSpec`'s
  `peak_factor` default moves from `1.06` to `0.23`, the measured value, so no fixture
  keeps implying `peak_fma ≈ best GEMM` is what real hardware does; the
  `denominator-intersection` scenario drops its now-vacuous `headroom` assertion rather
  than keep an off-topic expectation. Gates after: P0 50/0, P1 65 scenarios + 36
  checks / 0, `gates/p2.sh --self-test` 17/0, stubs 73/0.

### Changed — affects how a number is produced

- **`BLIS_REF` is pinned to a SHA instead of tracking `master`.**
  `061c2ebef87eda9189e6cdf38af4ea3d4a8efe7b`, read off the first P2 host's manifest
  rather than chosen — that is what its `master` resolved to, so the pin does not
  move the P2 dataset and P3 stays comparable to it. `master` was defensible while
  BLIS was "a reference arm, not the subject"; it stops being defensible at P3,
  where five hosts are built on five days and three passes run days apart, so the
  reference arm could differ between the passes whose agreement is the campaign's
  strongest evidence. A BLIS-vs-OpenBLAS gap that moved between passes would be
  indistinguishable from the effect the passes exist to test, and the p3 gate's
  `blas_sha` check is about OpenBLAS. An explicit `BLIS_REF=master` still only
  warns; `blas_sha_conflict` keys on `(library, target)`, so `blis/auto` carrying
  two SHAs across hosts is already flagged after the fact.

- **Warmup decays to zero at the expensive end, and the calibration call is reused
  as the first sample.** A large case cost seven calls — verify 1, warmup 2,
  calibration 1, samples 3 — and `ABS_MIN_SAMPLES = 3` rather than
  `MAX_MEASURE_SECONDS` is what floors that end, so the cap did not cap and three
  of the seven were overhead. Warmup's justification is OpenBLAS's *once-per-process*
  lazy buffer-pool allocation, which by `n=8192` has been warm for several hundred
  cases; it now runs only while it costs under `WARMUP_MAX_FRACTION = 0.02` of the
  measurement it precedes, which is self-scaling and adds no size threshold to keep
  in step with the regimes. **The naive form of this fix corrupts data**: the pool
  is per *thread*, OpenBLAS runs small problems single-threaded regardless of
  `OPENBLAS_NUM_THREADS`, and the ladder's first case recruits one thread, so
  "warm only the first case" moves threads 2..N's allocation into a timed region
  mid-ladder. An explicit `prime_threads()` at `PRIME_N = 1024` pays it outside
  every measurement and writes a `thread_prime` record. Calibration is **reused**
  (`_cal` becomes `samples[0]` when the case was unbatched and no warmup followed)
  rather than predicted from the previous ladder rung as suggested: same saving, no
  cross-case history dependence — prediction breaks wherever a rung is skipped,
  which the large cap now does — and `_cal` is the coldest call of the case, so
  reuse can only raise `t_min`/p50/p90 and never flatter an arm. Records carry
  `warmup_reps` and `cal_reused`; `gates/p2.sh` checks reuse's three preconditions
  per record, because reuse outside them is a flattered reading nothing else in the
  record looks wrong about. Measured against the pre-change binary on an Apple
  M-series host (dgemm ladder, two runs each): 58% of sweep wall clock removed,
  7.9% on the 136 cases common to both, GFLOP/s median −0.05% overall and −0.12% on
  the reuse path against a same-binary run-to-run spread of [−5.2%, +1.6%].
- **The large ladder is thread-dependent: `LARGE_CAP_LOW = 4096` below
  `LARGE_CAP_MIN_THREADS = 8`.** An `n=8192` single-threaded DGEMM answers no
  question the report reads — the large regime answers bandwidth and blocking
  questions and at 1 thread `n=4096` answers them — and it was the most expensive
  arithmetic in the campaign. The omitted cells have no hypothesis attached, and
  each writes a `case_skipped` record with a reason: standing order 11 at case
  granularity, because a cell absent from *every* arm at a thread point produces no
  cell at all and a census derived from the data cannot see it. The dry pass is
  deliberately **not** capped, so `matrix_id` still describes the design
  (`7c371fee324b7304` over 544 cases, unchanged) and a 1-thread stream pools with a
  192-thread one. The cap lives inside `sweep()`, which keeps it off the level-1
  cases built in `main()` — applied on `m` alone it truncated `ddot` at
  `n=4194304`, a 32 MB vector rather than a 512 MB working set, and the `incx-axis`
  fixture caught it by losing its whole non-unit-stride axis. **It touches standing
  order 1's denominator at low thread counts**, so `decompose.py` now reports which
  size the peak came from and out of how many (`at n=4096 of 3 size(s)`) and the
  policy question has been put to Scott rather than assumed. **Answered**: see the
  intersection denominator below. The annotation stays either way.
- **Standing order 1's denominator is the best large dgemm over the sizes the host
  ran at *every* thread count — the intersection, not each thread point's own best
  rung.** Scott's call, and the instruction was to change the policy rather than
  annotate it: a denominator drawn from a truncated ladder is not the same quantity
  as one drawn from the full ladder, and printing `at n=… of … size(s)` documents
  the inconsistency without removing it — a reader comparing 1-thread against
  192-thread efficiency would be dividing by two different ceilings with nothing in
  the arithmetic to stop them. On `c8g` the common size is `n=4096`. The
  restriction's cost is computed and printed per row (`the per-rung max was … not
  used: that size is absent at some thread point`) rather than assumed negligible,
  and the per-rung max is kept in the payload as provenance
  (`best_dgemm_unrestricted`). `peak_fma`'s headroom cross-check divides by the
  **same restricted** denominator: against the unrestricted one the ratio is
  smaller, so standing order 1's flag would fire *less* often than the published
  ceiling warrants, which is the wrong direction for a check whose job is to notice
  everyone leaving performance on the floor. A thread point with no large dgemm at
  all drops out of the intersection rather than emptying it — the alternative turns
  one dark rung into a host-wide loss of comparability, announced as an anomaly on
  rows whose own data is complete — and an empty intersection is reported as
  `denominator_not_comparable`, a `!`-severity anomaly, because an efficiency figure
  divided by a per-rung max looks identical to one divided by the common-size max.
  Fixtures: `denominator-intersection` (the per-rung max sits 8% above the common
  set, so the two policies give different answers) and
  `denominator-thread-point-dark`, both mutation-validated, and each caught by
  exactly one fixture and by none of the pre-existing 61.
- **`gates/p2.sh` section 3 now checks `measured + declined == matrix_cases`**, not
  `measured == matrix_cases`, and requires every declined case to carry a reason, a
  `thread_prime` record per stream, and `cal_reused` to appear only inside its
  preconditions. Five new mutants in `tools/p2-mutate.py` are the negative controls
  (`drop_record`, `blank_reason`, `forge_reuse`), and one of them found a real
  defect in the gate: `case_skipped` records carry a routine, so the arm census
  counted a stream that declined cases and measured nothing as *present* — which
  would have let the mandatory generic-`ARMV8`-at-1-thread requirement be satisfied
  by an arm that produced no numbers.
- **The spend policy's figures are struck rather than adjusted, all three of them.**
  `~$96/pass` described the pre-expansion table; **30–37 instance-hours per pass**
  was `18.6 h × ~1.8` where the 18.6 h rested on the `MAX_REPS`/156-measurements
  model that `6a8089f` deleted, and scaling a dead number does not revive it; and
  **$500–650 for three passes** was derived from the 30–37 h, so it went with it.
  The same edit strikes the **1005 cases / 6.4×** projection, which was computed
  against the 156-case table and whose own breakdown had begun to collide with
  today's total — the count is now read off the producer's dry pass (544 cases)
  rather than off `CLAUDE.md`. The only live figure is arithmetic over the current
  constants for the authorised one-host P2 launch, $61–107, and it is to be replaced
  by a measurement rather than multiplied into a campaign total.

### Changed

- **The spend policy now states how a pass's cost is summed: per rung, never per
  stream.** A stream is not a unit of cost, and the 8–14 instance-hour band posted to #3
  before the first P2 pass priced all eight thread points at roughly the head-of-ladder
  cost (`~9.6 h` over `~96 streams` — 6.0 min each, near the measured `t=8` figure), and
  counted the eight roofline streams, which cost seconds, as streams. Measured on that pass, one
  `openblas/DYNAMIC` stream costs **7.2 min at t=1, 6.6 at t=8, 4.1 at t=16**, because
  small, medium and level-1 are `MIN_SECONDS`-floored and therefore flat in thread count
  while large is `ABS_MIN_SAMPLES = 3`-bound and scales. The estimate is `Σ over thread
  points (per-rung stream cost × streams at that rung)`, each per-rung cost measured
  rather than derived. The term that stops the total collapsing is counter-intuitive and
  is called out: **t=8 is only 9% cheaper than t=1, not half**, because the
  thread-dependent large cap lifts at `LARGE_CAP_MIN_THREADS = 8` and the large ladder
  gains two rungs, so large costs *more* at t=8 (5.20 min) than at t=1 (4.60). That term
  is why the correction is ~1.4–1.8× rather than the 3–4× a straight "later rungs are
  cheaper" reading suggests. Progress is to be reported as elapsed-time fraction, not
  streams done — 17 of 88 streams read as 19% by stream count and 24–34% by time, an
  understatement, because the remaining streams are the cheap ones. Per-rung
  costs are host-dependent (`c6g`/`hpc7g` have no 192-thread rung at all), so P3
  re-derives them per host rather than scaling `c8g`'s. **No cost figure is added to
  `CLAUDE.md`**, per its own rule on the three struck figures; the method is written down,
  the numbers stay in #3.

  With five more rungs measured the model resolves into **two terms, `flat + large(t)`,
  and only the second needs measuring per host.** `flat` (small + medium + level-1) is
  `MIN_SECONDS`-floored and measured flat: **1.35 → 1.23 min/stream from t=8 to t=96,
  −9% across a 12× thread range**, of which medium is ~75%. `large` is
  `ABS_MIN_SAMPLES`-bound: 5.20 → 1.04 over the same range. So `flat × (arms × rungs)` is
  derivable for a host that has not been launched and only `large` has to be measured
  there — with one check first, since `flat` is host-independent only where the floor
  binds, and `c6g` is Graviton2. The cost table in `CLAUDE.md` now carries a **rungs**
  column and an instruction to read it before the `large` column, because t=8 costing more
  than t=1 (5.20 against 4.60) is **different work, not slower work** — the cap lifts at
  t=8, so t=8 measures `n=6144` and `n=8192` and t=1 does not. Without that column the
  inversion invites a threading explanation that does not exist.
- **`LARGE_CAP_MIN_THREADS = 8` is validated by measurement and marked must-not-raise.**
  Scott asked whether the "a 1-thread n=8192 DGEMM answers no hypothesis anyone will cite"
  argument extends to 8 of 192 cores, which would cut a large fraction of P3. Checked
  against the first P2 pass; the answer is no on all three grounds. (a) The premise holds
  for GEMM and fails elsewhere: at t=8 `dgemm`/`sgemm`/`dsymm` are flat across
  `n=2048…8192` to 0.4–0.7%, but `dsyrk` spreads 7.3% with `n=8192` the max, `dgemv`
  14.0%, and the `>4096` lift grows monotonically with thread count for every non-GEMM
  routine (`dsyrk` +4.2% at t=8 → **+20.6% at t=96**; `dtrsm` +1.2% → +11.0%) — so the
  cells the cap would remove are where TRSM/TRMM/SYRK are still climbing, and
  TRSM/TRMM/SYMM is the family the C11 false negative was confined to. (b) The saving is
  ~$9 per host per pass against the existing t<8 cap's ~$34, because `n=6144`+`n=8192` at
  one thread cost ~24 min/stream against a whole t=1 stream's 7.22. (c) t=1 is the only
  rung where truncation is free and the data says so — worst t=1 spread is 3.2%
  (`dgemv`). A routine-aware cap would recover ~71% of the saving without the data loss
  (`gemm/symm` is 70% of the `>4096` cost at t=8) and is **explicitly not implemented**:
  it moves `matrix_id`, so this pass would stop pooling with P3's, for ~$9 a host.
  Also recorded: `decompose.py`'s own `denom_restriction_cost` for the intersection
  denominator, which rises with thread count and stays about 1% (0.000% at t=1 to
  **1.240% at t=96**, where the unrestricted max would be 1708.28 at `n=6144` against
  1687.35 at `n=3072`). Read off the report, not computed by hand.
- **Section 1's reference arm is chosen once per host, not per comparison group.**
  The last of the count-derived-selection defects and the worst of them: the group
  key carries the regime, so on a host with two reference candidates whose coverage
  differs by regime the choice could flip *inside* one comparison — a count-derived
  selection moving a count-derived consequence, where the consequence is section 9's
  "deficit concentrated in the small regime", the line that says which kernels to
  fix. Section 4a keys on `reference_arm`, as it must, so a flip split the profile,
  nulled `small_minus_large` in both halves, and surfaced as `MISSING: regimes` —
  indistinguishable from thin coverage. Per host is the only scope invariant to all
  four axes the report compares along (regime, routine, thread count,
  pad/transposes). The scope is declared in the payload as `reference_scope` so it
  can be asserted rather than eyeballed; the tie-break is coverage breadth, then
  conditions, then `arm_label`, which is deterministic and cannot reorder between
  passes; and where the chosen reference produced nothing the row is an explicit
  `NO DATA — this host's reference arm … produced nothing here (status: reason)`,
  naming it, because an absence with a reason beats a silent substitution. Fixture:
  `reference-regime-flip`, mutation-validated against the per-group selector, and it
  plants the coverage so the conditions-winner is *not* the alphabet-winner —
  otherwise it would pass against a selector that read only the label.
- **Every threshold in `analysis/decompose.py` that was a fraction of raw cells
  is now either balanced-weighted or an absolute count.** Two defects found
  separately — cell-count majorities, so the longest size ladder voted, and
  `--max-nodata-fraction` at 34% while `dgemm`'s total exclusion moved from 40%
  of the cross to 29% purely by densifying the ladders — were one root cause:
  *a quantity defined as a fraction of cells is coupled to ladder density*. Both
  were latent from the start and only became reachable when the denominator
  moved, and items 3–5 of the #2 expansion move it again (transposes multiply
  `dgemm`'s cells by four), so the class was swept rather than the instances
  patched. `balanced_weights()` is now the single weighting rule: one unit per
  `(routine_family, regime)` group, split evenly among the routines in the group
  and then among each routine's cells. Pads, transposes and `incx` are
  deliberately *not* layers — they are the same hardware claim re-asked at a
  different alignment, not independent votes. Anything left unchanged by the
  sweep is density-invariant by construction.
- The coverage guard gained an **absolute half**, because no threshold on a
  fraction can express "one whole family of the design was not measured": a
  share can always be diluted by densifying elsewhere. `verdict.dark_groups`
  counts `(family, regime)` groups in which nothing was measured at all, and one
  of them refuses a directional verdict outright. Dark is measured against data
  (`n_sizes > 0` on an admissible host), not against a verdict — a group that
  compared and came out thin or split is inconclusive, not dark. The level-1
  ladder puts exactly one length in the medium regime, so `(axpy, medium)` and
  `(dot, medium)` are permanently thin by construction and must not read as holes.
- The majority comparison is now **exact rational arithmetic** rather than a
  float comparison with a tolerance. Balanced weight is a sum of reciprocals of
  integers, so it is exactly rational: `fractions.Fraction` accumulates it with
  no ordering sensitivity, and `as_exact()` reads a threshold as the decimal it
  was written as. This is not cosmetic — making the left side exact is precisely
  what breaks a float threshold, and it breaks it in a direction that depends on
  the threshold rather than on the data. Both of the campaign's own defaults sit
  exactly on a reachable boundary and they fall opposite ways against a float:
  `Fraction(3,5) >= 0.60` is **true** (binary `0.6` rounds down, so exactly
  three fifths clears it) while `Fraction(17,50) >= 0.34` is **false** (binary
  `0.34` rounds up, so exactly 34% does not). No epsilon is right for both, which
  is the argument against having one. `MAJORITY_EPS` is gone, and `gates/p1.sh`
  section 3 asserts it stays gone.
- The **effect-size floor stays on the raw median, on purpose**, and now says so.
  The directional branch asks two deliberately different questions — "how much of
  the design moved" (balanced) and "did the work move" (raw) — and weighting both
  collapses them into one question asked twice, which makes `MIXED` unreachable:
  the `family-swamped` fixture, a 22% effect on three of five families, then reads
  as a global `V1-SET-AHEAD`. The balanced median is kept as a diagnostic and
  printed where the two diverge, since a gap between them means the effect is
  concentrated in whichever routines have the longest ladders.
- The timing floor is part of the comparison key (`canon_floor()`), so the same
  `(routine, size)` measured at 0.05 s and at 0.30 s cannot collapse into one
  cell with min-within-run keeping whichever floor looked worse. Absent means the
  legacy 0.30 s, so pre-per-regime data is read unchanged.
- `transa`/`transb` are now in the **coverage census** cell key, not just the
  comparison key. An arm that ran NN and never ran TN at all was recorded as
  `partial` — "some sizes of this cell are absent" — where the truth was a whole
  missing copy kernel, which is what a SIGILL in `dgemm_tcopy` on a cross-built
  arm looks like. Standing order 11 turns on that difference.
- `tools/synth.py` now emits `min_seconds` per regime the way `src/bench.c` does.
  It is part of the comparison key, so a fixture that omitted it keyed every
  record at the legacy floor and no fixture exercised the small floor at all.

### Added — affects how a number is produced

- **`scripts/workload.sh`: the campaign pass payload is in-tree, and it refuses a
  pass before spending on it.** P2 pass 1 ran from a hand-written script in `/tmp`.
  P3 is fifteen passes — five hosts × three — launched days apart, and CLAUDE.md
  already names the failure: *"a hand-driven or newly-written procedure drifts
  between launches… a drift between passes is indistinguishable from the effect the
  passes exist to test."* This is the payload spawn runs, not a launcher: it creates,
  tags, waits on and terminates nothing, and the prohibition on `scripts/launch.sh`
  is untouched — the lifecycle stays with truffle/spawn, which is already exercised.

  Three preflight assertions, all of them **before** `build-libs.sh`, which is the
  whole point:

  - **ArmPL is acquired, not discovered.** `scripts/install-armpl.sh` existed and
    nothing called it. The payload now runs it first, gated on the operator having
    set `GBB_ARMPL_ACCEPT_EULA=1` — the script still never accepts the licence, and
    neither does this one. `GBB_PHASE=p3` makes a missing ArmPL **fatal**, which is
    CLAUDE.md's rule (*"ArmPL absent is admissible for P2 and not for P3"*) turned
    into a refusal that costs ~2 minutes instead of a census gap that costs a
    six-hour sweep. `GBB_PHASE=p2` records it as an explained absence and proceeds,
    which is exactly what pass 1 did.
  - **`GBB_EXPECT_HEAD` pins the harness commit.** Three passes off a moving `main`
    are three different harnesses and nothing in the dataset would say so. Unset is
    allowed for P2 and fatal for P3: *"it was main at the time"* is not a pin.
  - **Log paths are namespaced per host and per pass.** The P2 payload shipped to
    `gbb/logs/run.log` flat. Correct for one host; for fifteen it overwrites
    fourteen times, and the run log is the only account of what a pass did.

  `tests/workload-preflight.sh` (23 assertions, wired into CI and `gates/p0.sh`
  §5d) asserts both halves of each refusal: non-zero exit **and** that
  `build-libs.sh` was never reached. The second half is load-bearing and
  mutation-validated — moving the ArmPL gate to after the sweep leaves every
  exit-code assertion passing and fails exactly the three "did not build/sweep"
  ones. `GBB_COMPLETE_MARKER` exists only so the suite can assert that completion
  is signalled without signalling it; touching the real `/tmp/SPAWN_COMPLETE` would
  terminate whatever instance the test ran on.

- **`GBB_ARMPL_MIRROR` takes the vendor CDN off P3's critical path.** An s3:// prefix
  or a local directory, checked before the CDN and populated from the first fetch that
  passes the pinned digest — and given no more trust than the CDN, since the digest
  check is the same one either way. Fifteen 1.0 GB pulls from a registration-gated
  vendor permalink is fifteen chances for the pin to do its job by *aborting a
  spend-authorised pass*; mirroring once means all fifteen read identical bytes.

- **`scripts/diag-numa.sh`: is the t≥128 cliff the memory policy or the hardware?**
  `pin_for()` derives the memory policy from the thread count, so on a 2×96 host it
  switches `--membind=<node>` → `--interleave=0,1` at exactly t=128 — and two roofline
  numbers fall off a cliff at precisely that rung (triad 368.5 → 133.9 GB/s, allcore
  `scaling_efficiency` 0.942 → 0.531). 13 roofline cells × 2 reps and 3 full-matrix
  bench cells vary the policy independently of the thread count, including the two
  decisive controls: t=96 on one node under `--interleave=0,1` (policy varied, hardware
  fixed) and 96 threads spanning both sockets under the same policy (span varied,
  thread count fixed). Quarantined by construction, three ways: `GBB_ROLE=diagnostic`,
  which `decompose.py`'s `load()` drops before the shape dispatch and reports as
  `role_excluded`; a `diag-numa-*` run_id namespace; and a `gbb/diagnostics/` S3 prefix.
  It deliberately does not go through `run-matrix.sh`, which would correctly derive
  `role=campaign` on a campaign host.

- **Every bench record now carries `case_seconds`, the wall clock that case cost.**
  The spend policy's remaining unknown is the expanded matrix's wall-clock
  multiplier, `MIN_SECONDS` being per-regime moved it again, and the README's
  standing instruction — *instrument the slowest arm of the first P2 iteration,
  never a representative one* — had nothing in the record to read. `reps`, `batch`
  and `calls` describe the timing loop, not the case: they omit allocation, fill,
  verification and calibration, and the per-arm total was recoverable only from log
  timestamps that the per-arm S3 shipping path does not preserve. `case_seconds` is
  the interval from the previous record's emission to this one's, so a sum over an
  arm's records reconstructs its sweep wall clock exactly, and a sum over a subset
  answers which *sizes* cost the multiplier rather than only what the total was.

  Three choices in it are load-bearing:

  - **The clock is read before the `printf`, not after the `fflush`.**
    `run-matrix.sh` consumes stdout through a pipe, so a value taken after the
    flush charges the consumer's backpressure to the case and the cost model starts
    tracking how fast S3 was that day rather than how slow the arm was.
  - **The interval starts after the dry pass and the timer calibration**, inside
    the same `if (!g_dry)` block that stamps `matrix_id`. Starting it at process
    start would put a fixed launch cost into a per-case number that then gets
    multiplied by the case count. `gates/p1.sh` scopes its assertion to that block
    rather than searching the file, because a `g_last_emit = now()` at the top of
    `main()` satisfies a whole-file search and is exactly the defect.
  - **`tools/synth.py` models it, and the model is declared as one.** Reproducing
    `TIMED_LOOP` in Python would be a second copy of the timing policy, and the
    copy is what drifts. What the model does reproduce is the one property the cost
    plan turns on: below `min_seconds / MIN_SAMPLES` per call a case costs the
    floor and no more, above it the case cannot finish before `MIN_SAMPLES` samples
    of one call each, so the **slower** arm costs strictly more wall clock for the
    same measurement. Gate section 2d checks that property on the fixtures
    themselves — 227 arm pairs in the `null` scenario ordered by speed, zero
    inverted — because a fixture set in which every arm cost the same would be
    passed by a cost analysis that took the *first* arm instead of the slowest,
    which is the single mistake the instrumentation exists to prevent. The
    floor-overlap probe is priced against the floor each pair member ran under
    rather than the size's regime default; both members are the same size, so the
    regime default would report the two halves of the band costing the same.

- **Every bench record now carries `matrix_id` and `matrix_cases`, and more than
  one `matrix_id` in a results directory refuses the analysis outright.** P2 runs
  pre-expansion and P3 runs after items 3–5 of #2 land, so the two passes sweep
  different case sets — and the way they end up in one directory is one
  `aws s3 sync` of a bucket holding both prefixes, which is an operation this
  campaign will actually perform. Pooled, cells present in one matrix and absent
  from the other drop out of every intersection silently, and what survives is
  whatever the two happen to share: a number that looks like every other number in
  the report and means something else. This is the pass-intersection rule's blind
  spot, because that rule reasons about *arms* within a comparison and has no way to
  notice that the comparison's own case set moved.

  `src/bench.c` computes the id in a **dry pass over the same tables the sweep
  walks**, before any measurement: `sweep()` and `run_level1()` each fold their
  cases, and the id is the sum of per-case FNV-1a digests. Decisions worth keeping:

  - **A digest, not a version number.** A version number records what someone
    remembered to bump. Five case-set changes were checked during development — one
    extra size, one extra pad, one extra routine, a floor change, an `incx` change —
    and *two of them left `matrix_cases` unchanged while moving the id*, which is
    why the count sits beside the digest as a legible cross-check rather than being
    the mechanism.
  - **Summed, not XOR-ed.** Two identical cases XOR to nothing, so a duplicated
    case would be erased by the field whose job includes exposing it.
  - **The dry pass ignores the `--routine` filter**, so one arm's partial run
    carries the same id as the full sweep. The id describes the matrix the binary
    sweeps, not what this invocation measured.
  - **A routine in a sweep list that `sweep()` cannot dispatch is now fatal**
    (`exit(5)`, censused `harness_invalid` by `run-matrix.sh` with its own reason
    rather than `runtime_failed`'s SIGILL hint, which would send someone auditing
    the ISA of a host that is fine). Found by mutation: the dry pass folded 31 cases
    the real pass silently skipped, so the id would have claimed measurements that
    were never taken. That is worse than no id at all.
  - **Exit bit 64 is returned alone.** `decompose.py` refuses before section 0 and
    computes nothing, printing the breakdown by id with each id's run and instance
    ids on stderr. A refusal that still emitted a cross would hand over exactly the
    pooled table it exists to prevent, behind a non-zero exit nobody reads.
  - **`unstamped` is one group, not a wildcard.** A dataset written before the field
    existed still analyses, because all of its records agree with each other. A
    dataset *mixing* stamped and unstamped records is refused, because whether the
    two swept the same cases is precisely what no record says.
  - **`tools/synth.py` deliberately does not reproduce the digest.** Its ids live in
    a `synth-` namespace, so a fixture id cannot pass for a measured one in a report
    or in a bucket, and no fixture asserts a hand-copied C hash — a drift between
    the ladders would otherwise surface as fifty scenarios failing on an opaque hex
    value instead of as the `ladder_check`s naming the ladder that moved. What the
    gate asserts instead is the *property*: the id follows the case set and nothing
    else, checked by moving the case set.

  Four fixtures, each mutation-validated: `matrix-stamped`, `matrix-mixed`,
  `matrix-unstamped`, `matrix-mixed-unstamped`. Removing the refusal kills the two
  mixed ones; treating `unstamped` as a wildcard kills the two unstamped ones. Nine
  gate mutations — a producer that stops folding, either enumeration; the dry pass
  removed; the abort downgraded; the printf order drifted, format or arguments; the
  stamp never formatted; the namespace removed; the `rc=5` census lost — each fail
  `gates/p1.sh` with its own message. Not comparability-affecting: no measured value
  changes. The pre-expansion matrix stamps as `7c371fee324b7304` over 544 cases.

- **A timing-floor overlap band, because the per-regime `MIN_SECONDS` put a change
  of instrument at the size where the answer is expected to be.** The floor steps
  from 0.05 s to 0.30 s at n=256, and n=256 is also where `GEMM_SMALL_*` is
  hypothesised to hand over to the blocked kernel. A step in section 4's regime
  profile at n=256 was therefore ambiguous between "the fast path ends here" and
  "the averaging window changed here", the two predict the same picture, and nothing
  in the data resolved them after the fact — on the one section whose whole job is
  locating the effect in the size range. Moving the transition to n=512 would have
  separated them by assumption. This separates them by measurement, for about
  1.75 s per arm.

  `src/bench.c` grows `run_floor_overlap()`, which re-measures `dgemm` at
  n ∈ {192, 224, 256, 320, 384} at *both* floors and tags those records
  `probe: floor-overlap`. `decompose.py` grows section 9, which pairs them and
  reports `AGREES`, `AGREES-WITH-BIAS`, `DISAGREES`, `ORDER-CONFOUNDED`,
  `INCOMPLETE` or `ABSENT`. Section 4's title and the small-regime `CONSEQUENCE:`
  line now both point at it, and anything but the first two statuses sets the new
  **exit bit 32** plus a hard section-5 anomaly.

  Five decisions in it are load-bearing, and each is held by a mutation-validated
  fixture:

  - **The probe records are partitioned out of the cross before `build_cells()`,
    on the tag and not on the floor.** A probe record is the same condition as a
    matrix record bar `min_seconds`; left in the cross, min-within-run would have
    silently kept whichever of the two read faster. The floor in the comparison key
    is the fail-safe underneath, not the mechanism, and `split_floor_probe()`
    fail-closes on an unrecognised tag rather than treating a future probe as
    matrix data. Neutering the split kills all six band fixtures.
  - **The band must straddle the transition.** If every band size fell in one
    regime, both members of every pair would carry the same floor, every delta
    would be zero, and `AGREES` would be reported having compared nothing with
    nothing — a vacuous confirmation, indistinguishable downstream from a real one.
    `gates/p1.sh` asserts the straddle separately from asserting the sizes, because
    moving the band in `bench.c` and `synth.py` together passes the value check.
  - **`bench.c` alternates which floor runs first**, by size-index parity, and
    records the position. With a fixed order, "the first one reads high" and "the
    short floor reads high" are the same dataset, so a thermal or cache drift would
    have been reported as a floor bias and someone would have been sent to change
    `MIN_SECONDS` over it. The alternation is the only thing that makes
    `ORDER-CONFOUNDED` reachable.
  - **A *signed* bias above `--min-effect` is `DISAGREES` even when every pair sits
    inside its own band.** `band_for()` is adaptive — `max(min_effect, dispersion)`
    — so a dispersed cell gets a band wider than the reporting floor and a bias
    underneath it is invisible to a band test however large it is in reportable
    terms. `floor-band-bias-past-floor` is that case: a 10% bias on an arm whose
    band widened to 20%, `outside_band == 0`, and still a failure. Without the
    branch it reads as a footnote.
  - **`ABSENT` deliberately does not set bit 32.** Every result set produced before
    the probe existed has no probe records and must keep analysing exactly as it
    did. Requiring the probe to be *present* is `gates/p2.sh`'s job. `INCOMPLETE` —
    records present, not one complete pair — *is* a failure, because something
    produced half a probe.

  Six fixtures: `floor-band-agrees`, `floor-band-biased`, `floor-band-disagrees`,
  `floor-band-order-confounded`, `floor-band-bias-past-floor`, `floor-band-half`.
  Not comparability-affecting for the matrix — no matrix record changes value — but
  every bench record gains a `probe` field, and the runner's per-arm `records` count
  now includes the ten probe lines, deliberately: it answers "how much did this arm
  emit", and the probe is emitted.

### Fixed — census

- **The coretype-alias guard refused the arm it exists to detect, on real hardware.**
  `alias_ok()` in `scripts/run-matrix.sh` declared only `NEOVERSEV2:neoversen2`, and
  cc3fc1e takes the other direction: `gotoblas_NEOVERSEV2` is `#define`d to
  `gotoblas_NEOVERSEN2` unconditionally (`driver/others/dynamic_arm64.c:229`, outside
  every `DYN_*`/`NO_SVE` branch, with no `extern` for a V2 table), and
  `gotoblas_corename()` tests V2 (`corename[12]`) *before* N2 (`corename[13]`) on that
  single pointer — so it can never report `neoversen2`, and a `NEOVERSEN2` request
  reports back `neoversev2`. The guard read that as "request NOT honoured" and censused
  the arm `unrunnable`, when `force_coretype("NEOVERSEN2")` had returned exactly the
  table asked for (`found=13`). `c8g.metal-48xl` did this on 2026-08-20. Both
  directions are now declared, so the N2 request is censused `alias_duplicate` —
  standing order 11 at arm granularity, and the two statuses support opposite
  conclusions about gate P4's question: "V2 and N2 are the same kernel set on this
  build, which is the finding" versus "the forced-N2 arm could not be measured here".
  **No measurement changes**: the same arms run either way, since the V2 arm was
  already measuring that table. What changes is what the census claims about the arm
  that did not run.

  Three fixture sites carried the same inverted assumption and are corrected with it,
  none of them moving a set or a status: `tests/run-matrix-stubs.sh` scenario M
  asserted the N2-reporting direction as "the real 0.3.32 behaviour" and therefore
  could not catch this; `analysis/decompose.py` justified keeping `aliased` out of
  `CENSUS_SUCCESS` by calling it the expected path on the campaign's own hosts (the
  exclusion is right — `aliased` is written *before* the arm runs — but the rationale
  was false, and a later reader could have moved it on the grounds that the status is
  now unreachable); and `tools/synth.py`'s `aliased-coretype` said the same. Both
  directions stay declared and both stay fixtured, because corename()'s check order is
  an implementation detail OpenBLAS owes nobody.

- **The first P2 pass predates the fix above, and carries a dataset note saying so.**
  `docs/dataset-notes/20260820T031023Z-ip-172-31-36-19-census.md`, uploaded to that
  run's S3 prefix as `NOTE-census-…md` and **required in any release artifact that
  publishes the run**. Run `20260820T031023Z-ip-172-31-36-19` was already sweeping on
  `9048a2b` when the guard was fixed, so its census carries `unrunnable` for the
  `NEOVERSEN2` arm with a reason string asserting that `force_coretype()` ignored the
  request — a status the code can no longer emit for that arm, and a mechanism claim
  that is simply false. The pass was **not** re-run (a fresh on-demand sweep, and no
  measurement would change) and the shipped census was **not** edited: rewriting a
  record after it shipped destroys the only evidence of what the harness did. The note
  quotes both records verbatim, gives the cc3fc1e mechanism, and states what does not
  move — `NEOVERSEN2` produced zero bench records under either status, and both
  statuses sit outside `CENSUS_SUCCESS` carrying a reason, so the arm is an *explained*
  absence in both vocabularies and `gates/p2.sh`'s zero-`MISSING-UNEXPLAINED`
  requirement is met as shipped (verified: `coverage.by_arm` buckets all 110 of that
  arm's cells under `unrunnable`). What differs is the `by_status` bucket, so a reader
  diffing this pass's census against a P3 pass's will see one arm move, and that move
  is this note.

  The note now also carries **the build-system half of the identity, which is what makes
  `alias_duplicate` a correct description rather than a charitable one.**
  `kernel/arm64/KERNEL.NEOVERSEV2` at cc3fc1e is one line and 39 bytes — `include
  $(KERNELDIR)/KERNEL.NEOVERSEN2` (verified against the pinned SHA, not assumed). There
  is no V2 kernel table, so the two names select a byte-identical kernel set *before* any
  runtime dispatch. That converts "we shipped a wrong reason string on a failed arm" into
  "the arm was always redundant and the new status says so": declining it lost no kernel
  set, because the surviving `NEOVERSEV2` arm measured the same one. The note states the
  identity at all three levels it holds at — makefile, the `#define`, and the read-back.

  And it records **the one place standing order 10's assert-what-you-forced discipline is
  blind**, as a known limit rather than something to be discovered later: `gbb-coreprobe`'s
  read-back cannot distinguish `NEOVERSEN2` from `NEOVERSEV2`, because both requests report
  `neoversev2`. It is blind harmlessly *only* because of the makefile fact — there is one
  table, so there is nothing to fail to distinguish. **The harmlessness is a property of
  the pinned SHA, not of the mechanism**: an OpenBLAS that gives V2 its own kernel table,
  or reorders `corename[]`, makes the read-back load-bearing on this pair overnight and
  makes `alias_ok()`'s declaration wrong in the dangerous direction — a request landing on
  a different table than the label claims, which is exactly standing order 10's "plausible
  wrong answer". Also recorded in standing order 10 itself, so it is read before the next
  coretype is added rather than after. Re-derive against the pinned SHA; do not assume it.

- **`gates/p2.sh --self-test` rehearsed against one more arm than a real pass can
  contain.** The `p2-host` fixture planted `NEOVERSEV2` *and* `NEOVERSEN2` as fully
  measured arms — a shape `run-matrix.sh` cannot emit, since the second request is
  declined. Six coretypes requested, five measured, and the fixture now says so. New
  `alias-duplicate` scenario carries the claim on a minimal arm set: `alias_duplicate`
  is an explanation (so the cells the declined arm did not fill are explained absences,
  the opposite of `aliased`), the surviving arm is the one labelled `NEOVERSEV2`, and
  the by-coretype half of the central V1-vs-V2 cross is still populated by it. New stub
  scenario O covers the runner half. Both mutation-validated: adding `alias_duplicate`
  to `CENSUS_SUCCESS` fails `alias-duplicate` and `p2-host` and nothing else; removing
  the `NEOVERSEN2:neoversev2` alias entry fails three of scenario O's assertions.

### Fixed — gate

- **`gates/p0.sh` §7 checked a hardcoded list against the tree, which rots in exactly
  the way the section exists to catch.** Its own comment says a suite *"that exists in
  the tree but is not wired into CI rots silently"* — and the enumeration of what to
  check for was itself hand-maintained, so adding a suite and forgetting to add it to
  the list left the check green while the suite was unwired. The list is now derived
  from `find gates tests -name '*.sh'`, which fails safe: a new suite is required in CI
  by default. `NOT_IN_CI` names the gates that cannot run on a clean clone because they
  need a collected dataset (`p2`/`p3`/`p4`), individually rather than by pattern, so a
  future gate is not exempt by accident. Found while wiring the first new suite since
  that list was written.

- **`gates/p1.sh` could go green on code that was not on disk.** Section 2 loads
  `tools/synth.py` and `analysis/decompose.py` with
  `importlib.util.spec_from_file_location`, which writes `__pycache__` and
  validates it on `(mtime, size)`. A restore that lands a same-size file whose mtime
  the cache still considers current makes every assertion in that section read the
  *bytecode* instead of the file. Found while mutation-validating the band checks: a
  restored `src/bench.c` kept reporting the mutated band, and the failure presents
  as the gate agreeing with a file it never read. The gate now exports
  `PYTHONDONTWRITEBYTECODE=1` and removes any existing cache rather than trusting
  it. A green gate that measured the wrong artefact is the worst outcome available
  in this repository.

### Added

- **`scripts/install-armpl.sh` — the reference arm becomes reproducible.** ArmPL was
  absent from the first P2 pass because acquiring it was a manual,
  registration-gated step; the census recorded the absence honestly, but a manual
  step discovered at launch time is discovered on five hosts across three passes, and
  the campaign's framing is OpenBLAS against what the silicon can do. **Pinned by
  content, not by URL**: the Arm CDN permalink is stable by name and says nothing
  about what it returns, so the tarball is checked against a sha256 recorded per
  package family (rpm and deb are different files) and the install aborts on
  mismatch. Both digests were verified by independent download and agree with the
  digests Spack publishes for the same version — two independent sources, which is
  the most a vendor binary admits of. **The EULA is accepted by a human**: the script
  refuses to run without `GBB_ARMPL_ACCEPT_EULA=1`, and prints where it left the
  licence text. `ARMPL_DIR` is **discovered** from the install rather than guessed —
  the directory name carries the GCC version, which is part of the provenance
  `build-libs.sh` records, and a guessed path either fails at link time or, worse,
  exists and holds a different build. Everything conversational goes to stderr so
  `--print-dir` emits exactly one path. Verified end to end on aarch64 Linux
  (download, digest, install, discovery, `make armpl`, `ldd` resolving
  `libarmpl_mp.so` through the rpath), on an instrument-check host and not on a
  campaign host.
- **`gates/p2.sh`, and a self-test that makes it a gate rather than a wish.** P2 is
  the first phase whose gate is written before its data exists, so the ordinary
  failure mode is a gate that runs for the first time on the dataset that cost
  money and passes it by not checking anything. So the gate ships with
  `--self-test`: `tools/synth.py`'s new `p2-host` scenario writes a clean
  `c8g.metal-48xl` pass (one `instance_id`, 30 arm × thread streams, 311 cases
  each, floor band agreeing), the gate must pass it, and then
  `tools/p2-mutate.py` plants one defect at a time and the gate must go red **and
  name the field**. Ten mutants: the `ARMV8` arm gone; that arm present but not at
  1 thread; the floor-overlap band gone; `matrix_id` gone; one arm's ladder
  truncated; `env-*.json` gone; `topology-*.txt` gone; a second `instance_id`;
  `case_seconds` gone; the whole pass restamped as `c7g.metal`. The naming half is
  what earned its keep — three mutants went red with an *empty* message (a
  `KeyError` in the wall-clock section, and a report printed only on the success
  path), and one went red citing a count without saying which field declared it.
  A gate that has stopped saying what is wrong is the one thing a gate is for.

  Four requirements the CLAUDE.md gate row does not name are also enforced,
  because they came from the re-sequencing decision rather than from the original
  table: exactly one stamped `matrix_id` with every stream's distinct case count
  equal to `matrix_cases`; an OpenBLAS arm *reporting* `coretype ARMV8` with
  records at `threads == 1` (standing order 10 — reported, not requested — and the
  campaign's most expensive single arm, so it anchors the P3 extrapolation); the
  floor-overlap band present and `AGREES`, since `ABSENT` deliberately does not set
  exit bit 32 and so requiring the probe is the gate's job; and the wall-clock
  accounting printed, naming the slowest arm with its per-regime ms/case split.
  Section 9 fails only if that accounting is unavailable, never on the numbers
  themselves — the numbers are the measurement it exists to take.

  Both sides are quarantined by construction rather than by discipline: the gate
  refuses a `synth-` `matrix_id` in real mode and requires one in self-test mode,
  and `p2-mutate.py` exits 3 on any directory whose stamps are not `synth-`,
  because every mutation it performs writes a dataset that looks measured and is a
  lie. The gate's header also flags, rather than silently resolves, that the
  CLAUDE.md P2 row still says "spot host" while the spend policy in the same file
  reverses spot to on-demand; the gate asserts nothing about tenancy either way.

- Three P1 fixtures, each mutation-validated in both directions:
  `nodata-group-hole` (one dark `(family, regime)` group at 25% non-comparable
  balanced weight — under the 34% threshold — of which only 8% is the actual
  hole, so no threshold catches it and only the absolute count does),
  `medium-large-localised` (an effect on `dtrsm` in medium+large, which is 6 of
  11 raw cross rows = 55% and fails the majority, but 2 of 3 balanced groups =
  67% and passes, because large buys one `lda_pad` where small and medium buy
  four — so the alignment axis, not the hardware, decides whether a *regime*
  effect is reportable, and it decides against the large regime specifically,
  which is where the DDR generation and the L3 step live), and
  `transpose-lost` (an arm that produced no TN records whatsoever).
- `gates/p1.sh` section 3 asserts the majority arithmetic is exact on the
  boundary, order-independent, and free of a tolerance constant — a property of
  the analysis that no dataset can reach.
- `gates/p1.sh` section 2 cross-checks the `MIN_SECONDS` copies against
  `src/bench.c`, including the ladder→floor mapping read back off `sweep()`'s
  call sites. The constants agreeing is not the same as the mapping agreeing, and
  the mapping lives in call sites rather than in a table.

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
- **A `role` field in every record, decided from evidence rather than from a
  flag.** Instrument checks on non-Graviton hardware and campaign data must not
  be mixable by accident. `run-matrix.sh` now derives the role from two things it
  cannot fake — an IMDS instance type in the campaign set *and* a Graviton MIDR
  part — and instrument runs get their own directory, an `instr-` run_id prefix
  and a role-prefixed S3 path. `GBB_ROLE` is an assertion that aborts on
  mismatch, not an override, and the binaries default to `role=unknown` so a
  hand-run binary is never mistaken for campaign data.
- **`sve_kernels` per OpenBLAS build, read off the installed archive.** Standing
  order 8 names `NO_SVE` in the build as an escalate-now condition and nothing
  checked it. It is the quieter of the two triggers: `NO_SVE=1`, or an assembler
  too old to accept SVE, yields a library on which every arm still builds, still
  runs, and still reports plausible numbers while the entire SVE axis of the
  campaign measures nothing. `build-libs.sh` now looks for SVE kernel symbols in
  the installed `libopenblas.a` and records `yes`/`no`/`unknown` (`n/a` for
  ArmPL, BLIS and netlib), and `decompose.py` escalates `no` on a host that
  reports SVE, which makes the host inadmissible and sets exit bit 2.
- `analysis/decompose.py` rewritten. An adversarial review reproduced the
  previous version printing "V1 kernels win" on data where V2 won 4 of 5 sizes
  and the mean, printing `parity` for two arms that had produced 0.00 GFLOP/s,
  deciding rows at a hardcoded 2% while its header announced 5%, and returning 0
  on every input including one with no comparisons at all. Every threshold that
  decides anything is now a named constant with a stated reason and a flag, the
  `DECISION` block emits one computed machine-greppable `VERDICT:` line so the
  P4 gate can assert on it, and `NULL` (measured parity) and `NO-DATA` (the
  cross never ran) are distinct verdicts because they support opposite
  conclusions. `--json` emits schema `gbb-decompose/1`.
- `tests/run-matrix-stubs.sh` is now 61 assertions, up from 33: the role
  interlock, a forged IMDS type failing to promote a non-Graviton host, the
  declared-alias path, per-variant `arch_selected`, and the manifest stamping.
- **`bench.c` now verifies its own coretype label and refuses to measure under
  one it cannot confirm.** `arch_selected` was inherited from
  `GBB_ARCH_SELECTED`, measured by `gbb-coreprobe-<variant>` in a *separate
  process* — which can resolve a different `libopenblas` by rpath or be handed a
  different environment, so the label was a claim about a library that may not be
  the one doing the work. The measuring process now asks the loaded image
  directly via `dlsym(RTLD_DEFAULT, "openblas_get_corename")`; a disagreement
  exits 4 before any record is written. Looked up rather than linked so the
  compilation stays byte-identical across arms: a `-D` would make the harness
  differ per arm (standing order 6), and a weak declaration would need
  `weak_import` on Mach-O and so trade a per-arm difference for a per-platform
  one. The runner censuses exit 4 as `mislabelled` rather than `runtime_failed` —
  a retry reproduces it, and the useful fact is that the label and the artifact
  disagree — and `decompose.py` raises it as a hard anomaly with exit bit 2,
  because every forced-coretype label on that host came from the same probe.
  `tests/arch-selected-assert.sh` covers all four paths against a stub, in
  `gates/p0.sh`.
- Spend policy for P2 and P3 recorded in `CLAUDE.md`: **on-demand throughout,
  including P2** on `c8g.metal-48xl`, and P3 run **three times** on different
  `instance_id`s, launched with truffle/spawn rather than with new in-tree tooling.
  Spot for P2 was the earlier decision and is reversed: ~$100 of saving is not worth
  putting untested reclaim handling on the critical path. Costed at **$500–650 for
  three expanded passes** (30–37 instance-hours each); the earlier ~$96/pass figure
  described the pre-expansion routine table and is **retired rather than
  reconciled**. $500–650 is a **planning basis to be replaced by a measured
  number**: the sentence that stood here — "sweep time is not proportional to case
  count, `MAX_REPS` caps the small end and `MIN_REPS` floors the large end, so the
  expansion is ~4× the cases and ~1.6–2× the wall clock" — was written against a
  timing model `6a8089f` had already removed. See the `MIN_SECONDS` entry below.
  Three passes rather than two because two passes
  have no breakdown point — the median of two is the mean, and a single bad pass
  moves the answer with nothing to outvote it. The passes must be independent,
  which means three separate launches days apart with the instance terminated
  between them: a loop inside one instance's lifetime is a repeat measurement, not
  a replicate. The count stays uniform across all five hosts, cut only under
  capacity pressure and then from `c6g` first, because mixed pass counts push the
  handling into the pooling rule, which is the last place to want more code. P2's
  host is `c8g.metal-48xl` despite being the expensive choice: it is where the
  central cross lives, and debugging the harness on a cheaper host would leave the
  most important analysis path untested until P3. A replicate is identifiable with
  no new field — same `instance_type`, different `instance_id` — which fails safe,
  since a re-run on the same box shares the `instance_id` and is correctly not
  counted as one. Gate P3 now requires the headline to reproduce across passes,
  `REPRODUCES` or `REPRODUCES-MAJORITY` and never `DIVERGES-*`.

- CI now runs gate P1, `tests/arch-selected-assert.sh`, and a new P0 section that
  asserts every gate and suite in the tree is wired into CI. P0's requirement is
  "CI green on a clean clone", which is worth exactly the set of things CI runs —
  and P1 and the arch-selected suite were both in the tree and in neither job, so
  a drift between `synth.py` and `bench.c` would have been found by a dataset that
  cost instance-hours instead of by a push.

- **`incx` is now in the record and in the comparison key.** `run_level1` runs
  the same `(routine, m, n, k, lda_pad)` at stride 1 and stride 4, and the record
  did not say which — so the two collapsed into one cell and the min-within-run
  rule silently kept the slower of the pair. That deleted the stride axis, which is
  one of the specific places the arm64 tree is expected to be weakest, and it
  deleted it in the direction that hides an effect. Records written before this
  change default to `incx=1`, which is correct for every level-3 routine and merges
  the old level-1 pairs exactly as they merged before.
- `canon_coretype()` in `decompose.py`: an unforced arm reaches the analysis with
  `coretype` as `""`, `null`, or absent depending on which producer wrote the line,
  and those were three different arms in the arm key. One physical arm counted three
  times is a coverage hole, a thin cell, or both.

- `tools/synth.py` and `gates/p1.sh`: the analysis is now calibrated against
  datasets whose right answer is known by construction, because campaign data
  cannot serve that purpose — the right answer there is the thing being looked
  for. 42 scenarios plant a null, a broad effect, an effect confined to the small
  regime, an effect confined to `incx=4`, a leading-dimension penalty, a dead arm,
  an arm returning wrong answers, a mislabelled arm, an arm censused `aliased`, an
  arm that produced only some of its sizes, an arm with no provenance, generic
  `ARMV8` on an SVE host, an acknowledged escalation, a host with no `peak_fma`,
  a directory polluted with instrument-check records, a run with a lucky duplicate
  sample, three passes one of which is flattered, a host on which every arm failed
  to build, two passes that agree and two that disagree, and two hosts built from
  different OpenBLAS trees. Each declares its own
  expectations; `gates/p1.sh` generates it, runs `decompose.py` over it, and
  checks the report and the exit bits against them, so adding a scenario needs no
  gate edit. Fixtures are written to a scratch directory and never to `results/`:
  they are not measurements (standing order 3) and must not be able to reach the
  published dataset. The gate also asserts that synth.py's copies of bench.c's
  size ladders still match bench.c, since a drifted copy makes every fixture a
  faithful test of the wrong experiment.
- **`decompose.py` section 8, replicate agreement, and exit bit 16.** The
  separately launched passes gate P3 requires are now compared rather than pooled.
  Pooling
  would convert the campaign's strongest evidence — that the headline reproduces
  on a different box of the same type — into slightly tighter error bars on one
  number. Each `(instance_type, instance_id)` is analysed independently and the
  verdict codes are set against each other: `REPRODUCES`, `REPRODUCES-MAJORITY`,
  `DIVERGES-DIRECTION`, `DIVERGES-INCONCLUSIVE`, or `NO-REPLICATE`. Divergence sets
  exit bit 16 and
  prints a `VERDICT-CAVEAT:`. The per-pass delta spread is reported but never
  gated on — the claim is about the direction of the finding, not its magnitude.

- **The scenarios are validated by mutation, not by passing.** A fixture that
  cannot fail is a decoration, and an adversarial audit of the first 25 found
  several that could not: every effect-bearing scenario is now re-run with its
  planted effect deleted and must go red, and every rule a scenario claims to
  guard is broken in `decompose.py` and must also turn it red. Two rules turned out
  to be guarded by nothing at all — see the aggregation entry under Fixed — and
  the exit-bit table now covers bit 1 as well as 2, 4, 8 and 16.

- **The fixtures now cover the routine set the conclusion rests on, and the
  routine-localised shape the campaign predicts.** Every scenario had been running
  the default three routines, so `dtrsm`, `dtrmm` and `dsymm` appeared in no
  fixture — and those are the operations in the 90-kernel `NEOVERSEV2`/`N2` gap
  this campaign exists to price. The gate certified the analysis on `dgemm` and
  `dgemv` and said nothing about the routines the answer would be quoted from.
  `full-routine-set` plants all nine routines `bench.c` emits with the effect on
  the N2-gap three only. It found the verdict defect below.

- **Section 1, the deficit-by-routine table, is now asserted.** It was computed,
  printed and quoted by the write-up while nothing in the gate checked a single
  number in it — an analysis that got section 2 right and section 1's
  reference-relative deficit wrong passed green. Four new check kinds assert the
  deficit magnitude and sign, that exactly one arm per condition is marked
  `SHIPPED` and that it is the `openblas/DYNAMIC/unforced` one the wheels run, and
  both ways the table can have no reference to measure against. Mutation-checked:
  `is_shipped()` returning `False` for every arm, returning `True` for every arm,
  the deficit sign left un-negated, and the instance-level NO-DATA row not
  appended each turn a scenario red. Two scenarios were added for the absent
  branches — `reference-library-absent` (no non-OpenBLAS library on the host at
  all, which is the ordinary case if ArmPL is not on the AMI) and
  `reference-arm-partial` (a reference library that ran but has no kernel for one
  routine). The second also pins a coverage consequence: an arm censused
  `measured` that produced no records for a routine is `MISSING-UNEXPLAINED` and
  exit bit 4, not a quietly narrower table.

- **The fixtures now contain every arm shape the producers can write.** An audit
  of `build-libs.sh`'s `arm_record` call sites and `run-matrix.sh`'s census against
  the fixture set found four shapes no scenario could produce, so four branches of
  `decompose.py` would have run for the first time on campaign data:
  `openblas/DYNAMIC_OMP` (`thread_backend:openmp`), the `DYNAMIC_OMP_BOUND` arm the
  runner synthesises after the manifest loop to *measure* the pinning delta rather
  than assume it, a BLIS arm, and a control target `built:true` with
  `runnable:false`. `manifest-shapes` plants all four. It also puts two candidate
  reference libraries on one host for the first time, which turns section 1's
  "named reference arm" from a description into a claim that can fail: the new
  `deficit_reference` check asserts one reference per cell, the same one for every
  arm in it, since rows measured against different references are not one table.
  Mutation-checked: choosing the reference per arm instead of per cell turns
  `manifest-shapes` red and no other scenario.
  `reference-library-absent` now makes its reference arms absent the way the
  producers do — `armpl/native` and `blis` in the manifest as `built:false` with an
  empty `blas_sha` and a stated reason — rather than by being left out of the arm
  list, and asserts that an unbuilt arm with a reason is an explained absence and
  not exit bit 4. That is the ordinary state of at least one campaign host, so a
  dataset that set bit 4 there would set it on every real run.
- Two scenarios for provenance shapes that had no fixture: `probe-unavailable`
  (`capture-env.sh` could not run the DYNAMIC_ARCH probe, so
  `openblas_dynamic_selection` is null and `openblas_coretype_forcing` falls back
  to `not_probed`) and `topology-defaulted` (`lscpu` produced nothing, so
  `sockets`, `numa_nodes` and `threads_per_core` are defaults). The second also
  pins the exact warning text `capture-env.sh` emits, because `decompose.py`
  matches it by substring and a reword would not error — it would silently stop
  suppressing the cross-socket note and stop flagging the defaulted SMT field.
  Mutation-checked by rewording the constant.

- **`role` is a filter in `decompose.py`, not just a field in the record.**
  Records carrying a role other than the requested one (default `campaign`) are
  excluded before anything else looks at them, counted in
  `inputs.foreign_roles`, and reported as a `role_excluded` anomaly with exit
  bit 2. One `aws s3 sync` of a bucket holding both prefixes puts instrument-check
  records from `castor`/`pollux` into a campaign directory, and quarantining them
  by construction (standing order: by construction, not by discipline) requires the
  consumer to enforce it too. The failure was quiet in the worst way: those records
  scale every arm by the same factor, so the cross ratios survive pooling unchanged
  while the measured-peak denominator is inflated — which is precisely how standing
  order 1's headroom check goes silent.
- **An acknowledged escalation is reported by the analysis, not only recorded by
  the runner.** `GBB_ESCALATION_ACK` lets a sweep proceed past a standing-order-8
  refusal and writes an `escalation_ack` census record; `decompose.py` now loads it,
  raises it as a hard anomaly and sets exit bit 2. A trace nothing reads is not a
  trace.
- **`sve_kernels: unknown` is a provenance gap, not a pass.** The check that
  escalates `no` accepted `unknown` — "the archive could not be inspected" — as
  equivalent to "SVE kernels are present". It now records a provenance gap, raises
  `sve_kernels_unknown` and sets exit bit 8 — but only where an archive existed to
  inspect. `build-libs.sh`'s `sve_kernels()` prints `unknown` whenever there is no
  `libopenblas.a` to run `nm` over, so a build that failed *always* yields
  `unknown`; raising a provenance gap there reports a missing archive as an
  uninspectable one. An arm whose manifest line is not `built`, or is not
  `runnable`, now gets a note saying there was no archive to read.
- **A DYNAMIC_ARCH probe that did not run is a provenance gap too, on the same
  axis and for the same reason.** `openblas_dynamic_probe_status` of
  `not_attempted` / `build_failed` / `run_failed` all mean the standing-order-8
  generic-`ARMV8` check was never performed on the campaign's central hardware
  axis, and that was a `note` — and notes set no exit bit. It now raises
  `dynamic_probe_unavailable` and sets exit bit 8. Deliberately *not* an
  escalation: absent evidence about what DYNAMIC_ARCH selected is not evidence
  that it selected wrongly, and a fixture that let the escalate branch fire here
  would make the escalation unreadable on the host where it matters.
  `run-matrix.sh` exports `GBB_OPENBLAS_DYNAMIC_DIR` before `capture-env.sh`
  runs, so `ok` is the normal case and this does not fire on healthy data.
  `Host.provenance_gaps` now carries `(anomaly_kind, message)` pairs — it held
  bare strings and section 5 stamped every one of them `sve_kernels_unknown`,
  which was true of the only producer at the time and would have mislabelled the
  second one. It fires only where the probe *should* have run: if the
  `openblas/DYNAMIC` build itself is absent from the manifest or censused
  `build_failed` on that instance, there was no library to probe and the report
  says so as a note instead. An exit bit that fires routinely stops being read,
  which costs the bit entirely; the structurally-inapplicable case belongs to
  section 7's explained-absence machinery, not to bit 8.
- **An explained absence now explains itself in the report, not only in the
  census file.** Standing order 11 says every gap carries a reason; section 7 read
  that reason, used it to classify the gap, and then dropped it for every gap that
  was not a hole. A reader saw `build_failed=12` and had to go back to
  `census-*.ndjson` to learn that `ARMPL_DIR` was unset. Section 7 now lists each
  explained absence with its reason, and `coverage.explained` carries the same in
  the JSON. The two `excluded` statuses are deliberately not listed: their reason
  is this file's own exclusion, already stated as a hard anomaly in section 5.
- **`REPRODUCES-MAJORITY` in section 8, for the third pass to be worth buying.**
  Two passes have no breakdown point — the median of two is the mean, so one bad
  pass moves the answer — and with only `REPRODUCES` / `DIVERGES-*` available, any
  third pass that reached no direction (a crashed arm, a partial sweep) would have
  been read as a divergence. A verdict now reproduces by majority when at least
  three passes ran, one code holds a strict majority, that code has a direction,
  and no *other* pass contradicts its direction. The note names the dissenting
  `instance_id` and says to read that pass rather than average it in. A dissent
  that points the other way is still `DIVERGES-*` at any pass count.
- **`--replicate-passes` (default 3), and `UNDER-REPLICATED` as a printed line
  rather than a status.** The expected pass count comes from the spend policy, so
  the report states it and names the shortfall. It is deliberately not a status and
  sets no exit bit: a one-host P2 dataset is under-replicated by construction, and
  a bit that fires on every P2 run would train the reader to ignore bit 16 before
  P3 ever produced a real divergence. `passes_expected` and `under_replicated` are
  in the JSON for a gate to decide about.
- **Section 8 reports what each pass lost, not just what it concluded.**
  Section 7's explained-absence listing is pooled across passes, so an arm that
  failed on exactly one of three passes had its reason recorded and never printed —
  the same "a reason recorded is not a reason reported" gap as above, at pass
  granularity. Each pass now lists its own non-successful arms with status and
  reason, which is the difference between reading `REPRODUCES-MAJORITY` as noise
  and reading it as "the V1 arms crashed on pass c".
- **`SVE_KERNEL_SETS` and `kernel_set_note()`.** Section 2's header hardcoded
  "`NEOVERSEV1` = 99 SVE kernels" and printed it whatever `--v1-set`/`--v2-set`
  said, so reading the same dataset as `ARMV8SVE` vs `NEOVERSEV2` — which needs no
  new measurement, both are already forced coretypes on every sve2 host — would
  have captioned 94 kernels as 99. The counts now come from a table keyed by
  kernel-set name.
- Two P1 fixtures, taking `tools/synth.py` to 44 scenarios: `probe-inapplicable`
  (a host with no `openblas/DYNAMIC` build at all — asserts both bit-8 guards stay
  silent, both notes appear, and the buildlog reason survives to the report) and
  `replicate-majority` (three separately launched passes, two agreeing, the third
  losing its V1-set arms to a crash). Each was validated by mutation: reverting the
  behaviour it guards turns exactly that scenario red and nothing else.
  `replicate-majority` also surfaced a live consequence of the pooling rule — one
  arm lost on one of three passes made every *pooled* cell unequal-N and the verdict
  `INCONCLUSIVE` while section 8 showed two passes agreeing at +22%. That was
  escalated as an aggregation-policy question rather than fixed in place; the answer
  is the intersection rule below.
- **Each comparison is intersected to the passes carrying both of its arms.** Global
  equal-N was stronger than the arithmetic requires: what a paired comparison needs
  is equal N *within* the comparison. Three conditions come with it, and they are
  the policy rather than details. (a) A 2-of-3 intersection is back at
  median-of-2 = mean, so every such row carries `UNDER-REPLICATED`, the verdict line
  says how many cells rest on how few passes, and `headline_eligible` is false —
  a directional headline on intersected cells is not a full-replication claim.
  (b) The intersection is licensed by a census reason: `pass_explain()` is keyed on
  `(instance, run_id, arm[, threads])` and returning `None` keeps an unexplained
  loss out, where the comparison stays `inconclusive(unequal-N-unexplained:…)`.
  Pooled coverage is complete in that case, so section 7 and bit 4 cannot see it —
  the per-pass view is the only thing that can. (c) Every row prints
  `passes=UofA`, so 2-of-3 is never visually equal to 3-of-3.
- **`coherent_subsets()` weights by routine family, normalised, not by raw cells.**
  Cell counts follow `bench.c`'s ladder, not the hardware: a routine measured at
  five pads and four transposes contributes twenty times the rows of one measured
  once, all of them the same hardware claim repeated. Each family now contributes
  one unit of weight to an axis value, divided among its own rows, so GEMM's row
  count cannot decide whether a TRSM/TRMM/SYMM effect is coherent. This had to land
  before any table edit: every item in the #2 matrix expansion multiplies GEMM's
  rows faster than anything else's, and on raw counts the expansion would have made
  the C11 false negative *worse* than it was before C11 was fixed.
- **`transa`/`transb` in the comparison key, and as an axis of the coherence
  guard.** NN routes A through `gemm_ncopy_*` and TN through `gemm_tcopy_*`, so
  spanning them in one cell lets each arm be judged on whichever transpose
  flattered it — the max-over-cell defect that the `incx` and `lda_pad` keys each
  fixed once, in a third shape. The key alone was not enough: with the axis in the
  key and not in `coherent_subsets()`, a 35% effect present at every size of one
  transpose read out as "NULL — publish the negative result", because it is
  confined to no routine, regime or instance. `canon_trans()` defaults an absent
  field to `N`, so records written before `bench.c` emits the fields stay in one
  cell and every existing fixture is unchanged.
- Three P1 fixtures, taking `tools/synth.py` to 47 scenarios, each
  mutation-validated: `replicate-loss-unexplained` (the pair to
  `replicate-majority` — an arm's records absent from one pass while the census says
  `measured`, which must *not* be intersected), `transpose-shopping` (an effect at
  `TN` only) and `family-swamped` (GEMM at four transposes holding 32 of 41 rows
  against a coherent TRSM/TRMM/SYMM effect: 27% of rows, 75% of families).
  Mutation results: removing the intersection kills both replicate fixtures;
  intersecting unconditionally kills only `replicate-loss-unexplained`; silencing
  `UNDER-REPLICATED` kills only `replicate-majority`; counting raw rows kills
  `family-swamped` and `full-routine-set`; dropping the transpose from either the
  key or the guard kills `transpose-shopping` and `family-swamped`.
- `full-routine-set` now also expects `regime:small:V1`. That is the normalisation
  visible on the routine set a real host produces: `dgemm` and `sgemm` are one
  family, so the small ladder is gemm and syrk at parity against trsm, trmm and
  symm ahead — three of five families, where raw rows gave a minority.
- **`build-libs.sh` takes a lock on `$GBB_PREFIX` and `$GBB_SRC`, and
  `run-matrix.sh` refuses to sweep against a locked prefix.** Both paths are fixed,
  so two concurrent builds on one host check OpenBLAS out into the same source tree,
  `make install` over each other's `openblas-*` trees, and append interleaved lines
  to one `build-manifest.ndjson`. The damage is not a failed build but a successful
  one whose manifest describes a tree the other run built — standing order 10's
  mislabelling, moved from the runner into the builder. A PID-suffixed path is the
  wrong remedy here, unlike the test fixtures: `run-matrix.sh` reads the libraries
  back out of the prefix by name. `mkdir` is the lock primitive because it is atomic
  everywhere; `GBB_FORCE_UNLOCK=1` and `GBB_IGNORE_BUILD_LOCK=1` exist and say what
  they risk. Five new stub assertions (66 total).

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
- **Denser size ladders and an `lda_pad` axis — #2 landing-order item 2.** Small is
  16 sizes (`8..256`), medium 10 (`320..1536`), large 5 (`2048..8192`);
  `LDA_PADS_EXTRA = {1, 4, 8, 64}` for small and medium and
  `LDA_PADS_EXTRA_LARGE = {8}`, carried by `PADDED_ROUTINES = dgemm, dtrsm, dsymm`
  per the approved axis assignment. **544 small cases per arm**, verified against a
  real run rather than by arithmetic: dgemv 15, daxpy 8, ddot 8, sgemm/dtrmm/dsyrk
  31 each, dgemm/dtrsm/dsymm 140 each. Pad 0 is deliberately absent from both extra
  tables — the base sweep already emits it, and a 0 there would write a second
  record for the same condition in the same run, which min-within-run would
  silently resolve. `gates/p1.sh` now checks both pad tables and `PADDED_ROUTINES`
  against `bench.c` the way it already checked the size ladders, and asserts pad 0
  is absent on both sides.
- **`MIN_SECONDS` is per regime: 0.05 s below n=256, 0.30 s above.** Nothing
  defended 0.30 — it entered the scaffold as a bare `#define` (`11677e2`) and
  `6a8089f` only made the declared contract true, listing it as a contract that was
  documented and not met. It is not what protects the ~31 ns `now()` bracket;
  `MIN_BATCH_SECONDS = 1e-3` is, and it is unchanged, so a sample is still ~1 ms and
  the bracket still 0.003% of it. At 0.05 s an n=8 case is still ~500k calls and
  ~51 samples, `MIN_SAMPLES = 8` still binds nothing, and the destructive-operand
  bound on TRSM/TRMM gets tighter rather than looser. At 0.30 s it was three million
  calls at n=8, measuring harness dispatch as much as the kernel. **Every record now
  carries the `min_seconds` it was measured under**, so records from before and after
  this change are distinguishable rather than merely inconsistent — which is what
  this section of the changelog exists for.

### Fixed

- **The campaign verdict counted raw cells, so `bench.c`'s ladder was a voter —
  the max-over-cell defect's third appearance, and the first on the regime axis.**
  `coherent_subsets()` had been normalised per routine family; `compute_verdict()`
  had not. Before the ladder densification the three regimes contributed 20 cells
  each and the count was balanced by accident, so nothing showed; after it they
  contribute 160/110/20 and both failure directions exist. An effect confined to
  small+medium clears a 60% majority on cell count alone and reads as a
  campaign-level `V1-SET-AHEAD`; an effect confined to the large regime cannot reach
  60% however large it is, because large is ~6% of the cells — and large is where
  the DDR generation and the L3 step show, so the second failure would have silently
  removed the memory-side finding from the campaign's reach. The verdict majority is
  now over `(routine_family, regime)`-balanced weight, one unit per group divided
  among its cells, with raw counts still printed alongside. Killed by
  `v1-ahead-small` under mutation.
- **A balanced majority alone could publish a global claim from a minority of the
  work.** Balancing stops the ladder voting, but a 12-cell family then weighs as
  much as a 240-cell one, so three small families clear 60% of balanced weight while
  the dataset's median moves +0.2%. A directional verdict now also requires the
  median over *all* comparable cells to clear `--min-effect`, signed; below it the
  verdict is `MIXED` and names where the effect is. Escalated as a policy choice
  rather than fixed in place, and decided by Scott. `family-swamped` asserts both
  halves — with the floor removed it reports `V1-SET-AHEAD` on a dataset whose two
  GEMM families really are at parity.
- **A majority threshold was decided by floating-point summation order.** Balanced
  weight is a sum of reciprocals, so a 24-cell group is 24 × (1/24), which is not
  exactly 1.0. `full-routine-set` lands exactly on 3.0/5.0 = 0.60 by construction and
  went red on nothing but a ladder edit, with the two directions of one comparison
  able to disagree. All majority comparisons now go through `meets()` with
  `MAJORITY_EPS = 1e-9` — far below any resolvable difference, far above the
  accumulated error, and it settles the tie the way the policy's own arithmetic does.
- **A kernel returning wrong answers could publish "publish the negative result".**
  `verify-fail` excluded the arm and printed the anomaly correctly; the *verdict* was
  refused only because the excluded cells pushed the non-comparable fraction over
  `--max-nodata-fraction`. The densification took dgemm's total exclusion from 40% of
  the cross to 29%, under the 34% threshold, and the fixture went green on `NULL`.
  The threshold was never the guard. `compute_verdict()` now refuses `NULL` while any
  routine stands excluded for a failed verification, on principle: a wrong answer is
  not a slow answer, that routine never compared, and it is where a kernel difference
  was most likely. The threshold was **not** retuned. Per-pass verdicts get the same
  guard from each pass's own exclusions.
- **Section 3 could have pooled the pad axis unobserved.** With one extra pad value
  "tight versus padded" was a single comparison, so a per-pad attribution had nothing
  to distinguish it from an averaged one. `lda-penalty` now plants 18% at pads 1/4/8
  and leaves pad 64 flat *on the same arm*, and asserts both; pooling every padded
  stride against pad 0 fails it. A penalty is a property of the stride, and which
  stride it is is the packing finding.
- **A timer-outrun record was reported as a wrong answer.** `bench.c:381` sets
  *both* `gflops = 0` and `verified = false` on the same record when the timer is
  outrun, because it never ran the verification — so a timer-outrun record arrives
  with both markers set. `build_cells` tested `verified is False` first, which
  classified every one of them as a verification failure, made the `zero_gflops`
  branch unreachable on real data, and printed "WRONG ANSWER, excluded" against a
  kernel that had merely finished too fast to time. Both paths exclude the record,
  so no number changed — but the anomaly table exists to say *which* thing went
  wrong, and it was sending the reader after a numerical bug that did not exist.
  The specific diagnosis is now tested first. Found by gate P1's `dead-arm`
  scenario.
- **A clean dataset raised an unexplained coverage hole.** `run-matrix.sh`
  censuses the roofline cross-check as an arm (`library=roofline`,
  `target=native`) so that an absent `peak_fma` carries a stated reason like any
  other gap — but it writes `roofline-*.ndjson`, not `bench-*.ndjson`.
  `report_coverage` folded every census arm into the expected *bench* arms, so
  that pseudo-arm was expected to produce a cell for every condition on the host
  and produced none: 36 `MISSING-UNEXPLAINED` cells and exit bit 4 on a dataset
  with nothing missing. Every real P2 run would have looked broken, and the flag
  that says "you have a coverage hole" would have been the one flag guaranteed to
  be lying. Non-bench libraries are now excluded from the expected-arm set at that
  site. Found by gate P1's `null` scenario.
- **The same coverage-hole defect existed twice more, and one half of it was in the
  producer.** `run-matrix.sh` skipped the netlib reference arm with a bare
  `continue` before any `census()` call, so an arm the manifest declares built and
  runnable simply vanished — standing order 11 says every arm the runner declines to
  run carries a reason, and this one did not. It is now censused `skipped` with the
  reason "correctness control, never timed". On the analysis side `reference` and
  `host` joined `roofline` in the non-bench set: the `host` census record that
  `GBB_FORCE_INVALID_HOST=1` writes was also being expected to produce a cell for
  every condition on the host.
- **`aliased` was missing from the set of census statuses that mean "the arm
  ran".** Standing order 8 records that OpenBLAS resolves `NEOVERSEV2` onto
  `NEOVERSEN2` on a recognised V2/V3 part, so on every real `c8g`/`c9g` run the
  campaign's central arm is censused `aliased` — written *before* the arm runs, so
  it can never explain a missing cell. It was nonetheless accepted as one, which
  would have let a genuine hole in the arm the whole cross rests on be accounted for
  by a line that says "running it".
- **The aggregation policy was guarded by nothing.** `build_cells` takes the minimum
  within a `run_id` and the median across `run_id`s, both to stop the luckiest
  sample from being the one that survives — and no fixture emitted a duplicate
  record or a third pass, so swapping either rule for `max` left all 33 scenarios
  green. Two scenarios now plant exactly those shapes: a re-run appended into one
  file 40% faster than the honest sample (min keeps the honest one, `max` publishes
  a 40% kernel-set win that was never measured), and three passes one of which is
  25% fast on one side of the cross. The second is asserted on the pooled *number*
  rather than the verdict, deliberately: substituting `max` across runs raises
  `run_spread` by exactly the amount it raises the delta, so the parity band widens
  in step and every verdict-level assertion in the file survives the mutant.
- **The campaign verdict reported a routine-localised effect as a global null.**
  `compute_verdict` counts comparable cells, and the routine set does not
  contribute them evenly: padded and unpadded `dgemm` is 20 cells, `sgemm`,
  `dsyrk`, `dtrsm`, `dtrmm` and `dsymm` 12 each, `dgemv` 8. So an effect confined
  to TRSM/TRMM/SYMM — the shape the 94-vs-5 kernel gap predicts, and the whole
  reason the campaign exists — is 36 of 104 comparable cells, the parity cells
  then hold 65%, they clear the 60% majority, and the headline read
  `NULL … publish the negative result` over a coherent +22% on every cell of the
  three routines under study. Whether such an effect reaches the majority is
  decided by how many cells the *unaffected* routines contribute, which is a
  property of `bench.c`'s size ladder rather than of the hardware. The NULL branch
  now requires that no coherent subset carries a direction of its own —
  `coherent_subsets()`, over routine, regime and instance, both directions, at
  least `--subset-min-cells` (3) comparable cells and a `--verdict-majority` share
  within the subset — and the verdict is `MIXED` with the subsets named plus a
  `CONSEQUENCE: the difference is routine-localised …` line, which is the sentence
  the write-up needs for "worth doing, and where". This makes NULL *harder* to
  reach, so it is guarded in both directions: `null` and `noise-only` assert that
  the subset set is exactly empty, and a mutant that drops the within-subset
  majority test turns both red. A guard that could manufacture a localised effect
  out of a genuine null would be worse than the false negative it fixes — a null
  result is a publishable outcome here. Found by the `full-routine-set` fixture on
  its first run; nothing in the campaign's own hypothesis was used to derive the
  rule.
- **Exit code 1 was the one exit code no gate could assert on.** `decompose.py`
  returned before writing its report when nothing loaded, and `gates/p1.sh` cannot
  tell a scenario that wrote no report from one whose analysis crashed. It now
  writes the same schema with empty sections and `verdict.code = NO-DATA`, and the
  `all-arms-failed` scenario — provenance and a census of build failures, no
  measurements — asserts it.

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
  analysis could not reach it. It is now stamped into
  `results/manifest-<run_id>.ndjson` — stamped, not copied: `build-libs.sh` runs
  before anything knows which host it is on, and the analysis concatenates every
  host's manifest into one stream, where a per-build fact like `sve_kernels` is
  unattributable to a host and therefore unactionable. `instance` and `role` are
  inserted on the way in.
- **The coretype check suppressed the finding it existed to detect.** Any
  `OPENBLAS_CORETYPE` request whose reported corename differed from the request
  was written off as `unrunnable` — but `KERNEL.NEOVERSEV2` is a one-line include
  of `KERNEL.NEOVERSEN2`, so on Graviton 4/5 the expected and correct outcome is
  that `NEOVERSEV2` reports `neoversen2`. The check would therefore have deleted
  the V2-set arm on the two hosts the experiment is about. The known aliases are
  now *declared* rather than inferred, because "the reported name differs from
  the request" cannot by itself distinguish a documented alias from a request the
  library ignored, and the difference decides whether an arm is a measurement or
  an unlabelled duplicate of the unforced arm. A declared alias runs and is
  recorded as `aliased`; an undeclared mismatch is still refused; a second
  request resolving to a corename already claimed is `alias_duplicate`.
- **Every non-`DYNAMIC` arm was labelled with the `DYNAMIC` binary's kernel
  selection** — `openblas/NEOVERSEV1` recorded `arch_selected=neoversen2`, and so
  did ArmPL. That is provenance measured on a different library, which standing
  order 10 makes worse than no provenance at all. `build-libs.sh` now builds a
  coreprobe per OpenBLAS variant and each arm is labelled by the probe linked
  against its own library; ArmPL and BLIS record `n/a`, which says the question
  does not apply, where `unknown` would say we tried to answer it and failed.
- `probe_variant()` in `run-matrix.sh` declared `local v="$1" pr=".../$v"` in one
  statement, so `$v` expanded while `v` was a declared-but-unset local. Under
  `set -u` that aborts the function, and inside a command substitution the abort
  is invisible: the caller got `""` and labelled the arm `unknown`.
- **Two more silent-abort command substitutions of the same class, found by
  sweeping for the pattern rather than the trigger.** A `$( )` wrapping anything
  that can fail swallows the failure and yields an empty string, and every one of
  these assigned to a variable the sweep then acted on. `envq` returned `""` for
  every field if `python3` was absent or `env-*.json` was truncated by a host
  that died mid-write — `HAS_SVE=""` silently drops every SVE coretype on a host
  that has SVE, and `CORES=""` collapses the thread ladder to one rung, so the
  run completes looking like a clean dataset that happens to contain no SVE arms.
  The precondition is now checked once and loudly, and the fields that decide
  *what gets measured* go through `envq_req`, which stops rather than defaults.
  Separately, the inline `python3 -c` that looked up `DYNAMIC_OMP`'s `blas_sha`
  sat inside an `env` line, so a failure ran the arm with a blank `blas_sha` — a
  record identifying no library. It is hoisted, and the arm is refused with a
  census reason instead of defaulting to `unknown`.
- `us-east-2` was documented as a fallback region carrying all five families. It
  has no `hpc7g` at all. `hpc7g.16xlarge` exists in exactly three regions —
  `us-east-1`, `eu-west-1`, `ap-northeast-1` — one AZ each, and neither of the
  other two offers `c9g.metal-48xl`, so `us-east-1a` is the only availability
  zone where all five families can be placed and there is no fallback.
- A comment in `bench.c` claimed the size-regime boundaries were "re-derived per
  host from measured cache sizes by scripts/run-matrix.sh". They are compile-time
  constants, identical on every host, and nothing overrides them — which is
  correct, since per-host size ladders would mean the cross-host comparison was
  comparing different problem sets.
- README described a static decision guide in `decompose.py` that the rewrite
  replaced with a computed verdict, and said `capture-env.sh`'s dispatch-direction
  wording "is being corrected" after it had been.
- **Two suites sharing one fixture tree failed each other rather than the code.**
  `tests/run-matrix-stubs.sh` and `gates/p1.sh` both built their fixtures at a
  fixed path and `rm -rf`'d it on entry, so a `gates/p0.sh` run (which invokes the
  stub suite) concurrent with a direct run of the same suite deleted its stubs
  mid-flight: 35 assertions red, none of them about `run-matrix.sh`. Both scratch
  paths are now PID-suffixed, with `GBB_TEST_TMP` / `GBB_P1_WORK` still available to
  pin them. Serial CI never hit it; a test suite whose failures can be caused by
  another test suite is a suite that costs credibility the first time it is
  believed.

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
