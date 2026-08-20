// Carry register: hold the tail of a message across beat boundaries.
//
// The payload arrives KEEP_W bytes at a time; messages are 14 to 32 bytes and
// start wherever the previous one ended. This is the byte queue that makes those
// two facts independent of each other. Byte 0 of o_win is always the first byte
// the framer has not yet consumed, so a message start is at a known offset even
// though it arrived split across beats.
//
// The window is *live*: the beat arriving this cycle is merged in
// combinationally, so a message whose last byte is in this beat is extractable
// in this cycle rather than the next. That is what keeps the message latency a
// constant.
//
// Capacity. MSG_MAX-1 is the most that can be left over after a cycle -- what
// remains is always one partial message -- and one beat can add KEEP_W more, so
// BUF_BYTES = MSG_MAX-1 + KEEP_W is exactly enough and is checked by o_overflow.

`timescale 1ns / 1ps
`default_nettype none

module msg_stitch #(
  parameter int DATA_W    = 64,
  parameter int MSG_MAX   = 32,
  // Derived. Do not override.
  parameter int KEEP_W    = DATA_W / 8,
  parameter int BUF_BYTES = MSG_MAX - 1 + KEEP_W,
  parameter int BUF_BITS  = BUF_BYTES * 8,
  parameter int OFF_W     = $clog2(BUF_BYTES + 1),
  parameter int BCNT_W    = $clog2(DATA_W / 8 + 1)
) (
  input  wire                clk,
  input  wire                rst_n,

  // Payload stream from payload_window.
  input  wire                i_valid,
  input  wire [DATA_W-1:0]   i_data,
  input  wire                i_sof,
  input  wire                i_eof,
  input  wire [BCNT_W-1:0]   i_bytes,
  input  wire                i_empty,    // packet carried no payload at all

  // Back-pressure-free feedback from msg_framer: bytes retired this cycle.
  input  wire [OFF_W-1:0]    i_consume,

  output logic [BUF_BITS-1:0] o_win,     // byte 0 = first unconsumed payload byte
  output logic [OFF_W-1:0]    o_nvalid,
  output logic                o_pkt_start,  // a packet's message region begins now
  output logic                o_pkt_last,   // its final bytes are in o_win now
  output logic                o_overflow    // capacity exceeded: should be impossible
);

  localparam int SH_W = $clog2(BUF_BITS);

  logic [BUF_BITS-1:0] buf_q;
  logic [OFF_W-1:0]    nvalid_q;

  // A packet's first payload beat starts clean. Anything left over belonged to
  // the previous packet's truncated tail and has already been reported, so it
  // must not be carried across -- and the clear has to happen combinationally,
  // because the register that does it is updated by this same beat.
  logic             restart;
  logic [OFF_W-1:0] base;

  logic [BUF_BITS-1:0] beat_ext;
  logic [SH_W-1:0]     wr_shift;
  logic [SH_W-1:0]     rd_shift;

  always_comb begin
    restart  = (i_valid && i_sof) || i_empty;
    base     = restart ? OFF_W'(0) : nvalid_q;

    wr_shift = SH_W'({base, 3'b000});
    beat_ext = i_valid ? (BUF_BITS'(i_data) << wr_shift) : BUF_BITS'(0);

    // Live view, so a message completed by this beat is visible this cycle.
    o_win    = restart ? beat_ext : (buf_q | beat_ext);
    o_nvalid = base + (i_valid ? OFF_W'(i_bytes) : OFF_W'(0));

    o_pkt_start = (i_valid && i_sof) || i_empty;
    o_pkt_last  = (i_valid && i_eof) || i_empty;
    o_overflow  = i_valid && ((OFF_W + 1)'(base) + (OFF_W + 1)'(i_bytes)
                              > (OFF_W + 1)'(BUF_BYTES));

    // Retire the consumed bytes; what is left slides down to byte 0.
    rd_shift = SH_W'({i_consume, 3'b000});
  end

  always_ff @(posedge clk) begin
    if (!rst_n) begin
      buf_q    <= '0;
      nvalid_q <= '0;
    end else begin
      buf_q    <= o_win >> rd_shift;
      nvalid_q <= o_nvalid - i_consume;
    end
  end

endmodule

`default_nettype wire
