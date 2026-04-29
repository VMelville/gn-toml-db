"""Single-file Blender add-on for Geometry Nodes YAML IR."""

from __future__ import annotations

import json
from pathlib import Path
import re
from dataclasses import dataclass
from typing import Any

bl_info = {
    "name": "Geometry & Material from YAML IR",
    "author": "ChatGPT + User",
    "version": (0, 8, 0),
    "blender": (5, 0, 0),
    "location": "View3D > Sidebar > YAML IR",
    "description": "Build Geometry Nodes and Material from YAML IR files on disk",
    "category": "Node",
}

try:
    import bpy
    from bpy.props import EnumProperty, StringProperty
    from bpy.types import Operator, Panel
except ImportError:
    bpy = None
    Operator = object
    Panel = object

    def EnumProperty(**_kwargs):
        return None

    def StringProperty(**_kwargs):
        return None


_INT_RE = re.compile(r"^[+-]?(0|[1-9][0-9_]*)$")
_FLOAT_RE = re.compile(
    r"""^[+-]?(
        ([0-9][0-9_]*)?\.[0-9_]+([eE][+-]?[0-9_]+)? |
        [0-9][0-9_]*[eE][+-]?[0-9_]+
    )$""",
    re.VERBOSE,
)


class YAMLIRParseError(ValueError):
    """Raised when the bundled YAML subset parser finds invalid input."""


@dataclass
class _Line:
    number: int
    indent: int
    raw: str
    text: str


@dataclass
class _BuildContext:
    ir_cache: dict[Path, dict[str, Any]]
    active_group_files: set[Path]


def loads_yaml_ir(source: str) -> Any:
    """Parse the project's YAML IR subset."""
    parser = _Parser(source)
    return parser.parse()


class _Parser:
    def __init__(self, source: str):
        self.raw_lines = source.splitlines()
        self.lines = self._prepare_lines(source)
        self.index = 0

    def parse(self) -> Any:
        if not self.lines:
            return None

        result = self._parse_block(self.lines[0].indent)
        if self.index != len(self.lines):
            line = self.lines[self.index]
            raise YAMLIRParseError(f"Unexpected trailing content at line {line.number}")
        return result

    def _prepare_lines(self, source: str) -> list[_Line]:
        prepared: list[_Line] = []

        for number, raw in enumerate(source.splitlines(), start=1):
            indent_text = raw[: len(raw) - len(raw.lstrip(" \t"))]
            if "\t" in indent_text:
                raise YAMLIRParseError(f"Tabs are not supported for indentation (line {number})")

            text_no_comments = _strip_comments(raw)
            if not text_no_comments.strip():
                continue

            indent = len(text_no_comments) - len(text_no_comments.lstrip(" "))
            text = text_no_comments[indent:]
            prepared.append(_Line(number=number, indent=indent, raw=raw, text=text))

        return prepared

    def _parse_block(self, indent: int) -> Any:
        if self.index >= len(self.lines):
            raise YAMLIRParseError("Unexpected end of document")

        line = self.lines[self.index]
        if line.indent < indent:
            raise YAMLIRParseError(f"Unexpected dedent at line {line.number}")
        if line.indent > indent:
            raise YAMLIRParseError(f"Unexpected indentation at line {line.number}")

        if line.text == "-" or line.text.startswith("- "):
            return self._parse_sequence(indent)
        return self._parse_mapping(indent)

    def _parse_sequence(self, indent: int) -> list[Any]:
        items: list[Any] = []

        while self.index < len(self.lines):
            line = self.lines[self.index]
            if line.indent < indent:
                break
            if line.indent > indent:
                raise YAMLIRParseError(f"Unexpected indentation at line {line.number}")
            if not (line.text == "-" or line.text.startswith("- ")):
                break

            payload = line.text[1:].lstrip(" ")
            self.index += 1

            if not payload:
                if self.index < len(self.lines) and self.lines[self.index].indent > indent:
                    items.append(self._parse_block(self.lines[self.index].indent))
                else:
                    items.append(None)
                continue

            if _looks_like_mapping_entry(payload):
                items.append(self._parse_mapping(indent + 2, first_entry=(line.number, payload)))
                continue

            items.append(_parse_inline_value(payload, line.number))

        return items

    def _parse_mapping(
        self,
        indent: int,
        first_entry: tuple[int, str] | None = None,
    ) -> dict[str, Any]:
        mapping: dict[str, Any] = {}

        if first_entry is not None:
            key, value = self._parse_mapping_entry(indent, *first_entry)
            mapping[key] = value

        while self.index < len(self.lines):
            line = self.lines[self.index]
            if line.indent < indent:
                break
            if line.indent > indent:
                raise YAMLIRParseError(f"Unexpected indentation at line {line.number}")
            if line.text == "-" or line.text.startswith("- "):
                raise YAMLIRParseError(f"Unexpected list item at line {line.number}")

            self.index += 1
            key, value = self._parse_mapping_entry(indent, line.number, line.text)
            mapping[key] = value

        return mapping

    def _parse_mapping_entry(self, indent: int, line_number: int, text: str) -> tuple[str, Any]:
        key_text, value_text = _split_mapping_entry(text, line_number)
        key = _parse_key(key_text, line_number)

        if value_text in {"|", "|-", "|+"}:
            keep_newline = value_text != "|-"
            return key, self._parse_block_scalar(indent, line_number, keep_newline=keep_newline)
        if value_text in {">", ">-", ">+"}:
            keep_newline = value_text != ">-"
            literal = self._parse_block_scalar(indent, line_number, keep_newline=keep_newline)
            folded = " ".join(part for part in literal.splitlines() if part)
            return key, folded
        if not value_text:
            if self.index < len(self.lines) and self.lines[self.index].indent > indent:
                return key, self._parse_block(self.lines[self.index].indent)
            return key, None

        return key, _parse_inline_value(value_text, line_number)

    def _parse_block_scalar(self, parent_indent: int, header_line_number: int, keep_newline: bool) -> str:
        start_raw_index = header_line_number
        content_indent: int | None = None
        parts: list[str] = []
        stop_line_number = len(self.raw_lines) + 1

        for raw_index in range(start_raw_index, len(self.raw_lines)):
            raw = self.raw_lines[raw_index]
            indent_text = raw[: len(raw) - len(raw.lstrip(" \t"))]
            if "\t" in indent_text:
                raise YAMLIRParseError(f"Tabs are not supported for indentation (line {raw_index + 1})")

            indent = len(indent_text)
            is_blank = not raw.strip()

            if content_indent is None:
                if is_blank:
                    continue
                if indent <= parent_indent:
                    stop_line_number = raw_index + 1
                    break
                content_indent = indent

            if not is_blank and indent <= parent_indent:
                stop_line_number = raw_index + 1
                break

            if is_blank:
                parts.append("")
                continue

            if indent < content_indent:
                stop_line_number = raw_index + 1
                break

            parts.append(raw[content_indent:])
        else:
            stop_line_number = len(self.raw_lines) + 1

        while self.index < len(self.lines) and self.lines[self.index].number < stop_line_number:
            self.index += 1

        text = "\n".join(parts)
        if keep_newline and parts:
            return text + "\n"
        if keep_newline and not parts:
            return ""
        return text


