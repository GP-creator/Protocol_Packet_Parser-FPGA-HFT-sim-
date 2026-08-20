# ADR 0001 — Rotate, then slice

**Status:** accepted (decided at M0, implemented and measured at M4)

## Context

A `simple_feed` message can begin at any byte of a beat. Its fields are at fixed
offsets *within the message*, but the message's own position is only known at run
time, so every field's position in the beat is `message_start + field_offset` —
a run-time value.

The obvious implementation gives each field its own mux over every byte lane it
could occupy. For `simple_feed` at `DATA_W=64` that is 18 record fields, each
needing a 39-way byte mux, all computing addresses that differ only by a
compile-time constant. The mux count grows with **fields x lanes**, and every one
of them re-derives the same `message_start`.

## Decision

Compute the message start once, rotate a window of the payload so that the
message begins at bit 0, and then read every field as a **static bit slice** of
that window.

- `msg_framer` computes where each message begins (a serial chain: read a length,
  add it, that is the next start).
- `msg_rotate` shifts the stitch buffer so that start lands at lane 0. One
  instance per *message slot*.
- `field_extract` reads all 18 fields as constant slices. No lane muxing at all.

Cost scales with **messages per beat**, not with fields per message. The same
move appears one level up, at layer granularity: in `hdr_parse`, UDP sits at
`14 + IHL*4`, so one shift places the UDP window and its four fields are static
slices of it.

## What it actually cost

Measured from the generated `DATA_W=64` design (`docs/measurements.md`):

| | rotate-then-slice | mux per field |
|---|---|---|
| run-time shifters, `simple_feed` messages | **1** (SLOTS=1) | 18 |
| run-time shifters, `eth_ipv4_udp` headers | **1** (UDP only) | 4 |
| `always_comb` blocks in `field_extract_simple_feed` | **0** | 18 |

Zero muxes in `field_extract` is not a lucky property of this schema — it is what
rotating first buys. The only thing that would reintroduce a mux is a field name
appearing at *different* offsets in different message types, and even then it is
a mux over the four type codes, not over 39 byte lanes.
`tests/test_emit.py::test_field_extract_needs_no_muxes_for_simple_feed` asserts
the count, so a template change that reintroduced lane muxing fails a test.

Rotator count by datapath width, `SLOTS = ceil(KEEP_W / MSG_MIN)`:

| `DATA_W` | 64 | 128 | 256 | 512 |
|---|---|---|---|---|
| bytes per beat | 8 | 16 | 32 | 64 |
| SLOTS (= rotators) | 1 | 2 | 3 | 5 |
| stitch buffer, bytes | 39 | 47 | 63 | 95 |

## The part that was not obvious

Sizing SLOTS by *buffer capacity* — how many messages could fit in the window —
gives 2/3/4/6 and is wrong, or rather needlessly large. Because the framer
consumes greedily, whatever is left at the start of a cycle is always one message
that was **not** completable, so its declared total exceeds the leftover `L`. For
`k` messages to finish in one cycle:

```
L + KEEP_W >= total_1 + (k-1)*MSG_MIN  >  L + (k-1)*MSG_MIN
        =>  KEEP_W > (k-1)*MSG_MIN
        =>  k <= ceil(KEEP_W / MSG_MIN)
```

At `DATA_W=64` that is 1: with 8-byte beats and a 14-byte smallest message, a
second slot can never fire. The over-provisioned version was built first and the
testbench found the extra slot idle in every cycle of every test — which is how
the tighter bound got derived rather than guessed.

## Consequences

- The message *boundary* chain is inherently serial: SLOTS rotators in series
  with SLOTS adders and length reads. That is the critical path of the design,
  and it grows with `DATA_W`. At 512 it is five deep. This is the real cost of
  the approach, and it is the thing to look at first if timing closure fails.
- `msg_rotate` is exhaustively unit-tested on its own
  (`tb/unit/test_msg_rotate.py`, every offset from 0 to `IN_BYTES`), because a
  single wrong bit there corrupts every field of every message equally — which
  in an integration test reads as "the parser is broken" rather than "the shifter
  is off by a byte".
- The window must zero-fill past its end rather than wrap. A rotate that wrapped
  would place real data where a short message's padding belongs, and a size check
  could then pass on garbage. Tested directly.
