"""msg_rotate on its own: every offset, exhaustively.

Combinational, so this is a truth-table check rather than a protocol one. It is
worth having separately because the rotator is the one place a single wrong bit
would corrupt every field of every message equally, which in an integration test
looks like "the parser is broken" rather than "the shifter is off by a byte".
"""

from __future__ import annotations

import os
import random

import cocotb
from cocotb.triggers import Timer

from tb.common.axis_monitor import u

DATA_W = int(os.environ.get("DATA_W", "64"))
KEEP_W = DATA_W // 8
MSG_MAX = 32
IN_BYTES = MSG_MAX - 1 + KEEP_W
OUT_BYTES = MSG_MAX


def model(win: bytes, off: int) -> bytes:
    """What the rotator must produce: the window from `off`, zero-filled."""
    return (win[off:] + bytes(OUT_BYTES))[:OUT_BYTES]


async def apply(dut, win: bytes, off: int) -> bytes:
    dut.i_win.value = int.from_bytes(win, "little")
    dut.i_off.value = off
    # Purely combinational: there is no clock port, only settling time.
    await Timer(1, "ns")
    return u(dut.o_win).to_bytes(OUT_BYTES, "little")


@cocotb.test(timeout_time=200, timeout_unit="us")
async def test_every_offset(dut):
    """Sweep every legal offset against a pattern where each byte is distinct."""
    win = bytes((i * 7 + 3) & 0xFF for i in range(IN_BYTES))
    for off in range(IN_BYTES + 1):
        got = await apply(dut, win, off)
        assert got == model(win, off), (
            f"off={off}\n  got  {got.hex()}\n  want {model(win, off).hex()}"
        )


@cocotb.test(timeout_time=400, timeout_unit="us")
async def test_random_windows(dut):
    rng = random.Random(0x1234)
    for _ in range(300):
        win = bytes(rng.randrange(256) for _ in range(IN_BYTES))
        off = rng.randrange(IN_BYTES + 1)
        got = await apply(dut, win, off)
        assert got == model(win, off), f"off={off} win={win.hex()}"


@cocotb.test(timeout_time=200, timeout_unit="us")
async def test_offset_zero_is_a_wire(dut):
    """The common case: no rotation, the first OUT_BYTES straight through."""
    win = bytes(range(IN_BYTES))
    got = await apply(dut, win, 0)
    assert got == win[:OUT_BYTES]


@cocotb.test(timeout_time=200, timeout_unit="us")
async def test_offset_past_the_end_reads_zero(dut):
    """Bytes beyond the window must be zero, not wrapped around.

    msg_framer only trusts the first i_total bytes, but a rotate that wrapped
    would put real data where a short message's padding should be, and a size
    check could then pass on garbage.
    """
    win = bytes(0xFF for _ in range(IN_BYTES))
    got = await apply(dut, win, IN_BYTES)
    assert got == bytes(OUT_BYTES), got.hex()
    got = await apply(dut, win, IN_BYTES - 1)
    assert got == b"\xff" + bytes(OUT_BYTES - 1), got.hex()
