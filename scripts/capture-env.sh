#!/usr/bin/env bash
# graviton-blas-bench — capture everything about this host that could explain a number.
#
# Emits one JSON object on stdout. Run once per instance, before any timing.
# The MIDR part number and the OpenBLAS runtime core selection are the two
# fields the analysis actually branches on; the rest is for post-hoc "why is
# this arm weird" archaeology.
#
# Nothing here is ever guessed. If a file cannot be read the field is null or
# empty and the reason lands in the JSON `warnings` array. A plausible default
# substituted for a missing measurement would be a fabricated measurement.
#
# EXIT CODES — standing order 8 requires this script to be able to *stop* a run,
# so the exit status is load-bearing. Callers launching a multi-hour sweep must
# check it before spending instance-hours:
#
#   0  clean (or --warn-only was given, which always exits 0)
#   1  usage error
#   3  run-invalidating: the timings from this host would not be comparable.
#        - cpufreq governor is present and is not `performance`
#        - SMT is on (threads-per-core != 1)
#        - heterogeneous cores: more than one distinct MIDR on this host
#   4  escalate to a human before running anything (standing order 8). This
#      outweighs 3, so 4 wins when both apply.
#        - unrecognised MIDR part number (not in OpenBLAS dynamic_arm64.c)
#        - DYNAMIC_ARCH selected generic ARMV8 on a host that HAS SVE, i.e.
#          SVE detection failed
#
#   --warn-only  print the JSON and the warnings, then exit 0 regardless.
#
# Every warning is written to stderr *and* into the JSON `warnings` array. A
# backgrounded sweep sends stderr to scrollback nobody reads, and the instance
# is terminated on completion; a warning that exists only on a dead console is
# not provenance.

set -uo pipefail

WARN_ONLY=0
for arg in "$@"; do
  case "$arg" in
    --warn-only) WARN_ONLY=1 ;;
    -h|--help)
      sed -n '2,36p' "$0"
      exit 0
      ;;
    *)
      echo "gbb: usage: $0 [--warn-only]" >&2
      exit 1
      ;;
  esac
done

# ---- JSON emission helpers -------------------------------------------------
# Everything on stdout must stay a single valid JSON object: no trailing
# commas, no unescaped quotes or backslashes, no newlines inside strings.
sanitize() {
  local v="${1:-}"
  v="${v//\\/}"
  v="${v//\"/}"
  v="${v//$'\t'/ }"
  v="${v//$'\r'/ }"
  printf '%s' "${v//$'\n'/ }"
}
j() { printf '"%s":%s' "$1" "$2"; }
s() { printf '"%s":"%s"' "$1" "$(sanitize "${2:-}")"; }
# String field that becomes JSON null when we have no measurement for it.
sn() {
  if [ -n "${2:-}" ]; then s "$1" "$2"; else printf '"%s":null' "$1"; fi
}
# Numeric field; anything that is not a number becomes null rather than
# breaking the object or being rounded into a lie.
jnum() {
  if printf '%s' "${2:-}" | grep -Eq '^-?[0-9]+(\.[0-9]+)?$'; then
    printf '"%s":%s' "$1" "$2"
  else
    printf '"%s":null' "$1"
  fi
}
jbool() {
  case "${2:-}" in
    true|false) printf '"%s":%s' "$1" "$2" ;;
    *)          printf '"%s":null' "$1" ;;
  esac
}
# Echo $1 if it is a non-negative integer, else the fallback $2. Used only for
# the pre-existing lscpu-derived fields whose consumers assume a number; when
# the fallback is taken a warning records that the value is a default and not a
# measurement.
num_or() {
  if printf '%s' "${1:-}" | grep -Eq '^[0-9]+$'; then printf '%s' "$1"; else printf '%s' "$2"; fi
}
# Array of strings, e.g. warnings and the distinct MIDR list.
jstrarray() {
  local name="$1" v i
  shift
  printf '"%s":[' "$name"
  i=0
  for v in "$@"; do
    [ "$i" -eq 0 ] || printf ','
    printf '"%s"' "$(sanitize "$v")"
    i=$((i + 1))
  done
  printf ']'
}

