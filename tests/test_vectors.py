"""Hand-authored wire vectors: literal bytes, hand-computed expected values.

Why this file exists, stated plainly, because it is the only test here whose
value depends on how it was written.

Every other decoder test builds a packet with `tb.common.stimulus` and asks the
golden model to recover what the builder put in. That is already two
implementations -- the builder uses fixed `struct` format strings and never
looks at the IR -- so a wrong offset in `schemas/*.yaml` does not pass silently
there. What it *cannot* catch is the schema and the builder being wrong the same
way, because both were written from the same reading of the protocol.

So the vectors below are transcribed from the byte layout directly. The hex is
literal, the expected values are worked out by hand and written as constants
(`0xDEADBEEF`, not `struct.unpack`), and nothing in this file imports the
stimulus builder. If the schema, the IR and the builder all agreed on a layout
that is not the layout, this is the file that fails.

Two kinds of check:

* `test_vector_*` -- decode a literal packet, assert every field.
* `test_*_byte_map` -- flip each byte of a header in turn and assert exactly the
  fields that byte is supposed to feed changed. The map is written out from the
  protocol, so it pins offsets and widths without reusing the IR's own arithmetic
  to decide which bits to poke.
"""

from __future__ import annotations

import pytest

from wirespec.golden import Defect, decode

# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def hexb(*parts: str) -> bytes:
    """Join hex fragments, ignoring the whitespace that keeps them readable."""
    return bytes.fromhex("".join(parts).replace(" ", "").replace("\n", ""))


def flat(dec) -> dict[str, object]:
    """Every decoded field, keyed 'record.field', for the byte-map sweeps.

    Messages are keyed by position rather than by type name, because two
    messages of one type in a packet would otherwise collide -- and because a
    byte that changed *which* type decoded should read as a change, not as a
    key appearing and disappearing.
    """
    out: dict[str, object] = {}
    msgs = {id(m) for m in dec.messages}
    for r in dec.records:
        if id(r) in msgs:
            continue
        for k, v in r.values.items():
            out[f"{r.name}.{k}"] = v
    for i, m in enumerate(dec.messages):
        out[f"msg{i}.__type"] = m.name
        for k, v in m.values.items():
            out[f"msg{i}.{k}"] = v
    return out


def changed(a: dict, b: dict) -> set[str]:
    keys = set(a) | set(b)
    return {k for k in keys if a.get(k, ...) != b.get(k, ...)}


# --------------------------------------------------------------------------
# eth_ipv4_udp -- transcribed byte by byte
# --------------------------------------------------------------------------

# 01:23:45:67:89:ab -> cd:ef:00:11:22:33, ethertype 0x0800
ETH_HDR = "0123456789ab cdef00112233 0800"

# ver=4 ihl=5 | dscp=0b101101=45 ecn=0b10=2 | total_length=33 | id=0xBEEF
# flags=0b010=2 frag_offset=0x1AAA (0x5AAA = 010 1_1010_1010_1010)
# ttl=64 | proto=17 (UDP) | checksum=0x0000 | 8.8.8.8 -> 192.168.1.1
IPV4_HDR = "45 b6 0021 beef 5aaa 40 11 0000 08080808 c0a80101"

# src=8080 dst=53 len=13 checksum=0xABCD
UDP_HDR = "1f90 0035 000d abcd"

V_ETH_UDP = hexb(ETH_HDR, IPV4_HDR, UDP_HDR, "68656c6c6f")  # + "hello"


def test_vector_eth_ipv4_udp():
    assert len(V_ETH_UDP) == 47
    dec = decode(load_eth(), V_ETH_UDP)
    assert dec.ok, dec.errors

    eth = dec.by_name("eth").values
    assert eth == {
        "dst_mac": bytes.fromhex("0123456789ab"),
        "src_mac": bytes.fromhex("cdef00112233"),
        "ethertype": 0x0800,
    }

    ip = dec.by_name("ipv4").values
    assert ip == {
        "version": 4,
        "ihl": 5,
        "dscp": 45,
        "ecn": 2,
        "total_length": 33,
        "identification": 0xBEEF,
        "flags": 2,
        "frag_offset": 0x1AAA,
        "ttl": 64,
        "protocol": 17,
        "hdr_checksum": 0x0000,
        "src_ip": bytes.fromhex("08080808"),
        "dst_ip": bytes.fromhex("c0a80101"),
    }

    udp = dec.by_name("udp").values
    assert udp == {
        "src_port": 8080,
        "dst_port": 53,
        "length": 13,
        "checksum": 0xABCD,
    }

    assert dec.payload_offset == 42
    assert V_ETH_UDP[42:] == b"hello"


