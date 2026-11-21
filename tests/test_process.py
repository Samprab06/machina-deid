import hashlib
import json
import os

import pymupdf
import pytest

from machina_deid import cli
from machina_deid import process as pr
from machina_deid import transfer as tr
from machina_deid.state import StateDB

NAME = "Jane Quincy Doe"


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def make_pdf(path):
    doc = pymupdf.open()
    p = doc.new_page(width=300, height=400)
    p.insert_text((50, 100), f"Prepared by {NAME} for review", fontsize=12)
    doc.set_metadata({"author": NAME})
    doc.save(path)
    doc.close()


@pytest.fixture
def env(tmp_path):
    src, out, work = tmp_path / "src", tmp_path / "out", tmp_path / "work"
    (src / "a" / "b").mkdir(parents=True)
    files = {
        "a/one.sldprt": b"native-bytes-1",
        "a/b/copy_of_one.sldprt": b"native-bytes-1",  # identical content, different path
        "a/two.txt": b"hello",
        "a/empty.dat": b"",
        "a/fake.pdf": b"not really a pdf",  # mismatch? ext pdf without magic
    }
    for rel, data in files.items():
        (src / rel).write_bytes(data)
    make_pdf(src / "a" / "doc.pdf")
    rows = [(rel, (src / rel).stat().st_size) for rel in sorted(files) + ["a/doc.pdf"]]
    rows.append(("a/missing.bin", 5))
    man = tmp_path / "manifest.tsv"
    man.write_text("# header\n" + "".join(f"{p}\t{s}\t\n" for p, s in rows))
    return dict(manifest=man, source_root=src, output_root=out, work_dir=work, src=src, out=out, work=work)


def run(env, **kw):
    args = {k: env[k] for k in ("manifest", "source_root", "output_root", "work_dir")}
    kw.setdefault("limit", 100)
    return pr.process(**args, **kw)


def test_passthrough_aliases_and_hashes(env):
    res = run(env)
    assert res["this_run"]["passthrough"] >= 4
    out = env["out"]
    a, b = out / "a/one.sldprt", out / "a/b/copy_of_one.sldprt"
    assert a.read_bytes() == b.read_bytes() == b"native-bytes-1"
    assert os.stat(a).st_ino == os.stat(b).st_ino  # hardlink alias on same fs
    assert res["this_run"]["artifacts_built"] < res["claimed"]  # built once, materialized twice
    assert (out / "a/empty.dat").read_bytes() == b""
    db = StateDB(env["work"] / "state.sqlite")
    rows = {r["manifest_path"]: r for r in db.conn.execute("SELECT * FROM records")}
    r = rows["a/one.sldprt"]
    assert r["status"] == "validated" and r["disposition"] == "passthrough"
    assert r["note"] == "unsupported_format_unchanged"
    assert r["output_sha256"] == sha(b"native-bytes-1") == r["input_sha256"]
    assert rows["a/b/copy_of_one.sldprt"]["output_sha256"] == r["output_sha256"]
    # PDFs without a map are passed through unchanged with a limitation, never guessed
    pdf = rows["a/doc.pdf"]
    assert pdf["note"] == "no_identity_map_supplied" and pdf["disposition"] == "passthrough"
    assert (out / "a/doc.pdf").read_bytes() == (env["src"] / "a/doc.pdf").read_bytes()
    assert rows["a/fake.pdf"]["status"] == "skipped" and rows["a/fake.pdf"]["error_category"] == "classification_mismatch"
    assert rows["a/missing.bin"]["status"] == "skipped"
    assert not (out / "a/fake.pdf").exists() and not (out / "a/missing.bin").exists()
    db.close()


def test_copy_mode_and_sources_untouched(env):
    before = {p: sha(p.read_bytes()) for p in env["src"].rglob("*") if p.is_file()}
    run(env, link_mode="copy")
    a, b = env["out"] / "a/one.sldprt", env["out"] / "a/b/copy_of_one.sldprt"
    assert os.stat(a).st_ino != os.stat(b).st_ino and a.read_bytes() == b.read_bytes()
    assert {p: sha(p.read_bytes()) for p in env["src"].rglob("*") if p.is_file()} == before


def test_resume_is_idempotent_and_keeps_outputs(env):
    run(env)
    out_a = env["out"] / "a/two.txt"
    ino, mtime = os.stat(out_a).st_ino, os.stat(out_a).st_mtime_ns
    res = run(env)
    assert res["claimed"] == 0
    assert os.stat(out_a).st_ino == ino and os.stat(out_a).st_mtime_ns == mtime


def test_resume_after_interruption(env):
    run(env, limit=2)
    db = StateDB(env["work"] / "state.sqlite")
    assert db.counts()["validated"] + db.counts()["skipped"] >= 1
    # Register a fresh pending record, then simulate a crash after claiming it.
    db.register("interrupted.bin", "a/two.txt", env["src"] / "a/two.txt",
                input_size=5, classification="txt/unverified")
    rec = db.select_batch(1, include_untransformable=True)[0]
    db.close()
    res = run(env)
    assert res["this_run"]["recovered_interrupted"] == 1
    db = StateDB(env["work"] / "state.sqlite")
    assert db.get(rec["id"])["status"] in ("validated", "skipped")
    assert db.counts()["in_progress"] == 0
    db.close()


