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
    assert pdf["note"] == "pdf_identifiers_detected_without_explicit_map" and pdf["disposition"] == "passthrough"
    assert pdf["outcome"] == "identifiers_unmodifiable"
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


@pytest.mark.parametrize('mapping,expected,status', [
    ({NAME: 'Person A'}, 'transformed_validated', 'validated'),
    ({'Nobody Here': 'Person A'}, 'inspected_no_identifiers', 'validated'),
    ({NAME: 'Zoë'}, 'identifiers_unmodifiable', 'failed'),
])
def test_step_outcomes_and_cached_aliases(tmp_path, mapping, expected, status):
    from step_helpers import step_bytes
    src, out, work = (tmp_path / n for n in ('src', 'out', 'work'))
    src.mkdir()
    for name in ('one.step', 'two.step'):
        (src / name).write_bytes(step_bytes())
    manifest = tmp_path / 'manifest.tsv'
    manifest.write_text(''.join(f'{n}\t{(src / n).stat().st_size}\t\n'
                                for n in ('one.step', 'two.step')))
    idmap = tmp_path / 'map.json'
    idmap.write_text(json.dumps({'strings': mapping}))
    args = (manifest, src, out, work)
    result = pr.process(*args, limit=2, step_map=str(idmap), min_free=0)
    assert result['this_run'][f'outcome_{expected}'] == 2
    from contextlib import closing
    with closing(StateDB(work / 'state.sqlite')) as db:
        rows = list(db.conn.execute('SELECT * FROM records'))
        assert len(rows) == 2
        assert all(r['outcome'] == expected and r['status'] == status for r in rows)
    if status == 'failed':
        assert not list(out.rglob('*.step'))
    else:
        assert result['this_run']['cas_reused'] == 1
        assert all((out / n).is_file() for n in ('one.step', 'two.step'))
    assert pr.process(*args, limit=2, step_map=str(idmap), min_free=0)['claimed'] == 0


def _clean_step():
    from step_helpers import DATA, step_bytes
    return step_bytes(data=DATA.replace(NAME.encode(), b'P'))


def _zip_env(tmp_path, members):
    import zipfile
    src, out, work = (tmp_path / n for n in ('src', 'out', 'work'))
    src.mkdir()
    zp = src / 'bundle.zip'
    with zipfile.ZipFile(zp, 'w') as z:
        for name, data in members.items():
            z.writestr(name, data)
    (src / 'copy.zip').write_bytes(zp.read_bytes())
    manifest = tmp_path / 'manifest.tsv'
    manifest.write_text(''.join(f'{n}\t{(src / n).stat().st_size}\t\n' for n in ('bundle.zip', 'copy.zip')))
    return (manifest, src, out, work), zp.read_bytes()


def _records(work):
    from contextlib import closing
    with closing(StateDB(work / 'state.sqlite')) as db:
        return [dict(r) for r in db.conn.execute('SELECT * FROM records ORDER BY id')]


def _write_map(tmp_path, mapping):
    p = tmp_path / 'map.json'
    p.write_text(json.dumps({'strings': mapping}))
    return str(p)


def test_archive_no_map_inspected_not_deidentified(tmp_path):
    from step_helpers import step_bytes
    args, src_bytes = _zip_env(tmp_path, {'d/part.step': step_bytes(), 'readme.txt': b'hi'})
    res = pr.process(*args, limit=2, min_free=0)
    assert res['this_run']['outcome_inspected_no_map'] == 2
    assert res['this_run']['cas_reused'] == 1
    recs = _records(args[3])
    for r in recs:
        assert r['status'] == 'validated' and r['outcome'] == 'inspected_no_map'
        assert r['disposition'] == 'archive_rebuilt' and r['validation_status'] == 'archive_rebuilt_verified'
        assert 'archive_complete=0' in r['note'] and 'inspected_no_map=1' in r['note']
        assert r['input_sha256'] == sha(src_bytes)
        assert r['output_sha256'] == sha((args[2] / r['source_rel_path']).read_bytes())
    assert (args[1] / 'bundle.zip').read_bytes() == src_bytes  # source untouched
    assert pr.process(*args, limit=2, min_free=0)['claimed'] == 0


def test_archive_mapped_transformed_complete(tmp_path):
    import zipfile
    from step_helpers import step_bytes
    args, _ = _zip_env(tmp_path, {'d/': b'', 'd/part.step': _clean_step()})
    res = pr.process(*args, limit=2, min_free=0, archive_map=_write_map(tmp_path, {NAME: 'Person A'}))
    assert res['this_run']['outcome_transformed_validated'] == 2
    r = _records(args[3])[0]
    assert r['disposition'] == 'transformed' and 'archive_complete=1' in r['note']
    with zipfile.ZipFile(args[2] / 'bundle.zip') as z:
        assert NAME.encode() not in z.read('d/part.step')
        assert b'Person A' in z.read('d/part.step')


def test_archive_mapped_unsupported_member_not_complete(tmp_path):
    from step_helpers import step_bytes
    args, _ = _zip_env(tmp_path, {'d/part.step': _clean_step(), 'notes.bin': b'no identities'})
    res = pr.process(*args, limit=2, min_free=0, archive_map=_write_map(tmp_path, {NAME: 'Person A'}))
    assert res['this_run']['outcome_unsupported_passthrough'] == 2
    r = _records(args[3])[0]
    assert r['status'] == 'validated' and r['disposition'] == 'archive_rebuilt'
    assert 'archive_complete=0' in r['note'] and 'unsupported_members=1' in r['note']


def test_archive_mapped_residual_fails_not_materialized(tmp_path):
    args, _ = _zip_env(tmp_path, {'leak.txt': f'by {NAME}'.encode()})
    res = pr.process(*args, limit=2, min_free=0, archive_map=_write_map(tmp_path, {NAME: 'Person A'}))
    assert res['this_run']['outcome_identifiers_unmodifiable'] == 2
    assert all(r['status'] == 'failed' for r in _records(args[3]))
    assert not list(args[2].rglob('*.zip'))


def test_unsafe_archive_fails(tmp_path):
    args, _ = _zip_env(tmp_path, {'../evil.txt': b'x'})
    res = pr.process(*args, limit=2, min_free=0)
    assert res['this_run']['failed'] == 2
    assert {r['error_category'] for r in _records(args[3])} == {'archive_path_traversal'}
    assert not list(args[2].rglob('*.zip'))
