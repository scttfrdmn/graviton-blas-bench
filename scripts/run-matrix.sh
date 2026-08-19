#!/usr/bin/env bash
# graviton-blas-bench — run the full matrix on one host and append NDJSON to results/.
#
# Matrix on this host:  library x target x threads x routine x size
# The hardware axis is "which instance you ran this on"; the analysis joins
# across hosts on run_id.
#
# Thread control is the subtle part. Each library reads a different variable,
# and setting only OMP_NUM_THREADS silently leaves OpenBLAS at its own default.
# All of them are set, every time, to the same value.

set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BIN="$ROOT/bin"
PREFIX="${GBB_PREFIX:-$HOME/graviton-blas-bench-libs}"
RESULTS="${GBB_RESULTS:-$ROOT/results}"
RUN_ID="${GBB_RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)-$(hostname -s)}"
MANIFEST="$PREFIX/build-manifest.ndjson"

mkdir -p "$RESULTS"
export GBB_RUN_ID="$RUN_ID"
export GBB_HOST="$(hostname -s)"

log() { printf '[gbb] %s\n' "$*" >&2; }

# ---- provenance first, always --------------------------------------------
export GBB_OPENBLAS_DYNAMIC_DIR="$PREFIX/openblas-DYNAMIC"
ENVFILE="$RESULTS/env-$RUN_ID.json"
bash "$ROOT/scripts/capture-env.sh" > "$ENVFILE"
log "environment captured to $ENVFILE"

INSTANCE="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["instance_type"] or "unknown")' "$ENVFILE")"
CORES="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["cores_total"])' "$ENVFILE")"
export GBB_INSTANCE="$INSTANCE"

# ---- thread ladder --------------------------------------------------------
# 1 core isolates kernel quality. Half and full socket expose the threading
# layer and NUMA. 64 is included on every host regardless of size so the
# cross-generation comparison has one directly comparable point -- c6g/c7g/
# hpc7g top out at 64 while c8g/c9g reach 192.
build_threads() {
  local n="$1" out="1"
  for t in 8 16 32 64 96 128 192; do
    [ "$t" -le "$n" ] && out="$out $t"
  done
  [ "$n" -gt 1 ] && case " $out " in *" $n "*) ;; *) out="$out $n";; esac
  echo "$out"
}
THREADS="${GBB_THREADS_LADDER:-$(build_threads "$CORES")}"
log "instance=$INSTANCE cores=$CORES threads=[$THREADS]"

# ---- roofline, once per thread count --------------------------------------
ROOFOUT="$RESULTS/roofline-$RUN_ID.ndjson"
: > "$ROOFOUT"
for T in $THREADS; do
  GBB_THREADS="$T" OMP_NUM_THREADS="$T" OMP_PROC_BIND=close OMP_PLACES=cores \
    "$BIN/gbb-roofline" >> "$ROOFOUT" || {
      log "FATAL: roofline failed at threads=$T -- see stderr above. Aborting."
      exit 3; }
done
log "roofline written to $ROOFOUT"

# ---- which arms can actually run here -------------------------------------
runnable_arms() {
  python3 - "$MANIFEST" <<'PY'
import json, sys
for line in open(sys.argv[1]):
    line = line.strip()
    if not line: continue
    try: r = json.loads(line)
    except json.JSONDecodeError: continue
    if r.get("record") == "toolchain": continue
    if not r.get("built"): continue
    if not r.get("runnable", True): continue
    lib = r.get("library"); tgt = r.get("target", "native")
    if lib == "openblas": print(f"openblas\t{tgt}\tgbb-openblas-{tgt}")
    elif lib == "armpl":  print(f"armpl\tnative\tgbb-armpl")
    elif lib == "blis":   print(f"blis\t{tgt}\tgbb-blis")
PY
}

# ---- the sweep ------------------------------------------------------------
OUT="$RESULTS/bench-$RUN_ID.ndjson"
: > "$OUT"

while IFS=$'\t' read -r LIB TGT EXE; do
  [ -x "$BIN/$EXE" ] || { log "skip $LIB/$TGT: $BIN/$EXE not built"; continue; }
  for T in $THREADS; do
    log "run  library=$LIB target=$TGT threads=$T"
    env \
      GBB_LIBRARY="$LIB" \
      GBB_TARGET="$TGT" \
      GBB_BUILD="$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo nogit)" \
      GBB_THREADS="$T" \
      GBB_ARCH_SELECTED="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["openblas_dynamic_selection"])' "$ENVFILE")" \
      OPENBLAS_NUM_THREADS="$T" \
      OMP_NUM_THREADS="$T" \
      BLIS_NUM_THREADS="$T" \
      MKL_NUM_THREADS="$T" \
      OMP_PROC_BIND=close \
      OMP_PLACES=cores \
      "$BIN/$EXE" all >> "$OUT" 2>>"$RESULTS/stderr-$RUN_ID.log"
    rc=$?
    if [ $rc -ne 0 ]; then
      log "  arm $LIB/$TGT threads=$T exited $rc (SIGILL=132 means the target needs ISA this host lacks)"
      printf '{"run_id":"%s","library":"%s","target":"%s","threads":%d,"failed":true,"exit_code":%d}\n' \
        "$RUN_ID" "$LIB" "$TGT" "$T" "$rc" >> "$OUT"
    fi
  done
done < <(runnable_arms)

log "results: $OUT"
log "records: $(wc -l < "$OUT")"
