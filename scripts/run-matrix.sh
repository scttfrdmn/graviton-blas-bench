#!/usr/bin/env bash
# graviton-blas-bench — run the full matrix on one host and append NDJSON to results/.
#
# Matrix on this host:
#   artifact x coretype x threads x routine x size
# where `artifact` is a built library (see build-libs.sh) and `coretype` is the
# runtime OPENBLAS_CORETYPE forced onto the DYNAMIC_ARCH binary. The hardware
# axis is "which instance you ran this on"; the analysis joins across hosts.
#
# THREE THINGS THIS SCRIPT EXISTS TO GET RIGHT, each of which was a confound
# the size of the effect being measured:
#
# 1. PINNING IS EXTERNAL AND UNIFORM. The previous version set OMP_PROC_BIND and
#    OMP_PLACES on every arm. Only the OpenMP arms obey those -- ArmPL and the
#    USE_OPENMP=1 OpenBLAS -- while shipping pthread OpenBLAS ignores them
#    entirely. So the reference arm was pinned and the arm under test was not,
#    which is a systematic advantage to ArmPL of roughly the size of the deficit
#    being investigated. Binding is now done from outside the process with
#    numactl/taskset, identically for every arm regardless of threading backend,
#    and OMP_PROC_BIND is explicitly disabled so that no arm gets a 1:1 pinning
#    its competitors cannot have. What pinning is worth is then measured
#    separately by the DYNAMIC_OMP arm rather than left as a bias.
#
#    A uniform numactl memory policy also closes a second gap for free: bench.c
#    first-touches its matrices serially and roofline.c does so in parallel, so
#    on a multi-node host the two used to land their pages on different nodes.
#    Under one explicit --membind/--interleave policy they no longer can.
#
# 2. NOTHING IS LABELLED WITH WHAT WE ASKED FOR. force_coretype() ignores names
#    it does not know, and a non-DYNAMIC_ARCH build ignores OPENBLAS_CORETYPE
#    altogether. Every coretype is verified with gbb-coreprobe before the arm
#    runs, and the record carries what the library reported, not the request.
#
# 3. A MISSING ARM IS EXPLAINED, NOT MERELY ABSENT. Every arm this script
#    declines to run writes a census line saying why. Without that, decompose.py
#    cannot tell "V1 and V2 are at parity" from "the V1 arm never ran", and those
#    two lead to opposite conclusions.

set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BIN="$ROOT/bin"
PREFIX="${GBB_PREFIX:-$HOME/graviton-blas-bench-libs}"
RESULTS_ROOT="${GBB_RESULTS:-$ROOT/results}"
RUN_ID_BASE="${GBB_RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)-$(hostname -s)}"
BUILD_MANIFEST="$PREFIX/build-manifest.ndjson"

# S3 shipping. An instance is terminated on completion and a spot reclaim can
# take it sooner, so results must be durable as they are produced, not at the
# end of a multi-hour sweep. --region is always explicit: the profile default
# region is not the campaign region.
S3_URI="${GBB_S3_URI:-}"
AWS_REGION_ARG="${GBB_AWS_REGION:-us-east-1}"

GBB_HOST="$(hostname -s)"; export GBB_HOST

log() { printf '[gbb] %s\n' "$*" >&2; }
# No ship() here: the EXIT trap already runs it on every path out, including
# this one. Calling it twice would double every upload.
die() { printf '[gbb] FATAL: %s\n' "$*" >&2; exit 3; }

jstr() {
  local v="${1:-}"
  v="${v//\\/}"; v="${v//\"/}"; v="${v//$'\t'/ }"; v="${v//$'\r'/ }"
  printf '%s' "${v//$'\n'/ }"
}

# ---- role: campaign data, or an instrument check ---------------------------
# The local Grace/GB10 boxes (castor, pollux) are instrument checks -- they
# exercise the harness on real SVE2 silicon and are useful for that, and they
# are not campaign data. Mixing the two would put a heterogeneous 20-core
# desktop part into a table of five EC2 Neoverse hosts, which is not a mistake
# any amount of care in the analysis can undo afterwards.
#
# So the separation is structural rather than procedural: the role is decided
# here, from evidence this script cannot be talked out of, and it determines the
# output directory, the run_id prefix, the S3 prefix and a `role` field stamped
# into every record by bench.c and roofline.c. Campaign role requires BOTH:
#
#   1. IMDS reports an instance type in the campaign set, and
#   2. cpu0's MIDR part is one of the Graviton parts.
#
# Either condition failing means instrument. A laptop fails both. castor fails
# (2) -- Cortex-X925/A725, parts 0xd85/0xd87 -- so even a forged instance type
# cannot promote it. A genuinely new Graviton part also fails (2), which is
# correct and deliberate: standing order 8 makes an unrecognised MIDR a
# stop-and-escalate, so adding one is a human edit here and in capture-env.sh.
#
# AND THERE IS A THIRD CONDITION, added 2026-08-20 because the second one turned
# out to rest on an assumption about the test host rather than on construction.
# `GBB_TEST_IMDS_TYPE` forges (1); the comment on it claimed (2) "on any machine a
# test runs on it does not" hold. That is false, and CI found it: gate p0 runs on
# `ubuntu-24.04-arm`, whose cpu0 MIDR part IS in GRAVITON_PARTS, so the forged type
# promoted the runner to campaign role and the stub suite's own anti-forgery
# assertion failed. The same hole is open on any Neoverse host in the part list --
# including, most sharply, the campaign hosts themselves: running the stub suite on
# the c8g would have written campaign-namespace records from a test.
#
# So a forged instance type now refuses campaign role by construction, whatever the
# silicon says. The MIDR leg is kept and checked FIRST, so that where it does apply
# -- castor, a laptop, an x86 runner -- it is still what fires and is still what the
# suite exercises; the forgery leg catches the hosts where the MIDR leg cannot.
# CLAUDE.md's rule for these boxes is "quarantine by construction, not by
# discipline", and a hook whose safety depends on which machine ran the tests was
# discipline wearing construction's clothes.
CAMPAIGN_TYPES="${GBB_CAMPAIGN_TYPES:-c6g.metal c7g.metal hpc7g.16xlarge c8g.metal-48xl c9g.metal-48xl}"
GRAVITON_PARTS="0xd0c 0xd40 0xd49 0xd4f 0xd83 0xd84"

imds_instance_type() {
  # GBB_TEST_IMDS_TYPE exists so the stub suite can reach this code without an
  # EC2 network. It cannot manufacture campaign data, and that is now enforced by
  # condition (3) below rather than by an assumption about the test host -- see the
  # block above for the assumption and how CI falsified it.
  if [ -n "${GBB_TEST_IMDS_TYPE:-}" ]; then printf '%s' "$GBB_TEST_IMDS_TYPE"; return 0; fi
  command -v curl >/dev/null 2>&1 || return 1
  local tok
  tok="$(curl -sf -m 1 -X PUT http://169.254.169.254/latest/api/token \
          -H 'X-aws-ec2-metadata-token-ttl-seconds: 60' 2>/dev/null)" || return 1
  [ -n "$tok" ] || return 1
  curl -sf -m 1 -H "X-aws-ec2-metadata-token: $tok" \
    http://169.254.169.254/latest/meta-data/instance-type 2>/dev/null
}

