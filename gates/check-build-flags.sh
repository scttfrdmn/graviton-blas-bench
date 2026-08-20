#!/usr/bin/env bash
# Standing order 6: the harness itself is identical across arms. -O2, no
# -march=native. Only the BLAS under test varies.
#
# This is a real hazard, not a style rule. -march=native on a c7g host and a
# c8g host produces two different harnesses, and any difference between them
# lands in the results as if it were a difference between BLAS libraries.
# -O3 is excluded for the same reason it is excluded from roofline.c: it changes
# how aggressively the compiler vectorises code that is supposed to be a
# constant across arms.

set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT" || { printf 'FATAL: cannot cd to %s\n' "$ROOT" >&2; exit 1; }

RC=0

# The harness build flags live in the Makefile's CFLAGS default.
CFLAGS_LINE="$(grep -E '^CFLAGS[[:space:]]*\?=' Makefile || true)"
if [ -z "$CFLAGS_LINE" ]; then
  echo "FAIL: no 'CFLAGS ?=' default found in Makefile"
  RC=1
else
  echo "Makefile: $CFLAGS_LINE"
  case "$CFLAGS_LINE" in
    *-O2*) ;;
    *) echo "FAIL: harness CFLAGS does not specify -O2"; RC=1 ;;
  esac
fi

# No -march=native or -O3 anywhere that builds the harness. The BLAS libraries
# under test are built by their own build systems and are not covered by this
# rule -- build-libs.sh passes TARGET= to OpenBLAS, which is the whole point of
# the experiment.
#
# Comments are stripped before matching: this file and the Makefile both discuss
# the forbidden flags in prose, and a checker that trips over its own
# documentation is a checker people delete.
FORBIDDEN='\-march=native|\-mcpu=native|\-O3|\-Ofast'
# shellcheck disable=SC2043  # deliberate one-element list: the Makefile is the
# only thing that builds the harness today, and the loop is the seam for the
# second file rather than a bad glob. A missing Makefile is already caught
# above -- grep on an absent file leaves CFLAGS_LINE empty, which FAILs -- so
# the `continue` here cannot make the check pass vacuously.
for f in Makefile; do
  [ -f "$f" ] || continue
  if sed 's/#.*//' "$f" | grep -nE "$FORBIDDEN"; then
    echo "FAIL: $f introduces a forbidden optimisation flag (above)"
    RC=1
  fi
done

# Verify the real compile line, not just the declared flags: a rule could
# append -O3 after $(CFLAGS).
#
# An unverifiable check must not read as a pass. The earlier form was
# `$(make -n roofline 2>/dev/null || true)` guarded by `[ -n "$DRYRUN" ]`, so a
# make that failed for any reason -- missing include, syntax error, no such
# target -- produced an empty string and skipped both checks below in silence.
# That is the same shape as the sve_kernels() bug: a probe failure collapsing
# into a substantive answer. `roofline` is phony, so make prints the compile
# line even when bin/gbb-roofline is already built; an empty result therefore
# means the probe broke, never that there was nothing to inspect.
DRYRUN="$(make -n roofline 2>&1)"; DRC=$?
if [ "$DRC" -ne 0 ] || [ -z "$DRYRUN" ]; then
  echo "FAIL: 'make -n roofline' produced no compile line to inspect (exit $DRC)."
  echo "      standing order 6 cannot be verified from the declared CFLAGS alone,"
  echo "      so this is a failure and not a skip. Output was:"
  printf '%s\n' "$DRYRUN"
  RC=1
else
  if printf '%s' "$DRYRUN" | grep -qE '\-march=native|\-mcpu=native|\-O3|\-Ofast'; then
    echo "FAIL: 'make -n roofline' shows a forbidden flag in the actual compile line:"
    printf '%s\n' "$DRYRUN"
    RC=1
  fi
  if ! printf '%s' "$DRYRUN" | grep -q '\-O2'; then
    echo "FAIL: 'make -n roofline' compile line does not carry -O2:"
    printf '%s\n' "$DRYRUN"
    RC=1
  fi
fi

if [ "$RC" -eq 0 ]; then
  echo "harness build flags conform to standing order 6 (-O2, no native arch)"
fi
exit "$RC"
