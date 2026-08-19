"""Directed tests for the reference decoder."""

from __future__ import annotations

import struct

import pytest

from tb.common import stimulus as st
from wirespec.golden import Decoded, Defect, StopReason, StreamModel, decode
from wirespec.ir import build_ir
from wirespec.schema import parse_schema

# --------------------------------------------------------------------------
# Helpers shared with the randomised suite
# --------------------------------------------------------------------------


def check_eth(ir, pkt: st.Packet) -> Decoded:
    """Assert the golden decoder recovers exactly what the builder put in."""
    dec = decode(ir, pkt.data)
    assert dec.ok, f"unexpected defects {dec.errors}"
    for layer in ("eth", "ipv4", "udp"):
        want = pkt.expect.get(layer)
        got = dec.by_name(layer)
        if want is None:
            assert got is None, f"decoded layer '{layer}' that the builder did not emit"
            continue
        assert got is not None, f"layer '{layer}' missing from the decode"
        assert got.values == want, f"layer '{layer}' mismatch"
    assert dec.payload_offset == pkt.expect["payload_offset"]
    if "payload" in pkt.expect:
        assert pkt.data[dec.payload_offset :] == pkt.expect["payload"]
    return dec


def check_feed(ir, pkt: st.Packet) -> Decoded:
    dec = decode(ir, pkt.data)
    assert dec.ok, f"unexpected defects {dec.errors}"
    assert dec.header is not None
    assert dec.header.values == pkt.expect["feed_hdr"]
    want_msgs = pkt.expect["messages"]
    assert len(dec.messages) == len(want_msgs), "message count mismatch"
    for got, want in zip(dec.messages, want_msgs, strict=True):
        assert got.name == want["type"]
        assert got.code == want["code"]
        expected_values = {k: v for k, v in want.items() if k not in ("type", "code")}
        assert got.values == expected_values, f"message '{got.name}' at byte {got.offset}"
    return dec


# --------------------------------------------------------------------------
# eth_ipv4_udp
# --------------------------------------------------------------------------


def test_minimal_udp_frame(eth_ir):
    pkt = st.build_eth_ipv4_udp(payload=b"hello")
    dec = check_eth(eth_ir, pkt)
    assert dec.stop_reason is StopReason.END_OF_CHAIN
    assert [r.name for r in dec.records] == ["eth", "ipv4", "udp"]
    assert dec.payload_offset == 42
    assert dec.payload_len == 5


@pytest.mark.parametrize("ihl", [5, 6, 7, 8, 10, 15])
def test_variable_ihl(eth_ir, ihl):
    pkt = st.build_eth_ipv4_udp(ihl=ihl, payload=b"\xa5" * 16)
    dec = check_eth(eth_ir, pkt)
    assert dec.by_name("ipv4").length == ihl * 4
    assert dec.payload_offset == 14 + ihl * 4 + 8
    assert dec.payload_len == 16


def test_sub_byte_fields_round_trip(eth_ir):
    pkt = st.build_eth_ipv4_udp(dscp=0b101011, ecn=0b10, flags=0b101, frag_offset=0x1AAA)
    dec = check_eth(eth_ir, pkt)
    ip = dec.by_name("ipv4").values
    assert ip["version"] == 4
    assert ip["dscp"] == 0b101011
    assert ip["ecn"] == 0b10
    assert ip["flags"] == 0b101
    assert ip["frag_offset"] == 0x1AAA


def test_chain_stops_on_unknown_ethertype(eth_ir):
    pkt = st.build_eth_ipv4_udp(ethertype=st.ETHERTYPE_ARP, payload=b"")
    dec = decode(eth_ir, pkt.data)
    assert dec.ok
    assert dec.stop_reason is StopReason.UNKNOWN_SELECTOR
    assert [r.name for r in dec.records] == ["eth"]
    assert dec.payload_offset == 14


def test_chain_stops_on_non_udp_protocol(eth_ir):
    pkt = st.build_eth_ipv4_udp(protocol=st.IP_PROTO_TCP, payload=b"")
    dec = decode(eth_ir, pkt.data)
    assert dec.ok
    assert dec.stop_reason is StopReason.UNKNOWN_SELECTOR
    assert [r.name for r in dec.records] == ["eth", "ipv4"]


def test_truncated_ethernet_header(eth_ir):
    dec = decode(eth_ir, b"\x00" * 9)
    assert not dec.ok
    assert dec.defects == [Defect.TRUNCATED_HEADER]
    assert dec.stop_reason is StopReason.DEFECT


def test_truncated_udp_header(eth_ir):
    pkt = st.build_eth_ipv4_udp(payload=b"")
    dec = decode(eth_ir, pkt.data[:-3])
    assert dec.defects == [Defect.TRUNCATED_HEADER]


