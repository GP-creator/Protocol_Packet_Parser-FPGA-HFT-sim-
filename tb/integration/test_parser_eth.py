"""parser_top_eth_ipv4_udp against the golden model.

Every packet is checked three ways at once:

  * the record bus, field by field, against wirespec.golden;
  * the payload stream, byte for byte, against packet[payload_offset:];
  * the two structural promises -- s_axis_tready never falls, and every output
    event lands exactly LATENCY cycles after the ingress beat that caused it.

The last one is checked continuously rather than measured once, because a
constant latency that is only constant for the packets you happened to send is
not a property, it is a coincidence.
"""

from __future__ import annotations

import os
import random
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge

from tb.common.axis_driver import AxisSource
from tb.common.axis_monitor import IngressTracker, PayloadSink, RecordMonitor
from tb.common.scoreboard import ETH_RECORD_SIGNALS, EthScoreboard
from tb.common import stimulus as st
from wirespec.golden import decode
from wirespec.ir import load_ir

DATA_W = int(os.environ.get("DATA_W", "64"))
KEEP_W = DATA_W // 8

#: Cycles from the ingress beat carrying an output's last byte to that output.
#:
#: pkt_align (1) + payload_window (1) for a payload beat, and pkt_align (1) +
#: hdr_parse (1) for the record: 2 either way.
#:
#: TAIL_LATENCY is 3, and applies to at most one beat per packet. A payload beat
#: is emitted from the ingress beat that supplied its last byte, but when a
#: packet's payload does not end on a beat boundary the final ingress beat
#: supplies the last bytes of *two* output beats -- one full beat and a short
#: tail. Two beats cannot share one cycle on the bus, so the tail is one cycle
#: behind. That is structural, not elastic: which beat is late, and by how much,
#: is fixed by the packet's geometry alone. See docs/decisions/0003.
LATENCY = 2
TAIL_LATENCY = 3

#: Must match parser_top_eth_ipv4_udp's HDR_BYTES: 14 + 60 + 8.
HDR_BYTES = 82

ROOT = Path(__file__).resolve().parent.parent.parent
IR = load_ir(str(ROOT / "schemas" / "eth_ipv4_udp.yaml"))


async def setup(dut, seed=0):
    cocotb.start_soon(Clock(dut.clk, 2, unit="ns").start())
    dut.rst_n.value = 0
    src = AxisSource(dut, dut.clk, "s_axis", data_w=DATA_W, rng=random.Random(seed))
    await ClockCycles(dut.clk, 5)
    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 2)

    ingress = IngressTracker(dut, dut.clk, "s_axis", data_w=DATA_W)
    payload = PayloadSink(dut, dut.clk, "m_pay", data_w=DATA_W)
    records = RecordMonitor(dut, dut.clk, ETH_RECORD_SIGNALS)
    for mon in (ingress, payload, records):
        cocotb.start_soon(mon.run())

    # start_soon does not run a task until this one yields, so without this edge
    # the driver would present its first beat before the monitors have parked on
    # theirs -- the first beat would go unrecorded and every cycle comparison
    # after it would be off by one. Yielding here puts all four coroutines on the
    # same edge, so the beat written in the next timestep is the one the monitors
    # see in that timestep's ReadOnly phase.
    await RisingEdge(dut.clk)
    return src, ingress, payload, records


def check_no_backpressure(ingress: IngressTracker) -> None:
    assert not ingress.ready_low_cycles, (
        f"s_axis_tready fell on {len(ingress.ready_low_cycles)} cycle(s), first at "
        f"cycle {ingress.ready_low_cycles[0]}; the parser must never backpressure"
    )


def check_record_latency(ingress: IngressTracker, records: RecordMonitor) -> None:
    """Each record lands exactly LATENCY cycles after its packet's parse completes.

    The completing beat is whichever comes first: the one carrying the last byte
    of the 82-byte header window, or the packet's last beat.
    """
    for i, rec in enumerate(records.records):
        beats = ingress.packets[i]
        completing = next(
            (bt for bt in beats if bt.end >= HDR_BYTES),
            beats[-1],
        )
        delta = rec.cycle - completing.cycle
        assert delta == LATENCY, (
            f"packet {i}: record at cycle {rec.cycle}, completing ingress beat at "
            f"{completing.cycle}, latency {delta} != {LATENCY}"
        )


