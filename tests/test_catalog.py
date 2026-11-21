import json
import sqlite3

import pytest

from machina_deid.catalog import CatalogError, IdentityCatalog, Observation
from machina_deid.discovery import Candidate
from machina_deid.identity import PseudonymMapper

SECRET = b"unit-test-secret-0123456789"
H1 = "a" * 64
H2 = "b" * 64
RAW = ["Jane Doe", "Acme Widgets", "jane doe", "J. Doe", "Secret Project Falcon", "projects/Acme/part.step"]


def obs(value, kind="person", h=H1, path="projects/Acme/part.step", loc="step:FILE_NAME.author[0]",
        conf="high", reason="FILE_NAME author tuple entry"):
    return Observation(kind, value, h, path, loc, conf, reason)


@pytest.fixture
def cat(tmp_path):
    c = IdentityCatalog(tmp_path / "cat.sqlite", SECRET)
    yield c
    c.close()


def _assert_no_raw(tmp_path, *extra):
    blobs = [p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()]
    blobs += [s.encode() for s in extra]
    for blob in blobs:
        low = blob.lower()
        for raw in RAW:
            assert raw.lower().encode() not in low


def test_normalization_equivalence_matches_mapper(cat):
    a = cat.pseudonym("person", "Jane  Doe")
    assert a == cat.pseudonym("person", " JANE doe\t") == cat.pseudonym("person", "Ｊａｎｅ Doe")
    assert a == PseudonymMapper(SECRET).pseudonym("person", "Jane Doe")
    assert cat.pseudonym("org", "Jane Doe") != a
    assert cat.report()["summary"]["identities"] == 2


def test_restart_persistence_and_resume(tmp_path):
    db = tmp_path / "cat.sqlite"
    with IdentityCatalog(db, SECRET) as c:
        p = c.pseudonym("person", "Jane Doe")
        assert c.record([obs("Jane Doe")], confirmed=True) == 1
    with IdentityCatalog(db, SECRET) as c:
        assert c.lookup("person", "jane doe") == p
        assert c.record([obs("Jane Doe"), obs("Jane Doe", h=H2)], confirmed=True) == 1
        assert c.report()["summary"]["observations"] == 2
        assert sqlite3.connect(db).execute("PRAGMA user_version").fetchone()[0] == 1


def test_secret_mismatch_rejected(tmp_path):
    db = tmp_path / "cat.sqlite"
    IdentityCatalog(db, SECRET).close()
    with pytest.raises(CatalogError, match="different secret") as ei:
        IdentityCatalog(db, b"another-secret-9876543210")
    assert "another" not in str(ei.value)
    # Even with a fresh pseudonym store, the catalog's own fingerprint rejects it.
    with pytest.raises(CatalogError, match="different secret"):
        IdentityCatalog(db, b"another-secret-9876543210", store_path=tmp_path / "other.json")


def test_missing_store_detected(tmp_path):
    db = tmp_path / "cat.sqlite"
    with IdentityCatalog(db, SECRET) as c:
        c.pseudonym("person", "Jane Doe")
    db.with_suffix(".pseudonyms.json").unlink()
    with pytest.raises(CatalogError, match="missing"):
        IdentityCatalog(db, SECRET)


def test_duplicate_observations_idempotent(cat):
    batch = [obs("Jane Doe"), obs(" jane  doe "), obs("Jane Doe")]
    assert cat.record(batch, confirmed=True) == 1
    assert cat.record(batch, confirmed=True) == 0
    assert cat.record([obs("Jane Doe", loc="step:FILE_NAME.author[1]")], confirmed=True) == 1
    cand = [Candidate("person_or_org", "Acme Widgets", "pdf:metadata.author", "high", "PDF Info author")]
    assert cat.record_candidates(cand, source_sha256=H1, path="x.pdf") == 1
    assert cat.record_candidates(cand, source_sha256=H1, path="x.pdf") == 0
    assert cat.report()["summary"]["observations"] == 3


