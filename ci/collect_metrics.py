#!/usr/bin/env python3
"""Merge this run's metric fragments into metrics.json.

Two kinds of number end up in the file.

*Static* facts are counted here, from the tree as it stands: line counts,
module counts, the defect log. They are cheap and always available.

*Measured* facts come from `build/metrics/`, where each stage of `ci/check.sh`
drops a JSON fragment as it finishes -- pytest, each cocotb suite, the linter.
Those stages are separate processes and cannot hand numbers to each other, and
`ci/check.sh` clears the directory before it starts, so `metrics.json` describes
one run rather than the union of every run since the last clean.

A stage that did not run leaves no fragment, and its section is absent rather
than stale. `"complete": false` says so at the top level, which matters: a
number carried over from a previous run is worse than no number.

    python3 ci/collect_metrics.py [--out metrics.json]
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tb.common import metrics as met  # noqa: E402

#: Every stage that contributes measured numbers. Missing ones are reported.
EXPECTED = ("pytest", "cocotb", "lint")


def lines(path: Path) -> int:
    try:
        return len(path.read_text(encoding="utf-8", errors="replace").splitlines())
    except OSError:
        return 0


def total_lines(paths) -> int:
    return sum(lines(p) for p in paths)


def sv_modules(path: Path) -> list[str]:
    """Module names declared in one SystemVerilog file (packages excluded)."""
    text = path.read_text(encoding="utf-8", errors="replace")
    return re.findall(r"^\s*module\s+(\w+)", text, re.MULTILINE)


def git_head() -> dict:
    def run(*args):
        try:
            out = subprocess.run(
                ["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=10
            )
            return out.stdout.strip() if out.returncode == 0 else None
        except (OSError, subprocess.SubprocessError):
            return None

    return {
        "commit": run("rev-parse", "--short", "HEAD"),
        "tag": run("describe", "--tags", "--abbrev=0"),
        "dirty": bool(run("status", "--porcelain")),
    }


# --------------------------------------------------------------------------
# Defects
# --------------------------------------------------------------------------


def defects() -> dict:
    """Parse docs/bugs-found.md. The log is the source of truth, not a tally
    kept somewhere else that can drift from it."""
    path = ROOT / "docs" / "bugs-found.md"
    if not path.is_file():
        return {"total": 0, "entries": []}

    text = path.read_text(encoding="utf-8", errors="replace")
    entries = []
    for block in re.split(r"^## ", text, flags=re.MULTILINE)[1:]:
        head = block.splitlines()[0]
        # The log is written for people, so the separator is an em dash. Accept
        # a plain "--" too rather than making the prose serve the parser.
        m = re.match(r"(B\d+)\s*(?:--|—|-)\s*(.*)", head)
        if not m:
            continue
        ident, title = m.group(1), m.group(2).strip()
        mile = re.search(
            r"\*\*Milestone:\*\*\s*(\w+)(?:\s*[·.]\s*(\S+))?", block
        )
        entries.append(
            {
                "id": ident,
                "title": title,
                "milestone": mile.group(1) if mile else None,
                "where": mile.group(2) if mile and mile.group(2) else None,
            }
        )

    by_milestone: dict[str, int] = {}
    by_where: dict[str, int] = {}
    for e in entries:
        by_milestone[e["milestone"] or "?"] = by_milestone.get(e["milestone"] or "?", 0) + 1
        key = e["where"] or "(schema/tooling)"
        by_where[key] = by_where.get(key, 0) + 1

    return {
        "total": len(entries),
        "highest": max((e["id"] for e in entries), default=None),
        "by_milestone": dict(sorted(by_milestone.items())),
        "by_module": dict(sorted(by_where.items(), key=lambda kv: -kv[1])),
        "entries": entries,
    }


# --------------------------------------------------------------------------
# Size
# --------------------------------------------------------------------------


def size() -> dict:
    schemas = sorted((ROOT / "schemas").glob("*.yaml"))
    gen = sorted((ROOT / "rtl" / "generated").glob("*.sv"))
    common = sorted((ROOT / "rtl" / "common").glob("*.sv"))
    hand = sorted((ROOT / "rtl" / "handwritten").glob("*.sv"))
    templates = sorted((ROOT / "wirespec" / "templates").glob("*.j2"))
    tool = sorted((ROOT / "wirespec").glob("*.py"))
    tb = sorted((ROOT / "tb").rglob("*.py")) + sorted((ROOT / "tests").glob("*.py"))

    # Per protocol, so the ratio is a statement about one schema rather than an
    # average over two of different kinds.
    per_proto = {}
    for s in schemas:
        name = s.stem
        files = [p for p in gen if p.name.startswith(name) or p.name.endswith(f"_{name}.sv")]
        src = lines(s)
        out = total_lines(files)
        per_proto[name] = {
            "schema_lines": src,
            "generated_lines": out,
            "generated_files": len(files),
            "ratio": round(out / src, 1) if src else None,
        }

    common_modules = [m for p in common for m in sv_modules(p)]
    gen_modules = [m for p in gen for m in sv_modules(p)]

    return {
        "schema_lines": total_lines(schemas),
        "generated_rtl_lines": total_lines(gen),
        "generated_rtl_ratio": (
            round(total_lines(gen) / total_lines(schemas), 1) if schemas else None
        ),
        "per_protocol": per_proto,
        "handwritten_rtl_lines": total_lines(common) + total_lines(hand),
        "template_lines": total_lines(templates),
        "generator_lines": total_lines(tool),
        "testbench_lines": total_lines(tb),
        "modules": {
            "handwritten_common": sorted(common_modules),
            "handwritten_common_count": len(common_modules),
            "handwritten_m2_prototype": sorted(m for p in hand for m in sv_modules(p)),
            "generated": sorted(gen_modules),
            "generated_count": len(gen_modules),
        },
        "files": {
            p.relative_to(ROOT).as_posix(): lines(p)
            for p in (*schemas, *common, *gen, *templates)
        },
    }


# --------------------------------------------------------------------------
# Measured
# --------------------------------------------------------------------------


def measured(frags: dict) -> dict:
    out: dict = {}

    if "pytest" in frags:
        out["pytest"] = frags["pytest"]
    if "cocotb" in frags:
        out["cocotb"] = frags["cocotb"]
    if "lint" in frags:
        out["lint"] = frags["lint"]
    if "mutation" in frags:
        out["mutation"] = frags["mutation"]

    # Per-configuration parser numbers, keyed exactly as the suite ran them.
    parsers = {k: v for k, v in frags.items() if k.startswith("parser_")}
    if parsers:
        out["parsers"] = dict(sorted(parsers.items()))
        out["totals_by_config"] = {
            k: {
                "bytes": v.get("bytes"),
                "packets": v.get("packets"),
                "messages": v.get("messages"),
                "field_comparisons": v.get("field_comparisons"),
                "mismatches": v.get("mismatches"),
            }
            for k, v in sorted(parsers.items())
        }

    # Malformed-input counts are per protocol, not per width -- the same corpus
    # runs at every DATA_W. Grouping them keeps "31 cases" from being read as
    # "31 per configuration", and the by_config breakdown shows if a width was
    # somehow given a different corpus.
    malformed = {k: v for k, v in frags.items() if k.startswith("malformed_")}
    if malformed:
        groups: dict[str, dict[str, int]] = {}
        for k, v in sorted(malformed.items()):
            proto = "simple_feed" if "feed" in k else "eth_ipv4_udp"
            groups.setdefault(proto, {})[k] = v.get("cases", 0)
        out["malformed"] = {
            proto: {
                "cases": max(byconf.values()),
                "same_at_every_width": len(set(byconf.values())) == 1,
                "by_config": byconf,
            }
            for proto, byconf in groups.items()
        }
        out["malformed"]["total_cases"] = sum(
            g["cases"] for g in out["malformed"].values() if isinstance(g, dict)
        )
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "metrics.json"))
    args = ap.parse_args()

    frags = met.load_all()
    missing = [s for s in EXPECTED if s not in frags]

    doc = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "git": git_head(),
        "complete": not missing,
        "stages_missing": missing,
        "defects": defects(),
        "size": size(),
        "measured": measured(frags),
    }

    Path(args.out).write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")

    d, s, m = doc["defects"], doc["size"], doc["measured"]
    print(f"metrics.json: {len(json.dumps(doc))} bytes")
    print(f"  defects       {d['total']} logged, highest {d['highest']}, {d['by_milestone']}")
    print(
        f"  size          {s['schema_lines']} schema lines -> "
        f"{s['generated_rtl_lines']} generated RTL lines "
        f"({s['generated_rtl_ratio']}x)"
    )
    print(
        f"  modules       {s['modules']['handwritten_common_count']} hand-written, "
        f"{s['modules']['generated_count']} generated"
    )
    if "pytest" in m:
        print(f"  pytest        {m['pytest'].get('passed')} passed")
    if "cocotb" in m:
        c = m["cocotb"]
        print(f"  cocotb        {c.get('tests')} tests over {c.get('suite_runs')} suite runs")
    if "lint" in m:
        print(
            f"  lint          {m['lint'].get('invocations')} invocations, "
            f"{m['lint'].get('warnings')} warnings"
        )
    for k, v in m.get("totals_by_config", {}).items():
        msgs = f", {v['messages']} messages" if v.get("messages") is not None else ""
        print(
            f"  {k:<24}{v['bytes']} bytes, {v['packets']} packets{msgs}, "
            f"{v['mismatches']} mismatches"
        )
    if "mutation" in m:
        print(f"  mutation      {m['mutation']['killed']}/{m['mutation']['mutants']} killed")
    if missing:
        print(f"  INCOMPLETE    no fragment from: {', '.join(missing)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
