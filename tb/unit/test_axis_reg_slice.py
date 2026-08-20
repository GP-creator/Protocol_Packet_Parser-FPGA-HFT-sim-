"""axis_reg_slice: lossless under arbitrary backpressure, full rate without it.

This suite doubles as the self-check on tb/common/axis_driver.py. If the driver
sampled tready on the wrong side of the clock edge it would drop beats here, and
the reassembled packets would come back short.
"""

from __future__ import annotations

import os
import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, ReadOnly, RisingEdge

from tb.common.axis_driver import AxisSource, ReadyDriver
from tb.common.axis_monitor import AxisSink, b

DATA_W = int(os.environ.get("DATA_W", "64"))


async def setup(dut, ready_prob=1.0, seed=0):
    cocotb.start_soon(Clock(dut.clk, 2, unit="ns").start())
    dut.rst_n.value = 0
    src = AxisSource(dut, dut.clk, "s", data_w=DATA_W, rng=random.Random(seed))
    dut.s_tuser.value = 0
    sink = AxisSink(dut, dut.clk, "m", data_w=DATA_W)
    ready = ReadyDriver(dut, dut.clk, "m", ready_prob=ready_prob, rng=random.Random(seed + 1))
    await ClockCycles(dut.clk, 5)
    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 2)
    cocotb.start_soon(sink.run())
    if ready_prob < 1.0:
        cocotb.start_soon(ready.run())
    else:
        dut.m_tready.value = 1
    return src, sink


def make_packets(rng, n, max_bytes=200):
    return [
        bytes(rng.randrange(256) for _ in range(rng.randint(1, max_bytes))) for _ in range(n)
    ]


@cocotb.test(timeout_time=200, timeout_unit="us")
async def test_passthrough_no_backpressure(dut):
    """Every byte arrives, in order, with tready never blocking."""
    rng = random.Random(0xA11CE)
    src, sink = await setup(dut, ready_prob=1.0, seed=1)
    packets = make_packets(rng, 40)
    await src.send_many(packets)
    await ClockCycles(dut.clk, 20)
    assert sink.packets == packets, (
        f"got {len(sink.packets)} packets, expected {len(packets)}"
    )


@cocotb.test(timeout_time=400, timeout_unit="us")
@cocotb.parametrize(ready_prob=[0.9, 0.5, 0.25])
async def test_lossless_under_backpressure(dut, ready_prob):
    """The skid slot is what makes a stalled downstream lossless."""
    rng = random.Random(0xB0B)
    src, sink = await setup(dut, ready_prob=ready_prob, seed=int(ready_prob * 100))
    packets = make_packets(rng, 30)
    await src.send_many(packets, gap_prob=0.3, max_gap=3)
    await ClockCycles(dut.clk, 400)
    assert sink.packets == packets, (
        f"ready_prob={ready_prob}: {len(sink.packets)} of {len(packets)} packets arrived"
    )


@cocotb.test(timeout_time=200, timeout_unit="us")
async def test_full_throughput(dut):
    """With tready held high the stage sustains one beat per cycle.

    Two storage slots exist to absorb a stall, not to halve the rate; if the
    skid ever fills when the output is free, this drops below 100%.
    """
    src, sink = await setup(dut, ready_prob=1.0, seed=3)
    keep_w = DATA_W // 8
    n_beats = 200
    packet = bytes(range(256)) * ((n_beats * keep_w) // 256 + 1)
    packet = packet[: n_beats * keep_w]

    start = None
    stalls = 0

    async def watch_ready():
        nonlocal stalls
        while True:
            await RisingEdge(dut.clk)
            await ReadOnly()
            if b(dut.s_tvalid) and not b(dut.s_tready):
                stalls += 1

    cocotb.start_soon(watch_ready())
    start = sink.cycle
    await src.send_many([packet])
    await ClockCycles(dut.clk, 10)
    assert sink.packets == [packet]
    assert stalls == 0, f"s_tready fell {stalls} time(s) while the output was never stalled"
    elapsed = sink.beats[-1].cycle - start
    assert elapsed <= n_beats + 4, f"{n_beats} beats took {elapsed} cycles"


@cocotb.test(timeout_time=200, timeout_unit="us")
async def test_reset_clears_the_stage(dut):
    """A reset mid-stream must not leak a stale beat into the next packet."""
    rng = random.Random(7)
    src, sink = await setup(dut, ready_prob=1.0, seed=5)
    # Stall the output and offer beats: the payload and skid slots both fill,
    # and s_tready falls. Driving tready from the test rather than ReadyDriver
    # keeps the source from blocking forever once the slots are full.
    dut.m_tready.value = 0
    src.tvalid.value = 1
    src.tdata.value = 0xDEAD
    src.tkeep.value = (1 << (DATA_W // 8)) - 1
    src.tlast.value = 0
    await ClockCycles(dut.clk, 6)
    await ReadOnly()
    assert not b(dut.s_tready), "both slots should be full with the output stalled"
    await RisingEdge(dut.clk)
    src.quiesce()
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 3)
    dut.rst_n.value = 1
    dut.m_tready.value = 1
    await ClockCycles(dut.clk, 4)
    sink.packets.clear()

    clean = make_packets(rng, 5)
    await src.send_many(clean)
    await ClockCycles(dut.clk, 40)
    assert sink.packets == clean, "a beat survived reset"
