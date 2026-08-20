#!/usr/bin/env bash
# diag-numa.sh -- is the t>=128 cliff the memory policy, or the hardware?
#
# WHY THIS EXISTS
#
# run-matrix.sh's pin_for() derives the memory policy from the thread count:
# --membind=<node> while the threads fit one NUMA node, --interleave=<nodes> once
# they span two. On c8g.metal-48xl (2 nodes x 96 cpus) that switch lands at
# exactly t=128, so the top two rungs of the thread ladder are measured under a
# different memory policy than the six below them. Two roofline numbers fall off
# a cliff at precisely that rung:
#
#   triad GB/s        356.7 (t=64) -> 368.5 (t=96) -> 133.9 (t=128) -> 195.5 (t=192)
#   allcore scal_eff  0.959        -> 0.942        -> 0.531        -> 0.446
#
# Interleaving puts half the pages one hop away, which should cost roughly 2x on
# a streaming kernel, not 2.75x. And the second row cannot be a memory-policy
# effect at all: peak_fma_allcore is 32 doubles per thread living in registers
# and L1, with no streaming traffic to place. So there are at least two
# mechanisms at t>=128 and only one of them is a candidate for the policy switch.
#
# WHAT THIS IS *NOT* FOR. The GEMM headline is not confounded, and that was
# settled from the sweep itself rather than here: across the policy switch every
# one of the six DYNAMIC coretype arms scales t=96 -> t=128 by 1.30-1.32x against
# an ideal of 1.333x, i.e. 98% of ideal, and t=64 -> t=96 by 1.49x against 1.50x.
# Cache-blocked GEMM does not notice the switch. What *does* notice it is dgemv,
# which loses throughput crossing the rung (128/96 ratios of 0.69-0.99, varying
# by arm), and the two roofline instrument numbers above. So this diagnostic is
# about the roofline denominator and the memory-bound routines, not about the
# headline -- and its output decides one P3 question: whether pin_for() should
# keep deriving the policy from the thread count.
#
# QUARANTINE, BY CONSTRUCTION AND NOT BY DISCIPLINE
#
# This runs the same binaries over the same matrix_id as the campaign, so nothing
# in the *shape* of a record keeps it out of the campaign dataset. Three separate
# mechanisms do:
#
#   1. GBB_ROLE=diagnostic is stamped into every record by bench.c and
#      roofline.c. decompose.py's load() drops any record whose role is not the
#      requested one (default "campaign") before the shape dispatch, and reports
#      the drop as a `role_excluded` anomaly. That is a hard exclusion that
#      announces itself, which is the property that matters.
#   2. A distinct run_id namespace: diag-numa-<utc>-<host>. It can never collide
#      with a campaign run_id, so the two never share an output path.
#   3. A separate results directory and a separate S3 prefix
#      (gbb/diagnostics/, never gbb/campaign/), so an `aws s3 sync` of the
#      campaign prefix cannot pull these in.
#
# This deliberately does NOT go through run-matrix.sh. That script derives the
# role from IMDS evidence and would correctly stamp role=campaign here -- the
# host *is* a campaign host. The role has to be asserted by the caller, so the
# caller has to be this script.
#
# OMP_PROC_BIND. Two cells set OMP_PROC_BIND=close/OMP_PLACES=cores, which
# standing order 9 forbids in the campaign. That prohibition is about equalising
# threading across arms, and it is not what is happening here: the campaign cells
# in this diagnostic all use OMP_PROC_BIND=false exactly as the sweep does, and
# the bound cells exist to *identify a mechanism* by varying one thing. They are
# quarantined with everything else and no campaign number is derived from them.

set -euo pipefail

WORK="${GBB_WORK:-/opt/gbb-work}"
BIN="$WORK/repo/bin"
RESULTS="${GBB_DIAG_RESULTS:-$WORK/results-diag}"
S3_URI="${GBB_S3_URI:-s3://gbb-results-942542972736-us-east-1/gbb}"
AWS_REGION_ARG="${GBB_AWS_REGION:-us-east-1}"
# Two passes per roofline cell. Cheap (each cell is seconds) and it is the only
# thing separating "the cliff" from "one bad sample at one thread count".
REPS="${GBB_DIAG_REPS:-2}"

log() { printf '[diag] %s\n' "$*" >&2; }
die() { printf '[diag] FATAL: %s\n' "$*" >&2; exit 1; }

