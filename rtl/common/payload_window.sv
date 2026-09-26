// Strip the headers; present the payload as a contiguous, lane-0-aligned stream.
//
// The payload starts at an arbitrary byte inside some beat. Rather than mux
// every downstream consumer over every lane, one funnel shift here re-bases the
// whole stream so that payload byte 0 lands on lane 0 and stays there. This is
// the same rotate-then-slice move msg_rotate makes for messages, applied one
// level up (docs/decisions/0001-rotate-then-slice.md).
//
// Timing. Output beat k covers payload bytes [k*KEEP_W, (k+1)*KEEP_W), assembled
// from the beat that supplies its tail and the one before it. That is a uniform
// one-beat content lag plus one register: exactly one cycle of latency, the same
// for every value of the rotation, including zero. Special-casing an aligned
// payload would save a beat and cost the constant latency the design promises,
// so it is not special-cased.
//
// The last input beat can leave up to KEEP_W-1 payload bytes unshipped. Those go
// out in a drain cycle immediately after EOF, from registers captured at EOF, so
// a back-to-back packet arriving in that same cycle cannot disturb them.
//
// Clocking. i_strip_* is combinational from the header parser and is the
// deepest cone in the design, so it goes straight into a register, sampled with
// each beat, and nothing else reads it. That costs nothing: no beat that emits
// can be the one that first carries the header length (the length is known by
// the first payload beat, and emission starts a beat later), and the
// end-of-packet accounting is resolved in the drain cycle, by which time the
// EOF beat's sample is in the register.
//
// Byte counts come from beat geometry, not running counters: every beat but the
// last is full, so an emitting beat's payload offset is
// (i_offset/KEEP_W - strip/KEEP_W - 1) * KEEP_W, and on the emitting EOF beat
// the bytes still to ship are exactly i_bytes + KEEP_W - rot. Neither depends on
// whether earlier beats emitted, so the emit decision only drives o_valid. A
// packet with a tkeep violation (pkt_align o_keep_err) breaks the premise, but
// its payload content is already undefined there because the funnel shift
// assumes full beats; it still ends with exactly one o_eof.
// (docs/decisions/0004-timing-closure.md)

`timescale 1ns / 1ps
`default_nettype none

