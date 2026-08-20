#!/usr/bin/env python3
"""Mutation testing: break the design on purpose, one line at a time.

A green suite says the tests did not fail. It does not say they would have.
Coverage does not close that gap either -- a bin can be hit by stimulus that
checks nothing. The direct question is: if this line were wrong, would anything
notice? So each mutant below is a single-line edit that a tired engineer could
plausibly make, applied to a clean tree, with the suite rerun against it.

A mutant that survives is not a joke at the testbench's expense, it is a
specific hole with a specific address. Survivors are reported with what they
changed and which stage should have objected.

Rules the mutants follow:

* One line, and a line someone might really write. `x + 1` where `x` belongs,
  a dropped guard, an inverted convention -- not `return None` in the middle of
  a function, which proves nothing about a test's ability to discriminate.
* Spread across the layers, because they fail differently. A mutation in
  `ir.py` moves the model *and* the RTL together, so only a test with an
  independent notion of the truth can see it -- that is what the hand-authored
  vectors in `tests/test_vectors.py` are for. A mutation in the RTL moves only
  one side, and the scoreboard sees it immediately. Both kinds are here on
  purpose.

    python3 ci/mutate.py                # every mutant
    python3 ci/mutate.py -k rotate      # matching ids only
    python3 ci/mutate.py --list
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

#: Mutation runs do not need the million-byte stress pass; a mutant that
#: survives 20 kB of mixed traffic would survive a million too.
STRESS = "20000"

#: Snapshot tests are excluded from mutation runs, and the exclusion is the
#: point rather than a convenience.
#:
#: A snapshot fails on *any* change to the emitted text, so it would kill every
#: mutation that touches the generator or a template -- including one that
#: changed nothing but whitespace. Counting that as a kill would report a
#: testbench far stronger than it is: the snapshot proves the output moved, not
#: that anything noticed the output was now wrong. With these deselected, a
#: mutant only dies if a check with an opinion about correctness objects.
NO_SNAPSHOT = (
    "--deselect=tests/test_emit.py::test_framed_snapshots_match",
    "--deselect=tests/test_emit.py::test_snapshots_match",
)


@dataclass
class Mutant:
    id: str
    what: str
    file: str
    old: str
    new: str
    stages: tuple[str, ...]
    regen: bool = False
    expect: str = ""  # what should notice, for the survivor report
    result: dict = field(default_factory=dict)


MUTANTS: list[Mutant] = [
    Mutant(
        id="ir-field-offset",
        what="one field's byte offset moved by one (trade.qty starts a byte late)",
        file="wirespec/ir.py",
        old="        offset += f.width_bits\n",
        new="        offset += f.width_bits + (8 if f.name == 'qty' else 0)\n",
        stages=("pytest",),
        expect=(
            "the hand-authored vectors and the builder round trip -- both know "
            "the layout without asking the IR"
        ),
    ),
    Mutant(
        id="ir-endian-flip",
        what="one field declared little-endian (status.timestamp)",
        file="wirespec/ir.py",
        old="                endian=f.endian,\n",
        new="                endian=(Endian.LITTLE if f.name == 'timestamp' else f.endian),\n",
        stages=("pytest", "sim:parser_feed:64"),
        expect="the byte-order vectors, and the RTL/model comparison",
    ),
    Mutant(
        id="ir-length-convention",
        what="length prefix treated as covering the whole message, not what follows it",
        file="wirespec/ir.py",
        old="            return total - self.length_field.width_bytes\n",
        new="            return total\n",
        stages=("pytest", "sim:parser_feed:64"),
        regen=True,
        expect="every message's declared size, in the model and in field_extract",
    ),
    Mutant(
        id="golden-sign-extend",
        what="unsigned fields sign-extended by the reference decoder",
        file="wirespec/golden.py",
        old="    if f.signed and raw >= (1 << (f.width - 1)):\n",
        new="    if f.width >= 8 and raw >= (1 << (f.width - 1)):\n",
        stages=("pytest", "sim:parser_feed:64"),
        expect="the RTL disagreeing with the model on any large unsigned value",
    ),
    Mutant(
        id="stitch-drop-carry",
        what="msg_stitch forgets what it was holding and keeps only this beat",
        file="rtl/common/msg_stitch.sv",
        old="    o_win    = restart ? beat_ext : (buf_q | beat_ext);\n",
        new="    o_win    = beat_ext;\n",
        stages=("sim:msg_stitch:64", "sim:parser_feed:64"),
        expect="msg_stitch's own model check, before the parser is even involved",
    ),
    Mutant(
        id="rotate-off-by-one",
        what="msg_rotate shifts one byte lane too far",
        file="rtl/common/msg_rotate.sv",
        old="  assign shamt = SH_W'({i_off, 3'b000});\n",
        new="  assign shamt = SH_W'({i_off, 3'b000}) + SH_W'(8);\n",
        stages=("sim:msg_rotate:64", "sim:parser_feed:64"),
        expect="the exhaustive offset sweep in tb/unit/test_msg_rotate.py",
    ),
    Mutant(
        id="framer-accept-while-idle",
        what="msg_stitch counts bytes from a beat that was never valid",
        file="rtl/common/msg_stitch.sv",
        old="    o_nvalid = base + (i_valid ? OFF_W'(i_bytes) : OFF_W'(0));\n",
        new="    o_nvalid = base + OFF_W'(i_bytes);\n",
        stages=("sim:msg_stitch:64", "sim:parser_feed:64"),
        expect="any test with an idle cycle in it -- the tvalid-gap runs",
    ),
    Mutant(
        id="top-unregister-msg-bus",
        what="the message bus taken combinationally again (undoes the B006 fix)",
        file="wirespec/templates/parser_top_framed.sv.j2",
        old="  assign o_msg_valid = msg_valid_q;\n",
        new="  assign o_msg_valid = fr_slot_valid;\n",
        stages=("pytest", "sim:parser_feed:128"),
        regen=True,
        expect=(
            "tests/test_emit.py's register check, and the straddle sweep at "
            "DATA_W=128 where a beat can hold a whole message"
        ),
    ),
    Mutant(
        id="top-unregister-strip-err",
        what="the runt verdict taken a cycle early again (undoes the B010 fix)",
        file="wirespec/templates/parser_top_framed.sv.j2",
        old="  assign pkt_done_any  = fr_done || strip_err_q;\n",
        new="  assign pkt_done_any  = fr_done || pay_strip_err;\n",
        stages=("sim:parser_feed:64",),
        regen=True,
        expect="test_runts_between_full_packets_no_idle",
    ),
    Mutant(
        id="framer-drop-tail-check",
        what="the tail classifier stops faulting (undoes the B009 fix)",
        file="rtl/common/msg_framer.sv",
        old="      assign has_len_w   = avail_w >= OFF_W'(LEN_OFF + LEN_BYTES);\n",
        new="      assign has_len_w   = (g < SLOTS) && (avail_w >= OFF_W'(LEN_OFF + LEN_BYTES));\n",
        stages=("sim:parser_feed:64",),
        expect="test_trailing_fragment_on_the_last_beat",
    ),
]


# --------------------------------------------------------------------------


def run(cmd: list[str], env: dict | None = None) -> tuple[int, str]:
    e = dict(os.environ)
    e["PYTHONPATH"] = str(ROOT) + os.pathsep + e.get("PYTHONPATH", "")
    e["WIRESPEC_STRESS_BYTES"] = STRESS
    # Mutation runs must not overwrite the real run's metric fragments.
    e["WIRESPEC_METRICS_DIR"] = str(ROOT / "build" / "mutation-scratch")
    if env:
        e.update(env)
    p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, env=e)
    return p.returncode, (p.stdout + p.stderr)


def regenerate() -> tuple[int, str]:
    rc, out = 0, ""
    for schema in ("eth_ipv4_udp", "simple_feed"):
        r, o = run(
            [sys.executable, "-m", "wirespec.cli", "gen", "--schema",
             f"schemas/{schema}.yaml", "--data-w", "64", "--out", "rtl/generated",
             "--quiet"]
        )
        rc |= r
        out += o
    return rc, out


def run_stage(stage: str) -> tuple[bool, str]:
    """True if the stage passed (i.e. failed to notice the mutation)."""
    if stage == "pytest":
        # No -x: which tests object, and how many, is the interesting part.
        rc, out = run([sys.executable, "-m", "pytest", "-q", *NO_SNAPSHOT])
        return rc == 0, out
    m = re.fullmatch(r"sim:(\w+):(\d+)", stage)
    assert m, f"unknown stage {stage!r}"
    rc, out = run(
        [sys.executable, "tb/run_sim.py", "-k", m.group(1), "--width", m.group(2)]
    )
    return rc == 0, out


def apply(mut: Mutant, path: Path, original: str) -> str:
    n = original.count(mut.old)
    if n != 1:
        raise SystemExit(
            f"mutant {mut.id}: anchor text appears {n} times in {mut.file}, "
            f"expected exactly 1. The source moved; update ci/mutate.py.\n"
            f"  looking for: {mut.old!r}"
        )
    return original.replace(mut.old, mut.new)


def evaluate(mut: Mutant) -> None:
    path = ROOT / mut.file
    original = path.read_text(encoding="utf-8")
    mutated = apply(mut, path, original)

    t0 = time.time()
    killed_by = None
    note = ""
    objectors: list[str] = []
    try:
        path.write_text(mutated, encoding="utf-8")
        if mut.regen:
            rc, out = regenerate()
            if rc != 0:
                killed_by = "generate"
                note = "the generator refused to emit"
        if killed_by is None:
            for stage in mut.stages:
                passed, out = run_stage(stage)
                if not passed:
                    killed_by = stage
                    objectors = failing_tests(out)
                    note = first_failure(out)
                    break
    finally:
        path.write_text(original, encoding="utf-8")
        if mut.regen:
            regenerate()

    mut.result = {
        "id": mut.id,
        "what": mut.what,
        "file": mut.file,
        "killed": killed_by is not None,
        "killed_by": killed_by,
        "objectors": objectors[:8],
        "objector_count": len(objectors),
        "detail": note,
        "expected_to_notice": mut.expect,
        "seconds": round(time.time() - t0, 1),
    }


def failing_tests(out: str) -> list[str]:
    """Names of the tests that objected.

    Recorded because "something failed" and "the check written for this failed"
    are different results, and only the second one says the suite has an opinion
    about the thing that was broken.
    """
    names = re.findall(r"^FAILED (\S+?)(?:\s|$)", out, re.MULTILINE)
    names += re.findall(r"^\s*\*\* (tb\.\S+)\s+FAIL", out, re.MULTILINE)
    seen, uniq = set(), []
    for n in names:
        if n not in seen:
            seen.add(n)
            uniq.append(n)
    return uniq


def first_failure(out: str) -> str:
    """One line from the log saying what objected. Enough to tell a real kill
    from a mutant that merely made something crash."""
    for pat in (
        r"^E\s+AssertionError: (.*)$",
        r"^AssertionError: (.*)$",
        r"^FAILED (\S+)",
        r"^\s*\*\* (tb\.\S+)\s+FAIL",
        r"%Error.*",
    ):
        m = re.search(pat, out, re.MULTILINE)
        if m:
            return m.group(0).strip()[:200]
    return "(failed without a recognisable message)"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-k", "--filter", default="", help="only mutants whose id matches")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--json", default=str(ROOT / "build" / "mutation.json"))
    args = ap.parse_args()

    chosen = [m for m in MUTANTS if args.filter in m.id]
    if args.list:
        for m in chosen:
            print(f"{m.id:<28} {m.what}")
        return 0

    # A dirty tree means the restore at the end of each mutant could put back
    # something that was never committed. Refuse rather than risk it.
    rc, out = run(["git", "status", "--porcelain", "--", *{m.file for m in chosen}])
    if out.strip():
        print("refusing to run: these files have uncommitted changes\n" + out)
        print("commit or stash them first -- each mutant restores from what is on disk.")
        return 2

    print(f"mutation testing: {len(chosen)} mutant(s), stress={STRESS} bytes\n")
    for i, m in enumerate(chosen, 1):
        print(f"[{i}/{len(chosen)}] {m.id:<28} ", end="", flush=True)
        evaluate(m)
        r = m.result
        mark = "KILLED  " if r["killed"] else "SURVIVED"
        who = ""
        if r["objectors"]:
            who = f"  <- {r['objectors'][0]}"
            if r["objector_count"] > 1:
                who += f" (+{r['objector_count'] - 1} more)"
        print(f"{mark} {r['seconds']:>5.1f}s  {r['killed_by'] or ''}{who}")

    killed = [m for m in chosen if m.result["killed"]]
    survived = [m for m in chosen if not m.result["killed"]]

    print(f"\n{len(killed)}/{len(chosen)} killed")
    for m in survived:
        print(f"\nSURVIVED  {m.id}")
        print(f"  changed : {m.what}")
        print(f"  in      : {m.file}")
        print(f"  should have been caught by: {m.expect}")

    payload = {
        "mutants": len(chosen),
        "killed": len(killed),
        "survived": len(survived),
        "stress_bytes": int(STRESS),
        "results": [m.result for m in chosen],
    }
    Path(args.json).parent.mkdir(parents=True, exist_ok=True)
    Path(args.json).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    from tb.common import metrics as met

    met.write("mutation", payload)
    return 0 if not survived else 1


if __name__ == "__main__":
    raise SystemExit(main())
