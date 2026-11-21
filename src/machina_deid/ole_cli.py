"""Redacted OLE inspection / transform CLI (prints counts, lengths and kinds; never values).

    python -m machina_deid.ole_cli inspect FILE [FILE ...]
    python -m machina_deid.ole_cli transform IN OUT --map MAP.json      # {"original": "alias", ...}
    python -m machina_deid.ole_cli transform IN OUT --alias-key-file KEY # derive equal-length aliases

Exit status: 0 ok, 1 refused/failed (input left unwritten), 2 usage.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import ole_transform as ot


def _inspect(paths: list[str]) -> int:
    rc = 0
    for p in paths:
        data = Path(p).read_bytes()
        try:
            rep = ot.inspect(data)
        except ot.OleError as exc:
            rep, rc = {"error": str(exc)}, 1
        print(json.dumps({"file_index": paths.index(p), **rep}, sort_keys=True))
    return rc


def _transform(src: str, dst: str, map_path: str | None, key_path: str | None) -> int:
    data = Path(src).read_bytes()
    if map_path:
        mapping = json.loads(Path(map_path).read_text(encoding="utf-8"))
    elif key_path:
        key = Path(key_path).read_bytes().strip()
        try:
            vals = {p.value for p in ot.summary_props(ot.parse(data)) if p.value}
        except ot.OleError as exc:
            print(json.dumps({"ok": False, "reason": str(exc)}))
            return 1
        mapping = {v: ot.equal_length_alias(v, key) for v in vals}
    else:
        print("need --map or --alias-key-file", file=sys.stderr)
        return 2
    res = ot.transform(data, mapping)
    kinds: dict[str, int] = {}
    for w in res.writes:
        kinds[w.kind] = kinds.get(w.kind, 0) + 1
    ok = res.reason is None
    print(json.dumps({"ok": ok, "reason": res.reason, "writes": kinds, "occurrences": res.counts},
                     sort_keys=True))
    if ok:
        Path(dst).write_bytes(res.data)
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ole_cli")
    sub = ap.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("inspect")
    i.add_argument("files", nargs="+")
    t = sub.add_parser("transform")
    t.add_argument("src")
    t.add_argument("dst")
    g = t.add_mutually_exclusive_group(required=True)
    g.add_argument("--map")
    g.add_argument("--alias-key-file")
    a = ap.parse_args(argv)
    if a.cmd == "inspect":
        return _inspect(a.files)
    return _transform(a.src, a.dst, a.map, a.alias_key_file)


if __name__ == "__main__":
    sys.exit(main())