def _strip_comments(text: str) -> str:
    in_single = False
    in_double = False
    escaped = False

    for index, char in enumerate(text):
        if escaped:
            escaped = False
            continue
        if in_double and char == "\\":
            escaped = True
            continue
        if not in_double and char == "'" and not in_single:
            in_single = True
            continue
        if in_single and char == "'":
            in_single = False
            continue
        if not in_single and char == '"':
            in_double = not in_double
            continue
        if not in_single and not in_double and char == "#":
            return text[:index].rstrip()

    return text.rstrip()


def _looks_like_mapping_entry(text: str) -> bool:
    try:
        _split_mapping_entry(text, 0)
    except YAMLIRParseError:
        return False
    return True


def _split_mapping_entry(text: str, line_number: int) -> tuple[str, str]:
    depth = 0
    in_single = False
    in_double = False
    escaped = False

    for index, char in enumerate(text):
        if escaped:
            escaped = False
            continue
        if in_double and char == "\\":
            escaped = True
            continue
        if not in_double and char == "'" and not in_single:
            in_single = True
            continue
        if in_single and char == "'":
            in_single = False
            continue
        if not in_single and char == '"':
            in_double = not in_double
            continue
        if in_single or in_double:
            continue
        if char in "[{":
            depth += 1
            continue
        if char in "]}":
            depth -= 1
            continue
        if char == ":" and depth == 0:
            return text[:index].rstrip(), text[index + 1 :].lstrip()

    raise YAMLIRParseError(f"Invalid mapping entry at line {line_number}")


def _parse_key(text: str, line_number: int) -> str:
    key = text.strip()
    if not key:
        raise YAMLIRParseError(f"Empty mapping key at line {line_number}")
    if key[0] in {'"', "'"}:
        value = _parse_inline_value(key, line_number)
        if not isinstance(value, str):
            raise YAMLIRParseError(f"Invalid quoted mapping key at line {line_number}")
        return value
    return key


def _parse_inline_value(text: str, line_number: int) -> Any:
    text = text.strip()
    if not text:
        return None

    if text.startswith("["):
        value, end = _parse_flow_sequence(text, 0, line_number)
        if text[end:].strip():
            raise YAMLIRParseError(f"Unexpected trailing characters at line {line_number}")
        return value

    if text.startswith("{"):
        value, end = _parse_flow_mapping(text, 0, line_number)
        if text[end:].strip():
            raise YAMLIRParseError(f"Unexpected trailing characters at line {line_number}")
        return value

    if text[0] == '"':
        try:
            value, end = json.JSONDecoder().raw_decode(text)
        except json.JSONDecodeError as exc:
            raise YAMLIRParseError(f"Invalid double-quoted scalar at line {line_number}: {exc}") from exc
        if text[end:].strip():
            raise YAMLIRParseError(f"Unexpected trailing characters at line {line_number}")
        return value

    if text[0] == "'":
        return _parse_single_quoted_scalar(text, line_number)

    return _parse_plain_scalar(text)


def _parse_flow_sequence(text: str, start: int, line_number: int) -> tuple[list[Any], int]:
    if text[start] != "[":
        raise YAMLIRParseError(f"Invalid flow sequence at line {line_number}")

    index = start + 1
    items: list[Any] = []

    while True:
        index = _skip_ws(text, index)
        if index >= len(text):
            raise YAMLIRParseError(f"Unterminated flow sequence at line {line_number}")
        if text[index] == "]":
            return items, index + 1

        value, index = _parse_flow_value(text, index, line_number)
        items.append(value)

        index = _skip_ws(text, index)
        if index >= len(text):
            raise YAMLIRParseError(f"Unterminated flow sequence at line {line_number}")
        if text[index] == "]":
            return items, index + 1
        if text[index] != ",":
            raise YAMLIRParseError(f"Expected ',' in flow sequence at line {line_number}")
        index += 1


def _parse_flow_mapping(text: str, start: int, line_number: int) -> tuple[dict[str, Any], int]:
    if text[start] != "{":
        raise YAMLIRParseError(f"Invalid flow mapping at line {line_number}")

    index = start + 1
    mapping: dict[str, Any] = {}

    while True:
        index = _skip_ws(text, index)
        if index >= len(text):
            raise YAMLIRParseError(f"Unterminated flow mapping at line {line_number}")
        if text[index] == "}":
            return mapping, index + 1

        key_token, index = _consume_flow_key(text, index, line_number)
        key = _parse_key(key_token, line_number)

        index = _skip_ws(text, index)
        if index >= len(text) or text[index] != ":":
            raise YAMLIRParseError(f"Expected ':' in flow mapping at line {line_number}")
        index += 1
        index = _skip_ws(text, index)

        value, index = _parse_flow_value(text, index, line_number)
        mapping[key] = value

        index = _skip_ws(text, index)
        if index >= len(text):
            raise YAMLIRParseError(f"Unterminated flow mapping at line {line_number}")
        if text[index] == "}":
            return mapping, index + 1
        if text[index] != ",":
            raise YAMLIRParseError(f"Expected ',' in flow mapping at line {line_number}")
        index += 1


def _consume_flow_key(text: str, start: int, line_number: int) -> tuple[str, int]:
    depth = 0
    in_single = False
    in_double = False
    escaped = False

    for index in range(start, len(text)):
        char = text[index]
        if escaped:
            escaped = False
            continue
        if in_double and char == "\\":
            escaped = True
            continue
        if not in_double and char == "'" and not in_single:
            in_single = True
            continue
        if in_single and char == "'":
            in_single = False
            continue
        if not in_single and char == '"':
            in_double = not in_double
            continue
        if in_single or in_double:
            continue
        if char in "[{":
            depth += 1
            continue
        if char in "]}":
            if depth == 0:
                break
            depth -= 1
            continue
        if char == ":" and depth == 0:
            return text[start:index].rstrip(), index

    raise YAMLIRParseError(f"Invalid flow mapping key at line {line_number}")


