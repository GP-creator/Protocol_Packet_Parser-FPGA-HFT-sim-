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

  // Header length for this packet. Must be asserted no later than the beat that
  // carries the first payload byte; o_strip_err reports a packet where it was
  // never asserted at all.
  input  wire                i_strip_valid,
  input  wire [LEN_W-1:0]    i_strip_bytes,

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
  logic [DATA_W-1:0] prev_q;     // previous input beat
  logic [LEN_W-1:0]  strip_q;
  logic              strip_lat;  // strip_q holds this packet's header length
  logic [LEN_W-1:0]  emitted_q;  // payload bytes already shipped

  // A packet's terminal marker must sit at a fixed distance behind its EOF beat
  // or two packets' markers collide on the bus. A drained payload's last beat
  // lands two cycles after EOF, so o_empty and o_strip_err are staged to land
  // there too rather than one cycle after. (docs/bugs-found.md B004)
  logic              empty_pend;
  logic              strip_err_pend;

  logic              drain_q;
  logic [DATA_W-1:0] drain_src_q;
  logic [ROT_W-1:0]  drain_rot_q;
  logic [BCNT_W-1:0] drain_bytes_q;
  logic [LEN_W-1:0]  drain_off_q;

  // -------------------------------------------------------- combinational --
  logic              sof_now;
  logic              strip_ok;
  logic [LEN_W-1:0]  strip_eff;
  logic [LEN_W-1:0]  emitted_eff;
  logic [ROT_W-1:0]  rot;
  logic [LEN_W-1:0]  fbo;         // first payload beat, floored to a beat boundary
  logic              emit_now;
  logic [2*DATA_W-1:0] win;
  logic [DATA_W-1:0] win_sh;
  logic [ROT_W+2:0]  shamt;
  logic [LEN_W-1:0]  pkt_bytes;
  logic [LEN_W-1:0]  pay_total;
  logic [LEN_W-1:0]  rem_before;
  logic [BCNT_W-1:0] out_bytes;
  logic [LEN_W-1:0]  rem_after;

  logic [2*DATA_W-1:0] drain_win;
  logic [DATA_W-1:0]   drain_sh;
  logic [ROT_W+2:0]    drain_shamt;

  always_comb begin
    // On a packet's first beat, strip_lat / strip_q / emitted_q still describe
    // the *previous* packet: the registers that clear them are updated by this
    // same beat. A packet whose first beat is also its last -- one beat long,
    // or with its whole payload in the tail -- reads them in that stale cycle,
    // so the combinational path has to take the sof case explicitly rather than
    // relying on the register update. (docs/bugs-found.md B002, B003)
    sof_now     = i_valid && i_sof;
    strip_ok    = sof_now ? i_strip_valid : (strip_lat || i_strip_valid);
    strip_eff   = (sof_now || !strip_lat) ? i_strip_bytes : strip_q;
    emitted_eff = sof_now ? LEN_W'(0) : emitted_q;

    rot       = strip_eff[ROT_W-1:0];
    fbo       = strip_eff - LEN_W'(rot);

    // {this beat, previous beat} >> rot bytes: the low half is packet bytes
    // [i_offset-KEEP_W+rot, i_offset+rot).
    shamt  = {rot, 3'b000};
    win    = {i_data, prev_q};
    win_sh = DATA_W'(win >> shamt);

    emit_now = i_valid && strip_ok && (i_offset >= (fbo + LEN_W'(KEEP_W)));

    pkt_bytes  = i_offset + LEN_W'(i_bytes);
    pay_total  = (strip_ok && (pkt_bytes > strip_eff)) ? (pkt_bytes - strip_eff) : LEN_W'(0);
    rem_before = pay_total - emitted_eff;

    // Only the final beat can be short; every earlier one ships a full beat.
    if (i_eof) begin
      out_bytes = (rem_before >= LEN_W'(KEEP_W)) ? BCNT_W'(KEEP_W) : rem_before[BCNT_W-1:0];
    end else begin
      out_bytes = BCNT_W'(KEEP_W);
    end

    rem_after = i_eof ? (rem_before - (emit_now ? LEN_W'(out_bytes) : LEN_W'(0))) : LEN_W'(0);

    drain_shamt = {drain_rot_q, 3'b000};
    drain_win   = {{DATA_W{1'b0}}, drain_src_q};
    drain_sh    = DATA_W'(drain_win >> drain_shamt);
  end

  // ---------------------------------------------------------------- output --
  always_ff @(posedge clk) begin
    if (!rst_n) begin
      o_valid       <= 1'b0;
      o_data        <= '0;
      o_keep        <= '0;
      o_sof         <= 1'b0;
      o_eof         <= 1'b0;
      o_bytes       <= '0;
      o_offset      <= '0;
      o_empty        <= 1'b0;
      o_strip_err    <= 1'b0;
      empty_pend     <= 1'b0;
      strip_err_pend <= 1'b0;
      prev_q         <= '0;
      strip_q       <= '0;
      strip_lat     <= 1'b0;
      emitted_q     <= '0;
      drain_q       <= 1'b0;
      drain_src_q   <= '0;
      drain_rot_q   <= '0;
      drain_bytes_q <= '0;
      drain_off_q   <= '0;
    end else begin
      o_valid        <= 1'b0;
      drain_q        <= 1'b0;
      // One cycle behind the EOF beat that raised them, so that they surface
      // alongside where a drain beat would have been.
      o_empty        <= empty_pend;
      o_strip_err    <= strip_err_pend;
      empty_pend     <= 1'b0;
      strip_err_pend <= 1'b0;

      // The tail left over after the last input beat. Captured at EOF so the
      // next packet, which may start in this very cycle, cannot disturb it.
      if (drain_q) begin
        o_valid  <= 1'b1;
        o_data   <= drain_sh;
        o_keep   <= keep_mask(drain_bytes_q);
        o_bytes  <= drain_bytes_q;
        o_sof    <= (drain_off_q == LEN_W'(0));
        o_eof    <= 1'b1;
        o_offset <= drain_off_q;
      end

      if (i_valid) begin
        prev_q <= i_data;

        if (i_sof) begin
          emitted_q <= '0;
          strip_lat <= i_strip_valid;
          if (i_strip_valid) begin
            strip_q <= i_strip_bytes;
          end
        end else if (!strip_lat && i_strip_valid) begin
          strip_lat <= 1'b1;
          strip_q   <= i_strip_bytes;
        end

        if (emit_now) begin
          o_valid   <= 1'b1;
          o_data    <= win_sh;
          o_keep    <= keep_mask(out_bytes);
          o_bytes   <= out_bytes;
          o_sof     <= (emitted_eff == LEN_W'(0));
          o_eof     <= i_eof && (rem_after == LEN_W'(0));
          o_offset  <= emitted_eff;
          emitted_q <= emitted_eff + LEN_W'(out_bytes);
        end

        if (i_eof) begin
          if (rem_after != LEN_W'(0)) begin
            drain_q       <= 1'b1;
            drain_src_q   <= i_data;
            drain_rot_q   <= rot;
            drain_bytes_q <= rem_after[BCNT_W-1:0];
            drain_off_q   <= emitted_eff + (emit_now ? LEN_W'(out_bytes) : LEN_W'(0));
          end
          empty_pend     <= strip_ok && (pay_total == LEN_W'(0));
          strip_err_pend <= !strip_ok;
        end
      end
    end
  end

endmodule

`default_nettype wire
