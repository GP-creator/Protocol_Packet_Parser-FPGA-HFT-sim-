"""Packet builders.

Everything here is written against the protocol descriptions in prose, using
:mod:`struct`.  Nothing in this file reads the schema, the IR, or the golden
decoder -- that is the point.  A packet built here and decoded by
:mod:`wirespec.golden` exercises two independent readings of one specification,
so agreement between them is evidence and not a tautology.
"""

from __future__ import annotations

import random
import struct
from dataclasses import dataclass, field as dc_field

# --------------------------------------------------------------------------
# Common
# --------------------------------------------------------------------------


@dataclass
class Packet:
    """Wire bytes plus the values the decoder is expected to recover."""

    data: bytes
    expect: dict = dc_field(default_factory=dict)
    note: str = ""

    def __len__(self) -> int:
        return len(self.data)


# --------------------------------------------------------------------------
# eth_ipv4_udp
# --------------------------------------------------------------------------

ETHERTYPE_IPV4 = 0x0800
ETHERTYPE_ARP = 0x0806
IP_PROTO_UDP = 17
IP_PROTO_TCP = 6


def ipv4_checksum(header: bytes) -> int:
    """Standard one's-complement checksum over an IPv4 header."""
    if len(header) % 2:
        header += b"\x00"
    total = 0
    for i in range(0, len(header), 2):
        total += (header[i] << 8) | header[i + 1]
        total = (total & 0xFFFF) + (total >> 16)
    return (~total) & 0xFFFF


def build_eth_ipv4_udp(
    *,
    dst_mac: bytes = b"\x00\x11\x22\x33\x44\x55",
    src_mac: bytes = b"\xaa\xbb\xcc\xdd\xee\xff",
    ethertype: int = ETHERTYPE_IPV4,
    version: int = 4,
    ihl: int = 5,
    dscp: int = 0,
    ecn: int = 0,
    identification: int = 0x1234,
    flags: int = 0b010,
    frag_offset: int = 0,
    ttl: int = 64,
    protocol: int = IP_PROTO_UDP,
    src_ip: bytes = b"\xc0\xa8\x00\x01",
    dst_ip: bytes = b"\xc0\xa8\x00\x02",
    src_port: int = 5000,
    dst_port: int = 5001,
    payload: bytes = b"",
    udp_checksum: int = 0,
    options: bytes | None = None,
    total_length: int | None = None,
    udp_length: int | None = None,
) -> Packet:
    """Build one Ethernet/IPv4/UDP frame.

    ``ihl`` above 5 inserts (ihl-5)*4 bytes of IPv4 options; supply ``options``
    to control their content, otherwise they are filled with a NOP pattern.
    """
    assert len(dst_mac) == 6 and len(src_mac) == 6
    assert len(src_ip) == 4 and len(dst_ip) == 4
    assert 5 <= ihl <= 15

    opt_len = (ihl - 5) * 4
    if options is None:
        options = bytes((0x01,)) * opt_len
    assert len(options) == opt_len, f"options must be exactly {opt_len} bytes"

    eth = struct.pack(">6s6sH", dst_mac, src_mac, ethertype)

    udp_len = udp_length if udp_length is not None else 8 + len(payload)
    udp = struct.pack(">HHHH", src_port, dst_port, udp_len, udp_checksum)

    ip_total = total_length if total_length is not None else 20 + opt_len + len(udp) + len(payload)
    ver_ihl = (version << 4) | ihl
    dscp_ecn = (dscp << 2) | ecn
    flags_frag = (flags << 13) | frag_offset

    ip_no_ck = struct.pack(
        ">BBHHHBBH4s4s",
        ver_ihl,
        dscp_ecn,
        ip_total,
        identification,
        flags_frag,
        ttl,
        protocol,
        0,
        src_ip,
        dst_ip,
    )
    checksum = ipv4_checksum(ip_no_ck + options)
    ip = ip_no_ck[:10] + struct.pack(">H", checksum) + ip_no_ck[12:] + options

    data = eth + ip + udp + payload

    expect: dict = {
        "eth": {"dst_mac": dst_mac, "src_mac": src_mac, "ethertype": ethertype},
    }
    if ethertype == ETHERTYPE_IPV4:
        expect["ipv4"] = {
            "version": version,
            "ihl": ihl,
            "dscp": dscp,
            "ecn": ecn,
            "total_length": ip_total,
            "identification": identification,
            "flags": flags,
            "frag_offset": frag_offset,
            "ttl": ttl,
            "protocol": protocol,
            "hdr_checksum": checksum,
            "src_ip": src_ip,
            "dst_ip": dst_ip,
        }
        if protocol == IP_PROTO_UDP:
            expect["udp"] = {
                "src_port": src_port,
                "dst_port": dst_port,
                "udp_length": udp_len,
                "udp_checksum": udp_checksum,
            }
            expect["payload"] = payload
            expect["payload_offset"] = 14 + 20 + opt_len + 8
        else:
            expect["payload_offset"] = 14 + 20 + opt_len
    else:
        expect["payload_offset"] = 14

    return Packet(data, expect)


