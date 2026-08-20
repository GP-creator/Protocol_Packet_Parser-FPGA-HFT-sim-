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
if [ -f rtl/filelist_gen.f ]; then
  TARGETS+=("parser_top_simple_feed:@rtl/filelist_gen.f")
fi

rc=0
for entry in "${TARGETS[@]}"; do
  top="${entry%%:*}"
  src="${entry#*:}"
  for w in $WIDTHS; do
    printf 'lint %-28s DATA_W=%-4s ' "$top" "$w"
    if [ "${src:0:1}" = "@" ]; then
      out=$(verilator --lint-only -Wall -sv --top-module "$top" -GDATA_W="$w" -f "${src:1}" 2>&1)
    else
      # shellcheck disable=SC2086
      out=$(verilator --lint-only -Wall -sv --top-module "$top" -GDATA_W="$w" $src 2>&1)
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
