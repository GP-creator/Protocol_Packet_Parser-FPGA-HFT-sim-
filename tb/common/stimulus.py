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
                "length": udp_len,
                "checksum": udp_checksum,
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


def _lead_msgs(lead: int) -> list[tuple[bytes, dict]]:
    """`lead` well-formed messages to sit in front of a defective one.

    A defect in a packet's *first* message and a defect after a good one are
    different tests: the second one requires the framer to have retired state
    correctly before it can even reach the bad message.
    """
    return [make_status(timestamp=i) for i in range(lead)]


def feed_bad_length(delta: int = 3, *, lead: int = 0) -> Packet:
    """Length prefix disagrees with the size implied by the type code."""
    body, _ = make_trade(length_override=23 + delta)
    pad = bytes(max(0, delta))
    head = build_feed_packet(_lead_msgs(lead)).data
    return Packet(
        head + body + pad,
        {},
        note=f"trade length prefix off by {delta:+d} after {lead} good message(s)",
    )


def feed_zero_length(*, lead: int = 0) -> Packet:
    body = struct.pack(">HB", 0, MSG_TRADE) + bytes(22)
    return Packet(
        build_feed_packet(_lead_msgs(lead)).data + body,
        {},
        note=f"zero length prefix after {lead} good message(s)",
    )


def feed_unknown_type(code: int = 0x7F, *, lead: int = 0) -> Packet:
    body = struct.pack(">HB", 11, code) + bytes(10)
    return Packet(
        build_feed_packet(_lead_msgs(lead)).data + body,
        {},
        note=f"unknown type 0x{code:02x} after {lead} good message(s)",
    )


def feed_count_mismatch(declared: int = 5, actual: int = 2) -> Packet:
    msgs = [make_trade(), make_quote(), make_status(), make_imbalance()][:actual]
    return build_feed_packet(msgs, count_override=declared)


def feed_trailer(n: int = 1) -> Packet:
    """A clean packet with `n` stray bytes after the last message.

    Fewer than 3 trailing bytes cannot even carry a length prefix, which is a
    different path through the framer from a prefix that parses and then fails.
    """
    pkt = build_feed_packet([make_trade()])
    return Packet(pkt.data + bytes(n), {}, note=f"{n} stray trailing byte(s)")


def feed_huge_length(value: int = 0xFFFF) -> Packet:
    """A length prefix far larger than the stitch buffer can ever hold.

    The framer cannot buffer it, so it counts the bytes past instead -- the
    path B007 lived on. Whether the verdict is BAD_LENGTH or TRUNCATED then
    depends on whether the packet really is that long.
    """
    body = struct.pack(">HB", value, MSG_TRADE) + bytes(60)
    return Packet(
        build_feed_packet([]).data + body, {}, note=f"length prefix {value}"
    )


def feed_length_below_prefix(value: int = 0) -> Packet:
    """A declared length too small to cover even the type byte it introduces."""
    body = struct.pack(">HB", value, MSG_STATUS) + bytes(11)
    return Packet(
        build_feed_packet([]).data + body, {}, note=f"length {value} below the prefix"
    )


MALFORMED_FEED_BUILDERS = (
    feed_truncated_header,
    feed_truncated_message,
    feed_bad_length,
    feed_zero_length,
    feed_unknown_type,
    feed_count_mismatch,
    feed_trailer,
    feed_huge_length,
    feed_length_below_prefix,
)