IMDS_TYPE="$(imds_instance_type || true)"
MIDR_RAW="$(cat /sys/devices/system/cpu/cpu0/regs/identification/midr_el1 2>/dev/null || true)"
MIDR_PART=""
[ -n "$MIDR_RAW" ] && MIDR_PART="$(printf '0x%x' $(( ( $((MIDR_RAW)) >> 4 ) & 0xFFF )))"
# GBB_TEST_MIDR_PART lets the stub suite reach condition (3) on a host whose real
# MIDR stops it at condition (2) -- otherwise leg 3 would only ever be exercised on
# whichever runner happens to have a Neoverse part in the list, which is the shape of
# host-dependence that put the hole there in the first place.
#
# IT CAN ONLY DEMOTE, and that is a property of the ordering rather than of care.
# Promotion needs a campaign instance type from real IMDS, and this variable does not
# touch that; combined with GBB_TEST_IMDS_TYPE it lands on (3) and refuses. On a real
# campaign host the real part is already in the list, so setting this can only move
# the answer from campaign to instrument. There is no value of it that produces
# campaign role on a host that would not have had it anyway.
if [ -n "${GBB_TEST_MIDR_PART:-}" ]; then
  MIDR_PART="$GBB_TEST_MIDR_PART"
  log "WARNING: cpu0 MIDR part overridden to '$MIDR_PART' by GBB_TEST_MIDR_PART (test hook)"
fi

ROLE=instrument
ROLE_REASON=""
if [ -z "$IMDS_TYPE" ]; then
  ROLE_REASON="no EC2 instance metadata: this host is not an EC2 instance"
elif ! printf '%s' " $CAMPAIGN_TYPES " | grep -qF " $IMDS_TYPE "; then
  ROLE_REASON="instance type '$IMDS_TYPE' is not one of the campaign families ($CAMPAIGN_TYPES)"
elif [ -z "$MIDR_PART" ]; then
  ROLE_REASON="cpu0 MIDR unreadable, so the silicon cannot be confirmed"
elif ! printf '%s' " $GRAVITON_PARTS " | grep -qF " $MIDR_PART "; then
  ROLE_REASON="cpu0 MIDR part $MIDR_PART is not a known Graviton part ($GRAVITON_PARTS)"
elif [ -n "${GBB_TEST_IMDS_TYPE:-}" ]; then
  # Last, so the two evidence-based refusals above keep firing wherever they apply
  # and keep being what the stub suite exercises. This one exists for the hosts
  # where they cannot fire: a real Neoverse part in the list, which is every
  # campaign host and also GitHub's arm64 runner.
  ROLE_REASON="instance type '$IMDS_TYPE' came from GBB_TEST_IMDS_TYPE, which is a test hook and not evidence; campaign role requires an answer from IMDS itself, and cpu0 MIDR part $MIDR_PART cannot supply the other half on its own"
else
  ROLE=campaign
fi

# GBB_ROLE is an assertion checked against the evidence, never a selector. An
# operator who believes this is a campaign host and is wrong should be stopped,
# not obeyed.
if [ -n "${GBB_ROLE:-}" ] && [ "$GBB_ROLE" != "$ROLE" ]; then
  die "GBB_ROLE='$GBB_ROLE' but the evidence says '$ROLE': $ROLE_REASON
     GBB_ROLE is an assertion, not an override. If this really is a campaign
     host, fix the detection (CAMPAIGN_TYPES / GRAVITON_PARTS in this script)
     rather than the label -- a mislabelled host is a plausible wrong answer."
fi
export GBB_ROLE="$ROLE"

if [ "$ROLE" = campaign ]; then
  RESULTS="$RESULTS_ROOT"
  RUN_ID="$RUN_ID_BASE"
else
  RESULTS="$RESULTS_ROOT/instrument"
  case "$RUN_ID_BASE" in instr-*) RUN_ID="$RUN_ID_BASE" ;; *) RUN_ID="instr-$RUN_ID_BASE" ;; esac
fi

mkdir -p "$RESULTS"
export GBB_RUN_ID="$RUN_ID"

OUT="$RESULTS/bench-$RUN_ID.ndjson"
ROOFOUT="$RESULTS/roofline-$RUN_ID.ndjson"
CENSUS="$RESULTS/census-$RUN_ID.ndjson"
ENVFILE="$RESULTS/env-$RUN_ID.json"
MANIFEST="$RESULTS/manifest-$RUN_ID.ndjson"
TOPOFILE="$RESULTS/topology-$RUN_ID.txt"
STDERRLOG="$RESULTS/stderr-$RUN_ID.log"

if [ "$ROLE" = campaign ]; then
  log "role=campaign (instance $IMDS_TYPE, MIDR part $MIDR_PART) -> $RESULTS"
else
  log "role=INSTRUMENT -- $ROLE_REASON"
  log "     results go to $RESULTS with run_id '$RUN_ID' and role=instrument in"
  log "     every record. This is a harness check, not campaign data."
fi

# ---- durability -----------------------------------------------------------
# ship() is idempotent and cheap, and runs on every exit path including a
# trapped signal. Uploading the whole file each time rather than appending is
# deliberate: S3 objects are immutable, and a partial multipart append after a
# reclaim would be worse than a slightly stale complete file.
ship() {
  [ -n "$S3_URI" ] || return 0
  # The role is part of the S3 prefix as well as the local path: a bucket that
  # collects both must not interleave them under one prefix either.
  local dest="${S3_URI%/}/$ROLE/$GBB_HOST/$RUN_ID"
  local f
  for f in "$ENVFILE" "$MANIFEST" "$CENSUS" "$TOPOFILE" "$ROOFOUT" "$OUT" "$STDERRLOG"; do
    [ -s "$f" ] || continue
    aws s3 cp --region "$AWS_REGION_ARG" --only-show-errors \
      "$f" "$dest/$(basename "$f")" 2>>"$STDERRLOG" \
      || log "WARNING: S3 upload of $(basename "$f") failed; results remain local only"
  done
}
trap 'log "signal received -- shipping what exists then exiting"; ship; exit 130' INT TERM
trap ship EXIT

if [ -n "$S3_URI" ]; then
  command -v aws >/dev/null 2>&1 || die "GBB_S3_URI is set but the aws CLI is not installed."
  log "shipping results to ${S3_URI%/}/$ROLE/$GBB_HOST/$RUN_ID (region $AWS_REGION_ARG)"
else
  log "GBB_S3_URI unset -- results stay on this instance only. If it is terminated"
  log "before you collect them, the instance-hours are spent for nothing."
fi

