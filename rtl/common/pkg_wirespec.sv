// Shared typedefs, enums and helpers.
//
// Nothing here is protocol-specific; the generator emits a second, per-protocol
// package (<proto>_pkg.sv) alongside this one.

`timescale 1ns / 1ps
`default_nettype none

package pkg_wirespec;

  // Widest datapath the generator will emit.  This only sizes the argument
  // vectors of the helper functions below -- no design is forced this wide.
  localparam int WS_MAX_DATA_W = 512;
  localparam int WS_MAX_KEEP_W = WS_MAX_DATA_W / 8;

  // Packet-length counter width. 16 bits covers 64 KiB, past any jumbo frame.
  localparam int WS_LEN_W = 16;

  // Decode outcome. The values mirror wirespec.golden.Defect one for one, so a
  // testbench can compare the RTL's code against the model's without a lookup
  // table that could itself be wrong.
  typedef enum logic [2:0] {
    WS_OK             = 3'd0,
    WS_KEEP_ERR       = 3'd1,
    WS_TRUNCATED      = 3'd2,
    WS_BAD_HDR_LEN    = 3'd3,
    WS_BAD_LENGTH     = 3'd4,
    WS_ZERO_LENGTH    = 3'd5,
    WS_UNKNOWN_TYPE   = 3'd6,
    WS_COUNT_MISMATCH = 3'd7
  } ws_err_e;

  // Number of asserted lanes in a keep mask. Lanes above the caller's actual
  // KEEP_W must be zero-padded, which makes the count come out right without
  // passing a width in.
  function automatic int ws_keep_count(input logic [WS_MAX_KEEP_W-1:0] keep);
    int n;
    n = 0;
    for (int i = 0; i < WS_MAX_KEEP_W; i++) begin
      if (keep[i]) begin
        n = n + 1;
      end
    end
    return n;
  endfunction

  // A packet-stream keep mask must be a run of ones starting at lane 0. A hole
  // in the middle is a protocol violation, not a narrow beat.
  function automatic bit ws_keep_contiguous(input logic [WS_MAX_KEEP_W-1:0] keep);
    bit seen_gap;
    bit ok;
    seen_gap = 1'b0;
    ok       = 1'b1;
    for (int i = 0; i < WS_MAX_KEEP_W; i++) begin
      if (!keep[i]) begin
        seen_gap = 1'b1;
      end else if (seen_gap) begin
        ok = 1'b0;
      end
    end
    return ok;
  endfunction

endpackage

`default_nettype wire
