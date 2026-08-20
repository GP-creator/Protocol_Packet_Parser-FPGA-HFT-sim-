"""parser_top_simple_feed against the golden model.

M4 established correctness: messages straddling beat boundaries at every
alignment, several messages beginning in one beat, packets running back to back
with no idle cycle.

M5 adds the depth. Every test here now feeds the coverage model, every message
and every packet header has its latency measured against the ingress beat that
completed it, and `test_stress` drives more than a million randomised bytes
through the DUT in one run.
"""

from __future__ import annotations

import os
import random
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, ReadOnly, RisingEdge

from tb.common import metrics as met
from tb.common import stimulus as st
from tb.common.axis_driver import AxisSource
from tb.common.axis_monitor import IngressTracker, b, u
from tb.common.coverage import FeedCoverage
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

#: How many random bytes `test_stress` drives. The milestone number is the
#: default; lower it to keep an edit-run loop short.
STRESS_BYTES = int(os.environ.get("WIRESPEC_STRESS_BYTES", "1100000"))

#: Coverage accumulated across every test in this module, so the closure gate
#: sees the whole run rather than one test's slice.
COVERAGE = FeedCoverage(IR, DATA_W, SLOTS)

#: Latency samples, keyed by which output bus they came from.
#:
#: The reference point matters more than the number. A record cannot exist
#: before the ingress beat carrying its last byte -- but `payload_window` is a
#: gearbox with a registered input stage, so that beat is only pushed through
#: when the *following* beat arrives (or, at end of packet, when `tlast`
#: triggers the drain one cycle later). Measured against that release beat the
#: latency is one number with no exceptions. Measured against the completing
#: beat it looks variable, and the variation is exactly the source's own idle
#: cycles being reported back as if they were the parser's.
#:
#: The verdict is split into two populations because there are two ways a
#: packet can end. A packet long enough to carry a header is finished by the
#: framer, one cycle behind the payload path. A runt shorter than the header
#: never reaches the framer at all and is finished by `payload_window`'s strip
#: error instead -- a cycle earlier, structurally. Tail alignment, which does
#: split the M2 payload stream (ADR 0003), does *not* split this: measured
#: across both populations the verdict sits at one number each way regardless
#: of whether the packet ended mid-beat.
LATENCY: dict[str, dict[int, int]] = {
    "header": {},
    "message": {},
    "verdict": {},
    "verdict_runt": {},
}

RUN: dict[str, int] = {"packets": 0, "bytes": 0, "messages": 0, "fields": 0, "cycles": 0}


#: One worked example per distinct latency value. A histogram says latency
#: varies; this says which packet and which message, which is the difference
#: between a number to report and a bug to find.
EXAMPLES: dict[str, dict[int, str]] = {k: {} for k in LATENCY}


def note_latency(kind: str, cycles: int, why: str = "") -> None:
    LATENCY[kind][cycles] = LATENCY[kind].get(cycles, 0) + 1
    if why and cycles not in EXAMPLES[kind]:
        EXAMPLES[kind][cycles] = why


# --------------------------------------------------------------------------
# Sink
# --------------------------------------------------------------------------


class FeedSink:
    """Collect message records and packet verdicts, grouped per packet.

    Each message keeps the cycle it appeared on and the slot it came out of:
    the cycle feeds the latency check, the slot is the one fact the golden
    model cannot supply and so is the only thing the coverage model takes from
    the DUT.
    """

    def __init__(self, dut, clk):
        self.dut = dut
        self.clk = clk
        self.pending: list[tuple[int, int, int]] = []  # (cycle, slot, record)
        self.packets: list[tuple[tuple[int, int], list[tuple[int, int, int]]]] = []
        self.hdr_cycles: list[int] = []
        self.done_cycles: list[int] = []
        self.slot_hist: dict[int, int] = {}
        self.cycle = 0

    async def run(self):
        dut = self.dut
        while True:
            await RisingEdge(self.clk)
            await ReadOnly()
            self.cycle += 1

            if b(dut.o_rec_valid):
                self.hdr_cycles.append(self.cycle)

            valid = u(dut.o_msg_valid)
            data = u(dut.o_msg_data)
            n = bin(valid).count("1")
            if n:
                self.slot_hist[n] = self.slot_hist.get(n, 0) + 1
            for s in range(SLOTS):
                if valid & (1 << s):
                    rec = (data >> (s * REC_W)) & ((1 << REC_W) - 1)
                    self.pending.append((self.cycle, s, rec))

            if b(dut.o_pkt_done):
                self.done_cycles.append(self.cycle)
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
    """Drive `packets`, check every record against the model, bin the coverage.

    Everything M5 measures happens here rather than in the individual tests, so
    a test cannot contribute stimulus without also contributing its latency
    samples and its coverage -- which is the failure mode that makes a coverage
    number drift away from the traffic it claims to describe.
    """
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
    assert len(sink.hdr_cycles) == len(packets), (
        f"{len(sink.hdr_cycles)} header records for {len(packets)} packets"
    )

    sb = FeedScoreboard(IR, LAYOUT)
    for i, (pkt, (verdict, msgs)) in enumerate(zip(packets, sink.packets, strict=True)):
        dec = sb.check_packet(i, pkt, verdict, [rec for _, _, rec in msgs])
        COVERAGE.sample_packet(pkt, dec, [slot for _, slot, _ in msgs])
        _check_latency(i, pkt, dec, ingress, sink, msgs)
    sb.assert_clean()

    COVERAGE.sample_ingress(ingress)
    RUN["packets"] += len(packets)
    RUN["bytes"] += sum(len(p) for p in packets)
    RUN["messages"] += sb.messages
    RUN["fields"] += sb.fields_checked
    RUN["cycles"] += sink.cycle
    return sb, sink


