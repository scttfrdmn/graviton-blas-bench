#!/usr/bin/env python3
"""graviton-blas-bench synth -- plant a known effect, then check it was recovered.

P1 asks one question: does `decompose.py` say the right thing about data whose
answer we already know? That cannot be tested on campaign data, because on
campaign data the right answer is what we are trying to find out. So this file
manufactures datasets with a declared ground truth and a declared expectation,
and `gates/p1.sh` asserts the expectation holds.

The generated records are byte-faithful to the four real producers, field for
field and spelling for spelling:

    bench-*.ndjson     src/bench.c            emit()
    roofline-*.ndjson  src/roofline.c         main()
    manifest-*.ndjson  scripts/build-libs.sh  arm_record() / the toolchain record
    census-*.ndjson    scripts/run-matrix.sh  census()
    env-*.json         scripts/capture-env.sh the one big printf

Faithfulness is the whole point and is not decoration. Two bugs were found by
writing this file rather than by reasoning about the analysis: the three
producers spell "no coretype was forced" as null, "" and "unforced"
respectively -- which made the shipped arm read as MISSING-UNEXPLAINED on a
clean dataset -- and `incx` was absent from the record entirely, so the two
element strides of every level-1 call landed in one cell. A fixture written to
what the analysis *expects* would have found neither. If a producer's schema
changes, change it here too, and expect the gate to tell you what moved.

What is deliberately NOT modelled: the GFLOP/s surface is a plausible-looking
function of size and thread count, not a hardware claim. Nothing here may be
cited as a measurement (standing order 3) and nothing here goes near results/.
The surface exists only to carry an effect of known size and known location so
that "recovered" and "not recovered" are distinguishable.

Usage:
    python3 tools/synth.py list
    python3 tools/synth.py generate <scenario> <dir>     # writes <dir>/results + truth.json
    python3 tools/synth.py check <dir> <report.json> <stdout.txt> <exit-code>
"""

import argparse
import collections
import hashlib
import json
import pathlib
import sys
from dataclasses import dataclass, field

# ---- the size ladders, copied from src/bench.c --------------------------------
# Copied, not imported -- there is nothing to import from a C file. If bench.c's
# ladders change (which requires asking Scott) these must change with them, and
# gates/p1.sh cross-checks the two lists so the copy cannot rot silently.
SIZES_SMALL = (8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256)
SIZES_MEDIUM = (320, 384, 448, 512, 640, 768, 896, 1024, 1280, 1536)
SIZES_LARGE = (2048, 3072, 4096, 6144, 8192)
LEVEL1_LENS = (1024, 16384, 262144, 4194304)

# The lda_pad axis, likewise copied. 0 is absent from both by construction: the
# base pad=0 sweep already emits it, and a second pass would be a duplicate
# sample for one condition. PADDED_ROUTINES is the axis assignment fixed in
# CLAUDE.md -- pads on dgemm plus the two N2-gap routines, not on everything.
LDA_PADS_EXTRA = (1, 4, 8, 64)
LDA_PADS_EXTRA_LARGE = (8,)
PADDED_ROUTINES = ("dgemm", "dtrsm", "dsymm")
REGIMES = ("small", "medium", "large")

# The per-regime timing floor, copied like the ladders. This is not cosmetic: it
# is part of decompose.py's comparison key (see canon_floor()), so a fixture that
# omitted the field keyed every record at the legacy 0.30 s and no fixture
# exercised the small floor at all. gates/p1.sh checks these two numbers against
# bench.c's #defines AND checks that bench.c still passes them at the ladders this
# mapping assumes -- the mapping lives in bench.c's sweep() call sites, so the
# constants agreeing is not enough on its own.
MIN_SECONDS = 0.300
MIN_SECONDS_SMALL = 0.050


def min_seconds_for(m):
    """The floor bench.c measured this size under.

    bench.c passes MIN_SECONDS_SMALL for SIZES_SMALL and MIN_SECONDS for the other
    two ladders, and sets g_min_seconds = MIN_SECONDS explicitly for level 1. The
    level-1 lengths start at 1024, so keying off the ladder membership reproduces
    all four call sites exactly."""
    return MIN_SECONDS_SMALL if m in SIZES_SMALL else MIN_SECONDS


# bench.c's MIN_SAMPLES and MAX_MEASURE_SECONDS, hand-copied and asserted in
# gates/p1.sh section 2 like the ladders, because case_seconds below is a MODEL of
# TIMED_LOOP and these two constants are what give the model its shape.
MIN_SAMPLES = 8
MAX_MEASURE_SECONDS = 3.0
MIN_BATCH_SECONDS = 1.0e-3
ABS_MIN_SAMPLES = 3

# bench.c's conditional-warmup policy, hand-copied on the same terms and asserted in
# gates/p1.sh. The two fields the policy writes per record (warmup_reps, cal_reused)
# are not decoration: they are the only way to tell from a dataset which timing path
# a case took, and gate P2 checks that the expensive end actually stopped paying for
# warmup on real data. A fixture that hard-coded warmup_reps=2 everywhere would make
# that check pass on a dataset where the policy had been reverted.
WARMUP_REPS = 2
WARMUP_MAX_FRACTION = 0.02
# bench.c's large_cap_for_threads(): below this many threads the large ladder stops
# at LARGE_CAP_LOW and the omitted sizes emit case_skipped records instead.
LARGE_CAP_MIN_THREADS = 8
LARGE_CAP_LOW = 4096


def large_cap_for_threads(t):
    return LARGE_CAP_LOW if (t or 1) < LARGE_CAP_MIN_THREADS else max(SIZES_LARGE)


def timing_path_for(t_min, min_seconds):
    """(warmup_reps, cal_reused) as bench.c's TIMED_LOOP would decide them.

    Reproduced rather than modelled, because unlike case_seconds these two fields are
    DISCRETE and are checked for their exact values downstream -- a model that got the
    boundary roughly right would put the boundary in a different place from the
    producer and every assertion about "the expensive end stopped warming up" would be
    asserting about the fixture's boundary instead of bench.c's.

    The one thing not reproduced is the two-stage calibration, so batch is taken as 1
    whenever a call already exceeds MIN_BATCH_SECONDS -- which is the only regime where
    either field is interesting, and is where every expensive case lives."""
    # t_min == 0 is the timer-outrun arm (Arm.zero_gflops), and it is the LIMIT of
    # this function rather than an error case: a call too fast to time is batched
    # (so the calibration call cannot be reused) and its warmup is free (so warmup
    # runs). Returned explicitly because the arithmetic below would divide by it.
    if t_min <= 0.0:
        return WARMUP_REPS, False
    batch = 1 if t_min >= MIN_BATCH_SECONDS else max(1, int(MIN_BATCH_SECONDS / t_min))
    per = batch * t_min
    ns = max(MIN_SAMPLES, int(min_seconds / per) + 1)
    fit = int(MAX_MEASURE_SECONDS / per)
    if ns > fit:
        ns = fit if fit > ABS_MIN_SAMPLES else ABS_MIN_SAMPLES
    warm = WARMUP_REPS if WARMUP_REPS * t_min <= WARMUP_MAX_FRACTION * ns * per else 0
    return warm, (batch == 1 and warm == 0)


def case_seconds_for(t_min, min_seconds, bytes_touched):
    """A plausible value for bench.c's case_seconds.

    DECLARED A MODEL, not a reproduction. TIMED_LOOP's real cost comes out of a
    two-stage calibration this file does not reproduce and should not: the fixtures
    assert nothing about the timing loop, and synth already writes fixed reps/batch/
    calls. What the model does have to get right is the ONE property the cost plan
    turns on -- that wall-clock is anti-correlated with arm quality. Below t_call ~
    min_seconds/MIN_SAMPLES a case costs the floor and no more, because the batch
    absorbs the difference; above it the case cannot stop before MIN_SAMPLES samples
    of one call each, so a SLOWER arm costs strictly more wall clock for the same
    measurement. That is why CLAUDE.md says to instrument the slowest arm rather
    than a representative one, and a fixture in which every arm cost the same could
    not tell a cost analysis that found the slowest arm from one that took the
    first. MAX_MEASURE_SECONDS is the cap that stops the largest cases running away.
    The allocate-and-fill term is a flat 20 GB/s, which is the right order for a
    Graviton and is not asserted anywhere.

    The warmup and calibration terms are here because they are the whole point of the
    policy change they model: at the expensive end they are the difference between six
    calls and three, and a cost model that omitted them would report the saving as
    zero and the fixtures would certify a cost analysis that could not see it."""
    warm, reused = timing_path_for(t_min, min_seconds)
    measure = min(MAX_MEASURE_SECONDS, max(min_seconds, MIN_SAMPLES * t_min))
    overhead = warm * t_min + (0.0 if reused else t_min)
    return round(measure + overhead + bytes_touched / 2.0e10, 6)


def case_bytes(r, m, n, k):
    """Roughly what a case allocates and fills, for case_seconds_for()'s alloc term.

    Approximate on purpose and asserted nowhere: it is not bench.c's allocator, and
    the only thing it has to do is make a large case's fixed overhead larger than a
    small one's so that the floor dominates at the small end and does not at the
    large end. Single precision is 4 bytes, everything else 8."""
    elt = 4 if r == "sgemm" else 8
    if r in ("daxpy", "ddot"):
        return 2.0 * m * elt
    if r == "dgemv":
        return (m * n + m + n) * elt
    if r in ("dgemm", "sgemm"):
        return (m * k + k * n + m * n) * elt
    return (m * m + m * n) * elt


# bench.c's regime boundaries, likewise.
def regime(n):
    if n <= 256:
        return "small"
    if n <= 1536:
        return "medium"
    return "large"


def case_flops(r, m, n, k):
    """bench.c's case_flops(), exactly."""
    if r in ("dgemm", "sgemm"):
        return 2.0 * m * n * k
    if r in ("dtrsm", "dtrmm"):
        return 1.0 * m * m * n
    if r == "dsyrk":
        return 1.0 * n * n * k
    if r == "dsymm":
        return 2.0 * m * m * n
    if r == "dgemv":
        return 2.0 * m * n
    if r in ("daxpy", "ddot"):
        return 2.0 * m
    return 0.0


# bench.c's verification outcome per routine, and the note it carries.
#
# EVERY routine now carries a real corner check, so a clean arm reports
# verified=true across the board and an empty note. Until 2026-08-20 only dgemm
# did, and the other eight emitted verified=null with a `corner_check_absent`
# note -- 31723 of 42743 cells on the first P2 pass, concentrated in exactly the
# TRSM/TRMM/SYMM family the campaign's likely finding lives in. The tri-state
# `verified` reported that gap honestly, which is not the same as closing it.
#
# `verified=null` is therefore no longer reachable from bench.c for a MEASURED
# case, and section 5's coverage table is not thereby pointless: `case_skipped`
# records still carry no verdict, archived datasets (the P2 dry run among them)
# are full of nulls, and a routine added later starts at null again. Scenarios
# that need a null now ask for one explicitly via Arm.verified_null_routines
# rather than getting one for free from this table -- see sc_unverified_verdict.
VERIFY = {
    "dgemm": (True, ""),
    "sgemm": (True, ""),
    "dtrsm": (True, ""),
    "dtrmm": (True, ""),
    "dsyrk": (True, ""),
    "dsymm": (True, ""),
    "dgemv": (True, ""),
}


def pin_policy_for(threads):
    """run-matrix.sh's PIN_DESC for this thread count, in one place.

    It was written out inline at five sites -- bench records, probe records,
    roofline records, the census and the manifest -- which is four copies too many
    of a field the analysis cross-references BETWEEN those files. Standing order 9
    is that the policy is recorded per arm and applied uniformly; a fixture in
    which two files disagree about it would be testing the analysis against a
    dataset the runner cannot produce.

    Simplified from the real PIN_DESC on purpose: the real one selects membind or
    interleave from the topology, and no assertion here reads the policy's content,
    only that it is present and consistent. Faithful in shape, not in detail."""
    return "taskset -c 0" if threads == 1 else f"numactl -C 0-{threads - 1}"


# A plausible single-core ceiling per routine. Ratios roughly match what an SVE
# Neoverse does; the absolute numbers are irrelevant to every assertion here.
PEAK1 = {
    "dgemm": 45.0,
    "sgemm": 90.0,
    "dtrsm": 30.0,
    "dtrmm": 32.0,
    "dsyrk": 40.0,
    "dsymm": 38.0,
    "dgemv": 6.0,
    "daxpy": 2.2,
    "ddot": 3.1,
}
MEMORY_BOUND = frozenset({"dgemv", "daxpy", "ddot"})

# capture-env.sh's warning when lscpu is absent or produced nothing, copied
# verbatim from scripts/capture-env.sh. decompose.py matches it by substring
# (`LSCPU_DEFAULTED`), so the two strings are a contract between a shell script
# and a Python file with nothing to enforce it -- which makes this constant, and
# the `topology-defaulted` scenario that uses it, the only thing that would catch
# a reword. A reworded warning does not error: it silently stops suppressing the
# NUMA note and stops flagging the defaulted SMT field.
LSCPU_DEFAULTED_WARNING = (
    "lscpu produced no topology (not installed, or not Linux): sockets, numa_nodes and "
    "threads_per_core are defaulted to 1 and are NOT measurements on this host."
)


def base_gflops(routine, m, threads):
    """A smooth surface: ramps with size, scales sublinearly with threads.

    Memory-bound routines saturate rather than scale, so a thread-count effect
    planted on dgemm does not also appear on daxpy by accident."""
    ramp = m / (m + 64.0)
    if routine in MEMORY_BOUND:
        scale = 1.0 + 6.0 * (1.0 - 1.0 / (1.0 + 0.15 * (threads - 1)))
    else:
        scale = threads / (1.0 + 0.004 * (threads - 1))
    return PEAK1[routine] * ramp * scale


def jitter(*parts, amp):
    """Deterministic pseudo-noise in [1-amp, 1+amp].

    Keyed on the condition rather than drawn from a stream, so a fixture is
    identical however the loops are ordered and whatever Python does to its RNG
    between releases. A gate that changes its verdict when the loop order
    changes is not a gate."""
    if amp <= 0:
        return 1.0
    h = hashlib.blake2b("|".join(str(p) for p in parts).encode(), digest_size=8).digest()
    u = int.from_bytes(h, "big") / float(1 << 64)
    return 1.0 + amp * (2.0 * u - 1.0)


# ---- what a scenario is made of ---------------------------------------------


@dataclass
class Arm:
    """One (library, target, coretype) triple and how it behaves.

    `gain` is a multiplier on the base surface, keyed by regime, and is the only
    place an effect is planted. `gain_routines` restricts it: a kernel-set effect
    that also moved daxpy would be a bug in the fixture, not a finding."""

    library: str
    target: str
    coretype: str = "unforced"
    gain: dict = field(default_factory=dict)
    gain_routines: tuple | None = None
    gain_sizes: dict = field(default_factory=dict)  # size -> multiplier, overrides gain
    gain_incx: dict = field(default_factory=dict)  # incx -> multiplier, multiplies gain
    gain_pad: dict = field(default_factory=dict)  # lda_pad -> multiplier, multiplies gain
    # "NN"/"TN"/"NT"/"TT" -> multiplier, multiplies gain. NN and TN route A through
    # gemm_ncopy_* and gemm_tcopy_* respectively, so a kernel-set difference confined
    # to one transpose is the ordinary shape, not a contrived one -- and it is
    # unreachable unless transa/transb are part of the comparison key.
    gain_trans: dict = field(default_factory=dict)
    measured: bool = True
    census_status: str = "measured"
    census_reason: str = ""
    omit_census: bool = False
    omit_manifest: bool = False
    in_manifest: bool = True  # manifest arms are per (library, target); forced coretypes are not
    thread_backend: str = "pthreads"
    spread: float = 0.03  # (t_p50-t_min)/t_min
    noise: float = 0.012
    zero_gflops: bool = False
    # Sizes this arm ran but produced no record for -- the shape of a sweep killed
    # partway through a ladder. Distinct from omit_census/omit_manifest, which
    # remove the whole arm: this is the "arm ran and produced only some sizes"
    # case that decompose.py counts as `partial`, and it was inexpressible.
    omit_sizes: tuple = ()
    # {routine: sizes} -- sizes this arm produced no record for, on ONE routine.
    # omit_sizes cuts the same sizes out of every routine, which kills a whole
    # regime across every family at once; this cuts one family's regime and leaves
    # the rest of the design intact. That is the shape the ABSOLUTE half of the
    # coverage guard exists for (a sweep that died inside dtrsm's large ladder --
    # n=8192 TRSM is the campaign's largest working set), and it was inexpressible:
    # with a fraction-of-cells guard alone, one dark (family, regime) group out of
    # twelve is ~8% of the design and passes a 34% threshold unnoticed.
    omit_routine_sizes: dict = field(default_factory=dict)
    # Transposes ("NN"/"TN"/...) this arm produced no record for at all. An arm that
    # ran NN and never ran TN is what a SIGILL in one copy kernel looks like, and
    # until transa/transb were part of the COVERAGE census key it was recorded as
    # "some sizes absent" on a merged cell rather than as a whole missing transpose.
    omit_trans: tuple = ()
    # Routines this arm produced no record for at all. The reference arm's version
    # of omit_sizes, and the only way to reach section 1's per-arm "NO DATA -- this
    # host's reference arm produced nothing here" branch: a reference library that
    # ran but has no kernel
    # for one routine is the ordinary case (ArmPL and netlib do not cover the same
    # set), and until this existed that branch was unreachable from any fixture.
    omit_routines: tuple = ()
    # A second record for the SAME (condition, arm, run_id), faster than the real
    # one. bench.c writes one record per condition per run, but a re-run appended
    # into the same file, or a retried arm, produces exactly this -- and the
    # min-within-run rule exists so that the luckiest sample is not the one that
    # survives. No fixture emitted a duplicate at all, so that rule was undefended:
    # swapping min for max across a run left every scenario green.
    lucky_dup: float = 0.0
    verified_false_routines: tuple = ()
    # build-libs.sh's `target_effective`: what the built library reports about its
    # own configuration, as opposed to what was requested of it. None means no
    # read-back was attempted, which is the honest default and is what every arm
    # except BLIS carries -- see manifest_records().
    target_effective: object = None
    # This arm's records for these routines carry `verified: null`. Since every
    # routine in bench.c gained a corner check (2026-08-20), a null is no longer
    # something a scenario gets for free from the VERIFY table -- but the analysis
    # still has to handle it, because archived datasets are full of nulls and a
    # newly added routine starts there. A scenario that wants to test the
    # VERDICT-CAVEAT path must now say so out loud, which is the right direction:
    # the caveat is being tested deliberately rather than as a side effect of a
    # coverage gap that has since been closed.
    verified_null_routines: tuple = ()
    # This arm's streams emit no `thread_prime` record -- a build whose warmup fix
    # never landed, or a runner that lost the priming call. It is not a data hole
    # (every measurement is still present), which is exactly why it needs its own
    # switch: the damage is that thread 2..N's buffer-pool allocation lands INSIDE a
    # timed region, so the records are all there and some of them are quietly wrong.
    no_thread_prime: bool = False
    # This arm's declined large cases carry `"reason": ""`. Standing order 11 at case
    # granularity: an absence with an empty reason is indistinguishable from a hole
    # to anything that only checks the field's presence, so the guard has to check
    # its content and a fixture has to be able to violate that.
    skip_without_reason: bool = False
    sve_kernels: str = "yes"
    manifest_built: bool = True
    manifest_runnable: bool = True
    manifest_reason: str = ""

    @property
    def key(self):
        return (self.library, self.target, self.coretype)

    def multiplier(self, routine, m, incx, pad=0, trans=None):
        g = self.gain_incx.get(incx, 1.0) * self.gain_pad.get(pad, 1.0)
        if trans:
            g *= self.gain_trans.get(trans, 1.0)
        if self.gain_sizes and m in self.gain_sizes:
            return g * self.gain_sizes[m]
        if self.gain_routines is not None and routine not in self.gain_routines:
            return g
        return g * self.gain.get(regime(m), 1.0)


@dataclass
class HostSpec:
    instance_type: str
    instance_id: str
    run_id: str
    threads: tuple = (1, 64)
    cores: int = 64
    has_sve: bool = True
    has_sve2: bool = False
    governor: str = "performance"
    threads_per_core: int = 1
    numa_nodes: int = 1
    sockets: int = 1
    # ANNOTATED DELIBERATELY. Without `: float | None` this is a plain class
    # attribute, not a dataclass field, so HostSpec(cgroup_cpu_limit=0.5) raised
    # TypeError and decompose.py's cgroup-quota invalidation was unreachable from
    # any fixture. A missing annotation is a silently unconstructible axis.
    cgroup_cpu_limit: float | None = None
    cpus_online: int = 64
    cpus_affinity: int = 64
    midr: str = "0x413fd40f"
    midr_part: str = "0xd40"
    core_name: str = "NEOVERSEV1"
    # Tri-state, like the producer: capture-env.sh emits null when no MIDR could
    # be read at all, which is decompose.py's "core identity is UNVERIFIED"
    # invalidation. `bool` made that third state inexpressible.
    midr_uniform: bool | None = True
    clusters: tuple | None = None
    dynamic_selection: str = "neoversev1"
    dynamic_probe_status: str = "ok"
    forcing: str = "available"
    sve_vl: int = 32
    env_present: bool = True
    # peak_fma / best large dgemm. 0.23 because that is what real hardware does:
    # measured 4.22 / 18.16 on c8g.metal-48xl at t=1, which is why standing order 1's
    # cross-check was retired (a LOWER bound 4x under the thing it bounds). It used to
    # default to 1.06, and a fixture defaulting to "peak_fma is about the best GEMM"
    # would keep suggesting the check could fire. Only `peak-fma-retired` raises it
    # above 1, and that scenario exists to assert nothing happens when it does.
    peak_factor: float = 0.23
    host_scale: float = 1.0  # multiplies every arm on this host
    warnings: tuple = ()
    # Three per-host knobs that would otherwise need editing the files back after
    # writing them. Kept as generation inputs so every emitted number stays
    # self-consistent: swapping gflops post-hoc would leave t_min describing the
    # other arm's run, and a fixture whose own fields disagree cannot test an
    # analysis that reads both.
    # OpenBLAS resolves some coretype requests onto another kernel table. Keyed by
    # requested name, value is what the library reports back.
    coretype_aliases: dict = field(default_factory=dict)
    roofline_present: bool = True
    blas_sha_override: str | None = None
    swap_v1_v2: bool = False  # this pass reaches the opposite conclusion
    # build-libs.sh writes the netlib reference arm into the manifest
    # unconditionally, built:true/runnable:true, and run-matrix.sh declines to
    # time it. Off by default only because most scenarios predate it; the
    # `reference-arm` scenario turns it on and the real producers always do.
    reference_arm: bool = False
    reference_censused: bool = True
    # GBB_ESCALATION_ACK overrode capture-env.sh's refusal for this host.
    escalation_ack: str | None = None
    # Records from an instrument-check host (castor/pollux) that have leaked into
    # this results directory. The producers use an `instr-` run_id prefix.
    foreign_role: str | None = None
    foreign_role_gain: float = 3.0
    # One pass in which some arms came out flattered -- a quieter neighbour, a
    # better-binned physical box. Keyed by a target/coretype name, so the boost is
    # per host AND per arm: Arm is shared across a scenario's hosts and cannot say
    # "only on pass 3", and host_scale moves both sides of the cross together and so
    # leaves every ratio unchanged.
    pass_boost: dict = field(default_factory=dict)  # target or coretype name -> multiplier
    # Arms that produced nothing on THIS pass, censused `runtime_failed` with the
    # stated reason. Keyed like pass_boost, and per host for the same reason: Arm is
    # shared across a scenario's passes and cannot say "only on pass 3". This is the
    # realistic shape of a three-launch P3 -- the passes are independent, so a
    # crash on one of them takes out that pass's arm and not the campaign's.
    failed_arms: dict = field(default_factory=dict)  # target or coretype name -> reason
    # Arms whose records are absent from this pass with NOTHING in the census to say
    # why: the runner exited 0 and censused `measured`, and the records did not
    # arrive. Standing order 12 ships per-arm, so a dropped or truncated shipment is
    # exactly this shape. Keyed like failed_arms. The distinction from failed_arms is
    # the whole point of the pair: an explained per-pass loss may be intersected out
    # of a comparison, an unexplained one may not, because the missing records could
    # have said anything and there is no record of them having said nothing.
    lost_arms: tuple = ()
    # This pass was measured by a binary sweeping a DIFFERENT case matrix. Two
    # forms, because the two are different claims: `matrix_override` plants a
    # specific (matrix_id, matrix_cases) -- a post-expansion pass landing in the
    # same directory as a pre-expansion one -- and `matrix_unstamped` omits the two
    # fields entirely, which is a pass measured before bench.c grew the stamp.
    # Mixing either with a stamped host is what decompose.py refuses.
    matrix_override: tuple = ()  # (matrix_id, matrix_cases)
    matrix_unstamped: bool = False

    def failure_reason(self, arm):
        """Why this arm produced no records on this pass, or None if it ran."""
        for token, reason in self.failed_arms.items():
            if token in (arm.target, arm.coretype):
                return reason
        return None

    def lost(self, arm):
        """True if this arm's records vanished on this pass without a census reason."""
        return any(token in (arm.target, arm.coretype) for token in self.lost_arms)

    def core_clusters(self):
        if self.clusters is not None:
            return list(self.clusters)
        return [
            {
                "cpus": f"0-{self.cores - 1}",
                "midr": self.midr,
                "midr_implementer": "0x41",
                "midr_part": self.midr_part,
                "core_name": self.core_name,
                "cpu_count": self.cores,
                "cpuinfo_max_freq_khz": None,
            }
        ]


@dataclass
class Scenario:
    name: str
    description: str
    hosts: list
    arms: list
    routines: tuple = ("dgemm", "dtrsm", "dgemv")
    level1: bool = True
    # (transa, transb) pairs the GEMM-shaped routines were swept at, and the ONE
    # place this file is deliberately ahead of its producer: src/bench.c does not
    # emit transa/transb yet (that is item 3 of the landing order, and the analysis
    # is item 1). Off by default, so every other scenario stays byte-faithful to
    # what bench.c writes today; the scenarios that turn it on are testing that the
    # comparison key can carry the axis before the sweep produces it. When bench.c
    # does emit the fields, add a ladder_check for this tuple in gates/p1.sh
    # section 2 like the size ladders, and drop this paragraph.
    transposes: tuple = ()
    # The timing-floor overlap band, off by default. Off rather than on because
    # turning it on for every scenario would add ten records per (arm, thread point)
    # to fixtures whose claims are about the cross, and a fixture whose record count
    # moved for a reason unrelated to what it asserts is harder to trust, not easier.
    # Leaving it off also means the existing scenarios cover the ABSENT path, which
    # is a real state: every dataset written before bench.c grew the probe is in it.
    # See floor_probe_records() for the modes.
    floor_probe: dict | None = None
    # Every host in this scenario omits matrix_id/matrix_cases: a whole dataset
    # written before bench.c grew the stamp. Scenario-level rather than per-host
    # because "all unstamped" must be one flag and not a discipline applied to a
    # host list -- a scenario that meant to be entirely unstamped and left one host
    # stamped would exercise the refusal instead of the legacy path, and would look
    # like it was testing the legacy path.
    matrix_unstamped: bool = False
    expect: list = field(default_factory=list)
    blas_sha: str = "a" * 40
    blas_sha_overrides: dict = field(default_factory=dict)  # (library,target) -> sha


# ---- the record writers ------------------------------------------------------
# One function per producer. Field order matches the producer's printf so a diff
# against a real file is readable.


def conditions(routines, level1, transposes=()):
    """Every (routine, m, n, k, lda_pad, incx, transa, transb) bench.c would emit
    for `all`.

    transa/transb are None unless the scenario asked for them, and None means the
    record carries no such field -- which is what bench.c writes today. A scenario
    that names transposes gets one condition per pair on the GEMM-shaped routines
    only, because that is where the copy kernels differ; TRSM's side/uplo/diag
    axis is a separate question and is not being modelled here."""
    out = []
    pairs = tuple(transposes) or ((None, None),)
    gemmish = ("dgemm", "sgemm")
    for r in routines:
        if r in ("dgemm", "sgemm", "dtrsm", "dtrmm", "dsyrk", "dsymm"):
            for sizes in (SIZES_SMALL, SIZES_MEDIUM, SIZES_LARGE):
                for m in sizes:
                    for ta, tb in pairs if r in gemmish else ((None, None),):
                        if r in gemmish or r == "dsyrk":
                            out.append((r, m, m, m, 0, 1, ta, tb))
                        else:
                            out.append((r, m, m, 0, 0, 1, ta, tb))
    for r in PADDED_ROUTINES:
        if r not in routines:
            continue
        gm = r in gemmish
        for sizes, pads in (
            (SIZES_SMALL, LDA_PADS_EXTRA),
            (SIZES_MEDIUM, LDA_PADS_EXTRA),
            (SIZES_LARGE, LDA_PADS_EXTRA_LARGE),
        ):
            for m in sizes:
                for pad in pads:
                    for ta, tb in pairs if gm else ((None, None),):
                        out.append((r, m, m, m if gm else 0, pad, 1, ta, tb))
    if "dgemv" in routines:
        for sizes in (SIZES_MEDIUM, SIZES_LARGE):
            for m in sizes:
                out.append(("dgemv", m, m, 0, 0, 1, None, None))
    if level1:
        for m in LEVEL1_LENS:
            for incx in (1, 4):
                out.append(("daxpy", m, 0, 0, 0, incx, None, None))
            for incx in (1, 4):
                out.append(("ddot", m, 0, 0, 0, incx, None, None))
    return out


# ---- the matrix stamp -------------------------------------------------------
# The synthetic analogue of bench.c's g_matrix_id, and DELIBERATELY NOT THE SAME
# DIGEST. bench.c folds its own case walk; this folds conditions() above. If the
# two were required to agree, every fixture in this file would be asserting a
# hand-copied C hash, and the first divergence between the ladders would show up as
# 50-odd scenarios failing on an opaque hex value rather than as the ladder_checks
# in gates/p1.sh section 2 naming the ladder that moved. So the namespace is
# explicit: a synth id begins `synth-` and can never be mistaken for a measured
# one, in a fixture file or in a bucket.
#
# What IS required to agree is the PROPERTY, which is what the analysis depends on:
# the id changes when the case set changes and does not change otherwise. So it is
# computed from conditions() rather than being a constant per scenario, and it
# ignores per-arm omissions for the same reason bench.c's dry pass ignores the argv
# routine filter -- what an arm failed to produce does not change which matrix the
# binary was sweeping.
MATRIX_NAMESPACE = "synth-"


def matrix_stamp(sc: Scenario):
    """(matrix_id, matrix_cases) for this scenario's case set."""
    conds = conditions(sc.routines, sc.level1, sc.transposes)
    total = 0
    for r, m, n, k, pad, incx, _ta, _tb in conds:
        # Same shape of per-case string as fold_case() in bench.c, and summed rather
        # than XOR-ed for the same reason: two identical cases XOR to nothing, so a
        # duplicated case would be erased by the field meant to expose it.
        b = f"{r}:{m}|{n}|{k}|{pad}|{incx}|{round(min_seconds_for(m) * 1000)};"
        h = 1469598103934665603  # FNV-1a 64 offset basis
        for ch in b.encode():
            h = ((h ^ ch) * 1099511628211) & 0xFFFFFFFFFFFFFFFF
        total = (total + h) & 0xFFFFFFFFFFFFFFFF
    return f"{MATRIX_NAMESPACE}{total:016x}", len(conds)


def matrix_fields(sc: Scenario, host: HostSpec):
    """The two stamp fields, or nothing at all if this pass predates the stamp.

    Returned as a dict fragment so the caller splices it in at the position bench.c
    prints it -- after `probe`, before `threads`. Absence is a state the producer
    can genuinely be in, so it is expressible here; see canon_matrix_id()."""
    if sc.matrix_unstamped or host.matrix_unstamped:
        return {}
    if host.matrix_override:
        mid, cases = host.matrix_override
        return {"matrix_id": mid, "matrix_cases": cases}
    mid, cases = matrix_stamp(sc)
    return {"matrix_id": mid, "matrix_cases": cases}


