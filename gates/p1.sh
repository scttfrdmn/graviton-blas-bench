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

# Section 2 loads tools/synth.py and analysis/decompose.py with
# importlib.util.spec_from_file_location, which writes __pycache__ and validates it
# on (mtime, size). That is a stale-artefact hazard aimed straight at this gate: a
# checkout, `git stash pop`, or any restore that lands a same-size file whose mtime
# the cache still considers current makes every assertion in section 2 read the
# BYTECODE rather than the file, and the gate goes green on code that is not on disk.
# Found in exactly that way -- a restored src/bench.c kept reporting the mutated
# band. A green gate that measured the wrong artefact is the worst failure available
# here, so the cache is disabled and any existing one is removed rather than trusted.
export PYTHONDONTWRITEBYTECODE=1
rm -rf tools/__pycache__ analysis/__pycache__

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
ladder_check "overlap band sizes" "OVERLAP_SIZES" "OVERLAP_SIZES"
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

# The overlap band's numbers agreeing (ladder_check above) is not the same as the
# band still DOING anything. Three ways it can be silently neutered, none of which
# a value comparison catches:
#
#   - It stops straddling. If every band size fell in one regime, both members of
#     each pair would be measured at the same floor, every delta would be zero, and
#     AGREES would be reported having compared nothing with nothing. That is the
#     worst failure available here: the band's whole output is a confirmation, and a
#     vacuous confirmation is indistinguishable from a real one downstream.
#   - It stops alternating. bench.c's order alternation is the ONLY thing that
#     separates a floor effect from a first-vs-second drift; without it
#     ORDER-CONFOUNDED is unreachable and a drift reads as a floor bias.
#   - The probe tag drifts between the three files, and a probe record that
#     decompose.py does not recognise as one lands in the cross as a second
#     measurement of an existing condition.
if band=$("$PY" - <<'EOF'
import importlib.util, pathlib, re, sys

src = pathlib.Path("src/bench.c").read_text()
spec = importlib.util.spec_from_file_location("dc", pathlib.Path("analysis/decompose.py"))
dc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dc)
sspec = importlib.util.spec_from_file_location("synth", pathlib.Path("tools/synth.py"))
synth = importlib.util.module_from_spec(sspec)
sspec.loader.exec_module(synth)

bad = []
band = tuple(synth.OVERLAP_SIZES)
in_small = [m for m in band if m in synth.SIZES_SMALL]
in_medium = [m for m in band if m in synth.SIZES_MEDIUM]
stray = [m for m in band if m not in synth.SIZES_SMALL and m not in synth.SIZES_MEDIUM]
if not in_small or not in_medium:
    bad.append(
        f"the band does not straddle the floor step: {len(in_small)} sizes below, "
        f"{len(in_medium)} above. Both members of every pair would carry the same floor"
    )
if stray:
    bad.append(f"band sizes {stray} are on no ladder, so the matrix never measures them")
# Said the other way round, off the mapping rather than off the ladders: the band
# must contain at least one size at each floor.
if len({synth.min_seconds_for(m) for m in band}) != 2:
    bad.append(f"every band size maps to one floor: {sorted({synth.min_seconds_for(m) for m in band})}")

# The alternation, in both producers. Read out of the function bodies rather than
# the whole file so an unrelated `i % 2` elsewhere cannot satisfy it.
cbody = re.search(r"static void run_floor_overlap\(void\)\s*\{(.*?)\n\}", src, re.S)
if not cbody:
    bad.append("run_floor_overlap() not found in src/bench.c")
elif "i % 2 == 0" not in cbody.group(1):
    bad.append("src/bench.c's run_floor_overlap() no longer alternates which floor runs first")
pysrc = pathlib.Path("tools/synth.py").read_text()
pbody = re.search(r"\ndef floor_probe_records\(.*?\n(?=def )", pysrc, re.S)
if not pbody:
    bad.append("floor_probe_records() not found in tools/synth.py")
elif "i % 2 == 0" not in pbody.group(0):
    bad.append("tools/synth.py's floor_probe_records() no longer mirrors bench.c's alternation")