def test_transform_with_explicit_map_and_alias(env):
    import shutil
    shutil.copy(env["src"] / "a/doc.pdf", env["src"] / "a/b/doc_alias.pdf")
    man = env["manifest"]
    man.write_text(man.read_text() + f"a/b/doc_alias.pdf\t{(env['src'] / 'a/b/doc_alias.pdf').stat().st_size}\t\n")
    mp = env["work"].parent / "pdfmap.json"
    mp.write_text(json.dumps({NAME: "Person-ABCDE"}))
    res = run(env, pdf_map=str(mp))
    assert res["this_run"]["transformed"] == 2
    out = pymupdf.open(env["out"] / "a/doc.pdf")
    assert NAME not in out[0].get_text() and "Person-ABCDE" in out[0].get_text()
    out.close()
    assert (env["out"] / "a/doc.pdf").read_bytes() == (env["out"] / "a/b/doc_alias.pdf").read_bytes()
    db = StateDB(env["work"] / "state.sqlite")
    r = db.conn.execute("SELECT * FROM records WHERE manifest_path='a/doc.pdf'").fetchone()
    assert r["disposition"] == "transformed" and r["validation_status"] == "ok"
    assert r["output_sha256"] != r["input_sha256"]
    db.close()


def test_failed_transform_not_materialized(env, monkeypatch):
    mp = env["work"].parent / "pdfmap.json"
    mp.write_text(json.dumps({NAME: "Person-ABCDE"}))
    (env["src"] / "a/broken.pdf").write_bytes(b"%PDF-1.4\ngarbage")
    man = env["manifest"]
    man.write_text(man.read_text() + "a/broken.pdf\t16\t\n")
    res = run(env, pdf_map=str(mp))
    assert res["this_run"]["failed"] == 1
    assert not (env["out"] / "a/broken.pdf").exists()
    db = StateDB(env["work"] / "state.sqlite")
    r = db.conn.execute("SELECT * FROM records WHERE manifest_path='a/broken.pdf'").fetchone()
    assert r["status"] == "failed" and r["error_category"].startswith("transform_error")
    db.close()


def test_existing_different_output_not_overwritten(env):
    (env["out"] / "a").mkdir(parents=True)
    (env["out"] / "a/two.txt").write_bytes(b"someone else's file")
    res = run(env)
    assert res["this_run"]["failed"] == 1
    assert (env["out"] / "a/two.txt").read_bytes() == b"someone else's file"


def test_non_overlap_enforced(env, tmp_path):
    for out, work in ((env["src"] / "o", env["work"]), (tmp_path, env["work"]),
                      (env["out"], env["out"] / "w"), (env["out"], env["src"] / "w")):
        with pytest.raises(pr.ProcessError):
            pr.process(env["manifest"], env["src"], out, work, limit=5)


def test_bounded_limits(env):
    with pytest.raises(pr.ProcessError):
        run(env, limit=0)
    with pytest.raises(pr.ProcessError):
        run(env, limit=pr.HARD_MAX_FILES + 1)
    res = run(env, limit=2)
    assert res["claimed"] <= 2
    db = StateDB(env["work"] / "state.sqlite")
    assert db.conn.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 2  # only `limit` rows ingested
    db.close()
    res = run(env, limit=100, max_bytes=8)  # byte bound: oversize files stay pending
    db = StateDB(env["work"] / "state.sqlite")
    assert db.counts()["pending"] >= 0
    claimed_bytes = db.conn.execute("SELECT COALESCE(SUM(input_size),0) FROM records WHERE attempts>0").fetchone()[0]
    assert res["claimed"] <= 100
    db.close()
    assert claimed_bytes >= 0


def test_dry_run_writes_nothing(env):
    res = pr.plan(env["manifest"], env["src"], limit=100)
    assert res["dry_run"] and res["alias_paths"] >= 1 and res["unique_artifacts"] < res["rows_considered"]
    assert not env["out"].exists() and not env["work"].exists()


def test_cli_process_and_transfer(env, capsys, tmp_path):
    rc = cli.main(["process", "--manifest", str(env["manifest"]), "--source-root", str(env["src"]),
                   "--output-root", str(env["out"]), "--work-dir", str(env["work"]), "--limit", "10"])
    assert rc == 0 and json.loads(capsys.readouterr().out)["claimed"] > 0
    rc = cli.main(["transfer", "--local-dir", str(env["out"]), "--dest", "me@host:/data/x"])
    text = capsys.readouterr().out
    assert rc == 0 and "rsync -aH --partial" in text and "not executed" in text and "--delete" not in text


def test_transfer_rejects_bad_dest(tmp_path):
    for bad in ("-oProxyCommand=x", "/local/path", "host:relative", "host"):
        with pytest.raises(tr.TransferError):
            tr.build_commands(tmp_path, bad)
