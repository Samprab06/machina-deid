import json
import os

import pytest

from machina_deid.identity import IdentityError, PseudonymMapper, load_secret

SECRET = b"unit-test-secret-0123456789"


def test_stable_same_input():
    m = PseudonymMapper(SECRET)
    assert m.pseudonym("person", "Jane Doe") == m.pseudonym("person", "Jane Doe")
    assert PseudonymMapper(SECRET).pseudonym("person", "Jane Doe") == m.pseudonym("person", "Jane Doe")


def test_normalization_equivalence():
    m = PseudonymMapper(SECRET)
    assert m.pseudonym("person", "Jane  Doe") == m.pseudonym("person", " jane doe ")


def test_distinct_identities_kinds_and_secrets():
    m = PseudonymMapper(SECRET)
    assert m.pseudonym("person", "Jane Doe") != m.pseudonym("person", "John Doe")
    assert m.pseudonym("person", "Acme") != m.pseudonym("org", "Acme")
    other = PseudonymMapper(b"another-secret-9876543210")
    assert other.pseudonym("person", "Jane Doe") != m.pseudonym("person", "Jane Doe")


def test_typed_and_meaningful():
    m = PseudonymMapper(SECRET)
    assert m.pseudonym("person", "x").startswith("Person-")
    assert m.pseudonym("org", "x").startswith("Organization-")
    assert m.pseudonym("email", "a@b.com").endswith("@example.invalid")
    assert m.pseudonym("domain", "b.com").endswith(".example.invalid")
    assert "Jane" not in m.pseudonym("person", "Jane Doe")


def test_invalid_input():
    m = PseudonymMapper(SECRET)
    with pytest.raises(IdentityError):
        m.pseudonym("bogus", "x")
    with pytest.raises(IdentityError):
        m.pseudonym("person", "   ")
    with pytest.raises(IdentityError):
        PseudonymMapper(b"short")


def test_persistence_roundtrip_and_no_raw_identity(tmp_path):
    store = tmp_path / "sub" / "map.json"
    m = PseudonymMapper(SECRET, store)
    a = m.pseudonym("person", "Jane Doe")
    assert store.exists()
    text = store.read_text()
    assert "Jane" not in text and SECRET.decode() not in text
    json.loads(text)
    assert PseudonymMapper(SECRET, store).pseudonym("person", "Jane Doe") == a
    assert len(PseudonymMapper(SECRET, store)) == 1
    assert not [p for p in store.parent.iterdir() if p.suffix == ".tmp"]
    assert oct(store.stat().st_mode & 0o777) == "0o600"


def test_store_with_wrong_secret_rejected(tmp_path):
    store = tmp_path / "map.json"
    PseudonymMapper(SECRET, store).pseudonym("person", "x")
    with pytest.raises(IdentityError):
        PseudonymMapper(b"another-secret-9876543210", store)


def test_corrupt_store_rejected(tmp_path):
    store = tmp_path / "map.json"
    store.write_text("{not json")
    with pytest.raises(IdentityError):
        PseudonymMapper(SECRET, store)


def test_failed_save_keeps_old_store(tmp_path, monkeypatch):
    store = tmp_path / "map.json"
    m = PseudonymMapper(SECRET, store)
    m.pseudonym("person", "a")
    before = store.read_text()

    def boom(*a, **k):
        raise OSError("disk")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        m.pseudonym("person", "b")
    monkeypatch.undo()
    assert store.read_text() == before
    assert not [p for p in tmp_path.iterdir() if p.suffix == ".tmp"]


def test_load_secret_file_and_env(tmp_path, monkeypatch):
    f = tmp_path / "secret"
    f.write_text("  file-secret-0123456789\n")
    assert load_secret(f) == b"file-secret-0123456789"
    monkeypatch.setenv("MACHINA_DEID_SECRET", "env-secret-0123456789")
    assert load_secret() == b"env-secret-0123456789"
    assert load_secret(f) == b"file-secret-0123456789"  # explicit path wins
    monkeypatch.delenv("MACHINA_DEID_SECRET")
    with pytest.raises(IdentityError):
        load_secret()
    short = tmp_path / "short"
    short.write_text("abc")
    with pytest.raises(IdentityError):
        load_secret(short)
    with pytest.raises(IdentityError):
        load_secret(tmp_path / "missing")


def test_no_secret_or_identity_leak_in_repr_or_errors():
    m = PseudonymMapper(SECRET)
    m.pseudonym("person", "Jane Doe")
    assert SECRET.decode() not in repr(m) and "Jane" not in repr(m)
    with pytest.raises(IdentityError) as ei:
        m.pseudonym("bogus", "Jane Doe")
    assert "Jane" not in str(ei.value)
