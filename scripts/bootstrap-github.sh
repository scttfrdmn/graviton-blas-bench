#!/usr/bin/env bash
# graviton-blas-bench — create the GitHub project management surface.
#
# Idempotent: every create is guarded, so re-running after a partial failure
# finishes the job rather than erroring or duplicating. Requires `gh` with a
# token carrying repo + project scopes.
#
#   bash scripts/bootstrap-github.sh            # do it
#   DRY_RUN=1 bash scripts/bootstrap-github.sh  # print what it would do
#
# Creates: the public repo, labels, milestones P0-P4, one umbrella issue per
# phase, and a Projects v2 board. The umbrella issues are the back-and-forth
# channel with Scott -- standing order 13 posts a session-end status comment on
# the active one.

set -uo pipefail

REPO="${GBB_REPO:-scttfrdmn/graviton-blas-bench}"
OWNER="${REPO%%/*}"
DRY_RUN="${DRY_RUN:-}"

log()  { printf '[bootstrap] %s\n' "$*" >&2; }
# shellcheck disable=SC2294  # eval is load-bearing here: callers pass
# pre-quoted arguments (label names and descriptions containing spaces are
# wrapped in literal single quotes at the call site), so "$@" would pass the
# quotes through to gh as data. Dropping eval changes what gets created.
run()  { if [ -n "$DRY_RUN" ]; then printf '[dry-run] %s\n' "$*" >&2; else eval "$@"; fi; }

command -v gh >/dev/null || { log "FATAL: gh not installed"; exit 1; }
gh auth status >/dev/null 2>&1 || { log "FATAL: gh not authenticated -- run 'gh auth login'"; exit 1; }

# ---- repo -----------------------------------------------------------------
if gh repo view "$REPO" >/dev/null 2>&1; then
  log "repo $REPO exists"
else
  log "creating public repo $REPO"
  run gh repo create "$REPO" --public \
    --description "'A harness for deciding whether OpenBLAS kernel work on AWS Graviton is worth doing, and for producing a publishable decomposition either way.'" \
    --source=. --remote=origin --push
fi

# ---- labels ---------------------------------------------------------------
# name|colour|description
LABELS=(
  "phase:P0|ededed|Repo hygiene"
  "phase:P1|ededed|Synthetic instrument check"
  "phase:P2|ededed|Single-host end-to-end"
  "phase:P3|ededed|Full matrix"
  "phase:P4|ededed|Decomposition and write-up"
  "gate|5319e7|Blocks progression to the next phase"
  "umbrella|5319e7|Phase tracking issue"
  "provenance|b60205|Affects admissibility of a measurement"
  "measurement-impact|d93f0b|Changes comparability of collected results"
  "finding|0e8a16|A result, not a defect"
  "null-result|0e8a16|Evidence that the effect is absent"
  "cost|fbca04|Involves instance spend"
  "harness|1d76db|src/*.c and the Makefile"
  "analysis|1d76db|analysis/ and tools/"
  "infra|1d76db|scripts/, CI, gates/"
  "bug|d73a4a|Something is broken"
  "enhancement|a2eeef|New capability"
)
for spec in "${LABELS[@]}"; do
  IFS='|' read -r lname lcolour ldesc <<<"$spec"
  # cut+grep -xF, not `grep -P`: BSD grep has no -P, so on macOS the guard
  # errored out and the "idempotent" claim held only on Linux.
  if gh label list --repo "$REPO" --limit 200 2>/dev/null | cut -f1 | grep -qxF "$lname"; then
    log "label '$lname' exists"
  else
    log "creating label '$lname'"
    run gh label create "'$lname'" --repo "$REPO" --color "$lcolour" --description "'$ldesc'"
  fi
done

# ---- milestones -----------------------------------------------------------
milestone_number() {
  gh api "repos/$REPO/milestones?state=all&per_page=100" \
    --jq ".[] | select(.title==\"$1\") | .number" 2>/dev/null | head -1
}
declare -A MILESTONES=(
  [P0]="Repo hygiene — CI green on a clean clone, make roofline builds, bash -n clean"
  [P1]="Synthetic instrument check — every planted effect recovered, planted null reported as a null"
  [P2]="Single-host end-to-end — complete NDJSON from one c8g, numactl -H recorded"
  [P3]="Full matrix — five families, every env-*.json present, no unresolved anomalies"
  [P4]="Decomposition and write-up — a clear answer to whether the N2 gap is worth closing"
)
for m in P0 P1 P2 P3 P4; do
  if [ -n "$(milestone_number "$m")" ]; then
    log "milestone $m exists"
  else
    log "creating milestone $m"
    run gh api "repos/$REPO/milestones" -X POST \
      -f title="$m" -f description="'${MILESTONES[$m]}'" >/dev/null
  fi