census() {
  printf '{"record":"arm_outcome","run_id":"%s","role":"%s","host":"%s","instance":"%s",' \
    "$(jstr "$RUN_ID")" "$(jstr "$ROLE")" "$(jstr "$GBB_HOST")" \
    "$(jstr "${INSTANCE:-unknown}")" >> "$CENSUS"
  printf '"library":"%s","target":"%s","coretype":"%s","coretype_effective":"%s",' \
    "$(jstr "$1")" "$(jstr "$2")" "$(jstr "$3")" "$(jstr "$4")" >> "$CENSUS"
  printf '"threads":%s,"status":"%s","exit_code":%s,"records":%s,' \
    "$5" "$(jstr "$6")" "$7" "$8" >> "$CENSUS"
  printf '"thread_backend":"%s","pin_policy":"%s","reason":"%s"}\n' \
    "$(jstr "$9")" "$(jstr "${10}")" "$(jstr "${11}")" >> "$CENSUS"
}
: > "$CENSUS"

# ---- provenance first, always ---------------------------------------------
[ -r "$BUILD_MANIFEST" ] || die "no build manifest at $BUILD_MANIFEST -- run build-libs.sh first."
# The other side of build-libs.sh's lock. A sweep against a prefix that is being
# rebuilt is worse than a failed sweep: `make install` replaces the shared objects
# under a running arm, so some records describe the old library and some the new,
# every one of them stamped with the manifest's blas_sha. That is standing order 5
# provenance that is present and wrong, which no later check can detect.
if [ -d "$PREFIX/.gbb-build.lock" ]; then
  OWNER="$(cat "$PREFIX/.gbb-build.lock/owner" 2>/dev/null || echo 'unknown')"
  if [ "${GBB_IGNORE_BUILD_LOCK:-0}" = 1 ]; then
    log "WARNING: build lock held ($OWNER) and GBB_IGNORE_BUILD_LOCK=1 -- the libraries"
    log "         under $PREFIX may change during this sweep."
  else
    die "a build holds the lock on $PREFIX (held by: $OWNER).
     Libraries installed under a running sweep produce records whose blas_sha
     describes a tree they were not measured against. Wait for the build to
     finish. GBB_IGNORE_BUILD_LOCK=1 overrides, and should not be needed."
  fi
fi
# Stamped, not copied. build-libs.sh runs before anything knows which host this
# is, so its records carry no instance -- and the analysis concatenates every
# host's manifest into one stream, at which point "this build has no SVE
# kernels" is unattributable to a host and therefore not actionable. The
# instance stamped here is the interlock's IMDS answer, the same value
# capture-env.sh is cross-checked against below. Inserting after the opening
# brace keeps each line valid JSON without needing a JSON tool on the host.
sed "s/^{/{\"instance\":\"${IMDS_TYPE:-unknown}\",\"role\":\"$ROLE\",/" \
  "$BUILD_MANIFEST" > "$MANIFEST"

export GBB_OPENBLAS_DYNAMIC_DIR="$PREFIX/openblas-DYNAMIC"
bash "$ROOT/scripts/capture-env.sh" > "$ENVFILE"
ENV_RC=$?
log "environment captured to $ENVFILE (capture-env exit $ENV_RC)"

# capture-env.sh's exit status is load-bearing (standing order 8). Previously it
# was discarded, so the new exit codes stopped nothing and a multi-hour sweep
# would start on a host already known to produce incomparable numbers.
case "$ENV_RC" in
  0) ;;
  3)
    if [ "${GBB_FORCE_INVALID_HOST:-0}" = 1 ]; then
      log "WARNING: capture-env exited 3 (run-invalidating) and GBB_FORCE_INVALID_HOST=1"
      log "         was set. Timings from this host are NOT comparable to the others."
      census host host "" "" 0 forced_invalid_host 3 0 "" "" \
        "capture-env exit 3 overridden by GBB_FORCE_INVALID_HOST=1"
    else
      die "capture-env.sh exited 3: this host's timings would not be comparable
     (governor not 'performance', SMT on, or heterogeneous cores). See the
     warnings array in $ENVFILE. Set GBB_FORCE_INVALID_HOST=1 only if you have
     read them and intend to collect data you know is not comparable."
    fi
    ;;
  4)
    if [ -n "${GBB_ESCALATION_ACK:-}" ]; then
      log "WARNING: capture-env exited 4 (escalate) and GBB_ESCALATION_ACK is set."
      printf '{"record":"escalation_ack","run_id":"%s","role":"%s","host":"%s","note":"%s"}\n' \
        "$(jstr "$RUN_ID")" "$(jstr "$ROLE")" "$(jstr "$GBB_HOST")" \
        "$(jstr "$GBB_ESCALATION_ACK")" >> "$CENSUS"
    else
      die "capture-env.sh exited 4: standing order 8 says stop and escalate before
     running anything. An unrecognised MIDR, or generic ARMV8 selected on a host
     that has SVE. Read the warnings array in $ENVFILE -- that finding outweighs
     every kernel question in this repo. To proceed after escalating, set
     GBB_ESCALATION_ACK to a note explaining what was decided; it is recorded."
    fi
    ;;
  *) die "capture-env.sh exited $ENV_RC (usage error or crash). Nothing was measured." ;;
esac

# JSON booleans come back as `true`/`false`, not Python's `True`/`False`: shell
# comparisons downstream are against the JSON spelling, and a capitalised `True`
# would silently fail every one of them -- which would have quietly dropped the
# SVE coretypes from the sweep on every host that has SVE.
envq() {
  python3 - "$ENVFILE" "$1" <<'PY'
import json, sys
v = json.load(open(sys.argv[1])).get(sys.argv[2])
print("" if v is None else "true" if v is True else "false" if v is False else v)
PY
}

# A command substitution swallows the failure of what it wraps. If python3 is
# absent, or ENVFILE is truncated because capture-env.sh died mid-write, every
# envq below returns "" and the sweep proceeds: HAS_SVE="" silently drops every
# SVE coretype on a host that has SVE, CORES="" produces a one-rung thread
# ladder, and the run finishes looking like a complete dataset that happens to
# contain no SVE arms. That is the absent-vs-null confusion standing order 11
# exists to prevent, arriving through the shell rather than through the census.
# So the precondition is checked once, loudly, before any caller can mistake a
# read failure for a missing key. After this, "" from envq means the key really
# is absent or null.
command -v python3 >/dev/null 2>&1 \
  || die "python3 not found. Every provenance field this sweep stamps into its
     records is read from $ENVFILE with it, and without it they would all be
     stamped empty while the sweep ran to completion."
envq_selftest="$(envq instance_type 2>/dev/null)" || envq_selftest="__FAILED__"
[ "$envq_selftest" != "__FAILED__" ] \
  || die "capture-env.sh produced $ENVFILE but it cannot be read as a JSON object.
     Every provenance field would be stamped empty while the sweep ran to
     completion."

# envq_req <key> -- for the fields that decide what gets measured rather than
# merely describing it. An empty one is a stop, not a default: a wrong thread
# ladder or a missing SVE axis is not visible in the output it produces.
envq_req() {
  local v
  v="$(envq "$1")"
  [ -n "$v" ] || die "capture-env.sh recorded no '$1' in $ENVFILE. That field decides what this
     sweep measures, so defaulting it would produce a dataset that is wrong
     rather than one that is merely short."
  printf '%s' "$v"
}

