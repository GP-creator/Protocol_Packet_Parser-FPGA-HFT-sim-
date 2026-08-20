"""Hand-rolled functional coverage.

`cocotb-coverage` is not available here and is not wanted: the interesting part
is not the bin-counting machinery, it is that the bins are *derived from the
schema* rather than typed out. Add a message type to `simple_feed.yaml` and the
lane cross, the beat-span cross and every value-class bin for its fields appear
on their own, unhit, and the coverage gate fails until stimulus reaches them.

Two rules make the numbers mean something:

* Every declared bin must be reachable. A denominator padded with impossible
  combinations turns "94% covered" into a number that can never be 100% and so
  carries no information. Where a combination is unreachable -- a 32-byte
  message cannot span one beat of eight -- it is not declared, and the
  reachability is computed, not asserted by hand.
* Bins are declared up front, from the schema, before any stimulus runs. A
  coverage model that grows a bin the first time something happens can only ever
  report 100%.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from wirespec.golden import Decoded, Defect
from wirespec.ir import IR, IRField, IRRecord

# --------------------------------------------------------------------------
# Bin containers
# --------------------------------------------------------------------------


@dataclass
class Cross:
    """One coverage cross: named axes, an up-front bin set, and hit counts."""

    name: str
    axes: tuple[str, ...]
    note: str
    counts: dict[tuple, int] = field(default_factory=dict)

    def declare(self, *key) -> None:
        self.counts.setdefault(tuple(key), 0)

    def hit(self, *key) -> None:
        k = tuple(key)
        if k not in self.counts:
            # Sampling something never declared means the reachability analysis
            # above is wrong. Say so loudly rather than inflating the numerator.
            raise AssertionError(f"coverage cross '{self.name}': undeclared bin {k}")
        self.counts[k] += 1

    @property
    def declared(self) -> int:
        return len(self.counts)

    @property
    def hits(self) -> int:
        return sum(1 for v in self.counts.values() if v)

    @property
    def closed(self) -> bool:
        return self.hits == self.declared

    def holes(self, limit: int = 12) -> list[tuple]:
        out = [k for k, v in self.counts.items() if not v]
        return out[:limit]

    def as_dict(self) -> dict:
        return {
            "axes": list(self.axes),
            "note": self.note,
            "declared": self.declared,
            "hit": self.hits,
            "closed": self.closed,
            "holes": [list(map(str, h)) for h in self.holes()],
        }


# --------------------------------------------------------------------------
# Value classes -- the schema-generated part
# --------------------------------------------------------------------------

#: Value classes for an integer field, in the order they are tested.
SIGNED_CLASSES = ("min", "neg", "zero", "pos", "max")
UNSIGNED_CLASSES = ("zero", "mid", "max")
BYTES_CLASSES = ("zero", "ones", "mixed")


def value_class(f: IRField, value) -> str:
    """Which bin a decoded field value falls into."""
    if f.is_bytes_like:
        if all(x == 0 for x in value):
            return "zero"
        if all(x == 0xFF for x in value):
            return "ones"
        return "mixed"
    if f.signed:
        lo, hi = -(1 << (f.width - 1)), (1 << (f.width - 1)) - 1
        if value == lo:
            return "min"
        if value == hi:
            return "max"
        if value == 0:
            return "zero"
        return "neg" if value < 0 else "pos"
    hi = (1 << f.width) - 1
    if value == 0:
        return "zero"
    if value == hi:
        return "max"
    return "mid"


def classes_for(f: IRField) -> tuple[str, ...]:
    if f.is_bytes_like:
        return BYTES_CLASSES
    return SIGNED_CLASSES if f.signed else UNSIGNED_CLASSES


def _payload_fields(ir: IR, rec: IRRecord) -> list[IRField]:
    """Fields a test can choose the value of.

    The length and type prefix are structural -- their values are dictated by
    the message they introduce, so binning them would declare bins that no
    legal stimulus can reach. `msg_count` is excluded for the same reason.
    """
    skip_bits = ir.prefix_bytes * 8 if ir.is_framed else 0
    out = []
    for f in rec.fields:
        if f.bit_offset < skip_bits:
            continue
        if ir.count_field and f.name == ir.count_field:
            continue
        out.append(f)
    return out


# --------------------------------------------------------------------------
# The model
# --------------------------------------------------------------------------

#: Defect classes a framed packet can carry, and where in the packet they sit.
FRAMED_DEFECTS = (
    Defect.TRUNCATED_HEADER,
    Defect.TRUNCATED_MESSAGE,
    Defect.BAD_LENGTH,
    Defect.ZERO_LENGTH,
    Defect.UNKNOWN_TYPE,
    Defect.COUNT_MISMATCH,
)

MSG_BUCKETS = ("0", "1", "2-4", "5-9", "10+")


def msg_bucket(n: int) -> str:
    if n <= 1:
        return str(n)
    if n <= 4:
        return "2-4"
    if n <= 9:
        return "5-9"
    return "10+"


class FeedCoverage:
    """Coverage for a framed schema, driven from the golden decode of each packet.

    Sampling from the model rather than from RTL internals is deliberate: this
    measures what the *stimulus* reached, which is the question coverage exists
    to answer. Whether the RTL agreed is the scoreboard's job, and a bug that
    made both wrong would still show as a mismatch there.
    """

    def __init__(self, ir: IR, data_w: int, slots: int):
        self.ir = ir
        self.keep_w = data_w // 8
        self.slots = slots
        self.crosses: dict[str, Cross] = {}
        self.packets = 0
        self.messages = 0
        self._declare()

    # ------------------------------------------------------------ declare --
    def _add(self, name: str, axes: tuple[str, ...], note: str) -> Cross:
        c = Cross(name, axes, note)
        self.crosses[name] = c
        return c

    def _declare(self) -> None:
        ir, keep = self.ir, self.keep_w
        types = [m.name for m in ir.messages]

        c = self._add(
            "type_x_lane",
            ("message type", "start byte lane"),
            "every message type begins at every byte lane of a beat",
        )
        for t in types:
            for lane in range(keep):
                c.declare(t, lane)

        c = self._add(
            "type_x_beats",
            ("message type", "beats spanned"),
            "how many ingress beats one message is split across; "
            "only spans arithmetic allows are declared",
        )
        for m in ir.messages:
            spans = {
                -(-(lane + m.max_bytes) // keep) for lane in range(keep)
            }
            for n in sorted(spans):
                c.declare(m.name, n)

        c = self._add(
            "type_x_slot",
            ("message type", "framer slot"),
            "each type extracted through each parallel rotator, for the slots "
            "that type can actually reach",
        )
        msg_min = ir.min_message_bytes
        for m in ir.messages:
            for s in range(self.slots):
                # For slot s to fire, messages 0..s all complete this cycle.
                # The leftover L at cycle start belongs to a message that did
                # not complete, so total_0 > L, and the whole run fits in
                # L + KEEP_W. Subtracting: sum of totals 1..s < KEEP_W, and
                # every one before this slot is at least MSG_MIN. A big message
                # therefore cannot reach a high slot, and declaring that bin
                # would leave the cross permanently open.
                if s == 0 or m.max_bytes + (s - 1) * msg_min < keep:
                    c.declare(m.name, s)

        c = self._add(
            "defect_x_locus",
            ("defect", "where in the packet"),
            "each malformed class, both as a packet's first message and after "
            "a good one -- a parser can get the first right and leak state",
        )
        for d in FRAMED_DEFECTS:
            if d is Defect.TRUNCATED_HEADER:
                c.declare(d.name, "header")  # no message locus exists
            elif d is Defect.COUNT_MISMATCH:
                c.declare(d.name, "trailer")  # only observable at packet end
            else:
                c.declare(d.name, "first_message")
                c.declare(d.name, "later_message")

        c = self._add(
            "msgs_x_alignment",
            ("message count", "packet length vs beat width"),
            "message-count bucket against whether the packet ends mid-beat",
        )
        for bucket in MSG_BUCKETS:
            for align in ("aligned", "unaligned"):
                c.declare(bucket, align)

        c = self._add(
            "field_value_class",
            ("record.field", "value class"),
            "every settable field of every record at each end of its range; "
            "bins come from the field's own width and signedness",
        )
        for rec in (ir.header, *ir.messages):
            for f in _payload_fields(ir, rec):
                for cls in classes_for(f):
                    c.declare(f"{rec.name}.{f.name}", cls)

        c = self._add(
            "ingress_shape",
            ("tvalid gap before beat", "beat position in packet"),
            "a stalled source at each position, including the first and last "
            "beat of a packet",
        )
        for gap in ("gap", "no_gap"):
            for pos in ("sof", "mid", "eof"):
                c.declare(gap, pos)

    # ------------------------------------------------------------- sample --
    def sample_packet(self, pkt: bytes, dec: Decoded, slots_used: list[int]) -> None:
        """Record everything one packet reached.

        ``slots_used`` is the framer slot index each accepted message came out
        of, in order -- the one thing the model cannot know.
        """
        self.packets += 1
        keep = self.keep_w

        self.crosses["msgs_x_alignment"].hit(
            msg_bucket(len(dec.messages)),
            "aligned" if len(pkt) % keep == 0 else "unaligned",
        )

        if dec.header is not None:
            for f in _payload_fields(self.ir, self.ir.header):
                self.crosses["field_value_class"].hit(
                    f"{self.ir.header.name}.{f.name}",
                    value_class(f, dec.header[f.name]),
                )

        for i, m in enumerate(dec.messages):
            self.messages += 1
            rec = self.ir.record(m.name)
            self.crosses["type_x_lane"].hit(m.name, m.offset % keep)
            first, last = m.offset // keep, (m.offset + m.length - 1) // keep
            self.crosses["type_x_beats"].hit(m.name, last - first + 1)
            if i < len(slots_used):
                self.crosses["type_x_slot"].hit(m.name, slots_used[i])
            for f in _payload_fields(self.ir, rec):
                if f.name in m.values:
                    self.crosses["field_value_class"].hit(
                        f"{rec.name}.{f.name}", value_class(f, m.values[f.name])
                    )

        if dec.errors:
            d = dec.errors[0].defect
            if d in FRAMED_DEFECTS:
                if d is Defect.TRUNCATED_HEADER:
                    locus = "header"
                elif d is Defect.COUNT_MISMATCH:
                    locus = "trailer"
                else:
                    locus = "first_message" if not dec.messages else "later_message"
                self.crosses["defect_x_locus"].hit(d.name, locus)

    def sample_ingress(self, tracker) -> None:
        """Bin the shape of the traffic the source actually produced.

        The previous cycle carries across packet boundaries: an idle gap before
        a packet's first beat is exactly as interesting as one in the middle,
        and treating each packet as starting fresh would leave the
        ('gap', 'sof') bin permanently unhittable.
        """
        c = self.crosses["ingress_shape"]
        prev = None
        for beats in tracker.packets:
            for i, beat in enumerate(beats):
                pos = "sof" if i == 0 else ("eof" if beat.eof else "mid")
                gap = "gap" if prev is not None and beat.cycle - prev > 1 else "no_gap"
                c.hit(gap, pos)
                prev = beat.cycle

    # ------------------------------------------------------------- report --
    def merge(self, other: FeedCoverage) -> None:
        """Fold another run's hits in. Bins must match, which they do when the
        schema and DATA_W do -- so merging across widths is refused."""
        assert self.keep_w == other.keep_w, "cannot merge coverage across DATA_W"
        self.packets += other.packets
        self.messages += other.messages
        for name, c in other.crosses.items():
            mine = self.crosses[name]
            for k, v in c.counts.items():
                mine.counts[k] = mine.counts.get(k, 0) + v

    @property
    def declared(self) -> int:
        return sum(c.declared for c in self.crosses.values())

    @property
    def hits(self) -> int:
        return sum(c.hits for c in self.crosses.values())

    @property
    def closed(self) -> bool:
        return all(c.closed for c in self.crosses.values())

    def summary(self) -> str:
        lines = [f"coverage: {self.hits}/{self.declared} bins over "
                 f"{self.packets} packets, {self.messages} messages"]
        for name, c in self.crosses.items():
            mark = "closed" if c.closed else f"OPEN  holes={c.holes(6)}"
            lines.append(f"  {name:<22} {c.hits:>4}/{c.declared:<4} {mark}")
        return "\n".join(lines)

    def as_dict(self) -> dict:
        return {
            "packets": self.packets,
            "messages": self.messages,
            "declared": self.declared,
            "hit": self.hits,
            "closed": self.closed,
            "crosses": {n: c.as_dict() for n, c in self.crosses.items()},
        }

    def write(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.as_dict(), indent=2) + "\n")
