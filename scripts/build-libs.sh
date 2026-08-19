#!/usr/bin/env bash
# graviton-blas-bench — build every library arm on the current host and record what was built.
#
# The point of this campaign is the hardware x target cross: the same OpenBLAS
# TARGET= is built on every instance, so "V2 is bad at SVE" can be separated
# from "the N2 kernel set is worse". That means building targets the host is
# not, which is legal (TARGET= is a build-time kernel selection) but means the
# resulting library may use instructions the host lacks. Arms that cannot run
# on this host are built anyway and marked unrunnable in the manifest; the
# runner skips them rather than crashing on SIGILL.

set -euo pipefail

PREFIX="${GBB_PREFIX:-$HOME/graviton-blas-bench-libs}"
SRCDIR="${GBB_SRC:-$HOME/graviton-blas-bench-src}"
JOBS="${JOBS:-$(nproc)}"
OPENBLAS_REF="${OPENBLAS_REF:-develop}"
BLIS_REF="${BLIS_REF:-master}"
MANIFEST="$PREFIX/build-manifest.ndjson"

mkdir -p "$PREFIX" "$SRCDIR"
: > "$MANIFEST"

log() { printf '[gbb-build] %s\n' "$*" >&2; }

# ---- which OpenBLAS targets to build -------------------------------------
# The cross. NEOVERSEN1 has no SVE at all; NEOVERSEV1 is the SVE-rich set
# (99 SVE kernels); NEOVERSEV2 resolves to KERNEL.NEOVERSEN2 (5 SVE kernels);
# ARMV8SVE is the base SVE set that V1/A64FX inherit; ARMV8 is the generic
# NEON fallback that a failed CPU detection lands on.
TARGETS="${GBB_TARGETS:-ARMV8 NEOVERSEN1 ARMV8SVE NEOVERSEV1 NEOVERSEV2 DYNAMIC}"

# What each target requires of the host, so unrunnable arms can be flagged.
requires() {
  case "$1" in
    ARMV8|NEOVERSEN1) echo "none" ;;
    ARMV8SVE|NEOVERSEV1) echo "sve" ;;
    NEOVERSEV2) echo "sve2" ;;
    DYNAMIC) echo "none" ;;
    *) echo "unknown" ;;
  esac
}

host_has() {
  case "$1" in
    none) return 0 ;;
    sve)  grep -qw sve  /proc/cpuinfo && return 0 || return 1 ;;
    sve2) grep -qw sve2 /proc/cpuinfo && return 0 || return 1 ;;
    *) return 1 ;;
  esac
}

# ---- OpenBLAS -------------------------------------------------------------
if [ ! -d "$SRCDIR/OpenBLAS" ]; then
  log "cloning OpenBLAS ($OPENBLAS_REF)"
  git clone --quiet https://github.com/OpenMathLib/OpenBLAS.git "$SRCDIR/OpenBLAS"
fi
cd "$SRCDIR/OpenBLAS"
git fetch --quiet origin "$OPENBLAS_REF"
git checkout --quiet "$OPENBLAS_REF"
git pull --quiet --ff-only origin "$OPENBLAS_REF" 2>/dev/null || true
OB_SHA="$(git rev-parse --short HEAD)"
log "OpenBLAS at $OB_SHA"

for T in $TARGETS; do
  DEST="$PREFIX/openblas-$T"
  REQ="$(requires "$T")"
  RUNNABLE=true; host_has "$REQ" || RUNNABLE=false

  log "building OpenBLAS TARGET=$T (requires=$REQ runnable=$RUNNABLE)"
  make -C "$SRCDIR/OpenBLAS" clean >/dev/null 2>&1 || true

  if [ "$T" = "DYNAMIC" ]; then
    # DYNAMIC_ARCH is what distro packages and NumPy wheels ship. Its runtime
    # choice is a measurement in its own right -- see capture-env.sh, which
    # records openblas_get_corename() output per host.
    BUILD_ARGS=(DYNAMIC_ARCH=1 NUM_THREADS=256 USE_OPENMP=0)
  else
    BUILD_ARGS=(TARGET="$T" NUM_THREADS=256 USE_OPENMP=0)
  fi

  if make -C "$SRCDIR/OpenBLAS" -j"$JOBS" "${BUILD_ARGS[@]}" >"$PREFIX/openblas-$T.buildlog" 2>&1 \
     && make -C "$SRCDIR/OpenBLAS" "${BUILD_ARGS[@]}" PREFIX="$DEST" install \
        >>"$PREFIX/openblas-$T.buildlog" 2>&1; then
    BUILD_OK=true
    make -C "$(dirname "$0")/.." openblas OPENBLAS_DIR="$DEST" VARIANT="$T" \
      >>"$PREFIX/openblas-$T.buildlog" 2>&1 || BUILD_OK=false
  else
    BUILD_OK=false
    log "  BUILD FAILED -- see $PREFIX/openblas-$T.buildlog"
  fi

  printf '{"library":"openblas","target":"%s","sha":"%s","built":%s,"requires":"%s","runnable":%s,"prefix":"%s"}\n' \
    "$T" "$OB_SHA" "$BUILD_OK" "$REQ" "$RUNNABLE" "$DEST" >> "$MANIFEST"
