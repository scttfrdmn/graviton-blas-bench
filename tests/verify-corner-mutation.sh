#!/usr/bin/env bash
# tests/verify-corner-mutation.sh -- mutation-validate the per-routine corner checks.
#
# WHY THIS EXISTS. A correctness check that never fires is indistinguishable from
# `return 1;`, and the campaign has just been through the version of that mistake
# where the value was hardcoded. Running the harness against a real BLAS and
# seeing `verified: true` everywhere proves only that the checks do not produce
# FALSE POSITIVES. It says nothing about whether they would catch a wrong answer.
# So each check is validated the way every fixture in this repo is: break the
# thing it is supposed to notice, and require it to notice.
#
# EACH MUTATION OWNS ITS EXPECTATION (the gates/p1.sh rule, applied here). An
# unknown expectation kind FAILs rather than being skipped, and adding a mutation
# does not require editing the driver loop.
#
# WHAT IS MUTATED AND WHAT IS NOT. The mutations are injected into a SCRATCH COPY
# of src/bench.c, never the tree, and the scratch binary never writes to
# results/. Two things are altered in the scratch copy beyond the mutation
# itself, both purely to keep the fixture to seconds rather than an hour, and
# neither of which the checks' logic depends on:
#   - the timing floors drop to 1 ms, because this fixture asserts on the
#     `verified` field and not on any timing;
#   - the size ladders are cut to four rungs, one per regime, keeping n=2048 so
#     the large-m behaviour is still exercised.
# The scratch binary therefore reports a different matrix_id than the campaign
# one, which is correct: it is a different matrix, and it is not a measurement.
#
# THIS FIXTURE NEEDS A REAL BLAS AND FAILS WITHOUT ONE. It does not skip. A test
# that a missing dependency silently disables is not a test -- the CI comment on
# gate-p1 says exactly this, and it would be absurd to write the same hole here.
# That is also why this is not wired into gates/p0.sh: p0 must pass on a clean
# clone with no BLAS present, so this runs on the campaign hosts and locally.

set -uo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || { printf 'FATAL: cannot cd to %s\n' "$ROOT" >&2; exit 1; }

SCRATCH="$(mktemp -d "${TMPDIR:-/tmp}/gbb-vcm.XXXXXX")"
trap 'rm -rf "$SCRATCH"' EXIT

CC="${CC:-cc}"
CFLAGS_T="-O2 -g -Wall -Wextra -std=c11"

# ---- find a BLAS ---------------------------------------------------------
BLASLIBS=""
probe="$SCRATCH/probe.c"
cat >"$probe" <<'EOF'
void dgemm_(const char*,const char*,const int*,const int*,const int*,
            const double*,const double*,const int*,const double*,const int*,
            const double*,double*,const int*);
int main(void){ return 0; }
EOF
for cand in "-framework Accelerate" "-lopenblas" "-lblas"; do
    # shellcheck disable=SC2086  # cand is a deliberate multi-word link spec
    if $CC $CFLAGS_T "$probe" -o "$SCRATCH/probe" -lm $cand >/dev/null 2>&1; then
        BLASLIBS="$cand"; break
    fi
done
if [ -z "$BLASLIBS" ]; then
    printf 'FAIL: no BLAS found to validate against (tried Accelerate, -lopenblas, -lblas).\n' >&2
    printf '      This fixture asserts that the corner checks CATCH a wrong answer, which\n' >&2
    printf '      cannot be established without a library that produces right ones.\n' >&2
    exit 1
fi
printf 'blas: %s\n' "$BLASLIBS"

# ---- the fast scratch source --------------------------------------------
BASE="$SCRATCH/bench-base.c"
sed -e 's/^#define MIN_SECONDS          0.30/#define MIN_SECONDS          0.001/' \
    -e 's/^#define MIN_SECONDS_SMALL    0.05/#define MIN_SECONDS_SMALL    0.001/' \
    -e 's/^static const int SIZES_SMALL\[\]  = { 8, 16, 24, 32, 40, 48, 56, 64,$/static const int SIZES_SMALL[]  = { 8, 64,/' \
    -e 's/^                                    80, 96, 112, 128, 160, 192, 224, 256 };$/                                    };/' \
    -e 's/^static const int SIZES_MEDIUM\[\] = { 320, 384, 448, 512, 640, 768, 896, 1024,$/static const int SIZES_MEDIUM[] = { 512,/' \
    -e 's/^                                    1280, 1536 };$/                                    };/' \
    -e 's/^static const int SIZES_LARGE\[\]  = { 2048, 3072, 4096, 6144, 8192 };$/static const int SIZES_LARGE[]  = { 2048 };/' \
    src/bench.c >"$BASE"