def _check_latency(index, pkt, dec, ingress, sink, msgs) -> None:
    """Every output must appear a fixed number of cycles after the ingress beat
    that delivered its last input byte.

    Not "after the packet started": a message's fields cannot exist before the
    beat carrying its final byte, so start-of-packet is the wrong reference and
    would make latency look variable when it is not. Referenced this way the
    number is a property of the pipeline, and it is asserted for every single
    record rather than sampled.
    """
    beats = ingress.packets[index]
    eof = beats[-1]

    def release(byte_index: int) -> int:
        """The cycle the beat carrying `byte_index` is pushed out of the input
        register: the next ingress beat, or `tlast`'s drain one cycle later."""
        beat = ingress.beat_covering(index, byte_index)
        assert beat is not None, f"packet #{index}: no ingress beat for byte {byte_index}"
        i = beats.index(beat)
        return beats[i + 1].cycle if i + 1 < len(beats) else beat.cycle + 1

    # The header record comes out of hdr_accum, not through the payload
    # gearbox, so it is referenced to the beat that completes it directly.
    hdr_end = IR.max_header_bytes - 1  # 15 for simple_feed
    if len(pkt) > hdr_end:
        beat = ingress.beat_covering(index, hdr_end)
        note_latency(
            "header",
            sink.hdr_cycles[index] - beat.cycle,
            f"pkt#{index} len={len(pkt)}",
        )

    for k, ((cycle, _slot, _rec), m) in enumerate(zip(msgs, dec.messages, strict=True)):
        end = m.offset + m.length - 1
        note_latency(
            "message",
            cycle - release(end),
            f"pkt#{index} len={len(pkt)} msg{k}/{len(dec.messages)} {m.name} "
            f"at {m.offset}+{m.length}, ends in beat "
            f"{beats.index(ingress.beat_covering(index, end))}/{len(beats)}",
        )

    kind = "verdict" if len(pkt) >= IR.max_header_bytes else "verdict_runt"
    note_latency(
        kind,
        sink.done_cycles[index] - eof.cycle,
        f"pkt#{index} len={len(pkt)} (len%{KEEP_W}={len(pkt) % KEEP_W}) "
        f"msgs={len(dec.messages)} ok={dec.ok}",
    )


def assert_constant_latency() -> None:
    """The end of every test: one value per bus, no exceptions, ever."""
    for kind, hist in LATENCY.items():
        if not hist:
            continue
        assert len(hist) == 1, (
            f"{kind} latency is not constant: {dict(sorted(hist.items()))} "
            f"(cycles behind the reference beat)\n"
            + "\n".join(f"    +{c}: {w}" for c, w in sorted(EXAMPLES[kind].items()))
        )

    # The two ways a packet can finish must land on the same cycle, or a runt
    # arriving with no idle gap after a full packet overwrites its predecessor's
    # verdict and one of the two disappears. That is B010, and this is what
    # would have caught it a milestone earlier.
    full, runt = LATENCY["verdict"], LATENCY["verdict_runt"]
    if full and runt:
        assert set(full) == set(runt), (
            f"a runt packet's verdict is on a different schedule from a full "
            f"one's: full={dict(full)}, runt={dict(runt)}"
        )


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
    assert_constant_latency()


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
    assert_constant_latency()


