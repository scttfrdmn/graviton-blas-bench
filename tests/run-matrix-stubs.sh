#!/usr/bin/env bash
# Stub-based regression suite for scripts/run-matrix.sh.
#
# The runner's job is almost entirely decision-making: refuse to spend
# instance-hours on a host capture-env.sh has condemned, never label an arm with
# a coretype the library ignored, and account for every arm it declines to run.
# None of that needs a Graviton or a real BLAS, and all of it is exactly what
# would be most expensive to get wrong -- a mislabelled arm is not a failed run,
# it is a plausible wrong answer. So it is tested here against stubs.
#
# Each case below corresponds to a defect found in the pre-P1 audit; see
# docs/pre-P1-audit.md. Run from anywhere:  bash tests/run-matrix-stubs.sh
set -uo pipefail

REPO="${1:-$(cd "$(dirname "$0")/.." && pwd)}"
# PID-suffixed by default: `gates/p0.sh` runs this suite, so a p0 run concurrent
# with a direct run shared one fixture tree and each `rm -rf`'d the other's stubs
# mid-flight -- 35 assertions failed for reasons that had nothing to do with the
# code under test. Artifacts are deliberately left behind for post-mortem; pass
# GBB_TEST_TMP to pin the path.
T="${GBB_TEST_TMP:-/tmp/gbb-test.$$}"
rm -rf "$T/work"; mkdir -p "$T/work/bin" "$T/work/scripts" "$T/work/libs" "$T/work/results"
W="$T/work"

# Copy the scripts under test; stub out capture-env.
cp "$REPO/scripts/run-matrix.sh" "$W/scripts/"

mk_env() {  # mk_env <exit-code> <has_sve> <has_sve2> <forcing> [cpus_affinity]
  cat > "$W/scripts/capture-env.sh" <<EOF
#!/usr/bin/env bash
cat <<'JSON'
{"instance_type":"c8g.metal-48xl","cores_total":192,"cpus_affinity":${5:-192},
 "has_sve":$2,"has_sve2":$3,"openblas_coretype_forcing":"$4",
 "midr_uniform":true,"cpufreq_governor":"performance","warnings":[]}
JSON
exit $1
EOF
  chmod +x "$W/scripts/capture-env.sh"
}

