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
                      READ SECTION 9 FIRST: the MIN_SECONDS floor also steps at
                      n=256, so a step there has two candidate causes.
  5. anomalies        everything that should stop a conclusion.
  6. scaling          GFLOP/s vs threads against the measured all-core peak.
  7. coverage census  every expected cell classified. MISSING-UNEXPLAINED is the
                      one that matters: a hole nothing accounts for.
  8. replicates       P3 runs each host family three times, each pass a separate
                      launch on a different physical box. The passes are COMPARED,
                      never pooled: the whole point of the extra passes is whether
                      the first one reproduces, and a median across them would
                      convert the campaign's strongest evidence into slightly
                      tighter error bars. Three rather than two because the median
                      of two is the mean; three rejects one bad pass, which is
                      reported as REPRODUCES-MAJORITY.
  9. floor-overlap    the same DGEMM case measured at BOTH MIN_SECONDS floors.
                      The floor steps at n=256 and so, by hypothesis, does
                      GEMM_SMALL_*, so a step in section 4 is ambiguous between
                      the hardware and the instrument. This section settles it by
                      measurement. Numbered last only to leave 0-8 alone, since
                      CLAUDE.md and the gate text name those by number; it is
                      read before section 4, which says so in its own header.

  VERDICT             one machine-greppable line computed from the data.

EXIT CODES -- load-bearing, because gates/p1.sh has to be able to assert on
something. 2, 4, 8, 16 and 32 are bit flags and are OR-ed together; 1 and 64 are
returned alone.

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
 32  the timing-floor overlap band did not confirm that the two MIN_SECONDS floors
     agree: they measurably disagree, the differences track measurement order
     rather than the floor, or half a probe arrived. Section 4 cannot be read
     across n=256 in any of those states. The band being ABSENT does NOT set this
     -- pre-probe datasets have to keep analysing -- so requiring it is a gate's
     job and not this bit's
 64  more than one case matrix in one results directory. Returned ALONE and
     nothing else is computed, because sections 1-7 pool and section 8 compares:
     across two case matrices neither operation means anything, and the number it
     would produce looks like every other number in the report. The realistic way
     to get here is one `aws s3 sync` of a bucket holding a pre-expansion pass and
     a post-expansion one. bench.c stamps `matrix_id` by walking the same tables
     the sweep walks, so this fires on a one-line ladder change nobody announced

Usage:
    python3 decompose.py results/ [--min-effect 0.05] [--json out.json]
