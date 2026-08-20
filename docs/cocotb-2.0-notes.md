# cocotb 2.0 notes

Running notes on where cocotb 2.0.1 differs from the 1.x API that most examples
online still use. Added to as differences are actually hit, not copied from
release notes.

Installed here: cocotb 2.0.1, Verilator 5.032, Icarus (iverilog), Python 3.14.4.

## API replacements used in this repo

| 1.x | 2.0 |
|---|---|
| `cocotb.fork(coro)` | `cocotb.start_soon(coro)` — `fork` is gone, not deprecated |
| `TestFactory(...)` + `.generate_tests()` | `@cocotb.parametrize(...)` |
| `@cocotb.coroutine` + `yield` | `async def` + `await` |
| `BinaryValue` | `LogicArray` (multi-bit) / `Logic` (one bit) |
| `raise TestFailure(...)` / `TestError` | plain `assert` |
| `Makefile.sim` include per testbench | `cocotb_tools.runner.get_runner(...)` |

## Entries

### 1. `Clock(...)` takes `unit`, not `units`

```python
Clock(dut.clk, 2, unit="ns")     # 2.0
Clock(dut.clk, 2, units="ns")    # 1.x -- the keyword still exists but only
                                 # accepts None, so passing a string fails
```

### 2. A one-bit signal reads back as `Logic`, which has no `to_unsigned()`

This is the first thing that broke. `LogicArray` grew `to_unsigned()` /
`to_signed()` and deprecated `.integer`, so the obvious port of 1.x code is
`handle.value.to_unsigned()`. That works for `[N-1:0]` ports and raises
`AttributeError: 'Logic' object has no attribute 'to_unsigned'` for scalar ones.

`tb/common/axis_monitor.py:u()` handles both, and deliberately still raises on an
unresolvable value — an X silently read as 0 turns a reset bug into a passing
test:

```python
v = handle.value
to_unsigned = getattr(v, "to_unsigned", None)
if to_unsigned is not None:
    if not v.is_resolvable:
        raise AssertionError(...)
    return to_unsigned()
return int(v)          # Logic; raises ValueError on x/z
```

### 3. `LogicArray` has `from_bytes` / `to_bytes`

Not needed here — packing a beat is `int.from_bytes(chunk, "little")`, since byte
*k* of a beat rides lane *k* — but worth knowing it exists.

### 4. `cocotb_tools.runner`, not `cocotb.runner`

The Python build/run API moved package. `import cocotb.runner` is a
`ModuleNotFoundError` in 2.0.1; it is `cocotb_tools.runner`. There is no
`Simulator` class — `get_runner(name)` returns a `Runner`, whose methods are
`build(...)` and `test(...)`.

### 5. `get_results()` returns `(total, failed)`

Not `(passed, failed)`. Reading it as the latter reports "6 passed, 6 failed" for
a run where everything failed, which is exactly the sort of green-looking output
that hides a broken suite.

### 6. Sampling side matters, and the two directions are opposite

Not an API change, but the thing that cost the most time. Everything in
`tb/common/` follows one rule:

- **Drivers** must see the value the DUT sampled, i.e. *before* the edge:
  `write signals -> await ReadOnly() -> read tready -> await RisingEdge()`.
  Doing edge-first drops a beat whenever `tready` falls on that edge.
- **Monitors** want the *post*-edge value of a registered output:
  `await RisingEdge() -> await ReadOnly() -> read`.

`tb/unit/test_axis_reg_slice.py` at 25% `tready` is the self-check on the driver
half: get the sampling side wrong and the reassembled packets come back short.

### 7. `start_soon` does not start the task until the caller yields

A monitor started with `start_soon` has not yet reached its first
`await RisingEdge` when the caller returns. If the driver then writes its first
beat in that same timestep, the monitor misses it, and every cycle number
afterwards is off by one — which surfaces as a "latency is not constant" failure
that has nothing to do with the design. `setup()` in
`tb/integration/test_parser_eth.py` yields one edge after `start_soon` so that
all coroutines are parked on the same edge.

### 8. `@cocotb.test(timeout_time=..., timeout_unit=...)` is worth setting always

A driver waiting on a `tready` that will never rise advances simulation time
forever rather than deadlocking, so it never trips a wall-clock timeout. Every
test in this repo carries a sim-time timeout for that reason.