# Fake topology: two nodes of 96, matching a plausible c8g.metal-48xl.
mk_topo() {
  cat > "$W/bin/numactl" <<'EOF'
#!/usr/bin/env bash
if [ "${1:-}" = "-H" ]; then
  printf 'available: 2 nodes (0-1)\n'
  printf 'node 0 cpus:'; for i in $(seq 0 95); do printf ' %d' "$i"; done; printf '\n'
  printf 'node 0 size: 100000 MB\n'
  printf 'node 1 cpus:'; for i in $(seq 96 191); do printf ' %d' "$i"; done; printf '\n'
  printf 'node 1 size: 100000 MB\n'
  exit 0
fi
# Acting as the launcher: strip our own flags and exec the rest.
while [ $# -gt 0 ]; do case "$1" in --*) shift;; *) break;; esac; done
exec "$@"
EOF
  chmod +x "$W/bin/numactl"
}

# Stub harness binaries. gbb-openblas-* emits one plausible bench record and
# echoes the provenance env it was handed, so the test can assert the record
# carries what it should.
mk_bins() {
  for exe in gbb-openblas-DYNAMIC gbb-openblas-NEOVERSEV1 gbb-armpl; do
    cat > "$W/bin/$exe" <<'EOF'
#!/usr/bin/env bash
printf '{"run_id":"%s","library":"%s","target":"%s","blas_sha":"%s","coretype":"%s",' \
  "$GBB_RUN_ID" "$GBB_LIBRARY" "$GBB_TARGET" "$GBB_BLAS_SHA" "$GBB_CORETYPE"
printf '"thread_backend":"%s","pin_policy":"%s","arch_selected":"%s","role":"%s","threads":%s,' \
  "$GBB_THREAD_BACKEND" "$GBB_PIN_POLICY" "$GBB_ARCH_SELECTED" "${GBB_ROLE:-unknown}" "$GBB_THREADS"
printf '"routine":"dgemm","m":512,"n":512,"k":512,"gflops":100.0,"verified":true,'
printf '"omp_proc_bind":"%s","openblas_coretype_env":"%s"}\n' \
  "${OMP_PROC_BIND:-unset}" "${OPENBLAS_CORETYPE:-unset}"
EOF
    chmod +x "$W/bin/$exe"
  done
  cat > "$W/bin/gbb-roofline" <<'EOF'
#!/usr/bin/env bash
printf '{"metric":"peak_fma","gflops_f64":1000.0,"threads":%s}\n' "$GBB_THREADS"
EOF
  chmod +x "$W/bin/gbb-roofline"
  # The coretype probe: honours the request except for NEOVERSEN2, which it
  # reports as neoversen2... and ARMV8SVE, which it deliberately ignores so the
  # "request not honoured" path gets exercised.
  cat > "$W/bin/gbb-coreprobe-DYNAMIC" <<'EOF'
#!/usr/bin/env bash
case "${OPENBLAS_CORETYPE:-}" in
  "")         echo "neoversev2|OpenBLAS 0.3.32 DYNAMIC_ARCH NEOVERSEV2" ;;
  ARMV8SVE)   echo "neoversev2|OpenBLAS 0.3.32 DYNAMIC_ARCH NEOVERSEV2" ;;  # ignored request
  NEOVERSEV1) echo "neoversev1|OpenBLAS 0.3.32 DYNAMIC_ARCH NEOVERSEV1" ;;
  ARMV8)      echo "armv8|OpenBLAS 0.3.32 DYNAMIC_ARCH ARMV8" ;;
  NEOVERSEN1) echo "neoversen1|OpenBLAS 0.3.32 DYNAMIC_ARCH NEOVERSEN1" ;;
  NEOVERSEV2) echo "neoversev2|OpenBLAS 0.3.32 DYNAMIC_ARCH NEOVERSEV2" ;;
  NEOVERSEN2) exit 132 ;;   # SIGILL path
esac
EOF
  chmod +x "$W/bin/gbb-coreprobe-DYNAMIC"
}

mk_manifest() {
  cat > "$W/libs/build-manifest.ndjson" <<'EOF'
{"record":"arm","library":"openblas","target":"DYNAMIC","coretype":null,"blas_sha":"cc3fc1eaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","built":true,"runnable":true,"reason":"","thread_backend":"pthreads","exe":"gbb-openblas-DYNAMIC","prefix":"/libs/openblas-DYNAMIC","sve_kernels":"yes"}
{"record":"arm","library":"openblas","target":"DYNAMIC_OMP","coretype":null,"blas_sha":"cc3fc1eaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","built":false,"runnable":true,"reason":"build failed, see openblas-DYNAMIC_OMP.buildlog","thread_backend":"openmp","exe":"gbb-openblas-DYNAMIC_OMP","prefix":""}
{"record":"arm","library":"openblas","target":"NEOVERSEV1","coretype":null,"blas_sha":"cc3fc1eaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","built":true,"runnable":true,"reason":"","thread_backend":"pthreads","exe":"gbb-openblas-NEOVERSEV1","prefix":"/libs/openblas-NEOVERSEV1","sve_kernels":"no"}
{"record":"arm","library":"openblas","target":"ARMV8SVE","coretype":null,"blas_sha":"cc3fc1eaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","built":true,"runnable":false,"reason":"target requires sve which this host does not report","thread_backend":"pthreads","exe":"gbb-openblas-ARMV8SVE","prefix":""}
{"record":"arm","library":"armpl","target":"native","coretype":null,"blas_sha":"armpl-armpl_24.10_gcc","built":true,"runnable":true,"reason":"","thread_backend":"openmp","exe":"gbb-armpl","prefix":"/opt/arm","sve_kernels":"n/a"}
{"record":"arm","library":"reference","target":"native","coretype":null,"blas_sha":"","built":true,"runnable":true,"reason":"correctness control only, not timed","thread_backend":"pthreads","exe":"gbb-reference","prefix":""}
{"record":"toolchain","cc":"gcc","cc_version":"gcc 13.3.0","kernel":"6.8.0","libc":"2.39","openblas_ref":"cc3fc1e","openblas_sha":"cc3fc1eaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","blis_ref":"master","blis_sha":"deadbeef","native_target":"NEOVERSEV2","cross_target":"NEOVERSEV1","host_sve":true,"host_sve2":true}
EOF
}

