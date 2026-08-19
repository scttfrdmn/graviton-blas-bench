---
name: Bug report
about: A harness, analysis, or build problem
title: ''
labels: bug
assignees: ''
---

## What happened

<!-- What you ran and what it did. -->

## What you expected

## Provenance (required)

A measurement report without provenance is not actionable — see standing order 5
in `CLAUDE.md`. Please attach or paste:

- **Instance type** (e.g. `c8g.metal-48xl`), or `local` if not on EC2:
- **`env-*.json`** from `results/` for the affected run (this carries MIDR,
  HWCAP, governor, NUMA topology, and the `DYNAMIC_ARCH` selection):

```json
paste here
```

- **OpenBLAS SHA** as recorded in `build-manifest.ndjson`:
- **Which `gbb-*` binary** was used (e.g. `bin/gbb-openblas-NEOVERSEV1`):
- **Compiler and version** (`cc --version | head -1`):

## Reproduction

```bash
# exact commands
```

## Relevant output

<!-- Paste the failing NDJSON records, the decompose.py section, or the
     *.buildlog tail. Please don't truncate error text. -->
