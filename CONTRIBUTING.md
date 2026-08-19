# Contributing

Thanks for looking at `graviton-blas-bench`. This is a harness for deciding
whether OpenBLAS kernel work on AWS Graviton is worth doing, and for producing a
publishable decomposition either way.

Read `README.md` first, especially **Measurement discipline** and **Hazards,
learned the hard way**. Most of what looks like an arbitrary constraint in this
repo is there because a specific measurement went wrong once.

## Before you start: this is a measurement campaign

Results are collected across five instance families over weeks, then compared
against each other. That makes the code less like a library and more like an
instrument: a change that improves the code but shifts the numbers invalidates
every measurement taken before it.

So if your PR touches **how a number is produced** — the timing loop, warmup
policy, rep-count scaling, the choice of statistic, the size regimes, the
routine set, the thread ladder, the denominator policy, or the harness compiler
flags — say so explicitly in the PR description and explain the effect on
comparability with results already collected. Three answers are all acceptable:

- results stay comparable, and here is why;
- results do not stay comparable, and the already-collected set needs re-running
  (say which arms);
- results do not stay comparable, and the change is worth it anyway (say why).

What is not acceptable is a silent methodology change. A follow-on effect of
that policy: don't relax a verification tolerance to make records pass, and
don't fill a gap in the matrix with a number you did not measure.

## Build

```bash
make roofline     # bin/gbb-roofline, the peak-FMA and triad probe; no BLAS needed
make all          # same as above -- the default target builds only what needs no BLAS
```

The per-library bench binaries need a prefix pointing at the library under test
and are normally driven by `scripts/build-libs.sh` rather than invoked by hand:

```bash
make openblas OPENBLAS_DIR=... VARIANT=NEOVERSEV1
make armpl    ARMPL_DIR=...
make blis     BLIS_DIR=...
make reference                     # netlib BLAS: the slow-but-correct control
```

`make clean` removes `bin/`.

## Linters

CI runs exactly these; run them locally before opening a PR.

```bash
bash -n scripts/*.sh                       # shell syntax check, every script
ruff check .                               # Python lint
python -m py_compile analysis/decompose.py tools/*.py
```

Plus the compile itself, which is the C-side check:

```bash
make roofline
```

`analysis/` and `tools/` are Python 3 and **stdlib-only** — there is no
package manifest and no dependency set. If your change needs a third-party
package, raise it in an issue first; adding one changes what has to be installed
on every host in the campaign.

## Style

`.editorconfig` is authoritative: LF endings, final newline, no trailing
whitespace, 4-space indent for C and Python, 2-space for shell/YAML/JSON,
88-column limit for Python.

Two hard rules on the C side, both load-bearing:

- **No `-march=native` and no `-O3` in the harness build.** The harness must be
  identical on every host; only the BLAS under test varies. `-O2` is the
  setting.
- **Don't reintroduce the optimizer hazard in `src/roofline.c`.** The FMA chain
  reads its constants from volatile storage and a hard sanity bound aborts the
  run, because an early draft folded the chain away and reported 927 TFLOP/s on
  one core. If you touch that file, check the number is physically plausible
  before trusting anything downstream of it.

## Commit messages

[Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/):

```
<type>(<optional scope>): <description>

[optional body]

[optional footer(s)]
```

Types in use: `feat`, `fix`, `perf`, `docs`, `test`, `refactor`, `build`, `ci`,
`chore`, `revert`. Useful scopes here: `bench`, `roofline`, `build-libs`,
`run-matrix`, `capture-env`, `decompose`, `gates`, `tools`, `docs`.

Use `!` or a `BREAKING CHANGE:` footer for anything that breaks the NDJSON
record schema or the CLI of a script, since both are consumed by already-written
analysis.

```
feat(bench): record p90 alongside p50 and min
fix(capture-env): warn on unrecognised MIDR instead of exiting 0
docs: note that hpc7g has no metal size
perf(roofline)!: change triad array sizing -- invalidates prior bandwidth numbers
```

## Branches and pull requests

`main` is the default branch and is not committed to directly.

1. Branch from an up-to-date `main`: `<type>/<short-slug>`, e.g.
   `fix/armpl-serial-link` or `feat/synth-null-dataset`.
2. Commit in Conventional Commits form.
3. Update `CHANGELOG.md` under `## [Unreleased]`.
4. Run the linters above.
5. Open a PR against `main` and fill in the template. Link the issue it closes.
6. CI must be green. Squash merge; the PR title becomes the commit subject, so
   it needs to be a valid Conventional Commit line too.

Small, single-purpose PRs get reviewed quickly. A PR that mixes a methodology
change with unrelated refactoring will be asked to split.

## Issues

Bug reports about a measurement need provenance to be actionable: the instance
type, the `env-*.json` record from `scripts/capture-env.sh`, the OpenBLAS SHA,
and the exact `gbb-*` binary you ran. The issue template asks for these. Without
them a report cannot be told apart from a noisy neighbour, so it can't be
investigated.

## Versioning and releases

[SemVer 2.0.0](https://semver.org/spec/v2.0.0.html). Nothing is released yet;
the first tag will be `v0.0.1`. While the version is `0.x`, the NDJSON schema
and script interfaces may change between minor versions — the CHANGELOG is the
record of when.

## License

By contributing you agree that your contributions are licensed under the MIT
License, the same terms as the rest of the project. See `LICENSE`.
