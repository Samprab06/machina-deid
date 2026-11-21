import hashlib
import json

import pytest

from machina_deid import readiness as rd
from machina_deid import cli


def test_manifest_report_accounts_duplicate_rows_and_hashes(tmp_path):
    manifest = tmp_path / "manifest.tsv"
    manifest.write_text("a.txt\t3\tdead\n" "a.txt\t3\tdead\n" "b.bin\t2\tdead\n")
    report = rd.new_report(manifest)
    assert report["manifest_rows"] == 3
    assert report["duplicate_manifest_paths"] == ["a.txt"]
    assert report["duplicate_source_hashes"]["dead"] == ["1", "2", "3"]
    assert [r["outcome"] for r in report["rows"]] == ["failed_validation", "failed_validation", None]
    assert rd.outcome_counts(report)["failed_validation"] == 2


def test_checkpoint_is_resumable_and_exactly_once(tmp_path):
    manifest = tmp_path / "manifest.tsv"
    manifest.write_text("a.txt\t3\n" "b.txt\t2\n")
    report = rd.new_report(manifest)
    checkpoint = tmp_path / "checkpoint.json"
    result = rd.record_batch(report, [{"row_id": "1", "outcome": "unsupported_passthrough",
                                       "output_path": "a.txt", "output_sha256": "x", "output_bytes": 3}], checkpoint)
    assert result["updated"] == 1 and checkpoint.exists()
    loaded = rd.read_checkpoint(checkpoint)
    rd.record_batch(loaded, [{"row_id": "1", "outcome": "unsupported_passthrough",
                              "output_path": "a.txt", "output_sha256": "x", "output_bytes": 3}])
    with pytest.raises(ValueError):
        rd.record_batch(loaded, [{"row_id": "1", "outcome": "transformed_validated", "output_path": "a.txt"}])
    assert rd.outcome_counts(loaded)["pending"] == 1


def _layout(tmp_path, code_dir="code"):
    root = tmp_path / "attempt-1"
    (root / code_dir).mkdir(parents=True)
    (root / code_dir / "pyproject.toml").write_text("[project]\n")
    (root / "corpus").mkdir()
    (root / "README.txt").write_text("readme")
    (root / "corpus" / "a.txt").write_bytes(b"abc")
    manifest = tmp_path / "manifest.tsv"
    manifest.write_text("a.txt\t3\n")
    return root, manifest


def test_official_layout_uses_code_dir(tmp_path):
    root, manifest = _layout(tmp_path)
    result = rd.validate_final_layout(root, manifest)
    assert result["valid"], result["errors"]
    assert result["warnings"] == []
    plan = rd.upload_plan(root, "gs://bucket/teams/t/attempt-1")
    assert plan["paths"]["code"] == str(root / "code")
    assert plan["argv"][plan["argv"].index("--code") + 1] == str(root / "code")
    assert plan["argv"][plan["argv"].index("--corpus") + 1] == str(root / "corpus")
    assert plan["destination"] == "gs://bucket/teams/t/attempt-1/"
    assert plan["remote"]["code"].endswith("/attempt-1/code/")


def test_legacy_pipeline_source_layout_still_validates_with_warning(tmp_path):
    root, manifest = _layout(tmp_path, "pipeline-source")
    result = rd.validate_final_layout(root, manifest)
    assert result["valid"], result["errors"]
    assert result["warnings"]
    plan = rd.upload_plan(root, "gs://bucket/teams/t/attempt-2/")
    assert plan["argv"][plan["argv"].index("--code") + 1] == str(root / "pipeline-source")
    assert plan["remote"]["code"] == "gs://bucket/teams/t/attempt-2/code/"