# The tag, across all three files. bench.c is the producer, so its literal wins.
# The format string is C source, so the quotes around the JSON key are escaped in
# the file text: the bytes on disk are \"probe\":\"%s\". Match those bytes.
if r'\"probe\":\"%s\"' not in src:
    bad.append("src/bench.c's emit() no longer prints a probe field")
# And that the argument is in the position the format string asks for. printf takes
# the two lists positionally, so a key added to one and not the other -- or added to
# both in different places -- shifts every field after it and silently relabels the
# record. That is standing order 10's failure mode reached through a typo.
stmt = re.search(r'printf\("\{\\"run_id.*?\);', src, re.S)
if not stmt:
    bad.append("src/bench.c's emit() no longer has the record printf")
else:
    fmt, _, argv = stmt.group(0).partition(r'\n",')
    for a, b, c, where in ((r'\"role\"', r'\"probe\"', r'\"threads\"', "format string"),
                           ("g_role", "g_probe", "g_threads", "argument list")):
        text = fmt if where == "format string" else argv
        if not (0 <= text.find(a) < text.find(b) < text.find(c)):
            bad.append(f"src/bench.c's emit() {where} no longer has probe between role and threads")
if 'g_probe = "floor-overlap"' not in src:
    bad.append('src/bench.c no longer tags the band records "floor-overlap"')
if dc.FLOOR_PROBE != "floor-overlap":
    bad.append(f"decompose.py FLOOR_PROBE={dc.FLOOR_PROBE!r}, bench.c writes 'floor-overlap'")
if 'static const char *g_probe = "none"' not in src:
    bad.append("src/bench.c's g_probe no longer defaults to \"none\"")
if dc.NO_PROBE != "none" or dc.canon_probe(None) != "none":
    bad.append(f"decompose.py NO_PROBE={dc.NO_PROBE!r} / canon_probe(None)={dc.canon_probe(None)!r}")

# And that the split actually splits. A one-line behavioural check, because every
# assertion above is about spelling.
m, p = dc.split_floor_probe(
    [{"routine": "dgemm"}, {"routine": "dgemm", "probe": "none"}, {"routine": "dgemm", "probe": "floor-overlap"}]
)
if len(m) != 2 or len(p) != 1:
    bad.append(f"split_floor_probe put {len(m)} in the matrix and {len(p)} in the probe, want 2 and 1")
# An unknown probe name must NOT reach the matrix. Fail-closed: an unrecognised
# probe is not a matrix record, and admitting it would put a second measurement of
# an existing condition into the cross.
m2, p2 = dc.split_floor_probe([{"routine": "dgemm", "probe": "some-future-probe"}])
if m2 or len(p2) != 1:
    bad.append("split_floor_probe admitted an unknown probe tag into the matrix")

print(
    "; ".join(bad)
    if bad
    else f"band {band} straddles ({len(in_small)} below / {len(in_medium)} above), alternates, tags agree"
)
sys.exit(1 if bad else 0)
EOF
); then
  ok "the overlap band still measures something: $band"
else
  bad "the overlap band is neutered — $band"
fi

# ---- 2c. the matrix stamp is computed from the matrix ----------------------
# `matrix_id` exists so a pre-expansion P2 pass can never be pooled with a
# post-expansion P3 pass. The whole guarantee rests on the id being derived from
# the case tables by a walk of those same tables, so the ways it can go quiet are
# all ways the derivation stops being a derivation:
#
#   - A case producer stops folding. sweep() and run_level1() are two separate
#     enumerations; if only one folds, the id is stable across a change to the
#     other and the refusal never fires on the pass that differs.
#   - The dry pass and the real pass disagree about the case set. A routine in a
#     sweep list that sweep() cannot dispatch is the reachable version of this,
#     and it is fatal on purpose: the id would claim cases nothing measured.
#   - The two fields drift out of printf position, which relabels every field
#     after them (standing order 10 through a typo).
#   - `unstamped` starts behaving as a wildcard, at which point a dataset whose
#     two halves are silent about their case sets is analysed as if they agreed.
if stamp=$("$PY" - <<'EOF'
import importlib.util, pathlib, re, sys

src = pathlib.Path("src/bench.c").read_text()
spec = importlib.util.spec_from_file_location("dc", pathlib.Path("analysis/decompose.py"))
dc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dc)
sspec = importlib.util.spec_from_file_location("synth", pathlib.Path("tools/synth.py"))
synth = importlib.util.module_from_spec(sspec)
sspec.loader.exec_module(synth)

