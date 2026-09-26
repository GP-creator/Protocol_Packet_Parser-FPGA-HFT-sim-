# Timing closure — `parser_top_eth_ipv4_udp`, `DATA_W=64`

Vivado 2026.1, out-of-context, xc7a35tcpg236-1, `create_clock -period 4.0`
(250 MHz) on `clk` only. Every number below comes from a routed design produced
by `synth/vivado_ooc.tcl`. None is estimated.

```
# copy rtl/common/*.sv and rtl/generated/*.sv (not sample/) next to the script, then
vivado -mode batch -source vivado_ooc.tcl -tclargs 4.0                                     # default
vivado -mode batch -source vivado_ooc.tcl -tclargs 4.0 parser_top_eth_ipv4_udp xc7a35tcpg236-1 explore
```

The design decisions are in `docs/decisions/0004-timing-closure.md`.

## Result

| | before | after, default directives | after, Explore directives |
|---|---|---|---|
| WNS at 4.0 ns | -14.941 ns | -0.063 ns | **+0.047 ns** |
| Fmax (1 / (4.0 - WNS)) | 52.8 MHz | 246.1 MHz | **253.0 MHz** |
| TNS | -8153.1 ns | -0.095 ns | 0 |
| failing / total endpoints | 2122 / 2626 | 2 / 1923 | 0 / 1923 |
| logic levels, worst path | 31 | 4 | 5 |
| WHS | 0.179 ns | 0.138 ns | 0.107 ns |
| Slice LUTs | 1535 | 1494 | 1495 |
| Slice registers | 1398 | 1472 | 1472 |
| F7 muxes | 15 | 37 | 37 |

"Before" is `main` at the start of this work. The first run reported against it
gave WNS -13.980 ns with 2150/2634 failing endpoints. The reproduction here gave
-14.941 ns on the same worst path; the gap is placement variance.

**Latency is unchanged**: record 2 cycles, payload 2 with at most one tail beat
at 3, and on `simple_feed` header 2 / message 3 / verdict 5. No test was changed.
The full regression gives the same counts as M5 at both widths: 1,270,214 bytes,
201,322 field comparisons, 95,664 / 50,275 payload beats, identical simulation
time, and 0 mismatches. `ci/lint.sh`: 40 invocations, 0 warnings.

## Iterations

Each row is the routed default-directive result after the change on that row,
with the worst path it left behind.

| # | change | WNS (ns) | Fmax (MHz) | levels | worst path after the change |
|---|---|---|---|---|---|
| 0 | — | -14.941 | 52.8 | 31 | `u_align/o_offset` → `u_payload/drain_bytes_q` CE |
| 1 | `payload_window`: EOF accounting captured raw and resolved in the drain cycle; the live header length only latched. `pkt_align` decodes the beat slot; `hdr_accum` takes `o_pkt_bytes` instead of adding. | -3.174 | 139.4 | 9 | `u_align/o_sof` → `u_parse/o_hdr_bytes` |
| 2 | `hdr_accum` merges the beat on the ingress side of `pkt_align`'s edge, so the window is a flop | -2.227 | 160.6 | 10 | `u_align/o_offset` → `u_payload/o_bytes` |
| 3 | EOF-beat byte count from beat geometry (`i_bytes + K - rot`); running `consumed` counter removed | -0.953 | 201.9 | 6 | `u_align/o_offset` → `u_payload/o_keep` |
| 4 | slot one-hot and "window complete" predicted one beat ahead; payload fields load every cycle, only `o_valid` gated | -0.687 | 213.4 | 5 | `u_accum/o_have` → `u_payload/strip_q` CE |
| 5 | `have >= n` thermometer; generated `hdr_parse` looks it up instead of comparing | -0.647 | 215.2 | 5 | `u_accum/o_win` (IHL) → `u_payload/strip_q` |
| 6 | thermometer shifted by the beat's byte count (no adder); slot taken from it; `payload_window` samples the header length per beat and reads only that | -0.599 | 217.4 | 4 | `u_align/o_offset` → `u_payload/ep_emitted` R |
| 7 | payload offset from beat geometry; `emitted` counter removed; emit decision drives only `o_valid` | -0.063 | 246.1 | 4 | `u_accum/o_have_ge` → `u_parse/o_hdr_bytes` |
| 8 | *(reverted)* pre-decode IHL and selector matches into dedicated flops in `hdr_parse` | -0.332 | 230.8 | 5 | `u_parse/ipv4_ihl_c` → `u_payload/sr_bytes` |
| final | iteration 7 | -0.063 | 246.1 | 4 | `u_accum/o_have_ge` → `u_parse/o_hdr_bytes` |
| final, Explore | iteration 7 | **+0.047** | **253.0** | 5 | `u_accum/o_win` (IHL) → `u_parse/o_hdr_bytes` |

Two tool-only data points, for scale: Explore directives on iteration 5 gave
-0.497 ns (222.4 MHz), a 0.15 ns gain; on iteration 7 they gave +0.047 ns.

## The remaining critical path

With Explore directives, the worst paths per block are:

| into | slack | levels | path |
|---|---|---|---|
| `u_parse` | +0.047 | 5 | IHL bit of the header window → length decode → `have_ge` lookup → chain → `o_hdr_bytes` |
| `u_accum` | +0.066 | 1 | `pkt_align` sof flop → window merge (fan-out) |
| `u_payload` | +0.128 | 5 | `sr_bytes` → emit compare → `o_sof` |
| `u_align` | +1.247 | 3 | byte counter → `o_pkt_bytes` |

The next limit is the header parser's own cone: the IHL decides the IPv4
length, which decides where UDP starts, which decides the header length. That
chain is inherent to the protocol, and it is what a further step would have to
split, most likely with a pipeline stage on the record bus.

## Caveat: the ports are not timed

The flow above constrains only the clock, so paths that start at `s_axis_*` are
not timed. This work moved logic onto that side of `pkt_align`'s register:
`hdr_accum` now merges the window and shifts its thermometer by
`popcount(s_axis_tkeep)` before the edge. The baseline had the same kind of
path, `pkt_align`'s adder, and it was untimed too.

`vivado_ooc.tcl ... io` times the ports as if a flop sat directly on each one
(0 ns input and output delay):

| | before | after |
|---|---|---|
| WNS, default directives, ports timed | -15.537 ns (51.2 MHz) | -0.455 ns (224.5 MHz) |
| worst path | `u_align/o_offset` → `u_payload/drain_src_q` CE, 29 levels | `s_axis_tkeep[0]` → `u_accum/o_have_ge` D, 6 levels |
| worst path from a port | `rst_n` → `u_accum/win_q` R, -5.314 ns | `s_axis_tkeep[0]` → `u_accum/o_have_ge` D, -0.455 ns |

The design therefore still clears 200 MHz with the ports timed, but not 250 MHz.
The limiting path is `popcount(tkeep)` followed by the thermometer shift. Two
ways to close it:

- Build the thermometer from "at least j bytes" terms taken straight from
  `tkeep`, rather than shifting by the encoded count. That keeps the schedule,
  but it is only shallower at small `KEEP_W`.
- Register the ingress, which is where a real integration would normally put a
  register slice anyway. That costs one cycle measured from the parser's
  ports, which ADR 0003 counts, so it is a latency change and has not been
  made.

With Explore directives and the ports timed, Vivado 2026.1 crashed
(`EXCEPTION_ACCESS_VIOLATION`) in the post-route `phys_opt_design`, three times
at the same point. The last WNS it reported before the crash was -0.253 ns.
