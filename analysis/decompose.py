#!/usr/bin/env python3
"""graviton-blas-bench decompose -- turn the raw sweep into a decision.

The question is not "is OpenBLAS slower than ArmPL on Graviton". The question is
WHERE the deficit lives, so the answer is either "here is a tractable thing worth
fixing" or "there is nothing here, publish the survey and move on". Both are
acceptable outcomes, and this file exists to make the second one as visible as
the first.

It was rewritten because it could not do that. An adversarial review reproduced
the previous version printing "V1 kernels win" on data where V2 truly won 4 of 5
sizes and the mean, printing `parity` for two arms that had produced 0.00
GFLOP/s, deciding rows at a hardcoded 2% while its header announced 5%, and
returning 0 on every input including one with zero comparisons. Every guard here
is aimed at a specific one of those failures and says which.

Sections. 2 (target cross) and 5 (anomalies) keep their numbers: CLAUDE.md,
KICKOFF.md, bootstrap-github.sh and the P2/P3/P4 gate text all refer to them by
number. Everything else was renumbered around them.

  0. hosts            per-host provenance, and whether this host's numbers are
                      admissible at all. Printed first because a number without
                      provenance is not admissible (standing order 5).
  1. deficit-by-routine  each named OpenBLAS arm against a named reference arm,
                      size by size. Signed: negative means OpenBLAS is ahead.
  2. target-cross     same silicon, different OpenBLAS kernel set. This is the
                      experiment: it separates "V2/V3 silicon is bad at SVE"
                      from "the N2 kernel set is worse than the V1 one".
  3. lda-penalty      tight vs padded leading dimension, which isolates
                      packing-kernel quality from the inner kernel.
  4. regime-profile   small / medium / large. The N2 kernel set defines no
                      GEMM_SMALL_* entries at all, so if a deficit exists it
                      should be visible in small and absent in large. The
                      small-minus-large gap is reported as an explicit number.
  5. anomalies        everything that should stop a conclusion.
  6. scaling          GFLOP/s vs threads against the measured all-core peak.
  7. coverage census  every expected cell classified. MISSING-UNEXPLAINED is the
                      one that matters: a hole nothing accounts for.
  8. replicates       P3 runs each host family twice on different physical boxes.
                      The passes are COMPARED, never pooled: the whole point of
                      the second pass is whether the first one reproduces, and a
                      median across the two would convert the campaign's strongest
                      evidence into slightly tighter error bars.

  VERDICT             one machine-greppable line computed from the data.

EXIT CODES -- load-bearing, because gates/p1.sh has to be able to assert on
something. 2, 4, 8 and 16 are bit flags and are OR-ed together; 1 is returned
alone.

  0  clean
  1  nothing usable was loaded (no bench records at all)
  2  poisoned records or inadmissible hosts: a failed verification, a 0.00
     GFLOP/s record, a non-performance governor, SMT, heterogeneous cores, a
     cgroup CPU quota, OPENBLAS_CORETYPE forcing proved unavailable, SVE
     detection having failed on a host that has SVE, an OpenBLAS build with no SVE
     kernel symbols in it on a host that has SVE, an arm that refused to
     measure because its coretype label and its loaded library disagreed, a host
     whose provenance refusal was overridden with GBB_ESCALATION_ACK, or a results
     directory holding more than one role
  4  unexplained coverage hole: an expected (arm, condition) cell is absent --
     wholly, or short some of its sizes -- and neither the build manifest, the run
     census, nor an exclusion this file made accounts for it
  8  provenance incomplete: a bench record whose instance has no env-*.json,
     conflicting blas_sha for the same library/target, or an SVE host whose build
     could not be checked for SVE kernel symbols at all (`sve_kernels:unknown`,
     which build-libs.sh defines as "could not look", not as "fine")
 16  the headline does not reproduce: two independent passes on the same instance
     type and different physical boxes reached different verdicts. Not the same
     failure as noise -- the parity band already absorbs noise -- so it gets its
     own bit rather than widening the meaning of 2

Usage:
    python3 decompose.py results/ [--min-effect 0.05] [--json out.json]
"""

import argparse
import json
import pathlib
import statistics
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field

# ---- named thresholds -----------------------------------------------------
# Every threshold that decides anything is here, with its default and the reason
# for that default. The previous version decided rows at an inline `* 1.02` while
# printing 5% in its header; "do not tune the analysis until it finds something"
# is unenforceable if the numbers that decide are anonymous.

# Floor of the parity band. 5% because that is roughly what run-to-run variation
# on a shared-tenancy host produces, and because the campaign's decision guide
# and README are written around 5%. It is a FLOOR: the band widens to the
# dispersion actually observed in the records being compared.
DEFAULT_MIN_EFFECT = 0.05

# (t_p50 - t_min)/t_min above which a record is called out as a probable noisy
# neighbour. 25% on a no-turbo, no-SMT host is not thermal or frequency drift.
DEFAULT_NOISY_SPREAD = 0.25

# peak_fma has to exceed the best observed GEMM by this factor before standing
# order 1's "every arm is leaving headroom" flag fires. 1.15 leaves room for the
# ordinary gap between an FMA-chain microbenchmark and a real blocked GEMM.
DEFAULT_HEADROOM_FACTOR = 1.15

# Sizes that must be comparable at identical (m,n,k,lda_pad) before a regime may
# carry a directional verdict. 3 because MEDIUM and LARGE hold 5 sizes each and
# SMALL holds 10; a verdict from one or two sizes is a size-specific anecdote,
# which is exactly how the old max()-over-the-regime bug produced its inversion.
DEFAULT_MIN_SIZES = 3

# Fraction of the compared sizes that must agree in sign for a directional
# regime verdict. 0.6 because a median above the band with the sizes split near
# 50/50 is not a property of the kernel set.
DEFAULT_WIN_FRACTION = 0.60

# Fraction of comparable cells that must agree for a campaign-level verdict.
DEFAULT_VERDICT_MAJORITY = 0.60

# If more than this fraction of the cells in the target cross are not comparable
# (missing arm, thin, unequal N, inadmissible host), the campaign verdict is
# INCONCLUSIVE rather than directional: with a third of the design absent the
# sign of the aggregate is decided by which cells happened to survive.
DEFAULT_MAX_NODATA_FRACTION = 0.34

# The two kernel sets under test, as they appear in TARGET= and in
# OPENBLAS_CORETYPE. Parameters, not literals, because 0.3.32 maps V3 onto the
# V2 target and the campaign may need to name a different pair.
DEFAULT_V1_SET = "NEOVERSEV1"
DEFAULT_V2_SET = "NEOVERSEV2"

# Cap on how many individual items any one list in the report prints.
DEFAULT_MAX_LISTED = 20

# Token capture-env.sh emits for a MIDR part that is not in OpenBLAS's dispatch
# switch, and the substring of the warning that says the lscpu-derived topology
# fields were defaulted rather than measured. Both are contracts with that
# script; capture-env.sh says so at the emitting site. Do not reword either.
UNRECOGNISED = "UNRECOGNISED"
LSCPU_DEFAULTED = "lscpu produced no topology"

# Census statuses that mean the arm ran, and therefore explain NOTHING about a
# cell it failed to produce. run-matrix.sh emits nine:
#
#   measured             the arm ran                       -- not an explanation
#   aliased              the arm is ABOUT TO run under a coretype the library
#                        reports by another name; written before the run and
#                        followed by the run itself -- not an explanation
#   skipped              declined by policy (netlib control) -- explanation
#   build_failed         explanation
#   unrunnable           explanation (ISA absent, or forcing not honoured)
#   runtime_failed       explanation
#   mislabelled          explanation
#   alias_duplicate      explanation (the kernel set is already being measured)
#   forced_invalid_host  explanation, host-level not arm-level
#
# `mislabelled` is the one that must never be read as a flake: bench.c's
# in-process openblas_get_corename() disagreed with the probe the runner ran in a
# separate process, so the arm refused to measure rather than write records under
# a label belonging to a different library. A retry reproduces it.
#
# `aliased` is the one that matters most in practice, because it is the EXPECTED
# path on the campaign's own hosts: standing order 8 records that OpenBLAS
# resolves NEOVERSEV2 -> NEOVERSEN2, so the V2 coretype arm on a recognised V2/V3
# part is censused `aliased` on every real run. Reading it as an explanation would
# have let a genuine hole in the campaign's central arm be accounted for by a line
# that says "running it".
CENSUS_SUCCESS = frozenset({"measured", "ok", "aliased"})

# Libraries that appear in the manifest or the census but are not performance
# arms and write no bench records. Counting one of these among the expected BENCH
# arms makes every condition on the host a MISSING-UNEXPLAINED cell (36 on a
# one-host dataset: one per threads x routine x regime x lda_pad x incx group) and
# sets exit bit 4 on a perfectly clean dataset -- which would have made every real
# P2 run look broken while gate P2 demands zero MISSING-UNEXPLAINED, i.e. the one
# flag saying "you have a coverage hole" would have been the one flag guaranteed
# to be lying.
#
#   roofline   run-matrix.sh censuses the peak_fma cross-check as an arm, because
#              an absent denominator is a gap needing a stated reason like any
#              other. It writes roofline-*.ndjson, not bench-*.ndjson.
#   reference  netlib libblas. build-libs.sh builds it as a correctness control
#              and records it built:true/runnable:true; run-matrix.sh skips it
#              explicitly -- "correctness control, never timed".
#   host       not a library at all. run-matrix.sh uses the census arm slots for a
#              host-level `forced_invalid_host` record when GBB_FORCE_INVALID_HOST
#              overrides a refusal, writing library=host/target=host. Same failure
#              mode as the other two, reachable whenever that override is used.
#
# Both were found by the P1 fixtures, roofline first and reference only after an
# adversarial pass noticed the fix had been applied to one library and not the
# class. Prefer widening this set to special-casing at a use site: the question
# "does this library produce bench records" has one answer, and it belongs here.
NON_BENCH_LIBRARIES = frozenset({"roofline", "reference", "host"})

# Every record carries the role run-matrix.sh derived for the host it came from.
# Instrument checks (castor/pollux: DGX Spark GB10, Cortex-X925 + A725, SVE2 at
# VL=128) are real SVE2 silicon and exercise the whole pipeline, but they are not
# Neoverse and not Graviton, and CLAUDE.md requires they be quarantined "by
# construction, not by discipline". The producers separate them structurally --
# distinct output directory, run_id prefix and S3 prefix -- and both bench.c and
# roofline.c tell the reader "the analysis excludes anything that does not say
# campaign". Until now the analysis did no such thing: it never read the field.
# One `aws s3 sync` of a bucket holding both prefixes, or one tarball unpacked
# into the wrong directory, and GB10 numbers pool into the Graviton dataset in
# silence -- and because the pool feeds the measured-peak denominator, a faster
# instrument host also silently defeats standing order 1's headroom check.
# A record with no role at all predates the field and is taken at its word.
DEFAULT_ROLE = "campaign"

REGIMES = ("small", "medium", "large")

# The three producers spell "no coretype was forced" three different ways, and an
# arm is identified by (library, target, coretype) everywhere in this file:
#
#   build-libs.sh   manifest arm record   "coretype":null
#   run-matrix.sh   census arm_outcome    "coretype":""      (run_arm's $ct is empty)
#   bench.c         bench record          "coretype":"unforced"  (GBB_CORETYPE default)
#
# Untranslated, those are three different arms. The manifest's and the census's
# versions then have no cells, so the coverage census reported the shipped arm as
# MISSING-UNEXPLAINED and set exit bit 4 on a perfectly clean dataset, while
# cross_pairs() emitted a phantom second "by target" pair whose every cell was NO
# DATA. Canonicalise on read, at the one place every record passes through.
CANON_UNFORCED = "unforced"
_UNFORCED_SPELLINGS = frozenset({"", "null", "none", "nil", "unforced"})


def canon_coretype(v):
    if v is None:
        return CANON_UNFORCED
    s = str(v).strip()
    return CANON_UNFORCED if s.lower() in _UNFORCED_SPELLINGS else s


# ---- regime boundaries ----------------------------------------------------
# Deliberately coarse. The point is to separate "fits in cache and the fixed
# overheads dominate" from "streaming from DRAM", not to draw a precise line.
def regime(n: int) -> str:
    if n <= 256:
        return "small"
    if n <= 1536:
        return "medium"
    return "large"