def malformed_feed_corpus() -> list[Packet]:
    """Every malformed class the model can produce, at every locus we can put it.

    Kept here rather than inline in the testbench so `tests/` and `tb/` exercise
    the same list, and so the count in `metrics.json` is the length of one
    object rather than a number someone remembered to update.
    """
    return [
        # -- truncated header, at several depths
        feed_truncated_header(1),
        feed_truncated_header(9),
        feed_truncated_header(15),
        # -- truncated message, cut at different points
        feed_truncated_message(cut=1),
        feed_truncated_message(cut=5),
        feed_truncated_message(cut=22),
        # -- length prefix disagreeing with the type, first and later
        feed_bad_length(1),
        feed_bad_length(-1),
        feed_bad_length(3),
        feed_bad_length(100),  # more than the stitch buffer holds
        feed_bad_length(3, lead=1),
        feed_bad_length(-1, lead=3),
        # -- zero and undersized length
        feed_zero_length(),
        feed_zero_length(lead=2),
        feed_length_below_prefix(0),
        feed_length_below_prefix(1),
        # -- lengths beyond anything buildable
        feed_huge_length(0xFFFF),
        feed_huge_length(0x0100),
        # -- unknown type codes, first and later
        feed_unknown_type(0x00),
        feed_unknown_type(0x05),  # one past the last defined code
        feed_unknown_type(0x7F),
        feed_unknown_type(0xFF),
        feed_unknown_type(0x7F, lead=1),
        feed_unknown_type(0x00, lead=4),
        # -- the count field disagreeing in both directions
        feed_count_mismatch(5, 2),
        feed_count_mismatch(0, 2),
        feed_count_mismatch(0xFFFF, 1),
        feed_count_mismatch(1, 0),
        # -- stray bytes after the last message
        feed_trailer(1),
        feed_trailer(2),
        feed_trailer(3),
    ]


# --------------------------------------------------------------------------
# Directed extremes, for the value-class coverage bins
# --------------------------------------------------------------------------


def _extreme_round(r: int) -> tuple[int, int, int, int, int, int, bytes]:
    """Field values for round `r` of the extremes walk.

    Five rounds close every bin `tb.common.coverage` declares: signed fields
    need {min, neg, zero, pos, max}, unsigned {zero, mid, max}, and character
    fields {zero, mixed, ones}.
    """
    s64 = [-(2**63), -1, 0, 1, 2**63 - 1][r]
    s32 = [-(2**31), -1, 0, 1, 2**31 - 1][r]
    s16 = [-(2**15), -1, 0, 1, 2**15 - 1][r]
    u64 = [0, 1, 2**64 - 1, 1, 0][r]
    u32 = [0, 1, 2**32 - 1, 1, 0][r]
    u16 = [0, 1, 2**16 - 1, 1, 0][r]
    u8 = [0, 1, 255, 1, 0][r]
    sym = [bytes(8), b"BRK.B   ", b"\xff" * 8, b"AAPL    ", bytes(8)][r]
    return s64, s32, s16, u64, u32, u16, u8, sym  # type: ignore[return-value]


def feed_lane_walk(keep_w: int) -> list[Packet]:
    """One packet per byte lane, each starting a message on that lane.

    Padding with status messages alone cannot do this: status is 14 bytes and
    `gcd(14, keep_w)` is 2 for every power-of-two beat width, so it only ever
    reaches the even lanes. Trades are 25 bytes, and 25 is odd, so `25*a mod
    keep_w` walks all of them. Random traffic gets there eventually at
    DATA_W=64 and left four lanes open at 128 -- this makes it deterministic.
    """
    out = []
    for lane in range(keep_w):
        a = next(a for a in range(keep_w) if (25 * a) % keep_w == lane)
        msgs = [make_trade(qty=i) for i in range(a)]
        msgs += [make_trade(), make_quote(), make_status(), make_imbalance()]
        out.append(build_feed_packet(msgs, seq_num=lane + 1))
    return out


def feed_extreme_packets() -> list[Packet]:
    """One packet per round, each holding one message of every type.

    Random stimulus will not land on 2**63-1 exactly, so the ends of every
    field's range are walked deliberately. Without this the value-class bins
    sit at the two middle bins forever and the coverage number quietly means
    'we sent some numbers'.
    """
    out = []
    for r in range(5):
        s64, s32, s16, u64, u32, u16, u8, sym = _extreme_round(r)
        msgs = [
            make_trade(symbol=sym, price=s64, qty=u32, side=u8, trade_flags=u8),
            make_quote(symbol=sym, bid_px=s32, ask_px=s32, bid_sz=u16, ask_sz=u16),
            make_status(session_state=u8, reason_code=s16, timestamp=u64),
            make_imbalance(
                symbol=sym,
                paired_qty=u64,
                imbalance_qty=s64,
                ref_px=s32,
                auction_type=u8,
            ),
        ]
        out.append(
            build_feed_packet(
                msgs, session_id=u32, seq_num=u64, reserved=u16
            )
        )
    return out
