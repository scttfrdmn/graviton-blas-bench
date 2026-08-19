# Security Policy

## What this project is, in security terms

`graviton-blas-bench` is a benchmark harness. It runs locally and on EC2
instances the operator owns, it opens no sockets, and it exposes no network
service, daemon or API. It reads no untrusted input at runtime: the inputs are
matrices it generates itself and the environment variables the operator sets.
Nothing here is intended to be deployed, and nothing here handles credentials or
user data.

The realistic surface is therefore narrow and it is in the build path, not the
measurement path: `scripts/build-libs.sh` fetches third-party sources over the
network — OpenBLAS and BLIS — and then compiles and runs them on the operator's
host. ArmPL is likewise an out-of-band download from developer.arm.com that the
operator installs and points `ARMPL_DIR` at. Anything that could weaken that
fetch-and-build chain is in scope and worth reporting:

- a download without integrity checking, or one that could be redirected;
- a pinned ref that isn't actually pinned, or a SHA that isn't verified;
- unsafe handling of paths or environment variables that lets a build step write
  or execute outside its prefix;
- shell injection in a script through an unquoted expansion;
- a workflow permission or secret exposure in CI.

Out of scope: the absence of hardening in the C harness against inputs it
generates itself, resource exhaustion from deliberately large problem sizes (the
harness is meant to saturate the machine), and anything requiring the reporter to
already have shell on the operator's instance.

## Supported versions

| Version | Supported |
|---|---|
| `main` (unreleased, pre-`v0.0.1`) | Yes |

Nothing has been released yet. Until `v0.0.1` is tagged, `main` is the only
supported code and fixes land there. After that, only the latest released minor
version receives fixes; there are no long-term support branches.

## Reporting a vulnerability

Please report privately, not in a public issue.

Use GitHub's private vulnerability reporting: go to the
[Security tab](https://github.com/scttfrdmn/graviton-blas-bench/security/advisories)
of the repository and open a draft security advisory ("Report a vulnerability").
That keeps the report private to the maintainer until a fix is available.

Include what you have:

- what the issue is and which file or script it is in;
- how to reproduce it, ideally the exact command;
- what an attacker gets out of it;
- the commit SHA you looked at.

## What to expect

- **Acknowledgement within 5 business days.** If you haven't heard back in that
  window, assume the report was missed and open a public issue saying only that
  you are waiting on a private report — no details.
- An assessment of whether it's in scope, and if so a rough fix timeline, in the
  same advisory thread.
- Credit in the advisory and the CHANGELOG unless you'd rather not be named.

This is a small research project maintained by one person; there is no bug
bounty, and response is best-effort within the window above.
