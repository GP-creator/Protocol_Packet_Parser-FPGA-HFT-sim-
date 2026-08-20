// tkeep / tlast -> byte counts and packet framing.
//
// Turns a raw AXI4-Stream into the annotated beat the rest of the parser reads:
// how many bytes this beat carries, where its lane 0 sits in the packet, and
// whether it opens or closes one. One register stage; the ingress is never
// stalled, so there is no handshake to honour.
//
// Malformed framing (a hole in tkeep, an empty beat, a short beat that is not
// the last) is flagged rather than silently reinterpreted.

`timescale 1ns / 1ps
`default_nettype none

module pkt_align #(
  parameter int DATA_W = 64,
  parameter int LEN_W  = pkg_wirespec::WS_LEN_W,
  // Derived. Do not override.
  parameter int KEEP_W = DATA_W / 8,
  parameter int BCNT_W = $clog2(DATA_W / 8 + 1)
) (
  input  wire                clk,
  input  wire                rst_n,

  // Ingress. Never stalled: s_tready is tied high by the caller.
  input  wire                s_tvalid,
  input  wire [DATA_W-1:0]   s_tdata,
  input  wire [KEEP_W-1:0]   s_tkeep,
  input  wire                s_tlast,

  // Annotated beat, one cycle later.
  output logic               o_valid,
  output logic [DATA_W-1:0]  o_data,
  output logic [KEEP_W-1:0]  o_keep,
  output logic               o_sof,
  output logic               o_eof,
  output logic [BCNT_W-1:0]  o_bytes,      // valid bytes in this beat
  output logic [LEN_W-1:0]   o_offset,     // packet byte index of lane 0
  output logic [LEN_W-1:0]   o_pkt_bytes,  // total packet length; read at o_eof
  output logic               o_keep_err
);

  localparam int MAXK = pkg_wirespec::WS_MAX_KEEP_W;

  logic [MAXK-1:0]   keep_pad;
  logic [BCNT_W-1:0] nbytes;
  logic              contig;
  logic              full_keep;

  always_comb begin
    keep_pad             = '0;
    keep_pad[KEEP_W-1:0] = s_tkeep;
    nbytes               = BCNT_W'(pkg_wirespec::ws_keep_count(keep_pad));
    contig               = pkg_wirespec::ws_keep_contiguous(keep_pad);
    full_keep            = (s_tkeep == {KEEP_W{1'b1}});
  end

  // Bytes of this packet already seen, and whether the next beat opens one.
  logic             expect_sof;
  logic [LEN_W-1:0] byte_acc;
  logic [LEN_W-1:0] base;
  logic [LEN_W-1:0] next_acc;

  always_comb begin
    base     = expect_sof ? LEN_W'(0) : byte_acc;
    next_acc = base + LEN_W'(nbytes);
  end

  always_ff @(posedge clk) begin
    if (!rst_n) begin
      o_valid     <= 1'b0;
      o_data      <= '0;
      o_keep      <= '0;
      o_sof       <= 1'b0;
      o_eof       <= 1'b0;
      o_bytes     <= '0;
      o_offset    <= '0;
      o_pkt_bytes <= '0;
      o_keep_err  <= 1'b0;
      expect_sof  <= 1'b1;
      byte_acc    <= '0;
    end else begin
      o_valid <= s_tvalid;
      if (s_tvalid) begin
        o_data      <= s_tdata;
        o_keep      <= s_tkeep;
        o_sof       <= expect_sof;
        o_eof       <= s_tlast;
        o_bytes     <= nbytes;
        o_offset    <= base;
        o_pkt_bytes <= next_acc;
        // An empty beat, a hole in tkeep, or a short beat that is not the last
        // one: all three would corrupt every offset downstream of here.
        o_keep_err  <= (nbytes == BCNT_W'(0)) || !contig || (!s_tlast && !full_keep);

        byte_acc   <= next_acc;
        expect_sof <= s_tlast;
      end
    end
  end

endmodule

`default_nettype wire