"""

import argparse
import json
import pathlib
import statistics
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from fractions import Fraction

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

# Sizes that must be comparable at identical (m,n,k,lda_pad,transa,transb) before
# a regime may carry a directional verdict. A verdict from one or two sizes is a
# size-specific anecdote, which is exactly how the old max()-over-the-regime bug
# produced its inversion.
#
# 3 is an ABSOLUTE count over a HOMOGENEOUS group -- one routine, one regime, one
# pad, one transpose, one stride -- so it is density-invariant and the sweep leaves
# it alone. Its justification used to read "MEDIUM and LARGE hold 5 sizes each and
# SMALL holds 10", which the #2 densification made false: bench.c now holds 16/10/5
# (SIZES_SMALL/MEDIUM/LARGE). The number is unchanged and the reason is restated
# rather than rescaled -- 3 sizes is still not one or two sizes -- but the stale
# arithmetic is exactly how a fraction-of-cells assumption hides inside a constant
# that looks absolute.
DEFAULT_MIN_SIZES = 3

# Fraction of the compared sizes that must agree in sign for a directional
# regime verdict. 0.6 because a median above the band with the sizes split near
# 50/50 is not a property of the kernel set.
DEFAULT_WIN_FRACTION = 0.60

# Fraction of comparable cells that must agree for a campaign-level verdict.
DEFAULT_VERDICT_MAJORITY = 0.60

# Majority comparisons are EXACT, in rational arithmetic, with no tolerance
# constant.
#
# Balanced weighting makes each group contribute one unit of weight *as a sum of
# reciprocals of integers* — a 24-cell group is 24 * (1/24) — so the quantity
# being compared is exactly rational and does not need a tolerance. In binary
# floating point it does: 24 * (1/24) is not 1.0, and a dataset that lands on the
# threshold by construction (five small-regime families, three of them one-sided:
# 3/5 = 0.60) had its verdict decided by summation order rather than by the data,
# with the two directions of one comparison able to disagree with each other. An
# epsilon covers that, but it is a tolerance around a number that does not need
# one, and a future weighting scheme could land on a boundary the epsilon happens
# not to cover. Fraction accumulates the weight with no ordering sensitivity.
#
# The THRESHOLD has to be converted too, and that half is not optional — making
# the left side exact is precisely what breaks a float threshold. Fraction >=
# float compares against the float's *exact* rational value, not against the
# rounded decimal it was written as, so the slack that float-vs-float arithmetic
# silently provided disappears. Measured: `Fraction(3, 5) >= 0.60` is True
# (0.60 as a double is 0.59999999999999997779..., just under), but
# `Fraction(11, 20) >= 0.55` is **False** and `Fraction(17, 50) >= 0.34` is
# **False**, because those two doubles round just over. A dataset sitting exactly
# on the threshold would fail it, which is the same class of bug as the epsilon,
# reintroduced from the other side. Fraction(str(x)) reads the decimal that was
# actually written — repr() is the shortest decimal that round-trips, so
# Fraction(str(0.55)) is exactly 11/20 — and the comparison is then between the
# policy as written and the data as measured, with no binary in between.


def as_exact(threshold: float) -> Fraction:
    """A threshold as the exact decimal it was written as. See above."""
    return Fraction(str(threshold))


def majority_met(part: Fraction, total: Fraction, threshold: float) -> bool:
    """Exact `part / total >= threshold`. Zero total is never a majority: it
    means nothing was comparable, which the callers report separately."""
    return bool(total) and part / total >= as_exact(threshold)


def balanced_weights(cells):
    """Per-cell weight under the campaign's ONE weighting rule.

    `cells` is a sequence of (routine_family, routine, regime) triples, one per
    cell. Returns a list of Fractions parallel to it. The total is exactly the
    number of distinct (family, regime) groups present, because that is the whole
    construction: **one unit of weight per (family, regime) group, divided evenly
    among the ROUTINES in the group, and each routine's share divided evenly among
    its own cells.**

    Every fraction-of-cells quantity in this file that decides anything runs
    through here, and that is the point. The alternative -- each site deriving its
    own denominator from whatever it happened to have counted -- is the defect
    class this function exists to close, and that class has now appeared four
    times: raw cells in coherent_subsets (C11), raw cells on the regime axis in
    compute_verdict, a raw --max-nodata-fraction, and an unweighted median as the
    effect-size floor. Every one of them was latent until src/bench.c's ladder
    moved, and the #2 expansion moves it twice more (transposes multiply dgemm's
    cells by four, complex again after that). A quantity defined as a fraction of
    cells is a quantity that means something different after every expansion.

    Three layers, each with a reason:

      family   dgemm/sgemm/zgemm/cgemm are one family, so adding complex types
               cannot multiply GEMM's vote. See routine_family().
      regime   small/medium/large each carry one unit per family. bench.c's
               ladders are 16/10/5 sizes and the pad axis is 5 values below the
               large regime against 2 within it, so small+medium hold ~93% of
               every padded routine's cells: on raw counts an effect confined to
               the large regime -- where the DDR generation and the L3 step show,
               and where the campaign's memory-side finding would live -- cannot
               reach a 60% majority no matter how large it is.
      routine  a routine's cells are its own. Without this layer the family's unit
               splits by cell count *inside* the family, so dgemm (5 pads) already
               holds 5/6 of the gemm unit against sgemm's 1/6, and after the
               transposes land it holds ~20/21. That makes an sgemm-localised
               effect unreachable and a dgemm-localised one self-certifying --
               the family layer's own argument, one level down.

    A pad, a transpose and an incx are NOT layers: they are the same kernel-set
    claim re-asked at a different alignment or a different operand orientation, so
    they divide the routine's share rather than adding units of their own. That is
    the axis-assignment policy in CLAUDE.md, and it is why densifying those axes
    is weight-neutral here.
    """
    routines = defaultdict(set)
    per_routine = defaultdict(int)
    for fam, routine, reg in cells:
        routines[(fam, reg)].add(routine)
        per_routine[(fam, reg, routine)] += 1
    return [
        Fraction(1, len(routines[(fam, reg)]) * per_routine[(fam, reg, routine)])
        for fam, routine, reg in cells
    ]


def weighted_median(values, weights):
    """The value at which cumulative weight first reaches half the total.

    Used for the effect-size floor, and it has to be weighted for the same reason
    the majority does. The directional branch asks two questions -- "how much of
    the experiment moved" (the balanced majority) and "did the experiment move"
    (this) -- and it was asking the first on balanced weight and the second on raw
    cells. So the floor was decided by whichever routine had the longest ladder:
    dgemm carries five pads today and four transposes after item 3, at which point
    ~80% of the deltas in an unweighted median are dgemm's. A verdict whose two
    halves disagree about what a cell is worth is not a verdict about the hardware.

    Weights are exact Fractions, so the half-total comparison is exact and the
    ordering of equal-value cells cannot move the answer.
    """
    pairs = sorted(zip(values, weights, strict=True), key=lambda vw: vw[0])
    total = sum(weights, Fraction(0))
    if not pairs or not total:
        return None
    half = total / 2
    acc = Fraction(0)
    for v, w in pairs:
        acc += w
        if acc >= half:
            return v
    return pairs[-1][0]


# Comparable cells a single axis value (one routine, one regime, one instance)
# must hold before it is allowed to block the NULL branch. 3 for the same reason
# as DEFAULT_MIN_SIZES: two cells agreeing is not a localised effect.
#
# Deliberately an ABSOLUTE count, not a fraction, and it therefore survives the
# density sweep unchanged: it is a floor on how much evidence exists, not on what
# share of the design that evidence is. The share is already guarded, and by
# construction -- under balanced_weights() a qualifying subset carries at least
# --verdict-majority of at least one full group-unit -- so the two guards are not
# two spellings of one thing. What does change with density is the stringency
# ratio: 3 cells was 3-of-20 and is now 3-of-~80. That is the accepted cost of an
# absolute count, and the alternative (a fraction of the axis value's cells) is
# the defect class itself.
DEFAULT_SUBSET_MIN_CELLS = 3

# If more than this share of the target cross is not comparable (missing arm,
# thin, unequal N, inadmissible host), the campaign verdict is INCONCLUSIVE rather
# than directional: with a third of the design absent the sign of the aggregate is
# decided by which cells happened to survive.
#
# The share is BALANCED WEIGHT, not raw cells, and 0.34 is unchanged from when it
# was a raw fraction on purpose. Retuning it would have hidden what it revealed:
# the #2 densification took dgemm's total exclusion for wrong answers from 40% of
# the cross to 29% -- under the threshold -- without a single measurement
# changing, purely because the small ladder went from 10 sizes to 16. The number
# was never wrong; the denominator was.
DEFAULT_MAX_NODATA_FRACTION = 0.34

# The two kernel sets under test, as they appear in TARGET= and in
# OPENBLAS_CORETYPE. Parameters, not literals, because 0.3.32 maps V3 onto the
# V2 target and the campaign may need to name a different pair.
DEFAULT_V1_SET = "NEOVERSEV1"
DEFAULT_V2_SET = "NEOVERSEV2"

# SVE kernel counts per kernel set, counted in the OpenBLAS tree (KERNEL.<set>
# and the includes it pulls in), not measured here. The section 2 header used to
# hardcode the V1/V2 pair's counts and print them whatever --v1-set/--v2-set
# said, which quietly attributed 99 kernels to ARMV8SVE the moment the sharper
# ARMV8SVE-vs-NEOVERSEV2 cross was run. A set that was never counted gets no
# number claimed for it: an unlabelled axis is recoverable, a wrong count is not.
SVE_KERNEL_SETS = {
    "NEOVERSEV1": "NEOVERSEV1 = 99 SVE kernels",
    "ARMV8SVE": "ARMV8SVE = 94 SVE kernels (where an unrecognised SVE part lands)",
    "NEOVERSEN2": "NEOVERSEN2 = 5 SVE kernels",
    "NEOVERSEV2": "NEOVERSEV2 -> KERNEL.NEOVERSEN2 = 5 SVE kernels",
    "ARMV8": "ARMV8 = 0 SVE kernels",
    "NEOVERSEN1": "NEOVERSEN1 = 0 SVE kernels",
}

# Independent passes the spend policy buys per instance_type. Three, because the
# median of two is the mean: one bad pass moves it and nothing says which pass
# was bad. Reported, and asserted by gates/p3.sh; never an exit bit here, since a
# one-pass P2 dataset is legitimately short of it.
DEFAULT_REPLICATE_PASSES = 3

# Cap on how many individual items any one list in the report prints.
DEFAULT_MAX_LISTED = 20

# Token capture-env.sh emits for a MIDR part that is not in OpenBLAS's dispatch
# switch, and the substring of the warning that says the lscpu-derived topology
# fields were defaulted rather than measured. Both are contracts with that
# script; capture-env.sh says so at the emitting site. Do not reword either.
UNRECOGNISED = "UNRECOGNISED"
LSCPU_DEFAULTED = "lscpu produced no topology"

# Census statuses that mean the arm ran, and therefore explain NOTHING about a
# cell it failed to produce. run-matrix.sh emits ten:
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
#   harness_invalid      explanation, BINARY-level: bench.c's dry pass found a
#                        routine in a sweep list that sweep() cannot dispatch and
#                        refused (rc=5). Identical on every arm and every host, so
#                        a dataset carrying it is not a host with a hole -- it is
#                        a build that must not be believed at all
#
# `harness_invalid` is the one that must not be read as a per-arm condition. It is
# a property of the binary, so it appears on the FIRST arm and would appear on all
# of them; seeing one and concluding "that arm is unlucky" is the wrong read. It
# exists as its own status only so the record does not carry `runtime_failed`'s
# SIGILL hint, which would send someone auditing the ISA of a host that is fine.
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


def canon_trans(v):
    """BLAS transpose flag, upper-cased, defaulting to N.

    Absent means N: every record written before bench.c carried the field came
    from a sweep that only ever issued "N","N". Defaulting rather than
    None-propagating is what lets the comparison key be extended before the
    producer emits the fields, without splitting old data into two cells."""
    if v is None:
        return "N"
    s = str(v).strip().upper()
    return s if s in ("N", "T", "C") else "N"


# The single MIN_SECONDS floor in force before src/bench.c floored it per regime. A
# record with no min_seconds field was measured under that one floor, so that is
# what absent has to mean -- see canon_floor(). Copied from bench.c's MIN_SECONDS
# and cross-checked against it by gates/p1.sh, the same way the size ladders are,
# so the copy cannot rot silently.
LEGACY_MIN_SECONDS = 0.300


def canon_floor(v):
    """The MIN_SECONDS floor a record was measured under, as a stable key.

    This is part of the comparison condition, for the same reason transa/transb
    are. The floor sets how much work each measurement averages over, so the same
    (routine, size) taken at 0.05 s and at 0.30 s are two measurements with
    different noise characteristics, not two samples of one -- and the campaign is
    about to produce exactly that pair on purpose. The overlap band (n=192..384 at
    both floors, once) exists to show that the step at n=256 is the GEMM_SMALL_*
    crossover and not the floor changing underneath it. Without the floor in the
    key those pairs collapse into one cell, and min-within-run keeps whichever
    floor happened to look worse -- so the band designed to rule out an instrument
    artefact would be read through one. That is the max()-over-the-cell defect in
    its fourth shape, closed before the data exists rather than after.

    The probe records are kept out of the main cross by their own tag rather than
    by this key (see split_floor_probe()); this is the fail-safe under it.

    Quantised to the 3 decimals bench.c prints (%.3f), so the key is the number as
    written and not a float that may or may not compare equal. Absent means
    LEGACY_MIN_SECONDS: defaulting rather than None-propagating is what lets the
    key be extended without splitting pre-per-regime data into two cells."""
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        return f"{LEGACY_MIN_SECONDS:.3f}"
    return f"{float(v):.3f}"


NO_PROBE = "none"


def canon_probe(v):
    """Which probe produced a record, defaulting to NO_PROBE.

    Absent means a matrix record, which is what every record written before
    bench.c grew the field is. Defaulting rather than treating absence as unknown
    matters here: the default is the value that puts a record INTO the cross, so
    old data reads exactly as it used to, and the only records that get partitioned
    out are ones that positively asked to be."""
    s = str(v).strip() if isinstance(v, str) else ""
    return s if s else NO_PROBE


UNSTAMPED_MATRIX = "unstamped"


def canon_matrix_id(v):
    """Which case matrix a record came from, defaulting to UNSTAMPED_MATRIX.

    bench.c computes this by walking the same tables the sweep walks, so it changes
    whenever the case set changes -- a size, a pad, an incx, a routine, or a regime
    floor -- and it changes whether or not anyone remembered it should. See
    `matrix_ids()` for what is done with it, and g_matrix_id in src/bench.c for why
    it is a digest and not a version number.

    Absent means a record written before the field existed. Defaulting to a single
    shared sentinel is what lets an old dataset keep analysing: every one of its
    records agrees with every other, so it is one matrix as far as the pooling rule
    is concerned. It is NOT treated as "matches anything" -- a dataset mixing
    stamped and unstamped records is refused, because whether they are the same
    matrix is precisely what is unknown."""
    s = str(v).strip() if isinstance(v, str) else ""
    return s if s else UNSTAMPED_MATRIX


def matrix_ids(bench):
    """matrix_id -> {"records": n, "cases": {...}, "runs": {...}, "instances": {...}}.

    WHY MORE THAN ONE IS A REFUSAL RATHER THAN A WARNING. Sections 1-7 pool by
    median across passes, and a comparison restricted to two different case matrices
    is not a comparison: cells present in one and absent in the other silently drop
    out of the intersection, and the ones that survive are whatever the two matrices
    happen to share. The result is a number that looks like every other number in
    the report and means something else. The realistic way to get there is one
    `aws s3 sync` of a bucket holding a pre-expansion P2 pass and a post-expansion
    P3 pass -- which is a directory a careful operator produces on purpose.

    So a mixed directory stops the analysis. Not by excluding a minority, which
    would pick a winner without being asked, and not by warning, which puts a
    correct-looking report in front of someone who now has to notice a line. The
    breakdown is printed by id with its run and instance ids, because "which pass
    is the odd one out" is the next question and nothing else in the tree can
    answer it.

    This also subsumes the replicate rule: a pass cannot be counted as a replicate
    of a differently-stamped pass, because the two never reach section 8 together."""
    out = defaultdict(lambda: {"records": 0, "cases": set(), "runs": set(), "instances": set()})
    for r in bench:
        e = out[canon_matrix_id(r.get("matrix_id"))]
        e["records"] += 1
        c = r.get("matrix_cases")
        if isinstance(c, int) and not isinstance(c, bool):
            e["cases"].add(c)
        e["runs"].add(r.get("run_id"))
        e["instances"].add(r.get("instance"))
    return {
        k: {
            "records": v["records"],
            "cases": sorted(v["cases"]),
            "runs": sorted(x for x in v["runs"] if x),
            "instances": sorted(x for x in v["instances"] if x),
        }
        for k, v in out.items()
    }


# Routine families for verdict weighting. The family is the routine name minus
# its precision prefix, so dgemm/sgemm/zgemm/cgemm are one family and adding
# complex types cannot multiply GEMM's weight in the census. sbgemm -> bgemm
# stays its own family on purpose: bf16 GEMM is a different kernel, not another
# precision of the same one.
_PRECISION_PREFIXES = ("s", "d", "c", "z")


def routine_family(routine):
    r = (str(routine) if routine is not None else "").strip().lower()
    if not r:
        return "unknown"
    if r.startswith("sb"):
        return r[1:]
    if r[0] in _PRECISION_PREFIXES and len(r) > 1:
        return r[1:]
    return r


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
    # (anomaly_kind, message) pairs. It held bare strings and section 5 stamped
    # every one of them `sve_kernels_unknown`, which was true of the only producer
    # at the time and would have mislabelled the second one.
    provenance_gaps: list = field(default_factory=list)
    cpus_affinity: int | None = None
    forcing: str = "not_probed"

    @property
    def admissible(self) -> bool:
        return self.present and not self.invalid and not self.escalate


def _int_or_none(v):
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def dynamic_build_absent(inp: Inputs) -> set:
    """instance_types where there was no openblas/DYNAMIC build for the probe to read.

    The bit-8 guard. `openblas_dynamic_probe_status` covers two situations that
    must not share an exit bit:

      * the probe SHOULD have run and did not -- GBB_OPENBLAS_DYNAMIC_DIR unset by
        a harness slip, the directory gone, the probe binary failing to link
        against a library that is right there. That is a real provenance hole:
        the as-shipped arm is the highest-leverage part of this campaign and
        nothing here can say what DYNAMIC_ARCH selected. Bit 8 is for this.
      * there was no DYNAMIC build at all, because it failed. Then the missing
        selection is entirely accounted for by a census record that already
        carries the failure and its reason, and section 7 reports it as an
        explained absence. Firing bit 8 here reports one failure twice and makes
        the bit routine -- and a bit that fires routinely is a bit people stop
        reading, which costs the bit entirely.

    Instance-scoped from both producers. run-matrix.sh stamps `instance` onto
    build-libs.sh's manifest lines and censuses a not-built arm once per thread
    count, so both sources are attributable to a host; the analysis concatenates
    every host's files into one stream, so an unscoped test would let one host's
    failed build suppress the gap on the other four. `present` wins any conflict:
    if anything says the build was there, the probe should have run.
    """
    absent, present = set(), set()
    for m in inp.manifest_arms:
        if (m.get("library"), m.get("target")) != ("openblas", "DYNAMIC"):
            continue
        (present if m.get("built") else absent).add(m.get("instance") or "unknown")
    for o in inp.outcomes:
        if (o.get("library"), o.get("target")) != ("openblas", "DYNAMIC"):
            continue
        inst = o.get("instance") or "unknown"
        (absent if (o.get("status") or "") == "build_failed" else present).add(inst)
    return absent - present


def build_hosts(inp: Inputs) -> dict:
    """One Host per instance TYPE -- that is what `instance` is in the records.
    Two hosts of the same type merge, and the merge is pessimistic: any
    invalidating fact on either invalidates the type for this dataset."""
    hosts: dict[str, Host] = {}
    no_dynamic_build = dynamic_build_absent(inp)
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
        if status != "ok" and inst in no_dynamic_build:
            # Structurally inapplicable: no DYNAMIC build existed, so there was
            # nothing to probe. Recorded, but not a provenance gap -- see
            # dynamic_build_absent() for why this must not reach bit 8.
            h.notes.append(
                f"{who}: openblas_dynamic_probe_status={status!r} and the openblas/DYNAMIC build "
                f"failed on this host, so there was no DYNAMIC_ARCH library to probe. The absent "
                f"selection is accounted for by that build failure, which section 7 reports as an "
                f"explained absence; it is not a separate provenance gap."
            )
        elif status != "ok":
            # A provenance gap, not a note, and for the same reason
            # `sve_kernels:unknown` is one: this is the OTHER way the standing-order-8
            # check can fail to happen, and the two were being treated differently
            # for no reason anyone could state. `not_attempted`, `build_failed` and
            # `run_failed` all mean the generic-ARMV8 check did not run, on the
            # campaign's central hardware axis, and a list of notes nobody greps is
            # the wrong place for that. run-matrix.sh exports
            # GBB_OPENBLAS_DYNAMIC_DIR before capture-env.sh runs, so `ok` is the
            # normal case on a host whose DYNAMIC build succeeded -- this does not
            # fire on healthy data. Guarded above by whether the build existed at
            # all: the bit must fire when the probe should have run and did not,
            # never when it could not have run.
            h.provenance_gaps.append(
                (
                    "dynamic_probe_unavailable",
                    f"{who}: openblas_dynamic_probe_status={status!r}; the standing-order-8 "
                    f"generic-ARMV8 check was NOT performed on this host, and the openblas/DYNAMIC "
                    f"build is not recorded as having failed. Absent evidence about what "
                    f"DYNAMIC_ARCH selected is not evidence that it selected correctly, and "
                    f"standing order 5 says a number without provenance is not admissible.",
                )
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
        if not m.get("built") or not m.get("runnable", True):
            # The same guard as the DYNAMIC probe's, and needed for the same
            # reason: build-libs.sh's sve_kernels() prints `unknown` when there is
            # no archive to read, and a build that failed leaves no archive. So
            # every failed OpenBLAS build on an SVE host used to raise a
            # provenance gap and set bit 8 -- telling the reader to re-run with nm
            # available when nm was fine and the build simply failed. There is
            # also nothing to poison: an arm that never built never ran, so no
            # number is mislabelled. The census already carries the failure and
            # its reason, and section 7 reports it as an explained absence.
            h.notes.append(
                f"{inst}: {m.get('library')}/{m.get('target')} reports sve_kernels={sve!r}, but the "
                f"arm was not built/runnable — there was no archive to read. Explained by the build "
                f"outcome (section 7), not a standing-order-8 finding: the arm produced no numbers."
            )
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
                (
                    "sve_kernels_unknown",
                    f"{inst}: whether the {m.get('library')}/{m.get('target')} build contains SVE "
                    f"kernel symbols is UNKNOWN -- build-libs.sh could not read the archive. On a "
                    f"host that reports SVE this leaves standing order 8's quiet trigger unchecked; "
                    f"re-run build-libs.sh with nm available before trusting any SVE-coretype arm.",
                )
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
class Sample:
    """One pass's representative for one condition. Kept per run_id rather than
    flattened into a list of numbers so a comparison can be restricted to the
    passes both its arms actually have -- see Cell.restrict()."""

    run_id: str = ""
    value: float = 0.0
    spread: float = None
    verified: bool = True


@dataclass
class Cell:
    samples: list = field(default_factory=list)  # one Sample per run_id
    notes: set = field(default_factory=set)

    @property
    def values(self):
        return [s.value for s in self.samples]

    @property
    def runs(self):
        return [s.run_id for s in self.samples]

    @property
    def all_verified(self):
        return all(s.verified for s in self.samples)

    @property
    def value(self):
        return statistics.median(self.values)

    @property
    def n_runs(self):
        return len(self.samples)

    @property
    def within_spread(self):
        sp = [s.spread for s in self.samples if s.spread is not None]
        return statistics.median(sp) if sp else 0.0

    @property
    def run_spread(self):
        vals = self.values
        if len(vals) < 2:
            return 0.0
        m = statistics.median(vals)
        return (max(vals) - min(vals)) / m if m else 0.0

    def restrict(self, run_ids):
        """This cell as measured on `run_ids` only. Equal N within a comparison
        is what the aggregation requires; equal N across the whole dataset is
        stronger than the requirement, and refusing on it let one arm lost on one
        of three passes turn every pooled cell non-comparable."""
        c = Cell(samples=[s for s in self.samples if s.run_id in run_ids], notes=set(self.notes))
        return c


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
        # transa/transb are part of the condition for the same reason incx is, and
        # the reason is sharper: NN and TN route the A operand through different
        # packing kernels (gemm_ncopy_* vs gemm_tcopy_*), so a comparison that
        # spans them lets each arm be represented by its favourite transpose --
        # the max()-over-the-cell defect in a new shape. Records written before
        # bench.c carried the fields default to "N"/"N", which is what the
        # single-transpose sweep measured and merges old data unchanged.
        cond = (
            inst,
            r.get("threads"),
            r.get("routine"),
            r.get("m"),
            r.get("n"),
            r.get("k"),
            r.get("lda_pad"),
            r.get("incx", 1),
            canon_trans(r.get("transa")),
            canon_trans(r.get("transb")),
            canon_floor(r.get("min_seconds")),
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
            tmin, tp50 = rec.get("t_min"), rec.get("t_p50")
            spread = None
            if isinstance(tmin, (int, float)) and isinstance(tp50, (int, float)) and tmin > 0:
                spread = (tp50 - tmin) / tmin
            c.samples.append(
                Sample(
                    run_id=run_id,
                    value=rec["gflops"],
                    spread=spread,
                    verified=rec.get("verified") is True,
                )
            )
            if rec.get("note"):
                c.notes.add(rec["note"])
        cells[key] = c
    return cells


FLOOR_PROBE = "floor-overlap"


def split_floor_probe(bench):
    """Partition bench records into (matrix, probe) on the `probe` field.

    This must happen before build_cells(), and the reason is the same one that put
    the floor in the comparison key -- stated once here and once there because
    either alone would look like belt-and-braces.

    A floor-overlap record is the same (routine, m, n, k, lda_pad, incx, transa,
    transb) as a record the matrix already holds; only min_seconds differs. So it is
    a SECOND MEASUREMENT OF AN EXISTING CONDITION, which is precisely the shape the
    min-within-run rule exists to defend against -- except that here the rule would
    do the wrong thing in a new way. Min-within-run is right when two records are
    two samples of one measurement (keep the unluckier, do not let a lucky sample
    stand). These two are not samples of one measurement: they are one measurement
    at each of two averaging windows, and taking the min of the pair would report
    the shorter window's number whenever the shorter window reads low -- which is
    the very bias the band was built to detect. The probe would then be invisible
    inside the thing it is measuring.

    Tag-based rather than floor-based because the tag says WHY the record exists.
    The floor in the key stops the collision either way, but it would leave the
    probe's ten dgemm records sitting in the cross as extra thin rows at n=192..384,
    tugging the regime profile with measurements taken for a different purpose. The
    key is the fail-safe; this is the mechanism.

    Returns two lists rather than filtering in place so the caller can report on
    the probe. A record whose probe field names something this version of the
    analysis does not know still lands in `probe`, not in the matrix: an unknown
    probe is not a matrix record, and admitting it would be the fail-open
    direction."""
    matrix, probe = [], []
    for r in bench:
        (matrix if canon_probe(r.get("probe")) == NO_PROBE else probe).append(r)
    return matrix, probe


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
    passes_used: int = 0
    passes_avail: int = 0
    # (run_id, arm) pairs whose absence from this condition nothing accounts for.
    unexplained: tuple = ()


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


def per_size(cells, conds, arm_a, arm_b, min_effect, pass_explain=None):
    """Signed deltas of arm_a against arm_b at identical conditions only.

    Each comparison is restricted to the passes both arms have -- a paired design,
    which is the standard fix for unequal N and is weaker than the global equal-N
    rule this replaces. Global equal-N was refusing far more than the arithmetic
    requires: one arm lost on one of three passes made every pooled cell
    non-comparable, so a headline two passes agreed on read INCONCLUSIVE.

    Two limits are kept deliberately. The intersection is only taken when the
    census explains why the arm is missing from that pass; an unexplained loss is
    still `unequal-N-unexplained` and still refuses a direction, because
    intersecting over a hole nobody can account for is how a truncated sweep gets
    published as a clean one. And an intersection down to two passes is recorded
    as under-replicated: the median of two is the mean, which is the thing three
    passes were bought to prevent, so such a comparison may contribute a number
    but may not carry the headline unqualified."""
    rows = []
    for cond in conds:
        ca = cells.get((cond, arm_a))
        cb = cells.get((cond, arm_b))
        if ca is None or cb is None:
            continue
        ra, rb = set(ca.runs), set(cb.runs)
        used, avail = ra & rb, ra | rb
        unexplained = []
        if ra != rb:
            for rid in sorted(avail - used, key=str):
                missing = arm_b if rid in ra else arm_a
                hit = pass_explain(cond[0], rid, missing, cond[1]) if pass_explain else None
                if hit is None:
                    unexplained.append((str(rid), arm_label(missing)))
            ca, cb = ca.restrict(used), cb.restrict(used)
        if not used:
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
                passes_used=len(used),
                passes_avail=len(avail),
                unexplained=tuple(unexplained),
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
            "passes_used": 0,
            "passes_avail": 0,
            "under_replicated": False,
            "unexplained_passes": [],
        }
    deltas = [r.delta for r in rows]
    band = statistics.median([r.band for r in rows])
    med = statistics.median(deltas)
    n_a = sum(1 for r in rows if r.delta > r.band)
    n_b = sum(1 for r in rows if r.delta < -r.band)
    runs_a = {r.runs_a for r in rows}
    runs_b = {r.runs_b for r in rows}
    # After per_size()'s intersection the two arms of every row hold the same
    # passes, so what is left to report is how many passes each row could use and
    # whether any pass went missing without a reason.
    unexplained = sorted({u for r in rows for u in r.unexplained})
    used = min(r.passes_used for r in rows)
    avail = max(r.passes_avail for r in rows)
    under = used < avail
    unequal = under or bool(unexplained)
    verified = all(r.verified for r in rows)
    n = len(rows)

    if n < args.min_sizes:
        verdict = f"inconclusive(thin:{n}<{args.min_sizes})"
    elif abs(med) <= band:
        verdict = "parity"
    elif unexplained:
        # An arm missing from a pass with nothing in the census accounting for it.
        # Intersecting here would silently compare whatever survived, so this
        # keeps the old refusal: absent-for-a-stated-reason and absent-for-no-
        # reason are different claims, and only the first is poolable.
        who = ", ".join(f"{rid}:{a}" for rid, a in unexplained[: args.max_listed])
        verdict = f"inconclusive(unequal-N-unexplained:{who})"
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
        "passes_used": used,
        "passes_avail": avail,
        "under_replicated": under,
        "unexplained_passes": [f"{rid}:{a}" for rid, a in unexplained],
    }


def vflag(s):
    return "" if s["verified"] else "  UNVERIFIED"


def under_flag(s):
    """Printed on every row that used fewer passes than were available, so a
    2-of-3 comparison is never visually equal to a 3-of-3 one."""
    return "  UNDER-REPLICATED" if s.get("under_replicated") else ""


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

    # Per-pass, which explain() cannot be: it keys on (instance, arm) and the
    # passes of one instance_type share both, so the last pass loaded wins. An
    # arm that ran on two passes and died on the third is exactly the case the
    # intersection rule has to decide, and deciding it needs the run_id.
    by_pass = {}
    for o in inp.outcomes:
        st = o.get("status") or "unknown"
        if st in CENSUS_SUCCESS:
            continue
        arm = (o.get("library"), o.get("target"), canon_coretype(o.get("coretype")))
        hit = (st, o.get("reason") or "no reason recorded")
        by_pass.setdefault((o.get("instance"), o.get("run_id"), arm, o.get("threads")), hit)
        by_pass.setdefault((o.get("instance"), o.get("run_id"), arm), hit)

    def pass_explain(instance, run_id, arm, threads=None):
        """Why this arm produced nothing on this one pass, or None if nothing in
        the census accounts for it. None is load-bearing: it is what keeps an
        unexplained hole out of the intersection."""
        arm = (arm[0], arm[1], canon_coretype(arm[2]))
        if threads is not None:
            hit = by_pass.get((instance, run_id, arm, threads))
            if hit:
                return hit
        return by_pass.get((instance, run_id, arm))

    return explain, manifest, pass_explain


# ---- 0. hosts --------------------------------------------------------------


def report_hosts(hosts, bench_instances, mids, out):
    out("\n" + "=" * 78)
    out("0. HOSTS  — provenance and admissibility (standing order 5)")
    out("=" * 78)
    # One line, always, even though more than one id is impossible here -- main()
    # refuses before this runs. A report that does not say which case matrix it
    # describes is a report whose numbers cannot be compared to another report's,
    # and the campaign's output is three passes that get compared.
    for mid, e in sorted(mids.items(), key=lambda kv: -kv[1]["records"]):
        cases = ", ".join(str(c) for c in e["cases"]) or "unrecorded"
        out(f"  matrix_id={mid} cases={cases} ({e['records']} records)")
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
    """(instance, threads, routine, regime, lda_pad, incx, transa, transb)
    -> {arm: [conditions]}

    lda_pad and incx are in the key, not just in the condition. bench.c puts both
    leading dimensions into one regime and both element strides into one routine,
    and a median taken across a mix of them is a statement about neither -- the
    same conflation as the max()-over-the-cell bug, one level up. Section 3 is
    where the two leading dimensions are compared against each other. The
    transposes are in the key on the same argument: NN and TN exercise different
    packing kernels, so their median is a statement about neither.

    The timing floor is in the key for the same reason, as a fail-safe. Under the
    per-regime floor it partitions nothing -- the floor is a function of the regime,
    so every cell in a group already shares one -- and the overlap-band probe is
    kept out of this cross by its own tag rather than by this key (see
    split_floor_probe()). But if a probe record ever arrives untagged, the floor
    being here turns "medianed together with real data, silently" into "an extra
    thin row, visibly", and that is the direction to fail in. Appended last so the
    existing k[0..7] positions are unchanged."""
    g = defaultdict(lambda: defaultdict(list))
    for cond, arm in cells:
        inst, thr, routine, m, pad, incx = cond[0], cond[1], cond[2], cond[3], cond[6], cond[7]
        ta, tb, floor = cond[8], cond[9], cond[10]
        g[(inst, thr, routine, regime(m or 0), pad, incx, ta, tb, floor)][arm].append(cond)
    return g


def report_deficit_by_routine(cells, groups, hosts, explain, pass_explain, args, out):
    out("\n" + "=" * 78)
    out("1. DEFICIT BY ROUTINE  — each named OpenBLAS arm vs a named reference arm")
    out("=" * 78)
    out("Signed: + = OpenBLAS behind the reference, - = OpenBLAS ahead. Compared size by")
    out("size at identical (m,n,k,lda_pad,transa,transb); the regime line is the median.")
    out("SHIPPED marks openblas/DYNAMIC/unforced, which is what NumPy wheels actually run.")
    out("passes=UofA: U passes carry both arms of A available. U<A is under-replicated.")

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
            _, thr, routine, reg, pad, incx, ta, tb, floor = k
            present_refs = [a for a in arms if a in refs]
            if not present_refs:
                for arm in sorted((a for a in arms if a[0] == "openblas"), key=arm_label):
                    st, why = explain(inst, ("armpl", "native", "unforced"), thr)
                    out(
                        f"  {inst!s:14s} t={thr!s:<4} {routine!s:6s} {reg:6s} pad={pad!s:<3} "
                        f"incx={incx!s:<2} tr={ta}{tb} "
                        f"{arm_label(arm):30s} NO DATA — reference arm absent "
                        f"({st}: {why or 'no reason recorded'})"
                    )
                continue
            # Reference arm: the non-OpenBLAS arm covering the most conditions
            # here, named in every row. Never a max() over anonymous arms.
            ref = max(present_refs, key=lambda a: (len(arms[a]), arm_label(a)))
            for arm in sorted((a for a in arms if a[0] == "openblas"), key=arm_label):
                rows = per_size(cells, arms[arm], arm, ref, args.min_effect, pass_explain)
                s = summarise(rows, args, "openblas", arm_label(ref))
                # Deficit is the reference-relative shortfall: negate the signed
                # delta of openblas against the reference.
                med = None if s["median_delta"] is None else -s["median_delta"]
                mark = " SHIPPED" if is_shipped(arm) else ""
                out(
                    f"  {inst!s:14s} t={thr!s:<4} {routine!s:6s} {reg:6s} pad={pad!s:<3} "
                    f"incx={incx!s:<2} tr={ta}{tb} "
                    f"{arm_label(arm):30s} "
                    f"ob={fmt_val(s['mean_a'])} ref={fmt_val(s['mean_b'])} ({arm_label(ref)}) "
                    f"deficit={fmt_pct(med)} band={100 * s['band']:4.1f}% "
                    f"n={s['n_sizes']:<2} passes={s['passes_used']}of{s['passes_avail']}"
                    f"{under_flag(s)}{mark}{vflag(s)}{tag}"
                )
                payload.append(
                    {
                        "instance": inst,
                        "threads": thr,
                        "routine": routine,
                        "routine_family": routine_family(routine),
                        "regime": reg,
                        "lda_pad": pad,
                        "incx": incx,
                        "transa": ta,
                        "transb": tb,
                        "min_seconds": floor,
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
                        "passes_used": s["passes_used"],
                        "passes_available": s["passes_avail"],
                        "under_replicated": s["under_replicated"],
                        "unexplained_passes": s["unexplained_passes"],
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


def kernel_set_note(name):
    """What is known about a kernel set's SVE kernel count, or that nothing is."""
    return SVE_KERNEL_SETS.get(name, f"{name} = SVE kernel count not recorded in this file")