def arm_sha(sc: Scenario, host: HostSpec, arm: Arm):
    """The OpenBLAS SHA this arm was built from, on this host.

    Per-host, because "two hosts built the library from different trees" is a
    scenario: the analysis compares their records as one arm and, before the
    blas_sha check existed, could not tell."""
    if arm.library == "openblas" and host.blas_sha_override:
        return host.blas_sha_override
    if arm.library == "armpl":
        # build-libs.sh reads the version out of the install directory, so an arm
        # it could not find has an EMPTY sha, not a plausible one: `arm_record armpl
        # native "" false true "ARMPL_DIR unset or not a directory"`.
        return "armpl-24.10" if arm.manifest_built else ""
    if arm.library == "blis":
        # The same sha the toolchain record carries for BLIS. A fixture in which
        # the manifest and the toolchain record disagree about one library's tree
        # would be planting a blas_sha finding nobody asked for.
        return "b" * 40
    return sc.blas_sha_overrides.get((arm.library, arm.target), sc.blas_sha)


def bench_records(sc: Scenario, host: HostSpec):
    """src/bench.c emit(), field for field."""
    recs = []
    conds = conditions(sc.routines, sc.level1, sc.transposes)
    mfields = matrix_fields(sc, host)
    # A diverging replicate pass is generated by giving the two kernel-set arms
    # each other's gain, not by editing gflops afterwards. Both the target
    # mechanism (openblas/V1 vs openblas/V2) and the coretype mechanism
    # (DYNAMIC/V1 vs DYNAMIC/V2) invert together, because a pass in which the two
    # mechanisms disagreed would be testing something else.
    gain_of = {a.key: a for a in sc.arms}
    if host.swap_v1_v2:
        swap = {V1: V2, V2: V1}
        gain_of = {}
        for a in sc.arms:
            lib, tgt, ct = a.key
            other = (lib, swap.get(tgt, tgt), swap.get(ct, ct))
            gain_of[a.key] = next((b for b in sc.arms if b.key == other), a)
    for arm in sc.arms:
        if not arm.measured or host.failure_reason(arm) is not None or host.lost(arm):
            continue
        eff = gain_of[arm.key]
        sha = arm_sha(sc, host, arm)
        boost = 1.0
        for token, mult in host.pass_boost.items():
            if token in (arm.target, arm.coretype):
                boost *= mult
        for threads in host.threads:
            # The provenance prefix bench.c's emit_prefix() shares across all three
            # record kinds. Built once here rather than spelled out per kind, for the
            # reason emit_prefix() exists: a record kind carrying a SUBSET of these
            # fields is worse than one carrying none, because decompose.py's role gate
            # and instance dispatch both read them and a missing `role` silently files
            # the record under whatever role the reader defaulted to.
            prov = {
                "run_id": host.run_id,
                "host": f"ip-10-0-0-{abs(hash(host.instance_id)) % 200}",
                "instance": host.instance_type,
                "library": arm.library,
                "target": arm.target,
                "build": "synthetic",
                "blas_sha": sha,
                "coretype": arm.coretype,
                "thread_backend": arm.thread_backend,
                "pin_policy": pin_policy_for(threads),
                "arch_selected": host.dynamic_selection,
                "role": "campaign",
                # Matrix records, so "none". Emitted rather than left absent even
                # though decompose.py defaults absence to the same value: bench.c
                # writes the field on every record, and a fixture that relies on the
                # default is not testing the producer's output, it is testing the
                # consumer's fallback. The floor-overlap records are written by
                # floor_probe_records() below.
                "probe": "none",
                # Likewise 0 rather than absent. bench.c prints probe_rep from both
                # emit() and emit_prefix(), so a matrix record, a thread_prime and a
                # case_skipped all carry it; only the band ever sets it non-zero.
                "probe_rep": 0,
                **mfields,
                "threads": threads,
            }
            # The once-per-process priming call, one per (arm, threads) stream because
            # that is one process. Emitted for every stream a real sweep would have
            # produced one for, so that a fixture cannot make decompose.py's
            # unprimed-stream warning quiet by simply never having primed anything.
            if not arm.no_thread_prime:
                recs.append(
                    {
                        "record": "thread_prime",
                        **prov,
                        "case_seconds": 0.02,
                        "prime_n": 1024,
                        "prime_seconds": 0.004,
                        "note": "per-thread buffer pool allocated outside every measurement",
                    }
                )
            cap = large_cap_for_threads(threads)
            for routine, m, n, k, pad, incx, ta, tb in conds:
                # bench.c's thread-dependent large cap, and it comes BEFORE the
                # scenario's own omissions on purpose. The cap is a structural
                # property of the harness that every arm shares, so it produces an
                # explained absence; omit_sizes models an arm that LOST cases, which
                # is a hole. Collapsing the two would let a fixture plant a hole and
                # have it read as policy.
                #
                # Restricted to cases drawn from SIZES_LARGE, because that is where
                # bench.c's cap can reach: it lives inside sweep(), and the level-1
                # cases do not go through sweep() -- they are built directly from
                # `lens[]` in main(). A cap applied on m alone truncates ddot at
                # n=4194304, which is a 32 MB vector rather than a 512 MB working set
                # and answers a bandwidth question the report does read. Caught by the
                # incx-axis fixture, which lost its whole non-unit-stride axis.
                if m in SIZES_LARGE and m > cap:
                    recs.append(
                        {
                            "record": "case_skipped",
                            **prov,
                            "case_seconds": 0.00002,
                            "routine": routine,
                            "m": m,
                            "n": n,
                            "k": k,
                            "lda_pad": pad,
                            "incx": incx,
                            "min_seconds": min_seconds_for(m),
                            "reason": (
                                ""
                                if arm.skip_without_reason
                                else "large-regime size above the cap for this thread count: "
                                "answers no question the report reads, and is the most "
                                "expensive arithmetic in the campaign"
                            ),
                        }
                    )
                    continue
                if m in arm.omit_sizes or routine in arm.omit_routines:
                    continue
                if m in arm.omit_routine_sizes.get(routine, ()):
                    continue
                trans = f"{ta}{tb}" if ta is not None else None
                if trans is not None and trans in arm.omit_trans:
                    continue
                # The transpose enters the noise key only when it exists, so adding
                # the axis leaves every pre-existing fixture's numbers bit-identical:
                # a scenario whose verdict moved because an unrelated field joined a
                # hash would be a fixture change masquerading as an analysis change.
                noise_key = (host.run_id, arm.key, threads, routine, m, pad, incx)
                if trans is not None:
                    noise_key += (trans,)
                base = (
                    base_gflops(routine, m, threads)
                    * eff.multiplier(routine, m, incx, pad, trans)
                    * host.host_scale
                    * boost
                    * jitter(*noise_key, amp=arm.noise)
                )
                flops = case_flops(routine, m, n, k)
                # The real record first, then the lucky duplicate. Emission order
                # is deliberate: min-within-run must not be an artefact of the
                # aggregator happening to see the honest sample last.
                for dup in (1.0, arm.lucky_dup) if arm.lucky_dup else (1.0,):
                    gf = base * dup
                    if arm.zero_gflops:
                        gf, t_min, t_p50, t_p90 = 0.0, 0.0, 0.0, 0.0
                        note = "timer_resolution_outrun"
                        verified = False
                    else:
                        # t_* are derived from this record's own gflops, so the
                        # duplicate is internally consistent. A fixture whose own
                        # fields disagree cannot test an analysis that reads both.
                        t_min = flops / (gf * 1e9) if gf > 0 else 0.0
                        t_p50 = t_min * (1.0 + arm.spread)
                        t_p90 = t_min * (1.0 + 1.6 * arm.spread)
                        # daxpy/ddot fall to the default: they are checked too, and
                        # their note stays `incx=N` because that is the axis the
                        # case exists to probe -- bench.c appends the failure there
                        # rather than replacing it.
                        verified, note = VERIFY.get(routine, (True, f"incx={incx}"))
                        if routine in ("daxpy", "ddot"):
                            note = f"incx={incx}"
                        if routine in arm.verified_false_routines:
                            verified = False
                            note = (
                                f"incx={incx};corner_check_failed"
                                if routine in ("daxpy", "ddot")
                                else "corner_check_failed"
                            )
                        elif routine in arm.verified_null_routines:
                            verified, note = None, "corner_check_absent"
                    recs.append(
                        {
                            **prov,
                            "routine": routine,
                            "m": m,
                            "n": n,
                            "k": k,
                            "lda_pad": pad,
                            "incx": incx,
                            # Present only where the scenario asked for transposes,
                            # and in the position bench.c will write them: a field
                            # the producer does not emit must not appear in a
                            # fixture that claims to be faithful to the producer.
                            **({"transa": ta, "transb": tb} if ta is not None else {}),
                            "reps": 15,
                            "batch": 1,
                            "calls": 15,
                            # Modelled, in bench.c's printf position. Unlike reps/
                            # batch/calls above -- fixed placeholders no fixture reads
                            # -- this one carries a property the cost analysis reads,
                            # so it varies with the arm. See case_seconds_for().
                            "case_seconds": case_seconds_for(
                                t_min, min_seconds_for(m), case_bytes(routine, m, n, k)
                            ),
                            # Reproduced rather than modelled, and reproduced from
                            # t_min so a slow arm lands on the no-warmup path exactly
                            # where bench.c would put it. See timing_path_for().
                            "warmup_reps": timing_path_for(t_min, min_seconds_for(m))[0],
                            "cal_reused": timing_path_for(t_min, min_seconds_for(m))[1],
                            # Part of the comparison key, so it is emitted per
                            # regime the way bench.c does rather than left absent.
                            "min_seconds": min_seconds_for(m),
                            "timer_overhead_ns": 21.0,
                            "timer_res_ns": 1.0,
                            "t_min": t_min,
                            "t_p50": t_p50,
                            "t_p90": t_p90,
                            "gflops": round(gf, 6),
                            "gflops_p50": round(gf / (1.0 + arm.spread), 6) if gf else 0.0,
                            "verified": verified,
                            "note": note,
                        }
                    )
    return recs


# src/bench.c's OVERLAP_SIZES, hand-copied like the size ladders and asserted
# against the producer in gates/p1.sh section 2 for the same reason: there is
# nothing to import from a C file, and a copy that drifts turns the fixture into a
# rigorous test of the wrong band.
OVERLAP_SIZES = (192, 224, 256, 320, 384)

# src/bench.c's OVERLAP_REPS, asserted against the producer for the same reason as
# the sizes -- and for one more. The rep count is not decoration: decompose.py keys
# a pair on `probe_rep`, so if the fixture emitted one rep while bench.c emitted
# four, every replication assertion here would pass against an analysis that had
# quietly lost the field, and the underpowered state the replication was bought to
# fix would look fixed.
OVERLAP_REPS = 4


def floor_probe_records(sc: Scenario, host: HostSpec):
    """src/bench.c run_floor_overlap(), field for field.

    Four modes, one per way the band can come out, because the analysis reports
    four statuses and a fixture that only exercises the happy one asserts nothing
    about the other three:

      agree     independent jitter on each floor. Signs scatter and every delta is
                well inside the band -> AGREES.
      bias      the short floor reads consistently low by `amount`, below
                --min-effect -> AGREES-WITH-BIAS. This is the mode worth having:
                the band passes AND the bias is reported, so a reader can discount
                a section-4 step by it.
      disagree  the short floor reads low by more than the band -> DISAGREES.
      order     whichever floor ran FIRST reads high, by less than the band. Because
                bench.c alternates the order, the floor-signed deltas then alternate
                while the order-signed ones do not, which is the whole point of
                alternating -> ORDER-CONFOUNDED.
      half      only one floor is emitted -> INCOMPLETE.

    `order` is the mode that would be unreachable if bench.c ran the short floor
    first every time: with a fixed order, "first reads high" and "short reads high"
    are the same dataset and no analysis could separate them. The fixture and the
    producer have to agree about the alternation, so the parity of the index is
    computed the same way here as there -- on `i + r`, so a size's reps carry both
    orders (see bench.c's ORDER-ALTERNATES note).

    `spec["legacy"]` writes ONE rep and omits `probe_rep` entirely, which is the
    shape of every dataset produced before 2026-08-20 -- including the P2 pass that
    raised the underpowered question. It is a state a real dataset is in, so it has
    to stay analysable, and it is the only way to reach the analysis's
    no-replication branch."""
    spec = sc.floor_probe
    if not spec:
        return []
    mode = spec.get("mode", "agree")
    amount = spec.get("amount", 0.0)
    legacy = bool(spec.get("legacy"))
    reps = 1 if legacy else OVERLAP_REPS
    mfields = matrix_fields(sc, host)
    recs = []
    for arm in sc.arms:
        if not arm.measured or host.failure_reason(arm) is not None or host.lost(arm):
            continue
        for threads in host.threads:
            for i, m in enumerate(OVERLAP_SIZES):
                base = (
                    base_gflops("dgemm", m, threads)
                    * arm.multiplier("dgemm", m, 1, 0, None)
                    * host.host_scale
                )
                for rep in range(reps):
                    # Same parity rule as bench.c, on the same two indices.
                    short_first = (i + rep) % 2 == 0
                    floors = (
                        (MIN_SECONDS_SMALL, MIN_SECONDS) if short_first else (MIN_SECONDS, MIN_SECONDS_SMALL)
                    )
                    for pos, floor in enumerate(floors):
                        is_short = floor == MIN_SECONDS_SMALL
                        if mode == "half" and not is_short:
                            continue
                        mult = 1.0
                        if mode in ("bias", "disagree") and is_short:
                            mult = 1.0 - amount
                        elif mode == "order" and pos == 0:
                            mult = 1.0 + amount
                        # Keyed on the floor as well, so `agree` gets two independent
                        # draws and the sign of the difference scatters. Without the
                        # floor in the key both records would draw the same jitter,
                        # every delta would be exactly 0, and the sign test would be
                        # vacuous rather than passed. Keyed on the rep for the same
                        # reason one level up: reps that drew identical noise would
                        # make every out-of-band pair reproduce by construction, which
                        # is the answer the replication is supposed to be able to
                        # withhold.
                        gf = base * mult * jitter(host.run_id, arm.key, threads, m, floor, rep, amp=arm.noise)
                        flops = case_flops("dgemm", m, m, m)
                        t_min = flops / (gf * 1e9)
                        rec = {
                            "run_id": host.run_id,
                            "host": f"ip-10-0-0-{abs(hash(host.instance_id)) % 200}",
                            "instance": host.instance_type,
                            "library": arm.library,
                            "target": arm.target,
                            "build": "synthetic",
                            "blas_sha": arm_sha(sc, host, arm),
                            "coretype": arm.coretype,
                            "thread_backend": arm.thread_backend,
                            "pin_policy": pin_policy_for(threads),
                            "arch_selected": host.dynamic_selection,
                            "role": "campaign",
                            "probe": "floor-overlap",
                            # The same stamp as the matrix records from this host.
                            # bench.c computes the id in its dry pass and holds it in
                            # a global for the rest of the process, so the probe
                            # cannot carry a different one -- and a fixture whose
                            # probe records were stamped differently from its matrix
                            # records would trip the mixed-matrix refusal from a
                            # single ordinary run, which no real dataset can do.
                            **mfields,
                            "threads": threads,
                            "routine": "dgemm",
                            "m": m,
                            "n": m,
                            "k": m,
                            "lda_pad": 0,
                            "incx": 1,
                            "reps": 15,
                            "batch": 1,
                            "calls": 15,
                            # The probe's whole point is that the same size ran under
                            # two floors, so this is the one place case_seconds must
                            # be modelled against `floor` rather than the size's
                            # regime default -- keying it off min_seconds_for(m) would
                            # report both halves of the band costing the same, which is
                            # the opposite of what the probe demonstrates.
                            "case_seconds": case_seconds_for(t_min, floor, case_bytes("dgemm", m, m, m)),
                            "min_seconds": floor,
                            "timer_overhead_ns": 21.0,
                            "timer_res_ns": 1.0,
                            "t_min": t_min,
                            "t_p50": t_min * (1.0 + arm.spread),
                            "t_p90": t_min * (1.0 + 1.6 * arm.spread),
                            "gflops": round(gf, 6),
                            "gflops_p50": round(gf / (1.0 + arm.spread), 6),
                            "verified": True,
                            # The producer puts the pair position here, and the
                            # analysis reads it to tell a floor effect from a drift.
                            "note": "floor_probe_first" if pos == 0 else "floor_probe_second",
                        }
                        # After `probe`, where bench.c prints it. Spliced rather than
                        # written inline above so `legacy` can leave it out without a
                        # second copy of the record -- two copies is how the fixture
                        # and the producer drift.
                        if not legacy:
                            items = list(rec.items())
                            at = [k for k, _ in items].index("probe") + 1
                            rec = dict([*items[:at], ("probe_rep", rep), *items[at:]])
                        recs.append(rec)
    return recs


def measurements(bench):
    """The measurement records out of a bench stream.

    bench.c's bench-*.ndjson is not homogeneous: emit_prefix() also writes
    `thread_prime` and `case_skipped` records, which carry the provenance prefix and
    a `case_seconds` but no gflops and no timing. Anything deriving a NUMBER from
    the stream has to drop them first, and the filter is a named function rather
    than an inline `if` at each site because the failure mode is silent in one
    direction: a `case_skipped` record read as a measurement is a case at zero
    GFLOP/s, which is the shape of a real defect."""
    return [r for r in bench if not r.get("record")]


def honest_records(bench):
    """One record per (run_id, arm, condition) -- the slower one, where an Arm
    planted a lucky duplicate.

    roofline.c measures peak_fma with its own FMA chain, so a re-run appended into
    bench-*.ndjson cannot move it, and deriving peak_fma from the duplicate would make
    the fixture's own provenance disagree with its measurements. That used to matter
    more: it would also have fired standing order 1's headroom flag on a fixture whose
    stated claim is 'the cross stays a null'. The flag is retired, so what is left is
    the faithfulness argument, which stands on its own."""
    best = {}
    for r in measurements(bench):
        key = (
            r["run_id"],
            r["library"],
            r["target"],
            r["coretype"],
            r["threads"],
            r["routine"],
            r["m"],
            r["n"],
            r["k"],
            r["lda_pad"],
            r["incx"],
            r.get("transa"),
            r.get("transb"),
        )
        cur = best.get(key)
        if cur is None or r["gflops"] < cur["gflops"]:
            best[key] = r
    return list(best.values())


def roofline_records(host: HostSpec, bench):
    """src/roofline.c. peak_fma is derived from the best large dgemm actually
    generated, times host.peak_factor, so a scenario sets the relationship between the
    two with one number.

    Nothing in the report reads that relationship any more -- standing order 1's
    cross-check is retired and section 6 prints peak_fma as provenance -- so the knob
    is kept for exactly one purpose: `peak-fma-retired` sets it above 1 and asserts the
    report stays silent. Keep emitting these records regardless. They are provenance,
    `gates/p2.sh` requires the file family, and roofline.c's own optimizer-hazard abort
    (standing order 2) is what they came from."""
    recs = []
    for threads in host.threads:
        best = max(
            [
                r["gflops"]
                for r in honest_records(bench)
                if r["threads"] == threads and r["routine"] == "dgemm" and regime(r["m"]) == "large"
            ]
            or [1.0]
        )
        pk = best * host.peak_factor
        common = {
            "run_id": host.run_id,
            "host": f"ip-10-0-0-{abs(hash(host.instance_id)) % 200}",
            "instance": host.instance_type,
            "build": "synthetic",
            "role": "campaign",
            # roofline.c gained these on 2026-08-20 and this fixture has to follow
            # it, or the copy drifts from the producer -- which is the one failure
            # mode that turns every scenario into a rigorous test of the wrong
            # experiment. `pin_policy` was the provenance gap: standing order 9 says
            # record the binding per arm, bench.c did and roofline did not, on the
            # very instrument that shows the t>=128 per-core cliff. The place map is
            # there to rule out threads doubling up on cores before an instance is
            # launched to chase that cliff -- a place count below the thread count
            # explains a pure-compute efficiency drop with no reference to NUMA.
            "pin_policy": pin_policy_for(threads),
            "omp_places": threads,
            "omp_place_procs": 1,
            "omp_place_procs_total": threads,
        }
        if threads == 1:
            recs.append(
                {
                    **common,
                    "threads": 1,
                    "metric": "peak_fma",
                    "accumulators": 12,
                    "gflops_f64": round(pk, 4),
                    "gflops_f32": round(2 * pk, 4),
                }
            )
        else:
            recs.append(
                {
                    **common,
                    "threads": threads,
                    "metric": "peak_fma_allcore",
                    "accumulators": 12,
                    "gflops_f64": round(pk, 4),
                    "scaling_efficiency": 0.94,
                }
            )
        recs.append(
            {
                **common,
                "threads": threads,
                "metric": "bandwidth",
                "array_bytes": 536870912,
                "triad_gbs": 190.0,
            }
        )
    return recs


def manifest_records(sc: Scenario, host: HostSpec):
    """scripts/build-libs.sh arm_record() plus the toolchain record. Stamped with
    instance and role by run-matrix.sh, which is why those two come first."""
    recs = []
    seen = set()
    for arm in sc.arms:
        if arm.omit_manifest or not arm.in_manifest:
            continue
        if (arm.library, arm.target) in seen:
            continue
        seen.add((arm.library, arm.target))
        recs.append(
            {
                "instance": host.instance_type,
                "role": "campaign",
                "record": "arm",
                "library": arm.library,
                "target": arm.target,
                # build-libs.sh's arm_record() gained this on 2026-08-20 and it is
                # null for every library that does not read its own config back --
                # which is all of them except BLIS. Deliberately NOT a copy of
                # `target`: standing order 10's failure mode is a request echoed as
                # if it were an observation, and defaulting the field to the request
                # would build that mistake into the fixture.
                "target_effective": arm.target_effective,
                "coretype": None,
                "blas_sha": arm_sha(sc, host, arm),
                "built": arm.manifest_built,
                "runnable": arm.manifest_runnable,
                "reason": arm.manifest_reason,
                "thread_backend": arm.thread_backend,
                "exe": f"gbb-{arm.library}-{arm.target}",
                "prefix": f"/opt/gbb/{arm.library}-{arm.target}",
                "sve_kernels": arm.sve_kernels if arm.library == "openblas" else "n/a",
            }
        )
    if host.reference_arm:
        # build-libs.sh emits this unconditionally, built:true and runnable:true,
        # for a library run-matrix.sh never times. It is therefore a manifest arm
        # with no bench records by design -- the shape that put 36
        # MISSING-UNEXPLAINED cells and exit bit 4 on an otherwise clean dataset.
        recs.append(
            {
                "instance": host.instance_type,
                "role": "campaign",
                "record": "arm",
                "library": "reference",
                "target": "native",
                "target_effective": None,
                "coretype": None,
                "blas_sha": "",
                "built": True,
                "runnable": True,
                "reason": "correctness control only, not timed",
                "thread_backend": "pthreads",
                "exe": "gbb-reference",
                "prefix": "",
                "sve_kernels": "n/a",
            }
        )
    recs.append(
        {
            "instance": host.instance_type,
            "role": "campaign",
            "record": "toolchain",
            "cc": "gcc",
            "cc_version": "gcc (GCC) 11.4.1 20230605",
            "kernel": "6.1.0-synthetic",
            "libc": "ldd (GNU libc) 2.34",
            "openblas_ref": "v0.3.32",
            "openblas_sha": host.blas_sha_override or sc.blas_sha,
            "blis_ref": "1.0",
            "blis_sha": "b" * 40,
            "native_target": host.core_name,
            "cross_target": "NEOVERSEV1",
            "host_sve": host.has_sve,
            "host_sve2": host.has_sve2,
        }
    )
    return recs


def arm_effective(host: HostSpec, arm: Arm):
    """What openblas_get_corename() reports for this arm.

    An unforced arm gets whatever DYNAMIC_ARCH chose. A forced arm gets its own
    request, EXCEPT where OpenBLAS resolves the request to another name, which
    `coretype_aliases` expresses per host.

    Which direction the campaign's own hosts take is settled by reading cc3fc1e,
    not by standing order 8: gotoblas_NEOVERSEV2 is `#define`d to
    gotoblas_NEOVERSEN2 unconditionally and `gotoblas_corename()` checks V2 before
    N2 on that one pointer, so a V2 request reports back `neoversev2` -- it verifies
    exactly, and it is the NEOVERSEN2 request that comes back under another name and
    is declined `alias_duplicate`. `p2-host` plants that direction because it is what
    `run-matrix.sh` will write. `aliased-coretype` plants the other one on purpose;
    see its docstring for why that is still a scenario worth having."""
    if arm.library != "openblas":
        return "n/a"
    if arm.coretype == "unforced":
        return host.dynamic_selection
    return host.coretype_aliases.get(arm.coretype, arm.coretype).lower()


def census_records(sc: Scenario, host: HostSpec, bench, probe=()):
    """scripts/run-matrix.sh census(). Note the coretype spelling: run_arm is
    called with an empty $ct for the unforced arm, so the real census writes ""
    where bench.c writes "unforced". Reproduced deliberately -- that mismatch is
    one of the two bugs this file found.

    `probe` is folded into the `records` count and nowhere else. The runner takes
    that number from `wc -l` on the whole output file, so on real data it includes
    the floor-overlap records; counting only the matrix here would make the fixture
    disagree with the producer by exactly the probe's size. Nothing in the analysis
    reads the field -- which is why the drift would have gone unnoticed, and why it
    is worth removing rather than tolerating."""
    recs = []
    for threads in host.threads:
        recs.append(
            {
                "record": "arm_outcome",
                "run_id": host.run_id,
                "role": "campaign",
                "host": f"ip-10-0-0-{abs(hash(host.instance_id)) % 200}",
                "instance": host.instance_type,
                "library": "roofline",
                "target": "native",
                "coretype": "",
                "coretype_effective": "",
                "threads": threads,
                "status": "measured",
                "exit_code": 0,
                "records": 2,
                "thread_backend": "",
                "pin_policy": pin_policy_for(threads),
                "reason": "",
            }
        )
    if host.reference_arm and host.reference_censused:
        for threads in host.threads:
            recs.append(
                {
                    "record": "arm_outcome",
                    "run_id": host.run_id,
                    "role": "campaign",
                    "host": f"ip-10-0-0-{abs(hash(host.instance_id)) % 200}",
                    "instance": host.instance_type,
                    "library": "reference",
                    "target": "native",
                    "coretype": "",
                    "coretype_effective": "n/a",
                    "threads": threads,
                    "status": "skipped",
                    "exit_code": 0,
                    "records": 0,
                    "thread_backend": "pthreads",
                    "pin_policy": "",
                    "reason": "netlib correctness control, never timed -- not a performance arm",
                }
            )
    if host.escalation_ack is not None:
        recs.append(
            {
                "record": "escalation_ack",
                "run_id": host.run_id,
                "role": "campaign",
                "host": f"ip-10-0-0-{abs(hash(host.instance_id)) % 200}",
                "note": host.escalation_ack,
            }
        )
    for arm in sc.arms:
        if arm.omit_census:
            continue
        # An arm that died on this pass only. run-matrix.sh censuses it
        # `runtime_failed` with the harness exit status, which is an explanation:
        # standing order 11 wants the gap to carry a reason even when the same arm
        # ran fine on the other two passes.
        failed = host.failure_reason(arm)
        for threads in host.threads:
            n = sum(
                1
                for r in list(bench) + list(probe)
                if r["threads"] == threads and (r["library"], r["target"], r["coretype"]) == arm.key
            )
            recs.append(
                {
                    "record": "arm_outcome",
                    "run_id": host.run_id,
                    "role": "campaign",
                    "host": f"ip-10-0-0-{abs(hash(host.instance_id)) % 200}",
                    "instance": host.instance_type,
                    "library": arm.library,
                    "target": arm.target,
                    # The runner passes "" for the unforced arm and the coretype
                    # name for a forced one.
                    "coretype": "" if arm.coretype == "unforced" else arm.coretype,
                    # Per-ARM, not per-host. The previous version stamped
                    # host.dynamic_selection on every openblas arm, so the
                    # V1-forced and V2-forced arms carried the same effective
                    # coretype -- which real data never does, and which is the
                    # field the aliasing finding is read off.
                    "coretype_effective": arm_effective(host, arm),
                    "threads": threads,
                    "status": "runtime_failed" if failed else arm.census_status,
                    "exit_code": 134 if failed else (0 if arm.census_status == "measured" else 4),
                    "records": n,
                    "thread_backend": arm.thread_backend,
                    "pin_policy": pin_policy_for(threads),
                    "reason": failed or arm.census_reason,
                }
            )
    return recs


def env_record(host: HostSpec):
    """scripts/capture-env.sh's one big printf."""
    return {
        "run_id": host.run_id,
        "captured_at": "2026-08-19T00:00:00Z",
        "host": f"ip-10-0-0-{abs(hash(host.instance_id)) % 200}",
        "instance_type": host.instance_type,
        "instance_id": host.instance_id,
        "az": "us-east-1a",
        "midr": host.midr,
        "midr_implementer": "0x41",
        "midr_part": host.midr_part,
        "core_name": host.core_name,
        "midr_scalar_source": "cpu0",
        "cpu0_midr": host.midr,
        "cpu0_midr_part": host.midr_part,
        "cpu0_core_name": host.core_name,
        "midr_uniform": host.midr_uniform,
        "midr_cpus_read": host.cores,
        "midr_distinct": [host.midr],
        "core_clusters": host.core_clusters(),
        "cores_total": host.cores,
        "cpus_online": host.cpus_online,
        "cpus_online_list": f"0-{host.cpus_online - 1}",
        "cpus_affinity": host.cpus_affinity,
        "cpus_affinity_list": f"0-{host.cpus_affinity - 1}",
        "cgroup_version": "2",
        "cgroup_cpu_max": "max 100000",
        "cgroup_cpu_quota_us": None,
        "cgroup_cpu_period_us": 100000,
        "cgroup_cpu_limit": host.cgroup_cpu_limit,
        "sockets": host.sockets,
        "numa_nodes": host.numa_nodes,
        "threads_per_core": host.threads_per_core,
        "l1d": "64K",
        "l2": "1M",
        "l3": "32M",
        "cpu_features": "fp asimd sve" if host.has_sve else "fp asimd",
        "has_sve": host.has_sve,
        "has_sve2": host.has_sve2,
        "has_bf16": True,
        "has_i8mm": True,
        "has_sme": False,
        "sve_vector_length_bytes": str(host.sve_vl) if host.has_sve else "",
        "sve_default_vl_bytes": host.sve_vl if host.has_sve else None,
        "cpufreq_governor": host.governor,
        "cpufreq_cur_khz": "2600000",
        "kernel": "6.1.0-synthetic",
        "openblas_dynamic_selection": host.dynamic_selection,
        "openblas_dynamic_config": "OpenBLAS 0.3.32 DYNAMIC_ARCH",
        "openblas_dynamic_probe_status": host.dynamic_probe_status,
        "openblas_coretype_forcing": host.forcing,
        "openblas_dynamic_probe_dir": "/opt/gbb/openblas-DYNAMIC",
        "openblas_dynamic_lib_resolved": "/opt/gbb/openblas-DYNAMIC/lib/libopenblas.so.0",
        "warnings": list(host.warnings),
    }


