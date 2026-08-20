// Barrel rotator: bring the byte at i_off down to lane 0.
//
// This is the module the whole message datapath is built around. A message
// starts at an arbitrary byte inside the stitch buffer, and every one of its
// fields would otherwise need its own mux across every possible lane. Instead
// one shifter per *message slot* re-bases the window so the message begins at
// bit 0, and field_extract then reads every field as a static slice.
//
// Cost scales with messages-per-beat, not with fields-per-message: simple_feed
// has 18 fields across four types and needs SLOTS rotators, not 18 muxes.
// See docs/decisions/0001-rotate-then-slice.md.
//
// Purely combinational. Bytes past the end of the input read as zero, which is
// harmless: msg_framer only trusts the first i_total bytes, and it has already
// checked that many are present.

`timescale 1ns / 1ps
`default_nettype none

module msg_rotate #(
  parameter int IN_BYTES  = 39,
  parameter int OUT_BYTES = 32,
  // Derived. Do not override.
  parameter int OFF_W     = $clog2(IN_BYTES + 1)
) (
  input  wire [IN_BYTES*8-1:0]  i_win,
  input  wire [OFF_W-1:0]       i_off,
  output wire [OUT_BYTES*8-1:0] o_win
);

  // The shift amount must be exactly wide enough to index i_win, or verilator
  // reports a truncation and then flags the surplus bits as unused.
  localparam int SH_W = $clog2(IN_BYTES * 8);

  logic [SH_W-1:0] shamt;

  assign shamt = SH_W'({i_off, 3'b000});
  assign o_win = (OUT_BYTES*8)'(i_win >> shamt);

endmodule

`default_nettype wire
