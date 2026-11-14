#!/bin/bash
# OCI3 only.  usage: speaker_cluster_cos.sh CID CUT_MP4 WORKDIR UNITS_JSON INDEX_JSON
# TASK-11 item 6 (8c1, TASK-14): CAM++ embeddings (the frozen v8 copy, the same embedder v8 uses) of every segment of
# the whole-clip MOSS clusters, then each cluster's centroid against the host's enrolled voiceprint (enroll_lds_1).
# Compute only; everything written goes under WORKDIR.  Prints CLUSTER_COS_DONE and leaves WORKDIR/out/centroids.json.
set -euo pipefail
CID="$1"; CUT="$2"; W="$3"; UNITS="$4"; INDEX="$5"
# the frozen v8 workspace comes from the caller (profiles/<id>/manual_lane.json speaker_v8, layout.py)
D="${SPEAKER_V8_CODE_DIR:?}"
PY="${SPEAKER_V8_PYTHON:?}"
MODEL="${SPEAKER_V8_MODEL:?}"
mkdir -p "$W"/in "$W"/audio "$W"/code "$W"/out "$W"/model "$W"/cache
rm -f "$W/out/emb_units.npz" "$W/out/centroids.json"
cp "$UNITS" "$W/in/units.json"
cp "$INDEX" "$W/index.json"
cp "$D/in/truth_rows.json" "$W/in/truth_rows.json"   # the embed script's own calibration input (not this clip's)
ln -sfn "$MODEL" "$W/model/campp"
sed "s#$D#$W#g" "$D/code/oci3_embed.py" > "$W/code/oci3_embed.py"
ffmpeg -hide_banner -loglevel error -y -i "$CUT" -vn -ac 1 -ar 16000 -b:a 64k "$W/audio/$CID.mp3"
cd "$W/code"
$PY oci3_embed.py > "$W/out/embed.log" 2>&1 || true   # the unit embeddings are written first (see speaker_v8_run.sh)
if [ ! -s "$W/out/emb_units.npz" ]; then echo CLUSTER_EMBED_FAILED; exit 1; fi
$PY - "$W" "$D/out/emb_enroll.npz" <<'PY'
import json, sys
from collections import defaultdict
import numpy as np
w, enroll = sys.argv[1], sys.argv[2]
z = np.load(f"{w}/out/emb_units.npz")
emb, ok, ids = z["emb"], z["ok"], [str(i) for i in z["ids"]]
index = {r["id"]: r for r in json.load(open(f"{w}/index.json"))}
units = {u["id"]: u for u in json.load(open(f"{w}/in/units.json"))["units"]}
enr = np.load(enroll)["enroll_lds_1__full"].astype(np.float64)
enr /= np.linalg.norm(enr)
acc, dur = defaultdict(list), defaultdict(int)
for i, uid in enumerate(ids):
    if uid not in index or not ok[i]:
        continue
    v = emb[i].astype(np.float64)
    v /= np.linalg.norm(v)
    acc[index[uid]["cluster"]].append(v)
    dur[index[uid]["cluster"]] += units[uid]["e"] - units[uid]["s"]
out = {k: {"n": len(vs), "dur_ms": dur[k], "cos": float(np.mean(vs, axis=0) @ enr / np.linalg.norm(np.mean(vs, axis=0)))}
       for k, vs in acc.items()}
json.dump(out, open(f"{w}/out/centroids.json", "w"), ensure_ascii=False, indent=1)
print("clusters", len(out))
PY
echo CLUSTER_COS_DONE
