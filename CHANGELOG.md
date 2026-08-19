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
- Spend policy for P2 and P3 recorded in `CLAUDE.md`: spot for P2, on-demand for
  P3, and P3 run **twice** on different `instance_id`s. One clean pass is ~$96,
  so a second independent pass buys more than a 3× discount on the first buys.
  A replicate is identifiable with no new field — same `instance_type`, different
  `instance_id` — which fails safe, since a re-run on the same box shares the
  `instance_id` and is correctly not counted as one. Gate P3 now requires the
  headline to reproduce between passes.

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
- **`decompose.py` section 8, replicate agreement, and exit bit 16.** The two
  `hpc7g` passes gate P3 requires are now compared rather than pooled. Pooling
  would convert the campaign's strongest evidence — that the headline reproduces
  on a different box of the same type — into slightly tighter error bars on one
  number. Each `(instance_type, instance_id)` is analysed independently and the
  verdict codes are set against each other: `REPRODUCES`, `DIVERGES-DIRECTION`,
  `DIVERGES-INCONCLUSIVE`, or `NO-REPLICATE`. Divergence sets exit bit 16 and
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
  `sve_kernels_unknown` and sets exit bit 8.
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
  second one.
- **An explained absence now explains itself in the report, not only in the
  census file.** Standing order 11 says every gap carries a reason; section 7 read
  that reason, used it to classify the gap, and then dropped it for every gap that
  was not a hole. A reader saw `build_failed=12` and had to go back to
  `census-*.ndjson` to learn that `ARMPL_DIR` was unset. Section 7 now lists each
  explained absence with its reason, and `coverage.explained` carries the same in
  the JSON. The two `excluded` statuses are deliberately not listed: their reason
  is this file's own exclusion, already stated as a hard anomaly in section 5.

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
