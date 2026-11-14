#!/usr/bin/env python3
"""tags_remote.py TITLE_FILE SRT OUT_JSON : upload tags through the repo's generate_upload_tags (content layer on CPA).
Run on OCI3 by the root wrapper that loads cpa.env.  The repo is read-only."""
import json
import os
import sys
from pathlib import Path

sys.dont_write_bytecode = True
REPO_ENV = "MANUAL_LANE_REMOTE_REPO"   # the deployed repo, set by the lane (adapters.remote_python)
sys.path.insert(0, os.environ[REPO_ENV])
from scripts.suggest_upload_tags import generate_upload_tags

title = Path(sys.argv[1]).read_text(encoding="utf-8").strip()
out = generate_upload_tags(title, Path(sys.argv[2]))
Path(sys.argv[3]).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
print("status", out.get("status"), "final_tags", len(out.get("final_tags") or []))