done

# ---- ArmPL ----------------------------------------------------------------
# ArmPL is a download, not a build. Install it out of band (it ships as a deb
# or a tarball from developer.arm.com) and point ARMPL_DIR at the prefix.
if [ -n "${ARMPL_DIR:-}" ] && [ -d "$ARMPL_DIR" ]; then
  log "linking against ArmPL at $ARMPL_DIR"
  if make -C "$(dirname "$0")/.." armpl ARMPL_DIR="$ARMPL_DIR" \
       >"$PREFIX/armpl.buildlog" 2>&1; then OK=true; else OK=false; fi
  AV="$(basename "$ARMPL_DIR")"
  printf '{"library":"armpl","target":"native","version":"%s","built":%s,"requires":"none","runnable":true,"prefix":"%s"}\n' \
    "$AV" "$OK" "$ARMPL_DIR" >> "$MANIFEST"
else
  log "ARMPL_DIR unset or missing -- skipping ArmPL arm"
  printf '{"library":"armpl","built":false,"note":"ARMPL_DIR unset"}\n' >> "$MANIFEST"
fi

# ---- BLIS -----------------------------------------------------------------
if [ ! -d "$SRCDIR/blis" ]; then
  log "cloning BLIS"
  git clone --quiet https://github.com/flame/blis.git "$SRCDIR/blis"
fi
cd "$SRCDIR/blis"
git checkout --quiet "$BLIS_REF"
BLIS_SHA="$(git rev-parse --short HEAD)"
BLIS_CONF="${BLIS_CONFIG:-auto}"
log "building BLIS $BLIS_SHA config=$BLIS_CONF"
if ./configure --prefix="$PREFIX/blis" -t pthreads "$BLIS_CONF" >"$PREFIX/blis.buildlog" 2>&1 \
   && make -j"$JOBS" >>"$PREFIX/blis.buildlog" 2>&1 \
   && make install >>"$PREFIX/blis.buildlog" 2>&1; then
  OK=true
  make -C "$(dirname "$0")/.." blis BLIS_DIR="$PREFIX/blis" >>"$PREFIX/blis.buildlog" 2>&1 || OK=false
else
  OK=false
  log "  BLIS build failed -- see $PREFIX/blis.buildlog"
fi
printf '{"library":"blis","target":"%s","sha":"%s","built":%s,"requires":"none","runnable":true,"prefix":"%s"}\n' \
  "$BLIS_CONF" "$BLIS_SHA" "$OK" "$PREFIX/blis" >> "$MANIFEST"

# ---- reference netlib (correctness control) -------------------------------
if make -C "$(dirname "$0")/.." reference >"$PREFIX/reference.buildlog" 2>&1; then
  printf '{"library":"reference","built":true,"note":"correctness control only"}\n' >> "$MANIFEST"
fi

# ---- compiler provenance --------------------------------------------------
printf '{"record":"toolchain","cc":"%s","cc_version":"%s","kernel":"%s","libc":"%s"}\n' \
  "${CC:-gcc}" "$(${CC:-gcc} --version | head -1 | tr -d '"')" \
  "$(uname -r)" "$(ldd --version | head -1 | tr -d '"')" >> "$MANIFEST"

log "manifest written to $MANIFEST"
cat "$MANIFEST" >&2
