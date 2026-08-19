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

## M2 — hand-written RTL

*(not yet measured)*

## Defects found

See `docs/bugs-found.md`.