def write_scenario(sc: Scenario, root: pathlib.Path):
    res = root / "results"
    res.mkdir(parents=True, exist_ok=True)
    for host in sc.hosts:
        bench = bench_records(sc, host)
        # One stream, as bench.c writes it, but the probe records are kept out of
        # everything derived FROM the matrix. census_records() classifies expected
        # (arm, cell) coverage and roofline_records() takes the measured peak: a
        # probe record is neither an expected cell nor a candidate for peak, and
        # letting it into either would make the fixture assert that the analysis
        # tolerates a leak it should never see.
        probe = floor_probe_records(sc, host)
        _w(res / f"bench-{host.run_id}.ndjson", bench + probe)
        if host.foreign_role is not None:
            # An instrument-check host's records sitting in a campaign directory,
            # which is what one `aws s3 sync` of a bucket holding both prefixes
            # produces. Faster than the campaign host on purpose: if the analysis
            # pools them, it pools them into standing order 1's measured-peak
            # denominator too and every efficiency figure on this host deflates. The
            # role filter is the only thing checking that denominator now -- the
            # `peak_fma` cross-check that once nominally did is retired.
            leaked = []
            for r in bench:
                q = dict(r)
                q["role"] = host.foreign_role
                q["run_id"] = f"instr-{host.foreign_role}-castor"
                # The non-measurement records are copied across unscaled rather than
                # dropped. An instrument-check stream that primed its thread pool and
                # declined the same large cases is what the S3 path actually produces,
                # and a leak fixture whose foreign stream is missing them would be
                # testing a leak shape that cannot occur.
                if not r.get("record"):
                    q["gflops"] = r["gflops"] * host.foreign_role_gain
                    q["gflops_p50"] = r["gflops_p50"] * host.foreign_role_gain
                leaked.append(q)
            _w(res / f"bench-instr-{host.foreign_role}-castor.ndjson", leaked)
        if host.roofline_present:
            _w(res / f"roofline-{host.run_id}.ndjson", roofline_records(host, bench))
        _w(res / f"manifest-{host.run_id}.ndjson", manifest_records(sc, host))
        _w(res / f"census-{host.run_id}.ndjson", census_records(sc, host, bench, probe))
        if host.env_present:
            (res / f"env-{host.run_id}.json").write_text(json.dumps(env_record(host), indent=2) + "\n")
        (res / f"topology-{host.run_id}.txt").write_text(
            f"=== numactl -H ===\navailable: {host.numa_nodes} nodes (0"
            + (f"-{host.numa_nodes - 1}" if host.numa_nodes > 1 else "")
            + f")\nnode 0 cpus: {' '.join(str(i) for i in range(host.cores))}\n"
            f"\n=== lscpu ===\nArchitecture: aarch64\nCPU(s): {host.cores}\n"
            f"Thread(s) per core: {host.threads_per_core}\n"
        )
    (root / "truth.json").write_text(
        json.dumps(
            {
                "scenario": sc.name,
                "description": sc.description,
                "hosts": [
                    {"instance_type": h.instance_type, "instance_id": h.instance_id, "run_id": h.run_id}
                    for h in sc.hosts
                ],
                "arms": [
                    {
                        "arm": "/".join(a.key),
                        "measured": a.measured,
                        "gain": a.gain,
                        "census_status": a.census_status,
                    }
                    for a in sc.arms
                ],
                "expect": sc.expect,
            },
            indent=2,
        )
        + "\n"
    )
    return res


def _w(path, recs):
    path.write_text("".join(json.dumps(r) + "\n" for r in recs))


# ---- scenarios ---------------------------------------------------------------
# Each is one claim about decompose.py, with the expectation that makes the claim
# falsifiable. The `expect` list is checked by `check` below; every predicate it
# uses is implemented there, so an expectation cannot be satisfied by a typo.

V1 = "NEOVERSEV1"
V2 = "NEOVERSEV2"
N2 = "NEOVERSEN2"

# scripts/run-matrix.sh's alias_duplicate reason, verbatim. Hand-copied like the
# rest of the census vocabulary, because there is nothing to import from a shell
# script -- and load-bearing beyond faithfulness: `p2-host` asserts that the reason
# reaches the report, so a drift here is a drift in what gate P2 rehearses against.
ALIAS_DUPLICATE_REASON = (
    "requested NEOVERSEN2; openblas_get_corename() reports 'neoversev2', which the "
    "NEOVERSEV2 arm is already measuring. The two requests select the same kernel set "
    "-- that is the finding, and measuring it twice would read as two independent arms."
)


def _host(**kw):
    return HostSpec(
        instance_type=kw.pop("instance_type", "c7g.metal"),
        instance_id=kw.pop("instance_id", "i-0000000000000001"),
        run_id=kw.pop("run_id", "synth-c7g-pass1"),
        **kw,
    )


def _arms(
    v1_gain=None,
    v2_gain=None,
    shipped_gain=None,
    routines=None,
    v1_incx=None,
    v1_trans=None,
    **kw,
):
    """The standard six-arm set: the shipped arm, two forced coretypes, two
    static targets, and ArmPL as the named reference.

    `routines` restricts where the gain applies. Left unset the gain is broad,
    which is what a campaign-level verdict needs -- the verdict is a majority over
    all comparable cells, so an effect confined to a few routines yields MIXED
    however large it is, and `v1-ahead-broad` and `v1-ahead-small` are separate
    scenarios rather than one for that reason.

    That MIXED is not free, and this docstring asserted it before it was true: a
    minority effect used to land on the NULL branch whenever the unaffected
    routines held 60% of the cells, which `full-routine-set` demonstrated. It is
    `coherent_subsets()` in decompose.py that makes the sentence above correct."""
    v1_gain = v1_gain or {}
    v2_gain = v2_gain or {}
    shipped_gain = shipped_gain if shipped_gain is not None else v2_gain
    common = dict(gain_routines=routines, **kw)
    # The stride and transpose gains go on the V1 arms only, by both mechanisms:
    # an axis-localised effect that showed up under one mechanism and not the other
    # would be testing the label plumbing, not the axis.
    v1_axes = {"gain_incx": v1_incx or {}, "gain_trans": v1_trans or {}}
    return [
        Arm("openblas", "DYNAMIC", "unforced", gain=shipped_gain, **common),
        Arm("openblas", "DYNAMIC", V1, gain=v1_gain, **v1_axes, in_manifest=False, **common),
        Arm("openblas", "DYNAMIC", V2, gain=v2_gain, in_manifest=False, **common),
        Arm("openblas", V1, "unforced", gain=v1_gain, **v1_axes, **common),
        Arm("openblas", V2, "unforced", gain=v2_gain, **common),
        Arm("armpl", "native", "unforced", thread_backend="openmp", **kw),
    ]


def flat(value, regimes=REGIMES):
    """The same multiplier in every regime -- an effect with no size structure."""
    return dict.fromkeys(regimes, value)


def sc_null():
    return Scenario(
        name="null",
        description=(
            "The V1 and V2 kernel sets are identical to within noise, and both sit 12% "
            "behind ArmPL at every size. The campaign's publishable negative result."
        ),
        hosts=[_host()],
        arms=_arms(v1_gain=flat(0.88), v2_gain=flat(0.88)),
        expect=[
            {"kind": "verdict_code", "one_of": ["NULL"]},
            {"kind": "exit_bits_clear", "bits": [2, 4, 8, 16]},
            {"kind": "stdout_contains", "text": "publish the negative result"},
            {"kind": "cross_verdicts_all", "expect": "parity", "min_rows": 12},
            # The subset guard that keeps `full-routine-set` off the NULL branch
            # must find nothing here. A null result is a publishable outcome of
            # this campaign, so a guard that can invent a localised effect out of
            # a genuine null is worse than the false negative it was added to fix.
            {"kind": "coherent_subsets", "expect": []},
            # Section 1 as well as section 2. The kernel sets being at parity with
            # each other says nothing about where OpenBLAS stands against ArmPL,
            # and section 1 is the table the write-up quotes: the planted 12%
            # deficit has to appear as a 12% deficit, on the arm the wheels ship.
            {
                "kind": "deficit_where",
                "shipped_only": True,
                "routine": "dgemm",
                "op": ">=",
                "value": 0.08,
                "min_rows": 4,
            },
            {"kind": "deficit_where", "shipped_only": True, "routine": "dgemm", "op": "<=", "value": 0.17},
            {"kind": "deficit_shipped", "min_rows": 12},
            {"kind": "stdout_contains", "text": "SHIPPED"},
            {"kind": "json_number", "path": "coverage.missing_unexplained", "op": "==", "value": 0},
            {"kind": "json_number", "path": "coverage.partial", "op": "==", "value": 0},
            {"kind": "anomaly_kind_absent", "kind_name": "escalate"},
        ],
    )


def sc_v1_ahead_broad():
    return Scenario(
        name="v1-ahead-broad",
        description=(
            "The V1 kernel set is 22% ahead of the V2 set at every size and every "
            "routine -- an effect broad enough to carry a campaign-level verdict."
        ),
        hosts=[_host()],
        arms=_arms(v1_gain=flat(1.22)),
        expect=[
            {"kind": "verdict_code", "one_of": ["V1-SET-AHEAD"]},
            {"kind": "exit_bits_clear", "bits": [2, 4, 8, 16]},
            {
                "kind": "cross_verdicts_where",
                "regime": "small",
                "routine": "dgemm",
                "expect": "V1-set-ahead",
                "min_rows": 2,
            },
            {
                "kind": "cross_verdicts_where",
                "regime": "large",
                "routine": "dgemm",
                "expect": "V1-set-ahead",
                "min_rows": 2,
            },
            {"kind": "cross_delta_where", "regime": "large", "routine": "dgemm", "op": ">=", "value": 0.15},
            {"kind": "both_mechanisms_agree"},
        ],
    )


def sc_v1_ahead_small():
    return Scenario(
        name="v1-ahead-small",
        description=(
            "The effect lives only in the small regime: V1 set +27% small, +9% medium, "
            "parity large, and the shipped arm's deficit against ArmPL is concentrated "
            "there too. This is the shape the missing GEMM_SMALL_* path would produce."
        ),
        hosts=[_host()],
        arms=_arms(
            v1_gain={"small": 0.95, "medium": 1.00, "large": 1.00},
            v2_gain={"small": 0.75, "medium": 0.92, "large": 1.00},
        ),
        expect=[
            {"kind": "cross_verdicts_where", "regime": "small", "expect": "V1-set-ahead", "min_rows": 2},
            {
                "kind": "cross_verdicts_where",
                "regime": "large",
                "routine": "dgemm",
                "expect": "parity",
                "min_rows": 2,
            },
            {"kind": "regime_gap_cross", "op": ">=", "value": 0.15, "min_rows": 2},
            # MIXED, positively. An effect in one regime out of three cannot carry
            # a campaign-level direction, and saying so is the verdict majority
            # doing its job -- DEFAULT_VERDICT_MAJORITY decided nothing any
            # scenario checked while this read `not_one_of`, so an analysis that
            # ignored the majority rule entirely stayed green. MIXED is also the
            # honest headline for this shape: "worth fixing, in the small regime".
            {"kind": "verdict_code", "one_of": ["MIXED"]},
            {"kind": "stdout_contains", "text": "CONSEQUENCE: deficit concentrated in the small regime"},
            # The same concentration in section 1, on the arm that ships: 25%
            # behind ArmPL in the small regime, level with it in the large. The
            # CONSEQUENCE line above is derived from the regime profile, so it can
            # be right while the table it points at is wrong.
            {
                "kind": "deficit_where",
                "shipped_only": True,
                "routine": "dgemm",
                "regime": "small",
                "op": ">=",
                "value": 0.15,
                "min_rows": 2,
            },
            {
                "kind": "deficit_where",
                "shipped_only": True,
                "routine": "dgemm",
                "regime": "large",
                "op": "<=",
                "value": 0.05,
                "min_rows": 2,
            },
            {"kind": "exit_bits_clear", "bits": [2, 4, 8, 16]},
        ],
    )


def sc_v2_ahead():
    return Scenario(
        name="v2-ahead",
        description=(
            "The V2 (N2) kernel set is 18% AHEAD of the V1 set everywhere -- the outcome "
            "in which the five-kernel choice was correct. The analysis must be able to "
            "reach this conclusion as readily as the opposite one."
        ),
        hosts=[_host()],
        arms=_arms(v2_gain=flat(1.18), shipped_gain=flat(1.18)),
        expect=[
            {"kind": "verdict_code", "one_of": ["V2-SET-AHEAD"]},
            {"kind": "stdout_contains", "text": "the NEON choice was right"},
            {"kind": "exit_bits_clear", "bits": [2, 4, 8, 16]},
        ],
    )


def sc_noise_only():
    return Scenario(
        name="noise-only",
        description=(
            "A 3% difference between the kernel sets, under the 5% floor. The false "
            "positive the audit reproduced: this must read as a null, not a weak hit."
        ),
        hosts=[_host()],
        arms=_arms(v1_gain=flat(1.03)),
        expect=[
            {"kind": "verdict_code", "one_of": ["NULL"]},
            {"kind": "cross_verdicts_all", "expect": "parity", "min_rows": 40},
            {"kind": "stdout_absent", "text": "V1-SET-AHEAD"},
            {"kind": "exit_bits_clear", "bits": [2, 4, 8, 16]},
            # The delta is BRACKETED, not just bounded above. Asserting only
            # "parity" passes on an analysis that lost the signal entirely and
            # reported 0.0 -- which is the same output for the right reason and the
            # worst possible reason. The planted 3% must still be visible in the
            # number while being judged sub-threshold in the verdict.
            {"kind": "cross_delta_where", "routine": "dgemm", "op": ">=", "value": 0.015},
            {"kind": "cross_delta_where", "routine": "dgemm", "op": "<=", "value": 0.05},
            # A sub-threshold difference is present on every routine here. If the
            # subset guard fired on that it would convert the audit's original
            # false positive into a "localised" one, which is the same error with a
            # better vocabulary.
            {"kind": "coherent_subsets", "expect": []},
        ],
    )


def sc_under_dispersion():
    return Scenario(
        name="under-dispersion",
        description=(
            "A 22% difference measured on a host whose within-run dispersion is 30%. "
            "A delta smaller than the spread that produced it is not a finding: the band "
            "must widen to the observed dispersion and swallow it, and the dispersion "
            "itself must be reported rather than silently absorbed."
        ),
        hosts=[_host()],
        # 0.30 is above --noisy-spread's 0.25 default, so the same fixture proves
        # both halves: the band widens AND the host is flagged noisy. A spread
        # under the threshold would have tested only the first.
        arms=_arms(v1_gain=flat(1.22), spread=0.30),
        expect=[
            {"kind": "cross_verdicts_all", "expect": "parity", "min_rows": 40},
            {"kind": "verdict_code", "one_of": ["NULL"]},
            {"kind": "band_at_least", "value": 0.28},
            {"kind": "anomaly_kind_present", "kind_name": "noisy"},
            # Without this the scenario's whole claim -- "a 22% delta swallowed by
            # a 30% band" -- is untested: parity plus a wide band plus a noisy flag
            # are all true of zero-effect data, so it passed with the effect
            # deleted. The delta must be large AND the verdict must still be
            # parity, which is the only shape that distinguishes "correctly
            # swallowed" from "never measured".
            {"kind": "cross_delta_where", "routine": "dgemm", "op": ">=", "value": 0.15},
        ],
    )


def sc_inverted():
    # The audit's actual false positive: V2 wins four of five large sizes, V1 wins
    # one size by 3x. max() over the regime published "V1 kernels win".
    v1_sizes = {2048: 0.90, 3072: 0.90, 4096: 3.00, 6144: 0.90, 8192: 0.90}
    return Scenario(
        name="inverted",
        description=(
            "V2 wins four of five large sizes; V1 wins one size by 3x. The regression "
            "for the max()-over-the-cell bug, which published '+4.8% V1 kernels win' on "
            "exactly this shape."
        ),
        hosts=[_host()],
        arms=[
            Arm("openblas", "DYNAMIC", "unforced", gain_routines=("dgemm", "dtrsm")),
            Arm("openblas", "DYNAMIC", V1, gain_sizes=v1_sizes, in_manifest=False),
            Arm("openblas", "DYNAMIC", V2, in_manifest=False),
            Arm("openblas", V1, "unforced", gain_sizes=v1_sizes),
            Arm("openblas", V2, "unforced"),
            Arm("armpl", "native", "unforced", thread_backend="openmp"),
        ],
        expect=[
            # Both halves are needed and the second is the one that was missing.
            # `not_expect` alone is satisfied by flat parity data, and by an
            # analysis that emits nothing at all -- which is a poor regression test
            # for a bug whose symptom was a confident wrong direction. An
            # adversarial pass confirmed this scenario stayed green with the
            # planted effect deleted. So: V1 must NOT be called ahead, AND V2 must
            # actually be found ahead, in the same rows, on the same fixture.
            {
                "kind": "cross_verdicts_where",
                "regime": "large",
                "routine": "dgemm",
                "not_expect": "V1-set-ahead",
                "min_rows": 2,
            },
            {
                "kind": "cross_verdicts_where",
                "regime": "large",
                "routine": "dgemm",
                "expect": "V2-set-ahead",
                "min_rows": 2,
            },
            {
                "kind": "cross_delta_where",
                "regime": "large",
                "routine": "dgemm",
                "op": "<=",
                "value": -0.05,
            },
            {"kind": "verdict_code", "not_one_of": ["V1-SET-AHEAD"]},
        ],
    )


def sc_missing_arm_explained():
    arms = _arms(v1_gain=flat(1.25))
    for a in arms:
        if a.coretype == V1 or a.target == V1:
            a.measured = False
            a.census_status = "unrunnable"
            a.census_reason = (
                "coreprobe failed for this coretype (SIGILL=132 means the kernel set "
                "needs ISA this host lacks)"
            )
    return Scenario(
        name="missing-arm-explained",
        description=(
            "The V1-set arms never ran, and the census says why. Absent must not read as "
            "a null: the verdict may not be NULL, and there may be no unexplained hole."
        ),
        hosts=[_host()],
        arms=arms,
        expect=[
            {"kind": "verdict_code", "not_one_of": ["NULL", "V1-SET-AHEAD", "V2-SET-AHEAD"]},
            {"kind": "verdict_code", "one_of": ["INCONCLUSIVE", "NO-DATA"]},
            {"kind": "exit_bits_clear", "bits": [4]},
            {"kind": "json_number", "path": "coverage.missing_unexplained", "op": "==", "value": 0},
            {"kind": "stdout_contains", "text": "NO DATA"},
            {"kind": "stdout_contains", "text": "needs ISA this host lacks"},
        ],
    )


def sc_missing_arm_unexplained():
    arms = _arms(v1_gain=flat(1.25))
    for a in arms:
        if a.coretype == V1 or a.target == V1:
            a.measured = False
            a.omit_census = True
    return Scenario(
        name="missing-arm-unexplained",
        description=(
            "The same absence with nothing accounting for it. This is the case that must "
            "raise a coverage hole and set exit bit 4 -- the difference between this "
            "scenario and missing-arm-explained is the whole point of the census."
        ),
        hosts=[_host()],
        arms=arms,
        expect=[
            {"kind": "exit_bits_set", "bits": [4]},
            {"kind": "json_number", "path": "coverage.missing_unexplained", "op": ">", "value": 0},
            {"kind": "stdout_contains", "text": "MISSING-UNEXPLAINED"},
            {"kind": "verdict_code", "not_one_of": ["NULL"]},
        ],
    )


def sc_dead_arm():
    arms = _arms()
    for a in arms:
        if a.coretype == V1 or a.target == V1:
            a.zero_gflops = True
    return Scenario(
        name="dead-arm",
        description=(
            "The V1-set arms produced 0.00 GFLOP/s at every size. Two dead arms printing "
            "'parity' is what the previous version did, and parity maps to 'publish the "
            "negative result'."
        ),
        hosts=[_host()],
        arms=arms,
        expect=[
            {"kind": "exit_bits_set", "bits": [2]},
            {"kind": "anomaly_kind_present", "kind_name": "zero_gflops"},
            {"kind": "json_number", "path": "inputs.excluded.zero_gflops", "op": ">", "value": 0},
            {"kind": "verdict_code", "not_one_of": ["NULL", "V2-SET-AHEAD"]},
        ],
    )


def sc_verify_fail():
    arms = _arms()
    for a in arms:
        if a.coretype == V1:
            a.verified_false_routines = ("dgemm",)
    return Scenario(
        name="verify-fail",
        description="The V1 coretype arm returns wrong answers for dgemm. A wrong answer poisons the record.",
        hosts=[_host()],
        arms=arms,
        expect=[
            {"kind": "exit_bits_set", "bits": [2]},
            {"kind": "anomaly_kind_present", "kind_name": "verification_failed"},
            {"kind": "stdout_contains", "text": "WRONG ANSWER, excluded"},
            # The first three assert only that the anomaly was PRINTED. Deleting
            # the `continue` after exc.verified_false.append() keeps all three
            # green while the wrong-answer arm re-enters the comparison, comes out
            # `parity`, and flips the campaign verdict to NULL -- "publish the
            # negative result", off the back of a kernel returning wrong answers.
            # The printed sentence "WRONG ANSWER, excluded" would then be a
            # sentence the analysis had made false. So assert the exclusion
            # arithmetic and the consequence: the affected cells must be NO DATA,
            # not parity, and the campaign must not read as a null.
            {"kind": "json_number", "path": "inputs.excluded.verified_false", "op": ">", "value": 0},
            {"kind": "cross_nodata_where", "routine": "dgemm", "mechanism": "coretype", "min_rows": 2},
            {"kind": "verdict_code", "not_one_of": ["NULL", "V1-SET-AHEAD", "V2-SET-AHEAD"]},
        ],
    )


def sc_sve_kernels_absent():
    arms = _arms()
    for a in arms:
        if a.library == "openblas":
            a.sve_kernels = "no"
    return Scenario(
        name="sve-kernels-absent",
        description=(
            "The installed libopenblas.a holds no SVE kernel symbols on a host that "
            "reports SVE. Standing order 8's quiet trigger: every arm still runs and "
            "still reports plausible numbers while the SVE axis measures nothing."
        ),
        hosts=[_host()],
        arms=arms,
        expect=[
            {"kind": "exit_bits_set", "bits": [2]},
            {"kind": "anomaly_kind_present", "kind_name": "escalate"},
            {"kind": "stdout_contains", "text": "no SVE kernel symbols"},
            {"kind": "stdout_contains", "text": "Stop and escalate"},
            {"kind": "host_state", "instance": "c7g.metal", "expect": "ESCALATE"},
        ],
    )


def sc_sve_kernels_absent_neon_host():
    arms = _arms()
    for a in arms:
        if a.library == "openblas":
            a.sve_kernels = "no"
    return Scenario(
        name="sve-kernels-absent-neon-host",
        description=(
            "The same build with no SVE kernels, on a host with no SVE. Here it is the "
            "correct outcome and must not escalate -- the paired negative control for "
            "sve-kernels-absent."
        ),
        hosts=[
            _host(
                instance_type="c6g.metal",
                run_id="synth-c6g-pass1",
                has_sve=False,
                core_name="NEOVERSEN1",
                midr_part="0xd0c",
                dynamic_selection="neoversen1",
                sve_vl=0,
            )
        ],
        arms=arms,
        expect=[
            {"kind": "anomaly_kind_absent", "kind_name": "escalate"},
            {"kind": "host_state", "instance": "c6g.metal", "expect": "ADMISSIBLE"},
            {"kind": "exit_bits_clear", "bits": [2]},
        ],
    )


def sc_mislabelled():
    arms = _arms()
    for a in arms:
        if a.coretype == V1:
            a.measured = False
            a.census_status = "mislabelled"
            a.census_reason = (
                "bench.c's in-process openblas_get_corename() disagrees with the probe's "
                "'neoversev1'; the arm would have been measured under a label belonging to "
                "a different library or environment (standing order 10)"
            )
    return Scenario(
        name="mislabelled",
        description=(
            "bench.c refused an arm because its in-process corename disagreed with the "
            "runner's probe. Not a flake: every other forced-coretype label on the host "
            "came from that same probe."
        ),
        hosts=[_host()],
        arms=arms,
        expect=[
            {"kind": "exit_bits_set", "bits": [2]},
            {"kind": "anomaly_kind_present", "kind_name": "arch_selected_mismatch"},
            {"kind": "stdout_contains", "text": "refused to measure"},
            {"kind": "stdout_contains", "text": "is therefore unconfirmed"},
        ],
    )


def sc_no_provenance():
    return Scenario(
        name="no-provenance",
        description="Bench records exist and no env-*.json describes the host (standing order 5).",
        hosts=[_host(env_present=False)],
        arms=_arms(),
        expect=[
            {"kind": "exit_bits_set", "bits": [8]},
            {"kind": "anomaly_kind_present", "kind_name": "no_provenance"},
            {"kind": "host_state", "instance": "c7g.metal", "expect": "NO-PROVENANCE"},
        ],
    )


def sc_generic_armv8_on_sve():
    return Scenario(
        name="generic-armv8-on-sve",
        description=(
            "DYNAMIC_ARCH selected generic ARMV8 on a host that reports SVE. Standing "
            "order 8's loud trigger, and the one finding that outweighs every kernel "
            "question in the repo."
        ),
        hosts=[
            _host(
                dynamic_selection="armv8",
                warnings=("DYNAMIC_ARCH selected generic 'armv8' on a host that reports SVE.",),
            )
        ],
        arms=_arms(),
        expect=[
            {"kind": "exit_bits_set", "bits": [2]},
            {"kind": "anomaly_kind_present", "kind_name": "escalate"},
            {"kind": "stdout_contains", "text": "SVE detection failed"},
            {"kind": "host_state", "instance": "c7g.metal", "expect": "ESCALATE"},
        ],
    )


def sc_heterogeneous():
    clusters = (
        {
            "cpus": "0-9",
            "midr": "0x413fd850",
            "midr_implementer": "0x41",
            "midr_part": "0xd85",
            "core_name": "UNRECOGNISED",
            "cpu_count": 10,
            "cpuinfo_max_freq_khz": 4004000,
        },
        {
            "cpus": "10-19",
            "midr": "0x413fd870",
            "midr_implementer": "0x41",
            "midr_part": "0xd87",
            "core_name": "UNRECOGNISED",
            "cpu_count": 10,
            "cpuinfo_max_freq_khz": 2860000,
        },
    )
    return Scenario(
        name="heterogeneous",
        description=(
            "The DGX Spark shape: ten Cortex-X925 plus ten Cortex-A725, SVE2 at VL=128. "
            "Real silicon, useful as an instrument check, and every host-level number "
            "from it is a blend of microarchitectures."
        ),
        hosts=[
            _host(
                instance_type="castor.local",
                instance_id="i-castor",
                run_id="instrument-castor",
                threads=(1, 10),
                cores=20,
                cpus_online=20,
                cpus_affinity=20,
                has_sve2=True,
                sve_vl=16,
                midr_uniform=False,
                clusters=clusters,
                core_name="UNRECOGNISED",
                midr_part="0xd85",
                dynamic_selection="armv8sve",
            )
        ],
        arms=_arms(),
        expect=[
            {"kind": "exit_bits_set", "bits": [2]},
            {"kind": "anomaly_kind_present", "kind_name": "host_invalid"},
            {"kind": "stdout_contains", "text": "heterogeneous cores"},
            {"kind": "host_state", "instance": "castor.local", "expect": "INADMISSIBLE"},
            {"kind": "verdict_code", "one_of": ["INCONCLUSIVE"]},
        ],
    )


def sc_forcing_unavailable():
    return Scenario(
        name="forcing-unavailable",
        description=(
            "OPENBLAS_CORETYPE forcing does not work on this build, so every forced arm's "
            "coretype label is a claim about a library that ignored it."
        ),
        hosts=[_host(forcing="unavailable")],
        arms=_arms(v1_gain=flat(1.25)),
        expect=[
            {"kind": "exit_bits_set", "bits": [2]},
            {"kind": "anomaly_kind_present", "kind_name": "forced_coretype_excluded"},
            {"kind": "host_state", "instance": "c7g.metal", "expect": "INADMISSIBLE"},
            {"kind": "verdict_code", "not_one_of": ["V1-SET-AHEAD", "NULL"]},
        ],
    )


def sc_peak_fma_retired():
    """The negative fixture for a retired check, which is the only kind of fixture a
    retirement can have.

    Standing order 1's `peak_fma` cross-check was retired 2026-08-20 (Scott's call).
    It was justified on one case the empirical denominator cannot see -- every arm on a
    host being bad, which moves the ceiling down with the arms -- and it cannot detect
    that case: `roofline.c` declares `peak_fma` a LOWER bound because vectorising its
    accumulators is the compiler's decision and standing order 6 forbids
    `-march=native`, and on `c8g.metal-48xl` at t=1 it measured 4.22 GFLOP/s against a
    best large DGEMM of 18.16. A bound 4.3x under its own subject fires at no
    threshold, so the flag was absent while reading as protection. Building roofline.c
    alone with `-O3 -march=native` was the alternative and was rejected: it would make
    the campaign's only independent floor a function of gcc's vectoriser.

    So this scenario plants the case that USED to be the headline -- peak_fma 1.5x the
    best GEMM, which no Graviton host at `-O2` produces -- and asserts the report says
    nothing about it. Retiring a check means the report stops implying a floor exists,
    and the only way to hold that is to assert the silence. Re-adding the threshold
    fails this scenario; re-adding the CLI knob fails it separately, because a
    published `params.headroom_factor` tells a reader a check ran whether or not it
    fired. `peak_fma` itself must still print: it is provenance, and the report must
    label it as provenance rather than as a check that passed.

    What must NOT be retired with it: `IMPLAUSIBLE_GFLOPS_PER_CORE` and
    `sanity_check()`'s hard abort in `src/roofline.c`. Those guard standing order 2's
    optimizer hazard (927 TFLOP/s on one core from a folded FMA chain), which is worth
    guarding even though the number it protects now has no analytic use."""
    return Scenario(
        name="peak-fma-retired",
        description=(
            "peak_fma is 1.5x the best GEMM any arm reached -- what standing order 1 once "
            "made the headline. The cross-check is retired, so the report must raise no "
            "anomaly, publish no headroom_factor, and label peak_fma as provenance."
        ),
        hosts=[_host(peak_factor=1.5)],
        arms=_arms(),
        expect=[
            {"kind": "anomaly_kind_absent", "kind_name": "headroom"},
            {"kind": "stdout_absent", "text": "that gap is the headline"},
            {"kind": "stdout_absent", "text": "see section 5"},
            {"kind": "json_absent", "path": "params.headroom_factor"},
            # Provenance survives, and says what it is. Without these two the scenario
            # would also pass against a version that dropped peak_fma altogether.
            {"kind": "stdout_contains", "text": "NOT a cross-check"},
            {"kind": "stdout_contains", "text": "peak_fma="},
        ],
    )


def sc_peak_absent():
    """The other half of the retirement: absence is a provenance gap, not a finding.

    This scenario asserted the opposite until 2026-08-20 -- `peak_fma_absent` raised as
    an anomaly reading "the cross-check was NOT performed here" -- which was the right
    claim while there was a cross-check to perform. There is not. An anomaly is a call
    to action and there is no action, so the absence is printed in section 6 and
    nowhere else. See `peak-fma-retired` for why the check went."""
    return Scenario(
        name="peak-absent",
        description=(
            "No peak_fma record at all. With the cross-check retired this is a provenance "
            "gap that section 6 prints and section 5 does not flag -- there is no check "
            "left to report as 'not performed'."
        ),
        hosts=[_host(roofline_present=False)],
        arms=_arms(),
        expect=[
            {"kind": "anomaly_kind_absent", "kind_name": "peak_fma_absent"},
            {"kind": "stdout_absent", "text": "cross-check was NOT performed"},
            {"kind": "stdout_contains", "text": "peak_fma=absent"},
        ],
    )


def sc_replicate_reproduces():
    return Scenario(
        name="replicate-reproduces",
        description=(
            "Two independent P3 passes on the same instance type and different physical "
            "boxes, agreeing on a +22% V1-set headline. The strongest available defence "
            "of the number."
        ),
        hosts=[
            _host(instance_id="i-000000000000000a", run_id="synth-c7g-pass1"),
            _host(instance_id="i-000000000000000b", run_id="synth-c7g-pass2", host_scale=1.04),
        ],
        arms=_arms(v1_gain=flat(1.22)),
        expect=[
            {"kind": "replicate_status", "instance": "c7g.metal", "expect": "REPRODUCES"},
            {"kind": "stdout_contains", "text": "8. REPLICATES"},
            {"kind": "exit_bits_clear", "bits": [16]},
            {"kind": "verdict_code", "one_of": ["V1-SET-AHEAD"]},
        ],
    )