command -v numactl >/dev/null 2>&1 || die "numactl absent; every cell here is a numactl policy"
[ -x "$BIN/gbb-roofline" ] || die "no $BIN/gbb-roofline"
[ -x "$BIN/gbb-openblas-DYNAMIC" ] || die "no $BIN/gbb-openblas-DYNAMIC"

HOST="$(hostname -s)"
RUN_ID="diag-numa-$(date -u +%Y%m%dT%H%M%SZ)-$HOST"
mkdir -p "$RESULTS"
ROOFOUT="$RESULTS/roofline-$RUN_ID.ndjson"
OUT="$RESULTS/bench-$RUN_ID.ndjson"
CELLS="$RESULTS/cells-$RUN_ID.ndjson"
TOPO="$RESULTS/topology-$RUN_ID.txt"
ERRLOG="$RESULTS/stderr-$RUN_ID.log"
: > "$ROOFOUT"; : > "$OUT"; : > "$CELLS"; : > "$ERRLOG"

# Provenance. The instance and build identity have to be recorded here too: a
# diagnostic that decides a policy question is only worth what its provenance is
# worth, same as a measurement.
INSTANCE="$(curl -fsS -m 2 -H "X-aws-ec2-metadata-token: $(curl -fsS -m 2 -X PUT \
  -H 'X-aws-ec2-metadata-token-ttl-seconds: 60' \
  http://169.254.169.254/latest/api/token 2>/dev/null)" \
  http://169.254.169.254/latest/meta-data/instance-type 2>/dev/null || echo unknown)"
GBB_BUILD="$(cut -d' ' -f1 < "$WORK/HEAD.txt" 2>/dev/null || echo unknown)"
BLAS_SHA="$(python3 - "$WORK/libs/build-manifest.ndjson" <<'PY' 2>/dev/null || echo unknown
import json, sys
for line in open(sys.argv[1]):
    line = line.strip()
    if not line:
        continue
    r = json.loads(line)
    if r.get("record") == "arm" and r.get("library") == "openblas" and r.get("target") == "DYNAMIC":
        print(r.get("blas_sha") or "unknown")
        break
PY
)"
numactl -H > "$TOPO" 2>&1 || true

log "run_id=$RUN_ID role=diagnostic instance=$INSTANCE build=$GBB_BUILD blas_sha=$BLAS_SHA"
log "results -> $RESULTS"

# ---------------------------------------------------------------------------
# The cell table. Every cell names the cpuspec and the memory policy
# explicitly, because the whole point is that the policy is no longer derived
# from the thread count -- it is the independent variable.
#
# Node layout is asserted rather than assumed: if this host is not 2 x 96 the
# cpuspecs below are wrong and the script must stop, not measure something else.
# ---------------------------------------------------------------------------
python3 - "$TOPO" <<'PY' || die "node layout is not the 2 x 96 this cell table was written for -- re-derive the cpuspecs"
import re, sys
topo = open(sys.argv[1]).read()
nodes = {int(m.group(1)): [int(x) for x in m.group(2).split()]
         for m in re.finditer(r'^node (\d+) cpus:(.*)$', topo, re.M)}
nodes = {k: v for k, v in nodes.items() if v}
ok = (sorted(nodes) == [0, 1]
      and nodes[0] == list(range(0, 96))
      and nodes[1] == list(range(96, 192)))
sys.exit(0 if ok else 1)
PY

