"""Compare the parser's record bus against the golden model.

The scoreboard never re-derives a field value; it asks wirespec.golden for the
answer and demands the RTL agree. Fields belonging to a layer the model did not
reach are not compared -- the RTL fills them with whatever the window happened
to hold, and o_layer_valid is what says so.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from wirespec.golden import Decoded, Defect, decode
from wirespec.ir import IR

# ws_err_e in rtl/common/pkg_wirespec.sv. Kept in one place so a renumbering
# breaks loudly here rather than silently passing a comparison.
WS_OK = 0
WS_KEEP_ERR = 1
WS_TRUNCATED = 2
WS_BAD_HDR_LEN = 3
WS_BAD_LENGTH = 4
WS_ZERO_LENGTH = 5
WS_UNKNOWN_TYPE = 6
WS_COUNT_MISMATCH = 7

DEFECT_TO_WS = {
    Defect.TRUNCATED_HEADER: WS_TRUNCATED,
    Defect.TRUNCATED_MESSAGE: WS_TRUNCATED,
    Defect.BAD_HEADER_LENGTH: WS_BAD_HDR_LEN,
    Defect.BAD_LENGTH: WS_BAD_LENGTH,
    Defect.ZERO_LENGTH: WS_ZERO_LENGTH,
    Defect.UNKNOWN_TYPE: WS_UNKNOWN_TYPE,
    Defect.COUNT_MISMATCH: WS_COUNT_MISMATCH,
}

#: RTL record signal -> (layer, golden field name). Layer None means the signal
#: is derived rather than extracted.
ETH_FIELD_MAP = {
    "o_eth_dst_mac": ("eth", "dst_mac"),
    "o_eth_src_mac": ("eth", "src_mac"),
    "o_eth_ethertype": ("eth", "ethertype"),
    "o_ipv4_version": ("ipv4", "version"),
    "o_ipv4_ihl": ("ipv4", "ihl"),
    "o_ipv4_dscp": ("ipv4", "dscp"),
    "o_ipv4_ecn": ("ipv4", "ecn"),
    "o_ipv4_total_length": ("ipv4", "total_length"),
    "o_ipv4_identification": ("ipv4", "identification"),
    "o_ipv4_flags": ("ipv4", "flags"),
    "o_ipv4_frag_offset": ("ipv4", "frag_offset"),
    "o_ipv4_ttl": ("ipv4", "ttl"),
    "o_ipv4_protocol": ("ipv4", "protocol"),
    "o_ipv4_hdr_checksum": ("ipv4", "hdr_checksum"),
    "o_ipv4_src_ip": ("ipv4", "src_ip"),
    "o_ipv4_dst_ip": ("ipv4", "dst_ip"),
    "o_udp_src_port": ("udp", "src_port"),
    "o_udp_dst_port": ("udp", "dst_port"),
    "o_udp_length": ("udp", "length"),
    "o_udp_checksum": ("udp", "checksum"),
}

ETH_LAYER_BIT = {"eth": 0, "ipv4": 1, "udp": 2}

#: Every signal the record monitor must sample for the eth_ipv4_udp parser.
ETH_RECORD_SIGNALS = ["o_layer_valid", "o_err", "o_hdr_bytes", *ETH_FIELD_MAP]


def as_int(value) -> int:
    """Golden returns bytes for char/bytes fields; the RTL carries them as a
    big-endian vector with the first wire byte most significant."""
    if isinstance(value, bytes):
        return int.from_bytes(value, "big")
    return value


def golden_err(dec: Decoded) -> int:
    if dec.ok:
        return WS_OK
    return DEFECT_TO_WS[dec.errors[0].defect]


@dataclass
class Mismatch:
    index: int
    what: str
    got: int
    want: int
    packet: bytes

    def __str__(self) -> str:
        return (
            f"packet #{self.index} ({len(self.packet)} B): {self.what} "
            f"got 0x{self.got:x}, want 0x{self.want:x}\n  {self.packet.hex()}"
        )


@dataclass
class EthScoreboard:
    """Check eth_ipv4_udp records and payloads against wirespec.golden."""

    ir: IR
    mismatches: list[Mismatch] = field(default_factory=list)
    checked: int = 0
    fields_checked: int = 0

    def _fail(self, index: int, what: str, got: int, want: int, pkt: bytes) -> None:
        self.mismatches.append(Mismatch(index, what, got, want, pkt))

    def check_record(self, index: int, pkt: bytes, rec) -> Decoded:
        dec = decode(self.ir, pkt)
        self.checked += 1

        want_err = golden_err(dec)
        if rec["o_err"] != want_err:
            self._fail(index, "o_err", rec["o_err"], want_err, pkt)

        want_layers = 0
        for r in dec.records:
            want_layers |= 1 << ETH_LAYER_BIT[r.name]
        if rec["o_layer_valid"] != want_layers:
            self._fail(index, "o_layer_valid", rec["o_layer_valid"], want_layers, pkt)

        # A truncated packet has no meaningful header length: the model stops
        # where it ran out of bytes, the RTL reports where the chain would have
        # ended. Only compare it when the packet parsed cleanly.
        if dec.ok and rec["o_hdr_bytes"] != dec.payload_offset:
            self._fail(index, "o_hdr_bytes", rec["o_hdr_bytes"], dec.payload_offset, pkt)

        present = {r.name: r for r in dec.records}
        for sig, (layer, fname) in ETH_FIELD_MAP.items():
            if layer not in present:
                continue
            want = as_int(present[layer].values[fname])
            self.fields_checked += 1
            if rec[sig] != want:
                self._fail(index, f"{sig} ({layer}.{fname})", rec[sig], want, pkt)
        return dec

    def check_payload(self, index: int, pkt: bytes, got: bytes, dec: Decoded) -> None:
        want = pkt[dec.payload_offset :] if dec.ok else b""
        if got != want:
            self.mismatches.append(
                Mismatch(index, f"payload ({len(got)} B vs {len(want)} B)", 0, 0, pkt)
            )
            self.mismatches[-1].what += f"\n  got  {got.hex()}\n  want {want.hex()}"

    def report(self, limit: int = 5) -> str:
        head = "\n".join(str(m) for m in self.mismatches[:limit])
        extra = len(self.mismatches) - limit
        if extra > 0:
            head += f"\n... and {extra} more"
        return head

    def assert_clean(self) -> None:
        assert not self.mismatches, (
            f"{len(self.mismatches)} mismatch(es) over {self.checked} packets:\n"
            + self.report()
        )


# --------------------------------------------------------------------------
# simple_feed
# --------------------------------------------------------------------------


@dataclass
class FeedScoreboard:
    """Check simple_feed message records and packet verdicts against golden.

    The message record bus is one packed vector per slot; the layout tells us
    where each field sits. That unpacking is shared with the emitter on purpose
    -- it is a transport detail, not a claim about the protocol. The *values* it
    yields are still compared against a decoder that never saw the layout.
    """

    ir: IR
    layout: object  # wirespec.layout.Layout
    mismatches: list[str] = field(default_factory=list)
    packets: int = 0
    messages: int = 0
    fields_checked: int = 0

    def unpack(self, rec: int) -> dict[str, int]:
        out: dict[str, int] = {}
        for f in self.layout.msg_fields:  # type: ignore[attr-defined]
            raw = (rec >> f.rec_lsb) & ((1 << f.width) - 1)
            if f.signed and raw >= (1 << (f.width - 1)):
                raw -= 1 << f.width
            out[f.name] = raw
        return out

    def check_packet(self, index: int, pkt: bytes, verdict, msgs: list[int]) -> Decoded:
        """``verdict`` is (err, msg_count); ``msgs`` the raw record vectors."""
        dec = decode(self.ir, pkt)
        self.packets += 1
        err, count = verdict

        want_err = golden_err(dec)
        if err != want_err:
            self.mismatches.append(
                f"packet #{index}: o_pkt_err {err}, model says {want_err} "
                f"({dec.errors[0] if dec.errors else 'ok'})\n  {pkt.hex()}"
            )

        want = dec.messages
        if count != len(want):
            self.mismatches.append(
                f"packet #{index}: o_pkt_msgs {count}, model decoded {len(want)}"
            )
        if len(msgs) != len(want):
            self.mismatches.append(
                f"packet #{index}: {len(msgs)} message records for {len(want)} decoded"
            )
            return dec

        for k, (raw, w) in enumerate(zip(msgs, want, strict=True)):
            got = self.unpack(raw)
            self.messages += 1
            for name, value in w.values.items():
                self.fields_checked += 1
                if got.get(name) != as_int(value):
                    self.mismatches.append(
                        f"packet #{index} msg {k} ({w.name}): {name} "
                        f"got {got.get(name)}, want {as_int(value)}"
                    )
        return dec

    def assert_clean(self) -> None:
        assert not self.mismatches, (
            f"{len(self.mismatches)} mismatch(es) over {self.packets} packets, "
            f"{self.messages} messages:\n" + "\n".join(self.mismatches[:8])
        )