def sc_replicate_diverges():
    # Pass 2 inverts the effect. Pooling the two would report a weak null and
    # destroy the only evidence that the passes disagree.
    return Scenario(
        name="replicate-diverges",
        description=(
            "Pass 1 says the V1 set is 22% ahead; pass 2, on a different box, says the V2 "
            "set is. Pooling the two would print a tidy parity row and delete the finding."
        ),
        hosts=[
            _host(instance_id="i-000000000000000a", run_id="synth-c7g-pass1"),
            _host(instance_id="i-000000000000000b", run_id="synth-c7g-pass2", swap_v1_v2=True),
        ],
        arms=_arms(v1_gain=flat(1.22)),
        expect=[
            {"kind": "replicate_status", "instance": "c7g.metal", "expect": "DIVERGES-DIRECTION"},
            {"kind": "exit_bits_set", "bits": [16]},
            {"kind": "stdout_contains", "text": "does not reproduce"},
        ],
    )


def sc_replicate_majority():
    """Three independent passes, two agreeing and one that reached no verdict.

    This is what the spend policy's third pass is *for*, so the analysis has to
    read it as success. Two passes agree on a +22% V1-set headline; on the third,
    both V1-set arms died (`runtime_failed`, censused with a reason), so that pass
    has no comparable cells and no direction. A majority that is directional and
    uncontradicted is `REPRODUCES-MAJORITY` and exit bit 16 stays clear -- if a
    non-directional dissent counted as divergence, going from two passes to three
    would make the gate HARDER to pass, which is backwards for a change bought to
    make the headline more defensible.

    The line it must not cross is `replicate-diverges` and `lucky-pass`: a dissent
    that carries the OPPOSITE direction is still a divergence at any pass count,
    because no majority makes a contradiction publishable. Those two scenarios hold
    that end, this one holds this end, and the pair is what makes the rule a rule
    rather than a preference for the answer we want.

    It also found the cost of a partial pass, and that cost is what the aggregation
    rule now answers. Sections 1-7 used to refuse any cell whose two sides had
    unequal N, so the V1 arms having 2 samples where the V2 arms have 3 made all 72
    pooled cells non-comparable: the campaign line read INCONCLUSIVE while section 8
    showed +22.1% and +21.9% on the two complete passes -- the same false-negative
    shape as C11, reached by a different route. Global equal-N was stronger than the
    arithmetic needs; what a paired comparison needs is equal N *within* the
    comparison. So each comparison is now intersected down to the passes carrying
    BOTH arms, and this scenario asserts the directional verdict that recovers.

    What the intersection must not do is launder two passes into three. A 2-of-3
    intersection is back at median-of-2 = mean, breakdown point zero, which is the
    exact failure the three-pass policy was bought to prevent -- so every such row
    prints `passes=2of3`, the verdict line carries UNDER-REPLICATED, and the
    headline is explicitly not a full-replication claim. `replicate-loss-unexplained`
    holds the other half of the rule: intersecting is licensed by a census reason,
    not by convenience."""
    reason = "harness exited 134 on this pass; see /opt/gbb/stderr.log"
    return Scenario(
        name="replicate-majority",
        description=(
            "Three separately launched passes. Two agree on a +22% V1-set headline; on the "
            "third the V1-set arms crashed, so it reaches no direction. The majority is "
            "uncontradicted, which is what the third pass was bought for: not a divergence."
        ),
        hosts=[
            _host(instance_id="i-000000000000000a", run_id="synth-c7g-pass1"),
            _host(instance_id="i-000000000000000b", run_id="synth-c7g-pass2", host_scale=1.03),
            _host(
                instance_id="i-000000000000000c",
                run_id="synth-c7g-pass3",
                failed_arms={V1: reason},
            ),
        ],
        arms=_arms(v1_gain=flat(1.22)),
        expect=[
            {"kind": "replicate_status", "instance": "c7g.metal", "expect": "REPRODUCES-MAJORITY"},
            {"kind": "stdout_contains", "text": "2 of 3 passes: V1-SET-AHEAD"},
            # The dissenting pass is named rather than averaged away. "read the
            # dissenting pass before publishing" is the whole content of the
            # majority rule; a status without the box is not actionable.
            {"kind": "stdout_contains", "text": "i-000000000000000c"},
            {"kind": "exit_bits_clear", "bits": [4, 16]},
            # The pooled verdict is the effect the two complete passes agree on. The
            # loss is census-explained, so the comparison is intersected to the two
            # passes carrying both arms rather than refused -- one lost arm must not
            # nuke a 22% finding two passes agree on.
            {"kind": "verdict_code", "one_of": ["V1-SET-AHEAD"]},
            # ...and must not be readable as a three-pass claim. Both halves are
            # asserted, because either alone is a policy this scenario rejects.
            {"kind": "stdout_contains", "text": "UNDER-REPLICATED"},
            {"kind": "stdout_contains", "text": "passes=2of3"},
            {"kind": "json_number", "path": "verdict.under_replicated_cells", "op": ">", "value": 0},
            {"kind": "json_bool", "path": "verdict.headline_eligible", "value": False},
            {"kind": "stdout_contains", "text": "not a full-replication claim"},
            {"kind": "stdout_contains", "text": "do not read a pooled"},
            # Standing order 11 on a per-pass failure: the crash carries its reason
            # into the report, not just into the census file. Section 7 cannot do
            # this — the arm has cells, from the other two passes.
            {"kind": "stdout_contains", "text": "harness exited 134 on this pass"},
        ],
    )


def sc_replicate_loss_unexplained():
    """The same three-pass shape as `replicate-majority`, with the reason removed.

    The intersection rule is licensed by an explanation, not by the arithmetic. Here
    the runner exited 0 and censused the V1 arms `measured` on pass 3, and their
    records are simply not in the directory -- the shape of a per-arm S3 shipment
    that was dropped or truncated (standing order 12 ships per arm, so this is the
    ordinary way for one pass to lose one arm without anyone noticing).

    Nothing in the census says those records would have looked like the other two
    passes', and there is no record of them having said anything, so the comparison
    is NOT intersected: it stays `inconclusive(unequal-N-unexplained:...)` and the
    campaign verdict stays INCONCLUSIVE. This is the pair to `replicate-majority`,
    and the pair is the rule -- with only the explained case in the suite, an
    implementation that intersected unconditionally would be green, and the
    difference between "we know why that arm is missing" and "we do not" is exactly
    the difference standing order 11 exists to keep.

    Pooled coverage is complete, so section 7 cannot catch this and bit 4 stays
    clear: the arm has every condition, from the other two passes. Only the per-pass
    view sees the hole, which is why `pass_explain` had to be keyed on run_id."""
    return Scenario(
        name="replicate-loss-unexplained",
        description=(
            "Three passes; on the third, both V1-set arms' records are absent while the "
            "census says they ran. An unexplained loss is not intersected away -- the "
            "comparison stays unequal-N and the verdict stays INCONCLUSIVE."
        ),
        hosts=[
            _host(instance_id="i-000000000000000a", run_id="synth-c7g-pass1"),
            _host(instance_id="i-000000000000000b", run_id="synth-c7g-pass2", host_scale=1.03),
            _host(instance_id="i-000000000000000c", run_id="synth-c7g-pass3", lost_arms=(V1,)),
        ],
        arms=_arms(v1_gain=flat(1.22)),
        expect=[
            {"kind": "verdict_code", "one_of": ["INCONCLUSIVE"]},
            {"kind": "stdout_contains", "text": "unequal-N-unexplained"},
            # The pass and the arm that went missing are both named. "unexplained"
            # without saying which pass is a dead end for whoever has to go and look.
            {"kind": "stdout_contains", "text": "synth-c7g-pass3"},
            {"kind": "json_len", "path": "verdict.coherent_subsets", "op": "==", "value": 0},
            # Refusing is not the same as flagging a coverage hole: pooled, every
            # condition is present. Bit 4 firing here would mean the two rules are
            # reading the same evidence, and then only one of them is needed.
            {"kind": "exit_bits_clear", "bits": [4]},
        ],
    )


def sc_replicate_same_box():
    return Scenario(
        name="replicate-same-box",
        description=(
            "Two run_ids from the SAME instance_id: a re-run, not a replicate. The rule "
            "must fail safe and decline to count it."
        ),
        hosts=[
            _host(instance_id="i-000000000000000a", run_id="synth-c7g-run1"),
            _host(instance_id="i-000000000000000a", run_id="synth-c7g-run2"),
        ],
        arms=_arms(v1_gain=flat(1.22)),
        expect=[
            {"kind": "replicate_status", "instance": "c7g.metal", "expect": "NO-REPLICATE"},
            {"kind": "stdout_contains", "text": "1 instance_id"},
            {"kind": "exit_bits_clear", "bits": [16]},
        ],
    )


def sc_blas_sha_conflict():
    return Scenario(
        name="blas-sha-conflict",
        description=(
            "Two hosts built OpenBLAS from different trees. Their records are being "
            "compared as one arm, and before this check the artifact could not tell."
        ),
        hosts=[
            _host(instance_type="c7g.metal", instance_id="i-1", run_id="synth-c7g-pass1"),
            _host(
                instance_type="c6g.metal",
                instance_id="i-2",
                run_id="synth-c6g-pass1",
                core_name="NEOVERSEN1",
                midr_part="0xd0c",
                has_sve=False,
                dynamic_selection="neoversen1",
                sve_vl=0,
                blas_sha_override="c" * 40,
            ),
        ],
        arms=_arms(),
        expect=[
            {"kind": "exit_bits_set", "bits": [8]},
            {"kind": "anomaly_kind_present", "kind_name": "blas_sha_conflict"},
        ],
    )


def sc_incx_axis():
    """The stride axis, which the condition key used to collapse.

    The V1 set is 40% ahead at incx=4 and at parity at incx=1. Before incx joined
    the condition the two strides shared a cell and min-within-run kept the
    slower, so the stride-4 effect was arithmetically unreachable -- a stride
    result could not have been reported either way."""
    return Scenario(
        name="incx-axis",
        description=(
            "A level-1 effect that exists only at incx=4. Recovering it requires incx to "
            "be part of the condition; without that the two strides share one cell."
        ),
        hosts=[_host()],
        arms=_arms(v1_incx={4: 1.40}),
        expect=[
            {"kind": "cross_rows_have_incx", "values": [1, 4]},
            {
                "kind": "cross_verdicts_where",
                "routine": "daxpy",
                "incx": 4,
                "expect": "V1-set-ahead",
                "min_rows": 1,
            },
            {
                "kind": "cross_verdicts_where",
                "routine": "daxpy",
                "incx": 1,
                "expect": "parity",
                "min_rows": 1,
            },
        ],
    )


def sc_transpose_shopping():
    """The transpose axis, and the reason it has to be part of the comparison key.

    The V1 set is 35% ahead at TN and at parity at NN, NT and TT. NN routes A
    through `gemm_ncopy_*` and TN through `gemm_tcopy_*`, so "ahead on one
    transpose" is an ordinary kernel-set result rather than a contrived one -- and
    it is also the shape that breaks the aggregation if the key does not carry the
    axis. Four transposes sharing one cell means each arm's cell value is the best
    of its four, so every arm is compared on whichever transpose flattered it most:
    the max-over-cell defect that `incx-axis` and the lda_pad key each fixed once,
    returning in a third shape. The effect is then neither reportable nor
    localisable, and worse, a transpose where the V1 set is *behind* is invisible
    because the NN measurement covered for it.

    Deliberately ahead of the producer: src/bench.c does not emit transa/transb yet
    (landing order item 3). The analysis is item 1 and lands first, so the key must
    accept the axis before the sweep produces it, and must default absent to "N" so
    the same code reads today's data unchanged -- which `full-routine-set` and every
    other scenario go on asserting, since none of them carry the fields."""
    return Scenario(
        name="transpose-shopping",
        description=(
            "A GEMM effect that exists only at transa=T: 35% at TN, parity at NN/NT/TT. "
            "Recovering it requires transa/transb in the comparison key; without them the "
            "four transposes share a cell and each arm is judged on its favourite."
        ),
        hosts=[_host()],
        routines=("dgemm",),
        level1=False,
        transposes=(("N", "N"), ("T", "N"), ("N", "T"), ("T", "T")),
        arms=_arms(v1_trans={"TN": 1.35}),
        expect=[
            {"kind": "cross_rows_have_trans", "routine": "dgemm", "values": ["NN", "NT", "TN", "TT"]},
            {
                "kind": "cross_verdicts_where",
                "routine": "dgemm",
                "transa": "T",
                "transb": "N",
                "expect": "V1-set-ahead",
                "min_rows": 3,
            },
            {
                "kind": "cross_verdicts_where",
                "routine": "dgemm",
                "transa": "N",
                "transb": "N",
                "expect": "parity",
                "min_rows": 3,
            },
            # The verdict is transpose-localised, which means it is not global: with
            # one transpose of four affected, MIXED is the honest answer and a
            # directional headline would be the fixture's own hypothesis leaking in.
            {"kind": "verdict_code", "not_one_of": ["V1-SET-AHEAD", "NULL", "NO-DATA"]},
            # ...and the transpose has to be an axis of the coherence guard, not only
            # of the key. With the key extended and the axis missing, this dataset
            # read out as "NULL -- publish the negative result" over a 35% effect
            # present at every size of one transpose: 20 of 80 rows carry it, the
            # parity rows clear the 60% majority, and no routine, regime or instance
            # subset can see an effect that is confined to none of them. The key
            # extension created that hole and this closes it in the same commit.
            {"kind": "coherent_subsets", "expect": ["trans:TN:V1"]},
            {"kind": "stdout_contains", "text": "trans TN: V1 set ahead"},
            {"kind": "stdout_absent", "text": "publish the negative result"},
            {"kind": "exit_bits_clear", "bits": [2, 4, 16]},
        ],
    )


def sc_family_swamped():
    """GEMM's row count outvoting every other family -- the matrix expansion's cost
    to the C11 guard, planted.

    This is the failure mode the per-family normalisation exists for, and it does
    not exist in `full-routine-set`: there, one row per routine per regime meant raw
    cell counts and family counts differed only mildly. Give GEMM four transposes
    and the census is 32 GEMM rows against 3 each for TRSM, TRMM and SYMM, all of
    them the same hardware claim repeated at a different copy kernel. Counting rows,
    `regime:small` is 27% V1 and reaches no direction; counting families it is 75%
    V1 -- three of the four families measured at that size say the same thing.

    So the guard's majority is over families, normalised, with each family's one
    unit of weight divided among its own rows. Without that, every item in the
    matrix expansion makes the guard weaker than it was before C11 was fixed: the
    additions multiply GEMM's rows faster than anything else's, and the coherent
    TRSM/TRMM/SYMM effect the campaign was built to price gets diluted by rows that
    contain no independent information about it.

    The regime and instance subsets are the discriminating assertions. Per-routine
    subsets qualify either way -- one family per routine makes the normalisation a
    no-op there -- so a scenario asserting only those would be green on the raw-count
    implementation this one exists to reject."""
    return Scenario(
        name="family-swamped",
        description=(
            "The V1 set is 22% ahead on dtrsm/dtrmm/dsymm and at parity on dgemm/sgemm, "
            "which carry four transposes each and so hold 32 of 41 rows. Weighted by row "
            "the effect is a minority; weighted by routine family it is 3 of 4."
        ),
        hosts=[_host()],
        routines=("dgemm", "sgemm", "dtrsm", "dtrmm", "dsymm"),
        level1=False,
        transposes=(("N", "N"), ("T", "N"), ("N", "T"), ("T", "T")),
        arms=_arms(v1_gain=flat(1.22), routines=N2_GAP_ROUTINES),
        expect=[
            # The effect is where it was planted, per routine, whatever the weighting.
            {"kind": "cross_verdicts_where", "routine": "dtrsm", "expect": "V1-set-ahead", "min_rows": 3},
            {"kind": "cross_verdicts_where", "routine": "dtrmm", "expect": "V1-set-ahead", "min_rows": 3},
            {"kind": "cross_verdicts_where", "routine": "dsymm", "expect": "V1-set-ahead", "min_rows": 3},
            {"kind": "cross_verdicts_where", "routine": "dgemm", "expect": "parity", "min_rows": 8},
            # ...and the regime and instance axes recover it only under family
            # normalisation. Asserted as an exact set, so a guard that over-fires
            # fails here too.
            {
                "kind": "coherent_subsets",
                "expect": [
                    "routine:dtrsm:V1",
                    "routine:dtrmm:V1",
                    "routine:dsymm:V1",
                    "regime:small:V1",
                    "regime:medium:V1",
                    "regime:large:V1",
                    "instance:c7g.metal:V1",
                    # TRSM/TRMM/SYMM carry no transpose field, so they land in the
                    # NN bucket with the untransposed GEMM rows -- canon_trans()
                    # defaults absent to N deliberately, and the consequence is that
                    # NN is where three of the four families favour V1. True as
                    # stated; the transposed GEMM rows do not qualify.
                    "trans:NN:V1",
                ],
            },
            # The whole point: 22% on three of four families is not a null. And it
            # is not a campaign-level V1-SET-AHEAD either -- three families out of
            # four clear the balanced majority while the median over every
            # comparable cell stays inside the parity band, because the two GEMM
            # families really are at parity and they are most of the work. That is
            # the effect-size floor, and it is asserted here rather than in a
            # scenario of its own because this is the dataset that has both halves:
            # a balanced majority that must be believed, and a global median that
            # must not be published as one. Both directional codes are named, so a
            # mutant that flips the sign fails too.
            {"kind": "stdout_absent", "text": "publish the negative result"},
            {"kind": "verdict_code", "one_of": ["MIXED"]},
            {"kind": "verdict_code", "not_one_of": ["NULL", "V1-SET-AHEAD", "V2-SET-AHEAD"]},
            # The sentence the floor exists to print: located, with the number, and
            # explicitly not global. A MIXED reached by some other route would not
            # say this.
            {"kind": "stdout_contains", "text": "below the 5% floor"},
            {"kind": "stdout_contains", "text": "not a campaign-level direction"},
            # The MIXED line reports family weight, not row count -- a reader shown
            # "22% of cells" would conclude the effect is marginal when it is 3 of 4
            # families, and that sentence is what the campaign's answer to "where"
            # rests on.
            {"kind": "stdout_contains", "text": "of family weight"},
            {"kind": "exit_bits_clear", "bits": [2, 4, 16]},
        ],
    )


def sc_reference_arm():
    """The netlib correctness control, present exactly as the real producers emit it.

    build-libs.sh writes a `reference/native` manifest arm unconditionally, with
    built:true and runnable:true, and run-matrix.sh declines to time it. So every
    real dataset contains a manifest arm that produces no bench records by design.
    decompose.py folded it into the expected BENCH arms, which made every condition
    on the host an unexplained hole: 36 MISSING-UNEXPLAINED cells and exit bit 4 on
    a dataset with nothing whatsoever missing -- against a P2 gate that requires
    zero of them. The flag whose entire job is to say "you have a coverage hole"
    would have been the one flag guaranteed to be lying, on the very first real run.

    This is the same defect as the roofline pseudo-arm, and it survived the fix for
    it, because that fix named a library instead of the class of library. Hence
    NON_BENCH_LIBRARIES, and hence this scenario: it is the regression test for the
    class, not for the instance."""
    return Scenario(
        name="reference-arm",
        description=(
            "A clean dataset that also contains build-libs.sh's untimed netlib reference "
            "arm and run-matrix.sh's `skipped` census line for it, which every real "
            "dataset contains. It must still be clean."
        ),
        hosts=[_host(reference_arm=True)],
        arms=_arms(),
        expect=[
            {"kind": "verdict_code", "one_of": ["NULL"]},
            {"kind": "exit_bits_clear", "bits": [2, 4, 8, 16]},
            {"kind": "json_number", "path": "coverage.missing_unexplained", "op": "==", "value": 0},
            {"kind": "json_number", "path": "coverage.partial", "op": "==", "value": 0},
        ],
    )


def sc_reference_arm_uncensused():
    """The same arm with no census line at all -- the producer's state before
    standing order 11 was applied to it. Belt and braces: the analysis must be
    clean on the manifest evidence alone, so that a runner that forgets the census
    record cannot resurrect the bug."""
    return Scenario(
        name="reference-arm-uncensused",
        description=(
            "The untimed netlib arm in the manifest with NO census record. "
            "NON_BENCH_LIBRARIES must carry this on its own, without help from a "
            "stated reason."
        ),
        hosts=[_host(reference_arm=True, reference_censused=False)],
        arms=_arms(),
        expect=[
            {"kind": "verdict_code", "one_of": ["NULL"]},
            {"kind": "exit_bits_clear", "bits": [4]},
            {"kind": "json_number", "path": "coverage.missing_unexplained", "op": "==", "value": 0},
        ],
    )


def sc_sve_kernels_unknown():
    """build-libs.sh could not read the archive, so whether SVE kernels are in the
    build is UNKNOWN -- which it defines as "we could not look", not as "fine".

    The check read `sve_kernels != "no"`, so `unknown` took the same path as `yes`:
    standing order 8's quiet trigger switched itself off in exactly the case where
    the check could not be performed. Failing open on the campaign's central
    hardware axis. It is not an escalation either -- absent evidence is not evidence
    of absence -- so it is a provenance gap, and standing order 5 says a number
    without provenance is not admissible. Bit 8, not bit 2."""
    arms = _arms()
    for a in arms:
        if a.library == "openblas":
            a.sve_kernels = "unknown"
    return Scenario(
        name="sve-kernels-unknown",
        description=(
            "Whether the OpenBLAS build contains SVE kernel symbols could not be "
            "determined, on a host that reports SVE. Absence of evidence, on the axis "
            "the campaign is about."
        ),
        hosts=[_host()],
        arms=arms,
        expect=[
            {"kind": "exit_bits_set", "bits": [8]},
            {"kind": "anomaly_kind_present", "kind_name": "sve_kernels_unknown"},
            {"kind": "stdout_contains", "text": "UNKNOWN"},
            # Not an escalation: `escalate` is reserved for a confirmed failure, and
            # conflating the two would make the loud finding unreadable.
            {"kind": "anomaly_kind_absent", "kind_name": "escalate"},
        ],
    )


def sc_escalation_acked():
    """capture-env.sh refused this host and GBB_ESCALATION_ACK overrode the refusal.

    run-matrix.sh writes an `escalation_ack` record into the census to document it.
    That record matched none of the loader's shapes, so the single artifact proving
    the campaign's loudest interlock had been overridden was counted as a corrupt
    NDJSON line -- at a severity that sets no exit bit. The override survived into
    the dataset and left no trace in the analysis."""
    return Scenario(
        name="escalation-acked",
        description=(
            "A host whose provenance refusal was overridden with GBB_ESCALATION_ACK. "
            "The override must be legible in the report, not filed as data corruption."
        ),
        hosts=[_host(escalation_ack="known-good box, dispatch table checked by hand 2026-08-19")],
        arms=_arms(),
        expect=[
            {"kind": "anomaly_kind_present", "kind_name": "escalation_acked"},
            {"kind": "exit_bits_set", "bits": [2]},
            {"kind": "stdout_contains", "text": "GBB_ESCALATION_ACK"},
            # The record must be PARSED, not merely tolerated. If it lands in
            # bad_lines this fires, which is the whole finding.
            {"kind": "json_number", "path": "inputs.unparseable_lines", "op": "==", "value": 0},
            {"kind": "anomaly_kind_absent", "kind_name": "unparseable_lines"},
        ],
    )


def sc_role_mixed():
    """Instrument-check records in a campaign results directory.

    CLAUDE.md requires castor/pollux be quarantined "by construction, not by
    discipline". The producers separate them structurally -- distinct directory,
    run_id prefix and S3 prefix -- and bench.c and roofline.c both tell the reader
    "the analysis excludes anything that does not say campaign". The analysis did
    not read the field at all. One `aws s3 sync` of a bucket holding both prefixes
    and GB10 numbers pool into the Graviton dataset silently; because the pool also
    feeds standing order 1's measured-peak denominator, a faster instrument host
    deflates every efficiency figure on the host it contaminated. Nothing else guards
    that denominator -- the `peak_fma` cross-check that once nominally did is retired.

    The leaked records are 3x faster on purpose, so a fixture that pooled them
    could not possibly still read as a null."""
    return Scenario(
        name="role-mixed",
        description=(
            "A campaign directory polluted with role=instrument records at 3x the "
            "GFLOP/s. They must be excluded from every number, and the fact that the "
            "directory holds two roles must be reported rather than quietly handled."
        ),
        hosts=[_host(foreign_role="instrument", foreign_role_gain=3.0)],
        arms=_arms(),
        expect=[
            {"kind": "anomaly_kind_present", "kind_name": "role_excluded"},
            {"kind": "exit_bits_set", "bits": [2]},
            # Excluded, therefore the campaign answer is unchanged: still the null,
            # still parity everywhere. If the 3x records reached the cells, neither
            # of these could hold.
            {"kind": "verdict_code", "one_of": ["NULL"]},
            {"kind": "cross_verdicts_all", "expect": "parity", "min_rows": 40},
            {"kind": "json_number", "path": "inputs.foreign_roles.instrument", "op": ">", "value": 0},
            {"kind": "run_ids_none_start_with", "prefix": "instr-"},
        ],
    )


def sc_partial_arm():
    """An arm that ran and produced only some of its sizes.

    decompose.py counts this as `partial` and treats it as a hole one level down,
    but no fixture could express it: Arm had no per-size omission knob, so
    `coverage.partial` was 0 in all 25 reports and the bit-4 contribution from
    `partial` was undefended. It is also the exact shape of a regression the file
    documents -- the census said `measured` and the hole counted as coverage."""
    arms = _arms()
    for a in arms:
        if a.coretype == V1:
            a.omit_sizes = (2048, 3072, 4096)
    return Scenario(
        name="partial-arm",
        description=(
            "The V1-coretype arm is missing three of the five large sizes, while its "
            "census line says `measured`. A census record saying the arm ran explains "
            "nothing about a size it did not produce."
        ),
        hosts=[_host()],
        arms=arms,
        expect=[
            {"kind": "json_number", "path": "coverage.partial", "op": ">", "value": 0},
            {"kind": "exit_bits_set", "bits": [4]},
            {"kind": "stdout_contains", "text": "PARTIAL"},
        ],
    )


def sc_aliased_coretype():
    """A status that says "about to run" must never explain a missing cell.

    `aliased` is written BEFORE the arm runs and the arm then runs, so it can never
    account for an absence -- but it was absent from CENSUS_SUCCESS, which would have
    let a genuine hole in the campaign's central arm be excused by a line that says
    "running it". That is what this plants, and it is the whole claim.

    The direction planted here -- a NEOVERSEV2 request reporting back `neoversen2` --
    is NOT what cc3fc1e does: it `#define`s the two to one pointer and
    `gotoblas_corename()` checks V2 first, so the V2 request reports `neoversev2` and
    verifies exactly. `p2-host` carries the direction the real hosts take, and
    `alias-duplicate` carries its consequence. This scenario keeps the other
    direction because `run-matrix.sh` still declares it: corename()'s check order is
    an implementation detail OpenBLAS owes nobody, and if it flips, this is the arm
    that appears -- with `aliased` back on the live path and this fixture the only
    thing standing between it and CENSUS_SUCCESS. Do not "fix" it to match the
    hardware; the docstring that did claim this was the hardware's direction is what
    let the runner's own alias list go one-sided until c8g.metal-48xl refused an arm
    over it on 2026-08-20."""
    arms = _arms(v1_gain=flat(1.22))
    for a in arms:
        if a.coretype == V2:
            a.census_status = "aliased"
            a.census_reason = (
                "requested NEOVERSEV2, openblas_get_corename() reports 'neoversen2'. Running it."
            )
            a.omit_sizes = tuple(SIZES_LARGE)
    return Scenario(
        name="aliased-coretype",
        description=(
            "The V2 arm is censused `aliased` and is also missing every large size. "
            "`aliased` says the arm is being run, so it must not be accepted as the "
            "reason a cell is absent. The reported direction is the one this OpenBLAS "
            "does NOT take -- see the docstring for why it is kept anyway."
        ),
        hosts=[_host(coretype_aliases={V2: "NEOVERSEN2"})],
        arms=arms,
        expect=[
            {"kind": "exit_bits_set", "bits": [4]},
            {"kind": "stdout_contains", "text": "MISSING-UNEXPLAINED"},
            {"kind": "json_number", "path": "coverage.missing_unexplained", "op": ">", "value": 0},
        ],
    )


def sc_lda_penalty():
    """A leading-dimension penalty, which section 3 exists to find and no fixture
    planted. The same gain was applied to pad=0 and pad=8 everywhere, so `padding
    hurts` and `padding helps` were never produced and the packing-kernel
    CONSEQUENCE line was unreachable.

    The penalty is planted per pad, not uniformly across the pad axis, because
    with #2's LDA_PADS_EXTRA there now IS a pad axis: pads 1/4/8 hurt and pad 64
    is flat, on the same arm. A uniform plant would pass against a section 3 that
    pooled every padded stride into one comparison against pad=0, which is the
    obvious way to write it and would report one averaged penalty per size --
    losing the only thing the four pads were added to see."""
    hurts = {1: 0.82, 4: 0.82, 8: 0.82}  # pad 64 absent: flat by construction
    return Scenario(
        name="lda-penalty",
        description=(
            "Every OpenBLAS arm is 18% slower at lda_pad 1, 4 and 8 than at a tight "
            "leading dimension, flat at pad 64, with ArmPL flat everywhere. That "
            "isolates packing-kernel quality from the inner kernel, which is the only "
            "thing section 3 is for, and it must be attributed per pad."
        ),
        hosts=[_host()],
        arms=[
            Arm("openblas", "DYNAMIC", "unforced", gain_pad=hurts),
            Arm("openblas", "DYNAMIC", V1, gain_pad=hurts, in_manifest=False),
            Arm("openblas", "DYNAMIC", V2, gain_pad=hurts, in_manifest=False),
            Arm("openblas", V1, "unforced", gain_pad=hurts),
            Arm("openblas", V2, "unforced", gain_pad=hurts),
            Arm("armpl", "native", "unforced", thread_backend="openmp"),
        ],
        expect=[
            {
                "kind": "lda_verdict",
                "arm_contains": "openblas",
                "lda_pad_in": [1, 4, 8],
                "expect": "padding hurts",
                "min_rows": 4,
            },
            # The same arm, the pad that was left flat. Fails a section 3 that
            # pools pads: averaging 0.82, 0.82, 0.82 and 1.0 against pad=0 would
            # call every pad "padding hurts", including this one.
            {
                "kind": "lda_verdict",
                "arm_contains": "openblas",
                "lda_pad_in": [64],
                "expect": "within band",
                "min_rows": 4,
            },
            # ArmPL is flat by construction, so its rows must come back `within
            # band`. That is the stronger half of this scenario: it proves the
            # penalty is attributed to the arm that has it rather than to the host,
            # which a section-3 that compared across arms would get wrong.
            {
                "kind": "lda_verdict",
                "arm_contains": "armpl",
                "expect": "within band",
                "min_rows": 4,
            },
            {"kind": "stdout_contains", "text": "padding hurts"},
            # The cross must stay a null: both sides of the cross pay the same
            # penalty, so an lda effect must not leak into the kernel-set verdict.
            {"kind": "verdict_code", "one_of": ["NULL"]},
        ],
    )


def sc_lucky_sample():
    """The aggregation policy, which until now nothing tested.

    `build_cells` takes the minimum within a run_id and the median across run_ids,
    and its docstring says why: "the one host the docs say to run repeatedly was
    the one the instrument flattered." But no fixture ever emitted two records for
    the same (condition, arm, run_id), so the min-within-run rule guarded nothing
    a scenario could see -- swapping it for max left all 33 scenarios green.

    The duplicate is put on the V1 side of the cross only, and only that side, so
    the two aggregation policies give *different verdicts* rather than different
    numbers: min keeps the honest sample and the cross is a null, max keeps the
    lucky one and the cross reads V1-set-ahead by 40%. A mutant cannot survive by
    being slightly wrong."""
    arms = _arms(v1_gain=flat(0.88), v2_gain=flat(0.88))
    for a in arms:
        if V1 in (a.target, a.coretype):
            a.lucky_dup = 1.40
    return Scenario(
        name="lucky-sample",
        description=(
            "Both V1 arms carry a second record for every condition in the same run, "
            "40% faster than the real one -- an appended re-run. Min-within-run keeps "
            "the honest sample and the cross stays a null; anything that keeps the "
            "lucky one publishes a 40% kernel-set win that was never measured."
        ),
        hosts=[_host()],
        arms=arms,
        expect=[
            # The fixture's own claim, asserted first: `parity` is also what a
            # fixture with no duplicate produces, so without this the scenario
            # could pass because lucky_dup quietly stopped emitting.
            {"kind": "fixture_duplicate_records", "arm_contains": V1, "min_count": 100},
            {"kind": "verdict_code", "one_of": ["NULL"]},
            {"kind": "cross_verdicts_all", "expect": "parity", "min_rows": 40},
            {"kind": "exit_bits_clear", "bits": [2, 4, 8, 16]},
        ],
    )


