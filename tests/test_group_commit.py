"""Group-commit discovery: equivalence, crash windows, 27757e3 upgrade (synthetic fixtures only)."""
import contextlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from production_fixtures import SEED, file_set, make_workspace, tree_digest

from machina_deid import production as pr
from machina_deid.catalog import IdentityCatalog
from machina_deid.discovery import Candidate

R277 = "27757e305f9dbc8e4b607f991a6491734a7091ed"
TABLES = ("identities", "observations", "approvals", "decisions", "conflicts", "meta")


def cfg_for(root, head, n, **kw):
    base = dict(root=root, expected_rows=n, expected_revision=head, workers=2, reserve_bytes=0,
                poll_seconds=0.05, batch_files=7, heavy_slots=1)
    base.update(kw)
    return pr.Config(**base)


def catalog_dump(root):
    db = sqlite3.connect(root / "ops/attempt-1/catalog.sqlite")
    try:
        names = {t for (t,) in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        out = {}
        for t in TABLES:
            if t in names:
                cols = [c[1] for c in db.execute(f"PRAGMA table_info({t})") if c[1] not in ("rowid", "id", "at")]
                out[t] = sorted(db.execute(f"SELECT {','.join(cols)} FROM {t}"))
        return out
    finally:
        db.close()


def store(root):
    return json.loads((root / "ops/attempt-1/catalog.pseudonyms.json").read_text())


def discovery(root):
    db = sqlite3.connect(root / "ops/attempt-1/ledger.sqlite")
    try:
        return list(db.execute("SELECT path, status FROM discovery ORDER BY rowid"))
    finally:
        db.close()


@pytest.fixture(scope="module")
def files():
    return file_set(8)  # repeated persons/orgs (i % 4, i % 3) -> many duplicate identities


@pytest.fixture(scope="module")
def per_row_reference(tmp_path_factory, files):
    """Discovery exactly as 27757e3 did it: one catalog commit per operation, ledger commit per row."""
    root = tmp_path_factory.mktemp("perrow")
    head = make_workspace(root, files)
    mp = pytest.MonkeyPatch()
    mp.setattr(IdentityCatalog, "group", lambda self: contextlib.nullcontext())
    try:
        pr.run(cfg_for(root, head, len(files), workers=1), discovery_only=True)
    finally:
        mp.undo()
    return root


@pytest.mark.parametrize("workers,batch", [(1, 1), (1, 7), (3, 64)])
def test_group_commit_equivalent_to_per_row(per_row_reference, tmp_path, files, workers, batch):
    head = make_workspace(tmp_path, files)
    pr.run(cfg_for(tmp_path, head, len(files), workers=workers, batch_files=batch), discovery_only=True)
    assert discovery(tmp_path) == discovery(per_row_reference)  # same rows, same manifest order
    assert catalog_dump(tmp_path) == catalog_dump(per_row_reference)  # observations, approvals, decisions
    assert store(tmp_path) == store(per_row_reference)  # identical pseudonyms


def _crash_run(root, head, n, when):
    code = ("import sys; from pathlib import Path; from machina_deid import production as pr;"
            f"pr.run(pr.Config(root=Path({str(root)!r}), expected_rows={n}, expected_revision={head!r},"
            "workers=1, reserve_bytes=0, poll_seconds=0.05, batch_files=7, heavy_slots=1), discovery_only=True)")
    env = dict(os.environ, **{pr.TEST_CRASH_ENV + "_DISCOVERY": when})
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src") + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)


def test_crash_between_catalog_and_ledger_commit_rediscovers_safely(per_row_reference, tmp_path, files):
    head = make_workspace(tmp_path, files)
    r = _crash_run(tmp_path, head, len(files), "after_catalog")
    assert r.returncode == 71, r.stderr
    assert discovery(tmp_path) == []  # catalog committed, ledger not: rows will be re-discovered
    assert catalog_dump(tmp_path)["observations"]  # catalog effects are durable
    pr.run(cfg_for(tmp_path, head, len(files), workers=1), discovery_only=True)
    assert discovery(tmp_path) == discovery(per_row_reference)
    assert catalog_dump(tmp_path) == catalog_dump(per_row_reference)  # no double observations/decisions
    assert store(tmp_path) == store(per_row_reference)  # unchanged pseudonyms


def test_group_rollback_keeps_catalog_and_store_consistent(tmp_path):
    cat = IdentityCatalog(tmp_path / "c.sqlite", SEED)
    cat.approve("person", "Alice Example")
    with pytest.raises(RuntimeError):
        with cat.group():
            cat.approve("person", "Bob Example")
            cat.record_candidates([Candidate("person", "Carol Example", "pdf:author", "high", "pdf_metadata")],
                                  source_sha256="0" * 64, path="a.pdf")
            raise RuntimeError("simulated crash mid-batch")
    cat.close()
    cat = IdentityCatalog(tmp_path / "c.sqlite", SEED)  # store-consistency check passes on reopen
    assert cat.approval_status("person", "Alice Example") == "approved"
    assert cat.approval_status("person", "Bob Example") is None  # rolled back
    p = cat.approve("person", "Bob Example")  # re-run allocates the same deterministic pseudonym
    assert p == "approved"
    cat.close()


def test_group_savepoint_isolates_failed_operation(tmp_path):
    cat = IdentityCatalog(tmp_path / "c.sqlite", SEED)
    with cat.group():
        cat.approve("person", "Alice Example")
        with pytest.raises(Exception):
            cat.approve("person", "Dana Example", reason="contains Dana Example")  # value-leak guard
        cat.approve("person", "Erin Example")
    assert cat.approval_status("person", "Alice Example") == "approved"
    assert cat.approval_status("person", "Erin Example") == "approved"
    assert cat.approval_status("person", "Dana Example") is None
    cat.close()


def test_store_written_once_per_group(tmp_path, monkeypatch):
    cat = IdentityCatalog(tmp_path / "c.sqlite", SEED)
    saves = []
    real = type(cat._mapper).save
    monkeypatch.setattr(type(cat._mapper), "save", lambda self: (saves.append(1), real(self)))
    with cat.group():
        for i in range(50):
            cat.approve("person", f"Person Number {i}")
    assert len(saves) == 1
    cat.close()


def _r277_partial_state(tmp_path, files, monkeypatch):
    """27757e3 on-disk state mid-discovery: per-row commits, revision pinned to 27757e3."""
    head = make_workspace(tmp_path, files)
    present = list(files)[: len(files) // 2]
    monkeypatch.setattr(IdentityCatalog, "group", lambda self: contextlib.nullcontext())
    c = cfg_for(tmp_path, R277, len(files), workers=1, check_git=False)
    rows = pr.load_rows(c)
    pr.check_revision(c)
    ledger = pr.open_ledger(c)
    cat = IdentityCatalog(c.catalog_path, pr.ensure_seed(c).read_bytes(), c.store_path)
    pr.discover(c, [r for r in rows if r.path in present] , ledger, cat)
    cat.close()
    ledger.close()
    monkeypatch.undo()
    return head


def test_upgrade_from_27757e3_preserves_state_and_matches_fresh(per_row_reference, tmp_path, files, monkeypatch):
    head = _r277_partial_state(tmp_path, files, monkeypatch)
    before_disc, before_store = discovery(tmp_path), store(tmp_path)
    c = cfg_for(tmp_path, head, len(files))
    with pytest.raises(pr.RunnerError, match="migrate"):
        pr.run(c, discovery_only=True)
    rec = pr.migrate(c, R277, head)
    assert rec["migrated"] and rec["discovery_rows"] == len(before_disc)
    assert (tmp_path / "ops/attempt-1/pre-migration-27757e3/catalog.sqlite").exists()
    pr.run(c, discovery_only=True)
    after = discovery(tmp_path)
    assert after[: len(before_disc)] == before_disc  # existing rows untouched
    assert sorted(after) == sorted(discovery(per_row_reference))
    assert catalog_dump(tmp_path) == catalog_dump(per_row_reference)
    s = store(tmp_path)
    assert all(s["entries"][k] == v for k, v in before_store["entries"].items())  # pseudonyms unchanged
    assert s == store(per_row_reference)
    # rollback is still available before output
    assert pr.rollback(c) == {"rolled_back_to": R277}


def test_full_run_after_group_discovery_matches_transform(tmp_path, files):
    head = make_workspace(tmp_path, files)
    out = pr.run(cfg_for(tmp_path, head, len(files), workers=3, heavy_slots=2))
    assert out["phase"] == "packaged" and out["output_objects"] == len(files)
    ledger = sqlite3.connect(tmp_path / "ops/attempt-1/ledger.sqlite")
    assert ledger.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert tree_digest(tmp_path / "attempts/attempt-1/corpus")
