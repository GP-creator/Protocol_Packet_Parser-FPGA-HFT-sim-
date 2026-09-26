// Collect the first HDR_BYTES bytes of each packet into one flat window.
//
// Fixed-layout headers want to be read as static bit slices, and static slices
// want a register that does not move under them. This stage gathers the leading
// bytes of a packet into exactly that: byte k of the packet at o_win[8k+7:8k],
// regardless of DATA_W or which beat it arrived on.
//
// The window includes the beat pkt_align is presenting in the same cycle. That
// matters because a wide datapath can deliver a discriminator byte and the first
// payload byte in the same beat, and payload_window needs the header length by
// then. It is achieved by merging the beat on the *ingress* side of the edge that
// pkt_align registers it on, using pkt_align's pre-register annotation, so every
// output here is a flop and the header parser starts its cycle from registers.
//
// Byte counts are kept as a thermometer, o_have_ge[n] == (bytes seen >= n). A
// beat of n bytes shifts it up by n, which is a small mux rather than an adder
// followed by a compare, and both "where does the next beat land" and "is the
// window complete" are single bits of it.

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

  // The ingress beat, and pkt_align's nx_* annotation of it.
  input  wire                 s_valid,
  input  wire [DATA_W-1:0]    s_data,
  input  wire                 s_last,
  input  wire                 nx_sof,
  input  wire [BCNT_W-1:0]    nx_bytes,
  input  wire [LEN_W-1:0]     nx_pkt_bytes,

  // Aligned with pkt_align's o_* for the same beat.
  output logic [ACC_BITS-1:0] o_win,     // packet byte k at bits [8k+7:8k]
  output logic [LEN_W-1:0]    o_have,    // header bytes visible in o_win
  output logic [HDR_BYTES:0]  o_have_ge, // o_have_ge[n] == (o_have >= n)
  output logic                o_done,    // window complete, or the packet ended
  output logic                o_short    // ... and it ended first
);

  localparam int GE_W = HDR_BYTES + 1;

  logic                 armed;   // this packet has not reported completion yet
  logic [ACC_BEATS-1:0] slot_q;  // slot_q[b]: the next beat starts at byte b*KEEP_W

  logic [GE_W-1:0]        ge_from;
  logic [GE_W+KEEP_W-1:0] ge_ext;
  logic [GE_W-1:0]        ge_nx;
  logic                   have_all;
  logic [ACC_BEATS-1:0]   slot_nx;
  logic [ACC_BITS-1:0]    beat_ext;
  logic [ACC_BITS-1:0]    win_nx;
  logic                   done_nx;

  always_comb begin
    // therm(x + n)[m] == therm(x)[m - n] for m >= n, and 1 below it. A packet's
    // first beat starts from therm(0), which is bit 0 alone.
    ge_from  = nx_sof ? GE_W'(1) : o_have_ge;
    ge_ext   = {ge_from, {KEEP_W{1'b1}}};
    ge_nx    = GE_W'(ge_ext >> (BCNT_W'(KEEP_W) - nx_bytes));
    have_all = ge_nx[HDR_BYTES];

    // Every slot offset b*KEEP_W is below HDR_BYTES, so b*KEEP_W + 1 is in range.
    for (int b = 0; b < ACC_BEATS; b++) begin
      slot_nx[b] = s_last ? (b == 0) : (ge_nx[b * KEEP_W] && !ge_nx[b * KEEP_W + 1]);
    end

    // Place the incoming beat at its byte offset. Every beat but the last is
    // full, so the offset is always a whole number of beats -- a small decoder,
    // not a barrel shifter.
    beat_ext = '0;
    for (int b = 0; b < ACC_BEATS; b++) begin
      if (slot_q[b]) begin
        beat_ext[b*DATA_W +: DATA_W] = s_data;
      end
    end

    win_nx = nx_sof ? beat_ext : (o_win | beat_ext);

    done_nx = s_valid && armed && (have_all || s_last);
  end

  always_ff @(posedge clk) begin
    if (!rst_n) begin
      o_win     <= '0;
      o_have    <= '0;
      o_have_ge <= GE_W'(1);
      o_done    <= 1'b0;
      o_short   <= 1'b0;
      armed     <= 1'b1;
      slot_q    <= ACC_BEATS'(1);
    end else begin
      o_done  <= done_nx;
      o_short <= done_nx && !have_all;
      if (s_valid) begin
        o_win     <= win_nx;
        o_have    <= nx_pkt_bytes;
        o_have_ge <= ge_nx;
        slot_q    <= slot_nx;
        if (s_last) begin
          // Re-arm for the next packet even if this one completed early.
          armed <= 1'b1;
        end else if (done_nx) begin
          armed <= 1'b0;
        end
      end
    end
  end

endmodule

`default_nettype wire
