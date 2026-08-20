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


def pytest_sessionfinish(session, exitstatus):
    """Drop this run's counts where ci/collect_metrics.py will find them.

    Reported from the hook rather than scraped out of pytest's terminal summary,
    so a change to its output format cannot quietly turn the number into zero.
    """
    from tb.common import metrics as met

    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    stats = getattr(reporter, "stats", {}) if reporter else {}
    met.write(
        "pytest",
        {
            "collected": session.testscollected,
            "passed": len(stats.get("passed", [])),
            "failed": len(stats.get("failed", [])) + len(stats.get("error", [])),
            "skipped": len(stats.get("skipped", [])),
            "exit_status": int(exitstatus),
        },
    )
