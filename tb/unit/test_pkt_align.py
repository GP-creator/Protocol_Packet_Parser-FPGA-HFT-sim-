"""pkt_align: tkeep/tlast turned into byte counts and packet framing.

Everything downstream addresses bytes by their packet offset, so an off-by-one
here is an off-by-one everywhere. The checks are exact, per beat, rather than
"the totals add up".
"""

from __future__ import annotations

import os
import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, ReadOnly, RisingEdge

from tb.common.axis_driver import AxisSource
from tb.common.axis_monitor import b, u

DATA_W = int(os.environ.get("DATA_W", "64"))
KEEP_W = DATA_W // 8


class AlignSink:
    """Capture every annotated beat pkt_align emits."""

    def __init__(self, dut):
        self.dut = dut
        self.beats: list[dict] = []
        self.packets: list[bytes] = []
        self._partial = bytearray()

    async def run(self):
        dut = self.dut
        while True:
            await RisingEdge(dut.clk)
            await ReadOnly()
            if not b(dut.o_valid):
                continue
            n = u(dut.o_bytes)
            raw = u(dut.o_data).to_bytes(KEEP_W, "little")
            beat = {
                "bytes": n,
                "offset": u(dut.o_offset),
                "sof": b(dut.o_sof),
                "eof": b(dut.o_eof),
                "pkt_bytes": u(dut.o_pkt_bytes),
                "keep_err": b(dut.o_keep_err),
                "data": raw[:n],
                "keep": u(dut.o_keep),
            }
            self.beats.append(beat)
            self._partial += beat["data"]
            if beat["eof"]:
                self.packets.append(bytes(self._partial))
                self._partial = bytearray()


async def setup(dut, seed=0):
    cocotb.start_soon(Clock(dut.clk, 2, unit="ns").start())
    dut.rst_n.value = 0
    src = AxisSource(dut, dut.clk, "s", data_w=DATA_W, rng=random.Random(seed), honour_ready=False)
    await ClockCycles(dut.clk, 5)
    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 2)
    sink = AlignSink(dut)
    cocotb.start_soon(sink.run())
    return src, sink


def expected_beats(packet: bytes) -> list[dict]:
    out = []
    for off in range(0, len(packet), KEEP_W):
        chunk = packet[off : off + KEEP_W]
        out.append(
            {
                "bytes": len(chunk),
                "offset": off,
                "sof": off == 0,
                "eof": off + len(chunk) >= len(packet),
                "pkt_bytes": off + len(chunk),
                "keep_err": False,
                "data": chunk,
                "keep": (1 << len(chunk)) - 1,
            }
        )
    return out


@cocotb.test(timeout_time=400, timeout_unit="us")
@cocotb.parametrize(inter_packet_gap=[0, 1, 3])
async def test_beat_annotation_is_exact(dut, inter_packet_gap):
    """Byte counts, offsets and framing flags, beat by beat.

    inter_packet_gap=0 is the case that catches an offset accumulator that is
    cleared a cycle too late.
    """
    rng = random.Random(0x9110 + inter_packet_gap)
    src, sink = await setup(dut, seed=inter_packet_gap)

    lengths = [1, 2, KEEP_W - 1, KEEP_W, KEEP_W + 1, 2 * KEEP_W, 3 * KEEP_W - 1, 100, 257]
    packets = [bytes(rng.randrange(256) for _ in range(n)) for n in lengths]
    packets += [bytes(rng.randrange(256) for _ in range(rng.randint(1, 300))) for _ in range(30)]

    await src.send_many(packets, inter_packet_gap=inter_packet_gap)
    await ClockCycles(dut.clk, 10)

    want = [beat for p in packets for beat in expected_beats(p)]
    assert len(sink.beats) == len(want), f"got {len(sink.beats)} beats, expected {len(want)}"
    for i, (got, exp) in enumerate(zip(sink.beats, want, strict=True)):
        assert got == exp, f"beat {i}: {got} != {exp}"
    assert sink.packets == packets


@cocotb.test(timeout_time=200, timeout_unit="us")
async def test_random_tvalid_gaps(dut):
    """Idle cycles between beats must not disturb the offsets."""
    rng = random.Random(0x6A75)
    src, sink = await setup(dut, seed=11)
    packets = [bytes(rng.randrange(256) for _ in range(rng.randint(1, 200))) for _ in range(25)]
    await src.send_many(packets, gap_prob=0.4, max_gap=4, inter_packet_gap=2)
    await ClockCycles(dut.clk, 10)
    assert sink.packets == packets
    want = [beat for p in packets for beat in expected_beats(p)]
    assert sink.beats == want


@cocotb.test(timeout_time=200, timeout_unit="us")
async def test_keep_error_on_a_hole(dut):
    """A gap in tkeep is a framing violation, not a narrow beat."""
    src, sink = await setup(dut, seed=2)
    full = (1 << KEEP_W) - 1
    holed = full ^ 0b10  # lane 1 missing
    await src.send_raw_beats([(0x1122334455667788, holed, True)])
    await ClockCycles(dut.clk, 5)
    assert len(sink.beats) == 1
    assert sink.beats[0]["keep_err"], "a hole in tkeep was accepted"


@cocotb.test(timeout_time=200, timeout_unit="us")
async def test_keep_error_on_an_empty_beat(dut):
    src, sink = await setup(dut, seed=3)
    await src.send_raw_beats([(0, 0, True)])
    await ClockCycles(dut.clk, 5)
    assert len(sink.beats) == 1
    assert sink.beats[0]["keep_err"], "a beat with no valid bytes was accepted"


@cocotb.test(timeout_time=200, timeout_unit="us")
async def test_keep_error_on_a_short_non_last_beat(dut):
    """Only the last beat of a packet may be partial."""
    src, sink = await setup(dut, seed=4)
    full = (1 << KEEP_W) - 1
    await src.send_raw_beats([(0xAA, 0b11, False), (0xBB, full, True)])
    await ClockCycles(dut.clk, 5)
    assert len(sink.beats) == 2
    assert sink.beats[0]["keep_err"], "a short beat before tlast was accepted"
    assert not sink.beats[1]["keep_err"]


@cocotb.test(timeout_time=200, timeout_unit="us")
async def test_single_byte_packets_back_to_back(dut):
    """The degenerate case: every beat is simultaneously sof and eof."""
    src, sink = await setup(dut, seed=5)
    packets = [bytes([i & 0xFF]) for i in range(20)]
    await src.send_many(packets, inter_packet_gap=0)
    await ClockCycles(dut.clk, 10)
    assert sink.packets == packets
    for beat in sink.beats:
        assert beat["sof"] and beat["eof"]
        assert beat["offset"] == 0
        assert beat["pkt_bytes"] == 1