def sc_lucky_pass():
    """The other half of the aggregation policy: median ACROSS run_ids.

    `build_cells` pools passes by instance_type and takes the median of the
    per-run representatives, and the reason is in its docstring -- "the one host
    the docs say to run repeatedly was the one the instrument flattered." Three
    passes are needed to test it: with two, the median IS the mean and a lucky pass
    leaks in whatever the rule says, so no two-host scenario could distinguish
    median from max.

    The verdict alone cannot carry this claim, and that is worth being explicit
    about. Substituting max for the median raises `run_spread` by exactly the
    amount it raises the delta, so `band_for` widens in step and the cross row
    stays `parity` on the boundary -- a mutant that survives every verdict-level
    assertion in this file. So the assertion is on the pooled NUMBER: three passes
    must yield the median pass's delta, not the luckiest pass's."""
    return Scenario(
        name="lucky-pass",
        description=(
            "Three passes on one instance type, two honest and one in which the V1 "
            "arms alone came out 25% fast. The pooled number must be the median "
            "pass's, and the flattered pass must surface as a replicate "
            "disagreement rather than as a 25% kernel-set headline."
        ),
        hosts=[
            _host(instance_id="i-0000000000000001", run_id="synth-c7g-pass1"),
            _host(instance_id="i-0000000000000002", run_id="synth-c7g-pass2"),
            _host(
                instance_id="i-0000000000000003",
                run_id="synth-c7g-pass3",
                pass_boost={V1: 1.25},
            ),
        ],
        arms=_arms(v1_gain=flat(0.88), v2_gain=flat(0.88)),
        expect=[
            # The pooled delta is the median pass's, i.e. nothing. Under
            # max-across-runs it is the lucky pass's +25%.
            {"kind": "cross_delta_where", "regime": "large", "routine": "dgemm", "op": "<=", "value": 0.05},
            {"kind": "cross_delta_where", "regime": "small", "routine": "dgemm", "op": "<=", "value": 0.05},
            # ...and the flattered pass is not silently absorbed: it disagrees with
            # the other two and that is exit bit 16, not a footnote.
            {"kind": "replicate_status", "instance": "c7g.metal", "expect": "DIVERGES-DIRECTION"},
            {"kind": "exit_bits_set", "bits": [16]},
            {"kind": "stdout_contains", "text": "VERDICT-CAVEAT:"},
        ],
    )


def sc_all_arms_failed():
    """A host that produced provenance and a census of failures and nothing else.

    The realistic shape: capture-env.sh passed, then the OpenBLAS build failed and
    ArmPL was not installed, so `results/` holds env, manifest, census and no
    measurements at all. decompose.py must say NO-DATA and exit 1.

    Exit bit 1 is the only bit no scenario asserted. It used to be unassertable --
    the early return skipped the report, and gates/p1.sh cannot tell a scenario
    that wrote no report from one whose analysis crashed. The payload was added
    for exactly this fixture, and a payload with no fixture is a promise, not a
    test."""
    arms = _arms()
    for a in arms:
        a.measured = False
        a.manifest_built = False
        a.census_status = "build_failed"
        a.census_reason = "make failed: see build log -- no arm on this host produced a measurement"
    return Scenario(
        name="all-arms-failed",
        description=(
            "Every arm failed to build. The directory holds provenance and a census of "
            "failures and no bench records, and the answer is NO-DATA with exit 1 -- "
            "not a null, and not a crash."
        ),
        hosts=[_host()],
        arms=arms,
        expect=[
            {"kind": "exit_bits_set", "bits": [1]},
            {"kind": "verdict_code", "one_of": ["NO-DATA"]},
            {"kind": "json_number", "path": "inputs.bench_records", "op": "==", "value": 0},
            # The census was still read, so "nothing ran" is distinguishable from
            # "nothing was written": the files are accounted for in the report.
            {"kind": "json_len", "path": "inputs.files.census", "op": ">", "value": 0},
        ],
    )


# bench.c's own routine list, in its own order (src/bench.c:682 plus the dgemv and
# level-1 sweeps below it). Every scenario above this point runs the three-routine
# default, which is enough to carry a verdict and is NOT what a host produces: a
# real sweep writes all six level-3 routines, dgemm again at a padded leading
# dimension, dgemv, and daxpy/ddot at two strides. The routines that only ever
# appeared in the default set are dgemm, dtrsm and dgemv -- so sgemm, dtrmm, dsyrk
# and dsymm reached no fixture at all, and DTRMM and DSYMM are inside the very
# 90-operation N2 gap this campaign exists to price.
BENCH_ROUTINES = ("dgemm", "sgemm", "dtrsm", "dtrmm", "dsyrk", "dsymm", "dgemv")
# The operations KERNEL.NEOVERSEN2 does not cover. If the N2 kernel-selection gap
# is what the deficit is, this is the shape the data takes: a large effect on these
# and nothing on GEMM.
N2_GAP_ROUTINES = ("dtrsm", "dtrmm", "dsymm")


def sc_full_routine_set():
    """The routine set a real host actually produces, with the effect where the
    campaign's hypothesis puts it.

    Two claims, and the first is the reason this scenario is not optional. Until
    it existed the gate certified decompose.py on dgemm, dtrsm and dgemv and said
    nothing about sgemm, dtrmm, dsyrk or dsymm -- and dtrmm and dsymm are in the
    90-operation N2 gap the entire campaign exists to price. A routine the analysis
    mishandles would have passed P1 green and produced a confident wrong answer in
    P2 about the cheapest fix in the repo.

    The second claim is about localisation. The effect is planted on the N2-gap
    routines only, so a global verdict would be the wrong answer: MIXED is correct
    and is what the campaign would then report -- "worth closing, for TRSM/TRMM/
    SYMM" -- and the per-routine rows have to be right for that sentence to mean
    anything."""
    return Scenario(
        name="full-routine-set",
        description=(
            "All nine routines bench.c emits, with the V1 kernel set 22% ahead on "
            "dtrsm/dtrmm/dsymm -- the N2 gap -- and at parity on dgemm/sgemm/dsyrk. "
            "The verdict is routine-specific, and the analysis has to say which "
            "routines rather than announce a global win."
        ),
        hosts=[_host()],
        routines=BENCH_ROUTINES,
        arms=_arms(v1_gain=flat(1.22), routines=N2_GAP_ROUTINES),
        expect=[
            # 1. every routine survives to both reports. A KeyError or a silent drop
            # in either section would show up here and nowhere else.
            {
                "kind": "routines_covered",
                "section": "deficit_by_routine",
                "routines": [*BENCH_ROUTINES, "daxpy", "ddot"],
            },
            {
                "kind": "routines_covered",
                "section": "target_cross",
                "routines": [*BENCH_ROUTINES, "daxpy", "ddot"],
            },
            # 2. the effect is where it was planted, and only there.
            {"kind": "cross_verdicts_where", "routine": "dtrsm", "expect": "V1-set-ahead", "min_rows": 4},
            {"kind": "cross_verdicts_where", "routine": "dtrmm", "expect": "V1-set-ahead", "min_rows": 4},
            {"kind": "cross_verdicts_where", "routine": "dsymm", "expect": "V1-set-ahead", "min_rows": 4},
            {"kind": "cross_verdicts_where", "routine": "dgemm", "expect": "parity", "min_rows": 4},
            {"kind": "cross_verdicts_where", "routine": "sgemm", "expect": "parity", "min_rows": 4},
            {"kind": "cross_verdicts_where", "routine": "dsyrk", "expect": "parity", "min_rows": 4},
            # 3. and the campaign-level answer is the honest one: neither a global
            # win nor a null. 36 of 104 comparable cells carry the effect, so the
            # parity cells hold a 65% majority and the unguarded verdict said
            # "NULL ... publish the negative result" over a coherent +22% on every
            # cell of the three routines the campaign was built to price. That the
            # majority tips at all is a property of bench.c's ladder -- dgemm
            # contributes 20 cells, dgemv 8 -- not of the hardware, which is why
            # the guard is on the parity branch and not a threshold change.
            {"kind": "verdict_code", "one_of": ["MIXED"]},
            {
                "kind": "coherent_subsets",
                "expect": [
                    "routine:dtrsm:V1",
                    "routine:dtrmm:V1",
                    "routine:dsymm:V1",
                    # The small regime qualifies once the majority is over families
                    # rather than rows, and it did not before: dgemm and sgemm are one
                    # family, so the small ladder is gemm and syrk at parity against
                    # trsm, trmm and symm ahead -- three of five, which is the 60%
                    # majority exactly. Medium and large also carry dgemv, daxpy and
                    # ddot, so three of eight there, and they do not qualify. This
                    # entry is the normalisation's visible effect on the routine set a
                    # real host actually produces.
                    "regime:small:V1",
                ],
            },
            {"kind": "stdout_contains", "text": "CONSEQUENCE: the difference is routine-localised"},
            {"kind": "stdout_absent", "text": "publish the negative result"},
            # 4. and this verdict is now CERTIFIED, which is the point of the
            # 2026-08-20 change and the reason this expectation inverted. It used to
            # assert the opposite -- VERDICT-CAVEAT plus "verified=null" -- because
            # three of the four affected routines had no correctness check, so the
            # campaign's flagship sentence ("worth closing, for TRSM/TRMM/SYMM")
            # rested on records nothing had checked. That was a faithful fixture of a
            # broken producer. Now every routine carries a corner check, so a clean
            # arm must produce NO unverified caveat at all: if this line ever goes
            # back to expecting the null, a check has been lost.
            #
            # The caveat MACHINERY is still tested -- deliberately now, by
            # sc_unverified_verdict, rather than as a side effect of the gap.
            {"kind": "stdout_absent", "text": "verified=null"},
            {"kind": "json_number", "path": "verdict.unverified_cells", "op": "==", "value": 0},
            {"kind": "exit_bits_clear", "bits": [2, 4, 8, 16]},
        ],
    )


def sc_unverified_verdict():
    """The same routine-localised N2-gap finding as `full-routine-set`, but on a
    dataset where the three affected routines carry `verified: null`.

    WHY THIS SCENARIO REPLACES A SIDE EFFECT. Until 2026-08-20, bench.c had a
    corner check for dgemm and for nothing else, so every fixture that planted an
    effect on TRSM/TRMM/SYMM got the VERDICT-CAVEAT for free -- `full-routine-set`
    asserted it, and that assertion was really a fixture of the producer's gap
    rather than a test of the analysis. Closing the gap in bench.c would then have
    silently retired the only coverage the caveat had: the scenario would have been
    edited to stop expecting it, the machinery would have gone untested, and the
    next routine added to the matrix would have started life at `verified: null`
    with nothing asserting that the report says so.

    So the null is now planted deliberately. This is not a hypothetical shape: the
    P2 dry-run dataset is exactly it -- 31723 of 42743 cells -- and it is still the
    shape of any archived pass, any newly added routine before its check lands, and
    any reference library whose arm ran an older binary.

    The claim is narrow and is the one that matters: an unverified finding is still
    REPORTED (the analysis must not suppress it -- that would be tuning the
    analysis until it finds nothing) but it is never reported as certified. Both
    halves are asserted, because each without the other is a different bug."""
    return Scenario(
        name="unverified-verdict",
        description=(
            "The N2-gap finding on records nothing checked: dtrsm/dtrmm/dsymm carry "
            "verified=null. The verdict must still be MIXED and must still name the "
            "routines, and it must carry the caveat that says nothing verified them."
        ),
        hosts=[_host()],
        routines=BENCH_ROUTINES,
        arms=_arms(
            v1_gain=flat(1.22),
            routines=N2_GAP_ROUTINES,
            verified_null_routines=N2_GAP_ROUTINES,
        ),
        expect=[
            # 1. the finding survives. A null is not a licence to drop the cell:
            # absent and unverified are different claims, and an analysis that
            # quietly discarded the unverified rows would report a null result on a
            # host that had a +22% effect on three routines.
            {"kind": "verdict_code", "one_of": ["MIXED"]},
            {"kind": "cross_verdicts_where", "routine": "dtrsm", "expect": "V1-set-ahead", "min_rows": 4},
            {"kind": "cross_verdicts_where", "routine": "dsymm", "expect": "V1-set-ahead", "min_rows": 4},
            # 2. and it is never presented as certified.
            {"kind": "stdout_contains", "text": "VERDICT-CAVEAT:"},
            {"kind": "stdout_contains", "text": "verified=null"},
            {"kind": "json_number", "path": "verdict.unverified_cells", "op": ">", "value": 0},
            # 3. a null is NOT a failure. Exit bit 2 is for poisoned records -- a
            # verified=false -- and conflating the two would make an honest gap
            # indistinguishable from a wrong answer, which is the distinction the
            # tri-state exists for.
            {"kind": "json_number", "path": "inputs.excluded.verified_false", "op": "==", "value": 0},
            {"kind": "exit_bits_clear", "bits": [2, 4, 8, 16]},
            # 4. section 5's coverage table names the routines with no coverage, so a
            # reader can tell WHICH routines are uncertified rather than only how
            # many cells are. That sentence is what made the gap visible in the first
            # place.
            {"kind": "stdout_contains", "text": "no check exists for this routine"},
        ],
    )


def sc_reference_library_absent():
    """A host where no reference library ran at all -- ArmPL not installed, or its
    licence check failed. Section 1 has nothing to be a deficit against.

    This is the ordinary case for at least one host in the campaign: ArmPL is not
    on every AMI, and standing order 3 forbids filling the gap from the published
    comparisons. So the row must say NO DATA and say why, and section 2 -- which
    compares the kernel sets against each other and needs no reference -- must be
    unaffected. A section 1 that silently emitted an empty table would let a
    reader conclude "no deficit".

    The reference arms are absent the way the producers make them absent, not by
    being left out of the arm list. build-libs.sh emits an `armpl/native` manifest
    record unconditionally -- `built:false`, an EMPTY blas_sha, and "ARMPL_DIR unset
    or not a directory" as the reason -- and a `blis` record the same way when the
    build fails; run-matrix.sh then censuses both `build_failed`. So this host's
    manifest names two reference arms that produced nothing, which is the shape that
    has to be readable as an explained absence rather than a coverage hole."""
    arms = [a for a in _arms(v1_gain=flat(1.22)) if a.library == "openblas"]
    arms += [
        Arm(
            "armpl",
            "native",
            "unforced",
            thread_backend="openmp",
            measured=False,
            manifest_built=False,
            manifest_reason="ARMPL_DIR unset or not a directory",
            census_status="build_failed",
            census_reason="ARMPL_DIR unset or not a directory",
        ),
        Arm(
            "blis",
            "auto",
            "unforced",
            measured=False,
            manifest_built=False,
            manifest_reason="build failed, see /opt/gbb/blis.buildlog",
            census_status="build_failed",
            census_reason="build failed, see /opt/gbb/blis.buildlog",
        ),
    ]
    return Scenario(
        name="reference-library-absent",
        description=(
            "No non-OpenBLAS library produced a record on this host: both reference arms "
            "are in the manifest as built:false with an empty blas_sha. Section 1 must "
            "report NO DATA with a reason and compute nothing; section 2 must still "
            "answer the kernel-set question."
        ),
        hosts=[_host()],
        arms=arms,
        expect=[
            {"kind": "deficit_nodata", "scope": "instance", "min_rows": 1},
            {"kind": "stdout_contains", "text": "NO DATA — reference library absent"},
            {"kind": "deficit_absent"},
            # Section 2 is untouched: the planted kernel-set effect is still found.
            {"kind": "verdict_code", "one_of": ["V1-SET-AHEAD"]},
            {"kind": "cross_delta_where", "routine": "dgemm", "op": ">=", "value": 0.15},
            # An unbuilt arm with a stated reason is an explained absence. This is
            # the ordinary state of at least one campaign host, so a dataset that
            # sets bit 4 here would set it on every real run and make the bit
            # meaningless.
            {"kind": "json_number", "path": "coverage.missing_unexplained", "op": "==", "value": 0},
            {"kind": "exit_bits_clear", "bits": [4]},
            {"kind": "stdout_contains", "text": "ARMPL_DIR unset or not a directory"},
        ],
    )


def sc_reference_arm_partial():
    """ArmPL ran, but produced no dtrsm. The per-condition twin of the scenario
    above, and a different claim: the reference library is present, so the instance
    is not skipped, but one routine has no reference to be measured against.

    Real: reference libraries do not cover the same routine set, and a licence or
    a missing kernel takes out one routine rather than the library. The row has to
    name the arm it could not compare and stay out of the payload -- a deficit
    computed against a reference that did not run for that routine would be a
    number nobody measured."""
    arms = _arms(v1_gain=flat(1.22))
    for a in arms:
        if a.library == "armpl":
            a.omit_routines = ("dtrsm",)
    return Scenario(
        name="reference-arm-partial",
        description=(
            "The reference library ran but has no dtrsm. Section 1 must print NO DATA "
            "for dtrsm naming the absent reference arm, keep it out of the payload, and "
            "report the other routines normally."
        ),
        hosts=[_host()],
        arms=arms,
        expect=[
            {"kind": "deficit_nodata", "min_rows": 4},
            {"kind": "deficit_absent", "routine": "dtrsm"},
            # ...and the routines that do have a reference are unaffected.
            {
                "kind": "deficit_where",
                "routine": "dgemm",
                "shipped_only": True,
                "op": "<=",
                "value": 0.05,
                "min_rows": 4,
            },
            {"kind": "deficit_shipped", "min_rows": 8},
            # One reference candidate on this host, so every row that does have a
            # deficit must name it. This is the coverage half of the choice
            # `manifest-shapes` exercises with two candidates: losing dtrsm must not
            # promote some other arm into the reference slot for the other routines.
            {"kind": "deficit_reference", "arm": "armpl/native/unforced", "min_rows": 8},
            # dtrsm still has a kernel-set verdict: section 2 does not need ArmPL,
            # and losing the reference must not lose the comparison that matters.
            {"kind": "cross_verdicts_where", "routine": "dtrsm", "expect": "V1-set-ahead", "min_rows": 2},
            # And the hole is reported as a hole. The census says this arm ran, so
            # nothing in results/ accounts for the absent dtrsm cells: by standing
            # order 11 that is MISSING-UNEXPLAINED and bit 4, not a silently
            # narrower table. Asserted with the count, because "some hole
            # somewhere" would also be satisfied by a coverage model that had lost
            # track of which arm was missing.
            #
            # 24, and the arithmetic is the pad axis: a coverage cell is
            # (instance, arm, threads, routine, regime, lda_pad, incx), dtrsm is in
            # PADDED_ROUTINES, and this fixture runs two thread counts. So
            # 2 threads x (small 5 pads + medium 5 pads + large 2 pads) = 24, where
            # 5 is pad 0 plus LDA_PADS_EXTRA and 2 is pad 0 plus
            # LDA_PADS_EXTRA_LARGE. It was 6 before #2 landed the pad axis
            # (2 threads x 3 regimes x pad 0 alone), and it moves again if either
            # pad tuple or PADDED_ROUTINES changes -- which is the point of
            # asserting the number rather than ">= 1".
            {"kind": "exit_bits_set", "bits": [4]},
            {"kind": "exit_bits_clear", "bits": [2, 8, 16]},
            {"kind": "json_number", "path": "coverage.missing_unexplained", "op": "==", "value": 24},
        ],
    )


def sc_manifest_shapes():
    """Every arm shape `build-libs.sh` and `run-matrix.sh` can emit that no fixture
    contained.

    The manifest is where the expected-arm census comes from, so a shape those two
    scripts can write and no fixture can produce is a branch of `decompose.py` that
    first runs on real data. Four were missing, all from call sites that exist
    today: the OpenMP-threaded OpenBLAS (`DYNAMIC_OMP`, `thread_backend:openmp`),
    the `DYNAMIC_OMP_BOUND` arm the runner synthesises after the manifest loop to
    measure the pinning delta rather than assume it (census-only, no manifest
    record), a BLIS arm, and a control target built but not runnable.

    `built:true` + `runnable:false` is the only way that pair occurs: build-libs.sh
    builds a control target anyway and records that the host cannot run it, and
    run-matrix.sh then censuses it `unrunnable` per thread count. Both halves are
    here, because that is what the producers write -- the manifest reason alone is
    a shape only a sweep truncated before the arm shipped its census would give.
    NEOVERSEN2 is the faithful choice on this host: `requires()` puts it behind
    sve2, and this host reports sve without sve2.

    BLIS also gives section 1 two reference candidates for the first time, so the
    `max(present_refs, key=(conditions, label))` choice at decompose.py:1102 is
    exercised rather than defaulted. Both candidates cover every condition here, so
    the tie falls to the label -- which is why what gets asserted is that ONE
    reference is named and it is the same one for every arm in a cell, not which of
    the two it is. Coverage deciding the choice is `reference-arm-partial`."""
    arms = _arms(v1_gain=flat(1.22))
    unrunnable = "target requires sve2 which this host does not report (sve=true sve2=false)"
    arms += [
        Arm("openblas", "DYNAMIC_OMP", "unforced", thread_backend="openmp"),
        Arm("openblas", "DYNAMIC_OMP_BOUND", "unforced", thread_backend="openmp", in_manifest=False),
        Arm("blis", "auto", "unforced"),
        Arm(
            "openblas",
            "NEOVERSEN2",
            "unforced",
            measured=False,
            census_status="unrunnable",
            census_reason=unrunnable,
            manifest_runnable=False,
            manifest_reason=unrunnable,
        ),
    ]
    return Scenario(
        name="manifest-shapes",
        description=(
            "Every arm shape the producers can write: the OpenMP OpenBLAS and its bound "
            "twin, a BLIS arm, and a control target built but not runnable here. Two "
            "reference candidates, so section 1 must name one and name it consistently."
        ),
        hosts=[_host()],
        arms=arms,
        expect=[
            # built:true + runnable:false + census unrunnable is a stated reason,
            # so it is an explained absence and not a hole.
            {"kind": "json_number", "path": "coverage.missing_unexplained", "op": "==", "value": 0},
            {"kind": "json_number", "path": "coverage.by_status.unrunnable", "op": ">", "value": 0},
            {"kind": "exit_bits_clear", "bits": [4]},
            # And the reason reaches the artifact, not just the census file. Both
            # producers write this string for this arm and the census is consulted
            # first, so what is asserted here is that an explained absence explains
            # itself to a reader of the report.
            {"kind": "stdout_contains", "text": "sve2 which this host does not report"},
            # Two candidate reference arms: exactly one is named per cell, and every
            # OpenBLAS arm in that cell is measured against the same one. Rows
            # compared against different references do not belong in one table.
            {
                "kind": "deficit_reference",
                "routine": "dgemm",
                "one_of": ["armpl/native/unforced", "blis/auto/unforced"],
                "min_rows": 8,
            },
            {
                "kind": "deficit_reference",
                "routine": "dgemv",
                "one_of": ["armpl/native/unforced", "blis/auto/unforced"],
                "min_rows": 4,
            },
            {"kind": "deficit_shipped", "min_rows": 12},
            # The two OpenMP arms are their own arms, not variants of a target
            # already present, so both must reach the report rather than merge.
            {"kind": "stdout_contains", "text": "openblas/DYNAMIC_OMP/unforced"},
            {"kind": "stdout_contains", "text": "openblas/DYNAMIC_OMP_BOUND/unforced"},
            # Four extra arms, none of them V1 or V2, must not disturb the cross.
            {"kind": "cross_verdicts_where", "routine": "dgemm", "expect": "V1-set-ahead", "min_rows": 4},
            {"kind": "verdict_code", "one_of": ["V1-SET-AHEAD"]},
            {"kind": "exit_bits_clear", "bits": [2, 8]},
            # Every arm here has target_effective null -- no producer but BLIS reads
            # its own config back, and this BLIS arm is the pre-2026-08-20 shape. A
            # null must therefore say NOTHING: it means no read-back was attempted,
            # which is the ordinary state of four of the five libraries and of every
            # archived dataset. This is the silent half of the pair whose firing half
            # is `target-readback`; without it, the check could be made to pass by
            # raising an anomaly on absence, which would fire on every real run and
            # so tell a reader nothing.
            {"kind": "anomaly_kind_absent", "kind_name": "target_readback_failed"},
            {"kind": "anomaly_kind_absent", "kind_name": "target_resolved_elsewhere"},
        ],
    )


def sc_target_readback():
    """Two BLIS arms whose builds answered the question `target` cannot: one where
    the read-back failed, and one where the config it asked for resolved to a
    different one at runtime.

    WHY THIS EXISTS. `target` is a REQUEST. The first P2 pass shipped its BLIS arm
    as `target: "auto"` with nothing recording what auto resolved to, and that arm
    ran single-threaded large DGEMM at 0.35x OpenBLAS -- a number that means
    misconfigured, not slow, because no threading is involved at one thread. The
    manifest could not express that and the analysis could not see it. It is
    standing order 10 one library over from the coretype axis: `configure auto` on
    Neoverse V2 falling back to a generic arm64 sub-config is exactly a request
    landing somewhere other than where the label claims, and a mislabelled arm is
    a plausible wrong answer rather than a failed run.

    The two cases are deliberately different claims and get different sentences:

      - `unknown` means a read-back was ATTEMPTED and FAILED. The label is
        unverifiable, so it is a request presented as an observation.
      - a value that differs from `target` is NOT a fault. A family config resolving
        to a sub-config at runtime is the normal thing and the whole reason the field
        exists; what matters is that the resolved name reaches the report, so a
        deficit is read against the kernel set that actually ran.

    Neither is an admissibility failure -- the reference arm is not the subject of
    the campaign -- so both sit at warning level and neither may set an exit bit.
    That restraint is asserted, because a check that made an ordinary resolution
    fatal would be removed within a week and then the P2 defect would be
    undetectable again."""
    arms = _arms(v1_gain=flat(1.22))
    arms += [
        # What the P2 pass shipped, plus the field it lacked: the probe compiled and
        # ran but could not answer, so build-libs.sh writes "unknown" and a reason
        # rather than echoing "auto".
        Arm("blis", "auto", "unforced", target_effective="unknown"),
        # And the benign case, which must still be legible: `arm64` is a config
        # FAMILY, so bli_arch_query_id() naming a member of it is correct behaviour
        # and not a mismatch.
        Arm("blis", "arm64", "unforced", target_effective="neoversev2"),
    ]
    return Scenario(
        name="target-readback",
        description=(
            "One BLIS arm whose config read-back failed and one whose requested family "
            "resolved to a sub-config. Both facts must reach section 5 by name, and "
            "neither may be treated as inadmissible."
        ),
        hosts=[_host()],
        arms=arms,
        expect=[
            {"kind": "anomaly_kind_present", "kind_name": "target_readback_failed"},
            {"kind": "anomaly_kind_present", "kind_name": "target_resolved_elsewhere"},
            # Named, not counted. "one arm could not be verified" sends nobody to a
            # build log; the library and the resolved config do.
            {"kind": "stdout_contains", "text": "blis/auto"},
            {"kind": "stdout_contains", "text": "neoversev2"},
            # Not fatal, and not poison: the records are real measurements of
            # whatever kernel set ran, so nothing is excluded and no bit is set.
            {"kind": "json_number", "path": "inputs.excluded.verified_false", "op": "==", "value": 0},
            {"kind": "exit_bits_clear", "bits": [2, 4, 8, 16]},
            # Two extra reference candidates must not disturb the kernel-set cross,
            # which is OpenBLAS against OpenBLAS and needs no reference at all.
            {"kind": "verdict_code", "one_of": ["V1-SET-AHEAD"]},
            {"kind": "cross_verdicts_where", "routine": "dgemm", "expect": "V1-set-ahead", "min_rows": 4},
        ],
    )


def sc_reference_regime_flip():
    """Two reference candidates whose coverage disagrees BETWEEN regimes, which is
    how a per-group reference choice flips inside one comparison.

    The defect this is the regression test for: section 1 used to choose its
    reference arm per comparison group, by cell count. The group key contains the
    regime, so the arm with more cells in the small regime could differ from the arm
    with more cells in the large regime, and section 1 would then measure the small
    rows against one reference and the large rows against another -- with nothing in
    either row looking wrong.

    That is a count-derived SELECTION feeding a count-derived CONSEQUENCE, and the
    consequence is section 9's "deficit concentrated in the small regime", which is a
    decision-guide output: it is the sentence that says which kernels to fix. Section
    4a keys on reference_arm (it has to -- rows measured against different references
    are not one profile), so a flip splits the profile in two, `small_minus_large`
    becomes None in both halves, and the whole thing presents as `MISSING:large` /
    `MISSING:small` rather than as an error. A reader sees thin coverage, not a
    reference that moved under them.

    So the planted shape: armpl is missing two SMALL sizes and blis is missing three
    medium plus two large ones. Per group that is a flip -- blis covers small better,
    armpl covers medium and large better. Per host, armpl covers more conditions in
    total, so it is chosen everywhere and every regime of every profile is measured
    against the same library.

    ARMPL WINNING IS THE LOAD-BEARING PART, and it is why the coverage is planted in
    this direction rather than the other: the tie-break after coverage and conditions
    is `arm_label`, and `max()` on labels prefers "blis/auto/unforced" over
    "armpl/native/unforced". A fixture in which the conditions-winner and the
    alphabet-winner were the same arm would pass just as well against a selector that
    read only the alphabet.

    The deficit is planted small-concentrated (15% small, 0% large) so that section
    9's consequence has something to say, and the two kernel sets are at parity so
    that nothing else in the report competes for the verdict."""
    small_led = {"small": 0.85, "medium": 0.95, "large": 1.0}
    arms = _arms(v1_gain=small_led, v2_gain=small_led)
    for a in arms:
        if a.library == "armpl":
            # Two of sixteen small sizes: enough to lose the small groups on count,
            # not enough to lose the group entirely (it must stay PRESENT everywhere,
            # or breadth-of-coverage would decide the choice and the conditions
            # tie-break -- the thing under test -- would never be reached).
            a.omit_sizes = (8, 16)
    arms.append(
        Arm(
            "blis",
            "auto",
            "unforced",
            # Three medium and the two smallest large rungs. At one thread the large
            # ladder is capped at 4096, so this leaves blis exactly one large size
            # there: still present, comfortably outcounted.
            omit_sizes=(320, 384, 448, 2048, 3072),
        )
    )
    return Scenario(
        name="reference-regime-flip",
        description=(
            "Two reference candidates, armpl thinner in small and blis thinner in "
            "medium+large. A per-group reference choice flips between regimes and nulls "
            "section 4a's small-large gap; the per-host choice must name armpl in every "
            "regime and keep the gap computable."
        ),
        hosts=[_host()],
        arms=arms,
        expect=[
            # The invariant, asserted at host scope: ONE reference arm across every
            # section-1 row on this instance, and it is the conditions-winner rather
            # than the alphabet-winner.
            {
                "kind": "deficit_reference_invariant",
                "instance": "c7g.metal",
                "arm": "armpl/native/unforced",
                "min_rows": 24,
            },
            {"kind": "stdout_contains", "text": "chosen ONCE for this host"},
            {"kind": "stdout_contains", "text": "not chosen: blis/auto/unforced"},
            # The consequence the flip used to break, on the slice the design fills in
            # every regime: dgemm at pad 0 and NN. Every profile there carries all
            # three regimes and a computable small-large gap at the planted magnitude.
            # Under a per-group reference these rows split in two and both halves go
            # to MISSING, which is what `complete` refuses.
            {
                "kind": "regime_gap_deficit",
                "routine": "dgemm",
                "lda_pad": 0,
                "transa": "N",
                "transb": "N",
                "op": ">=",
                "value": 0.10,
                "min_rows": 8,
                "complete": True,
            },
            {
                "kind": "regime_gap_deficit",
                "routine": "dgemm",
                "lda_pad": 0,
                "transa": "N",
                "transb": "N",
                "op": "<=",
                "value": 0.20,
                "min_rows": 8,
            },
            {"kind": "stdout_contains", "text": "deficit concentrated in the small regime"},
            # The planted deficit reaches section 1 on the arm the wheels ship, and
            # the kernel sets stay at parity so nothing competes for the verdict.
            {"kind": "deficit_shipped", "min_rows": 12},
            {"kind": "cross_verdicts_all", "expect": "parity", "min_rows": 12},
            {"kind": "verdict_code", "one_of": ["NULL"]},
            # The thinned coverage is PARTIAL, not a hole: both references ran every
            # cell they appear in and produced some of its sizes, which is what
            # omit_sizes models. Zero MISSING-UNEXPLAINED is the load-bearing half --
            # a fixture that planted the flip by making a reference vanish from whole
            # cells would be testing the coverage census instead. 52 = armpl's 20 thin
            # small cells + blis's 22 medium and 10 large, and it moves with the pad
            # and transpose axes, which is why it is asserted rather than described.
            {"kind": "json_number", "path": "coverage.missing_unexplained", "op": "==", "value": 0},
            {"kind": "json_number", "path": "coverage.partial", "op": "==", "value": 52},
            {"kind": "exit_bits_set", "bits": [4]},
            {"kind": "exit_bits_clear", "bits": [2, 8, 16]},
        ],
    )