def _parse_flow_value(text: str, start: int, line_number: int) -> tuple[Any, int]:
    start = _skip_ws(text, start)
    if start >= len(text):
        raise YAMLIRParseError(f"Unexpected end of flow value at line {line_number}")

    char = text[start]
    if char == "[":
        return _parse_flow_sequence(text, start, line_number)
    if char == "{":
        return _parse_flow_mapping(text, start, line_number)
    if char == '"':
        try:
            value, end = json.JSONDecoder().raw_decode(text[start:])
        except json.JSONDecodeError as exc:
            raise YAMLIRParseError(f"Invalid double-quoted scalar at line {line_number}: {exc}") from exc
        return value, start + end
    if char == "'":
        token, end = _consume_single_quoted(text, start, line_number)
        return _parse_single_quoted_scalar(token, line_number), end

    end = start
    depth = 0
    while end < len(text):
        char = text[end]
        if char in "[{":
            depth += 1
        elif char in "]}":
            if depth == 0:
                break
            depth -= 1
        elif char == "," and depth == 0:
            break
        end += 1

    token = text[start:end].strip()
    if not token:
        raise YAMLIRParseError(f"Empty flow value at line {line_number}")
    return _parse_plain_scalar(token), end


def _parse_single_quoted_scalar(text: str, line_number: int) -> str:
    token, end = _consume_single_quoted(text, 0, line_number)
    if text[end:].strip():
        raise YAMLIRParseError(f"Unexpected trailing characters at line {line_number}")
    inner = token[1:-1]
    return inner.replace("''", "'")


def _consume_single_quoted(text: str, start: int, line_number: int) -> tuple[str, int]:
    if text[start] != "'":
        raise YAMLIRParseError(f"Invalid single-quoted scalar at line {line_number}")

    index = start + 1
    while index < len(text):
        if text[index] == "'":
            if index + 1 < len(text) and text[index + 1] == "'":
                index += 2
                continue
            return text[start : index + 1], index + 1
        index += 1

    raise YAMLIRParseError(f"Unterminated single-quoted scalar at line {line_number}")


def _parse_plain_scalar(text: str) -> Any:
    lowered = text.lower()
    if lowered in {"null", "~"}:
        return None
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered in {"inf", ".inf", "+inf", "+.inf"}:
        return float("inf")
    if lowered in {"-inf", "-.inf"}:
        return float("-inf")

    numeric = text.replace("_", "")
    if _INT_RE.match(text):
        try:
            return int(numeric)
        except ValueError:
            pass
    if _FLOAT_RE.match(text):
        try:
            return float(numeric)
        except ValueError:
            pass

    return text


def _skip_ws(text: str, index: int) -> int:
    while index < len(text) and text[index] in " \t":
        index += 1
    return index


def _normalize_socket_name(name: str) -> str:
    return " ".join(name.strip().lower().split())


def _socket_is_geometry(sock: Any) -> bool:
    return (
        getattr(sock, "bl_socket_idname", "") == "NodeSocketGeometry"
        or getattr(sock, "bl_idname", "") == "NodeSocketGeometry"
        or getattr(sock, "type", "") == "GEOMETRY"
    )


def _split_socket_name_index(socket_name: str) -> tuple[str, int | None]:
    stripped = socket_name.strip()

    match = re.fullmatch(r"(.+?)\s+(\d+)", stripped)
    if match is not None:
        return match.group(1), int(match.group(2))

    # Blender often exposes duplicate socket identifiers as Value_001,
    # Value_002, ... while the UI label remains just "Value".
    match = re.fullmatch(r"(.+?)_(\d{3})", stripped)
    if match is not None:
        return match.group(1), int(match.group(2)) + 1

    return socket_name, None


def _get_socket_templates(node: Any, is_input: bool) -> list[Any]:
    template_getter = getattr(type(node), "input_template" if is_input else "output_template", None)
    if template_getter is None:
        return []

    templates: list[Any] = []
    index = 0
    while True:
        try:
            template = template_getter(index)
        except Exception:
            break
        if template is None:
            break
        templates.append(template)
        index += 1

    return templates


def _get_socket_from_templates(collection: Any, socket_name: str):
    sockets = list(collection)
    if not sockets:
        return None

    owner_node = getattr(sockets[0], "node", None)
    if owner_node is None:
        return None

    owner_inputs = list(getattr(owner_node, "inputs", []))
    owner_outputs = list(getattr(owner_node, "outputs", []))

    is_input = None
    if len(owner_inputs) == len(sockets) and all(a is b for a, b in zip(owner_inputs, sockets)):
        is_input = True
    elif len(owner_outputs) == len(sockets) and all(a is b for a, b in zip(owner_outputs, sockets)):
        is_input = False
    else:
        return None

    templates = _get_socket_templates(owner_node, is_input=is_input)
    if not templates:
        return None

    normalized = _normalize_socket_name(socket_name)
    base_name, ordinal = _split_socket_name_index(socket_name)
    normalized_base = _normalize_socket_name(base_name)

    def match_template_name(template: Any, expected: str) -> bool:
        return (
            _normalize_socket_name(getattr(template, "name", "")) == expected
            or _normalize_socket_name(getattr(template, "identifier", "")) == expected
        )

    for index, template in enumerate(templates):
        if index >= len(sockets):
            break
        if match_template_name(template, normalized):
            return sockets[index]

    if ordinal is not None:
        matching_indexes = [
            index
            for index, template in enumerate(templates[: len(sockets)])
            if match_template_name(template, normalized_base)
        ]
        if 1 <= ordinal <= len(matching_indexes):
            return sockets[matching_indexes[ordinal - 1]]

    return None


