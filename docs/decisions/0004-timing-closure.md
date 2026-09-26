# ADR 0004 — Close timing by moving work across the existing registers, not by adding one

**Status:** accepted (timing closure, after M5)

## Context

The first out-of-context implementation of `parser_top_eth_ipv4_udp` at
`DATA_W=64` on an xc7a35t-1 ran at 53 MHz against a 250 MHz target: 31 logic
levels from `pkt_align`'s `o_offset` register to `payload_window`'s drain
registers. Numbers and the per-iteration record are in `docs/timing-closure.md`.

The path crossed four modules in one cycle: `hdr_accum` decoded the beat's slot
from `o_offset` and merged it into the live header window; `hdr_parse` walked the
protocol chain on that window and compared byte counts with 16-bit carry chains
to produce the header length; `payload_window` then did four more 16-bit
add/subtract/compare steps with that length to decide the EOF beat's byte count
and whether a drain beat follows.

ADR 0003 fixes the schedule: record 2 cycles, payload 2 (tail 3), and the framed
buses at 2/3/5. Adding a pipeline stage anywhere on this path would move one of
those numbers. The requirement was to keep them.

## Decision

Keep the register count. Move logic to the side of an existing register that
has slack, and replace arithmetic whose answer is already implied by the
design's invariants with that answer.

1. **The header length only ever reaches a D pin in `payload_window`.** It is
   sampled with each beat (`sr_*`). No beat that emits is the beat that first
   carries the length: the length is known by the first payload beat, and
   emission starts one beat later. The end-of-packet accounting that does need
   the EOF beat's own length is captured raw at EOF and resolved in the drain
   cycle, which already existed. The drain beat, `o_empty` and `o_strip_err`
   leave on the same cycle as before.

2. **Byte counts from beat geometry.** Every beat but the last is full, so an
   emitting beat's payload offset is `(offset/K - strip/K - 1) * K`, and the
   bytes left on the emitting EOF beat are `i_bytes + K - rot`. That removes the
   running counters and two chained 16-bit carry chains. The emit decision now
   drives only `o_valid`: the payload bus is valid-only (ADR 0003), so its
   fields load every cycle rather than hold.

3. **`hdr_accum` merges on the ingress side of `pkt_align`'s edge.** Using
   `pkt_align`'s pre-register annotation (`nx_sof`, `nx_bytes`, `nx_pkt_bytes`),
   the window, byte count and `o_done` are flops aligned with `pkt_align`'s
   `o_*`, so `hdr_parse` starts its cycle from registers rather than from a merge.

4. **Byte counts as a thermometer.** `o_have_ge[n] == (bytes >= n)`. A beat of
   `n` bytes shifts it up by `n`: a small mux instead of an adder then a compare.
   The next beat's slot and "window complete" are single bits of it.

5. **`hdr_parse` looks up, rather than compares.** The generated chain walk reads
   `have_lt(off + len)` from the thermometer. `off + len` is a function of a few
   header bits, so the lookup is a small mux on those bits, not a carry chain.

Items 1–4 are in `rtl/common`. Item 5 is in `wirespec/templates`, and the
generated RTL was regenerated from it.

## Rejected

- **A pipeline stage.** It is the obvious fix and would have closed timing, but
  it would cost a cycle on the record bus or the payload stream, and it was not
  needed.
- **Pre-decoding selectors and declared lengths in `hdr_parse`** from the
  next-cycle window (registered copies of the IHL field and the
  ethertype/protocol matches). Tried as iteration 8: the default flow went from
  -0.063 ns to -0.332 ns, because the path still ran through the same number of
  LUTs and the extra flops only moved placement. Reverted.
- **Tool effort alone.** Explore directives gave 0.15 ns at iteration 5, when the
  design was 0.65 ns short. They closed the last 0.06 ns only after the RTL was
  within reach.

## Consequences

- **Latency unchanged.** Every test runs unmodified and asserts the ADR 0003
  numbers per record. The regression is numerically identical to M5: the same
  1,270,214 bytes, 201,322 field comparisons, 95,664 payload beats and sim time
  per width.
- **Payload fields are defined only with `o_valid`.** They were already
  documented as valid-only, and every in-tree consumer (`msg_stitch`, the
  testbench's `PayloadSink`) qualifies with valid. A consumer that latched
  fields without looking at valid would now see them change.
- **`i_strip_*` must hold once valid** for the rest of the packet.
  `hdr_parse` does so by construction, since its chain only reads bytes that
  are already present, and the unit test already drives it that way.
- **Under a tkeep violation** (`o_keep_err`), payload byte counts and offsets
  follow beat geometry rather than a byte count. The payload *content* there was
  already undefined, because the funnel shift assumes full beats. The packet
  still ends with exactly one `o_eof`.
- **Interfaces.** `pkt_align` gains `nx_*` outputs. `hdr_accum` takes the
  ingress beat and gains `o_have_ge`. The generated `hdr_parse` takes
  `i_have_ge` instead of `i_have`. The hand-written M2 `hdr_parse` is unchanged
  and still reads `o_have`, so the hand-written vs generated comparison keeps
  running.
- **250 MHz needs the Explore implementation directives**
  (`synth/vivado_ooc.tcl ... explore`). Default directives reach 246 MHz.
- **More logic now sits in front of `pkt_align`'s register**: the window merge
  and the thermometer shift by `popcount(tkeep)`. The clock-only flow does not
  time it. With the ports timed as registers it is the critical path, at
  224.5 MHz. See `docs/timing-closure.md`.
