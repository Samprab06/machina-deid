"""Production runner: resume, concurrency, reproducibility, migration (synthetic fixtures only)."""
import json
import sqlite3
import threading
import time

import pytest
from production_fixtures import SEED, file_set, make_workspace, tree_digest, write_input

from machina_deid import production as pr
from machina_deid.catalog import IdentityCatalog

DC3 = next(iter(pr.MIGRATABLE_FROM))


def cfg_for(root, head, n, **kw):
    base = dict(root=root, expected_rows=n, expected_revision=head, workers=2, reserve_bytes=0,
                poll_seconds=0.05, batch_files=7, heavy_slots=1)
    base.update(kw)
    return pr.Config(**base)


def ledger_rows(root):
    """Ledger contents; DXF digests are excluded (volatile ezdxf save metadata, see stable_bytes)."""
    db = sqlite3.connect(root / "ops/attempt-1/ledger.sqlite")
    try:
        outputs = [(p, s) + ((None, None) if p.endswith(".dxf") else (n, d)) for p, s, n, d in db.execute("SELECT * FROM output")]
        return (sorted(db.execute("SELECT * FROM discovery")), sorted(outputs),
                sorted(db.execute("SELECT * FROM output_detail")))
    finally:
        db.close()


@pytest.fixture(scope="module")
def files():
    return file_set(4)


@pytest.fixture(scope="module")
def serial_run(tmp_path_factory, files):
    root = tmp_path_factory.mktemp("serial")
    head = make_workspace(root, files)
    out = pr.run(cfg_for(root, head, len(files), workers=1))
    return root, out


def test_full_run_statuses_passthrough_and_raw_immutability(serial_run, files):
    root, out = serial_run
    assert out["phase"] == "packaged"
    assert out["manifest_files"] == len(files)
    oc = out["outcomes"]
    assert oc["transformed"] > 0 and oc["unsupported_passthrough"] > 0 and oc.get("failed", 0) > 0
    assert sum(oc.values()) == len(files)
    # raw inputs untouched
    for rel, data in files.items():
        assert (root / "input" / rel).read_bytes() == data
    corpus = root / "attempts/attempt-1/corpus"
    _, outputs, _ = ledger_rows(root)
    for path, status, size, digest in outputs:
        data = (corpus / path).read_bytes()
        if status != "transformed":  # byte-identical pass-through
            assert data == files[path]
        else:
            assert data != files[path]
    assert (root / "attempts/attempt-1/README.txt").read_text().count("NOT\nde-identified") == 1
    assert (root / "attempts/attempt-1/code/run-dependencies.txt").exists()
    assert not list((root / "ops/attempt-1/staging").iterdir())
    # private material stays out of the attempt package
    names = {p.name for p in (root / "attempts/attempt-1").rglob("*")}
    assert not names & {"seed", "ledger.sqlite", "catalog.sqlite", "catalog.pseudonyms.json"}


def test_parallel_is_byte_identical_to_serial(serial_run, tmp_path, files):
    root_s, _ = serial_run
    head = make_workspace(tmp_path, files)
    out = pr.run(cfg_for(tmp_path, head, len(files), workers=3, heavy_slots=2))
    assert out["phase"] == "packaged"
    assert tree_digest(tmp_path / "attempts/attempt-1/corpus") == tree_digest(root_s / "attempts/attempt-1/corpus")
    assert ledger_rows(tmp_path) == ledger_rows(root_s)


def test_interrupt_and_resume_matches_uninterrupted(serial_run, tmp_path, files):
    root_s, _ = serial_run
    head = make_workspace(tmp_path, files)
    c = cfg_for(tmp_path, head, len(files), stop_after_outputs=5)
    first = pr.run(c)
    assert first["phase"] == "transform" and first["remaining"] > 0
    # a stale staging file from a crash is cleaned, never published
    (tmp_path / "ops/attempt-1/staging/s-stale.pdf").write_bytes(b"junk")
    c.stop_after_outputs = 0
    assert pr.run(c)["phase"] == "packaged"
    assert tree_digest(tmp_path / "attempts/attempt-1/corpus") == tree_digest(root_s / "attempts/attempt-1/corpus")
    assert ledger_rows(tmp_path) == ledger_rows(root_s)