for probe_re in 'MIN_SECONDS          0.001' 'SIZES_SMALL\[\]  = { 8, 64,' \
                'SIZES_MEDIUM\[\] = { 512,' 'SIZES_LARGE\[\]  = { 2048 };'; do
    if ! grep -q "$probe_re" "$BASE"; then
        printf 'FAIL: scratch rewrite did not apply (%s). src/bench.c has drifted from\n' "$probe_re" >&2
        printf '      what this fixture greps for; fix the sed rather than the assertion.\n' >&2
        exit 1
    fi
done

# ---- mutation table ------------------------------------------------------
# One record per line: name | routine | anchor | expectation | C statement
#
# expectation kinds:
#   fires        -- at least one record must carry verified:false
#   silent       -- no record may carry verified:false (a no-false-positive claim)
#   blind_large  -- every failing record must have m < 2048: the check still
#                   catches the corruption at small m but goes quiet at large m.
#                   Used to demonstrate that a tolerance choice is load-bearing.
MUTATIONS=$(cat <<'EOF'
gemm-perturbed-corner|dgemm|verify_gemm_corner|fires|C[0] *= 1.0 + 1e-9;
sgemm-perturbed-corner|sgemm|verify_sgemm_corner|fires|C[0] *= 1.0f + 1e-2f;
syrk-perturbed-lower|dsyrk|verify_syrk_corner|fires|C[0] *= 1.0 + 1e-9;
syrk-upper-triangle-garbage|dsyrk|verify_syrk_corner|silent|for (int _j=1;_j<4&&_j<n;_j++) for (int _i=0;_i<_j;_i++) C[(size_t)_j*ldc+_i]=1e30;
symm-perturbed-corner|dsymm|verify_symm_corner|fires|C[0] *= 1.0 + 1e-9;
gemv-perturbed-corner|dgemv|verify_gemv_corner|fires|y[0] *= 1.0 + 1e-9;
axpy-perturbed-corner|daxpy|verify_axpy_corner|fires|y[0] *= 1.0 + 1e-9;
dot-perturbed-result|ddot|verify_dot_result|fires|/*MUT_DOT*/
trsm-ignores-offdiagonal|dtrsm|verify_trsm_corner|fires|for (int _j=0;_j<4&&_j<n;_j++) for (int _i=0;_i<4&&_i<m;_i++) B[(size_t)_j*ldb+_i]=alpha*B0[(size_t)_j*ldb+_i];
trmm-ignores-offdiagonal|dtrmm|verify_trmm_corner|fires|for (int _j=0;_j<4&&_j<n;_j++) for (int _i=0;_i<4&&_i<m;_i++) B[(size_t)_j*ldb+_i]=alpha*B0[(size_t)_j*ldb+_i];
trsm-m-scaled-tolerance-is-blind|dtrsm|verify_trsm_corner|blind_large|/*MUT_TOL*/for (int _j=0;_j<4&&_j<n;_j++) for (int _i=0;_i<4&&_i<m;_i++) B[(size_t)_j*ldb+_i]=alpha*B0[(size_t)_j*ldb+_i];
EOF
)

RC=0
NRUN=0

# Baseline: the unmutated scratch binary must be clean on every routine. If this
# fails, nothing below means anything -- a check that false-positives makes every
# "fires" result unattributable to the mutation.
printf '\n== baseline (no mutation) ==\n'
# shellcheck disable=SC2086
if ! $CC $CFLAGS_T "$BASE" -o "$SCRATCH/bench-base" -lm -lpthread $BLASLIBS 2>"$SCRATCH/base.cc.log"; then
    printf 'FAIL baseline: compile failed\n' >&2; sed -n '1,20p' "$SCRATCH/base.cc.log" >&2; exit 1
fi
if grep -q . "$SCRATCH/base.cc.log"; then
    printf 'FAIL baseline: compiler emitted diagnostics under -Wall -Wextra\n' >&2
    sed -n '1,20p' "$SCRATCH/base.cc.log" >&2; RC=1
fi
OPENBLAS_NUM_THREADS=1 VECLIB_MAXIMUM_THREADS=1 OMP_NUM_THREADS=1 \
    "$SCRATCH/bench-base" all >"$SCRATCH/base.ndjson" 2>/dev/null
base_summary=$(python3 - "$SCRATCH/base.ndjson" <<'PY'
import json, sys, collections
c = collections.Counter(); routines = set()
for line in open(sys.argv[1]):
    line = line.strip()
    if not line.startswith('{'): continue
    r = json.loads(line)
    if 'verified' not in r: continue
    routines.add(r['routine'])
    c[r['verified']] += 1
print(f"{c[True]} true, {c[False]} false, {c.get(None,0)} null, over {len(routines)} routines")
sys.exit(1 if (c[False] or c.get(None,0) or len(routines) < 9) else 0)
PY
)
brc=$?
printf '  %s\n' "$base_summary"
if [ "$brc" -ne 0 ]; then
    printf 'FAIL baseline: every routine must report verified:true against a real BLAS,\n' >&2
    printf '      and all nine must be present. A null means a routine still has no check.\n' >&2
    RC=1
fi