def test_vector_sub_byte_fields_are_msb_first():
    """0xB6 is dscp=101101, ecn=10 -- not ecn=10 then dscp, and not byte-swapped.

    Written out because this is the one place a plausible wrong answer exists:
    reading the low six bits as dscp gives 54 instead of 45, and both are
    'a number that came out of that byte'.
    """
    ip = decode(load_eth(), V_ETH_UDP).by_name("ipv4")
    assert ip["dscp"] == 0b101101 == 45
    assert ip["ecn"] == 0b10 == 2
    assert (ip["dscp"] << 2) | ip["ecn"] == 0xB6


def test_vector_field_straddling_a_byte_boundary():
    """frag_offset is 13 bits spanning bytes 20 and 21 of the frame."""
    ip = decode(load_eth(), V_ETH_UDP).by_name("ipv4")
    assert V_ETH_UDP[20:22] == b"\x5a\xaa"
    assert ip["flags"] == 0b010
    assert ip["frag_offset"] == 0b1_1010_1010_1010 == 0x1AAA
    assert (ip["flags"] << 13) | ip["frag_offset"] == 0x5AAA


V_ETH_ARP = hexb("0123456789ab cdef00112233 0806", "0102030405060708")


def test_vector_unknown_ethertype_stops_the_chain():
    dec = decode(load_eth(), V_ETH_ARP)
    assert dec.ok
    assert [r.name for r in dec.records] == ["eth"]
    assert dec.by_name("eth")["ethertype"] == 0x0806
    assert dec.payload_offset == 14


# ihl=6 -> a 24-byte IPv4 header (20 fixed + 4 option), protocol 6 = TCP
V_ETH_TCP_OPT = hexb(
    ETH_HDR,
    "46 00 001c 0001 0000 20 06 ffff 0a000001 0a000002",
    "deadbeef",  # the one option word
    "aabbccdd",  # payload
)


def test_vector_ipv4_options_and_non_udp_protocol():
    dec = decode(load_eth(), V_ETH_TCP_OPT)
    assert dec.ok
    assert [r.name for r in dec.records] == ["eth", "ipv4"]
    ip = dec.by_name("ipv4")
    assert ip["ihl"] == 6
    assert ip["protocol"] == 6
    assert ip["dst_ip"] == bytes.fromhex("0a000002")
    # 14 + 6*4 = 38: the option word is header, not payload.
    assert dec.payload_offset == 38
    assert V_ETH_TCP_OPT[38:] == bytes.fromhex("aabbccdd")


# ihl=15 -> the largest legal IPv4 header: 20 fixed + 40 option bytes.
V_ETH_MAX_IHL = hexb(
    ETH_HDR,
    "4f 00 0050 0002 0000 ff 11 0000 01020304 05060708",
    "00112233445566778899aabbccddeeff" * 2 + "0123456789abcdef",
    "0050 0051 0009 0000",
    "5a",
)


def test_vector_largest_ipv4_header():
    assert len(V_ETH_MAX_IHL) == 14 + 60 + 8 + 1
    dec = decode(load_eth(), V_ETH_MAX_IHL)
    assert dec.ok
    assert dec.by_name("ipv4")["ihl"] == 15
    assert dec.payload_offset == 82
    assert dec.by_name("udp") == dec.by_name("udp")
    assert dec.by_name("udp")["src_port"] == 80
    assert dec.by_name("udp")["dst_port"] == 81
    assert V_ETH_MAX_IHL[82:] == b"\x5a"


def test_vector_ihl_below_minimum():
    bad = bytearray(V_ETH_UDP)
    bad[14] = 0x44  # ver=4, ihl=4 -> 16 bytes, below the 20-byte fixed part
    dec = decode(load_eth(), bytes(bad))
    assert dec.defects == [Defect.BAD_HEADER_LENGTH]