# ---- warnings --------------------------------------------------------------
WARNINGS=()
EXIT_CODE=0
# warn <level> <message>: level 0 informational, 3 run-invalidating,
# 4 escalate-to-human. 4 beats 3; neither is ever downgraded.
warn() {
  local level="$1"
  shift
  WARNINGS+=("$*")
  echo "gbb: WARNING: $*" >&2
  case "$level:$EXIT_CODE" in
    4:*)   EXIT_CODE=4 ;;
    3:0|3:3) EXIT_CODE=3 ;;
  esac
  return 0
}

IMDS_TOKEN="$(curl -sS -m 2 -X PUT http://169.254.169.254/latest/api/token \
  -H 'X-aws-ec2-metadata-token-ttl-seconds: 60' 2>/dev/null || true)"
imds() {
  [ -n "$IMDS_TOKEN" ] || return 0
  local v
  v="$(curl -sS -m 2 -H "X-aws-ec2-metadata-token: $IMDS_TOKEN" \
       "http://169.254.169.254/latest/meta-data/$1" 2>/dev/null || true)"
  # IMDS values are short and space-free. Anything else is a proxy or error
  # page, and must not be laundered into the provenance record as if it were
  # an instance type.
  case "$v" in
    ""|*" "*|*"<"*) return 0 ;;
  esac
  [ ${#v} -le 64 ] && printf '%s' "$v"
}

INSTANCE_TYPE="$(imds instance-type)"
INSTANCE_ID="$(imds instance-id)"
AZ="$(imds placement/availability-zone)"

# ---- MIDR, every core -----------------------------------------------------
# MIDR is the field OpenBLAS dispatches on. Reading cpu0 alone labels the host
# from one core and silently assumes every core is identical -- standing order
# 5 wants "all cores are identical" to be a recorded fact, not an assumption.
# A big.LITTLE host with interleaved clusters (e.g. X925 on cpus 5-9,15-19 and
# A725 on cpus 0-4,10-14) makes OMP_PLACES=cores report a blend of two
# microarchitectures as one number.
decode_part() {
  case "${1:-}" in
    0xd0c) printf 'Neoverse N1' ;;
    0xd40) printf 'Neoverse V1' ;;
    0xd49) printf 'Neoverse N2' ;;
    0xd4f) printf 'Neoverse V2' ;;
    0xd83) printf 'Neoverse V3AE' ;;
    0xd84) printf 'Neoverse V3' ;;
    0xd85) printf 'Cortex-X925' ;;
    0xd87) printf 'Cortex-A725' ;;
    # decompose.py tests for this exact token. Do not reword it.
    *)     printf 'UNRECOGNISED' ;;
  esac
}
part_of() { printf '0x%x' $(( ( ${1} >> 4 ) & 0xFFF )); }
impl_of() { printf '0x%x' $(( ( ${1} >> 24 ) & 0xFF )); }

# "0-4,10-14" from a sorted ascending list of integers on stdin.
compress_cpus() {
  awk '
    function emit() { printf "%s%s", (n++ ? "," : ""), (start == prev ? start : start "-" prev) }
    NR == 1 { start = $1; prev = $1; next }
    $1 == prev + 1 { prev = $1; next }
    { emit(); start = $1; prev = $1 }
    END { if (NR > 0) emit() }
  '
}

# Count the CPUs in a kernel-style range list such as "0-3,8,10-11".
count_cpu_list() {
  [ -n "${1:-}" ] || return 0
  printf '%s' "$1" | awk -F, '{
    n = 0
    for (i = 1; i <= NF; i++) {
      k = split($i, a, "-")
      n += (k == 2 ? a[2] - a[1] + 1 : 1)
    }
    print n
  }'
}

# Only online CPUs expose regs/identification/midr_el1, so the glob is already
# the online set; unreadable entries are skipped rather than filled in.
list_cpu_midrs() {
  local d n m
  for d in /sys/devices/system/cpu/cpu[0-9]*; do
    [ -d "$d" ] || continue
    n="${d##*/cpu}"
    case "$n" in *[!0-9]*) continue ;; esac
    m="$(cat "$d/regs/identification/midr_el1" 2>/dev/null || true)"
    [ -n "$m" ] || continue
    printf '%s %s\n' "$n" "$m"
  done
}

CPU_IDS=()
CPU_MIDRS=()
while read -r cpu_n cpu_m; do
  [ -n "${cpu_n:-}" ] || continue
  CPU_IDS+=("$cpu_n")
  CPU_MIDRS+=("$cpu_m")
done < <(list_cpu_midrs | sort -n -k1,1)

