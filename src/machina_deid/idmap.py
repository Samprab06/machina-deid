"""Explicit identity maps for the format transformers.

JSON file, either a flat ``{"old": "new"}`` object (strings only) or::

    {"strings": {"old": "new"}, "layers": {"OLD": "NEW"}, "blocks": {"OLD": "NEW"}}

Nothing is ever inferred: only listed strings are touched. Reports from the
transformers refer to entries by index, never by raw value.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


class IdentityMapError(Exception):
    pass


@dataclass
class IdentityMap:
    strings: dict[str, str] = field(default_factory=dict)
    layers: dict[str, str] = field(default_factory=dict)
    blocks: dict[str, str] = field(default_factory=dict)

    def ordered_strings(self) -> list[tuple[str, str]]:
        """Longest-first so a longer identity wins over a contained shorter one."""
        return sorted(self.strings.items(), key=lambda kv: -len(kv[0]))

    def __repr__(self) -> str:  # never leak raw identities
        return f"IdentityMap(strings={len(self.strings)}, layers={len(self.layers)}, blocks={len(self.blocks)})"


def _check(d: object, name: str) -> dict[str, str]:
    if not isinstance(d, dict):
        raise IdentityMapError(f"'{name}' must be an object")
    for k, v in d.items():
        if not isinstance(k, str) or not isinstance(v, str) or not k.strip():
            raise IdentityMapError(f"'{name}' entries must be non-empty string pairs")
        if k == v:
            raise IdentityMapError(f"'{name}' contains an identity mapped to itself")
    return d


def from_dict(data: dict) -> IdentityMap:
    if not isinstance(data, dict):
        raise IdentityMapError("identity map must be a JSON object")
    if set(data) & {"strings", "layers", "blocks"}:
        extra = set(data) - {"strings", "layers", "blocks"}
        if extra:
            raise IdentityMapError("unknown keys in identity map")
        return IdentityMap(_check(data.get("strings", {}), "strings"), _check(data.get("layers", {}), "layers"),
                           _check(data.get("blocks", {}), "blocks"))
    return IdentityMap(strings=_check(data, "strings"))


def load(path: str | Path) -> IdentityMap:
    try:
        return from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
    except (OSError, ValueError) as exc:
        raise IdentityMapError(f"cannot read identity map ({type(exc).__name__})") from None
