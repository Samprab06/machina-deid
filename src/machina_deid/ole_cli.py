"""Redacted OLE inspection / transform CLI (prints counts, lengths and kinds; never values).

    python -m machina_deid.ole_cli inspect FILE [FILE ...]
    python -m machina_deid.ole_cli transform IN OUT --map MAP.json      # {"original": "alias", ...}
    python -m machina_deid.ole_cli transform IN OUT --alias-key-file KEY # derive width-preserving aliases
    python -m machina_deid.ole_cli transform IN OUT --alias-config CFG.json # process-shared (see docs)

The source is opened read-only and never written; OUT must not exist and must not be the source (same path or
inode). OUT is written via a same-directory temp file + fsync + atomic rename, only on success.

Exit status: 0 ok, 1 refused/failed (input left unwritten), 2 usage.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
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


def _read_src(src: str) -> tuple[bytes, os.stat_result]:
    fd = os.open(src, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        st = os.fstat(fd)
        with os.fdopen(fd, "rb", closefd=False) as f:
            return f.read(), st
    finally:
        os.close(fd)


def atomic_write_new(dst: str, data: bytes, src_stat: os.stat_result | None = None) -> None:
    """Write ``data`` to a not-yet-existing ``dst`` atomically; refuse if it would alias the source."""
    d = Path(dst)
    if d.exists() or d.is_symlink():
        raise FileExistsError("output_exists")
    parent = d.parent if str(d.parent) else Path(".")
    if src_stat is not None:
        pst = os.stat(parent)
        if (pst.st_dev, pst.st_ino) == (src_stat.st_dev, src_stat.st_ino):  # pragma: no cover - paranoia
            raise ValueError("output_is_source")
    fd, tmp = tempfile.mkstemp(prefix=".ole-", suffix=".part", dir=parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.link(tmp, d)  # fails if dst appeared meanwhile (no silent overwrite)
    finally:
        os.unlink(tmp)
    dfd = os.open(parent, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def load_alias_config(path: str) -> "ot.AliasRegistry":
    """Process-shared alias config: {"key_file": PATH, "fixed": {original: alias}} (see docs/ole-core.md)."""
    cfg = json.loads(Path(path).read_text(encoding="utf-8"))
    kp = Path(cfg["key_file"])
    if not kp.is_absolute():
        kp = Path(path).parent / kp
    return ot.AliasRegistry(kp.read_bytes().strip(), cfg.get("fixed") or {})


def _transform(src: str, dst: str, map_path: str | None, key_path: str | None, cfg_path: str | None = None) -> int:
    def refuse(reason):
        print(json.dumps({"ok": False, "reason": reason}))
        return 1
    if os.path.lexists(dst):
        return refuse("output_exists")
    if os.path.abspath(src) == os.path.abspath(dst):
        return refuse("output_is_source")
    data, st = _read_src(src)
    if map_path:
        mapping = json.loads(Path(map_path).read_text(encoding="utf-8"))
    else:
        reg = load_alias_config(cfg_path) if cfg_path else ot.AliasRegistry(Path(key_path).read_bytes().strip())
        try:
            vals = {p.value for p in ot.summary_props(ot.parse(data)) if p.value}
            mapping = {v: reg.get(v) for v in vals}
        except ot.OleError as exc:
            return refuse(str(exc))
    res = ot.transform(data, mapping)
    kinds: dict[str, int] = {}
    for w in res.writes:
        kinds[w.kind] = kinds.get(w.kind, 0) + 1
    ok = res.reason is None
    reason = res.reason
    if ok:
        try:
            atomic_write_new(dst, res.data, st)
        except (FileExistsError, ValueError) as exc:
            ok, reason = False, str(exc)
    print(json.dumps({"ok": ok, "reason": reason, "writes": kinds, "occurrences": res.counts},
                     sort_keys=True))
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
    g.add_argument("--alias-config")
    a = ap.parse_args(argv)
    if a.cmd == "inspect":
        return _inspect(a.files)
    return _transform(a.src, a.dst, a.map, a.alias_key_file, a.alias_config)


if __name__ == "__main__":
    sys.exit(main())