def sc_denominator_intersection():
    """The per-rung max sits above the common-size set, so the two denominator
    policies give different numbers and the fixture can tell them apart.

    Standing order 1's denominator is the best large dgemm on the host at that
    thread count. bench.c caps the large ladder at n=4096 below 8 threads, so the
    1-thread ladder has three rungs and the 64-thread ladder has five: a per-rung max
    would divide 1-thread efficiency by a ceiling drawn from one ladder and 64-thread
    efficiency by a ceiling drawn from another. Those are not the same quantity, and
    section 6 puts them in adjacent rows.

    The policy is therefore the max over the sizes this host ran at EVERY thread
    count. Here the reference arm is 8% faster at 6144 and 8192 -- deterministically,
    by size rather than by regime, so the winning rung cannot move with the noise
    key -- which puts the unrestricted max at n=8192 and the restricted one at n=4096.
    A revert to per-rung maxima moves `best_dgemm_m` from 4096 to 8192 at 64 threads,
    which is asserted, and it also silently raises that row's ceiling by ~8% while
    leaving the 1-thread row alone, which is the harm.

    peak_factor is left at the host default. It used to be pinned to 1.0 here to keep
    the headroom cross-check quiet, since the restriction raises `peak_fma /
    best_dgemm` by construction (the ratio's denominator got smaller). That check is
    retired -- see `peak-fma-retired` -- so there is nothing left for the restriction to
    trip, and pinning the factor would only imply otherwise."""
    arms = _arms(v1_gain=flat(1.0), v2_gain=flat(1.0))
    for a in arms:
        if a.library == "armpl":
            a.gain_sizes = {6144: 1.08, 8192: 1.08}
    return Scenario(
        name="denominator-intersection",
        description=(
            "An arm 8% faster at n=6144/8192, which the 1-thread ladder does not reach. "
            "Standing order 1's denominator must come from the sizes common to both "
            "thread points (n=4096), not from each thread point's own best rung."
        ),
        hosts=[_host()],
        arms=arms,
        expect=[
            # 64 threads: five rungs available, three shared, and the denominator is
            # the best of the three -- not the 8192 rung that only this thread point
            # has. The unrestricted max is kept as provenance and must still name 8192.
            {
                "kind": "scaling_denominator",
                "instance": "c7g.metal",
                "threads": 64,
                "basis": "common",
                "best_m": 4096,
                "common_sizes": [2048, 3072, 4096],
                "unrestricted_m": 8192,
                "op": ">=",
                "value": 0.05,
            },
            # 1 thread: the ladder IS the common set, so the restriction costs exactly
            # nothing and the second line must not print. Asserted as == 0 rather than
            # "small": a policy that restricted only some rows would show up here.
            {
                "kind": "scaling_denominator",
                "instance": "c7g.metal",
                "threads": 1,
                "basis": "common",
                "best_m": 4096,
                "common_sizes": [2048, 3072, 4096],
                "op": "==",
                "value": 0.0,
            },
            {"kind": "stdout_contains", "text": "not used: that size is absent at some thread point"},
            {"kind": "stdout_contains", "text": "at n=4096 of 5 size(s)"},
            {"kind": "stdout_contains", "text": "at n=4096 of 3 size(s)"},
            # The failure flag this scenario is about stays down: the intersection is
            # non-empty, so nothing fell back. The `headroom` assertion that used to sit
            # beside it has moved to `peak-fma-retired`, where the host actually plants a
            # peak_fma above the best GEMM and the absence of a flag is the claim. Here
            # it would assert nothing -- this host's peak_factor is below 1 -- and an
            # off-topic assertion is how a scenario stops owning its own expectations.
            {"kind": "anomaly_kind_absent", "kind_name": "denominator_not_comparable"},
            {"kind": "verdict_code", "one_of": ["NULL"]},
            # Nothing is missing here -- the restriction is policy operating on a
            # complete dataset, and it must not read as a coverage problem.
            {"kind": "json_number", "path": "coverage.missing_unexplained", "op": "==", "value": 0},
            {"kind": "json_number", "path": "coverage.partial", "op": "==", "value": 0},
            {"kind": "exit_bits_clear", "bits": [2, 4, 8, 16]},
        ],
    )


def sc_denominator_thread_point_dark():
    """A thread point with NO large dgemm at all must drop out of the intersection,
    not empty it.

    The intersection is over the thread points that HAVE large dgemm. Without that
    filter, one thread point missing the whole regime contributes an empty set, the
    intersection is empty for the entire host, and every other thread point's
    denominator falls back to its own per-rung max -- so one missing rung would
    silently un-comparable the rows that were fine. That is the opposite of what the
    restriction is for, and it presents as a section-5 anomaly on rows whose data is
    complete.

    Planted by removing dgemm's three cheapest large rungs from every arm. At one
    thread the cap has already removed 6144 and 8192, so that thread point has no
    large dgemm whatsoever; at 64 threads it has 6144 and 8192 and they are shared
    with nothing, so they are the whole common set. The 1-thread row then carries no
    denominator of its own -- which is the honest answer, and is reported where it
    happens rather than propagated -- while the 64-thread row stays `common`.

    The removal is per routine, not per size across the board, so the other large
    ladders are intact and the loss is confined to the one the denominator reads."""
    arms = _arms(v1_gain=flat(1.0), v2_gain=flat(1.0))
    for a in arms:
        a.omit_routine_sizes = {"dgemm": (2048, 3072, 4096)}
    return Scenario(
        name="denominator-thread-point-dark",
        description=(
            "No large dgemm at 1 thread at all (the cap took 6144/8192, the fixture took "
            "the rest). That thread point must drop out of the intersection rather than "
            "empty it, leaving the 64-thread denominator comparable and the 1-thread row "
            "explicitly without one."
        ),
        hosts=[_host()],
        arms=arms,
        expect=[
            {
                "kind": "scaling_denominator",
                "instance": "c7g.metal",
                "threads": 64,
                "basis": "common",
                "best_m": 8192,
                "common_sizes": [6144, 8192],
                "op": "==",
                "value": 0.0,
            },
            {
                "kind": "scaling_denominator",
                "instance": "c7g.metal",
                "threads": 1,
                "basis": "absent",
                "best_m": None,
            },
            # The mutation kill: drop the "thread points that HAVE large dgemm" filter
            # and the 64-thread row goes to per-rung-fallback and raises this.
            {"kind": "anomaly_kind_absent", "kind_name": "denominator_not_comparable"},
            {"kind": "stdout_contains", "text": "best_large_dgemm=absent"},
            # The omission is invisible to the coverage census at 1 thread and that is
            # correct: no arm measured dgemm's small large rungs there, so the census
            # -- which derives its expectation from what some arm did measure -- has no
            # cell to call missing. It is asserted so that the fixture cannot be read
            # as "the census caught this"; the intersection filter is the only thing
            # standing between the dark thread point and every other row's denominator.
            {"kind": "json_number", "path": "coverage.missing_unexplained", "op": "==", "value": 0},
            {"kind": "json_number", "path": "coverage.partial", "op": "==", "value": 0},
            {"kind": "exit_bits_clear", "bits": [2, 4, 8, 16]},
        ],
    )


def sc_probe_unavailable():
    """`capture-env.sh` could not run the DYNAMIC_ARCH probe, so nobody knows what
    the shipped library selects on this host.

    Standing order 8's loud trigger is generic `ARMV8` on an SVE host. This is the
    case where that check did not happen: `openblas_dynamic_probe_status` is one of
    `not_attempted`/`build_failed`/`run_failed`, `openblas_dynamic_selection` is
    null, and `openblas_coretype_forcing` falls back to `not_probed` because
    capture-env.sh only probes forcing once the corename probe works. The faithful
    pairing is both fields together, which is why one scenario covers both.

    It was a note, and notes set no exit bit. That is the same failing-open that
    `sve_kernels:unknown` was fixed for, on the same axis, so it is now the same
    thing: a provenance gap and exit bit 8. Not an escalation -- absent evidence is
    not evidence of absence -- and `escalate` must stay empty here."""
    return Scenario(
        name="probe-unavailable",
        description=(
            "The DYNAMIC_ARCH probe failed to run, so the standing-order-8 generic-ARMV8 "
            "check was never performed and coretype forcing was never proven. A "
            "provenance gap on the campaign's central axis, not a note and not an alarm."
        ),
        hosts=[
            _host(
                dynamic_probe_status="run_failed",
                dynamic_selection=None,
                forcing="not_probed",
                warnings=(
                    "DYNAMIC_ARCH probe failed: could not build/link the corename probe against "
                    "/opt/gbb/openblas-DYNAMIC/lib. openblas_dynamic_selection is null -- "
                    "detection broke, it did not report a clean result.",
                ),
            )
        ],
        arms=_arms(v1_gain=flat(1.22)),
        expect=[
            {"kind": "anomaly_kind_present", "kind_name": "dynamic_probe_unavailable"},
            {"kind": "exit_bits_set", "bits": [8]},
            {"kind": "exit_bits_clear", "bits": [2, 4, 16]},
            # Absent evidence is not evidence of absence: this is not standing
            # order 8 firing, and a fixture that let it fire here would make the
            # escalation unreadable on the host where it matters.
            {"kind": "anomaly_kind_absent", "kind_name": "escalate"},
            {"kind": "stdout_contains", "text": "the coretype axis is unproven here"},
            # And the numbers are still analysed. A provenance gap says "do not
            # publish this yet", not "throw the sweep away".
            {"kind": "cross_verdicts_where", "routine": "dgemm", "expect": "V1-set-ahead", "min_rows": 4},
        ],
    )


def sc_topology_defaulted():
    """`lscpu` produced no topology, so `sockets`, `numa_nodes` and
    `threads_per_core` in the record are defaults rather than measurements.

    Two hosts, because the warning has two distinct consequences. On the first,
    `threads_per_core=1` must not be read as "SMT is off" -- that is the same
    fail-open shape as a null `verified` being treated as a pass, and Graviton
    having no SMT is a fact about Graviton, not evidence about this box. On the
    second, `numa_nodes=2` must NOT produce the cross-socket note, because a 2 that
    came from a default is not a measurement either. The warning lives in
    `warnings[]`, matched by substring, and CLAUDE.md's contract with
    capture-env.sh says do not reword it -- so this fixture is also the regression
    test for that string."""
    return Scenario(
        name="topology-defaulted",
        description=(
            "lscpu produced nothing, so the topology fields are defaults. SMT-off must "
            "read as unverified rather than as measured, and a defaulted numa_nodes must "
            "not produce a cross-socket note."
        ),
        hosts=[
            _host(warnings=(LSCPU_DEFAULTED_WARNING,)),
            _host(
                instance_type="c7g.16xlarge",
                instance_id="i-0000000000000002",
                run_id="synth-c7g16-pass1",
                numa_nodes=2,
                warnings=(LSCPU_DEFAULTED_WARNING,),
            ),
        ],
        arms=_arms(),
        expect=[
            {"kind": "stdout_contains", "text": "is a DEFAULT (lscpu produced no topology)"},
            {"kind": "stdout_contains", "text": "SMT being off is unverified here"},
            {"kind": "stdout_absent", "text": "multithreaded arms cross a socket"},
            # A defaulted topology is not a reason to distrust the timings, so
            # nothing here is invalid, poisoned or missing.
            {"kind": "exit_bits_clear", "bits": [2, 4, 8, 16]},
            {"kind": "verdict_code", "one_of": ["NULL"]},
        ],
    )


def sc_nodata_group_hole():
    """One (family, regime) group measured nowhere, under a coverage fraction that
    passes -- the absolute half of the coverage guard, planted.

    The kernel sets are at parity everywhere they compared, so without the guard
    this dataset reads out as "VERDICT: NULL -- publish the negative result". The
    hole is dtrsm's entire large regime on the V1 side: n=2048..8192 produced no
    record, which is what a sweep dying inside one routine's largest working set
    looks like. TRSM at n=8192 is the campaign's biggest allocation and the most
    likely single place for that to happen.

    The point is the arithmetic, and it is sharper than a single number. Twelve
    (family, regime) groups exist here and three of them are not comparable, so
    nodata_share_balanced is 3/12 = 25% -- under --max-nodata-fraction's 34%, and
    the fraction passes. But only ONE of those three is a hole: the other two are
    (axpy, medium) and (dot, medium), which hold a single level-1 length each
    against --min-sizes 3 and are thin by construction, forever, on every host.
    The real hole is 1 of 12 = 8%.

    So the fraction is simultaneously too high to be about the hole and too low to
    refuse the dataset, and no threshold fixes both: raising it to catch 8% would
    reject every campaign dataset ever produced on the two permanent thin groups,
    and leaving it at 34% publishes the null. A fraction can also always be diluted
    by densifying somewhere else -- which is precisely what took dgemm's total
    exclusion from 40% of the cross to 29% and let the verify-fail fixture publish a
    negative result over a kernel returning wrong answers. So the guard is a COUNT
    of dark groups, and one of them refuses a directional verdict outright.

    Large is also the regime where the DDR generation and the L3 step show, which
    makes "publish a null with TRSM-large missing" the specific wrong answer this
    campaign is most exposed to.

    Dark is measured against data, not against a verdict: this scenario's mutation
    partner is the over-firing direction, where treating a THIN group as dark turns
    (axpy, medium) -- one level-1 length, by construction -- into a hole and makes
    every clean scenario INCONCLUSIVE."""
    dark = _arms(v1_gain=flat(1.0), v2_gain=flat(1.0))
    for a in dark:
        if a.coretype == V1 or a.target == V1:
            a.omit_routine_sizes = {"dtrsm": SIZES_LARGE}
    return Scenario(
        name="nodata-group-hole",
        description=(
            "Parity everywhere that compared, and dtrsm's entire large regime absent on "
            "the V1 side. 3 of 12 groups are non-comparable (25%, under the 34% threshold) "
            "but only one is a hole -- the other two are thin by construction."
        ),
        hosts=[_host()],
        arms=dark,
        expect=[
            # The refusal, and the reason for it, named as the group that is dark.
            {"kind": "verdict_code", "one_of": ["INCONCLUSIVE"]},
            {"kind": "json_strings", "path": "verdict.dark_groups", "expect": ["trsm/large"]},
            {"kind": "stdout_contains", "text": "trsm/large was not measured at all"},
            # The counterfactual: the fraction alone does NOT catch this, so a guard
            # that were only a fraction would publish the line below.
            {
                "kind": "json_number",
                "path": "verdict.nodata_share_balanced",
                "op": "<=",
                "value": 0.34,
            },
            {"kind": "stdout_absent", "text": "publish the negative result"},
            # ...and the hole is confined to where it was planted. Every other
            # group compared, so this is not "the dataset is broken", it is
            # "one group of the design is missing and the rest is fine".
            {"kind": "cross_verdicts_where", "routine": "dgemm", "expect": "parity", "min_rows": 3},
            {"kind": "cross_nodata_where", "routine": "dtrsm", "regime": "large", "min_rows": 1},
            {"kind": "exit_bits_clear", "bits": [2, 16]},
        ],
    )


def sc_medium_large_localised():
    """An effect in medium+large that raw cell counts cannot see, because the pad
    axis is not there.

    dtrsm carries four extra lda_pads at small and medium and only one at large
    (LDA_PADS_EXTRA vs LDA_PADS_EXTRA_LARGE -- an 8192x8192 padded DTRSM is
    expensive and the campaign buys one pad there, not four). So dtrsm's cross rows
    are 5 small, 5 medium, 1 large. An effect on medium+large is 6 of 11 rows =
    55%, under the 60% majority; balanced by (family, regime) it is 2 of 3 = 67%,
    over it.

    That gap is the defect class in its subtlest form. The pad values are the same
    hardware claim re-asked at a different alignment, so counting them as
    independent votes lets the alignment axis decide whether a REGIME effect is
    reportable -- and it decides against the large regime specifically, which is
    where the DDR generation and the L3 step live. `family-swamped` plants the
    same class on the family axis and `v1-ahead-small` plants a regime effect broad
    enough for either weighting; neither can fail on this one, because neither has
    an axis whose density differs BETWEEN regimes of one routine.

    Deliberately not a campaign-level headline: one family of five moved, so MIXED
    is the honest verdict and the subset is where the answer lives."""
    return Scenario(
        name="medium-large-localised",
        description=(
            "The V1 set is 22% ahead on dtrsm in medium and large only. dtrsm holds 5 "
            "small, 5 medium and 1 large row because large buys one lda_pad, so raw "
            "counts make the effect 55% (under threshold) and balanced weight makes it 67%."
        ),
        hosts=[_host()],
        arms=_arms(
            v1_gain={"small": 1.0, "medium": 1.22, "large": 1.22},
            routines=("dtrsm",),
        ),
        expect=[
            # Where it was planted, in both regimes, and absent from small.
            {
                "kind": "cross_verdicts_where",
                "routine": "dtrsm",
                "regime": "medium",
                "expect": "V1-set-ahead",
                "min_rows": 3,
            },
            {
                "kind": "cross_verdicts_where",
                "routine": "dtrsm",
                "regime": "large",
                "expect": "V1-set-ahead",
                "min_rows": 1,
            },
            {
                "kind": "cross_verdicts_where",
                "routine": "dtrsm",
                "regime": "small",
                "expect": "parity",
                "min_rows": 3,
            },
            # One family of five moved, so no directional headline.
            {"kind": "verdict_code", "one_of": ["MIXED"]},
            {"kind": "stdout_absent", "text": "publish the negative result"},
            # THE discriminating assertion. Balanced, dtrsm carries 2 of its 3
            # (family, regime) groups = 66.7% and qualifies; raw, it is 28 of 48
            # rows = 58.3% and does not. So a raw-count coherent_subsets reports no
            # subset at all here, the parity majority stands unopposed, and the
            # verdict becomes NULL over a 22% effect across two whole regimes.
            {"kind": "coherent_subsets", "expect": ["routine:dtrsm:V1"]},
            {"kind": "stdout_contains", "text": "V1 set ahead in 28/48 cells (67% of family weight)"},
            {"kind": "exit_bits_clear", "bits": [2, 16]},
        ],
    )


def sc_transpose_lost():
    """An arm that ran NN and never ran TN at all -- the coverage census key, and
    why transa/transb had to be in it.

    `transpose-shopping` proves the axis has to be in the COMPARISON key, so the
    two transposes do not share a cell and let each arm be scored on its favourite.
    This is the other key: section 7's census cell was
    (threads, routine, regime, lda_pad, incx), with no transpose in it, so an arm
    that produced NN and nothing at all for TN was recorded as `partial` -- "some
    sizes of this cell are absent" -- when the truth was "this arm never ran TN".
    Those are different claims and standing order 11 turns on the difference: a
    partial cell reads as a truncated ladder, and a whole missing transpose is one
    copy kernel that did not execute. A SIGILL in `dgemm_tcopy` on a cross-built
    arm is exactly that, and it is a P2 hazard rather than a hypothetical.

    So the assertion is on the STATUS, not on a count of cells: with the transpose
    in the key the TN cells are MISSING-UNEXPLAINED and nothing is `partial`;
    without it, the merged cell is `partial` and missing_unexplained is zero. The
    two implementations disagree on both numbers in opposite directions, which is
    what makes this fixture able to fail."""
    lost = _arms()
    for a in lost:
        if a.coretype == V1:
            a.omit_trans = ("TN",)
    return Scenario(
        name="transpose-lost",
        description=(
            "One arm produced NN records and no TN records whatsoever. With transa/transb "
            "in the census cell key that is MISSING-UNEXPLAINED on the TN cells; without "
            "them it is a `partial` cell, which reads as a truncated size ladder instead."
        ),
        hosts=[_host()],
        routines=("dgemm",),
        level1=False,
        transposes=(("N", "N"), ("T", "N")),
        arms=lost,
        expect=[
            # The whole missing transpose is a hole, and it is reported as one.
            {"kind": "json_number", "path": "coverage.missing_unexplained", "op": ">=", "value": 1},
            {"kind": "stdout_contains", "text": "tr=TN"},
            # ...and NOT as a truncated ladder. This is the assertion the old key
            # fails: merged, the cell has NN records in it and reads `partial`.
            {"kind": "json_number", "path": "coverage.partial", "op": "==", "value": 0},
            {"kind": "exit_bits_set", "bits": [4]},
            # The arm that did run TN still compares there, so the hole is a hole
            # and not a collapse of the axis.
            {"kind": "cross_rows_have_trans", "routine": "dgemm", "values": ["NN", "TN"]},
        ],
    )


def _floor_band(name, description, spec, expect, **arm_kw):
    """One overlap-band scenario. A null cross on purpose: the band is a statement
    about the instrument, so a fixture that also planted a kernel effect would leave
    it ambiguous whether the band status came from the probe records or from the
    matrix ones.

    `arm_kw` reaches the arms, and the only thing it is used for is `spread` --
    `band_for()` is adaptive, so widening the band is how the one case that the band
    test alone cannot catch gets constructed."""
    return Scenario(
        name=name,
        description=description,
        hosts=[_host()],
        routines=("dgemm",),
        level1=False,
        arms=_arms(**arm_kw),
        floor_probe=spec,
        expect=expect,
    )


def sc_floor_band_agrees():
    """The two MIN_SECONDS floors give the same answer, with the signs scattered.

    The baseline the other three are read against, and the state the campaign needs
    to be in before section 4 can be read across n=256. Each floor draws its own
    jitter, so the difference is scatter rather than a lean: that is what AGREES
    means, and it is a different claim from AGREES-WITH-BIAS.

    Note what this scenario must NOT do -- set the exit bit. A confirming band is
    the clean case, and if bit 32 fired here it would fire on every good dataset."""
    return _floor_band(
        "floor-band-agrees",
        (
            "n=192..384 measured at 0.05 s and 0.30 s, independent noise on each. Deltas "
            "well inside the parity band and signs scattered, so the band confirms and the "
            "step at n=256 in section 4 is attributable to the hardware."
        ),
        {"mode": "agree"},
        [
            {"kind": "json_string", "path": "floor_overlap.status", "expect": "AGREES"},
            {"kind": "json_bool", "path": "floor_overlap.confirmed", "value": True},
            # 6 arms x 2 thread points x 5 sizes = 60 cells, x OVERLAP_REPS pairs each.
            # Asserted as a number because a probe that silently emitted half its pairs
            # would still say AGREES. Written against the constant rather than as a
            # literal so it tracks bench.c, which gate section 2 pins it to.
            {"kind": "json_number", "path": "floor_overlap.n_pairs", "op": "==", "value": 60 * OVERLAP_REPS},
            {"kind": "json_number", "path": "floor_overlap.cells", "op": "==", "value": 60},
            # The replication itself. Without this, dropping probe_rep from either
            # producer would collapse four pairs per cell into one and every other
            # assertion in this scenario would still pass.
            {"kind": "json_number", "path": "floor_overlap.reps_per_cell", "op": "==", "value": OVERLAP_REPS},
            {"kind": "json_number", "path": "floor_overlap.outside_band", "op": "==", "value": 0},
            {"kind": "json_number", "path": "floor_overlap.incomplete_cases", "op": "==", "value": 0},
            # The probe records are held out of the cross, not analysed inside it.
            # If they leaked in they would appear as extra dgemm rows at n=192..384
            # carrying a second min_seconds, and this is the cheapest assertion that
            # catches it: the census counts expected cells, and a leaked probe cell
            # is not one.
            {"kind": "json_number", "path": "coverage.missing_unexplained", "op": "==", "value": 0},
            {"kind": "json_number", "path": "coverage.partial", "op": "==", "value": 0},
            {"kind": "exit_bits_clear", "bits": [32]},
            {"kind": "stdout_contains", "text": "9. TIMING-FLOOR OVERLAP BAND"},
            {"kind": "stdout_contains", "text": "held out of the cross"},
        ],
    )


def sc_floor_band_biased():
    """The floors agree within band, but consistently: the short floor reads 2% low
    on every single pair.

    The mode worth having, and the reason the sign test is not redundant with the
    band test. Every delta here is inside the parity band, so a check that only
    asked "is any pair outside its band" would report a clean AGREES and the 2%
    lean would go unrecorded. It is a real property of the instrument and the
    reader needs the number, because a 2% step at n=256 in section 4 is now
    explained without appeal to the hardware.

    It is deliberately BELOW --min-effect, which is what makes it a footnote rather
    than a failure: nothing under the reporting floor can become a finding. Above
    it, the same fixture would be DISAGREES -- see `floor-band-disagrees`."""
    return _floor_band(
        "floor-band-biased",
        (
            "The 0.05 s floor reads 2% below the 0.30 s floor on every pair. Inside the "
            "parity band, so the band passes, but consistently signed, so the bias is "
            "reported as a quantity to discount a section-4 step against."
        ),
        {"mode": "bias", "amount": 0.02},
        [
            {"kind": "json_string", "path": "floor_overlap.status", "expect": "AGREES-WITH-BIAS"},
            # Confirmed: the floors do agree. The bias is a measurement, not a fault.
            {"kind": "json_bool", "path": "floor_overlap.confirmed", "value": True},
            {"kind": "json_number", "path": "floor_overlap.outside_band", "op": "==", "value": 0},
            {"kind": "json_number", "path": "floor_overlap.floor_sign_consistency", "op": "==", "value": 1.0},
            # Signed, and negative: the SHORT floor reads low. A fixture that only
            # asserted the magnitude would pass on an analysis that lost the sign,
            # and the sign is the whole content of "which floor reads low".
            {"kind": "json_number", "path": "floor_overlap.median_bias", "op": "<", "value": -0.01},
            {"kind": "json_number", "path": "floor_overlap.median_bias", "op": ">", "value": -0.04},
            {"kind": "exit_bits_clear", "bits": [32]},
            {"kind": "stdout_contains", "text": "consistently signed"},
            {"kind": "anomaly_kind_absent", "kind_name": "floor_overlap_unconfirmed"},
        ],
    )


def sc_floor_band_disagrees():
    """The short floor reads 12% low — past the band, so the per-regime floor is an
    instrument artefact sitting exactly where the answer is.

    The failure this whole probe exists to detect, and the one the alternative
    design (move the transition to n=512 and assume) could never have detected. The
    consequence is specific: section 4's small-minus-large number straddles n=256,
    so it cannot be published, and the DECISION section says so next to the
    sentence that would have been quoted."""
    return _floor_band(
        "floor-band-disagrees",
        (
            "The 0.05 s floor reads 12% below the 0.30 s floor, past the parity band. Bit 32, "
            "a hard section-5 anomaly, and the small-regime CONSEQUENCE in the DECISION "
            "section carries a do-not-publish caveat."
        ),
        {"mode": "disagree", "amount": 0.12},
        [
            {"kind": "json_string", "path": "floor_overlap.status", "expect": "DISAGREES"},
            {"kind": "json_bool", "path": "floor_overlap.confirmed", "value": False},
            {
                "kind": "json_number",
                "path": "floor_overlap.outside_band",
                "op": "==",
                "value": 60 * OVERLAP_REPS,
            },
            # A real effect reproduces, and the analysis has to say so: every one of
            # the 60 cells is out of band in all its reps with one sign. This is the
            # assertion that separates a planted effect from jitter meeting a band, and
            # it is the state the P2 pass's 2-of-390 was NOT in.
            {"kind": "json_number", "path": "floor_overlap.n_persistent_cells", "op": "==", "value": 60},
            {"kind": "json_number", "path": "floor_overlap.n_unreproduced_cells", "op": "==", "value": 0},
            {"kind": "stdout_contains", "text": "PERSISTENT"},
            {"kind": "exit_bits_set", "bits": [32]},
            # Section 5 is where a reader is told to look before trusting section 4,
            # so the finding has to reach it and not only section 9.
            {"kind": "anomaly_kind_present", "kind_name": "floor_overlap_unconfirmed"},
            {"kind": "stdout_contains", "text": "cannot be read across n=256"},
            # ...and it must not be mistaken for a coverage or provenance problem.
            # Every arm ran and every record is accounted for; only the instrument
            # is unvalidated.
            {"kind": "exit_bits_clear", "bits": [4, 8, 16]},
        ],
    )


def sc_floor_band_order_confounded():
    """Whichever floor ran FIRST reads 3% high, which is drift and not a floor
    effect — and the only reason that is knowable is that bench.c alternates.

    This is the scenario that justifies the alternation. Under a fixed order (short
    floor always first) this dataset and `floor-band-biased` would be the same
    dataset: "the first one reads high" and "the short one reads high" would be
    indistinguishable, and the analysis would have called a thermal or cache drift a
    floor bias. Alternating makes the floor-signed deltas alternate while the
    order-signed ones stay consistent, and the two consistency numbers separate the
    explanations.

    ORDER-CONFOUNDED is neither a pass nor a floor problem. The probe did not
    measure what it set out to measure, so it sets bit 32 -- the band is unconfirmed
    -- but the text says drift, because sending someone to change MIN_SECONDS over a
    drift would be the wrong fix applied confidently.

    IT ALSO PINS THE STATUS PRECEDENCE, which replication is what made necessary. A 3%
    order effect plus per-rep jitter puts a handful of the 240 individual pairs outside
    their parity bands -- 5, at these seeds -- while no cell reproduces. Before the
    precedence was fixed, `outside -> DISAGREES` came first and this dataset was
    reported as a floor disagreement, so ORDER-CONFOUNDED became unreachable the moment
    the band was replicated. That is why `outside_band` is asserted NON-zero here: this
    scenario is now the only place the ordering of the two branches is tested, and a
    fixture asserting 0 would have been asserting the pre-replication world."""
    return _floor_band(
        "floor-band-order-confounded",
        (
            "Whichever floor ran first reads 3% high. Inside the band, but the signs track "
            "measurement order and not the floor, which is only separable because bench.c "
            "alternates which floor goes first. Reported as ORDER-CONFOUNDED, not as a bias."
        ),
        {"mode": "order", "amount": 0.03},
        [
            {"kind": "json_string", "path": "floor_overlap.status", "expect": "ORDER-CONFOUNDED"},
            {"kind": "json_bool", "path": "floor_overlap.confirmed", "value": False},
            {"kind": "json_number", "path": "floor_overlap.order_sign_consistency", "op": "==", "value": 1.0},
            # The discriminator: order explains every pair, the floor explains a
            # minority. If these two were equal the alternation would have bought
            # nothing and the status would be unreachable.
            {"kind": "json_number", "path": "floor_overlap.floor_sign_consistency", "op": "<", "value": 0.5},
            # Pairs DO stray -- see the docstring. Both halves matter: strays exist, so
            # the DISAGREES branch was reachable and was passed over; and no cell
            # reproduces, which is the condition under which passing it over is right.
            {"kind": "json_number", "path": "floor_overlap.outside_band", "op": ">", "value": 0},
            {"kind": "json_number", "path": "floor_overlap.n_persistent_cells", "op": "==", "value": 0},
            {"kind": "exit_bits_set", "bits": [32]},
            {"kind": "anomaly_kind_present", "kind_name": "floor_overlap_unconfirmed"},
            {"kind": "stdout_contains", "text": "measured drift"},
            # The strays are named in the status line rather than dropped, so a reader
            # is not told the band was clean when five pairs were not.
            {"kind": "stdout_contains", "text": "none of them reproducing within a cell"},
        ],
    )


