"""Intermediate representation: every field resolved to an absolute bit offset.

A *record* is a contiguous run of wire bytes with a fixed field layout -- one
protocol layer, the framed packet header, or one complete message including its
length/type prefix.  Within a record, bit 0 is the most significant bit of byte
0, i.e. records are addressed as a big-endian (network order) bit string.

Two views of a field are produced:

``bit_offset`` / ``width``
    The wire-order view.  :mod:`wirespec.golden` reads a field straight from
    these using integer arithmetic on the packet bytes.

``segments``
    The RTL view.  A byte at wire index *k* arrives on AXI-Stream lane *k*,
    occupying ``tdata[8k+7 : 8k]``.  A field is therefore a concatenation of one
    or more descending bit runs of that vector, listed most significant first.
    :mod:`wirespec.emit` turns each segment into a static slice.

The two are derived independently on purpose; see
``docs/decisions/0002-independent-golden-model.md``.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field

from .schema import (
    Endian,
    Field,
    HeaderLenExpr,
    Kind,
    Layer,
    LengthCovers,
    Schema,
)


@dataclass(frozen=True)
class Segment:
    """A descending, contiguous bit run of a record's flattened byte vector."""

    msb: int
    lsb: int

    @property
    def width(self) -> int:
        return self.msb - self.lsb + 1

    def shifted(self, base_bytes: int) -> Segment:
        off = base_bytes * 8
        return Segment(self.msb + off, self.lsb + off)


@dataclass(frozen=True)
class IRField:
    """A schema field pinned to a position inside its record."""

    name: str
    record: str
    bit_offset: int
    width: int
    kind: Kind
    endian: Endian
    enum: str | None
    doc: str
    segments: tuple[Segment, ...]

    @property
    def signed(self) -> bool:
        return self.kind.is_signed

    @property
    def is_bytes_like(self) -> bool:
        return self.kind in (Kind.CHAR, Kind.BYTES)

    @property
    def byte_offset(self) -> int:
        return self.bit_offset // 8

    @property
    def byte_aligned(self) -> bool:
        return self.bit_offset % 8 == 0 and self.width % 8 == 0

    @property
    def width_bytes(self) -> int:
        return self.width // 8

    @property
    def end_byte(self) -> int:
        """One past the last byte the field touches."""
        return (self.bit_offset + self.width + 7) // 8

    @property
    def sv_decl(self) -> str:
        """SystemVerilog packed-vector declaration for this field's value."""
        sign = " signed" if self.signed else ""
        return f"logic{sign} [{self.width - 1}:0]"


@dataclass(frozen=True)
class IRRecord:
    """A fixed-layout run of bytes: a layer, a packet header, or a message."""

    name: str
    role: str  # "layer" | "header" | "message"
    fields: tuple[IRField, ...]
    fixed_bytes: int
    doc: str = ""
    # layered only
    header_len: HeaderLenExpr | None = None
    next_selector: str | None = None
    next_map: dict[int, str] = dc_field(default_factory=dict)
    # message only
    code: int | None = None
    payload_offset: int = 0  # byte offset of the first payload field

    def __post_init__(self) -> None:
        object.__setattr__(self, "_by_name", {f.name: f for f in self.fields})

    def field(self, name: str) -> IRField:
        return self._by_name[name]  # type: ignore[attr-defined]

    def has(self, name: str) -> bool:
        return name in self._by_name  # type: ignore[attr-defined]

    @property
    def min_bytes(self) -> int:
        if self.header_len is None:
            return self.fixed_bytes
        return self.header_len.min_bytes

    @property
    def max_bytes(self) -> int:
        if self.header_len is None:
            return self.fixed_bytes
        return self.header_len.max_bytes

    @property
    def is_variable(self) -> bool:
        return self.header_len is not None


@dataclass(frozen=True)
class IR:
    """The whole schema, resolved."""

    name: str
    kind: str
    schema: Schema
    records: tuple[IRRecord, ...]
    start: str | None = None
    header_record: str | None = None
    message_records: tuple[str, ...] = ()
    # framed only, hoisted for convenience
    length_field: IRField | None = None
    type_field: IRField | None = None
    length_covers: LengthCovers | None = None
    count_field: str | None = None
    sequence_field: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "_by_name", {r.name: r for r in self.records})

    def record(self, name: str) -> IRRecord:
        return self._by_name[name]  # type: ignore[attr-defined]

    @property
    def is_layered(self) -> bool:
        return self.kind == "layered"

    @property
    def is_framed(self) -> bool:
        return self.kind == "framed"

    @property
    def layers(self) -> tuple[IRRecord, ...]:
        return tuple(r for r in self.records if r.role == "layer")

    @property
    def header(self) -> IRRecord:
        assert self.header_record is not None
        return self.record(self.header_record)

    @property
    def messages(self) -> tuple[IRRecord, ...]:
        return tuple(self.record(n) for n in self.message_records)

    @property
    def max_message_bytes(self) -> int:
        return max((r.fixed_bytes for r in self.messages), default=0)

    @property
    def min_message_bytes(self) -> int:
        return min((r.fixed_bytes for r in self.messages), default=0)

    @property
    def prefix_bytes(self) -> int:
        """Bytes of length+type that must be seen before a message is sized."""
        assert self.length_field is not None and self.type_field is not None
        return self.length_field.width_bytes + self.type_field.width_bytes

    @property
    def max_header_bytes(self) -> int:
        """Worst-case bytes consumed before the payload begins."""
        if self.is_framed:
            return self.header.fixed_bytes
        return _longest_chain_bytes(self)

    def expected_length_value(self, msg: IRRecord) -> int:
        """The value the length prefix must carry for a well-formed message."""
        assert self.length_covers is not None and self.length_field is not None
        total = msg.fixed_bytes
        if self.length_covers is LengthCovers.WHOLE_MESSAGE:
            return total
        if self.length_covers is LengthCovers.AFTER_LENGTH:
            return total - self.length_field.width_bytes
        return total - self.prefix_bytes

    def total_from_length(self, value: int) -> int:
        """Invert :meth:`expected_length_value`: wire length value -> total bytes."""
        assert self.length_covers is not None and self.length_field is not None
        if self.length_covers is LengthCovers.WHOLE_MESSAGE:
            return value
        if self.length_covers is LengthCovers.AFTER_LENGTH:
            return value + self.length_field.width_bytes
        return value + self.prefix_bytes

    def message_by_code(self, code: int) -> IRRecord | None:
        for r in self.messages:
            if r.code == code:
                return r
        return None


