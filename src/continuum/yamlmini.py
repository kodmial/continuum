"""Dependency-free parser for the restricted YAML subset used by Continuum config.

Continuum review automation must run on stock `ubuntu-latest` runners without
`pip install`, so a full YAML implementation is not available. Instead of
pretending to be YAML, this module implements a deliberately small subset and
rejects everything else. A configuration file that uses an unsupported
construct fails closed with a precise error instead of being misread.

Supported:
  * block mappings and block sequences, nested by indentation (spaces only)
  * plain, single-quoted and double-quoted scalars
  * `true` / `false` / `null` (and `~`) plus integer and float scalars
  * `#` comments, blank lines, `---` document start

Rejected (always, with a line number):
  * tabs used for indentation
  * anchors (`&`), aliases (`*`), tags (`!`), merge keys (`<<`)
  * flow collections (`{...}`, `[...]`) and multi-line block scalars (`|`, `>`)
  * multiple documents, duplicate mapping keys
"""

from __future__ import annotations

from typing import Any, List, Optional, Tuple

_UNSUPPORTED_PREFIXES = {
    "&": "anchors are not supported",
    "*": "aliases are not supported",
    "!": "tags are not supported",
    "{": "flow mappings are not supported",
    "[": "flow sequences are not supported",
    "|": "block scalars are not supported",
    ">": "folded block scalars are not supported",
    "%": "directives are not supported",
    "?": "explicit key syntax is not supported",
}


class YamlSubsetError(ValueError):
    """Raised when input is outside the supported YAML subset."""


class _Line:
    __slots__ = ("indent", "text", "number")

    def __init__(self, indent: int, text: str, number: int) -> None:
        self.indent = indent
        self.text = text
        self.number = number


def _strip_comment(raw: str, number: int) -> str:
    out: List[str] = []
    quote: Optional[str] = None
    for index, char in enumerate(raw):
        if quote is not None:
            out.append(char)
            if char == quote:
                quote = None
            continue
        if char in ("'", '"'):
            quote = char
            out.append(char)
            continue
        if char == "#" and (index == 0 or raw[index - 1] in " \t"):
            break
        out.append(char)
    result = "".join(out).rstrip()
    if quote is not None:
        raise YamlSubsetError(f"line {number}: unterminated quoted scalar")
    return result


def _tokenize(text: str) -> List[_Line]:
    lines: List[_Line] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        if "\t" in raw[: len(raw) - len(raw.lstrip(" \t"))]:
            raise YamlSubsetError(f"line {number}: tab indentation is not supported")
        stripped = _strip_comment(raw, number)
        if not stripped.strip():
            continue
        if stripped.lstrip().startswith("---"):
            if lines:
                raise YamlSubsetError("line %d: multiple documents are not supported" % number)
            if stripped.strip() != "---":
                raise YamlSubsetError("line %d: inline document content is not supported" % number)
            continue
        if stripped.strip() == "...":
            continue
        indent = len(stripped) - len(stripped.lstrip(" "))
        lines.append(_Line(indent, stripped.strip(), number))
    return lines


def _parse_scalar(raw: str, number: int) -> Any:
    text = raw.strip()
    if text == "":
        return ""
    if text[0] in _UNSUPPORTED_PREFIXES:
        raise YamlSubsetError(
            "line %d: %s" % (number, _UNSUPPORTED_PREFIXES[text[0]])
        )
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        inner = text[1:-1]
        if text[0] == "'":
            return inner.replace("''", "'")
        return _unescape_double(inner, number)
    lowered = text.lower()
    if lowered in ("true", "yes", "on"):
        return True
    if lowered in ("false", "no", "off"):
        return False
    if lowered in ("null", "~", ""):
        return None
    try:
        return int(text, 10)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        pass
    return text


_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\", "/": "/"}


def _unescape_double(inner: str, number: int) -> str:
    out: List[str] = []
    index = 0
    while index < len(inner):
        char = inner[index]
        if char != "\\":
            out.append(char)
            index += 1
            continue
        if index + 1 >= len(inner):
            raise YamlSubsetError(f"line {number}: trailing escape character")
        marker = inner[index + 1]
        if marker in _ESCAPES:
            out.append(_ESCAPES[marker])
            index += 2
            continue
        if marker == "u" and index + 5 < len(inner) + 1:
            try:
                out.append(chr(int(inner[index + 2 : index + 6], 16)))
            except ValueError:
                raise YamlSubsetError(f"line {number}: invalid unicode escape") from None
            index += 6
            continue
        raise YamlSubsetError(f"line {number}: unsupported escape \\{marker}")
    return "".join(out)


