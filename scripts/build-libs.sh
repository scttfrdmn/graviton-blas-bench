#!/usr/bin/env bash
# graviton-blas-bench — build every library arm on the current host and record what was built.
#
# WHAT CHANGED AND WHY, because it changes what the campaign measures:
#
# This script used to build six separate OpenBLAS `TARGET=` trees to get the
# hardware x kernel-set cross. That was more confounded than it looked and also
# unnecessary. `TARGET=` is not only a kernel-table selection -- it also sets the
# compiler flags applied to the *common* code (Makefile.arm64 gives NEOVERSEN2
# `-march=armv8.5-a+sve+sve2+bf16` and NEOVERSEN1 something much older), so a
# NEOVERSEV1-vs-NEOVERSEV2 comparison across two builds moves the kernel table
# AND the codegen of every shared source file at once. There is no way to
# attribute the difference afterwards.
#
# OpenBLAS's own force_coretype() makes every target in its switch reachable by
# name at runtime on one DYNAMIC_ARCH binary via OPENBLAS_CORETYPE. That is the
# strictly better experiment: one binary, one set of common-code flags, only the
# kernel table varying. So DYNAMIC_ARCH is now the primary artifact and the
# cross is a runtime sweep in run-matrix.sh.
#
# Two static `TARGET=` builds survive as controls, not as the experiment:
#   - the host's own native target, to check DYNAMIC_ARCH dispatch does not
#     itself cost anything measurable versus a purpose-built library;
#   - the experimental cross target, to check that forcing that coretype on the
#     DYNAMIC binary lands in the same place a real TARGET= build does.
# If either control disagrees with its forced counterpart, the coretype axis is
# not measuring what it claims and that finding precedes every kernel question.
#
# Arms whose ISA the host lacks are built anyway and marked unrunnable; the
# runner skips them rather than dying on SIGILL, and records that it skipped
# them so a gap in the results is explained rather than merely absent.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PREFIX="${GBB_PREFIX:-$HOME/graviton-blas-bench-libs}"
SRCDIR="${GBB_SRC:-$HOME/graviton-blas-bench-src}"
JOBS="${JOBS:-$(nproc)}"
MANIFEST="$PREFIX/build-manifest.ndjson"

# BLIS is a reference arm, not the subject, so the default used to be `master` on
# the reasoning that a moving reference arm is a recorded warning rather than a
# hard error (see the BLIS section below, which keeps that distinction for an
# explicit override). That reasoning does not survive P3: the five hosts are built
# on five separate days and P3 runs three times days apart, so `master` means the
# reference arm can differ between the passes whose agreement is the campaign's
# strongest evidence -- and a BLIS-vs-OpenBLAS gap that moves between passes would
# be indistinguishable from the effect the passes exist to test. The p3 gate's
# blas_sha check is about OpenBLAS and does not cover this.
#
# Pinned to what the first P2 host actually resolved `master` to, read off its
# manifest rather than chosen: flame/blis HEAD at 2026-08-20, committed
# 2026-07-10. So this is a no-op against the P2 dataset by construction, and P3 is
# comparable to P2 for the same reason.
BLIS_REF="${BLIS_REF:-061c2ebef87eda9189e6cdf38af4ea3d4a8efe7b}"

# The audited tree. KICKOFF.md's source audit -- the 5-vs-99 SVE kernel count,
# KERNEL.NEOVERSEN2's missing includes, the dispatch fall-through -- was done
# against this commit, so every claim in this repo is a claim about it.
OPENBLAS_REF="${OPENBLAS_REF:-cc3fc1e}"

mkdir -p "$PREFIX" "$SRCDIR"

log() { printf '[gbb-build] %s\n' "$*" >&2; }
die() { printf '[gbb-build] FATAL: %s\n' "$*" >&2; exit 1; }