def sortable(v):
    """Sort key that survives None and mixed int/str columns."""
    if v is None:
        return (2, 0.0, "")
    if isinstance(v, bool):
        return (1, 0.0, str(v))
    if isinstance(v, (int, float)):
        return (0, float(v), "")
    return (1, 0.0, str(v))


def skey(t):
    return tuple(sortable(x) for x in t)


def rel(a, b):
    """Signed relative difference of a against b, or None if not computable.

    The old pct() guarded with `if not a or not b`, so a legitimate 0.00
    GFLOP/s read as "missing" and two dead arms printed `parity` -- the exact
    word the decision guide maps to "publish the negative result"."""
    if a is None or b is None or b == 0:
        return None
    return (a - b) / b


def fmt_pct(x):
    return "   n/a " if x is None else f"{100 * x:+6.1f}%"


def fmt_val(x):
    return "     n/a" if x is None else f"{x:8.2f}"


# ---- input ----------------------------------------------------------------


@dataclass
class Inputs:
    bench: list = field(default_factory=list)
    arm_failures: list = field(default_factory=list)
    roof: list = field(default_factory=list)
    envs: list = field(default_factory=list)
    manifest_arms: list = field(default_factory=list)
    toolchains: list = field(default_factory=list)
    outcomes: list = field(default_factory=list)
    files: dict = field(default_factory=lambda: defaultdict(list))
    bad_lines: int = 0
    bad_env_files: list = field(default_factory=list)
    missing_families: list = field(default_factory=list)
    escalation_acks: list = field(default_factory=list)
    foreign_roles: dict = field(default_factory=lambda: defaultdict(int))


