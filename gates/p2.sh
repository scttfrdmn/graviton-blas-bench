#!/usr/bin/env bash
# Gate P2 — one host's dataset is complete, self-consistent, and admissible.
# Exits 0 (green) or 1 (red) and prints its evidence.
#
#   gates/p2.sh [results-dir]     judge a real dataset (default: results/)
#   gates/p2.sh --self-test       judge the gate itself, against fixtures
#
# CLAUDE.md's P2 row, verbatim: "complete NDJSON set from one c8g.metal-48xl spot
# host; decompose.py clean bar genuine findings; topology-*.txt recorded; every arm
# in census-*.ndjson either measured or carrying a stated reason — zero
# MISSING-UNEXPLAINED".
#
# ONE DISCREPANCY IN THAT ROW, FLAGGED RATHER THAN SILENTLY RESOLVED. It says
# "spot host", and the spend policy in the same file reverses that: "Everything runs
# on-demand, including P2." The gate cannot tell spot from on-demand — nothing in
# env-*.json records the lifecycle, and IMDS's spot fields are absent on on-demand
# rather than contradictory — so this gate asserts nothing about tenancy either way.
# The row's wording is stale, not a requirement this gate declines to check. Fix the
# row or add a tenancy field; do not read this silence as approval of spot.
#
# Four requirements are here that the row does not name, all of them consequences of
# decisions taken after it was written:
#
#   - EXACTLY ONE STAMPED matrix_id, and the case count actually measured must equal
#     `matrix_cases`. decompose.py's census is derived from the data, so it can see
#     that one arm has fewer cells than another but not that EVERY arm is short: the
#     stamp is the campaign's only absolute coverage expectation, and this is the
#     only gate positioned to check it. P2 runs pre-expansion on purpose so that its
#     data can never pool with P3's, which makes the stamp being present a P2
#     deliverable and not a nicety.
#   - THE GENERIC ARMV8 ARM AT 1 THREAD, with records. Named in the re-sequencing
#     decision as a condition on running P2 before expansion items 3-5: it is the
#     campaign's most expensive single arm (wall-clock is anti-correlated with arm
#     quality, so the slowest arm at the fewest threads is the worst case) and the
#     P3 cost extrapolation is anchored on it. `run-matrix.sh` runs it by
#     construction — CORETYPES always starts "ARMV8 NEOVERSEN1" and the ladder
#     always starts at 1 — so requiring it here is cheap insurance against a
#     GBB_CORETYPES or GBB_THREADS_LADDER override that quietly dropped it. Note
#     that this arm is a FORCED coretype and has nothing to do with standing order
#     8, which is about what DYNAMIC_ARCH *selected*; the two are checked
#     separately and the second one is still an escalation.
#   - THE FLOOR-OVERLAP BAND PRESENT AND CONFIRMING. `ABSENT` deliberately does not
#     set exit bit 32, because every dataset collected before the probe existed is
#     ABSENT; requiring the probe to be present is this gate's job, as README says.
#     Without it section 4 cannot be read across n=256, which is where the effect
#     is expected to be.
#   - THE WALL-CLOCK ACCOUNTING, printed. This is P2's other deliverable and the
#     reason it runs before the expansion: the P3 budget rests on a multiplier that
#     is currently a guess, only real hardware produces the number, and CLAUDE.md
#     says to read it off the SLOWEST arm rather than a representative one. The
#     gate therefore names the slowest arm and prints its per-regime split, so the
#     figure that goes into the umbrella issue is one this gate produced.
#
# --self-test exists because this gate is otherwise unexercised code sitting between
# the spend and the verdict. It generates the `p2-host` fixture, requires the gate to
# pass it, and then requires the gate to FAIL each of a series of mutants — one per
# requirement above. A self-test that only proved the gate can go green would prove
# nothing: `exit 0` passes that test.
#
# Fixtures are quarantined by construction, not by discipline. bench.c stamps a bare
# 16-hex-digit matrix_id and tools/synth.py prefixes "synth-", so this gate REFUSES a
# synth-namespaced id in real mode and REQUIRES one in self-test mode. A fixture
# cannot pass gate P2 and a measurement cannot pass its self-test.

set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT" || { printf 'FATAL: cannot cd to %s\n' "$ROOT" >&2; exit 1; }

PY="${PYTHON:-python3}"

# Same reason as gates/p1.sh: the python blocks below load decompose.py by path, and
# a stale __pycache__ makes them read the bytecode rather than the file. A green gate
# that measured the wrong artefact is the worst failure available here.
export PYTHONDONTWRITEBYTECODE=1
rm -rf tools/__pycache__ analysis/__pycache__

# Set by --self-test on the child invocations. Inverts the namespace quarantine and
# nothing else: every other check runs exactly as it will on the real dataset, which
# is the only way the self-test says anything about the real run.
FIXTURE="${GBB_P2_FIXTURE:-0}"

EXPECT_INSTANCE="${GBB_P2_INSTANCE:-c8g.metal-48xl}"
EXPECT_AZ="${GBB_P2_AZ:-us-east-1a}"

PASS=0
FAIL=0
ok()   { printf '  \033[32mPASS\033[0m  %s\n' "$*"; PASS=$((PASS+1)); }
bad()  { printf '  \033[31mFAIL\033[0m  %s\n' "$*"; FAIL=$((FAIL+1)); }
note() { printf '        %s\n' "$*"; }
head_() { printf '\n%s\n%s\n' "$*" "$(printf '%.0s-' $(seq 1 ${#1}))"; }

