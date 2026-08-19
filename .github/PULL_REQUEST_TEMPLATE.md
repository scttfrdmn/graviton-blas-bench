## What this changes

## Why

<!-- Link the issue. If this touches anything that produces a number, say which
     conclusion it affects. -->

Closes #

## Checklist

- [ ] `bash -n` clean on every changed script; `shellcheck` warnings addressed
- [ ] `ruff check .` and `ruff format --check .` pass
- [ ] `make roofline` builds and `bin/gbb-roofline` emits a *plausible* number
      (standing order 2 — the FMA chain gets folded away if you let it)
- [ ] No `-O3` and no `-march=native` introduced into the harness build
      (standing order 6 — the harness must not be what differs between arms)
- [ ] `CHANGELOG.md` updated under `## [Unreleased]`
- [ ] Conventional Commit messages

## Measurement impact

- [ ] None — does not touch anything that produces a number
- [ ] Additive — new measurements only; already-collected results stay valid
- [ ] **Breaks comparability** — explain below

<!-- If comparability breaks, state exactly which already-collected results are
     invalidated and must be re-run. Changing the routine set, the size regimes,
     the thread ladder, or the denominator policy in standing order 1 requires
     sign-off before merge. Verification tolerances are not to be relaxed to make
     records pass (standing order 4). -->
