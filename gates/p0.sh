#!/usr/bin/env bash
# Gate P0 — repo hygiene. Exits 0 (green) or 1 (red) and prints its evidence.
#
# P0 asserts only that the repo is buildable and lintable on a clean clone. It
# deliberately asserts nothing about numbers: that is P1's job, on synthetic
# data, before any cloud spend.

set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PASS=0
FAIL=0

ok()   { printf '  \033[32mPASS\033[0m  %s\n' "$*"; PASS=$((PASS+1)); }
bad()  { printf '  \033[31mFAIL\033[0m  %s\n' "$*"; FAIL=$((FAIL+1)); }
head_() { printf '\n%s\n%s\n' "$*" "$(printf '%.0s-' $(seq 1 ${#1}))"; }

printf '=== gate P0: repo hygiene ===\n'
printf 'root: %s\n' "$ROOT"

# ---- 1. required files exist ----------------------------------------------
head_ "1. required files"
for f in LICENSE README.md CHANGELOG.md CLAUDE.md .gitignore Makefile \
         CONTRIBUTING.md SECURITY.md CODE_OF_CONDUCT.md \
         src/bench.c src/roofline.c src/coreprobe.c \
         scripts/build-libs.sh scripts/capture-env.sh scripts/run-matrix.sh \
         analysis/decompose.py tests/run-matrix-stubs.sh \
         tests/arch-selected-assert.sh \
         .github/workflows/ci.yml scripts/bootstrap-github.sh; do
  if [ -f "$f" ]; then ok "$f"; else bad "$f missing"; fi
done

# ---- 2. licence is MIT and correctly attributed ---------------------------
head_ "2. licence"
if grep -q 'MIT License' LICENSE && \
   grep -q 'Copyright (c) 2026 Playground Logic LLC' LICENSE; then
  ok "LICENSE is MIT, Copyright (c) 2026 Playground Logic LLC"
else
  bad "LICENSE is not the expected MIT text/attribution"
fi

# ---- 3. shell syntax ------------------------------------------------------
head_ "3. bash -n on every script"
while IFS= read -r f; do
  if bash -n "$f" 2>/dev/null; then ok "bash -n $f"; else bad "bash -n $f"; fi
done < <(find . -name '*.sh' -not -path './.git/*' | sort)

# ---- 4. python ------------------------------------------------------------
head_ "4. python"
while IFS= read -r f; do
  if python3 -m py_compile "$f" 2>/dev/null; then ok "py_compile $f"; else bad "py_compile $f"; fi
done < <(find . -name '*.py' -not -path './.git/*' | sort)

if command -v ruff >/dev/null 2>&1; then
  if ruff check . >/dev/null 2>&1; then ok "ruff check"; else bad "ruff check (run 'ruff check .' for detail)"; fi
else
  printf '  \033[33mSKIP\033[0m  ruff not installed locally (CI enforces it)\n'
fi

# ---- 5. the harness builds ------------------------------------------------
head_ "5. build"
if make -s roofline >/tmp/gbb-p0-roofline.log 2>&1; then
  ok "make roofline"
else
  bad "make roofline -- see /tmp/gbb-p0-roofline.log"
  sed -n '1,20p' /tmp/gbb-p0-roofline.log
fi

# bench.c needs a BLAS to link. Compiling it to an object proves the source is
# good without requiring one to be installed.
if ${CC:-cc} -O2 -Wall -Wextra -std=c11 -c src/bench.c -o /tmp/gbb-p0-bench.o \
     >/tmp/gbb-p0-bench.log 2>&1; then
  ok "bench.c compiles (-c, no BLAS needed)"
else
  bad "bench.c does not compile -- see /tmp/gbb-p0-bench.log"
  sed -n '1,20p' /tmp/gbb-p0-bench.log
fi

if ${CC:-cc} -O2 -Wall -Wextra -std=c11 -c src/coreprobe.c -o /tmp/gbb-p0-coreprobe.o \
     >/tmp/gbb-p0-coreprobe.log 2>&1; then
  ok "coreprobe.c compiles (-c, no BLAS needed)"
else
  bad "coreprobe.c does not compile -- see /tmp/gbb-p0-coreprobe.log"
  sed -n '1,20p' /tmp/gbb-p0-coreprobe.log
fi

# ---- 5b. the runner's decision logic --------------------------------------
# Stub-based, so it runs anywhere. This is the part of the harness that decides
# whether to spend instance-hours and what to call each arm; a defect here does
# not produce a failed run, it produces a plausible wrong answer.
if bash tests/run-matrix-stubs.sh >/tmp/gbb-p0-stubs.log 2>&1; then
  ok "run-matrix stub suite ($(grep -o 'pass=[0-9]*' /tmp/gbb-p0-stubs.log | tail -1) assertions)"
else
  bad "run-matrix stub suite -- see /tmp/gbb-p0-stubs.log"
  grep -A2 'FAIL' /tmp/gbb-p0-stubs.log | head -30
fi

# ---- 5c. bench.c refuses a label it cannot confirm ------------------------
# Compiles bench.c against a stub that exports openblas_get_corename and asserts
# the in-process check fires. Same class of defect as 5b and the same reason it
# matters: an arm that runs under the wrong coretype label produces numbers that
# pass every downstream check.
if bash tests/arch-selected-assert.sh >/tmp/gbb-p0-arch.log 2>&1; then
  if grep -q '^SKIP' /tmp/gbb-p0-arch.log; then
    printf '  \033[33mSKIP\033[0m  %s\n' "$(head -1 /tmp/gbb-p0-arch.log)"
  else
    ok "arch_selected assertion ($(grep -o 'pass=[0-9]*' /tmp/gbb-p0-arch.log | tail -1) assertions)"
  fi
else
  bad "arch_selected assertion -- see /tmp/gbb-p0-arch.log"
  grep -A2 'FAIL' /tmp/gbb-p0-arch.log | head -20
fi

# ---- 6. build flags are the ones we promised -----------------------------
head_ "6. harness build flags (standing order 6)"
if bash gates/check-build-flags.sh >/tmp/gbb-p0-flags.log 2>&1; then
  ok "$(tail -1 /tmp/gbb-p0-flags.log)"
else
  bad "harness build flags violate standing order 6"
  cat /tmp/gbb-p0-flags.log
fi

# ---- 7. no results or binaries committed ---------------------------------
head_ "7. no artifacts committed"
if git -C "$ROOT" rev-parse --git-dir >/dev/null 2>&1; then
  if git ls-files | grep -Eq '^(bin/|results/)|\.ndjson$|\.o$'; then
    bad "build output or results are tracked by git"
    git ls-files | grep -E '^(bin/|results/)|\.ndjson$|\.o$'
  else
    ok "no bin/, results/, *.ndjson or *.o tracked"
  fi
else
  printf '  \033[33mSKIP\033[0m  not a git repo\n'
fi

# ---- verdict --------------------------------------------------------------
printf '\n=== gate P0: %d passed, %d failed ===\n' "$PASS" "$FAIL"
if [ "$FAIL" -eq 0 ]; then
  printf '\033[32mGATE P0 GREEN\033[0m — P1 may start once Scott confirms.\n'
  exit 0
fi
printf '\033[31mGATE P0 RED\033[0m — do not start P1.\n'
exit 1