def report_target_cross(cells, groups, hosts, explain, pass_explain, inp, args, out):
    out("\n" + "=" * 78)
    out("2. TARGET CROSS  — same hardware, different OpenBLAS kernel set")
    out("=" * 78)
    out(f"{kernel_set_note(args.v1_set)}.  {kernel_set_note(args.v2_set)}.")
    out("Signed delta of the V1 set against the V2 set, per size, at identical")
    out("(m,n,k,lda_pad,transa,transb). Positive = V1 set ahead. TARGET= and")
    out("OPENBLAS_CORETYPE are compared separately: they are different claims.")
    out("passes=UofA: the comparison used U of the A passes; U<A is UNDER-REPLICATED and")
    out("may contribute a number but not carry the headline unqualified.")

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
                rows = per_size(cells, conds, arm_a, arm_b, args.min_effect, pass_explain)
                _, thr, routine, reg, pad, incx, ta, tb, floor = k
                if rows:
                    comparable += 1
                    s = summarise(rows, args, "V1-set", "V2-set")
                    out(
                        f"  {inst!s:14s} t={thr!s:<4} {routine!s:6s} {reg:6s} pad={pad!s:<3} "
                        f"incx={incx!s:<2} tr={ta}{tb} by={mech:8s} "
                        f"V1={fmt_val(s['mean_a'])} V2={fmt_val(s['mean_b'])} "
                        f"delta={fmt_pct(s['median_delta'])} band={100 * s['band']:4.1f}% "
                        f"sizes={s['n_sizes']:<2} (+{s['n_a_ahead']}/-{s['n_b_ahead']}) "
                        f"passes={s['passes_used']}of{s['passes_avail']}  "
                        f"{s['verdict']}{under_flag(s)}{vflag(s)}{tag}"
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
                        "routine_family": routine_family(routine),
                        "regime": reg,
                        "lda_pad": pad,
                        "incx": incx,
                        "transa": ta,
                        "transb": tb,
                        "min_seconds": floor,
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
                        "passes_used": s["passes_used"],
                        "passes_available": s["passes_avail"],
                        "under_replicated": s["under_replicated"],
                        "unexplained_passes": s["unexplained_passes"],
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
                        f"incx={k[5]!s:<2} tr={k[6]}{k[7]} by={mech2:8s} NO DATA — "
                        f"{arm_label(missing)} absent "
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
        inst, thr, routine, m, n, k, pad, incx, ta, tb, floor = cond
        # `floor` stays in the pairing key. A tight-vs-padded penalty read across
        # two timing floors would be part stride and part instrument, which is the
        # same objection as pairing across passes two lines down.
        base = (inst, arm, thr, routine, m, n, k, incx, ta, tb, floor)
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
            inst, arm, thr, routine, m, _n, _k, incx, ta, tb, floor = base
            # Same paired rule as per_size(): a tight-vs-padded penalty measured
            # over different passes on each side would be part stride and part
            # box. Both sides are the same arm here, so a pass that has one and
            # not the other is a truncated sweep, not an arm that failed -- there
            # is no census reason to look for, and the intersection is the whole
            # remedy.
            shared = set(ct.runs) & set(cp.runs)
            if not shared:
                continue
            avail = len(set(ct.runs) | set(cp.runs))
            ct, cp = ct.restrict(shared), cp.restrict(shared)
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
            under = "  UNDER-REPLICATED" if len(shared) < avail else ""
            out(
                f"  {inst!s:14s} {arm_label(arm):30s} t={thr!s:<4} {routine!s:6s} n={m!s:<5} "
                f"pad={pad!s:<3} tr={ta}{tb} tight={fmt_val(ct.value)} padded={fmt_val(cp.value)} "
                f"penalty={fmt_pct(pen)} band={100 * band:4.1f}% "
                f"passes={len(shared)}of{avail}  {verdict}{under}{ver}{tag}"
            )
            payload.append(
                {
                    "instance": inst,
                    "arm": arm_label(arm),
                    "threads": thr,
                    "routine": routine,
                    "routine_family": routine_family(routine),
                    "m": m,
                    "lda_pad": pad,
                    "incx": incx,
                    "transa": ta,
                    "transb": tb,
                    "min_seconds": floor,
                    "regime": regime(m or 0),
                    "tight": ct.value,
                    "padded": cp.value,
                    "penalty": pen,
                    "band": band,
                    "runs_tight": ct.n_runs,
                    "runs_padded": cp.n_runs,
                    "passes_used": len(shared),
                    "passes_available": avail,
                    "under_replicated": len(shared) < avail,
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
    out("4. REGIME PROFILE  — where in the size range the effect lives (see §9 first)")
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
                d.get("transa", "N"),
                d.get("transb", "N"),
                d["reference_arm"],
            )
        ][d["regime"]] = d
    if not by:
        out("      no reference-arm deficits to profile")
    for k in sorted(by, key=skey):
        inst, thr, arm, routine, pad, incx, ta, tb, ref = k
        r = by[k]
        vals = {reg: (r[reg]["median_deficit"] if reg in r else None) for reg in REGIMES}
        gap = None if vals["small"] is None or vals["large"] is None else vals["small"] - vals["large"]
        thin = [reg for reg in REGIMES if reg not in r]
        out(
            f"      {inst!s:14s} t={thr!s:<4} {arm:30s} {routine!s:6s} pad={pad!s:<3} "
            f"incx={incx!s:<2} tr={ta}{tb} vs {ref:22s} "
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
                "transa": ta,
                "transb": tb,
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
                c.get("transa", "N"),
                c.get("transb", "N"),
            )
        ][c["regime"]] = c
    if not by2:
        out("      no comparable target-cross cell in any regime")
    for k in sorted(by2, key=skey):
        inst, thr, mech, a1, _a2, routine, pad, incx, ta, tb = k
        r = by2[k]
        vals = {reg: (r[reg]["median_delta"] if reg in r else None) for reg in REGIMES}
        gap = None if vals["small"] is None or vals["large"] is None else vals["small"] - vals["large"]
        thin = [reg for reg in REGIMES if reg not in r]
        out(
            f"      {inst!s:14s} t={thr!s:<4} by={mech:8s} {routine!s:6s} pad={pad!s:<3} "
            f"incx={incx!s:<2} tr={ta}{tb} {a1:24s} "
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
                "transa": ta,
                "transb": tb,
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


def report_anomalies(inp, cells, hosts, exc: Excluded, scaling, overlap, args, out):
    out("\n" + "=" * 78)
    out("5. ANOMALIES  — read this before trusting any number in sections 1-4, 6-9")
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

    # Surfaced here as well as in section 9 because this section is where a reader
    # is told to look before trusting section 4, and an unconfirmed timing floor is
    # a reason not to trust section 4 specifically. AGREES-WITH-BIAS is not an
    # anomaly: the bias is below --min-effect by construction, so it is a
    # measurement to quote, not a hazard to flag.
    if overlap["status"] in ("DISAGREES", "ORDER-CONFOUNDED", "INCOMPLETE"):
        add(
            "!!",
            "floor_overlap_unconfirmed",
            f"timing-floor overlap band {overlap['status']}: {overlap['why']}. Section 4's "
            f"regime profile cannot be read across n=256 until this is resolved — a step there "
            f"is what the band exists to attribute. See section 9.",
        )
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
        for kind, r in h.provenance_gaps:
            add("!!", kind, r)
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
    # Standing order 11 says every gap carries a reason. The reason was being read,
    # used to classify the gap, and then dropped for every gap that was NOT a hole:
    # a reader of the report saw `build_failed=12` and had to go back to
    # census-*.ndjson to learn that ARMPL_DIR was unset. "Absent and null are
    # different claims" is a claim about the artifact, not about the input files.
    explained = defaultdict(int)
    explained_why = {}
    for inst, conds in sorted(conds_by_inst.items(), key=lambda kv: str(kv[0])):
        arms = sorted(arms_by_inst[inst] | expected_arms, key=arm_label)
        cellset = defaultdict(list)
        for cond in conds:
            # The census cell key must be the comparison's key, minus only the
            # sizes it aggregates over. It was missing transa/transb, so an arm
            # that lost an entire transpose while keeping the others was recorded
            # as `partial` on one cell instead of MISSING-UNEXPLAINED on its own --
            # section 7 saying "some sizes are absent" where the truth was "this
            # arm never ran TN at all". `floor` is here for the same reason it is
            # in group_cells(): so an untagged probe record cannot merge into a
            # real cell's expectation and make it look complete.
            cellset[
                (
                    cond[1],
                    cond[2],
                    regime(cond[3] or 0),
                    cond[6],
                    cond[7],
                    cond[8],
                    cond[9],
                    cond[10],
                )
            ].append(cond)
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
                            "transa": ck[5],
                            "transb": ck[6],
                            "min_seconds": ck[7],
                            "status": status,
                            "measured_conditions": have,
                            "expected_conditions": len(clist),
                            "reason": why,
                        }
                    )
                elif status not in ("measured", "excluded", "partial-excluded"):
                    # The two excluded statuses are deliberately not listed here:
                    # their reason is this file's own exclusion, already stated as a
                    # hard anomaly in section 5, and explain() would answer with the
                    # census's view of an arm that ran fine.
                    _st, why = explain(inst, arm, thr)
                    key = (inst, arm_label(arm), status)
                    explained[key] += 1
                    explained_why.setdefault(key, why)

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
                f"{m['regime']:6s} pad={m['lda_pad']!s:<3} tr={m['transa']}{m['transb']} "
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
                f"{m['regime']:6s} pad={m['lda_pad']!s:<3} tr={m['transa']}{m['transb']} "
                f"{m['measured_conditions']}/{m['expected_conditions']} conditions"
            )
        if len(partial) > args.max_listed:
            out(f"    ... and {len(partial) - args.max_listed} more")

    if explained:
        out(f"\n  explained absences ({len(explained)}); the reason each gap carries:")
        for (inst, arm, status), n in sorted(explained.items(), key=lambda kv: (str(kv[0][0]), kv[0][1])):
            out(
                f"    {inst!s:14s} {arm:30s} {status:14s} {n:<4} cells — "
                f"{explained_why[(inst, arm, status)] or 'no reason recorded'}"
            )

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
        "explained": [
            {
                "instance": inst,
                "arm": arm,
                "status": status,
                "cells": n,
                "reason": explained_why[(inst, arm, status)],
            }
            for (inst, arm, status), n in sorted(explained.items(), key=lambda kv: (str(kv[0][0]), kv[0][1]))
        ],
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