# name|threads|cpuspec|mempolicy|ompbind|what it isolates
ROOF_CELLS='
t96-node0-membind0|96|0-95|--membind=0|false|sweep baseline at the last good rung; must reproduce ~368 GB/s or nothing else here is readable
t96-node0-interleave|96|0-95|--interleave=0,1|false|THE CONTROL: same 96 cpus on one node, sweep t>=128 memory policy. Isolates policy from thread count
t96-node0-membind1|96|0-95|--membind=1|false|every page one hop away. The pure cross-node cost, an upper bound on what interleave can charge
t96-split-interleave|96|0-47,96-143|--interleave=0,1|false|THE OTHER CONTROL: 96 threads spanning both sockets under the t>=128 policy. Isolates socket span from thread count
t96-split-localalloc|96|0-47,96-143|--localalloc|false|same span, best-case placement. The gap to the line above is what interleave costs when the span is fixed
t128-interleave|128|0-127|--interleave=0,1|false|sweep baseline at the cliff; must reproduce ~134 GB/s
t128-membind01|128|0-127|--membind=0,1|false|both nodes eligible, first-touch decides. roofline.c first-touches under the reading thread layout, so this should beat interleave if placement is the mechanism
t128-localalloc|128|0-127|--localalloc|false|best-case placement at the cliff rung
t192-interleave|192|0-191|--interleave=0,1|false|sweep baseline at the top rung; must reproduce ~196 GB/s
t192-localalloc|192|0-191|--localalloc|false|best-case placement at the top rung
t128-interleave-ompbind|128|0-127|--interleave=0,1|close|allcore FMA has no streaming traffic, so its cliff cannot be the memory policy. If binding fixes it the mechanism is thread placement
t192-interleave-ompbind|192|0-191|--interleave=0,1|close|same at the top rung
t96-node0-membind0-ompbind|96|0-95|--membind=0|close|control for the two above: does binding move the rung that was already fine?
'

# Routine-resolved cells. gbb-bench has no case filter, so each of these is the
# full 544-case matrix (~2 min at these thread counts) -- which is a feature: it
# gives dgemv, dgemm and everything else at each policy for the price of the one
# routine that motivated it. Kept to three cells because that is what the
# question needs: reproduce the drop, then try to remove it two ways.
BENCH_CELLS='
t128-interleave|128|0-127|--interleave=0,1|false|reproduces the sweep dgemv drop under the sweep policy
t128-localalloc|128|0-127|--localalloc|false|does dgemv recover when placement is best-case? If yes, pin_for() is costing the campaign real throughput at t>=128
t96-split-interleave|96|0-47,96-143|--interleave=0,1|false|dgemv at a thread count that was fine, spanning both sockets under the cliff policy
'

# OMP_PLACES only exists where binding is on. Setting OMP_PLACES=cores alongside
# OMP_PROC_BIND=false is not the sweep's environment, and the sweep's environment
# is what the unbound cells are supposed to be reproducing.
omp_env() {
  case "$1" in
    false|"") printf 'OMP_PROC_BIND=false' ;;
    *)        printf 'OMP_PROC_BIND=%s OMP_PLACES=cores' "$1" ;;
  esac
}

# Emit one diag_cell record. Kept in one place so the roofline and bench paths
# cannot drift in what they record about a cell.
emit_cell() {
  # emit_cell <instrument> <cell> <threads> <cpus> <mem> <bind> <rep> <rc> <records> <t0> <t1> <what>
  RUN_ID="$RUN_ID" HOST="$HOST" INSTANCE="$INSTANCE" BUILD="$GBB_BUILD" SHA="$BLAS_SHA" \
    python3 -c '
import json, os, sys
a = sys.argv[1:]
print(json.dumps({
    "record": "diag_cell", "run_id": os.environ["RUN_ID"], "role": "diagnostic",
    "host": os.environ["HOST"], "instance": os.environ["INSTANCE"],
    "build": os.environ["BUILD"], "blas_sha": os.environ["SHA"],
    "instrument": a[0], "cell": a[1], "threads": int(a[2]),
    "cpuspec": a[3], "mem_policy": a[4], "omp_proc_bind": a[5],
    "rep": int(a[6]), "rc": int(a[7]), "records": int(a[8]),
    "seconds": round(float(a[10]) - float(a[9]), 3), "isolates": a[11],
}, sort_keys=True))' "$@" >> "$CELLS"
}

run_roof_cell() {
  local name="$1" T="$2" cpus="$3" mem="$4" bind="$5" what="$6" rep="$7"
  local pol="numactl --physcpubind=$cpus $mem;omp_bind=$bind"
  local before after rc t0 t1
  before=$(wc -l < "$ROOFOUT")
  t0=$(date +%s.%N)
  set +e
  # shellcheck disable=SC2046
  env GBB_RUN_ID="$RUN_ID" GBB_HOST="$HOST" GBB_INSTANCE="$INSTANCE" \
      GBB_BUILD="$GBB_BUILD" GBB_ROLE=diagnostic \
      GBB_THREADS="$T" OMP_NUM_THREADS="$T" $(omp_env "$bind") \
      GBB_DIAG_CELL="$name" \
      numactl --physcpubind="$cpus" "$mem" "$BIN/gbb-roofline" \
      >> "$ROOFOUT" 2>>"$ERRLOG"
  rc=$?
  set -e
  t1=$(date +%s.%N)
  after=$(wc -l < "$ROOFOUT")
  emit_cell roofline "$name" "$T" "$cpus" "$mem" "$bind" "$rep" "$rc" \
    "$((after - before))" "$t0" "$t1" "$what"
  log "  roofline $name rep=$rep rc=$rc records=$((after - before)) policy='$pol'"
  # A failed cell is recorded and the sweep of cells continues: a hole with a
  # reason beats aborting the other twelve (standing order 11, one level down).
  [ "$rc" -eq 0 ] || log "  ^^ FAILED, see $ERRLOG"
}

