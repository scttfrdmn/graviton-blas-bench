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


# bench.c's verification outcome per routine, and the note it carries. dgemm is
# the only routine with a real corner check; the rest emit verified=null, which
# is why section 5 prints verification coverage per routine.
VERIFY = {
    "dgemm": (True, ""),
    "sgemm": (None, "corner_check_absent_fp32"),
    "dtrsm": (None, "corner_check_absent"),
    "dtrmm": (None, "corner_check_absent"),
    "dsyrk": (None, "corner_check_absent"),
    "dsymm": (None, "corner_check_absent"),
    "dgemv": (None, "corner_check_absent"),
}

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
    # Routines this arm produced no record for at all. The reference arm's version
    # of omit_sizes, and the only way to reach section 1's per-arm "NO DATA --
    # reference arm absent" branch: a reference library that ran but has no kernel
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
    peak_factor: float = 1.06  # peak_fma / best large dgemm
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
            for routine, m, n, k, pad, incx, ta, tb in conds:
                if m in arm.omit_sizes or routine in arm.omit_routines:
                    continue
                trans = f"{ta}{tb}" if ta is not None else None
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
                        verified, note = VERIFY.get(routine, (None, f"incx={incx}"))
                        if routine in ("daxpy", "ddot"):
                            note = f"incx={incx}"
                        if routine in arm.verified_false_routines:
                            verified, note = False, "corner_check_failed"
                    recs.append(
                        {
                            "run_id": host.run_id,
                            "host": f"ip-10-0-0-{abs(hash(host.instance_id)) % 200}",
                            "instance": host.instance_type,
                            "library": arm.library,
                            "target": arm.target,
                            "build": "synthetic",
                            "blas_sha": sha,
                            "coretype": arm.coretype,
                            "thread_backend": arm.thread_backend,
                            "pin_policy": "taskset -c 0" if threads == 1 else f"numactl -C 0-{threads - 1}",
                            "arch_selected": host.dynamic_selection,
                            "role": "campaign",
                            "threads": threads,
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


def honest_records(bench):
    """One record per (run_id, arm, condition) -- the slower one, where an Arm
    planted a lucky duplicate.

    roofline.c measures peak_fma with its own FMA chain, so a re-run appended into
    bench-*.ndjson cannot move it. Deriving peak_fma from the duplicate would fire
    the standing-order-1 headroom flag on a fixture that is not about headroom, and
    a scenario whose stated claim is 'the cross stays a null' must not also be
    quietly asserting an anomaly it never mentions."""
    best = {}
    for r in bench:
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
    generated, times host.peak_factor -- so a scenario controls whether standing
    order 1's headroom flag fires by setting one number, and the fixture cannot
    drift into firing it by accident when the surface changes."""
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
    request, EXCEPT where OpenBLAS resolves the request to another name -- which is
    the campaign's own case: standing order 8 records NEOVERSEV2 -> NEOVERSEN2 on a
    recognised V2/V3 part, so the V2 arm on a real host is an alias and is censused
    as one. That is the expected path, not an edge case."""
    if arm.library != "openblas":
        return "n/a"
    if arm.coretype == "unforced":
        return host.dynamic_selection
    return host.coretype_aliases.get(arm.coretype, arm.coretype).lower()


def census_records(sc: Scenario, host: HostSpec, bench):
    """scripts/run-matrix.sh census(). Note the coretype spelling: run_arm is
    called with an empty $ct for the unforced arm, so the real census writes ""
    where bench.c writes "unforced". Reproduced deliberately -- that mismatch is
    one of the two bugs this file found."""
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
                "pin_policy": "taskset -c 0" if threads == 1 else f"numactl -C 0-{threads - 1}",
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
                for r in bench
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
                    "pin_policy": "taskset -c 0" if threads == 1 else f"numactl -C 0-{threads - 1}",
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
        _w(res / f"bench-{host.run_id}.ndjson", bench)
        if host.foreign_role is not None:
            # An instrument-check host's records sitting in a campaign directory,
            # which is what one `aws s3 sync` of a bucket holding both prefixes
            # produces. Faster than the campaign host on purpose: if the analysis
            # pools them, it pools them into the measured-peak denominator too and
            # standing order 1's headroom check goes quiet.
            leaked = []
            for r in bench:
                q = dict(r)
                q["role"] = host.foreign_role
                q["run_id"] = f"instr-{host.foreign_role}-castor"
                q["gflops"] = r["gflops"] * host.foreign_role_gain
                q["gflops_p50"] = r["gflops_p50"] * host.foreign_role_gain
                leaked.append(q)
            _w(res / f"bench-instr-{host.foreign_role}-castor.ndjson", leaked)
        if host.roofline_present:
            _w(res / f"roofline-{host.run_id}.ndjson", roofline_records(host, bench))
        _w(res / f"manifest-{host.run_id}.ndjson", manifest_records(sc, host))
        _w(res / f"census-{host.run_id}.ndjson", census_records(sc, host, bench))
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


def sc_headroom():
    return Scenario(
        name="headroom",
        description=(
            "peak_fma is 1.5x the best GEMM any arm reached. Standing order 1 makes that "
            "gap the headline, ahead of any kernel-set comparison."
        ),
        hosts=[_host(peak_factor=1.5)],
        arms=_arms(),
        expect=[
            {"kind": "anomaly_kind_present", "kind_name": "headroom"},
            {"kind": "stdout_contains", "text": "that gap is the headline"},
        ],
    )


def sc_peak_absent():
    return Scenario(
        name="peak-absent",
        description=(
            "No peak_fma record at all. Absent must read as 'the cross-check was NOT "
            "performed', never as 'it passed'."
        ),
        hosts=[_host(roofline_present=False)],
        arms=_arms(),
        expect=[
            {"kind": "anomaly_kind_present", "kind_name": "peak_fma_absent"},
            {"kind": "stdout_contains", "text": "cross-check was NOT performed"},
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
    feeds the measured-peak denominator, a faster instrument host would defeat
    standing order 1's headroom check at the same time.

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
    """The expected path on the campaign's own hosts, not an edge case.

    Standing order 8 records that OpenBLAS resolves NEOVERSEV2 -> NEOVERSEN2 on a
    recognised V2/V3 part, so on every real c8g/c9g run the V2 coretype arm is
    censused `aliased` and its records carry a coretype_effective that differs from
    the request. `aliased` is written BEFORE the arm runs and the arm then runs, so
    it can never explain a missing cell -- but it was absent from CENSUS_SUCCESS,
    which would have let a genuine hole in the campaign's central arm be accounted
    for by a line that says "running it"."""
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
            "The V2 arm is censused `aliased` -- the real campaign's own case -- and is "
            "also missing every large size. `aliased` says the arm is being run, so it "
            "must not be accepted as the reason a cell is absent."
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
            # 4. three of the four affected routines have no correctness check in
            # bench.c, so this verdict rests on verified=null records and must say
            # so. A TRSM win that nothing verified is the exact failure mode the
            # tri-state `verified` was introduced for.
            {"kind": "stdout_contains", "text": "VERDICT-CAVEAT:"},
            {"kind": "stdout_contains", "text": "verified=null"},
            {"kind": "exit_bits_clear", "bits": [2, 4, 8, 16]},
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
        sc_headroom,
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
        sc_lda_penalty,
        sc_lucky_sample,
        sc_lucky_pass,
        sc_all_arms_failed,
        sc_full_routine_set,
        sc_reference_library_absent,
        sc_reference_arm_partial,
        sc_manifest_shapes,
        sc_probe_unavailable,
        sc_probe_inapplicable,
        sc_topology_defaulted,
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
        # while the measured-peak denominator is silently inflated, which is how
        # standing order 1's headroom check goes quiet. What must be true is that
        # the foreign run_id never entered the analysis at all.
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
        n = stdout.count("NO DATA — reference arm absent")
        return n >= exp.get("min_rows", 1), (
            f"{n} 'NO DATA — reference arm absent' lines in section 1, want >= {exp.get('min_rows', 1)}"
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