INSTANCE="$(envq instance_type)"; [ -n "$INSTANCE" ] || INSTANCE=unknown
# Two independent IMDS reads must agree. The role interlock above read the
# instance type itself rather than waiting for capture-env.sh, precisely so that
# it could not be steered by anything downstream -- which means there are now two
# sources for the same fact, and a disagreement is either a bug here or a host
# that changed identity mid-run. On a campaign host that invalidates the record.
if [ -n "$IMDS_TYPE" ] && [ "$INSTANCE" != "$IMDS_TYPE" ]; then
  if [ "$ROLE" = campaign ]; then
    die "instance type disagrees between the two IMDS reads: the role interlock saw
     '$IMDS_TYPE', capture-env.sh recorded '$INSTANCE'. One of them is wrong and
     every record on this host would carry the wrong host identity."
  fi
  log "WARNING: instance type '$INSTANCE' from capture-env != '$IMDS_TYPE' from the"
  log "         role interlock. Instrument run, so this is noted and not fatal."
fi
CORES="$(envq_req cores_total)"
CPUS_AFFINITY="$(envq cpus_affinity)"
FORCING="$(envq openblas_coretype_forcing)"
HAS_SVE="$(envq_req has_sve)"
HAS_SVE2="$(envq_req has_sve2)"
export GBB_INSTANCE="$INSTANCE"
ship

# numactl -H is required evidence for gate P2 -- whether c8g.metal-48xl at 192 vCPU
# is one socket or two decides how to read every multithreaded number on it.
{ echo "=== numactl -H ==="; numactl -H 2>&1 || echo "(numactl unavailable)"
  echo; echo "=== lscpu ==="; lscpu 2>&1 || echo "(lscpu unavailable)"; } > "$TOPOFILE"

# ---- thread ladder --------------------------------------------------------
# 1 core isolates kernel quality. Half and full socket expose the threading
# layer and NUMA. 64 is included on every host regardless of size so the
# cross-generation comparison has one directly comparable point -- c6g/c7g/hpc7g
# top out at 64 while c8g/c9g reach 192. Capped by the affinity mask rather than
# by nproc: under a cpuset the two differ, and a ladder rung above the mask
# oversubscribes and measures the scheduler.
LADDER_CAP="$CORES"
if [ -n "$CPUS_AFFINITY" ] && [ "$CPUS_AFFINITY" -lt "$CORES" ] 2>/dev/null; then
  LADDER_CAP="$CPUS_AFFINITY"
  log "WARNING: affinity mask allows $CPUS_AFFINITY of $CORES cores; capping the ladder there."
fi
build_threads() {
  local n="$1" out="1" t
  for t in 8 16 32 64 96 128 192; do
    [ "$t" -le "$n" ] && out="$out $t"
  done
  [ "$n" -gt 1 ] && case " $out " in *" $n "*) ;; *) out="$out $n";; esac
  echo "$out"
}
THREADS="${GBB_THREADS_LADDER:-$(build_threads "$LADDER_CAP")}"
log "instance=$INSTANCE cores=$CORES cap=$LADDER_CAP threads=[$THREADS]"

# ---- the pinning policy ---------------------------------------------------
# Derived from the real per-node CPU lists in `numactl -H`, not from an
# assumption that node N owns a contiguous ascending range. Threads are filled
# node by node, so a thread count that fits one node stays on one node and
# memory is bound there; a count that spans nodes interleaves memory across
# exactly the nodes used. The policy string is recorded in every record it
# applies to, because "pinned" without saying how is not provenance.
pin_for() {
  python3 - "$TOPOFILE" "$1" <<'PY'
import re, sys
topo = open(sys.argv[1]).read()
want = int(sys.argv[2])
nodes = []
for m in re.finditer(r'^node (\d+) cpus:(.*)$', topo, re.M):
    cpus = [int(x) for x in m.group(2).split()]
    if cpus:
        nodes.append((int(m.group(1)), cpus))
nodes.sort()
chosen, used = [], []
for nid, cpus in nodes:
    if len(chosen) >= want:
        break
    take = cpus[: want - len(chosen)]
    if take:
        chosen += take
        used.append(nid)
if len(chosen) < want:
    # Not enough enumerated CPUs to honour the request. Emit nothing rather than
    # a policy that silently covers fewer cores than the arm will spawn threads.
    print("")
    raise SystemExit(0)
def compress(xs):
    xs = sorted(xs); out = []; s = p = xs[0]
    for x in xs[1:]:
        if x == p + 1: p = x; continue
        out.append(f"{s}-{p}" if s != p else f"{s}"); s = p = x
    out.append(f"{s}-{p}" if s != p else f"{s}")
    return ",".join(out)
cpuspec = compress(chosen)
nodespec = ",".join(str(n) for n in used)
mem = f"--membind={nodespec}" if len(used) == 1 else f"--interleave={nodespec}"
print(f"numactl --physcpubind={cpuspec} {mem}")
PY
}

HAVE_NUMACTL=0; command -v numactl >/dev/null 2>&1 && HAVE_NUMACTL=1
HAVE_TASKSET=0; command -v taskset >/dev/null 2>&1 && HAVE_TASKSET=1

# PIN_CMD[T] / PIN_DESC[T] for each rung of the ladder.
declare -A PIN_CMD PIN_DESC
for T in $THREADS; do
  P=""
  [ "$HAVE_NUMACTL" -eq 1 ] && P="$(pin_for "$T")"
  if [ -z "$P" ] && [ "$HAVE_TASKSET" -eq 1 ]; then
    P="taskset -c 0-$((T - 1))"
    log "WARNING: falling back to '$P' at threads=$T (no numactl, or its node CPU"
    log "         lists did not cover $T CPUs). No memory policy is applied, so"
    log "         first-touch placement is whatever the scheduler chose."
  fi
  if [ -z "$P" ]; then
    log "WARNING: no external binding available at threads=$T. Threads may migrate"
    log "         across NUMA nodes mid-measurement; this is recorded as pin=none."
    PIN_CMD[$T]=""
    PIN_DESC[$T]="none"
  else
    PIN_CMD[$T]="$P"
    # OMP_PROC_BIND=false is part of the policy, not an omission. See the header:
    # leaving it at `close` pins only the OpenMP arms and biases the comparison.
    PIN_DESC[$T]="$P;omp_bind=false"
  fi
done
log "pin policy at ${THREADS%% *} thread: ${PIN_DESC[${THREADS%% *}]}"

# ---- which coretypes can be forced here -----------------------------------
# Requested set, filtered by what the host's ISA can actually execute. Forcing
# NEOVERSEV1 on a NEON-only c6g would SIGILL, and the crash would be recorded as
# an arm failure rather than as the category error it is.
CORETYPES="ARMV8 NEOVERSEN1"
if [ "$HAS_SVE" = true ]; then
  CORETYPES="$CORETYPES ARMV8SVE NEOVERSEV1"
fi
if [ "$HAS_SVE2" = true ]; then
  # Both, deliberately, and only one of them can survive. cc3fc1e `#define`s
  # gotoblas_NEOVERSEV2 to gotoblas_NEOVERSEN2 unconditionally, so the two
  # requests select one pointer and the second is necessarily an alias duplicate
  # (see alias_ok below). Requesting both anyway is what turns that identity from
  # an assumption in this comment into an `alias_duplicate` census record stating
  # it, measured on the host -- and it is what would catch a future OpenBLAS that
  # gave V2 kernels of its own, where suddenly both would verify exactly and both
  # would run.
  CORETYPES="$CORETYPES NEOVERSEV2 NEOVERSEN2"