def report_replicates(inp, hosts, explain, pass_explain, args, out):
    """Each pass analysed alone, then the verdicts compared.

    Deliberately NOT a pooled statistic. P3 spends the extra passes to find out
    whether the first one reproduces; medianing them would answer a different
    and much weaker question -- and would do it while looking tidier, which is
    worse. Every number below comes from one pass and is labelled with its box."""
    want = args.replicate_passes
    out("\n" + "=" * 78)
    out("8. REPLICATES  — does the headline survive another physical machine")
    out("=" * 78)
    out("A replicate is the same instance_type on a different instance_id. The passes are")
    out("compared, never pooled: a median across them would report a number neither pass")
    out("measured, and would hide the one thing the extra passes were bought to test.")
    out(f"Spend policy expects {want} separately launched passes per instance_type.")

    passes = replicate_passes(inp)
    payload = []
    for inst in sorted(passes, key=str):
        boxes = passes[inst]
        n_runs = sum(len(v) for v in boxes.values())
        # Reported as a distinct fact from the reproduction status, not folded into
        # it. "did the headline reproduce" and "did we buy enough passes to ask"
        # are different claims, and a P2 dataset legitimately has one pass; making
        # the shortfall a DIVERGES-* status would fire bit 16 on every single-host
        # run and cost the bit its meaning. gates/p3.sh asserts the count.
        short = len(boxes) < want
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
                    "passes_expected": want,
                    "under_replicated": short,
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
            # per pass would inflate section 5's counts. Kept rather than discarded
            # because compute_verdict() reads it to refuse a null over a wrong
            # answer, and a pass whose own arm got a routine wrong must be refused
            # on that pass's evidence -- not on the campaign's, and not at all.
            # It is never merged into `exc`; only read.
            pexc = Excluded()
            pcells = build_cells(bench, hosts, pexc)
            pcross = report_target_cross(
                pcells, cell_groups(pcells), hosts, explain, pass_explain, inp, args, lambda _line: None
            )
            v = compute_verdict(pcross, hosts, pexc, args)
            # Arms this pass lost. Reported here and nowhere else: section 7 lists
            # an absence only when the arm produced no cells at all, and an arm that
            # ran on two passes and died on the third produces cells -- so the
            # failure, and the reason the census recorded for it, appeared in no
            # section of the report. That is the same "a reason recorded is not a
            # reason reported" gap as section 7's, at pass granularity, and it is
            # exactly the fact a three-pass policy needs: it decides whether the
            # dissenting pass is evidence against the headline or a box to re-run.
            # It also explains a pooled INCONCLUSIVE: an arm present on two passes
            # and absent on a third makes every pooled cell unequal-N.
            lost = {}
            for o in inp.outcomes:
                if o.get("run_id") not in rids or o.get("library") in NON_BENCH_LIBRARIES:
                    continue
                st = o.get("status") or "unknown"
                if st in CENSUS_SUCCESS:
                    continue
                arm = (o.get("library"), o.get("target"), canon_coretype(o.get("coretype")))
                lost.setdefault((arm_label(arm), st), o.get("reason") or "no reason recorded")
            per.append(
                {
                    "instance_id": iid,
                    "run_ids": sorted(rids, key=str),
                    "cells": len(pcells),
                    "verdict_code": v["code"],
                    "median_delta": v["median_delta"],
                    "cells_comparable": v["cells_comparable"],
                    "arms_lost": [
                        {"arm": a, "status": st, "reason": why} for (a, st), why in sorted(lost.items())
                    ],
                }
            )
            out(
                f"  {inst!s:14s} {iid!s:22s} runs={','.join(sorted(map(str, rids))):24s} "
                f"cells={len(pcells):<5} comparable={v['cells_comparable']:<4} "
                f"median={fmt_pct(v['median_delta'])} {v['code']}"
            )
            for (a, st), why in sorted(lost.items()):
                out(f"  {inst!s:14s}   this pass lost {a} ({st}): {why}")

        codes = {p["verdict_code"] for p in per}
        dirs = {_direction(p["verdict_code"]) for p in per}
        # Majority agreement, which is the whole reason P3 buys a third pass. The
        # median of two passes IS the mean: one bad pass moves it and nothing says
        # which pass was bad. Three passes reject one. So a strict majority sharing
        # one directional code, with every dissenter non-directional, is the
        # intended outcome and must not read as a divergence -- otherwise the third
        # pass makes the gate *harder* to pass than two did, which is backwards.
        # A dissenter that carries the OPPOSITE direction is not covered here: two
        # passes claiming opposite signs stay a divergence at any pass count,
        # because no majority makes a contradiction publishable.
        tally = Counter(p["verdict_code"] for p in per)
        top_code, top_n = tally.most_common(1)[0]
        majority = (
            len(per) >= 3
            and top_n * 2 > len(per)
            and _direction(top_code) is not None
            and all(_direction(c) is None for c in codes - {top_code})
        )
        if len(codes) == 1:
            status = "REPRODUCES"
            note = f"all {len(per)} passes: {next(iter(codes))}"
        elif majority:
            status = "REPRODUCES-MAJORITY"
            dissent = ", ".join(
                f"{p['instance_id']}={p['verdict_code']}" for p in per if p["verdict_code"] != top_code
            )
            note = (
                f"{top_n} of {len(per)} passes: {top_code}. Dissenting and "
                f"non-directional: {dissent}. The majority is directional and "
                f"uncontradicted, which is what the third pass was bought for; read "
                f"the dissenting pass before publishing, do not average it in."
            )
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
        if short:
            out(
                f"  {inst!s:14s} UNDER-REPLICATED     {len(boxes)} independent passes, "
                f"policy is {want}. With two passes the median is the mean: one bad pass "
                f"moves it and nothing identifies which. Not a divergence, a shortfall."
            )

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
                "passes_expected": want,
                "under_replicated": short,
                "passes": per,
            }
        )

    if not payload:
        out("  no env-*.json carries both instance_type and instance_id: replicates unknowable")
    return payload


