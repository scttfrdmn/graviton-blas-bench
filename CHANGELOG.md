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

### Fixed — gate

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
