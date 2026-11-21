"""Synthetic production workspaces (no real data). Shared by tests and scripts/production_throughput.py."""
from __future__ import annotations

import hashlib
import io
import re
import subprocess
import zipfile
from pathlib import Path

import ezdxf
import pymupdf

from step_helpers import step_bytes

PERSON = "Jane Quincy Doe"
ORG = "Acme Widgets Inc"
SEED = b"synthetic-seed-0123456789abcdef!"


def pdf_bytes(author=PERSON, body="Bracket drawing"):
    doc = pymupdf.open()
    doc.new_page(width=300, height=400).insert_text((50, 100), body, fontsize=12)
    doc.set_metadata({"author": author, "title": "Bracket"})
    out = doc.tobytes()
    doc.close()
    return out


def dxf_bytes(company=ORG):
    doc = ezdxf.new("R2010")
    doc.modelspace().add_line((0, 0), (100.25, 50.5))
    doc.modelspace().add_text(f"Drawn by {company}", height=2)
    buf = io.StringIO()
    doc.write(buf)
    return buf.getvalue().encode()


def zip_bytes(members):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in members:
            z.writestr(zipfile.ZipInfo(name, (2024, 1, 1, 0, 0, 0)), data)
    return buf.getvalue()


def file_set(n: int) -> dict[str, bytes]:
    """``n`` groups of mixed formats; every 4th person/org varies so several identities exist."""
    files: dict[str, bytes] = {}
    for i in range(n):
        who, org = f"{PERSON} {i % 4}", f"{ORG} {i % 3}"
        d = f"proj{i % 5}/part{i}"
        files[f"{d}/drawing.pdf"] = pdf_bytes(author=who, body=f"Sheet {i}")
        files[f"{d}/model.step"] = step_bytes(name=f"bracket {i}", author=f"('{who}')", org=f"('{org}')")
        files[f"{d}/layout.dxf"] = dxf_bytes(org)
        files[f"{d}/bundle.zip"] = zip_bytes([("doc/a.pdf", pdf_bytes(author=who)), ("notes.txt", b"plain")])
        files[f"{d}/notes.bin"] = bytes(range(256)) * (i + 1)
        files[f"{d}/broken.pdf"] = b"%PDF-1.7\nnot really a pdf"
    files["empty.txt"] = b""
    return files


def make_workspace(root: Path, files: dict[str, bytes], *, present=None, seed: bytes | None = SEED,
                   md5=True) -> str:
    """Create ``root/{input,repo,ops,attempts}`` with a manifest; returns the repo HEAD commit."""
    inp = root / "input"
    (inp / ".get-input").mkdir(parents=True)
    lines = ["# path\tbytes\tmd5\turl"]
    for rel, data in files.items():
        lines.append(f"{rel}\t{len(data)}\t{hashlib.md5(data).hexdigest() if md5 else ''}\t/x")
        if present is None or rel in present:
            write_input(root, rel, data)
    (inp / ".get-input" / "manifest.tsv").write_text("\n".join(lines) + "\n")
    (root / "ops" / "attempt-1").mkdir(parents=True)
    if seed is not None:
        (root / "ops" / "attempt-1" / "seed").write_bytes(seed)
    repo = root / "repo"
    repo.mkdir()
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    subprocess.run(git[:3] + ["init", "-q"], check=True)
    (repo / "README").write_text("synthetic\n")
    subprocess.run(git + ["add", "README"], check=True)
    subprocess.run(git + ["commit", "-q", "-m", "x"], check=True)
    return subprocess.check_output(git[:3] + ["rev-parse", "HEAD"], text=True).strip()


def write_input(root: Path, rel: str, data: bytes) -> None:
    p = root / "input" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


_VOLATILE_DXF = re.compile(rb"\{[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}\}"  # $VERSIONGUID
                           rb"|^\d{7}\.\d+\r?$"  # $TDUPDATE / $TDUUPDATE julian dates
                           rb"|@ \d{4}-\d\d-\d\dT[0-9:.+]+", re.M)  # ezdxf write marker


def stable_bytes(rel: str, data: bytes) -> bytes:
    """ezdxf stamps save time and a fresh GUID on every write (serial runs too); mask only those."""
    return _VOLATILE_DXF.sub(b"<volatile>", data) if rel.endswith(".dxf") else data


def tree_digest(base: Path) -> dict[str, str]:
    out = {}
    for p in sorted(base.rglob("*")):
        if p.is_file():
            rel = p.relative_to(base).as_posix()
            out[rel] = hashlib.sha256(stable_bytes(rel, p.read_bytes())).hexdigest()
    return out
