import pytest

from machina_deid.state import StateDB, StateError, output_path_for


@pytest.fixture
def db(tmp_path):
    d = StateDB(tmp_path / "work" / "state.sqlite")
    yield d
    d.close()


def reg(db, tmp_path, name, size=100, transformer="pdf"):
    src = tmp_path / "src" / name
    src.parent.mkdir(exist_ok=True)
    src.write_bytes(b"x" * size)
    db.register(name, name, src, input_size=size, classification="pdf/verified", transformer=transformer)
    return src


def test_register_explicit_source_and_idempotent(db, tmp_path):
    src = reg(db, tmp_path, "a.pdf")
    assert db.get(1)["source_physical_path"] == str(src)
    assert not db.register("a.pdf", "a.pdf", src)  # duplicate ignored, state retained
    with pytest.raises(StateError):
        db.register("rel", "rel", "relative/path")
    with pytest.raises(StateError):
        db.register("../x", "../x", src)


def test_output_path_mapping_case_sensitive_and_safe(tmp_path):
    out = output_path_for(tmp_path / "o", "Dir/Part.PDF")
    assert out == tmp_path / "o" / "Dir" / "Part.PDF"
    assert output_path_for(tmp_path / "o", "dir/part.pdf") != out
    for bad in ("/abs", "a/../b", "", "./a"):
        with pytest.raises(StateError):
            output_path_for(tmp_path, bad)


def test_bounded_batch_and_lifecycle(db, tmp_path):
    for i in range(5):
        reg(db, tmp_path, f"f{i}.pdf", size=100)
    reg(db, tmp_path, "big.pdf", size=10_000)
    reg(db, tmp_path, "step.step", transformer=None)
    b = db.select_batch(max_files=3)
    assert len(b) == 3 and all(r["status"] == "in_progress" and r["attempts"] == 1 for r in b)
    b2 = db.select_batch(max_files=10, max_bytes=250)
    assert [r["manifest_path"] for r in b2] == ["f3.pdf", "f4.pdf"]  # big + no-transformer never claimed
    assert db.counts()["pending"] == 2
    rid = b[0]["id"]
    db.mark_transformed(rid, tmp_path / "out" / "f0.pdf", "abc")
    db.mark_validated(rid)
    row = db.get(rid)
    assert (row["status"], row["output_sha256"], row["validation_status"]) == ("validated", "abc", "ok")
    assert row["finished_at"]
    with pytest.raises(StateError):
        db.mark_failed(rid, "x")  # validated is terminal


def test_output_must_differ_from_source(db, tmp_path):
    src = reg(db, tmp_path, "a.pdf")
    (r,) = db.select_batch(1)
    with pytest.raises(StateError):
        db.mark_transformed(r["id"], src, "h")
    assert db.get(r["id"])["status"] == "in_progress"  # rolled back
    assert src.exists()


def test_interrupt_and_resume(tmp_path):
    path = tmp_path / "s.sqlite"
    db = StateDB(path)
    for i in range(3):
        reg(db, tmp_path, f"f{i}.pdf")
    done, crashed = db.select_batch(2)
    db.mark_transformed(done["id"], tmp_path / "o1", "h")
    db.close()  # simulate process death with one record still in_progress

    db2 = StateDB(path)
    assert db2.counts()["in_progress"] == 1
    assert db2.select_batch(5)[0]["manifest_path"] == "f2.pdf"  # in_progress not re-claimed implicitly
    assert db2.recover_interrupted() == 2
    assert db2.get(crashed["id"])["error_category"] == "interrupted"
    again = db2.select_batch(5)
    assert {r["manifest_path"] for r in again} == {crashed["manifest_path"], "f2.pdf"}
    assert db2.get(done["id"])["status"] == "transformed"  # completed work survives
    db2.mark_failed(again[0]["id"], "pdf_error")
    db2.requeue(again[0]["id"])
    assert db2.get(again[0]["id"])["status"] == "pending"
    db2.close()


def test_recover_transformed_checkpoint_validates_existing_output(tmp_path):
    path = tmp_path / "s.sqlite"
    db = StateDB(path)
    src = reg(db, tmp_path, "f.pdf")
    (rec,) = db.select_batch(1)
    output = tmp_path / "out" / "f.pdf"
    output.parent.mkdir()
    output.write_bytes(b"validated output")
    import hashlib
    db.mark_transformed(rec["id"], output, hashlib.sha256(output.read_bytes()).hexdigest(),
                        validation_status="ok", outcome="transformed_validated")
    db.close()

    db = StateDB(path)
    assert db.recover_checkpoints() == 1
    row = db.get(rec["id"])
    assert row["status"] == "validated"
    assert row["output_sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
    db.close()


def test_recover_transformed_checkpoint_fails_if_output_changed(tmp_path):
    path = tmp_path / "s.sqlite"
    db = StateDB(path)
    reg(db, tmp_path, "f.pdf")
    (rec,) = db.select_batch(1)
    output = tmp_path / "out" / "f.pdf"
    output.parent.mkdir()
    output.write_bytes(b"original")
    db.mark_transformed(rec["id"], output, "not-the-output-hash", outcome="transformed_validated")
    output.write_bytes(b"changed")
    db.close()

    db = StateDB(path)
    assert db.recover_checkpoints() == 1
    row = db.get(rec["id"])
    assert row["status"] == "failed" and row["outcome"] == "failed_validation"
    db.close()


def test_sources_never_deleted(db, tmp_path):
    src = reg(db, tmp_path, "a.pdf")
    (r,) = db.select_batch(1)
    db.mark_failed(r["id"], "boom")
    db.requeue(r["id"])
    db.recover_interrupted()
    assert src.read_bytes() == b"x" * 100