# ---- one build at a time, per prefix and per source tree -------------------
# $GBB_PREFIX and $GBB_SRC are fixed paths, so two concurrent runs on one host
# share both: they check out OpenBLAS into the same $SRCDIR, `make install` over
# each other's $PREFIX/openblas-* trees, and append interleaved lines to the same
# build-manifest.ndjson. The damage is not a failed build -- it is a *successful*
# one whose manifest says a target was built while the tree at that path came from
# the other run, which is standing order 10's mislabelling failure moved from the
# runner into the builder, and a plausible wrong answer rather than a gap.
#
# A PID-suffixed path is the wrong remedy here, unlike the test fixtures: the whole
# point of the prefix is that run-matrix.sh reads the libraries back out of it by
# name, so making it unique per run breaks the consumer. Mutual exclusion is the
# remedy -- the second run refuses and says who holds the lock.
#
# `mkdir` is the lock primitive because it is atomic on POSIX and needs no flock,
# which busybox and macOS do not both have.
LOCKS=()
release_locks() {
  local d
  for d in "${LOCKS[@]:-}"; do [ -n "$d" ] && rm -rf "$d"; done
}
take_lock() {
  local what="$2" lock="$1/.gbb-build.lock"
  if ! mkdir "$lock" 2>/dev/null; then
    local owner
    owner="$(cat "$lock/owner" 2>/dev/null || echo 'unknown -- no owner file')"
    if [ "${GBB_FORCE_UNLOCK:-0}" = 1 ]; then
      log "WARNING: breaking the $what lock held by: $owner"
      log "         GBB_FORCE_UNLOCK=1 was set. If that build is still running, the"
      log "         manifest this run writes will describe trees it did not build."
      rm -rf "$lock"
      mkdir "$lock" || die "cannot take the $what lock at $lock even after forcing"
    else
      die "another build holds the $what lock at $lock
     held by: $owner
     Two builds sharing one $what install over each other and append to one
     manifest, so the manifest can end up describing a tree the other run built.
     Wait for it, or point this run elsewhere:
       GBB_PREFIX=... GBB_SRC=... $0
     If that build is definitely dead, GBB_FORCE_UNLOCK=1 breaks the lock."
    fi
  fi
  printf 'pid=%s host=%s started=%s prefix=%s src=%s\n' \
    "$$" "$(hostname)" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$PREFIX" "$SRCDIR" >"$lock/owner"
  LOCKS+=("$lock")
}
trap release_locks EXIT
take_lock "$PREFIX" "install prefix"
# Locked separately: a second run pointed at a different prefix still checks out
# and builds OpenBLAS in the same $GBB_SRC unless that is also redirected, and a
# `git checkout` under a running `make` is its own kind of wrong answer.
if [ "$SRCDIR" != "$PREFIX" ]; then
  take_lock "$SRCDIR" "source tree"
fi

# ---- refs must be immutable ------------------------------------------------
# The five hosts are built on different days. A branch name means host c6g and
# host c9g can silently get different libraries, and the cross-host comparison
# that is the entire deliverable then compares two OpenBLASes as if they were
# one. `develop` was the old default; that was a latent correctness bug in the
# campaign, not a convenience.
immutable_ref() {
  case "${#1}" in 7|8|9|1[0-9]|20|2[1-9]|3[0-9]|40) ;; *) return 1 ;; esac
  case "$1" in *[!0-9a-f]*) return 1 ;; esac
  return 0
}

if ! immutable_ref "$OPENBLAS_REF"; then
  if [ "${GBB_ALLOW_MUTABLE_REF:-0}" = 1 ]; then
    log "WARNING: OPENBLAS_REF='$OPENBLAS_REF' is not a SHA. GBB_ALLOW_MUTABLE_REF=1"
    log "         was set, so proceeding -- but hosts built at different times may"
    log "         not be running the same library. decompose.py cross-checks blas_sha"
    log "         across hosts and will flag it."
  else
    die "OPENBLAS_REF='$OPENBLAS_REF' is not an immutable commit SHA.
     OpenBLAS is the subject of this campaign; building the five hosts from a
     moving branch makes the cross-host comparison meaningless. Pass a full or
     abbreviated SHA, or set GBB_ALLOW_MUTABLE_REF=1 if you accept the drift."
  fi
fi

# ---- what this host is, for choosing the control builds --------------------
# cpu0 only, which is sufficient for picking a build target. capture-env.sh does
# the rigorous every-core version and refuses to run a sweep on a heterogeneous
# host; that check belongs there, not here.
MIDR="$(cat /sys/devices/system/cpu/cpu0/regs/identification/midr_el1 2>/dev/null || true)"
PART=""
[ -n "$MIDR" ] && PART="$(printf '0x%x' $(( ( $((MIDR)) >> 4 ) & 0xFFF )))"

FLAGS="$(grep -m1 '^Features' /proc/cpuinfo 2>/dev/null | cut -d: -f2- | xargs || true)"
has_flag() { printf '%s' " $FLAGS " | grep -qw "$1"; }
HAS_SVE=false;  has_flag sve  && HAS_SVE=true
HAS_SVE2=false; has_flag sve2 && HAS_SVE2=true

