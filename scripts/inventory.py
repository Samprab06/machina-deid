#!/usr/bin/env python3
"""Inventory a private sample without modifying it."""
from __future__ import annotations
import argparse, hashlib, json, mimetypes, os, stat, zipfile
from collections import Counter
from pathlib import Path

ARCHIVE_EXTS={'.zip','.7z','.gz','.tar','.tgz','.bz2','.xz','.z'}

def sha256(path: Path, chunk=1024*1024):
    h=hashlib.sha256()
    with path.open('rb') as f:
        while b:=f.read(chunk): h.update(b)
    return h.hexdigest()

def archive_info(path: Path):
    if path.suffix.lower() != '.zip': return None
    try:
        with zipfile.ZipFile(path) as z:
            return {'members':len(z.infolist()),'encrypted':any(i.flag_bits & 1 for i in z.infolist()),'names':z.namelist()[:20]}
    except Exception as e: return {'error':type(e).__name__}

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('root',type=Path); ap.add_argument('-o','--output',type=Path,required=True); args=ap.parse_args()
    rows=[]; ext=Counter(); total=0
    for p in sorted(args.root.rglob('*')):
        if '.get-input' in p.relative_to(args.root).parts: continue
        if not p.is_file(): continue
        try:
            s=p.stat(); suffix=p.suffix.lower() or '<none>'; total+=s.st_size; ext[suffix]+=1
            row={'path':str(p.relative_to(args.root)),'bytes':s.st_size,'extension':suffix,'mime':mimetypes.guess_type(p.name)[0], 'sha256':sha256(p)}
            if s.st_size==0: row['empty']=True
            if suffix in ARCHIVE_EXTS: row['archive']=archive_info(p)
        except Exception as e: row={'path':str(p.relative_to(args.root)),'error':type(e).__name__}
        rows.append(row)
    out={'root':str(args.root),'files':len(rows),'bytes':total,'extensions':ext,'entries':rows}
    args.output.parent.mkdir(parents=True,exist_ok=True); args.output.write_text(json.dumps(out,indent=2),encoding='utf-8')
    print(json.dumps({'files':len(rows),'bytes':total,'extensions':ext,'output':str(args.output)},default=dict))
if __name__=='__main__': main()
