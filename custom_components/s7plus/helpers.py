"""Small helpers shared by the config flow and platforms."""

from __future__ import annotations


def parse_options(text: str) -> dict[int, str]:
    """Parse ``value=label`` lines (or ``value:label``, ``;``-separated).

    Raises ``ValueError`` on a malformed line or a duplicate.
    """
    result: dict[int, str] = {}
    for raw_line in text.replace(";", "\n").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        sep = "=" if "=" in line else ":"
        if sep not in line:
            raise ValueError(line)
        value, label = line.split(sep, 1)
        number = int(value.strip(), 0)
        label = label.strip()
        if not label or number in result or label in result.values():
            raise ValueError(line)
        result[number] = label
    if not result:
        raise ValueError("empty")
    return result