bad = []

# -- the producer prints both fields, in position.
for key in (r'\"matrix_id\":\"%s\"', r'\"matrix_cases\":%ld'):
    if key not in src:
        bad.append(f"src/bench.c's emit() no longer prints {key}")
stmt = re.search(r'printf\("\{\\"run_id.*?\);', src, re.S)
if not stmt:
    bad.append("src/bench.c's emit() no longer has the record printf")
else:
    fmt, _, argv = stmt.group(0).partition(r'\n",')
    for a, b, c, where in ((r'\"probe\"', r'\"matrix_id\"', r'\"threads\"', "format string"),
                           ("g_probe", "g_matrix_id", "g_threads", "argument list")):
        text = fmt if where == "format string" else argv
        if not (0 <= text.find(a) < text.find(b) < text.find(c)):
            bad.append(
                f"src/bench.c's emit() {where} no longer has matrix_id between probe and threads"
            )

# -- both case producers fold. Read out of the function bodies, so a fold in one
# cannot satisfy the check for the other.
for fn in ("sweep", "run_level1"):
    body = re.search(r"static void " + fn + r"\(.*?\n\}", src, re.S)
    if not body:
        bad.append(f"{fn}() not found in src/bench.c")
    elif "fold_case(" not in body.group(0):
        bad.append(
            f"src/bench.c's {fn}() no longer folds its cases into the matrix id, so the id is "
            f"blind to every change in the cases it enumerates"
        )

# -- the dry pass runs first and does not measure, and the id is stamped between
# the two passes. `g_dry` gating the fold is what makes the id independent of the
# argv routine filter, which is the property that lets one arm's --routine run
# carry the same id as the full sweep.
if "g_dry = (pass == 0)" not in src:
    bad.append("src/bench.c no longer runs a dry pass before the measured one")
if not re.search(r"if \(!g_dry\).*?g_matrix_id.*?%016llx", src, re.S):
    bad.append("src/bench.c no longer formats g_matrix_id between the dry pass and the real one")
# -- and the driverless-routine refusal is still fatal. Found by mutation: without
# it the dry pass folded 31 cases the real pass skipped.
if "exit(5)" not in src:
    bad.append(
        "src/bench.c no longer aborts on a routine with no driver, so the matrix id can count "
        "cases nothing measures"
    )
rm = pathlib.Path("scripts/run-matrix.sh").read_text()
if "rc -eq 5" not in rm or "harness_invalid" not in rm:
    bad.append(
        "scripts/run-matrix.sh no longer censuses bench.c's rc=5 as harness_invalid, so a "
        "harness defect would be recorded with runtime_failed's SIGILL hint"
    )

# -- the consumer. Absence is one group, not a wildcard, and behaviourally so.
if dc.canon_matrix_id(None) != dc.UNSTAMPED_MATRIX or dc.canon_matrix_id("") != dc.UNSTAMPED_MATRIX:
    bad.append("decompose.py's canon_matrix_id no longer defaults absence to UNSTAMPED_MATRIX")
mixed = dc.matrix_ids([{"matrix_id": "abc"}, {}])
if len(mixed) != 2:
    bad.append(
        f"decompose.py groups a stamped and an unstamped record into {len(mixed)} matrix, so "
        f"`unstamped` is matching anything"
    )
same = dc.matrix_ids([{}, {"matrix_id": ""}, {"matrix_id": "  "}])
if len(same) != 1:
    bad.append(f"decompose.py splits three unstamped records into {len(same)} matrices")

# -- synth's namespace. A fixture id must not be able to pass for a measured one,
# in a report or in a bucket: bench.c writes 16 bare hex digits and synth prefixes.
# Checked against the snprintf and not against the whole file, because bench.c
# names the namespace in a comment explaining why it does not use it.
fmtline = re.search(r"snprintf\(g_matrix_id[^;]*;", src)
if not fmtline:
    bad.append("src/bench.c no longer formats g_matrix_id with snprintf")
