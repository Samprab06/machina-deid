"""Attempt 2 storage: adopt frozen attempt-1 discovery, hardlink byte-identical outputs, never touch attempt 1."""
import hashlib
import os
import sqlite3

import pytest
from production_fixtures import file_set, make_workspace

from machina_deid import production as pr


def cfg(root, head, n, attempt, **kw):
    base = dict(root=root, attempt=attempt, expected_rows=n, expected_revision=head, workers=2, reserve_bytes=0,
                poll_seconds=0.05, batch_files=7, heavy_slots=1)
    base.update(kw)
    return pr.Config(**base)


def snapshot(d):
    """Bytes + every inode attribute that a write/truncate/chmod/utime would change."""
    out = {}
    for p in sorted(d.rglob("*")):
        if p.is_file():
            st = os.lstat(p)
            out[p.relative_to(d).as_posix()] = (hashlib.sha256(p.read_bytes()).hexdigest(), st.st_size,
                                                st.st_mode, st.st_mtime_ns, st.st_ino)
    return out


@pytest.fixture(scope="module")
def a1(tmp_path_factory):
    files = file_set(4)
    root = tmp_path_factory.mktemp("a2")
    head = make_workspace(root, files)
    assert pr.run(cfg(root, head, len(files), "attempt-1"))["phase"] == "packaged"
    return root, head, files


def test_adopt_reuse_links_and_attempt1_immutable(a1):
    root, head, files = a1
    a1_corpus, a1_ops = root / "attempts/attempt-1/corpus", root / "ops/attempt-1"
    before_corpus, before_ops = snapshot(a1_corpus), {k: v[0] for k, v in snapshot(a1_ops).items()
                                                      if not k.endswith("runner.lock")}
    c2 = cfg(root, head, len(files), "attempt-2", reuse_from="attempt-1")
    out = pr.adopt(c2, "attempt-1")
    assert out["catalog_digest_verified"] and out["discovery_rows"] == len(files)
    with pytest.raises(pr.RunnerError):
        pr.adopt(c2, "attempt-1")  # never over existing state
    # interrupted then resumed run
    part = pr.run(cfg(root, head, len(files), "attempt-2", reuse_from="attempt-1", stop_after_outputs=3))
    assert part["phase"] == "transform" and part["remaining"] > 0
    res = pr.run(c2)
    assert res["phase"] == "packaged" and res["manifest_files"] == len(files)
    a2 = root / "attempts/attempt-2/corpus"
    linked = 0
    for rel in files:
        s1, s2 = os.lstat(a1_corpus / rel), os.lstat(a2 / rel)
        same = (a1_corpus / rel).read_bytes() == (a2 / rel).read_bytes()
        if s1.st_ino == s2.st_ino:
            linked += 1
            assert same  # a link is only ever made to identical bytes
    assert linked >= sum(1 for r in files if not r.endswith(".dxf")) // 2
    # attempt 1 bytes, modes, mtimes, inode numbers, ledger/catalog/seed unchanged
    assert snapshot(a1_corpus) == before_corpus
    assert {k: v[0] for k, v in snapshot(a1_ops).items() if not k.endswith("runner.lock")} == before_ops
    # distinct attempt-2 ledger with exact coverage; adopted digest recorded
    db = sqlite3.connect(root / "ops/attempt-2/ledger.sqlite")
    try:
        assert db.execute("SELECT COUNT(*) FROM output").fetchone()[0] == len(files)
        assert pr.meta_get(db, "adopted_from") == "attempt-1"
    finally:
        db.close()


def test_link_reference_rejects_changed_reference(tmp_path):
    ref, out = tmp_path / "ref", tmp_path / "o" / "x"
    out.parent.mkdir()
    ref.write_bytes(b"abc")
    st = os.lstat(ref)
    info = {"ino": st.st_ino, "dev": st.st_dev, "size": 3, "mtime_ns": st.st_mtime_ns + 1}
    with pytest.raises(pr.LinkError):
        pr.link_reference(ref, out, info)
    assert not out.exists() and not list(out.parent.iterdir())
    info["mtime_ns"] = st.st_mtime_ns
    out.write_bytes(b"old")  # pre-existing output name is replaced, its inode is not truncated
    old_ino = os.lstat(out).st_ino
    pr.link_reference(ref, out, info)
    assert os.lstat(out).st_ino == st.st_ino != old_ino and ref.read_bytes() == b"abc"


def test_ref_identity_requires_exact_bytes(tmp_path):
    (tmp_path / "c").mkdir()
    (tmp_path / "c" / "f").write_bytes(b"hello")
    good = hashlib.sha256(b"hello").hexdigest()
    assert pr._ref_identity(str(tmp_path / "c"), "f", 5, good)
    assert pr._ref_identity(str(tmp_path / "c"), "f", 5, "0" * 64) is None
    assert pr._ref_identity(str(tmp_path / "c"), "missing", 5, good) is None
    os.symlink(tmp_path / "c" / "f", tmp_path / "c" / "s")
    assert pr._ref_identity(str(tmp_path / "c"), "s", 5, good) is None
