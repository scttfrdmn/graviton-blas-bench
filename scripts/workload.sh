#!/usr/bin/env bash
# graviton-blas-bench — the canonical payload for one campaign pass on one host.
#
# WHAT THIS IS, AND WHAT IT DELIBERATELY IS NOT
#
# This is the workload spawn runs *on* the instance. It is not a launcher: it does
# not create, tag, wait on or terminate anything, and there must not be a
# scripts/launch.sh next to it. The lifecycle stays with truffle/spawn, which
# already drives the keel host pool and is therefore exercised; putting an
# unexercised lifecycle between the spend and the data is the failure that rule
# exists to prevent.
#
# The payload is a different question. P2 pass 1 ran from a hand-written script in
# /tmp, and P3 runs fifteen passes (five hosts x three), days apart. A hand-driven
# payload drifts between launches, and a drift between passes is indistinguishable
# from the effect the passes exist to test. So the payload is in-tree, versioned,
# and asserts the things that must not vary.
#
# WHAT IT ASSERTS, AND WHY EACH ONE IS HERE RATHER THAN IN A CHECKLIST
#
#   1. ArmPL, before anything expensive. ArmPL is a registration-gated, ~1.0 GB
#      manual download, and it was absent from P2 pass 1 for exactly that reason.
#      CLAUDE.md admits that absence for P2 and refuses it for P3, because the
#      published framing is OpenBLAS against what the silicon can do. So this runs
#      install-armpl.sh *first* -- before build-libs.sh, before the sweep. On a
#      pass that requires ArmPL, a missing ArmPL now costs two minutes of instance
#      time instead of six hours, and it is discovered at launch rather than in the
#      census afterwards.
#   2. The commit. Three passes off a moving `main` are three different harnesses.
#      GBB_EXPECT_HEAD is checked against the tree actually placed here, and a
#      mismatch aborts before spending anything.
#   3. Log paths namespaced per host and per pass. The P2 payload shipped to
#      gbb/logs/run.log flat, which is fine for one host and silently overwrites
#      for fifteen. The run's own log is the only account of what a pass did.
#
# The EULA is NOT accepted here. GBB_ARMPL_ACCEPT_EULA has to arrive from the
# operator's environment; this script never defaults it, never infers it, and the
# comment is load-bearing because the easy fix when a pass aborts on it is to set
# it here, which would be automation agreeing to a licence on someone's behalf.
#
# Usage (spawn passes the env through; nothing below reads a positional arg):
#
#   GBB_PHASE=p3 \
#   GBB_EXPECT_HEAD=<sha> \
#   GBB_ARMPL_ACCEPT_EULA=1 \
#   GBB_ARMPL_MIRROR=s3://gbb-results-942542972736-us-east-1/gbb/vendor \
#     bash scripts/workload.sh
#
# GBB_PHASE governs one thing only: whether a missing ArmPL is fatal. p2 -> no
# (recorded as an explained absence, which is what P2 pass 1 did), p3 -> yes.

set -uo pipefail

WORK="${GBB_WORK:-/opt/gbb-work}"
ROOT="${GBB_ROOT:-$WORK/repo}"
export GBB_PREFIX="${GBB_PREFIX:-$WORK/libs}"
export GBB_SRC="${GBB_SRC:-$WORK/src}"
export GBB_S3_URI="${GBB_S3_URI:-s3://gbb-results-942542972736-us-east-1/gbb}"
export GBB_AWS_REGION="${GBB_AWS_REGION:-us-east-1}"
export JOBS="${JOBS:-$(nproc)}"
PHASE="${GBB_PHASE:-p2}"
# The marker spawn's on-complete hook watches. Overridable for ONE reason: so
# tests/workload-preflight.sh can assert that completion is signalled without
# signalling it -- a test that touched the real path while a sweep was running on
# the same box would terminate the instance. The default is the real path and no
# launch should ever set this.
COMPLETE_MARKER="${GBB_COMPLETE_MARKER:-/tmp/SPAWN_COMPLETE}"

mkdir -p "$WORK"
STAMP="$WORK/workload.log"
say() { printf '[workload %s] %s\n' "$(date -u +%H:%M:%SZ)" "$*" | tee -a "$STAMP" >&2; }
die() {
  printf '[workload] FATAL: %s\n' "$*" | tee -a "$STAMP" >&2
  # Ship what we have and let the on-complete hook stop the clock. An abort that
  # leaves the instance running is a spend leak; an abort that ships no log is an
  # abort nobody can diagnose.
  ship_logs || true
  touch "$COMPLETE_MARKER"
  exit 1
}

[ -d "$ROOT" ] || { printf '[workload] FATAL: no tree at %s\n' "$ROOT" >&2; exit 1; }
cd "$ROOT" || exit 1

HEAD_SHA="$(git -C "$ROOT" rev-parse HEAD 2>/dev/null || echo unknown)"
git -C "$ROOT" log --oneline -1 > "$WORK/HEAD.txt" 2>&1 || echo "$HEAD_SHA" > "$WORK/HEAD.txt"
HOST="$(hostname -s)"

# Namespaced from the start. The run_id is not known until run-matrix.sh derives
# it, so logs are keyed on host + the workload's own start stamp, which is unique
# per pass and is recorded here so the two can be joined.
PASS_STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
LOGDEST="${GBB_S3_URI%/}/logs/$HOST/$PASS_STAMP"

