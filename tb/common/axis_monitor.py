"""Stream monitors, written by hand against the cocotb 2.0 API.

Sampling discipline, the mirror image of the driver's (docs/cocotb-2.0-notes.md):
a monitor wants the *post*-edge value of a registered output, so it waits for the
edge and then enters that timestep's ReadOnly phase:

    await RisingEdge()  ->  await ReadOnly()  ->  read

Every monitor counts its own clock edges. They are all started before the first
edge, so their cycle numbers agree without a shared counter that would introduce
an ordering dependency between coroutines waking on the same edge.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from cocotb.triggers import ReadOnly, RisingEdge


def u(handle) -> int:
    """Read a handle as an unsigned int, refusing X/Z rather than guessing.

    A one-bit port reads back as ``Logic``, a wider one as ``LogicArray``, and
    only the latter has ``to_unsigned``. Both raise on an unresolvable value,
    which is the behaviour worth keeping: an X that quietly becomes 0 turns a
    reset bug into a passing test.
    """
    v = handle.value
    to_unsigned = getattr(v, "to_unsigned", None)
    if to_unsigned is not None:
        if not v.is_resolvable:
            raise AssertionError(f"signal is not resolvable: {v}")
        return to_unsigned()
    try:
        return int(v)
    except ValueError as exc:  # Logic('x') / Logic('z')
        raise AssertionError(f"signal is not resolvable: {v}") from exc


def b(handle) -> bool:
    return bool(u(handle))


def _opt(dut, name):
    try:
        return getattr(dut, name)
    except AttributeError:
        return None


@dataclass
class Beat:
    cycle: int
    data: bytes
    last: bool


class AxisSink:
    """Reassemble packets from an AXI4-Stream master port."""

    def __init__(self, dut, clk, prefix="m_axis", data_w=64):
        self.clk = clk
        self.keep_w = data_w // 8
        self.tvalid = getattr(dut, f"{prefix}_tvalid")
        self.tdata = getattr(dut, f"{prefix}_tdata")
        self.tkeep = _opt(dut, f"{prefix}_tkeep")
        self.tlast = getattr(dut, f"{prefix}_tlast")
        self.tready = _opt(dut, f"{prefix}_tready")
        self.packets: list[bytes] = []
        self.beats: list[Beat] = []
        self.cycle = 0
        self._partial = bytearray()

    async def run(self) -> None:
        while True:
            await RisingEdge(self.clk)
            await ReadOnly()
            self.cycle += 1
            if not b(self.tvalid):
                continue
            if self.tready is not None and not b(self.tready):
                continue
            raw = u(self.tdata).to_bytes(self.keep_w, "little")
            n = bin(u(self.tkeep)).count("1") if self.tkeep is not None else self.keep_w
            chunk = raw[:n]
            self.beats.append(Beat(self.cycle, chunk, b(self.tlast)))
            self._partial += chunk
            if b(self.tlast):
                self.packets.append(bytes(self._partial))
                self._partial = bytearray()


@dataclass
class PayloadBeat:
    cycle: int
    offset: int
    nbytes: int
    eof: bool


@dataclass
class PayloadPacket:
    data: bytes
    first_cycle: int
    last_cycle: int
    beats: int
    beat_records: list[PayloadBeat] = field(default_factory=list)


class PayloadSink:
    """Reassemble the parser's valid-only, lane-0-aligned payload stream.

    The bus carries sof/eof and an explicit byte count rather than tkeep alone,
    and an o_empty pulse for a packet whose payload is zero bytes long -- which
    would otherwise be indistinguishable from no packet at all.
    """

    def __init__(self, dut, clk, prefix="m_pay", data_w=64):
        self.clk = clk
        self.keep_w = data_w // 8
        self.valid = getattr(dut, f"{prefix}_valid")
        self.data = getattr(dut, f"{prefix}_data")
        self.sof = getattr(dut, f"{prefix}_sof")
        self.eof = getattr(dut, f"{prefix}_eof")
        self.nbytes = getattr(dut, f"{prefix}_bytes")
        self.offset = getattr(dut, f"{prefix}_offset")
        self.keep = _opt(dut, f"{prefix}_keep")
        self.empty = _opt(dut, f"{prefix}_empty")
        self.strip_err = _opt(dut, "o_strip_err")

        self.payloads: list[PayloadPacket] = []
        self.strip_err_cycles: list[int] = []
        self.valid_cycles: list[int] = []
        self.cycle = 0
        self.errors: list[str] = []
        self._partial = bytearray()
        self._start = 0
        self._beats = 0
        self._beat_records: list[PayloadBeat] = []

    async def run(self) -> None:
        while True:
            await RisingEdge(self.clk)
            await ReadOnly()
            self.cycle += 1

            if self.strip_err is not None and b(self.strip_err):
                self.strip_err_cycles.append(self.cycle)

            if self.empty is not None and b(self.empty):
                # Two packets' terminal markers on one cycle would leave their
                # order undefined on the bus, which is how B004 hid.
                if b(self.valid):
                    self.errors.append(
                        f"cycle {self.cycle}: o_empty and o_valid asserted together, "
                        f"so two packets' terminal events share a cycle"
                    )
                self.payloads.append(PayloadPacket(b"", self.cycle, self.cycle, 0, []))
                self.valid_cycles.append(self.cycle)

            if not b(self.valid):
                continue

            self.valid_cycles.append(self.cycle)
            n = u(self.nbytes)
            raw = u(self.data).to_bytes(self.keep_w, "little")
            chunk = raw[:n]

            if self.keep is not None:
                want = (1 << n) - 1
                got = u(self.keep)
                if got != want:
                    self.errors.append(
                        f"cycle {self.cycle}: keep 0x{got:x} disagrees with bytes {n} "
                        f"(expected 0x{want:x})"
                    )

            if b(self.sof):
                if self._partial:
                    self.errors.append(f"cycle {self.cycle}: sof while a payload was open")
                self._partial = bytearray()
                self._start = self.cycle
                self._beats = 0
                self._beat_records = []
                if u(self.offset) != 0:
                    self.errors.append(f"cycle {self.cycle}: sof beat has offset != 0")
            elif u(self.offset) != len(self._partial):
                self.errors.append(
                    f"cycle {self.cycle}: offset {u(self.offset)} != bytes so far "
                    f"{len(self._partial)}"
                )

            self._beat_records.append(
                PayloadBeat(self.cycle, u(self.offset), n, b(self.eof))
            )
            self._partial += chunk
            self._beats += 1

            if b(self.eof):
                self.payloads.append(
                    PayloadPacket(
                        bytes(self._partial),
                        self._start,
                        self.cycle,
                        self._beats,
                        self._beat_records,
                    )
                )
                self._partial = bytearray()
                self._beat_records = []


@dataclass
class Record:
    """One header record captured off the parser's record bus."""

    cycle: int
    fields: dict[str, int] = field(default_factory=dict)

    def __getitem__(self, key: str) -> int:
        return self.fields[key]