fi
CORETYPES="${GBB_CORETYPES:-$CORETYPES}"

PROBE="$BIN/gbb-coreprobe-DYNAMIC"
DYN_BACKEND=pthreads

UNFORCED_EFF=""
if [ -x "$PROBE" ]; then
  UNFORCED_EFF="$("$PROBE" 2>/dev/null | cut -d'|' -f1)"
  log "unforced DYNAMIC_ARCH selects: '${UNFORCED_EFF:-unknown}'"
fi

# arch_selected must be measured on the library it describes. Every non-DYNAMIC
# arm used to be labelled with the DYNAMIC binary's unforced selection, which is
# provenance taken from a different library -- wrong for a static TARGET= build
# (not DYNAMIC_ARCH at all, so it reports its fixed target) and meaningless for
# ArmPL and BLIS, which export no such symbol. build-libs.sh now builds one probe
# per OpenBLAS variant; anything else is `n/a` or `unprobed`, never a guess.
probe_variant() {
  # Two statements, not one. In `local v="$1" pr="...$v"` the reference to $v is
  # expanded while v is a declared-but-unset local, which under `set -u` aborts
  # the function -- and inside $( ) that failure is invisible: the caller just
  # gets an empty string and labels the arm "unknown".
  local v="$1"
  local pr="$BIN/gbb-coreprobe-$v" out
  [ -x "$pr" ] || { printf 'unprobed'; return 0; }
  out="$("$pr" 2>/dev/null | cut -d'|' -f1)"
  printf '%s' "${out:-unprobed}"
}

# Verify each coretype is honoured, and record what came back. `unforced` is its
# own arm: it is what a NumPy wheel gets on this host, which is a finding in its
# own right rather than bookkeeping. It is deliberately NOT deduplicated against
# the forced arms even when it selects the same kernel set -- comparing it with
# the forced arm that lands in the same place is how "does forcing cost
# anything" gets answered.
declare -A CT_EFFECTIVE
declare -A EFF_CLAIMED
VERIFIED_CORETYPES=""

# alias_ok <requested-uppercase> <reported-lowercase>
# The name aliases OpenBLAS's arm64 kernel tables are known to contain in the
# audited tree. Declared rather than inferred: "the reported name differs from
# the request" cannot by itself tell an alias from an ignored request, and the
# difference decides whether an arm is a measurement or a duplicate.
#
# Read off cc3fc1e's driver/others/dynamic_arm64.c, not assumed. Three facts
# there, and each one moves an entry in this list:
#
#   1. `#define gotoblas_NEOVERSEV2 gotoblas_NEOVERSEN2` (line 229) is
#      UNCONDITIONAL -- outside every DYN_*/NO_SVE branch, and there is no
#      `extern gotoblas_t gotoblas_NEOVERSEV2` anywhere in the file. So V2 has no
#      table of its own in any arm64 DYNAMIC_ARCH build: the two names are one
#      pointer. That is stronger than "KERNEL.NEOVERSEV2 is a one-line include of
#      KERNEL.NEOVERSEN2", which is a makefile fact; this is a C-level identity.
#   2. `gotoblas_corename()` tests V2 (corename[12]) BEFORE N2 (corename[13]) on
#      that single pointer, so it can never return "neoversen2" in this tree.
#      Both requests report back `neoversev2`. The N2:neoversev2 direction is the
#      one real hardware takes -- and it is what c8g.metal-48xl actually did on
#      2026-08-20, where its absence here refused the arm as "request NOT
#      honoured" when `force_coretype("NEOVERSEN2")` had returned exactly the
#      table asked for (found=13 -> &gotoblas_NEOVERSEN2). Both directions are
#      declared because which one appears depends on that check order, which is
#      not a contract OpenBLAS owes anyone.
#   3. NEOVERSEV3 is NOT in `corename[]` at all, so `force_coretype("NEOVERSEV3")`
#      finds nothing, warns "Core not found", and returns NULL -- whereupon
#      `gotoblas_dynamic_init()` sets `gotoblas = &gotoblas_ARMV8` and reports
#      `armv8`. An unknown name does not degrade to auto-detection; it degrades to
#      generic ARMV8, which is standing order 8's escalation trigger and must stay
#      refused. The previous `NEOVERSEV3:neoversev2|NEOVERSEV3:neoversen2` entries
#      described a mapping that does not exist and could only ever have permitted
#      an arm whose request was provably ignored. Removed.
alias_ok() {
  case "$1:$2" in
    NEOVERSEN2:neoversev2) return 0 ;;  # what this tree does: corename checks V2 first
    NEOVERSEV2:neoversen2) return 0 ;;  # the same identity seen from the other side
    *) return 1 ;;
  esac
}
if [ "$FORCING" = unavailable ]; then
  log "WARNING: capture-env reports OPENBLAS_CORETYPE forcing is UNAVAILABLE on this"
  log "         build. The entire coretype axis is unmeasurable here -- running it"
  log "         anyway would label every arm with a request the library ignored."
  for CT in $CORETYPES; do
    census openblas DYNAMIC "$CT" "" 0 unrunnable 0 0 "$DYN_BACKEND" "" \
      "OPENBLAS_CORETYPE forcing unavailable on this build (openblas_coretype_forcing=unavailable)"
  done
  CORETYPES=""
fi
if [ ! -x "$PROBE" ]; then
  log "WARNING: $PROBE is absent, so no coretype can be verified."
  for CT in $CORETYPES; do
    census openblas DYNAMIC "$CT" "" 0 unrunnable 0 0 "$DYN_BACKEND" "" \
      "gbb-coreprobe-DYNAMIC not built; coretype could not be verified and will not be claimed"
  done
  CORETYPES=""