def test_ihl_below_minimum_is_rejected(eth_ir):
    pkt = st.build_eth_ipv4_udp(payload=b"")
    bad = bytearray(pkt.data)
    bad[14] = (4 << 4) | 4  # IHL=4 -> 16 bytes, below the 20-byte minimum
    dec = decode(eth_ir, bytes(bad))
    assert dec.defects == [Defect.BAD_HEADER_LENGTH]


def test_ihl_declares_more_than_is_present(eth_ir):
    pkt = st.build_eth_ipv4_udp(payload=b"")
    bad = bytearray(pkt.data)
    bad[14] = (4 << 4) | 15  # IHL=15 -> 60 bytes of header that are not there
    dec = decode(eth_ir, bytes(bad))
    assert dec.defects == [Defect.TRUNCATED_HEADER]


def test_mac_and_ip_fields_are_raw_bytes(eth_ir):
    pkt = st.build_eth_ipv4_udp(
        dst_mac=b"\x01\x02\x03\x04\x05\x06", src_ip=b"\x08\x08\x08\x08"
    )
    dec = check_eth(eth_ir, pkt)
    assert dec.by_name("eth")["dst_mac"] == b"\x01\x02\x03\x04\x05\x06"
    assert dec.by_name("ipv4")["src_ip"] == b"\x08\x08\x08\x08"


# --------------------------------------------------------------------------
# simple_feed
# --------------------------------------------------------------------------


def test_empty_feed_packet(feed_ir):
    pkt = st.build_feed_packet([])
    dec = check_feed(feed_ir, pkt)
    assert dec.messages == []
    assert dec.header["msg_count"] == 0


def test_one_of_each_message_type(feed_ir):
    pkt = st.build_feed_packet(
        [st.make_trade(), st.make_quote(), st.make_status(), st.make_imbalance()]
    )
    dec = check_feed(feed_ir, pkt)
    assert [m.name for m in dec.messages] == ["trade", "quote", "status", "imbalance"]
    assert [m.offset for m in dec.messages] == [16, 41, 64, 78]
    assert [m.length for m in dec.messages] == [25, 23, 14, 32]
    assert len(pkt.data) == 16 + 25 + 23 + 14 + 32


def test_signed_fields_at_their_extremes(feed_ir):
    lo, hi = -(2**63), 2**63 - 1
    pkt = st.build_feed_packet(
        [
            st.make_trade(price=lo),
            st.make_trade(price=hi),
            st.make_trade(price=-1),
            st.make_quote(bid_px=-(2**31), ask_px=2**31 - 1),
            st.make_status(reason_code=-(2**15)),
            st.make_imbalance(imbalance_qty=lo, paired_qty=2**64 - 1),
        ]
    )
    dec = check_feed(feed_ir, pkt)
    assert dec.messages[0]["price"] == lo
    assert dec.messages[1]["price"] == hi
    assert dec.messages[2]["price"] == -1
    assert dec.messages[5]["paired_qty"] == 2**64 - 1


def test_char_field_is_preserved_verbatim(feed_ir):
    pkt = st.build_feed_packet([st.make_trade(symbol=b"BRK.B   ")])
    dec = check_feed(feed_ir, pkt)
    assert dec.messages[0]["symbol"] == b"BRK.B   "


def test_big_endian_multi_byte_fields(feed_ir):
    """A hand-checked byte pattern, so an endian slip cannot hide behind symmetry."""
    pkt = st.build_feed_packet([st.make_status(timestamp=0x0102030405060708, reason_code=0x0102)])
    body = pkt.data[16:]
    assert body[:3] == b"\x00\x0c\x03"  # length 12, type 3
    assert body[4:6] == b"\x01\x02"  # reason_code, most significant byte first
    assert body[6:14] == b"\x01\x02\x03\x04\x05\x06\x07\x08"
    dec = check_feed(feed_ir, pkt)
    assert dec.messages[0]["timestamp"] == 0x0102030405060708
    assert dec.messages[0]["reason_code"] == 0x0102


@pytest.mark.parametrize("count", [1, 2, 5, 17, 40])
def test_many_messages(feed_ir, count):
    msgs = [st.make_trade(qty=i) for i in range(count)]
    dec = check_feed(feed_ir, st.build_feed_packet(msgs))
    assert [m["qty"] for m in dec.messages] == list(range(count))


# --------------------------------------------------------------------------
# Malformed feed packets
# --------------------------------------------------------------------------


def test_truncated_feed_header(feed_ir):
    dec = decode(feed_ir, st.feed_truncated_header(9).data)
    assert dec.defects == [Defect.TRUNCATED_HEADER]


def test_truncated_feed_message(feed_ir):
    dec = decode(feed_ir, st.feed_truncated_message(cut=5).data)
    assert dec.defects == [Defect.TRUNCATED_MESSAGE]
    assert len(dec.messages) == 1  # the first message decoded fine