# ---- timing-floor overlap band ---------------------------------------------


def _sign(x):
    return (x > 0) - (x < 0)


def compute_floor_overlap(probe_cells, args):
    """Does the same case measured at both MIN_SECONDS floors give the same answer?

    WHY THIS SECTION EXISTS. The floor steps from 0.05 s to 0.30 s at n=256, and
    n=256 is also where OpenBLAS's GEMM_SMALL_* path is hypothesised to hand over
    to the blocked kernel. A step in the section-4 regime profile at n=256 is
    therefore ambiguous between "the fast path ends here" and "the averaging window
    changed here", and the two predict the same picture, so nothing in the data
    resolves them after the fact. bench.c measures n=192..384 at both floors so the
    ambiguity is settled by measurement instead of by argument.

    THREE STATISTICS, because the probe can fail in three distinguishable ways and
    only one of them is "the floors disagree".

    (1) Per pair, is the difference inside the parity band? Same band_for() the
        whole campaign uses, so "the floors agree" means exactly what "these two
        arms are at parity" means everywhere else in this file. Using a bespoke
        tolerance here would make the instrument check stricter or looser than the
        findings it is validating, and there is no argument for either.

    (2) Pooled, is the sign consistent? Scatter within the band is noise; five
        sizes all leaning the same way is a bias, and pooling across arms is where
        the power is (one arm's five pairs is 6% under a fair-coin null, twenty
        arms' hundred pairs is not a coincidence anyone need weigh). A consistent
        bias smaller than --min-effect cannot manufacture a finding, because
        nothing below that floor is reportable -- so it is recorded as a quantity
        to discount a regime step against, not as a failure.

        But band_for() WIDENS past min_effect on a dispersed cell, so a consistent
        bias can in principle clear min_effect while every pair still sits inside
        its own widened band. That case is a failure, not a footnote, and it is
        tested separately: a signed bias above min_effect is DISAGREES even when
        every individual pair passes (1).

    (3) Pooled, does the difference track ORDER better than it tracks the FLOOR?
        bench.c alternates which floor runs first precisely so this is answerable.
        If the short floor always ran first it would always meet the colder cache,
        and a first-vs-second drift would be indistinguishable from a
        short-vs-long floor effect. When order explains the signs better than the
        floor does, the probe has not measured what it set out to measure: that is
        ORDER-CONFOUNDED, which is neither a pass nor a floor problem, and it is
        reported as its own status rather than being rounded to either.

    ABSENT is not a failure. Datasets written before bench.c grew the probe have no
    such records and must keep analysing exactly as they did; requiring the probe is
    gates/p2.sh's job, not the exit code's. INCOMPLETE -- probe records present but
    not one complete pair among them -- IS a failure, because something produced
    half a probe."""
    pairs = []
    by_case = defaultdict(dict)
    for (cond, arm), cell in probe_cells.items():
        # Everything except the floor, which is what the pair varies.
        by_case[(cond[:10], arm)][cond[10]] = cell

    incomplete = 0
    for (case, arm), by_floor in by_case.items():
        if len(by_floor) != 2:
            incomplete += 1
            continue
        # Read short/long off the data rather than off a constant. bench.c owns
        # the two values and this file must not hold a third copy of them.
        f_short, f_long = sorted(by_floor, key=float)
        cs, cl = by_floor[f_short], by_floor[f_long]
        g_short, g_long = cs.value, cl.value
        if not (g_long > 0):
            incomplete += 1
            continue
        d_floor = (g_short - g_long) / g_long
        # Positive means the first-measured of the pair read higher. Same
        # magnitude as d_floor by construction; only the sign carries information,
        # and the sign is what separates a floor effect from a drift.
        short_first = "floor_probe_first" in cs.notes
        long_first = "floor_probe_first" in cl.notes
        d_order = d_floor if short_first else -d_floor if long_first else None
        pairs.append(
            {
                "instance": case[0],
                "threads": case[1],
                "routine": case[2],
                "m": case[3],
                "arm": arm_label(arm),
                "floor_short": f_short,
                "floor_long": f_long,
                "gflops_short": g_short,
                "gflops_long": g_long,
                "delta": d_floor,
                "band": band_for(cs, cl, args.min_effect),
                "short_ran_first": short_first if (short_first or long_first) else None,
                "delta_order": d_order,
                "verified": cs.all_verified and cl.all_verified,
            }
        )

    pairs.sort(key=lambda p: skey((p["instance"], p["arm"], p["threads"], p["m"])))
    outside = [p for p in pairs if abs(p["delta"]) > p["band"]]

    signs = [_sign(p["delta"]) for p in pairs if p["delta"]]
    order_signs = [_sign(p["delta_order"]) for p in pairs if p["delta_order"]]
    floor_consistency = abs(sum(signs)) / len(signs) if signs else 0.0
    order_consistency = abs(sum(order_signs)) / len(order_signs) if order_signs else 0.0
    median_bias = statistics.median([p["delta"] for p in pairs]) if pairs else None
    worst = max(pairs, key=lambda p: abs(p["delta"])) if pairs else None

    # 5 is one full band on one arm: the smallest set in which all-same-sign is
    # better than one-in-ten under a fair coin. Below it, sign consistency is not
    # evidence of anything and claiming a bias from it would be the tuning this
    # campaign is not allowed to do.
    MIN_FOR_SIGN = 5
    biased = len(signs) >= MIN_FOR_SIGN and floor_consistency == 1.0
    order_explains = (
        len(order_signs) >= MIN_FOR_SIGN
        and order_consistency == 1.0
        and order_consistency > floor_consistency
    )

    if not pairs:
        status = "INCOMPLETE" if incomplete else "ABSENT"
        why = (
            f"{incomplete} probe case(s) carry only one floor, so no pair could be formed"
            if incomplete
            else "no floor-overlap probe records in this dataset"
        )
    elif outside:
        status = "DISAGREES"
        w = outside[0]
        why = (
            f"{len(outside)} of {len(pairs)} pairs differ by more than their parity band, "
            f"worst {w['instance']} {w['arm']} n={w['m']} at {100 * w['delta']:+.1f}% "
            f"against a band of {100 * w['band']:.1f}%"
        )
    elif biased and abs(median_bias) > args.min_effect:
        status = "DISAGREES"
        why = (
            f"every pair is inside its own band, but all {len(signs)} lean the same way and the "
            f"median bias is {100 * median_bias:+.1f}%, past the {100 * args.min_effect:.0f}% "
            f"reporting floor. The bands were widened by dispersion; a bias this size can "
            f"produce a section-4 step on its own"
        )
    elif order_explains:
        status = "ORDER-CONFOUNDED"
        why = (
            f"all {len(order_signs)} differences follow measurement order and only "
            f"{100 * floor_consistency:.0f}% follow the floor, so the probe measured drift "
            f"rather than the floor and cannot settle the n=256 ambiguity"
        )
    elif biased:
        status = "AGREES-WITH-BIAS"
        why = (
            f"all {len(signs)} pairs agree within band, but consistently signed: the "
            f"{float(pairs[0]['floor_short']):g} s floor reads {100 * median_bias:+.1f}% against "
            f"the {float(pairs[0]['floor_long']):g} s floor. Below the "
            f"{100 * args.min_effect:.0f}% reporting floor, so it cannot create a finding — "
            f"discount a section-4 step near n=256 by this much"
        )
    else:
        status = "AGREES"
        why = (
            f"all {len(pairs)} pairs agree within band, signs scattered "
            f"({100 * floor_consistency:.0f}% consistent), median "
            f"{100 * median_bias:+.1f}%. The step at n=256 in section 4 is the hardware"
        )

    return {
        "status": status,
        "why": why,
        "confirmed": status in ("AGREES", "AGREES-WITH-BIAS"),
        "pairs": pairs,
        "n_pairs": len(pairs),
        "incomplete_cases": incomplete,
        "outside_band": len(outside),
        "floor_sign_consistency": floor_consistency,
        "order_sign_consistency": order_consistency,
        "median_bias": median_bias,
        "worst_delta": worst["delta"] if worst else None,
        "min_pairs_for_sign_test": MIN_FOR_SIGN,
    }