def check_payload_latency(
    ingress: IngressTracker, payload: PayloadSink, pkt_indices: list[int], strips: list[int]
) -> int:
    """Every payload beat sits LATENCY behind the ingress beat that finished it.

    The one permitted exception is a packet's tail beat: when the payload does
    not end on a beat boundary, the packet's final ingress beat supplies the last
    byte of two output beats, and the second of them lands at TAIL_LATENCY. So a
    late beat is legal only if it is the packet's last, and only if it is sourced
    from the packet's last ingress beat. Anything else -- a beat that drifts, a
    late beat in the middle, or two of them in one packet -- means the pipeline
    acquired elasticity somewhere.
    """
    tails = 0
    for pi, strip, pay in zip(pkt_indices, strips, payload.payloads, strict=True):
        last_ingress = ingress.packets[pi][-1]
        late_in_packet = 0
        for k, pb in enumerate(pay.beat_records):
            last_byte = strip + pb.offset + pb.nbytes - 1
            source = ingress.beat_covering(pi, last_byte)
            assert source is not None, (
                f"packet {pi}: payload byte {last_byte} was never on the ingress bus"
            )
            delta = pb.cycle - source.cycle
            if delta == LATENCY:
                continue
            if delta != TAIL_LATENCY:
                raise AssertionError(
                    f"packet {pi}: payload beat at cycle {pb.cycle} is {delta} cycles "
                    f"behind its source ingress beat at {source.cycle}; expected "
                    f"{LATENCY} or {TAIL_LATENCY}"
                )
            assert k == len(pay.beat_records) - 1, (
                f"packet {pi}: beat {k} of {len(pay.beat_records)} is late, but only "
                f"a packet's final beat may be"
            )
            assert source is last_ingress, (
                f"packet {pi}: late beat at cycle {pb.cycle} is sourced from ingress "
                f"cycle {source.cycle}, not the packet's last beat at "
                f"{last_ingress.cycle}"
            )
            late_in_packet += 1
            tails += 1
        assert late_in_packet <= 1, (
            f"packet {pi}: {late_in_packet} late beats; at most the tail may be late"
        )
    return tails


def directed_corpus() -> list[bytes]:
    """A little over a thousand packets covering the shapes that matter."""
    rng = random.Random(0xE740)
    pkts: list[bytes] = []

    # Every IHL against every payload length that straddles a beat boundary
    # differently. This is the cross-product the variable-length header exists
    # to exercise.
    for ihl in range(5, 16):
        for plen in [0, 1, 2, 7, 8, 9, 15, 16, 17, 31, 32, 33, 63, 64, 65, 100, 127, 128]:
            pkts.append(
                st.build_eth_ipv4_udp(
                    ihl=ihl,
                    payload=bytes((i + ihl) & 0xFF for i in range(plen)),
                    src_port=1000 + ihl,
                    dst_port=2000 + plen,
                ).data
            )

    # Sub-byte field patterns: all-ones and alternating, so a slice that is off
    # by a bit cannot look right by symmetry.
    for dscp, ecn, flags, frag in [
        (0, 0, 0, 0),
        (0x3F, 0x3, 0x7, 0x1FFF),
        (0x2A, 0x1, 0x5, 0x1555),
        (0x15, 0x2, 0x2, 0x0AAA),
    ]:
        pkts.append(
            st.build_eth_ipv4_udp(
                dscp=dscp, ecn=ecn, flags=flags, frag_offset=frag, payload=b"\xa5" * 24
            ).data
        )

    # Chains that stop early.
    for et in (st.ETHERTYPE_ARP, 0x8100, 0x86DD, 0x0000, 0xFFFF):
        pkts.append(st.build_eth_ipv4_udp(ethertype=et, payload=b"x" * 20).data)
    for proto in (1, 6, 47, 132, 0, 255):
        for ihl in (5, 7, 15):
            pkts.append(st.build_eth_ipv4_udp(protocol=proto, ihl=ihl, payload=b"y" * 30).data)

    # Boundary field values.
    for kw in (
        {"ttl": 0},
        {"ttl": 255},
        {"identification": 0xFFFF},
        {"udp_checksum": 0xFFFF},
        {"src_port": 0xFFFF, "dst_port": 0xFFFF},
        {"dst_mac": b"\xff" * 6, "src_mac": b"\x00" * 6},
        {"src_ip": b"\xff\xff\xff\xff", "dst_ip": b"\x00\x00\x00\x00"},
    ):
        pkts.append(st.build_eth_ipv4_udp(payload=b"", **kw).data)

    # Fill out to a thousand with randoms.
    while len(pkts) < 1000:
        pkts.append(st.random_eth_ipv4_udp(rng, max_payload=200).data)
    return pkts


