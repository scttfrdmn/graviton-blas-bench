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
T="${GBB_TEST_TMP:-/tmp/gbb-test}"
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
printf '"thread_backend":"%s","pin_policy":"%s","arch_selected":"%s","threads":%s,' \
  "$GBB_THREAD_BACKEND" "$GBB_PIN_POLICY" "$GBB_ARCH_SELECTED" "$GBB_THREADS"
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
{"record":"arm","library":"openblas","target":"DYNAMIC","coretype":null,"blas_sha":"cc3fc1eaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","built":true,"runnable":true,"reason":"","thread_backend":"pthreads","exe":"gbb-openblas-DYNAMIC","prefix":"/libs/openblas-DYNAMIC"}
{"record":"arm","library":"openblas","target":"DYNAMIC_OMP","coretype":null,"blas_sha":"cc3fc1eaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","built":false,"runnable":true,"reason":"build failed, see openblas-DYNAMIC_OMP.buildlog","thread_backend":"openmp","exe":"gbb-openblas-DYNAMIC_OMP","prefix":""}
{"record":"arm","library":"openblas","target":"NEOVERSEV1","coretype":null,"blas_sha":"cc3fc1eaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","built":true,"runnable":true,"reason":"","thread_backend":"pthreads","exe":"gbb-openblas-NEOVERSEV1","prefix":"/libs/openblas-NEOVERSEV1"}
{"record":"arm","library":"openblas","target":"ARMV8SVE","coretype":null,"blas_sha":"cc3fc1eaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","built":true,"runnable":false,"reason":"target requires sve which this host does not report","thread_backend":"pthreads","exe":"gbb-openblas-ARMV8SVE","prefix":""}
{"record":"arm","library":"armpl","target":"native","coretype":null,"blas_sha":"armpl-armpl_24.10_gcc","built":true,"runnable":true,"reason":"","thread_backend":"openmp","exe":"gbb-armpl","prefix":"/opt/arm"}
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

pass=0; fail=0
chk() { if [ "$2" = "$3" ]; then pass=$((pass+1)); printf '  ok   %s\n' "$1";
        else fail=$((fail+1)); printf '  FAIL %s\n       want=%s\n       got =%s\n' "$1" "$3" "$2"; fi; }

mk_topo; mk_bins; mk_manifest

echo "== A. capture-env exit 4 must refuse to sweep =="
mk_env 4 true true available
rc=$(run A)
chk "exit code is 3 (refused)" "$rc" "3"
chk "no bench records written" "$( [ -s "$W/results/bench-A.ndjson" ] && echo some || echo none )" "none"
chk "stderr names standing order 8" "$(grep -c 'standing order 8' "$W/results/A.stderr")" "1"

echo "== B. capture-env exit 4 + GBB_ESCALATION_ACK proceeds and records the ack =="
rc=$(GBB_LADDER_OVERRIDE="1" run B GBB_ESCALATION_ACK="Scott confirmed 0xd84 is expected on c9g")
chk "exit code is 0" "$rc" "0"
chk "escalation_ack recorded" "$(grep -c '"record":"escalation_ack"' "$W/results/census-B.ndjson")" "1"

echo "== C. capture-env exit 3 must refuse unless forced =="
mk_env 3 true true available
rc=$(run C)
chk "exit code is 3 (refused)" "$rc" "3"
mk_env 3 true true available
rc=$(GBB_LADDER_OVERRIDE="1" run C2 GBB_FORCE_INVALID_HOST=1)
chk "forced run proceeds" "$rc" "0"
chk "forced_invalid_host in census" "$(grep -c 'forced_invalid_host' "$W/results/census-C2.ndjson")" "1"

echo "== D. clean host: the full sweep =="
mk_env 0 true true available
rc=$(run D)
chk "exit code is 0" "$rc" "0"
echo "  --- census statuses ---"
python3 - "$W/results/census-D.ndjson" <<'PY'
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
print("ok")' "$W/results/census-D.ndjson")" "ok"
chk "every bench line is valid JSON" \
  "$(python3 -c 'import json,sys
for l in open(sys.argv[1]): json.loads(l)
print("ok")' "$W/results/bench-D.ndjson")" "ok"
chk "DYNAMIC_OMP reported build_failed not absent" \
  "$(python3 -c 'import json,sys
print(sum(1 for l in open(sys.argv[1]) for r in [json.loads(l)] if r.get("target")=="DYNAMIC_OMP" and r.get("status")=="build_failed")>0)' "$W/results/census-D.ndjson")" "True"
chk "ARMV8SVE reported unrunnable with a reason" \
  "$(python3 -c 'import json,sys
print(any(json.loads(l).get("target")=="ARMV8SVE" and json.loads(l).get("status")=="unrunnable" and json.loads(l).get("reason") for l in open(sys.argv[1])))' "$W/results/census-D.ndjson")" "True"
chk "NEOVERSEN2 (SIGILL in probe) unrunnable, never claimed" \
  "$(python3 -c 'import json,sys
rs=[json.loads(l) for l in open(sys.argv[1])]
print(any(r.get("coretype")=="NEOVERSEN2" and r.get("status")=="unrunnable" for r in rs))' "$W/results/census-D.ndjson")" "True"
chk "ARMV8SVE coretype rejected: request not honoured" \
  "$(python3 -c 'import json,sys