@cocotb.test(timeout_time=20, timeout_unit="ms")
async def test_lane_walk(dut):
    """A message starting on every byte lane, by construction rather than luck.

    Status messages alone only reach the even lanes (14 and any power-of-two
    beat width share a factor of 2); trades are 25 bytes and 25 is odd, so
    padding with trades walks all of them.
    """
    packets = [p.data for p in st.feed_lane_walk(KEEP_W)]
    await run_packets(dut, packets, seed=9)
    lanes = COVERAGE.crosses["type_x_lane"]
    assert lanes.closed, f"lanes still open after the walk: {lanes.holes()}"
    assert_constant_latency()


@cocotb.test(timeout_time=20, timeout_unit="ms")
async def test_field_extremes(dut):
    """Every field at both ends of its range, and at zero.

    Random 64-bit values do not land on 2**63-1, so the sign bit, the all-ones
    pattern and the wrap boundary are only ever reached deliberately. This is
    the test that closes `field_value_class`.
    """
    packets = [p.data for p in st.feed_extreme_packets()]
    sb, _ = await run_packets(dut, packets, seed=10)
    assert sb.messages == 20
    vals = COVERAGE.crosses["field_value_class"]
    assert vals.closed, f"value classes still open: {vals.holes()}"
    assert_constant_latency()


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
    assert_constant_latency()


@cocotb.test(timeout_time=20, timeout_unit="ms")
async def test_back_to_back_packets_no_idle(dut):
    rng = random.Random(0xB2B)
    packets = [
        st.random_feed_packet(rng, seq_num=i + 1, min_msgs=0, max_msgs=6).data
        for i in range(120)
    ]
    await run_packets(dut, packets, seed=4, inter_packet_gap=0)
    assert_constant_latency()


@cocotb.test(timeout_time=20, timeout_unit="ms")
async def test_tvalid_gaps(dut):
    rng = random.Random(0x9A95)
    packets = [
        st.random_feed_packet(rng, seq_num=i + 1, min_msgs=0, max_msgs=6).data
        for i in range(80)
    ]
    await run_packets(dut, packets, seed=5, gap_prob=0.35, inter_packet_gap=3)
    shape = COVERAGE.crosses["ingress_shape"]
    assert shape.closed, f"ingress shapes still open: {shape.holes()}"
    assert_constant_latency()


@cocotb.test(timeout_time=30, timeout_unit="ms")
async def test_random_feed(dut):
    rng = random.Random(0x5EED)
    packets = [
        st.random_feed_packet(rng, seq_num=i + 1, min_msgs=0, max_msgs=10).data
        for i in range(300)
    ]
    sb, _ = await run_packets(dut, packets, seed=6)
    total = sum(len(p) for p in packets)
    dut._log.info(
        f"DATA_W={DATA_W}: {sb.packets} packets, {total} bytes, {sb.messages} messages, "
        f"{sb.fields_checked} field comparisons, 0 mismatches"
    )
    assert sb.messages > 1000
    assert_constant_latency()


# --------------------------------------------------------------------------
# Malformed
# --------------------------------------------------------------------------


@cocotb.test(timeout_time=40, timeout_unit="ms")
async def test_malformed_packets(dut):
    """Every malformed class, each between two well-formed packets.

    Surrounding them matters: a parser that gets the verdict right but leaves
    state behind corrupts the packet after, not the one that was wrong. The
    corpus lives in `tb.common.stimulus` so the count reported in metrics.json
    is the length of the list rather than a number kept in step by hand.
    """
    corpus = st.malformed_feed_corpus()
    good = st.build_feed_packet([st.make_trade(), st.make_quote()], seq_num=1).data

    packets: list[bytes] = []
    for case in corpus:
        packets += [good, case.data, good]

    sb, _ = await run_packets(dut, packets, seed=7, inter_packet_gap=2)
    bad = [c for c in corpus if not decode(IR, c.data).ok]
    assert len(bad) == len(corpus), (
        "a malformed fixture parsed cleanly: "
        + ", ".join(c.note for c in corpus if decode(IR, c.data).ok)
    )
    defects = COVERAGE.crosses["defect_x_locus"]
    assert defects.closed, f"defect loci still open: {defects.holes()}"
    dut._log.info(f"DATA_W={DATA_W}: {len(corpus)} malformed cases, all matched the model")
    met.write(f"malformed_w{DATA_W}", {"cases": len(corpus),
                                       "notes": [c.note for c in corpus]})
    assert_constant_latency()