MIDR_SEEN=${#CPU_IDS[@]}

# Distinct MIDRs, in first-seen (lowest cpu) order.
MIDR_DISTINCT=()
for m in ${CPU_MIDRS[@]+"${CPU_MIDRS[@]}"}; do
  seen=0
  for k in ${MIDR_DISTINCT[@]+"${MIDR_DISTINCT[@]}"}; do
    [ "$k" = "$m" ] && { seen=1; break; }
  done
  [ "$seen" -eq 1 ] || MIDR_DISTINCT+=("$m")
done

# Per-cluster: cpu list, part, decoded name, and that cluster's max frequency.
CL_MIDR=()
CL_PART=()
CL_NAME=()
CL_CPUS=()
CL_COUNT=()
CL_MAXFREQ=()
for m in ${MIDR_DISTINCT[@]+"${MIDR_DISTINCT[@]}"}; do
  raw=""
  count=0
  freq=""
  i=0
  while [ "$i" -lt "$MIDR_SEEN" ]; do
    if [ "${CPU_MIDRS[$i]}" = "$m" ]; then
      raw="$raw${CPU_IDS[$i]}
"
      count=$((count + 1))
      if [ -z "$freq" ]; then
        freq="$(cat "/sys/devices/system/cpu/cpu${CPU_IDS[$i]}/cpufreq/cpuinfo_max_freq" 2>/dev/null || true)"
      fi
    fi
    i=$((i + 1))
  done
  part="$(part_of "$m")"
  CL_MIDR+=("$m")
  CL_PART+=("$part")
  CL_NAME+=("$(decode_part "$part")")
  CL_CPUS+=("$(printf '%s' "$raw" | compress_cpus)")
  CL_COUNT+=("$count")
  CL_MAXFREQ+=("$freq")
done

# Scalar MIDR fields are cpu0's, kept for backward compatibility with
# decompose.py. midr_scalar_source says so explicitly so no reader mistakes
# them for a property of the host; core_clusters is the host-level truth.
MIDR="$(cat /sys/devices/system/cpu/cpu0/regs/identification/midr_el1 2>/dev/null || true)"
if [ -n "$MIDR" ]; then
  MIDR_INT=$((MIDR))
  IMPL="$(impl_of "$MIDR_INT")"
  PART="$(part_of "$MIDR_INT")"
else
  IMPL=""
  PART=""
fi
if [ -n "$MIDR" ]; then
  CORE_NAME="$(decode_part "$PART")"
else
  # No MIDR to decode. "UNRECOGNISED" here would assert that a part number was
  # read and not found in OpenBLAS's switch, which is a claim we cannot make.
  CORE_NAME=""
fi

if [ "$MIDR_SEEN" -eq 0 ]; then
  MIDR_UNIFORM=""
  warn 0 "MIDR is not readable on this host (no cpu*/regs/identification/midr_el1). Core identity is UNVERIFIED: midr_uniform, core_clusters and core_name are not measurements here."
elif [ "${#MIDR_DISTINCT[@]}" -eq 1 ]; then
  MIDR_UNIFORM=true
else
  MIDR_UNIFORM=false
fi

emit_core_clusters() {
  local i=0
  printf '"core_clusters":['
  while [ "$i" -lt "${#CL_MIDR[@]}" ]; do
    [ "$i" -eq 0 ] || printf ','
    printf '{'
    s midr "${CL_MIDR[$i]}"; printf ','
    s midr_part "${CL_PART[$i]}"; printf ','
    s core_name "${CL_NAME[$i]}"; printf ','
    s cpus "${CL_CPUS[$i]}"; printf ','
    jnum cpu_count "${CL_COUNT[$i]}"; printf ','
    jnum cpuinfo_max_freq_khz "${CL_MAXFREQ[$i]}"
    printf '}'
    i=$((i + 1))
  done
  printf ']'
}

FLAGS="$(grep -m1 '^Features' /proc/cpuinfo 2>/dev/null | cut -d: -f2- | xargs || true)"
has() { printf '%s' " $FLAGS " | grep -qw "$1" && echo true || echo false; }

HAS_SVE="$(has sve)"
HAS_SVE2="$(has sve2)"
HAS_BF16="$(has bf16)"
HAS_I8MM="$(has i8mm)"
HAS_SME="$(has sme)"

# Runtime SVE vector length in bytes. This is the campaign's central axis --
# SVE1 VL=256b on Graviton3 vs SVE2 VL=128b on Graviton4/5 -- so it is recorded
# unconditionally, read from the kernel rather than inferred from the part
# number. Note the kernel exposes a *default* VL that a process can shrink with
# prctl(PR_SVE_SET_VL); this is the value an unmodified arm will see.
SVE_VL="$(cat /proc/sys/abi/sve_default_vector_length 2>/dev/null || true)"
if [ "$HAS_SVE" = true ] && [ -z "$SVE_VL" ]; then
  warn 0 "host reports SVE in cpuinfo Features but /proc/sys/abi/sve_default_vector_length is unreadable; runtime vector length is unrecorded for this host."
fi
if [ -z "$FLAGS" ]; then
  warn 0 "/proc/cpuinfo Features is unreadable; has_sve/has_sve2/has_bf16/has_i8mm are false because nothing was measured, not because the ISA is absent."
fi

NUMA_NODES_RAW="$(lscpu 2>/dev/null | awk -F: '/NUMA node\(s\)/{gsub(/ /,"",$2);print $2}')"
SOCKETS_RAW="$(lscpu 2>/dev/null | awk -F: '/^Socket\(s\)/{gsub(/ /,"",$2);print $2}')"
TPC_RAW="$(lscpu 2>/dev/null | awk -F: '/Thread\(s\) per core/{gsub(/ /,"",$2);print $2}')"
CORES_TOTAL="$(num_or "$(nproc 2>/dev/null || true)" 0)"
# These three keep their historical default of 1 because decompose.py treats a
# missing threads_per_core as 1 and would otherwise report phantom SMT. When a
# default is used it is stated in `warnings` rather than passed off as measured.
NUMA_NODES="$(num_or "$NUMA_NODES_RAW" 1)"
SOCKETS="$(num_or "$SOCKETS_RAW" 1)"
THREADS_PER_CORE="$(num_or "$TPC_RAW" 1)"
if [ -z "$NUMA_NODES_RAW$SOCKETS_RAW$TPC_RAW" ]; then
  warn 0 "lscpu produced no topology (not installed, or not Linux): sockets, numa_nodes and threads_per_core are defaulted to 1 and are NOT measurements on this host."
fi
L1D="$(lscpu 2>/dev/null | awk -F: '/L1d cache/{gsub(/^ +/,"",$2);print $2}')"
L2="$(lscpu  2>/dev/null | awk -F: '/L2 cache/{gsub(/^ +/,"",$2);print $2}')"
L3="$(lscpu  2>/dev/null | awk -F: '/L3 cache/{gsub(/^ +/,"",$2);print $2}')"

# ---- what this process may actually run on ---------------------------------
# In a container or under a CPU quota the visible CPU count is not the usable
# CPU count, and every thread-scaling number computed from the wrong one is
# wrong. /sys reports the machine; sched_getaffinity reports us.
CPUS_ONLINE_LIST="$(cat /sys/devices/system/cpu/online 2>/dev/null || true)"
CPUS_ONLINE="$(count_cpu_list "$CPUS_ONLINE_LIST")"
if [ -z "$CPUS_ONLINE" ] && [ "$MIDR_SEEN" -gt 0 ]; then
  CPUS_ONLINE="$MIDR_SEEN"
fi

CPUS_AFFINITY_LIST="$(awk '/^Cpus_allowed_list:/{print $2}' /proc/self/status 2>/dev/null || true)"
CPUS_AFFINITY="$(count_cpu_list "$CPUS_AFFINITY_LIST")"
[ -n "$CPUS_AFFINITY" ] || CPUS_AFFINITY="$CORES_TOTAL"

if [ -n "$CPUS_AFFINITY" ] && [ -n "$CPUS_ONLINE" ] && [ "$CPUS_AFFINITY" != "$CPUS_ONLINE" ]; then
  warn 0 "sched_getaffinity allows $CPUS_AFFINITY CPUs but $CPUS_ONLINE are online (allowed list: ${CPUS_AFFINITY_LIST:-unknown}). Thread-scaling arms above $CPUS_AFFINITY threads will oversubscribe; treat this host's scaling curve as suspect."
fi

CGROUP_VERSION="none"
CGROUP_CPU_MAX=""
CGROUP_QUOTA_US=""
CGROUP_PERIOD_US=""
CGROUP_CPU_LIMIT=""
CG2_PATH="$(awk -F: '/^0::/{print $3}' /proc/self/cgroup 2>/dev/null || true)"
for p in "/sys/fs/cgroup${CG2_PATH}/cpu.max" "/sys/fs/cgroup/cpu.max"; do
  [ -r "$p" ] || continue
  CGROUP_CPU_MAX="$(cat "$p" 2>/dev/null || true)"
  [ -n "$CGROUP_CPU_MAX" ] || continue
  CGROUP_VERSION="v2"
  # cpu.max is "<quota|max> <period>", e.g. "200000 100000" or "max 100000".
  read -r cg_quota cg_period <<<"$CGROUP_CPU_MAX"
  if [ -n "${cg_quota:-}" ] && [ "$cg_quota" != "max" ]; then
    CGROUP_QUOTA_US="$cg_quota"
    CGROUP_PERIOD_US="${cg_period:-}"
  fi
  break
done
if [ "$CGROUP_VERSION" = "none" ]; then
  CG1_PATH="$(awk -F: '$2 ~ /(^|,)cpu(,|$)/ {print $3}' /proc/self/cgroup 2>/dev/null | head -1 || true)"
  for p in "/sys/fs/cgroup/cpu${CG1_PATH}" "/sys/fs/cgroup/cpu,cpuacct${CG1_PATH}" \
           /sys/fs/cgroup/cpu /sys/fs/cgroup/cpu,cpuacct; do
    [ -r "$p/cpu.cfs_quota_us" ] && [ -r "$p/cpu.cfs_period_us" ] || continue
    CGROUP_VERSION="v1"
    q="$(cat "$p/cpu.cfs_quota_us" 2>/dev/null || true)"
    pe="$(cat "$p/cpu.cfs_period_us" 2>/dev/null || true)"
    if [ -n "$q" ] && [ "$q" != "-1" ]; then
      CGROUP_QUOTA_US="$q"
      CGROUP_PERIOD_US="$pe"
    fi
    break
  done
fi
if [ -n "$CGROUP_QUOTA_US" ] && [ -n "$CGROUP_PERIOD_US" ] && [ "$CGROUP_PERIOD_US" -gt 0 ] 2>/dev/null; then
  CGROUP_CPU_LIMIT="$(awk -v q="$CGROUP_QUOTA_US" -v p="$CGROUP_PERIOD_US" 'BEGIN{printf "%.3f", q/p}')"
  warn 0 "a cgroup CPU quota is in effect (${CGROUP_VERSION}: quota ${CGROUP_QUOTA_US}us / period ${CGROUP_PERIOD_US}us = ${CGROUP_CPU_LIMIT} CPUs). Wall-clock GFLOP/s from this host is throttled and is not a hardware measurement."
fi

# Fixed-frequency check. Graviton has no turbo, so scaling_cur_freq should be
# constant. If a governor is present and not "performance", say so loudly --
# it would invalidate every timing on this host.
GOV="$(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null || echo 'none')"
CUR_FREQ="$(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq 2>/dev/null || true)"

# ---- what DYNAMIC_ARCH OpenBLAS actually picks here ------------------------
# This is a finding, not bookkeeping. Three states are kept distinct, because
# "the probe never ran" and "the probe says generic ARMV8" must never be
# confused: openblas_dynamic_probe_status is one of
#   not_attempted | build_failed | run_failed | ok
# and openblas_dynamic_selection is null unless status is ok.
OB_CORE=""
OB_CONFIG=""
OB_STATUS="not_attempted"
OB_FORCING="not_probed"
if [ -n "${GBB_OPENBLAS_DYNAMIC_DIR:-}" ] && [ -d "$GBB_OPENBLAS_DYNAMIC_DIR" ]; then
  OB_STATUS="build_failed"
  TMPC="$(mktemp /tmp/gblas-obcore-XXXX.c)"; TMPB="${TMPC%.c}"
  cat > "$TMPC" <<'EOF'
#include <stdio.h>
char *openblas_get_corename(void);
char *openblas_get_config(void);
int main(void){ printf("%s|%s\n", openblas_get_corename(), openblas_get_config()); return 0; }
EOF
  if gcc -O0 "$TMPC" -o "$TMPB" -L"$GBB_OPENBLAS_DYNAMIC_DIR/lib" -lopenblas \
       -Wl,-rpath,"$GBB_OPENBLAS_DYNAMIC_DIR/lib" 2>/dev/null; then
    OB_STATUS="run_failed"
    OB_OUT="$("$TMPB" 2>/dev/null || true)"
    case "$OB_OUT" in
      *"|"*)
        OB_CORE="${OB_OUT%%|*}"
        OB_CONFIG="${OB_OUT#*|}"
        OB_STATUS="ok"
        ;;
    esac
    if [ "$OB_STATUS" = ok ]; then
      # OpenBLAS's force_coretype() makes every target in its switch reachable
      # by name on a single DYNAMIC_ARCH binary. Prove that on this build
      # rather than assuming it: force a target that differs from the one
      # autodetection picked and see whether the corename changes.
      case "$(printf '%s' "$OB_CORE" | tr '[:upper:]' '[:lower:]')" in
        armv8) FORCE_TARGET="NEOVERSEN1" ;;
        *)     FORCE_TARGET="ARMV8" ;;
      esac
      OB_FORCED="$(OPENBLAS_CORETYPE="$FORCE_TARGET" "$TMPB" 2>/dev/null | cut -d'|' -f1 || true)"
      if [ -n "$OB_FORCED" ] && [ "$OB_FORCED" != "$OB_CORE" ]; then
        OB_FORCING="available"
      else
        OB_FORCING="unavailable"
      fi
    fi
  fi
  rm -f "$TMPC" "$TMPB"
