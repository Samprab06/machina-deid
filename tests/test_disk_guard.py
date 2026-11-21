"""Physical host disk guard: stop submitting resumably when the host mount cannot absorb in-flight work."""
import json

import pytest
from production_fixtures import file_set, make_workspace

from machina_deid import production as pr


def cfg(root, head, n, **kw):
    base = dict(root=root, attempt="attempt-1", expected_rows=n, expected_revision=head, workers=2, reserve_bytes=0,
                poll_seconds=0.05, batch_files=5, heavy_slots=1)
    base.update(kw)
    return pr.Config(**base)


def test_shortfall_off_and_failsafe(tmp_path):
    c = pr.Config(root=tmp_path)
    assert pr.host_space_shortfall(c, 10**12) is None  # guard off
    c = pr.Config(root=tmp_path, host_space_path=tmp_path / "missing", host_min_free_bytes=1)
    assert pr.host_space_shortfall(c, 0)["reason"] == "host_path_unavailable"  # fail safe, no path text
    c = pr.Config(root=tmp_path, host_space_path=tmp_path, host_min_free_bytes=1)
    assert pr.host_space_shortfall(c, 0) is None
    assert pr.host_space_shortfall(c, 10**18)["reason"] == "host_free_below_minimum"


def test_guard_stops_resumably_then_completes(tmp_path, monkeypatch):
    files = file_set(3)
    head = make_workspace(tmp_path, files)
    free = {"v": 0}
    monkeypatch.setattr(pr, "host_free_bytes", lambda p: free["v"])
    out = pr.run(cfg(tmp_path, head, len(files), host_space_path=tmp_path, host_min_free_bytes=1))
    assert out["phase"] == "transform" and out["remaining"] == len(files)
    stop = tmp_path / "ops/attempt-1" / pr.STORAGE_STOP
    rec = json.loads(stop.read_text())
    assert rec["reason"] == "host_free_below_minimum" and rec["stopped"]
    assert out["session"]["storage_stop"]["outputs_this_session"] == 0
    free["v"] = 10**15
    out = pr.run(cfg(tmp_path, head, len(files), host_space_path=tmp_path, host_min_free_bytes=1))
    assert out["phase"] == "packaged" and not stop.exists()


def test_cli_requires_both_flags():
    with pytest.raises(SystemExit):
        pr.main(["run", "--host-min-free-bytes", "5", "--expected-revision", "x"])
