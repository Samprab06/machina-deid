#!/usr/bin/env python3
"""Build and run a bounded, real multi-format Phase 3 benchmark."""
from __future__ import annotations
import hashlib, json, os, shutil
import tempfile
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from machina_deid import process
from machina_deid.discovery import discover_pdf, discover_step, discover_dxf, report

BASE = Path("/srv/new2/machina-deid/work/phase3-benchmark").resolve()
SRC, OUT, WORK = BASE / "input", BASE / "output", BASE / "state"

def sha(p):
    h=hashlib.sha256()
    with p.open('rb') as f:
        while b:=f.read(1<<20): h.update(b)
    return h.hexdigest()

def stage(src, rel):
    dst=SRC/rel; dst.parent.mkdir(parents=True, exist_ok=True)
    if not dst.exists():
        try:
            os.link(src, dst)
        except OSError as exc:
            if getattr(exc, "errno", None) != 18:
                raise
            shutil.copyfile(src, dst)
        if sha(dst) != sha(src):
            dst.unlink()
            raise RuntimeError(f"staged source checksum mismatch: {rel}")
    return dst

def main():
    global BASE, SRC, OUT, WORK
    # Resolve samples only when executing, so importing this module is safe.
    PDF = next(Path("/srv/new2/machina-deid/representative-pdf").rglob("*.PDF"))
    DXF = next(Path("/srv/new2/machina-deid/representative-dxf").rglob("*.dxf"))
    STEP = next(Path("/home/opc/machina-deid/input").rglob("*.STEP"))
    NATIVE = next(Path("/home/opc/machina-deid/input").rglob("*.SLDPRT"))
    # Fresh private runs preserve previous evidence and protect raw maps and copies.
    BASE.parent.mkdir(parents=True, exist_ok=True)
    BASE = Path(tempfile.mkdtemp(prefix=BASE.name + "-", dir=BASE.parent))
    SRC, OUT, WORK = BASE / "input", BASE / "output", BASE / "state"
    SRC.mkdir()
    files=[(PDF,Path('pdf/sample.PDF')),(DXF,Path('dxf/sample.dxf')),(STEP,Path('step/sample.STEP')),(NATIVE,Path('native/sample.SLDPRT'))]
    for s,r in files: stage(s,r)
    rows=[]
    for s,r in files: rows.append(f"{r}\t{s.stat().st_size}\t{hashlib.md5(s.read_bytes()).hexdigest()}\n")
    manifest=BASE/'manifest.tsv'; manifest.write_text(''.join(rows),encoding='utf-8')
    pdf_c=discover_pdf(PDF); step_c=discover_step(STEP,include_software=True)
    pdf_map={c.value:f"Deidentified-{i+1:03d}" for i,c in enumerate(pdf_c) if c.confidence in ('high','medium')}
    step_map={c.value:f"Deidentified-STEP-{i+1:03d}" for i,c in enumerate(step_c) if c.confidence=='high' or c.kind=='file_name'}
    (BASE/'pdf-map.json').write_text(json.dumps({'strings':pdf_map},indent=2),encoding='utf-8')
    (BASE/'step-map.json').write_text(json.dumps({'strings':step_map},indent=2),encoding='utf-8')
    before={str(r):sha(s) for s,r in files}
    result=process.process(manifest,SRC,OUT,WORK,limit=4,max_bytes=2*1024**3,pdf_map=str(BASE/'pdf-map.json'),step_map=str(BASE/'step-map.json'),min_free=15*1024**3)
    after={str(r):sha(s) for s,r in files}
    result['source_unchanged']=before==after
    salt = os.urandom(32)  # ephemeral; never persist a key that permits name guessing
    result['discovery']={'pdf':report(pdf_c, salt=salt),'step':report(step_c, salt=salt),'dxf':report(discover_dxf(DXF), salt=salt)}
    result['maps_generated_automatically']={'pdf_fields':len(pdf_map),'step_fields':len(step_map)}
    result['inputs']=[{'path':str(r),'format':s.suffix.lower(),'bytes':s.stat().st_size,'source_sha256':before[str(r)]} for s,r in files]
    result['output_bytes']=sum(p.stat().st_size for p in OUT.rglob('*') if p.is_file())
    (BASE/'benchmark-report.json').write_text(json.dumps(result,indent=2,sort_keys=True),encoding='utf-8')
    print(json.dumps(result,indent=2,sort_keys=True))
if __name__=='__main__': main()
