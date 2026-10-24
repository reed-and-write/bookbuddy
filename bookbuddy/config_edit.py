"""Minimal, comment-preserving edits to config.toml (the web UI writes settings and API keys through this).

Only what Book Buddy itself writes is supported: strings, numbers, booleans and lists of strings, as keys of a
`[table]` / `[a.b]` or at the top level. Everything else in the file (comments, other tables) is left untouched.
The file is written atomically with mode 0600, because it may hold API keys.
"""
from __future__ import annotations

import json
import os
import re
import tomllib
from pathlib import Path
from typing import Any

_HEADER = re.compile(r"^\s*\[\s*([^\[\]]+?)\s*\]\s*(?:#.*)?$")
_KEY = re.compile(r"^\s*([A-Za-z0-9_\-]+)\s*=")


def _fmt(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_fmt(x) for x in v) + "]"
    return json.dumps(str(v), ensure_ascii=False)       # JSON string escapes are valid TOML basic-string escapes


def _value_end(lines: list[str], i: int) -> int:
    """Index after the (possibly multi-line array) value that starts on lines[i]."""
    depth = 0
    j = i
    while j < len(lines):
        text = lines[j].split("=", 1)[1] if j == i else lines[j]
        in_str = None
        for ch in re.sub(r"#.*$", "", text) if not re.search(r"[\"']", text) else text:
            if in_str:
                if ch == in_str:
                    in_str = None
            elif ch in "\"'":
                in_str = ch
            elif ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
            elif ch == "#":
                break
        j += 1
        if depth <= 0:
            break
    return j


def _find_table(lines: list[str], table: str) -> tuple[int, int] | None:
    """(first line after the header, end) of `[table]`; None when absent. table == '' is the top level."""
    if not table:
        end = next((i for i, l in enumerate(lines) if _HEADER.match(l)), len(lines))
        return 0, end
    for i, l in enumerate(lines):
        m = _HEADER.match(l)
        if m and m.group(1).replace(" ", "") == table:
            end = next((k for k in range(i + 1, len(lines)) if _HEADER.match(lines[k])), len(lines))
            return i + 1, end
    return None


def _apply(text: str, table: str, key: str, value: Any | None) -> str:
    lines = text.splitlines()
    span = _find_table(lines, table)
    if span is not None:
        start, end = span
        i = start
        while i < end:
            m = _KEY.match(lines[i])
            if m and m.group(1) == key:
                j = min(_value_end(lines, i), end) if "=" in lines[i] else i + 1
                if value is None:
                    del lines[i:j]
                else:
                    lines[i:j] = [f"{key} = {_fmt(value)}"]
                return "\n".join(lines) + "\n"
            i += 1
        if value is None:
            return text
        at = end
        while at > start and not lines[at - 1].strip():      # keep trailing blank lines after the new key
            at -= 1
        lines.insert(at, f"{key} = {_fmt(value)}")
        return "\n".join(lines) + "\n"
    if value is None:
        return text
    if not table:
        lines.insert(0, f"{key} = {_fmt(value)}")
        return "\n".join(lines) + "\n"
    if lines and lines[-1].strip():
        lines.append("")
    lines += [f"[{table}]", f"{key} = {_fmt(value)}"]
    return "\n".join(lines) + "\n"


def update_config(path: Path, changes: dict[tuple[str, str], Any]) -> None:
    """Apply {(table, key): value} (value None removes the key) and write the file atomically with mode 0600."""
    path = Path(path)
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    for (table, key), value in changes.items():
        text = _apply(text, table, key, value)
    tomllib.loads(text)                       # never write a file we could not read back
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
