#!/usr/bin/env bash
# Asserts that build-libs.sh's sve_kernels() can actually report `yes`.
#
# WHY THIS SUITE EXISTS
#
# The first P2 pass shipped a manifest recording sve_kernels="no" for all four
# OpenBLAS builds, on a host where SVE was demonstrably present. decompose.py did
# exactly what it should with that and raised standing order 8 -- "every
# SVE-coretype arm on this host measures the NEON path under an SVE label" -- four
# times, on a dataset where the SVE kernels were fine.
#
# The cause was `nm ... | grep -qE ...` under `set -o pipefail`. grep -q exits on
# its first match, nm dies on SIGPIPE, pipefail reports 141, and the `if` takes the
# else branch. Both outcomes therefore printed `no`:
#
#     SVE present -> grep matches early -> nm killed 141 -> pipefail -> `no`
#     SVE absent  -> grep reads all, exits 1             -> pipefail -> `no`
#
# So the probe was a constant function returning `no`. It could not pass, and it
# could not fail; it could only be believed. That is the whole reason this file is
# a suite and not a comment: standing order 8 is the one condition CLAUDE.md says
# outweighs every other question in the repo, and its trigger had no test.
#
# TWO PROPERTIES THIS SUITE HOLDS ITSELF TO
#
#   1. It runs under `set -euo pipefail`, because a bare `bash test.sh` does NOT
#      inherit pipefail and the bug is invisible without it. Forty standalone runs
#      of the broken function returned `yes` during the investigation, which is
#      precisely how it survived review.
#   2. It proves its own fixture is adequate. A pipeline only induces SIGPIPE if
#      the producer is still writing when the consumer exits, so an archive whose
#      whole nm output fits in the 64 KiB pipe buffer would let the OLD form
#      return `yes` and this suite would pass against the bug. So the suite
#      asserts the old form returns `no` on this fixture. If that assertion fails,
#      the fixture is too small -- and the suite FAILS and says so rather than
#      reporting a green it did not earn.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$ROOT/scripts/build-libs.sh"

pass=0
fail=0
ok()  { printf '  PASS  %s\n' "$*"; pass=$((pass+1)); }
bad() { printf '  FAIL  %s\n' "$*"; fail=$((fail+1)); }

for tool in nm ar cc; do
  command -v "$tool" >/dev/null 2>&1 || {
    printf 'SKIP: %s not available; cannot build a fixture archive\n' "$tool"
    exit 0
  }
done

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# ---- the function under test, taken from the producer ----------------------
# Extracted rather than reimplemented. A copy here would drift from build-libs.sh
# and this suite would then rigorously test a function nobody runs.
FN="$TMP/fn.sh"
sed -n '/^sve_kernels() {$/,/^}$/p' "$SRC" > "$FN"
if [ ! -s "$FN" ]; then
  printf 'FAIL: could not extract sve_kernels() from %s\n' "$SRC"
  printf '      (renamed, or reformatted so the sed range no longer matches)\n'
  exit 1
fi
ok "extracted sve_kernels() from scripts/build-libs.sh ($(wc -l <"$FN" | tr -d ' ') lines)"

# sve_kernels() calls log(); build-libs.sh sends it to stderr and so must we, or
# the log lines would be captured as part of the probe's answer.
log() { printf '[stub-log] %s\n' "$*" >&2; }
# shellcheck source=/dev/null
. "$FN"

# The old, broken form, kept verbatim so the fixture-adequacy check below is a
# real reproduction and not a paraphrase of one.
sve_kernels_old() {
  local dest="$1" lib
  command -v nm >/dev/null 2>&1 || { printf 'unknown'; return 0; }
  for lib in "$dest/lib/libopenblas.a" "$dest/lib64/libopenblas.a"; do
    [ -f "$lib" ] || continue
    if nm --defined-only "$lib" 2>/dev/null | grep -qE '(ARMV8SVE|_sve|sve_)'; then
      printf 'yes'
    else
      printf 'no'
    fi
    return 0
  done
  printf 'unknown'
}