def sc_floor_band_bias_past_floor():
    """A 10% consistent bias on an arm noisy enough that the parity band widened to
    20% — so every pair passes the band test and the probe still has to fail.

    The hole in the band test, and the reason the sign test carries its own
    DISAGREES branch rather than only ever producing a footnote. `band_for()` is
    adaptive by design (see its KNOWN LIMIT note): it returns
    max(min_effect, dispersion), so a dispersed cell gets a band wider than
    --min-effect and a bias underneath that band is invisible to `outside_band`
    however large it is in reportable terms. 10% is twice the 5% reporting floor: a
    bias that size can produce a section-4 step at n=256 on its own, which is
    exactly the ambiguity the probe was built to remove.

    Read against `floor-band-biased`, which is the same mode at 2%. The two differ
    only in whether the bias clears --min-effect, and that difference alone moves the
    status from AGREES-WITH-BIAS to DISAGREES. Both have outside_band == 0, so a
    fixture set containing only one of them would leave the branch that separates
    them untested, and the analysis could report either as the other.

    `spread` is 0.20 and not 0.30: above --noisy-spread's 0.25 default the host would
    also be flagged noisy, and a scenario whose claim is about the band must not
    quietly depend on an anomaly it never mentions."""
    return _floor_band(
        "floor-band-bias-past-floor",
        (
            "The 0.05 s floor reads 10% low on every pair, on an arm dispersed enough that "
            "the adaptive parity band widened to 20%. Every pair is inside its own band, so "
            "the band test passes, and the signed bias is still past the 5% reporting floor: "
            "DISAGREES on the sign test alone."
        ),
        {"mode": "bias", "amount": 0.10},
        [
            {"kind": "json_string", "path": "floor_overlap.status", "expect": "DISAGREES"},
            {"kind": "json_bool", "path": "floor_overlap.confirmed", "value": False},
            # The discriminator against `floor-band-disagrees`: that scenario fails
            # because pairs fall outside the band, this one fails despite none doing.
            # If this number were ever nonzero the scenario would be testing the same
            # branch as the other and would prove nothing.
            {"kind": "json_number", "path": "floor_overlap.outside_band", "op": "==", "value": 0},
            {"kind": "json_number", "path": "floor_overlap.floor_sign_consistency", "op": "==", "value": 1.0},
            # Past --min-effect, which is the whole reason this is a failure and the
            # 2% version is not. Signed: the short floor is the one reading low.
            {"kind": "json_number", "path": "floor_overlap.median_bias", "op": "<", "value": -0.05},
            {"kind": "exit_bits_set", "bits": [32]},
            {"kind": "anomaly_kind_present", "kind_name": "floor_overlap_unconfirmed"},
            {"kind": "stdout_contains", "text": "The bands were widened by dispersion"},
            # And it must not be reported as drift: the order-signed deltas alternate
            # here, because the bias follows the floor and bench.c alternates the
            # order. ORDER-CONFOUNDED would send someone after the wrong cause.
            {"kind": "json_number", "path": "floor_overlap.order_sign_consistency", "op": "<", "value": 0.5},
        ],
        spread=0.20,
    )


def sc_floor_band_half():
    """Half a probe: every case carries the short floor and none carries the long
    one, so not one pair can be formed.

    Distinguishable from ABSENT and that distinction is the point. ABSENT means no
    probe ran, which is the state of every dataset written before bench.c grew one
    and must stay analysable. INCOMPLETE means a probe ran and produced unusable
    output -- a truncated file, an arm killed between the two measurements, or a
    producer bug -- and the band is unconfirmed. Reporting both as "no band" would
    make a broken probe look like an old dataset."""
    return _floor_band(
        "floor-band-half",
        (
            "Probe records present but only one floor among them, so no pair exists. "
            "INCOMPLETE and bit 32, which is a different claim from ABSENT: something "
            "produced half a probe, rather than nothing having produced one."
        ),
        {"mode": "half"},
        [
            {"kind": "json_string", "path": "floor_overlap.status", "expect": "INCOMPLETE"},
            {"kind": "json_number", "path": "floor_overlap.n_pairs", "op": "==", "value": 0},
            # Per (cell, rep), because a rep is its own case: the short-floor-only
            # records still arrive OVERLAP_REPS times and each one is an unpaired case.
            {
                "kind": "json_number",
                "path": "floor_overlap.incomplete_cases",
                "op": "==",
                "value": 60 * OVERLAP_REPS,
            },
            {"kind": "exit_bits_set", "bits": [32]},
            {"kind": "anomaly_kind_present", "kind_name": "floor_overlap_unconfirmed"},
            {"kind": "stdout_absent", "text": "no floor-overlap probe records"},
        ],
    )


def sc_floor_band_unreplicated():
    """The band ran once per cell and carries no `probe_rep` field at all — the shape
    of every dataset produced before 2026-08-20, including the P2 pass.

    Kept as a scenario rather than deleted along with the single-rep producer, for two
    reasons that pull the same way. (1) The P2 dataset is still the campaign's only
    measured cost basis and the errata read against it, so `decompose.py` has to keep
    analysing a fieldless probe exactly as it did — a missing `probe_rep` defaults to
    0, which is one rep per cell and not one cell holding every rep. (2) It is the only
    way to reach the analysis's no-replication branch, and that branch is the one that
    says out loud what Scott's ruling said: at one pair per cell the band is
    underpowered in both directions, so an out-of-band pair can be neither reproduced
    nor dismissed.

    `disagree` rather than `agree`, because the branch under test is in the DISAGREES
    why-string. An agreeing single-rep dataset would exercise the NO REPLICATION line
    in section 9 and nothing else, and the status line is what the anomaly table and
    the verdict quote."""
    return _floor_band(
        "floor-band-unreplicated",
        (
            "A pre-replication dataset: one pair per cell and no probe_rep field. Still "
            "analysable, still DISAGREES on a 12% short floor, and the status says the "
            "pairs can be neither reproduced nor dismissed rather than implying they were "
            "checked."
        ),
        {"mode": "disagree", "amount": 0.12, "legacy": True},
        [
            {"kind": "json_string", "path": "floor_overlap.status", "expect": "DISAGREES"},
            {"kind": "json_bool", "path": "floor_overlap.confirmed", "value": False},
            # One pair per cell, not four collapsed into one: 60 cells, 60 pairs. If a
            # fieldless record were keyed apart from itself these would diverge, and if
            # four reps were collapsed n_pairs would be 60 with cells at 60 too — so
            # reps_per_cell is the assertion that tells the two apart.
            {"kind": "json_number", "path": "floor_overlap.n_pairs", "op": "==", "value": 60},
            {"kind": "json_number", "path": "floor_overlap.cells", "op": "==", "value": 60},
            {"kind": "json_number", "path": "floor_overlap.reps_per_cell", "op": "==", "value": 1},
            # Nothing is persistent and nothing is dismissed. A single-rep cell must not
            # be counted either way: calling it persistent would manufacture the
            # reproduction, calling it unreproduced would dismiss it on no evidence.
            {"kind": "json_number", "path": "floor_overlap.n_persistent_cells", "op": "==", "value": 0},
            {"kind": "json_number", "path": "floor_overlap.n_unreproduced_cells", "op": "==", "value": 0},
            {
                "kind": "json_number",
                "path": "floor_overlap.n_unreplicated_cells",
                "op": "==",
                "value": 60,
            },
            {"kind": "stdout_contains", "text": "NO REPLICATION"},
            {"kind": "stdout_contains", "text": "reproduced nor dismissed"},
            {"kind": "exit_bits_set", "bits": [32]},
            {"kind": "anomaly_kind_present", "kind_name": "floor_overlap_unconfirmed"},
            # And it is not mistaken for a coverage or provenance fault: the field's
            # absence is a producer vintage, not a missing measurement.
            {"kind": "exit_bits_clear", "bits": [4, 8, 16]},
        ],
    )


def sc_probe_inapplicable():
    """The DYNAMIC_ARCH probe did not run because the DYNAMIC build failed, so
    there was nothing to probe.

    The other half of `probe-unavailable`, and the guard on exit bit 8. Both hosts
    report `openblas_dynamic_probe_status=not_attempted` with a null selection; the
    difference is whether the probe *should* have run. Here it could not have:
    `capture-env.sh` sets `not_attempted` when `GBB_OPENBLAS_DYNAMIC_DIR` is unset
    or absent, and a failed `build_openblas DYNAMIC` leaves that directory absent
    while the manifest and the census both record the failure and its reason.

    An exit bit that fires on a condition already fully explained elsewhere is an
    exit bit people learn to ignore, and bit 8 is load-bearing for the
    highest-leverage arm in the campaign. So the inapplicable case is a note plus a
    section 7 explained absence, and bit 8 stays clear -- while `probe-unavailable`
    keeps proving it still fires when the build was there and the probe was not.

    Faithful to the producers in the part that matters: a failed DYNAMIC build
    takes the forced-coretype arms with it, because they are that binary run under
    a different OPENBLAS_CORETYPE. So the coretype half of the cross does not exist
    on this host at all, and the static TARGET= arms carry the comparison alone --
    which is also why this asserts a verdict: losing a mechanism must not lose the
    experiment."""
    reason = "build failed, see /opt/gbb/openblas-DYNAMIC.buildlog"
    arms = [a for a in _arms(v1_gain=flat(1.22)) if a.target != "DYNAMIC"]
    arms.append(
        Arm(
            "openblas",
            "DYNAMIC",
            "unforced",
            measured=False,
            manifest_built=False,
            manifest_reason=reason,
            census_status="build_failed",
            census_reason=reason,
            # Not decoration, and the second half of what this scenario tests.
            # build-libs.sh's sve_kernels() prints `unknown` when it finds no
            # libopenblas.a to run nm over, and a build that failed leaves none --
            # so `built:false` and `sve_kernels:unknown` always arrive together
            # from the real producer, and every failed OpenBLAS build on an SVE
            # host used to raise a provenance gap telling the reader to install nm.
            sve_kernels="unknown",
        )
    )
    return Scenario(
        name="probe-inapplicable",
        description=(
            "The DYNAMIC build failed, so the DYNAMIC_ARCH probe had nothing to read. The "
            "absent selection is explained by the build failure, not a provenance gap: bit 8 "
            "must stay clear here and fire in probe-unavailable."
        ),
        hosts=[
            _host(
                dynamic_probe_status="not_attempted",
                dynamic_selection=None,
                forcing="not_probed",
                warnings=(
                    "DYNAMIC_ARCH probe not attempted (GBB_OPENBLAS_DYNAMIC_DIR unset or "
                    "absent). openblas_dynamic_selection is null: the standing-order-8 "
                    "generic-ARMV8 check was NOT performed on this host.",
                ),
            )
        ],
        arms=arms,
        expect=[
            {"kind": "anomaly_kind_absent", "kind_name": "dynamic_probe_unavailable"},
            # Same guard, second trigger: an arm that never built has no archive to
            # read, so `sve_kernels:unknown` here is the build failure restated and
            # not an unchecked standing-order-8 trigger. Nothing was mislabelled
            # because nothing ran.
            {"kind": "anomaly_kind_absent", "kind_name": "sve_kernels_unknown"},
            {"kind": "exit_bits_clear", "bits": [2, 4, 8, 16]},
            {"kind": "stdout_contains", "text": "there was no DYNAMIC_ARCH library to probe"},
            {"kind": "stdout_contains", "text": "there was no archive to read"},
            # The reason has to reach the report, not just the classifier: standing
            # order 11 is that a gap carries a reason, and section 7's
            # explained-absences block is where the inapplicable case lives.
            {"kind": "stdout_contains", "text": "openblas-DYNAMIC.buildlog"},
            {"kind": "json_number", "path": "coverage.missing_unexplained", "op": "==", "value": 0},
            # Not an escalation, for the same reason as probe-unavailable.
            {"kind": "anomaly_kind_absent", "kind_name": "escalate"},
            # And the static-target cross still answers the question.
            {"kind": "cross_verdicts_where", "routine": "dgemm", "expect": "V1-set-ahead", "min_rows": 4},
            {"kind": "verdict_code", "one_of": ["V1-SET-AHEAD"]},
        ],
    )


# ---- the matrix stamp -------------------------------------------------------
# Four scenarios, because the stamp has four states and three of them are states a
# REAL directory reaches. The pre-expansion P2 pass and the post-expansion P3
# passes are the whole reason the field exists, and the way they end up in one
# directory is one `aws s3 sync` of a bucket holding both -- so `matrix-mixed` is
# not a hypothetical, it is the operation the campaign will actually perform.
#
# The planted ids are legible rather than digest-shaped on purpose. The analysis
# treats matrix_id as an opaque grouping key and never parses it, so a fixture
# gains nothing from a plausible-looking hex value and loses the ability to say in
# its own failure message which pass was the odd one out.


def sc_matrix_stamped():
    return Scenario(
        name="matrix-stamped",
        description=(
            "One case matrix, stamped on every record including the floor-overlap probe's. "
            "The ordinary state: section 0 says which matrix produced the report, and the "
            "mixed-matrix refusal stays silent."
        ),
        hosts=[_host()],
        arms=_arms(v1_gain=flat(0.88), v2_gain=flat(0.88)),
        floor_probe={"mode": "agree"},
        expect=[
            {"kind": "matrix_ids", "count": 1, "none_unstamped": True},
            {"kind": "exit_bits_clear", "bits": [64]},
            {"kind": "verdict_code", "not_one_of": ["MIXED-MATRIX"]},
            # Section 0 has to name it. A stamp the report does not print is a field
            # that constrains the analysis and tells the reader nothing, and "which
            # matrix is this table over" is the first question a two-pass campaign
            # asks of a report someone else generated.
            {"kind": "stdout_contains", "text": "matrix_id=synth-"},
            {"kind": "stdout_contains", "text": "cases="},
            # And the analysis still ran: the stamp is a guard, not a filter.
            {"kind": "verdict_code", "one_of": ["NULL"]},
            {"kind": "json_string", "path": "floor_overlap.status", "expect": "AGREES"},
        ],
    )


def sc_matrix_mixed():
    return Scenario(
        name="matrix-mixed",
        description=(
            "A pre-expansion pass and a post-expansion pass synced into one directory. "
            "Pooling them is not a comparison, so the analysis refuses and computes nothing: "
            "exit 64 alone."
        ),
        hosts=[
            _host(instance_id="i-000000000000000a", run_id="synth-c7g-pass1"),
            _host(
                instance_id="i-000000000000000b",
                run_id="synth-c7g-pass2",
                matrix_override=("synth-expanded-matrix", 1005),
            ),
        ],
        # A planted +22% headline, which is the point of putting one here: this
        # directory contains a real finding, and the refusal must fire anyway. A
        # version that refused only on datasets with nothing to say would be
        # indistinguishable from one that works.
        arms=_arms(v1_gain=flat(1.22)),
        expect=[
            {"kind": "verdict_code", "one_of": ["MIXED-MATRIX"]},
            {"kind": "exit_bits_set", "bits": [64]},
            # 64 is EXCLUSIVE. Not one of these bits may accompany it, because each
            # one is a claim about a dataset that was never aggregated.
            {"kind": "exit_bits_clear", "bits": [1, 2, 4, 8, 16, 32]},
            {"kind": "matrix_ids", "count": 2, "none_unstamped": True},
            # The stronger half of exclusivity: no table was written. A refusal that
            # still emitted a cross would hand over exactly the pooled-across-two-
            # matrices table it exists to prevent, with a non-zero exit nobody reads.
            # The key names are the report's, checked against cross_rows() and
            # deficit_rows() above -- an absence assertion on a key the report never
            # had is vacuous, and it passes on the mutant that computes everything.
            {"kind": "json_absent", "path": "target_cross"},
            {"kind": "json_absent", "path": "deficit_by_routine"},
            {"kind": "json_absent", "path": "coverage"},
            {"kind": "json_absent", "path": "hosts"},
            # Section 8 must not have seen them either. Two passes on the same
            # instance type with different instance_ids is exactly a replicate pair
            # by the spend policy's rule, so this is the case where a mixed
            # directory would otherwise be silently counted as a reproduction.
            {"kind": "json_absent", "path": "replicates"},
            {"kind": "json_absent", "path": "verdict.line"},
        ],
    )


def sc_matrix_unstamped():
    return Scenario(
        name="matrix-unstamped",
        description=(
            "Every record predates the stamp, as every dataset written before bench.c grew "
            "the field does. One matrix as far as pooling is concerned, so the analysis "
            "proceeds: decompose.py must still be able to read its own history."
        ),
        hosts=[_host()],
        arms=_arms(v1_gain=flat(0.88), v2_gain=flat(0.88)),
        matrix_unstamped=True,
        expect=[
            {"kind": "matrix_ids", "count": 1, "all_unstamped": True},
            {"kind": "exit_bits_clear", "bits": [64]},
            {"kind": "verdict_code", "one_of": ["NULL"]},
            {"kind": "stdout_contains", "text": "matrix_id=unstamped"},
            # The case count is genuinely unknown here, not zero. Section 0 says so
            # in a word rather than printing a number it would be inventing.
            {"kind": "stdout_contains", "text": "cases=unrecorded"},
        ],
    )


def sc_matrix_mixed_unstamped():
    return Scenario(
        name="matrix-mixed-unstamped",
        description=(
            "One stamped pass and one from before the field existed. This is the case that "
            "decides whether `unstamped` means 'matches anything': it must not, because "
            "whether the two swept the same cases is precisely what no record says."
        ),
        hosts=[
            _host(instance_id="i-000000000000000a", run_id="synth-c7g-pass1"),
            _host(
                instance_id="i-000000000000000b",
                run_id="synth-c7g-pass2",
                matrix_unstamped=True,
            ),
        ],
        arms=_arms(v1_gain=flat(1.22)),
        expect=[
            {"kind": "verdict_code", "one_of": ["MIXED-MATRIX"]},
            {"kind": "exit_bits_set", "bits": [64]},
            {"kind": "exit_bits_clear", "bits": [1, 2, 4, 8, 16, 32]},
            {"kind": "matrix_ids", "count": 2, "any_unstamped": True},
            {"kind": "json_absent", "path": "target_cross"},
            {"kind": "json_absent", "path": "verdict.line"},
        ],
    )


def sc_p2_host():
    """A clean single-host P2 dataset, shaped like the run gate P2 will judge.

    This scenario exists for `gates/p2.sh --self-test`, which is the only way that
    gate gets exercised before money is spent on the data it judges. Writing a gate
    against a dataset that does not exist yet and running it for the first time on
    the dataset that cost $150 puts unexercised code between the spend and the
    verdict -- the same objection the spend policy raises against writing new launch
    tooling. It is a legitimate P1 scenario too, so it runs in gates/p1.sh like any
    other and its expectations are its own.

    Faithful to `scripts/run-matrix.sh` on `c8g.metal-48xl` in the dimensions gate
    P2 actually reads, and not in the others:

      - the instance type and a single instance_id, because a P2 pass is ONE host
        and one physical box, and two instance_ids in one directory is the shape
        that P3's replicate rule reads as two passes;
      - all six coretypes an SVE2 host can force (`ARMV8 NEOVERSEN1 ARMV8SVE
        NEOVERSEV1 NEOVERSEV2 NEOVERSEN2`), plus the unforced arm the wheels ship
        and ArmPL and netlib as named references. Six REQUESTED, five measured:
        NEOVERSEN2 lands on the table NEOVERSEV2 is already measuring and is
        declined `alias_duplicate` with zero records. That is not a thinning of the
        fixture, it is the shape of the dataset -- this fixture had both measured,
        which is a shape `run-matrix.sh` cannot emit, so the P2 self-test was
        rehearsing against one more arm than the real pass will contain;
      - the generic ARMV8 arm at 1 thread, which is the arm the re-sequencing
        decision named as mandatory: it is the campaign's most expensive single arm
        and the one the P3 cost extrapolation is anchored on. It is planted SLOW,
        at 0.45x, because that is both the expected result on SVE2 hardware and
        what makes the wall-clock accounting have a genuine slowest arm to find;
      - the thread ladder truncated to 1/64/192. The real ladder is
        `1 8 16 32 64 96 128 192`; the rungs between change the case count and
        nothing gate P2 asserts, and eight rungs times 1005 cases times nine arms
        is a fixture nobody will wait for.

    The cross is a NULL on purpose. The fixture must not plant a hardware claim: a
    gate that went green partly because the fixture agreed with the campaign's
    hypothesis would be calibrated on the answer rather than on the shape."""
    slow = Arm("openblas", "DYNAMIC", "ARMV8", gain=flat(0.45), in_manifest=False)
    return Scenario(
        name="p2-host",
        description=(
            "One clean c8g.metal-48xl pass: nine arms, three thread points, the generic ARMV8 "
            "arm at 1 thread, a confirming floor-overlap band, and a null cross. The shape "
            "gates/p2.sh judges, so that gate can be exercised before the spend."
        ),
        hosts=[
            _host(
                instance_type="c8g.metal-48xl",
                instance_id="i-0c8g000000000001",
                run_id="synth-c8g-p2",
                threads=(1, 64, 192),
                cores=192,
                cpus_online=192,
                cpus_affinity=192,
                has_sve=True,
                has_sve2=True,
                midr="0x413fd4f0",
                midr_part="0xd4f",
                core_name="NEOVERSEV2",
                dynamic_selection="neoversev2",
                sve_vl=16,
                reference_arm=True,
                coretype_aliases={"NEOVERSEN2": "NEOVERSEV2"},
            )
        ],
        arms=[
            *_arms(v1_gain=flat(0.88), v2_gain=flat(0.88)),
            slow,
            Arm("openblas", "DYNAMIC", "NEOVERSEN1", gain=flat(0.71), in_manifest=False),
            Arm("openblas", "DYNAMIC", "ARMV8SVE", gain=flat(0.87), in_manifest=False),
            Arm(
                "openblas",
                "DYNAMIC",
                "NEOVERSEN2",
                in_manifest=False,
                measured=False,
                census_status="alias_duplicate",
                census_reason=ALIAS_DUPLICATE_REASON,
            ),
        ],
        floor_probe={"mode": "agree"},
        expect=[
            # The dataset gate P2 will accept has to be clean on every bit, not
            # just on the coverage one: P2's own gate row says "decompose.py clean
            # bar genuine findings", and a genuine finding is a verdict, not a bit.
            {"kind": "exit_bits_clear", "bits": [1, 2, 4, 8, 16, 32, 64]},
            {"kind": "json_number", "path": "coverage.missing_unexplained", "op": "==", "value": 0},
            {"kind": "json_number", "path": "coverage.partial", "op": "==", "value": 0},
            {"kind": "matrix_ids", "count": 1, "none_unstamped": True},
            {"kind": "json_string", "path": "floor_overlap.status", "expect": "AGREES"},
            {"kind": "verdict_code", "one_of": ["NULL"]},
            # The mandatory arm, asserted here as well as in gates/p2.sh. A fixture
            # that quietly lost it would make the P2 gate's own self-test vacuous,
            # and the self-test is what stands in for the missing dataset.
            {"kind": "stdout_contains", "text": "ARMV8"},
            # The N2 request is declined, and the decline is accounted for. Both
            # halves matter: the bits-clear assertion above is what would fail if
            # `alias_duplicate` were ever read as a success status, and this is what
            # names the reason so a reader of the report is told which arm absorbed
            # the arm that is missing.
            {"kind": "json_number", "path": "coverage.by_status.alias_duplicate", "op": ">", "value": 0},
            {"kind": "stdout_contains", "text": "select the same kernel set"},
        ],
    )


def sc_alias_duplicate():
    """The direction the campaign's own SVE2 hosts take, and its consequence.

    cc3fc1e makes NEOVERSEV2 and NEOVERSEN2 one kernel table and reports it as
    `neoversev2`, so `run-matrix.sh` measures the V2 request and declines the N2 one
    `alias_duplicate`. Three things follow, and this plants all three at once because
    a fixture that got any one of them wrong would still look plausible:

      1. `alias_duplicate` IS an explanation. The arm produced nothing on purpose and
         the census says why, so the cells it did not fill are explained absences and
         not holes -- the opposite of `aliased`, which `aliased-coretype` plants.
      2. The SURVIVING arm is the one labelled NEOVERSEV2, because CORETYPES puts V2
         first -- and that label is what the by-coretype mechanism compares on.
      3. It is not a hole in the CROSS either. V1-vs-V2 is the campaign's central
         comparison and its coretype half is carried entirely by the surviving arm; a
         fixture where the decline silently emptied that half would pass (1) and still
         be useless.

    Distinct from `p2-host`, which carries the same shape among eight other arms: a
    nine-arm gate rehearsal is where an accounting error hides, and this is the
    minimal set where it cannot."""
    arms = _arms(v1_gain=flat(1.22))
    arms.append(
        Arm(
            "openblas",
            "DYNAMIC",
            N2,
            in_manifest=False,
            measured=False,
            census_status="alias_duplicate",
            census_reason=ALIAS_DUPLICATE_REASON,
        )
    )
    return Scenario(
        name="alias-duplicate",
        description=(
            "The NEOVERSEN2 request lands on the table the NEOVERSEV2 arm is already "
            "measuring and is declined `alias_duplicate`. An arm that produced nothing "
            "on purpose, with the reason stated: an explained absence, not a hole, and "
            "the surviving V2 arm still carries the central cross."
        ),
        # A c8g identity, not the default c7g one with sve2 switched on. The status
        # this plants only exists on SVE2 silicon, and #9's objection is exactly that
        # a fixture whose host label disagrees with its arm set is a rigorous test of
        # a machine that does not exist -- so the MIDR, the core name and the
        # DYNAMIC_ARCH selection say Graviton 4 too, and the V1/V2 static targets in
        # `_arms()` are runnable here for the reason `requires()` gives.
        hosts=[
            _host(
                instance_type="c8g.metal-48xl",
                instance_id="i-0c8g000000000002",
                run_id="synth-c8g-alias",
                cores=192,
                cpus_online=192,
                cpus_affinity=192,
                has_sve=True,
                has_sve2=True,
                midr="0x413fd4f0",
                midr_part="0xd4f",
                core_name="NEOVERSEV2",
                dynamic_selection="neoversev2",
                sve_vl=16,
                coretype_aliases={N2: V2},
            )
        ],
        arms=arms,
        expect=[
            {"kind": "json_number", "path": "coverage.missing_unexplained", "op": "==", "value": 0},
            {"kind": "json_number", "path": "coverage.by_status.alias_duplicate", "op": ">", "value": 0},
            {"kind": "exit_bits_clear", "bits": [4]},
            {"kind": "stdout_contains", "text": "select the same kernel set"},
            # (2) and (3) in one structural assertion. `both_mechanisms_agree`
            # FAILS if either mechanism produced no comparable rows, and the
            # coretype mechanism's rows exist only if the surviving V2 coretype arm
            # is there under that label -- so a decline that took the wrong arm, or
            # took the cross with it, cannot pass this. V1 is planted 22% ahead, so
            # the verdict has to be directional as well as present: a cross emptied
            # by the decline would go INCONCLUSIVE and fail the next two.
            {"kind": "both_mechanisms_agree"},
            {"kind": "verdict_code", "one_of": ["V1-SET-AHEAD"]},
            {
                "kind": "cross_verdicts_where",
                "regime": "large",
                "routine": "dgemm",
                "mechanism": "coretype",
                "expect": "V1-set-ahead",
                "min_rows": 2,
            },
        ],
    )


SCENARIOS = {
    f.__name__[3:].replace("_", "-"): f
    for f in (
        sc_null,
        sc_v1_ahead_broad,
        sc_v1_ahead_small,
        sc_v2_ahead,
        sc_noise_only,
        sc_under_dispersion,
        sc_inverted,
        sc_missing_arm_explained,
        sc_missing_arm_unexplained,
        sc_dead_arm,
        sc_verify_fail,
        sc_sve_kernels_absent,
        sc_sve_kernels_absent_neon_host,
        sc_mislabelled,
        sc_no_provenance,
        sc_generic_armv8_on_sve,
        sc_heterogeneous,
        sc_forcing_unavailable,
        sc_peak_fma_retired,
        sc_peak_absent,
        sc_replicate_reproduces,
        sc_replicate_diverges,
        sc_replicate_majority,
        sc_replicate_loss_unexplained,
        sc_replicate_same_box,
        sc_blas_sha_conflict,
        sc_incx_axis,
        sc_transpose_shopping,
        sc_family_swamped,
        sc_reference_arm,
        sc_reference_arm_uncensused,
        sc_sve_kernels_unknown,
        sc_escalation_acked,
        sc_role_mixed,
        sc_partial_arm,
        sc_aliased_coretype,
        sc_alias_duplicate,
        sc_lda_penalty,
        sc_lucky_sample,
        sc_lucky_pass,
        sc_all_arms_failed,
        sc_full_routine_set,
        sc_unverified_verdict,
        sc_reference_library_absent,
        sc_reference_arm_partial,
        sc_manifest_shapes,
        sc_target_readback,
        sc_reference_regime_flip,
        sc_denominator_intersection,
        sc_denominator_thread_point_dark,
        sc_probe_unavailable,
        sc_probe_inapplicable,
        sc_topology_defaulted,
        sc_nodata_group_hole,
        sc_medium_large_localised,
        sc_transpose_lost,
        sc_floor_band_agrees,
        sc_floor_band_biased,
        sc_floor_band_disagrees,
        sc_floor_band_order_confounded,
        sc_floor_band_bias_past_floor,
        sc_floor_band_half,
        sc_floor_band_unreplicated,
        sc_matrix_stamped,
        sc_matrix_mixed,
        sc_matrix_unstamped,
        sc_matrix_mixed_unstamped,
        sc_p2_host,
    )
}


# ---- post-hoc scenario surgery ----------------------------------------------
# Three scenarios need something the dataclasses cannot express declaratively.
# Done here, once, rather than by threading three more flags through Arm.


# ---- checking ----------------------------------------------------------------
# Predicates over decompose.py's --json report, its stdout, and its exit code.
# Every `kind` a scenario may use is implemented here; an unknown kind FAILS
# rather than passing vacuously, which is the failure mode a declarative
# expectation language invites.

OPS = {
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
}


def dig(obj, path):
    cur = obj
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def cross_rows(report, **filters):
    rows = report.get("target_cross") or []
    for key, val in filters.items():
        rows = [r for r in rows if r.get(key) == val]
    return rows


def deficit_rows(report, **filters):
    """Section 1's per-arm rows. The instance-level `no_data` rows share the list
    and carry none of these keys, so they are dropped by the `arm` guard rather
    than by the filters -- an expectation about deficits must not silently match a
    row that has none."""
    rows = [r for r in (report.get("deficit_by_routine") or []) if r.get("arm")]
    for key, val in filters.items():
        rows = [r for r in rows if r.get(key) == val]
    return rows


def _pick(exp, keys):
    return {k: exp[k] for k in keys if k in exp}


def comparable(rows):
    """Rows that carry a verdict about the kernel sets, and the count discarded.

    Two kinds of row do not: NO DATA, and `inconclusive(thin:n<min_sizes)` -- a
    regime with fewer sizes than --min-sizes. Thin rows are real and expected,
    because bench.c's level-1 ladder puts exactly one length (1024) in the medium
    regime, so every level-1 medium cell is thin on faithful data. Asserting
    "every comparable row says parity" must therefore skip them, and must say how
    many it skipped -- silently dropping rows is how an assertion starts passing
    for the wrong reason."""
    keep = [
        r
        for r in rows
        if r.get("median_delta") is not None and not str(r.get("verdict", "")).startswith("inconclusive(thin")
    ]
    return keep, len(rows) - len(keep)


def fixture_bench(root):
    """Every bench record the generator wrote, read back off disk.

    A few claims are about the fixture rather than about the report -- "the
    duplicate really is there" -- and those have to be checked against the files
    decompose.py was actually given, not against the generator's intent."""
    out = []
    for path in sorted((root / "results").glob("bench-*.ndjson")):
        for line in path.read_text().splitlines():
            if line.strip():
                out.append(json.loads(line))
    return out


