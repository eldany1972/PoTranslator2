from __future__ import annotations

from dataclasses import dataclass, field
import ast
from pathlib import Path
import re
from typing import Iterable


class POParseError(ValueError):
    pass


@dataclass(slots=True)
class POEntry:
    msgid: str
    msgstr: str = ""
    msgctxt: str = ""
    msgid_plural: str = ""
    msgstr_plural: dict[str, str] = field(default_factory=dict)
    translator_comments: list[str] = field(default_factory=list)
    extracted_comments: list[str] = field(default_factory=list)
    references: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    previous: list[str] = field(default_factory=list)


@dataclass(slots=True)
class POCatalog:
    header: dict[str, str] = field(default_factory=dict)
    preamble_comments: list[str] = field(default_factory=list)
    entries: list[POEntry] = field(default_factory=list)


FIELD_RE = re.compile(r"^(msgctxt|msgid_plural|msgid|msgstr(?:\[(\d+)\])?)\s+(\".*\")\s*$")


def _unquote(value: str, line_number: int) -> str:
    try:
        parsed = ast.literal_eval(value)
    except (SyntaxError, ValueError) as exc:
        raise POParseError(f"Invalid PO string on line {line_number}: {value}") from exc
    if not isinstance(parsed, str):
        raise POParseError(f"Expected a string on line {line_number}.")
    return parsed


def _split_blocks(text: str) -> list[list[tuple[int, str]]]:
    blocks: list[list[tuple[int, str]]] = []
    current: list[tuple[int, str]] = []
    for line_number, line in enumerate(text.replace("\r\n", "\n").replace("\r", "\n").split("\n"), 1):
        if not line.strip():
            if current:
                blocks.append(current)
                current = []
        else:
            current.append((line_number, line))
    if current:
        blocks.append(current)
    return blocks


def parse_po(text: str) -> POCatalog:
    catalog = POCatalog()
    for block in _split_blocks(text.lstrip("\ufeff")):
        entry = _parse_block(block)
        if entry is None:
            continue
        if entry.msgid == "" and not catalog.header:
            catalog.header = parse_header(entry.msgstr)
            catalog.preamble_comments = entry.translator_comments
        else:
            catalog.entries.append(entry)
    if not catalog.entries and not catalog.header:
        raise POParseError("The file does not contain valid PO entries.")
    return catalog


def _parse_block(block: list[tuple[int, str]]) -> POEntry | None:
    values: dict[str, str] = {}
    translator_comments: list[str] = []
    extracted_comments: list[str] = []
    references: list[str] = []
    flags: list[str] = []
    previous: list[str] = []
    current_field = ""

    for line_number, original_line in block:
        line = original_line.strip()
        if line.startswith("#~"):
            return None
        if line.startswith("#."):
            extracted_comments.append(line[2:].lstrip())
            continue
        if line.startswith("#:"):
            references.extend(part for part in line[2:].strip().split() if part)
            continue
        if line.startswith("#,"):
            flags.extend(part.strip() for part in line[2:].split(",") if part.strip())
            continue
        if line.startswith("#|"):
            previous.append(line[2:].lstrip())
            continue
        if line.startswith("#"):
            translator_comments.append(line[1:].lstrip())
            continue
        match = FIELD_RE.match(line)
        if match:
            current_field = match.group(1)
            values[current_field] = _unquote(match.group(3), line_number)
            continue
        if line.startswith('"') and current_field:
            values[current_field] = values.get(current_field, "") + _unquote(line, line_number)
            continue
        raise POParseError(f"Unrecognized PO syntax on line {line_number}: {original_line}")

    if "msgid" not in values:
        return None
    plurals = {
        key[7:-1]: value
        for key, value in values.items()
        if key.startswith("msgstr[")
    }
    return POEntry(
        msgid=values.get("msgid", ""),
        msgstr=values.get("msgstr", ""),
        msgctxt=values.get("msgctxt", ""),
        msgid_plural=values.get("msgid_plural", ""),
        msgstr_plural=plurals,
        translator_comments=translator_comments,
        extracted_comments=extracted_comments,
        references=references,
        flags=flags,
        previous=previous,
    )


def parse_header(value: str) -> dict[str, str]:
    header: dict[str, str] = {}
    for line in value.splitlines():
        if ":" in line:
            key, item = line.split(":", 1)
            header[key.strip()] = item.strip()
    return header


def load_po(path: str | Path) -> POCatalog:
    file_path = Path(path)
    try:
        text = file_path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        text = file_path.read_text(encoding="latin-1")
    return parse_po(text)


def _quote(value: str) -> str:
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\t", "\\t")
        .replace("\r", "\\r")
        .replace("\n", "\\n")
    )
    return f'"{escaped}"'


def _header_string(header: dict[str, str]) -> str:
    return "".join(f"{key}: {value}\n" for key, value in header.items())


def _comment_lines(entry: POEntry) -> Iterable[str]:
    for comment in entry.translator_comments:
        yield f"# {comment}" if comment else "#"
    for comment in entry.extracted_comments:
        yield f"#. {comment}"
    if entry.references:
        yield "#: " + " ".join(entry.references)
    if entry.flags:
        yield "#, " + ", ".join(dict.fromkeys(entry.flags))
    for previous in entry.previous:
        yield f"#| {previous}"


def render_po(catalog: POCatalog) -> str:
    lines: list[str] = []
    for comment in catalog.preamble_comments:
        lines.append(f"# {comment}" if comment else "#")
    lines.extend(("msgid \"\"", "msgstr \"\""))
    for header_line in _header_string(catalog.header).splitlines(keepends=True):
        lines.append(_quote(header_line))
    lines.append("")
    for entry in catalog.entries:
        lines.extend(_comment_lines(entry))
        if entry.msgctxt:
            lines.append(f"msgctxt {_quote(entry.msgctxt)}")
        lines.append(f"msgid {_quote(entry.msgid)}")
        if entry.msgid_plural:
            lines.append(f"msgid_plural {_quote(entry.msgid_plural)}")
            plural_map = entry.msgstr_plural or {"0": "", "1": ""}
            for index in sorted(plural_map, key=lambda value: int(value)):
                lines.append(f"msgstr[{index}] {_quote(plural_map[index])}")
        else:
            lines.append(f"msgstr {_quote(entry.msgstr)}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