# The target a purpose-built library for this exact chip would use.
case "$PART" in
  0xd0c)               NATIVE_TARGET=NEOVERSEN1 ;;
  0xd40)               NATIVE_TARGET=NEOVERSEV1 ;;
  0xd49)               NATIVE_TARGET=NEOVERSEN2 ;;
  0xd4f|0xd83|0xd84)   NATIVE_TARGET=NEOVERSEV2 ;;
  *) if [ "$HAS_SVE" = true ]; then NATIVE_TARGET=ARMV8SVE; else NATIVE_TARGET=ARMV8; fi ;;
esac

# The cross target: the kernel set this host is NOT supposed to get, chosen so
# the comparison answers the campaign's question on this host. On SVE2 silicon
# that is NEOVERSEV1's 99 SVE kernels against NEOVERSEN2's 5. On SVE1 silicon
# there is no such gap to close, so ARMV8SVE is the informative control instead
# -- it is the file KERNEL.NEOVERSEN2 conspicuously does not include.
if [ "$HAS_SVE2" = true ]; then
  CROSS_TARGET=NEOVERSEV1
elif [ "$HAS_SVE" = true ]; then
  CROSS_TARGET=ARMV8SVE
else
  CROSS_TARGET=ARMV8
fi
# The two must differ or there is no cross. They collide on a NEON-only host
# (native ARMV8SVE/ARMV8 both fall to ARMV8) and on any host whose MIDR is
# unreadable. Building the same target twice would install over itself and emit
# two identical manifest lines, which the outcome census would then count as two
# arms -- inflating apparent coverage on exactly the hosts where we know least.
if [ "$CROSS_TARGET" = "$NATIVE_TARGET" ]; then
  if [ "$NATIVE_TARGET" = NEOVERSEN1 ]; then CROSS_TARGET=ARMV8; else CROSS_TARGET=NEOVERSEN1; fi
  log "cross target collided with native ($NATIVE_TARGET); using $CROSS_TARGET instead"
fi

CONTROL_TARGETS="${GBB_CONTROL_TARGETS:-$NATIVE_TARGET $CROSS_TARGET}"
# Deduplicate whatever we ended up with, including an operator-supplied list.
DEDUP=""
for t in $CONTROL_TARGETS; do
  case " $DEDUP " in *" $t "*) log "dropping duplicate control target $t" ;; *) DEDUP="$DEDUP $t" ;; esac
done
CONTROL_TARGETS="${DEDUP# }"

log "host part=${PART:-unreadable} sve=$HAS_SVE sve2=$HAS_SVE2"
log "native target=$NATIVE_TARGET  cross target=$CROSS_TARGET"
log "static control builds: $CONTROL_TARGETS"

requires() {
  case "$1" in
    ARMV8|NEOVERSEN1) echo none ;;
    ARMV8SVE|NEOVERSEV1) echo sve ;;
    NEOVERSEN2|NEOVERSEV2) echo sve2 ;;
    *) echo unknown ;;
  esac
}
host_has() {
  case "$1" in
    none) return 0 ;;
    sve)  [ "$HAS_SVE"  = true ] ;;
    sve2) [ "$HAS_SVE2" = true ] ;;
    *) return 1 ;;
  esac
}

# ---- manifest emission ----------------------------------------------------
# One `record:"arm"` line per built artifact. coretype is null here because it
# is a runtime property: run-matrix.sh sweeps it and writes the per-arm outcome
# census. `reason` is non-empty whenever built or runnable is false, so a gap in
# the results always has a stated cause rather than being merely absent.
: > "$MANIFEST"

# `reason` carries build-log paths and compiler chatter, so it must be escaped
# rather than interpolated. One malformed line makes the whole manifest
# unparseable, and the manifest is what tells decompose.py the difference
# between "this arm was never built" and "this arm is missing for no reason" --
# the second is a hole in the experiment and the first is not.
jstr() {
  local v="${1:-}"
  v="${v//\\/}"; v="${v//\"/}"; v="${v//$'\t'/ }"; v="${v//$'\r'/ }"
  printf '%s' "${v//$'\n'/ }"
}

