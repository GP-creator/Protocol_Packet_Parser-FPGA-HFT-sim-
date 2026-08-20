// Collect the first HDR_BYTES bytes of each packet into one flat window.
//
// Fixed-layout headers want to be read as static bit slices, and static slices
// want a register that does not move under them. This stage gathers the leading
// bytes of a packet into exactly that: byte k of the packet at o_win[8k+7:8k],
// regardless of DATA_W or which beat it arrived on.
//
// The window is presented *live*: the beat arriving this cycle is merged in
// combinationally rather than a cycle later. That matters because a wide
// datapath can deliver a discriminator byte and the first payload byte in the
// same beat, and payload_window needs the header length by then.

`timescale 1ns / 1ps
`default_nettype none

module hdr_accum #(
  parameter int DATA_W    = 64,
  parameter int HDR_BYTES = 82,
  parameter int LEN_W     = pkg_wirespec::WS_LEN_W,
  // Derived. Do not override.
  parameter int KEEP_W    = DATA_W / 8,
  parameter int BCNT_W    = $clog2(DATA_W / 8 + 1),
  parameter int ACC_BEATS = (HDR_BYTES + KEEP_W - 1) / KEEP_W,
  parameter int ACC_BYTES = ACC_BEATS * KEEP_W,
  parameter int ACC_BITS  = ACC_BYTES * 8
) (
  input  wire                 clk,
  input  wire                 rst_n,

  input  wire                 i_valid,
  input  wire [DATA_W-1:0]    i_data,
  input  wire                 i_sof,
  input  wire                 i_eof,
  input  wire [BCNT_W-1:0]    i_bytes,
  input  wire [LEN_W-1:0]     i_offset,

  output logic [ACC_BITS-1:0] o_win,    // packet byte k at bits [8k+7:8k]
  output logic [LEN_W-1:0]    o_have,   // header bytes visible in o_win
  output logic                o_done,   // window complete, or the packet ended
  output logic                o_short   // ... and it ended first
);

  // Accumulated beats of this packet, excluding the one arriving now.
  logic [ACC_BITS-1:0] win_q;
  logic [LEN_W-1:0]    have_q;
  logic                armed;  // this packet has not reported completion yet

  // Place the incoming beat at its byte offset. Every beat but the last is
  // full, so the offset is always a whole number of beats -- a small decoder,
  // not a barrel shifter.
  logic [ACC_BITS-1:0] beat_ext;

  always_comb begin
    beat_ext = '0;
    if (i_valid) begin
      for (int b = 0; b < ACC_BEATS; b++) begin
        if (i_offset == LEN_W'(b * KEEP_W)) begin
          beat_ext[b*DATA_W +: DATA_W] = i_data;
        end
      end
    end
  end

  logic [LEN_W-1:0] have_live;

  always_comb begin
    o_win = (i_valid && i_sof) ? beat_ext : (win_q | beat_ext);
    have_live = i_valid ? (i_offset + LEN_W'(i_bytes)) : have_q;
    o_have = have_live;
    o_done = i_valid && armed && ((have_live >= LEN_W'(HDR_BYTES)) || i_eof);
    o_short = o_done && (have_live < LEN_W'(HDR_BYTES));
  end

  always_ff @(posedge clk) begin
    if (!rst_n) begin
      win_q  <= '0;
      have_q <= '0;
      armed  <= 1'b1;
    end else if (i_valid) begin
      win_q  <= o_win;
      have_q <= have_live;
      if (i_eof) begin
        // Re-arm for the next packet even if this one completed early.
        armed <= 1'b1;
      end else if (o_done) begin
        armed <= 1'b0;
      end
    end
  end

endmodule

`default_nettype wire