# ---- self-test dispatch ----------------------------------------------------
if [ "${1:-}" = "--self-test" ]; then
  WORK="${GBB_P2_WORK:-${TMPDIR:-/tmp}/gbb-p2-selftest.$$}"
  KEEP="${GBB_P2_KEEP:-0}"
  rm -rf "$WORK"; mkdir -p "$WORK" || { printf 'cannot create %s\n' "$WORK"; exit 1; }
  if [ "$KEEP" != "1" ]; then
    # shellcheck disable=SC2064
    trap "rm -rf '$WORK'" EXIT
  fi

  printf '=== gate P2 self-test: the gate is exercised before the spend ===\n'
  printf 'root:    %s\n' "$ROOT"
  printf 'scratch: %s\n' "$WORK"

  head_ "S1. the fixture generates"
  if "$PY" tools/synth.py generate p2-host "$WORK/good" >"$WORK/gen.log" 2>&1; then
    ok "tools/synth.py generate p2-host — $(tail -1 "$WORK/gen.log")"
  else
    bad "tools/synth.py generate p2-host failed"; sed 's/^/        /' "$WORK/gen.log"
    printf '\n\033[31mGATE P2 SELF-TEST RED\033[0m — no fixture to judge.\n'; exit 1
  fi

  head_ "S2. the gate passes a clean P2-shaped dataset"
  if GBB_P2_FIXTURE=1 "$0" "$WORK/good/results" >"$WORK/good.log" 2>&1; then
    ok "gate P2 green on the p2-host fixture"
  else
    bad "gate P2 RED on a fixture that is clean by construction"
    sed 's/^/        /' "$WORK/good.log"
  fi

  # Each mutant breaks exactly one requirement. `mutate` is a python one-liner over
  # the copied results dir; the gate must go red and its output must mention `want`,
  # so a mutant that fails for an unrelated reason is not counted as covered.
  head_ "S3. the gate fails a dataset that breaks each requirement"
  mutants=$(cat <<'MUTANTS'
armv8-arm-gone|the mandatory generic ARMV8 arm produced nothing|ARMV8|drop_arm(coretype="ARMV8")
armv8-1thread-gone|the ARMV8 arm ran, but never at 1 thread|1 thread|drop_arm(coretype="ARMV8", threads=1)
probe-gone|the floor-overlap band was not measured|floor-overlap|drop_probe()
probe-unreplicated|the band ran but every cell holds one pair, so nothing in it reproduces|reps_per_cell|strip_field("probe_rep")
unstamped|the records carry no matrix_id|matrix_id|strip_field("matrix_id")
short-ladder|one arm swept fewer cases than the matrix claims|matrix_cases|truncate_arm(coretype="NEOVERSEN1")
env-gone|no env-*.json, so nothing has provenance|env|drop_files("env-*.json")
topology-gone|numactl -H was never recorded|topology|drop_files("topology-*.txt")
two-instance-ids|two physical boxes in one pass|instance_id|second_instance_id()
case-seconds-gone|no per-case wall clock, so no cost basis|case_seconds|strip_field("case_seconds")
wrong-instance|the pass ran on the wrong host|c8g.metal-48xl|retype("c7g.metal")
prime-gone|the thread pool was never primed, so an allocation lands in a timed region|thread_prime|drop_record("thread_prime")
decline-gone|the truncated large cases vanished instead of being recorded|matrix_cases|drop_record("case_skipped")
decline-unreasoned|a case was declined with nothing saying why|no reason|blank_reason()
warmup-field-gone|the sweep predates the warmup policy, so its wall clock is not the campaign's|warmup_reps|strip_field("warmup_reps")
cal-reused-batched|the calibration call was reused where it is not the coldest call|cal_reused|forge_reuse()
MUTANTS
)
  while IFS='|' read -r name why want op; do
    [ -n "$name" ] || continue
    rm -rf "$WORK/m-$name"
    cp -R "$WORK/good" "$WORK/m-$name"
    if ! GBB_P2_MUTATE="$op" "$PY" tools/p2-mutate.py "$WORK/m-$name/results" >"$WORK/m-$name.mut" 2>&1; then
      bad "$name: the mutation itself failed"; sed 's/^/        /' "$WORK/m-$name.mut"; continue
    fi
    if GBB_P2_FIXTURE=1 "$0" "$WORK/m-$name/results" >"$WORK/m-$name.log" 2>&1; then
      bad "$name: gate P2 went GREEN on a dataset where $why"
      note "the mutation applied: $(cat "$WORK/m-$name.mut")"
    elif grep -q -- "$want" "$WORK/m-$name.log"; then
      ok "$name: RED, and it named the reason — $why"
    else
      bad "$name: RED, but nothing in the output mentioned '$want', so it failed for another reason"
      grep -m3 FAIL "$WORK/m-$name.log" | sed 's/^/        /'
    fi
  done <<<"$mutants"

  printf '\n=== gate P2 self-test: %d passed, %d failed ===\n' "$PASS" "$FAIL"
  if [ "$FAIL" -eq 0 ]; then
    printf '\033[32mSELF-TEST GREEN\033[0m — gate P2 accepts a clean pass and rejects each defect.\n'
    exit 0
  fi
  printf '\033[31mSELF-TEST RED\033[0m — gate P2 is not yet safe to spend money behind.\n'
  exit 1
fi

# ---- real mode -------------------------------------------------------------
RESULTS="${1:-${GBB_P2_RESULTS:-results}}"