# Did this build actually get SVE kernels compiled into it? Standing order 8
# names `NO_SVE` in the build as one of its two escalate-now conditions, and it
# is the more insidious of the two: NO_SVE=1, or an assembler too old to accept
# SVE, produces a library with no SVE kernels at all, on which every arm still
# builds, still runs, and still reports plausible numbers -- while the entire SVE
# axis of this campaign silently measures nothing. So it is read off the
# installed artifact rather than inferred from the variables we passed, and the
# static archive is used because it is installed unstripped by every variant.
#   yes / no / unknown -- `unknown` means we could not look, not that it is fine.
#
# DO NOT rewrite the nm call as `nm ... | grep -q ...`. That was the original form
# and it was broken in the worst available direction. `grep -q` exits on its first
# match; nm then dies on SIGPIPE; and under this script's `set -o pipefail` (line
# 33) the pipeline reports 141. So:
#
#     SVE present -> grep matches early -> nm killed 141 -> pipefail -> `no`
#     SVE absent  -> grep reads all, exits 1             -> pipefail -> `no`
#
# It was a constant function returning `no` -- incapable of ever printing `yes` --
# which made standing order 8's escalation channel a wire that was always live.
# It fired on all four OpenBLAS builds of the first P2 pass (2026-08-20,
# 20260820T031023Z-ip-172-31-36-19) while SVE was demonstrably present: 1092
# matching defined symbols, `dgemm_kernel_ARMV8SVE` among them, 135312 SVE
# instructions in the .so, and a measured GEMM_SMALL effect that cannot exist
# without SVE kernels. A checker that cries wolf on the one condition CLAUDE.md
# says outweighs every other question in this repo is worse than no checker,
# because the next reader discounts it.
#
# So: capture nm's output whole, keep its exit status, and count with `grep -c`,
# which consumes all of its input and cannot induce SIGPIPE in its producer.
#
# The answer is also logged as it is taken. The pipefail bug above survived a whole
# P2 pass because the only place its output was ever read was decompose.py, hours
# later and on another machine; `log` goes to stderr and workload.sh folds stderr
# into build.log, so from here on the probe states its finding on the path every
# build already takes.
sve_kernels() {
  local dest="$1" lib found out rc n ans
  ans=unknown
  found=
  if ! command -v nm >/dev/null 2>&1; then
    log "  nm is not on PATH, so SVE symbols cannot be read"
  else
    for lib in "$dest/lib/libopenblas.a" "$dest/lib64/libopenblas.a"; do
      [ -f "$lib" ] || continue
      found=$lib
      out=""; rc=0
      # `|| rc=$?` and not a bare assignment: `set -e` would abort the whole
      # build on a failed nm, treating a probe failure as a build failure.
      out="$(nm --defined-only "$lib" 2>/dev/null)" || rc=$?
      # A failed nm, or an archive whose symbol table reads as empty, is "we
      # could not look" -- `unknown`, not `no`. Collapsing the two is the same
      # class of mistake as the pipefail bug above: decompose.py treats `no` as
      # the escalation and `unknown` as a provenance gap, and those are
      # different claims about the library. nm prints nothing and exits
      # non-zero on a truncated or non-archive file, so without this branch a
      # half-written archive reads as a confirmed absence of SVE.
      if [ "$rc" -ne 0 ] || [ -z "$out" ]; then
        log "  nm on $lib failed (rc=$rc) or read an empty symbol table"
      else
        n="$(printf '%s\n' "$out" | grep -cE '(ARMV8SVE|_sve|sve_)' || true)"
        if [ "${n:-0}" -gt 0 ]; then ans=yes; else ans=no; fi
        log "  $lib: ${n:-0} SVE-matching defined symbols"
      fi
      break
    done
    [ -n "$found" ] || log "  no libopenblas.a under $dest/lib or $dest/lib64"
  fi
  log "sve_kernels($(basename "$dest")) = $ans"
  printf '%s' "$ans"
}

# arm_record <library> <target> <blas_sha> <built> <runnable> <reason>
#            <thread_backend> <exe> <prefix> <sve_kernels>
# `target` is what was REQUESTED of the build. `target_effective` is what the
# built library reports at runtime, and it defaults to null -- meaning no read-back
# was attempted -- rather than to a copy of the request. Standing order 10 is about
# exactly this asymmetry: a request echoed back as if it were an observation is not
# a failed measurement, it is a plausible wrong label. The BLIS arm shipped a whole
# P2 pass as `target: "auto"` with nothing recording what `auto` resolved to, which
# is the same defect one library over from labelling an OpenBLAS arm with
# OPENBLAS_CORETYPE instead of openblas_get_corename().
arm_record() {
  printf '{"record":"arm","library":"%s","target":"%s","target_effective":%s,' \
    "$(jstr "$1")" "$(jstr "$2")" \
    "$([ -n "${11:-}" ] && printf '"%s"' "$(jstr "${11}")" || printf 'null')"
  printf '"coretype":null,"blas_sha":"%s",' "$(jstr "$3")"
  printf '"built":%s,"runnable":%s,"reason":"%s","thread_backend":"%s",' \
    "$4" "$5" "$(jstr "$6")" "$(jstr "$7")"
  printf '"exe":"%s","prefix":"%s","sve_kernels":"%s"}\n' \
    "$(jstr "$8")" "$(jstr "$9")" "$(jstr "${10:-unknown}")"
}

