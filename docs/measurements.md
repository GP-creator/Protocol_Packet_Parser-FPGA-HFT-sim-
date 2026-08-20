# Measurements

Numbers recorded as they are measured, with the command that produced them.
Nothing here is estimated or projected.

## M1 — schema and golden model

Measured 2026-08-19 on WSL2 Ubuntu, Python 3.14.4.

| Quantity | Value |
|---|---|
| pytest tests | 118 passed |
| pytest wall time | 0.94 s |
| invalid-schema fixtures | 31, each asserting a distinct rejection message |
| `eth_ipv4_udp` random packets decoded | 10,000 |
| `eth_ipv4_udp` random bytes decoded | 1,163,122 |
| `eth_ipv4_udp` chain mix | eth/ipv4/udp 8,822 · eth only 590 · eth/ipv4 588 |
| `simple_feed` random packets decoded | 10,000 |
| `simple_feed` random bytes decoded | 1,108,372 |
| `simple_feed` messages decoded | 40,281 |
| `simple_feed` message mix | trade 10,095 · quote 10,141 · status 9,927 · imbalance 10,118 |
| Builder↔golden mismatches | 0 |
| Fuzz iterations (truncation + corruption, no crash) | 4,000 |

Reproduce:

```
cd ~/wirespec
python3 -m pytest                       # the whole suite
python3 -m pytest -s -k test_random      # prints the packet/byte counts above
WIRESPEC_RANDOM_N=100000 python3 -m pytest -s -k test_random   # deeper run
```

### Static schema facts (from `tests/test_ir.py`)

| Quantity | `eth_ipv4_udp` | `simple_feed` |
|---|---|---|
| worst-case header bytes | 82 (14 + 60 + 8) | 16 |
| smallest record | eth, 14 B | status, 14 B |
| largest record | ipv4 w/ options, 60 B | imbalance, 32 B |
| length/type prefix | — | 3 B |

## M2 — hand-written RTL, `eth_ipv4_udp`

Measured 2026-08-19. Verilator 5.032, cocotb 2.0.1, 2 ns clock (so cycles = ns/2).

### Latency — measured, not assumed

| Path | Cycles behind the ingress beat that completed it |
|---|---|
| record bus (`o_rec_valid` + all fields) | **2**, every packet, both widths, no exceptions |
| payload beat, steady state | **2** |
| payload tail beat (unaligned payload end) | **3**, at most one per packet |
| `o_empty` / `o_strip_err` per-packet marker | **3**, aligned with where a tail beat would be |

Asserted continuously, per beat, in every integration test — see
`check_record_latency` and `check_payload_latency` in
`tb/integration/test_parser_eth.py`. Why the payload tail is 3 rather than 2 is
`docs/decisions/0003-no-backpressure-fixed-schedule.md`; it is arithmetic, not
slack.

`s_axis_tready` fell on **0** cycles across every test at both widths.

### Verification volume

| Quantity | `DATA_W=64` | `DATA_W=128` |
|---|---|---|
| Directed corpus packets | 1,000 | 1,000 |
| Directed corpus bytes | 139,329 | 139,329 |
| Record field comparisons vs golden | 18,982 | 18,982 |
| Payload beats checked | 11,323 | 5,899 |
| ...of which tail beats at +3 | 497 | 439 |
| Mismatches | **0** | **0** |

Plus, at each width: 200 random packets with 35% `tvalid` gap probability and a
3-cycle inter-packet gap; 200 minimum-size (42-byte, zero-payload) frames
back to back with no idle; 14 malformed frames.

### Suites

| Suite | Tests | DATA_W | Sim time |
|---|---|---|---|
| `axis_reg_slice` | 6 | 64, 128 | 10,136 / 6,710 ns |
| `pkt_align` | 8 | 64, 128 | 5,764 / 3,306 ns |
| `payload_window` | 8 | 64, 128 | 6,492 / 6,618 ns |
| `parser_eth` | 5 | 64, 128 | 49,246 / 25,932 ns |

**54 cocotb tests across 8 suite runs, all passing. 57,102 simulated cycles.**

`payload_window`'s `test_every_strip_alignment` sweeps every strip length from 0
to `3*KEEP_W` against 8 packet lengths — 200 directed alignment cases at
`DATA_W=64`.

### Lint

`verilator --lint-only -Wall`, **zero warnings**, on 5 targets × 4 widths
(64/128/256/512):

