# Protocol Parser Generator (HFT sim)

Describe a network or market-data protocol in a short YAML file, and this tool
generates the FPGA hardware that decodes it at line rate, along with the tests
that prove the hardware is correct.

## The idea, in plain terms

Trading firms receive market data (trades, quotes, order-book updates) as a
constant stream of network packets. The faster a system can pull the useful
fields out of those packets, the sooner it can act on them. For the lowest
latency, that decoding is done in an **FPGA**: a chip whose circuits are
designed for one job, instead of software running on a CPU.

Hand-writing that hardware for every protocol is slow and error-prone. This
project automates it:

1. You describe the packet format (which fields exist, how many bits each one
   has, how layers nest) in about 70–90 lines of YAML.
2. A Python generator turns that description into roughly 9× as much
   SystemVerilog hardware code.
3. A test suite pushes more than a million random and hand-crafted packets
   through the generated hardware in simulation. It checks every field against
   an independent software model.

The result decodes a packet in a **fixed 2 clock cycles (8 ns at 250 MHz)**. It
never slows down or refuses data, so a new chunk of data can enter on every
clock cycle.

## Headline numbers

| | |
|---|---|
| Decode latency | 2 clock cycles, constant: the same for every packet, never data-dependent |
| Throughput | one 64–512-bit word per cycle; the input is never stalled |
| Clock speed | **253 MHz** on a Xilinx Artix-7 (xc7a35t-1), up from 53 MHz ([timing-closure write-up](docs/timing-closure.md)) |
| Size | ~1,500 LUTs and ~1,470 flip-flops, about 7% of that small FPGA |
| Verification | ~1.2 MB of traffic per configuration, **0 mismatches**; functional coverage closed; 10/10 injected bugs caught |
| Bugs found and fixed | 11, each with a regression test ([bugs-found.md](docs/bugs-found.md)) |

## What it parses

Two protocols are included, chosen to exercise different problems:

- **Ethernet / IPv4 / UDP** (`schemas/eth_ipv4_udp.yaml`). These are the layers
  every market-data packet arrives in. The IPv4 header has a variable length,
  so where the UDP header starts is only known once a field inside the packet
  has been read.
- **`simple_feed`** (`schemas/simple_feed.yaml`). A market-data-style feed:
  one packet carries many trade, quote, status and imbalance messages of
  different sizes. They are packed back to back, so a message can straddle two
  bus words, and several can finish in the same clock cycle.

Adding a protocol means writing a new YAML file; no hardware code changes.

## How it works (for the technical reader)

```
 YAML schema ──► wirespec (Python) ──► IR ──► Jinja2 templates ──► SystemVerilog
                        │
                        └──► golden model (Python) ──► cocotb testbench ◄── Verilator
```

- **Datapath.** AXI4-Stream in (`tdata`/`tkeep`/`tlast`), parameterised
  `DATA_W` = 64/128/256/512. `pkt_align` annotates each beat; `hdr_accum`
  gathers the header into a flat window; the generated `hdr_parse` slices
  fields with static bit selects plus one run-time shift per variable-offset
  layer. `payload_window` re-bases the payload onto lane 0 with a single funnel
  shift (rotate-then-slice; [ADR 0001](docs/decisions/0001-rotate-then-slice.md)).
  For framed feeds, `msg_stitch` / `msg_framer` split the payload into
  messages.
- **Fixed schedule, no backpressure.** `s_axis_tready` is tied high and there is
  no handshake anywhere inside, so latency is a function of packet geometry
  alone: record 2 cycles, payload 2 (tail beat 3), feed message 3, packet
  verdict 5. These numbers are asserted on every record, not sampled
  ([ADR 0003](docs/decisions/0003-no-backpressure-fixed-schedule.md)).
- **Error handling.** Truncated frames, out-of-range IPv4 IHL, length-prefix
  disagreements, unknown message types, count mismatches and misplaced `tlast`
  each produce a typed verdict. A packet that doesn't parse never emits payload
  bytes. Checksum fields are extracted but not verified.
- **Verification.** The golden model is independent of the generator
  ([ADR 0002](docs/decisions/0002-independent-golden-model.md)), and 99 checks
  compare it against hand-computed byte vectors. On top of that:
  - schema-derived functional coverage (141/141 and 175/175 bins);
  - 31 malformed-input classes;
  - mutation testing (10/10 killed);
  - hand-written vs generated RTL, compared cycle for cycle.

  Numbers are in [measurements.md](docs/measurements.md).
- **Timing closure.** Taken from 53 MHz to 253 MHz without adding a pipeline
  stage. Logic was moved across existing registers, and the arithmetic replaced
  with values the design's invariants already guarantee
  ([ADR 0004](docs/decisions/0004-timing-closure.md),
  [timing-closure.md](docs/timing-closure.md)).

## Repository layout

| Path | Contents |
|---|---|
| `schemas/` | protocol descriptions (plus deliberately invalid ones for the tests) |
| `wirespec/` | the generator: schema loader, IR, layout, Jinja2 templates, golden model, CLI |
| `rtl/common/` | hand-written reusable hardware blocks |
| `rtl/generated/sample/` | one checked-in example of generated output |
| `rtl/handwritten/` | the original hand-written parser the generator is checked against |
| `tb/` | cocotb testbenches, stimulus, coverage and scoreboards |
| `tests/` | pytest suite for the generator and golden model |
| `synth/` | Vivado out-of-context synthesis/implementation script |
| `ci/` | lint, full check and mutation-testing scripts |
| `docs/` | measurements, bug log, design decisions (ADRs), timing closure |

## Running it

Requires Python ≥ 3.11 (`PyYAML`, `Jinja2`, `cocotb`), Verilator 5.x, and
optionally Vivado for synthesis.

```bash
make gen DATA_W=64      # generate RTL for both protocols into rtl/generated/
make test               # pytest: generator and golden model
make lint               # verilator --lint-only -Wall, every module, 4 widths
make sim                # all cocotb testbenches
./ci/check.sh           # the whole gate; writes metrics.json

python3 -m wirespec.cli info --schema schemas/simple_feed.yaml   # inspect a schema
```

The Python package and CLI are called `wirespec`, the project's working name.
