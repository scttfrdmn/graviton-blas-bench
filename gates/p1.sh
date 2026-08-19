#!/usr/bin/env bash
# Gate P1 — the analysis is calibrated. Exits 0 (green) or 1 (red) and prints
# its evidence.
#
# P1 answers a question P2 cannot: does `analysis/decompose.py` say the right
# thing about data whose right answer is already known? On campaign data the
# right answer is what the campaign is trying to find out, so the only place to
# establish this is on planted data. Nothing here touches AWS and nothing here
# costs anything; per CLAUDE.md's gate table it must be green before a single
# instance-hour is spent.
#
# CLAUDE.md's requirement, verbatim: "expected-arm census present and read from
# manifest-*.ndjson + census-*.ndjson; every planted effect recovered; the
# planted null reported as a null, not a weak hit, and distinguishable from a
# missing arm".
#
# Each scenario in tools/synth.py declares its own expectations. This script
# generates it, runs decompose.py over it, and hands the report, the stdout and
# the exit code back to `synth.py check`, which evaluates them. The gate itself
# holds no expectations, so adding a scenario needs no edit here.
#
# Fixtures go to a scratch directory, never to results/. They are not
# measurements (standing order 3) and must not be capable of reaching the
# published dataset.

set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# PID-suffixed for the same reason as tests/run-matrix-stubs.sh: two concurrent
# runs sharing one fixture tree fail each other, not the code.
WORK="${GBB_P1_WORK:-${TMPDIR:-/tmp}/gbb-p1.$$}"
KEEP="${GBB_P1_KEEP:-0}"
PY="${PYTHON:-python3}"

PASS=0
FAIL=0
SCEN_PASS=0
SCEN_FAIL=0
FAILED_SCENARIOS=()

ok()   { printf '  \033[32mPASS\033[0m  %s\n' "$*"; PASS=$((PASS+1)); }
bad()  { printf '  \033[31mFAIL\033[0m  %s\n' "$*"; FAIL=$((FAIL+1)); }
head_() { printf '\n%s\n%s\n' "$*" "$(printf '%.0s-' $(seq 1 ${#1}))"; }

printf '=== gate P1: the analysis is calibrated on planted data ===\n'
printf 'root:    %s\n' "$ROOT"
printf 'scratch: %s\n' "$WORK"

rm -rf "$WORK"
mkdir -p "$WORK" || { printf 'cannot create %s\n' "$WORK"; exit 1; }
if [ "$KEEP" != "1" ]; then
  # shellcheck disable=SC2064
  trap "rm -rf '$WORK'" EXIT
fi

# ---- 1. the tools are present and runnable --------------------------------
head_ "1. tooling"
for f in tools/synth.py analysis/decompose.py; do
  if [ -f "$f" ]; then ok "$f"; else bad "$f missing"; fi
done
if [ "$FAIL" -ne 0 ]; then
  printf '\n\033[31mGATE P1 RED\033[0m — tooling missing.\n'
  exit 1
fi

# ---- 2. the fixture's size ladders still match bench.c ---------------------
# synth.py copies bench.c's ladders because there is nothing to import from a C
# file. A copy that drifts makes every fixture a test of the wrong experiment,
# silently, so the copy is checked rather than trusted. Compared as sets of
# integers: bench.c's arrays and synth.py's tuples are formatted differently and
# only the contents are the contract.
head_ "2. fixture ladders match src/bench.c (standing order: same regimes on every host)"
ladder_check() {
  local name="$1" cvar="$2" pyvar="$3"
  local from_c from_py
  from_c=$(
    "$PY" - "$cvar" <<'EOF'
import re, sys, pathlib
var = sys.argv[1]
src = pathlib.Path("src/bench.c").read_text()
m = re.search(r"\b" + re.escape(var) + r"\b\s*\[[^\]]*\]\s*=\s*\{([^}]*)\}", src)
if not m:
    sys.exit("no match")
print(",".join(str(v) for v in sorted(int(x, 0) for x in re.findall(r"0x[0-9a-fA-F]+|\d+", m.group(1)))))
EOF
  )
  from_py=$(
    "$PY" - "$pyvar" <<'EOF'
import sys, importlib.util, pathlib
spec = importlib.util.spec_from_file_location("synth", pathlib.Path("tools/synth.py"))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
print(",".join(str(v) for v in sorted(int(x) for x in getattr(mod, sys.argv[1]))))
EOF
  )
  if [ -z "$from_c" ]; then
    bad "$name: could not read $cvar out of src/bench.c"
  elif [ "$from_c" = "$from_py" ]; then
    ok "$name: $from_py"
  else
    bad "$name drifted — bench.c has [$from_c], synth.py has [$from_py]"
  fi
}
ladder_check "small sizes"  "SIZES_SMALL"  "SIZES_SMALL"
ladder_check "medium sizes" "SIZES_MEDIUM" "SIZES_MEDIUM"
ladder_check "large sizes"  "SIZES_LARGE"  "SIZES_LARGE"
ladder_check "level-1 lengths" "lens" "LEVEL1_LENS"

