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


def test_final_layout_validates_paths_hashes_and_size(tmp_path):
    root = tmp_path / "attempt"
    (root / "pipeline-source").mkdir(parents=True)
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
    plan = rd.upload_plan(tmp_path / "attempt", "gs://bucket/team/attempt-1/")
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