def test_trailing_bytes_shorter_than_a_length_prefix(feed_ir):
    pkt = st.build_feed_packet([st.make_trade()])
    dec = decode(feed_ir, pkt.data + b"\x00")
    assert dec.defects == [Defect.TRUNCATED_MESSAGE]
    assert "too few for a 2-byte length prefix" in str(dec.errors[0])


@pytest.mark.parametrize("delta", [-3, -1, 1, 3, 100])
def test_bad_length_prefix(feed_ir, delta):
    dec = decode(feed_ir, st.feed_bad_length(delta).data)
    assert dec.defects[0] in (Defect.BAD_LENGTH, Defect.TRUNCATED_MESSAGE)


def test_zero_length_prefix(feed_ir):
    dec = decode(feed_ir, st.feed_zero_length().data)
    assert dec.defects == [Defect.ZERO_LENGTH]


def test_length_below_the_prefix():
    """Reachable only when the length covers the whole message, so build a schema for it."""
    ir = build_ir(
        parse_schema(
            {
                "name": "whole_len",
                "kind": "framed",
                "header": {"name": "h", "fields": [{"name": "pad", "bytes": 2}]},
                "messages": {
                    "length": {"name": "mlen", "bytes": 2},
                    "type": {"name": "mtype", "bytes": 1},
                    "length_covers": "whole_message",
                    "types": [
                        {"name": "only", "code": 1, "fields": [{"name": "v", "bytes": 4}]}
                    ],
                },
            }
        )
    )
    assert ir.expected_length_value(ir.record("only")) == 7
    # A length of 2 claims a message shorter than its own length+type prefix.
    data = b"\x00\x00" + struct.pack(">HB", 2, 1) + bytes(4)
    dec = decode(ir, data)
    assert dec.defects == [Defect.BAD_LENGTH]
    assert "below the 3-byte length+type prefix" in str(dec.errors[0])


def test_length_covers_payload_only():
    ir = build_ir(
        parse_schema(
            {
                "name": "payload_len",
                "kind": "framed",
                "header": {"name": "h", "fields": [{"name": "pad", "bytes": 2}]},
                "messages": {
                    "length": {"name": "mlen", "bytes": 2},
                    "type": {"name": "mtype", "bytes": 1},
                    "length_covers": "payload_only",
                    "types": [
                        {"name": "only", "code": 1, "fields": [{"name": "v", "bytes": 4}]}
                    ],
                },
            }
        )
    )
    assert ir.expected_length_value(ir.record("only")) == 4
    data = b"\x00\x00" + struct.pack(">HBI", 4, 1, 0xDEADBEEF)
    dec = decode(ir, data)
    assert dec.ok
    assert dec.messages[0]["v"] == 0xDEADBEEF


def test_unknown_message_type(feed_ir):
    dec = decode(feed_ir, st.feed_unknown_type(0x7F).data)
    assert dec.defects == [Defect.UNKNOWN_TYPE]


def test_count_mismatch_is_reported_after_a_clean_parse(feed_ir):
    pkt = st.feed_count_mismatch()
    dec = decode(feed_ir, pkt.data)
    assert dec.defects == [Defect.COUNT_MISMATCH]
    assert len(dec.messages) == 2  # both messages still decoded


def test_defect_stops_the_message_walk(feed_ir):
    good = st.make_trade()
    bad = (struct.pack(">HB", 11, 0x7F) + bytes(10), {})
    after = st.make_quote()
    pkt = st.build_feed_packet([good, bad, after], count_override=3)
    dec = decode(feed_ir, pkt.data)
    assert dec.defects == [Defect.UNKNOWN_TYPE]
    assert len(dec.messages) == 1  # nothing after the defect is trusted


# --------------------------------------------------------------------------
# Stream-level counters
# --------------------------------------------------------------------------


def test_stream_model_counts_packets_and_messages(feed_ir):
    model = StreamModel(feed_ir)
    for seq in range(1, 6):
        model.feed(st.build_feed_packet([st.make_trade(), st.make_quote()], seq_num=seq).data)
    assert model.stats.as_dict() == {
        "packets": 5,
        "messages": 10,
        "seq_gaps": 0,
        "malformed": 0,
    }


def test_stream_model_counts_sequence_gaps(feed_ir):
    model = StreamModel(feed_ir)
    for seq in (10, 11, 13, 14, 20):
        model.feed(st.build_feed_packet([], seq_num=seq).data)
    assert model.stats.seq_gaps == 2
    assert model.stats.packets == 5


def test_stream_model_counts_malformed(feed_ir):
    model = StreamModel(feed_ir)
    model.feed(st.build_feed_packet([], seq_num=1).data)
    model.feed(st.feed_zero_length().data)
    model.feed(st.build_feed_packet([], seq_num=2).data)
    assert model.stats.malformed == 1
    assert model.stats.packets == 3
    assert model.stats.seq_gaps == 0