`axis_reg_slice`, `pkt_align`, `hdr_accum`, `payload_window`,
`parser_top_eth_ipv4_udp`. Each library module is linted standalone as well as
through the top, so a warning cannot be masked by a parent pinning its
parameters.

```
cd ~/wirespec
./ci/lint.sh                    # all targets, all widths
make sim                        # all cocotb suites
make sim SIM_ARGS="-k parser_eth --width 64"
./ci/check.sh                   # the whole gate
```

### Defects found

4 (`B002`–`B005`), all in the byte-rotation and packet-boundary logic, all with
directed regressions. See `docs/bugs-found.md`.

Notably all four needed *sequences* of packets to reproduce: three required a
specific preceding packet, and one required zero idle cycles between two packets
of particular shapes. None would have been found by checking packets one at a
time.

## M3 — the generator

Measured 2026-08-19.

### The acceptance criterion, met exactly

The same test module (`tb/integration/test_parser_eth.py`) runs against the
hand-written M2 prototype and against the generated RTL, as two suites. The
results are not merely both-passing but **numerically identical**, including
simulation time:

| | hand-written | generated |
|---|---|---|
| `DATA_W=64` | 1,000 pkts, 18,982 field comparisons, 11,323 payload beats (497 tails), 35,810 ns | *identical* |
| `DATA_W=128` | 1,000 pkts, 18,982 field comparisons, 5,899 payload beats (439 tails), 18,418 ns | *identical* |

Identical sim time means the generated parser is cycle-for-cycle the same
machine, not merely a functionally equivalent one.

### Size

| | lines |
|---|---|
| `wirespec/templates/*.j2` | 419 |
| generated RTL (`DATA_W=64`) | 602 |
| ...`eth_ipv4_udp_pkg.sv` | 96 |
| ...`hdr_parse_eth_ipv4_udp.sv` | 285 |
| ...`parser_top_eth_ipv4_udp.sv` | 221 |

`hdr_parse` contains **one** run-time shifter, for the one layer (`udp`) whose
offset is not known at elaboration. A mux-per-field implementation would need
four. `tests/test_emit.py::test_one_shift_per_dynamic_layer_and_none_per_field`
asserts the count.

### Totals at M3

| Quantity | Value |
|---|---|
| pytest tests | 153 passed, 1.21 s |
| cocotb suite runs | 10 (was 8; the generated parser adds 2) |
| verilator lint targets | 6 × 4 widths = 24 invocations, **zero warnings** |
| `./ci/check.sh` | green |

```
make gen DATA_W=64          # emit into rtl/generated/
make gen-sample             # refresh the checked-in sample
python3 -m wirespec.cli info --schema schemas/simple_feed.yaml
WIRESPEC_UPDATE_SNAPSHOTS=1 python3 -m pytest tests/test_emit.py
```

## M4 — `simple_feed` end to end

Measured 2026-08-19.

### Sizing, from `wirespec/layout.py`

`MSG_MIN`=14, `MSG_MAX`=32, `SLOTS = ceil(KEEP_W / MSG_MIN)`,
`BUF_BYTES = MSG_MAX-1 + KEEP_W`.

| `DATA_W` | 64 | 128 | 256 | 512 |
|---|---|---|---|---|
| bytes per beat | 8 | 16 | 32 | 64 |
| SLOTS (= `msg_rotate` instances) | 1 | 2 | 3 | 5 |
| stitch buffer, bytes | 39 | 47 | 63 | 95 |
| message record bus, bits | 552 | 552 | 552 | 552 |

Record bus is 18 merged fields across 4 message types. `symbol` appears in three
types at the same offset and is one slice; **every** field is uniform, so
`field_extract_simple_feed.sv` contains **zero** muxes — asserted by
`tests/test_emit.py::test_field_extract_needs_no_muxes_for_simple_feed`.

### Multiple messages per beat, observed

`test_every_straddle_position`, histogram of messages accepted per cycle:

| `DATA_W` | messages/cycle |
|---|---|
| 64 | `{1: 81}` — SLOTS=1, and provably no cycle can do better |
| 128 | `{1: 197, 2: 12}` — the second slot fires |

At 8 bytes/beat a second message can never complete in one cycle; at 16 it can.
That is why `SLOTS` is `ceil(KEEP_W/MSG_MIN)` and not larger — see `B008`.

### Verification volume