elif '"%016llx"' not in fmtline.group(0):
    bad.append(f"src/bench.c's matrix id is no longer bare hex: {fmtline.group(0)}")
if not synth.MATRIX_NAMESPACE or (fmtline and synth.MATRIX_NAMESPACE in fmtline.group(0)):
    bad.append(
        f"synth's MATRIX_NAMESPACE {synth.MATRIX_NAMESPACE!r} is what src/bench.c emits, so a "
        f"synthetic matrix id is indistinguishable from a measured one"
    )

# -- and the PROPERTY the analysis depends on, checked by moving the case set
# rather than by reading the code: the id follows the cases, and nothing else.
sc = synth.SCENARIOS["null"]()
base_id, base_n = synth.matrix_stamp(sc)
if not base_id.startswith(synth.MATRIX_NAMESPACE):
    bad.append(f"synth.matrix_stamp produced an unnamespaced id {base_id!r}")
wider = synth.Scenario(
    name="probe", description="", hosts=sc.hosts, arms=sc.arms,
    routines=(*sc.routines, "dsyrk"), level1=sc.level1,
)
wide_id, wide_n = synth.matrix_stamp(wider)
if wide_id == base_id or wide_n <= base_n:
    bad.append(
        f"adding a routine left the matrix id at {base_id} ({base_n} -> {wide_n} cases): the id "
        f"is not derived from the case set"
    )
no_l1 = synth.Scenario(
    name="probe", description="", hosts=sc.hosts, arms=sc.arms,
    routines=sc.routines, level1=False,
)
l1_id, l1_n = synth.matrix_stamp(no_l1)
if l1_id == base_id or l1_n >= base_n:
    bad.append(
        f"dropping the level-1 ladder left the matrix id at {base_id}: the level-1 cases are "
        f"not folded, so the id is blind to half the sweep"
    )

print(
    "; ".join(bad)
    if bad
    else f"stamp derived from {base_n} cases, moves with the case set, namespaced {synth.MATRIX_NAMESPACE!r}"
)
sys.exit(1 if bad else 0)
EOF
); then
  ok "the matrix stamp is derived from the matrix: $stamp"
else
  bad "the matrix stamp does not follow the matrix — $stamp"
fi

