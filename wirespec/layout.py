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
class MsgTypeLayout:
    """One message type, with the length/type prefix included in its record."""

    rec: IRRecord

    @property
    def name(self) -> str:
        return self.rec.name

    @property
    def code(self) -> int:
        assert self.rec.code is not None
        return self.rec.code

    @property
    def bytes_(self) -> int:
        return self.rec.fixed_bytes


@dataclass(frozen=True)
class MsgFieldLayout:
    """A field of the message record bus, merged across the types that carry it.

    A name that appears in several types at the same offset -- ``symbol`` in
    trade, quote and imbalance -- is one static slice. A name that appears at
    different offsets needs a mux, but over the handful of *types*, never over
    the lanes of a beat.
    """

    name: str
    width: int
    signed: bool
    rec_lsb: int
    placements: tuple[tuple[str, tuple[Segment, ...]], ...]

    @property
    def decl(self) -> str:
        return f"logic{' signed' if self.signed else ''} [{self.width - 1}:0]"

    @property
    def uniform(self) -> bool:
        """True when every type that carries this field puts it in one place."""
        return len({segs for _, segs in self.placements}) == 1

    @property
    def segments(self) -> tuple[Segment, ...]:
        assert self.uniform
        return self.placements[0][1]

    @property
    def types(self) -> tuple[str, ...]:
        return tuple(t for t, _ in self.placements)

    @property
    def sig(self) -> str:
        return f"f_{self.name}"


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
    # framed only
    messages: tuple[MsgTypeLayout, ...] = ()
    msg_fields: tuple[MsgFieldLayout, ...] = ()
    msg_rec_bits: int = 0

    # ------------------------------------------------------------- framed --
    @property
    def framed(self) -> bool:
        return self.ir.is_framed

    @property
    def msg_max(self) -> int:
        return self.ir.max_message_bytes

    @property
    def msg_min(self) -> int:
        return self.ir.min_message_bytes

    @property
    def buf_bytes(self) -> int:
        """MSG_MAX-1 leftover, plus one beat. See msg_stitch.sv."""
        return self.msg_max - 1 + self.keep_w

    @property
    def slots(self) -> int:
        """Messages that can complete in one cycle, worst case.

        ceil(KEEP_W / MSG_MIN) is tight, not conservative: the framer consumes
        greedily, so the leftover at the start of a cycle is always one message
        that would not fit, which bounds how many the next beat can finish. The
        derivation is in msg_framer.sv.
        """
        return max(1, -(-self.keep_w // self.msg_min))

    @property
    def off_w(self) -> int:
        return max(1, (self.buf_bytes).bit_length())

    @property
    def msg_bits(self) -> int:
        return self.msg_max * 8

    @property
    def len_field(self):
        assert self.ir.length_field is not None
        return self.ir.length_field

    @property
    def type_field(self):
        assert self.ir.type_field is not None
        return self.ir.type_field

    @property
    def len_off(self) -> int:
        return self.len_field.bit_offset // 8

    @property
    def len_bytes(self) -> int:
        return self.len_field.width_bytes

    @property
    def len_add(self) -> int:
        """total bytes = length value + this."""
        return self.ir.total_from_length(0)

    @property
    def prefix_bytes(self) -> int:
        return self.ir.prefix_bytes

    @property
    def tot_w(self) -> int:
        return self.len_bytes * 8 + 2

    def msg_field(self, name: str) -> MsgFieldLayout:
        for f in self.msg_fields:
            if f.name == name:
                return f
        raise KeyError(name)

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

    if ir.is_framed:
        return _build_framed(ir, data_w, len_w)

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

    return _finish(ir, data_w, len_w, layouts)


def _enums(ir: IR) -> tuple[EnumLayout, ...]:
    return tuple(
        EnumLayout(e.name, e.width_bits, tuple(sorted(e.values.items(), key=lambda kv: kv[1])))
        for e in ir.schema.enums.values()
    )


def _finish(ir: IR, data_w: int, len_w: int, layouts: list[LayerLayout], **kw) -> Layout:
    record_bits = sum(f.width for lay in layouts for f in lay.fields)
    return Layout(
        ir=ir,
        data_w=data_w,
        len_w=len_w,
        hdr_bytes=ir.max_header_bytes,
        layers=tuple(layouts),
        enums=_enums(ir),
        record_bits=record_bits,
        fields_total=sum(len(lay.fields) for lay in layouts),
        **kw,
    )


def _build_framed(ir: IR, data_w: int, len_w: int) -> Layout:
    """A framed schema: one fixed packet header, then length-prefixed messages.

    The packet header is laid out as a single terminal layer, which lets the
    same hdr_parse template serve both schema kinds -- a fixed header is just a
    chain of length one.
    """
    hdr = ir.header
    fields = tuple(FieldLayout(f, _port_name(hdr.name, f.name), f.segments) for f in hdr.fields)
    header_layer = LayerLayout(
        rec=hdr,
        index=0,
        parent=None,
        parent_selector_value=None,
        static_offset=0,
        max_offset=0,
        children=(),
        fields=fields,
    )

    spec = ir.schema.messages
    assert spec is not None
    if ir.length_field is None or ir.type_field is None:
        raise LayoutError(f"schema '{ir.name}' has no length/type prefix")
    if ir.length_field.endian is Endian.LITTLE:
        raise LayoutError(
            "a little-endian length prefix is not generated yet; msg_framer reads "
            "the prefix big-endian"
        )
    if ir.length_field.bit_offset != 0:
        raise LayoutError("the length prefix must be the first field of a message")

    messages = tuple(MsgTypeLayout(rec) for rec in ir.messages)
    if ir.min_message_bytes < ir.prefix_bytes + 1:
        raise LayoutError(
            f"the smallest message is {ir.min_message_bytes} bytes, which leaves no "
            f"payload after the {ir.prefix_bytes}-byte prefix"
        )

    # Merge fields by name across types. Same name at the same offset is one
    # slice; same name at different offsets becomes a mux over the type code.
    order: list[str] = []
    placements: dict[str, list[tuple[str, tuple[Segment, ...]]]] = {}
    shape: dict[str, tuple[int, bool]] = {}
    for rec in ir.messages:
        for f in rec.fields:
            if f.name not in placements:
                placements[f.name] = []
                order.append(f.name)
                shape[f.name] = (f.width, f.signed)
            elif shape[f.name] != (f.width, f.signed):
                w, s = shape[f.name]
                raise LayoutError(
                    f"field '{f.name}' is {f.width} bits{' signed' if f.signed else ''} in "
                    f"'{rec.name}' but {w} bits{' signed' if s else ''} elsewhere; one "
                    f"record slot cannot hold both"
                )
            placements[f.name].append((rec.name, f.segments))

    msg_fields: list[MsgFieldLayout] = []
    lsb = 0
    for name in order:
        width, signed = shape[name]
        msg_fields.append(
            MsgFieldLayout(name, width, signed, lsb, tuple(placements[name]))
        )
        lsb += width

    return _finish(
        ir,
        data_w,
        len_w,
        [header_layer],
        messages=messages,
        msg_fields=tuple(msg_fields),
        msg_rec_bits=lsb,
    )


