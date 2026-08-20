"""payload_window: the funnel shift that re-bases a payload onto lane 0.

This is the module the whole design leans on, so the sweep is exhaustive rather
than sampled: every strip length from 0 to just over three beats, against packet
lengths chosen to land on both sides of every beat boundary. If the rotation,
the drain beat or the emit predicate is wrong for one alignment, one of these
combinations shows it.
"""

from __future__ import annotations

import os
import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge

from tb.common.axis_monitor import PayloadSink

DATA_W = int(os.environ.get("DATA_W", "64"))
KEEP_W = DATA_W // 8


def idle(dut):
    dut.i_valid.value = 0
    dut.i_sof.value = 0
    dut.i_eof.value = 0
    dut.i_data.value = 0
    dut.i_bytes.value = 0
    dut.i_offset.value = 0
    dut.i_strip_valid.value = 0
    dut.i_strip_bytes.value = 0


async def setup(dut):
    cocotb.start_soon(Clock(dut.clk, 2, unit="ns").start())
    dut.rst_n.value = 0
    idle(dut)
    await ClockCycles(dut.clk, 5)
    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 2)
    sink = PayloadSink(dut, dut.clk, "o", data_w=DATA_W)
    cocotb.start_soon(sink.run())
    return sink


async def push(dut, packet: bytes, strip: int, *, strip_from: int = 0, rng=None, gap_prob=0.0):
    """Drive one packet as pkt_align would present it.

    ``strip_from`` is the packet byte offset from which the header length is
    allowed to be known; the real parser learns it from a discriminator byte, so
    the tests cover it arriving late as well as immediately.
    """
    for off in range(0, len(packet), KEEP_W):
        chunk = packet[off : off + KEEP_W]
        if gap_prob and rng and off and rng.random() < gap_prob:
            idle(dut)
            for _ in range(rng.randint(1, 3)):
                await RisingEdge(dut.clk)
        dut.i_valid.value = 1
        dut.i_data.value = int.from_bytes(chunk.ljust(KEEP_W, b"\x00"), "little")
        dut.i_bytes.value = len(chunk)
        dut.i_offset.value = off
        dut.i_sof.value = 1 if off == 0 else 0
        dut.i_eof.value = 1 if off + len(chunk) >= len(packet) else 0
        known = off + len(chunk) > strip_from
        dut.i_strip_valid.value = 1 if known else 0
        dut.i_strip_bytes.value = strip
        await RisingEdge(dut.clk)
    idle(dut)


def expect(packet: bytes, strip: int) -> bytes:
    return packet[strip:] if strip <= len(packet) else b""


@cocotb.test(timeout_time=2000, timeout_unit="us")
async def test_every_strip_alignment(dut):
    """Sweep the rotation across three beats against every packet-tail length."""
    sink = await setup(dut)
    cases = []
    lengths = sorted(
        {KEEP_W, KEEP_W + 1, 2 * KEEP_W - 1, 2 * KEEP_W, 4 * KEEP_W + 3, 5 * KEEP_W, 100, 137}
    )
    for strip in range(0, 3 * KEEP_W + 1):
        for n in lengths:
            if n < strip:
                continue
            packet = bytes(((strip * 7 + n * 3 + i) & 0xFF) for i in range(n))
            cases.append((packet, strip))
            await push(dut, packet, strip)
            await ClockCycles(dut.clk, 3)

    await ClockCycles(dut.clk, 5)
    assert not sink.errors, "\n".join(sink.errors)
    got = [p.data for p in sink.payloads]
    want = [expect(p, s) for p, s in cases]
    assert len(got) == len(want), f"{len(got)} payloads emitted, expected {len(want)}"
    for i, ((p, s), g, w) in enumerate(zip(cases, got, want, strict=True)):
        assert g == w, (
            f"case {i}: len={len(p)} strip={s} rot={s % KEEP_W}\n"
            f"  got  {g.hex()}\n  want {w.hex()}"
        )


@cocotb.test(timeout_time=400, timeout_unit="us")
async def test_back_to_back_packets_with_different_strips(dut):
    """Zero idle between packets: the drain beat of one overlaps the sof of the next."""
    rng = random.Random(0xB2B)
    sink = await setup(dut)
    cases = []
    for _ in range(60):
        n = rng.randint(1, 120)
        strip = rng.randint(0, min(n, 3 * KEEP_W))
        packet = bytes(rng.randrange(256) for _ in range(n))
        cases.append((packet, strip))

    for packet, strip in cases:
        await push(dut, packet, strip)  # no gap: next sof lands in the drain cycle

    await ClockCycles(dut.clk, 5)
    assert not sink.errors, "\n".join(sink.errors)
    got = [p.data for p in sink.payloads]
    want = [expect(p, s) for p, s in cases]
    if got != want:
        i = next(
            (i for i, (g, w) in enumerate(zip(got, want)) if g != w), min(len(got), len(want))
        )
        lo, hi = max(0, i - 2), i + 3
        detail = [f"{len(got)} payloads, expected {len(want)}; first divergence at {i}"]
        for j in range(lo, min(hi, max(len(got), len(want)))):
            p, s = cases[j] if j < len(cases) else (b"", 0)
            g = got[j] if j < len(got) else None
            w = want[j] if j < len(want) else None
            detail.append(
                f"[{j}] n={len(p)} strip={s} rot={s % KEEP_W} "
                f"gotlen={len(g) if g is not None else -1} "
                f"wantlen={len(w) if w is not None else -1} "
                f"match={g == w}"
            )
        raise AssertionError("\n".join(detail))