def test_vector_truncated_before_the_ethernet_header_ends():
    dec = decode(load_eth(), hexb("0123456789ab cdef00"))
    assert dec.defects == [Defect.TRUNCATED_HEADER]


# --------------------------------------------------------------------------
# simple_feed -- transcribed byte by byte
# --------------------------------------------------------------------------

# session_id=0xDEADBEEF, seq_num=1, msg_count=0, reserved=0
V_FEED_EMPTY = hexb("deadbeef", "0000000000000001", "0000", "0000")


def test_vector_feed_header_only():
    assert len(V_FEED_EMPTY) == 16
    dec = decode(load_feed(), V_FEED_EMPTY)
    assert dec.ok, dec.errors
    assert dec.header.values == {
        "session_id": 0xDEADBEEF,
        "seq_num": 1,
        "msg_count": 0,
        "reserved": 0,
    }
    assert dec.messages == []


# msg_len=12 (14 bytes, less the 2-byte length itself), type=3
# session_state=1, reason_code=0xFFFF = -1, timestamp=255
V_FEED_STATUS = hexb(
    "01020304", "0000000000000002", "0001", "0000",
    "000c", "03", "01", "ffff", "00000000000000ff",
)


def test_vector_feed_status_message():
    assert len(V_FEED_STATUS) == 16 + 14
    dec = decode(load_feed(), V_FEED_STATUS)
    assert dec.ok, dec.errors
    assert dec.header["session_id"] == 0x01020304
    assert dec.header["seq_num"] == 2
    assert len(dec.messages) == 1
    m = dec.messages[0]
    assert m.name == "status"
    assert m.code == 3
    assert m.offset == 16
    assert m.length == 14
    assert m.values == {
        "msg_len": 12,
        "msg_type": 3,
        "session_state": 1,
        "reason_code": -1,  # 0xFFFF read as a signed 16-bit value
        "timestamp": 255,
    }


# msg_len=23 (25 bytes less the length field), type=1, symbol="AAPL    "
# price = 0x8000000000000000 = INT64_MIN, qty = 0xFFFFFFFF, side='B', flags=1
V_FEED_TRADE = hexb(
    "00000001", "ffffffffffffffff", "0001", "abcd",
    "0017", "01", "4141504c20202020",
    "8000000000000000", "ffffffff", "42", "01",
)


def test_vector_feed_trade_at_the_extremes():
    assert len(V_FEED_TRADE) == 16 + 25
    dec = decode(load_feed(), V_FEED_TRADE)
    assert dec.ok, dec.errors
    assert dec.header["seq_num"] == 0xFFFFFFFFFFFFFFFF == 2**64 - 1
    assert dec.header["reserved"] == 0xABCD
    m = dec.messages[0]
    assert m.name == "trade"
    assert m.values == {
        "msg_len": 23,
        "msg_type": 1,
        "symbol": b"AAPL    ",
        "price": -(2**63),  # 0x8000... is the most negative int64, not +2^63
        "qty": 2**32 - 1,
        "side": 0x42,
        "trade_flags": 1,
    }


# msg_len=21 (23 bytes less the length field), type=2, symbol="MSFT    "
# bid_px = -1, ask_px = INT32_MAX, bid_sz = 100, ask_sz = 65535
V_FEED_QUOTE = hexb(
    "00000002", "0000000000000003", "0001", "0000",
    "0015", "02", "4d5346542020202020"[:16],
    "ffffffff", "7fffffff", "0064", "ffff",
)


def test_vector_feed_quote_signed_and_unsigned_side_by_side():
    assert len(V_FEED_QUOTE) == 16 + 23
    m = decode(load_feed(), V_FEED_QUOTE).messages[0]
    assert m.name == "quote"
    assert m.values == {
        "msg_len": 21,
        "msg_type": 2,
        "symbol": b"MSFT    ",
        "bid_px": -1,  # 0xFFFFFFFF signed
        "ask_px": 2**31 - 1,
        "bid_sz": 100,  # the same 0xFFFFFFFF pattern split unsigned
        "ask_sz": 2**16 - 1,
    }


