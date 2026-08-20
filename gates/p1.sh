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

# The same check for a table of strings. The pad axis is two integer tables and
# one list of routine names, and the names are the half that decides which
# routines get pads at all -- a drift there would leave the fixture planting
# padded cells for a routine bench.c never pads, or vice versa, and either way
# section 3's coverage arithmetic (see reference-arm-partial's 24) is measuring a
# design that was not run.
strings_check() {
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
print(",".join(sorted(re.findall(r'"([^"]*)"', m.group(1)))))
EOF
  )
  from_py=$(
    "$PY" - "$pyvar" <<'EOF'
import sys, importlib.util, pathlib
spec = importlib.util.spec_from_file_location("synth", pathlib.Path("tools/synth.py"))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
print(",".join(sorted(str(v) for v in getattr(mod, sys.argv[1]))))
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
ladder_check "lda pads (small+medium)" "LDA_PADS_EXTRA" "LDA_PADS_EXTRA"
ladder_check "lda pads (large)" "LDA_PADS_EXTRA_LARGE" "LDA_PADS_EXTRA_LARGE"
strings_check "padded routines" "PADDED_ROUTINES" "PADDED_ROUTINES"

# pad 0 must not appear in either extra-pad table. The base sweep already emits
# every routine at pad 0, so a 0 here would emit a second record for the same
# condition in the same run -- which min-within-run would then silently resolve,
# turning a duplicated case into a quietly different sample. Asserted on both
# sides because the hazard is in bench.c and the fixture copies it.
if pad0=$("$PY" - <<'EOF'
import importlib.util, pathlib, re, sys
spec = importlib.util.spec_from_file_location("synth", pathlib.Path("tools/synth.py"))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
src = pathlib.Path("src/bench.c").read_text()
bad = []
for var in ("LDA_PADS_EXTRA", "LDA_PADS_EXTRA_LARGE"):
    m = re.search(r"\b" + var + r"\b\s*\[[^\]]*\]\s*=\s*\{([^}]*)\}", src)
    if m and 0 in [int(x, 0) for x in re.findall(r"0x[0-9a-fA-F]+|\d+", m.group(1))]:
        bad.append(f"src/bench.c:{var}")
    if 0 in [int(x) for x in getattr(mod, var)]:
        bad.append(f"tools/synth.py:{var}")
print("; ".join(bad) if bad else "no extra-pad table contains 0")
sys.exit(1 if bad else 0)
EOF
); then
  ok "pad 0 is absent from the extra-pad tables: $pad0"
else
  bad "pad 0 appears in an extra-pad table ($pad0) — the base sweep emits that condition too"
fi

# The timing floor is a third hand-copy of a bench.c constant, and it decides
# comparisons rather than just describing them: decompose.py keys every cell by
# the floor it was measured under, and a record with no min_seconds field is
# keyed as LEGACY_MIN_SECONDS. If that default stops matching the floor bench.c
# actually uses for medium and large, a fieldless record is silently keyed APART
# from its own siblings and quietly stops comparing -- the one failure mode a
# default is supposed to prevent. Also asserted: the small floor is genuinely
# different, because if it were not, the per-regime floor and the whole n=192..384
# overlap band would be measuring nothing.
if floors=$("$PY" - <<'EOF'
import importlib.util, pathlib, re, sys

src = pathlib.Path("src/bench.c").read_text()


def define(name):
    m = re.search(r"^\s*#define\s+" + re.escape(name) + r"\s+([0-9.eE+-]+)", src, re.M)
    return float(m.group(1)) if m else None


spec = importlib.util.spec_from_file_location("dc", pathlib.Path("analysis/decompose.py"))
dc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dc)

sspec = importlib.util.spec_from_file_location("synth", pathlib.Path("tools/synth.py"))
synth = importlib.util.module_from_spec(sspec)
sspec.loader.exec_module(synth)

bad = []
c_default, c_small = define("MIN_SECONDS"), define("MIN_SECONDS_SMALL")
if c_default is None or c_small is None:
    bad.append("could not read MIN_SECONDS / MIN_SECONDS_SMALL out of src/bench.c")
else:
    if dc.LEGACY_MIN_SECONDS != c_default:
        bad.append(f"LEGACY_MIN_SECONDS={dc.LEGACY_MIN_SECONDS} but bench.c MIN_SECONDS={c_default}")
    if (synth.MIN_SECONDS, synth.MIN_SECONDS_SMALL) != (c_default, c_small):
        bad.append(
            f"synth.py has ({synth.MIN_SECONDS}, {synth.MIN_SECONDS_SMALL}) "
            f"but bench.c has ({c_default}, {c_small})"
        )

# The constants agreeing is not the same as the MAPPING agreeing, and the mapping
# lives in bench.c's sweep() call sites, not in a table. min_seconds_for() assumes
# SIZES_SMALL -> MIN_SECONDS_SMALL and the other two ladders -> MIN_SECONDS, so
# that assumption is read straight back off the call sites. A future sweep() call
# that floors SIZES_MEDIUM at the small floor would make every medium fixture
# record claim a floor bench.c never used.
calls = re.findall(
    r"sweep\(\s*[^,]+,\s*(SIZES_\w+)\s*,[^,]+,[^,]+,\s*(MIN_SECONDS\w*)\s*\)", src
)
seen = {}
for ladder, floor in calls:
    seen.setdefault(ladder, set()).add(floor)
want = {
    "SIZES_SMALL": {"MIN_SECONDS_SMALL"},
    "SIZES_MEDIUM": {"MIN_SECONDS"},
    "SIZES_LARGE": {"MIN_SECONDS"},
}
if not calls:
    bad.append("no sweep(ladder, ..., floor) call sites matched in src/bench.c")
elif seen != want:
    bad.append(f"bench.c's ladder->floor mapping is {seen}, min_seconds_for() assumes {want}")
# Level 1 does not go through sweep(); bench.c sets the floor by hand there.
if not re.search(r"g_min_seconds\s*=\s*MIN_SECONDS\s*;", src):
    bad.append("bench.c's level-1 block no longer sets g_min_seconds = MIN_SECONDS")
if c_default is not None:
    for m, want_floor in ((8, c_small), (256, c_small), (320, c_default), (1024, c_default)):
        if synth.min_seconds_for(m) != want_floor:
            bad.append(f"min_seconds_for({m})={synth.min_seconds_for(m)}, want {want_floor}")
    if c_small == c_default:
        bad.append(f"MIN_SECONDS_SMALL == MIN_SECONDS == {c_default}: the per-regime floor is a no-op")
    # A fieldless record and an explicit medium/large record must land on ONE key.
    if dc.canon_floor(None) != dc.canon_floor(c_default):
        bad.append("canon_floor(None) does not equal canon_floor(MIN_SECONDS)")
    # ...and the two floors must land on two.
    if dc.canon_floor(c_small) == dc.canon_floor(c_default):
        bad.append("canon_floor collapses the two floors into one key")
    # The key is the number as bench.c prints it (%.3f), not a float.
    if dc.canon_floor(0.05) != "0.050" or dc.canon_floor(0.0500000001) != "0.050":
        bad.append(f"canon_floor is not quantised to bench.c's %.3f: {dc.canon_floor(0.0500000001)!r}")

print("; ".join(bad) if bad else f"default={c_default} small={c_small}, legacy default matches, keys distinct")
sys.exit(1 if bad else 0)
EOF
); then
  ok "MIN_SECONDS floors agree with src/bench.c: $floors"
