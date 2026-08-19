"""Schema loading and, mostly, schema rejection."""

from __future__ import annotations

import pytest
import yaml

from wirespec.schema import Endian, Kind, LengthCovers, SchemaError, load_schema, parse_schema

from .conftest import INVALID_DIR


def _expected_message(path) -> str:
    """Every invalid fixture declares the substring its error must contain."""
    first = path.read_text(encoding="utf-8").splitlines()[0]
    assert first.startswith("# expect: "), f"{path.name} is missing its '# expect:' line"
    return first[len("# expect: ") :].strip()


INVALID_FILES = sorted(INVALID_DIR.glob("*.yaml"))


def test_invalid_fixtures_exist():
    assert len(INVALID_FILES) >= 25, "the invalid-schema corpus has shrunk unexpectedly"


@pytest.mark.parametrize("path", INVALID_FILES, ids=lambda p: p.stem)
def test_invalid_schema_is_rejected(path):
    want = _expected_message(path)
    with pytest.raises(SchemaError) as exc:
        load_schema(path)
    assert want in str(exc.value), (
        f"{path.name}: expected the error to mention {want!r}, got {exc.value!r}"
    )
    # The path prefix is what makes these messages actionable.
    assert exc.value.path, "SchemaError must carry a non-empty path"


def test_eth_schema_shape(eth_schema):
    assert eth_schema.is_layered
    assert eth_schema.start == "eth"
    assert [lay.name for lay in eth_schema.layers] == ["eth", "ipv4", "udp"]

    eth = eth_schema.layer("eth")
    assert eth.fixed_bytes == 14
    assert eth.next_map == {0x0800: "ipv4"}

    ip = eth_schema.layer("ipv4")
    assert ip.fixed_bytes == 20
    assert ip.header_len is not None
    assert (ip.header_len.field, ip.header_len.scale) == ("ihl", 4)
    assert (ip.header_len.min_bytes, ip.header_len.max_bytes) == (20, 60)

    assert eth_schema.layer("udp").fixed_bytes == 8
    assert eth_schema.layer("udp").next_selector is None


def test_feed_schema_shape(feed_schema):
    assert feed_schema.is_framed
    assert feed_schema.header is not None
    assert feed_schema.header.fixed_bytes == 16
    spec = feed_schema.messages
    assert spec is not None
    assert spec.covers is LengthCovers.AFTER_LENGTH
    assert spec.prefix_bytes == 3
    assert spec.count_field == "msg_count"
    assert spec.sequence_field == "seq_num"
    assert [t.name for t in spec.types] == ["trade", "quote", "status", "imbalance"]
    assert [t.code for t in spec.types] == [1, 2, 3, 4]


def test_field_endianness_normalisation(feed_schema):
    """Byte-transparent and sub-byte fields must land on Endian.NONE."""
    trade = next(t for t in feed_schema.messages.types if t.name == "trade")
    by_name = {f.name: f for f in trade.fields}
    assert by_name["symbol"].kind is Kind.CHAR
    assert by_name["symbol"].endian is Endian.NONE
    assert by_name["price"].endian is Endian.BIG
    assert by_name["price"].kind is Kind.INT
    assert by_name["trade_flags"].endian is Endian.NONE  # 8 bits: no byte order


def test_subbyte_ipv4_fields_are_endian_none(eth_schema):
    ip = {f.name: f for f in eth_schema.layer("ipv4").fields}
    assert ip["version"].endian is Endian.NONE
    assert ip["frag_offset"].endian is Endian.BIG  # 13 bits, still ordered MSB-first
    assert ip["total_length"].endian is Endian.BIG


def test_enum_lookup(eth_schema):
    et = eth_schema.enums["ethertype_e"]
    assert et.values["IPV4"] == 0x0800
    assert et.name_of(0x0806) == "ARP"
    assert et.name_of(0x1234) is None


def test_start_defaults_to_first_layer():
    doc = yaml.safe_load(
        """
        name: two_layer
        kind: layered
        layers:
          - name: first
            fields: [{name: sel, bits: 8}, {name: pad, bits: 8}]
            next: {selector: sel, map: {1: second}}
          - name: second
            fields: [{name: v, bits: 16}]
        """
    )
    assert parse_schema(doc).start == "first"


def test_missing_file_is_a_schema_error(tmp_path):
    with pytest.raises(SchemaError) as exc:
        load_schema(tmp_path / "nope.yaml")
    assert "cannot read schema file" in str(exc.value)


def test_empty_file_is_a_schema_error(tmp_path):
    p = tmp_path / "empty.yaml"
    p.write_text("# nothing here\n", encoding="utf-8")
    with pytest.raises(SchemaError) as exc:
        load_schema(p)
    assert "empty" in str(exc.value)


def test_malformed_yaml_is_a_schema_error(tmp_path):
    p = tmp_path / "broken.yaml"
    p.write_text("name: x\n  kind: [unclosed\n", encoding="utf-8")
    with pytest.raises(SchemaError) as exc:
        load_schema(p)
    assert "invalid YAML" in str(exc.value)


def test_non_mapping_top_level():
    with pytest.raises(SchemaError) as exc:
        parse_schema([1, 2, 3])
    assert "expected a mapping" in str(exc.value)


@pytest.mark.parametrize("literal", ["0x0800", 2048, "2048"])
def test_integer_literals_accept_hex_and_decimal(literal):
    doc = {
        "name": "lit",
        "kind": "layered",
        "layers": [
            {
                "name": "a",
                "fields": [{"name": "sel", "bits": 16}],
                "next": {"selector": "sel", "map": {literal: "b"}},
            },
            {"name": "b", "fields": [{"name": "v", "bits": 8}, {"name": "w", "bits": 8}]},
        ],
    }
    schema = parse_schema(doc)
    assert schema.layer("a").next_map == {0x0800: "b"}
