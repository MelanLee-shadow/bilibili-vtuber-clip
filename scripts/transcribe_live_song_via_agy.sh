#!/bin/bash
# Transcribe a live-song capture from scratch with agy on the free host.
# Usage: transcribe_live_song_via_agy.sh <local_media> <out_srt> <out_notes_json>
# Unlike jingting refinement (draft.srt is timing authority), this is a
# from-scratch singing-aware transcription used as selector/repair input.
set -euo pipefail

LOCAL_MEDIA="$1"
OUT_SRT="$2"
OUT_NOTES="$3"
AGY_MODEL="${AGY_MODEL:-Gemini 3.6 Flash (High)}"

STAMP="$(date +%Y%m%d-%H%M%S)"
JOB_DIR="/opt/bilive/livesong_jobs/transcribe-${STAMP}"

# agy is validated on MP4 input; remux locally first
TMP_MP4="$(mktemp -t livesong).mp4"
trap 'rm -f "$TMP_MP4"' EXIT
ffmpeg -hide_banner -loglevel error -y -i "$LOCAL_MEDIA" -c copy "$TMP_MP4" \
  || ffmpeg -hide_banner -loglevel error -y -i "$LOCAL_MEDIA" -vf scale=1280:-2 -c:v libx264 -preset veryfast -crf 28 -c:a aac -b:a 96k "$TMP_MP4"

ssh recording-host "mkdir -p '$JOB_DIR'"
scp -q "$TMP_MP4" "free:$JOB_DIR/input.mp4"

PROMPT_FILE="$(mktemp -t livesongprompt)"
cat > "$PROMPT_FILE" <<EOF
You are transcribing a Bilibili VTuber live-singing capture.

Use only these local files in this job directory:
- input.mp4

Allowed tools:
- view_file on prompt.md and input.mp4
- write_to_file to relative output.srt and relative notes.json
- view_file on output.srt / notes.json only after writing

Forbidden actions:
- No shell, terminal, browser, web, search, or any file outside this job directory.
- Do not write to an absolute path.

Task:
1. Listen to the FULL audio of input.mp4 from 00:00 to the end. Do not stop early.
2. Write output.srt: a complete simplified-Chinese transcription of everything sung and spoken.
   - For singing, transcribe the actual sung lyrics line by line, one lyric line per cue.
   - Cue timestamps must be as accurate as you can hear them (start when the line starts being sung).
   - Cover the whole file; do not leave multi-minute gaps where there is clearly singing or talking.
   - SRT only in that file: index, HH:MM:SS,mmm --> HH:MM:SS,mmm, text. No markdown fences.
3. Write notes.json with exactly these keys:
   {"singing_confirmed": bool,
    "songs": [{"probable_song_title": str, "start": "HH:MM:SS", "end": "HH:MM:SS", "complete": bool}],
    "notes": str}
   - List every distinct song performed, its rough start/end in the video, and whether the
     performance was complete (heard its beginning and its ending) inside this capture.
   - No markdown fences, valid JSON only.
EOF
scp -q "$PROMPT_FILE" "free:$JOB_DIR/prompt.md"
rm -f "$PROMPT_FILE"

ssh recording-host "cd '$JOB_DIR' && agy --sandbox --dangerously-skip-permissions --add-dir '$JOB_DIR' --model '$AGY_MODEL' -p 'Open $JOB_DIR/prompt.md with view_file and follow it exactly. Use only $JOB_DIR/prompt.md, $JOB_DIR/input.mp4, $JOB_DIR/output.srt, $JOB_DIR/notes.json. Do not inspect any other file or directory. Do not use shell or terminal.' --print-timeout ${AGY_TIMEOUT:-30m} > '$JOB_DIR/agy.stdout' 2> '$JOB_DIR/agy.stderr'; echo rc=\$? > '$JOB_DIR/agy.rc'"

ssh recording-host "cat '$JOB_DIR/agy.rc'"
scp -q "free:$JOB_DIR/output.srt" "$OUT_SRT"
scp -q "free:$JOB_DIR/notes.json" "$OUT_NOTES"
[ -s "$OUT_SRT" ] || { echo "agy output.srt is empty (headless auto-deny or model failure); see $JOB_DIR" >&2; exit 3; }
[ -s "$OUT_NOTES" ] || { echo "agy notes.json is empty; see $JOB_DIR" >&2; exit 3; }
echo "job_dir=$JOB_DIR"