printf '=== gate P2: one host'"'"'s dataset is complete and admissible ===\n'
printf 'root:    %s\n' "$ROOT"
printf 'results: %s\n' "$RESULTS"
printf 'expect:  instance=%s az=%s fixture-mode=%s\n' "$EXPECT_INSTANCE" "$EXPECT_AZ" "$FIXTURE"

head_ "1. the inputs are there"
# shellcheck disable=SC2043  # deliberate one-element list, and the missing-file
# branch is the point: this reports `bad` rather than `continue`, so the gate
# cannot pass by finding nothing to check.
for f in analysis/decompose.py; do
  if [ -f "$f" ]; then ok "$f"; else bad "$f missing"; fi
done
if [ ! -d "$RESULTS" ]; then
  bad "$RESULTS is not a directory"
  printf '\n\033[31mGATE P2 RED\033[0m — nothing to judge.\n'; exit 1
fi
for fam in 'bench-*.ndjson' 'roofline-*.ndjson' 'census-*.ndjson' 'manifest-*.ndjson' 'env-*.json' 'topology-*.txt'; do
  # shellcheck disable=SC2086
  n=$(ls -1 $RESULTS/$fam 2>/dev/null | wc -l | tr -d ' ')
  if [ "$n" -gt 0 ]; then ok "$fam — $n file(s)"; else bad "$fam — none; the NDJSON set is incomplete"; fi
done
if [ "$FAIL" -ne 0 ]; then
  printf '\n\033[31mGATE P2 RED\033[0m — the input set is incomplete; the checks below would be misleading.\n'
  exit 1
fi

# decompose.py is run ONCE and its report reused. Running it per section would risk
# the sections disagreeing about the dataset, which is the failure mode a gate can
# least afford.
REPORT="$(mktemp -t gbb-p2-report.XXXXXX)"
STDOUT="$(mktemp -t gbb-p2-stdout.XXXXXX)"
trap 'rm -f "$REPORT" "$STDOUT"' EXIT
"$PY" analysis/decompose.py "$RESULTS" --json "$REPORT" >"$STDOUT" 2>&1
DC_EXIT=$?

head_ "2. one host, one physical box, one pass"
# A P2 pass is one instance. Two instance_ids is not a richer dataset: under the
# spend policy the same instance_type on a different instance_id IS a P3 replicate,
# so a P2 directory holding two of them either pooled two passes or shipped into the
# wrong prefix, and the replicate rule downstream will read it as evidence it is not.
if one=$("$PY" - "$RESULTS" "$EXPECT_INSTANCE" "$EXPECT_AZ" "$FIXTURE" <<'EOF'
import json, pathlib, sys
from collections import Counter

res, want_inst, want_az, fixture = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4] == "1"
bad = []
insts, ids, azs, runs = Counter(), Counter(), Counter(), Counter()
for p in sorted(res.glob("env-*.json")):
    try:
        e = json.loads(p.read_text(errors="replace"))
    except json.JSONDecodeError as exc:
        bad.append(f"{p.name} is unparseable: {exc}")
        continue
    insts[e.get("instance_type")] += 1
    ids[e.get("instance_id")] += 1
    azs[e.get("az")] += 1
    runs[e.get("run_id")] += 1
for p in sorted(res.glob("bench-*.ndjson")):
    for line in p.read_text(errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(r, dict) and "routine" in r:
            # Registered at count 0: the records must agree with env-*.json about
            # which host they came from, and a bench stream saying c7g while the env
            # says c8g is a mis-shipped prefix that every later check would read as
            # one clean host. Counting these would only inflate the tallies.
            insts[r.get("instance")] += 0
            runs[r.get("run_id")] += 0

if len(insts) != 1:
    bad.append(f"{len(insts)} instance types in one results dir: {sorted(map(str, insts))}")
elif next(iter(insts)) != want_inst:
    bad.append(f"instance_type={next(iter(insts))!r}, and P2 is pinned to {want_inst!r}")
if len(ids) != 1:
    bad.append(
        f"{len(ids)} instance_ids: {sorted(map(str, ids))}. Same instance_type on a different "
        f"instance_id is how the spend policy IDENTIFIES a P3 replicate, so two of them in a P2 "
        f"directory is either two pooled passes or a mis-shipped prefix"
    )
elif not next(iter(ids)):
    bad.append("instance_id is empty, so this pass cannot be told apart from a P3 replicate")
if len(runs) != 1:
    bad.append(f"{len(runs)} run_ids: {sorted(map(str, runs))}; one P2 pass is one run-matrix.sh run")
if len(azs) != 1:
    bad.append(f"{len(azs)} availability zones: {sorted(map(str, azs))}")
elif next(iter(azs)) != want_az:
    bad.append(
        f"az={next(iter(azs))!r}, and the campaign is pinned to {want_az!r} — the only AZ where "
        f"all five families can be placed together"
    )
print(
    "; ".join(bad)
    if bad
    else f"{next(iter(insts))} {next(iter(ids))} in {next(iter(azs))}, run_id={next(iter(runs))}"
)
sys.exit(1 if bad else 0)
EOF
); then
  ok "one host, one box, one pass: $one"
else
  bad "the dataset is not one clean pass — $one"
fi