else
  bad "the timing-floor copy drifted — $floors"
fi

# ---- 3. the majority comparison is exact ----------------------------------
# A property of decompose.py, not of any dataset, and it has to be checked here
# because no fixture can reach it: the default --verdict-majority of 0.60 is one
# of the thresholds whose double rounds just UNDER, so a dataset sitting exactly
# on it clears a float comparison by luck. 0.34 and 0.55 round just over and
# would fail. The campaign's own --max-nodata-fraction is 0.34, so this is not a
# hypothetical threshold.
head_ "3. majority arithmetic is exact (no tolerance constant)"
if exact=$("$PY" - <<'EOF'
import importlib.util, pathlib, sys
from fractions import Fraction

spec = importlib.util.spec_from_file_location("dc", pathlib.Path("analysis/decompose.py"))
dc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dc)

bad = []

# 1. The threshold is read as the decimal that was written, not as a double.
for thr, want in ((0.60, Fraction(3, 5)), (0.55, Fraction(11, 20)), (0.34, Fraction(17, 50))):
    got = dc.as_exact(thr)
    if got != want:
        bad.append(f"as_exact({thr}) = {got}, want {want}")

# 2. Weight landing EXACTLY on the threshold clears it, at every threshold --
#    including the ones a float comparison gets wrong.
for thr in (0.34, 0.5, 0.55, 0.60, 0.7, 0.75, 0.9):
    e = Fraction(str(thr))
    if not dc.majority_met(e, Fraction(1), thr):
        bad.append(f"exact {e} does not clear a threshold of {thr}")
    # and just under must NOT clear it, or the check above is vacuous
    if dc.majority_met(e - Fraction(1, 1000), Fraction(1), thr):
        bad.append(f"{e} - 1/1000 wrongly clears a threshold of {thr}")

# 3. Summation order cannot change the answer. Five groups, three one-sided:
#    3/5 = exactly the default majority, assembled from reciprocals of unequal
#    group sizes so a float sum would depend on the order.
sizes = (24, 160, 7, 13, 96)
one = [Fraction(1, n) for n in sizes for _ in range(n)]
fwd = sum(one[: sizes[0] + sizes[1] + sizes[2]], Fraction(0))
rev = sum(reversed(one[: sizes[0] + sizes[1] + sizes[2]]), Fraction(0))
tot = sum(one, Fraction(0))
if fwd != rev or fwd != 3 or tot != 5:
    bad.append(f"reciprocal sums are order-dependent: fwd={fwd} rev={rev} total={tot}")
if not (dc.majority_met(fwd, tot, 0.60) and dc.majority_met(rev, tot, 0.60)):
    bad.append("3 of 5 balanced groups does not clear a 0.60 majority")

# 4. No epsilon left behind. The point of the swap is that the constant is gone,
#    not that it is unused: a reader who finds one will reasonably assume the
#    comparison still needs a tolerance.
src = pathlib.Path("analysis/decompose.py").read_text()
if "MAJORITY_EPS" in src:
    bad.append("MAJORITY_EPS is back in decompose.py")

print("; ".join(bad) if bad else "exact on the boundary, order-independent, no epsilon")
sys.exit(1 if bad else 0)
EOF
); then
  ok "majority comparisons are exact: $exact"
else
  bad "majority arithmetic is not exact — $exact"
fi

# ---- 4. every scenario ----------------------------------------------------
head_ "4. planted scenarios"
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

# ---- 5. the three CLAUDE.md clauses, asserted by name ---------------------
# The scenario loop above already covers these, but it covers them as one line
# among many. The gate table names them specifically, so they are restated here
# as named requirements: a green gate should be readable against the row that
# demanded it, not just against a pass count.
head_ "5. CLAUDE.md's P1 row, clause by clause"
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

# ---- 6. exit-code bits are distinct --------------------------------------
# The bits are the gate's only machine-readable channel. If two different
# failures set the same bit, no downstream gate can tell them apart.
head_ "6. exit bits are distinguishable"
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
