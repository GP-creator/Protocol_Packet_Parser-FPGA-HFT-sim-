# ADR 0003 — The parser core never backpressures, and its output schedule is fixed

**Status:** accepted (M2)

## Context

Two properties were required of the parser: `s_axis_tready` must never fall, and
end-to-end latency must be a constant number of cycles.

The first is straightforward and is the whole shape of the design: there is no
handshake anywhere inside the parser. `s_axis_tready` is tied to `1'b1` in
`parser_top`, every stage is an unconditional register, and nothing downstream
can reach back. A line-rate parser that can stall is a parser that needs an
elastic buffer sized for the worst case, and then a policy for what happens when
that buffer fills. Not having the stall is cheaper than having the policy.

The second turned out not to be literally achievable for the payload stream, and
this ADR records why, and what is guaranteed instead.

## The record bus: latency is exactly 2 cycles

`pkt_align` registers the beat; `hdr_parse` registers the record. Every packet's
record appears exactly two cycles after the ingress beat that completed its
header — the beat carrying byte 82, or the packet's last beat, whichever comes
first. No exceptions, at any `DATA_W`.
`test_parser_eth.py::check_record_latency` asserts this for every packet of
every test.

## The payload stream: 2 cycles, and one tail beat at 3

A payload beat is emitted by the ingress beat that supplies its last byte. When
the payload's length is not a multiple of `DATA_W/8`, the packet's final ingress
beat supplies the last byte of **two** output beats: one full beat and a short
tail. Two beats cannot occupy one cycle on one bus, so the tail is one cycle
behind.

This is arithmetic, not an implementation shortcoming. A packet with `P` payload
bytes needs `ceil(P / KEEP_W)` output beats. In the unaligned case the input beat
that completes output beat `k` also completes output beat `k+1`, because the
header consumed part of the first beat. No amount of restructuring makes two
beats fit in one cycle; the only alternatives are:

- **emit short beats mid-stream** (one payload beat per ingress beat, carrying
  however many payload bytes that beat held). Latency becomes exactly 2 for
  every beat, at the cost of a payload stream that is no longer densely packed —
  which pushes the packing problem into `msg_framer`, where it is harder.
- **add an output FIFO** and let it absorb the extra beat. That is elasticity,
  which is the thing the no-backpressure rule exists to avoid.

Neither is an improvement, so the tail beat stays.

What is guaranteed, and asserted continuously by
`test_parser_eth.py::check_payload_latency` on every packet of every test:

1. every payload beat is either 2 or 3 cycles behind the ingress beat that
   supplied its last byte;
2. a beat at 3 is always a packet's **final** beat, and is always sourced from
   the packet's **final** ingress beat;
3. at most one beat per packet is at 3.

So the schedule is a function of the packet's geometry alone — its length and its
header length. It does not depend on history, on the preceding packet, on
`tvalid` gaps, or on anything downstream. Two packets with the same geometry
produce byte-identical output on identical relative cycles. That is the property
that matters for a fixed-latency pipeline; "one number" is a stronger claim than
the arithmetic allows.

The per-packet terminal markers `o_empty` and `o_strip_err` are staged to land at
3 as well, so that they sit where a tail beat would have. Getting this wrong was
`B004`: at 2, a short packet's marker overtook the previous packet's tail beat
and the two packets' results came out of order.

## Consequences

- Nothing downstream of the parser may assert backpressure. The payload and
  record buses are valid-only, with no `tready`. Integrations that need
  backpressure put `axis_reg_slice` at the boundary and size their own buffer.
- `axis_reg_slice` is therefore not in the parser's datapath. It is a library
  module, and it is where the backpressure testing lives —
  `tb/unit/test_axis_reg_slice.py` drives it at 90%, 50% and 25% `tready` and
  requires zero loss.
- A consumer of the payload stream must tolerate a tail beat arriving one cycle
  later than the steady-state cadence. `msg_framer` is written to that contract.
- The claim "constant latency" should be read as "fixed schedule, no
  elasticity". The tests state the schedule precisely rather than a single
  number, so a change that introduced real elasticity would fail them.