@cocotb.test(timeout_time=400, timeout_unit="us")
async def test_strip_arriving_late(dut):
    """The header length may show up any time before the first payload byte."""
    sink = await setup(dut)
    cases = []
    for strip in (14, 24, 34, 42, 50, 3 * KEEP_W):
        for known_at in (0, strip - 1, strip):
            n = strip + 40
            packet = bytes(((i * 5 + strip) & 0xFF) for i in range(n))
            cases.append((packet, strip))
            await push(dut, packet, strip, strip_from=known_at)
            await ClockCycles(dut.clk, 3)

    await ClockCycles(dut.clk, 5)
    assert not sink.errors, "\n".join(sink.errors)
    got = [p.data for p in sink.payloads]
    want = [expect(p, s) for p, s in cases]
    assert got == want


@cocotb.test(timeout_time=200, timeout_unit="us")
async def test_empty_payload_is_announced(dut):
    """strip == packet length: no beats, but the packet still has to be visible."""
    sink = await setup(dut)
    for n in (1, KEEP_W, KEEP_W + 1, 40, 42):
        packet = bytes(range(n % 251 + 1))[:n].ljust(n, b"\x00")
        await push(dut, packet, n)
        await ClockCycles(dut.clk, 3)
    await ClockCycles(dut.clk, 5)
    assert not sink.errors, "\n".join(sink.errors)
    assert [p.data for p in sink.payloads] == [b""] * 5
    assert all(p.beats == 0 for p in sink.payloads)


@cocotb.test(timeout_time=200, timeout_unit="us")
async def test_strip_longer_than_the_packet(dut):
    """A header that runs past the end of the packet yields no payload, not garbage."""
    sink = await setup(dut)
    for n, strip in ((10, 42), (20, 42), (41, 42), (1, 8)):
        packet = bytes(range(n))
        await push(dut, packet, strip)
        await ClockCycles(dut.clk, 3)
    await ClockCycles(dut.clk, 5)
    assert not sink.errors, "\n".join(sink.errors)
    assert [p.data for p in sink.payloads] == [b""] * 4


@cocotb.test(timeout_time=200, timeout_unit="us")
async def test_never_learning_the_strip_is_flagged(dut):
    """A packet that ended before the header length was known must not go quiet."""
    sink = await setup(dut)
    errs = 0

    async def watch():
        nonlocal errs
        while True:
            await RisingEdge(dut.clk)
            if dut.o_strip_err.value:
                errs += 1

    cocotb.start_soon(watch())
    packet = bytes(range(20))
    # strip_from beyond the packet: i_strip_valid is never asserted.
    await push(dut, packet, 42, strip_from=1000)
    await ClockCycles(dut.clk, 6)
    assert errs == 1, f"expected exactly one o_strip_err pulse, saw {errs}"
    assert [p.data for p in sink.payloads] == []


@cocotb.test(timeout_time=200, timeout_unit="us")
async def test_single_beat_packet_after_a_long_one(dut):
    """Directed regression for B002 and B003 (docs/bugs-found.md).

    A packet whose first beat is also its last reads strip_lat, strip_q and
    emitted_q in the one cycle where they still describe the *previous* packet.
    Before the fix, the short packet inherited the long one's byte offset and
    its strip length -- and only when it followed a longer packet, which is why
    an isolated single-beat test would have passed.
    """
    sink = await setup(dut)
    long_pkt = bytes((i & 0xFF) for i in range(100))
    short_pkt = bytes(range(KEEP_W))

    await push(dut, long_pkt, 0)
    await ClockCycles(dut.clk, 4)
    await push(dut, short_pkt, 0)  # B002: stale emitted_q -> offset 96, sof low
    await ClockCycles(dut.clk, 4)
    await push(dut, long_pkt, 0)
    await ClockCycles(dut.clk, 4)
    await push(dut, short_pkt, 3)  # B003: stale strip_q -> stripped 0, not 3
    await ClockCycles(dut.clk, 6)

    def nbeats(payload: bytes) -> int:
        return (len(payload) + KEEP_W - 1) // KEEP_W

    assert not sink.errors, "\n".join(sink.errors)
    got = [(p.data, p.beats) for p in sink.payloads]
    want = [long_pkt, short_pkt, long_pkt, short_pkt[3:]]
    assert got == [(p, nbeats(p)) for p in want], got


@cocotb.test(timeout_time=400, timeout_unit="us")
async def test_tvalid_gaps(dut):
    """Idle cycles inside a packet must not shift the rotation."""
    rng = random.Random(0x9A95)
    sink = await setup(dut)
    cases = []
    for _ in range(40):
        n = rng.randint(1, 150)
        strip = rng.randint(0, min(n, 2 * KEEP_W))
        packet = bytes(rng.randrange(256) for _ in range(n))
        cases.append((packet, strip))
        await push(dut, packet, strip, rng=rng, gap_prob=0.35)
        await ClockCycles(dut.clk, rng.randint(1, 4))

    await ClockCycles(dut.clk, 5)
    assert not sink.errors, "\n".join(sink.errors)
    assert [p.data for p in sink.payloads] == [expect(p, s) for p, s in cases]
