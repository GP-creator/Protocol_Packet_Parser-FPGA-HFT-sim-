"""Turn an IR plus a datapath width into everything the templates need.

The IR says where fields live in a *packet*. This module says where they live in
*hardware*: which window each layer is sliced from, whether that window sits at a
compile-time offset or has to be shifted at run time, how wide the header
accumulator must be, and how wide the record bus comes out.

Nothing here emits text. Keeping the arithmetic out of the templates is what
makes it testable — `tests/test_layout.py` checks the numbers directly, and the
snapshot tests only have to check that the text agrees with them.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field

from .ir import IR, IRField, IRRecord, Segment
from .schema import Endian, Kind

#: Datapath widths the generator is expected to produce working RTL for.
SUPPORTED_DATA_W = (64, 128, 256, 512)


class LayoutError(ValueError):
    """The schema is valid but the generator cannot render it."""


@dataclass(frozen=True)
class FieldLayout:
    """One field, resolved to slices of its layer's window."""

    ir: IRField
    port: str  # o_<layer>_<field>, de-duplicated
    segments: tuple[Segment, ...]

    @property
    def name(self) -> str:
        return self.ir.name

    @property
    def sig(self) -> str:
        """Internal signal name: the port without its ``o_``.

        Layer-qualified, because two layers may legitimately carry a field of
        the same name -- `ipv4.checksum` and `udp.checksum` would otherwise be
        one signal and a compile error.
        """
        return self.port[2:]

    @property
    def width(self) -> int:
        return self.ir.width

    @property
    def signed(self) -> bool:
        return self.ir.signed

    @property
    def decl(self) -> str:
        return f"logic{' signed' if self.signed else ''} [{self.width - 1}:0]"

    @property
    def swapped(self) -> bool:
        """True when the value is not one contiguous run of the window."""
        return len(self.segments) > 1

    @property
    def note(self) -> str:
        bits = f"bits {self.ir.bit_offset}..{self.ir.bit_offset + self.width - 1}"
        kind = self.ir.kind.value
        if self.ir.kind is Kind.ENUM:
            kind = f"enum {self.ir.enum}"
        endian = "" if self.ir.endian is Endian.NONE else f", {self.ir.endian.value}-endian"
        return f"{bits}, {self.width}b {kind}{endian}"


@dataclass(frozen=True)
class LayerLayout:
    """One protocol layer, placed in the header window."""

    rec: IRRecord
    index: int
    parent: str | None
    parent_selector_value: int | None
    static_offset: int | None  # byte offset, or None if it must be computed
    max_offset: int
    children: tuple[tuple[int, str], ...] = ()
    fields: tuple[FieldLayout, ...] = ()

    @property
    def name(self) -> str:
        return self.rec.name

    @property
    def bytes_(self) -> int:
        return self.rec.fixed_bytes

    @property
    def bits(self) -> int:
        return self.rec.fixed_bytes * 8

    @property
    def is_variable(self) -> bool:
        return self.rec.is_variable

    @property
    def is_terminal(self) -> bool:
        return not self.children

    @property
    def dynamic(self) -> bool:
        return self.static_offset is None

    @property
    def len_expr(self):
        return self.rec.header_len

    def sig(self, field_name: str) -> str:
        """The internal signal carrying one of this layer's fields."""
        return _port_name(self.name, field_name)[2:]

    @property
    def selector_sig(self) -> str:
        assert self.rec.next_selector is not None
        return self.sig(self.rec.next_selector)

    @property
    def selector_width(self) -> int:
        assert self.rec.next_selector is not None
        return self.rec.field(self.rec.next_selector).width


@dataclass(frozen=True)
class EnumLayout:
    name: str
    width: int
    values: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class Layout:
    ir: IR
    data_w: int
    len_w: int
    hdr_bytes: int
    layers: tuple[LayerLayout, ...]
    enums: tuple[EnumLayout, ...]
    record_bits: int
    fields_total: int

    # ---------------------------------------------------------------- sizes --
    @property
    def keep_w(self) -> int:
        return self.data_w // 8

    @property
    def bcnt_w(self) -> int:
        return (self.keep_w).bit_length()  # bits to hold 0..keep_w

    @property
    def rot_w(self) -> int:
        return max(1, (self.keep_w - 1).bit_length())

    @property
    def acc_beats(self) -> int:
        return (self.hdr_bytes + self.keep_w - 1) // self.keep_w

    @property
    def acc_bytes(self) -> int:
        return self.acc_beats * self.keep_w

    @property
    def acc_bits(self) -> int:
        return self.acc_bytes * 8

    @property
    def win_bits(self) -> int:
        return self.hdr_bytes * 8

    @property
    def sh_w(self) -> int:
        """Bits for a bit index into the header window.

        Verilator requires a variable part-select index be exactly wide enough
        for the vector it indexes -- wider is a WIDTHTRUNC, and the surplus bits
        then read as unused. So this is the width, not LEN_W.
        """
        return max(1, (self.win_bits - 1).bit_length())

    @property
    def name(self) -> str:
        return self.ir.name

    @property
    def pkg(self) -> str:
        return f"{self.ir.name}_pkg"

    @property
    def upper(self) -> str:
        return self.ir.name.upper()

    def layer(self, name: str) -> LayerLayout:
        for lay in self.layers:
            if lay.name == name:
                return lay
        raise KeyError(name)

    @property
    def start(self) -> LayerLayout:
        return self.layers[0]

    @property
    def all_fields(self) -> list[FieldLayout]:
        return [f for lay in self.layers for f in lay.fields]


