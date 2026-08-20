"""Layout arithmetic: the numbers the templates print."""

from __future__ import annotations

import pytest

from wirespec.ir import build_ir
from wirespec.layout import SUPPORTED_DATA_W, LayoutError, build_layout
from wirespec.schema import parse_schema


@pytest.fixture(scope="module")
def L(eth_ir):
    return build_layout(eth_ir, 64)


def test_layer_order_and_parents(L):
    assert [lay.name for lay in L.layers] == ["eth", "ipv4", "udp"]
    assert [lay.parent for lay in L.layers] == [None, "eth", "ipv4"]
    assert [lay.parent_selector_value for lay in L.layers] == [None, 0x0800, 17]


def test_static_versus_dynamic_offsets(L):
    """A layer is dynamic exactly when something before it has a variable length."""
    eth, ipv4, udp = L.layers
    assert eth.static_offset == 0 and not eth.dynamic
    assert ipv4.static_offset == 14 and not ipv4.dynamic
    # udp sits behind IPv4's IHL, so its offset is only known at run time.
    assert udp.static_offset is None and udp.dynamic
    assert udp.max_offset == 74  # 14 + 60


def test_window_and_accumulator_sizes(L):
    assert L.hdr_bytes == 82
    assert L.win_bits == 656
    assert L.acc_beats == 11  # 82 bytes at 8 bytes/beat
    assert L.acc_bytes == 88
    assert L.sh_w == 10  # a bit index into 656 bits


@pytest.mark.parametrize("data_w,beats", [(64, 11), (128, 6), (256, 3), (512, 2)])
def test_accumulator_scales_with_datapath(eth_ir, data_w, beats):
    lay = build_layout(eth_ir, data_w)
    assert lay.acc_beats == beats
    assert lay.acc_bytes >= 82
    assert lay.hdr_bytes == 82  # window size is a protocol property, not a width one


def test_port_names_do_not_stutter(L):
    ports = {f.port for f in L.all_fields}
    assert "o_udp_length" in ports  # udp.length, not o_udp_udp_length
    assert "o_ipv4_frag_offset" in ports
    assert "o_eth_dst_mac" in ports
    assert len(ports) == L.fields_total == 20


def test_record_width(L):
    assert L.record_bits == 336  # eth 112 + ipv4 160 + udp 64


def test_every_layer_fits_in_the_window(L):
    for lay in L.layers:
        assert lay.max_offset + lay.rec.max_bytes <= L.hdr_bytes


def test_segments_survive_into_the_layout(L):
    """Field slices are relative to their own layer's window, not the packet."""
    ipv4 = L.layer("ipv4")
    ihl = next(f for f in ipv4.fields if f.name == "ihl")
    assert ihl.segments[0].msb == 3 and ihl.segments[0].lsb == 0
    total_len = next(f for f in ipv4.fields if f.name == "total_length")
    assert total_len.swapped  # big-endian: two segments, high byte first
    assert [(s.msb, s.lsb) for s in total_len.segments] == [(23, 16), (31, 24)]


def test_unsupported_data_w_is_rejected(eth_ir):
    with pytest.raises(LayoutError) as exc:
        build_layout(eth_ir, 32)
    assert "not supported" in str(exc.value)


def test_framed_schema_is_rejected_for_now(feed_ir):
    with pytest.raises(LayoutError) as exc:
        build_layout(feed_ir, 64)
    assert "only layered schemas" in str(exc.value)


def test_all_supported_widths_build(eth_ir):
    for w in SUPPORTED_DATA_W:
        build_layout(eth_ir, w)


def test_layer_with_two_parents_is_rejected():
    """Two parents means two possible byte offsets for one layer."""
    doc = {
        "name": "diamond",
        "kind": "layered",
        "layers": [
            {
                "name": "top",
                "fields": [{"name": "sel", "bits": 8}, {"name": "pad", "bits": 8}],
                "next": {"selector": "sel", "map": {1: "left", 2: "right"}},
            },
            {
                "name": "left",
                "fields": [{"name": "a", "bits": 8}, {"name": "s2", "bits": 8}],
                "next": {"selector": "s2", "map": {9: "bottom"}},
            },
            {
                "name": "right",
                "fields": [{"name": "b", "bits": 24}, {"name": "s3", "bits": 8}],
                "next": {"selector": "s3", "map": {9: "bottom"}},
            },
            {"name": "bottom", "fields": [{"name": "z", "bits": 16}]},
        ],
    }
    with pytest.raises(LayoutError) as exc:
        build_layout(build_ir(parse_schema(doc)), 64)
    assert "one byte offset" in str(exc.value)


def test_branching_tree_is_accepted():
    """Siblings are fine -- each still has exactly one parent."""
    doc = {
        "name": "forked",
        "kind": "layered",
        "layers": [
            {
                "name": "top",
                "fields": [{"name": "sel", "bits": 8}, {"name": "pad", "bits": 8}],
                "next": {"selector": "sel", "map": {1: "left", 2: "right"}},
            },
            {"name": "left", "fields": [{"name": "a", "bits": 16}]},
            {"name": "right", "fields": [{"name": "b", "bits": 32}]},
        ],
    }
    lay = build_layout(build_ir(parse_schema(doc)), 64)
    assert [x.name for x in lay.layers] == ["top", "left", "right"]
    assert lay.layer("left").static_offset == 2
    assert lay.layer("right").static_offset == 2
    assert lay.hdr_bytes == 6  # 2 + the longer branch