head_ "3. the case matrix is stamped, and the sweep measured all of it"
# The stamp's two jobs are different and both are checked. Being SINGLE is what stops
# a pre-expansion pass pooling with a post-expansion one (decompose.py refuses that
# itself, exit bit 64). Being HONOURED — every arm having actually measured
# matrix_cases distinct cases — is what no other check in the repo can see, because
# every other coverage check is derived from the data and therefore cannot notice
# that the whole dataset is short.
if mat=$("$PY" - "$RESULTS" "$FIXTURE" <<'EOF'
import json, pathlib, sys
from collections import defaultdict

res, fixture = pathlib.Path(sys.argv[1]), sys.argv[2] == "1"
sys.path.insert(0, "analysis")
import importlib.util

spec = importlib.util.spec_from_file_location("dc", pathlib.Path("analysis/decompose.py"))
dc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dc)

bad = []
ids, counts = defaultdict(int), defaultdict(int)
cases = defaultdict(set)
declined, unreasoned = defaultdict(set), defaultdict(set)
primed = set()
warmups, nofield, reuse_bad = [], 0, 0
for p in sorted(res.glob("bench-*.ndjson")):
    for line in p.read_text(errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(r, dict):
            continue
        stream_k = (r.get("run_id"), r.get("library"), r.get("target"), r.get("coretype"), r.get("threads"))
        if r.get("record") == "thread_prime":
            # Read BEFORE the routine filter, because a priming record carries no
            # routine. It is the evidence that thread 2..N's buffer pool was
            # allocated outside every timed region: without it the whole stream's
            # timings are suspect in a way no individual record shows.
            primed.add(stream_k)
            continue
        if "routine" not in r:
            continue
        ids[dc.canon_matrix_id(r.get("matrix_id"))] += 1
        cell = (r.get("routine"), r.get("m"), r.get("n"), r.get("k"), r.get("lda_pad"),
                r.get("incx"), r.get("transa"), r.get("transb"))
        if r.get("record") == "case_skipped":
            # A declined case carries a routine, so it reaches this point and must be
            # accounted for separately. Counting it as measured would let a stream
            # that declined EVERY case look complete; not counting it at all would
            # make the thread-dependent large ladder read as a short sweep. The
            # matrix is honoured when measured + declined covers it, and a decline
            # only counts as accounted-for when it says why.
            declined[stream_k].add(cell)
            if not (r.get("reason") or "").strip():
                unreasoned[stream_k].add(cell)
            continue
        if (r.get("probe") or "none") != "none":
            # A probe record re-measures a matrix case under the other floor, so
            # counting it toward the sweep's case set would make a dataset with the
            # band look complete when the band is exactly what is extra.
            continue
        counts[r.get("matrix_cases")] += 1
        cases[stream_k].add(cell)
        if "warmup_reps" not in r or "cal_reused" not in r:
            nofield += 1
        else:
            warmups.append(r["warmup_reps"])
            if r["cal_reused"] and (r.get("batch", 1) != 1 or r["warmup_reps"] != 0):
                reuse_bad += 1

if len(ids) != 1:
    bad.append(f"{len(ids)} matrix_ids: {sorted(ids)} — decompose.py refuses this outright (bit 64)")
else:
    mid = next(iter(ids))
    if mid == dc.UNSTAMPED_MATRIX:
        bad.append(
            "the records carry no matrix_id. P2 runs pre-expansion so its data can never pool "
            "with P3's, and an unstamped pass is one that cannot make that claim about itself"
        )
    elif fixture and not mid.startswith("synth-"):
        bad.append(f"self-test mode, but matrix_id={mid!r} is not synth-namespaced: this is real data")
    elif not fixture and mid.startswith("synth-"):
        bad.append(
            f"matrix_id={mid!r} is tools/synth.py's namespace. This is a FIXTURE, not a "
            f"measurement, and standing order 3 says a number nobody measured is not a result"
        )

declared = {c for c in counts if isinstance(c, int) and c > 0}
if len(declared) != 1:
    bad.append(f"matrix_cases is not one number across the dataset: {sorted(map(str, counts))}")
elif not bad:
    want = next(iter(declared))
    # measured + declined, not measured alone. bench.c truncates the large ladder at
    # low thread counts and writes a case_skipped record for each omission, so a
    # 1-thread stream is SHORT ON MEASUREMENTS BY DESIGN and complete on accounting.
    # Checking measurements alone would fail every 1-thread stream; checking the
    # union without checking the reasons would accept a sweep that declined
    # everything. Both halves are load-bearing.
    short = {s: (len(c), len(declined.get(s, ()))) for s, c in cases.items()
             if len(c | declined.get(s, set())) != want}
    if short:
        worst = sorted(short.items(), key=lambda kv: sum(kv[1]))[:3]
        bad.append(
            f"{len(short)} of {len(cases)} (arm, threads) streams account for a different number "
            f"of distinct cases than matrix_cases={want} declares: "
            + ", ".join(f"{'/'.join(str(x) for x in s[1:])}={m}+{d} declined" for s, (m, d) in worst)
            + ". Every other coverage check in the repo is derived from the data and so cannot "
              "see a dataset that is uniformly short; the stamp is the only absolute expectation"
        )
    if unreasoned:
        n = sum(len(c) for c in unreasoned.values())
        bad.append(
            f"{n} declined cases across {len(unreasoned)} streams carry no reason. Standing "
            f"order 11 at case granularity: a case absent from EVERY arm at a thread point "
            f"produces no cell at all, so a data-derived census cannot see it and the record's "
            f"own reason is the only thing separating policy from a hole"
        )
    unprimed = sorted(set(cases) - primed)
    if unprimed:
        bad.append(
            f"{len(unprimed)} of {len(cases)} streams have no thread_prime record, e.g. "
            + ", ".join("/".join(str(x) for x in s[1:]) for s in unprimed[:3])
            + ". Warmup is a per-process cost and the priming call is where it is paid; "
              "without it thread 2..N's buffer pool is allocated inside a timed region"
        )
    # The timing policy, read off the records rather than recomputed. Three claims:
    # the fields exist at all (a pre-change binary produces none of them, and that is
    # a dataset whose cost basis does not describe the campaign that will run); the
    # no-warmup path was actually reached somewhere (or the policy is present and
    # inert); and reuse never happened outside its own preconditions. The last is the
    # only one that could corrupt a number: reusing the calibration call when the
    # case was batched, or when warmup ran after it, means samples[0] is not the
    # coldest comparable call and the reading is flattered.
    if nofield:
        bad.append(
            f"{nofield} measurements carry no warmup_reps/cal_reused. This is a pre-policy "
            f"binary, so its per-case wall clock is not the cost basis the campaign will run on"
        )
    elif not any(w == 0 for w in warmups):
        bad.append(
            "no case in the dataset reached the no-warmup path, so the conditional warmup is "
            "present and inert and the expensive end is still paying for it"
        )
    if reuse_bad:
        bad.append(
            f"{reuse_bad} measurements report cal_reused with batch>1 or warmup_reps>0: the "
            f"calibration call was reused where it is not the coldest comparable call"
        )

print(
    "; ".join(bad)
    if bad
    else f"matrix_id={next(iter(ids))} cases={next(iter(declared))}, honoured by all "
         f"{len(cases)} (arm, threads) streams "
         f"({sum(len(c) for c in declined.values())} declined with reasons, all primed, "
         f"{sum(1 for w in warmups if w == 0)}/{len(warmups)} cases past the warmup boundary)"
)
sys.exit(1 if bad else 0)
EOF
); then
  ok "the case matrix is stamped and complete: $mat"