module payload_window #(
  parameter int DATA_W = 64,
  parameter int LEN_W  = pkg_wirespec::WS_LEN_W,
  // Derived. Do not override.
  parameter int KEEP_W = DATA_W / 8,
  parameter int BCNT_W = $clog2(DATA_W / 8 + 1),
  parameter int ROT_W  = $clog2(DATA_W / 8)
) (
  input  wire                clk,
  input  wire                rst_n,

  // Annotated beat from pkt_align.
  input  wire                i_valid,
  input  wire [DATA_W-1:0]   i_data,
  input  wire                i_sof,
  input  wire                i_eof,
  input  wire [BCNT_W-1:0]   i_bytes,
  input  wire [LEN_W-1:0]    i_offset,

  // Header length for this packet, sampled with each beat. Must be asserted no
  // later than the beat that carries the first payload byte and held for the
  // rest of the packet; o_strip_err reports a packet where it never was.
  input  wire                i_strip_valid,
  input  wire [LEN_W-1:0]    i_strip_bytes,

  // Valid-only: every field but o_empty / o_strip_err is meaningful only with
  // o_valid.
  output logic               o_valid,
  output logic [DATA_W-1:0]  o_data,
  output logic [KEEP_W-1:0]  o_keep,
  output logic               o_sof,
  output logic               o_eof,
  output logic [BCNT_W-1:0]  o_bytes,
  output logic [LEN_W-1:0]   o_offset,   // payload byte index of lane 0
  output logic               o_empty,    // pulse: this packet carried no payload
  output logic               o_strip_err // pulse: header length never arrived
);

  function automatic logic [KEEP_W-1:0] keep_mask(input logic [BCNT_W-1:0] n);
    logic [KEEP_W:0] m;
    m = {{KEEP_W{1'b0}}, 1'b1} << n;
    m = m - {{KEEP_W{1'b0}}, 1'b1};
    return m[KEEP_W-1:0];
  endfunction

  // ---------------------------------------------------------------- state --
  logic [DATA_W-1:0] prev_q;      // previous input beat
  logic              sr_valid;    // i_strip_* as sampled with the last beat
  logic [LEN_W-1:0]  sr_bytes;

  // End-of-packet capture, resolved in the following (drain) cycle. The drain
  // beat, o_empty and o_strip_err all land two cycles after EOF: a packet's
  // terminal marker must sit at a fixed distance behind its EOF beat or two
  // packets' markers collide on the bus. (docs/bugs-found.md B004)
  logic              ep_q;        // an EOF beat arrived last cycle
  logic              ep_emit;     // ... and emitted a beat of its own
  logic [BCNT_W-1:0] ep_bytes;    // its byte count
  logic [LEN_W-1:0]  ep_pkt;      // the packet length
  logic [LEN_W-1:0]  ep_shipped;  // payload bytes shipped by EOF, if it emitted
  logic [DATA_W-1:0] drain_src_q;

  // -------------------------------------------------------- combinational --
  logic [ROT_W-1:0]  rot;
  logic              emit_now;
  logic [2*DATA_W-1:0] win;
  logic [DATA_W-1:0] win_sh;
  logic [ROT_W+2:0]  shamt;
  logic              last_out;
  logic [BCNT_W-1:0] out_bytes;
  logic [LEN_W-ROT_W-1:0] beats_before;  // payload beats shipped before this one

  logic              ep_more;
  logic [BCNT_W-1:0] ep_rem;      // < KEEP_W whenever drain_now
  logic              drain_now;
  logic [2*DATA_W-1:0] drain_win;
  logic [DATA_W-1:0]   drain_sh;

  always_comb begin
    rot = sr_bytes[ROT_W-1:0];

    // A beat emits once lane 0 has passed the first payload beat:
    // i_offset >= floor(strip / KEEP_W) * KEEP_W + KEEP_W. On a packet's first
    // beat the sample is the previous packet's, but a first beat never emits.
    emit_now = i_valid && !i_sof && sr_valid &&
               (i_offset[LEN_W-1:ROT_W] > sr_bytes[LEN_W-1:ROT_W]);

    // {this beat, previous beat} >> rot bytes: the low half is packet bytes
    // [i_offset-KEEP_W+rot, i_offset+rot).
    shamt  = {rot, 3'b000};
    win    = {i_data, prev_q};
    win_sh = DATA_W'(win >> shamt);

    // Only the final beat can be short; every earlier one ships a full beat.
    // On the EOF beat, i_bytes + KEEP_W - rot bytes remain: this beat takes up
    // to KEEP_W of them and the drain takes i_bytes - rot if that is positive.
    last_out  = i_eof && (i_bytes <= BCNT_W'(rot));
    out_bytes = last_out ? BCNT_W'(i_bytes + BCNT_W'(KEEP_W) - BCNT_W'(rot))
                         : BCNT_W'(KEEP_W);

    beats_before = i_offset[LEN_W-1:ROT_W] - sr_bytes[LEN_W-1:ROT_W] - 1'b1;

    // Drain cycle: sr_* now holds the EOF beat's sample. Emitting, the drain is
    // i_bytes - rot; not emitting, nothing has shipped and it is the packet
    // length minus the header.
    if (ep_emit) begin
      ep_more = ep_bytes > BCNT_W'(rot);
      ep_rem  = ep_bytes - BCNT_W'(rot);
    end else begin
      ep_more = ep_pkt > sr_bytes;
      ep_rem  = BCNT_W'(ep_pkt) - BCNT_W'(sr_bytes);
    end
    drain_now = ep_q && sr_valid && ep_more;

    drain_win = {{DATA_W{1'b0}}, drain_src_q};
    drain_sh  = DATA_W'(drain_win >> shamt);
  end

  // ---------------------------------------------------------------- output --
  always_ff @(posedge clk) begin
    if (!rst_n) begin
      o_valid     <= 1'b0;
      o_data      <= '0;
      o_keep      <= '0;
      o_sof       <= 1'b0;
      o_eof       <= 1'b0;
      o_bytes     <= '0;
      o_offset    <= '0;
      o_empty     <= 1'b0;
      o_strip_err <= 1'b0;
      prev_q      <= '0;
      sr_valid    <= 1'b0;
      sr_bytes    <= '0;
      ep_q        <= 1'b0;
      ep_emit     <= 1'b0;
      ep_bytes    <= '0;
      ep_pkt      <= '0;
      ep_shipped  <= '0;
      drain_src_q <= '0;
    end else begin
      ep_q        <= 1'b0;
      o_empty     <= ep_q && sr_valid && !ep_more && !ep_emit;
      o_strip_err <= ep_q && !sr_valid;

      // The payload fields are loaded every cycle and only o_valid carries the
      // emit/drain decision. A drain cycle can never also emit -- the beat after
      // EOF opens a packet -- so ep_q alone picks the source.
      o_valid <= drain_now || emit_now;
      if (ep_q) begin
        o_data   <= drain_sh;
        o_keep   <= keep_mask(ep_rem);
        o_bytes  <= ep_rem;
        o_sof    <= !ep_emit;
        o_eof    <= 1'b1;
        o_offset <= ep_emit ? ep_shipped : LEN_W'(0);
      end else begin
        o_data   <= win_sh;
        o_keep   <= keep_mask(out_bytes);
        o_bytes  <= out_bytes;
        o_sof    <= (beats_before == '0);
        o_eof    <= last_out;
        o_offset <= {beats_before, ROT_W'(0)};
      end

      if (i_valid) begin
        prev_q   <= i_data;
        sr_valid <= i_strip_valid;
        sr_bytes <= i_strip_bytes;

        if (i_eof) begin
          ep_q        <= 1'b1;
          ep_emit     <= emit_now;
          ep_bytes    <= i_bytes;
          ep_pkt      <= i_offset + LEN_W'(i_bytes);
          ep_shipped  <= {beats_before + 1'b1, ROT_W'(0)};
          drain_src_q <= i_data;
        end
      end
    end
  end

endmodule

`default_nettype wire