done

# ---- umbrella issues ------------------------------------------------------
umbrella_body() {
  cat <<EOF
Umbrella issue for **phase $1**. This is the back-and-forth channel for the
phase: session-end status comments land here (standing order 13).

**Gate:** \`gates/$(printf '%s' "$1" | tr 'A-Z' 'a-z').sh\` must exit 0 before the
next phase starts.

$2

---
### Status log

<!-- Session-end comments below: what ran, what the gate said, what is blocked,
     what is needed from Scott. -->
EOF
}

declare -A UMBRELLA_SCOPE=(
  [P0]="Scope: drop in the harness sources, add LICENSE/CHANGELOG/.gitignore/CLAUDE.md/bootstrap, stand up CI (compile both C sources, \`bash -n\` every script, \`ruff\` + \`py_compile\` on the analysis), tag \`v0.0.1\`.

Gate evidence required: CI green on a clean clone, \`make roofline\` builds, \`bash -n\` clean."
  [P1]="Scope: \`tools/synth.py\` emits NDJSON with **planted** effects — a small-regime penalty, a leading-dimension penalty, a \`DYNAMIC_ARCH\`→generic-\`ARMV8\` fallback, a failed verification, a noisy-neighbour p50/min spread — and we assert \`decompose.py\` surfaces each one.

Also plant a **null**: a dataset where the V1-set and V2-set are at parity, and assert the decision guide reads as \"publish the negative result\". An instrument that can only find hits is not an instrument.

No cloud spend in this phase."
  [P2]="Scope: one \`c8g.metal-48xl\` in the pinned region. \`build-libs.sh\` → \`run-matrix.sh\` → \`decompose.py\`.

Expect and resolve: OpenBLAS builds for targets the host lacks ISA for (mark unrunnable, do not crash), ArmPL install (a download from developer.arm.com, not a build), BLIS config selection.

Gate evidence required: a complete NDJSON set from one host, \`decompose.py\` running without warnings other than genuine findings, and \`numactl -H\` recorded — confirm whether \`c8g.metal-48xl\` at 192 vCPU is one socket or two.

**Cost estimate must be posted here before any instance launches.**"
  [P3]="Scope: all five families. Metal sizes where they exist; \`hpc7g\` has none, so run it repeatedly and lean on the p50/p90 spread. Capture \`numactl -H\` and \`capture-env.sh\` on every host before any timing.

Gate evidence required: five hosts' results collected, every host's \`env-*.json\` present, zero unresolved anomalies in \`decompose.py\` section 5 other than genuine findings.

**Cost estimate must be posted here before any instance launches.**"
  [P4]="Scope: produce the artifact that does not currently exist publicly — the deficit broken down by routine, size regime, and thread count, with the hardware × target cross resolved.

Gate evidence required: the report states a clear answer to \"is the N2 gap worth closing\", supported by section 2 of the decomposition, and states it as a null if that is what the data says."
)

for m in P0 P1 P2 P3 P4; do
  TITLE="$m umbrella: ${MILESTONES[$m]%% —*}"
  if gh issue list --repo "$REPO" --state all --limit 200 --search "\"$m umbrella\" in:title" \
       --json title --jq '.[].title' 2>/dev/null | grep -q "$m umbrella"; then
    log "umbrella issue for $m exists"
  else
    log "creating umbrella issue for $m"
    BODY="$(umbrella_body "$m" "${UMBRELLA_SCOPE[$m]}")"
    if [ -n "$DRY_RUN" ]; then
      printf '[dry-run] gh issue create --title %q\n' "$TITLE" >&2
    else
      gh issue create --repo "$REPO" --title "$TITLE" --body "$BODY" \
        --label "umbrella,gate,phase:$m" --milestone "$m" >&2
    fi
  fi
done

# ---- project board --------------------------------------------------------
if gh project list --owner "$OWNER" --format json 2>/dev/null \
     | grep -q '"title":"graviton-blas-bench"'; then
  log "project board exists"
else
  log "creating project board"
  run gh project create --owner "$OWNER" --title "'graviton-blas-bench'"
fi

log "done. Umbrella issues are the status channel -- see standing order 13."