| | `DATA_W=64` | `DATA_W=128` |
|---|---|---|
| Random packets vs golden | 300 | 300 |
| Random bytes | 40,115 | 40,115 |
| Messages decoded | 1,515 | 1,515 |
| Field comparisons | 9,803 | 9,803 |
| Mismatches | **0** | **0** |

Plus, at each width: every straddle alignment (0..`KEEP_W` padding messages
ahead of five messages of mixed type); message runs of 1/2/3/5/8/13/21/40;
120 random packets back to back with **zero** idle cycles; 80 with 35% `tvalid`
gaps; 13 defect classes each sandwiched between two well-formed packets; and
`stats.sv` compared field-for-field against `golden.StreamModel`.

`s_axis_tready` fell on **0** cycles. `msg_stitch`'s `o_overflow` asserted **0**
times outside the test that provokes it deliberately.

### Suites at M4

**16 cocotb suite runs, all passing.** New this milestone:

| Suite | Tests | Widths |
|---|---|---|
| `msg_rotate` | 4 | 64, 128 |
| `msg_stitch` | 7 | 64, 128 |
| `parser_feed` | 8 | 64, 128 |

`msg_rotate` sweeps every offset from 0 to `IN_BYTES` exhaustively; `msg_stitch`
runs 600 cycles of random beats against a Python byte-queue model.

### Size

| | lines |
|---|---|
| `msg_rotate.sv` | 42 |
| `msg_stitch.sv` | 100 |
| `msg_framer.sv` | 361 |
| `stats.sv` | 73 |
| `field_extract_simple_feed.sv` (generated) | 128 |
| `parser_top_simple_feed.sv` (generated) | 335 |

### Lint

`verilator --lint-only -Wall`, **zero warnings**, 10 targets × 4 widths.
`parser_top_simple_feed` is clean at 64/128/256/512.

### Totals at M4

| Quantity | Value |
|---|---|
| pytest tests | 169 passed, 1.32 s |
| cocotb suite runs | 16 |
| lint invocations | 40, zero warnings |
| `./ci/check.sh` | green |

## M5 — verification depth

Measured 2026-08-20. Every number below is also written to `metrics.json` by
`ci/check.sh` on each run, so this table can be checked against the tree rather
than trusted.

### Volume, per configuration

Reported per configuration, not summed: the same corpus at two widths is one
corpus tested twice, and adding the byte counts would double-count it.

| | `parser_eth` w64 | `parser_eth` w128 | `parser_feed` w64 | `parser_feed` w128 |
|---|---|---|---|---|
| bytes through the DUT | 1,270,214 | 1,270,214 | 1,174,255 | 1,179,915 |
| packets | 10,734 | 10,734 | 11,990 | 12,006 |
| messages | — | — | 41,855 | 42,119 |
| field comparisons vs golden | 201,322 | 201,322 | 271,795 | 273,411 |
| payload beats checked | 95,664 | 50,275 | — | — |
| **mismatches** | **0** | **0** | **0** | **0** |

`parser_eth` runs twice at each width — once against the hand-written M2
prototype, once against the generated RTL — and all four sets of numbers are
identical, which is the M3 acceptance criterion still holding at 30x the volume.

`s_axis_tready` fell on **0** cycles. `msg_stitch`'s `o_overflow` asserted **0**
times outside the test that provokes it deliberately.

### Beats per message (`simple_feed`)

Not messages per packet: how far a single message was spread across ingress
beats, which is what says whether the stitch buffer was exercised.

| `DATA_W` | spans observed | max |
|---|---|---|
| 64 | 2, 3, 4, 5 | **5** — the arithmetic maximum, `ceil((7 + 32) / 8)` |
| 128 | 1, 2, 3 | **3** — the arithmetic maximum, `ceil((15 + 32) / 16)` |

### Latency

One number per bus, asserted on every record rather than sampled. See
`docs/decisions/0003` for why the message bus is referenced to the beat *after*
the one that completes it.

| bus | reference beat | `DATA_W=64` | `DATA_W=128` |
|---|---|---|---|
| `eth` record | completing beat | 2 (× 10,734) | 2 (× 10,734) |
| `eth` payload beat | completing beat | 2 | 2 |
| `eth` payload tail | completing beat | 3 (4,346 of 95,664) | 3 (4,067 of 50,275) |
| `feed` header record | completing beat | 2 (× 11,981) | 2 (× 11,997) |
| `feed` message record | release beat | **3 (× 41,855)** | **3 (× 42,119)** |
| `feed` packet verdict | final ingress beat | 5 (× 11,990) | 5 (× 12,006) |