run() {
  ( cd "$W" && PATH="$W/bin:$PATH" \
      GBB_PREFIX="$W/libs" GBB_RESULTS="$W/results" GBB_RUN_ID="$1" \
      GBB_THREADS_LADDER="${GBB_LADDER_OVERRIDE:-1 64}" \
      env "${@:2}" bash "$W/scripts/run-matrix.sh" ) 2>"$W/results/$1.stderr"
  echo "$?"
}

# Every one of these runs is an INSTRUMENT run and cannot be anything else: the
# role interlock in run-matrix.sh requires both an IMDS instance type in the
# campaign set and a Graviton MIDR part in cpu0, and no machine this test runs on
# has the second. That is the property under test as much as it is a fact about
# the fixture -- so the files land under results/instrument/ with an instr-
# prefixed run_id, and the assertions below spell those out rather than hiding
# them behind a variable that could quietly become results/ again.
RES="$W/results/instrument"

pass=0; fail=0
chk() { if [ "$2" = "$3" ]; then pass=$((pass+1)); printf '  ok   %s\n' "$1";
        else fail=$((fail+1)); printf '  FAIL %s\n       want=%s\n       got =%s\n' "$1" "$3" "$2"; fi; }

mk_topo; mk_bins; mk_manifest

echo "== A. capture-env exit 4 must refuse to sweep =="
mk_env 4 true true available
rc=$(run A)
chk "exit code is 3 (refused)" "$rc" "3"
chk "no bench records written" "$( [ -s "$RES/bench-instr-A.ndjson" ] && echo some || echo none )" "none"
chk "stderr names standing order 8" "$(grep -c 'standing order 8' "$W/results/A.stderr")" "1"

echo "== B. capture-env exit 4 + GBB_ESCALATION_ACK proceeds and records the ack =="
rc=$(GBB_LADDER_OVERRIDE="1" run B GBB_ESCALATION_ACK="Scott confirmed 0xd84 is expected on c9g")
chk "exit code is 0" "$rc" "0"
chk "escalation_ack recorded" "$(grep -c '"record":"escalation_ack"' "$RES/census-instr-B.ndjson")" "1"

echo "== C. capture-env exit 3 must refuse unless forced =="
mk_env 3 true true available
rc=$(run C)
chk "exit code is 3 (refused)" "$rc" "3"
mk_env 3 true true available
rc=$(GBB_LADDER_OVERRIDE="1" run C2 GBB_FORCE_INVALID_HOST=1)
chk "forced run proceeds" "$rc" "0"
chk "forced_invalid_host in census" "$(grep -c 'forced_invalid_host' "$RES/census-instr-C2.ndjson")" "1"

echo "== D. clean host: the full sweep =="
mk_env 0 true true available
rc=$(run D)
chk "exit code is 0" "$rc" "0"
echo "  --- census statuses ---"
python3 - "$RES/census-instr-D.ndjson" <<'PY'
import json, collections, sys
c = collections.Counter()
for l in open(sys.argv[1]):
    r = json.loads(l)
    if r.get("record") != "arm_outcome": continue
    c[(r["library"], r["target"], r["coretype"], r["status"])] += 1
for k in sorted(c): print("     ", k, "x", c[k])
PY
chk "every census line is valid JSON" \
  "$(python3 -c 'import json,sys
n=0
for l in open(sys.argv[1]):
    json.loads(l); n+=1
print("ok")' "$RES/census-instr-D.ndjson")" "ok"
chk "every bench line is valid JSON" \
  "$(python3 -c 'import json,sys
for l in open(sys.argv[1]): json.loads(l)
print("ok")' "$RES/bench-instr-D.ndjson")" "ok"
chk "DYNAMIC_OMP reported build_failed not absent" \
  "$(python3 -c 'import json,sys