def report_floor_overlap(ov, args, out):
    out("\n" + "=" * 78)
    out("9. TIMING-FLOOR OVERLAP BAND  — is the step at n=256 hardware or instrument")
    out("=" * 78)
    out(f"  {ov['status']}: {ov['why']}")
    if not ov["pairs"]:
        if ov["status"] == "ABSENT":
            out("  bench.c emits this probe on every arm; a dataset without it predates the")
            out("  probe. Nothing here is wrong, but nothing here is confirmed either.")
        return ov
    out("")
    out(
        f"  {'instance':<18} {'arm':<26} {'thr':>4} {'n':>5} "
        f"{'short':>9} {'long':>9} {'delta':>8} {'band':>7} first"
    )
    for p in ov["pairs"][: args.max_listed]:
        first = "short" if p["short_ran_first"] else "long" if p["short_ran_first"] is False else "?"
        flag = " !!" if abs(p["delta"]) > p["band"] else ""
        out(
            f"  {p['instance']!s:<18} {p['arm']:<26} {p['threads']!s:>4} {p['m']!s:>5} "
            f"{p['gflops_short']:9.2f} {p['gflops_long']:9.2f} "
            f"{100 * p['delta']:+7.1f}% {100 * p['band']:6.1f}% {first}{flag}"
        )
    if len(ov["pairs"]) > args.max_listed:
        out(f"  ... {len(ov['pairs']) - args.max_listed} more pairs, see --json")
    out("")
    out(
        f"  sign consistency: floor {100 * ov['floor_sign_consistency']:.0f}%, "
        f"order {100 * ov['order_sign_consistency']:.0f}% "
        f"(order is the control; bench.c alternates which floor runs first so that a "
        f"first-vs-second"
    )
    out("  drift cannot masquerade as a short-vs-long floor effect)")
    if ov["incomplete_cases"]:
        out(f"  {ov['incomplete_cases']} probe case(s) carried only one floor and were not paired")
    return ov


# ---- verdict ---------------------------------------------------------------


def coherent_subsets(cross, args):
    """Axis values whose own comparable cells carry a direction by majority.

    This exists to guard the NULL branch, and the reason is arithmetic. The
    campaign verdict counts cells; the routine set does not contribute cells
    evenly. `dgemm` alone contributes 20 cells (padded and unpadded), `sgemm`,
    `dsyrk`, `dtrsm`, `dtrmm` and `dsymm` 12 each, `dgemv` 8. So an effect
    confined to TRSM/TRMM/SYMM -- which is 90 of the 94-vs-5 kernel gap this
    campaign exists to measure -- is 36 of 104 comparable cells, or 35%. The
    parity cells then hold 65%, clear the 60% majority, and the headline reads
    "NULL ... publish the negative result" over a coherent +22% on every cell of
    the three routines under study.

    Whether such an effect reaches the global majority is decided by how many
    cells the *unaffected* routines contribute, which is a property of bench.c's
    size ladder, not of the hardware. That makes the unguarded NULL branch a
    false negative on exactly the shape the campaign predicts.

    A subset must clear more than the global rule does to qualify: every cell in
    it already passed `band_for()`, at least DEFAULT_SUBSET_MIN_CELLS cells are
    comparable, and one direction holds `--verdict-majority` of them. Both
    directions are tested, so a routine where the V1 set is *worse* blocks NULL
    on the same terms -- this widens what counts as "not a null", it does not
    lean on the campaign's hypothesis.

    Found by C11: before this, gates/p1.sh certified the analysis on `dgemm` and
    `dgemv` only, and the routine-localised fixture read out as a global null.

    **The majority is over balanced weight, not raw cells**, and that is what makes
    the guard survive a larger matrix. Cell counts follow bench.c's ladder: a
    routine measured at five pads and four transposes contributes twenty times the
    rows of one measured at one pad and one transpose, all of them the same
    hardware claim repeated. Counting rows would let GEMM's row count decide
    whether an effect on TRSM/TRMM/SYMM is coherent, so expanding the matrix would
    dilute the guard it was built to be -- worse than before C11, since every
    planned addition multiplies GEMM's rows faster than anything else's.

    The weighting is `balanced_weights()`, the same rule compute_verdict() uses,
    applied *within each axis value*. It was family-only here, and that was two
    thirds of a fix: a family's unit split evenly across its rows regardless of
    regime, so for the routine/instance/trans axes an effect confined to the large
    regime of one routine could not reach 60% -- 10 large rows against 130
    small+medium ones -- which is the same false negative one level down, on the
    axis where the memory-side finding lives. And the family's unit split by row
    count *inside* the family, so dgemm already outweighed sgemm 5:1 on the pad
    axis alone. Both are properties of the ladder, not of the hardware."""
    raw = defaultdict(lambda: defaultdict(int))
    deltas = defaultdict(list)
    contributing = []
    for c in cross:
        if not c["host_admissible"]:
            continue
        v = c["verdict"]
        if v == "parity":
            bucket = "parity"
        elif v.endswith("V1-set-ahead"):
            bucket = "v1"
        elif v.endswith("V2-set-ahead"):
            bucket = "v2"
        else:
            continue
        fam = c.get("routine_family") or routine_family(c.get("routine"))
        axes = (
            ("routine", c["routine"]),
            ("regime", c["regime"]),
            ("instance", c["instance"]),
            ("trans", f"{canon_trans(c.get('transa'))}{canon_trans(c.get('transb'))}"),
        )
        contributing.append((axes, (fam, c["routine"], c["regime"]), bucket, c["median_delta"]))

    # Weighted per axis value, because the denominator is that axis value's own
    # groups: at ("routine", "dtrsm") the groups are dtrsm's three regimes, and at
    # ("regime", "large") they are the families that reach the large regime.
    by_axis = defaultdict(list)
    for i, (axes, _grp, _bucket, _delta) in enumerate(contributing):
        for av in axes:
            by_axis[av].append(i)

    weights = defaultdict(lambda: defaultdict(Fraction))
    groups_seen = defaultdict(set)
    for av, idx in by_axis.items():
        cells = [contributing[i][1] for i in idx]
        for i, w in zip(idx, balanced_weights(cells), strict=True):
            _axes, (fam, _routine, reg), bucket, delta = contributing[i]
            weights[av][bucket] += w
            raw[av][bucket] += 1
            groups_seen[av].add((fam, reg))
            if delta is not None:
                # Carried with its weight: the subset's reported median is
                # weighted for the same reason the campaign median is, and this is
                # the number `located()` prints beside the share.
                deltas[(av[0], av[1], bucket)].append((delta, w))

    found = []
    for (axis, value), t in sorted(weights.items(), key=lambda kv: skey(kv[0])):
        n_raw = sum(raw[(axis, value)].values())
        if n_raw < args.subset_min_cells:
            continue
        total_w = t["v1"] + t["v2"] + t["parity"]
        if not total_w:
            continue
        for direction, side in (("V1", "v1"), ("V2", "v2")):
            if not majority_met(t[side], total_w, args.verdict_majority):
                continue
            d = deltas[(axis, value, side)]
            found.append(
                {
                    "axis": axis,
                    "value": value,
                    "direction": direction,
                    "wins": raw[(axis, value)][side],
                    "comparable": n_raw,
                    # Cast at the wire boundary, not in the arithmetic: Fraction
                    # is not JSON-serialisable, and the share is rendered from
                    # the exact ratio rather than from two rounded floats.
                    "weight": round(float(t[side]), 4),
                    "weight_total": round(float(total_w), 4),
                    "weight_share": float(t[side] / total_w),
                    # The group count is the audit trail for the weighting: it is
                    # exactly what `weight_total` counts, so a reader can check the
                    # share without re-deriving the rule.
                    "groups": len(groups_seen[(axis, value)]),
                    "families": sorted({fam for fam, _reg in groups_seen[(axis, value)]}),
                    "median_delta": weighted_median([v for v, _w in d], [w for _v, w in d]),
                }
            )
    return found