# msg_len=30 (32 bytes less the length field), type=4, symbol="TSLA    "
# paired_qty = 0 (u64), imbalance_qty = INT64_MAX, ref_px = INT32_MIN, auction=7
V_FEED_IMBALANCE = hexb(
    "00000003", "0000000000000004", "0001", "0000",
    "001e", "04", "54534c4120202020",
    "0000000000000000", "7fffffffffffffff", "80000000", "07",
)


def test_vector_feed_imbalance_largest_message():
    assert len(V_FEED_IMBALANCE) == 16 + 32
    m = decode(load_feed(), V_FEED_IMBALANCE).messages[0]
    assert m.name == "imbalance"
    assert m.length == 32
    assert m.values == {
        "msg_len": 30,
        "msg_type": 4,
        "symbol": b"TSLA    ",
        "paired_qty": 0,
        "imbalance_qty": 2**63 - 1,
        "ref_px": -(2**31),
        "auction_type": 7,
    }


# Three messages of different lengths, so the second and third only decode
# correctly if the length prefix advanced the walk by exactly 14 and 25 bytes.
V_FEED_THREE = hexb(
    "0badc0de", "0000000000000005", "0003", "0000",
    "000c", "03", "02", "0100", "0000000000000001",              # status, 14 B
    "0017", "01", "5a5a5a5a5a5a5a5a",
    "0000000000000064", "00000005", "53", "00",                   # trade, 25 B
    "0015", "02", "4141414120202020",
    "00000001", "00000002", "0003", "0004",                       # quote, 23 B
)


def test_vector_feed_three_messages_walk_by_the_length_prefix():
    assert len(V_FEED_THREE) == 16 + 14 + 25 + 23
    dec = decode(load_feed(), V_FEED_THREE)
    assert dec.ok, dec.errors
    assert [m.name for m in dec.messages] == ["status", "trade", "quote"]
    assert [m.offset for m in dec.messages] == [16, 30, 55]
    assert [m.length for m in dec.messages] == [14, 25, 23]
    assert dec.messages[0]["reason_code"] == 0x0100 == 256
    assert dec.messages[1]["price"] == 100
    assert dec.messages[1]["side"] == 0x53
    assert dec.messages[2]["ask_sz"] == 4


def test_vector_length_prefix_excludes_itself():
    """`length_covers: after_length` -- 12 on the wire means 14 bytes total.

    Inverting this is the single most plausible schema mistake, and it is
    invisible in a one-message packet: the walk ends either way. Here the
    second message only lands if the first advanced by 14.
    """
    assert V_FEED_THREE[16:18] == b"\x00\x0c"  # declares 12
    assert V_FEED_THREE[30:32] == b"\x00\x17"  # the next message starts at 30
    dec = decode(load_feed(), V_FEED_THREE)
    assert dec.messages[1].offset == 16 + 12 + 2


def test_vector_feed_bad_length_and_unknown_type():
    # Declared on a packet with room to spare, so the verdict is about the
    # length disagreeing with the type rather than about running out of bytes.
    bad_len = bytearray(V_FEED_THREE)
    bad_len[16:18] = b"\x00\x0d"  # 13 -> a 15-byte message; status is 14
    assert decode(load_feed(), bytes(bad_len)).defects == [Defect.BAD_LENGTH]

    # The same declaration at the end of the packet is truncation instead: the
    # bytes it asks for are not there, and that is the earlier verdict.
    short = bytearray(V_FEED_STATUS)
    short[16:18] = b"\x00\x0d"
    assert decode(load_feed(), bytes(short)).defects == [Defect.TRUNCATED_MESSAGE]

    bad_type = bytearray(V_FEED_STATUS)
    bad_type[18] = 0x7F
    assert decode(load_feed(), bytes(bad_type)).defects == [Defect.UNKNOWN_TYPE]

    zero_len = bytearray(V_FEED_STATUS)
    zero_len[16:18] = b"\x00\x00"
    assert decode(load_feed(), bytes(zero_len)).defects == [Defect.ZERO_LENGTH]


# --------------------------------------------------------------------------
# Byte maps -- which byte feeds which field, written from the protocol
# --------------------------------------------------------------------------