# ---- OpenBLAS -------------------------------------------------------------
if [ ! -d "$SRCDIR/OpenBLAS" ]; then
  log "cloning OpenBLAS"
  git clone --quiet https://github.com/OpenMathLib/OpenBLAS.git "$SRCDIR/OpenBLAS"
fi
git -C "$SRCDIR/OpenBLAS" fetch --quiet --tags origin
git -C "$SRCDIR/OpenBLAS" checkout --quiet --detach "$OPENBLAS_REF" \
  || die "OpenBLAS ref '$OPENBLAS_REF' not found in the clone at $SRCDIR/OpenBLAS"
# Full SHA, not --short. The short form does not identify a commit outside the
# repo that produced it, and these records are meant to be read years later.
OB_SHA="$(git -C "$SRCDIR/OpenBLAS" rev-parse HEAD)"
log "OpenBLAS at $OB_SHA"

# build_openblas <label> <mk-target-args...> -- sets BUILD_OK
build_openblas() {
  local label="$1"; shift
  local dest="$PREFIX/openblas-$label"
  local logf="$PREFIX/openblas-$label.buildlog"
  log "building OpenBLAS $label ($*)"
  make -C "$SRCDIR/OpenBLAS" clean >/dev/null 2>&1 || true
  if make -C "$SRCDIR/OpenBLAS" -j"$JOBS" "$@" >"$logf" 2>&1 \
     && make -C "$SRCDIR/OpenBLAS" "$@" PREFIX="$dest" install >>"$logf" 2>&1; then
    BUILD_OK=true
  else
    BUILD_OK=false
    log "  BUILD FAILED -- see $logf"
  fi
}

# --- primary artifact: DYNAMIC_ARCH, pthreads ---
# This is what distro packages and NumPy wheels actually ship, so it is both the
# control for "what do real users get" and the vehicle for the whole coretype
# sweep. USE_OPENMP=0 is not a default we inherited, it is the shipping
# configuration and therefore the one under test.
build_openblas DYNAMIC DYNAMIC_ARCH=1 NUM_THREADS=256 USE_OPENMP=0
OK_DYN=$BUILD_OK
if [ "$OK_DYN" = true ]; then
  make -C "$ROOT" openblas   OPENBLAS_DIR="$PREFIX/openblas-DYNAMIC" VARIANT=DYNAMIC \
    >>"$PREFIX/openblas-DYNAMIC.buildlog" 2>&1 || OK_DYN=false
  # The coretype probe must link the same library by rpath, or the sweep would
  # be verified against a different OpenBLAS than it runs.
  make -C "$ROOT" coreprobe   OPENBLAS_DIR="$PREFIX/openblas-DYNAMIC" VARIANT=DYNAMIC \
    >>"$PREFIX/openblas-DYNAMIC.buildlog" 2>&1 || OK_DYN=false
fi
arm_record openblas DYNAMIC "$OB_SHA" "$OK_DYN" true "" pthreads \
  gbb-openblas-DYNAMIC "$PREFIX/openblas-DYNAMIC" \
  "$(sve_kernels "$PREFIX/openblas-DYNAMIC")" >> "$MANIFEST"

# --- the threading-backend arm ---
# Same sources, same TARGET dispatch, different threading runtime. Kept as its
# own arm rather than used to "equalise" the reference comparison: ArmPL is
# OpenMP and obeys OMP_PROC_BIND while shipping OpenBLAS is pthreads and does
# not, and the honest way to handle that is to measure the difference, not to
# rebuild OpenBLAS into something nobody installs.
build_openblas DYNAMIC_OMP DYNAMIC_ARCH=1 NUM_THREADS=256 USE_OPENMP=1
OK_OMP=$BUILD_OK
if [ "$OK_OMP" = true ]; then
  make -C "$ROOT" openblas-omp OPENBLAS_DIR="$PREFIX/openblas-DYNAMIC_OMP" VARIANT=DYNAMIC_OMP \
    >>"$PREFIX/openblas-DYNAMIC_OMP.buildlog" 2>&1 || OK_OMP=false
  # A probe per variant, not one probe for all of them. This is also DYNAMIC_ARCH,
  # so what it selects is a real per-library fact, and the runner used to label
  # every non-DYNAMIC arm with the DYNAMIC binary's selection -- provenance
  # measured on a different library.
  make -C "$ROOT" coreprobe OPENBLAS_DIR="$PREFIX/openblas-DYNAMIC_OMP" VARIANT=DYNAMIC_OMP \
    >>"$PREFIX/openblas-DYNAMIC_OMP.buildlog" 2>&1 || true