class RecordMonitor:
    """Capture the parser's per-packet record bus."""

    def __init__(self, dut, clk, signals: list[str], valid="o_rec_valid"):
        self.clk = clk
        self.dut = dut
        self.valid = getattr(dut, valid)
        self.signals = {name: getattr(dut, name) for name in signals}
        self.records: list[Record] = []
        self.cycle = 0

    async def run(self) -> None:
        while True:
            await RisingEdge(self.clk)
            await ReadOnly()
            self.cycle += 1
            if b(self.valid):
                self.records.append(
                    Record(self.cycle, {n: u(h) for n, h in self.signals.items()})
                )


@dataclass
class IngressBeat:
    cycle: int
    offset: int  # packet byte index of lane 0
    nbytes: int
    eof: bool

    @property
    def end(self) -> int:
        """One past the last packet byte this beat carries."""
        return self.offset + self.nbytes


class IngressTracker:
    """Record every accepted ingress beat, grouped into packets.

    Feeds two continuous checks: that tready is never deasserted, and that each
    output beat appears a fixed number of cycles after the ingress beat that
    delivered its last byte.
    """

    def __init__(self, dut, clk, prefix="s_axis", data_w=64):
        self.clk = clk
        self.keep_w = data_w // 8
        self.tvalid = getattr(dut, f"{prefix}_tvalid")
        self.tready = _opt(dut, f"{prefix}_tready")
        self.tlast = getattr(dut, f"{prefix}_tlast")
        self.tkeep = _opt(dut, f"{prefix}_tkeep")
        self.cycle = 0
        self.valid_cycles: set[int] = set()
        self.eof_cycles: list[int] = []
        self.ready_low_cycles: list[int] = []
        self.packets: list[list[IngressBeat]] = []
        self._current: list[IngressBeat] = []
        self._offset = 0

    def beat_covering(self, pkt_index: int, byte_index: int) -> IngressBeat | None:
        """The beat of packet ``pkt_index`` that carried ``byte_index``."""
        for beat in self.packets[pkt_index]:
            if beat.offset <= byte_index < beat.end:
                return beat
        return None

    async def run(self) -> None:
        while True:
            await RisingEdge(self.clk)
            await ReadOnly()
            self.cycle += 1
            if self.tready is not None and not b(self.tready):
                self.ready_low_cycles.append(self.cycle)
            if not b(self.tvalid):
                continue

            self.valid_cycles.add(self.cycle)
            n = bin(u(self.tkeep)).count("1") if self.tkeep is not None else self.keep_w
            last = b(self.tlast)
            self._current.append(IngressBeat(self.cycle, self._offset, n, last))
            self._offset += n
            if last:
                self.eof_cycles.append(self.cycle)
                self.packets.append(self._current)
                self._current = []
                self._offset = 0