fi

OB_LOWER="$(printf '%s' "$OB_CORE" | tr '[:upper:]' '[:lower:]')"

printf '{'
s run_id "${GBB_RUN_ID:-unset}"; printf ','
s captured_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)"; printf ','
s host "$(hostname)"; printf ','
s instance_type "$INSTANCE_TYPE"; printf ','
s instance_id "$INSTANCE_ID"; printf ','
s az "$AZ"; printf ','
s midr "$MIDR"; printf ','
s midr_implementer "$IMPL"; printf ','
s midr_part "$PART"; printf ','
s core_name "$CORE_NAME"; printf ','
s midr_scalar_source "cpu0"; printf ','
s cpu0_midr "$MIDR"; printf ','
s cpu0_midr_part "$PART"; printf ','
s cpu0_core_name "$CORE_NAME"; printf ','
jbool midr_uniform "$MIDR_UNIFORM"; printf ','
jnum midr_cpus_read "$MIDR_SEEN"; printf ','
jstrarray midr_distinct ${MIDR_DISTINCT[@]+"${MIDR_DISTINCT[@]}"}; printf ','
emit_core_clusters; printf ','
j cores_total "$CORES_TOTAL"; printf ','
jnum cpus_online "$CPUS_ONLINE"; printf ','
s cpus_online_list "$CPUS_ONLINE_LIST"; printf ','
jnum cpus_affinity "$CPUS_AFFINITY"; printf ','
s cpus_affinity_list "$CPUS_AFFINITY_LIST"; printf ','
s cgroup_version "$CGROUP_VERSION"; printf ','
sn cgroup_cpu_max "$CGROUP_CPU_MAX"; printf ','
jnum cgroup_cpu_quota_us "$CGROUP_QUOTA_US"; printf ','
jnum cgroup_cpu_period_us "$CGROUP_PERIOD_US"; printf ','
jnum cgroup_cpu_limit "$CGROUP_CPU_LIMIT"; printf ','
j sockets "$SOCKETS"; printf ','
j numa_nodes "$NUMA_NODES"; printf ','
j threads_per_core "$THREADS_PER_CORE"; printf ','
s l1d "$L1D"; printf ','
s l2 "$L2"; printf ','
s l3 "$L3"; printf ','
s cpu_features "$FLAGS"; printf ','
j has_sve "$HAS_SVE"; printf ','
j has_sve2 "$HAS_SVE2"; printf ','
j has_bf16 "$HAS_BF16"; printf ','
j has_i8mm "$HAS_I8MM"; printf ','
j has_sme "$HAS_SME"; printf ','
s sve_vector_length_bytes "$SVE_VL"; printf ','
jnum sve_default_vl_bytes "$SVE_VL"; printf ','
s cpufreq_governor "$GOV"; printf ','
s cpufreq_cur_khz "$CUR_FREQ"; printf ','
s kernel "$(uname -r)"; printf ','
sn openblas_dynamic_selection "$OB_CORE"; printf ','
sn openblas_dynamic_config "$OB_CONFIG"; printf ','
s openblas_dynamic_probe_status "$OB_STATUS"; printf ','
s openblas_coretype_forcing "$OB_FORCING"; printf ','