async def run_corpus(dut, packets, *, gap_prob=0.0, inter_packet_gap=0, seed=0):
    src, ingress, payload, records = await setup(dut, seed=seed)
    await src.send_many(packets, gap_prob=gap_prob, inter_packet_gap=inter_packet_gap)
    await ClockCycles(dut.clk, 30)

    sb = EthScoreboard(IR)
    assert len(records.records) == len(packets), (
        f"{len(records.records)} records for {len(packets)} packets"
    )
    assert not payload.errors, "\n".join(payload.errors[:10])

    decs = [sb.check_record(i, p, r) for i, (p, r) in enumerate(zip(packets, records.records))]

    # Well-formed packets each terminate the payload stream exactly once.
    clean = [(i, p, d) for i, (p, d) in enumerate(zip(packets, decs)) if d.ok]
    assert len(payload.payloads) == len(clean), (
        f"{len(payload.payloads)} payloads for {len(clean)} cleanly-parsed packets"
    )
    for (i, p, d), got in zip(clean, payload.payloads, strict=True):
        sb.check_payload(i, p, got.data, d)

    sb.assert_clean()
    check_no_backpressure(ingress)
    check_record_latency(ingress, records)
    tails = check_payload_latency(
        ingress, payload, [i for i, _, _ in clean], [d.payload_offset for _, _, d in clean]
    )
    return sb, ingress, payload, records, tails


@cocotb.test(timeout_time=20, timeout_unit="ms")
async def test_directed_corpus(dut):
    """The M2 acceptance run: 1,000 directed packets, back to back, no idle."""
    packets = directed_corpus()
    sb, _, payload, _, tails = await run_corpus(dut, packets, seed=1)
    total = sum(len(p) for p in packets)
    beats = sum(len(p.beat_records) for p in payload.payloads)
    dut._log.info(
        f"DATA_W={DATA_W}: {len(packets)} packets, {total} bytes, "
        f"{sb.fields_checked} field comparisons, {beats} payload beats "
        f"({tails} tail beats at +{TAIL_LATENCY}), 0 mismatches"
    )
    assert sb.checked == len(packets)
    assert sb.fields_checked > 15000, "the corpus stopped exercising most fields"
    assert tails > 0, "the corpus never produced an unaligned payload tail"


@cocotb.test(timeout_time=20, timeout_unit="ms")
async def test_with_tvalid_gaps(dut):
    """Idle cycles anywhere must change nothing but when things happen."""
    rng = random.Random(0x9A95)
    packets = [st.random_eth_ipv4_udp(rng, max_payload=120).data for _ in range(200)]
    await run_corpus(dut, packets, gap_prob=0.35, inter_packet_gap=3, seed=2)


