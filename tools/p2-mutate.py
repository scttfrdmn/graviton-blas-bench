#!/usr/bin/env python3
"""Break one thing about a P2 results directory, in place, for gates/p2.sh --self-test.

    GBB_P2_MUTATE='drop_arm(coretype="ARMV8")' tools/p2-mutate.py DIR

This exists so gate P2's negative controls are real. A self-test that only showed the
gate going green on a clean fixture would be passed by `exit 0`; what has to be
demonstrated is that each requirement, removed, turns the gate red — and for the
reason it names, not incidentally. So there is one mutation per requirement, each
named after the defect it plants rather than after the field it edits.

Operates on a COPY the caller made, and refuses a directory that is not obviously a
fixture: every mutation here produces a dataset that looks like a real one and is a
lie, so pointing it at `results/` would fabricate a measurement. The guard is the
matrix_id namespace, which is the same by-construction quarantine gate P2 itself
uses (bench.c writes bare hex, synth.py prefixes `synth-`).

Prints a one-line description of what it did; the gate quotes that line when a mutant
unexpectedly passes, because "the gate went green" is only actionable alongside "and
here is what was broken".
"""

import json
import os
import pathlib
import sys

RES = None


def _bench_files():
    return sorted(RES.glob("bench-*.ndjson"))


def _rewrite(path, fn):
    """Map fn over every record in one ndjson file. fn returns a dict or None to drop."""
    kept, dropped = [], 0
    for line in path.read_text(errors="replace").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        out = fn(r) if isinstance(r, dict) else r
        if out is None:
            dropped += 1
        else:
            kept.append(json.dumps(out))
    path.write_text("\n".join(kept) + ("\n" if kept else ""))
    return dropped


def _is_bench(r):
    return "routine" in r


def drop_arm(coretype=None, threads=None):
    """Remove an arm's bench records entirely, or only its records at one thread count.

    Two different claims. Without `threads` the arm produced nothing, which is what a
    dropped GBB_CORETYPES entry looks like. With it the arm ran but not at the thread
    point the cost extrapolation is taken at, which is what a shortened ladder looks
    like — and which a check that only asked "is the arm present" would miss."""
    n = 0
    for p in _bench_files():
        n += _rewrite(
            p,
            lambda r: None
            if (
                _is_bench(r)
                and (coretype is None or (r.get("coretype") or "").upper() == coretype.upper())
                and (threads is None or r.get("threads") == threads)
            )
            else r,
        )
    where = "" if threads is None else f" at threads={threads}"
    return f"dropped {n} bench records for coretype={coretype}{where}"


def drop_probe():
    """Remove the floor-overlap band, leaving the matrix records untouched.

    The shape of a dataset collected by a bench.c from before the probe existed, which
    is exactly why ABSENT does not set exit bit 32 and why requiring the probe is gate
    P2's job rather than decompose.py's."""
    n = 0
    for p in _bench_files():
        n += _rewrite(p, lambda r: None if _is_bench(r) and (r.get("probe") or "none") != "none" else r)
    return f"dropped {n} floor-overlap probe records"


def strip_field(field):
    """Delete one field from every bench record.

    Models a sweep run by an older binary rather than a corrupted file: the records
    are well-formed and complete in every other respect, which is what makes a missing
    field the hard case. A truncated file announces itself."""
    n = 0

    def f(r):
        nonlocal n
        if _is_bench(r) and field in r:
            r = dict(r)
            del r[field]
            n += 1
        return r

    for p in _bench_files():
        _rewrite(p, f)
    return f"removed {field!r} from {n} bench records"


def truncate_arm(coretype, keep=0.8):
    """Cut the tail off one arm's case set, leaving the rest of the dataset intact.

    A sweep killed partway through — the shape a spot reclaim or an OOM produces. The
    point of confining it to ONE arm is that this is the version decompose.py can also
    see (as `partial`); the version only the matrix stamp can see is every arm being
    short, and no mutation here can plant that without also moving matrix_cases, which
    is the guarantee the stamp exists to provide."""
    cases = []
    seen = set()
    for p in _bench_files():
        for line in p.read_text(errors="replace").splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            if not isinstance(r, dict) or not _is_bench(r):
                continue
            if (r.get("coretype") or "").upper() != coretype.upper():
                continue
            key = (r.get("routine"), r.get("m"), r.get("n"), r.get("k"), r.get("lda_pad"), r.get("incx"))
            if key not in seen:
                seen.add(key)
                cases.append(key)
    cut = set(cases[int(len(cases) * keep):])
    n = 0
    for p in _bench_files():
        n += _rewrite(
            p,
            lambda r: None
            if (
                _is_bench(r)
                and (r.get("coretype") or "").upper() == coretype.upper()
                and (r.get("routine"), r.get("m"), r.get("n"), r.get("k"), r.get("lda_pad"), r.get("incx"))
                in cut
            )
            else r,
        )
    return f"cut {len(cut)} of {len(cases)} cases ({n} records) from coretype={coretype}"