# ---- drive the mutations -------------------------------------------------
while IFS='|' read -r name routine anchor expect stmt; do
    [ -z "${name:-}" ] && continue
    NRUN=$((NRUN + 1))
    src="$SCRATCH/mut-$name.c"

    case "$stmt" in
      '/*MUT_DOT*/')
        # ddot's result is the value under test, so it is perturbed in the call
        # expression rather than in an operand.
        sed 's|verify_dot_result(x, y, n, incx, ddot_(&n,x,\&incx,y,\&incx))|verify_dot_result(x, y, n, incx, ddot_(\&n,x,\&incx,y,\&incx)*(1.0+1e-6))|' \
            "$BASE" >"$src"
        if ! grep -q '1.0+1e-6' "$src"; then
            printf 'FAIL %s: ddot anchor not found\n' "$name" >&2; RC=1; continue
        fi
        ;;
      /\*MUT_TOL\*/*)
        # Revert the tolerance to the m-scaled form the check does NOT use, then
        # apply the corruption. Proves the mm-not-m choice is load-bearing.
        body=${stmt#/\*MUT_TOL\*/}
        awk -v anchor="$anchor" -v stmt="$body" '
          /verify_trsm_corner\(const double \*A/ { intrsm=1 }
          intrsm && /double tol = 8\.0 \* \(double\)mm \* DBL_EPSILON;/ {
              sub(/\(double\)mm/, "(double)m"); intrsm=0 }
          index($0, "int ok = " anchor) { print "    " stmt }
          { print }
        ' "$BASE" >"$src"
        grep -q 'double tol = 8.0 \* (double)m \* DBL_EPSILON;' "$src" || {
            printf 'FAIL %s: tolerance revert did not apply\n' "$name" >&2; RC=1; continue; }
        ;;
      *)
        awk -v anchor="$anchor" -v stmt="$stmt" '
          index($0, "ok = " anchor) { print "    " stmt }
          { print }
        ' "$BASE" >"$src"
        if [ "$(grep -c -F "$stmt" "$src")" -eq 0 ]; then
            printf 'FAIL %s: anchor "%s" not found in bench.c\n' "$name" "$anchor" >&2; RC=1; continue
        fi
        ;;
    esac

    bin="$SCRATCH/bench-$name"
    # shellcheck disable=SC2086
    if ! $CC $CFLAGS_T "$src" -o "$bin" -lm -lpthread $BLASLIBS 2>"$SCRATCH/$name.cc.log"; then
        printf 'FAIL %-34s compile failed\n' "$name" >&2
        sed -n '1,10p' "$SCRATCH/$name.cc.log" >&2; RC=1; continue
    fi
    OPENBLAS_NUM_THREADS=1 VECLIB_MAXIMUM_THREADS=1 OMP_NUM_THREADS=1 \
        "$bin" "$routine" >"$SCRATCH/$name.ndjson" 2>/dev/null

    out=$(python3 - "$SCRATCH/$name.ndjson" "$routine" "$expect" <<'PY'
import json, sys, collections
path, routine, expect = sys.argv[1], sys.argv[2], sys.argv[3]
tot = collections.Counter(); fail_m = []
for line in open(path):
    line = line.strip()
    if not line.startswith('{'): continue
    r = json.loads(line)
    if r.get('routine') != routine or 'verified' not in r: continue
    tot[r['verified']] += 1
    if r['verified'] is False: fail_m.append(r.get('m', 0))
n_false, n_true = tot[False], tot[True]
if n_true + n_false == 0:
    print(f"no {routine} records at all"); sys.exit(1)
detail = f"{n_true} true, {n_false} false"
if expect == 'fires':
    print(detail + ("" if n_false else "  <-- check did not notice the corruption"))
    sys.exit(0 if n_false else 1)
if expect == 'silent':
    print(detail + ("" if not n_false else "  <-- false positive"))
    sys.exit(0 if not n_false else 1)
if expect == 'blind_large':
    large = [m for m in fail_m if m >= 2048]
    small = [m for m in fail_m if m < 2048]
    print(detail + f"; failing m: {len(small)} small/medium, {len(large)} at m>=2048")
    # Blind at large m (that is the point) but still awake at small m, which
    # proves the corruption really is present and the silence is the tolerance.
    sys.exit(0 if (not large and small) else 1)
print(f"unknown expectation kind {expect!r}"); sys.exit(1)
PY
)
    mrc=$?
    if [ "$mrc" -eq 0 ]; then
        printf 'ok   %-34s %-8s %s\n' "$name" "$expect" "$out"
    else
        printf 'FAIL %-34s %-8s %s\n' "$name" "$expect" "$out"; RC=1
    fi
done <<< "$MUTATIONS"

printf '\n%d mutations run\n' "$NRUN"
if [ "$RC" -eq 0 ]; then
    printf 'PASS: every corner check catches the wrong answer it is responsible for.\n'
else
    printf 'FAIL: see above.\n'
fi
exit "$RC"
