"""The M1 acceptance test: 10,000 random packets of each protocol.

The builders in ``tb/common/stimulus.py`` know the protocols from prose and
:mod:`struct`; the decoder in ``wirespec/golden.py`` knows them from the YAML
schema.  Agreement across 20,000 packets is the evidence that the schema says
what the protocol says.
"""

from __future__ import annotations

import os
import random

import pytest

from tb.common import stimulus as st
from wirespec.golden import StreamModel, decode

from .test_golden import check_eth, check_feed

N = int(os.environ.get("WIRESPEC_RANDOM_N", "10000"))


def test_random_eth_ipv4_udp(eth_ir):
    rng = random.Random(0xE7E1)
    total_bytes = 0
    chains: dict[tuple[str, ...], int] = {}
    for _ in range(N):
        pkt = st.random_eth_ipv4_udp(rng)
        dec = check_eth(eth_ir, pkt)
        total_bytes += len(pkt.data)
        key = tuple(r.name for r in dec.records)
        chains[key] = chains.get(key, 0) + 1

    # The generator must actually have produced the interesting shapes.
    assert chains[("eth", "ipv4", "udp")] > N * 0.8
    assert chains[("eth",)] > 0, "no non-IPv4 frames were generated"
    assert chains[("eth", "ipv4")] > 0, "no non-UDP frames were generated"
    print(f"\neth_ipv4_udp: {N} packets, {total_bytes} bytes, chains={chains}")


def test_random_simple_feed(feed_ir):
    rng = random.Random(0x5EED)
    model = StreamModel(feed_ir)
    total_bytes = 0
    per_type: dict[str, int] = {}
    for seq in range(1, N + 1):
        pkt = st.random_feed_packet(rng, seq_num=seq)
        check_feed(feed_ir, pkt)
        model.feed(pkt.data)
        total_bytes += len(pkt.data)
        for meta in pkt.expect["messages"]:
            per_type[meta["type"]] = per_type.get(meta["type"], 0) + 1

    assert model.stats.packets == N
    assert model.stats.malformed == 0
    assert model.stats.seq_gaps == 0
    assert model.stats.messages == sum(per_type.values())
    assert set(per_type) == {"trade", "quote", "status", "imbalance"}
    assert min(per_type.values()) > N // 10, f"message mix is lopsided: {per_type}"
    print(
        f"\nsimple_feed: {N} packets, {total_bytes} bytes, "
        f"{model.stats.messages} messages, mix={per_type}"
    )


def test_random_field_values_survive_the_extremes(feed_ir):
    """Hammer the sign boundaries specifically; uniform randoms rarely hit them."""
    rng = random.Random(7)
    edges = [0, 1, -1, 2**7, 2**7 - 1, -(2**7), 2**15, 2**15 - 1, -(2**15), 2**31 - 1, -(2**31)]
    msgs = []
    for _ in range(400):
        msgs.append(
            st.make_quote(
                bid_px=rng.choice(edges),
                ask_px=rng.choice(edges),
                bid_sz=rng.choice([0, 1, 0xFFFF, 0x8000]),
                ask_sz=rng.choice([0, 1, 0xFFFF, 0x8000]),
            )
        )
    check_feed(feed_ir, st.build_feed_packet(msgs))


@pytest.mark.parametrize("builder", st.MALFORMED_FEED_BUILDERS, ids=lambda b: b.__name__)
def test_every_malformed_builder_is_caught(feed_ir, builder):
    pkt = builder()
    dec = decode(feed_ir, pkt.data)
    assert not dec.ok, f"{builder.__name__} produced a packet the decoder accepted"


def test_random_truncation_never_crashes_the_decoder(feed_ir, eth_ir):
    """Any prefix of any packet must decode or report a defect -- never raise."""
    rng = random.Random(0xBADC0DE)
    for _ in range(2000):
        if rng.random() < 0.5:
            ir, pkt = feed_ir, st.random_feed_packet(rng, seq_num=1)
        else:
            ir, pkt = eth_ir, st.random_eth_ipv4_udp(rng)
        cut = rng.randrange(len(pkt.data) + 1)
        decode(ir, pkt.data[:cut])


def test_random_corruption_never_crashes_the_decoder(feed_ir, eth_ir):
    rng = random.Random(0xC0FFEE)
    for _ in range(2000):
        if rng.random() < 0.5:
            ir, pkt = feed_ir, st.random_feed_packet(rng, seq_num=1)
        else:
            ir, pkt = eth_ir, st.random_eth_ipv4_udp(rng)
        data = bytearray(pkt.data)
        for _ in range(rng.randint(1, 4)):
            if data:
                data[rng.randrange(len(data))] = rng.randrange(256)
        decode(ir, bytes(data))
