// Top level: AXI4-Stream in, one header record per packet plus a lane-0-aligned
// payload stream out. Hand-written prototype for the M3 template.
//
// Two properties this module promises, both asserted continuously by the
// testbench (tb/integration/test_parser_eth.py):
//
//   1. s_axis_tready is tied high. The parser never backpressures its ingress.
//   2. The payload stream is exactly PAY_LATENCY cycles behind the ingress beat
//      that completes it, for every packet, every alignment and every DATA_W.
//
// Both fall out of the structure rather than being arranged for: there is no
// handshake anywhere inside, and no stage's depth depends on the data.

`timescale 1ns / 1ps
`default_nettype none

module parser_top_eth_ipv4_udp #(
  parameter int DATA_W = 64,
  parameter int LEN_W  = pkg_wirespec::WS_LEN_W,
  // Derived. Do not override.
  parameter int KEEP_W = DATA_W / 8,
  parameter int BCNT_W = $clog2(DATA_W / 8 + 1)
) (
  input  wire                clk,
  input  wire                rst_n,

  input  wire                s_axis_tvalid,
  output wire                s_axis_tready,
  input  wire [DATA_W-1:0]   s_axis_tdata,
  input  wire [KEEP_W-1:0]   s_axis_tkeep,
  input  wire                s_axis_tlast,

  // Payload stream. Valid-only: nothing downstream may stall the parser.
  output wire                m_pay_valid,
  output wire [DATA_W-1:0]   m_pay_data,
  output wire [KEEP_W-1:0]   m_pay_keep,
  output wire                m_pay_sof,
  output wire                m_pay_eof,
  output wire [BCNT_W-1:0]   m_pay_bytes,
  output wire [LEN_W-1:0]    m_pay_offset,
  output wire                m_pay_empty,

  // One record per packet.
  output wire                o_rec_valid,
  output wire [2:0]          o_layer_valid,
  output wire [2:0]          o_err,
  output wire [LEN_W-1:0]    o_hdr_bytes,
  output wire                o_keep_err,
  output wire                o_strip_err,

  output wire [47:0]         o_eth_dst_mac,
  output wire [47:0]         o_eth_src_mac,
  output wire [15:0]         o_eth_ethertype,

  output wire [3:0]          o_ipv4_version,
  output wire [3:0]          o_ipv4_ihl,
  output wire [5:0]          o_ipv4_dscp,
  output wire [1:0]          o_ipv4_ecn,
  output wire [15:0]         o_ipv4_total_length,
  output wire [15:0]         o_ipv4_identification,
  output wire [2:0]          o_ipv4_flags,
  output wire [12:0]         o_ipv4_frag_offset,
  output wire [7:0]          o_ipv4_ttl,
  output wire [7:0]          o_ipv4_protocol,
  output wire [15:0]         o_ipv4_hdr_checksum,
  output wire [31:0]         o_ipv4_src_ip,
  output wire [31:0]         o_ipv4_dst_ip,

  output wire [15:0]         o_udp_src_port,
  output wire [15:0]         o_udp_dst_port,
  output wire [15:0]         o_udp_length,
  output wire [15:0]         o_udp_checksum
);

  // Worst-case chain: 14 eth + 60 ipv4-with-options + 8 udp.
  localparam int HDR_BYTES = 82;
  localparam int WIN_BITS  = HDR_BYTES * 8;

  // Payload latency is pkt_align (1) + payload_window (1) = 2 cycles. It is not
  // a constant here on purpose: the testbench asserts the number it observes
  // against its own, so a structural change that alters the depth fails a test
  // rather than quietly updating a parameter nobody reads.

  assign s_axis_tready = 1'b1;

  // ------------------------------------------------------------ alignment --
  logic              al_valid;
  logic [DATA_W-1:0] al_data;
  logic [KEEP_W-1:0] al_keep;
  logic              al_sof;
  logic              al_eof;
  logic [BCNT_W-1:0] al_bytes;
  logic [LEN_W-1:0]  al_offset;
  logic [LEN_W-1:0]  al_pkt_bytes;
  logic              al_keep_err;

  pkt_align #(
    .DATA_W(DATA_W),
    .LEN_W (LEN_W)
  ) u_align (
    .clk        (clk),
    .rst_n      (rst_n),
    .s_tvalid   (s_axis_tvalid),
    .s_tdata    (s_axis_tdata),
    .s_tkeep    (s_axis_tkeep),
    .s_tlast    (s_axis_tlast),
    .o_valid    (al_valid),
    .o_data     (al_data),
    .o_keep     (al_keep),
    .o_sof      (al_sof),
    .o_eof      (al_eof),
    .o_bytes    (al_bytes),
    .o_offset   (al_offset),
    .o_pkt_bytes(al_pkt_bytes),
    .o_keep_err (al_keep_err)
  );

  // ------------------------------------------------------ header gathering --
  localparam int ACC_BEATS = (HDR_BYTES + KEEP_W - 1) / KEEP_W;
  localparam int ACC_BITS  = ACC_BEATS * KEEP_W * 8;

  logic [ACC_BITS-1:0] acc_win;
  logic [LEN_W-1:0]    acc_have;
  logic                acc_done;
  logic                acc_short;

  hdr_accum #(
    .DATA_W   (DATA_W),
    .HDR_BYTES(HDR_BYTES),
    .LEN_W    (LEN_W)
  ) u_accum (
    .clk     (clk),
    .rst_n   (rst_n),
    .i_valid (al_valid),
    .i_data  (al_data),
    .i_sof   (al_sof),
    .i_eof   (al_eof),
    .i_bytes (al_bytes),
    .i_offset(al_offset),
    .o_win   (acc_win),
    .o_have  (acc_have),
    .o_done  (acc_done),
    .o_short (acc_short)
  );

  // ------------------------------------------------------------- the parse --
  logic             strip_valid;
  logic [LEN_W-1:0] strip_bytes;

  hdr_parse_eth_ipv4_udp #(
    .LEN_W    (LEN_W),
    .HDR_BYTES(HDR_BYTES)
  ) u_parse (
    .clk                (clk),
    .rst_n              (rst_n),
    .i_win              (acc_win[WIN_BITS-1:0]),
    .i_have             (acc_have),
    .i_done             (acc_done),
    .o_strip_valid      (strip_valid),
    .o_strip_bytes      (strip_bytes),
    .o_rec_valid        (o_rec_valid),
    .o_layer_valid      (o_layer_valid),
    .o_err              (o_err),
    .o_hdr_bytes        (o_hdr_bytes),
    .o_eth_dst_mac      (o_eth_dst_mac),
    .o_eth_src_mac      (o_eth_src_mac),
    .o_eth_ethertype    (o_eth_ethertype),
    .o_ipv4_version       (o_ipv4_version),
    .o_ipv4_ihl           (o_ipv4_ihl),
    .o_ipv4_dscp          (o_ipv4_dscp),
    .o_ipv4_ecn           (o_ipv4_ecn),
    .o_ipv4_total_length  (o_ipv4_total_length),
    .o_ipv4_identification(o_ipv4_identification),
    .o_ipv4_flags         (o_ipv4_flags),
    .o_ipv4_frag_offset   (o_ipv4_frag_offset),
    .o_ipv4_ttl           (o_ipv4_ttl),
    .o_ipv4_protocol      (o_ipv4_protocol),
    .o_ipv4_hdr_checksum  (o_ipv4_hdr_checksum),
    .o_ipv4_src_ip        (o_ipv4_src_ip),
    .o_ipv4_dst_ip        (o_ipv4_dst_ip),
    .o_udp_src_port     (o_udp_src_port),
    .o_udp_dst_port     (o_udp_dst_port),
    .o_udp_length       (o_udp_length),
    .o_udp_checksum     (o_udp_checksum)
  );

  // ------------------------------------------------------------- payload ---
  payload_window #(
    .DATA_W(DATA_W),
    .LEN_W (LEN_W)
  ) u_payload (
    .clk          (clk),
    .rst_n        (rst_n),
    .i_valid      (al_valid),
    .i_data       (al_data),
    .i_sof        (al_sof),
    .i_eof        (al_eof),
    .i_bytes      (al_bytes),
    .i_offset     (al_offset),
    .i_strip_valid(strip_valid),
    .i_strip_bytes(strip_bytes),
    .o_valid      (m_pay_valid),
    .o_data       (m_pay_data),
    .o_keep       (m_pay_keep),
    .o_sof        (m_pay_sof),
    .o_eof        (m_pay_eof),
    .o_bytes      (m_pay_bytes),
    .o_offset     (m_pay_offset),
    .o_empty      (m_pay_empty),
    .o_strip_err  (o_strip_err)
  );

  assign o_keep_err = al_keep_err;

  // acc_short / al_keep / al_pkt_bytes are diagnostics the record does not
  // carry: truncation is already implied by acc_have at acc_done, and the keep
  // mask is already validated by pkt_align. Tie them off explicitly rather than
  // leaving dangling nets.
  logic unused_ok;
  assign unused_ok = &{1'b0, acc_short, al_keep, al_pkt_bytes,
                       acc_win[ACC_BITS-1:WIN_BITS]};

endmodule

`default_nettype wire
