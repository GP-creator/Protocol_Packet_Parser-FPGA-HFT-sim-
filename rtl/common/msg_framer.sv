// Read the length prefix, compute message boundaries, schedule the beat.
//
// Message k+1 starts where message k ended, so the offsets are a serial
// dependency: read a length, add it, rotate to the next start, read the next
// length. SLOTS of that chain are unrolled per cycle, one msg_rotate each, and
// every field of every message then comes off its slot's window as a static
// slice.
//
// SLOTS = ceil(KEEP_W / MSG_MIN), which is exactly enough. Because the framer
// consumes greedily, whatever is left at the start of a cycle is one message
// that was *not* completable, so its declared total exceeds the leftover L. For
// k messages to finish this cycle we would need
//     L + KEEP_W >= total_1 + (k-1)*MSG_MIN > L + (k-1)*MSG_MIN
// hence KEEP_W > (k-1)*MSG_MIN, i.e. k <= ceil(KEEP_W/MSG_MIN). Sizing to the
// buffer's capacity instead over-provisions: at DATA_W=64, 8-byte beats and
// 14-byte messages mean a second slot can never fire.
//
// Having exactly enough also makes message latency a constant: a message is
// emitted in the cycle its last byte enters the window, never a cycle later
// because every slot was busy.
//
// Error precedence follows wirespec/golden.py::_decode_framed exactly:
//   length prefix present -> length non-zero -> total >= prefix ->
//   whole message present -> type known -> declared size matches the type.
// The first defect ends the packet; nothing after it is trusted.
//
// The chain is written as per-slot scalars inside one generate block, each slot
// reading its predecessor by hierarchical name. Carrying it in packed vectors
// instead reads to verilator as a vector depending on itself, and it reports a
// circular combinational path that is not there.
//
// There are SLOTS+1 stages, not SLOTS. The extra one can never accept -- the
// bound above says so -- and exists only to classify what is left over after
// the last message this cycle. Without it, a trailing fragment exposed on the
// packet's final beat is never looked at, and the packet's verdict depends on
// where the beat boundaries happened to fall rather than on its bytes (B009).
// It rotates PREFIX bytes rather than MSG_MAX, because a fragment is only ever
// classified by its length and type, never extracted.

`timescale 1ns / 1ps
`default_nettype none

