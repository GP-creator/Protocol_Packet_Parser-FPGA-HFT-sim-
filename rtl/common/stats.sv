// Packet / message / sequence-gap / malformed counters.
//
// The software twin is wirespec.golden.StreamModel, and the rules are copied
// from it rather than invented here: the first packet of a stream is never a
// sequence gap, and a malformed packet is counted but does not advance the
// expected sequence number -- otherwise one bad packet would be reported as two
// gaps.
//
// Plain output ports, no register interface. A bus wrapper is somebody else's
// module.

`timescale 1ns / 1ps
`default_nettype none

module stats #(
  parameter int CNT_W   = 32,
  parameter int SEQ_W   = 64,
  parameter int MSG_W   = 16
) (
  input  wire               clk,
  input  wire               rst_n,
  input  wire               clear,

  // One pulse per packet, with that packet's verdict.
  input  wire               i_pkt_done,
  input  wire               i_pkt_bad,
  input  wire [MSG_W-1:0]   i_pkt_msgs,

  // Sequence number of the packet reported by i_pkt_done, when it has one.
  input  wire               i_seq_valid,
  input  wire [SEQ_W-1:0]   i_seq,

  output logic [CNT_W-1:0]  o_packets,
  output logic [CNT_W-1:0]  o_messages,
  output logic [CNT_W-1:0]  o_seq_gaps,
  output logic [CNT_W-1:0]  o_malformed
);

  logic [SEQ_W-1:0] expect_q;
  logic             have_expect_q;

  logic gap;
  assign gap = i_pkt_done && !i_pkt_bad && i_seq_valid && have_expect_q &&
               (i_seq != expect_q);

  always_ff @(posedge clk) begin
    if (!rst_n || clear) begin
      o_packets     <= '0;
      o_messages    <= '0;
      o_seq_gaps    <= '0;
      o_malformed   <= '0;
      expect_q      <= '0;
      have_expect_q <= 1'b0;
    end else if (i_pkt_done) begin
      o_packets  <= o_packets + CNT_W'(1);
      o_messages <= o_messages + CNT_W'(i_pkt_msgs);
      if (i_pkt_bad) begin
        o_malformed <= o_malformed + CNT_W'(1);
      end else begin
        if (gap) begin
          o_seq_gaps <= o_seq_gaps + CNT_W'(1);
        end
        if (i_seq_valid) begin
          expect_q      <= i_seq + SEQ_W'(1);
          have_expect_q <= 1'b1;
        end
      end
    end
  end

endmodule

`default_nettype wire
