"""AXI4-Stream source, written by hand against the cocotb 2.0 API.

Sampling discipline (see docs/cocotb-2.0-notes.md). A driver must decide whether
its beat was accepted using the tready the DUT itself sampled, which is the value
settled *before* the clock edge. So the wait is:

    write signals  ->  await ReadOnly()  ->  read tready  ->  await RisingEdge()

The ReadOnly phase of the timestep we are already in is after cocotb has applied
our writes and after combinational logic has settled, but before the edge. Doing
it the other way round -- edge first, then read -- samples the post-edge value
and silently drops a beat whenever tready falls on that edge.
"""

from __future__ import annotations

import random
from collections.abc import Iterable

from cocotb.triggers import ReadOnly, RisingEdge


def _opt(dut, name):
    try:
        return getattr(dut, name)
    except AttributeError:
        return None


class AxisSource:
    """Drive packets onto an AXI4-Stream slave port."""

    def __init__(self, dut, clk, prefix="s_axis", data_w=64, rng=None, honour_ready=True):
        self.clk = clk
        self.data_w = data_w
        self.keep_w = data_w // 8
        self.tvalid = getattr(dut, f"{prefix}_tvalid")
        self.tdata = getattr(dut, f"{prefix}_tdata")
        self.tkeep = _opt(dut, f"{prefix}_tkeep")
        self.tlast = getattr(dut, f"{prefix}_tlast")
        self.tuser = _opt(dut, f"{prefix}_tuser")
        self.tready = _opt(dut, f"{prefix}_tready") if honour_ready else None
        self.rng = rng or random.Random(0)
        self.beats_sent = 0
        self.bytes_sent = 0
        self.packets_sent = 0
        self.quiesce()

    # ---------------------------------------------------------------- state --
    def quiesce(self) -> None:
        """Drive an idle cycle's worth of signals. Safe to call before reset."""
        self.tvalid.value = 0
        self.tdata.value = 0
        self.tlast.value = 0
        if self.tkeep is not None:
            self.tkeep.value = 0
        if self.tuser is not None:
            self.tuser.value = 0

    def beats(self, data: bytes) -> list[tuple[int, int, bool]]:
        """Split a packet into (tdata, tkeep, tlast) triples.

        Byte k of a beat rides lane k, i.e. tdata bits [8k+7:8k]; that is a
        little-endian integer read of the beat.
        """
        assert data, "a zero-byte packet has no beats and cannot carry tlast"
        chunks = [data[i : i + self.keep_w] for i in range(0, len(data), self.keep_w)]
        out = []
        for i, chunk in enumerate(chunks):
            padded = chunk.ljust(self.keep_w, b"\x00")
            out.append(
                (
                    int.from_bytes(padded, "little"),
                    (1 << len(chunk)) - 1,
                    i == len(chunks) - 1,
                )
            )
        return out

    # ---------------------------------------------------------------- drive --
    async def idle_cycles(self, n: int) -> None:
        if n <= 0:
            return
        self.tvalid.value = 0
        self.tlast.value = 0
        for _ in range(n):
            await RisingEdge(self.clk)

    async def _drive(self, tdata: int, tkeep: int, tlast: bool) -> None:
        self.tvalid.value = 1
        self.tdata.value = tdata
        self.tlast.value = 1 if tlast else 0
        if self.tkeep is not None:
            self.tkeep.value = tkeep
        while True:
            await ReadOnly()
            accepted = self.tready is None or bool(self.tready.value)
            await RisingEdge(self.clk)
            if accepted:
                break
        self.beats_sent += 1

    async def send(self, data: bytes, *, gap_prob: float = 0.0, max_gap: int = 3) -> None:
        """Send one packet, then leave the bus idle."""
        await self.send_many([data], gap_prob=gap_prob, max_gap=max_gap)

    async def send_many(
        self,
        packets: Iterable[bytes],
        *,
        gap_prob: float = 0.0,
        max_gap: int = 3,
        inter_packet_gap: int = 0,
        first: bool = True,
    ) -> None:
        """Send packets back to back.

        ``inter_packet_gap=0`` and ``gap_prob=0`` produces a stream with no idle
        cycles at all, which is the case that catches state a design forgot to
        clear between packets.
        """
        for pkt in packets:
            if not first and inter_packet_gap:
                await self.idle_cycles(inter_packet_gap)
            first = False
            for i, (tdata, tkeep, tlast) in enumerate(self.beats(pkt)):
                if i and gap_prob and self.rng.random() < gap_prob:
                    await self.idle_cycles(self.rng.randint(1, max_gap))
                await self._drive(tdata, tkeep, tlast)
            self.packets_sent += 1
            self.bytes_sent += len(pkt)
        self.quiesce()

    async def send_raw_beats(self, beats: Iterable[tuple[int, int, bool]]) -> None:
        """Drive arbitrary beats, including illegal ones. For malformed-input tests."""
        for tdata, tkeep, tlast in beats:
            await self._drive(tdata, tkeep, tlast)
        self.quiesce()


class ReadyDriver:
    """Randomised backpressure on an AXI4-Stream master port's tready."""

    def __init__(self, dut, clk, prefix="m_axis", ready_prob=1.0, rng=None):
        self.clk = clk
        self.tready = getattr(dut, f"{prefix}_tready")
        self.ready_prob = ready_prob
        self.rng = rng or random.Random(1)
        self.tready.value = 1 if ready_prob >= 1.0 else 0

    async def run(self) -> None:
        while True:
            await RisingEdge(self.clk)
            self.tready.value = 1 if self.rng.random() < self.ready_prob else 0