print(sum(1 for l in open(sys.argv[1]) for r in [json.loads(l)] if r.get("target")=="DYNAMIC_OMP" and r.get("status")=="build_failed")>0)' "$RES/census-instr-D.ndjson")" "True"
chk "ARMV8SVE reported unrunnable with a reason" \
  "$(python3 -c 'import json,sys
print(any(json.loads(l).get("target")=="ARMV8SVE" and json.loads(l).get("status")=="unrunnable" and json.loads(l).get("reason") for l in open(sys.argv[1])))' "$RES/census-instr-D.ndjson")" "True"
chk "NEOVERSEN2 (SIGILL in probe) unrunnable, never claimed" \
  "$(python3 -c 'import json,sys
rs=[json.loads(l) for l in open(sys.argv[1])]
print(any(r.get("coretype")=="NEOVERSEN2" and r.get("status")=="unrunnable" for r in rs))' "$RES/census-instr-D.ndjson")" "True"
chk "ARMV8SVE coretype rejected: request not honoured" \
  "$(python3 -c 'import json,sys
rs=[json.loads(l) for l in open(sys.argv[1])]
print(any(r.get("coretype")=="ARMV8SVE" and r.get("status")=="unrunnable"
          and "not a declared alias" in r.get("reason","") for r in rs))' "$RES/census-instr-D.ndjson")" "True"
chk "no bench record carries an unhonoured coretype" \
  "$(python3 -c 'import json,sys
bad=[]
for l in open(sys.argv[1]):
    r=json.loads(l)
    ct=r.get("coretype"); sel=r.get("arch_selected","")
    if ct and ct!="unforced" and ct.lower()!=sel.lower(): bad.append((ct,sel))
print(bad or "none")' "$RES/bench-instr-D.ndjson")" "none"
chk "OMP_PROC_BIND=false on every swept arm" \
  "$(python3 -c 'import json,sys
v={json.loads(l)["omp_proc_bind"] for l in open(sys.argv[1])}
print(sorted(v))' "$RES/bench-instr-D.ndjson")" "['false']"
chk "pin policy recorded per record" \
  "$(python3 -c 'import json,sys
v={json.loads(l)["pin_policy"] for l in open(sys.argv[1])}
print(sorted(v))' "$RES/bench-instr-D.ndjson")" \
  "['numactl --physcpubind=0 --membind=0;omp_bind=false', 'numactl --physcpubind=0-63 --membind=0;omp_bind=false']"
# The point is that no record inherits the gbb repo SHA or the string "unknown"
# in place of the library's own identity -- that conflation was the original bug.
chk "every record carries a real BLAS identity" \
  "$(python3 -c 'import json,re,sys
bad=[]
for l in open(sys.argv[1]):
    r=json.loads(l); s=r.get("blas_sha","")
    if not (re.fullmatch(r"[0-9a-f]{40}", s) or s.startswith("armpl-")): bad.append((r["library"],s))
print(bad or "none")' "$RES/bench-instr-D.ndjson")" "none"
chk "topology captured" "$( [ -s "$RES/topology-instr-D.txt" ] && echo yes || echo no )" "yes"

echo "== E. pinning arithmetic spans nodes correctly =="
rc=$(GBB_LADDER_OVERRIDE="1 96 128 192" run E)
chk "exit 0" "$rc" "0"
python3 - "$RES/bench-instr-E.ndjson" <<'PY'
import json, sys
seen = {}
for l in open(sys.argv[1]):
    r = json.loads(l); seen[r["threads"]] = r["pin_policy"]
for t in sorted(seen): print("     ", t, "->", seen[t])
PY
chk "96 threads stays on one node, membind" \
  "$(python3 -c 'import json,sys
print([json.loads(l)["pin_policy"] for l in open(sys.argv[1]) if json.loads(l)["threads"]==96][0])' "$RES/bench-instr-E.ndjson")" \
  "numactl --physcpubind=0-95 --membind=0;omp_bind=false"
chk "128 threads spans two nodes, interleave" \
  "$(python3 -c 'import json,sys
print([json.loads(l)["pin_policy"] for l in open(sys.argv[1]) if json.loads(l)["threads"]==128][0])' "$RES/bench-instr-E.ndjson")" \
  "numactl --physcpubind=0-127 --interleave=0,1;omp_bind=false"

