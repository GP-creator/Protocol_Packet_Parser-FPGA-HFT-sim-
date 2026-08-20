#!/usr/bin/env python3
"""Build and run the cocotb testbenches.

cocotb 2.0 dropped the 1.x Makefile-per-testbench convention in favour of
cocotb_tools.runner, which is what this uses: one place that knows every suite,
its top module, its sources and the DATA_W values it must pass at.

    python3 tb/run_sim.py                 # everything
    python3 tb/run_sim.py -k payload      # suites whose name matches
    python3 tb/run_sim.py --waves         # dump FST traces
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cocotb_tools.runner import get_runner  # noqa: E402

COMMON = ROOT / "rtl" / "common"
HAND = ROOT / "rtl" / "handwritten"
GEN = ROOT / "rtl" / "generated"

PKG = [COMMON / "pkg_wirespec.sv"]


@dataclass
class Suite:
    name: str
    toplevel: str
    module: str
    sources: list[Path]
    widths: tuple[int, ...] = (64,)
    parameters: dict[str, object] = field(default_factory=dict)


SUITES: list[Suite] = [
    Suite(
        name="axis_reg_slice",
        toplevel="axis_reg_slice",
        module="tb.unit.test_axis_reg_slice",
        sources=[COMMON / "axis_reg_slice.sv"],
        widths=(64, 128),
    ),
    Suite(
        name="pkt_align",
        toplevel="pkt_align",
        module="tb.unit.test_pkt_align",
        sources=PKG + [COMMON / "pkt_align.sv"],
        widths=(64, 128),
    ),
    Suite(
        name="payload_window",
        toplevel="payload_window",
        module="tb.unit.test_payload_window",
        sources=PKG + [COMMON / "payload_window.sv"],
        widths=(64, 128),
    ),
    # The same test module runs against the hand-written M2 prototype and
    # against the generated RTL. Identical results is the M3 acceptance
    # criterion, so it is checked by construction rather than by inspection.
    Suite(
        name="msg_rotate",
        toplevel="msg_rotate",
        module="tb.unit.test_msg_rotate",
        sources=[COMMON / "msg_rotate.sv"],
        widths=(64, 128),
        parameters={"IN_BYTES": 0, "OUT_BYTES": 32},  # IN_BYTES fixed up per width
    ),
    Suite(
        name="msg_stitch",
        toplevel="msg_stitch",
        module="tb.unit.test_msg_stitch",
        sources=[COMMON / "msg_stitch.sv"],
        widths=(64, 128),
        parameters={"MSG_MAX": 32},
    ),
    Suite(
        name="parser_eth_hand",
        toplevel="parser_top_eth_ipv4_udp",
        module="tb.integration.test_parser_eth",
        sources=PKG
        + [
            COMMON / "pkt_align.sv",
            COMMON / "hdr_accum.sv",
            COMMON / "payload_window.sv",
            HAND / "hdr_parse_eth_ipv4_udp.sv",
            HAND / "parser_top_eth_ipv4_udp.sv",
        ],
        widths=(64, 128),
    ),
    Suite(
        name="parser_eth_gen",
        toplevel="parser_top_eth_ipv4_udp",
        module="tb.integration.test_parser_eth",
        sources=PKG
        + [
            GEN / "eth_ipv4_udp_pkg.sv",
            COMMON / "pkt_align.sv",
            COMMON / "hdr_accum.sv",
            COMMON / "payload_window.sv",
            GEN / "hdr_parse_eth_ipv4_udp.sv",
            GEN / "parser_top_eth_ipv4_udp.sv",
        ],
        widths=(64, 128),
    ),
    Suite(
        name="parser_feed",
        toplevel="parser_top_simple_feed",
        module="tb.integration.test_parser_feed",
        sources=PKG
        + [
            GEN / "simple_feed_pkg.sv",
            COMMON / "pkt_align.sv",
            COMMON / "hdr_accum.sv",
            COMMON / "payload_window.sv",
            COMMON / "msg_rotate.sv",
            COMMON / "msg_stitch.sv",
            COMMON / "msg_framer.sv",
            COMMON / "stats.sv",
            GEN / "hdr_parse_simple_feed.sv",
            GEN / "field_extract_simple_feed.sv",
            GEN / "msg_check_simple_feed.sv",
            GEN / "parser_top_simple_feed.sv",
        ],
        widths=(64, 128),
    ),
]


def run_one(suite: Suite, width: int, *, waves: bool, verbose: bool) -> bool:
    tag = f"{suite.name}_w{width}"
    build_dir = ROOT / "sim_build" / tag
    params = dict(suite.parameters)
    if suite.toplevel == "msg_rotate":
        # msg_rotate has no DATA_W of its own; size it the way msg_framer does.
        params["IN_BYTES"] = params["OUT_BYTES"] - 1 + width // 8
    else:
        params["DATA_W"] = width

    runner = get_runner("verilator")
    runner.build(
        sources=[str(p) for p in suite.sources],
        hdl_toplevel=suite.toplevel,
        parameters=params,
        build_dir=str(build_dir),
        build_args=["--trace-fst", "--trace-structs"] if waves else [],
        always=True,
        waves=waves,
        verbose=verbose,
    )

    env = dict(os.environ)
    env["DATA_W"] = str(width)
    # The same test module runs against the hand-written prototype and against
    # the generated RTL. Their metric fragments must not overwrite each other:
    # "identical numbers from two builds" is the M3 claim, and it can only be
    # checked if both sets survive.
    env["WIRESPEC_SUITE"] = suite.name
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")

    results = runner.test(
        hdl_toplevel=suite.toplevel,
        test_module=suite.module,
        build_dir=str(build_dir),
        test_dir=str(build_dir),
        results_xml=f"{tag}.xml",
        extra_env=env,
        waves=waves,
        verbose=verbose,
    )

    from cocotb_tools.runner import get_results

    # get_results returns (total, failed), not (passed, failed).
    total, failed = get_results(results)
    status = "PASS" if failed == 0 else "FAIL"
    print(f"  {tag}: {total - failed}/{total} passed  [{status}]", flush=True)
    TALLY.append({"suite": suite.name, "data_w": width, "tests": total, "failed": failed})
    return failed == 0


#: Per-suite results, folded into one metrics fragment at the end of main().
TALLY: list[dict] = []


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-k", "--filter", default="", help="only suites whose name contains this")
    ap.add_argument("--waves", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--width", type=int, action="append", help="restrict to these DATA_W values")
    args = ap.parse_args()

    ok = True
    ran = 0
    for suite in SUITES:
        if args.filter and args.filter not in suite.name:
            continue
        widths = [w for w in suite.widths if not args.width or w in args.width]
        for width in widths:
            print(f"\n=== {suite.name} DATA_W={width} ===", flush=True)
            ran += 1
            try:
                ok &= run_one(suite, width, waves=args.waves, verbose=args.verbose)
            except Exception as exc:  # a build failure must not look like a pass
                print(f"  {suite.name}_w{width}: ERROR {exc}", flush=True)
                ok = False

    if ran == 0:
        print("no suites selected", file=sys.stderr)
        return 2

    # Only a full run describes the whole testbench, so a filtered one says so
    # rather than letting `metrics.json` report a subset as if it were the total.
    from tb.common import metrics as met

    met.write(
        "cocotb",
        {
            "suite_runs": ran,
            "tests": sum(t["tests"] for t in TALLY),
            "failed": sum(t["failed"] for t in TALLY),
            "suites": len({t["suite"] for t in TALLY}),
            "widths": sorted({t["data_w"] for t in TALLY}),
            "filtered": bool(args.filter or args.width),
            "by_suite": TALLY,
        },
    )

    print(f"\n{ran} suite run(s): {'all passed' if ok else 'FAILURES'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
