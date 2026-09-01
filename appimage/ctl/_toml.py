# Copyright 2023-2026 SSH-MITM Dev-Team. All rights reserved.
"""Low-level helpers for reading and writing ``[tool.appimage]`` TOML values."""

import re
from pathlib import Path


def _toml_value(v: object) -> str:
    """Serialise a Python value to its TOML representation.

    Parameters
    ----------
    v : object
        Value to serialise (str, list of str, or other).

    Returns
    -------
    str
        TOML-formatted value string.

    """
    if isinstance(v, str):
        return f'"{v}"'
    if isinstance(v, list):
        parts = ", ".join(_toml_value(i) for i in v)
        return f"[{parts}]"
    return str(v)


def _replace_or_append_toml_fields(
    pyproject_path: Path,
    new: dict[str, object],
) -> None:
    """Write *new* key/value pairs into ``[tool.appimage]``, overwriting existing lines.

    The opposite of ``write_config``'s insertion, which only ever adds
    missing keys and never touches an existing one - this is what
    ``update_tools`` needs to move pins forward instead of filling gaps.
    Scoped strictly to the ``[tool.appimage]`` section's own scalar lines,
    stopping at the next ``[`` header (a subtable like
    ``[tool.appimage.env]``, or an unrelated table), so a same-named key
    elsewhere is never touched.
    """
    header = "[tool.appimage]"
    content = pyproject_path.read_text()
    if header not in content:
        lines = "\n".join(f"{k} = {_toml_value(v)}" for k, v in new.items())
        pyproject_path.write_text(content + f"\n{header}\n{lines}\n")
        return

    start = content.index(header) + len(header)
    next_header = re.search(r"^\[", content[start:], re.MULTILINE)
    end = start + next_header.start() if next_header else len(content)
    section = content[start:end]

    remaining = dict(new)

    def _replace_line(match: re.Match[str]) -> str:
        key = match.group(1)
        if key in remaining:
            return f"{key} = {_toml_value(remaining.pop(key))}"
        return match.group(0)

    section = re.sub(
        r"^([A-Za-z_][A-Za-z0-9_]*) = .*$",
        _replace_line,
        section,
        flags=re.MULTILINE,
    )
    if remaining:
        addition = "\n".join(f"{k} = {_toml_value(v)}" for k, v in remaining.items())
        section = section.rstrip("\n") + f"\n{addition}\n"

    pyproject_path.write_text(content[:start] + section + content[end:])
