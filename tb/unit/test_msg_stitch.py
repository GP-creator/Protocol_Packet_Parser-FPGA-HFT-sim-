"""msg_stitch on its own: the byte queue, against a Python model of a byte queue.

Driven with arbitrary consume patterns rather than realistic ones, because the
framer's real pattern exercises only a slice of the state space and the bugs
live in the corners: consuming everything, consuming nothing for many beats, a
packet restarting while bytes are still held.
"""

from __future__ import annotations

import os
import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, ReadOnly, RisingEdge

from tb.common.axis_monitor import b, u

DATA_W = int(os.environ.get("DATA_W", "64"))
KEEP_W = DATA_W // 8
MSG_MAX = 32
BUF_BYTES = MSG_MAX - 1 + KEEP_W


def idle(dut):
    dut.i_valid.value = 0
    dut.i_data.value = 0
    dut.i_sof.value = 0
    dut.i_eof.value = 0
    dut.i_bytes.value = 0
    dut.i_empty.value = 0
    dut.i_consume.value = 0


async def setup(dut):
    cocotb.start_soon(Clock(dut.clk, 2, unit="ns").start())
    dut.rst_n.value = 0
    idle(dut)
    await ClockCycles(dut.clk, 5)
    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 2)


async def step(dut, *, data=b"", sof=False, eof=False, empty=False, consume=0):
    """One cycle. Returns the live window and count the DUT presented."""
    dut.i_valid.value = 1 if data else 0
    dut.i_data.value = int.from_bytes(data.ljust(KEEP_W, b"\x00"), "little") if data else 0
    dut.i_bytes.value = len(data)
    dut.i_sof.value = 1 if sof else 0
    dut.i_eof.value = 1 if eof else 0
    dut.i_empty.value = 1 if empty else 0
    dut.i_consume.value = consume

    await ReadOnly()
    nvalid = u(dut.o_nvalid)
    win = u(dut.o_win).to_bytes(BUF_BYTES, "little")[:nvalid]
    start = b(dut.o_pkt_start)
    last = b(dut.o_pkt_last)
    over = b(dut.o_overflow)
    await RisingEdge(dut.clk)
    idle(dut)
    return win, nvalid, start, last, over


@cocotb.test(timeout_time=400, timeout_unit="us")
async def test_queue_against_a_model(dut):
    """Random beats and random consume amounts, checked byte for byte."""
    await setup(dut)
    rng = random.Random(0x5717C4)
    held = bytearray()

    for _ in range(600):
        send = rng.random() < 0.75
        chunk = bytes(rng.randrange(256) for _ in range(rng.randint(1, KEEP_W))) if send else b""

        # The live window must already include this cycle's beat.
        want = bytes(held) + chunk
        consume = rng.randint(0, len(want)) if want and rng.random() < 0.6 else 0

        win, nvalid, _, _, over = await step(dut, data=chunk, consume=consume)
        assert not over, "overflow with a model that never exceeds capacity"
        assert nvalid == len(want), f"nvalid {nvalid}, model {len(want)}"
        assert win == want, f"\n  got  {win.hex()}\n  want {want.hex()}"

        held = bytearray(want[consume:])
        if len(held) + KEEP_W > BUF_BYTES:
            # Keep the model inside the capacity the framer guarantees.
            held = bytearray(held[: BUF_BYTES - KEEP_W])
            await setup(dut)
            held = bytearray()


@cocotb.test(timeout_time=200, timeout_unit="us")
async def test_live_window_includes_this_cycle(dut):
    """A message completed by the arriving beat must be extractable now.

    Without this the framer would need an extra cycle per message and the
    message latency would stop being a constant.
    """
    await setup(dut)
    beat = bytes((0x41 + i) & 0xFF for i in range(KEEP_W))
    win, nvalid, _, _, _ = await step(dut, data=beat, sof=True)
    assert nvalid == KEEP_W
    assert win == beat


@cocotb.test(timeout_time=200, timeout_unit="us")
async def test_carry_across_beats(dut):
    """The point of the module: a message split over several beats comes out whole."""
    await setup(dut)
    beats = min(3, BUF_BYTES // KEEP_W)  # stay inside the guaranteed capacity
    payload = bytes(range(beats * KEEP_W))
    seen = b""
    for i in range(beats):
        chunk = payload[i * KEEP_W : (i + 1) * KEEP_W]
        win, nvalid, _, _, _ = await step(
            dut, data=chunk, sof=(i == 0), eof=(i == beats - 1), consume=0
        )
        seen = win
        assert nvalid == (i + 1) * KEEP_W
    assert seen == payload, f"\n  got  {seen.hex()}\n  want {payload.hex()}"


@cocotb.test(timeout_time=200, timeout_unit="us")
async def test_sof_discards_the_previous_packet_tail(dut):
    """A truncated tail must not be prepended to the next packet's first message."""
    await setup(dut)
    # Leave five bytes stranded.
    await step(dut, data=b"\xde\xad\xbe\xef\x99", sof=True, eof=True, consume=0)
    win, nvalid, start, _, _ = await step(dut, data=b"\x01\x02\x03", sof=True)
    assert start
    assert nvalid == 3, f"nvalid {nvalid}: the old tail was carried over"
    assert win == b"\x01\x02\x03", win.hex()


@cocotb.test(timeout_time=200, timeout_unit="us")
async def test_empty_packet_clears_and_announces(dut):
    await setup(dut)
    await step(dut, data=b"\x11\x22\x33", sof=True, eof=True, consume=0)
    _, nvalid, start, last, _ = await step(dut, empty=True)
    assert start and last
    assert nvalid == 0, "an empty packet must present an empty window"


@cocotb.test(timeout_time=200, timeout_unit="us")
async def test_consume_everything_then_refill(dut):
    await setup(dut)
    chunk = bytes(range(KEEP_W))
    _, n1, _, _, _ = await step(dut, data=chunk, sof=True, consume=KEEP_W)
    assert n1 == KEEP_W
    win, n2, _, _, _ = await step(dut, data=chunk, eof=True, consume=0)
    assert n2 == KEEP_W, f"nvalid {n2}: consumed bytes came back"
    assert win == chunk


@cocotb.test(timeout_time=200, timeout_unit="us")
async def test_overflow_is_reported(dut):
    """Capacity is a guarantee the framer relies on; if it ever breaks, say so."""
    await setup(dut)
    over = False
    n_beats = (BUF_BYTES // KEEP_W) + 2
    for i in range(n_beats):
        _, _, _, _, o = await step(
            dut, data=bytes(KEEP_W), sof=(i == 0), consume=0
        )
        over |= o
    assert over, f"{n_beats} full beats with no consume should exceed {BUF_BYTES} bytes"
