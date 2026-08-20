"""parser_top_simple_feed against the golden model.

The M4 acceptance case: messages that straddle beat boundaries at every
alignment, several messages beginning in one beat, and packets running back to
back with no idle cycle between them.
"""

from __future__ import annotations

import os
import random
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, ReadOnly, RisingEdge

from tb.common import stimulus as st
from tb.common.axis_driver import AxisSource
from tb.common.axis_monitor import IngressTracker, b, u
from tb.common.scoreboard import FeedScoreboard
from wirespec.golden import StreamModel, decode
from wirespec.ir import load_ir
from wirespec.layout import build_layout

DATA_W = int(os.environ.get("DATA_W", "64"))
KEEP_W = DATA_W // 8

ROOT = Path(__file__).resolve().parent.parent.parent
IR = load_ir(str(ROOT / "schemas" / "simple_feed.yaml"))
LAYOUT = build_layout(IR, DATA_W)
SLOTS = LAYOUT.slots
REC_W = LAYOUT.msg_rec_bits


class FeedSink:
    """Collect message records and packet verdicts, grouped per packet."""

    def __init__(self, dut, clk):
        self.dut = dut
        self.clk = clk
        self.pending: list[int] = []
        self.packets: list[tuple[tuple[int, int], list[int]]] = []
        self.slot_hist: dict[int, int] = {}
        self.cycle = 0
        self.msg_cycles: list[int] = []

    async def run(self):
        dut = self.dut
        while True:
            await RisingEdge(self.clk)
            await ReadOnly()
            self.cycle += 1

            valid = u(dut.o_msg_valid)
            data = u(dut.o_msg_data)
            n = bin(valid).count("1")
            if n:
                self.slot_hist[n] = self.slot_hist.get(n, 0) + 1
                self.msg_cycles.append(self.cycle)
            for s in range(SLOTS):
                if valid & (1 << s):
                    self.pending.append((data >> (s * REC_W)) & ((1 << REC_W) - 1))

            if b(dut.o_pkt_done):
                self.packets.append(
                    ((u(dut.o_pkt_err), u(dut.o_pkt_msgs)), self.pending)
                )
                self.pending = []


async def setup(dut, seed=0):
    cocotb.start_soon(Clock(dut.clk, 2, unit="ns").start())
    dut.rst_n.value = 0
    src = AxisSource(dut, dut.clk, "s_axis", data_w=DATA_W, rng=random.Random(seed))
    await ClockCycles(dut.clk, 5)
    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 2)

    ingress = IngressTracker(dut, dut.clk, "s_axis", data_w=DATA_W)
    sink = FeedSink(dut, dut.clk)
    for mon in (ingress, sink):
        cocotb.start_soon(mon.run())
    await RisingEdge(dut.clk)
    return src, ingress, sink


async def run_packets(dut, packets, *, seed=0, gap_prob=0.0, inter_packet_gap=0):
    src, ingress, sink = await setup(dut, seed=seed)
    await src.send_many(packets, gap_prob=gap_prob, inter_packet_gap=inter_packet_gap)
    await ClockCycles(dut.clk, 60)

    assert not ingress.ready_low_cycles, (
        f"s_axis_tready fell on {len(ingress.ready_low_cycles)} cycle(s)"
    )
    assert not b(dut.o_overflow), "msg_stitch reported a buffer overflow"
    assert len(sink.packets) == len(packets), (
        f"{len(sink.packets)} packet verdicts for {len(packets)} packets"
    )

    sb = FeedScoreboard(IR, LAYOUT)
    for i, (pkt, (verdict, msgs)) in enumerate(zip(packets, sink.packets, strict=True)):
        sb.check_packet(i, pkt, verdict, msgs)
    sb.assert_clean()
    return sb, sink


# --------------------------------------------------------------------------
# Directed
# --------------------------------------------------------------------------


@cocotb.test(timeout_time=10, timeout_unit="ms")
async def test_one_of_each_type(dut):
    pkt = st.build_feed_packet(
        [st.make_trade(), st.make_quote(), st.make_status(), st.make_imbalance()]
    )
    sb, _ = await run_packets(dut, [pkt.data], seed=1)
    assert sb.messages == 4


@cocotb.test(timeout_time=20, timeout_unit="ms")
async def test_every_straddle_position(dut):
    """Walk the message boundary through every byte lane.

    Prepending 0..KEEP_W-1 extra status messages shifts every later message by a
    different amount modulo the beat width, so each message type gets extracted
    at every possible alignment.
    """
    packets = []
    for pad in range(KEEP_W + 1):
        msgs = [st.make_status(timestamp=i) for i in range(pad)]
        msgs += [
            st.make_trade(price=-(10**15), qty=0xDEADBEEF),
            st.make_quote(bid_px=-1, ask_px=2**31 - 1),
            st.make_imbalance(imbalance_qty=-(2**63)),
            st.make_status(reason_code=-32768),
            st.make_trade(symbol=b"BRK.B   "),
        ]
        packets.append(st.build_feed_packet(msgs, seq_num=pad + 1).data)

    sb, sink = await run_packets(dut, packets, seed=2)
    dut._log.info(
        f"DATA_W={DATA_W}: {sb.packets} packets, {sb.messages} messages, "
        f"{sb.fields_checked} field comparisons, slots/cycle={sink.slot_hist}"
    )