# ---- 2d. per-case wall clock is recorded, and is a cost signal ------------
# `case_seconds` exists for one reason: it is the only measured input to the P3
# cost estimate, and CLAUDE.md's §"Wall-clock is anti-correlated with arm quality"
# says it must be read off the SLOWEST arm because a representative arm
# extrapolates low. That instruction is only actionable if the field actually
# varies with arm speed, so the ways it can go quiet are:
#
#   - It stops being read before the printf. fflush() can block on the pipe to
#     run-matrix.sh, so a value taken after the write charges the consumer's
#     backpressure to the case and the cost model starts tracking how fast S3 was
#     that day.
#   - The interval starts at process start rather than after calibration, putting a
#     fixed startup cost into a per-case number that gets multiplied by 1005.
#   - It drifts out of printf position, relabelling every field after it.
#   - synth models it as a constant. Then every arm costs the same in every fixture,
#     and an analysis that took the FIRST arm instead of the slowest one would pass
#     the gate -- the single mistake this instrumentation exists to prevent.
if wall=$("$PY" - <<'EOF'
import importlib.util, pathlib, re, sys
from collections import defaultdict

src = pathlib.Path("src/bench.c").read_text()
sspec = importlib.util.spec_from_file_location("synth", pathlib.Path("tools/synth.py"))
synth = importlib.util.module_from_spec(sspec)
sspec.loader.exec_module(synth)


def define(name):
    m = re.search(r"^\s*#define\s+" + re.escape(name) + r"\s+([0-9.eE+-]+)", src, re.M)
    return float(m.group(1)) if m else None


bad = []

# -- the producer prints it, between calls and min_seconds, in both lists.
stmt = re.search(r'printf\("\{\\"run_id.*?\);', src, re.S)
if not stmt:
    bad.append("src/bench.c's emit() no longer has the record printf")
else:
    fmt, _, argv = stmt.group(0).partition(r'\n",')
    for a, b, c, where in ((r'\"calls\":%ld', r'\"case_seconds\":%.6f', r'\"min_seconds\":%.3f', "format string"),
                           ("g_batch", "case_seconds,", "g_min_seconds", "argument list")):
        text = fmt if where == "format string" else argv
        if not (0 <= text.find(a) < text.find(b) < text.find(c)):
            bad.append(
                f"src/bench.c's emit() {where} no longer has case_seconds between calls and "
                f"min_seconds"
            )
    # -- and reads the clock BEFORE it, not after the fflush.
    read = src.find("g_last_emit = now_s;")
    if read < 0:
        bad.append("src/bench.c's emit() no longer closes the interval into g_last_emit")
    elif not 0 <= read < src.find(stmt.group(0)):
        bad.append(
            "src/bench.c reads the clock after the record printf, so a blocked fflush is "
            "charged to the case"
        )

# -- the interval starts after calibration, inside the same !g_dry block that
# stamps the matrix id. Scoped to that block rather than searched for globally: a
# g_last_emit = now() at the top of main() would satisfy a whole-file search and
# would put process startup into the first case.
block = re.search(r"if \(!g_dry\) \{(.*?)\n    \}", src, re.S)
if not block:
    bad.append("src/bench.c's main() no longer has the post-dry-pass !g_dry block")
elif "g_last_emit = now();" not in block.group(1):
    bad.append(
        "src/bench.c does not start the wall-clock accounting in the !g_dry block, so the "
        "first case_seconds includes process startup and timer calibration"
    )
# -- and the thread-pool priming call lives in the same block, for the same reason
# and one more: in the dry pass there is no BLAS work to prime for, and a priming
# DGEMM during --dry-run would spend a second of every arm's startup on nothing.
# Scoped to the block rather than searched globally: a prime_threads() anywhere in
# the file would satisfy a whole-file search while running in the wrong pass.
if block and "prime_threads();" not in block.group(1):
    bad.append(
        "src/bench.c does not call prime_threads() in the !g_dry block, so either the dry "
        "pass primes a pool it will not use or thread 2..N's buffer pool is allocated inside "
        "the first timed region"
    )
if "prime_threads" in src and src.find("prime_threads();") < src.find("static void prime_threads"):
    bad.append("prime_threads() is called before it is defined")

# -- synth's two model constants agree with the producer's #defines. They are what
# give the model its anti-correlation, so a drift makes the fixtures assert the
# wrong shape rather than nothing.
for name, have in (("MIN_SAMPLES", synth.MIN_SAMPLES), ("MAX_MEASURE_SECONDS", synth.MAX_MEASURE_SECONDS)):
    want = define(name)
    if want is None:
        bad.append(f"could not read {name} out of src/bench.c")
    elif float(have) != want:
        bad.append(f"synth.{name}={have} but bench.c {name}={want}")

# -- the model's shape, as three properties rather than as a formula. Floor-clamped
# at the cheap end, MIN_SAMPLES-clamped at the expensive end, capped at the top.
floor = synth.MIN_SECONDS
t_fast = floor / synth.MIN_SAMPLES / 100
cheap = synth.case_seconds_for(t_fast, floor, 0.0)
# The floor binds the MEASUREMENT; the warmup and calibration calls sit outside it
# and are the point of the policy, so the expected cheap cost is the floor plus
# exactly the calls the policy says this case makes. Written as the policy's own
# arithmetic rather than as a tolerance: a tolerance would also pass a model that
# had stopped charging for warmup at all, which is the thing being measured.
w_fast, reuse_fast = synth.timing_path_for(t_fast, floor)
want_cheap = round(floor + w_fast * t_fast + (0.0 if reuse_fast else t_fast), 6)
if abs(cheap - want_cheap) > 1e-9:
    bad.append(
        f"a fast case costs {cheap}, not the {want_cheap} the floor plus {w_fast} warmup "
        f"call(s) predicts: the floor no longer binds"
    )
slow = synth.case_seconds_for(floor, floor, 0.0)
if slow <= cheap:
    bad.append(
        f"an arm {synth.MIN_SAMPLES * 100}x slower on the same case costs {slow} against {cheap}: "
        f"the model has no anti-correlation and no fixture can distinguish the slowest arm"
    )
capped = synth.case_seconds_for(1e6, floor, 0.0)
if capped > synth.MAX_MEASURE_SECONDS + 1e-9:
    bad.append(f"case_seconds_for is unbounded: {capped} > MAX_MEASURE_SECONDS")

# -- and the property in the fixtures themselves, which is what an analysis reads.
# Within one condition the arms differ only in speed, so case_seconds must be
# monotone non-increasing in gflops, and must do so STRICTLY somewhere: all-equal
# would satisfy monotonicity while carrying no signal at all.
sc = synth.SCENARIOS["null"]()
stream = synth.bench_records(sc, sc.hosts[0])
# The stream is not homogeneous any more -- thread_prime and case_skipped records
# carry case_seconds but no gflops -- so the monotonicity check reads measurements
# only. Filtered through synth.measurements() rather than by an inline predicate so
# that this check and the producer cannot disagree about what a measurement is.
recs = synth.measurements(stream)
missing = [r for r in recs if "case_seconds" not in r]
if any("case_seconds" not in r for r in stream):
    bad.append("a non-measurement record carries no case_seconds, so the stream no longer sums")
if missing:
    bad.append(f"{len(missing)} of {len(recs)} synth bench records carry no case_seconds")
conds = defaultdict(list)
# Guarded, so a dropped field reports itself rather than dying in a KeyError below:
# the traceback goes to stderr, the check's own message comes back empty, and the
# gate FAILs with nothing to read. Found by mutation.
for r in recs if not missing else ():
    conds[(r["threads"], r["routine"], r["m"], r["n"], r["k"], r["lda_pad"], r["incx"])].append(
        (r["gflops"], r["case_seconds"], r["t_min"], r["min_seconds"])
    )
strict = inverted = straddle = 0
for arms in conds.values():
    ordered = sorted(arms)
    for lo, hi in zip(ordered, ordered[1:]):
        if hi[1] > lo[1] + 1e-9:
            # The faster arm costs more, which the anti-correlation forbids -- EXCEPT
            # across the warmup boundary, where the faster arm still warms up and
            # reruns its calibration call while the slower one has stopped doing
            # both. That inversion is real in bench.c too, and it is bounded by the
            # three calls the policy charges, whereas the anti-correlation at the
            # expensive end is unbounded. So a straddling pair is allowed only if it
            # stays inside its own overhead; anything else is the model losing the
            # property the cost plan turns on.
            # The bound is TIGHT where both arms sit on the floor: the measurement
            # halves are then equal and the whole excess is the overhead, so excess
            # == span to the last digit. 1e-6 rather than 1e-9 because case_seconds
            # is rounded to bench.c's %.6f field on both sides of the subtraction,
            # and the bound is computed from an unrounded t_min.
            span = (synth.WARMUP_REPS + 1) * hi[2]
            paths_differ = synth.timing_path_for(lo[2], lo[3]) != synth.timing_path_for(hi[2], hi[3])
            if paths_differ and hi[1] - lo[1] <= span + 1e-6:
                straddle += 1
            else:
                inverted += 1
        elif lo[1] > hi[1] + 1e-9:
            strict += 1
if inverted:
    bad.append(
        f"{inverted} arm pairs where the FASTER arm's case costs more wall clock and not by "
        f"warmup: the fixtures model wall-clock as correlated with arm quality, not "
        f"anti-correlated"
    )
if not strict and not missing:
    bad.append(
        "no arm pair in the null scenario differs in case_seconds, so every fixture costs the "
        "same on every arm and a cost analysis that picked the first arm would pass"
    )

# -- and on the probe records, where it must be keyed off the floor the pair member
# actually ran under rather than off the size's regime default. Both halves of a
# pair are the SAME size, so a model that read min_seconds_for(m) would report them
# costing the same -- and the cheaper floor being cheaper is the only reason the
# per-regime floor was adopted. Nothing else in the fixtures would notice.
fp = synth.SCENARIOS["floor-band-agrees"]()
probe = synth.floor_probe_records(fp, fp.hosts[0])
if not probe:
    bad.append("the floor-band scenario produced no probe records")
elif any("case_seconds" not in r for r in probe):
    bad.append("floor-overlap probe records carry no case_seconds, so the band's cost is unaccounted")
else:
    by_floor = defaultdict(set)
    for r in probe:
        by_floor[r["m"]].add((round(r["min_seconds"], 3), r["case_seconds"]))
    flat = [m for m, pairs in by_floor.items() if len({cs for _, cs in pairs}) < 2]
    if len(by_floor) < 2 or flat:
        bad.append(
            f"{len(flat)} of {len(by_floor)} band sizes cost the same under both floors, so "
            f"case_seconds is keyed off the size's regime rather than off the floor it ran under"
        )

# -- the conditional-warmup and calibration-reuse policy, against bench.c's own
# constants. These two fields are discrete and are asserted for exact values by
# gates/p2.sh, so a fixture whose boundary sits in a different place from the
# producer's would certify a P2 gate that checks the fixture's opinion.
for name, want in (
    ("WARMUP_REPS", define("WARMUP_REPS")),
    ("WARMUP_MAX_FRACTION", define("WARMUP_MAX_FRACTION")),
    ("ABS_MIN_SAMPLES", define("ABS_MIN_SAMPLES")),
    ("MIN_BATCH_SECONDS", define("MIN_BATCH_SECONDS")),
):
    have = getattr(synth, name, None)
    if want is None:
        bad.append(f"bench.c has no #define {name}")
    elif have is None:
        bad.append(f"synth has no {name}")
    elif float(have) != float(want):
        bad.append(f"synth.{name}={have} but bench.c {name}={want}")

# The policy's two ends, as behaviour rather than as constants: a cheap case warms
# up and cannot reuse its calibration call (it was batched); an expensive one does
# neither. If both ends agreed, the fixtures would carry one path and every P2
# assertion about the expensive end would be vacuous.
w_cheap, r_cheap = synth.timing_path_for(1e-6, synth.MIN_SECONDS_SMALL)
w_slow, r_slow = synth.timing_path_for(0.5, synth.MIN_SECONDS)
if (w_cheap, r_cheap) != (synth.WARMUP_REPS, False):
    bad.append(f"a 1 us case gives warmup={w_cheap} reuse={r_cheap}, want ({synth.WARMUP_REPS}, False)")
if (w_slow, r_slow) != (0, True):
    bad.append(f"a 0.5 s case gives warmup={w_slow} reuse={r_slow}, want (0, True)")
# And the boundary is where the policy says it is: warmup stops when it would cost
# more than WARMUP_MAX_FRACTION of the measurement it precedes. Checked as a
# crossing rather than at a size, because the crossing moves with thread count and
# any size written here would be a fourth place to keep in step.
cross = [t for t in (1e-5, 1e-4, 1e-3, 1e-2, 1e-1, 1.0) if synth.timing_path_for(t, synth.MIN_SECONDS)[0] == 0]
if not cross or cross != sorted(cross) or len(cross) == 6:
    bad.append(f"warmup does not decay to zero exactly once across 1e-5..1 s: zero at {cross}")

# -- the thread-dependent large cap, matched to bench.c's large_cap_for_threads()
# arm by arm. bench.c's is a one-line function, so this reads its two constants.
c_low = define("LARGE_CAP_LOW")
c_thr = define("LARGE_CAP_MIN_THREADS")
if c_low is None or c_thr is None:
    bad.append("bench.c has no LARGE_CAP_LOW/LARGE_CAP_MIN_THREADS to check against")
else:
    for t in (1, 2, 4, 8, 16, 64, 192):
        want = int(c_low) if t < int(c_thr) else max(synth.SIZES_LARGE)
        if synth.large_cap_for_threads(t) != want:
            bad.append(f"large_cap_for_threads({t})={synth.large_cap_for_threads(t)}, bench.c says {want}")

# -- and the two properties that make the cap safe rather than merely cheap: every
# omitted case carries a REASON, and the fold is not capped, so a truncated stream
# and an untruncated one still stamp the same matrix. The second is the one that
# would silently unpool P2 from P3.
skipped = [r for r in stream if r.get("record") == "case_skipped"]
primes = [r for r in stream if r.get("record") == "thread_prime"]
low = [t for t in sc.hosts[0].threads if t < synth.LARGE_CAP_MIN_THREADS]
if low and not skipped:
    bad.append(f"threads={low} are below the cap but no case_skipped record was written")
if any(not r.get("reason") for r in skipped):
    bad.append("a case_skipped record carries no reason: standing order 11 at case granularity")
if any(r["m"] <= synth.large_cap_for_threads(r["threads"]) for r in skipped):
    bad.append("a case_skipped record is for a size at or below the cap")
# bench.c's cap lives inside sweep(), and the level-1 cases are built in main()
# without going through it. A fixture that capped them would delete the non-unit
# stride axis at every low thread count and call it policy.
if any(r["routine"] in ("daxpy", "ddot") for r in skipped):
    bad.append("a level-1 case was capped, but bench.c's cap cannot reach level-1")
if any(r["m"] not in synth.SIZES_LARGE for r in skipped):
    bad.append("a case_skipped record is for a size that is not on the large ladder")
if len(primes) != len({(r["library"], r["target"], r["coretype"], r["threads"]) for r in recs}):
    bad.append(f"{len(primes)} thread_prime records for {len(sc.hosts[0].threads)} thread points per arm")
if synth.matrix_fields(sc, sc.hosts[0]).get("matrix_cases") != len(
    synth.conditions(sc.routines, sc.level1, sc.transposes)
):
    bad.append("the matrix stamp counts something other than the full uncapped design")

print(
    "; ".join(bad)
    if bad
    else f"recorded in position, clock read pre-fflush, {strict} arm pairs ordered by speed, "
         f"0 inverted, {straddle} straddling the warmup boundary within their own overhead, "
         f"both probe floors priced apart, warmup decays at "
         f"{synth.WARMUP_MAX_FRACTION:g} of the measurement, {len(skipped)} capped cases all "
         f"with reasons, {len(primes)} streams primed, stamp over the uncapped design"
)
sys.exit(1 if bad else 0)
EOF
); then
  ok "per-case wall clock is recorded and tracks arm slowness: $wall"
