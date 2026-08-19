# ADR 0002 — The golden model interprets the IR; it is not generated

**Status:** accepted (M1)

## Context

The RTL parser and the reference decoder both have to implement one
specification: the YAML schema. The cheap way to build the reference is to add a
`golden.py.j2` next to the SystemVerilog templates and render both from the same
loop over `IRField`. Then the testbench compares one rendering of a template
against another rendering of the same template.

That comparison passes unconditionally. If the template's endian handling is
inverted, both sides invert it and the scoreboard is silent. The only bugs such
a check can find are the ones in the surrounding plumbing — exactly the bugs
that are easiest to find by other means.

## Decision

`wirespec/golden.py` is hand-written and interprets the IR at runtime. It is
never generated, and it shares no rendering code with `wirespec/emit.py`.

Where the two must compute the same thing, they compute it differently on
purpose:

| | emitter (`emit.py`) | golden (`golden.py`) |
|---|---|---|
| field value | concatenates the static bit slices in `IRField.segments` | converts the touched byte span to one integer and shifts |
| endian swap | falls out of the segment order | falls out of `int.from_bytes` byte order |
| sign extension | `logic signed` declaration | explicit `raw -= 1 << width` |
| message sizing | comparators generated per type code | `ir.total_from_length()` then a table lookup |

`tests/test_ir.py::test_segment_view_matches_golden_field_read` pins the two
derivations against each other over random record contents, so a divergence is
caught in pytest rather than in a waveform.

There is a third independent implementation: the packet builders in
`tb/common/stimulus.py` are written from the protocol descriptions with
`struct`, and read neither the schema nor the IR. The M1 acceptance test drives
20,000 random packets through builder → golden and requires exact agreement.

## Consequences

- `golden.py` has to be maintained by hand whenever the schema language grows a
  feature. This is the cost, and it is the point: a feature that is not
  implemented twice is not verified.
- `IRField.segments` and `golden.read_field` can drift. The cross-check test
  above exists specifically to catch that, and must be kept passing.
- Schema-language features cannot be added by touching only a template. That
  friction is deliberate.