def test_discovery_catches_up_while_download_runs(tmp_path, files):
    present = set(list(files)[: len(files) // 2])
    head = make_workspace(tmp_path, files, present=present)
    c = cfg_for(tmp_path, head, len(files))

    def finish_download():
        time.sleep(0.5)
        for rel, data in files.items():
            if rel not in present:
                write_input(tmp_path, rel, data)
        (tmp_path / "ops/download.exit").write_text("0\n")

    t = threading.Thread(target=finish_download)
    t.start()
    out = pr.run(c, discovery_only=True)
    t.join()
    assert sum(out["discovery"].values()) == len(files)


def test_downloader_failure_stops_with_count(tmp_path, files):
    missing = next(iter(files))
    head = make_workspace(tmp_path, files, present=set(files) - {missing})
    (tmp_path / "ops/download.exit").write_text("1\n")
    with pytest.raises(pr.RunnerError, match="1 rows await"):
        pr.run(cfg_for(tmp_path, head, len(files)))


def test_size_mismatch_waits_and_transform_needs_complete_discovery(tmp_path, files):
    rel = next(iter(files))
    head = make_workspace(tmp_path, files)
    (tmp_path / "input" / rel).write_bytes(files[rel][:-1])  # partial download
    (tmp_path / "ops/download.exit").write_text("0\n")
    with pytest.raises(pr.RunnerError, match="await complete input"):
        pr.run(cfg_for(tmp_path, head, len(files)))
    assert not (tmp_path / "attempts/attempt-1/corpus").exists()


def test_frozen_catalog_tamper_is_refused(tmp_path, files):
    head = make_workspace(tmp_path, files)
    c = cfg_for(tmp_path, head, len(files), stop_after_outputs=2)
    pr.run(c)
    cat = IdentityCatalog(tmp_path / "ops/attempt-1/catalog.sqlite", SEED)
    cat.approve("person", "Someone Else Entirely")
    cat.close()
    c.stop_after_outputs = 0
    with pytest.raises(pr.RunnerError, match="catalog"):
        pr.run(c)


def test_revision_pin_and_expected_revision(tmp_path, files):
    head = make_workspace(tmp_path, files)
    with pytest.raises(pr.RunnerError, match="expected revision"):
        pr.run(cfg_for(tmp_path, "0" * 40, len(files)))
    (tmp_path / "ops/attempt-1/revision").write_text(DC3 + "\n")
    with pytest.raises(pr.RunnerError, match="migrate"):
        pr.run(cfg_for(tmp_path, head, len(files)))


def test_lock_prevents_concurrent_runners(tmp_path, files):
    head = make_workspace(tmp_path, files)
    c = cfg_for(tmp_path, head, len(files))
    with pr.RunLock(c):
        with pytest.raises(pr.RunnerError, match="lock"):
            pr.run(c)


def test_md5_mismatch_is_not_recorded(tmp_path, files):
    head = make_workspace(tmp_path, files)
    rel = "proj0/part0/notes.bin"
    man = tmp_path / "input/.get-input/manifest.tsv"
    lines = [ln if not ln.startswith(rel + "\t") else "\t".join(ln.split("\t")[:2] + ["0" * 32, "/x"])
             for ln in man.read_text().splitlines()]
    man.write_text("\n".join(lines) + "\n")
    out = pr.run(cfg_for(tmp_path, head, len(files)))
    assert out["remaining"] == 1 and out["errors"] == {"input_md5_mismatch": 1}
    assert pr.main(["run", "--root", str(tmp_path), "--expected-rows", str(len(files)), "--expected-revision", head,
                    "--reserve-bytes", "0", "--workers", "2"]) == 3


def test_worker_crash_is_isolated_and_passed_through(tmp_path, files, monkeypatch):
    monkeypatch.setenv(pr.TEST_CRASH_ENV, "part1/layout.dxf")
    head = make_workspace(tmp_path, files)
    out = pr.run(cfg_for(tmp_path, head, len(files)))
    assert out["phase"] == "packaged"
    assert out["failed_reasons"].get("worker_crash") == 1
    data = (tmp_path / "attempts/attempt-1/corpus/proj1/part1/layout.dxf").read_bytes()
    assert data == files["proj1/part1/layout.dxf"]


def _old_runner_state(tmp_path, files):
    """Discovery-only state as the dc3da69 serial runner leaves it (same tables, seed, catalog)."""
    head = make_workspace(tmp_path, files)
    c = cfg_for(tmp_path, head, len(files), workers=1)
    pr.run(c, discovery_only=True)
    db = sqlite3.connect(tmp_path / "ops/attempt-1/ledger.sqlite")
    db.execute("DROP TABLE output_detail")
    db.execute("DROP TABLE runner_meta")
    db.commit()
    db.close()
    (tmp_path / "ops/attempt-1/revision").write_text(DC3 + "\n")
    return head, c


def test_migration_reuses_discovery_and_matches_fresh_run(serial_run, tmp_path, files):
    root_s, _ = serial_run
    head, c = _old_runner_state(tmp_path, files)
    before, _, _ = ledger_rows_pre(tmp_path)
    with pytest.raises(pr.RunnerError, match="dc3da69"):
        pr.migrate(c, "f" * 40, head)
    rec = pr.migrate(c, DC3, head)
    assert rec["migrated"] and rec["discovery_rows"] == len(files)
    assert (tmp_path / "ops/attempt-1/pre-migration-dc3da69/ledger.sqlite").exists()
    assert (tmp_path / "ops/attempt-1/revision").read_text().strip() == head
    assert pr.migrate(c, DC3, head) == {"migrated": False, "already": True}
    out = pr.run(c)
    assert out["phase"] == "packaged"
    assert ledger_rows(tmp_path)[0] == before  # discovery ledger reused, not redone
    assert tree_digest(tmp_path / "attempts/attempt-1/corpus") == tree_digest(root_s / "attempts/attempt-1/corpus")


def ledger_rows_pre(root):
    db = sqlite3.connect(root / "ops/attempt-1/ledger.sqlite")
    try:
        return sorted(db.execute("SELECT * FROM discovery")), None, None
    finally:
        db.close()


def test_migration_refused_after_output_and_rollback(tmp_path, files):
    head, c = _old_runner_state(tmp_path, files)
    pr.migrate(c, DC3, head)
    assert pr.rollback(c) == {"rolled_back_to": DC3}
    assert (tmp_path / "ops/attempt-1/revision").read_text().strip() == DC3
    db = sqlite3.connect(tmp_path / "ops/attempt-1/ledger.sqlite")
    db.execute("INSERT INTO output VALUES ('x', 'transformed', 1, 'd')")
    db.commit()
    db.close()
    with pytest.raises(pr.RunnerError, match="already committed"):
        pr.migrate(c, DC3, head)


def test_migration_refuses_wrong_checkout(tmp_path, files):
    head, c = _old_runner_state(tmp_path, files)
    with pytest.raises(pr.RunnerError, match="target revision"):
        pr.migrate(c, DC3, "1" * 40)


def test_cli_status_redacted(serial_run, capsys):
    root, _ = serial_run
    assert pr.main(["status", "--root", str(root)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert set(out) == {"revision", "discovery", "outputs"}
    assert "proj" not in json.dumps(out) and "Jane" not in json.dumps(out)


def test_documented_adoption_commands_end_to_end(tmp_path, files):
    """The exact runbook sequence: migrate -> run -> status, via the script entry point."""
    import subprocess
    import sys
    from pathlib import Path
    head, _ = _old_runner_state(tmp_path, files)
    script = Path(__file__).resolve().parents[1] / "scripts/run_full_corpus.py"
    common = ["--root", str(tmp_path)]
    run = lambda *a: subprocess.run([sys.executable, str(script), *a], capture_output=True, text=True)  # noqa: E731
    r = run("run", *common, "--expected-rows", str(len(files)), "--expected-revision", head, "--reserve-bytes", "0")
    assert r.returncode == 2 and "migrate" in r.stderr
    r = run("migrate", *common, "--from-revision", DC3, "--expected-revision", head)
    assert r.returncode == 0, r.stderr
    r = run("run", *common, "--expected-rows", str(len(files)), "--expected-revision", head, "--reserve-bytes", "0",
            "--workers", "2", "--heavy-slots", "1", "--poll-seconds", "0.05")
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout.strip().splitlines()[-1])["phase"] == "packaged"
    assert (tmp_path / "attempts/attempt-1/code/run_full_corpus.py").exists()
    r = run("status", *common)
    assert r.returncode == 0 and sum(json.loads(r.stdout)["outputs"].values()) == len(files)