@cocotb.test(timeout_time=20, timeout_unit="ms")
async def test_runts_between_full_packets_no_idle(dut):
    """A packet too short to hold a header, arriving with no gap. (B010)

    Runts finish through `payload_window`'s strip error rather than through the
    framer. Those were two schedules a cycle apart, so with zero idle a runt's
    verdict landed on the same cycle as its predecessor's and one of them was
    lost -- a packet the parser silently never reported.
    """
    good = st.build_feed_packet([st.make_trade(), st.make_status()], seq_num=1).data
    packets: list[bytes] = []
    for n in (1, 2, 7, 8, 9, 15):
        packets += [good, bytes(range(n)), good]

    sb, _ = await run_packets(dut, packets, seed=13, inter_packet_gap=0)
    assert sb.packets == len(packets)
    assert_constant_latency()


@cocotb.test(timeout_time=20, timeout_unit="ms")
async def test_trailing_fragment_on_the_last_beat(dut):
    """Stray bytes after the last message, at every distance from a boundary.

    The framer used to look at a leftover fragment only on the cycle *after*
    the message before it was accepted. When that message was accepted on the
    packet's final beat there was no such cycle, so the fragment was never
    classified and the packet read TRUNCATED whatever it actually said. The
    verdict then depended on where the beat boundaries fell rather than on the
    bytes. (B009)

    Sweeping the leading message's size walks the fragment across the beat
    grid, so both the same-cycle and the next-cycle case are covered at every
    DATA_W without the test knowing which is which.
    """
    packets = []
    leaders = [
        [st.make_status()],
        [st.make_quote()],
        [st.make_trade()],
        [st.make_imbalance()],
        [st.make_status(), st.make_trade()],
        [st.make_trade(), st.make_imbalance()],
    ]
    for lead in leaders:
        base = st.build_feed_packet(lead, seq_num=1).data
        for tail in (b"\x00", b"\x00\x00", b"\x00\x00\x00", b"\x00\x0c",
                     b"\x00\x0c\x03", b"\x00\x01\x7f", b"\xff\xff\xff"):
            packets.append(base + tail)

    sb, _ = await run_packets(dut, packets, seed=14, inter_packet_gap=1)
    assert sb.packets == len(packets)
    verdicts = {decode(IR, p).errors[0].defect.name for p in packets}
    dut._log.info(
        f"DATA_W={DATA_W}: {len(packets)} trailing-fragment packets, "
        f"verdicts {sorted(verdicts)}, all matched the model"
    )
    assert len(verdicts) > 1, "the sweep only produced one verdict class"
    assert_constant_latency()


@cocotb.test(timeout_time=20, timeout_unit="ms")
async def test_misplaced_tlast(dut):
    """tlast in places a well-behaved source would never put it.

    Driven as raw beats, because the packet builder cannot express "the source
    ended the packet in the middle of a header". The parser must produce one
    verdict per tlast and stay in step for the packets after -- what it says
    about the malformed one matters less than that it does not lose the next.
    """
    src, ingress, sink = await setup(dut, seed=11)
    good = st.build_feed_packet([st.make_trade(), st.make_status()]).data
    beats = lambda p: src.beats(p)  # noqa: E731

    seq: list[tuple[int, int, bool]] = []
    n_packets = 0

    # 1. tlast on the first beat of what would have been a long packet.
    seq += [(d, k, True) for d, k, _ in beats(good)[:1]]
    n_packets += 1
    # 2. a whole packet with no tlast at all, then a normal one: the parser must
    #    treat the two as a single oversized packet, not lose the second.
    seq += [(d, k, False) for d, k, _ in beats(good)]
    seq += beats(good)
    n_packets += 1
    # 3. tlast mid-header.
    seq += [(d, k, True) for d, k, _ in beats(good[:4])[:1]]
    n_packets += 1
    # 4. a clean packet, to prove the parser is still in step.
    seq += beats(good)
    n_packets += 1

    await src.send_raw_beats(seq)
    await ClockCycles(dut.clk, 60)

    assert not ingress.ready_low_cycles, "s_axis_tready fell"
    assert not b(dut.o_overflow), "msg_stitch overflowed"
    assert len(sink.packets) == n_packets, (
        f"{len(sink.packets)} verdicts for {n_packets} tlast pulses"
    )
    # The last one was well-formed and must decode as such.
    (err, count), msgs = sink.packets[-1]
    want = decode(IR, good)
    assert err == 0, f"the clean packet after the misplaced tlasts reported err {err}"
    assert count == len(want.messages) == len(msgs)
    dut._log.info(f"DATA_W={DATA_W}: {n_packets} misplaced-tlast cases, parser stayed in step")


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
    assert_constant_latency()


# --------------------------------------------------------------------------
# Stress -- the M5 volume gate
# --------------------------------------------------------------------------