# --------------------------------------------------------------------------
# Construction
# --------------------------------------------------------------------------


def _port_name(layer: str, field: str) -> str:
    """``o_<layer>_<field>``, without stuttering when the field repeats the layer."""
    if field == layer or field.startswith(f"{layer}_"):
        return f"o_{field}"
    return f"o_{layer}_{field}"


def _topo_order(ir: IR) -> list[IRRecord]:
    """Layers in chain order, breadth first from the start layer."""
    assert ir.start is not None
    order: list[IRRecord] = []
    seen: set[str] = set()
    queue = [ir.start]
    while queue:
        name = queue.pop(0)
        if name in seen:
            continue
        seen.add(name)
        rec = ir.record(name)
        order.append(rec)
        queue.extend(rec.next_map.values())
    unreachable = {lay.name for lay in ir.layers} - seen
    if unreachable:
        raise LayoutError(
            f"layer(s) {sorted(unreachable)} are not reachable from start layer "
            f"'{ir.start}'; nothing would ever extract them"
        )
    return order


def build_layout(ir: IR, data_w: int, *, len_w: int = 16) -> Layout:
    """Resolve ``ir`` onto a ``data_w``-bit datapath."""
    if data_w not in SUPPORTED_DATA_W:
        raise LayoutError(
            f"DATA_W={data_w} is not supported; expected one of {list(SUPPORTED_DATA_W)}"
        )
    if not ir.is_layered:
        raise LayoutError(
            f"schema '{ir.name}' is kind '{ir.kind}'; only layered schemas are "
            f"generated so far (framed lands at M4)"
        )

    order = _topo_order(ir)

    # Every layer must have at most one parent, or its offset is ambiguous: two
    # parents of different lengths would place the same layer in two places.
    parents: dict[str, list[tuple[str, int]]] = {rec.name: [] for rec in order}
    for rec in order:
        for value, target in rec.next_map.items():
            parents[target].append((rec.name, value))
    for name, plist in parents.items():
        if len(plist) > 1:
            raise LayoutError(
                f"layer '{name}' is reachable from {sorted(p for p, _ in plist)}; the "
                f"generator needs a tree, so that a layer has one byte offset"
            )

    # Offsets. static_offset is None once any ancestor has a variable length.
    static: dict[str, int | None] = {}
    max_off: dict[str, int] = {}
    layouts: list[LayerLayout] = []

    for index, rec in enumerate(order):
        plist = parents[rec.name]
        if not plist:
            parent, sel_val = None, None
            static[rec.name] = 0
            max_off[rec.name] = 0
        else:
            parent, sel_val = plist[0]
            prec = ir.record(parent)
            pstatic = static[parent]
            static[rec.name] = None if (pstatic is None or prec.is_variable) else pstatic + prec.fixed_bytes
            max_off[rec.name] = max_off[parent] + prec.max_bytes

        end = max_off[rec.name] + rec.max_bytes
        if end > ir.max_header_bytes:
            raise LayoutError(
                f"layer '{rec.name}' can end at byte {end}, past the {ir.max_header_bytes}-byte "
                f"header window"
            )

        seen_ports: dict[str, str] = {}
        fields: list[FieldLayout] = []
        for f in rec.fields:
            port = _port_name(rec.name, f.name)
            if port in seen_ports:
                raise LayoutError(
                    f"fields '{seen_ports[port]}' and '{rec.name}.{f.name}' both map to "
                    f"port '{port}'"
                )
            seen_ports[port] = f"{rec.name}.{f.name}"
            fields.append(FieldLayout(f, port, f.segments))

        layouts.append(
            LayerLayout(
                rec=rec,
                index=index,
                parent=parent,
                parent_selector_value=sel_val,
                static_offset=static[rec.name],
                max_offset=max_off[rec.name],
                children=tuple(sorted(rec.next_map.items())),
                fields=tuple(fields),
            )
        )

    enums = tuple(
        EnumLayout(e.name, e.width_bits, tuple(sorted(e.values.items(), key=lambda kv: kv[1])))
        for e in ir.schema.enums.values()
    )

    record_bits = sum(f.width for lay in layouts for f in lay.fields)
    return Layout(
        ir=ir,
        data_w=data_w,
        len_w=len_w,
        hdr_bytes=ir.max_header_bytes,
        layers=tuple(layouts),
        enums=enums,
        record_bits=record_bits,
        fields_total=sum(len(lay.fields) for lay in layouts),
    )