@cocotb.test(timeout_time=20, timeout_unit="ms")
async def test_multiple_messages_per_beat(dut):
    """Long runs of the smallest message, so several finish in one cycle."""
    packets = [
        st.build_feed_packet(
            [st.make_status(timestamp=i) for i in range(n)], seq_num=n
        ).data
        for n in (1, 2, 3, 5, 8, 13, 21, 40)
    ]
    sb, sink = await run_packets(dut, packets, seed=3)
    assert sb.messages == 1 + 2 + 3 + 5 + 8 + 13 + 21 + 40
    if SLOTS > 1:
        assert max(sink.slot_hist) > 1, (
            f"SLOTS={SLOTS} but never more than one message per cycle: {sink.slot_hist}"
        )
    dut._log.info(f"DATA_W={DATA_W}: SLOTS={SLOTS}, messages per cycle {sink.slot_hist}")


@cocotb.test(timeout_time=20, timeout_unit="ms")
async def test_back_to_back_packets_no_idle(dut):
    rng = random.Random(0xB2B)
    packets = [
        st.random_feed_packet(rng, seq_num=i + 1, min_msgs=0, max_msgs=6).data
        for i in range(120)
    ]
    await run_packets(dut, packets, seed=4, inter_packet_gap=0)


@cocotb.test(timeout_time=20, timeout_unit="ms")
async def test_tvalid_gaps(dut):
    rng = random.Random(0x9A95)
    packets = [
        st.random_feed_packet(rng, seq_num=i + 1, min_msgs=0, max_msgs=6).data
        for i in range(80)
    ]
    await run_packets(dut, packets, seed=5, gap_prob=0.35, inter_packet_gap=3)


@cocotb.test(timeout_time=30, timeout_unit="ms")
async def test_random_feed(dut):
    rng = random.Random(0x5EED)
    packets = [
        st.random_feed_packet(rng, seq_num=i + 1, min_msgs=0, max_msgs=10).data
        for i in range(300)
    ]
    sb, sink = await run_packets(dut, packets, seed=6)
    total = sum(len(p) for p in packets)
    dut._log.info(
        f"DATA_W={DATA_W}: {sb.packets} packets, {total} bytes, {sb.messages} messages, "
        f"{sb.fields_checked} field comparisons, 0 mismatches"
    )
    assert sb.messages > 1000


# --------------------------------------------------------------------------
# Malformed
# --------------------------------------------------------------------------


@cocotb.test(timeout_time=20, timeout_unit="ms")
async def test_malformed_packets(dut):
    """Every defect class, each between two well-formed packets.

    Surrounding them matters: a parser that gets the verdict right but leaves
    state behind corrupts the packet after, not the one that was wrong.
    """
    good = st.build_feed_packet([st.make_trade(), st.make_quote()], seq_num=1).data
    cases = [
        st.feed_truncated_header(9).data,
        st.feed_truncated_header(15).data,
        st.feed_truncated_message(cut=5).data,
        st.feed_truncated_message(cut=1).data,
        st.feed_bad_length(1).data,
        st.feed_bad_length(-1).data,
        st.feed_bad_length(3).data,
        st.feed_bad_length(100).data,  # declares more than the buffer can hold
        st.feed_zero_length().data,
        st.feed_unknown_type(0x7F).data,
        st.feed_unknown_type(0x00).data,
        st.feed_count_mismatch().data,
        st.build_feed_packet([st.make_trade()]).data + b"\x00",  # stray tail byte
    ]

    packets: list[bytes] = []
    for c in cases:
        packets += [good, c, good]

    sb, _ = await run_packets(dut, packets, seed=7, inter_packet_gap=2)
    bad = [p for p in packets if not decode(IR, p).ok]
    assert len(bad) == len(cases), "a malformed fixture parsed cleanly"
    dut._log.info(f"DATA_W={DATA_W}: {len(cases)} defect classes, all matched the model")


# --------------------------------------------------------------------------
# Counters
# --------------------------------------------------------------------------


@cocotb.test(timeout_time=20, timeout_unit="ms")
async def test_stats_match_the_stream_model(dut):
    """stats.sv against wirespec.golden.StreamModel, including sequence gaps."""
    rng = random.Random(0x57A75)
    seqs = [1, 2, 3, 5, 6, 10, 11, 12]
    packets = [
        st.random_feed_packet(rng, seq_num=s, min_msgs=1, max_msgs=4).data for s in seqs
    ]
    # Two malformed packets in the middle: counted, but must not advance the
    # expected sequence number and so must not manufacture extra gaps.
    packets.insert(3, st.feed_zero_length().data)
    packets.insert(7, st.feed_unknown_type().data)

    await run_packets(dut, packets, seed=8, inter_packet_gap=2)
    await ClockCycles(dut.clk, 10)

    model = StreamModel(IR)
    for p in packets:
        model.feed(p)
    want = model.stats.as_dict()

    got = {
        "packets": u(dut.o_stat_packets),
        "messages": u(dut.o_stat_messages),
        "seq_gaps": u(dut.o_stat_seq_gaps),
        "malformed": u(dut.o_stat_malformed),
    }
    assert got == want, f"stats {got} != model {want}"
    dut._log.info(f"DATA_W={DATA_W}: stats {got}")