Constant, with the payload tail the one documented exception (ADR 0003), and that
one is arithmetic: the final ingress beat supplies the last byte of two output
beats.

### Coverage

Bins are generated from the schema by `tb/common/coverage.py` and declared before
any stimulus runs. Only reachable bins are declared, and reachability is
*computed*: a 32-byte message cannot span one 8-byte beat, and cannot reach a
high framer slot. Sampling an undeclared bin is an error, so the reachability
rule is checked rather than asserted.

| cross | axes | `DATA_W=64` | `DATA_W=128` |
|---|---|---|---|
| `type_x_lane` | message type × start byte lane | 32/32 | 64/64 |
| `type_x_beats` | message type × beats spanned | 7/7 | 8/8 |
| `type_x_slot` | message type × framer slot | 4/4 | 5/5 |
| `defect_x_locus` | defect class × where in the packet | 10/10 | 10/10 |
| `msgs_x_alignment` | message count × packet tail alignment | 10/10 | 10/10 |
| `field_value_class` | every settable field × value class | 72/72 | 72/72 |
| `ingress_shape` | tvalid gap × beat position | 6/6 | 6/6 |
| **total** | | **141/141 closed** | **175/175 closed** |

Two crosses needed directed stimulus rather than volume:

- `field_value_class` — random 64-bit values never land on `2**63-1`, so
  `feed_extreme_packets()` walks every field to both ends of its range and zero.
- `type_x_lane` — status messages are 14 bytes and `gcd(14, keep_w) = 2` for any
  power-of-two beat width, so padding with them only ever reaches the even lanes.
  `feed_lane_walk()` pads with 25-byte trades instead; 25 is odd, so
  `25a mod keep_w` walks all of them. Random traffic closed this at 64 and left
  four lanes open at 128.

`type_x_slot` at 128 declares 5 bins, not 8. Slot 1 can only hold a message small
enough that `S + (s-1)*MSG_MIN < KEEP_W`, which at 16 bytes per beat means status
alone. Declaring the other three would have left the cross permanently open
at 62%.

### Malformed input

| protocol | classes |
|---|---|
| `simple_feed` | **31** (13 at M4) |
| `eth_ipv4_udp` | 14 |

The `simple_feed` corpus is `tb.common.stimulus.malformed_feed_corpus()`, so the
number in `metrics.json` is the length of that list rather than a count kept in
step by hand. It covers truncation at three depths in the header and three in a
message, six length-prefix disagreements, zero and undersized lengths, two
lengths beyond anything buildable, six unknown type codes, four count-field
disagreements, and three trailing-fragment lengths — each class both as a
packet's first message and after a good one. Plus `test_misplaced_tlast`, which
drives raw beats: `tlast` on the first beat, a packet with no `tlast` at all, and
`tlast` mid-header.

Every case is asserted to be malformed *by the model* before the RTL's verdict is
compared, so a fixture that quietly became well-formed fails rather than passing
vacuously.

### Mutation testing

Ten single-line defects, applied one at a time to a clean tree, each with the
suite rerun against it. **10/10 killed.**

| mutant | layer | killed by |
|---|---|---|
| one field's byte offset moved by one | `ir.py` | 57 pytest objectors |
| one field flipped to little-endian | `ir.py` | 20, pytest + sim |
| length-prefix convention inverted | `ir.py` | 70, pytest + sim |
| unsigned fields sign-extended | `golden.py` | 30, pytest + sim |
| `msg_stitch` drops its carry | RTL | 17, sim |
| `msg_rotate` off by one lane | RTL | 19, sim |
| bytes counted from an invalid beat | RTL | 6, sim |
| message bus taken combinationally (undo B006) | template | 15, pytest + sim |
| runt verdict a cycle early (undo B010) | template | 7, sim |
| tail classifier stops faulting (undo B009) | RTL | 2, sim |

Two decisions make the number mean something:

- **Snapshot tests are deselected during mutation runs.** A snapshot fails on any
  change to the emitted text, so it would kill every generator or template
  mutant — including one that changed only whitespace. Counting those would
  report a far stronger testbench than exists. With them out, a mutant only dies
  if a check with an opinion about *correctness* objects.
