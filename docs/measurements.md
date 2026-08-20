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

## Defects found

See `docs/bugs-found.md`.