fi
for CT in $CORETYPES; do
  EFF="$(OPENBLAS_CORETYPE="$CT" "$PROBE" 2>/dev/null | cut -d'|' -f1)"
  RC=$?
  if [ $RC -ne 0 ] || [ -z "$EFF" ]; then
    log "  coretype $CT: probe failed (rc=$RC) -- not run"
    census openblas DYNAMIC "$CT" "" 0 unrunnable "$RC" 0 "$DYN_BACKEND" "" \
      "coreprobe failed for this coretype (SIGILL=132 means the kernel set needs ISA this host lacks)"
    continue
  fi
  CT_EFFECTIVE[$CT]="$EFF"
  # Compare case-insensitively: force_coretype() takes upper case and
  # openblas_get_corename() returns lower.
  LC_EFF="$(printf '%s' "$EFF" | tr '[:upper:]' '[:lower:]')"
  LC_CT="$(printf '%s' "$CT" | tr '[:upper:]' '[:lower:]')"

  # Three outcomes, not two.
  #
  # A request that reports back a DIFFERENT name is not automatically a failure:
  # on SVE2 silicon it is the campaign's central assumption coming true. V2 and N2
  # are one pointer in this tree, and `gotoblas_corename()` checks V2 first, so a
  # NEOVERSEN2 request reporting `neoversev2` is the expected result and is the
  # finding. An earlier version of this loop wrote that arm off as `unrunnable`,
  # which made the check meant to *detect* the aliasing the thing that suppressed
  # it -- and it did exactly that on c8g.metal-48xl on 2026-08-20, because the
  # alias list carried only the V2:neoversen2 direction and the hardware takes the
  # other one. Both are declared now; see alias_ok.
  #
  # But a request that lands somewhere unexpected is a different thing: it means
  # force_coretype() ignored the name, and the arm is then an unlabelled
  # duplicate of the unforced arm rather than a measurement of what was asked
  # for. So the aliases are declared here, and only a declared one is permitted.
  # Anything else is still refused -- an undeclared surprise is exactly the case
  # where guessing would be wrong.
  if alias_ok "$CT" "$LC_EFF"; then
    :
  elif [ "$LC_EFF" != "$LC_CT" ]; then
    log "  coretype $CT: library reports '$EFF' -- request NOT honoured, not run"
    census openblas DYNAMIC "$CT" "$EFF" 0 unrunnable 0 0 "$DYN_BACKEND" "" \
      "requested coretype $CT but openblas_get_corename() reports $EFF, which is not a declared alias of it; force_coretype() ignored the request and the arm would be an unlabelled duplicate of the unforced arm"
    continue
  fi

  # A second request landing on a kernel set already being measured is skipped.
  # Running the identical kernel table twice buys nothing and would read as two
  # independent arms. The order of CORETYPES is therefore load-bearing:
  # NEOVERSEV2 precedes NEOVERSEN2 so the surviving arm carries the label the
  # analysis compares on. The unforced arm is not in this map on purpose -- it is
  # kept even when it lands on the same set, because comparing it against the
  # forced arm that agrees with it is how "does forcing cost anything" is
  # answered.
  if [ -n "${EFF_CLAIMED[$LC_EFF]:-}" ]; then
    log "  coretype $CT: library reports '$EFF', already measured as ${EFF_CLAIMED[$LC_EFF]} -- alias, not run twice"
    census openblas DYNAMIC "$CT" "$EFF" 0 alias_duplicate 0 0 "$DYN_BACKEND" "" \
      "requested $CT; openblas_get_corename() reports '$EFF', which the ${EFF_CLAIMED[$LC_EFF]} arm is already measuring. The two requests select the same kernel set -- that is the finding, and measuring it twice would read as two independent arms."
    continue
  fi
  EFF_CLAIMED[$LC_EFF]="$CT"

  if [ "$LC_EFF" != "$LC_CT" ]; then
    log "  coretype $CT: library reports '$EFF' -- ALIAS. Running it; the record says both."
    census openblas DYNAMIC "$CT" "$EFF" 0 aliased 0 0 "$DYN_BACKEND" "" \
      "requested $CT, openblas_get_corename() reports '$EFF'. Running it: the arm is a valid measurement of '$EFF' and every record carries both the request and the reported name."
  else
    log "  coretype $CT: verified as '$EFF'"
  fi
  VERIFIED_CORETYPES="$VERIFIED_CORETYPES $CT"
done

# ---- roofline, twice per thread count above one ----------------------------
# UNBOUND FIRST, and that one is the sweep's own environment: same external
# binding as the bench arms, OMP_PROC_BIND=false exactly as PIN_DESC records. If
# the instrument and the arms were measured under different placement policies,
# the instrument would be describing a different machine from the one the arms
# ran on.
#
# BOUND SECOND, and it is not redundant. peak_fma_allcore is registers-only, so no
# memory policy can move it -- and it still fell from 94% to 53% per core between
# t=96 and t=128. The fixed-t pinning diagnostic (2026-08-20) showed why: at fixed
# thread count, binding takes it 273 -> 501 GFLOP/s while the memory policy moves
# it 273 -> 260. The cliff in that column is thread PLACEMENT, and once the cpuset
# spans both sockets an unbound OpenMP runtime stops placing threads on distinct
# cores. So the unbound figure describes the instrument as the sweep ran it and the
# bound figure describes the silicon; the campaign wants both, because the RATIO is
# the measurement of how much of the cliff is placement. It costs seconds -- the
# whole binary is a 128M-FMA chain plus five triad reps.
#
# Skipped at T=1, where there is nothing to place: peak_fma_allcore is not even
# emitted below two threads, so the second invocation would re-measure the
# single-core chain and the triad and call the duplicate provenance.
#
# The two are told apart by `omp_proc_bind`, which roofline.c reads from
# omp_get_proc_bind() rather than from the environment -- the runtime's answer, not
# the request. Binding cannot be applied from inside the process: OpenMP specifies
# that a `proc_bind` clause is IGNORED when OMP_PROC_BIND is false, so a self-bound
# region would silently do nothing under the sweep's own environment.
#
# GBB_PIN_POLICY is passed to BOTH, and until now it was passed to neither: 26317de
# added the field to roofline.c's output and this loop never set it, so every
# campaign roofline record carried roofline.c's `"none"` default while the bench
# records beside it carried the real policy. Standing order 9 asks for the policy
# per arm and the instrument is censused as an arm.
: > "$ROOFOUT"

# The policy string for a given rung and bind mode. PIN_DESC[T] already carries the
# unbound one and is reused verbatim rather than rebuilt, so the two cannot drift;
# the bound variant substitutes the one token that differs. When no external binding
# was available at all PIN_DESC[T] is the bare word "none", and that stays readable.
roof_policy() {   # $1 = thread count, $2 = bind mode
  local desc="${PIN_DESC[$1]}"
  if [ "$2" = "false" ]; then
    printf '%s' "$desc"
  else
    printf '%s;omp_bind=%s omp_places=cores' "${desc%;omp_bind=false}" "$2"
  fi
}

roofline_once() {   # $1 = thread count, $2 = OMP_PROC_BIND value
  local T="$1" bind="$2"
  local -a extra=()
  # OMP_PLACES is meaningless with binding off, and an EMPTY OMP_PLACES is not the
  # same as an absent one -- libgomp warns on it. So it is set only where it applies
  # and otherwise not present at all, which is also what the sweep's arms see.
  [ "$bind" = "false" ] || extra=(OMP_PLACES=cores)
  # shellcheck disable=SC2086
  env GBB_THREADS="$T" OMP_NUM_THREADS="$T" \
    OMP_PROC_BIND="$bind" ${extra[@]+"${extra[@]}"} \
    GBB_PIN_POLICY="$(roof_policy "$T" "$bind")" \
    ${PIN_CMD[$T]} "$BIN/gbb-roofline" >> "$ROOFOUT" 2>>"$STDERRLOG"
}