# ---- fixtures --------------------------------------------------------------
# The matching symbol goes in the FIRST archive member so grep -q matches early
# and leaves nm with plenty still to write -- the SIGPIPE window.
mkfixture() {   # mkfixture <dir> <with-sve: yes|no>
  local dir="$1" withsve="$2" i
  mkdir -p "$dir/lib"
  if [ "$withsve" = yes ]; then
    printf 'int sve_kernel_marker(void){return 0;}\nint dgemm_kernel_ARMV8SVE(void){return 1;}\n' \
      > "$TMP/m.c"
    cc -c "$TMP/m.c" -o "$TMP/m.o"
    ar rcs "$dir/lib/libopenblas.a" "$TMP/m.o" 2>/dev/null
  else
    : > "$TMP/none.c"
    printf 'int only_a_placeholder(void){return 0;}\n' > "$TMP/none.c"
    cc -c "$TMP/none.c" -o "$TMP/none.o"
    ar rcs "$dir/lib/libopenblas.a" "$TMP/none.o" 2>/dev/null
  fi
  # Filler, to push nm's output well past the pipe buffer so grep -q's early exit
  # leaves nm still writing. Names deliberately free of `sve_`, `_sve` and
  # `ARMV8SVE`.
  #
  # THE SIZE IS NOT ARBITRARY AND "past the pipe buffer" IS NOT ENOUGH. This started
  # at 6x500 short-named symbols -- about 93 KB of nm output against a 64 KiB pipe
  # buffer -- and section 2 failed it on x86 CI while passing on the dev host. The
  # margin was the whole problem: grep's first read drains the full 64 KiB, and while
  # grep is still scanning that block for a match, nm writes the remaining ~29 KB and
  # exits 0. No SIGPIPE, so the old form returns `yes` and the fixture stops
  # reproducing the bug. It is a race, so it was never reliably reproducing it -- it
  # won on one platform and lost on another, and the earlier note in section 1 that
  # "a 60 KB fixture reproduced the bug fine" was recording luck as evidence.
  #
  # The fix is margin measured in multiples, not in kilobytes: names are padded so
  # each nm line is long, which buys bytes far more cheaply than more symbols buys
  # compile time. ~12k symbols at ~100 bytes a line is ~1.4 MB, more than 20x the
  # buffer, so nm cannot possibly finish inside one of grep's scans.
  local pad=________________________________________________________________
  for i in 1 2 3 4 5 6 7 8; do
    seq 1 1500 | awk -v b="$i" -v p="$pad" \
      '{printf "int filler_%s_%d_%d(void){return %d;}\n", p, b, $1, $1}' > "$TMP/f.c"
    cc -c "$TMP/f.c" -o "$TMP/f_$i.o"
  done
  ar rcs "$dir/lib/libopenblas.a" "$TMP"/f_*.o 2>/dev/null
  rm -f "$TMP"/f_*.o
}

printf '\n1. fixtures\n'
mkfixture "$TMP/with" yes
mkfixture "$TMP/without" no
# Captured to a file rather than piped, for the same reason the fixed probe captures
# it: any `nm | grep -m1`-shaped measurement below would exit early, SIGPIPE nm, and
# under this suite's own `set -o pipefail` kill the suite. That happened while these
# assertions were being written, which is a fair indication of how easy the original
# defect was to introduce.
nm --defined-only "$TMP/with/lib/libopenblas.a" > "$TMP/with.nm"
NSYM=$(wc -l <"$TMP/with.nm" | tr -d ' ')
NBYTES=$(wc -c <"$TMP/with.nm" | tr -d ' ')
NMATCH=$(grep -cE '(ARMV8SVE|_sve|sve_)' "$TMP/with.nm" || true)
ok "with-SVE archive: $NSYM defined symbols, $NBYTES bytes of nm output, $NMATCH matching"
# The two halves of the SIGPIPE window, asserted separately from section 2's
# end-to-end reproduction so that a fixture which stops reproducing the bug says
# WHICH property it lost instead of only that it lost one. Section 2 is a race by
# nature -- it depends on grep and nm interleaving -- and these two are not, so they
# are what keeps section 2 from being flaky rather than merely lucky.
#   (a) the match is early, so grep -q exits with most of the output unwritten;
#   (b) there is far more output than one pipe buffer, so nm cannot drain to
#       completion inside a single one of grep's scans. 93 KB against a 64 KiB
#       buffer did exactly that and cost this suite a red on CI and a green here.
#
# An earlier note here called the byte count "informational, NOT an assertion",
# on the grounds that nm's output "only has to outlast grep's first match rather
# than exceed any particular size -- a 60 KB fixture reproduced the bug fine."
# That reasoning is why this suite shipped a race. The first clause is true and the
# conclusion does not follow: outlasting grep's first match is exactly what a size
# comparable to the buffer cannot guarantee, and "reproduced the bug fine" was one
# platform's scheduler, not a property of the fixture. The buffer is 64 KiB on
# Linux and starts smaller on macOS, so the bound below is conservative on the
# platform that matters and slack on the other.
FIRST=$(grep -nE -m1 '(ARMV8SVE|_sve|sve_)' "$TMP/with.nm" | cut -d: -f1)
if [ "${FIRST:-0}" -gt 0 ] && [ "$FIRST" -lt $((NSYM / 10)) ]; then
  ok "first matching symbol is at line $FIRST of $NSYM -- grep -q exits early (the SIGPIPE window)"