else
  bad "case_seconds is not a usable cost signal — $wall"
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
  "floor-band-disagrees:32:timing-floor band unconfirmed"
  "matrix-mixed:64:more than one case matrix"
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

# Bits 1 and 64 are returned ALONE, and that is a property of the code rather than
# of a dataset: both mean "nothing downstream was computed", so a companion bit
# would be a claim about an analysis that never ran. 64 is the one worth asserting
# here — a mixed directory is otherwise a perfectly analysable dataset, so a version
# that refused and then went on to aggregate would look right in every other way.
for scen in matrix-mixed matrix-mixed-unstamped; do
  f="$WORK/$scen/report.json"
  if [ ! -f "$f" ]; then bad "bit 64 exclusivity: scenario '$scen' produced no report"; continue; fi
  got=$("$PY" -c "import json,sys;print(json.load(open(sys.argv[1]))['exit_code'])" "$f")
  if [ "$got" = "64" ]; then
    ok "bit 64 is returned alone by '$scen' (exit=$got, no companion bits)"
  else
    bad "'$scen' returned exit $got — 64 must be exclusive, nothing else was computed"
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
  # Specifically that bit 32 is not set by a dataset with NO probe records. ABSENT
  # and unconfirmed are different claims: every result set produced before the probe
  # existed is ABSENT, and decompose.py must still be able to read its own history.
  # Requiring the probe to be PRESENT is gates/p2.sh's job, not this one's.
  st=$("$PY" -c "import json,sys;print(json.load(open(sys.argv[1]))['floor_overlap']['status'])" \
       "$WORK/null/report.json")
  if [ "$st" = "ABSENT" ] && [ "$(bit_of "$got" 32)" = "0" ]; then
    ok "a dataset with no probe records is ABSENT and does NOT set bit 32"
  else
    bad "no-probe dataset reported '$st' with exit $got — ABSENT must not set bit 32"
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
