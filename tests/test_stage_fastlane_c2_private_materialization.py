import shutil, subprocess, sys
from pathlib import Path
def run(*args): return subprocess.run([sys.executable,'scripts/stage_fastlane_c2_private_materialization.py',*map(str,args)],capture_output=True,text=True)
def test_stage_rejects_wrong_sealed_stem(tmp_path):
 q=tmp_path/'bad.srt'; q.write_text('x'); r=run('--predecessor-dir',tmp_path,'--sealed-srt',q,'--out',tmp_path/'o'); assert r.returncode and 'wrong-stem-or-sealed-SRT-drift' in r.stderr
def test_stage_rejects_missing_or_drifted_predecessor(tmp_path):
 q=tmp_path/'auto_203011_328_389.recut.srt'; shutil.copy2('assets/lidousha/fastlane_c2_private/auto_203011_328_389.reviewed.srt',q); r=run('--predecessor-dir',tmp_path,'--sealed-srt',q,'--out',tmp_path/'o'); assert r.returncode and 'video-drift' in r.stderr
