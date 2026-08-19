# cocotb 2.0 notes

Running notes on where cocotb 2.0.1 differs from the 1.x API that most examples
online still use. Added to as differences are actually hit, not copied from
release notes.

Installed here: cocotb 2.0.1, Verilator 5.032, Icarus (iverilog), Python 3.14.4.

## API replacements used in this repo

| 1.x | 2.0 |
|---|---|
| `cocotb.fork(coro)` | `cocotb.start_soon(coro)` |
| `TestFactory(...)` + `.generate_tests()` | `@cocotb.parametrize(...)` |
| `@cocotb.coroutine` + `yield` | `async def` + `await` |
| `BinaryValue` | `LogicArray` / `Logic` |
| `raise TestFailure(...)` / `TestError` | plain `assert` |
| `cocotb.result.TestSuccess` | `return` from the test |

## Entries

*(No differences recorded yet — M1 is Python-only. Entries land from M2, when
the first testbench runs.)*
