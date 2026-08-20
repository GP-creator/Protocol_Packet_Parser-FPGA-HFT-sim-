"""wirespec command line.

    wirespec gen --schema schemas/eth_ipv4_udp.yaml --data-w 64 --out rtl/generated
    wirespec check --schema schemas/invalid/dup_field.yaml
    wirespec info  --schema schemas/simple_feed.yaml
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .emit import render, write
from .golden import decode
from .ir import build_ir
from .layout import SUPPORTED_DATA_W, LayoutError, build_layout
from .schema import SchemaError, load_schema


def _load(path: str):
    schema = load_schema(path)
    return schema, build_ir(schema)


def cmd_gen(args: argparse.Namespace) -> int:
    _, ir = _load(args.schema)
    layout = build_layout(ir, args.data_w)
    files = render(layout, out_dir=args.out)
    if args.dry_run:
        for f in files:
            print(f"--- {f.path} ({f.lines} lines) ---")
            print(f.text)
        return 0
    written = write(files, root=args.root)
    for p in written:
        print(f"wrote {p}")
    if not args.quiet:
        print(
            f"{layout.name}: DATA_W={layout.data_w}, header window {layout.hdr_bytes} B "
            f"({layout.acc_beats} beats), {layout.fields_total} fields, "
            f"{layout.record_bits}-bit record"
        )
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    schema, ir = _load(args.schema)
    print(f"{schema.name}: kind={schema.kind}, endian={schema.endian.value}, ok")
    for w in SUPPORTED_DATA_W:
        try:
            build_layout(ir, w)
        except LayoutError as exc:
            print(f"  DATA_W={w}: {exc}")
            return 1
        print(f"  DATA_W={w}: layout ok")
    return 0


def cmd_info(args: argparse.Namespace) -> int:
    schema, ir = _load(args.schema)
    print(f"{schema.name}  ({schema.kind}, {schema.endian.value}-endian)")
    print(f"  worst-case header: {ir.max_header_bytes} bytes")
    for rec in ir.records:
        span = f"{rec.fixed_bytes} B"
        if rec.is_variable:
            span += f" fixed, {rec.min_bytes}..{rec.max_bytes} B declared"
        code = f"  code 0x{rec.code:02x}" if rec.code is not None else ""
        print(f"  {rec.role:<8} {rec.name:<12} {span}{code}")
        for f in rec.fields:
            segs = " ".join(f"[{s.msb}:{s.lsb}]" for s in f.segments)
            print(f"      {f.name:<18} bit {f.bit_offset:>4} w {f.width:>3}  {segs}")
    return 0


def cmd_decode(args: argparse.Namespace) -> int:
    _, ir = _load(args.schema)
    data = bytes.fromhex(args.hex.replace(" ", ""))
    dec = decode(ir, data)
    print(f"{len(data)} bytes, {'ok' if dec.ok else 'MALFORMED'}")
    for rec in dec.records:
        print(f"  {rec.name} @{rec.offset} ({rec.length} B)")
        for k, v in rec.values.items():
            shown = v.hex() if isinstance(v, bytes) else v
            print(f"      {k:<18} {shown}")
    for err in dec.errors:
        print(f"  ! {err}")
    return 0 if dec.ok else 1


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="wirespec", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("gen", help="generate RTL from a schema")
    g.add_argument("--schema", required=True)
    g.add_argument("--data-w", type=int, default=64, choices=SUPPORTED_DATA_W)
    g.add_argument("--out", default="rtl/generated", help="output directory")
    g.add_argument("--root", default=".", help="directory --out is relative to")
    g.add_argument("--dry-run", action="store_true", help="print instead of writing")
    g.add_argument("--quiet", action="store_true")
    g.set_defaults(func=cmd_gen)

    c = sub.add_parser("check", help="validate a schema and its layouts")
    c.add_argument("--schema", required=True)
    c.set_defaults(func=cmd_check)

    i = sub.add_parser("info", help="print the resolved field layout")
    i.add_argument("--schema", required=True)
    i.set_defaults(func=cmd_info)

    d = sub.add_parser("decode", help="decode a hex packet with the golden model")
    d.add_argument("--schema", required=True)
    d.add_argument("--hex", required=True)
    d.set_defaults(func=cmd_decode)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (SchemaError, LayoutError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