def _get_socket(collection: Any, socket_name: str, owner_label: str):
    # Prefer Blender's internal socket key lookup first so identifiers like
    # Value_001 remain addressable even when the UI names are duplicated.
    try:
        return collection[socket_name]
    except Exception:
        pass

    normalized = _normalize_socket_name(socket_name)
    sockets = list(collection)
    owner_node = getattr(sockets[0], "node", None) if sockets else None

    if (
        normalized == "iterations"
        and owner_node is not None
        and getattr(owner_node, "bl_idname", "") == "GeometryNodeRepeatOutput"
    ):
        raise KeyError(
            f"{owner_label}: socket 'Iterations' is not available on Repeat Output. "
            "Connect 'Iterations' on the Repeat Input node."
        )

    for sock in sockets:
        if _normalize_socket_name(getattr(sock, "name", "")) == normalized:
            return sock

    base_name, ordinal = _split_socket_name_index(socket_name)
    normalized_base = _normalize_socket_name(base_name)
    matching_base = [
        sock for sock in sockets if _normalize_socket_name(getattr(sock, "name", "")) == normalized_base
    ]
    if ordinal is not None and 1 <= ordinal <= len(matching_base):
        return matching_base[ordinal - 1]

    templated = _get_socket_from_templates(collection, socket_name)
    if templated is not None:
        return templated

    if normalized in {"mesh", "geometry"} or normalized_base in {"mesh", "geometry"}:
        geometry_sockets = [sock for sock in sockets if _socket_is_geometry(sock)]
        if ordinal is not None and 1 <= ordinal <= len(geometry_sockets):
            return geometry_sockets[ordinal - 1]
        if len(geometry_sockets) == 1:
            return geometry_sockets[0]

    available = ", ".join(getattr(sock, "name", "<unnamed>") for sock in sockets)
    raise KeyError(f"{owner_label}: socket '{socket_name}' not found. Available: {available}")


def _get_input_sockets(collection: Any, socket_name: str, item_count: int, owner_label: str) -> list[Any]:
    try:
        return [_get_socket(collection, socket_name, owner_label)]
    except KeyError:
        normalized = _normalize_socket_name(socket_name)
        if normalized not in {"mesh", "geometry"}:
            raise

        geometry_sockets = [sock for sock in list(collection) if _socket_is_geometry(sock)]
        if item_count > 1 and len(geometry_sockets) == item_count:
            return geometry_sockets
        raise


def _require_bpy():
    if bpy is None:
        raise RuntimeError("This operation requires Blender's Python environment")


def _configure_repeat_items(node: Any, items_spec: Any, node_id: str):
    if not items_spec:
        return

    repeat_items = getattr(node, "repeat_items", None)
    if repeat_items is None:
        raise ValueError(f"Node '{node_id}' does not support repeat_items")

    for item in items_spec:
        if not isinstance(item, dict):
            raise ValueError(f"Node '{node_id}' repeat_items must be mappings")
        socket_type = item.get("socket_type")
        name = item.get("name")
        if not isinstance(socket_type, str) or not socket_type:
            raise ValueError(f"Node '{node_id}' repeat item is missing socket_type")
        if not isinstance(name, str) or not name:
            raise ValueError(f"Node '{node_id}' repeat item is missing name")
        repeat_items.new(socket_type, name)


def _pair_zone_node(node: Any, output_node: Any, node_id: str, output_id: str):
    pair_method = getattr(node, "pair_with_output", None)
    if pair_method is None:
        raise ValueError(f"Node '{node_id}' does not support zone_pair")

    paired = pair_method(output_node)
    if paired is False:
        raise ValueError(f"Failed to pair zone node '{node_id}' with '{output_id}'")


def _iter_from_endpoints(spec: Any):
    if isinstance(spec, list):
        for item in spec:
            if isinstance(item, dict) and isinstance(item.get("from"), str):
                yield item["from"]
        return

    if isinstance(spec, dict) and isinstance(spec.get("from"), str):
        yield spec["from"]


def _average(values: list[float], default: float = 0.0) -> float:
    if not values:
        return default
    return sum(values) / len(values)


def _extract_dependency_graph(node_specs: list[dict[str, Any]]) -> tuple[dict[str, set[str]], dict[str, int]]:
    deps_by_node: dict[str, set[str]] = {}
    order_by_node: dict[str, int] = {}

    for index, nd in enumerate(node_specs):
        nid = nd["id"]
        order_by_node[nid] = index
        deps: set[str] = set()

        for spec in nd.get("inputs", {}).values():
            for endpoint in _iter_from_endpoints(spec):
                owner, _sock = endpoint.split(".", 1)
                deps.add(owner)

        deps_by_node[nid] = deps

    return deps_by_node, order_by_node


def _compute_depths(node_ids: list[str], deps_by_node: dict[str, set[str]]) -> dict[str, int]:
    memo: dict[str, int] = {}
    visiting: set[str] = set()

    def depth_of(nid: str) -> int:
        if nid in memo:
            return memo[nid]
        if nid in visiting:
            return 1

        visiting.add(nid)
        max_dep_depth = 0

        for dep in deps_by_node.get(nid, set()):
            if dep in {"input", "output"}:
                continue
            if dep in deps_by_node:
                max_dep_depth = max(max_dep_depth, depth_of(dep))

        visiting.remove(nid)
        memo[nid] = max_dep_depth + 1
        return memo[nid]

    for nid in node_ids:
        depth_of(nid)

    return memo


def _compute_layout_positions(
    node_specs: list[dict[str, Any]],
    output_spec: dict[str, Any],
    include_group_input: bool,
    x_spacing: float = 320.0,
    y_spacing: float = 220.0,
) -> tuple[dict[str, tuple[float, float]], tuple[float, float] | None, tuple[float, float]]:
    if not node_specs:
        input_pos = (-x_spacing, 0.0) if include_group_input else None
        return {}, input_pos, (x_spacing, 0.0)

    deps_by_node, order_by_node = _extract_dependency_graph(node_specs)
    node_ids = [nd["id"] for nd in node_specs]
    depths = _compute_depths(node_ids, deps_by_node)

    layers: dict[int, list[str]] = {}
    for nid in node_ids:
        layers.setdefault(depths[nid], []).append(nid)

    positions: dict[str, tuple[float, float]] = {}
    y_by_node: dict[str, float] = {}

    for depth in sorted(layers):
        layer_ids = layers[depth]

        def sort_key(nid: str):
            upstream_y = [y_by_node[dep] for dep in deps_by_node.get(nid, set()) if dep in y_by_node]
            return (-_average(upstream_y, default=0.0), order_by_node[nid])

        ordered_ids = sorted(layer_ids, key=sort_key)
        top_y = ((len(ordered_ids) - 1) * y_spacing) / 2.0

        for index, nid in enumerate(ordered_ids):
            x = depth * x_spacing
            y = top_y - index * y_spacing
            positions[nid] = (x, y)
            y_by_node[nid] = y

    max_depth = max(depths.values())

    output_sources_y: list[float] = []
    for spec in output_spec.values():
        for endpoint in _iter_from_endpoints(spec):
            owner, _sock = endpoint.split(".", 1)
            if owner in y_by_node:
                output_sources_y.append(y_by_node[owner])
    output_pos = ((max_depth + 1) * x_spacing, _average(output_sources_y, default=0.0))

    input_pos = None
    if include_group_input:
        input_user_y = [y_by_node[nid] for nid in node_ids if "input" in deps_by_node.get(nid, set())]
        input_pos = (-x_spacing, _average(input_user_y, default=0.0))

    return positions, input_pos, output_pos


