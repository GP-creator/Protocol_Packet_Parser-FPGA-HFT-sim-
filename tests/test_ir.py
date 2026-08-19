"""Offset resolution and the RTL segment view."""

from __future__ import annotations

import random

import pytest

from wirespec.golden import read_field
from wirespec.ir import Segment, bit_sources, to_segments
from wirespec.schema import Endian

from .conftest import SCHEMA_DIR  # noqa: F401  (kept for symmetry with other modules)


def offsets(rec) -> dict[str, tuple[int, int]]:
    return {f.name: (f.bit_offset, f.width) for f in rec.fields}


# --------------------------------------------------------------------------
# Offsets
# --------------------------------------------------------------------------


def test_eth_layer_offsets(eth_ir):
    assert offsets(eth_ir.record("eth")) == {
        "dst_mac": (0, 48),
        "src_mac": (48, 48),
        "ethertype": (96, 16),
    }
    assert eth_ir.record("eth").fixed_bytes == 14


def test_ipv4_offsets_including_sub_byte_fields(eth_ir):
    assert offsets(eth_ir.record("ipv4")) == {
        "version": (0, 4),
        "ihl": (4, 4),
        "dscp": (8, 6),
        "ecn": (14, 2),
        "total_length": (16, 16),
        "identification": (32, 16),
        "flags": (48, 3),
        "frag_offset": (51, 13),
        "ttl": (64, 8),
        "protocol": (72, 8),
        "hdr_checksum": (80, 16),
        "src_ip": (96, 32),
        "dst_ip": (128, 32),
    }
    ip = eth_ir.record("ipv4")
    assert ip.fixed_bytes == 20
    assert ip.is_variable
    assert (ip.min_bytes, ip.max_bytes) == (20, 60)


def test_message_records_include_the_length_type_prefix(feed_ir):
    trade = feed_ir.record("trade")
    assert offsets(trade) == {
        "msg_len": (0, 16),
        "msg_type": (16, 8),
        "symbol": (24, 64),
        "price": (88, 64),
        "qty": (152, 32),
        "side": (184, 8),
        "trade_flags": (192, 8),
    }
    assert trade.fixed_bytes == 25
    assert trade.payload_offset == 3
    assert trade.code == 1


@pytest.mark.parametrize(
    "name,size", [("trade", 25), ("quote", 23), ("status", 14), ("imbalance", 32)]
)
def test_message_sizes(feed_ir, name, size):
    assert feed_ir.record(name).fixed_bytes == size


def test_ir_summary_numbers(feed_ir, eth_ir):
    assert feed_ir.max_message_bytes == 32
    assert feed_ir.min_message_bytes == 14
    assert feed_ir.prefix_bytes == 3
    assert feed_ir.max_header_bytes == 16
    # eth(14) + ipv4 worst case(60) + udp(8)
    assert eth_ir.max_header_bytes == 82


def test_length_arithmetic_round_trips(feed_ir):
    for msg in feed_ir.messages:
        value = feed_ir.expected_length_value(msg)
        assert value == msg.fixed_bytes - 2  # length_covers: after_length
        assert feed_ir.total_from_length(value) == msg.fixed_bytes


def test_message_lookup_by_code(feed_ir):
    assert feed_ir.message_by_code(1).name == "trade"
    assert feed_ir.message_by_code(4).name == "imbalance"
    assert feed_ir.message_by_code(0x7F) is None


# --------------------------------------------------------------------------
# Segments
# --------------------------------------------------------------------------


def test_to_segments_collapses_runs():
    assert to_segments([7, 6, 5, 4]) == (Segment(7, 4),)
    assert to_segments([23, 22, 21, 20, 19, 18, 17, 16, 31, 30, 29, 28, 27, 26, 25, 24]) == (
        Segment(23, 16),
        Segment(31, 24),
    )
    assert to_segments([]) == ()
    assert to_segments([5]) == (Segment(5, 5),)


def test_big_endian_16bit_field_swaps_bytes():
    # A 16-bit field at wire byte 2: wire byte 2 is the value's high half and
    # lives at RTL bits 23:16, so the concatenation is {23:16, 31:24}.
    assert to_segments(bit_sources(16, 16, Endian.BIG)) == (Segment(23, 16), Segment(31, 24))


def test_little_endian_16bit_field_does_not_swap():
    assert to_segments(bit_sources(16, 16, Endian.LITTLE)) == (Segment(31, 16),)


def test_sub_byte_fields_are_single_segments(eth_ir):
    ip = eth_ir.record("ipv4")
    assert ip.field("version").segments == (Segment(7, 4),)
    assert ip.field("ihl").segments == (Segment(3, 0),)
    assert ip.field("dscp").segments == (Segment(15, 10),)
    assert ip.field("ecn").segments == (Segment(9, 8),)


def test_field_straddling_a_byte_boundary(eth_ir):
    ip = eth_ir.record("ipv4")
    assert ip.field("flags").segments == (Segment(55, 53),)
    # 13 bits: five bits from wire byte 6, then all of wire byte 7.
    assert ip.field("frag_offset").segments == (Segment(52, 48), Segment(63, 56))


def test_bytes_fields_keep_wire_order(feed_ir):
    sym = feed_ir.record("trade").field("symbol")
    # char[8] at byte 3: eight one-byte segments, first wire byte most significant.
    assert sym.segments == tuple(
        Segment(8 * b + 7, 8 * b) for b in range(3, 11)
    )


@pytest.mark.parametrize("ir_name", ["eth_ir", "feed_ir"])
def test_segments_are_well_formed(request, ir_name):
    ir = request.getfixturevalue(ir_name)
    for rec in ir.records:
        limit = rec.fixed_bytes * 8
        for f in rec.fields:
            assert f.segments, f"{rec.name}.{f.name} has no segments"
            assert sum(s.width for s in f.segments) == f.width
            seen: set[int] = set()
            for s in f.segments:
                assert s.msb >= s.lsb
                assert 0 <= s.lsb and s.msb < limit
                bits = set(range(s.lsb, s.msb + 1))
                assert not (bits & seen), f"{rec.name}.{f.name} segments overlap"
                seen |= bits


# --------------------------------------------------------------------------
# The two views must agree
# --------------------------------------------------------------------------


def _read_via_segments(record_bytes: bytes, f) -> int:
    """Extract a field the way the RTL will: concatenate static bit slices.

    Byte k of the record occupies bits 8k+7..8k of the flattened vector, which
    is exactly a little-endian integer read of the record.
    """
    vec = int.from_bytes(record_bytes, "little")
    acc = 0
    for seg in f.segments:
        acc = (acc << seg.width) | ((vec >> seg.lsb) & ((1 << seg.width) - 1))
    if f.signed and acc >= (1 << (f.width - 1)):
        acc -= 1 << f.width
    return acc


@pytest.mark.parametrize("ir_name", ["eth_ir", "feed_ir"])
def test_segment_view_matches_golden_field_read(request, ir_name):
    """The RTL derivation and the golden derivation must produce one value."""
    ir = request.getfixturevalue(ir_name)
    rng = random.Random(0xA5A5)
    for rec in ir.records:
        for _ in range(200):
            raw = bytes(rng.randrange(256) for _ in range(rec.fixed_bytes))
            for f in rec.fields:
                want = read_field(raw, 0, f)
                if isinstance(want, bytes):
                    want = int.from_bytes(want, "big")
                assert _read_via_segments(raw, f) == want, (
                    f"{rec.name}.{f.name} disagrees on {raw.hex()}"
                )