def drop_files(pattern):
    """Delete a whole file family."""
    gone = [p.name for p in sorted(RES.glob(pattern))]
    for p in RES.glob(pattern):
        p.unlink()
    return f"deleted {len(gone)} file(s) matching {pattern!r}: {', '.join(gone)}"


def second_instance_id():
    """Copy the env file under a second run_id with a different instance_id.

    The same instance_type on a different instance_id is precisely how the spend
    policy IDENTIFIES a P3 replicate, so this is not a cosmetic edit: a P2 directory
    holding two of them is either two pooled passes or a mis-shipped prefix, and the
    replicate machinery downstream would read it as evidence of reproduction."""
    src = sorted(RES.glob("env-*.json"))
    if not src:
        raise SystemExit("no env-*.json to duplicate")
    e = json.loads(src[0].read_text())
    e["instance_id"] = "i-0dead0000000dead"
    e["run_id"] = (e.get("run_id") or "run") + "-b"
    (RES / f"env-{e['run_id']}.json").write_text(json.dumps(e, indent=2) + "\n")
    return f"added env-{e['run_id']}.json on instance_id={e['instance_id']}"


def retype(instance_type):
    """Restamp the whole pass as a different instance type.

    Not a partial edit: every env file and every record moves together, so the dataset
    is internally consistent and merely on the wrong host. A gate that checked only
    for internal agreement would pass it."""
    n = 0
    for p in sorted(RES.glob("env-*.json")):
        e = json.loads(p.read_text())
        e["instance_type"] = instance_type
        p.write_text(json.dumps(e, indent=2) + "\n")
        n += 1
    for pat in ("bench-*.ndjson", "census-*.ndjson", "manifest-*.ndjson", "roofline-*.ndjson"):
        for p in sorted(RES.glob(pat)):

            def f(r):
                if "instance" in r:
                    r = dict(r)
                    r["instance"] = instance_type
                return r

            _rewrite(p, f)
    return f"restamped {n} env file(s) and every record as instance_type={instance_type}"


OPS = {
    "drop_arm": drop_arm,
    "drop_probe": drop_probe,
    "strip_field": strip_field,
    "truncate_arm": truncate_arm,
    "drop_files": drop_files,
    "second_instance_id": second_instance_id,
    "retype": retype,
}


def main(argv=None):
    global RES
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 1:
        print(__doc__.splitlines()[2].strip(), file=sys.stderr)
        return 2
    RES = pathlib.Path(argv[0])
    if not RES.is_dir():
        print(f"{RES} is not a directory", file=sys.stderr)
        return 2

    # Refuse anything that is not a fixture. Every op here writes a dataset that looks
    # measured and is not one; run against results/ it would fabricate a measurement,
    # which standing order 3 puts above any convenience this tool has.
    stamps = set()
    for p in sorted(RES.glob("bench-*.ndjson")):
        for line in p.read_text(errors="replace").splitlines():
            if line.strip():
                r = json.loads(line)
                if isinstance(r, dict) and "routine" in r:
                    stamps.add(r.get("matrix_id"))
    if not stamps or not all((s or "").startswith("synth-") for s in stamps):
        print(
            f"refusing {RES}: matrix_id {sorted(map(str, stamps))} is not tools/synth.py's namespace, "
            f"so this is not a fixture. Every mutation here plants a plausible lie; pointing it at "
            f"real results would fabricate a measurement.",
            file=sys.stderr,
        )
        return 3

    expr = os.environ.get("GBB_P2_MUTATE", "").strip()
    if not expr:
        print("GBB_P2_MUTATE is unset; nothing to do", file=sys.stderr)
        return 2
    # eval against OPS only, with no builtins: the expressions come from gates/p2.sh's
    # own table, and keeping the namespace to the seven ops means a typo is a NameError
    # naming the op rather than something that half-runs.
    try:
        print(eval(expr, {"__builtins__": {}}, dict(OPS)))
    except Exception as exc:
        print(f"mutation {expr!r} failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
