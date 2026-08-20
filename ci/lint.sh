#!/usr/bin/env bash
# verilator --lint-only -Wall over every RTL module, at every supported DATA_W.
#
# Each module is linted standalone as well as through the top, because a warning
# inside a submodule can be masked once a parent pins its parameters. Zero
# warnings is the gate: verilator exits non-zero on the first one.
set -uo pipefail

cd "$(dirname "$0")/.."

WIDTHS="${WIDTHS:-64 128 256 512}"
COMMON="rtl/common/pkg_wirespec.sv"

# top:sources (sources are space-separated, package first where needed)
TARGETS=(
  "axis_reg_slice:rtl/common/axis_reg_slice.sv"
  "pkt_align:$COMMON rtl/common/pkt_align.sv"
  "hdr_accum:$COMMON rtl/common/hdr_accum.sv"
  "payload_window:$COMMON rtl/common/payload_window.sv"
  "parser_top_eth_ipv4_udp:@rtl/filelist_m2.f"
)

# These do not import pkg_wirespec, so linting them with it in the file list
# would report its constants as unused. A leading '!' means "has no DATA_W".
TARGETS+=(
  "msg_rotate:!rtl/common/msg_rotate.sv"
  "msg_stitch:rtl/common/msg_stitch.sv"
  "stats:!rtl/common/stats.sv"
)
# msg_framer needs pkg_wirespec for ws_err_e but not for WS_LEN_W, so linting it
# alone would report that constant as unused. It is covered through
# parser_top_simple_feed at all four widths.

# Generated RTL, if it has been emitted. Regenerate with `make gen`.
if [ -f rtl/generated/parser_top_eth_ipv4_udp.sv ]; then
  TARGETS+=("parser_top_eth_ipv4_udp[generated]:@rtl/filelist_gen.f")
fi
if [ -f rtl/generated/parser_top_simple_feed.sv ]; then
  TARGETS+=("parser_top_simple_feed[generated]:@rtl/filelist_feed.f")
fi

rc=0
for entry in "${TARGETS[@]}"; do
  top="${entry%%:*}"
  src="${entry#*:}"
  label="$top"
  top="${top%%\[*}"   # strip a "[generated]" tag from the module name
  gparam=(-GDATA_W=64)
  if [ "${src:0:1}" = "!" ]; then
    src="${src:1}"
    gparam=()
  fi

  for w in $WIDTHS; do
    printf 'lint %-40s DATA_W=%-4s ' "$label" "$w"
    if [ ${#gparam[@]} -gt 0 ]; then gparam=(-GDATA_W="$w"); fi
    if [ "${src:0:1}" = "@" ]; then
      out=$(verilator --lint-only -Wall -sv --top-module "$top" "${gparam[@]}" -f "${src:1}" 2>&1)
    else
      # shellcheck disable=SC2086
      out=$(verilator --lint-only -Wall -sv --top-module "$top" "${gparam[@]}" $src 2>&1)
    fi
    if [ $? -ne 0 ]; then
      printf 'FAIL\n%s\n' "$out"
      rc=1
    else
      printf 'clean\n'
    fi
  done
done

if [ "$rc" -eq 0 ]; then
  printf '\nlint clean: %s target(s) x {%s}\n' "${#TARGETS[@]}" "$WIDTHS"
fi
exit $rc