module msg_framer #(
  parameter int DATA_W     = 64,
  parameter int MSG_MAX    = 32,
  parameter int MSG_MIN    = 14,
  parameter int LEN_OFF    = 0,   // byte offset of the length prefix in a message
  parameter int LEN_BYTES  = 2,
  parameter int LEN_ADD    = 2,   // total bytes = length value + LEN_ADD
  parameter int PREFIX     = 3,   // length + type bytes
  parameter int CNT_W      = 16,
  // Derived. Do not override.
  parameter int KEEP_W     = DATA_W / 8,
  parameter int BUF_BYTES  = MSG_MAX - 1 + KEEP_W,
  parameter int BUF_BITS   = BUF_BYTES * 8,
  parameter int OFF_W      = $clog2(BUF_BYTES + 1),
  parameter int MSG_BITS   = MSG_MAX * 8,
  parameter int SLOTS      = (KEEP_W + MSG_MIN - 1) / MSG_MIN,
  parameter int CHK        = SLOTS + 1,   // slots plus the tail classifier
  parameter int TOT_W      = LEN_BYTES * 8 + 2
) (
  input  wire                      clk,
  input  wire                      rst_n,

  input  wire [BUF_BITS-1:0]       i_win,
  input  wire [OFF_W-1:0]          i_nvalid,
  input  wire                      i_pkt_start,
  input  wire                      i_pkt_last,

  // Bit SLOTS is the tail classifier's verdict, answered from o_tail_pre and
  // o_tail_total rather than from a full message window.
  input  wire [CHK-1:0]            i_type_ok,
  input  wire [CHK-1:0]            i_size_ok,

  output wire [SLOTS*MSG_BITS-1:0] o_win,
  output wire [SLOTS*TOT_W-1:0]    o_total,
  output wire [PREFIX*8-1:0]       o_tail_pre,
  output wire [SLOTS-1:0]          o_slot_valid,
  output wire [OFF_W-1:0]          o_consume,

  output logic                     o_pkt_done,
  output logic [2:0]               o_pkt_err,
  output logic [CNT_W-1:0]         o_pkt_msgs
);

  import pkg_wirespec::*;

  // ---------------------------------------------------------- packet state --
  logic             bad_q;
  logic [2:0]       err_q;
  logic [CNT_W-1:0] count_q;
  logic             open_q;

  // A message declaring more bytes than the buffer could ever hold is certainly
  // malformed, but whether the model calls it BAD_LENGTH or TRUNCATED depends on
  // whether that many bytes actually arrive. Counting them past without storing
  // them is enough to tell the two apart.
  logic             skip_q;
  logic [TOT_W-1:0] skip_left_q;
  logic             skip_type_ok_q;

  logic start_ok;
  assign start_ok = !bad_q && !skip_q && (open_q || i_pkt_start);

  // ------------------------------------------------------------ slot chain --
  genvar g;
  generate
    for (g = 0; g < CHK; g++) begin : g_slot
      wire [OFF_W-1:0]    off_w;
      wire [PREFIX*8-1:0] pre_w;    // length + type bytes, every slot has these
      wire [TOT_W-1:0]    lenv_w;
      wire [TOT_W-1:0]    total_w;
      wire [OFF_W-1:0]    avail_w;
      wire [OFF_W-1:0]    off_next;
      wire                has_len_w;
      wire                zero_len_w;
      wire                short_tot_w;
      wire                huge_tot_w;
      wire                whole_w;
      wire                prev_ok;
      wire                accept_w;
      wire                fault_w;
      wire                skip_w;
      logic [2:0]         err_w;

      if (g == 0) begin : g_first
        assign off_w   = OFF_W'(0);
        assign prev_ok = start_ok;
      end else begin : g_rest
        assign off_w   = g_slot[g-1].off_next;
        assign prev_ok = g_slot[g-1].accept_w;
      end

      // The tail classifier only ever reads a length and a type, so it rotates
      // PREFIX bytes instead of MSG_MAX. That keeps the stage this adds to the
      // fault path a narrow byte select rather than a second full rotator.
      if (g == SLOTS) begin : g_tail_rot
        msg_rotate #(
          .IN_BYTES (BUF_BYTES),
          .OUT_BYTES(PREFIX)
        ) u_rot (
          .i_win(i_win),
          .i_off(off_w),
          .o_win(pre_w)
        );
      end else begin : g_full_rot
        wire [MSG_BITS-1:0] full_w;
        msg_rotate #(
          .IN_BYTES (BUF_BYTES),
          .OUT_BYTES(MSG_MAX)
        ) u_rot (
          .i_win(i_win),
          .i_off(off_w),
          .o_win(full_w)
        );
        assign pre_w = full_w[PREFIX*8-1:0];
      end

      // Big-endian length prefix, read straight off the rotated window. Only
      // the prefix is needed, so this is the one read the tail stage shares.
      logic [TOT_W-1:0] lenv_acc;
      always_comb begin
        lenv_acc = TOT_W'(0);
        for (int b = 0; b < LEN_BYTES; b++) begin
          lenv_acc = (lenv_acc << 8) | TOT_W'(pre_w[(LEN_OFF + b)*8 +: 8]);
        end
      end
      assign lenv_w = lenv_acc;

      assign total_w  = lenv_w + TOT_W'(LEN_ADD);
      // Written as a strict compare so slot 0, whose offset is the constant
      // zero, does not read as a comparison that is always true.
      assign avail_w  = (i_nvalid > off_w) ? (i_nvalid - off_w) : OFF_W'(0);
      assign off_next = off_w + OFF_W'(total_w);

      assign has_len_w   = avail_w >= OFF_W'(LEN_OFF + LEN_BYTES);
      assign zero_len_w  = has_len_w && (lenv_w == TOT_W'(0));
      assign short_tot_w = has_len_w && (total_w < TOT_W'(PREFIX));
      // "Huge" means the buffer could never hold it, not merely that no type is
      // that size. A message between MSG_MAX and BUF_BYTES still arrives whole
      // and is rejected on its size, exactly as the model rejects it; only one
      // the buffer cannot hold has to be counted past instead.
      assign huge_tot_w  = has_len_w && (total_w > TOT_W'(BUF_BYTES));
      assign whole_w     = has_len_w && !zero_len_w && !short_tot_w &&
                           !huge_tot_w && (TOT_W'(avail_w) >= total_w);

      // The tail classifier never emits a record. By the SLOTS bound its
      // whole_w cannot be true anyway when every earlier slot accepted, so
      // this is belt and braces rather than a behavioural difference.
      if (g == SLOTS) begin : g_tail_acc
        assign accept_w = 1'b0;
      end else begin : g_real_acc
        assign accept_w = prev_ok && whole_w && i_type_ok[g] && i_size_ok[g];
      end

      // The message is here and does not decode, or it is malformed on its
      // face, or the packet ended without it: the packet is over either way.
      assign fault_w = prev_ok && (
                         zero_len_w || short_tot_w ||
                         (whole_w && (!i_type_ok[g] || !i_size_ok[g])) ||
                         (i_pkt_last && !whole_w && !huge_tot_w &&
                          (avail_w != OFF_W'(0)))
                       );

      assign skip_w = prev_ok && huge_tot_w && !fault_w;

      always_comb begin
        if (!has_len_w) begin
          err_w = 3'(WS_TRUNCATED);
        end else if (zero_len_w) begin
          err_w = 3'(WS_ZERO_LENGTH);
        end else if (short_tot_w) begin
          err_w = 3'(WS_BAD_LENGTH);
        end else if (!whole_w) begin
          err_w = 3'(WS_TRUNCATED);
        end else if (!i_type_ok[g]) begin
          err_w = 3'(WS_UNKNOWN_TYPE);
        end else begin
          err_w = 3'(WS_BAD_LENGTH);
        end
      end
    end
  endgenerate

  // ---------------------------------------------------------------- pack ---
  // Read-only views of the chain. Nothing here feeds back into it.
  logic [CHK-1:0]   accept_v;
  logic [CHK-1:0]   fault_v;
  logic [CHK-1:0]   skip_v;
  logic [OFF_W-1:0] offnext_v [CHK];
  logic [OFF_W-1:0] avail_v   [CHK];
  logic [TOT_W-1:0] total_v   [CHK];
  logic [2:0]       err_v     [CHK];

  generate
    for (g = 0; g < CHK; g++) begin : g_pack
      assign accept_v[g]  = g_slot[g].accept_w;
      assign fault_v[g]   = g_slot[g].fault_w;
      assign skip_v[g]    = g_slot[g].skip_w;
      assign offnext_v[g] = g_slot[g].off_next;
      assign avail_v[g]   = g_slot[g].avail_w;
      assign total_v[g]   = g_slot[g].total_w;
      assign err_v[g]     = g_slot[g].err_w;

      if (g < SLOTS) begin : g_rec
        assign o_win[g*MSG_BITS +: MSG_BITS] = g_slot[g].g_full_rot.full_w;
        assign o_total[g*TOT_W +: TOT_W]     = g_slot[g].total_w;
      end
    end
  endgenerate

  assign o_tail_pre = g_slot[SLOTS].pre_w;

  assign o_slot_valid = accept_v[SLOTS-1:0];

  logic             enter_skip;
  logic [TOT_W-1:0] skip_total;
  logic [TOT_W-1:0] skip_seen;    // bytes of it already going past this cycle
  logic             skip_type_ok;

  always_comb begin
    enter_skip   = |skip_v;
    skip_total   = TOT_W'(0);
    skip_seen    = TOT_W'(0);
    skip_type_ok = 1'b0;
    for (int j = CHK - 1; j >= 0; j--) begin
      if (skip_v[j]) begin
        skip_total   = total_v[j];
        // The whole buffer is retired on the cycle skip begins, so the bytes of
        // this message that are already in it must come off the count -- they
        // are never seen again. Missing this made a long declared length
        // impossible to satisfy, so every such message read as TRUNCATED.
        skip_seen    = TOT_W'(avail_v[j]);
        skip_type_ok = i_type_ok[j];
      end
    end
  end

  // ------------------------------------------------------------- consume ---
  logic [OFF_W-1:0] consume_msgs;
  logic [OFF_W-1:0] skip_now;

  always_comb begin
    consume_msgs = OFF_W'(0);
    for (int j = 0; j < SLOTS; j++) begin
      if (accept_v[j]) begin
        consume_msgs = offnext_v[j];
      end
    end
    skip_now = (TOT_W'(i_nvalid) < skip_left_q) ? i_nvalid : OFF_W'(skip_left_q);
  end

  // Once the packet is over, drain what is left so the buffer starts the next
  // packet clean.
  assign o_consume = (bad_q || |fault_v) ? i_nvalid
                   : skip_q              ? skip_now
                   : enter_skip          ? i_nvalid
                   : consume_msgs;

  // --------------------------------------------------------------- state ---
  logic [OFF_W-1:0] left_after;
  logic             bad;
  logic [2:0]       err;
  logic [CNT_W-1:0] count;
  logic             skip_resolved;
  logic             skip_expired;
  logic             pkt_complete;

  always_comb begin
    left_after = i_nvalid - o_consume;

    bad   = bad_q;
    err   = err_q;
    count = i_pkt_start ? CNT_W'(0) : count_q;

    for (int j = 0; j < SLOTS; j++) begin
      if (accept_v[j]) begin
        count = count + CNT_W'(1);
      end
    end

    // The first fault of the packet wins; later slots are already gated off.
    for (int j = CHK - 1; j >= 0; j--) begin
      if (fault_v[j]) begin
        bad = 1'b1;
        err = err_v[j];
      end
    end

    skip_resolved = skip_q && (TOT_W'(skip_now) >= skip_left_q);
    // Skip mode normally spans several cycles, so `skip_q` carries it. A skip
    // that *begins* on the packet's final beat has no next cycle to expire in,
    // and read on its own would let the packet finish clean. It cannot ever
    // resolve: "huge" means the declared total exceeds the whole buffer, and
    // the buffer is all there is left. (docs/bugs-found.md B011)
    skip_expired  = i_pkt_last &&
                    ((skip_q && !skip_resolved) || (enter_skip && !bad));

    if (skip_resolved) begin
      bad = 1'b1;
      err = skip_type_ok_q ? 3'(WS_BAD_LENGTH) : 3'(WS_UNKNOWN_TYPE);
    end else if (skip_expired) begin
      bad = 1'b1;
      err = 3'(WS_TRUNCATED);
    end

    // Bytes still held when the packet ends are a message that never finished.
    if (i_pkt_last && !bad && (left_after != OFF_W'(0))) begin
      bad = 1'b1;
      err = 3'(WS_TRUNCATED);
    end

    pkt_complete = (open_q || i_pkt_start) && i_pkt_last;
  end

  always_ff @(posedge clk) begin
    if (!rst_n) begin
      bad_q          <= 1'b0;
      err_q          <= 3'(WS_OK);
      count_q        <= '0;
      open_q         <= 1'b0;
      skip_q         <= 1'b0;
      skip_left_q    <= '0;
      skip_type_ok_q <= 1'b0;
      o_pkt_done     <= 1'b0;
      o_pkt_err      <= 3'(WS_OK);
      o_pkt_msgs     <= '0;
    end else begin
      o_pkt_done <= 1'b0;

      if (i_pkt_start) begin
        open_q <= 1'b1;
      end

      if (open_q || i_pkt_start) begin
        bad_q   <= bad;
        err_q   <= err;
        count_q <= count;

        if (enter_skip && !bad) begin
          skip_q         <= 1'b1;
          skip_left_q    <= skip_total - skip_seen;
          skip_type_ok_q <= skip_type_ok;
        end else if (skip_resolved || skip_expired) begin
          skip_q      <= 1'b0;
          skip_left_q <= '0;
        end else if (skip_q) begin
          skip_left_q <= skip_left_q - TOT_W'(skip_now);
        end
      end

      if (pkt_complete) begin
        o_pkt_done <= 1'b1;
        o_pkt_err  <= err;
        o_pkt_msgs <= count;
        open_q     <= 1'b0;
        bad_q      <= 1'b0;
        err_q      <= 3'(WS_OK);
        count_q    <= '0;
        skip_q     <= 1'b0;
      end
    end
  end

endmodule

`default_nettype wire