run_bench_cell() {
  local name="$1" T="$2" cpus="$3" mem="$4" bind="$5" what="$6" rep="$7"
  local pol="numactl --physcpubind=$cpus $mem;omp_bind=$bind"
  local before after rc t0 t1
  before=$(wc -l < "$OUT")
  t0=$(date +%s.%N)
  set +e
  # GBB_ARCH_SELECTED is left at the "unknown" sentinel on purpose: bench.c's
  # in-process openblas_get_corename() then wins uncontested and stamps what the
  # library actually reports, which is standing order 10's rule. There is no
  # separate coreprobe run to agree with here, so asserting a value would be
  # asserting one this script did not read.
  # shellcheck disable=SC2046
  env GBB_RUN_ID="$RUN_ID" GBB_HOST="$HOST" GBB_INSTANCE="$INSTANCE" \
      GBB_LIBRARY=openblas GBB_TARGET=DYNAMIC GBB_BLAS_SHA="$BLAS_SHA" \
      GBB_CORETYPE=unforced GBB_THREAD_BACKEND=pthreads \
      GBB_PIN_POLICY="$pol" GBB_BUILD="$GBB_BUILD" \
      GBB_ARCH_SELECTED=unknown GBB_ROLE=diagnostic \
      GBB_THREADS="$T" OPENBLAS_NUM_THREADS="$T" OMP_NUM_THREADS="$T" \
      BLIS_NUM_THREADS="$T" $(omp_env "$bind") \
      numactl --physcpubind="$cpus" "$mem" "$BIN/gbb-openblas-DYNAMIC" all \
      >> "$OUT" 2>>"$ERRLOG"
  rc=$?
  set -e
  t1=$(date +%s.%N)
  after=$(wc -l < "$OUT")
  emit_cell bench "$name" "$T" "$cpus" "$mem" "$bind" "$rep" "$rc" \
    "$((after - before))" "$t0" "$t1" "$what"
  log "  bench $name rep=$rep rc=$rc records=$((after - before)) policy='$pol'"
  [ "$rc" -eq 0 ] || log "  ^^ FAILED, see $ERRLOG"
}

log "=== roofline cells (${REPS} reps each) ==="
for rep in $(seq 1 "$REPS"); do
  printf '%s\n' "$ROOF_CELLS" | while IFS='|' read -r name T cpus mem bind what; do
    [ -n "${name:-}" ] || continue
    run_roof_cell "$name" "$T" "$cpus" "$mem" "$bind" "$what" "$rep"
  done
done

log "=== bench cells (1 rep each; each is the full 544-case matrix) ==="
printf '%s\n' "$BENCH_CELLS" | while IFS='|' read -r name T cpus mem bind what; do
  [ -n "${name:-}" ] || continue
  run_bench_cell "$name" "$T" "$cpus" "$mem" "$bind" "$what" 1
done

# ---------------------------------------------------------------------------
# Ship. gbb/diagnostics/, never gbb/campaign/ -- see QUARANTINE above.
# ---------------------------------------------------------------------------
DEST="${S3_URI%/}/diagnostics/$HOST/$RUN_ID"
log "shipping to $DEST"
if aws s3 cp --region "$AWS_REGION_ARG" --recursive --only-show-errors \
     "$RESULTS/" "$DEST/" 2>>"$ERRLOG"; then
  log "shipped"
else
  log "SHIP FAILED -- results are on the instance only, see $ERRLOG"
fi

log "done: run_id=$RUN_ID"
log "  $(wc -l < "$CELLS") cells, $(wc -l < "$ROOFOUT") roofline records, $(wc -l < "$OUT") bench records"