def test_explicit_alias_only(cat):
    a = cat.pseudonym("person", "Jane Doe")
    b = cat.pseudonym("person", "J. Doe")
    assert a != b  # never merged automatically
    assert cat.assert_alias("person", "Jane Doe", "J. Doe") == a
    assert cat.pseudonym("person", "J. Doe") == a
    assert cat.assert_alias("person", "Jane Doe", "J. Doe") == a  # idempotent
    r = cat.report()
    assert r["summary"]["aliases"] == 1 and r["summary"]["conflicts"] == 0
    with pytest.raises(CatalogError):
        cat.assert_alias("person", "Jane Doe", " jane DOE ")
    with pytest.raises(CatalogError):
        cat.assert_alias("bogus", "Jane Doe", "J. Doe")


def test_conflict_reporting(cat):
    cat.assert_alias("person", "Jane Doe", "J. Doe")
    with pytest.raises(CatalogError, match="conflicts"):
        cat.assert_alias("person", "Acme Widgets", "J. Doe")
    with pytest.raises(CatalogError, match="cycle"):
        cat.assert_alias("person", "J. Doe", "Jane Doe")
    assert cat.pseudonym("person", "J. Doe") == cat.pseudonym("person", "Jane Doe")  # unchanged
    # Same value confirmed as two kinds is an ambiguity.
    cat.pseudonym("org", "Acme Widgets")
    r = cat.report()
    assert {c["type"] for c in r["conflicts"]} == {"alias_conflict", "alias_cycle"}
    assert r["ambiguities"][0]["kinds"] == ["org", "person"]


def test_unresolved_candidates(cat):
    cat.record([obs("Secret Project Falcon", kind="project", loc="path:dir[1]", conf="low",
                    reason="path component")], confirmed=False)
    cat.record([obs("Acme Widgets", kind="person_or_org", loc="pdf:metadata.author")], confirmed=False)
    r = cat.report()
    assert r["summary"]["unresolved_candidates"] == 2
    cat.pseudonym("org", "acme widgets")  # resolving the exact value clears it
    r = cat.report()
    assert r["summary"]["unresolved_candidates"] == 1
    assert r["unresolved_candidates"][0]["locations"][0]["location"] == "path:dir[1]"


def test_redaction(tmp_path, cat):
    cat.record([obs(v) for v in ("Jane Doe", "J. Doe")], confirmed=True)
    cat.record([obs("Secret Project Falcon", kind="project")], confirmed=False)
    cat.assert_alias("person", "Jane Doe", "J. Doe")
    with pytest.raises(CatalogError):
        cat.assert_alias("person", "Acme Widgets", "J. Doe")
    report = json.dumps(cat.report())
    with pytest.raises(CatalogError) as ei:
        cat.record([obs("Jane Doe", loc="author=Jane Doe\n")], confirmed=True)
    _assert_no_raw(tmp_path, report, repr(cat), repr(obs("Jane Doe")), str(ei.value))


def test_rollback_on_invalid_batch(tmp_path, cat):
    good = [obs("Jane Doe"), obs("Acme Widgets", kind="org")]
    with pytest.raises(CatalogError):
        cat.record(good + [obs("x", h="not-a-hash")], confirmed=True)
    r = cat.report()["summary"]
    assert r["observations"] == 0 and r["identities"] == 0
    with pytest.raises(CatalogError):
        cat.record([obs("Jane Doe"), obs("y", conf="certain")], confirmed=True)
    assert cat.report()["summary"]["observations"] == 0
    # Catalog still usable after rollback and survives reopen.
    assert cat.record(good, confirmed=True) == 2
    cat.close()
    with IdentityCatalog(tmp_path / "cat.sqlite", SECRET) as c:
        assert c.report()["summary"]["identities"] == 2


def test_rollback_on_exception_mid_transaction(cat, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("injected")

    monkeypatch.setattr(cat, "path_id", boom)
    with pytest.raises(RuntimeError):
        cat.record([obs("Jane Doe")], confirmed=True)
    monkeypatch.undo()
    assert cat.report()["summary"]["identities"] == 0