fi
arm_record openblas DYNAMIC_OMP "$OB_SHA" "$OK_OMP" true "" openmp \
  gbb-openblas-DYNAMIC_OMP "$PREFIX/openblas-DYNAMIC_OMP" \
  "$(sve_kernels "$PREFIX/openblas-DYNAMIC_OMP")" >> "$MANIFEST"

# --- static controls ---
for T in $CONTROL_TARGETS; do
  REQ="$(requires "$T")"
  RUNNABLE=true; REASON=""
  if ! host_has "$REQ"; then
    RUNNABLE=false
    REASON="target requires $REQ which this host does not report (sve=$HAS_SVE sve2=$HAS_SVE2)"
  fi
  build_openblas "$T" TARGET="$T" NUM_THREADS=256 USE_OPENMP=0
  OK=$BUILD_OK
  if [ "$OK" = true ]; then
    make -C "$ROOT" openblas OPENBLAS_DIR="$PREFIX/openblas-$T" VARIANT="$T" \
      >>"$PREFIX/openblas-$T.buildlog" 2>&1 || { OK=false; REASON="harness link failed"; }
    # A static TARGET= build is not DYNAMIC_ARCH, so openblas_get_corename()
    # reports its fixed target. That is worth recording: it is how the control
    # confirms a forced coretype lands where a real TARGET= build does. Failure
    # to build the probe is not fatal -- the arm is still measurable, it just
    # carries an unprobed selection.
    make -C "$ROOT" coreprobe OPENBLAS_DIR="$PREFIX/openblas-$T" VARIANT="$T" \
      >>"$PREFIX/openblas-$T.buildlog" 2>&1 || true
  else
    REASON="${REASON:-build failed, see $PREFIX/openblas-$T.buildlog}"
  fi
  arm_record openblas "$T" "$OB_SHA" "$OK" "$RUNNABLE" "$REASON" pthreads \
    "gbb-openblas-$T" "$PREFIX/openblas-$T" \
    "$(sve_kernels "$PREFIX/openblas-$T")" >> "$MANIFEST"
done

# ---- ArmPL ----------------------------------------------------------------
# A download from developer.arm.com, not a build. The version string is the only
# provenance available, so it goes in blas_sha -- there is no SHA to record and
# inventing one would be worse than saying so.
if [ -n "${ARMPL_DIR:-}" ] && [ -d "$ARMPL_DIR" ]; then
  log "linking against ArmPL at $ARMPL_DIR"
  if make -C "$ROOT" armpl ARMPL_DIR="$ARMPL_DIR" >"$PREFIX/armpl.buildlog" 2>&1; then
    OK=true; REASON=""
  else
    OK=false; REASON="harness link against $ARMPL_DIR failed, see $PREFIX/armpl.buildlog"
  fi
  arm_record armpl native "armpl-$(basename "$ARMPL_DIR")" "$OK" true "$REASON" openmp \
    gbb-armpl "$ARMPL_DIR" n/a >> "$MANIFEST"
else
  log "ARMPL_DIR unset or missing -- skipping ArmPL arm"
  arm_record armpl native "" false true "ARMPL_DIR unset or not a directory" openmp \
    gbb-armpl "" n/a >> "$MANIFEST"
fi

# ---- BLIS -----------------------------------------------------------------
# BLIS is a third-party reference arm, not the subject, so an *explicitly
# overridden* mutable ref is a recorded warning here rather than the hard error it
# is for OpenBLAS. The default is a SHA (see BLIS_REF above), so reaching this
# warning takes a deliberate `BLIS_REF=master`. The full SHA is captured either
# way, which lets decompose.py detect after the fact that two hosts got different
# BLISes -- blas_sha_conflict already keys on (library, target), so blis/auto with
# two SHAs is flagged without any new check.
if [ ! -d "$SRCDIR/blis" ]; then
  log "cloning BLIS"
  git clone --quiet https://github.com/flame/blis.git "$SRCDIR/blis"