def _apply_geometry_node_layout(
    node_specs: list[dict[str, Any]],
    output_spec: dict[str, Any],
    node_map: dict[str, Any],
    n_in: Any,
    n_out: Any,
):
    positions, input_pos, output_pos = _compute_layout_positions(
        node_specs,
        output_spec,
        include_group_input=True,
    )

    if input_pos is not None:
        n_in.location = input_pos
    n_out.location = output_pos

    for nid, pos in positions.items():
        node_map[nid].location = pos


def _apply_material_node_layout(
    node_specs: list[dict[str, Any]],
    output_spec: dict[str, Any],
    node_map: dict[str, Any],
    mat_out: Any,
):
    positions, _input_pos, output_pos = _compute_layout_positions(
        node_specs,
        output_spec,
        include_group_input=False,
    )

    mat_out.location = output_pos

    for nid, pos in positions.items():
        node_map[nid].location = pos


def _get_repeat_output_iterations_socket(collection: Any):
    sockets = list(collection)

    named = [sock for sock in sockets if _normalize_socket_name(getattr(sock, "name", "")) == "iterations"]
    if named:
        return named[0]

    typed = [sock for sock in sockets if getattr(sock, "type", "") in {"INT", "VALUE"}]
    if len(typed) == 1:
        return typed[0]

    unnamed_typed = [sock for sock in typed if not getattr(sock, "name", "").strip()]
    if len(unnamed_typed) == 1:
        return unnamed_typed[0]

    non_geometry = [sock for sock in sockets if not _socket_is_geometry(sock)]
    if len(non_geometry) == 1:
        return non_geometry[0]

    return None


def _resolve_directory_path(directory_path: str) -> Path:
    path_text = (directory_path or "").strip()
    if not path_text:
        return Path()

    if bpy is not None:
        path_text = bpy.path.abspath(path_text)

    return Path(path_text).expanduser()


def _normalize_ir_kind(kind: Any) -> str:
    text = str(kind or "object").strip().lower()
    if text in {"group", "node_group", "geometry_node_group"}:
        return "node_group"
    return "object"


def _get_ir_kind(ir: dict[str, Any]) -> str:
    info = ir.get("info", {})
    if not isinstance(info, dict):
        return "object"
    return _normalize_ir_kind(info.get("kind"))


def _get_output_socket_specs(ir: dict[str, Any]) -> list[dict[str, Any]]:
    specs = ir.get("output_socket")
    if specs is None:
        return [{"name": "Geometry", "socket_type": "NodeSocketGeometry"}]
    if not isinstance(specs, list):
        raise ValueError("YAML IR 'output_socket' must be a sequence")
    return specs


def _apply_interface_socket_options(socket: Any, spec: dict[str, Any], in_out: str):
    if "subtype" in spec:
        try:
            socket.subtype = spec["subtype"]
        except Exception:
            pass

    if in_out != "INPUT":
        return

    if "default_value" in spec:
        try:
            socket.default_value = spec["default_value"]
        except Exception:
            pass
    if isinstance(spec.get("min_value"), (int, float)):
        try:
            socket.min_value = spec["min_value"]
        except Exception:
            pass
    if isinstance(spec.get("max_value"), (int, float)):
        try:
            socket.max_value = spec["max_value"]
        except Exception:
            pass


def _ensure_node_group_interface(node_group: Any, ir: dict[str, Any]):
    iface = node_group.interface
    for item in list(iface.items_tree):
        iface.remove(item)

    params = ir.get("parameter", [])
    if not isinstance(params, list):
        raise ValueError("YAML IR 'parameter' must be a sequence")

    for p in params:
        if not isinstance(p, dict):
            raise ValueError("Each parameter entry must be a mapping")
        socket_type = p.get("socket_type")
        name = p.get("name")
        if not isinstance(socket_type, str) or not socket_type:
            raise ValueError("Parameter entry is missing socket_type")
        if not isinstance(name, str) or not name:
            raise ValueError("Parameter entry is missing name")
        socket = iface.new_socket(
            name=name,
            in_out="INPUT",
            socket_type=socket_type,
            description=p.get("description", ""),
        )
        _apply_interface_socket_options(socket, p, in_out="INPUT")

    for spec in _get_output_socket_specs(ir):
        if not isinstance(spec, dict):
            raise ValueError("Each output_socket entry must be a mapping")
        socket_type = spec.get("socket_type")
        name = spec.get("name")
        if not isinstance(socket_type, str) or not socket_type:
            raise ValueError("output_socket entry is missing socket_type")
        if not isinstance(name, str) or not name:
            raise ValueError("output_socket entry is missing name")
        socket = iface.new_socket(
            name=name,
            in_out="OUTPUT",
            socket_type=socket_type,
            description=spec.get("description", ""),
        )
        _apply_interface_socket_options(socket, spec, in_out="OUTPUT")


def _make_build_context(context: _BuildContext | None = None) -> _BuildContext:
    if context is not None:
        return context
    return _BuildContext(ir_cache={}, active_group_files=set())


def _load_ir_from_file_cached(file_path: str | Path, context: _BuildContext) -> dict[str, Any]:
    resolved = Path(file_path).resolve()
    cached = context.ir_cache.get(resolved)
    if cached is not None:
        return cached
    ir = load_ir_from_file(resolved)
    context.ir_cache[resolved] = ir
    return ir


def _looks_like_yaml_path(text: str) -> bool:
    stripped = text.strip()
    lowered = stripped.lower()
    return (
        lowered.endswith(".yaml")
        or lowered.endswith(".yml")
        or "/" in stripped
        or "\\" in stripped
    )