# ---- warnings, computed before the array is emitted ------------------------
# Anything that invalidates a run or needs a human is decided here so it lands
# inside the JSON as well as on stderr.
if [ "$MIDR_SEEN" -gt 0 ] && [ "$CORE_NAME" = "UNRECOGNISED" ]; then
  warn 4 "cpu0 MIDR part $PART is not in OpenBLAS dynamic_arm64.c's switch. Record which target DYNAMIC_ARCH actually selects: on a host with SVE the fallback is ARMV8SVE, which carries more SVE kernels than a named Neoverse V2/V3 target, so this may be an interesting result rather than a regression. Escalate before drawing a conclusion (standing order 8)."
fi
i=0
while [ "$i" -lt "${#CL_NAME[@]}" ]; do
  if [ "${CL_NAME[$i]}" = "UNRECOGNISED" ] && [ "${CL_PART[$i]}" != "$PART" ]; then
    warn 4 "MIDR part ${CL_PART[$i]} on cpus ${CL_CPUS[$i]} is not in OpenBLAS dynamic_arm64.c's switch. Escalate (standing order 8)."
  fi
  i=$((i + 1))
done

if [ "$MIDR_UNIFORM" = false ]; then
  DESC=""
  i=0
  while [ "$i" -lt "${#CL_NAME[@]}" ]; do
    DESC="$DESC${DESC:+, }${CL_NAME[$i]} (${CL_PART[$i]}) on cpus ${CL_CPUS[$i]}${CL_MAXFREQ[$i]:+ @ ${CL_MAXFREQ[$i]}kHz}"
    i=$((i + 1))
  done
  warn 3 "heterogeneous cores: ${#MIDR_DISTINCT[@]} distinct MIDRs -- $DESC. Every host-level number from this box is a blend of microarchitectures: OMP_PLACES=cores with OMP_PROC_BIND=close will land threads on whichever cluster the CPU numbering gives it, and the clusters may be interleaved rather than contiguous. Pin per cluster with the cpu lists above, or do not use this host for timings."