fi
git -C "$SRCDIR/blis" fetch --quiet --tags origin
git -C "$SRCDIR/blis" checkout --quiet --detach "$BLIS_REF" 2>/dev/null \
  || git -C "$SRCDIR/blis" checkout --quiet "$BLIS_REF"
BLIS_SHA="$(git -C "$SRCDIR/blis" rev-parse HEAD)"

# The config is CHOSEN from the host, not left to `configure auto`.
#
# WHY, measured rather than assumed. On the first P2 pass (c8g.metal-48xl,
# Neoverse V2) `auto` produced a library that ran large single-threaded DGEMM at
# 6.1 GFLOP/s against OpenBLAS's 17.7 -- 0.35x, at one thread, so no threading
# question is involved -- and then scaled perfectly linearly at a flat 1.33
# GFLOP/s per core from t=8 to t=96. Perfect scaling at a 4.6x-bad constant is a
# kernel deficit, not oversubscription and not a tuning gap. `auto` had almost
# certainly landed on a generic arm64 sub-config with no Neoverse kernels, and
# nothing in the record said which, because the config was never read back and
# blis.buildlog was never shipped.
#
# A misconfigured reference arm is worse than an absent one: section 1 measures
# every deficit against the reference, so a bad reference manufactures a deficit
# everywhere. BLIS is on one attempt here by Scott's ruling -- fix the config, or
# drop BLIS from P3 entirely.
#
# The choice is verified against the checked-out tree rather than hardcoded,
# because a config name that BLIS renames or removes would otherwise fail the
# whole build at configure time on a host, in a script whose job is to make five
# hosts identical.
blis_config_choose() {
  local want=""
  if [ "$HAS_SVE" = true ]; then
    # VL-agnostic SVE kernels. Right for Graviton 3 and 4 (V1/V2) and for any
    # future SVE part, which matters because standing order 8's whole point is
    # that an unrecognised SVE part should still get SVE kernels.
    want=armsve
  else
    case "${PART:-}" in
      # Neoverse N1: Graviton 2. No SVE, so armsve is not merely suboptimal, it
      # would not run. BLIS's altra config IS N1.
      0xd0c) want=altra ;;
      # Anything else non-SVE falls to the arm64 FAMILY config, which dispatches
      # among its sub-configs at runtime. That is a deliberate last resort and not
      # the same as `auto`: the read-back below records which sub-config won.
      *)     want=arm64 ;;
    esac
  fi
  if [ -d "$SRCDIR/blis/config/$want" ]; then
    printf '%s' "$want"
  else
    log "WARNING: BLIS at $BLIS_SHA has no config/$want; falling back to auto"
    printf 'auto'
  fi
}
if [ -n "${BLIS_CONFIG:-}" ]; then
  BLIS_CONF="$BLIS_CONFIG"
  log "BLIS config overridden to '$BLIS_CONF' by BLIS_CONFIG"
else
  BLIS_CONF="$(blis_config_choose)"
fi
BLIS_ARCH=""
BLIS_REASON=""
immutable_ref "$BLIS_REF" || {
  BLIS_REASON="BLIS_REF='$BLIS_REF' is a mutable ref; resolved to $BLIS_SHA on this host only"
  log "WARNING: $BLIS_REASON"
}
log "building BLIS $BLIS_SHA config=$BLIS_CONF"
cd "$SRCDIR/blis"
if ./configure --prefix="$PREFIX/blis" -t pthreads "$BLIS_CONF" >"$PREFIX/blis.buildlog" 2>&1 \
   && make -j"$JOBS" >>"$PREFIX/blis.buildlog" 2>&1 \
   && make install >>"$PREFIX/blis.buildlog" 2>&1; then
  OK=true
  make -C "$ROOT" blis BLIS_DIR="$PREFIX/blis" >>"$PREFIX/blis.buildlog" 2>&1 \
    || { OK=false; BLIS_REASON="harness link failed"; }
else
  OK=false
  BLIS_REASON="${BLIS_REASON:-build failed, see $PREFIX/blis.buildlog}"
fi
cd "$ROOT"

# ---- read the config back off the library, at runtime ---------------------
# Standing order 10 applied to BLIS. `bli_arch_query_id()` is the same kind of
# question `openblas_get_corename()` answers, and the answer can legitimately
# differ from the request: a family config like arm64 picks a sub-config at
# runtime, so `target: arm64` with `target_effective: firestorm` is not a fault,
# it is the fact the manifest was missing. A request that does NOT come back is
# the fault, and it is recorded as one rather than smoothed over.
if [ "$OK" = true ]; then
  probe_c="$SRCDIR/blis-arch-probe.c"
  cat > "$probe_c" <<'PROBE'