def test_ambiguous_or_missing_code_tree_is_rejected(tmp_path):
    root, manifest = _layout(tmp_path)
    (root / "pipeline-source").mkdir()
    assert any("ambiguous" in e for e in rd.validate_final_layout(root, manifest)["errors"])
    with pytest.raises(ValueError):
        rd.upload_plan(root, "gs://bucket/teams/t/attempt-1/")
    (root / "pipeline-source").rmdir()
    for p in (root / "code").iterdir():
        p.unlink()
    (root / "code").rmdir()
    assert "missing code" in rd.validate_final_layout(root, manifest)["errors"]
    with pytest.raises(ValueError):
        rd.upload_plan(root, "gs://bucket/teams/t/attempt-1/")


def test_upload_plan_rejects_corpus_level_destination(tmp_path):
    root, _ = _layout(tmp_path)
    for bad in ("gs://bucket/teams/t/attempt-1/corpus/", "s3://bucket/submissions/attempt-1/"):
        with pytest.raises(ValueError):
            rd.upload_plan(root, bad)
    assert cli.main(["readiness", "upload-plan", "--layout-root", str(root),
                     "--destination", "gs://bucket/teams/t/attempt-1/corpus/"]) == 2


def test_organizer_size_limit_is_decimal_2gb(tmp_path, monkeypatch):
    assert rd.MAX_FILE_BYTES == 2_000_000_000
    root, manifest = _layout(tmp_path)
    assert not rd.validate_final_layout(root, manifest, max_file_bytes=2)["valid"]
    monkeypatch.setattr(rd, "MAX_CORPUS_OBJECTS", 0)
    assert any("objects" in e for e in rd.validate_final_layout(root, manifest)["errors"])


def test_large_file_hash_is_streamed(tmp_path, monkeypatch):
    p = tmp_path / "big.bin"
    data = bytes(range(256)) * 5000
    p.write_bytes(data)
    monkeypatch.setattr(type(p), "read_bytes", lambda self: pytest.fail("whole-file read"))
    assert rd.sha256_file(p, chunk_bytes=4096) == hashlib.sha256(data).hexdigest()
    root, manifest = _layout(tmp_path)
    report = rd.new_report(manifest)
    rd.record_batch(report, [{"row_id": "1", "outcome": "unsupported_passthrough", "output_path": "a.txt",
                              "output_sha256": hashlib.sha256(b"abc").hexdigest(), "output_bytes": 3}])
    assert rd.validate_final_layout(root, manifest, report=report)["valid"]


def test_final_layout_validates_paths_hashes_and_size(tmp_path):
    root = tmp_path / "attempt"
    (root / "code").mkdir(parents=True)
    (root / "corpus").mkdir()
    (root / "README.txt").write_text("readme")
    payload = b"abc"
    (root / "corpus" / "a.txt").write_bytes(payload)
    manifest = tmp_path / "manifest.tsv"
    manifest.write_text("a.txt\t3\n")
    report = rd.new_report(manifest)
    rd.record_batch(report, [{"row_id": "1", "outcome": "unsupported_passthrough", "output_path": "a.txt",
                              "output_sha256": hashlib.sha256(payload).hexdigest(), "output_bytes": 3}])
    assert rd.validate_final_layout(root, manifest, report=report)["valid"]
    (root / "corpus" / "a.txt").write_bytes(b"bad")
    assert not rd.validate_final_layout(root, manifest, report=report)["valid"]


def test_upload_plan_is_credential_free(tmp_path):
    root, _ = _layout(tmp_path)
    plan = rd.upload_plan(root, "gs://bucket/team/attempt-1/")
    assert plan["executed"] is False and plan["credentials_required"] is False
    assert "--corpus" in plan["argv"] and "--code" in plan["argv"]
    assert "--token" not in plan["argv"]


def test_readiness_cli_report(tmp_path, capsys):
    manifest = tmp_path / "manifest.tsv"
    manifest.write_text("x.dat\t1\n")
    checkpoint = tmp_path / "report.json"
    assert cli.main(["readiness", "report", "--manifest", str(manifest), "--out", str(checkpoint)]) == 0
    assert json.loads(capsys.readouterr().out)["manifest_rows"] == 1
    assert checkpoint.exists()