def compute_verdict(cross, hosts, exc: Excluded, args):
    """One line, computed. The previous version's decision guide was
    unconditional literal text, so `grep -q parity` and `grep -q "publish the
    negative result"` both passed on a dataset with zero comparisons.

    **Nothing in this function is a fraction of raw cells.** Three quantities
    decide the verdict and all three now run through balanced_weights():

      the majority       which bucket carries --verdict-majority
      the effect size    the median tested against --min-effect
      the coverage guard the share tested against --max-nodata-fraction

    They were fixed one at a time, in that order, and each fix made the next one's
    absence louder -- a branch whose majority half was balanced and whose
    effect-size half was raw is not half-right, it is a verdict whose two halves
    disagree about what a cell is worth. The first was the third appearance of the
    defect and the first on the regime axis: before the #2 densification the three
    regimes each contributed 20 cells to the default fixture -- balanced by
    accident, so nothing showed -- and after it they contribute 160/110/20, wrong
    in both directions. An effect confined to small+medium clears a 60% majority on
    cell count alone and reads as a campaign-level V1-SET-AHEAD; an effect confined
    to the large regime cannot reach 60% no matter how large it is, because large
    is ~6% of the cells. The large regime is where the DDR generation and the L3
    step show, so that second failure would have silently removed the memory-side
    finding from the campaign's reach.

    Raw counts are still printed beside every balanced fraction: the balanced one
    decides, and the reader can see both. Asserted in both directions by the
    fixtures -- a rule that could manufacture a direction out of a genuine null
    would be worse than the false negative it fixes.

    The coverage guard also has an ABSOLUTE half, and it needs one. A share can
    always be diluted by densifying elsewhere, so no threshold on a fraction can
    express "one whole family of the design did not compare"; `dark_groups` is a
    count of (family, regime) groups in which nothing was measured at all, and a
    single one of them refuses a directional verdict outright.

    Dark is measured against DATA, not against a verdict, and the distinction is
    load-bearing: a group whose cells compared and came out thin, split, or
    unequal-N is not dark, it is inconclusive, and those are already counted.
    Level-1's four lengths put exactly one size in the medium regime, so
    (axpy, medium) and (dot, medium) are permanently `inconclusive(thin:1<3)` --
    a property of the ladder, not a hole in the data. Reading those as dark made
    every clean scenario INCONCLUSIVE, which is the coverage guard refusing the
    design it was given rather than the data it was missing. So a cell lights its
    group when `n_sizes > 0` on an admissible host, and the only two buckets that
    leave it dark are `no_data` (nothing to compare) and `inadmissible` (a host
    the campaign excluded).

    `exc` is read for one thing only: which routines an arm got WRONG. A wrong
    answer is not a slow answer. The kernel computed something other than the
    reference function, so the comparison in that routine did not happen -- and it
    is precisely where a kernel difference was most likely, because a kernel that
    gets the answer wrong is a kernel doing something different. So NULL is
    refused while any routine stands excluded for a verification failure: "no
    difference anywhere" is a claim about the whole design, and the excluded part
    is the part that cannot support it. This used to be left to
    --max-nodata-fraction, which was never the guard, it only happened to be one:
    the #2 densification took dgemm's total exclusion from 40% of the cross down
    to 29%, under the 34% threshold, and the verify-fail fixture went green on
    "publish the negative result" off a kernel returning wrong answers. Standing
    order 4 says a failed verification poisons the record; this is that order at
    the one branch where the poison would have been published as a finding."""
    tally = defaultdict(int)
    weight = defaultdict(Fraction)
    per_ir = defaultdict(lambda: defaultdict(int))
    fam_cells = defaultdict(int)
    # One entry per cross row, comparable or not. The nodata guard's denominator is
    # the WHOLE cross, so both halves have to be weighted in the same group space
    # or the fraction compares two different things.
    all_groups = []
    all_buckets = []
    deltas = []
    unverified = 0
    under = 0
    under_passes = set()
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
        fam = c.get("routine_family") or routine_family(c.get("routine"))
        all_groups.append((fam, c["routine"], c["regime"]))
        all_buckets.append(bucket)
        if bucket in ("v1_wins", "v2_wins", "parity"):
            fam_cells[fam] += 1
            if not c["verified"]:
                unverified += 1
            if c.get("under_replicated"):
                under += 1
                under_passes.add(f"{c.get('passes_used')}of{c.get('passes_available')}")

    total = sum(tally.values())
    comparable = tally["v1_wins"] + tally["v2_wins"] + tally["parity"]

    COMPARABLE = ("v1_wins", "v2_wins", "parity")
    # Two weightings, over two populations, and the distinction is the point.
    #
    #   cross_w  over EVERY cross row -- the denominator of the coverage guard,
    #            which asks what share of the DESIGN is missing.
    #   weight   over the comparable rows only -- the denominator of the verdict,
    #            which asks what share of the MEASUREMENTS moved.
    #
    # Weighting the comparable rows inside the full cross instead would make a
    # verdict share depend on how much data is absent, which is a different
    # question and not this one.
    cross_w = balanced_weights(all_groups)
    cross_total = sum(cross_w, Fraction(0))
    nodata_w = sum(
        (w for w, b in zip(cross_w, all_buckets, strict=True) if b not in COMPARABLE),
        Fraction(0),
    )
    nodata_share = (nodata_w / cross_total) if cross_total else Fraction(0)
    # A (family, regime) group in which nothing was measured at all. This is the
    # ABSOLUTE half of the coverage guard and it is what --max-nodata-fraction
    # never was: no share, at any density, can express "one whole family of the
    # design did not compare". A fraction can always be diluted by densifying
    # somewhere else -- that is exactly what took dgemm's exclusion from 40% to
    # 29% -- and a count of dark groups cannot.
    #
    # `n_sizes > 0` is the signal, not the verdict: a group that compared and came
    # out thin or split is inconclusive, not dark. See the docstring for why --
    # (axpy, medium) holds one level-1 length by construction and would otherwise
    # make every scenario in the campaign INCONCLUSIVE forever.
    group_lit = defaultdict(int)
    for c, (fam, _routine, reg), b in zip(cross, all_groups, all_buckets, strict=True):
        group_lit[(fam, reg)] += 1 if (b != "inadmissible" and c["n_sizes"] > 0) else 0
    dark_groups = sorted(f"{fam}/{reg}" for (fam, reg), n in group_lit.items() if n == 0)

    cmp_rows = [(c, g, b) for c, g, b in zip(cross, all_groups, all_buckets, strict=True) if b in COMPARABLE]
    cmp_w = balanced_weights([g for _c, g, _b in cmp_rows])
    weight_total = sum(cmp_w, Fraction(0))
    for (c, _g, b), w in zip(cmp_rows, cmp_w, strict=True):
        weight[b] += w
        if c["median_delta"] is not None:
            deltas.append((c["median_delta"], w))

    def majority(bucket):
        """Whether this bucket carries --verdict-majority of the balanced
        weight. Exact; see majority_met()."""
        return majority_met(weight[bucket], weight_total, args.verdict_majority)

    def pct(bucket):
        """The same share, as a percentage, for printing only. Zero weight means
        no comparable cells, which the INCONCLUSIVE branch has already caught."""
        return float(100 * weight[bucket] / weight_total) if weight_total else 0.0

    # The effect-size floor is tested against the RAW median, on purpose, and
    # this is the one place in this function that is not balanced-weighted. Do
    # not "fix" it — it is the counterweight that makes the branch below able to
    # say MIXED.
    #
    # The directional branch asks two deliberately different questions, and it is
    # only informative because they are different:
    #
    #   majority()  — how much of the DESIGN moved. Balanced, so the longest
    #                 ladder cannot vote (that was defect 1).
    #   med         — did the WORK move. Raw, so a family carrying 12 cells
    #                 cannot speak for one carrying 240.
    #
    # Weighting both collapses them into one question asked twice. Measured:
    # under a weighted median the `family-swamped` fixture — V1 ahead 22% on
    # three of five families, which is the N2 gap and exactly the "where" this
    # campaign exists to report — reads as a global V1-SET-AHEAD, because the
    # three moving families carry 3/5 of balanced weight in the majority AND 3/5
    # of it in the median. The MIXED branch becomes unreachable. The raw median
    # is what notices that those three families are the small ones.
    #
    # The balanced median is kept as a diagnostic and printed where they diverge:
    # a gap between them says the effect is concentrated in whichever routines
    # have the longest ladders, which is a finding in itself and should not have
    # to be inferred.
    med = statistics.median([d for d, _w in deltas]) if deltas else None
    med_bal = weighted_median([d for d, _w in deltas], [w for _d, w in deltas])
    band_pct = 100 * args.min_effect
    # Printed only where a median is printed, and only when the two disagree by
    # enough to matter (a tenth of the effect floor). Silence means the ladder is
    # not skewing the aggregate, which is worth being able to see too.
    skew = ""
    if med is not None and med_bal is not None and abs(med - med_bal) > args.min_effect / 10:
        skew = f" (balanced {100 * med_bal:+.1f}%, so the effect is unevenly spread across the design)"
    subsets = coherent_subsets(cross, args)
    poisoned = sorted({r.get("routine") for r in exc.verified_false} - {None})

    def located():
        """The coherent subsets, rendered. Shared by every branch that reports a
        located effect, so a reader never has to learn two phrasings for it."""
        return "; ".join(
            f"{s['axis']} {s['value']}: {s['direction']} set ahead in {s['wins']}/{s['comparable']} cells "
            f"({100 * s['weight_share']:.0f}% of family weight)"
            + (f" (median {100 * s['median_delta']:+.1f}%)" if s["median_delta"] is not None else "")
            for s in subsets[: args.max_listed]
        )

    if total == 0:
        code = "NO-DATA"
        line = (
            f"VERDICT: NO-DATA — no {args.v1_set}/{args.v2_set} comparison exists in this dataset; "
            f"nothing here can answer whether the N2 gap is worth closing"
        )
    elif comparable == 0 or nodata_share > as_exact(args.max_nodata_fraction) or dark_groups:
        code = "INCONCLUSIVE"
        why = (
            f"{', '.join(dark_groups[: args.max_listed])} was not measured at all"
            if dark_groups
            else f"{100 * float(nodata_share):.0f}% of the design's balanced weight is not comparable"
        )
        line = (
            f"VERDICT: INCONCLUSIVE — {why}; {total - comparable} of {total} cells have no comparable "
            f"{args.v1_set}-set measurement (no_data={tally['no_data']}, "
            f"inconclusive={tally['inconclusive']}, inadmissible-host={tally['inadmissible']})"
        )
    elif majority("v1_wins") or majority("v2_wins"):
        # A balanced majority is necessary for a directional headline and not
        # sufficient. It answers "how much of the experiment moved", and the
        # second question — "did the experiment move" — is an effect size, so it
        # is asked as one rather than as a second cell count.
        #
        # This is the guard the ladder densification made load-bearing. Balancing
        # by (family, regime) is what stops the ladder voting, but it also means a
        # family with 12 cells weighs as much as one with 240, so an effect on
        # three small families clears 60% of balanced weight while moving the
        # dataset's median by +0.24%. Publishing "V1-SET-AHEAD, median +0.2%" off
        # that would be the max-over-cell defect in its final form: a global claim
        # sourced from a minority of the work. The floor is --min-effect, the same
        # one the parity band uses, and it is signed — a V1 majority whose median
        # runs the other way is not a V1 headline either.
        direction = "V1" if majority("v1_wins") else "V2"
        bucket = "v1_wins" if direction == "V1" else "v2_wins"
        signed = (med or 0.0) if direction == "V1" else -(med or 0.0)
        if med is not None and signed >= args.min_effect:
            code = f"{direction}-SET-AHEAD"
            tail = (
                f"above the {band_pct:.0f}% floor"
                if direction == "V1"
                else ("against the V1 set; the NEON choice was right, publish the negative result")
            )
            line = (
                f"VERDICT: {code} — median {100 * med:+.1f}%{skew} over "
                f"{tally[bucket]}/{comparable} comparable cells "
                f"({pct(bucket):.0f}% of balanced weight), {tail}"
            )
        else:
            code = "MIXED"
            shown = f"{100 * med:+.2f}%{skew}" if med is not None else "undefined"
            line = (
                f"VERDICT: MIXED — the {direction} set carries {pct(bucket):.0f}% of balanced "
                f"weight but only {tally[bucket]}/{comparable} comparable cells, and the median "
                f"across all of them is {shown}, below the {band_pct:.0f}% floor. So the effect is located, "
                f"not global: {located()}. A balanced majority over a minority of the work is not a "
                f"campaign-level direction"
            )
    elif majority("parity") and not subsets and poisoned:
        # Parity everywhere the comparison ran, and a routine that never ran
        # because an arm got it wrong. See the docstring: this is refused as a
        # null on principle, not on a coverage threshold.
        code = "INCONCLUSIVE"
        line = (
            f"VERDICT: INCONCLUSIVE — {tally['parity']}/{comparable} comparable cells are at parity, "
            f"but {', '.join(poisoned[: args.max_listed])} was excluded for WRONG ANSWERS "
            f"(section 5), so the routine most likely to differ is the one that did not compare. "
            f"A null is a claim about the whole design; fix the correctness failure and re-run "
            f"before reading this as parity"
        )
    elif majority("parity") and not subsets:
        code = "NULL"
        line = (
            f"VERDICT: NULL — {args.v1_set}-set and {args.v2_set}-set at parity in "
            f"{tally['parity']}/{comparable} comparable cells "
            f"({pct('parity'):.0f}% of balanced weight); publish the negative result"
        )
    elif majority("parity"):
        # A parity majority with a coherent minority is not a null. See
        # coherent_subsets(): the majority here is an artefact of how many cells
        # the unaffected routines contribute, so reporting NULL would publish a
        # negative result over a real, located effect.
        code = "MIXED"
        line = (
            f"VERDICT: MIXED — {tally['parity']}/{comparable} comparable cells are at parity, but the "
            f"difference is located, not absent: {located()}. Not a null: the effect is confined to a "
            f"minority of cells, and cell counts follow bench.c's size ladder, not the hardware"
        )
    else:
        code = "MIXED"
        line = (
            f"VERDICT: MIXED — {tally['v1_wins']} cells favour the V1 set, {tally['v2_wins']} the V2 "
            f"set, {tally['parity']} at parity, of {comparable} comparable; no majority at "
            f"{100 * args.verdict_majority:.0f}% of balanced weight "
            f"(V1 {pct('v1_wins'):.0f}%, V2 {pct('v2_wins'):.0f}%, "
            f"parity {pct('parity'):.0f}%)"
        )
    # A directional headline that rests on any intersected comparison is not a
    # full-replication claim, and must not be able to be read as one. The code
    # stays -- refusing the direction is what the intersection rule exists to stop
    # -- but the line carries the marker and section 8 remains the arbiter of
    # whether the headline reproduced.
    headline_eligible = under == 0 or _direction(code) is None
    if not headline_eligible:
        line += (
            f"  [UNDER-REPLICATED: {under} of {comparable} comparable cells used "
            f"{'/'.join(sorted(under_passes))} passes]"
        )
    return {
        "code": code,
        "line": line,
        "headline_eligible": headline_eligible,
        "under_replicated_cells": under,
        "under_replicated_passes": sorted(under_passes),
        "routine_family_cells": dict(sorted(fam_cells.items())),
        "cells_total": total,
        "cells_comparable": comparable,
        "v1_wins": tally["v1_wins"],
        "v2_wins": tally["v2_wins"],
        "parity": tally["parity"],
        "no_data": tally["no_data"],
        "inconclusive": tally["inconclusive"],
        "inadmissible_host": tally["inadmissible"],
        # median_delta is raw and is what the effect-size floor tested;
        # median_delta_balanced is the diagnostic. See the comment above them.
        "median_delta": med,
        "median_delta_balanced": med_bal,
        "nodata_share_balanced": float(nodata_share),
        "dark_groups": dark_groups,
        "min_effect": args.min_effect,
        "unverified_cells": unverified,
        "poisoned_routines": poisoned,
        "coherent_subsets": subsets,
        "by_instance_regime": [
            {"instance": i, "regime": r, **dict(per_ir[(i, r)])} for (i, r) in sorted(per_ir, key=skey)
        ],
        "hosts_admissible": sorted(i for i, h in hosts.items() if h.admissible),
    }