for T in $THREADS; do
  roofline_once "$T" false
  RC=$?
  if [ $RC -ne 0 ]; then
    census roofline native "" "" "$T" runtime_failed "$RC" 0 "" "$(roof_policy "$T" false)" \
      "gbb-roofline failed unbound; the host's measured-peak provenance is absent at this thread count"
    die "roofline failed at threads=$T (exit $RC). Aborting: peak_fma and the triad are
     this host's instrument-side provenance, and standing order 5 does not admit a
     number without it."
  fi
  census roofline native "" "" "$T" measured 0 "$(wc -l < "$ROOFOUT")" "" \
    "$(roof_policy "$T" false)" ""

  [ "$T" -gt 1 ] || continue
  # A bound invocation that fails is a lost provenance column, not a lost sweep.
  # The unbound run above already carries everything the analysis reads, so this one
  # warns and records a reason rather than aborting: letting a placement-provenance
  # extra kill a launched host would trade a hundred dollars of instance time for a
  # column nothing in sections 1-4 or 7-9 depends on.
  roofline_once "$T" close
  RC=$?
  if [ $RC -ne 0 ]; then
    log "WARNING: bound roofline failed at threads=$T (exit $RC). The placement"
    log "         provenance is absent at this rung; the unbound figures stand."
    census roofline native "" "" "$T" runtime_failed "$RC" 0 "" "$(roof_policy "$T" close)" \
      "bound roofline failed; peak_fma_allcore at this thread count is unbound-only, so the placement ratio cannot be computed here"
  else
    census roofline native "" "" "$T" measured 0 "$(wc -l < "$ROOFOUT")" "" \
      "$(roof_policy "$T" close)" ""
  fi
done
log "roofline written to $ROOFOUT (unbound + bound above t=1)"
ship

# ---- the arm list ---------------------------------------------------------
# From the build manifest, so an arm that failed to build is reported as
# build_failed rather than vanishing. Emits: library, target, exe, backend,
# blas_sha, built, runnable, reason.
manifest_arms() {
  python3 - "$MANIFEST" <<'PY'
import json, sys
for line in open(sys.argv[1]):
    line = line.strip()
    if not line:
        continue
    try:
        r = json.loads(line)
    except json.JSONDecodeError:
        continue
    if r.get("record") != "arm":
        continue
    print("\t".join([
        r.get("library", ""), r.get("target", ""), r.get("exe", ""),
        r.get("thread_backend", "unknown"), r.get("blas_sha") or "unknown",
        "1" if r.get("built") else "0", "1" if r.get("runnable", True) else "0",
        r.get("reason", ""),
    ]))
PY
}

# run_arm <library> <target> <exe> <backend> <blas_sha> <coretype> <coretype_eff> <threads>
run_arm() {
  local lib="$1" tgt="$2" exe="$3" backend="$4" sha="$5" ct="$6" cteff="$7" T="$8"
  local before after rc
  # An array, not ${ct:+VAR="$ct"}: inside that expansion the quotes are literal
  # characters, so the unforced arm would have been handed a variable literally
  # named OPENBLAS_CORETYPE with quotes in its value.
  local -a ctenv=()
  [ -n "$ct" ] && ctenv=(OPENBLAS_CORETYPE="$ct")
  # A line count, so it includes the ten floor-overlap probe records bench.c writes
  # after the matrix on any arm that ran dgemm. Deliberately not netted out: this
  # field answers "how much did this arm emit", and the probe is emitted. The
  # analysis never reads it -- it partitions on the `probe` field instead.
  before=$(wc -l < "$OUT")
  log "run library=$lib target=$tgt coretype=${ct:-unforced} threads=$T"
  # shellcheck disable=SC2086
  env GBB_LIBRARY="$lib" \
      GBB_TARGET="$tgt" \
      GBB_BLAS_SHA="$sha" \
      GBB_CORETYPE="${ct:-unforced}" \
      GBB_THREAD_BACKEND="$backend" \
      GBB_PIN_POLICY="${PIN_DESC[$T]}" \
      GBB_BUILD="$GBB_BUILD" \
      GBB_THREADS="$T" \
      GBB_ARCH_SELECTED="${cteff:-unknown}" \
      OPENBLAS_NUM_THREADS="$T" \
      OMP_NUM_THREADS="$T" \
      BLIS_NUM_THREADS="$T" \
      OMP_PROC_BIND=false \
      ${ctenv[@]+"${ctenv[@]}"} \
      ${PIN_CMD[$T]} "$BIN/$exe" all >> "$OUT" 2>>"$STDERRLOG"
  rc=$?
  after=$(wc -l < "$OUT")
  if [ $rc -eq 4 ]; then
    # bench.c's own arch_selected check refused the arm: the libopenblas it
    # loaded reports a different corename than the probe this runner ran in a
    # separate process. That is not a flake and must not be censused as one --
    # a retry would reproduce it, and the useful fact is that the label and the
    # artifact disagree. Distinct status so decompose.py can separate "this arm
    # is noisy" from "this arm cannot be labelled".
    log "  REFUSED: bench.c reports arch_selected != '${cteff:-unknown}' -- see $STDERRLOG"
    census "$lib" "$tgt" "$ct" "$cteff" "$T" mislabelled "$rc" "$((after - before))" \
      "$backend" "${PIN_DESC[$T]}" \
      "bench.c's in-process openblas_get_corename() disagrees with the probe's
       '${cteff:-unknown}'; the arm would have been measured under a label
       belonging to a different library or environment (standing order 10)"
  elif [ $rc -eq 5 ]; then
    # bench.c's dry pass found a routine in a sweep list with no driver, and
    # refused before measuring anything. Not a host condition and not a flake: it
    # is the same on every arm and on every host, because it is a property of the
    # binary. Censused distinctly so the record does not carry the SIGILL hint,
    # which would send someone looking at the ISA of a host that is fine.
    log "  REFUSED: bench.c has a routine with no driver -- see $STDERRLOG. Every arm will fail."
    census "$lib" "$tgt" "$ct" "$cteff" "$T" harness_invalid "$rc" "$((after - before))" \
      "$backend" "${PIN_DESC[$T]}" \
      "bench.c refused in its matrix-id dry pass: a sweep list names a routine
       sweep() cannot dispatch, so the matrix id would count cases nothing
       measures. A build-time defect in the harness, identical on every arm"
  elif [ $rc -ne 0 ]; then
    log "  exited $rc (SIGILL=132 means this kernel set needs ISA the host lacks)"
    census "$lib" "$tgt" "$ct" "$cteff" "$T" runtime_failed "$rc" "$((after - before))" \
      "$backend" "${PIN_DESC[$T]}" "harness exited $rc; see $STDERRLOG"
  else
    census "$lib" "$tgt" "$ct" "$cteff" "$T" measured 0 "$((after - before))" \
      "$backend" "${PIN_DESC[$T]}" ""
  fi
  ship
}

GBB_BUILD="$(git -C "$ROOT" rev-parse HEAD 2>/dev/null || echo nogit)"
export GBB_BUILD
: > "$OUT"
: > "$STDERRLOG"