def _iter_group_search_roots(source_file: Path | None) -> list[Path]:
    if source_file is None:
        return []

    source_dir = source_file.parent.resolve()
    roots: list[Path] = []

    def add_root(path: Path):
        resolved = path.resolve()
        if resolved.is_dir() and resolved not in roots:
            roots.append(resolved)

    add_root(source_dir)
    add_root(source_dir / "groups")
    if source_dir.name == "groups":
        add_root(source_dir.parent)
    return roots


def _find_group_yaml_by_name(group_name: str, source_file: Path | None, context: _BuildContext) -> Path | None:
    roots = _iter_group_search_roots(source_file)
    normalized = group_name.strip().lower()

    direct_candidates: list[Path] = []
    for root in roots:
        for suffix in (".yaml", ".yml"):
            candidate = (root / f"{group_name}{suffix}").resolve()
            if candidate.is_file() and candidate not in direct_candidates:
                direct_candidates.append(candidate)

    valid_direct: list[Path] = []
    for candidate in direct_candidates:
        ir = _load_ir_from_file_cached(candidate, context)
        info = ir.get("info", {})
        if _get_ir_kind(ir) == "node_group" and str(info.get("name", "")).strip().lower() == normalized:
            valid_direct.append(candidate)
    if len(valid_direct) == 1:
        return valid_direct[0]
    if len(valid_direct) > 1:
        raise ValueError(f"Multiple node group YAML files found for '{group_name}'")

    matches: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        for pattern in ("*.yaml", "*.yml"):
            for path in root.rglob(pattern):
                resolved = path.resolve()
                if resolved in seen:
                    continue
                seen.add(resolved)
                ir = _load_ir_from_file_cached(resolved, context)
                info = ir.get("info", {})
                if _get_ir_kind(ir) != "node_group":
                    continue
                if str(info.get("name", "")).strip().lower() == normalized:
                    matches.append(resolved)

    if not matches:
        return None
    if len(matches) > 1:
        raise ValueError(f"Multiple node group YAML files found for '{group_name}'")
    return matches[0]


def _resolve_group_yaml_path(node_tree_ref: str, source_file: Path | None, context: _BuildContext) -> Path:
    ref = node_tree_ref.strip()
    if not ref:
        raise ValueError("Node group reference is empty")

    if _looks_like_yaml_path(ref):
        candidates: list[Path] = []
        raw_path = Path(ref).expanduser()
        if raw_path.is_absolute():
            candidates.append(raw_path)
        else:
            if source_file is not None:
                candidates.append((source_file.parent / raw_path).resolve())
            candidates.append(raw_path.resolve())

        for candidate in candidates:
            if candidate.is_file():
                return candidate

        raise FileNotFoundError(f"Node group YAML not found: {ref}")

    found = _find_group_yaml_by_name(ref, source_file, context)
    if found is None:
        raise FileNotFoundError(f"Node group YAML not found for '{ref}'")
    return found


def list_yaml_files(directory_path: str) -> list[Path]:
    directory = _resolve_directory_path(directory_path)
    if not directory_path or not directory.is_dir():
        return []

    try:
        return sorted(
            (
                path
                for path in directory.iterdir()
                if path.is_file() and path.suffix.lower() in {".yaml", ".yml"}
            ),
            key=lambda path: path.name.lower(),
        )
    except OSError:
        return []


def resolve_yaml_file_path(directory_path: str, file_name: str) -> Path:
    if not directory_path.strip():
        raise ValueError("YAML folder path is empty")
    if not file_name or file_name == "NONE":
        raise ValueError("No YAML file selected")

    directory = _resolve_directory_path(directory_path)
    if not directory.is_dir():
        raise ValueError(f"YAML folder not found: {directory}")

    file_path = directory / file_name
    if file_path.suffix.lower() not in {".yaml", ".yml"}:
        raise ValueError(f"Selected file is not YAML: {file_name}")
    if not file_path.is_file():
        raise ValueError(f"YAML file not found: {file_path}")

    return file_path


def load_ir_from_string(source: str) -> dict[str, Any]:
    ir = loads_yaml_ir(source)
    if not isinstance(ir, dict):
        raise ValueError("YAML IR root must be a mapping")
    return ir


# ============================================================
# IR Loader from YAML file
# ============================================================

def load_ir_from_file(file_path: str | Path) -> dict[str, Any]:
    raw = Path(file_path).read_text(encoding="utf-8")
    return load_ir_from_string(raw)


# ============================================================
# Geometry Nodes Builder
# ============================================================

def _resolve_geometry_node_tree_reference(
    node_tree_ref: str,
    source_file: Path | None,
    context: _BuildContext,
):
    _require_bpy()

    ref = node_tree_ref.strip()
    if not ref:
        raise ValueError("Node group reference is empty")

    if not _looks_like_yaml_path(ref):
        existing = bpy.data.node_groups.get(ref)
        if existing is not None:
            return existing

    group_file = _resolve_group_yaml_path(ref, source_file, context)
    ir = _load_ir_from_file_cached(group_file, context)
    if _get_ir_kind(ir) != "node_group":
        raise ValueError(f"Referenced YAML is not a node group: {group_file}")

    info = ir.get("info", {})
    group_name = info.get("name")
    if not isinstance(group_name, str) or not group_name:
        raise ValueError(f"Node group YAML has invalid info.name: {group_file}")

    existing = bpy.data.node_groups.get(group_name)
    if existing is not None:
        return existing

    if group_file in context.active_group_files:
        raise ValueError(f"Cyclic node group dependency detected: {group_file}")

    context.active_group_files.add(group_file)
    try:
        node_group, _obj = build_geometry_nodes_from_ir(
            ir,
            source_file=group_file,
            context=context,
        )
        return node_group
    finally:
        context.active_group_files.remove(group_file)