- **Every stage runs even after one objects.** Otherwise "the RTL comparison
  never got a chance" is indistinguishable from "the RTL comparison did not
  care". That is how it came out that the B006 mutant is caught behaviourally as
  well as by the structural check written for it.

The first version of the length-convention mutant died to a single test, which
turned out to say something about the code rather than about the suite:
`IR.expected_length_value` is reached only from tests — the decoder and the
layout both go through its inverse. The mutant was retargeted to the live path.

### Hand-authored vectors

`tests/test_vectors.py`: 99 checks over literal byte strings with hand-computed
expected values, importing neither the packet builder nor anything that would let
the schema define its own correctness.

This closes a gap that was narrower than it first looked. Every other decoder
test builds a packet with `tb.common.stimulus` and asks the model to recover it —
and the builder uses fixed `struct` format strings and never reads the IR, so it
was *already* an independent implementation. What it could not catch is the
schema and the builder being wrong the same way, because both were written from
one reading of the protocol.

Checked directly: each of the four generator-level mutants above is caught by
`tests/test_vectors.py` **alone**, with the rest of the suite deselected.

The file also carries byte maps — flip frame byte *n*, assert exactly the fields
that byte feeds changed. 42 parametrised cases across the two protocols. That
catches an offset that is right for one field and wrong for its neighbour, which
a per-field round trip cannot: a field read one byte early still round-trips if
the builder writes it one byte early too.

No defect was found by these vectors. Worth stating as a result rather than
omitting: the schemas, the IR and the builder agree with a byte-level reading of
both protocols, including sub-byte fields, a 13-bit field straddling a byte
boundary, and the length-prefix convention.

### Size, and the ratio

| | schema lines | generated RTL lines | ratio |
|---|---|---|---|
| `eth_ipv4_udp` | 71 | 610 (3 files) | **8.6x** |
| `simple_feed` | 92 | 832 (5 files) | **9.0x** |
| both | 163 | 1,442 | **8.8x** |

At `DATA_W=64`; the generated text does not grow with `DATA_W` since the modules
are parameterised. 8 hand-written modules in `rtl/common/` (1,156 lines) plus 2
in `rtl/handwritten/` kept as the M2 prototype; 6 generated modules from 5
templates (~500 lines of Jinja2).

### Totals at M5

| Quantity | Value |
|---|---|
| pytest tests | 275 passed, ~2 s |
| cocotb tests | 124, over 16 suite runs |
| lint invocations | 40, **0 warnings** |
| bytes through the DUT | 1.17–1.27 M **per configuration**, 6 configurations |
| mismatches | **0** |
| coverage | 141/141 and 175/175 bins, closed |
| mutants killed | 10/10 |
| `./ci/check.sh` | green, 2 m 28 s (3 m 56 s with `--mutate`) |

```
./ci/check.sh                          # the gate; writes metrics.json
./ci/check.sh --mutate                 # and the mutation run
python3 ci/mutate.py --list            # what the mutants are
WIRESPEC_STRESS_BYTES=20000 make sim   # short loop while editing
cat metrics.json
```

## Defects found

See `docs/bugs-found.md`. Eleven so far.

| milestone | defects | what they were about |
|---|---|---|
| M1 | 1 | `B001`, an invalid-schema fixture testing the wrong rule |
| M2 | 4 | `B002`–`B005`, byte rotation and packet boundaries |
| M3 | 0 | — |
| M4 | 3 | `B006`–`B008`, the framed pipeline |
| M5 | 3 | `B009`–`B011`, packet-end classification |

By the file each defect lived in:

| file | defects |
|---|---|
| `rtl/common/msg_framer.sv` | 4 |
| `rtl/common/payload_window.sv` | 3 |
| `wirespec/templates/parser_top_framed.sv.j2` | 2 |
| `rtl/handwritten/hdr_parse_eth_ipv4_udp.sv` | 1 |
| schema fixture | 1 |

Two patterns account for most of them, and both are worth naming:

- **A value read one cycle before the register holding it is updated** — `B002`,
  `B003`, `B009`, `B011`. Four of eleven, in three modules, across three
  milestones.
- **Two events that should share a schedule landing a cycle apart** — `B004`,
  `B006`, `B010`. Three of eleven, and `B010` is `B004` one level up, in a module
  written after `B004` was fixed.

Every one of the eleven needed a *sequence* to reproduce: a specific preceding
packet, zero idle cycles, a particular beat alignment, or a defect placed after a
good message rather than first. None would have been found by checking packets
one at a time.