def random_eth_ipv4_udp(rng: random.Random, *, max_payload: int = 128) -> Packet:
    """A random well-formed frame; occasionally one whose chain stops early."""
    roll = rng.random()
    if roll < 0.06:
        ethertype = rng.choice([ETHERTYPE_ARP, 0x8100, 0x86DD])
        protocol = IP_PROTO_UDP
    elif roll < 0.12:
        ethertype = ETHERTYPE_IPV4
        protocol = rng.choice([1, IP_PROTO_TCP, 47, 132])
    else:
        ethertype = ETHERTYPE_IPV4
        protocol = IP_PROTO_UDP

    return build_eth_ipv4_udp(
        dst_mac=bytes(rng.randrange(256) for _ in range(6)),
        src_mac=bytes(rng.randrange(256) for _ in range(6)),
        ethertype=ethertype,
        ihl=rng.choice([5, 5, 5, 6, 7, 8, 10, 15]),
        dscp=rng.randrange(64),
        ecn=rng.randrange(4),
        identification=rng.randrange(1 << 16),
        flags=rng.randrange(8),
        frag_offset=rng.randrange(1 << 13),
        ttl=rng.randrange(256),
        protocol=protocol,
        src_ip=bytes(rng.randrange(256) for _ in range(4)),
        dst_ip=bytes(rng.randrange(256) for _ in range(4)),
        src_port=rng.randrange(1 << 16),
        dst_port=rng.randrange(1 << 16),
        udp_checksum=rng.randrange(1 << 16),
        payload=bytes(rng.randrange(256) for _ in range(rng.randrange(max_payload + 1))),
    )


# --------------------------------------------------------------------------
# simple_feed
# --------------------------------------------------------------------------

FEED_HDR_BYTES = 16

MSG_TRADE = 0x01
MSG_QUOTE = 0x02
MSG_STATUS = 0x03
MSG_IMBALANCE = 0x04

SIDE_BUY = 0x42
SIDE_SELL = 0x53

#: wire size of a complete message, prefix included, keyed by type code
FEED_MSG_BYTES = {MSG_TRADE: 25, MSG_QUOTE: 23, MSG_STATUS: 14, MSG_IMBALANCE: 32}

SYMBOLS = [
    b"AAPL    ",
    b"MSFT    ",
    b"GOOG    ",
    b"TSLA    ",
    b"BRK.B   ",
    b"SPY     ",
    b"NVDA    ",
    b"AMZN    ",
]


def _msg(code: int, body: bytes, *, length_override: int | None = None) -> bytes:
    """Prepend the 2-byte length and 1-byte type. Length counts type+payload."""
    n = length_override if length_override is not None else 1 + len(body)
    return struct.pack(">HB", n, code) + body


def make_trade(
    symbol: bytes = b"AAPL    ",
    price: int = 1234500,
    qty: int = 100,
    side: int = SIDE_BUY,
    trade_flags: int = 0,
    **kw,
) -> tuple[bytes, dict]:
    assert len(symbol) == 8
    body = struct.pack(">8sqIBB", symbol, price, qty, side, trade_flags)
    return _msg(MSG_TRADE, body, **kw), {
        "type": "trade",
        "code": MSG_TRADE,
        "msg_len": 23,
        "msg_type": MSG_TRADE,
        "symbol": symbol,
        "price": price,
        "qty": qty,
        "side": side,
        "trade_flags": trade_flags,
    }


def make_quote(
    symbol: bytes = b"MSFT    ",
    bid_px: int = -50,
    ask_px: int = 51,
    bid_sz: int = 10,
    ask_sz: int = 20,
    **kw,
) -> tuple[bytes, dict]:
    assert len(symbol) == 8
    body = struct.pack(">8siiHH", symbol, bid_px, ask_px, bid_sz, ask_sz)
    return _msg(MSG_QUOTE, body, **kw), {
        "type": "quote",
        "code": MSG_QUOTE,
        "msg_len": 21,
        "msg_type": MSG_QUOTE,
        "symbol": symbol,
        "bid_px": bid_px,
        "ask_px": ask_px,
        "bid_sz": bid_sz,
        "ask_sz": ask_sz,
    }


def make_status(
    session_state: int = 1, reason_code: int = -3, timestamp: int = 0xDEADBEEFCAFE, **kw
) -> tuple[bytes, dict]:
    body = struct.pack(">BhQ", session_state, reason_code, timestamp)
    return _msg(MSG_STATUS, body, **kw), {
        "type": "status",
        "code": MSG_STATUS,
        "msg_len": 12,
        "msg_type": MSG_STATUS,
        "session_state": session_state,
        "reason_code": reason_code,
        "timestamp": timestamp,
    }