else
  bad "first match at line ${FIRST:-none} of $NSYM: not in the first tenth, so grep -q would
        consume most of nm's output before exiting and the bug would not reproduce"
fi
if [ "$NBYTES" -gt $((64 * 1024 * 8)) ]; then
  ok "nm output is $((NBYTES / 1024)) KB, >8x a 64 KiB pipe buffer (nm cannot finish mid-scan)"
else
  bad "nm output is only $((NBYTES / 1024)) KB: too close to the 64 KiB pipe buffer for the
        race to go the same way every time. Enlarge the filler."
fi
WMATCH=$(nm --defined-only "$TMP/without/lib/libopenblas.a" | grep -cE '(ARMV8SVE|_sve|sve_)' || true)
if [ "$WMATCH" -eq 0 ]; then
  ok "without-SVE archive has 0 matching symbols"
else
  bad "without-SVE archive has $WMATCH matching symbols -- filler names collide with the pattern"
fi

# ---- 2. the fixture really does reproduce the bug --------------------------
# If this says `yes`, the suite is not exercising the failure and every assertion
# below would pass against the broken implementation too.
printf '\n2. fixture adequacy (the old form must still be broken here)\n'
OLD="$(sve_kernels_old "$TMP/with" 2>/dev/null)"
if [ "$OLD" = no ]; then
  ok "old \`nm | grep -q\` form returns 'no' on an archive that DOES contain SVE (the bug)"
else
  bad "old form returned '$OLD', not 'no' -- this fixture does not reproduce the bug,
        so section 3 would pass against the broken implementation. Enlarge the fixture."
fi

# ---- 3. the fixed probe ----------------------------------------------------
printf '\n3. sve_kernels() under set -euo pipefail\n'
case "$(set -o | grep pipefail)" in
  *on*) ok "pipefail is on for this suite (the condition the bug needs)" ;;
  *)    bad "pipefail is OFF -- this suite cannot detect the regression" ;;
esac

GOT="$(sve_kernels "$TMP/with" 2>/dev/null)"
if [ "$GOT" = yes ]; then
  ok "archive with SVE symbols -> 'yes'"
else
  bad "archive with SVE symbols -> '$GOT', expected 'yes' (standing order 8 would fire falsely)"
fi

GOT="$(sve_kernels "$TMP/without" 2>/dev/null)"
if [ "$GOT" = no ]; then
  ok "archive without SVE symbols -> 'no'"
else
  bad "archive without SVE symbols -> '$GOT', expected 'no' (the real escalation would be missed)"
fi

# ---- 4. `unknown` means could-not-look, and never collapses into `no` ------
printf '\n4. probe failure is `unknown`, not `no`\n'
mkdir -p "$TMP/absent/lib"
GOT="$(sve_kernels "$TMP/absent" 2>/dev/null)"
if [ "$GOT" = unknown ]; then
  ok "no archive at all -> 'unknown'"
else
  bad "no archive -> '$GOT', expected 'unknown'"
fi

# A truncated archive: nm exits non-zero with "file format not recognized" and
# prints nothing. Under the old form that was indistinguishable from a confirmed
# absence, so a half-written install read as an SVE-less library.
mkdir -p "$TMP/trunc/lib"
head -c 4096 "$TMP/with/lib/libopenblas.a" > "$TMP/trunc/lib/libopenblas.a"
GOT="$(sve_kernels "$TMP/trunc" 2>/dev/null)"
if [ "$GOT" = unknown ]; then
  ok "truncated archive -> 'unknown' (not a confirmed absence)"
else
  bad "truncated archive -> '$GOT', expected 'unknown'"
fi

# ---- 5. the probe does not abort the build ---------------------------------
# `set -e` is on here as it is in build-libs.sh. A bare `out="$(nm ...)"` would
# make a failed probe kill the whole build, turning a provenance gap into a lost
# pass.
printf '\n5. a failed probe does not abort the caller\n'
# shellcheck source=/dev/null
if ( set -euo pipefail; . "$FN"; sve_kernels "$TMP/trunc" >/dev/null 2>&1; echo reached ) \
     | grep -q reached; then
  ok "sve_kernels returns normally on an unreadable archive under set -e"
else
  bad "sve_kernels aborted its caller under set -e -- a probe failure must not end the build"
fi

printf '\n=== sve probe: pass=%d fail=%d ===\n' "$pass" "$fail"
[ "$fail" -eq 0 ] || exit 1