while IFS=$'\t' read -r LIB TGT EXE BACKEND SHA BUILT RUNNABLE REASON; do
  [ -n "$LIB" ] || continue

  # netlib reference: a correctness control, never timed. It is declined here, so
  # standing order 11 applies -- "every arm the runner declines to run writes a
  # census record saying why". It used to `continue` before reaching census(),
  # which left build-libs.sh's manifest claiming built:true/runnable:true for an
  # arm that produced no records and no reason. decompose.py was right to call
  # that an unexplained hole; it just happened in every dataset, which would have
  # put 36 MISSING-UNEXPLAINED cells and exit bit 4 on the first real P2 run
  # against a gate that requires zero of them.
  if [ "$LIB" = reference ]; then
    for T in $THREADS; do
      census "$LIB" "$TGT" "" "" "$T" skipped 0 0 "$BACKEND" "${PIN_DESC[$T]}" \
        "netlib correctness control, never timed -- not a performance arm"
    done
    log "skip $LIB/$TGT: correctness control, never timed"
    continue
  fi

  if [ "$BUILT" != 1 ]; then
    for T in $THREADS; do
      census "$LIB" "$TGT" "" "" "$T" build_failed 0 0 "$BACKEND" "${PIN_DESC[$T]}" \
        "${REASON:-not built, no reason recorded}"
    done
    log "skip $LIB/$TGT: not built (${REASON:-no reason recorded})"
    continue
  fi
  if [ "$RUNNABLE" != 1 ]; then
    for T in $THREADS; do
      census "$LIB" "$TGT" "" "" "$T" unrunnable 0 0 "$BACKEND" "${PIN_DESC[$T]}" \
        "${REASON:-marked unrunnable, no reason recorded}"
    done
    log "skip $LIB/$TGT: unrunnable here (${REASON:-no reason recorded})"
    continue
  fi
  if [ ! -x "$BIN/$EXE" ]; then
    for T in $THREADS; do
      census "$LIB" "$TGT" "" "" "$T" build_failed 0 0 "$BACKEND" "${PIN_DESC[$T]}" \
        "manifest says built but $BIN/$EXE is missing or not executable"
    done
    log "skip $LIB/$TGT: $BIN/$EXE absent despite the manifest"
    continue
  fi

  for T in $THREADS; do
    if [ "$LIB" = openblas ] && [ "$TGT" = DYNAMIC ]; then
      # The unforced arm is what wheels ship, and each verified coretype is one
      # cell of the hardware x kernel-set cross -- one binary, one set of
      # common-code compiler flags, only the kernel table varying.
      run_arm "$LIB" "$TGT" "$EXE" "$BACKEND" "$SHA" "" "$UNFORCED_EFF" "$T"
      for CT in $VERIFIED_CORETYPES; do
        run_arm "$LIB" "$TGT" "$EXE" "$BACKEND" "$SHA" "$CT" "${CT_EFFECTIVE[$CT]}" "$T"
      done
    elif [ "$LIB" = openblas ]; then
      run_arm "$LIB" "$TGT" "$EXE" "$BACKEND" "$SHA" "" "$(probe_variant "$TGT")" "$T"
    else
      # ArmPL and BLIS have no OpenBLAS coretype. `n/a` says the question does
      # not apply here; `unknown` would say we tried to answer it and failed.
      run_arm "$LIB" "$TGT" "$EXE" "$BACKEND" "$SHA" "" "n/a" "$T"
    fi
  done
done < <(manifest_arms)

# ---- the pinning delta, measured rather than assumed ----------------------
# The one place OMP_PROC_BIND is switched on. Same binary as the DYNAMIC_OMP arm
# above, same external binding, differing only in whether threads are nailed 1:1
# inside the cpuset. That difference is what the old harness was silently giving
# to ArmPL and not to OpenBLAS, so it is measured here as its own arm and
# subtracted in the analysis rather than left in the comparison.
OMP_EXE=gbb-openblas-DYNAMIC_OMP
OMP_EFF="$(probe_variant DYNAMIC_OMP)"
# Hoisted out of the `env` line it used to sit in. Inline, a failure of this
# python -- a truncated manifest, a JSON error -- expanded to "" and the arm ran
# and was recorded with a blank blas_sha, which is a record that identifies no
# library and is inadmissible under standing order 5. Inside a command
# substitution on an `env` line nothing reports that; the arm simply succeeds.
OMP_SHA="$(python3 -c 'import json,sys
for l in open(sys.argv[1]):
    r=json.loads(l)
    if r.get("record")=="arm" and r.get("target")=="DYNAMIC_OMP":
        print(r.get("blas_sha") or ""); break
else: print("")' "$MANIFEST" 2>/dev/null || true)"
if [ -x "$BIN/$OMP_EXE" ] && [ -z "$OMP_SHA" ]; then
  # Refused, not defaulted to "unknown". A DYNAMIC_OMP_BOUND arm exists only to
  # be differenced against DYNAMIC_OMP, and that difference is meaningless if we
  # cannot show both were the same library.
  for T in $THREADS; do
    census openblas DYNAMIC_OMP_BOUND unforced "$OMP_EFF" "$T" \
      unrunnable 0 0 openmp "${PIN_CMD[$T]};omp_bind=close" \
      "no blas_sha for the DYNAMIC_OMP arm in $MANIFEST, so this arm could not be
       shown to be the same library as the arm it is differenced against"
  done
  log "skip openblas/DYNAMIC_OMP_BOUND: no blas_sha for DYNAMIC_OMP in the manifest"
elif [ -x "$BIN/$OMP_EXE" ]; then
  for T in $THREADS; do
    before=$(wc -l < "$OUT")
    log "run library=openblas target=DYNAMIC_OMP_BOUND threads=$T"
    # shellcheck disable=SC2086
    env GBB_LIBRARY=openblas GBB_TARGET=DYNAMIC_OMP_BOUND \
        GBB_BLAS_SHA="$OMP_SHA" \
        GBB_CORETYPE=unforced GBB_THREAD_BACKEND=openmp \
        GBB_PIN_POLICY="${PIN_CMD[$T]};omp_bind=close,omp_places=cores" \
        GBB_BUILD="$GBB_BUILD" GBB_THREADS="$T" \
        GBB_ARCH_SELECTED="$OMP_EFF" \
        OPENBLAS_NUM_THREADS="$T" OMP_NUM_THREADS="$T" \
        OMP_PROC_BIND=close OMP_PLACES=cores \
        ${PIN_CMD[$T]} "$BIN/$OMP_EXE" all >> "$OUT" 2>>"$STDERRLOG"
    rc=$?
    after=$(wc -l < "$OUT")
    if [ $rc -ne 0 ]; then
      census openblas DYNAMIC_OMP_BOUND unforced "$OMP_EFF" "$T" \
        runtime_failed "$rc" "$((after - before))" openmp \
        "${PIN_CMD[$T]};omp_bind=close" "harness exited $rc"
    else
      census openblas DYNAMIC_OMP_BOUND unforced "$OMP_EFF" "$T" \
        measured 0 "$((after - before))" openmp "${PIN_CMD[$T]};omp_bind=close" ""
    fi
    ship
  done
else
  for T in $THREADS; do
    census openblas DYNAMIC_OMP_BOUND unforced "" "$T" build_failed 0 0 openmp \
      "${PIN_DESC[$T]}" "$OMP_EXE not built, so the value of thread pinning is unmeasured on this host"
  done
fi

log "results:  $OUT ($(wc -l < "$OUT") records)"
log "census:   $CENSUS ($(wc -l < "$CENSUS") lines)"
log "topology: $TOPOFILE"
ship
