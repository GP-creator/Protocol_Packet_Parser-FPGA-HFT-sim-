from __future__ import annotations

from pathlib import Path

import pytest

from wirespec.ir import IR, build_ir
from wirespec.schema import Schema, load_schema

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_DIR = ROOT / "schemas"
INVALID_DIR = SCHEMA_DIR / "invalid"


@pytest.fixture(scope="session")
def eth_schema() -> Schema:
    return load_schema(SCHEMA_DIR / "eth_ipv4_udp.yaml")


@pytest.fixture(scope="session")
def feed_schema() -> Schema:
    return load_schema(SCHEMA_DIR / "simple_feed.yaml")


@pytest.fixture(scope="session")
def eth_ir(eth_schema: Schema) -> IR:
    return build_ir(eth_schema)


@pytest.fixture(scope="session")
def feed_ir(feed_schema: Schema) -> IR:
    return build_ir(feed_schema)
