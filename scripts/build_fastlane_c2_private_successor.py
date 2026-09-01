#!/usr/bin/env python3
"""Build C2's sealed, no-upload subtitle/title successor in a private directory."""
import argparse, hashlib, json, re, shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CID = "auto_203011_328_389"
AUTH = ROOT / "assets/lidousha/fastlane_c2_private/auto_203011_328_389.correction-authority.v1.json"
SRT = re.compile(r"(\d+)\n(\d\d):(\d\d):(\d\d),(\d\d\d) --> (\d\d):(\d\d):(\d\d),(\d\d\d)\n(.*?)(?=\n\n|\Z)", re.S)
def digest(p): return hashlib.sha256(p.read_bytes()).hexdigest()
def ms(m, o): return ((int(m[o])*60+int(m[o+1]))*60+int(m[o+2]))*1000+int(m[o+3])
def load():
    a=json.loads(AUTH.read_text()); assert a["candidate_id"]==CID and a["upload_allowed"] is False; return a
def project(srt, a):
    rows=list(SRT.finditer(srt.strip())); assert len(rows)==22
    change=a["exact_subtitle_changes"][0]; out=[]; seen21=False
    for n,m in enumerate(rows,1):
        start,end=ms(m,2),ms(m,6); text=m.group(10)
        if n==change["cue"]:
            assert (start,end,text)==(change["start_ms"],change["end_ms"],change["before"]); text=change["after"]
        if n==21:
            c=a["chat_context_freeze"]; assert (start,end,text)==(c["start_ms"],c["end_ms"],c["release_text"]); seen21=True
        out.append(f"{n}\n{m.group(2)}:{m.group(3)}:{m.group(4)},{m.group(5)} --> {m.group(6)}:{m.group(7)}:{m.group(8)},{m.group(9)}\n{text}")
    assert seen21
    return "\n\n".join(out)+"\n"
def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--predecessor-dir",type=Path,required=True); ap.add_argument("--out",type=Path,required=True); x=ap.parse_args(); a=load(); o=x.out.resolve(); p=x.predecessor_dir.resolve()
    if o.exists(): raise SystemExit("refusing to overwrite private output")
    ins={"srt":p/f"{CID}.recut.srt","video":p/f"{CID}.recut.mp4"}
    if not all(v.is_file() for v in ins.values()) or digest(ins["srt"])!=a["predecessor"]["srt_sha256"] or digest(ins["video"])!=a["predecessor"]["video_sha256"]: raise SystemExit("predecessor hash drift")
    o.mkdir(parents=True); out=o/f"{CID}.recut.srt"; out.write_text(project(ins["srt"].read_text(),a)); shutil.copy2(ins["video"],o/f"{CID}.recut.mp4")
    r={"schema_version":"fastlane-c2-private-successor.v1","candidate_id":CID,"upload_allowed":False,"title":a["identity"]["title"],"cover_lines":a["identity"]["cover_lines"],"artifacts":{q.name:digest(q) for q in o.iterdir()}}
    (o/"fastlane-c2-private-successor.v1.json").write_text(json.dumps(r,ensure_ascii=False,indent=2)+"\n"); print(json.dumps(r,ensure_ascii=False)); return 0
if __name__=="__main__": raise SystemExit(main())