rs=[json.loads(l) for l in open(sys.argv[1])]
print(any(r.get("coretype")=="ARMV8SVE" and r.get("status")=="unrunnable"
          and "the label would be false" in r.get("reason","") for r in rs))' "$W/results/census-D.ndjson")" "True"
chk "no bench record carries an unhonoured coretype" \
  "$(python3 -c 'import json,sys
bad=[]
for l in open(sys.argv[1]):
    r=json.loads(l)
    ct=r.get("coretype"); sel=r.get("arch_selected","")
    if ct and ct!="unforced" and ct.lower()!=sel.lower(): bad.append((ct,sel))
print(bad or "none")' "$W/results/bench-D.ndjson")" "none"
chk "OMP_PROC_BIND=false on every swept arm" \
  "$(python3 -c 'import json,sys
v={json.loads(l)["omp_proc_bind"] for l in open(sys.argv[1])}
print(sorted(v))' "$W/results/bench-D.ndjson")" "['false']"
chk "pin policy recorded per record" \
  "$(python3 -c 'import json,sys
v={json.loads(l)["pin_policy"] for l in open(sys.argv[1])}
print(sorted(v))' "$W/results/bench-D.ndjson")" \
  "['numactl --physcpubind=0 --membind=0;omp_bind=false', 'numactl --physcpubind=0-63 --membind=0;omp_bind=false']"
# The point is that no record inherits the gbb repo SHA or the string "unknown"
# in place of the library's own identity -- that conflation was the original bug.
chk "every record carries a real BLAS identity" \
  "$(python3 -c 'import json,re,sys
bad=[]
for l in open(sys.argv[1]):
    r=json.loads(l); s=r.get("blas_sha","")
    if not (re.fullmatch(r"[0-9a-f]{40}", s) or s.startswith("armpl-")): bad.append((r["library"],s))
print(bad or "none")' "$W/results/bench-D.ndjson")" "none"
chk "topology captured" "$( [ -s "$W/results/topology-D.txt" ] && echo yes || echo no )" "yes"

echo "== E. pinning arithmetic spans nodes correctly =="
rc=$(GBB_LADDER_OVERRIDE="1 96 128 192" run E)
chk "exit 0" "$rc" "0"
python3 - "$W/results/bench-E.ndjson" <<'PY'
import json, sys
seen = {}
for l in open(sys.argv[1]):
    r = json.loads(l); seen[r["threads"]] = r["pin_policy"]
for t in sorted(seen): print("     ", t, "->", seen[t])
PY
chk "96 threads stays on one node, membind" \
  "$(python3 -c 'import json,sys
print([json.loads(l)["pin_policy"] for l in open(sys.argv[1]) if json.loads(l)["threads"]==96][0])' "$W/results/bench-E.ndjson")" \
  "numactl --physcpubind=0-95 --membind=0;omp_bind=false"
chk "128 threads spans two nodes, interleave" \
  "$(python3 -c 'import json,sys
print([json.loads(l)["pin_policy"] for l in open(sys.argv[1]) if json.loads(l)["threads"]==128][0])' "$W/results/bench-E.ndjson")" \
  "numactl --physcpubind=0-127 --interleave=0,1;omp_bind=false"

echo "== F. coretype forcing unavailable: axis dropped, not faked =="
mk_env 0 true true unavailable
rc=$(GBB_LADDER_OVERRIDE="1" run F)
chk "exit 0" "$rc" "0"
chk "all coretypes marked unrunnable" \
  "$(python3 -c 'import json,sys
rs=[json.loads(l) for l in open(sys.argv[1])]
u=[r for r in rs if r.get("coretype") in ("ARMV8","NEOVERSEN1","ARMV8SVE","NEOVERSEV1","NEOVERSEV2","NEOVERSEN2")]
print(len(u)>0 and all(r["status"]=="unrunnable" for r in u))' "$W/results/census-F.ndjson")" "True"
chk "unforced DYNAMIC arm still ran" \
  "$(python3 -c 'import json,sys
print(any(json.loads(l).get("coretype")=="unforced" for l in open(sys.argv[1])))' "$W/results/bench-F.ndjson")" "True"

echo "== G. no-SVE host: SVE coretypes never requested =="
mk_env 0 false false available
rc=$(GBB_LADDER_OVERRIDE="1" run G)
chk "exit 0" "$rc" "0"
chk "no SVE coretype in the census at all" \
  "$(python3 -c 'import json,sys
rs=[json.loads(l) for l in open(sys.argv[1])]
print(sorted({r.get("coretype") for r in rs if r.get("coretype")}))' "$W/results/census-G.ndjson")" \
  "['ARMV8', 'NEOVERSEN1', 'unforced']"

echo "== H. affinity mask caps the ladder =="
mk_env 0 true true available 64
rc=$(unset GBB_LADDER_OVERRIDE; ( cd "$W" && PATH="$W/bin:$PATH" GBB_PREFIX="$W/libs" \
   GBB_RESULTS="$W/results" GBB_RUN_ID=H bash "$W/scripts/run-matrix.sh" ) 2>"$W/results/H.stderr"; echo $?)
chk "exit 0" "$rc" "0"
chk "ladder capped at 64" \
  "$(python3 -c 'import json,sys
print(sorted({json.loads(l)["threads"] for l in open(sys.argv[1])}))' "$W/results/bench-H.ndjson")" \
  "[1, 8, 16, 32, 64]"
chk "cap warning emitted" "$(grep -c 'capping the ladder' "$W/results/H.stderr")" "1"

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
