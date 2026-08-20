// Ethernet / IPv4 / UDP header extraction. Hand-written prototype for the
// template that replaces it at M3 -- every construct here is one the generator
// can emit from the IR, and nothing here is hand-tuned in a way a template
// could not reproduce.
//
// Eth and IPv4 sit at compile-time offsets, so their fields are plain static
// slices of the accumulated window. UDP does not: it begins at 14 + IHL*4. One
// shared shift moves the window so UDP's byte 0 lands at bit 0, and UDP's four
// fields are then static slices too. That is rotate-then-slice at layer
// granularity (docs/decisions/0001-rotate-then-slice.md); the alternative is
// four independent 11-way muxes that all compute the same address.
//
// Error precedence deliberately mirrors wirespec/golden.py's _decode_layered:
// a per-layer truncation check, then the layer's own length check, then the
// next-layer selector. Getting a different answer than the model on a malformed
// packet is a bug even when both "report an error".

`timescale 1ns / 1ps
`default_nettype none

module hdr_parse_eth_ipv4_udp #(
  parameter int LEN_W     = 16,
  parameter int HDR_BYTES = 82,          // 14 + 60 + 8, the worst-case chain
  // Derived. Do not override.
  parameter int WIN_BITS  = HDR_BYTES * 8
) (
  input  wire                 clk,
  input  wire                 rst_n,

  // Live header window from hdr_accum.
  input  wire [WIN_BITS-1:0]  i_win,
  input  wire [LEN_W-1:0]     i_have,
  input  wire                 i_done,

  // Combinational header length, for payload_window. Valid as soon as enough
  // of the window has arrived to determine it -- byte 14 for a non-IPv4 frame,
  // byte 24 for an IPv4 one -- which is always before the first payload byte.
  output logic                o_strip_valid,
  output logic [LEN_W-1:0]    o_strip_bytes,

  // Record, registered, one pulse per packet.
  output logic                o_rec_valid,
  output logic [2:0]          o_layer_valid,   // {udp, ipv4, eth}
  output logic [2:0]          o_err,           // pkg_wirespec::ws_err_e
  output logic [LEN_W-1:0]    o_hdr_bytes,

  output logic [47:0]         o_eth_dst_mac,
  output logic [47:0]         o_eth_src_mac,
  output logic [15:0]         o_eth_ethertype,

  output logic [3:0]          o_ipv4_version,
  output logic [3:0]          o_ipv4_ihl,
  output logic [5:0]          o_ipv4_dscp,
  output logic [1:0]          o_ipv4_ecn,
  output logic [15:0]         o_ipv4_total_length,
  output logic [15:0]         o_ipv4_identification,
  output logic [2:0]          o_ipv4_flags,
  output logic [12:0]         o_ipv4_frag_offset,
  output logic [7:0]          o_ipv4_ttl,
  output logic [7:0]          o_ipv4_protocol,
  output logic [15:0]         o_ipv4_hdr_checksum,
  output logic [31:0]         o_ipv4_src_ip,
  output logic [31:0]         o_ipv4_dst_ip,

  output logic [15:0]         o_udp_src_port,
  output logic [15:0]         o_udp_dst_port,
  output logic [15:0]         o_udp_length,
  output logic [15:0]         o_udp_checksum
);

  import pkg_wirespec::*;

  localparam int ETH_BYTES     = 14;
  localparam int IP_FIXED      = 20;
  localparam int UDP_BYTES     = 8;
  localparam int IP_OFF        = ETH_BYTES;
  localparam int IP_MIN_BYTES  = IP_OFF + IP_FIXED;   // 34
  localparam int PROTO_BYTE    = IP_OFF + 9;          // 23
  localparam logic [15:0] ETHERTYPE_V  = 16'h0800;
  localparam logic [7:0]  IP_PROTO_UDP = 8'd17;

  // ------------------------------------------------------------ eth, ipv4 --
  // Static slices. Byte k of the packet is i_win[8k+7:8k]; a big-endian field
  // is that run of bytes concatenated with the first one most significant.
  logic [47:0] eth_dst_mac;
  logic [47:0] eth_src_mac;
  logic [15:0] eth_ethertype;

  assign eth_dst_mac   = {i_win[7:0],     i_win[15:8],   i_win[23:16],
                          i_win[31:24],   i_win[39:32],  i_win[47:40]};
  assign eth_src_mac   = {i_win[55:48],   i_win[63:56],  i_win[71:64],
                          i_win[79:72],   i_win[87:80],  i_win[95:88]};
  assign eth_ethertype = {i_win[103:96],  i_win[111:104]};

  logic [3:0]  ip_version;
  logic [3:0]  ip_ihl;
  logic [5:0]  ip_dscp;
  logic [1:0]  ip_ecn;
  logic [15:0] ip_total_length;
  logic [15:0] ip_identification;
  logic [2:0]  ip_flags;
  logic [12:0] ip_frag_offset;
  logic [7:0]  ip_ttl;
  logic [7:0]  ip_protocol;
  logic [15:0] ip_hdr_checksum;
  logic [31:0] ip_src_ip;
  logic [31:0] ip_dst_ip;

  // Sub-byte fields are MSB-first within their byte, so version is the *top*
  // nibble of byte 14 and IHL the bottom one.
  assign ip_version        = i_win[119:116];
  assign ip_ihl            = i_win[115:112];
  assign ip_dscp           = i_win[127:122];
  assign ip_ecn            = i_win[121:120];
  assign ip_total_length   = {i_win[135:128], i_win[143:136]};
  assign ip_identification = {i_win[151:144], i_win[159:152]};
  assign ip_flags          = i_win[167:165];
  // 13 bits: the low five of byte 20, then all of byte 21.
  assign ip_frag_offset    = {i_win[164:160], i_win[175:168]};
  assign ip_ttl            = i_win[183:176];
  assign ip_protocol       = i_win[191:184];
  assign ip_hdr_checksum   = {i_win[199:192], i_win[207:200]};
  assign ip_src_ip         = {i_win[215:208], i_win[223:216],
                              i_win[231:224], i_win[239:232]};
  assign ip_dst_ip         = {i_win[247:240], i_win[255:248],
                              i_win[263:256], i_win[271:264]};

  // ------------------------------------------------------------------ udp --
  // One shift shared by all four UDP fields. IHL is four bits and at least 5,
  // so the window can start at any of eleven 4-byte-aligned offsets from 34 to
  // 74; 74 + 8 = 82 = HDR_BYTES, so the widest select is still in range.
  logic [UDP_BYTES*8-1:0] udp_win;

  always_comb begin
    udp_win = '0;
    for (int h = 5; h <= 15; h++) begin
      if (ip_ihl == 4'(h)) begin
        udp_win = i_win[(IP_OFF + h*4)*8 +: UDP_BYTES*8];
      end
    end
  end

  logic [15:0] udp_src_port;
  logic [15:0] udp_dst_port;
  logic [15:0] udp_length;
  logic [15:0] udp_checksum;

  assign udp_src_port = {udp_win[7:0],   udp_win[15:8]};
  assign udp_dst_port = {udp_win[23:16], udp_win[31:24]};
  assign udp_length   = {udp_win[39:32], udp_win[47:40]};
  assign udp_checksum = {udp_win[55:48], udp_win[63:56]};

  // ------------------------------------------------- chain discrimination --
  logic             is_ipv4;
  logic             ihl_ok;
  logic [LEN_W-1:0] ip_hdr_bytes;
  logic             is_udp;

  always_comb begin
    is_ipv4      = (eth_ethertype == ETHERTYPE_V);
    ihl_ok       = (ip_ihl >= 4'd5);
    ip_hdr_bytes = LEN_W'({12'b0, ip_ihl} << 2);
    is_udp       = is_ipv4 && ihl_ok && (ip_protocol == IP_PROTO_UDP);
  end

  // Header length, live. A non-IPv4 frame is settled by byte 14; an IPv4 one
  // needs byte 23 for the protocol, which arrives no later than byte 24.
  //
  // ihl_ok is part of the condition, not just of the arithmetic: an IPv4 header
  // whose IHL is below the legal minimum has no defined end, so it has no
  // defined payload either. Withholding o_strip_valid makes payload_window
  // report o_strip_err and emit nothing, rather than shipping bytes measured
  // from a fallback offset that the record has already declared invalid.
  // (docs/bugs-found.md B005)
  always_comb begin
    o_strip_valid = (i_have >= LEN_W'(ETH_BYTES)) &&
                    (!is_ipv4 || ((i_have >= LEN_W'(PROTO_BYTE + 1)) && ihl_ok));

    if (!is_ipv4 || !ihl_ok) begin
      o_strip_bytes = LEN_W'(ETH_BYTES);
    end else if (is_udp) begin
      o_strip_bytes = LEN_W'(ETH_BYTES) + ip_hdr_bytes + LEN_W'(UDP_BYTES);
    end else begin
      o_strip_bytes = LEN_W'(ETH_BYTES) + ip_hdr_bytes;
    end
  end

  // ----------------------------------------------------------- the record --
  // i_have at i_done is the whole packet when it ended early, and HDR_BYTES
  // otherwise -- and HDR_BYTES already exceeds every threshold below, so one
  // set of comparisons covers both cases.
  logic [2:0] layers;
  logic [2:0] err;

  always_comb begin
    layers = 3'b000;
    err    = 3'(WS_OK);

    if (i_have < LEN_W'(ETH_BYTES)) begin
      err = 3'(WS_TRUNCATED);
    end else begin
      layers[0] = 1'b1;                       // eth
      if (is_ipv4) begin
        if (i_have < LEN_W'(IP_MIN_BYTES)) begin
          err = 3'(WS_TRUNCATED);
        end else if (!ihl_ok) begin
          err = 3'(WS_BAD_HDR_LEN);
        end else if (i_have < (LEN_W'(ETH_BYTES) + ip_hdr_bytes)) begin
          err = 3'(WS_TRUNCATED);
        end else begin
          layers[1] = 1'b1;                   // ipv4
          if (is_udp) begin
            if (i_have < (LEN_W'(ETH_BYTES) + ip_hdr_bytes + LEN_W'(UDP_BYTES))) begin
              err = 3'(WS_TRUNCATED);
            end else begin
              layers[2] = 1'b1;               // udp
            end
          end
        end
      end
    end
  end

  always_ff @(posedge clk) begin
    if (!rst_n) begin
      o_rec_valid <= 1'b0;
    end else begin
      o_rec_valid <= i_done;
      if (i_done) begin
        o_layer_valid <= layers;
        o_err         <= err;
        o_hdr_bytes   <= o_strip_bytes;

        o_eth_dst_mac       <= eth_dst_mac;
        o_eth_src_mac       <= eth_src_mac;
        o_eth_ethertype     <= eth_ethertype;

        o_ipv4_version        <= ip_version;
        o_ipv4_ihl            <= ip_ihl;
        o_ipv4_dscp           <= ip_dscp;
        o_ipv4_ecn            <= ip_ecn;
        o_ipv4_total_length   <= ip_total_length;
        o_ipv4_identification <= ip_identification;
        o_ipv4_flags          <= ip_flags;
        o_ipv4_frag_offset    <= ip_frag_offset;
        o_ipv4_ttl            <= ip_ttl;
        o_ipv4_protocol       <= ip_protocol;
        o_ipv4_hdr_checksum   <= ip_hdr_checksum;
        o_ipv4_src_ip         <= ip_src_ip;
        o_ipv4_dst_ip         <= ip_dst_ip;

        o_udp_src_port      <= udp_src_port;
        o_udp_dst_port      <= udp_dst_port;
        o_udp_length        <= udp_length;
        o_udp_checksum      <= udp_checksum;
      end
    end
  end

endmodule

`default_nettype wire
