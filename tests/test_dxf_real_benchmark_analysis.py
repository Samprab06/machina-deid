"""Redacted analysis of the frozen real DXF sample.

This is intentionally a safety test, not an approval fixture: real DXF metadata is
observed and classified, but no value is approved without typed corroboration.
"""
from pathlib import Path

import pytest

import ezdxf

from machina_deid.discovery import discover_dxf
from machina_deid.twopass import auto_decision


BENCHMARK = Path("/srv/new2/machina-deid/work/phase5e-balanced/raw")


def _real_dxfs():
    paths = sorted(BENCHMARK.glob("dxf-*.dxf"))
    if not paths:
        pytest.skip("frozen real DXF benchmark is not available")
    return paths


def test_frozen_real_dxf_candidates_remain_review_only():
    """Title-block/header observations have no objectively typed identity proof."""
    candidates = [c for path in _real_dxfs() for c in discover_dxf(path)]
    assert candidates, "the frozen DXF sample must exercise discovery"
    assert all(auto_decision(c)[0] == "review" for c in candidates)
    assert not any(auto_decision(c)[0] == "approved" for c in candidates)
    assert all(c.kind != "person_or_org" for c in candidates)


def test_frozen_real_dxf_has_no_external_reference_identity_surface():
    """No sampled XREF path is available to justify a new rewrite rule."""
    xref_paths = []
    for path in _real_dxfs():
        doc = ezdxf.readfile(path)
        for block in doc.blocks:
            value = (block.block_record.dxf.xref_path
                     if block.block_record.dxf.hasattr("xref_path") else None)
            if value:
                xref_paths.append(value)
    assert xref_paths == []


def test_frozen_real_dxf_project_headers_are_not_person_approvals():
    """PROJECTNAME is project metadata; empty values are not identities."""
    for path in _real_dxfs():
        doc = ezdxf.readfile(path)
        if "$PROJECTNAME" in doc.header:
            assert doc.header["$PROJECTNAME"] in (None, "")
        assert "$LASTSAVEDBY" not in doc.header