else
  bad "the case matrix is not accounted for — $mat"
fi

head_ "4. the arms P2 exists to produce are present"
# The generic ARMV8 arm at 1 thread is the condition the re-sequencing decision put
# on running P2 before the expansion. It is also the arm most likely to be lost
# quietly: it is the slowest, so it is the one an operator shortening a run cuts.
if arms=$("$PY" - "$RESULTS" <<'EOF'
import json, pathlib, sys
from collections import defaultdict

res = pathlib.Path(sys.argv[1])
bad = []
by_arm = defaultdict(lambda: defaultdict(int))
for p in sorted(res.glob("bench-*.ndjson")):
    for line in p.read_text(errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(r, dict) or "routine" not in r:
            continue
        if r.get("record"):
            # A `case_skipped` record carries a routine, and an arm that declined
            # cases and measured nothing is NOT present. Counting bookkeeping as
            # evidence of an arm having run would make the ARMV8-at-1-thread
            # requirement satisfiable by a stream that produced no numbers, which is
            # the arm most likely to be cut and the one the extrapolation rests on.
            continue
        if (r.get("probe") or "none") != "none":
            continue
        by_arm[(r.get("library"), r.get("target"), r.get("coretype") or "unforced")][r.get("threads")] += 1

# Labelled by what the library REPORTED (standing order 10): `coretype` in a bench
# record is run-matrix.sh's coretype_effective, read back out of
# openblas_get_corename(), not the OPENBLAS_CORETYPE that was requested.
armv8 = [a for a in by_arm if a[0] == "openblas" and (a[2] or "").upper() == "ARMV8"]
if not armv8:
    bad.append(
        "no openblas arm reported coretype ARMV8. The generic arm at 1 thread is the campaign's "
        "most expensive single arm and the P3 cost extrapolation is anchored on it; run-matrix.sh "
        "runs it by construction, so its absence means GBB_CORETYPES was overridden or the force "
        "was not honoured"
    )
else:
    for a in armv8:
        if not by_arm[a].get(1):
            bad.append(
                f"{'/'.join(map(str, a))} produced records at threads "
                f"{sorted(t for t in by_arm[a] if t)}, but none at 1 thread — and 1 thread is the "
                f"point the cost extrapolation is taken at, because wall-clock is anti-correlated "
                f"with arm quality"
            )
shipped = [a for a in by_arm if a[0] == "openblas" and a[1] == "DYNAMIC" and a[2] == "unforced"]
if not shipped:
    bad.append("no openblas/DYNAMIC/unforced arm — that is the arm NumPy wheels actually run")
refs = [a for a in by_arm if a[0] not in (None, "openblas")]
if not refs:
    bad.append("no non-OpenBLAS reference arm, so section 1 has nothing to compare against")
if not any(t == 1 for a in by_arm for t in by_arm[a] if t):
    bad.append("nothing ran at 1 thread, so kernel quality is nowhere isolated from the threading layer")
print(
    "; ".join(bad)
    if bad
    else f"{len(by_arm)} arms; ARMV8 at threads {sorted(t for a in armv8 for t in by_arm[a])}; "
         f"reference {sorted('/'.join(map(str, a)) for a in refs)}"
)
sys.exit(1 if bad else 0)
EOF
); then
  ok "the mandatory arms ran: $arms"
else
  bad "an arm P2 is required to produce is missing — $arms"
fi

head_ "5. provenance is complete (standing order 5)"
# The named list: OpenBLAS SHA, compiler version, MIDR, HWCAP, governor, NUMA
# topology, and what DYNAMIC_ARCH selected. decompose.py sets exit bit 8 when these
# are short, and section 8 below requires bit 8 clear -- but a bit says only that
# something is missing. This says which field, because "re-run capture-env.sh" and
# "re-run build-libs.sh with nm available" are different remedies.
if prov=$("$PY" - "$RESULTS" <<'EOF'
import json, pathlib, sys

res = pathlib.Path(sys.argv[1])
bad = []
envs = []
for p in sorted(res.glob("env-*.json")):
    try:
        envs.append((p.name, json.loads(p.read_text(errors="replace"))))
    except json.JSONDecodeError as exc:
        bad.append(f"{p.name} unparseable: {exc}")
if not envs:
    bad.append("no env-*.json: nothing on this host has provenance and no number here is admissible")
for name, e in envs:
    for field in (
        "midr", "midr_uniform", "core_clusters", "cpu_features", "cpufreq_governor",
        "numa_nodes", "sockets", "cpus_online", "cpus_affinity", "kernel",
        "openblas_dynamic_selection", "openblas_dynamic_probe_status", "openblas_coretype_forcing",
    ):
        if e.get(field) in (None, "", []):
            bad.append(f"{name}: {field} is absent or empty")
    if e.get("has_sve") is True and not e.get("sve_default_vl_bytes"):
        bad.append(f"{name}: has_sve but no sve_default_vl_bytes — VL is the campaign's central axis")
    # Standing order 8, stated here as well as inside decompose.py. Distinct from
    # section 4's ARMV8 arm: that one is a coretype the runner FORCED on purpose,
    # this is what DYNAMIC_ARCH chose by itself, and on an SVE host generic ARMV8
    # means SVE detection failed and outweighs every kernel question in the repo.
    sel = (e.get("openblas_dynamic_selection") or "").lower()
    if e.get("has_sve") is True and "armv8" in sel and "sve" not in sel:
        bad.append(
            f"{name}: DYNAMIC_ARCH selected {sel!r} on a host reporting SVE. STOP AND ESCALATE "
            f"(standing order 8) — this outweighs every kernel question in the repo and is not a "
            f"completeness problem to be gated past"
        )

shas, ccs = set(), set()
for p in sorted(res.glob("manifest-*.ndjson")):
    for line in p.read_text(errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(r, dict):
            continue
        if r.get("record") == "toolchain":
            ccs.add(r.get("cc_version") or "")
            shas.add(r.get("openblas_sha") or "")
        elif r.get("record") == "arm" and r.get("library") == "openblas" and r.get("built"):
            shas.add(r.get("blas_sha") or "")
if not ccs:
    bad.append("no toolchain record in manifest-*.ndjson: the compiler version is unrecorded")
elif "" in ccs:
    bad.append("the toolchain record carries an empty cc_version")
shas.discard("")
if not shas:
    bad.append("no OpenBLAS SHA anywhere in the manifest: the library under test is unidentified")
elif len(shas) != 1:
    bad.append(
        f"{len(shas)} distinct OpenBLAS SHAs on one host: {sorted(shas)}. Standing order 6 says "
        f"only the BLAS under test varies between arms, and it must be ONE tree per pass"
    )
print("; ".join(bad) if bad else f"env fields complete, cc={sorted(ccs)[0]}, openblas_sha={sorted(shas)[0]}")
sys.exit(1 if bad else 0)
EOF
); then
  ok "provenance is complete: $prov"
else
  bad "provenance is incomplete — $prov"
fi

head_ "6. the topology was recorded, not assumed"
# CLAUDE.md names topology-*.txt explicitly, and run-matrix.sh says why: whether
# c8g.metal-48xl at 192 vCPU is one socket or two decides how every multithreaded
# number on it is read. A file that exists but says "(numactl unavailable)" answers
# nothing, so the content is checked and not just the name.
if topo=$("$PY" - "$RESULTS" <<'EOF'
import pathlib, sys

res = pathlib.Path(sys.argv[1])
bad = []
files = sorted(res.glob("topology-*.txt"))
if not files:
    bad.append("no topology-*.txt")
for p in files:
    t = p.read_text(errors="replace")
    if "=== numactl -H ===" not in t:
        bad.append(f"{p.name} has no numactl -H section")
    elif "(numactl unavailable)" in t:
        bad.append(
            f"{p.name}: numactl was unavailable, so the NUMA layout is unrecorded and every "
            f"multithreaded number on this host is uninterpretable"
        )
    elif "available:" not in t and "node 0 cpus" not in t:
        bad.append(f"{p.name}: the numactl -H section names no nodes")
    if "=== lscpu ===" not in t:
        bad.append(f"{p.name} has no lscpu section")
print("; ".join(bad) if bad else f"{len(files)} file(s), numactl -H and lscpu both recorded")
sys.exit(1 if bad else 0)
EOF
); then
  ok "topology recorded: $topo"
else
  bad "the topology is not recorded — $topo"
fi

head_ "7. the timing-floor band was measured and confirms"
# ABSENT does not set exit bit 32 on purpose -- every pre-probe dataset is ABSENT --
# so requiring the probe to be PRESENT is this gate's job. Unconfirmed, section 4
# cannot be read across n=256, which is where GEMM_SMALL_*'s crossover is expected.
if band=$("$PY" - "$REPORT" <<'EOF'
import json, pathlib, sys

d = json.loads(pathlib.Path(sys.argv[1]).read_text())
o = d.get("floor_overlap") or {}
st = o.get("status")
bad = []
if st in (None, "ABSENT"):
    bad.append(
        "the floor-overlap band is ABSENT. The 0.05/0.30 floor transition sits at n=256, which is "
        "where the effect is expected, so without the band a step there is ambiguous between "
        "'the fast path ends here' and 'the measurement window changed here'"
    )
elif st == "INCOMPLETE":
    bad.append(f"the band ran but did not pair up: {o.get('why')}")
elif st not in ("AGREES", "AGREES-WITH-BIAS"):
    bad.append(f"the band came out {st}: {o.get('why')}")
# The band being confirmed is not the same as the band being able to confirm anything.
# At one pair per cell an out-of-band pair can be neither reproduced nor dismissed --
# which is what the first P2 pass's 2-of-390 at 56% floor sign consistency was, and it
# is why bench.c gained OVERLAP_REPS. A pass built from a pre-replication binary would
# otherwise report AGREES here and read as stronger evidence than it is, so the rep
# count is a gate requirement and not a footnote.
if (o.get("reps_per_cell") or 0) < 2 and st not in (None, "ABSENT"):
    bad.append(
        f"the band ran with reps_per_cell={o.get('reps_per_cell')}: one pair per cell, so "
        "nothing in it can be reproduced or dismissed. Either the binary predates "
        "bench.c's OVERLAP_REPS or probe_rep is not reaching the records"
    )
print(
    "; ".join(bad)
    if bad
    else f"{st} over {o.get('n_pairs')} pairs = {o.get('cells')} cell(s) x "
         f"{o.get('reps_per_cell')} rep(s), median bias {o.get('median_bias')}, "
         f"worst delta {o.get('worst_delta')}"
)
sys.exit(1 if bad else 0)
EOF
); then
  ok "the floor-overlap band: $band"
else
  bad "the floor-overlap band does not support reading section 4 across n=256 — $band"
fi

head_ "8. decompose.py is clean, and every gap carries a reason"
# Exit 0, not "no bits this gate cares about". A genuine finding is a VERDICT, not a
# bit: the bits are poisoned records, coverage holes, missing provenance, an
# irreproducible headline, an unconfirmed floor band and a mixed matrix, and none of
# those is a finding. So "clean bar genuine findings" is exactly exit 0.
if [ "$DC_EXIT" -eq 0 ]; then
  ok "analysis/decompose.py exited 0"
else
  bad "analysis/decompose.py exited $DC_EXIT — bits: $(
    b=""; for pair in '1:nothing loaded' '2:poisoned records' '4:unexplained coverage hole' \
      '8:provenance incomplete' '16:headline does not reproduce' '32:floor band unconfirmed' \
      '64:more than one case matrix'; do
      bit="${pair%%:*}"; why="${pair#*:}"
      [ $((DC_EXIT & bit)) -ne 0 ] && b="$b [$bit $why]"
    done; echo "$b")"
  grep -E 'ESCALATE|!!|VERDICT-CAVEAT|MISSING-UNEXPLAINED cells' "$STDOUT" | head -6 | sed 's/^/        /'
fi
if cov=$("$PY" - "$REPORT" <<'EOF'
import json, pathlib, sys

d = json.loads(pathlib.Path(sys.argv[1]).read_text())
c = d.get("coverage") or {}
bad = []
if c.get("missing_unexplained"):
    bad.append(f"{c['missing_unexplained']} MISSING-UNEXPLAINED cells — the P2 row requires zero")
if c.get("partial"):
    bad.append(f"{c['partial']} partial cells: an arm short of sizes with nothing accounting for it")
# An explained absence with an empty reason is the same claim as an unexplained one
# wearing a status. Standing order 11: absent and null are different claims, and the
# reason is what lets the analysis tell them apart.
for e in c.get("explained") or []:
    if not (e.get("reason") or "").strip():
        bad.append(
            f"{e.get('instance')} {e.get('arm')} is {e.get('status')} over {e.get('cells')} cells "
            f"with NO reason recorded"
        )
for h in d.get("hosts") or []:
    if h.get("state") != "ADMISSIBLE":
        bad.append(f"{h.get('instance')} is {h.get('state')}: {h.get('escalate') or h.get('invalidating')}")
if (d.get("inputs") or {}).get("unparseable_lines"):
    bad.append(f"{d['inputs']['unparseable_lines']} unparseable lines: a truncated or mixed shipment")
if (d.get("inputs") or {}).get("foreign_roles"):
    bad.append(
        f"records from other roles in this directory: {d['inputs']['foreign_roles']}. "
        f"castor/pollux are instrument checks, never data"
    )
if (d.get("inputs") or {}).get("escalation_acks"):
    bad.append(
        f"{d['inputs']['escalation_acks']} GBB_ESCALATION_ACK record(s): capture-env.sh refused this "
        f"host and was overridden. Whatever it refused for is still true"
    )
tally = c.get("by_status") or {}
print(
    "; ".join(bad)
    if bad
    else f"{c.get('expected_cells')} expected cells, {tally}, every absence carries a reason"
)
sys.exit(1 if bad else 0)
EOF
); then
  ok "the coverage census is closed: $cov"
else
  bad "the coverage census has an unaccounted gap — $cov"
fi

head_ "9. the wall-clock cost basis, measured"
# P2's other deliverable, and the reason it runs before expansion items 3-5. The
# figure that goes in the umbrella issue comes from here, off the SLOWEST arm: the
# cheap/expensive boundary is wherever MIN_SAMPLES stops being satisfied inside the
# floor, which moves with thread count and with how fast the arm is, so a
# representative arm extrapolates low. This section FAILS only if the accounting is
# unavailable; the numbers themselves are a report, not a threshold. A gate that
# refused a dataset for being expensive would be refusing the measurement it exists
# to obtain.
if cost=$("$PY" - "$RESULTS" <<'EOF'
import json, pathlib, sys
from collections import defaultdict


def regime(n):
    return "small" if n <= 256 else "medium" if n <= 1536 else "large"


res = pathlib.Path(sys.argv[1])
bad = []
arm_total = defaultdict(float)
arm_cases = defaultdict(int)
by_regime = defaultdict(lambda: defaultdict(float))
regime_cases = defaultdict(lambda: defaultdict(int))
missing = 0
nonpositive = 0
total = 0.0
probe_total = 0.0
prime_total = 0.0
declined_total = 0.0
declined_n = 0
for p in sorted(res.glob("bench-*.ndjson")):
    for line in p.read_text(errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(r, dict):
            continue
        kind = r.get("record")
        if kind in ("thread_prime", "case_skipped"):
            # Overhead, not measurement, and accounted for separately rather than
            # dropped. bench.c closes the wall-clock interval on these records too, so
            # the case_seconds column still sums to the process's wall clock -- and
            # that additivity is the whole reason the cost basis can be read off the
            # records at all. Folding them into `total` would inflate the per-case
            # figures the expansion multiplies; dropping them would make the pass look
            # cheaper than the instance-hours it bills.
            cs = r.get("case_seconds")
            if isinstance(cs, (int, float)) and cs >= 0:
                if kind == "thread_prime":
                    prime_total += cs
                else:
                    declined_total += cs
                    declined_n += 1
            continue
        if "routine" not in r:
            continue
        if "case_seconds" not in r:
            missing += 1
            continue
        cs = r["case_seconds"]
        if not isinstance(cs, (int, float)) or cs <= 0:
            nonpositive += 1
            continue
        if (r.get("probe") or "none") != "none":
            probe_total += cs
            continue
        key = (
            f"{r.get('library')}/{r.get('target')}/{r.get('coretype') or 'unforced'}",
            r.get("threads"),
        )
        arm_total[key] += cs
        arm_cases[key] += 1
        by_regime[key][regime(r.get("m") or 0)] += cs
        regime_cases[key][regime(r.get("m") or 0)] += 1
        total += cs

if missing:
    bad.append(
        f"{missing} bench records carry no case_seconds, so this pass produced no cost basis. "
        f"The P3 budget rests on a wall-clock multiplier that only real hardware measures, and "
        f"a pass that did not record it has to be re-run to get it"
    )
if nonpositive:
    bad.append(f"{nonpositive} records carry a non-positive case_seconds")
if not arm_total and not bad:
    bad.append("no non-probe bench records to account for")

if not bad:
    ranked = sorted(arm_total.items(), key=lambda kv: -kv[1])
    slowest, slowest_s = ranked[0]
    print(f"pass wall clock across all arms: {total / 3600:.2f} instance-hours of measurement")
    print(f"    (plus {probe_total:.1f} s in the floor-overlap band)")
    print(
        f"    (plus {prime_total:.1f} s priming thread pools and {declined_total:.1f} s "
        f"declining {declined_n} capped large cases — the overhead the timing policy moved "
        f"out of the measurements)"
    )
    print("    most expensive arms:")
    for (arm, thr), s in ranked[:5]:
        print(f"      {arm:34s} t={thr!s:<4} {s / 60:8.1f} min over {arm_cases[(arm, thr)]:5d} cases")
    print(f"    cheapest arm: {ranked[-1][0][0]} t={ranked[-1][0][1]} {ranked[-1][1] / 60:.1f} min")
    print(f"    SLOWEST ARM — extrapolate P3 from this one, never from a representative arm:")
    print(f"      {slowest[0]} at t={slowest[1]}, {slowest_s / 60:.1f} min")
    for reg in ("small", "medium", "large"):
        s, n = by_regime[slowest][reg], regime_cases[slowest][reg]
        if n:
            print(f"        {reg:6s} {s:8.1f} s over {n:4d} cases = {s / n * 1000:8.2f} ms/case")
    # The multiplier the spend policy needs: what one MORE case costs, per regime, on
    # the arm that costs the most. Printed rather than checked -- the campaign wants
    # this number, and a threshold on it would be this gate deciding the budget.
    print("    per-case cost on the slowest arm is what expansion items 3-5 multiply.")
else:
    # Printed on stdout like every other block's diagnosis, not left to the caller to
    # infer from a bare exit code. Missed the first time and caught by the self-test:
    # the mutant went red with an empty message, which is a gate that has stopped
    # saying what is wrong -- the one thing a gate is for.
    print("; ".join(bad))
sys.exit(1 if bad else 0)
EOF
); then
  ok "the cost basis is recorded"
  printf '%s\n' "$cost" | sed 's/^/        /'
else
  bad "this pass produced no wall-clock cost basis — $cost"
fi

printf '\n=== gate P2: %d passed, %d failed ===\n' "$PASS" "$FAIL"
if [ "$FAIL" -eq 0 ]; then
  printf '\033[32mGATE P2 GREEN\033[0m — one complete admissible pass. P3 may start once Scott confirms the spend.\n'
  exit 0
fi
printf '\033[31mGATE P2 RED\033[0m — this dataset does not support a P3 launch.\n'
exit 1
