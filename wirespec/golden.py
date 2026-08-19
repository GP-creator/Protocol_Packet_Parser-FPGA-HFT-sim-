"""Reference decoder: interprets the IR at runtime.

This module is the model the RTL is checked against, so it deliberately shares
no code path with the emitter.  Where :mod:`wirespec.emit` builds a
concatenation of static bit slices from ``IRField.segments``, this file reads
the same field by converting the byte span it touches to a Python integer and
shifting.  Two implementations of one specification is a real cross-check; two
renderings of one template is not.

See ``docs/decisions/0002-independent-golden-model.md``.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field as dc_field

from .ir import IR, IRField, IRRecord
from .schema import Endian


class Defect(enum.Enum):
    """Why a packet failed to decode cleanly."""

    TRUNCATED_HEADER = "truncated_header"
    TRUNCATED_MESSAGE = "truncated_message"
    BAD_HEADER_LENGTH = "bad_header_length"
    BAD_LENGTH = "bad_length"
    ZERO_LENGTH = "zero_length"
    UNKNOWN_TYPE = "unknown_type"
    COUNT_MISMATCH = "count_mismatch"


class StopReason(enum.Enum):
    """Why the layered walk stopped."""

    END_OF_CHAIN = "end_of_chain"
    UNKNOWN_SELECTOR = "unknown_selector"
    DEFECT = "defect"


@dataclass(frozen=True)
class DecodeError:
    defect: Defect
    offset: int
    detail: str

    def __str__(self) -> str:
        return f"{self.defect.value}@{self.offset}: {self.detail}"


@dataclass
class DecodedRecord:
    """One decoded layer, packet header, or message."""

    name: str
    offset: int
    length: int
    values: dict[str, int | bytes]
    code: int | None = None

    def __getitem__(self, key: str) -> int | bytes:
        return self.values[key]


@dataclass
class Decoded:
    protocol: str
    kind: str
    raw_len: int
    records: list[DecodedRecord] = dc_field(default_factory=list)
    errors: list[DecodeError] = dc_field(default_factory=list)
    payload_offset: int = 0
    payload_len: int = 0
    stop_reason: StopReason = StopReason.END_OF_CHAIN

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def layers(self) -> list[DecodedRecord]:
        return self.records

    @property
    def messages(self) -> list[DecodedRecord]:
        return self.records[1:] if self.kind == "framed" else []

    @property
    def header(self) -> DecodedRecord | None:
        if self.kind == "framed" and self.records:
            return self.records[0]
        return None

    def by_name(self, name: str) -> DecodedRecord | None:
        for r in self.records:
            if r.name == name:
                return r
        return None

    @property
    def defects(self) -> list[Defect]:
        return [e.defect for e in self.errors]


# --------------------------------------------------------------------------
# Field reading
# --------------------------------------------------------------------------


def read_field(data: bytes, base: int, f: IRField) -> int | bytes:
    """Read one field out of ``data`` starting at record byte ``base``.

    Wire-order bit arithmetic: take the byte span the field touches, treat it as
    one big-endian integer, then shift the leading bits off the top and mask.
    """
    first = base + f.bit_offset // 8
    last = base + (f.bit_offset + f.width - 1) // 8
    span = data[first : last + 1]
    if len(span) != last - first + 1:
        raise IndexError(f"field '{f.name}' runs past the end of the buffer")

    if f.is_bytes_like:
        return bytes(span)

    if f.endian is Endian.LITTLE:
        raw = int.from_bytes(span, "little")
    else:
        span_bits = len(span) * 8
        lead = f.bit_offset % 8
        raw = (int.from_bytes(span, "big") >> (span_bits - lead - f.width)) & ((1 << f.width) - 1)

    if f.signed and raw >= (1 << (f.width - 1)):
        raw -= 1 << f.width
    return raw


def read_record(data: bytes, base: int, rec: IRRecord) -> dict[str, int | bytes]:
    return {f.name: read_field(data, base, f) for f in rec.fields}


# --------------------------------------------------------------------------
# Decoders
# --------------------------------------------------------------------------


def decode(ir: IR, data: bytes) -> Decoded:
    """Decode one packet against ``ir``."""
    if ir.is_layered:
        return _decode_layered(ir, data)
    return _decode_framed(ir, data)


def _decode_layered(ir: IR, data: bytes) -> Decoded:
    out = Decoded(protocol=ir.name, kind=ir.kind, raw_len=len(data))
    offset = 0
    name: str | None = ir.start

    while name is not None:
        rec = ir.record(name)
        avail = len(data) - offset
        if avail < rec.fixed_bytes:
            out.errors.append(
                DecodeError(
                    Defect.TRUNCATED_HEADER,
                    offset,
                    f"layer '{name}' needs {rec.fixed_bytes} bytes, {avail} remain",
                )
            )
            out.stop_reason = StopReason.DEFECT
            break

        values = read_record(data, offset, rec)

        hdr_bytes = rec.fixed_bytes
        if rec.header_len is not None:
            raw = values[rec.header_len.field]
            assert isinstance(raw, int)
            hdr_bytes = raw * rec.header_len.scale
            lo, hi = rec.header_len.min_bytes, rec.header_len.max_bytes
            if not lo <= hdr_bytes <= hi:
                out.errors.append(
                    DecodeError(
                        Defect.BAD_HEADER_LENGTH,
                        offset,
                        f"layer '{name}' header length {hdr_bytes} outside {lo}..{hi}",
                    )
                )
                out.stop_reason = StopReason.DEFECT
                break
            if avail < hdr_bytes:
                out.errors.append(
                    DecodeError(
                        Defect.TRUNCATED_HEADER,
                        offset,
                        f"layer '{name}' declares {hdr_bytes} bytes, {avail} remain",
                    )
                )
                out.stop_reason = StopReason.DEFECT
                break

        out.records.append(DecodedRecord(name, offset, hdr_bytes, values))
        offset += hdr_bytes

        if rec.next_selector is None:
            out.stop_reason = StopReason.END_OF_CHAIN
            break
        sel = values[rec.next_selector]
        assert isinstance(sel, int)
        nxt = rec.next_map.get(sel)
        if nxt is None:
            out.stop_reason = StopReason.UNKNOWN_SELECTOR
            break
        name = nxt

    out.payload_offset = offset
    out.payload_len = max(0, len(data) - offset)
    return out


def _decode_framed(ir: IR, data: bytes) -> Decoded:
    out = Decoded(protocol=ir.name, kind=ir.kind, raw_len=len(data))
    hdr = ir.header
    assert ir.length_field is not None and ir.type_field is not None

    if len(data) < hdr.fixed_bytes:
        out.errors.append(
            DecodeError(
                Defect.TRUNCATED_HEADER,
                0,
                f"packet header needs {hdr.fixed_bytes} bytes, {len(data)} present",
            )
        )
        out.stop_reason = StopReason.DEFECT
        return out

    hdr_values = read_record(data, 0, hdr)
    out.records.append(DecodedRecord(hdr.name, 0, hdr.fixed_bytes, hdr_values))
    out.payload_offset = hdr.fixed_bytes

    len_bytes = ir.length_field.width_bytes
    prefix = ir.prefix_bytes
    offset = hdr.fixed_bytes

    while offset < len(data):
        avail = len(data) - offset
        if avail < len_bytes:
            out.errors.append(
                DecodeError(
                    Defect.TRUNCATED_MESSAGE,
                    offset,
                    f"{avail} trailing byte(s), too few for a {len_bytes}-byte length prefix",
                )
            )
            out.stop_reason = StopReason.DEFECT
            break

        len_val = read_field(data, offset, ir.length_field)
        assert isinstance(len_val, int)
        total = ir.total_from_length(len_val)

        if len_val == 0:
            out.errors.append(
                DecodeError(Defect.ZERO_LENGTH, offset, "length prefix is zero, cannot advance")
            )
            out.stop_reason = StopReason.DEFECT
            break
        if total < prefix:
            out.errors.append(
                DecodeError(
                    Defect.BAD_LENGTH,
                    offset,
                    f"length {len_val} implies {total} total bytes, below the "
                    f"{prefix}-byte length+type prefix",
                )
            )
            out.stop_reason = StopReason.DEFECT
            break
        if avail < total:
            out.errors.append(
                DecodeError(
                    Defect.TRUNCATED_MESSAGE,
                    offset,
                    f"message declares {total} bytes, only {avail} remain",
                )
            )
            out.stop_reason = StopReason.DEFECT
            break

        code = read_field(data, offset, ir.type_field)
        assert isinstance(code, int)
        mrec = ir.message_by_code(code)
        if mrec is None:
            out.errors.append(
                DecodeError(Defect.UNKNOWN_TYPE, offset, f"no message type has code 0x{code:02x}")
            )
            out.stop_reason = StopReason.DEFECT
            break
        if total != mrec.fixed_bytes:
            out.errors.append(
                DecodeError(
                    Defect.BAD_LENGTH,
                    offset,
                    f"message '{mrec.name}' is {mrec.fixed_bytes} bytes, "
                    f"length prefix declares {total}",
                )
            )
            out.stop_reason = StopReason.DEFECT
            break

        out.records.append(
            DecodedRecord(mrec.name, offset, total, read_record(data, offset, mrec), code)
        )
        offset += total

    out.payload_len = max(0, len(data) - hdr.fixed_bytes)

    if ir.count_field is not None and out.stop_reason is not StopReason.DEFECT:
        declared = hdr_values[ir.count_field]
        assert isinstance(declared, int)
        actual = len(out.records) - 1
        if declared != actual:
            out.errors.append(
                DecodeError(
                    Defect.COUNT_MISMATCH,
                    0,
                    f"header declares {declared} message(s), {actual} decoded",
                )
            )

    return out


# --------------------------------------------------------------------------
# Stream-level model (the software twin of rtl/common/stats.sv)
# --------------------------------------------------------------------------


@dataclass
class StreamStats:
    packets: int = 0
    messages: int = 0
    seq_gaps: int = 0
    malformed: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "packets": self.packets,
            "messages": self.messages,
            "seq_gaps": self.seq_gaps,
            "malformed": self.malformed,
        }


class StreamModel:
    """Decode a sequence of packets and accumulate the counters stats.sv keeps.

    A sequence gap is any packet whose sequence number is not exactly one more
    than the previous packet's; the first packet of a stream never counts as a
    gap.  Malformed packets are counted but do not advance the expectation.
    """

    def __init__(self, ir: IR) -> None:
        self.ir = ir
        self.stats = StreamStats()
        self._expect_seq: int | None = None

    def feed(self, data: bytes) -> Decoded:
        dec = decode(self.ir, data)
        self.stats.packets += 1
        self.stats.messages += len(dec.messages)
        if not dec.ok:
            self.stats.malformed += 1
            return dec
        if self.ir.sequence_field is not None and dec.header is not None:
            seq = dec.header.values.get(self.ir.sequence_field)
            if isinstance(seq, int):
                if self._expect_seq is not None and seq != self._expect_seq:
                    self.stats.seq_gaps += 1
                self._expect_seq = seq + 1
        return dec