# ---- 3. every scenario ----------------------------------------------------
head_ "3. planted scenarios"
SCENARIOS=$("$PY" tools/synth.py list | grep -v '^ ' | grep -v '^$')
if [ -z "$SCENARIOS" ]; then
  bad "tools/synth.py list produced no scenarios"
else
  ok "$(printf '%s\n' "$SCENARIOS" | wc -l | tr -d ' ') scenarios declared"
fi

for s in $SCENARIOS; do
  d="$WORK/$s"
  printf '\n'
  if ! "$PY" tools/synth.py generate "$s" "$d" >"$d.gen.log" 2>&1; then
    bad "$s: generate failed"
    sed -n '1,20p' "$d.gen.log"
    SCEN_FAIL=$((SCEN_FAIL+1)); FAILED_SCENARIOS+=("$s")
    continue
  fi

  # decompose.py's exit code is load-bearing and non-zero is the expected
  # outcome for most scenarios, so it is captured, never allowed to abort the
  # gate. `set -e` is deliberately not in force in this file for that reason.
  "$PY" analysis/decompose.py "$d/results" --json "$d/report.json" \
    >"$d/stdout.txt" 2>"$d/stderr.txt"
  rc=$?

  if [ ! -s "$d/report.json" ]; then
    bad "$s: decompose.py wrote no --json report (exit $rc)"
    sed -n '1,20p' "$d/stderr.txt"
    SCEN_FAIL=$((SCEN_FAIL+1)); FAILED_SCENARIOS+=("$s")
    continue
  fi

  if "$PY" tools/synth.py check "$d" "$d/report.json" "$d/stdout.txt" "$rc"; then
    SCEN_PASS=$((SCEN_PASS+1))
  else
    SCEN_FAIL=$((SCEN_FAIL+1)); FAILED_SCENARIOS+=("$s")
  fi
done

# ---- 4. the three CLAUDE.md clauses, asserted by name ---------------------
# The scenario loop above already covers these, but it covers them as one line
# among many. The gate table names them specifically, so they are restated here
# as named requirements: a green gate should be readable against the row that
# demanded it, not just against a pass count.
head_ "4. CLAUDE.md's P1 row, clause by clause"
clause() {
  local label="$1" scen="$2"
  if [ ! -f "$WORK/$scen/report.json" ]; then
    bad "$label — scenario '$scen' did not produce a report"
    return
  fi
  if printf '%s\n' "${FAILED_SCENARIOS[@]:-}" | grep -qx "$scen"; then
    bad "$label — scenario '$scen' failed its own expectations"
  else
    ok "$label (scenario '$scen')"
  fi
}
clause "the expected-arm census is read from manifest-* and census-*" "missing-arm-explained"
clause "every planted effect is recovered" "v1-ahead-broad"
clause "an effect confined to one regime is located, not just detected" "v1-ahead-small"
clause "the planted null is reported as a null" "null"
clause "a sub-threshold difference is NOT reported as a weak hit" "noise-only"
clause "a null is distinguishable from a missing arm" "missing-arm-unexplained"
clause "the opposite result is reachable — V2 ahead is not unrepresentable" "v2-ahead"