#include <stdio.h>
#include "blis.h"
int main(void) { printf("%s\n", bli_arch_string(bli_arch_query_id())); return 0; }
PROBE
  if ${CC:-gcc} -O2 -std=c11 -I"$PREFIX/blis/include/blis" "$probe_c" \
       -o "$SRCDIR/blis-arch-probe" -L"$PREFIX/blis/lib" -lblis -lm -lpthread \
       >>"$PREFIX/blis.buildlog" 2>&1; then
    BLIS_ARCH="$("$SRCDIR/blis-arch-probe" 2>>"$PREFIX/blis.buildlog" | tr -d '[:space:]')"
  fi
  if [ -z "$BLIS_ARCH" ]; then
    BLIS_ARCH="unknown"
    BLIS_REASON="${BLIS_REASON:+$BLIS_REASON; }bli_arch_query_id() read-back unavailable, so target='$BLIS_CONF' is a request and not an observation"
    log "WARNING: BLIS arch read-back failed; target_effective=unknown"
  else
    log "BLIS reports arch '$BLIS_ARCH' (requested '$BLIS_CONF')"
    if [ "$BLIS_ARCH" != "$BLIS_CONF" ] && [ "$BLIS_CONF" != auto ] \
       && [ "$BLIS_CONF" != arm64 ]; then
      # Not a family config, so the runtime answer should be the request. It is
      # recorded, not silently accepted -- the same shape as run-matrix.sh
      # refusing an OpenBLAS arm whose coretype request was ignored, except that
      # BLIS is the reference and not the subject, so this warns.
      BLIS_REASON="${BLIS_REASON:+$BLIS_REASON; }requested config '$BLIS_CONF' but bli_arch_query_id() reports '$BLIS_ARCH'"
      log "WARNING: $BLIS_REASON"
    fi
  fi
fi
arm_record blis "$BLIS_CONF" "$BLIS_SHA" "$OK" true "$BLIS_REASON" pthreads \
  gbb-blis "$PREFIX/blis" n/a "$BLIS_ARCH" >> "$MANIFEST"

# ---- reference netlib (correctness control) -------------------------------
# Not a performance arm. It is here so that "fast" and "correct" can be
# separated when a verification fails.
if make -C "$ROOT" reference >"$PREFIX/reference.buildlog" 2>&1; then
  arm_record reference native "" true true "correctness control only, not timed" \
    pthreads gbb-reference "" n/a >> "$MANIFEST"
else
  arm_record reference native "" false true "netlib libblas not available on this host" \
    pthreads gbb-reference "" n/a >> "$MANIFEST"
fi

# ---- roofline -------------------------------------------------------------
make -C "$ROOT" roofline >"$PREFIX/roofline.buildlog" 2>&1 \
  || die "roofline build failed -- see $PREFIX/roofline.buildlog. The measured-peak
     denominator (standing order 1) comes from this binary; without it every
     efficiency figure on this host would have no cross-check."

# ---- compiler provenance --------------------------------------------------
printf '{"record":"toolchain","cc":"%s","cc_version":"%s","kernel":"%s","libc":"%s",' \
  "${CC:-gcc}" "$(${CC:-gcc} --version | head -1 | tr -d '"\\')" \
  "$(uname -r)" "$(ldd --version 2>&1 | head -1 | tr -d '"\\')" >> "$MANIFEST"
printf '"openblas_ref":"%s","openblas_sha":"%s","blis_ref":"%s","blis_sha":"%s",' \
  "$OPENBLAS_REF" "$OB_SHA" "$BLIS_REF" "$BLIS_SHA" >> "$MANIFEST"
printf '"native_target":"%s","cross_target":"%s","host_sve":%s,"host_sve2":%s}\n' \
  "$NATIVE_TARGET" "$CROSS_TARGET" "$HAS_SVE" "$HAS_SVE2" >> "$MANIFEST"

log "manifest written to $MANIFEST"
cat "$MANIFEST" >&2

if ! grep -q '"target":"DYNAMIC","coretype":null,"blas_sha":"[0-9a-f]*","built":true' "$MANIFEST"; then
  die "the DYNAMIC_ARCH arm did not build. It carries the entire OPENBLAS_CORETYPE
     sweep, so there is no experiment without it. See $PREFIX/openblas-DYNAMIC.buildlog"
fi
