#!/usr/bin/env bash
# Local gate. No hosted CI: this script is the whole story.
#
# Stages are added as milestones land.  Anything not yet built is reported as
# SKIP with a reason, never silently passed over.
set -uo pipefail

cd "$(dirname "$0")/.."
ROOT="$PWD"
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"

FAILED=0
declare -a RESULTS=()

stage() {
  local name="$1"; shift
  printf '\n=== %s ===\n' "$name"
  if "$@"; then
    RESULTS+=("PASS  $name")
  else
    RESULTS+=("FAIL  $name")
    FAILED=1
  fi
}

skip() {
  RESULTS+=("SKIP  $1 -- $2")
  printf '\n=== %s ===\nskipped: %s\n' "$1" "$2"
}

# ---------------------------------------------------------------- python ----
stage "pytest" python3 -m pytest

if command -v ruff >/dev/null 2>&1; then
  stage "ruff" ruff check wirespec tests tb
else
  skip "ruff" "ruff is not installed (optional)"
fi

# ------------------------------------------------------------------- rtl ----
have_files() { [ -n "$(find "$@" -type f -name "$FIND_NAME" -print -quit 2>/dev/null)" ]; }

FIND_NAME='*.sv'
if have_files rtl/common; then
  stage "verilator lint" make -s lint
else
  skip "verilator lint" "no RTL yet (M2)"
fi

FIND_NAME='test_*.py'
if have_files tb/unit tb/integration; then
  stage "cocotb" make -s sim
else
  skip "cocotb" "no testbenches yet (M2)"
fi

# --------------------------------------------------------------- summary ----
printf '\n=== summary ===\n'
printf '%s\n' "${RESULTS[@]}"
if [ "$FAILED" -ne 0 ]; then
  printf '\ncheck FAILED\n'
  exit 1
fi
printf '\ncheck OK\n'