echo "== F. coretype forcing unavailable: axis dropped, not faked =="
mk_env 0 true true unavailable
rc=$(GBB_LADDER_OVERRIDE="1" run F)
chk "exit 0" "$rc" "0"
chk "all coretypes marked unrunnable" \
  "$(python3 -c 'import json,sys
rs=[json.loads(l) for l in open(sys.argv[1])]
u=[r for r in rs if r.get("coretype") in ("ARMV8","NEOVERSEN1","ARMV8SVE","NEOVERSEV1","NEOVERSEV2","NEOVERSEN2")]
print(len(u)>0 and all(r["status"]=="unrunnable" for r in u))' "$RES/census-instr-F.ndjson")" "True"
chk "unforced DYNAMIC arm still ran" \
  "$(python3 -c 'import json,sys
print(any(json.loads(l).get("coretype")=="unforced" for l in open(sys.argv[1])))' "$RES/bench-instr-F.ndjson")" "True"

echo "== G. no-SVE host: SVE coretypes never requested =="
mk_env 0 false false available
rc=$(GBB_LADDER_OVERRIDE="1" run G)
chk "exit 0" "$rc" "0"
chk "no SVE coretype in the census at all" \
  "$(python3 -c 'import json,sys
rs=[json.loads(l) for l in open(sys.argv[1])]
print(sorted({r.get("coretype") for r in rs if r.get("coretype")}))' "$RES/census-instr-G.ndjson")" \
  "['ARMV8', 'NEOVERSEN1', 'unforced']"

echo "== H. affinity mask caps the ladder =="
mk_env 0 true true available 64
rc=$(unset GBB_LADDER_OVERRIDE; ( cd "$W" && PATH="$W/bin:$PATH" GBB_PREFIX="$W/libs" \
   GBB_RESULTS="$W/results" GBB_RUN_ID=H bash "$W/scripts/run-matrix.sh" ) 2>"$W/results/H.stderr"; echo $?)
chk "exit 0" "$rc" "0"
chk "ladder capped at 64" \
  "$(python3 -c 'import json,sys
print(sorted({json.loads(l)["threads"] for l in open(sys.argv[1])}))' "$RES/bench-instr-H.ndjson")" \
  "[1, 8, 16, 32, 64]"
chk "cap warning emitted" "$(grep -c 'capping the ladder' "$W/results/H.stderr")" "1"

echo "== M. a DECLARED alias runs; the record carries request and reported name =="
# This is the real 0.3.32 behaviour on Graviton 4/5: KERNEL.NEOVERSEV2 is a
# one-line include of KERNEL.NEOVERSEN2, so a NEOVERSEV2 request reports back
# `neoversen2`. That is the campaign's central assumption coming true, and an
# earlier version of run-matrix.sh recorded it as `unrunnable` -- the check meant
# to detect the aliasing was the thing suppressing it. NEOVERSEN2 requested after
# it must then be skipped as a duplicate, not measured twice.
cat > "$W/bin/gbb-coreprobe-DYNAMIC" <<'EOF'
#!/usr/bin/env bash
case "${OPENBLAS_CORETYPE:-}" in
  "")           echo "neoversen2|OpenBLAS 0.3.32 DYNAMIC_ARCH NEOVERSEN2" ;;
  NEOVERSEV2)   echo "neoversen2|OpenBLAS 0.3.32 DYNAMIC_ARCH NEOVERSEN2" ;;
  NEOVERSEN2)   echo "neoversen2|OpenBLAS 0.3.32 DYNAMIC_ARCH NEOVERSEN2" ;;
  ARMV8SVE)     echo "armv8sve|OpenBLAS 0.3.32 DYNAMIC_ARCH ARMV8SVE" ;;
  NEOVERSEV1)   echo "neoversev1|OpenBLAS 0.3.32 DYNAMIC_ARCH NEOVERSEV1" ;;
  ARMV8)        echo "armv8|OpenBLAS 0.3.32 DYNAMIC_ARCH ARMV8" ;;
  NEOVERSEN1)   echo "neoversen1|OpenBLAS 0.3.32 DYNAMIC_ARCH NEOVERSEN1" ;;
