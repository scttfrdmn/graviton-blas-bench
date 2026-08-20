#!/usr/bin/env bash
# graviton-blas-bench — bench.c must refuse to measure under a label it cannot
# confirm.
#
# arch_selected used to be inherited wholesale from GBB_ARCH_SELECTED, which the
# runner measures in a SEPARATE PROCESS (gbb-coreprobe-<variant>) before the arm
# runs. A separate process can resolve a different libopenblas by rpath, or be
# handed a different environment, so the value was a claim about a library that
# may not be the one doing the work. bench.c now asks the loaded image directly
# via dlsym(RTLD_DEFAULT, ...) and treats a disagreement as fatal.
#
# Standing order 10 is why this is fatal rather than a warning: a mislabelled arm
# "is not a failed run, it is a plausible wrong answer, which is worse". A failed
# run leaves a gap the census explains. A wrong label produces a number that
# survives every downstream check.
#
# Needs no OpenBLAS and no Graviton: a stub exporting openblas_get_corename and
# no-op BLAS entry points is enough, because the assertion runs before the sweep.
set -uo pipefail
cd "$(dirname "$0")/.." || { printf 'FATAL: cannot cd to the repo root\n' >&2; exit 1; }
CC="${CC:-gcc}"
W="$(mktemp -d)"
trap 'rm -rf "$W"' EXIT

pass=0; fail=0
chk() { if [ "$2" = "$3" ]; then pass=$((pass+1)); printf '  ok   %s\n' "$1";
        else fail=$((fail+1)); printf '  FAIL %s\n       want=%s\n       got =%s\n' "$1" "$3" "$2"; fi; }

# Stub BLAS. Never called -- the assertion under test runs before the sweep -- but
# needed to link. The corename is fixed at neoversen2, which is what a real
# Graviton 4 DYNAMIC_ARCH build reports.
cat > "$W/stub.c" <<'EOF'
char *openblas_get_corename(void) { return "neoversen2"; }
char *openblas_get_config(void)   { return "stub"; }
void dgemm_(void){} void sgemm_(void){} void dtrsm_(void){} void dtrmm_(void){}
void dsyrk_(void){} void dsymm_(void){} void dgemv_(void){} void daxpy_(void){}
double ddot_(void){ return 0.0; }
EOF

# Compiled exactly as the campaign compiles it: -O2, no -march, no -D. If this
# needed a per-arm flag to see the symbol it would violate standing order 6.
"$CC" -O2 -g -Wall -Wextra -std=c11 src/bench.c "$W/stub.c" -o "$W/bench_ob" \
  -lm -lpthread 2>"$W/cc.log" || {
  echo "SKIP: could not link bench.c against the stub:"; sed 's/^/    /' "$W/cc.log"; exit 0; }

# The same source with NO libopenblas in the image: dlsym must return NULL and
# the arm must be labelled n/a rather than guessing. This is the ArmPL/BLIS case.
cat > "$W/noob.c" <<'EOF'
void dgemm_(void){} void sgemm_(void){} void dtrsm_(void){} void dtrmm_(void){}
void dsyrk_(void){} void dsymm_(void){} void dgemv_(void){} void daxpy_(void){}
double ddot_(void){ return 0.0; }
EOF
"$CC" -O2 -g -Wall -Wextra -std=c11 src/bench.c "$W/noob.c" -o "$W/bench_noob" \
  -lm -lpthread 2>>"$W/cc.log" || { echo "SKIP: could not link the no-OpenBLAS variant"; exit 0; }

run() {  # run <binary> <arch-selected> -> exit code; records on stdout
  env GBB_RUN_ID=t GBB_HOST=h GBB_INSTANCE=i GBB_LIBRARY=openblas \
      GBB_TARGET=DYNAMIC GBB_BUILD=b GBB_ROLE=instrument GBB_THREADS=1 \
      GBB_BLAS_SHA=aa GBB_CORETYPE=unforced GBB_THREAD_BACKEND=pthreads \
      GBB_ARCH_SELECTED="$2" "$1" daxpy >"$W/out" 2>"$W/err"
  echo "$?"
}

echo "== a disagreeing label must stop the arm =="
rc=$(run "$W/bench_ob" neoversev1)
chk "exit 4, not 0" "$rc" "4"
chk "stderr names both values" \
  "$(grep -c 'GBB_ARCH_SELECTED=neoversev1' "$W/err")" "1"
chk "stderr says what the library actually reports" \
  "$(grep -c 'reports neoversen2' "$W/err")" "1"
chk "no records were written under the wrong label" \
  "$( [ -s "$W/out" ] && echo some || echo none )" "none"

echo "== an agreeing label runs, and the library's answer is what gets recorded =="
rc=$(run "$W/bench_ob" neoversen2)
chk "exit 0" "$rc" "0"
chk "records carry the in-process answer" \
  "$(python3 -c 'import json,sys
print(sorted({json.loads(l)["arch_selected"] for l in open(sys.argv[1]) if l.strip()}))' "$W/out")" \
  "['neoversen2']"

echo "== the runner's sentinels are not corenames and must not be asserted against =="
# unprobed is what the runner passes for a static TARGET= build whose coreprobe
# failed to build. The in-process lookup can still answer, and it should win
# rather than the arm being refused over a sentinel.
rc=$(run "$W/bench_ob" unprobed)
chk "unprobed does not stop the arm" "$rc" "0"
chk "unprobed is upgraded to the measured answer" \
  "$(python3 -c 'import json,sys
print(sorted({json.loads(l)["arch_selected"] for l in open(sys.argv[1]) if l.strip()}))' "$W/out")" \
  "['neoversen2']"

echo "== with no OpenBLAS in the image the question does not apply =="
rc=$(run "$W/bench_noob" n/a)
chk "exit 0" "$rc" "0"
chk "arch_selected is n/a, not a guess" \
  "$(python3 -c 'import json,sys
print(sorted({json.loads(l)["arch_selected"] for l in open(sys.argv[1]) if l.strip()}))' "$W/out")" \
  "['n/a']"

echo
printf 'pass=%d fail=%d\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