def check_one(exp, report, stdout, exit_code, root):
    """-> (ok, message). The message says what was expected and what was found;
    a gate that prints only 'FAIL' cannot be acted on.

    The message states the OBSERVATION, not the complaint. Phrasing every message
    as the failure case is the obvious way to write this and it makes a green gate
    unreadable -- `ok anomaly_kind_present: anomaly 'zero_gflops' absent` asserts
    the opposite of what just passed, and a reader skimming for trouble finds it
    on every line. Where a check's finding differs between outcomes, both wordings
    are spelled out."""
    kind = exp.get("kind")

    if kind == "verdict_code":
        code = dig(report, "verdict.code")
        if "one_of" in exp:
            return code in exp["one_of"], f"verdict.code={code!r}, want one of {exp['one_of']}"
        return code not in exp["not_one_of"], f"verdict.code={code!r}, want none of {exp['not_one_of']}"

    if kind in ("exit_bits_set", "exit_bits_clear"):
        want_set = kind == "exit_bits_set"
        bad = [b for b in exp["bits"] if bool(exit_code & b) != want_set]
        return not bad, (
            f"exit={exit_code}; bits {exp['bits']} should be {'set' if want_set else 'clear'}; wrong: {bad}"
        )

    if kind == "stdout_contains":
        hit = exp["text"] in stdout
        return hit, f"{'found' if hit else 'MISSING'} in stdout: {exp['text']!r}"

    if kind == "stdout_absent":
        hit = exp["text"] in stdout
        return not hit, f"{'PRESENT' if hit else 'absent'} from stdout as required: {exp['text']!r}"

    if kind == "json_number":
        got = dig(report, exp["path"])
        if not isinstance(got, (int, float)) or isinstance(got, bool):
            return False, f"{exp['path']} is {got!r}, not a number"
        return OPS[exp["op"]](got, exp["value"]), f"{exp['path']}={got} {exp['op']} {exp['value']}"

    if kind == "json_bool":
        # Separate from json_number because that check rejects bools on purpose:
        # `headline_eligible: false` and `headline_eligible: 0` are the same to `==`
        # and only one of them is the field this asserts.
        got = dig(report, exp["path"])
        if not isinstance(got, bool):
            return False, f"{exp['path']} is {got!r}, not a boolean"
        return got == exp["value"], f"{exp['path']}={got}, want {exp['value']}"

    if kind == "routines_covered":
        # Every routine the fixture emitted must reach the named report section.
        # Cheap, and the only assertion that fails when the analysis drops a
        # routine wholesale rather than getting its number wrong.
        rows = report.get(exp["section"]) or []
        got = {r.get("routine") for r in rows if r.get("routine")}
        missing = [r for r in exp["routines"] if r not in got]
        return not missing, (
            f"{exp['section']} covers {len(got)} routines"
            + (f"; MISSING {missing}" if missing else f" including all {len(exp['routines'])} expected")
        )

    if kind == "coherent_subsets":
        # verdict.coherent_subsets is what blocks the NULL branch, so this is
        # asserted as a set and by default exactly. A length or membership check
        # would pass on a guard that over-fires -- a regime or an instance bucket
        # qualifying as well would widen "not a null" on a shape the scenario
        # never planted, and the fixtures where the guard must stay silent
        # (`null`, `noise-only`) are the only place that shows up.
        subs = dig(report, "verdict.coherent_subsets") or []
        got = sorted({f"{s['axis']}:{s['value']}:{s['direction']}" for s in subs})
        want = sorted(exp["expect"])
        hit = got == want if exp.get("exact", True) else all(w in got for w in want)
        return (
            hit,
            f"coherent subsets {got}, want {'exactly' if exp.get('exact', True) else 'at least'} {want}",
        )

    if kind == "json_string":
        # An exact string field. Distinct from stdout_contains because a status is
        # a closed vocabulary and substring matching on it is wrong in a way that
        # only shows up later: "AGREES" is a substring of "AGREES-WITH-BIAS", so a
        # stdout check for the former would pass on the latter, and the two differ
        # by whether a bias was found. Asserting the field forces the distinction.
        got = dig(report, exp["path"])
        return got == exp["expect"], f"{exp['path']}={got!r}, want {exp['expect']!r}"

    if kind == "json_strings":
        # A list-of-strings field, asserted as an exact sorted set. json_len would
        # pass on a guard that fires on the wrong group, and that is the failure
        # this is guarding: `dark_groups` refuses a directional verdict outright, so
        # a version that over-fires converts every clean scenario into
        # INCONCLUSIVE -- which is exactly what the first draft did, on
        # (axpy, medium) and (dot, medium), whose single level-1 length is a
        # property of the ladder and not a hole in the data.
        got = dig(report, exp["path"])
        if not isinstance(got, list) or any(not isinstance(x, str) for x in got):
            return False, f"{exp['path']} is {got!r}, not a list of strings"
        want = sorted(exp["expect"])
        return sorted(got) == want, f"{exp['path']}={sorted(got)}, want exactly {want}"

    if kind == "json_absent":
        # A field the report must NOT have computed. The mixed-matrix refusal is
        # exclusive -- it returns before anything is aggregated -- and "exit code is
        # 64" does not assert that: a version that refused AND went on to write a
        # cross would set the same bit and hand a reader a table pooled across two
        # case matrices, which is the exact artefact the refusal exists to prevent.
        # Absent and empty are both accepted: `[]` computes nothing either.
        got = dig(report, exp["path"])
        # Truncated: the failure case is a whole report section, and a gate log that
        # scrolls a 400-row cross past the reader has told them less than one line.
        shown = "absent" if got is None else repr(got)[:120]
        return got in (None, [], {}), f"{exp['path']}={shown}"

    if kind == "matrix_ids":
        # The report's own account of which case matrices it saw. Asserted as a
        # structure rather than through json_len paths because the ids are digests:
        # an expectation cannot name one, so it has to name properties of the set.
        got = dig(report, "inputs.matrix_ids")
        if not isinstance(got, dict):
            return False, f"inputs.matrix_ids is {got!r}, not an object"
        ids = sorted(got)
        unst = [i for i in ids if i == "unstamped"]
        if "count" in exp and len(ids) != exp["count"]:
            return False, f"{len(ids)} matrix_ids {ids}, want {exp['count']}"
        if exp.get("all_unstamped") and len(unst) != len(ids):
            return False, f"matrix_ids {ids} are not all unstamped"
        if exp.get("any_unstamped") and not unst:
            return False, f"matrix_ids {ids} include no unstamped group"
        if exp.get("none_unstamped") and unst:
            return False, f"matrix_ids {ids} include an unstamped group"
        # Always, for every scenario that uses this kind: one id must mean one case
        # count. Two counts under one id is a fold that hashed less than it counted,
        # and it would make the id a weaker check than the count sitting next to it.
        multi = {i: got[i].get("cases") for i in ids if len(got[i].get("cases") or []) > 1}
        if multi:
            return False, f"matrix_ids with more than one case count: {multi}"
        counts = {i: got[i].get("cases") for i in ids}
        return True, f"matrix_ids={ids}, cases={counts}"

    if kind == "json_len":
        # For the report's list-valued fields -- `inputs.files.<family>` is the
        # filenames, not a count, so json_number cannot reach it.
        got = dig(report, exp["path"])
        if not isinstance(got, (list, dict, str)):
            return False, f"{exp['path']} is {got!r}, which has no length"
        return OPS[exp["op"]](len(got), exp["value"]), (
            f"len({exp['path']})={len(got)} {exp['op']} {exp['value']}"
        )

    if kind == "cross_verdicts_all":
        rows, thin = comparable(cross_rows(report))
        bad = [r for r in rows if r["verdict"] != exp["expect"]]
        if len(rows) < exp.get("min_rows", 1):
            return False, (
                f"only {len(rows)} comparable cross rows ({thin} NO-DATA/thin), "
                f"want >= {exp.get('min_rows', 1)}"
            )
        return not bad, (
            f"{len(rows) - len(bad)}/{len(rows)} comparable cross rows are {exp['expect']!r} "
            f"({thin} NO-DATA/thin skipped)"
            + ("".join(f"; {r['routine']}/{r['regime']}={r['verdict']}" for r in bad[:5]))
        )

    if kind == "cross_verdicts_where":
        f = {
            k: v
            for k, v in exp.items()
            if k in ("regime", "routine", "mechanism", "incx", "threads", "transa", "transb")
        }
        rows, thin = comparable(cross_rows(report, **f))
        if len(rows) < exp.get("min_rows", 1):
            return False, (
                f"{f}: {len(rows)} comparable rows ({thin} NO-DATA/thin), want >= {exp.get('min_rows', 1)}"
            )
        if "expect" in exp:
            bad = [r for r in rows if r["verdict"] != exp["expect"]]
            return not bad, (
                f"{f}: {len(rows) - len(bad)}/{len(rows)} rows are {exp['expect']!r}"
                + ("; wrong: " + ", ".join(f"{r['verdict']}@{r['lda_pad']}" for r in bad[:5]) if bad else "")
            )
        bad = [r for r in rows if r["verdict"] == exp["not_expect"]]
        return not bad, (f"{f}: {len(bad)}/{len(rows)} rows are {exp['not_expect']!r}, which is disallowed")

    if kind == "run_ids_none_start_with":
        # The structural claim, asserted structurally. A verdict-level assertion is
        # not enough here: the leaked records scale every arm by the same factor, so
        # the cross ratios survive pooling unchanged and the verdict stays NULL --
        # while standing order 1's measured-peak denominator is silently inflated, and
        # every efficiency figure printed against it is silently wrong. Nothing
        # downstream would flag that. What must be true is that the foreign run_id
        # never entered the analysis at all.
        got = [r for r in (dig(report, "inputs.run_ids") or []) if str(r).startswith(exp["prefix"])]
        return not got, (
            f"{len(got)} run_ids start with {exp['prefix']!r}"
            + (f": {got[:3]}" if got else " (none, as required)")
        )

    if kind == "lda_verdict":
        rows = [r for r in (report.get("lda_penalty") or []) if r.get("penalty") is not None]
        if "arm_contains" in exp:
            rows = [r for r in rows if exp["arm_contains"] in str(r.get("arm"))]
        if "lda_pad_in" in exp:
            # The pad axis is filterable because a penalty is a property of the
            # stride, not of the arm: one arm can hurt at pad 8 and be flat at
            # pad 64, and an expectation that could not say which pad it meant
            # would have to be satisfied by every pad or by none.
            pads = {int(p) for p in exp["lda_pad_in"]}
            rows = [r for r in rows if r.get("lda_pad") in pads]
        if len(rows) < exp.get("min_rows", 1):
            return False, f"{len(rows)} lda rows with a penalty, want >= {exp.get('min_rows', 1)}"
        bad = [r for r in rows if r.get("verdict") != exp["expect"]]
        return not bad, (
            f"{len(rows) - len(bad)}/{len(rows)} lda rows are {exp['expect']!r}"
            + (
                "; wrong: "
                + ", ".join(f"{r['arm']}@n={r['m']},pad={r['lda_pad']}={r['verdict']}" for r in bad[:5])
                if bad
                else ""
            )
        )

    if kind == "cross_nodata_where":
        # Deliberately NOT routed through comparable(): the rows being asserted on
        # are precisely the ones comparable() exists to drop. "The arm was excluded
        # from the numbers" is a claim about the rows that have no numbers, so it
        # cannot be made with a predicate that only sees rows that do.
        f = {k: v for k, v in exp.items() if k in ("regime", "routine", "mechanism", "incx", "threads")}
        rows = cross_rows(report, **f)
        nodata = [r for r in rows if r.get("verdict") == "NO DATA"]
        return len(nodata) >= exp.get("min_rows", 1), (
            f"{f}: {len(nodata)}/{len(rows)} rows are NO DATA, want >= {exp.get('min_rows', 1)}"
        )

    if kind == "cross_delta_where":
        f = {k: v for k, v in exp.items() if k in ("regime", "routine", "mechanism", "incx")}
        rows, _thin = comparable(cross_rows(report, **f))
        if not rows:
            return False, f"{f}: no comparable rows"
        bad = [r for r in rows if not OPS[exp["op"]](r["median_delta"], exp["value"])]
        return not bad, (
            f"{f}: {len(rows) - len(bad)}/{len(rows)} deltas satisfy {exp['op']} {exp['value']}"
            + ("; failing: " + ", ".join(f"{r['median_delta']:+.3f}" for r in bad[:5]) if bad else "")
        )

    if kind == "deficit_where":
        # Section 1 is the table the write-up quotes: "OpenBLAS is N% behind ArmPL
        # on this routine". Until C4 nothing asserted a single number in it, so an
        # analysis that got section 2's kernel-set cross right and section 1's
        # reference-relative deficit wrong passed the gate green.
        rows = deficit_rows(report, **_pick(exp, ("instance", "routine", "regime", "incx", "lda_pad")))
        if "arm_contains" in exp:
            rows = [r for r in rows if exp["arm_contains"] in str(r.get("arm"))]
        if exp.get("shipped_only"):
            rows = [r for r in rows if r.get("shipped_arm")]
        rows = [r for r in rows if r.get("median_deficit") is not None]
        if len(rows) < exp.get("min_rows", 1):
            return False, f"{len(rows)} section-1 rows with a deficit, want >= {exp.get('min_rows', 1)}"
        bad = [r for r in rows if not OPS[exp["op"]](r["median_deficit"], exp["value"])]
        return not bad, (
            f"{len(rows) - len(bad)}/{len(rows)} section-1 deficits satisfy {exp['op']} {exp['value']}"
            + (
                "; failing: "
                + ", ".join(f"{r['arm']}/{r['routine']}={r['median_deficit']:+.3f}" for r in bad[:5])
                if bad
                else ""
            )
        )

    if kind == "deficit_shipped":
        # is_shipped() decides which row of section 1 is the one anybody runs, and
        # a mutant returning False for every arm left the gate green: the table
        # still printed, every number in it was still right, and the sentence
        # "this is what the wheels do" had quietly lost its subject. Asserted as a
        # partition -- exactly one shipped arm per condition, and it is the
        # DYNAMIC/unforced one -- because always-True passes a bare "some row is
        # marked" check just as well as always-False fails it.
        rows = [r for r in (report.get("deficit_by_routine") or []) if r.get("arm")]
        if not rows:
            return False, "section 1 has no per-arm rows"
        groups = collections.defaultdict(list)
        for r in rows:
            groups[(r["instance"], r["threads"], r["routine"], r["regime"], r["lda_pad"], r["incx"])].append(
                r
            )
        wrong = []
        for key, rs in sorted(groups.items(), key=str):
            marked = [r for r in rs if r.get("shipped_arm")]
            if len(marked) != 1:
                wrong.append(f"{key}: {len(marked)} arms marked SHIPPED of {len(rs)}")
            elif marked[0]["arm"] != exp.get("arm", "openblas/DYNAMIC/unforced"):
                wrong.append(f"{key}: SHIPPED is {marked[0]['arm']!r}")
        if len(groups) < exp.get("min_rows", 1):
            return False, f"{len(groups)} condition groups in section 1, want >= {exp.get('min_rows', 1)}"
        return not wrong, (
            f"{len(groups) - len(wrong)}/{len(groups)} condition groups mark exactly one SHIPPED arm "
            f"({exp.get('arm', 'openblas/DYNAMIC/unforced')})"
            + ("; wrong: " + "; ".join(wrong[:3]) if wrong else "")
        )

    if kind == "deficit_reference":
        # Section 1's header promises "each named OpenBLAS arm vs a NAMED reference
        # arm", and with two candidate reference libraries on a host that promise
        # acquires a failure mode: a per-row choice. Rows measured against
        # different references are not one table, and nothing about the printed
        # deficits would look wrong. So the invariant asserted is per-cell
        # uniqueness first, membership second -- which of two equally-covering
        # candidates wins is a tie-break, not a finding, and pinning it would make
        # the fixture assert the alphabet.
        rows = deficit_rows(report, **_pick(exp, ("instance", "routine", "regime", "incx", "lda_pad")))
        if len(rows) < exp.get("min_rows", 1):
            return False, f"{len(rows)} section-1 rows, want >= {exp.get('min_rows', 1)}"
        groups = collections.defaultdict(set)
        for r in rows:
            key = (r["instance"], r["threads"], r["routine"], r["regime"], r["lda_pad"], r["incx"])
            groups[key].add(r.get("reference_arm"))
        wrong = []
        for key, refs in sorted(groups.items(), key=str):
            if len(refs) != 1:
                wrong.append(f"{key}: {len(refs)} different reference arms {sorted(map(str, refs))}")
                continue
            ref = next(iter(refs))
            if "arm" in exp and ref != exp["arm"]:
                wrong.append(f"{key}: reference is {ref!r}, want {exp['arm']!r}")
            elif "one_of" in exp and ref not in exp["one_of"]:
                wrong.append(f"{key}: reference is {ref!r}, not in {exp['one_of']}")
            elif not ref:
                wrong.append(f"{key}: reference_arm is {ref!r}")
        want = exp.get("arm") or exp.get("one_of") or "a named non-OpenBLAS arm"
        return not wrong, (
            f"{len(groups) - len(wrong)}/{len(groups)} section-1 cells name one reference arm ({want})"
            + ("; wrong: " + "; ".join(wrong[:3]) if wrong else "")
        )

    if kind == "deficit_reference_invariant":
        # `deficit_reference` above asserts uniqueness per CELL, which is the weaker
        # claim and the one that let the flip through: two cells differing only in
        # regime can each name one reference and name a different one. The reference
        # has to be invariant to every axis the report compares ALONG -- regime (4a),
        # routine (9), thread count (6), pad and transposes (2) -- and per host is
        # the only scope invariant to all of them. So this asserts one reference arm
        # across every section-1 row on the instance, plus the declared scope: a
        # per-group selector that happened to agree everywhere on this fixture would
        # satisfy the first half and fail the second.
        rows = deficit_rows(report, **_pick(exp, ("instance", "routine", "regime", "incx", "lda_pad")))
        if len(rows) < exp.get("min_rows", 1):
            return False, f"{len(rows)} section-1 rows, want >= {exp.get('min_rows', 1)}"
        by_inst = collections.defaultdict(set)
        scopes = collections.defaultdict(set)
        for r in rows:
            by_inst[r["instance"]].add(r.get("reference_arm"))
            scopes[r["instance"]].add(r.get("reference_scope"))
        wrong = []
        for inst, refs in sorted(by_inst.items()):
            if len(refs) != 1:
                # Named with the regimes each reference was used for, because
                # "2 references" and "the small rows used the other one" are the
                # same defect described at two different levels of use.
                per_ref = {
                    ref: sorted({r["regime"] for r in rows if r.get("reference_arm") == ref}) for ref in refs
                }
                wrong.append(f"{inst}: {len(refs)} reference arms across regimes {per_ref}")
                continue
            ref = next(iter(refs))
            if "arm" in exp and ref != exp["arm"]:
                wrong.append(f"{inst}: reference is {ref!r}, want {exp['arm']!r}")
            if scopes[inst] != {"instance"}:
                wrong.append(f"{inst}: reference_scope is {sorted(map(str, scopes[inst]))}, want instance")
        return not wrong, (
            f"{len(by_inst) - len(wrong)}/{len(by_inst)} instances name ONE reference arm "
            f"({exp.get('arm', 'any')}) across all {len(rows)} section-1 rows"
            + ("; wrong: " + "; ".join(wrong[:3]) if wrong else "")
        )

    if kind == "regime_gap_deficit":
        # The section-4a twin of regime_gap_cross, and the reason it exists as its own
        # kind: 4a's group key carries reference_arm and 4b's does not, so 4a is the
        # only one of the two a reference flip can null out. `complete` additionally
        # requires that no profile is missing a regime -- the flip's actual
        # presentation was `MISSING:large`, which is indistinguishable from thin
        # coverage unless the fixture says the coverage is not thin.
        #
        # FILTERED, and it has to be: most 4a profiles are legitimately missing a
        # regime by design, not by defect. Level-1 lengths reach medium and large and
        # never small; the extra lda_pads exist at small and medium and only pad 0 at
        # large; and dgemm carries NT/TT at small and medium and only NN/TN at large.
        # So `complete` is only a meaningful claim on a slice the design fills in every
        # regime, and asserting it globally would assert the design's shape instead of
        # the reference's invariance.
        keys = ("instance", "threads", "routine", "lda_pad", "incx", "transa", "transb")
        want = _pick(exp, keys)
        rows = [
            r
            for r in (dig(report, "regime_profile.deficit") or [])
            if all(r.get(k) == v for k, v in want.items())
        ]
        if exp.get("complete"):
            thin = [r for r in rows if r.get("regimes_missing")]
            if thin:
                return False, (
                    f"{len(thin)}/{len(rows)} section-4a profiles matching {want} are missing a "
                    "regime: "
                    + ", ".join(
                        f"{r['arm']}/{r['routine']} t={r['threads']} vs {r['reference_arm']} "
                        f"missing {r['regimes_missing']}"
                        for r in thin[:3]
                    )
                )
        have = [r for r in rows if r.get("small_minus_large") is not None]
        if len(have) < exp.get("min_rows", 1):
            return False, (
                f"{len(have)} section-4a profiles with a small-large gap of {len(rows)} rows, "
                f"want >= {exp.get('min_rows', 1)}"
            )
        bad = [r for r in have if not OPS[exp["op"]](r["small_minus_large"], exp["value"])]
        return not bad, (
            f"{len(have) - len(bad)}/{len(have)} section-4a small-large gaps satisfy "
            f"{exp['op']} {exp['value']}"
            + (
                "; failing: "
                + ", ".join(f"{r['arm']}/{r['routine']}={r['small_minus_large']:+.3f}" for r in bad[:5])
                if bad
                else ""
            )
        )

    if kind == "scaling_denominator":
        # Standing order 1's denominator, asserted field by field rather than by its
        # value: the value moves with the surface, but WHICH SIZE won and out of which
        # set is the policy. A revert to per-rung maxima changes best_m and nothing
        # else, so best_m is the assertion that has to be there.
        rows = [
            s
            for s in (report.get("scaling") or [])
            if s.get("instance") == exp["instance"] and s.get("threads") == exp["threads"]
        ]
        if len(rows) != 1:
            return False, (
                f"{len(rows)} section-6 rows for {exp['instance']} t={exp['threads']}, want exactly 1"
            )
        s = rows[0]
        checks = [
            ("denom_basis", "basis"),
            ("best_dgemm_m", "best_m"),
            ("denom_common_sizes", "common_sizes"),
        ]
        wrong = [
            f"{field}={s.get(field)!r}, want {exp[key]!r}"
            for field, key in checks
            if key in exp and s.get(field) != exp[key]
        ]
        if "unrestricted_m" in exp and s.get("best_dgemm_unrestricted_m") != exp["unrestricted_m"]:
            wrong.append(
                f"best_dgemm_unrestricted_m={s.get('best_dgemm_unrestricted_m')!r}, "
                f"want {exp['unrestricted_m']!r}"
            )
        if "op" in exp:
            # None and 0.0 are different claims here -- absent means there was no
            # denominator to restrict -- so the None case is named rather than
            # coerced into the comparison.
            cost = s.get("denom_restriction_cost")
            if cost is None:
                wrong.append(f"denom_restriction_cost is None, want {exp['op']} {exp['value']}")
            elif not OPS[exp["op"]](cost, exp["value"]):
                wrong.append(f"denom_restriction_cost={cost:+.4f}, want {exp['op']} {exp['value']}")
        return not wrong, (
            f"{exp['instance']} t={exp['threads']}: basis={s.get('denom_basis')} "
            f"best_dgemm_m={s.get('best_dgemm_m')} common={s.get('denom_common_sizes')} "
            f"cost={s.get('denom_restriction_cost')}" + ("; wrong: " + "; ".join(wrong[:4]) if wrong else "")
        )

    if kind == "deficit_absent":
        # The other half of a NO-DATA claim: nothing was quietly computed anyway.
        # A branch that prints "NO DATA" and still appends a row would satisfy
        # deficit_nodata on its own.
        rows = deficit_rows(report, **_pick(exp, ("instance", "routine", "regime", "incx", "lda_pad")))
        return not rows, (
            f"{_pick(exp, ('instance', 'routine', 'regime', 'incx', 'lda_pad'))}: "
            f"{len(rows)} section-1 deficit rows, want none"
            + ("; found: " + ", ".join(f"{r['arm']}/{r['routine']}" for r in rows[:3]) if rows else "")
        )

    if kind == "deficit_nodata":
        # The two ways section 1 can have nothing to compare against, which are
        # different claims: no reference library on the host at all (one row for
        # the whole instance) versus a reference library that ran but has no
        # kernel for this condition (one row per OpenBLAS arm). Standing order 11:
        # absent and null are different, and so are these two absences.
        rows = report.get("deficit_by_routine") or []
        nd = [r for r in rows if r.get("no_data")]
        if exp.get("scope") == "instance":
            hit = len(nd) >= exp.get("min_rows", 1)
            return (
                hit,
                f"{len(nd)} instance-level NO-DATA rows in section 1, want >= {exp.get('min_rows', 1)}",
            )
        # The per-arm branch prints but does not append, by design: a row with no
        # number must not enter the payload the report averages. So it is asserted
        # on stdout, and the count matters -- one line per OpenBLAS arm.
        #
        # The string moved with the per-host reference choice, and deliberately: the
        # line now names WHICH reference produced nothing, because with the reference
        # fixed per host "absent" alone leaves a reader unable to tell whether the gap
        # is that library's coverage or this campaign's. Matched on the invariant
        # prefix rather than the whole sentence, which carries the arm label.
        marker = "NO DATA — this host's reference arm"
        n = stdout.count(marker)
        return n >= exp.get("min_rows", 1), (
            f"{n} {marker!r} lines in section 1, want >= {exp.get('min_rows', 1)}"
        )

    if kind == "regime_gap_cross":
        rows = [
            r
            for r in (dig(report, "regime_profile.target_cross") or [])
            if r.get("small_minus_large") is not None
        ]
        if len(rows) < exp.get("min_rows", 1):
            return False, f"{len(rows)} profiles with a small-large gap, want >= {exp.get('min_rows', 1)}"
        bad = [r for r in rows if not OPS[exp["op"]](r["small_minus_large"], exp["value"])]
        return not bad, (
            f"{len(rows) - len(bad)}/{len(rows)} small-large gaps satisfy {exp['op']} {exp['value']}"
            + (
                "; failing: " + ", ".join(f"{r['routine']}={r['small_minus_large']:+.3f}" for r in bad[:5])
                if bad
                else ""
            )
        )

    if kind == "both_mechanisms_agree":
        got = {}
        for mech in ("target", "coretype"):
            rows, _thin = comparable(cross_rows(report, mechanism=mech))
            got[mech] = {r["verdict"] for r in rows}
        if not got["target"] or not got["coretype"]:
            return False, f"a mechanism produced no comparable rows: {got}"
        agree = got["target"] == got["coretype"]
        return agree, (f"target and coretype mechanisms {'agree' if agree else 'DISAGREE'}: {got}")

    if kind == "band_at_least":
        rows, _thin = comparable(cross_rows(report))
        if not rows:
            return False, "no comparable cross rows"
        worst = min(r["band"] for r in rows)
        return worst >= exp["value"], f"narrowest band {worst:.3f}, want >= {exp['value']}"

    if kind == "anomaly_kind_present":
        kinds = {a["kind"] for a in (dig(report, "anomalies.items") or [])}
        hit = exp["kind_name"] in kinds
        return hit, (f"anomaly {exp['kind_name']!r} {'raised' if hit else 'NOT RAISED'}; saw {sorted(kinds)}")

    if kind == "anomaly_kind_absent":
        kinds = {a["kind"] for a in (dig(report, "anomalies.items") or [])}
        hit = exp["kind_name"] in kinds
        return not hit, (
            f"anomaly {exp['kind_name']!r} {'RAISED and should not be' if hit else 'not raised'}"
        )

    if kind == "host_state":
        hosts = {h["instance"]: h["state"] for h in (report.get("hosts") or [])}
        got = hosts.get(exp["instance"])
        return got == exp["expect"], f"host {exp['instance']} state={got!r}, want {exp['expect']!r}"

    if kind == "replicate_status":
        reps = {r["instance"]: r["status"] for r in (report.get("replicates") or [])}
        got = reps.get(exp["instance"])
        return got == exp["expect"], f"replicate status for {exp['instance']}={got!r}, want {exp['expect']!r}"

    if kind == "fixture_duplicate_records":
        # Counts (run_id, arm, condition) keys that appear more than once in the
        # generated files. decompose.py's min-within-run rule only guards anything
        # where such a key exists, and a scenario asserting the rule works must be
        # able to fail when the duplicate is gone as well as when the rule is.
        groups = collections.Counter()
        for r in fixture_bench(root):
            arm = f"{r.get('library')}/{r.get('target')}/{r.get('coretype')}"
            if exp.get("arm_contains") and exp["arm_contains"] not in arm:
                continue
            groups[
                (
                    r.get("run_id"),
                    arm,
                    r.get("threads"),
                    r.get("routine"),
                    r.get("m"),
                    r.get("n"),
                    r.get("k"),
                    r.get("lda_pad"),
                    r.get("incx"),
                )
            ] += 1
        dups = sum(1 for n in groups.values() if n > 1)
        want = exp.get("min_count", 1)
        return dups >= want, (
            f"{dups} (run_id, arm, condition) keys carry more than one record"
            + (f" (arm contains {exp['arm_contains']!r})" if exp.get("arm_contains") else "")
            + f", want >= {want}"
        )

    if kind == "cross_rows_have_trans":
        # The structural half of the transpose claim: the cross must carry one row
        # per transpose pair. A verdict assertion alone would pass on an analysis
        # that collapsed the four into one and happened to land on the right side.
        got = sorted(
            {
                f"{r.get('transa')}{r.get('transb')}"
                for r in cross_rows(report)
                if r.get("routine") == exp["routine"]
            }
        )
        want = sorted(exp["values"])
        return got == want, f"{exp['routine']} cross rows carry transposes {got}, want {want}"

    if kind == "cross_rows_have_incx":
        got = sorted({r.get("incx") for r in cross_rows(report) if r.get("routine") == "daxpy"})
        want = sorted(exp["values"])
        return got == want, f"daxpy cross rows carry incx {got}, want {want}"

    return False, f"unknown expectation kind {kind!r} -- check() must implement every kind a scenario uses"


def cmd_check(args):
    root = pathlib.Path(args.dir)
    truth = json.loads((root / "truth.json").read_text())
    report = json.loads(pathlib.Path(args.report).read_text())
    stdout = pathlib.Path(args.stdout).read_text()
    exit_code = int(args.exit_code)

    print(f"  scenario: {truth['scenario']}")
    print(f"    {truth['description']}")
    npass = nfail = 0
    for exp in truth["expect"]:
        ok, msg = check_one(exp, report, stdout, exit_code, root)
        if ok:
            npass += 1
            print(f"    ok   {exp['kind']}: {msg}")
        else:
            nfail += 1
            print(f"    FAIL {exp['kind']}: {msg}")
    print(f"    {npass} passed, {nfail} failed")
    return 1 if nfail else 0


def cmd_generate(args):
    if args.scenario not in SCENARIOS:
        print(f"unknown scenario {args.scenario!r}; try `list`", file=sys.stderr)
        return 2
    sc = SCENARIOS[args.scenario]()
    root = pathlib.Path(args.dir)
    res = write_scenario(sc, root)
    print(f"{sc.name}: {len(list(res.glob('*')))} files under {res}")
    return 0


def cmd_list(_args):
    for name in SCENARIOS:
        sc = SCENARIOS[name]()
        print(f"{name}\n    {sc.description}")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list").set_defaults(fn=cmd_list)
    g = sub.add_parser("generate")
    g.add_argument("scenario")
    g.add_argument("dir")
    g.set_defaults(fn=cmd_generate)
    c = sub.add_parser("check")
    c.add_argument("dir")
    c.add_argument("report")
    c.add_argument("stdout")
    c.add_argument("exit_code")
    c.set_defaults(fn=cmd_check)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