fi

if [ "$GOV" != "none" ] && [ "$GOV" != "performance" ]; then
  warn 3 "cpufreq governor is '$GOV', not 'performance'. Timings on this host are not comparable."
fi
if [ "${THREADS_PER_CORE:-1}" != "1" ]; then
  warn 3 "threads-per-core is ${THREADS_PER_CORE}, not 1. Graviton has no SMT; this is not a Graviton host."
fi

case "$OB_STATUS" in
  not_attempted)
    warn 0 "DYNAMIC_ARCH probe not attempted (GBB_OPENBLAS_DYNAMIC_DIR unset or absent). openblas_dynamic_selection is null: the standing-order-8 generic-ARMV8 check was NOT performed on this host."
    ;;
  build_failed)
    warn 0 "DYNAMIC_ARCH probe failed: could not build/link the corename probe against $GBB_OPENBLAS_DYNAMIC_DIR/lib. openblas_dynamic_selection is null -- detection broke, it did not report a clean result."
    ;;
  run_failed)
    warn 0 "DYNAMIC_ARCH probe failed: the corename probe built but produced no usable output. openblas_dynamic_selection is null -- detection broke, it did not report a clean result."
    ;;
esac

if [ "$OB_STATUS" = ok ]; then
  case "$OB_LOWER" in
    *sve*) : ;;
    *armv8*)
      if [ "$HAS_SVE" = true ]; then
        warn 4 "DYNAMIC_ARCH selected generic '$OB_CORE' on a host that reports SVE. This is the alarming case: SVE detection failed, so default NumPy/R/Julia here run generic NEON and leave the SVE kernels unused entirely. Stop and escalate (standing order 8)."
      else
        warn 0 "DYNAMIC_ARCH selected generic '$OB_CORE'. This host reports no SVE, so generic NEON is the expected target; recorded for the archaeology."
      fi
      ;;
  esac
fi

jstrarray warnings ${WARNINGS[@]+"${WARNINGS[@]}"}
printf '}\n'

if [ "$WARN_ONLY" -eq 1 ]; then
  [ "$EXIT_CODE" -eq 0 ] || echo "gbb: --warn-only given; exiting 0 despite exit code $EXIT_CODE." >&2
  exit 0
fi
exit "$EXIT_CODE"