def load(results_dir: pathlib.Path, role: str = DEFAULT_ROLE) -> Inputs:
    """Read every file family, tolerating absence and partial writes.

    A host terminated mid-write used to take the whole analysis down: the bench
    path warned and continued on a bad line, but env-*.json was parsed with a
    bare json.loads and an uncaught JSONDecodeError produced zero output."""
    d = Inputs()
    if not results_dir.is_dir():
        print(f"gbb: {results_dir} is not a directory", file=sys.stderr)
        return d

    for p in sorted(results_dir.glob("*.ndjson")):
        family = (
            "bench"
            if p.name.startswith("bench-")
            else "roofline"
            if p.name.startswith("roofline-")
            else "manifest"
            if p.name.startswith("manifest-")
            else "census"
            if p.name.startswith("census-")
            else "unnamed"
        )
        d.files[family].append(p.name)
        for line in p.read_text(errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                d.bad_lines += 1
                print(f"warn: unparseable line in {p.name}", file=sys.stderr)
                continue
            if not isinstance(r, dict):
                d.bad_lines += 1
                continue
            # Dispatch on record shape, not on filename: results collected from
            # several hosts get concatenated by hand more often than not.
            # The role gate comes before the shape dispatch, so a foreign record
            # cannot reach any list. Counted rather than merely dropped: a
            # directory holding two roles means someone's collection path is
            # broken, and silently analysing the correct subset would leave that
            # broken path in service.
            got_role = r.get("role", role)
            if got_role != role:
                d.foreign_roles[got_role] += 1
                continue
            rec = r.get("record")
            if rec == "arm":
                r["coretype"] = canon_coretype(r.get("coretype"))
                d.manifest_arms.append(r)
            elif rec == "toolchain":
                d.toolchains.append(r)
            elif rec == "arm_outcome":
                r["coretype"] = canon_coretype(r.get("coretype"))
                d.outcomes.append(r)
            elif "metric" in r:
                d.roof.append(r)
            elif "routine" in r:
                r["coretype"] = canon_coretype(r.get("coretype"))
                d.bench.append(r)
            elif rec == "escalation_ack":
                # GBB_ESCALATION_ACK let a sweep proceed on a host that
                # capture-env.sh had refused (standing order 8). Before this
                # branch the record matched no shape and was counted as a corrupt
                # line -- so the one artifact documenting that the campaign's
                # loudest interlock had been overridden was filed as data
                # corruption, at a severity that sets no exit bit. The underlying
                # condition is still detected independently from env-*.json, which
                # is what sets the bit; this makes the override itself legible
                # rather than making it invisible.
                d.escalation_acks.append(r)
            elif r.get("failed"):
                d.arm_failures.append(r)
            else:
                d.bad_lines += 1

    for p in sorted(results_dir.glob("env-*.json")):
        d.files["env"].append(p.name)
        try:
            e = json.loads(p.read_text(errors="replace"))
        except json.JSONDecodeError as exc:
            d.bad_env_files.append((p.name, f"unparseable JSON: {exc}"))
            print(f"warn: unparseable {p.name}: {exc}", file=sys.stderr)
            continue
        if not isinstance(e, dict):
            d.bad_env_files.append((p.name, "top-level JSON is not an object"))
            continue
        d.envs.append(e)

    for fam in ("bench", "roofline", "env", "manifest", "census"):
        if not d.files.get(fam):
            d.missing_families.append(fam)
    return d


# ---- per-host admissibility ------------------------------------------------


@dataclass
class Host:
    instance: str
    present: bool = False
    envs: list = field(default_factory=list)
    invalid: list = field(default_factory=list)  # timings from here are not comparable
    escalate: list = field(default_factory=list)  # standing order 8
    notes: list = field(default_factory=list)
    # Distinct from both `escalate` and `notes`: the check that standing order 8
    # relies on could not be performed. Not an escalation, because absent evidence
    # is not evidence of absence -- but not a note either, because it must set the
    # provenance bit rather than scroll past in a list nobody greps.
    provenance_gaps: list = field(default_factory=list)
    cpus_affinity: int | None = None
    forcing: str = "not_probed"

    @property
    def admissible(self) -> bool:
        return self.present and not self.invalid and not self.escalate


def _int_or_none(v):
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def build_hosts(inp: Inputs) -> dict:
    """One Host per instance TYPE -- that is what `instance` is in the records.
    Two hosts of the same type merge, and the merge is pessimistic: any
    invalidating fact on either invalidates the type for this dataset."""
    hosts: dict[str, Host] = {}
    for e in inp.envs:
        inst = e.get("instance_type") or "unknown"
        h = hosts.setdefault(inst, Host(instance=inst))
        h.present = True
        h.envs.append(e)
        who = e.get("host") or e.get("instance_id") or inst

        # warnings[] is echoed verbatim and never summarised. It is the whole
        # reason the field exists: a backgrounded sweep sends stderr to
        # scrollback nobody reads and then terminates the instance, so a warning
        # that lives only on a dead console is not provenance.
        for w in e.get("warnings") or []:
            h.notes.append(f"{who}: capture-env warning: {w}")
        lscpu_defaulted = any(LSCPU_DEFAULTED in (w or "") for w in e.get("warnings") or [])

        # Host identity. Branch on core_clusters, never on the scalar midr
        # fields: those are cpu0's only (midr_scalar_source says so), and a
        # heterogeneous host whose cpu0 happens to be recognised used to pass
        # this section in silence.
        clusters = e.get("core_clusters") or []
        uniform = e.get("midr_uniform")
        if uniform is False:
            desc = ", ".join(
                f"{c.get('core_name')} ({c.get('midr_part')}) on cpus {c.get('cpus')}"
                f" x{c.get('cpu_count')}"
                + (f" @ {c.get('cpuinfo_max_freq_khz')}kHz" if c.get("cpuinfo_max_freq_khz") else "")
                for c in clusters
            )
            h.invalid.append(
                f"{who}: heterogeneous cores -- {desc or 'core_clusters empty'}. Every host-level "
                f"number from this box is a blend of microarchitectures."
            )
        elif uniform is None:
            h.invalid.append(
                f"{who}: midr_uniform is null -- no MIDR was readable, so core identity is "
                f"UNVERIFIED and core_name is not a measurement on this host."
            )
        for c in clusters:
            if c.get("core_name") == UNRECOGNISED:
                h.notes.append(
                    f"{who}: MIDR part {c.get('midr_part')} on cpus {c.get('cpus')} is not in "
                    f"OpenBLAS dynamic_arm64.c's switch. With SVE the fallback is ARMV8SVE and its "
                    f"94 kernels, which is more than a named NEOVERSEV2 gets -- read this against "
                    f"the ARMV8SVE arm, it is not by itself an alarm."
                )

        # DYNAMIC_ARCH probe. "the check was not performed" must read
        # differently from "the check ran and found generic ARMV8": the old code
        # matched substrings against a field that was the string "unknown" when
        # the probe broke, so it fell silent exactly when detection failed.
        status = e.get("openblas_dynamic_probe_status")
        sel = e.get("openblas_dynamic_selection")
        if status != "ok":
            h.notes.append(
                f"{who}: openblas_dynamic_probe_status={status!r}; the standing-order-8 "
                f"generic-ARMV8 check was NOT performed on this host."
            )
        else:
            lib = e.get("openblas_dynamic_lib_resolved")
            probe_dir = e.get("openblas_dynamic_probe_dir")
            if not lib:
                h.notes.append(
                    f"{who}: probe ran but ldd could not say which libopenblas answered; "
                    f"openblas_dynamic_selection={sel!r} may not describe the build under test."
                )
            elif probe_dir and not str(lib).startswith(str(probe_dir)):
                h.notes.append(
                    f"{who}: probe resolved {lib}, outside {probe_dir}. "
                    f"openblas_dynamic_selection={sel!r} describes THAT library, not the build "
                    f"under test -- the selection field is invalid for this host."
                )
            low = (sel or "").lower()
            if "armv8" in low and "sve" not in low:
                if e.get("has_sve") is True:
                    h.escalate.append(
                        f"{who}: DYNAMIC_ARCH selected {sel!r} on a host that reports SVE. SVE "
                        f"detection failed; default NumPy/R/Julia here run generic NEON. Stop and "
                        f"escalate (standing order 8)."
                    )
                else:
                    h.notes.append(
                        f"{who}: DYNAMIC_ARCH selected {sel!r} and this host reports no SVE, so "
                        f"generic NEON is the expected target. Recorded, not an alarm."
                    )

        # The whole coretype axis depends on OPENBLAS_CORETYPE forcing working,
        # and capture-env.sh proves it per host rather than assuming it.
        forcing = e.get("openblas_coretype_forcing") or "not_probed"
        if h.forcing == "not_probed" or forcing == "unavailable":
            h.forcing = forcing
        if forcing == "unavailable":
            h.invalid.append(
                f"{who}: openblas_coretype_forcing=unavailable -- forcing a target did not change "
                f"the corename. Every forced-coretype arm on this host is mislabelled: its "
                f"coretype field is a claim about a library that ignored it."
            )
        elif forcing == "not_probed":
            h.notes.append(
                f"{who}: openblas_coretype_forcing not probed; the coretype axis is unproven here."
            )

        gov = e.get("cpufreq_governor")
        if gov not in (None, "", "none", "performance"):
            h.invalid.append(
                f"{who}: cpufreq governor {gov!r}, not 'performance'. Timings are not comparable."
            )

        tpc = e.get("threads_per_core")
        if tpc not in (None, 1):
            h.invalid.append(
                f"{who}: threads_per_core={tpc}. Graviton has no SMT; this is not a Graviton host."
            )
        elif tpc == 1 and lscpu_defaulted:
            # Same trap as `verified` failing open: the 1 is a default here, not
            # a measurement, so "SMT is off" has not been established.
            h.notes.append(
                f"{who}: threads_per_core=1 is a DEFAULT (lscpu produced no topology), not a "
                f"measurement -- SMT being off is unverified here, as are sockets and numa_nodes."
            )

        if (e.get("numa_nodes") or 1) > 1 and not lscpu_defaulted:
            h.notes.append(
                f"{who}: {e['numa_nodes']} NUMA nodes -- multithreaded arms cross a socket "
                f"boundary; compare per-socket too."
            )

        limit = e.get("cgroup_cpu_limit")
        if limit is not None:
            h.invalid.append(
                f"{who}: cgroup CPU quota in effect (cgroup_cpu_limit={limit}). Wall-clock "
                f"GFLOP/s from this host is throttled and is not a hardware measurement."
            )
        online = _int_or_none(e.get("cpus_online"))
        aff = _int_or_none(e.get("cpus_affinity"))
        if aff is not None and online is not None and aff < online:
            h.notes.append(
                f"{who}: cpus_affinity={aff} is narrower than cpus_online={online} "
                f"({e.get('cpus_affinity_list')}); arms above {aff} threads are oversubscribed "
                f"and are excluded from every comparison."
            )
            h.cpus_affinity = aff if h.cpus_affinity is None else min(h.cpus_affinity, aff)

        vl = e.get("sve_default_vl_bytes")
        if vl is None and e.get("has_sve") is True:
            h.notes.append(
                f"{who}: has_sve is true but sve_default_vl_bytes is null -- the runtime vector "
                f"length is unrecorded, and VL is the campaign's central hardware axis."
            )

    # Standing order 8's second trigger: SVE kernels never compiled in. This is
    # the quiet one. Generic ARMV8 on an SVE host at least shows up in the
    # selection string, but a library built NO_SVE=1 -- or with an assembler too
    # old to accept SVE -- still builds every arm, still runs, and still reports
    # plausible numbers, while the entire SVE axis of the campaign measures
    # nothing. build-libs.sh reads sve_kernels off the installed archive and
    # run-matrix.sh stamps the instance onto the manifest so the finding is
    # attributable to a host rather than to the dataset.
    for m in inp.manifest_arms:
        sve = m.get("sve_kernels")
        if sve not in ("no", "unknown"):
            continue
        inst = m.get("instance") or "unknown"
        h = hosts.get(inst)
        if h is None or not any(e.get("has_sve") is True for e in h.envs):
            # No SVE on this host means no SVE kernels is the correct outcome,
            # and an unstamped/unknown instance is a provenance gap, not this
            # finding -- section 7 owns that.
            continue
        if sve == "unknown":
            # build-libs.sh defines `unknown` as "we could not look" -- no nm, or
            # no readable archive -- and NOT as "the symbols are fine". Testing
            # `!= "no"` treated it as fine, so the quiet trigger for standing
            # order 8 switched itself off in exactly the case where the check
            # could not be performed. That is the wrong direction to fail in on
            # the campaign's central hardware axis. It is not an escalation
            # either, because absent evidence is not evidence of absence: it is a
            # provenance hole, and standing order 5 says a number without
            # provenance is not admissible.
            h.provenance_gaps.append(
                f"{inst}: whether the {m.get('library')}/{m.get('target')} build contains SVE "
                f"kernel symbols is UNKNOWN -- build-libs.sh could not read the archive. On a "
                f"host that reports SVE this leaves standing order 8's quiet trigger unchecked; "
                f"re-run build-libs.sh with nm available before trusting any SVE-coretype arm."
            )
            continue
        h.escalate.append(
            f"{inst}: the {m.get('library')}/{m.get('target')} build contains no SVE kernel "
            f"symbols on a host that reports SVE. NO_SVE was set, or the assembler rejected "
            f"SVE. Every SVE-coretype arm on this host measures the NEON path under an SVE "
            f"label. Stop and escalate (standing order 8)."
        )
    return hosts


# ---- cells: one aggregated number per (condition, arm) --------------------
# A condition is the full measurement condition, m/n/k/lda_pad included. The old
# group key omitted m and lda_pad and then took max() over the cell, so each
# target was represented by its own best-case size and stride, chosen
# independently -- which is how a dataset where V2 won 4 of 5 sizes printed
# "+4.8% V1 kernels win". Never max() across a condition you intend to compare.


@dataclass
class Cell:
    values: list = field(default_factory=list)  # one representative per run_id
    runs: list = field(default_factory=list)
    spreads: list = field(default_factory=list)
    all_verified: bool = True
    notes: set = field(default_factory=set)

    @property
    def value(self):
        return statistics.median(self.values)

    @property
    def n_runs(self):
        return len(self.values)

    @property
    def within_spread(self):
        return statistics.median(self.spreads) if self.spreads else 0.0

    @property
    def run_spread(self):
        if len(self.values) < 2:
            return 0.0
        m = statistics.median(self.values)
        return (max(self.values) - min(self.values)) / m if m else 0.0


def arm_of(r):
    return (r.get("library"), r.get("target"), r.get("coretype"))


def arm_label(arm):
    lib, tgt, core = arm
    return f"{lib or '?'}/{tgt or '?'}/{core or '?'}"


def is_shipped(arm):
    """The arm the wheels actually ship: DYNAMIC_ARCH, nothing forced. A "best
    of 6 kernel sets" OpenBLAS is a configuration nobody runs."""
    lib, tgt, core = arm
    return lib == "openblas" and (tgt or "").upper() == "DYNAMIC" and (core or "unforced") in ("unforced", "")


@dataclass
class Excluded:
    verified_false: list = field(default_factory=list)
    zero_gflops: list = field(default_factory=list)
    oversubscribed: int = 0
    forced_coretype: int = 0
    no_gflops: int = 0
    # (condition, arm) of every record this file dropped. The census needs it to
    # tell a cell truncated by a documented exclusion -- already a hard anomaly
    # with its own exit bit -- from a cell that is simply short of sizes and has
    # nothing accounting for it. Without this the second reads as the first.
    dropped: set = field(default_factory=set)


def build_cells(bench, hosts, exc: Excluded):
    """(condition, arm) -> Cell.

    Aggregation order is min-within-run then median-across-runs. The docs
    instruct repeated hpc7g runs, and the previous version globbed with no
    run_id awareness and took max() over the pool, so the one host the docs say
    to run repeatedly was the one the instrument flattered."""
    staged = defaultdict(lambda: defaultdict(list))
    for r in bench:
        inst = r.get("instance")
        arm = arm_of(r)
        gf = r.get("gflops")
        ver = r.get("verified")
        # incx is part of the condition, not a footnote. run_level1 runs the same
        # (routine, m, n, k, lda_pad) at stride 1 and stride 4, so without incx
        # here the two collapse into one cell and the min-within-run rule keeps
        # the slower of the two -- deleting the stride axis the campaign names as
        # the arm64 tree's weakest point. Records written before bench.c carried
        # the field default to 1, which is correct for every level-3 routine and
        # merges the old level-1 pairs exactly as they used to merge.
        cond = (
            inst,
            r.get("threads"),
            r.get("routine"),
            r.get("m"),
            r.get("n"),
            r.get("k"),
            r.get("lda_pad"),
            r.get("incx", 1),
        )
        if not isinstance(gf, (int, float)) or isinstance(gf, bool):
            exc.no_gflops += 1
            exc.dropped.add((cond, arm))
            continue
        # Order matters, and it used to be the other way round. A real
        # measurement can never be 0; bench.c emits 0 to say the timer was
        # outrun -- and at src/bench.c:381 the same branch also forces
        # `verified` to false, because it never ran the verification. So a
        # timer-outrun record arrives with BOTH markers set, and testing `ver is
        # False` first classified every one of them as a wrong answer. That made
        # this branch unreachable on real data and printed "WRONG ANSWER,
        # excluded" against a kernel that had merely finished too fast to time,
        # sending the reader after a numerical bug that does not exist. Both
        # paths exclude the record either way, so the only thing at stake is
        # which diagnosis the anomaly table shows -- which is the whole value of
        # the table. The more specific claim wins.
        if gf == 0:
            exc.zero_gflops.append(r)
            exc.dropped.add((cond, arm))
            continue
        if ver is False:
            exc.verified_false.append(r)
            exc.dropped.add((cond, arm))
            continue
        h = hosts.get(inst)
        if h is not None:
            if (
                h.cpus_affinity is not None
                and isinstance(r.get("threads"), int)
                and r["threads"] > h.cpus_affinity
            ):
                exc.oversubscribed += 1
                exc.dropped.add((cond, arm))
                continue
            if h.forcing == "unavailable" and (r.get("coretype") or "unforced") != "unforced":
                exc.forced_coretype += 1
                exc.dropped.add((cond, arm))
                continue
        staged[(cond, arm)][r.get("run_id")].append(r)

    cells = {}
    for key, by_run in staged.items():
        c = Cell()
        for run_id, recs in by_run.items():
            # min within a run_id: if a condition was measured more than once in
            # one file, the luckiest sample must not be the one that survives.
            rec = min(recs, key=lambda x: x["gflops"])
            c.values.append(rec["gflops"])
            c.runs.append(run_id)
            tmin, tp50 = rec.get("t_min"), rec.get("t_p50")
            if isinstance(tmin, (int, float)) and isinstance(tp50, (int, float)) and tmin > 0:
                c.spreads.append((tp50 - tmin) / tmin)
            if rec.get("verified") is not True:
                c.all_verified = False
            if rec.get("note"):
                c.notes.add(rec["note"])
        cells[key] = c
    return cells


# ---- comparison primitives -------------------------------------------------


@dataclass
class SizeDelta:
    cond: tuple
    a: float
    b: float
    delta: float
    band: float
    verified: bool
    runs_a: int
    runs_b: int


def band_for(ca: Cell, cb: Cell, min_effect: float) -> float:
    """Effective parity band: max(min_effect, dispersion actually observed).

    A delta smaller than the spread that produced it is not a finding. This is
    the single guard that stops run-to-run noise being published as a kernel
    result -- t_min/t_p50/t_p90 were collected and never used before.

    KNOWN LIMIT, and it is a property of the design rather than a defect in it:
    **an adaptive band derived from the same samples as the statistic it bands can
    absorb a bias in that statistic.** run_spread comes from the same per-run
    values whose median becomes the cell value, so anything that biases the value
    toward one run's number widens the band by about the amount it moves the delta
    and the row stays `parity`. Found by mutation: substituting max for the
    median across runs is invisible to every verdict-level assertion in
    gates/p1.sh, which is why the lucky-pass scenario asserts the pooled *number*
    instead. Two consequences worth carrying forward -- a self-cancelling bias in
    the aggregation reads as parity rather than as an anomaly, so aggregation must
    be tested on numbers and not on verdicts; and a real effect on a genuinely
    noisy host is banded away, which is a deliberate trade (a false null is
    publishable, a false headline is not) and the reason within_spread and
    run_spread are printed next to every verdict rather than folded into it."""
    return max(min_effect, ca.within_spread, cb.within_spread, ca.run_spread, cb.run_spread)


def per_size(cells, conds, arm_a, arm_b, min_effect):
    """Signed deltas of arm_a against arm_b at identical conditions only."""
    rows = []
    for cond in conds:
        ca = cells.get((cond, arm_a))
        cb = cells.get((cond, arm_b))
        if ca is None or cb is None:
            continue
        d = rel(ca.value, cb.value)
        if d is None:
            continue
        rows.append(
            SizeDelta(
                cond=cond,
                a=ca.value,
                b=cb.value,
                delta=d,
                band=band_for(ca, cb, min_effect),
                verified=ca.all_verified and cb.all_verified,
                runs_a=ca.n_runs,
                runs_b=cb.n_runs,
            )
        )
    return rows


def summarise(rows, args, label_a, label_b):
    """Regime verdict derived only from per-size deltas: median, direction
    counts, and the number of sizes compared."""
    if not rows:
        return {
            "n_sizes": 0,
            "median_delta": None,
            "band": args.min_effect,
            "n_a_ahead": 0,
            "n_b_ahead": 0,
            "verdict": "NO DATA",
            "verified": False,
            "unequal_runs": False,
            "mean_a": None,
            "mean_b": None,
            "runs_a": 0,
            "runs_b": 0,
        }
    deltas = [r.delta for r in rows]
    band = statistics.median([r.band for r in rows])
    med = statistics.median(deltas)
    n_a = sum(1 for r in rows if r.delta > r.band)
    n_b = sum(1 for r in rows if r.delta < -r.band)
    runs_a = {r.runs_a for r in rows}
    runs_b = {r.runs_b for r in rows}
    unequal = runs_a != runs_b or len(runs_a) > 1 or len(runs_b) > 1
    verified = all(r.verified for r in rows)
    n = len(rows)

    if n < args.min_sizes:
        verdict = f"inconclusive(thin:{n}<{args.min_sizes})"
    elif abs(med) <= band:
        verdict = "parity"
    elif unequal:
        # Unequal N between the arms of a comparison: max-of-N and
        # median-of-N are both N-sensitive, so a directional call here would be
        # partly a statement about sample counts.
        verdict = f"inconclusive(unequal-N:{sorted(runs_a)}v{sorted(runs_b)})"
    elif med > 0 and n_a / n >= args.win_fraction:
        verdict = f"{label_a}-ahead"
    elif med < 0 and n_b / n >= args.win_fraction:
        verdict = f"{label_b}-ahead"
    else:
        verdict = f"inconclusive(split:{n_a}/{n_b} of {n})"

    return {
        "n_sizes": n,
        "median_delta": med,
        "band": band,
        "n_a_ahead": n_a,
        "n_b_ahead": n_b,
        "verdict": verdict,
        "verified": verified,
        "unequal_runs": unequal,
        "mean_a": statistics.fmean([r.a for r in rows]),
        "mean_b": statistics.fmean([r.b for r in rows]),
        "runs_a": max(runs_a),
        "runs_b": max(runs_b),
    }


def vflag(s):
    return "" if s["verified"] else "  UNVERIFIED"


# ---- absence explained -----------------------------------------------------


def build_absence(inp: Inputs):
    """Why an arm produced no records, from the census first and the build
    manifest second. Absent must never look like a measured null."""
    by_arm_threads = {}
    by_arm = {}
    for o in inp.outcomes:
        arm = (o.get("library"), o.get("target"), o.get("coretype"))
        st = o.get("status") or "unknown"
        reason = o.get("reason") or ""
        if st in CENSUS_SUCCESS:
            # A census line saying the arm ran is not a reason for a cell it did
            # not produce; it is the definition of a hole. Verification case (f)
            # reproduced the alternative: the V1 arm's large regime was absent,
            # the census said status=measured, and the census section printed the
            # missing cell as "measured" -- the hole counted as coverage.
            st, reason = (
                "MISSING-UNEXPLAINED",
                f"census says status={st} with {o.get('records')} records for this arm; "
                "nothing accounts for these conditions being absent",
            )
        by_arm_threads[(o.get("instance"), arm, o.get("threads"))] = (st, reason)
        by_arm.setdefault((o.get("instance"), arm), (st, reason))
    manifest = {}
    for m in inp.manifest_arms:
        arm = (m.get("library"), m.get("target"), m.get("coretype"))
        if not m.get("built"):
            manifest[arm] = ("build_failed", m.get("reason") or "")
        elif not m.get("runnable", True):
            manifest[arm] = ("unrunnable", m.get("reason") or "")
        else:
            manifest[arm] = ("built", m.get("reason") or "")
    have_manifest = bool(inp.manifest_arms)
    have_census = bool(inp.outcomes)

    def explain(instance, arm, threads=None):
        if threads is not None and (instance, arm, threads) in by_arm_threads:
            return by_arm_threads[(instance, arm, threads)]
        if (instance, arm) in by_arm:
            return by_arm[(instance, arm)]
        if arm in manifest and manifest[arm][0] != "built":
            return manifest[arm]
        if not have_census and not have_manifest:
            return ("MISSING-UNEXPLAINED", "no census-*.ndjson and no manifest-*.ndjson in results/")
        if not have_census:
            return ("MISSING-UNEXPLAINED", "manifest says the arm was built and runnable; no census record")
        return ("MISSING-UNEXPLAINED", "nothing in the census or the manifest accounts for this arm")

    return explain, manifest


# ---- 0. hosts --------------------------------------------------------------


def report_hosts(hosts, bench_instances, out):
    out("\n" + "=" * 78)
    out("0. HOSTS  — provenance and admissibility (standing order 5)")
    out("=" * 78)
    payload = []
    for inst in sorted(set(hosts) | set(bench_instances), key=str):
        h = hosts.get(inst) or Host(instance=inst)
        e = h.envs[0] if h.envs else {}
        cores = ", ".join(
            f"{c.get('core_name')}({c.get('midr_part')})x{c.get('cpu_count')}"
            for c in (e.get("core_clusters") or [])
        )
        state = "ADMISSIBLE"
        if not h.present:
            state = "NO-PROVENANCE"
        elif h.escalate:
            state = "ESCALATE"
        elif h.invalid:
            state = "INADMISSIBLE"
        vl = e.get("sve_default_vl_bytes")
        out(
            f"  {inst!s:14s} {state:14s} cores=[{cores or 'unrecorded'}] "
            f"uniform={e.get('midr_uniform')} "
            f"sve_vl={f'{vl}B' if vl else 'unrecorded'} "
            f"gov={e.get('cpufreq_governor')} numa={e.get('numa_nodes')} "
            f"cpus={e.get('cpus_affinity')}/{e.get('cpus_online')} "
            f"probe={e.get('openblas_dynamic_probe_status')} "
            f"sel={e.get('openblas_dynamic_selection')} forcing={h.forcing}"
        )
        for r in h.escalate + h.invalid:
            out(f"      !! {r}")
        payload.append(
            {
                "instance": inst,
                "state": state,
                "env_present": h.present,
                "midr_uniform": e.get("midr_uniform"),
                "core_clusters": e.get("core_clusters"),
                "sve_default_vl_bytes": e.get("sve_default_vl_bytes"),
                "cpufreq_governor": e.get("cpufreq_governor"),
                "threads_per_core": e.get("threads_per_core"),
                "numa_nodes": e.get("numa_nodes"),
                "cpus_online": e.get("cpus_online"),
                "cpus_affinity": e.get("cpus_affinity"),
                "cgroup_cpu_limit": e.get("cgroup_cpu_limit"),
                "openblas_dynamic_probe_status": e.get("openblas_dynamic_probe_status"),
                "openblas_dynamic_selection": e.get("openblas_dynamic_selection"),
                "openblas_coretype_forcing": h.forcing,
                "invalidating": h.invalid,
                "escalate": h.escalate,
                "notes": h.notes,
            }
        )
    if not payload:
        out("  no hosts and no bench records")
    return payload


# ---- 1. deficit by routine -------------------------------------------------


def cell_groups(cells):
    """(instance, threads, routine, regime, lda_pad, incx) -> {arm: [conditions]}

    lda_pad and incx are in the key, not just in the condition. bench.c puts both
    leading dimensions into one regime and both element strides into one routine,
    and a median taken across a mix of them is a statement about neither -- the
    same conflation as the max()-over-the-cell bug, one level up. Section 3 is
    where the two leading dimensions are compared against each other."""
    g = defaultdict(lambda: defaultdict(list))
    for cond, arm in cells:
        inst, thr, routine, m, pad, incx = cond[0], cond[1], cond[2], cond[3], cond[6], cond[7]
        g[(inst, thr, routine, regime(m or 0), pad, incx)][arm].append(cond)
    return g


def report_deficit_by_routine(cells, groups, hosts, explain, args, out):
    out("\n" + "=" * 78)
    out("1. DEFICIT BY ROUTINE  — each named OpenBLAS arm vs a named reference arm")
    out("=" * 78)
    out("Signed: + = OpenBLAS behind the reference, - = OpenBLAS ahead. Compared size by")
    out("size at identical (m,n,k,lda_pad); the regime line is the median of those.")
    out("SHIPPED marks openblas/DYNAMIC/unforced, which is what NumPy wheels actually run.")

    payload = []
    instances = sorted({k[0] for k in groups}, key=str)
    for inst in instances:
        refs = {arm for k in groups if k[0] == inst for arm in groups[k] if arm[0] not in (None, "openblas")}
        if not refs:
            why = "no non-OpenBLAS library produced a record on this host"
            out(f"  {inst!s:14s} NO DATA — reference library absent: {why}")
            payload.append({"instance": inst, "no_data": why})
            continue
        h = hosts.get(inst)
        tag = "" if (h and h.admissible) else "  [HOST-NOT-ADMISSIBLE]"
        for k in sorted((k for k in groups if k[0] == inst), key=skey):
            arms = groups[k]
            _, thr, routine, reg, pad, incx = k
            present_refs = [a for a in arms if a in refs]
            if not present_refs:
                for arm in sorted((a for a in arms if a[0] == "openblas"), key=arm_label):
                    st, why = explain(inst, ("armpl", "native", "unforced"), thr)
                    out(
                        f"  {inst!s:14s} t={thr!s:<4} {routine!s:6s} {reg:6s} pad={pad!s:<3} "
                        f"incx={incx!s:<2} "
                        f"{arm_label(arm):30s} NO DATA — reference arm absent "
                        f"({st}: {why or 'no reason recorded'})"
                    )
                continue
            # Reference arm: the non-OpenBLAS arm covering the most conditions
            # here, named in every row. Never a max() over anonymous arms.
            ref = max(present_refs, key=lambda a: (len(arms[a]), arm_label(a)))
            for arm in sorted((a for a in arms if a[0] == "openblas"), key=arm_label):
                rows = per_size(cells, arms[arm], arm, ref, args.min_effect)
                s = summarise(rows, args, "openblas", arm_label(ref))
                # Deficit is the reference-relative shortfall: negate the signed
                # delta of openblas against the reference.
                med = None if s["median_delta"] is None else -s["median_delta"]
                mark = " SHIPPED" if is_shipped(arm) else ""
                out(
                    f"  {inst!s:14s} t={thr!s:<4} {routine!s:6s} {reg:6s} pad={pad!s:<3} "
                    f"incx={incx!s:<2} "
                    f"{arm_label(arm):30s} "
                    f"ob={fmt_val(s['mean_a'])} ref={fmt_val(s['mean_b'])} ({arm_label(ref)}) "
                    f"deficit={fmt_pct(med)} band={100 * s['band']:4.1f}% "
                    f"n={s['n_sizes']:<2} runs={s['runs_a']}/{s['runs_b']}{mark}{vflag(s)}{tag}"
                )
                payload.append(
                    {
                        "instance": inst,
                        "threads": thr,
                        "routine": routine,
                        "regime": reg,
                        "lda_pad": pad,
                        "incx": incx,
                        "arm": arm_label(arm),
                        "reference_arm": arm_label(ref),
                        "shipped_arm": is_shipped(arm),
                        "mean_openblas": s["mean_a"],
                        "mean_reference": s["mean_b"],
                        "median_deficit": med,
                        "band": s["band"],
                        "n_sizes": s["n_sizes"],
                        "runs_openblas": s["runs_a"],
                        "runs_reference": s["runs_b"],
                        "verified": s["verified"],
                        "host_admissible": bool(h and h.admissible),
                    }
                )
    if not payload:
        out("  no comparable cells")
    return payload


# ---- 2. target cross -------------------------------------------------------


def cross_pairs(cells, inp, args):
    """Candidate (arm_v1, arm_v2, mechanism) pairs per instance.

    TARGET= and OPENBLAS_CORETYPE are two different claims, so a build-target
    arm is never paired against a coretype-forced one. The mechanism is printed.
    Arms the manifest expected but which produced nothing are included, so a
    missing side becomes a NO DATA row instead of vanishing."""
    arms_by_inst = defaultdict(set)
    for cond, arm in cells:
        arms_by_inst[cond[0]].add(arm)
    expected = {(m.get("library"), m.get("target"), m.get("coretype")) for m in inp.manifest_arms}
    expected |= {(o.get("library"), o.get("target"), o.get("coretype")) for o in inp.outcomes}
    out = {}
    for inst, present in arms_by_inst.items():
        universe = {a for a in present | expected if a[0] == "openblas"}
        pairs = []
        coretypes = {a[2] for a in universe if a[1] in (args.v1_set, args.v2_set)}
        for c in sorted(coretypes, key=str):
            pairs.append((("openblas", args.v1_set, c), ("openblas", args.v2_set, c), "target"))
        targets = {a[1] for a in universe if a[2] in (args.v1_set, args.v2_set)}
        for t in sorted(targets, key=str):
            pairs.append((("openblas", t, args.v1_set), ("openblas", t, args.v2_set), "coretype"))
        if not pairs:
            # Nothing named either kernel set anywhere on this host. Say so,
            # with why, rather than printing an empty section.
            pairs.append(
                (("openblas", args.v1_set, "unforced"), ("openblas", args.v2_set, "unforced"), "target")
            )
        out[inst] = pairs
    return out


def report_target_cross(cells, groups, hosts, explain, inp, args, out):
    out("\n" + "=" * 78)
    out("2. TARGET CROSS  — same hardware, different OpenBLAS kernel set")
    out("=" * 78)
    out(f"{args.v1_set} = 99 SVE kernels.  {args.v2_set} -> KERNEL.NEOVERSEN2 = 5 SVE kernels.")
    out("Signed delta of the V1 set against the V2 set, per size, at identical")
    out("(m,n,k,lda_pad). Positive = V1 set ahead. TARGET= and OPENBLAS_CORETYPE are")
    out("compared separately: they are different claims.")

    payload = []
    pairs_by_inst = cross_pairs(cells, inp, args)
    for inst in sorted(pairs_by_inst, key=str):
        h = hosts.get(inst)
        tag = "" if (h and h.admissible) else "  [HOST-NOT-ADMISSIBLE]"
        keys = sorted((k for k in groups if k[0] == inst), key=skey)
        for arm_a, arm_b, mech in pairs_by_inst[inst]:
            comparable = 0
            deferred = []
            for k in keys:
                arms = groups[k]
                conds = sorted(set(arms.get(arm_a, [])) | set(arms.get(arm_b, [])), key=skey)
                rows = per_size(cells, conds, arm_a, arm_b, args.min_effect)
                _, thr, routine, reg, pad, incx = k
                if rows:
                    comparable += 1
                    s = summarise(rows, args, "V1-set", "V2-set")
                    out(
                        f"  {inst!s:14s} t={thr!s:<4} {routine!s:6s} {reg:6s} pad={pad!s:<3} "
                        f"incx={incx!s:<2} by={mech:8s} "
                        f"V1={fmt_val(s['mean_a'])} V2={fmt_val(s['mean_b'])} "
                        f"delta={fmt_pct(s['median_delta'])} band={100 * s['band']:4.1f}% "
                        f"sizes={s['n_sizes']:<2} (+{s['n_a_ahead']}/-{s['n_b_ahead']}) "
                        f"runs={s['runs_a']}/{s['runs_b']}  {s['verdict']}{vflag(s)}{tag}"
                    )
                else:
                    # A hole and a null must not look alike. The old code
                    # `continue`d here, so a missing arm printed nothing while a
                    # true null printed one `parity` row.
                    missing = arm_b if arms.get(arm_a) else arm_a
                    other = arm_a if missing == arm_b else arm_b
                    st, why = explain(inst, missing, thr)
                    half = bool(arms.get(arm_a) or arms.get(arm_b))
                    deferred.append((k, mech, missing, other, st, why, half))
                    s = summarise([], args, "V1-set", "V2-set")
                payload.append(
                    {
                        "instance": inst,
                        "threads": thr,
                        "routine": routine,
                        "regime": reg,
                        "lda_pad": pad,
                        "incx": incx,
                        "mechanism": mech,
                        "arm_v1": arm_label(arm_a),
                        "arm_v2": arm_label(arm_b),
                        "mean_v1": s["mean_a"],
                        "mean_v2": s["mean_b"],
                        "median_delta": s["median_delta"],
                        "band": s["band"],
                        "n_sizes": s["n_sizes"],
                        "n_v1_ahead": s["n_a_ahead"],
                        "n_v2_ahead": s["n_b_ahead"],
                        "runs_v1": s["runs_a"],
                        "runs_v2": s["runs_b"],
                        "unequal_runs": s["unequal_runs"],
                        "verified": s["verified"],
                        "verdict": s["verdict"],
                        "host_admissible": bool(h and h.admissible),
                    }
                )
            if comparable == 0 and deferred and not any(d[6] for d in deferred):
                # Neither side of this pair exists anywhere on the host. One
                # collapsed line, not one row per cell.
                st, why = explain(inst, arm_a, None)
                st2, why2 = explain(inst, arm_b, None)
                out(
                    f"  {inst!s:14s} NO DATA — no {args.v1_set}/{args.v2_set} pair by {mech} on this "
                    f"host: {arm_label(arm_a)} {st} ({why or 'no reason recorded'}); "
                    f"{arm_label(arm_b)} {st2} ({why2 or 'no reason recorded'})"
                )
            else:
                shown = 0
                for k, mech2, missing, other, st, why, _any in deferred:
                    if shown >= args.max_listed:
                        break
                    shown += 1
                    out(
                        f"  {inst!s:14s} t={k[1]!s:<4} {k[2]!s:6s} {k[3]:6s} pad={k[4]!s:<3} "
                        f"incx={k[5]!s:<2} by={mech2:8s} NO DATA — {arm_label(missing)} absent "
                        f"({st}: {why or 'no reason recorded'}); {arm_label(other)} present"
                    )
                if len(deferred) > shown:
                    out(f"  {inst!s:14s} ... and {len(deferred) - shown} more NO DATA cells by {mech}")
    if not payload:
        out("  no target-cross cells at all: no OpenBLAS records to pair")
    return payload


# ---- 3. lda penalty --------------------------------------------------------


def report_lda_penalty(cells, hosts, args, out):
    out("\n" + "=" * 78)
    out("3. LEADING-DIMENSION PENALTY  — isolates packing from the inner kernel")
    out("=" * 78)
    out("Positive = padding costs performance. Aggregated per arm and size across runs;")
    out("the previous version used plain assignment, so the last file in glob order won.")

    tight = {}
    padded = {}
    for (cond, arm), c in cells.items():
        inst, thr, routine, m, n, k, pad, incx = cond
        base = (inst, arm, thr, routine, m, n, k, incx)
        if pad == 0:
            tight[base] = c
        elif pad:
            padded.setdefault(pad, {})[base] = c

    payload = []
    for pad in sorted(padded, key=str):
        for base in sorted(padded[pad], key=skey):
            ct = tight.get(base)
            cp = padded[pad][base]
            if ct is None:
                continue
            inst, arm, thr, routine, m, _n, _k, incx = base
            pen = rel(ct.value, cp.value)
            band = band_for(ct, cp, args.min_effect)
            h = hosts.get(inst)
            tag = "" if (h and h.admissible) else "  [HOST-NOT-ADMISSIBLE]"
            verdict = (
                "n/a"
                if pen is None
                else (
                    "within band" if abs(pen) <= band else ("padding hurts" if pen > 0 else "padding helps")
                )
            )
            ver = "" if (ct.all_verified and cp.all_verified) else "  UNVERIFIED"
            out(
                f"  {inst!s:14s} {arm_label(arm):30s} t={thr!s:<4} {routine!s:6s} n={m!s:<5} "
                f"pad={pad!s:<3} tight={fmt_val(ct.value)} padded={fmt_val(cp.value)} "
                f"penalty={fmt_pct(pen)} band={100 * band:4.1f}% "
                f"runs={ct.n_runs}/{cp.n_runs}  {verdict}{ver}{tag}"
            )
            payload.append(
                {
                    "instance": inst,
                    "arm": arm_label(arm),
                    "threads": thr,
                    "routine": routine,
                    "m": m,
                    "lda_pad": pad,
                    "incx": incx,
                    "regime": regime(m or 0),
                    "tight": ct.value,
                    "padded": cp.value,
                    "penalty": pen,
                    "band": band,
                    "runs_tight": ct.n_runs,
                    "runs_padded": cp.n_runs,
                    "verdict": verdict,
                    "verified": ct.all_verified and cp.all_verified,
                    "host_admissible": bool(h and h.admissible),
                }
            )
    if not payload:
        out("  no (tight, padded) pair at an identical size for any arm")
    return payload


# ---- 4. regime profile -----------------------------------------------------


def report_regime_profile(deficits, cross, args, out):
    """Promised in the docstring since the first draft and never implemented,
    while P1 requires the planted small-regime penalty be recovered."""
    out("\n" + "=" * 78)
    out("4. REGIME PROFILE  — where in the size range the effect lives")
    out("=" * 78)
    out("The N2 kernel set defines no GEMM_SMALL_* entries, so a kernel-set deficit should")
    out("be visible in small and absent in large. small-large is that gap, explicitly.")

    payload = {"deficit": [], "target_cross": []}

    out("\n  4a. deficit vs reference arm, by regime")
    by = defaultdict(dict)
    for d in deficits:
        if d.get("median_deficit") is None:
            continue
        by[
            (
                d["instance"],
                d["threads"],
                d["arm"],
                d["routine"],
                d["lda_pad"],
                d["incx"],
                d["reference_arm"],
            )
        ][d["regime"]] = d
    if not by:
        out("      no reference-arm deficits to profile")
    for k in sorted(by, key=skey):
        inst, thr, arm, routine, pad, incx, ref = k
        r = by[k]
        vals = {reg: (r[reg]["median_deficit"] if reg in r else None) for reg in REGIMES}
        gap = None if vals["small"] is None or vals["large"] is None else vals["small"] - vals["large"]
        thin = [reg for reg in REGIMES if reg not in r]
        out(
            f"      {inst!s:14s} t={thr!s:<4} {arm:30s} {routine!s:6s} pad={pad!s:<3} "
            f"incx={incx!s:<2} vs {ref:22s} "
            f"small={fmt_pct(vals['small'])} medium={fmt_pct(vals['medium'])} "
            f"large={fmt_pct(vals['large'])}  small-large={fmt_pct(gap)}"
            + (f"  MISSING:{','.join(thin)}" if thin else "")
        )
        payload["deficit"].append(
            {
                "instance": inst,
                "threads": thr,
                "arm": arm,
                "routine": routine,
                "lda_pad": pad,
                "incx": incx,
                "reference_arm": ref,
                "small": vals["small"],
                "medium": vals["medium"],
                "large": vals["large"],
                "small_minus_large": gap,
                "regimes_missing": thin,
            }
        )

    out(f"\n  4b. {args.v1_set} vs {args.v2_set} delta, by regime (+ = V1 set ahead)")
    by2 = defaultdict(dict)
    for c in cross:
        if c.get("median_delta") is None:
            continue
        by2[
            (
                c["instance"],
                c["threads"],
                c["mechanism"],
                c["arm_v1"],
                c["arm_v2"],
                c["routine"],
                c["lda_pad"],
                c["incx"],
            )
        ][c["regime"]] = c
    if not by2:
        out("      no comparable target-cross cell in any regime")
    for k in sorted(by2, key=skey):
        inst, thr, mech, a1, _a2, routine, pad, incx = k
        r = by2[k]
        vals = {reg: (r[reg]["median_delta"] if reg in r else None) for reg in REGIMES}
        gap = None if vals["small"] is None or vals["large"] is None else vals["small"] - vals["large"]
        thin = [reg for reg in REGIMES if reg not in r]
        out(
            f"      {inst!s:14s} t={thr!s:<4} by={mech:8s} {routine!s:6s} pad={pad!s:<3} "
            f"incx={incx!s:<2} {a1:24s} "
            f"small={fmt_pct(vals['small'])} medium={fmt_pct(vals['medium'])} "
            f"large={fmt_pct(vals['large'])}  small-large={fmt_pct(gap)}"
            + (f"  MISSING:{','.join(thin)}" if thin else "")
        )
        payload["target_cross"].append(
            {
                "instance": inst,
                "threads": thr,
                "mechanism": mech,
                "routine": routine,
                "lda_pad": pad,
                "incx": incx,
                "arm_v1": a1,
                "small": vals["small"],
                "medium": vals["medium"],
                "large": vals["large"],
                "small_minus_large": gap,
                "regimes_missing": thin,
            }
        )
    return payload


# ---- 5. anomalies ----------------------------------------------------------


def report_anomalies(inp, cells, hosts, exc: Excluded, scaling, args, out):
    out("\n" + "=" * 78)
    out("5. ANOMALIES  — read this before trusting any number in sections 1-4, 6-7")
    out("=" * 78)
    items = []

    def add(sev, kind, text):
        # Every finding increments the counter. The old version printed "none"
        # underneath a flagged NUMA note and a flagged noisy-neighbour block
        # because neither incremented it, and gate P3 greps for exactly "none".
        items.append({"severity": sev, "kind": kind, "text": text})
        out(f"  {sev} {text}")

    for name, why in inp.bad_env_files:
        add("!!", "env_unparseable", f"{name}: {why}. That host's provenance is unreadable.")

    # A refused-because-mislabelled arm is not a coverage hole to be explained and
    # moved past. It means the coretype label and the library that would have done
    # the work disagreed on this host, so every OTHER forced-coretype arm here is
    # suspect too -- the same probe produced all their labels.
    for o in inp.outcomes:
        if (o.get("status") or "") != "mislabelled":
            continue
        add(
            "!!",
            "arch_selected_mismatch",
            f"{o.get('instance') or 'unknown'}: {o.get('library')}/{o.get('target')} "
            f"coretype={o.get('coretype') or 'unforced'!r} refused to measure -- bench.c's "
            f"in-process corename disagreed with the runner's probe "
            f"({o.get('coretype_effective') or 'unknown'!r}). Every forced-coretype label on "
            f"this host comes from that same probe and is therefore unconfirmed.",
        )
    for fam in inp.missing_families:
        add(".", "file_family_absent", f"no {fam}-*.ndjson/json in results/ — coverage fact, see section 7")
    if inp.bad_lines:
        add("!", "unparseable_lines", f"{inp.bad_lines} unparseable/unclassifiable NDJSON lines were skipped")
    for a in inp.escalation_acks:
        add(
            "!!",
            "escalation_acked",
            f"{a.get('host') or a.get('run_id') or 'unknown'}: capture-env.sh refused this host and "
            f"GBB_ESCALATION_ACK overrode the refusal to let the sweep run. Note given: "
            f"{a.get('note')!r}. Standing order 8 outweighs every kernel question in the repo, so "
            f"the override is reported here whether or not the condition is still detectable.",
        )
    for role, n in sorted(inp.foreign_roles.items(), key=lambda kv: str(kv[0])):
        add(
            "!!",
            "role_excluded",
            f"{n} records with role={role!r} were dropped before analysis: this directory holds "
            f"more than one role. They are excluded correctly, but a collection path that mixes "
            f"instrument checks with campaign data is broken and stays broken until it is fixed. "
            f"Instrument hosts are not Neoverse and not Graviton (CLAUDE.md).",
        )

    for inst in sorted(hosts, key=str):
        h = hosts[inst]
        for r in h.escalate:
            add("!!", "escalate", r)
        for r in h.invalid:
            add("!!", "host_invalid", r)
        for r in h.provenance_gaps:
            add("!!", "sve_kernels_unknown", r)
        for r in h.notes:
            add(".", "host_note", r)

    bench_instances = {r.get("instance") for r in inp.bench}
    for inst in sorted(bench_instances, key=str):
        if inst not in hosts or not hosts[inst].present:
            add(
                "!!",
                "no_provenance",
                f"{inst}: bench records exist but no env-*.json describes this instance. "
                f"A number without provenance is not admissible (standing order 5).",
            )

    # blas_sha divergence: two hosts built from different OpenBLAS trees and the
    # artifact could not previously tell.
    shas = defaultdict(set)
    for r in inp.bench:
        shas[(r.get("library"), r.get("target"))].add(r.get("blas_sha"))
    for (lib, tgt), s in sorted(shas.items(), key=lambda kv: skey(kv[0])):
        if len(s) > 1:
            add(
                "!!",
                "blas_sha_conflict",
                f"{lib}/{tgt} appears with {len(s)} different blas_sha values ({sorted(map(str, s))}). "
                f"Records from different libraries are being compared as one arm.",
            )
        if s == {""} or s == {None}:
            add(
                "!",
                "blas_sha_absent",
                f"{lib}/{tgt}: blas_sha is empty — the BLAS under test is unidentified",
            )

    for r in inp.arm_failures:
        note = " (SIGILL: target needs ISA this host lacks)" if r.get("exit_code") == 132 else ""
        add(
            "!",
            "arm_failed",
            f"arm failed: {r.get('library')}/{r.get('target')} threads={r.get('threads')} "
            f"run={r.get('run_id')} exit={r.get('exit_code')}{note}",
        )

    for r in exc.verified_false[: args.max_listed]:
        add(
            "!!",
            "verification_failed",
            f"WRONG ANSWER, excluded: {arm_label(arm_of(r))} {r.get('routine')} n={r.get('m')} "
            f"threads={r.get('threads')} note={r.get('note')!r}",
        )
    if len(exc.verified_false) > args.max_listed:
        add("!!", "verification_failed_more", f"... and {len(exc.verified_false) - args.max_listed} more")

    for r in exc.zero_gflops[: args.max_listed]:
        add(
            "!",
            "zero_gflops",
            f"0.00 GFLOP/s, excluded: {arm_label(arm_of(r))} {r.get('routine')} n={r.get('m')} "
            f"threads={r.get('threads')} note={r.get('note')!r} — a real measurement is never 0",
        )
    if len(exc.zero_gflops) > args.max_listed:
        add("!", "zero_gflops_more", f"... and {len(exc.zero_gflops) - args.max_listed} more")

    if exc.oversubscribed:
        add(
            "!",
            "oversubscribed_excluded",
            f"{exc.oversubscribed} records excluded: thread count exceeds cpus_affinity on that host",
        )
    if exc.forced_coretype:
        add(
            "!!",
            "forced_coretype_excluded",
            f"{exc.forced_coretype} records excluded: OPENBLAS_CORETYPE forcing is unavailable on that "
            f"host, so their coretype label is not a fact about the library that ran",
        )
    if exc.no_gflops:
        add("!", "no_gflops_field", f"{exc.no_gflops} records had no numeric gflops field")

    noisy = []
    for r in inp.bench:
        tmin, tp50 = r.get("t_min"), r.get("t_p50")
        if not (isinstance(tmin, (int, float)) and isinstance(tp50, (int, float)) and tmin > 0):
            continue
        if (tp50 - tmin) / tmin > args.noisy_spread:
            noisy.append(((tp50 - tmin) / tmin, r))
    noisy.sort(key=lambda x: -x[0])
    for spread, r in noisy[: args.max_listed]:
        add(
            ".",
            "noisy",
            f"spread {100 * spread:.0f}% (p50 vs min): {r.get('instance')} {arm_label(arm_of(r))} "
            f"{r.get('routine')} n={r.get('m')} t={r.get('threads')} — widens that cell's band, "
            f"it is not silently dropped",
        )
    if len(noisy) > args.max_listed:
        add(
            ".",
            "noisy_more",
            f"... and {len(noisy) - args.max_listed} more above {100 * args.noisy_spread:.0f}% spread",
        )

    for s in scaling:
        if s["peak_fma"] is None:
            add(
                ".",
                "peak_fma_absent",
                f"{s['instance']} t={s['threads']}: no peak_fma record — standing order 1's "
                f"cross-check was NOT performed here (absent, not passed)",
            )
        elif s["headroom_ratio"] is not None and s["headroom_ratio"] > args.headroom_factor:
            add(
                "!",
                "headroom",
                f"{s['instance']} t={s['threads']}: peak_fma {s['peak_fma']:.1f} exceeds best GEMM "
                f"{s['best_dgemm']:.1f} by {100 * (s['headroom_ratio'] - 1):.0f}% — every arm on this "
                f"host may be leaving headroom, and that gap is the headline",
            )

    # Verification coverage, per routine. dtrsm/dtrmm/dsymm have no check at all,
    # and those are exactly the operations in the 90-kernel N2 gap under study.
    out("\n  verification coverage (verified=true is the only thing that counts):")
    cov = defaultdict(lambda: [0, 0, 0])
    for r in inp.bench:
        v = r.get("verified")
        idx = 0 if v is True else 1 if v is False else 2
        cov[r.get("routine")][idx] += 1
    coverage = []
    for routine in sorted(cov, key=str):
        t, f, u = cov[routine]
        total = t + f + u
        frac = t / total if total else 0.0
        out(
            f"    {routine!s:8s} true={t:<6} false={f:<5} null={u:<6} "
            f"verified={100 * frac:5.1f}%" + ("   <-- no check exists for this routine" if t == 0 else "")
        )
        coverage.append(
            {"routine": routine, "n_true": t, "n_false": f, "n_null": u, "fraction_verified": frac}
        )
    unver_cells = sum(1 for c in cells.values() if not c.all_verified)
    out(f"    {unver_cells} of {len(cells)} aggregated cells contain at least one unverified record")

    if not items:
        out("\n  none")
    return items, coverage, unver_cells


# ---- 6. scaling ------------------------------------------------------------


def compute_scaling(cells, roof, args):
    """Primary denominator: the best GFLOP/s any arm achieved on that host at
    that thread count over large dgemm (standing order 1 -- changing this needs
    Scott). max() over arms is the policy here and only here; it is computed
    from aggregated cells so a lucky repeat cannot become the ceiling."""
    best = defaultdict(list)
    for (cond, _arm), c in cells.items():
        inst, thr, routine, m = cond[0], cond[1], cond[2], cond[3]
        if routine == "dgemm" and regime(m or 0) == "large":
            best[(inst, thr)].append(c.value)
    peaks = defaultdict(list)
    for r in roof:
        if r.get("metric") in ("peak_fma", "peak_fma_allcore"):
            gf = r.get("gflops_f64")
            if isinstance(gf, (int, float)) and not isinstance(gf, bool):
                peaks[(r.get("instance"), r.get("threads"))].append(gf)

    rows = []
    for key in sorted(set(best) | set(peaks), key=skey):
        inst, thr = key
        emp = max(best[key]) if best.get(key) else None
        # An empty peak list must stay None. It used to collapse to 0, so the
        # cross-check printed `nan` and vanished instead of announcing itself.
        pk = max(peaks[key]) if peaks.get(key) else None
        ratio = None if (emp is None or not emp or pk is None) else pk / emp
        rows.append(
            {
                "instance": inst,
                "threads": thr,
                "best_dgemm": emp,
                "peak_fma": pk,
                "headroom_ratio": ratio,
                "peak_fma_status": (
                    "absent"
                    if pk is None
                    else "headroom"
                    if (ratio is not None and ratio > args.headroom_factor)
                    else "ok"
                ),
            }
        )
    return rows


def report_scaling(rows, out):
    out("\n" + "=" * 78)
    out("6. SCALING  — against measured all-core peak, never theoretical")
    out("=" * 78)
    for s in rows:
        pk = "absent (cross-check NOT performed)" if s["peak_fma"] is None else f"{s['peak_fma']:9.2f}"
        emp = "absent" if s["best_dgemm"] is None else f"{s['best_dgemm']:9.2f}"
        flag = "  <-- headroom, see section 5" if s["peak_fma_status"] == "headroom" else ""
        out(f"  {s['instance']!s:14s} t={s['threads']!s:<4} best_large_dgemm={emp} peak_fma={pk}{flag}")
    if not rows:
        out("  no large dgemm and no peak_fma record")


# ---- 7. coverage census ----------------------------------------------------


def report_coverage(cells, inp, explain, hosts, exc: Excluded, args, out):
    """Expected cells vs measured cells, with every absence classified.

    The expectation is derived, never invented: the expected conditions on a
    host are the conditions some arm actually measured there, and the expected
    arms are the manifest's arms plus the census's plus the observed ones. So
    MISSING-UNEXPLAINED means "another arm measured this and nothing says why
    this one did not" -- a hole in the experiment, not a guess about bench.c."""
    out("\n" + "=" * 78)
    out("7. COVERAGE CENSUS  — measured / explained-absent / MISSING-UNEXPLAINED")
    out("=" * 78)

    conds_by_inst = defaultdict(set)
    arms_by_inst = defaultdict(set)
    for cond, arm in cells:
        conds_by_inst[cond[0]].add(cond)
        arms_by_inst[cond[0]].add(arm)
    expected_arms = {
        (m.get("library"), m.get("target"), m.get("coretype"))
        for m in inp.manifest_arms
        if m.get("library") not in NON_BENCH_LIBRARIES
    }
    for o in inp.outcomes:
        if o.get("library") in NON_BENCH_LIBRARIES:
            continue
        if o.get("instance") in conds_by_inst or not conds_by_inst:
            expected_arms.add((o.get("library"), o.get("target"), o.get("coretype")))

    tally = defaultdict(int)
    per_arm = defaultdict(lambda: defaultdict(int))
    missing_cells = []
    for inst, conds in sorted(conds_by_inst.items(), key=lambda kv: str(kv[0])):
        arms = sorted(arms_by_inst[inst] | expected_arms, key=arm_label)
        cellset = defaultdict(list)
        for cond in conds:
            cellset[(cond[1], cond[2], regime(cond[3] or 0), cond[6], cond[7])].append(cond)
        for arm in arms:
            for ck, clist in sorted(cellset.items(), key=skey):
                thr = ck[0]
                absent = [cond for cond in clist if (cond, arm) not in cells]
                have = len(clist) - len(absent)
                if not absent:
                    status = "measured"
                elif all((cond, arm) in exc.dropped for cond in absent):
                    # Truncated by an exclusion this file made and already
                    # reported as a hard anomaly in section 5. Counting it as a
                    # coverage hole too would double-count one fact; leaving it
                    # as plain "partial" would hide which of the two it is.
                    status = "excluded" if not have else "partial-excluded"
                elif have:
                    status = "partial"
                else:
                    status, _why = explain(inst, arm, thr)
                tally[status] += 1
                per_arm[(inst, arm)][status] += 1
                if status in ("MISSING-UNEXPLAINED", "partial"):
                    _st, why = explain(inst, arm, thr)
                    missing_cells.append(
                        {
                            "instance": inst,
                            "arm": arm_label(arm),
                            "threads": thr,
                            "routine": ck[1],
                            "regime": ck[2],
                            "lda_pad": ck[3],
                            "incx": ck[4],
                            "status": status,
                            "measured_conditions": have,
                            "expected_conditions": len(clist),
                            "reason": why,
                        }
                    )

    for inst, arm in sorted(per_arm, key=lambda k: (str(k[0]), arm_label(k[1]))):
        d = per_arm[(inst, arm)]
        total = sum(d.values())
        bits = " ".join(f"{k}={v}" for k, v in sorted(d.items()))
        flag = "  <-- HOLE" if d.get("MISSING-UNEXPLAINED") or d.get("partial") else ""
        out(f"  {inst!s:14s} {arm_label(arm):30s} cells={total:<5} {bits}{flag}")
    if not per_arm:
        out("  nothing to census: no cells")

    shown = [m for m in missing_cells if m["status"] == "MISSING-UNEXPLAINED"]
    if shown:
        out(f"\n  MISSING-UNEXPLAINED cells ({len(shown)}); nothing in results/ accounts for these:")
        for m in shown[: args.max_listed]:
            out(
                f"    {m['instance']!s:14s} {m['arm']:30s} t={m['threads']!s:<4} {m['routine']!s:6s} "
                f"{m['regime']:6s} pad={m['lda_pad']!s:<3} "
                f"0/{m['expected_conditions']} conditions — {m['reason']}"
            )
        if len(shown) > args.max_listed:
            out(f"    ... and {len(shown) - args.max_listed} more")
    partial = [m for m in missing_cells if m["status"] == "partial"]
    if partial:
        out(f"\n  PARTIAL cells ({len(partial)}); some sizes present, some absent unexplained:")
        for m in partial[: args.max_listed]:
            out(
                f"    {m['instance']!s:14s} {m['arm']:30s} t={m['threads']!s:<4} {m['routine']!s:6s} "
                f"{m['regime']:6s} pad={m['lda_pad']!s:<3} "
                f"{m['measured_conditions']}/{m['expected_conditions']} conditions"
            )
        if len(partial) > args.max_listed:
            out(f"    ... and {len(partial) - args.max_listed} more")

    inadmissible = sorted(i for i, h in hosts.items() if not h.admissible)
    if inadmissible:
        out(f"\n  hosts whose cells cannot support a verdict: {', '.join(map(str, inadmissible))}")
    return {
        "expected_cells": sum(tally.values()),
        "by_status": dict(tally),
        "missing_unexplained": tally.get("MISSING-UNEXPLAINED", 0),
        "partial": tally.get("partial", 0),
        "partial_excluded": tally.get("partial-excluded", 0),
        "excluded": tally.get("excluded", 0),
        "measured": tally.get("measured", 0),
        "cells": missing_cells,
        "by_arm": [
            {"instance": inst, "arm": arm_label(arm), **dict(per_arm[(inst, arm)])} for (inst, arm) in per_arm
        ],
        "inadmissible_hosts": inadmissible,
    }


# ---- 8. replicates ---------------------------------------------------------


def replicate_passes(inp):
    """instance_type -> {instance_id -> {run_id, ...}}, read from env-*.json.

    A replicate is the same instance_type on a different instance_id. Both fields
    are already recorded by capture-env.sh, so this needs no new field and fails
    safe in the direction that matters: a re-run on the same box shares its
    instance_id and is correctly not counted as a second pass. run_id is the join
    key because it is the only field bench records and env files share."""
    passes = defaultdict(lambda: defaultdict(set))
    for e in inp.envs:
        rid, itype, iid = e.get("run_id"), e.get("instance_type"), e.get("instance_id")
        if rid and itype and iid:
            passes[itype][iid].add(rid)
    return passes


def _direction(code):
    """+1 / -1 / 0 for a verdict that makes a claim, None for one that does not."""
    return {"V1-SET-AHEAD": 1, "V2-SET-AHEAD": -1, "NULL": 0}.get(code)


def report_replicates(inp, hosts, explain, args, out):
    """Each pass analysed alone, then the verdicts compared.

    Deliberately NOT a pooled statistic. P3 spends the second pass to find out
    whether the first one reproduces; medianing the two would answer a different
    and much weaker question -- and would do it while looking tidier, which is
    worse. Every number below comes from one pass and is labelled with its box."""
    out("\n" + "=" * 78)
    out("8. REPLICATES  — does the headline survive a second physical machine")
    out("=" * 78)
    out("A replicate is the same instance_type on a different instance_id. The passes are")
    out("compared, never pooled: a median across them would report a number neither pass")
    out("measured, and would hide the one thing the second pass was bought to test.")

    passes = replicate_passes(inp)
    payload = []
    for inst in sorted(passes, key=str):
        boxes = passes[inst]
        n_runs = sum(len(v) for v in boxes.values())
        if len(boxes) < 2:
            why = f"{len(boxes)} instance_id across {n_runs} run_id(s) — " + (
                "a re-run on the same physical box is not an independent pass"
                if n_runs > 1
                else "one pass only"
            )
            out(f"  {inst!s:14s} NO-REPLICATE         {why}")
            payload.append(
                {
                    "instance": inst,
                    "status": "NO-REPLICATE",
                    "why": why,
                    "instance_ids": sorted(boxes, key=str),
                    "run_ids": sorted((r for v in boxes.values() for r in v), key=str),
                    "passes": [],
                }
            )
            continue

        per = []
        for iid in sorted(boxes, key=str):
            rids = boxes[iid]
            bench = [r for r in inp.bench if r.get("run_id") in rids]
            # A fresh Excluded per pass: exc is a report of what this file dropped,
            # and adding the same dropped record to the campaign-level tally once
            # per pass would inflate section 5's counts.
            pcells = build_cells(bench, hosts, Excluded())
            pcross = report_target_cross(
                pcells, cell_groups(pcells), hosts, explain, inp, args, lambda _line: None
            )
            v = compute_verdict(pcross, hosts, args)
            per.append(
                {
                    "instance_id": iid,
                    "run_ids": sorted(rids, key=str),
                    "cells": len(pcells),
                    "verdict_code": v["code"],
                    "median_delta": v["median_delta"],
                    "cells_comparable": v["cells_comparable"],
                }
            )
            out(
                f"  {inst!s:14s} {iid!s:22s} runs={','.join(sorted(map(str, rids))):24s} "
                f"cells={len(pcells):<5} comparable={v['cells_comparable']:<4} "
                f"median={fmt_pct(v['median_delta'])} {v['code']}"
            )

        codes = {p["verdict_code"] for p in per}
        dirs = {_direction(p["verdict_code"]) for p in per}
        if len(codes) == 1:
            status = "REPRODUCES"
            note = f"both passes: {next(iter(codes))}"
        elif None not in dirs and len(dirs) > 1:
            status = "DIVERGES-DIRECTION"
            note = (
                f"the passes do not reproduce: {', '.join(sorted(codes))}. These are "
                f"different answers to the campaign's question, from the same instance "
                f"type on different boxes; neither is publishable as the headline."
            )
        else:
            status = "DIVERGES-INCONCLUSIVE"
            note = (
                f"the passes do not reproduce: {', '.join(sorted(codes))}. At least one "
                f"pass reached no directional verdict, so the disagreement is about "
                f"whether the effect was measurable, not about its sign."
            )
        out(f"  {inst!s:14s} {status:20s} {note}")

        deltas = [p["median_delta"] for p in per if p["median_delta"] is not None]
        spread = (max(deltas) - min(deltas)) if len(deltas) > 1 else None
        if spread is not None:
            # Reported, never gated on. Two boxes of the same instance type differ
            # in ways this campaign does not control, and turning that into a
            # pass/fail would be tuning the analysis. The claim under test is the
            # verdict; the spread is context for reading it.
            out(f"  {inst!s:14s} pass-to-pass spread of the median delta: {fmt_pct(spread)}")
        payload.append(
            {
                "instance": inst,
                "status": status,
                "why": note,
                "instance_ids": sorted(boxes, key=str),
                "run_ids": sorted((r for v in boxes.values() for r in v), key=str),
                "median_delta_spread": spread,
                "passes": per,
            }
        )

    if not payload:
        out("  no env-*.json carries both instance_type and instance_id: replicates unknowable")
    return payload


# ---- verdict ---------------------------------------------------------------


def compute_verdict(cross, hosts, args):
    """One line, computed. The previous version's decision guide was
    unconditional literal text, so `grep -q parity` and `grep -q "publish the
    negative result"` both passed on a dataset with zero comparisons."""
    tally = defaultdict(int)
    per_ir = defaultdict(lambda: defaultdict(int))
    deltas = []
    unverified = 0
    for c in cross:
        v = c["verdict"]
        adm = c["host_admissible"]
        if not adm:
            bucket = "inadmissible"
        elif v == "NO DATA":
            bucket = "no_data"
        elif v == "parity":
            bucket = "parity"
        elif v.endswith("V1-set-ahead"):
            bucket = "v1_wins"
        elif v.endswith("V2-set-ahead"):
            bucket = "v2_wins"
        else:
            bucket = "inconclusive"
        tally[bucket] += 1
        per_ir[(c["instance"], c["regime"])][bucket] += 1
        if bucket in ("v1_wins", "v2_wins", "parity"):
            if c["median_delta"] is not None:
                deltas.append(c["median_delta"])
            if not c["verified"]:
                unverified += 1

    total = sum(tally.values())
    comparable = tally["v1_wins"] + tally["v2_wins"] + tally["parity"]
    med = statistics.median(deltas) if deltas else None
    band_pct = 100 * args.min_effect

    if total == 0:
        code = "NO-DATA"
        line = (
            f"VERDICT: NO-DATA — no {args.v1_set}/{args.v2_set} comparison exists in this dataset; "
            f"nothing here can answer whether the N2 gap is worth closing"
        )
    elif comparable == 0 or (total - comparable) / total > args.max_nodata_fraction:
        code = "INCONCLUSIVE"
        line = (
            f"VERDICT: INCONCLUSIVE — {total - comparable} of {total} cells have no comparable "
            f"{args.v1_set}-set measurement (no_data={tally['no_data']}, "
            f"inconclusive={tally['inconclusive']}, inadmissible-host={tally['inadmissible']})"
        )
    elif tally["v1_wins"] / comparable >= args.verdict_majority:
        code = "V1-SET-AHEAD"
        line = (
            f"VERDICT: V1-SET-AHEAD — median {100 * med:+.1f}% over {tally['v1_wins']}/{comparable} "
            f"comparable cells, above the {band_pct:.0f}% floor"
        )
    elif tally["v2_wins"] / comparable >= args.verdict_majority:
        code = "V2-SET-AHEAD"
        line = (
            f"VERDICT: V2-SET-AHEAD — median {100 * med:+.1f}% over {tally['v2_wins']}/{comparable} "
            f"comparable cells, against the V1 set; the NEON choice was right, publish the negative result"
        )
    elif tally["parity"] / comparable >= args.verdict_majority:
        code = "NULL"
        line = (
            f"VERDICT: NULL — {args.v1_set}-set and {args.v2_set}-set at parity in "
            f"{tally['parity']}/{comparable} comparable cells; publish the negative result"
        )
    else:
        code = "MIXED"
        line = (
            f"VERDICT: MIXED — {tally['v1_wins']} cells favour the V1 set, {tally['v2_wins']} the V2 "
            f"set, {tally['parity']} at parity, of {comparable} comparable; no majority at "
            f"{100 * args.verdict_majority:.0f}%"
        )
    return {
        "code": code,
        "line": line,
        "cells_total": total,
        "cells_comparable": comparable,
        "v1_wins": tally["v1_wins"],
        "v2_wins": tally["v2_wins"],
        "parity": tally["parity"],
        "no_data": tally["no_data"],
        "inconclusive": tally["inconclusive"],
        "inadmissible_host": tally["inadmissible"],
        "median_delta": med,
        "min_effect": args.min_effect,
        "unverified_cells": unverified,
        "by_instance_regime": [
            {"instance": i, "regime": r, **dict(per_ir[(i, r)])} for (i, r) in sorted(per_ir, key=skey)
        ],
        "hosts_admissible": sorted(i for i, h in hosts.items() if h.admissible),
    }


def report_verdict(verdict, lda, regimes, coverage, anomalies, replicates, exit_code, args, out):
    out("\n" + "=" * 78)
    out("DECISION")
    out("=" * 78)
    out("  " + verdict["line"])
    if verdict["unverified_cells"]:
        out(
            f"  VERDICT-CAVEAT: {verdict['unverified_cells']} contributing cells rest on records with "
            f"verified=null (no correctness check exists for that routine)."
        )
    if coverage["missing_unexplained"] or coverage["partial"]:
        out(
            f"  VERDICT-CAVEAT: {coverage['missing_unexplained']} MISSING-UNEXPLAINED and "
            f"{coverage['partial']} PARTIAL cells — the experiment has holes nothing in results/ "
            f"accounts for (section 7)."
        )
    poisonous = [a for a in anomalies if a["severity"] == "!!"]
    if poisonous:
        out(f"  VERDICT-CAVEAT: {len(poisonous)} hard anomalies in section 5.")
    diverged = [r for r in replicates if r["status"].startswith("DIVERGES")]
    if diverged:
        # Printed as a caveat on the verdict line rather than folded into it: the
        # pooled verdict above is still what the pooled data says, and overwriting
        # it here would hide which of the two claims is being reported.
        out(
            f"  VERDICT-CAVEAT: the headline does not reproduce on "
            f"{', '.join(str(r['instance']) for r in diverged)} — independent passes on "
            f"different physical boxes disagree (section 8). The line above is the pooled "
            f"reading and should not be published while that holds."
        )

    # Consequences are printed only for findings the data actually shows. The
    # old guide stated all of them unconditionally, which is why the gate could
    # not assert on any of them.
    hurts = [r for r in lda if r["verdict"] == "padding hurts" and r["host_admissible"]]
    if hurts:
        med = statistics.median([r["penalty"] for r in hurts])
        out(
            f"  CONSEQUENCE: leading-dimension penalty is real in {len(hurts)} of {len(lda)} pairs "
            f"(median {100 * med:+.1f}%) — packing kernels are the target."
        )
    small_led = [
        r
        for r in regimes["deficit"]
        if r["small_minus_large"] is not None and r["small_minus_large"] > args.min_effect
    ]
    if small_led:
        med = statistics.median([r["small_minus_large"] for r in small_led])
        out(
            f"  CONSEQUENCE: deficit concentrated in the small regime in {len(small_led)} profiles "
            f"(median small-large {100 * med:+.1f}%) — the missing GEMM_SMALL_* path."
        )
    out(
        f"  EXIT: {exit_code} (0 clean; 2 poisoned/inadmissible, 4 coverage hole, "
        f"8 provenance, 16 does-not-reproduce, OR-ed)"
    )


# ---- main ------------------------------------------------------------------


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="decompose a graviton-blas-bench sweep into a decision")
    ap.add_argument("results", type=pathlib.Path)
    ap.add_argument(
        "--min-effect",
        type=float,
        default=DEFAULT_MIN_EFFECT,
        help="floor of the parity band as a fraction; the band widens to observed dispersion "
        f"(default {DEFAULT_MIN_EFFECT})",
    )
    ap.add_argument(
        "--min-sizes",
        type=int,
        default=DEFAULT_MIN_SIZES,
        help=f"sizes that must be comparable before a regime verdict is directional "
        f"(default {DEFAULT_MIN_SIZES})",
    )
    ap.add_argument(
        "--win-fraction",
        type=float,
        default=DEFAULT_WIN_FRACTION,
        help=f"fraction of sizes that must agree in sign for a direction (default {DEFAULT_WIN_FRACTION})",
    )
    ap.add_argument(
        "--verdict-majority",
        type=float,
        default=DEFAULT_VERDICT_MAJORITY,
        help=f"fraction of comparable cells needed for a campaign verdict "
        f"(default {DEFAULT_VERDICT_MAJORITY})",
    )
    ap.add_argument(
        "--max-nodata-fraction",
        type=float,
        default=DEFAULT_MAX_NODATA_FRACTION,
        help=f"above this fraction of non-comparable cells the verdict is INCONCLUSIVE "
        f"(default {DEFAULT_MAX_NODATA_FRACTION})",
    )
    ap.add_argument(
        "--noisy-spread",
        type=float,
        default=DEFAULT_NOISY_SPREAD,
        help=f"(t_p50-t_min)/t_min above which a record is named as noisy (default {DEFAULT_NOISY_SPREAD})",
    )
    ap.add_argument(
        "--headroom-factor",
        type=float,
        default=DEFAULT_HEADROOM_FACTOR,
        help=f"peak_fma / best GEMM above which standing order 1's headroom flag fires "
        f"(default {DEFAULT_HEADROOM_FACTOR})",
    )
    ap.add_argument("--v1-set", default=DEFAULT_V1_SET, help=f"kernel set A (default {DEFAULT_V1_SET})")
    ap.add_argument("--v2-set", default=DEFAULT_V2_SET, help=f"kernel set B (default {DEFAULT_V2_SET})")
    ap.add_argument(
        "--max-listed",
        type=int,
        default=DEFAULT_MAX_LISTED,
        help=f"cap on items printed per list (default {DEFAULT_MAX_LISTED})",
    )
    ap.add_argument(
        "--role",
        default=DEFAULT_ROLE,
        help=(
            f"analyse only records carrying this role (default {DEFAULT_ROLE}). "
            f"Instrument-check hosts are quarantined by construction; pass --role instrument "
            f"to look at them deliberately, never to pool them."
        ),
    )
    ap.add_argument("--json", type=pathlib.Path, default=None, help="write the machine-readable report here")
    return ap.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    inp = load(args.results, role=args.role)
    if not inp.bench:
        print(f"no benchmark records found under {args.results}", file=sys.stderr)
        # Still write the report. Returning before this made exit code 1 the one
        # exit code no gate could assert on -- gates/p1.sh treats a missing report
        # as a scenario failure, so "nothing loaded" and "the analysis crashed"
        # were indistinguishable to the only consumer that matters. The payload is
        # deliberately the same schema with empty sections rather than a special
        # shape, so a caller can read exit_code without branching on the schema.
        if args.json:
            args.json.write_text(
                json.dumps(
                    {
                        "schema": "gbb-decompose/1",
                        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        "results_dir": str(args.results),
                        "inputs": {
                            "files": dict(sorted(inp.files.items())),
                            "missing_file_families": inp.missing_families,
                            "bench_records": 0,
                            "unparseable_lines": inp.bad_lines,
                            "foreign_roles": dict(inp.foreign_roles),
                        },
                        "verdict": {"code": "NO-DATA", "why": "no bench records were loaded"},
                        "exit_code": 1,
                    },
                    indent=2,
                    default=str,
                )
                + "\n"
            )
        return 1

    hosts = build_hosts(inp)
    for inst in {r.get("instance") for r in inp.bench}:
        hosts.setdefault(inst, Host(instance=inst))
    exc = Excluded()
    cells = build_cells(inp.bench, hosts, exc)
    explain, _manifest = build_absence(inp)

    lines = []
    out = lines.append
    run_ids = sorted({r.get("run_id") for r in inp.bench}, key=str)
    out(
        f"graviton-blas-bench decomposition — {len(inp.bench)} records, {len(cells)} cells, "
        f"{len(run_ids)} run_ids, {len(hosts)} instance types, {len(inp.envs)} env files"
    )
    out(
        f"min-effect floor {100 * args.min_effect:.0f}%, widened per cell to observed dispersion; "
        f"min-sizes {args.min_sizes}; win-fraction {args.win_fraction}"
    )
    out(f"run_ids: {', '.join(map(str, run_ids))}")

    host_payload = report_hosts(hosts, {r.get("instance") for r in inp.bench}, out)
    groups = cell_groups(cells)
    deficits = report_deficit_by_routine(cells, groups, hosts, explain, args, out)
    cross = report_target_cross(cells, groups, hosts, explain, inp, args, out)
    lda = report_lda_penalty(cells, hosts, args, out)
    regimes = report_regime_profile(deficits, cross, args, out)
    scaling = compute_scaling(cells, inp.roof, args)
    anomalies, coverage_table, unver_cells = report_anomalies(inp, cells, hosts, exc, scaling, args, out)
    report_scaling(scaling, out)
    coverage = report_coverage(cells, inp, explain, hosts, exc, args, out)
    replicates = report_replicates(inp, hosts, explain, args, out)
    verdict = compute_verdict(cross, hosts, args)

    exit_code = 0
    if exc.verified_false or exc.zero_gflops or exc.forced_coretype:
        exit_code |= 2
    if any(h.invalid or h.escalate for h in hosts.values() if h.present):
        exit_code |= 2
    if coverage["missing_unexplained"] or coverage["partial"]:
        # partial counts: a cell short of sizes with no exclusion of this file's
        # own accounting for the absence is the same hole one level down.
        exit_code |= 4
    if any(a["kind"] == "arch_selected_mismatch" for a in anomalies):
        exit_code |= 2
    if any(
        a["kind"] in ("no_provenance", "blas_sha_conflict", "env_unparseable", "sve_kernels_unknown")
        for a in anomalies
    ):
        exit_code |= 8
    if any(a["kind"] in ("escalation_acked", "role_excluded") for a in anomalies):
        # Both mean the dataset is not what the directory claims it is: a host the
        # provenance check refused, or two roles in one place. Same class as an
        # inadmissible host, so the same bit.
        exit_code |= 2
    if any(r["status"].startswith("DIVERGES") for r in replicates):
        # Its own bit. A non-reproducing headline is not a poisoned record, not a
        # coverage hole and not a provenance gap: every arm ran, every arm is
        # accounted for, and the two passes disagree anyway. Folding it into 2
        # would make "the data is unusable" and "the finding is not real" the same
        # signal to the gate.
        exit_code |= 16

    report_verdict(verdict, lda, regimes, coverage, anomalies, replicates, exit_code, args, out)
    print("\n".join(lines))

    if args.json:
        payload = {
            "schema": "gbb-decompose/1",
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "results_dir": str(args.results),
            "params": {
                "min_effect": args.min_effect,
                "min_sizes": args.min_sizes,
                "win_fraction": args.win_fraction,
                "verdict_majority": args.verdict_majority,
                "max_nodata_fraction": args.max_nodata_fraction,
                "noisy_spread": args.noisy_spread,
                "headroom_factor": args.headroom_factor,
                "v1_set": args.v1_set,
                "v2_set": args.v2_set,
                "max_listed": args.max_listed,
            },
            "inputs": {
                "files": dict(sorted(inp.files.items())),
                "missing_file_families": inp.missing_families,
                "bench_records": len(inp.bench),
                "cells": len(cells),
                "run_ids": run_ids,
                "unparseable_lines": inp.bad_lines,
                "unparseable_env_files": [{"file": n, "why": w} for n, w in inp.bad_env_files],
                "role": args.role,
                "foreign_roles": dict(inp.foreign_roles),
                "escalation_acks": len(inp.escalation_acks),
                "arms": sorted({arm_label(a) for _c, a in cells}),
                "excluded": {
                    "verified_false": len(exc.verified_false),
                    "zero_gflops": len(exc.zero_gflops),
                    "oversubscribed": exc.oversubscribed,
                    "forced_coretype_unavailable": exc.forced_coretype,
                    "no_gflops_field": exc.no_gflops,
                },
            },
            "hosts": host_payload,
            "verdict": verdict,
            "deficit_by_routine": deficits,
            "target_cross": cross,
            "lda_penalty": lda,
            "regime_profile": regimes,
            "scaling": scaling,
            "anomalies": {
                "count": len(anomalies),
                "hard": sum(1 for a in anomalies if a["severity"] == "!!"),
                "items": anomalies,
            },
            "verification_coverage": coverage_table,
            "unverified_cells": unver_cells,
            "coverage": coverage,
            "replicates": replicates,
            "exit_code": exit_code,
        }
        args.json.write_text(json.dumps(payload, indent=2, default=str) + "\n")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