# The census clause deserves a direct check as well: a scenario can pass its
# expectations while decompose.py ignores the census entirely, if the
# expectations only ever look at cells.
if [ -f "$WORK/missing-arm-explained/report.json" ]; then
  if "$PY" - "$WORK/missing-arm-explained/report.json" <<'EOF'
import json, sys
rep = json.load(open(sys.argv[1]))
files = rep["inputs"]["files"]
have = [k for k in files if "census" in k or "manifest" in k]
statuses = {s for a in rep["coverage"]["by_arm"] for s in a if s != "instance" and s != "arm"}
# The reason string can only have come from the census record.
reasons = " ".join(str(c.get("reason") or "") for c in rep["coverage"]["cells"])
ok = bool(have) and "unrunnable" in statuses
print(f"file families read: {sorted(have)}; statuses seen: {sorted(statuses)}")
sys.exit(0 if ok else 1)
EOF
  then
    ok "census/manifest families are loaded AND a census-only status reaches the coverage table"
  else
    bad "the coverage table shows no status that could only have come from the census"
  fi
fi

# ---- 5. exit-code bits are distinct --------------------------------------
# The bits are the gate's only machine-readable channel. If two different
# failures set the same bit, no downstream gate can tell them apart.
head_ "5. exit bits are distinguishable"
bit_of() { "$PY" -c "import sys;print(int(sys.argv[1]) & int(sys.argv[2]))" "$1" "$2"; }
declare -a BITCASES=(
  "all-arms-failed:1:nothing loaded"
  "dead-arm:2:poisoned records"
  "missing-arm-unexplained:4:unexplained coverage hole"
  "no-provenance:8:provenance incomplete"
  "replicate-diverges:16:headline does not reproduce"
)
for case in "${BITCASES[@]}"; do
  scen="${case%%:*}"; rest="${case#*:}"; bit="${rest%%:*}"; what="${rest#*:}"
  f="$WORK/$scen/report.json"
  if [ ! -f "$f" ]; then bad "bit $bit ($what): scenario '$scen' produced no report"; continue; fi
  got=$("$PY" -c "import json,sys;print(json.load(open(sys.argv[1]))['exit_code'])" "$f")
  if [ "$(bit_of "$got" "$bit")" = "$bit" ]; then
    ok "bit $bit set by '$scen' ($what); exit=$got"
  else
    bad "bit $bit NOT set by '$scen' ($what); exit=$got"
  fi
done

# A clean scenario must return 0. Without this the bit checks above would pass
# on an implementation that simply always set every bit.
if [ -f "$WORK/null/report.json" ]; then
  got=$("$PY" -c "import json,sys;print(json.load(open(sys.argv[1]))['exit_code'])" "$WORK/null/report.json")
  if [ "$got" = "0" ]; then
    ok "the clean scenario 'null' returns exit 0 (the bits are not always-on)"
  else
    bad "the clean scenario 'null' returned exit $got, not 0"
  fi
fi

# ---- verdict --------------------------------------------------------------
printf '\n=== gate P1: %d scenarios passed, %d failed; %d checks passed, %d failed ===\n' \
  "$SCEN_PASS" "$SCEN_FAIL" "$PASS" "$FAIL"
if [ "$SCEN_FAIL" -ne 0 ]; then
  printf 'failed scenarios: %s\n' "${FAILED_SCENARIOS[*]}"
fi
if [ "$KEEP" = "1" ]; then
  printf 'fixtures kept under %s (GBB_P1_KEEP=1)\n' "$WORK"
fi
if [ "$FAIL" -eq 0 ] && [ "$SCEN_FAIL" -eq 0 ]; then
  printf '\033[32mGATE P1 GREEN\033[0m — the analysis recovers what was planted and reports the null as a null.\n'
  printf 'P2 may start once Scott confirms the spend.\n'
  exit 0
fi
printf '\033[31mGATE P1 RED\033[0m — do not spend instance-hours on a miscalibrated analysis.\n'
exit 1