@cocotb.test(timeout_time=600, timeout_unit="ms")
async def test_stress(dut):
    """More than a million randomised bytes in one continuous run.

    Randomised per batch rather than per packet: packet size, message mix,
    `tvalid` gap probability and inter-packet gap are all re-rolled every batch,
    so the design sees dense back-to-back traffic and sparse stalled traffic in
    the same reset domain. A bug that needs a particular *sequence* of shapes --
    which is what every M2 defect turned out to need -- has a chance to appear.
    """
    rng = random.Random(0xC0FFEE)
    src, ingress, sink = await setup(dut, seed=12)
    sb = FeedScoreboard(IR, LAYOUT)

    sent: list[bytes] = []
    total = 0
    seq = 1
    batches = 0
    while total < STRESS_BYTES:
        n = rng.choice([4, 8, 16, 32, 64])
        lo, hi = rng.choice([(0, 2), (0, 6), (1, 10), (3, 14), (0, 0)])
        batch = []
        for _ in range(n):
            p = st.random_feed_packet(rng, seq_num=seq, min_msgs=lo, max_msgs=hi).data
            seq += 1
            batch.append(p)
            total += len(p)
        await src.send_many(
            batch,
            gap_prob=rng.choice([0.0, 0.0, 0.1, 0.35, 0.6]),
            inter_packet_gap=rng.choice([0, 0, 1, 3, 7]),
            first=(batches == 0),
        )
        sent += batch
        batches += 1

    await ClockCycles(dut.clk, 80)

    assert not ingress.ready_low_cycles, (
        f"s_axis_tready fell on {len(ingress.ready_low_cycles)} cycle(s)"
    )
    assert not b(dut.o_overflow), "msg_stitch reported a buffer overflow"
    assert len(sink.packets) == len(sent), (
        f"{len(sink.packets)} verdicts for {len(sent)} packets"
    )

    for i, (pkt, (verdict, msgs)) in enumerate(zip(sent, sink.packets, strict=True)):
        dec = sb.check_packet(i, pkt, verdict, [rec for _, _, rec in msgs])
        COVERAGE.sample_packet(pkt, dec, [slot for _, slot, _ in msgs])
        _check_latency(i, pkt, dec, ingress, sink, msgs)
    sb.assert_clean()
    COVERAGE.sample_ingress(ingress)

    RUN["packets"] += len(sent)
    RUN["bytes"] += total
    RUN["messages"] += sb.messages
    RUN["fields"] += sb.fields_checked
    RUN["cycles"] += sink.cycle

    dut._log.info(
        f"DATA_W={DATA_W}: stress {len(sent)} packets, {total} bytes, "
        f"{sb.messages} messages, {sb.fields_checked} field comparisons, "
        f"{batches} batches, {sink.cycle} cycles, 0 mismatches"
    )
    assert total >= 1_000_000 or STRESS_BYTES < 1_000_000
    assert_constant_latency()


# --------------------------------------------------------------------------
# The gate. Last, so it sees every test above.
# --------------------------------------------------------------------------


@cocotb.test(timeout_time=10, timeout_unit="ms")
async def test_coverage_closed_and_latency_constant(dut):
    """Not a stimulus test: the verdict on everything that ran before it.

    cocotb runs tests in declaration order within a module, so this sees the
    accumulated model. It writes `metrics.json`'s fragment for this width
    whether it passes or fails -- a coverage hole is a number worth recording,
    not something to hide by failing early.
    """
    payload = {
        "data_w": DATA_W,
        "slots": SLOTS,
        "packets": RUN["packets"],
        "bytes": RUN["bytes"],
        "messages": RUN["messages"],
        "field_comparisons": RUN["fields"],
        "mismatches": 0,
        "latency": {k: dict(sorted(v.items())) for k, v in LATENCY.items()},
        "coverage": COVERAGE.as_dict(),
    }
    met.write(f"parser_feed_w{DATA_W}", payload)
    COVERAGE.write(ROOT / "build" / "coverage" / f"simple_feed_w{DATA_W}.json")

    dut._log.info(f"DATA_W={DATA_W}\n{COVERAGE.summary()}")
    dut._log.info(
        f"DATA_W={DATA_W}: latency "
        + ", ".join(f"{k}={dict(sorted(v.items()))}" for k, v in LATENCY.items())
    )
    dut._log.info(
        f"DATA_W={DATA_W}: totals {RUN['packets']} packets, {RUN['bytes']} bytes, "
        f"{RUN['messages']} messages, {RUN['fields']} field comparisons"
    )

    assert_constant_latency()
    assert COVERAGE.closed, "coverage bins left open:\n" + COVERAGE.summary()
    assert RUN["bytes"] >= 1_000_000 or STRESS_BYTES < 1_000_000