esac
EOF
chmod +x "$W/bin/gbb-coreprobe-DYNAMIC"
mk_env 0 true true available
rc=$(GBB_LADDER_OVERRIDE="1" run M)
chk "exit 0" "$rc" "0"
chk "NEOVERSEV2 ran, recorded as aliased not unrunnable" \
  "$(python3 -c 'import json,sys
rs=[json.loads(l) for l in open(sys.argv[1])]
print(sorted({r["status"] for r in rs if r.get("coretype")=="NEOVERSEV2"}))' "$RES/census-instr-M.ndjson")" \
  "['aliased', 'measured']"
chk "the aliased arm produced bench records" \
  "$(python3 -c 'import json,sys
print(sum(1 for l in open(sys.argv[1]) if json.loads(l).get("coretype")=="NEOVERSEV2"))' "$RES/bench-instr-M.ndjson")" \
  "1"
chk "record carries the reported name in arch_selected" \
  "$(python3 -c 'import json,sys
print(sorted({json.loads(l)["arch_selected"] for l in open(sys.argv[1]) if json.loads(l).get("coretype")=="NEOVERSEV2"}))' "$RES/bench-instr-M.ndjson")" \
  "['neoversen2']"
chk "NEOVERSEN2 skipped as an alias duplicate, with a reason" \
  "$(python3 -c 'import json,sys
rs=[json.loads(l) for l in open(sys.argv[1])]
print([ (r["status"], "NEOVERSEV2 arm" in r["reason"]) for r in rs if r.get("coretype")=="NEOVERSEN2" ])' "$RES/census-instr-M.ndjson")" \
  "[('alias_duplicate', True)]"
chk "the DYNAMIC kernel set is never measured twice under two names" \
  "$(python3 -c 'import json,sys
rs=[json.loads(l) for l in open(sys.argv[1])]
print(sum(1 for r in rs if r["target"]=="DYNAMIC" and r["arch_selected"]=="neoversen2"))' "$RES/bench-instr-M.ndjson")" \
  "2"
chk "unforced kept even though it lands on the same set" \
  "$(python3 -c 'import json,sys
rs=[json.loads(l) for l in open(sys.argv[1])]
print(sorted({r["coretype"] for r in rs if r["target"]=="DYNAMIC" and r["arch_selected"]=="neoversen2"}))' "$RES/bench-instr-M.ndjson")" \
  "['NEOVERSEV2', 'unforced']"
chk "arch_selected is n/a for ArmPL, not the DYNAMIC binary's selection" \
  "$(python3 -c 'import json,sys
print(sorted({json.loads(l)["arch_selected"] for l in open(sys.argv[1]) if json.loads(l)["library"]=="armpl"}))' "$RES/bench-instr-M.ndjson")" \
  "['n/a']"
chk "arch_selected is unprobed for a static build with no probe binary" \
  "$(python3 -c 'import json,sys
print(sorted({json.loads(l)["arch_selected"] for l in open(sys.argv[1]) if json.loads(l)["target"]=="NEOVERSEV1"}))' "$RES/bench-instr-M.ndjson")" \
  "['unprobed']"
mk_bins   # restore the default stubs for anything after this

echo
echo "== J. quarantine by construction: this host cannot produce campaign data =="
# The condition Scott set for running the harness on castor/pollux: instrument
# checks must be separated from campaign results by construction, not by
# remembering to pass the right flag. Three things have to hold at once, because
# any one of them alone is only a convention.
mk_env 0 true true available
rc=$(run J)
chk "exit 0" "$rc" "0"
chk "run_id is prefixed instr-" "$( [ -s "$RES/bench-instr-J.ndjson" ] && echo yes || echo no )" "yes"
chk "results/ itself has no bench file" \
  "$( ls "$W/results" | grep -c '^bench-' || true )" "0"
chk "results/ itself has no census file" \
  "$( ls "$W/results" | grep -c '^census-' || true )" "0"
chk "every bench record says role=instrument" \
  "$(python3 -c 'import json,sys
print(sorted({json.loads(l)["role"] for l in open(sys.argv[1])}))' "$RES/bench-instr-J.ndjson")" \
  "['instrument']"
chk "every census record says role=instrument" \
  "$(python3 -c 'import json,sys
