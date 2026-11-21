"""Synthetic benchmark: per-row (27757e3) vs group-commit discovery checkpointing.

Feeds synthetic in-memory discovery results (no parsing, no real data) through the exact
catalog + ledger recording path, so it measures only checkpoint I/O. Prints rows/s and a
SHA-256 of the resulting catalog tables, pseudonym store and ordered ledger for each mode.

    python scripts/discovery_commit_benchmark.py --rows 2000 --identities 50 --dir /path/on/target/fs
"""
import argparse
import contextlib
import hashlib
import json
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path

from machina_deid import twopass
from machina_deid.catalog import IdentityCatalog
from machina_deid.discovery import Candidate
from machina_deid.production import EXTRA_SCHEMA, LEDGER_SCHEMA

SEED = b"s" * 32


def members(i, identities):
    who = f"Synthetic Person {i % identities}"
    org = f"Synthetic Org {(i * 7) % identities}"
    d = twopass.DiscoveredFile(path=f"p/{i}.step", format="step", sha256=hashlib.sha256(str(i).encode()).hexdigest(),
                               status="ok", reason="", candidates=[
        Candidate("person_or_org", who, "step:FILE_NAME.author[0]", "high", "step_header"),
        Candidate("person_or_org", org, "step:FILE_NAME.organization[0]", "high", "step_header"),
        Candidate("file_name", f"part {i % 13}", "step:FILE_NAME.name", "medium", "step_header")])
    return [d]


def run(base: Path, rows: int, identities: int, batch: int, grouped: bool) -> tuple[float, str]:
    base.mkdir(parents=True)
    ledger = sqlite3.connect(base / "ledger.sqlite")
    ledger.execute("PRAGMA synchronous = FULL")
    for s in LEDGER_SCHEMA + EXTRA_SCHEMA:
        ledger.execute(s)
    ledger.commit()
    cat = IdentityCatalog(base / "catalog.sqlite", SEED)
    t0 = time.perf_counter()
    for start in range(0, rows, batch):
        idx = range(start, min(rows, start + batch))
        staged = []
        with (cat.group() if grouped else contextlib.nullcontext()):
            for i in idx:
                twopass._record_discovered(cat, [members(i, identities)], twopass.BatchResult(), True)
                if grouped:
                    staged.append((f"p/{i}.step", "inspected"))
                else:
                    ledger.execute("INSERT INTO discovery VALUES (?, ?)", (f"p/{i}.step", "inspected"))
                    ledger.commit()
        if grouped:
            ledger.executemany("INSERT INTO discovery VALUES (?, ?)", staged)
            ledger.commit()
    dt = time.perf_counter() - t0
    h = hashlib.sha256()
    for t in ("identities", "observations", "approvals", "decisions", "conflicts"):
        cols = [c[1] for c in cat._db.execute(f"PRAGMA table_info({t})") if c[1] not in ("at",)]
        h.update(json.dumps(sorted(cat._db.execute(f"SELECT {','.join(cols)} FROM {t}"))).encode())
    cat.close()
    h.update((base / "catalog.pseudonyms.json").read_bytes())
    h.update(json.dumps(list(ledger.execute("SELECT * FROM discovery ORDER BY rowid"))).encode())
    ledger.close()
    return rows / dt, h.hexdigest()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--rows", type=int, default=1000)
    p.add_argument("--identities", type=int, default=50)
    p.add_argument("--batch", type=int, default=256)
    p.add_argument("--dir", type=Path, default=None)
    a = p.parse_args()
    tmp = Path(tempfile.mkdtemp(dir=a.dir, prefix="dcbench-"))
    try:
        old = run(tmp / "per_row", a.rows, a.identities, a.batch, False)
        new = run(tmp / "group", a.rows, a.identities, a.batch, True)
    finally:
        shutil.rmtree(tmp)
    print(json.dumps({"rows": a.rows, "identities": a.identities, "batch": a.batch,
                      "per_row_rows_per_s": round(old[0], 1), "group_rows_per_s": round(new[0], 1),
                      "speedup": round(new[0] / old[0], 1), "state_hash_equal": old[1] == new[1]}))


if __name__ == "__main__":
    main()
