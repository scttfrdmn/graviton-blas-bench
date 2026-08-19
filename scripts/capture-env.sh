#!/usr/bin/env bash
# graviton-blas-bench — capture everything about this host that could explain a number.
#
# Emits one JSON object on stdout. Run once per instance, before any timing.
# The MIDR part number and the OpenBLAS runtime core selection are the two
# fields the analysis actually branches on; the rest is for post-hoc "why is
# this arm weird" archaeology.

set -uo pipefail

j() { printf '"%s":%s' "$1" "$2"; }
s() { printf '"%s":"%s"' "$1" "$(printf '%s' "${2:-}" | tr -d '"' | tr '\n' ' ')"; }

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

# MIDR: the field OpenBLAS dispatches on. 0xd0c=N1, 0xd40=V1, 0xd4f=V2,
# 0xd84=V3. Anything not in dynamic_arm64.c's switch falls back to generic
# ARMV8 -- which is exactly the failure mode worth catching on new silicon.
MIDR="$(cat /sys/devices/system/cpu/cpu0/regs/identification/midr_el1 2>/dev/null || echo '')"
if [ -n "$MIDR" ]; then
  MIDR_INT=$((MIDR))
  IMPL=$(printf '0x%x' $(( (MIDR_INT >> 24) & 0xFF )))
  PART=$(printf '0x%x' $(( (MIDR_INT >> 4)  & 0xFFF )))
else
  IMPL=""; PART=""
fi

case "$PART" in
  0xd0c) CORE_NAME="Neoverse N1" ;;
  0xd40) CORE_NAME="Neoverse V1" ;;
  0xd49) CORE_NAME="Neoverse N2" ;;
  0xd4f) CORE_NAME="Neoverse V2" ;;
  0xd83) CORE_NAME="Neoverse V3AE" ;;
  0xd84) CORE_NAME="Neoverse V3" ;;
  0xd85) CORE_NAME="Cortex-X925" ;;
  0xd87) CORE_NAME="Cortex-A725" ;;
  *)     CORE_NAME="UNRECOGNISED" ;;
esac

FLAGS="$(grep -m1 '^Features' /proc/cpuinfo | cut -d: -f2- | xargs || true)"
has() { printf '%s' " $FLAGS " | grep -qw "$1" && echo true || echo false; }

# SVE vector length in bytes, if any. This is the number that decides whether
# the SVE kernels can beat NEON at all.
SVE_VL=""
if command -v python3 >/dev/null && [ "$(has sve)" = true ]; then
  SVE_VL="$(cat /proc/sys/abi/sve_default_vector_length 2>/dev/null || echo '')"
fi

NUMA_NODES="$(lscpu 2>/dev/null | awk -F: '/NUMA node\(s\)/{gsub(/ /,"",$2);print $2}')"
SOCKETS="$(lscpu 2>/dev/null | awk -F: '/^Socket\(s\)/{gsub(/ /,"",$2);print $2}')"
THREADS_PER_CORE="$(lscpu 2>/dev/null | awk -F: '/Thread\(s\) per core/{gsub(/ /,"",$2);print $2}')"
CORES_TOTAL="$(nproc)"
L1D="$(lscpu 2>/dev/null | awk -F: '/L1d cache/{gsub(/^ +/,"",$2);print $2}')"
L2="$(lscpu  2>/dev/null | awk -F: '/L2 cache/{gsub(/^ +/,"",$2);print $2}')"
L3="$(lscpu  2>/dev/null | awk -F: '/L3 cache/{gsub(/^ +/,"",$2);print $2}')"

# Fixed-frequency check. Graviton has no turbo, so scaling_cur_freq should be
# constant. If a governor is present and not "performance", say so loudly --
# it would invalidate every timing on this host.
GOV="$(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null || echo 'none')"
CUR_FREQ="$(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq 2>/dev/null || echo '')"

# What DYNAMIC_ARCH OpenBLAS actually picks here. This is a finding, not
# bookkeeping: if it reports armv8 on c9g, default NumPy is running generic
# NEON on the newest Graviton.
OB_CORE="unknown"
if [ -n "${GBB_OPENBLAS_DYNAMIC_DIR:-}" ] && [ -d "$GBB_OPENBLAS_DYNAMIC_DIR" ]; then
  TMPC="$(mktemp /tmp/gblas-obcore-XXXX.c)"; TMPB="${TMPC%.c}"
  cat > "$TMPC" <<'EOF'
#include <stdio.h>
char *openblas_get_corename(void);
char *openblas_get_config(void);
int main(void){ printf("%s|%s\n", openblas_get_corename(), openblas_get_config()); return 0; }
EOF
  if gcc -O0 "$TMPC" -o "$TMPB" -L"$GBB_OPENBLAS_DYNAMIC_DIR/lib" -lopenblas \
       -Wl,-rpath,"$GBB_OPENBLAS_DYNAMIC_DIR/lib" 2>/dev/null; then
    OB_CORE="$("$TMPB" 2>/dev/null || echo unknown)"
  fi
  rm -f "$TMPC" "$TMPB"
fi

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
j cores_total "${CORES_TOTAL:-0}"; printf ','
j sockets "${SOCKETS:-1}"; printf ','
j numa_nodes "${NUMA_NODES:-1}"; printf ','
j threads_per_core "${THREADS_PER_CORE:-1}"; printf ','
s l1d "$L1D"; printf ','
s l2 "$L2"; printf ','
s l3 "$L3"; printf ','
s cpu_features "$FLAGS"; printf ','
j has_sve "$(has sve)"; printf ','
j has_sve2 "$(has sve2)"; printf ','
j has_bf16 "$(has bf16)"; printf ','
j has_i8mm "$(has i8mm)"; printf ','
j has_sme "$(has sme)"; printf ','
s sve_vector_length_bytes "$SVE_VL"; printf ','
s cpufreq_governor "$GOV"; printf ','
s cpufreq_cur_khz "$CUR_FREQ"; printf ','
s kernel "$(uname -r)"; printf ','
s openblas_dynamic_selection "$OB_CORE"
printf '}\n'

# Loud warnings to stderr -- these invalidate runs and must not be buried.
[ "$CORE_NAME" = "UNRECOGNISED" ] && \
  echo "gbb: WARNING: MIDR part $PART is not in OpenBLAS dynamic_arm64.c -- DYNAMIC_ARCH builds will fall back to generic ARMV8 on this host. That is a finding; record it." >&2
[ "$GOV" != "none" ] && [ "$GOV" != "performance" ] && \
  echo "gbb: WARNING: cpufreq governor is '$GOV', not 'performance'. Timings on this host are not comparable." >&2
[ "${THREADS_PER_CORE:-1}" != "1" ] && \
  echo "gbb: WARNING: threads-per-core is ${THREADS_PER_CORE}, not 1. Graviton has no SMT; this is not a Graviton host." >&2
exit 0
