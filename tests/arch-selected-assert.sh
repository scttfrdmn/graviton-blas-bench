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
#
# THE STUB IS A SHARED LIBRARY, AND THAT IS NOT A DETAIL. bench.c finds the corename
# with dlsym(RTLD_DEFAULT, ...), which searches the process's loaded images. On ELF, a
# symbol compiled into the *executable* is absent from .dynsym unless the link passes
# -rdynamic, so an executable-resident stub is invisible to the very lookup under test:
# dlsym returns NULL, bench.c correctly concludes there is no OpenBLAS in the image,
# labels the arm n/a, and every assertion about refusing a disagreeing label passes
# vacuously. Mach-O exports executable globals by default, so the same fixture passed
# on the dev host and could only fail on the platform the campaign actually runs on.
# Adding -rdynamic would have fixed the symptom while making the fixture test a
# topology that does not exist -- in the campaign the corename comes out of
# libopenblas.so. A .so is what production looks like, so a .so is what this links.
set -uo pipefail
cd "$(dirname "$0")/.." || { printf 'FATAL: cannot cd to the repo root\n' >&2; exit 1; }
CC="${CC:-gcc}"
W="$(mktemp -d)"
trap 'rm -rf "$W"' EXIT

pass=0; fail=0
chk() { if [ "$2" = "$3" ]; then pass=$((pass+1)); printf '  ok   %s\n' "$1";
        else fail=$((fail+1)); printf '  FAIL %s\n       want=%s\n       got =%s\n' "$1" "$3" "$2"; fi; }

# A build failure is a FAILURE, not a skip -- unless there is no compiler at all, which
# is the only case where the question genuinely does not apply. `exit 0` on a broken
# build is the vacuous-pass shape gates/check-build-flags.sh was just fixed for, and it
# is how a fixture that has stopped compiling keeps reporting green: a renamed source
# file or a new dependency in bench.c would print SKIP and pass.
if ! command -v "$CC" >/dev/null 2>&1; then
  printf 'SKIP: no %s on this host, so nothing can be linked to ask the question\n' "$CC"
  exit 0
fi
die_build() { printf 'FAIL: %s\n' "$1" >&2; sed 's/^/    /' "$W/cc.log" >&2; exit 1; }

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

# The same source with NO libopenblas in the image: dlsym must return NULL and
# the arm must be labelled n/a rather than guessing. This is the ArmPL/BLIS case.
cat > "$W/noob.c" <<'EOF'
void dgemm_(void){} void sgemm_(void){} void dtrsm_(void){} void dtrmm_(void){}
void dsyrk_(void){} void dsymm_(void){} void dgemv_(void){} void daxpy_(void){}
double ddot_(void){ return 0.0; }
EOF

# Two stub .so's: one that answers the corename question and one that does not,
# which is the ArmPL/BLIS shape. Built as shared objects so that the dlsym under
# test resolves the way it does in the campaign -- see the header.
soname() { case "$(uname -s)" in Darwin) echo "$1.dylib" ;; *) echo "$1.so" ;; esac; }
OB_SO="$(soname "$W/libgbbstub_ob")"
NOOB_SO="$(soname "$W/libgbbstub_noob")"
"$CC" -O2 -g -Wall -Wextra -std=c11 -fPIC -shared "$W/stub.c" -o "$OB_SO" \
  2>"$W/cc.log" || die_build "cannot build the corename stub .so"
"$CC" -O2 -g -Wall -Wextra -std=c11 -fPIC -shared "$W/noob.c" -o "$NOOB_SO" \
  2>>"$W/cc.log" || die_build "cannot build the no-OpenBLAS stub .so"

# Compiled exactly as the campaign compiles it: -O2, no -march, no -D. If this
# needed a per-arm flag to see the symbol it would violate standing order 6.
# -rdynamic is deliberately NOT here: needing it would mean the fixture had put the
# symbol somewhere the campaign never puts it.
link_bench() {  # link_bench <out> <stub.so>
  "$CC" -O2 -g -Wall -Wextra -std=c11 src/bench.c -o "$1" \
    "$2" -Wl,-rpath,"$W" -lm -lpthread 2>>"$W/cc.log"
}
link_bench "$W/bench_ob" "$OB_SO" || die_build "could not link bench.c against the corename stub"
link_bench "$W/bench_noob" "$NOOB_SO" || die_build "could not link the no-OpenBLAS variant"

# The fixture proves it is testing what it claims before it asserts anything with it.
# Without this, the whole suite passes vacuously the moment dlsym stops resolving --
# which is exactly how it came to pass on Mach-O and fail on ELF.
probe_arch() {  # probe_arch <binary> -> the arch_selected the binary recorded
  env GBB_RUN_ID=t GBB_HOST=h GBB_INSTANCE=i GBB_LIBRARY=openblas GBB_TARGET=DYNAMIC \
      GBB_BUILD=b GBB_ROLE=instrument GBB_THREADS=1 GBB_BLAS_SHA=aa \
      GBB_CORETYPE=unforced GBB_THREAD_BACKEND=pthreads GBB_ARCH_SELECTED=unprobed \
      "$1" daxpy 2>/dev/null | python3 -c 'import json,sys
print(sorted({json.loads(l)["arch_selected"] for l in sys.stdin if l.strip()}))'
}
if [ "$(probe_arch "$W/bench_ob")" != "['neoversen2']" ]; then
  printf 'FATAL: the stub .so is loaded but dlsym(RTLD_DEFAULT) did not find\n' >&2
  printf '  openblas_get_corename in it, so every assertion below would pass\n' >&2
  printf '  vacuously against a binary that thinks it has no OpenBLAS. Refusing to\n' >&2
  printf '  report a green this fixture has not earned. saw: %s\n' \
    "$(probe_arch "$W/bench_ob")" >&2
  exit 1
fi

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
