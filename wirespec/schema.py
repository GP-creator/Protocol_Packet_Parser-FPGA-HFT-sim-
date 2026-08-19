"""YAML schema loading and validation.

The schema language describes a binary wire protocol in one of two shapes:

``kind: layered``
    A chain of fixed-layout headers, each optionally selecting the next layer
    from one of its own field values, and optionally declaring a variable
    header length derived from a field (IPv4's IHL).

``kind: framed``
    A fixed packet header followed by N length-prefixed, type-discriminated
    messages, each message type having its own fixed field layout.

Both shapes share the field model.  A record is a big-endian (MSB-first) bit
string; every field is a run of bits in that string.  Field widths that are not
a multiple of eight are permitted (IPv4 version/IHL/flags/fragment-offset), and
so are runs that straddle a byte boundary.

Validation is deliberately hand-written rather than delegated to a schema
library, so that every rejection carries a message naming the offending path.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field as dc_field
from pathlib import Path
from typing import Any

import yaml

MAX_INT_BITS = 64


class SchemaError(ValueError):
    """Raised for any malformed schema.  The message always names a path."""

    def __init__(self, path: str, message: str) -> None:
        super().__init__(f"{path}: {message}")
        self.path = path
        self.message = message


class Endian(enum.Enum):
    BIG = "big"
    LITTLE = "little"
    NONE = "none"  # byte-transparent: char / bytes / sub-byte integers


class Kind(enum.Enum):
    UINT = "uint"
    INT = "int"
    CHAR = "char"
    BYTES = "bytes"
    ENUM = "enum"

    @property
    def is_integer(self) -> bool:
        return self in (Kind.UINT, Kind.INT, Kind.ENUM)

    @property
    def is_signed(self) -> bool:
        return self is Kind.INT


class LengthCovers(enum.Enum):
    """What the message length prefix counts."""

    AFTER_LENGTH = "after_length"  # total = len_field_bytes + value
    WHOLE_MESSAGE = "whole_message"  # total = value
    PAYLOAD_ONLY = "payload_only"  # total = len_bytes + type_bytes + value


# --------------------------------------------------------------------------
# Dataclasses
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class EnumDef:
    name: str
    width_bits: int
    values: dict[str, int]

    def name_of(self, value: int) -> str | None:
        for k, v in self.values.items():
            if v == value:
                return k
        return None


@dataclass(frozen=True)
class Field:
    name: str
    width_bits: int
    kind: Kind
    endian: Endian
    enum: str | None = None
    doc: str = ""

    @property
    def width_bytes(self) -> int:
        assert self.width_bits % 8 == 0
        return self.width_bits // 8


@dataclass(frozen=True)
class HeaderLenExpr:
    """Variable header length: ``value(field) * scale``, clamped by min/max."""

    field: str
    scale: int
    min_bytes: int
    max_bytes: int


@dataclass(frozen=True)
class Layer:
    name: str
    fields: tuple[Field, ...]
    header_len: HeaderLenExpr | None = None
    next_selector: str | None = None
    next_map: dict[int, str] = dc_field(default_factory=dict)
    doc: str = ""

    @property
    def fixed_bits(self) -> int:
        return sum(f.width_bits for f in self.fields)

    @property
    def fixed_bytes(self) -> int:
        return self.fixed_bits // 8


@dataclass(frozen=True)
class MessageType:
    name: str
    code: int
    fields: tuple[Field, ...]
    doc: str = ""

    @property
    def payload_bits(self) -> int:
        return sum(f.width_bits for f in self.fields)


@dataclass(frozen=True)
class MessageSpec:
    length_field: Field
    type_field: Field
    covers: LengthCovers
    types: tuple[MessageType, ...]
    # Names of packet-header fields the decoder cross-checks against the
    # message stream.  Optional; both name integer fields of the header.
    count_field: str | None = None
    sequence_field: str | None = None

    @property
    def prefix_bytes(self) -> int:
        return (self.length_field.width_bits + self.type_field.width_bits) // 8


@dataclass(frozen=True)
class Schema:
    name: str
    kind: str  # "layered" | "framed"
    endian: Endian
    doc: str = ""
    enums: dict[str, EnumDef] = dc_field(default_factory=dict)
    # layered
    start: str | None = None
    layers: tuple[Layer, ...] = ()
    # framed
    header: Layer | None = None
    messages: MessageSpec | None = None

    @property
    def is_layered(self) -> bool:
        return self.kind == "layered"

    @property
    def is_framed(self) -> bool:
        return self.kind == "framed"

    def layer(self, name: str) -> Layer:
        for lay in self.layers:
            if lay.name == name:
                return lay
        raise KeyError(name)


# --------------------------------------------------------------------------
# Parsing helpers
# --------------------------------------------------------------------------

_IDENT_OK = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")

_SV_RESERVED = {
    "begin", "end", "module", "endmodule", "logic", "wire", "reg", "input",
    "output", "inout", "always", "assign", "case", "endcase", "if", "else",
    "for", "while", "function", "endfunction", "task", "endtask", "parameter",
    "localparam", "typedef", "struct", "enum", "package", "endpackage", "bit",
    "byte", "int", "longint", "shortint", "signed", "unsigned", "default",
    "type", "return", "generate", "endgenerate", "genvar", "initial", "final",
}


def _require_mapping(node: Any, path: str) -> dict[str, Any]:
    if not isinstance(node, dict):
        raise SchemaError(path, f"expected a mapping, got {type(node).__name__}")
    return node


def _require_list(node: Any, path: str) -> list[Any]:
    if not isinstance(node, list):
        raise SchemaError(path, f"expected a list, got {type(node).__name__}")
    return node


def _check_keys(node: dict[str, Any], path: str, allowed: set[str], required: set[str]) -> None:
    for key in required:
        if key not in node:
            raise SchemaError(path, f"missing required key '{key}'")
    for key in node:
        if key not in allowed:
            near = sorted(allowed)
            raise SchemaError(path, f"unknown key '{key}' (allowed: {', '.join(near)})")


def _check_ident(name: Any, path: str) -> str:
    if not isinstance(name, str) or not name:
        raise SchemaError(path, "name must be a non-empty string")
    if name[0].isdigit():
        raise SchemaError(path, f"name '{name}' may not start with a digit")
    bad = sorted(set(name) - _IDENT_OK)
    if bad:
        raise SchemaError(path, f"name '{name}' contains illegal characters: {bad}")
    if name.lower() in _SV_RESERVED:
        raise SchemaError(path, f"name '{name}' is a SystemVerilog reserved word")
    return name


def _int_value(node: Any, path: str) -> int:
    """Accept an int, or a string like '0x0800' / '17'."""
    if isinstance(node, bool):
        raise SchemaError(path, "expected an integer, got a boolean")
    if isinstance(node, int):
        return node
    if isinstance(node, str):
        try:
            return int(node, 0)
        except ValueError:
            raise SchemaError(path, f"cannot parse '{node}' as an integer") from None
    raise SchemaError(path, f"expected an integer, got {type(node).__name__}")


# --------------------------------------------------------------------------
# Field parsing
# --------------------------------------------------------------------------

_FIELD_KEYS = {"name", "bits", "bytes", "type", "endian", "enum", "doc"}


def _parse_field(node: Any, path: str, default_endian: Endian, enums: dict[str, EnumDef]) -> Field:
    node = _require_mapping(node, path)
    _check_keys(node, path, _FIELD_KEYS, {"name"})
    name = _check_ident(node["name"], f"{path}.name")
    fpath = f"{path}[{name}]"

    if ("bits" in node) == ("bytes" in node):
        raise SchemaError(fpath, "specify exactly one of 'bits' or 'bytes'")
    if "bits" in node:
        width = _int_value(node["bits"], f"{fpath}.bits")
    else:
        width = _int_value(node["bytes"], f"{fpath}.bytes") * 8
    if width <= 0:
        raise SchemaError(fpath, f"width must be positive, got {width} bits")

    type_name = node.get("type", "uint")
    try:
        kind = Kind(type_name)
    except ValueError:
        raise SchemaError(
            fpath, f"unknown type '{type_name}' (expected one of {[k.value for k in Kind]})"
        ) from None

    enum_name = node.get("enum")
    if kind is Kind.ENUM:
        if enum_name is None:
            raise SchemaError(fpath, "type 'enum' requires an 'enum' key naming the enum")
        if enum_name not in enums:
            raise SchemaError(fpath, f"references unknown enum '{enum_name}'")
        if enums[enum_name].width_bits != width:
            raise SchemaError(
                fpath,
                f"is {width} bits but enum '{enum_name}' is "
                f"{enums[enum_name].width_bits} bits",
            )
    elif enum_name is not None:
        raise SchemaError(fpath, f"has an 'enum' key but type is '{type_name}', not 'enum'")

    if kind.is_integer and width > MAX_INT_BITS:
        raise SchemaError(fpath, f"integer width {width} exceeds the {MAX_INT_BITS}-bit limit")
    if kind in (Kind.CHAR, Kind.BYTES) and width % 8 != 0:
        raise SchemaError(fpath, f"type '{kind.value}' must be a whole number of bytes, got {width} bits")

    # Endianness.  Byte-transparent kinds and sub-byte integers have no byte
    # order to speak of; forcing them to NONE keeps the IR unambiguous.
    if "endian" in node:
        raw = node["endian"]
        try:
            endian = Endian(raw)
        except ValueError:
            raise SchemaError(fpath, f"unknown endian '{raw}' (expected big, little or none)") from None
    else:
        endian = default_endian
    if kind in (Kind.CHAR, Kind.BYTES) or width <= 8:
        endian = Endian.NONE
    if endian is Endian.LITTLE and width % 8 != 0:
        raise SchemaError(
            fpath, f"little-endian field must be a whole number of bytes, got {width} bits"
        )

    return Field(name, width, kind, endian, enum_name, str(node.get("doc", "")))


def _parse_fields(
    node: Any, path: str, default_endian: Endian, enums: dict[str, EnumDef]
) -> tuple[Field, ...]:
    items = _require_list(node, path)
    if not items:
        raise SchemaError(path, "must declare at least one field")
    fields: list[Field] = []
    seen: set[str] = set()
    for i, item in enumerate(items):
        f = _parse_field(item, f"{path}[{i}]", default_endian, enums)
        if f.name in seen:
            raise SchemaError(path, f"duplicate field name '{f.name}'")
        seen.add(f.name)
        fields.append(f)
    total = sum(f.width_bits for f in fields)
    if total % 8 != 0:
        raise SchemaError(path, f"total width {total} bits is not a whole number of bytes")
    return tuple(fields)


# --------------------------------------------------------------------------
# Section parsing
# --------------------------------------------------------------------------


def _parse_enums(node: Any, path: str) -> dict[str, EnumDef]:
    node = _require_mapping(node, path)
    out: dict[str, EnumDef] = {}
    for ename, body in node.items():
        epath = f"{path}.{ename}"
        _check_ident(ename, epath)
        body = _require_mapping(body, epath)
        _check_keys(body, epath, {"width", "values"}, {"width", "values"})
        width = _int_value(body["width"], f"{epath}.width")
        if width <= 0 or width > MAX_INT_BITS:
            raise SchemaError(epath, f"width {width} is out of range 1..{MAX_INT_BITS}")
        values = _require_mapping(body["values"], f"{epath}.values")
        parsed: dict[str, int] = {}
        for vname, vnode in values.items():
            _check_ident(vname, f"{epath}.values.{vname}")
            val = _int_value(vnode, f"{epath}.values.{vname}")
            if not 0 <= val < (1 << width):
                raise SchemaError(
                    f"{epath}.values.{vname}", f"value {val} does not fit in {width} bits"
                )
            if val in parsed.values():
                raise SchemaError(f"{epath}.values.{vname}", f"duplicate value {val}")
            parsed[vname] = val
        if not parsed:
            raise SchemaError(epath, "must declare at least one value")
        out[ename] = EnumDef(ename, width, parsed)
    return out


_LAYER_KEYS = {"name", "fields", "header_bytes", "next", "doc"}
_HDRLEN_KEYS = {"field", "scale", "min", "max"}
_NEXT_KEYS = {"selector", "map"}


def _parse_layer(node: Any, path: str, default_endian: Endian, enums: dict[str, EnumDef]) -> Layer:
    node = _require_mapping(node, path)
    _check_keys(node, path, _LAYER_KEYS, {"name", "fields"})
    name = _check_ident(node["name"], f"{path}.name")
    lpath = f"{path}[{name}]"
    fields = _parse_fields(node["fields"], f"{lpath}.fields", default_endian, enums)
    by_name = {f.name: f for f in fields}

    hdr_len = None
    if "header_bytes" in node:
        h = _require_mapping(node["header_bytes"], f"{lpath}.header_bytes")
        _check_keys(h, f"{lpath}.header_bytes", _HDRLEN_KEYS, {"field", "scale"})
        hfield = h["field"]
        if hfield not in by_name:
            raise SchemaError(
                f"{lpath}.header_bytes.field", f"'{hfield}' is not a field of layer '{name}'"
            )
        if not by_name[hfield].kind.is_integer:
            raise SchemaError(
                f"{lpath}.header_bytes.field", f"'{hfield}' must be an integer field"
            )
        scale = _int_value(h["scale"], f"{lpath}.header_bytes.scale")
        if scale < 1:
            raise SchemaError(f"{lpath}.header_bytes.scale", f"scale must be >= 1, got {scale}")
        fixed = sum(f.width_bits for f in fields) // 8
        lo = _int_value(h.get("min", fixed), f"{lpath}.header_bytes.min")
        hi = _int_value(h.get("max", fixed), f"{lpath}.header_bytes.max")
        if lo > hi:
            raise SchemaError(f"{lpath}.header_bytes", f"min {lo} exceeds max {hi}")
        if lo < fixed:
            raise SchemaError(
                f"{lpath}.header_bytes.min",
                f"min {lo} is smaller than the {fixed} bytes of declared fields",
            )
        hdr_len = HeaderLenExpr(hfield, scale, lo, hi)

    selector = None
    next_map: dict[int, str] = {}
    if "next" in node:
        n = _require_mapping(node["next"], f"{lpath}.next")
        _check_keys(n, f"{lpath}.next", _NEXT_KEYS, {"selector", "map"})
        selector = n["selector"]
        if selector not in by_name:
            raise SchemaError(
                f"{lpath}.next.selector", f"'{selector}' is not a field of layer '{name}'"
            )
        if not by_name[selector].kind.is_integer:
            raise SchemaError(f"{lpath}.next.selector", f"'{selector}' must be an integer field")
        sel_width = by_name[selector].width_bits
        raw_map = _require_mapping(n["map"], f"{lpath}.next.map")
        if not raw_map:
            raise SchemaError(f"{lpath}.next.map", "must declare at least one entry")
        for key, target in raw_map.items():
            val = _int_value(key, f"{lpath}.next.map key")
            if not 0 <= val < (1 << sel_width):
                raise SchemaError(
                    f"{lpath}.next.map", f"selector value {val} does not fit in {sel_width} bits"
                )
            if val in next_map:
                raise SchemaError(f"{lpath}.next.map", f"duplicate selector value {val}")
            if not isinstance(target, str):
                raise SchemaError(f"{lpath}.next.map", f"target for {val} must be a layer name")
            next_map[val] = target

    return Layer(name, fields, hdr_len, selector, next_map, str(node.get("doc", "")))


_MSG_KEYS = {"length", "type", "length_covers", "types", "count_field", "sequence_field"}
_MSGTYPE_KEYS = {"name", "code", "fields", "doc"}


def _parse_messages(
    node: Any, path: str, default_endian: Endian, enums: dict[str, EnumDef]
) -> MessageSpec:
    node = _require_mapping(node, path)
    _check_keys(node, path, _MSG_KEYS, {"length", "type", "types"})

    length_field = _parse_field(node["length"], f"{path}.length", default_endian, enums)
    if not length_field.kind.is_integer:
        raise SchemaError(f"{path}.length", "the length prefix must be an integer field")
    if length_field.width_bits % 8 != 0:
        raise SchemaError(f"{path}.length", "the length prefix must be a whole number of bytes")

    type_field = _parse_field(node["type"], f"{path}.type", default_endian, enums)
    if not type_field.kind.is_integer:
        raise SchemaError(f"{path}.type", "the type discriminator must be an integer field")
    if type_field.width_bits % 8 != 0:
        raise SchemaError(f"{path}.type", "the type discriminator must be a whole number of bytes")
    if length_field.name == type_field.name:
        raise SchemaError(path, f"length and type fields share the name '{length_field.name}'")

    covers_raw = node.get("length_covers", "after_length")
    try:
        covers = LengthCovers(covers_raw)
    except ValueError:
        raise SchemaError(
            f"{path}.length_covers",
            f"unknown value '{covers_raw}' (expected one of {[c.value for c in LengthCovers]})",
        ) from None

    items = _require_list(node["types"], f"{path}.types")
    if not items:
        raise SchemaError(f"{path}.types", "must declare at least one message type")
    types: list[MessageType] = []
    seen_names: set[str] = set()
    seen_codes: dict[int, str] = {}
    reserved = {length_field.name, type_field.name}
    for i, item in enumerate(items):
        tpath = f"{path}.types[{i}]"
        item = _require_mapping(item, tpath)
        _check_keys(item, tpath, _MSGTYPE_KEYS, {"name", "code", "fields"})
        tname = _check_ident(item["name"], f"{tpath}.name")
        tpath = f"{path}.types[{tname}]"
        if tname in seen_names:
            raise SchemaError(f"{path}.types", f"duplicate message type name '{tname}'")
        seen_names.add(tname)
        code = _int_value(item["code"], f"{tpath}.code")
        if not 0 <= code < (1 << type_field.width_bits):
            raise SchemaError(
                f"{tpath}.code",
                f"code {code} does not fit in the {type_field.width_bits}-bit type field",
            )
        if code in seen_codes:
            raise SchemaError(
                f"{tpath}.code", f"code {code} is already used by message type '{seen_codes[code]}'"
            )
        seen_codes[code] = tname
        fields = _parse_fields(item["fields"], f"{tpath}.fields", default_endian, enums)
        clash = sorted({f.name for f in fields} & reserved)
        if clash:
            raise SchemaError(
                f"{tpath}.fields", f"field name(s) {clash} collide with the length/type prefix"
            )
        types.append(MessageType(tname, code, fields, str(item.get("doc", ""))))

    count_field = node.get("count_field")
    sequence_field = node.get("sequence_field")
    for key, val in (("count_field", count_field), ("sequence_field", sequence_field)):
        if val is not None and not isinstance(val, str):
            raise SchemaError(f"{path}.{key}", "must name a field of the packet header")

    return MessageSpec(
        length_field, type_field, covers, tuple(types), count_field, sequence_field
    )


# --------------------------------------------------------------------------
# Top level
# --------------------------------------------------------------------------

_TOP_KEYS = {"name", "kind", "endian", "doc", "enums", "start", "layers", "header", "messages"}


def parse_schema(doc: Any, *, source: str = "<schema>") -> Schema:
    """Validate a already-loaded YAML document and build a :class:`Schema`."""
    doc = _require_mapping(doc, source)
    _check_keys(doc, source, _TOP_KEYS, {"name", "kind"})

    name = _check_ident(doc["name"], f"{source}.name")
    kind = doc["kind"]
    if kind not in ("layered", "framed"):
        raise SchemaError(f"{source}.kind", f"unknown kind '{kind}' (expected layered or framed)")

    endian_raw = doc.get("endian", "big")
    try:
        endian = Endian(endian_raw)
    except ValueError:
        raise SchemaError(
            f"{source}.endian", f"unknown endian '{endian_raw}' (expected big or little)"
        ) from None
    if endian is Endian.NONE:
        raise SchemaError(f"{source}.endian", "the protocol default endian must be big or little")

    enums = _parse_enums(doc["enums"], f"{source}.enums") if "enums" in doc else {}

    if kind == "layered":
        if "layers" not in doc:
            raise SchemaError(source, "a layered schema must declare 'layers'")
        for stray in ("header", "messages"):
            if stray in doc:
                raise SchemaError(source, f"a layered schema must not declare '{stray}'")
        items = _require_list(doc["layers"], f"{source}.layers")
        if not items:
            raise SchemaError(f"{source}.layers", "must declare at least one layer")
        layers: list[Layer] = []
        seen: set[str] = set()
        for i, item in enumerate(items):
            lay = _parse_layer(item, f"{source}.layers[{i}]", endian, enums)
            if lay.name in seen:
                raise SchemaError(f"{source}.layers", f"duplicate layer name '{lay.name}'")
            seen.add(lay.name)
            layers.append(lay)
        start = doc.get("start", layers[0].name)
        if start not in seen:
            raise SchemaError(f"{source}.start", f"'{start}' is not a declared layer")
        for lay in layers:
            for val, target in lay.next_map.items():
                if target not in seen:
                    raise SchemaError(
                        f"{source}.layers[{lay.name}].next.map",
                        f"selector {val} targets unknown layer '{target}'",
                    )
                if target == lay.name:
                    raise SchemaError(
                        f"{source}.layers[{lay.name}].next.map",
                        f"selector {val} targets its own layer, which would not terminate",
                    )
        _check_acyclic(layers, start, source)
        schema = Schema(
            name=name,
            kind=kind,
            endian=endian,
            doc=str(doc.get("doc", "")),
            enums=enums,
            start=start,
            layers=tuple(layers),
        )
    else:
        for required in ("header", "messages"):
            if required not in doc:
                raise SchemaError(source, f"a framed schema must declare '{required}'")
        for stray in ("layers", "start"):
            if stray in doc:
                raise SchemaError(source, f"a framed schema must not declare '{stray}'")
        header = _parse_layer(doc["header"], f"{source}.header", endian, enums)
        if header.next_selector is not None:
            raise SchemaError(f"{source}.header", "a framed packet header may not declare 'next'")
        messages = _parse_messages(doc["messages"], f"{source}.messages", endian, enums)
        hdr_fields = {f.name: f for f in header.fields}
        for key in ("count_field", "sequence_field"):
            ref = getattr(messages, key)
            if ref is None:
                continue
            if ref not in hdr_fields:
                raise SchemaError(
                    f"{source}.messages.{key}", f"'{ref}' is not a field of the packet header"
                )
            if not hdr_fields[ref].kind.is_integer:
                raise SchemaError(
                    f"{source}.messages.{key}", f"'{ref}' must be an integer field"
                )
        schema = Schema(
            name=name,
            kind=kind,
            endian=endian,
            doc=str(doc.get("doc", "")),
            enums=enums,
            header=header,
            messages=messages,
        )

    return schema


def _check_acyclic(layers: list[Layer], start: str, source: str) -> None:
    """Reject a layer graph with a cycle: the golden decoder walks it eagerly."""
    by_name = {lay.name: lay for lay in layers}
    state: dict[str, int] = {}

    def visit(node: str, stack: list[str]) -> None:
        if state.get(node) == 2:
            return
        if state.get(node) == 1:
            cycle = " -> ".join(stack + [node])
            raise SchemaError(f"{source}.layers", f"layer chain contains a cycle: {cycle}")
        state[node] = 1
        for target in by_name[node].next_map.values():
            visit(target, stack + [node])
        state[node] = 2

    visit(start, [])


def load_schema(path: str | Path) -> Schema:
    """Load and validate a schema from a YAML file."""
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise SchemaError(str(p), f"cannot read schema file: {exc}") from None
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise SchemaError(str(p), f"invalid YAML: {exc}") from None
    if doc is None:
        raise SchemaError(str(p), "schema file is empty")
    return parse_schema(doc, source=p.name)