# --------------------------------------------------------------------------
# Bit-position resolution
# --------------------------------------------------------------------------


def bit_sources(bit_offset: int, width: int, endian: Endian) -> list[int]:
    """RTL bit indices feeding a field value, most significant bit of the value first.

    The record's byte *k* lives at RTL bits ``8k+7 .. 8k``, so wire-order bit
    *i* (MSB-first) is RTL bit ``8*(i//8) + 7 - (i%8)``.
    """
    if endian is Endian.LITTLE:
        # Whole bytes only (enforced in schema.py): the most significant byte of
        # the value is the *last* byte on the wire.
        first = bit_offset // 8
        nbytes = width // 8
        return [
            8 * b + k
            for b in range(first + nbytes - 1, first - 1, -1)
            for k in range(7, -1, -1)
        ]
    return [8 * ((bit_offset + j) // 8) + 7 - ((bit_offset + j) % 8) for j in range(width)]


def to_segments(positions: list[int]) -> tuple[Segment, ...]:
    """Collapse a MSB-first list of RTL bit indices into descending runs."""
    if not positions:
        return ()
    segs: list[Segment] = []
    run_msb = positions[0]
    prev = positions[0]
    for pos in positions[1:]:
        if pos == prev - 1:
            prev = pos
            continue
        segs.append(Segment(run_msb, prev))
        run_msb = pos
        prev = pos
    segs.append(Segment(run_msb, prev))
    return tuple(segs)


def _resolve_fields(
    fields: tuple[Field, ...], record: str, base_bits: int = 0
) -> tuple[tuple[IRField, ...], int]:
    out: list[IRField] = []
    offset = base_bits
    for f in fields:
        segs = to_segments(bit_sources(offset, f.width_bits, f.endian))
        out.append(
            IRField(
                name=f.name,
                record=record,
                bit_offset=offset,
                width=f.width_bits,
                kind=f.kind,
                endian=f.endian,
                enum=f.enum,
                doc=f.doc,
                segments=segs,
            )
        )
        offset += f.width_bits
    return tuple(out), offset


def _layer_record(layer: Layer, role: str) -> IRRecord:
    fields, total_bits = _resolve_fields(layer.fields, layer.name)
    return IRRecord(
        name=layer.name,
        role=role,
        fields=fields,
        fixed_bytes=total_bits // 8,
        doc=layer.doc,
        header_len=layer.header_len,
        next_selector=layer.next_selector,
        next_map=dict(layer.next_map),
    )


def build_ir(schema: Schema) -> IR:
    """Resolve a validated :class:`~wirespec.schema.Schema` into an :class:`IR`."""
    if schema.is_layered:
        records = tuple(_layer_record(lay, "layer") for lay in schema.layers)
        return IR(
            name=schema.name,
            kind=schema.kind,
            schema=schema,
            records=records,
            start=schema.start,
        )

    assert schema.header is not None and schema.messages is not None
    spec = schema.messages
    hdr = _layer_record(schema.header, "header")

    records: list[IRRecord] = [hdr]
    msg_names: list[str] = []
    len_ir: IRField | None = None
    type_ir: IRField | None = None

    for mt in spec.types:
        # Every message record starts with the shared length/type prefix so that
        # the rotated window can be sliced uniformly from the message's byte 0.
        prefix = (spec.length_field, spec.type_field)
        fields, total_bits = _resolve_fields(prefix + mt.fields, mt.name)
        payload_offset = (spec.length_field.width_bits + spec.type_field.width_bits) // 8
        records.append(
            IRRecord(
                name=mt.name,
                role="message",
                fields=fields,
                fixed_bytes=total_bits // 8,
                doc=mt.doc,
                code=mt.code,
                payload_offset=payload_offset,
            )
        )
        msg_names.append(mt.name)
        if len_ir is None:
            len_ir = fields[0]
            type_ir = fields[1]

    if len_ir is None:  # pragma: no cover - schema.py rejects an empty type list
        raise ValueError("framed schema resolved to zero message types")

    return IR(
        name=schema.name,
        kind=schema.kind,
        schema=schema,
        records=tuple(records),
        header_record=hdr.name,
        message_records=tuple(msg_names),
        length_field=len_ir,
        type_field=type_ir,
        length_covers=spec.covers,
        count_field=spec.count_field,
        sequence_field=spec.sequence_field,
    )


def _longest_chain_bytes(ir: IR) -> int:
    """Worst-case total header bytes over every root-to-leaf path of the chain."""
    assert ir.start is not None
    memo: dict[str, int] = {}

    def walk(name: str) -> int:
        if name in memo:
            return memo[name]
        rec = ir.record(name)
        best = max((walk(t) for t in rec.next_map.values()), default=0)
        memo[name] = rec.max_bytes + best
        return memo[name]

    return walk(ir.start)


def load_ir(path: str) -> IR:
    """Convenience: YAML path -> validated schema -> IR."""
    from .schema import load_schema

    return build_ir(load_schema(path))