#: Frame byte index -> the fields it contributes to, for `V_ETH_UDP`.
#: "STRUCTURAL" marks a byte that changes what gets parsed at all, not just a
#: value; those are checked separately below.
ETH_BYTE_MAP: dict[int, object] = {
    **{i: {"eth.dst_mac"} for i in range(0, 6)},
    **{i: {"eth.src_mac"} for i in range(6, 12)},
    12: "STRUCTURAL",  # ethertype selects the next layer
    13: "STRUCTURAL",
    14: "STRUCTURAL",  # version/ihl -- ihl moves every later layer
    15: {"ipv4.dscp", "ipv4.ecn"},
    16: {"ipv4.total_length"},
    17: {"ipv4.total_length"},
    18: {"ipv4.identification"},
    19: {"ipv4.identification"},
    20: {"ipv4.flags", "ipv4.frag_offset"},
    21: {"ipv4.frag_offset"},
    22: {"ipv4.ttl"},
    23: "STRUCTURAL",  # protocol selects the next layer
    24: {"ipv4.hdr_checksum"},
    25: {"ipv4.hdr_checksum"},
    **{i: {"ipv4.src_ip"} for i in range(26, 30)},
    **{i: {"ipv4.dst_ip"} for i in range(30, 34)},
    34: {"udp.src_port"},
    35: {"udp.src_port"},
    36: {"udp.dst_port"},
    37: {"udp.dst_port"},
    38: {"udp.length"},
    39: {"udp.length"},
    40: {"udp.checksum"},
    41: {"udp.checksum"},
}


@pytest.mark.parametrize("index", sorted(ETH_BYTE_MAP))
def test_eth_byte_map(index):
    """Flip one byte; exactly the fields that byte feeds must change.

    Catches an offset that is right for one field and wrong for its neighbour,
    which a per-field round trip cannot: a field read one byte early still
    round-trips if the builder writes it one byte early too.
    """
    want = ETH_BYTE_MAP[index]
    base = decode(load_eth(), V_ETH_UDP)
    poked = bytearray(V_ETH_UDP)
    poked[index] ^= 0xA5
    other = decode(load_eth(), bytes(poked))

    if want == "STRUCTURAL":
        assert [r.name for r in other.records] != [r.name for r in base.records] or (
            other.payload_offset != base.payload_offset
        ), f"byte {index} was declared structural but changed nothing structural"
        return

    assert other.ok, other.errors
    assert changed(flat(base), flat(other)) == want, f"byte {index}"


#: Packet byte index -> fields, for `V_FEED_TRADE` (16-byte header + one trade).
FEED_BYTE_MAP: dict[int, object] = {
    **{i: {"feed_hdr.session_id"} for i in range(0, 4)},
    **{i: {"feed_hdr.seq_num"} for i in range(4, 12)},
    12: "STRUCTURAL",  # msg_count -- a mismatch is a defect, not a value change
    13: "STRUCTURAL",
    14: {"feed_hdr.reserved"},
    15: {"feed_hdr.reserved"},
    16: "STRUCTURAL",  # msg_len
    17: "STRUCTURAL",
    18: "STRUCTURAL",  # msg_type
    **{i: {"msg0.symbol"} for i in range(19, 27)},
    **{i: {"msg0.price"} for i in range(27, 35)},
    **{i: {"msg0.qty"} for i in range(35, 39)},
    39: {"msg0.side"},
    40: {"msg0.trade_flags"},
}


@pytest.mark.parametrize("index", sorted(FEED_BYTE_MAP))
def test_feed_byte_map(index):
    want = FEED_BYTE_MAP[index]
    base = decode(load_feed(), V_FEED_TRADE)
    poked = bytearray(V_FEED_TRADE)
    poked[index] ^= 0xA5
    other = decode(load_feed(), bytes(poked))

    if want == "STRUCTURAL":
        assert not other.ok, f"byte {index} was declared structural but still parsed"
        return

    assert other.ok, other.errors
    assert changed(flat(base), flat(other)) == want, f"byte {index}"


# --------------------------------------------------------------------------
# Fixtures. Loaded per call rather than via the shared session fixtures so
# nothing in this file can be perturbed by another test's use of them.
# --------------------------------------------------------------------------


def load_eth():
    from wirespec.ir import load_ir

    return load_ir("schemas/eth_ipv4_udp.yaml")


def load_feed():
    from wirespec.ir import load_ir

    return load_ir("schemas/simple_feed.yaml")