ship_logs() {
  command -v aws >/dev/null 2>&1 || return 0
  for f in workload.log build.log run.log armpl.log HEAD.txt \
           t-build-start t-build-end t-sweep-start t-sweep-end; do
    [ -f "$WORK/$f" ] || continue
    aws s3 cp --region "$GBB_AWS_REGION" --only-show-errors "$WORK/$f" "$LOGDEST/$f" \
      >/dev/null 2>&1 || true
  done
}

say "phase=$PHASE host=$HOST head=$HEAD_SHA jobs=$JOBS"
say "logs -> $LOGDEST"

# ---- 2. the commit -------------------------------------------------------
# Checked before ArmPL because it is free, and because an ArmPL install charged to
# the wrong harness is wasted either way.
if [ -n "${GBB_EXPECT_HEAD:-}" ]; then
  case "$HEAD_SHA" in
    "$GBB_EXPECT_HEAD"*) say "HEAD matches GBB_EXPECT_HEAD" ;;
    *) die "GBB_EXPECT_HEAD=$GBB_EXPECT_HEAD but the tree at $ROOT is $HEAD_SHA.
     Three passes off different trees are three different harnesses, and nothing
     in the dataset would say so. Place the pinned commit and relaunch." ;;
  esac
else
  say "GBB_EXPECT_HEAD unset -- the harness commit is recorded but not asserted"
  [ "$PHASE" = p3 ] && die "phase p3 without GBB_EXPECT_HEAD. The three passes must
     be the same harness, and 'it was main at the time' is not a pin."
fi

# ---- 1. ArmPL, before anything expensive ---------------------------------
# Three outcomes, and the third one is the point of putting this first:
#   installed          -> ARMPL_DIR exported, build-libs.sh links the reference arm
#   declined, phase p2 -> explained absence in the manifest, pass continues
#   declined, phase p3 -> abort now, having spent ~2 minutes rather than ~6 hours
ARMPL_STATUS=skipped
if [ -n "${ARMPL_DIR:-}" ] && [ -d "${ARMPL_DIR:-}" ]; then
  ARMPL_STATUS="preinstalled"
  say "ARMPL_DIR already set and present: $ARMPL_DIR"
elif [ "${GBB_ARMPL_ACCEPT_EULA:-0}" = 1 ]; then
  say "installing ArmPL (EULA accepted by the operator via GBB_ARMPL_ACCEPT_EULA=1)"
  if D="$(bash "$ROOT/scripts/install-armpl.sh" --print-dir 2>"$WORK/armpl.log")"; then
    export ARMPL_DIR="$D"
    ARMPL_STATUS="installed"
    say "ARMPL_DIR=$ARMPL_DIR"
  else
    ARMPL_STATUS="install_failed"
    say "ArmPL install FAILED -- see $WORK/armpl.log"
    tail -5 "$WORK/armpl.log" >&2 || true
  fi
else
  ARMPL_STATUS="eula_not_accepted"
  say "GBB_ARMPL_ACCEPT_EULA is not 1, so ArmPL is not installed and this script
     will not accept the licence on anyone's behalf. The reference arm will be an
     explained absence in the manifest."
fi

if [ "$PHASE" = p3 ] && [ "$ARMPL_STATUS" != installed ] && [ "$ARMPL_STATUS" != preinstalled ]; then
  die "phase p3 requires ArmPL and it is '$ARMPL_STATUS'.
     CLAUDE.md admits an absent ArmPL for P2 and refuses it for P3: the published
     framing is OpenBLAS against what the silicon can do, and a reference arm
     discovered at launch time is discovered on five hosts across three passes.
     Nothing expensive has run yet -- fix the acquisition and relaunch."
fi
printf '%s\n' "$ARMPL_STATUS" > "$WORK/armpl-status"

# ---- build ----------------------------------------------------------------
date -u +%Y-%m-%dT%H:%M:%SZ > "$WORK/t-build-start"
say "build-libs.sh (~40 min)"
bash "$ROOT/scripts/build-libs.sh" > "$WORK/build.log" 2>&1
BUILD_RC=$?
echo "build-libs exit=$BUILD_RC" >> "$WORK/build.log"
date -u +%Y-%m-%dT%H:%M:%SZ > "$WORK/t-build-end"
say "build-libs exit=$BUILD_RC"
# Not fatal on its own: build-libs.sh records a per-arm outcome and a pass with
# one library missing is an explained gap, not a dead pass. A total failure shows
# up as a census with no measured arms, which the gate catches.
[ "$BUILD_RC" -eq 0 ] || say "build-libs.sh returned $BUILD_RC -- continuing; the
     manifest records which arms built and which did not"

# ---- sweep ----------------------------------------------------------------
date -u +%Y-%m-%dT%H:%M:%SZ > "$WORK/t-sweep-start"
say "run-matrix.sh (hours; ships per arm to $GBB_S3_URI as it goes)"
bash "$ROOT/scripts/run-matrix.sh" > "$WORK/run.log" 2>&1
SWEEP_RC=$?
echo "run-matrix exit=$SWEEP_RC" >> "$WORK/run.log"
date -u +%Y-%m-%dT%H:%M:%SZ > "$WORK/t-sweep-end"
say "run-matrix exit=$SWEEP_RC"

# ---- logs, then hand back to the on-complete hook -------------------------
# Records are NOT copied here. run-matrix.sh's ship() already put them under
# $GBB_S3_URI/<role>/<host>/<run_id>/ per arm, and a duplicate under a second
# prefix would double every measurement the moment someone syncs the bucket into
# one directory.
ship_logs
say "done (armpl=$ARMPL_STATUS build=$BUILD_RC sweep=$SWEEP_RC); signalling completion"
touch "$COMPLETE_MARKER"