def build_geometry_nodes_from_ir(
    ir: dict[str, Any],
    *,
    source_file: str | Path | None = None,
    context: _BuildContext | None = None,
):
    _require_bpy()
    context = _make_build_context(context)
    source_path = Path(source_file).resolve() if source_file is not None else None

    info = ir.get("info", {})
    if not isinstance(info, dict):
        raise ValueError("YAML IR 'info' must be a mapping")

    gn_name = info.get("name")
    if not isinstance(gn_name, str) or not gn_name:
        raise ValueError("YAML IR info.name must be a non-empty string")

    ir_kind = _get_ir_kind(ir)

    node_group = bpy.data.node_groups.get(gn_name)
    if node_group is None:
        node_group = bpy.data.node_groups.new(gn_name, "GeometryNodeTree")

    try:
        node_group.is_modifier = ir_kind != "node_group"
    except Exception:
        pass

    _ensure_node_group_interface(node_group, ir)

    nodes = node_group.nodes
    links = node_group.links
    nodes.clear()

    n_in = nodes.new("NodeGroupInput")

    n_out = nodes.new("NodeGroupOutput")

    node_map = {}

    for nd in ir.get("node", []):
        nid = nd["id"]
        ntype = nd["type"]
        node = nodes.new(ntype)
        node.name = nid
        node_map[nid] = node

    for nd in ir.get("node", []):
        nid = nd["id"]
        node = node_map[nid]

        for k, v in nd.get("props", {}).items():
            if k == "node_tree" and isinstance(v, str):
                node.node_tree = _resolve_geometry_node_tree_reference(v, source_path, context)
            elif node.bl_idname == "GeometryNodeSetMaterial" and k == "material" and isinstance(v, str):
                mat = bpy.data.materials.get(v)
                if mat is None:
                    print(f"[YAML-IR] WARNING: material '{v}' not found for SetMaterial node '{nid}' (props)")
                else:
                    node.material = mat
            else:
                setattr(node, k, v)

    for nd in ir.get("node", []):
        nid = nd["id"]
        node = node_map[nid]
        _configure_repeat_items(node, nd.get("repeat_items"), nid)

    for nd in ir.get("node", []):
        nid = nd["id"]
        pair_target = nd.get("zone_pair")
        if not isinstance(pair_target, str) or not pair_target:
            continue
        if pair_target not in node_map:
            raise KeyError(f"Node '{nid}': zone_pair target '{pair_target}' not found")
        _pair_zone_node(node_map[nid], node_map[pair_target], nid, pair_target)

    def resolve_output_socket(endpoint: str):
        owner, sock = endpoint.split(".", 1)
        if owner == "input":
            return _get_socket(n_in.outputs, sock, "GroupInput outputs")
        if owner == "output":
            return _get_socket(n_out.outputs, sock, "GroupOutput outputs")
        return _get_socket(node_map[owner].outputs, sock, f"Node '{owner}' outputs")

    for nd in ir.get("node", []):
        node = node_map[nd["id"]]
        inputs_spec = nd.get("inputs", {})

        for socket_name, spec in inputs_spec.items():
            if isinstance(spec, list):
                input_sockets = _get_input_sockets(
                    node.inputs,
                    socket_name,
                    len(spec),
                    f"Node '{nd['id']}' inputs",
                )
                for index, item in enumerate(spec):
                    in_sock = input_sockets[min(index, len(input_sockets) - 1)]
                    if "from" in item:
                        src = resolve_output_socket(item["from"])
                        links.new(src, in_sock)
                    elif "material" in item and isinstance(item["material"], str):
                        mat = bpy.data.materials.get(item["material"])
                        if mat is None:
                            print(
                                f"[YAML-IR] WARNING: material '{item['material']}' not found "
                                f"for node '{nd['id']}', socket '{socket_name}' (multi)"
                            )
                        else:
                            in_sock.default_value = mat
                    elif "value" in item:
                        in_sock.default_value = item["value"]
                continue

            in_sock = _get_socket(node.inputs, socket_name, f"Node '{nd['id']}' inputs")

            if "from" in spec:
                src = resolve_output_socket(spec["from"])
                links.new(src, in_sock)
            elif "material" in spec and isinstance(spec["material"], str):
                mat = bpy.data.materials.get(spec["material"])
                if mat is None:
                    print(
                        f"[YAML-IR] WARNING: material '{spec['material']}' not found "
                        f"for node '{nd['id']}', socket '{socket_name}'"
                    )
                else:
                    in_sock.default_value = mat
            elif "value" in spec:
                in_sock.default_value = spec["value"]

    for nd in ir.get("node", []):
        node = node_map[nd["id"]]
        outputs_spec = nd.get("outputs", {})

        for socket_name, spec in outputs_spec.items():
            out_sock = _get_socket(node.outputs, socket_name, f"Node '{nd['id']}' outputs")

            if isinstance(spec, list):
                for item in spec:
                    if "value" in item:
                        out_sock.default_value = item["value"]
                continue

            if "value" in spec:
                out_sock.default_value = spec["value"]

    output_spec = ir.get("output", {})
    for socket_name, spec in output_spec.items():
        src = resolve_output_socket(spec["from"])
        dst = _get_socket(n_out.inputs, socket_name, "GroupOutput inputs")
        links.new(src, dst)

    _apply_geometry_node_layout(ir.get("node", []), output_spec, node_map, n_in, n_out)

    if ir_kind == "node_group":
        return node_group, None

    obj = bpy.data.objects.get(gn_name)
    if obj is None:
        mesh = bpy.data.meshes.new(gn_name + "Mesh")
        obj = bpy.data.objects.new(gn_name, mesh)
        bpy.context.collection.objects.link(obj)

    bpy.context.view_layer.objects.active = obj

    mod = None
    for existing_mod in obj.modifiers:
        if existing_mod.type == "NODES" and existing_mod.name == gn_name:
            mod = existing_mod
            break
    if mod is None:
        mod = obj.modifiers.new(name=gn_name, type="NODES")

    mod.node_group = node_group

    return node_group, obj


# ============================================================
# Material Builder
# ============================================================