print(sorted({json.loads(l).get("role") for l in open(sys.argv[1])}))' "$RES/census-instr-J.ndjson")" \
  "['instrument']"
chk "run_id inside the records carries the prefix" \
  "$(python3 -c 'import json,sys
print(sorted({json.loads(l)["run_id"] for l in open(sys.argv[1])}))' "$RES/bench-instr-J.ndjson")" \
  "['instr-J']"

echo "== K. GBB_ROLE is an assertion, not an override =="
# Asserting campaign on a host the evidence says is not one must stop the run.
# If this ever passes instead of dying, the quarantine is back to being a matter
# of discipline.
rc=$(run K GBB_ROLE=campaign)
chk "exit 3 (refused)" "$rc" "3"
chk "says it is an assertion, not an override" \
  "$(grep -c 'assertion, not an override' "$W/results/K.stderr")" "1"
chk "no campaign-namespace records written" \
  "$( ls "$W/results" | grep -c '^bench-' || true )" "0"
chk "no instrument records under the asserted role either" \
  "$( [ -e "$RES/bench-instr-K.ndjson" ] && echo some || echo none )" "none"

echo "== L. a forged instance type cannot promote a non-Graviton host =="
# GBB_TEST_IMDS_TYPE exists so this code path is reachable without EC2. It must
# not be sufficient on its own: cpu0's MIDR still has to be a Graviton part, and
# on the machine running this test it is not.
rc=$(GBB_LADDER_OVERRIDE="1" run L GBB_TEST_IMDS_TYPE=c8g.metal-48xl)
chk "exit 0" "$rc" "0"
chk "still instrument" \
  "$(python3 -c 'import json,sys
print(sorted({json.loads(l)["role"] for l in open(sys.argv[1])}))' "$RES/bench-instr-L.ndjson")" \
  "['instrument']"
chk "reason names the MIDR, not the instance type" \
  "$(grep -c 'is not a known Graviton part\|MIDR unreadable' "$W/results/L.stderr")" "1"

echo "== M2. the manifest is stamped with a host, not merely copied =="
# build-libs.sh runs before anything knows which host it is on, so its records
# carry no instance. The analysis concatenates every host's manifest into one
# stream, where "this build has no SVE kernels" is unattributable and therefore
# unactionable -- so run-matrix.sh stamps instance and role on the way in. The
# stamp is a sed insertion after the opening brace, so "still valid JSON" is the
# assertion that matters most here.
mk_manifest
rc=$(GBB_LADDER_OVERRIDE="1" run M2)
chk "exit 0" "$rc" "0"
chk "every stamped manifest line is still valid JSON" \
  "$(python3 -c 'import json,sys
print(all(isinstance(json.loads(l),dict) for l in open(sys.argv[1]) if l.strip()))' \
     "$RES/manifest-instr-M2.ndjson")" "True"
chk "every line carries the role" \
  "$(python3 -c 'import json,sys
print(sorted({json.loads(l)["role"] for l in open(sys.argv[1]) if l.strip()}))' \
     "$RES/manifest-instr-M2.ndjson")" "['instrument']"
chk "every line carries an instance, including the toolchain record" \
  "$(python3 -c 'import json,sys
print(all("instance" in json.loads(l) for l in open(sys.argv[1]) if l.strip()))' \
     "$RES/manifest-instr-M2.ndjson")" "True"
chk "sve_kernels survives the stamp" \
  "$(python3 -c 'import json,sys
d={(r.get("library"),r.get("target")):r.get("sve_kernels") for r in map(json.loads,open(sys.argv[1])) if r.get("record")=="arm"}
print(d.get(("openblas","DYNAMIC")), d.get(("openblas","NEOVERSEV1")), d.get(("armpl","native")))' \
     "$RES/manifest-instr-M2.ndjson")" "yes no n/a"

echo
echo "== missing build manifest =="
rm -f "$W/libs/build-manifest.ndjson"
mk_env 0 true true available
rc=$(run I)
chk "exit 3, tells you to run build-libs.sh" "$rc" "3"
chk "message names build-libs.sh" "$(grep -c 'build-libs.sh first' "$W/results/I.stderr")" "1"

echo
printf 'pass=%d fail=%d\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