@cocotb.test(timeout_time=20, timeout_unit="ms")
async def test_minimum_size_packets_back_to_back(dut):
    """42-byte frames with no payload, with zero idle between them.

    The tightest packing the protocol allows, and the case where one packet's
    state is most likely to leak into the next.
    """
    packets = [
        st.build_eth_ipv4_udp(payload=b"", identification=i & 0xFFFF).data for i in range(200)
    ]
    _, _, payload, _, _ = await run_corpus(dut, packets, seed=3)
    assert all(p.data == b"" for p in payload.payloads)
    assert len(payload.payloads) == len(packets)


@cocotb.test(timeout_time=10, timeout_unit="ms")
async def test_latency_measured_in_isolation(dut):
    """One packet on an otherwise idle bus, so every delta is unambiguous."""
    src, ingress, payload, records = await setup(dut, seed=4)
    # 64-byte payload after a 42-byte header: the payload ends mid-beat, so this
    # exercises both the regular path and the tail.
    pkt = st.build_eth_ipv4_udp(payload=b"Z" * 64).data
    await src.send_many([pkt])
    await ClockCycles(dut.clk, 30)

    assert len(payload.payloads) == 1
    strip = decode(IR, pkt).payload_offset
    tails = check_payload_latency(ingress, payload, [0], [strip])
    check_record_latency(ingress, records)
    check_no_backpressure(ingress)

    deltas = []
    for pb in payload.payloads[0].beat_records:
        src_beat = ingress.beat_covering(0, strip + pb.offset + pb.nbytes - 1)
        deltas.append(pb.cycle - src_beat.cycle)
    dut._log.info(
        f"DATA_W={DATA_W}: payload beat latencies {deltas} cycles "
        f"({tails} tail beat), record latency {LATENCY} cycles"
    )
    assert set(deltas) <= {LATENCY, TAIL_LATENCY}


@cocotb.test(timeout_time=10, timeout_unit="ms")
async def test_malformed_packets(dut):
    """Truncated frames and bad IHL: the RTL must reach the model's verdict.

    Only the record is compared -- a packet that does not parse has no defined
    payload, and claiming one would be inventing a requirement.
    """
    good = st.build_eth_ipv4_udp(payload=b"P" * 32).data
    packets: list[bytes] = []

    # Truncated at every interesting boundary. 42 is excluded on purpose: a
    # 42-byte frame is a complete header chain with an empty payload, so it
    # parses cleanly and belongs in the directed corpus, not here.
    for n in (1, 8, 13, 14, 15, 20, 33, 34, 35, 41):
        packets.append(good[:n])

    # IHL below the 20-byte minimum, and IHL claiming more than is present.
    for ihl in (0, 1, 4):
        bad = bytearray(good)
        bad[14] = (4 << 4) | ihl
        packets.append(bytes(bad))
    short = bytearray(good[:42])
    short[14] = (4 << 4) | 15  # 60 bytes of IPv4 header in a 42-byte frame
    packets.append(bytes(short))

    src, ingress, payload, records = await setup(dut, seed=5)
    await src.send_many(packets, inter_packet_gap=2)
    await ClockCycles(dut.clk, 30)

    assert len(records.records) == len(packets)
    sb = EthScoreboard(IR)
    for i, (p, r) in enumerate(zip(packets, records.records, strict=True)):
        sb.check_record(i, p, r)
    sb.assert_clean()
    check_no_backpressure(ingress)
    check_record_latency(ingress, records)

    # A packet whose header ran off the end still has a well-defined answer for
    # the payload: there is none. Depending on how far it got, the parser says so
    # with o_empty (the header length was known, the packet was shorter) or
    # o_strip_err (the packet ended before the length could be worked out). What
    # must never happen is payload bytes coming out of a packet that did not
    # parse.
    assert all(p.data == b"" for p in payload.payloads), (
        "a malformed packet produced payload bytes"
    )
    assert len(payload.payloads) + len(payload.strip_err_cycles) == len(packets), (
        f"{len(payload.payloads)} empty markers + {len(payload.strip_err_cycles)} strip "
        f"errors != {len(packets)} packets: some packet reported nothing at all"
    )

    # Every one of these should have been rejected by the model too.
    assert all(not decode(IR, p).ok for p in packets), "a malformed fixture parsed cleanly"