def report_verdict(
    verdict, lda, regimes, coverage, anomalies, replicates, overlap, exit_code, args, out
):
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
    partial = [
        (r["instance"], p["instance_id"], a["arm"])
        for r in replicates
        for p in r.get("passes", [])
        for a in p.get("arms_lost", [])
    ]
    if partial:
        # Named because it is what put the UNDER-REPLICATED marks in sections 1-3.
        # Sections 1-7 pool passes, and each comparison is restricted to the passes
        # where both its arms ran, so an arm lost on one pass costs that comparison
        # a pass rather than costing the whole pooled reading its direction. What
        # the reader needs is which arms, on which box, and that the shortfall is
        # explained: an unexplained loss is not intersected and shows as
        # unequal-N-unexplained instead.
        arms = sorted({a for _i, _b, a in partial})
        out(
            f"  VERDICT-CAVEAT: {len(partial)} arm-pass(es) are missing from otherwise complete "
            f"passes ({', '.join(arms[: args.max_listed])}). Every comparison involving them was "
            f"intersected to the passes carrying both arms and is marked UNDER-REPLICATED — read "
            f"section 8's per-pass verdicts before publishing, and do not read a pooled "
            f"INCONCLUSIVE here as parity."
        )
    if verdict.get("under_replicated_cells") and not verdict.get("headline_eligible", True):
        # The condition Scott put on intersecting: an intersection down to two
        # passes is median-of-two, which is the mean, which is the breakdown point
        # the third pass was bought to fix. Such a cell may carry a number; it may
        # not carry the headline unqualified.
        out(
            f"  VERDICT-CAVEAT: {verdict['under_replicated_cells']} of "
            f"{verdict['cells_comparable']} comparable cells rest on "
            f"{'/'.join(verdict['under_replicated_passes'])} passes, not the full set. The median of "
            f"two passes is their mean and can be moved by one bad box, so the line above is not a "
            f"full-replication claim: section 8 decides whether the headline reproduced."
        )
    short = [r for r in replicates if r.get("under_replicated")]
    if short and _direction(verdict["code"]) is not None:
        # Only when a claim is actually on the table. A NO-DATA or INCONCLUSIVE
        # line is not being published as a number, and printing this under every
        # one of them would make it wallpaper -- which is how a caveat stops being
        # read. Deliberately not an exit bit: see DEFAULT_REPLICATE_PASSES.
        want = args.replicate_passes
        out(
            f"  VERDICT-CAVEAT: {', '.join(str(r['instance']) for r in short)} carry fewer than "
            f"{want} independent passes (section 8). The line above is a claim from "
            f"under-replicated data; the spend policy buys {want} passes because two cannot "
            f"outvote one bad box."
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
    by_routine = [s for s in verdict["coherent_subsets"] if s["axis"] == "routine"]
    if by_routine and verdict["code"] != "NO-DATA":
        # Named because this is the sentence the write-up quotes: "worth doing,
        # and here" needs the routines, not a campaign-wide fraction.
        out(
            "  CONSEQUENCE: the difference is routine-localised — "
            + ", ".join(
                f"{s['value']} ({s['direction']} set ahead in {s['wins']}/{s['comparable']} cells)"
                for s in by_routine[: args.max_listed]
            )
            + ' — so the answer to "where" is these kernels, not the library as a whole.'
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
        # This is the one CONSEQUENCE the timing floor can fabricate, so it is the
        # one that carries the band's status. small-minus-large straddles n=256,
        # where MIN_SECONDS also steps, and "small is slower" and "small was
        # measured over a shorter window" predict the same sign. The band is the
        # only thing that tells them apart, so the sentence that would be quoted
        # says out loud whether the band backs it.
        if not overlap["confirmed"]:
            out(
                f"    ...BUT the timing-floor overlap band is {overlap['status']} (section 9), and "
                f"small-minus-large straddles the floor step at n=256. Do not publish this "
                f"consequence until the band confirms."
            )
        elif overlap["status"] == "AGREES-WITH-BIAS":
            # The ratio is the useful number -- "the lean is 15x smaller than the
            # effect" is what makes the effect safe -- but median_bias can be
            # exactly 0 when enough pairs land on 0 to carry the median while five
            # nonzero pairs still share a sign. Print the lean without the ratio
            # rather than dividing by it.
            bias = overlap["median_bias"]
            ratio = f", {abs(med / bias):.0f}x smaller than the effect above" if bias else ""
            out(
                f"    ...and the band confirms it is not the instrument: the floors agree within "
                f"band, with a {100 * bias:+.1f}% consistent lean{ratio}."
            )
    out(
        f"  EXIT: {exit_code} (0 clean; 2 poisoned/inadmissible, 4 coverage hole, "
        f"8 provenance, 16 does-not-reproduce, 32 floor-band unconfirmed, OR-ed. "
        f"64 mixed case matrices is exclusive: nothing else is computed)"
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
        "--subset-min-cells",
        type=int,
        default=DEFAULT_SUBSET_MIN_CELLS,
        help=f"comparable cells one routine/regime/instance must hold before a direction of its own "
        f"blocks the NULL verdict (default {DEFAULT_SUBSET_MIN_CELLS})",
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
        "--replicate-passes",
        type=int,
        default=DEFAULT_REPLICATE_PASSES,
        help=f"independent passes the spend policy buys per instance_type; section 8 names "
        f"the shortfall, it is never an exit bit (default {DEFAULT_REPLICATE_PASSES})",
    )
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

    # Before anything is aggregated, and before section 0, because a mixed-matrix
    # directory makes every subsequent number a pooled comparison across two
    # different case sets. See matrix_ids() for why this refuses instead of warning.
    mids = matrix_ids(inp.bench)
    if len(mids) > 1:
        lines = [
            f"REFUSING TO ANALYSE {args.results}: {len(mids)} different case matrices in one "
            "directory.",
            "",
            "Sections 1-7 pool by median across passes and section 8 compares passes. Neither "
            "is meaningful across two case matrices: cells present in one and absent in the "
            "other drop out of every intersection silently, and what survives is whatever the "
            "two matrices happen to share. That number would look like every other number in "
            "this report.",
            "",
            "matrix_id                records   cases  instances / runs",
        ]
        for mid in sorted(mids, key=lambda k: -mids[k]["records"]):
            e = mids[mid]
            lines.append(
                f"  {mid:23s} {e['records']:7d}  {','.join(str(c) for c in e['cases']) or '?':>6} "
                f"  {' '.join(e['instances'])} / {' '.join(e['runs'])}"
            )
        lines += [
            "",
            "Separate the directory by matrix_id and analyse each on its own. Do not merge the "
            "reports: two matrices are two experiments.",
        ]
        for ln in lines:
            print(ln, file=sys.stderr)
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
                            "bench_records": len(inp.bench),
                            # Same path as the clean report's, deliberately. A
                            # consumer that reads inputs.matrix_ids must not have to
                            # know which exit code moved the field.
                            "matrix_ids": mids,
                            "unparseable_lines": inp.bad_lines,
                            "foreign_roles": dict(inp.foreign_roles),
                        },
                        "verdict": {
                            "code": "MIXED-MATRIX",
                            "why": f"{len(mids)} case matrices in one results directory; "
                            "pooling across them is not a comparison",
                        },
                        "exit_code": 64,
                    },
                    indent=2,
                    default=str,
                )
                + "\n"
            )
        return 64

    hosts = build_hosts(inp)
    for inst in {r.get("instance") for r in inp.bench}:
        hosts.setdefault(inst, Host(instance=inst))
    exc = Excluded()
    # The probe leaves the pool before anything is aggregated; see
    # split_floor_probe() for why the tag and not the floor key does this.
    matrix_recs, probe_recs = split_floor_probe(inp.bench)
    cells = build_cells(matrix_recs, hosts, exc)
    # Deliberately the same `exc`, not a throwaway. A probe record that failed
    # verification is a poisoned record under standing order 4 whatever sweep wrote
    # it -- it is the same dgemm on the same arm -- so it must set the same exit
    # bit. The exclusion bookkeeping is keyed on (condition, arm) and a probe
    # condition carries the probe's floor, so nothing it adds is ever looked up by
    # the coverage census, which builds its cell set from the matrix cells only.
    probe_cells = build_cells(probe_recs, hosts, exc)
    overlap = compute_floor_overlap(probe_cells, args)
    explain, _manifest, pass_explain = build_absence(inp)

    lines = []
    out = lines.append
    run_ids = sorted({r.get("run_id") for r in inp.bench}, key=str)
    out(
        f"graviton-blas-bench decomposition — {len(inp.bench)} records, {len(cells)} cells, "
        f"{len(run_ids)} run_ids, {len(hosts)} instance types, {len(inp.envs)} env files"
    )
    if probe_recs:
        out(
            f"  of which {len(probe_recs)} are probe records held out of the cross "
            f"({len(probe_cells)} cells) — see section 9"
        )
    out(
        f"min-effect floor {100 * args.min_effect:.0f}%, widened per cell to observed dispersion; "
        f"min-sizes {args.min_sizes}; win-fraction {args.win_fraction}"
    )
    out(f"run_ids: {', '.join(map(str, run_ids))}")

    host_payload = report_hosts(hosts, {r.get("instance") for r in inp.bench}, mids, out)
    groups = cell_groups(cells)
    deficits = report_deficit_by_routine(cells, groups, hosts, explain, pass_explain, args, out)
    cross = report_target_cross(cells, groups, hosts, explain, pass_explain, inp, args, out)
    lda = report_lda_penalty(cells, hosts, args, out)
    regimes = report_regime_profile(deficits, cross, args, out)
    scaling = compute_scaling(cells, inp.roof, args)
    anomalies, coverage_table, unver_cells = report_anomalies(
        inp, cells, hosts, exc, scaling, overlap, args, out
    )
    report_scaling(scaling, out)
    coverage = report_coverage(cells, inp, explain, hosts, exc, args, out)
    replicates = report_replicates(inp, hosts, explain, pass_explain, args, out)
    report_floor_overlap(overlap, args, out)
    verdict = compute_verdict(cross, hosts, exc, args)

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
        a["kind"]
        in (
            "no_provenance",
            "blas_sha_conflict",
            "env_unparseable",
            "sve_kernels_unknown",
            "dynamic_probe_unavailable",
        )
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
    if overlap["status"] in ("DISAGREES", "ORDER-CONFOUNDED", "INCOMPLETE"):
        # Bit 32 means exactly "the timing-floor overlap band did not confirm that
        # the two floors agree". Its own bit for the same reason 16 is: every arm
        # ran, every record is accounted for, no host is inadmissible, and the
        # instrument is nonetheless unvalidated at the one size the campaign most
        # needs it validated at.
        #
        # ABSENT deliberately does NOT set it. A dataset written before bench.c
        # grew the probe has no such records and must keep analysing exactly as it
        # did; making its absence an error would mean this file could no longer read
        # its own earlier output. Requiring the probe to be PRESENT is a gate's job
        # (gates/p2.sh), where the requirement can be stated about the dataset being
        # collected rather than about every dataset ever.
        exit_code |= 32

    report_verdict(
        verdict, lda, regimes, coverage, anomalies, replicates, overlap, exit_code, args, out
    )
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
                # One key by construction -- more than one is the exclusive exit-64
                # refusal above, so this is here to say WHICH matrix produced the
                # report rather than to be checked for length.
                "matrix_ids": mids,
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
            "floor_overlap": overlap,
            "exit_code": exit_code,
        }
        args.json.write_text(json.dumps(payload, indent=2, default=str) + "\n")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