def _split_key(text: str, number: int) -> Tuple[str, str]:
    quote: Optional[str] = None
    for index, char in enumerate(text):
        if quote is not None:
            if char == quote:
                quote = None
            continue
        if char in ("'", '"'):
            quote = char
            continue
        if char == ":" and (index + 1 == len(text) or text[index + 1] in " \t"):
            key = text[:index].strip()
            if not key:
                raise YamlSubsetError(f"line {number}: empty mapping key")
            return key, text[index + 1 :].strip()
    if text.endswith(":"):
        return text[:-1].strip(), ""
    raise YamlSubsetError(f"line {number}: expected 'key: value' but found {text!r}")


def _parse_block(lines: List[_Line], start: int, indent: int) -> Tuple[Any, int]:
    if start >= len(lines):
        return None, start
    if lines[start].text == "-" or lines[start].text.startswith("- "):
        return _parse_sequence(lines, start, indent)
    return _parse_mapping(lines, start, indent)


def _parse_sequence(lines: List[_Line], start: int, indent: int) -> Tuple[List[Any], int]:
    items: List[Any] = []
    index = start
    while index < len(lines):
        line = lines[index]
        if line.indent < indent:
            break
        if line.indent > indent:
            raise YamlSubsetError(
                f"line {line.number}: unexpected indentation inside a sequence"
            )
        if not (line.text == "-" or line.text.startswith("- ")):
            break
        rest = line.text[1:].strip()
        index += 1
        if not rest:
            if index < len(lines) and lines[index].indent > indent:
                value, index = _parse_block(lines, index, lines[index].indent)
                items.append(value)
            else:
                items.append(None)
            continue
        if rest[0] in _UNSUPPORTED_PREFIXES:
            raise YamlSubsetError(
                "line %d: %s" % (line.number, _UNSUPPORTED_PREFIXES[rest[0]])
            )
        if ":" in rest and not _is_quoted_whole(rest):
            # A mapping that starts on the sequence-item line. Its keys align
            # with the first character after "- ".
            key, value_text = _split_key(rest, line.number)
            inner_indent = line.indent + (len(line.text) - len(line.text[1:].lstrip()))
            mapping: dict = {}
            _assign(mapping, key, value_text, line.number)
            while index < len(lines) and lines[index].indent > indent:
                inner = lines[index]
                if inner.indent != inner_indent:
                    raise YamlSubsetError(
                        f"line {inner.number}: inconsistent indentation in sequence item"
                    )
                sub_key, sub_value = _split_key(inner.text, inner.number)
                index += 1
                if sub_value == "":
                    if index < len(lines) and lines[index].indent > inner_indent:
                        sub_value, index = _parse_block(lines, index, lines[index].indent)
                    else:
                        sub_value = None
                    _set(mapping, sub_key, sub_value, inner.number)
                else:
                    _assign(mapping, sub_key, sub_value, inner.number)
            items.append(mapping)
            continue
        items.append(_parse_scalar(rest, line.number))
    return items, index


def _is_quoted_whole(text: str) -> bool:
    return len(text) >= 2 and text[0] in ("'", '"') and text[-1] == text[0]


def _assign(mapping: dict, key: str, raw: str, number: int) -> None:
    """Assign a still-unparsed scalar value to `key`."""

    _set(mapping, key, _parse_scalar(raw, number), number)


def _set(mapping: dict, key: str, value: Any, number: int) -> None:
    if key in mapping:
        raise YamlSubsetError(f"line {number}: duplicate mapping key {key!r}")
    mapping[key] = value


def _parse_mapping(lines: List[_Line], start: int, indent: int) -> Tuple[dict, int]:
    mapping: dict = {}
    index = start
    while index < len(lines):
        line = lines[index]
        if line.indent < indent:
            break
        if line.indent > indent:
            raise YamlSubsetError(
                f"line {line.number}: unexpected indentation inside a mapping"
            )
        if line.text == "-" or line.text.startswith("- "):
            break
        key, value_text = _split_key(line.text, line.number)
        number = line.number
        index += 1
        if value_text == "":
            if index < len(lines) and lines[index].indent > indent:
                value, index = _parse_block(lines, index, lines[index].indent)
            elif index < len(lines) and lines[index].indent == indent and (
                lines[index].text == "-" or lines[index].text.startswith("- ")
            ):
                value, index = _parse_sequence(lines, index, indent)
            else:
                value = None
        else:
            value = _parse_scalar(value_text, number)
        _set(mapping, key, value, number)
    return mapping, index


def loads(text: str) -> Any:
    """Parse ``text`` and return plain Python dict/list/scalar values."""

    lines = _tokenize(text)
    if not lines:
        return {}
    value, index = _parse_block(lines, 0, lines[0].indent)
    if index != len(lines):
        raise YamlSubsetError(
            f"line {lines[index].number}: could not parse {lines[index].text!r}"
        )
    return value