def make_imbalance(
    symbol: bytes = b"SPY     ",
    paired_qty: int = 1_000_000,
    imbalance_qty: int = -250_000,
    ref_px: int = 4_500_00,
    auction_type: int = 1,
    **kw,
) -> tuple[bytes, dict]:
    assert len(symbol) == 8
    body = struct.pack(">8sQqiB", symbol, paired_qty, imbalance_qty, ref_px, auction_type)
    return _msg(MSG_IMBALANCE, body, **kw), {
        "type": "imbalance",
        "code": MSG_IMBALANCE,
        "msg_len": 30,
        "msg_type": MSG_IMBALANCE,
        "symbol": symbol,
        "paired_qty": paired_qty,
        "imbalance_qty": imbalance_qty,
        "ref_px": ref_px,
        "auction_type": auction_type,
    }


MSG_MAKERS = (make_trade, make_quote, make_status, make_imbalance)


def build_feed_packet(
    messages: list[tuple[bytes, dict]],
    *,
    session_id: int = 0x53455331,
    seq_num: int = 1,
    reserved: int = 0,
    count_override: int | None = None,
    trailer: bytes = b"",
) -> Packet:
    """Assemble a feed packet from already-built messages."""
    count = count_override if count_override is not None else len(messages)
    hdr = struct.pack(">IQHH", session_id, seq_num, count, reserved)
    body = b"".join(m for m, _ in messages)
    return Packet(
        hdr + body + trailer,
        {
            "feed_hdr": {
                "session_id": session_id,
                "seq_num": seq_num,
                "msg_count": count,
                "reserved": reserved,
            },
            "messages": [meta for _, meta in messages],
        },
    )


def random_feed_message(rng: random.Random) -> tuple[bytes, dict]:
    maker = rng.choice(MSG_MAKERS)
    sym = rng.choice(SYMBOLS)
    if maker is make_trade:
        return make_trade(
            symbol=sym,
            price=rng.randrange(-(1 << 63), 1 << 63),
            qty=rng.randrange(1 << 32),
            side=rng.choice([SIDE_BUY, SIDE_SELL]),
            trade_flags=rng.randrange(256),
        )
    if maker is make_quote:
        return make_quote(
            symbol=sym,
            bid_px=rng.randrange(-(1 << 31), 1 << 31),
            ask_px=rng.randrange(-(1 << 31), 1 << 31),
            bid_sz=rng.randrange(1 << 16),
            ask_sz=rng.randrange(1 << 16),
        )
    if maker is make_status:
        return make_status(
            session_state=rng.randrange(4),
            reason_code=rng.randrange(-(1 << 15), 1 << 15),
            timestamp=rng.randrange(1 << 64),
        )
    return make_imbalance(
        symbol=sym,
        paired_qty=rng.randrange(1 << 64),
        imbalance_qty=rng.randrange(-(1 << 63), 1 << 63),
        ref_px=rng.randrange(-(1 << 31), 1 << 31),
        auction_type=rng.randrange(4),
    )


def random_feed_packet(
    rng: random.Random, *, seq_num: int = 1, min_msgs: int = 0, max_msgs: int = 8
) -> Packet:
    n = rng.randint(min_msgs, max_msgs)
    msgs = [random_feed_message(rng) for _ in range(n)]
    return build_feed_packet(
        msgs,
        session_id=rng.randrange(1 << 32),
        seq_num=seq_num,
        reserved=rng.randrange(1 << 16),
    )


# --------------------------------------------------------------------------
# Deliberately malformed packets
# --------------------------------------------------------------------------


def feed_truncated_header(n: int = 9) -> Packet:
    """Fewer than 16 header bytes."""
    return Packet(bytes(range(n)), {}, note=f"header truncated to {n} bytes")


def feed_truncated_message(rng: random.Random | None = None, cut: int = 5) -> Packet:
    """A well-formed packet with the last message cut short."""
    rng = rng or random.Random(0)
    msgs = [make_trade(), make_quote()]
    pkt = build_feed_packet(msgs)
    return Packet(pkt.data[:-cut], {}, note=f"last message short by {cut} bytes")


def feed_bad_length(delta: int = 3) -> Packet:
    """Length prefix disagrees with the size implied by the type code."""
    body, _ = make_trade(length_override=23 + delta)
    pad = bytes(max(0, delta))
    return Packet(
        build_feed_packet([]).data + body + pad,
        {},
        note=f"trade length prefix off by {delta:+d}",
    )


def feed_zero_length() -> Packet:
    body = struct.pack(">HB", 0, MSG_TRADE) + bytes(22)
    return Packet(build_feed_packet([]).data + body, {}, note="zero length prefix")


def feed_unknown_type(code: int = 0x7F) -> Packet:
    body = struct.pack(">HB", 11, code) + bytes(10)
    return Packet(build_feed_packet([]).data + body, {}, note=f"unknown type 0x{code:02x}")


def feed_count_mismatch() -> Packet:
    return build_feed_packet([make_trade(), make_quote()], count_override=5)


MALFORMED_FEED_BUILDERS = (
    feed_truncated_header,
    feed_truncated_message,
    feed_bad_length,
    feed_zero_length,
    feed_unknown_type,
    feed_count_mismatch,
)
