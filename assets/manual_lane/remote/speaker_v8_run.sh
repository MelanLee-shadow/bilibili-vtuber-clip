#!/bin/bash
# OCI3 only.  usage: speaker_v8_run.sh CID CUT_MP4 WORKDIR UNITS_JSON
# Frozen v8 speaker classifier on one clip (TASK-5 item 8).  The v8 code, the CAM++ copy and the classifier are read
# from the frozen v8 workspace; everything written goes under WORKDIR (one per clip, never shared).
set -euo pipefail
CID="$1"; CUT="$2"; W="$3"; UNITS="$4"
# the frozen v8 workspace comes from the caller (profiles/<id>/manual_lane.json speaker_v8, layout.py)
D="${SPEAKER_V8_CODE_DIR:?}"
PY="${SPEAKER_V8_PYTHON:?}"
MODEL="${SPEAKER_V8_MODEL:?}"
SCORE="${SPEAKER_V8_SCORE:?}"
mkdir -p "$W"/in "$W"/audio "$W"/code "$W"/out/final "$W"/model "$W"/cache
rm -f "$W/out/emb_units.npz" "$W/out/pred.json"
cp "$UNITS" "$W/in/units.json"
cp "$D/in/truth_rows.json" "$W/in/truth_rows.json"
cp "$D/out/final/classifier.json" "$W/out/final/classifier.json"
ln -sfn "$MODEL" "$W/model/campp"
for f in oci3_embed.py oci3_feats.py; do sed "s#$D#$W#g" "$D/code/$f" > "$W/code/$f"; done
ffmpeg -hide_banner -loglevel error -y -i "$CUT" -vn -ac 1 -ar 16000 -b:a 64k "$W/audio/$CID.mp3"
cd "$W/code"
# The embed script also embeds calibration sub-windows of the frozen truth rows, which are not for this clip and may
# fail; the unit embeddings are written before that step, so the exit code is not trusted, the file is.
$PY oci3_embed.py > "$W/out/embed.log" 2>&1 || true
if [ ! -s "$W/out/emb_units.npz" ]; then echo SPEAKER_EMBED_FAILED; exit 1; fi
$PY oci3_feats.py > "$W/out/feats.log" 2>&1
$PY "$SCORE" --emb "$W/out/emb_units.npz" --feats "$W/out/feats.npz" --clf "$W/out/final/classifier.json" \
  --out "$W/out/pred.json" > "$W/out/score.log" 2>&1
if [ ! -s "$W/out/pred.json" ]; then echo SPEAKER_SCORE_FAILED; exit 1; fi
echo SPEAKER_V8_DONE
