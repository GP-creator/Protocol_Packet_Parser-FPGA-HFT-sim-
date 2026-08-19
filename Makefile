# Wirespec top-level build.
#
# Targets that are not yet implemented fail loudly rather than succeeding
# vacuously -- a green `make check` must mean something.

SHELL      := /bin/bash
PYTHON     ?= python3
ROOT       := $(patsubst %/,%,$(dir $(abspath $(lastword $(MAKEFILE_LIST)))))
export PYTHONPATH := $(ROOT)$(if $(PYTHONPATH),:$(PYTHONPATH))

SCHEMA_DIR := schemas
GEN_DIR    := rtl/generated
DATA_W     ?= 64
PROTOS     := eth_ipv4_udp simple_feed

.PHONY: all gen lint sim test check clean help

help:
	@echo "make gen    [DATA_W=64]  generate RTL from the schemas"
	@echo "make lint                verilator --lint-only -Wall over all RTL"
	@echo "make sim                 run the cocotb testbenches"
	@echo "make test                run the pytest suite for the generator"
	@echo "make check               lint + test + sim  (same gate as ci/check.sh)"
	@echo "make clean               remove generated and simulation artifacts"

all: gen lint test

test:
	$(PYTHON) -m pytest

gen:
	@echo "gen: not implemented until M3" >&2; exit 1

lint:
	@echo "lint: no RTL until M2" >&2; exit 1

sim:
	@echo "sim: no testbenches until M2" >&2; exit 1

check: ci/check.sh
	@./ci/check.sh

clean:
	rm -rf $(GEN_DIR)/* sim_build obj_dir results.xml *.vcd *.fst
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
	rm -rf .pytest_cache
