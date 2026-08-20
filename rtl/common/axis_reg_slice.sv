// Fully registered AXI4-Stream pipeline stage.
//
// Every output -- tvalid, tdata and tready alike -- comes straight off a flop,
// so this breaks a combinational path in both directions. Two storage slots
// (payload + skid) are what it costs to do that without losing a beat when the
// downstream stalls, and they are what lets the stage still sustain one beat
// per cycle when it does not.
//
// The parser core never backpressures (see docs/decisions/0003), so this module
// is not in its datapath. It is here for the stream boundaries around it, and
// it is where the backpressure testing lands.

`timescale 1ns / 1ps
`default_nettype none

module axis_reg_slice #(
  parameter int DATA_W = 64,
  parameter int USER_W = 1,
  // Derived. Do not override.
  parameter int KEEP_W = DATA_W / 8,
  parameter int PAY_W  = DATA_W + KEEP_W + 1 + USER_W
) (
  input  wire               clk,
  input  wire               rst_n,

  input  wire               s_tvalid,
  output wire               s_tready,
  input  wire [DATA_W-1:0]  s_tdata,
  input  wire [KEEP_W-1:0]  s_tkeep,
  input  wire               s_tlast,
  input  wire [USER_W-1:0]  s_tuser,

  output wire               m_tvalid,
  input  wire               m_tready,
  output wire [DATA_W-1:0]  m_tdata,
  output wire [KEEP_W-1:0]  m_tkeep,
  output wire               m_tlast,
  output wire [USER_W-1:0]  m_tuser
);

  logic [PAY_W-1:0] s_pay;
  logic [PAY_W-1:0] pay_q;
  logic [PAY_W-1:0] skid_q;
  logic             pay_v;
  logic             skid_v;

  assign s_pay = {s_tuser, s_tlast, s_tkeep, s_tdata};

  // Registered in both directions: skid_v and pay_v are flops.
  assign s_tready = !skid_v;
  assign m_tvalid = pay_v;
  assign {m_tuser, m_tlast, m_tkeep, m_tdata} = pay_q;

  always_ff @(posedge clk) begin
    if (!rst_n) begin
      pay_v  <= 1'b0;
      skid_v <= 1'b0;
      pay_q  <= '0;
      skid_q <= '0;
    end else if (!skid_v) begin
      if (pay_v && !m_tready) begin
        // Output is stalled and already holds a beat: park the new one.
        if (s_tvalid) begin
          skid_q <= s_pay;
          skid_v <= 1'b1;
        end
      end else begin
        // Output slot is free, or frees this cycle.
        pay_v <= s_tvalid;
        if (s_tvalid) begin
          pay_q <= s_pay;
        end
      end
    end else if (m_tready) begin
      // Skid is full, so s_tready is low and nothing new can arrive.
      pay_q  <= skid_q;
      pay_v  <= 1'b1;
      skid_v <= 1'b0;
    end
  end

endmodule

`default_nettype wire