def build_material_from_ir(ir: dict[str, Any]):
    _require_bpy()

    mat_spec = ir.get("material")
    if not mat_spec:
        return None

    info = ir.get("info", {})
    mat_name = mat_spec.get("name") or (info.get("name", "YAML_Mat") + "_Mat")

    mat = bpy.data.materials.get(mat_name)
    if mat is None:
        mat = bpy.data.materials.new(mat_name)
    mat.use_nodes = True

    nt = mat.node_tree
    nodes = nt.nodes
    links = nt.links
    nodes.clear()

    mat_out = nodes.new("ShaderNodeOutputMaterial")
    mat_out.name = "MaterialOutput"

    node_map = {"MaterialOutput": mat_out}

    for nd in mat_spec.get("node", []):
        nid = nd["id"]
        ntype = nd["type"]
        node = nodes.new(ntype)
        node.name = nid
        node_map[nid] = node

        for k, v in nd.get("props", {}).items():
            setattr(node, k, v)

    def resolve_output_socket(endpoint: str):
        owner, sock = endpoint.split(".", 1)
        return _get_socket(node_map[owner].outputs, sock, f"Material node '{owner}' outputs")

    for nd in mat_spec.get("node", []):
        node = node_map[nd["id"]]
        inputs_spec = nd.get("inputs", {})

        for socket_name, spec in inputs_spec.items():
            if isinstance(spec, list):
                input_sockets = _get_input_sockets(
                    node.inputs,
                    socket_name,
                    len(spec),
                    f"Material node '{nd['id']}' inputs",
                )
                for index, item in enumerate(spec):
                    in_sock = input_sockets[min(index, len(input_sockets) - 1)]
                    if "from" in item:
                        src = resolve_output_socket(item["from"])
                        links.new(src, in_sock)
                    elif "value" in item:
                        in_sock.default_value = item["value"]
                continue

            in_sock = _get_socket(node.inputs, socket_name, f"Material node '{nd['id']}' inputs")

            if "from" in spec:
                src = resolve_output_socket(spec["from"])
                links.new(src, in_sock)
            elif "value" in spec:
                in_sock.default_value = spec["value"]

    for nd in mat_spec.get("node", []):
        node = node_map[nd["id"]]
        outputs_spec = nd.get("outputs", {})

        for socket_name, spec in outputs_spec.items():
            out_sock = _get_socket(node.outputs, socket_name, f"Material node '{nd['id']}' outputs")

            if isinstance(spec, list):
                for item in spec:
                    if "value" in item:
                        out_sock.default_value = item["value"]
                continue

            if "value" in spec:
                out_sock.default_value = spec["value"]

    out_spec = mat_spec.get("output", {})
    for socket_name, spec in out_spec.items():
        if socket_name not in mat_out.inputs:
            continue
        src = resolve_output_socket(spec["from"])
        dst = mat_out.inputs[socket_name]
        links.new(src, dst)

    _apply_material_node_layout(mat_spec.get("node", []), out_spec, node_map, mat_out)

    return mat


# ============================================================
# Shortcut helper
# ============================================================

def build_from_file(directory_path: str, file_name: str):
    """YAML ファイルを指定して Geometry & Material を構築."""
    _require_bpy()

    file_path = resolve_yaml_file_path(directory_path, file_name)
    ir = load_ir_from_file(file_path)
    ir_kind = _get_ir_kind(ir)

    mat = None
    if ir_kind != "node_group":
        mat = build_material_from_ir(ir)

    node_group, obj = build_geometry_nodes_from_ir(
        ir,
        source_file=file_path,
        context=_make_build_context(),
    )

    if mat is not None and obj is not None and hasattr(obj.data, "materials"):
        if obj.data.materials:
            obj.data.materials[0] = mat
        else:
            obj.data.materials.append(mat)

    try:
        bpy.context.scene.render.engine = "BLENDER_EEVEE"
    except Exception:
        pass

    return node_group, mat, obj


# ============================================================
# UI / Operator
# ============================================================

def yaml_file_enum_items(self, context):
    directory_path = ""
    if context is not None and getattr(context, "scene", None) is not None:
        directory_path = getattr(context.scene, "yaml_ir_directory", "")
    elif self is not None:
        directory_path = getattr(self, "yaml_ir_directory", "")

    items = [(path.name, path.name, str(path)) for path in list_yaml_files(directory_path)]
    if not items:
        items.append(("NONE", "NONE", "No YAML files in folder"))
    return items


class OBJECT_OT_build_from_yaml(Operator):
    bl_idname = "object.build_from_yaml_ir"
    bl_label = "Build Geometry & Material from YAML IR"
    bl_description = "選択された YAML ファイルを読み込み、Geometry Nodes と Material を生成"
    bl_options = {"REGISTER", "UNDO"}

    directory_path: StringProperty(
        name="Folder",
        description="YAML files folder",
        subtype="DIR_PATH",
    )

    file_name: StringProperty(
        name="YAML File",
        description="Selected YAML IR file",
    )

    def execute(self, context):
        if not self.directory_path.strip():
            self.report({"ERROR"}, "No YAML folder selected")
            return {"CANCELLED"}

        if not self.file_name or self.file_name == "NONE":
            self.report({"ERROR"}, "No YAML file selected")
            return {"CANCELLED"}

        try:
            file_path = resolve_yaml_file_path(self.directory_path, self.file_name)
            node_group, mat, obj = build_from_file(self.directory_path, self.file_name)
        except Exception as exc:
            self.report({"ERROR"}, f"Failed: {exc}")
            return {"CANCELLED"}

        self.report(
            {"INFO"},
            (
                f"Built node group '{node_group.name}' from '{file_path.name}'"
                if obj is None
                else f"Built '{file_path.name}' as NodeGroup '{node_group.name}' and Material '{mat.name if mat else 'None'}'"
            ),
        )
        return {"FINISHED"}


class VIEW3D_PT_yaml_ir_builder(Panel):
    bl_label = "YAML IR Builder"
    bl_idname = "VIEW3D_PT_yaml_ir_builder"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "YAML IR"

    def draw(self, context):
        layout = self.layout
        col = layout.column()
        col.label(text="Build from YAML File:")
        col.prop(context.scene, "yaml_ir_directory", text="Folder")
        col.prop(context.scene, "yaml_ir_file_name", text="YAML")

        op = col.operator(OBJECT_OT_build_from_yaml.bl_idname, text="Build")
        op.directory_path = context.scene.yaml_ir_directory
        op.file_name = context.scene.yaml_ir_file_name


def register():
    _require_bpy()

    bpy.utils.register_class(OBJECT_OT_build_from_yaml)
    bpy.utils.register_class(VIEW3D_PT_yaml_ir_builder)
    bpy.types.Scene.yaml_ir_directory = StringProperty(
        name="YAML Folder",
        description="Folder containing YAML IR files",
        subtype="DIR_PATH",
        default="//",
    )
    bpy.types.Scene.yaml_ir_file_name = EnumProperty(
        name="YAML File",
        description="YAML IR file in the selected folder",
        items=yaml_file_enum_items,
    )


def unregister():
    _require_bpy()

    del bpy.types.Scene.yaml_ir_file_name
    del bpy.types.Scene.yaml_ir_directory
    bpy.utils.unregister_class(VIEW3D_PT_yaml_ir_builder)
    bpy.utils.unregister_class(OBJECT_OT_build_from_yaml)


if __name__ == "__main__" and bpy is not None:
    register()
