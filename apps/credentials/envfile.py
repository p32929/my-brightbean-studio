"""Read and update a dotenv file in place, for Settings → Platform keys.

Edits keep every other line (comments, unrelated keys, ordering) untouched and
write through the same inode, so a single-file Docker bind mount of ``.env``
stays attached.
"""

from __future__ import annotations

import fcntl
import re
from pathlib import Path

_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")
_BARE = re.compile(r"[A-Za-z0-9_./:@+=,-]*")


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value


def _quote(value: str) -> str:
    if "\n" in value or "\r" in value:
        raise ValueError("Values cannot contain line breaks.")
    if _BARE.fullmatch(value):
        return value
    if "'" in value:
        raise ValueError("Values with spaces or symbols cannot also contain a single quote (').")
    return f"'{value}'"


def read_env_file(path: str | Path) -> dict[str, str]:
    """Return KEY → value for every assignment in the file (later lines win)."""
    try:
        text = Path(path).read_text()
    except FileNotFoundError:
        return {}
    values = {}
    for line in text.splitlines():
        match = _LINE.match(line)
        if match:
            values[match[1]] = _unquote(match[2])
    return values


def update_env_file(path: str | Path, updates: dict[str, str]) -> None:
    """Set each KEY in ``updates``: rewrite existing lines, append the rest."""
    rendered = {key: _quote(value) for key, value in updates.items()}  # validate before touching the file
    with open(path, "r+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        out, seen = [], set()
        for line in handle.read().splitlines():
            match = _LINE.match(line)
            if match and match[1] in rendered:
                out.append(f"{match[1]}={rendered[match[1]]}")
                seen.add(match[1])
            else:
                out.append(line)
        out.extend(f"{key}={value}" for key, value in rendered.items() if key not in seen)
        handle.seek(0)
        handle.write("\n".join(out) + "\n")
        handle.truncate()
