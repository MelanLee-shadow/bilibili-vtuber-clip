#!/bin/bash
# Transcribe a live-talk capture from scratch with agy on the free host.
# Usage: transcribe_live_talk_via_agy.sh <local_media> <out_srt> <out_notes_json>
# Talk variant of transcribe_live_song_via_agy.sh: cue text quality matters for
# the talk selector (setup/punchline/closure markers), so the prompt asks for
# faithful colloquial Chinese with accurate per-utterance timing.
set -euo pipefail

LOCAL_MEDIA="$1"
OUT_SRT="$2"
OUT_NOTES="$3"
AGY_MODEL="${AGY_MODEL:-Gemini 3.6 Flash (High)}"

STAMP="$(date +%Y%m%d-%H%M%S)"
JOB_DIR="/opt/bilive/livetalk_jobs/transcribe-${STAMP}"

TMP_MP4="$(mktemp -t livetalk).mp4"
trap 'rm -f "$TMP_MP4"' EXIT
ffmpeg -hide_banner -loglevel error -y -i "$LOCAL_MEDIA" -c copy "$TMP_MP4" \
  || ffmpeg -hide_banner -loglevel error -y -i "$LOCAL_MEDIA" -vf scale=1280:-2 -c:v libx264 -preset veryfast -crf 28 -c:a aac -b:a 96k "$TMP_MP4"

ssh recording-host "mkdir -p '$JOB_DIR'"
scp -q "$TMP_MP4" "free:$JOB_DIR/input.mp4"

PROMPT_FILE="$(mktemp -t livetalkprompt)"
cat > "$PROMPT_FILE" <<EOF
You are transcribing a Bilibili VTuber live TALK stream capture (杂谈).

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
2. Write output.srt: a complete simplified-Chinese transcription of everything the streamer says.
   - One utterance/sentence per cue; keep the streamer's colloquial wording, particles, and tone words faithfully (口语、语气词都保留).
   - Cue timestamps must be as accurate as you can hear them.
   - Cover the whole file; do not leave multi-minute gaps where there is clearly talking.
   - When the streamer reads out a danmaku/viewer comment before reacting, transcribe that too.
   - SRT only in that file: index, HH:MM:SS,mmm --> HH:MM:SS,mmm, text. No markdown fences.
3. Write notes.json with exactly these keys:
   {"talking_confirmed": bool,
    "funny_moments": [{"start": "HH:MM:SS", "end": "HH:MM:SS", "summary": str, "why_funny": str}],
    "topics": [str],
    "notes": str}
   - funny_moments: list EVERY segment a viewer would find funny/clip-worthy (jokes, roasts,
     overreactions, embarrassing stories, danmaku battles, sudden mood swings). Use the actual
     audio, not guesses. Include the setup start and the punchline/reaction end.
   - No markdown fences, valid JSON only.
EOF
scp -q "$PROMPT_FILE" "free:$JOB_DIR/prompt.md"
rm -f "$PROMPT_FILE"

ssh recording-host "cd '$JOB_DIR' && agy --sandbox --dangerously-skip-permissions --add-dir '$JOB_DIR' --model '$AGY_MODEL' -p 'Open $JOB_DIR/prompt.md with view_file and follow it exactly. Use only $JOB_DIR/prompt.md, $JOB_DIR/input.mp4, $JOB_DIR/output.srt, $JOB_DIR/notes.json. Do not inspect any other file or directory. Do not use shell or terminal.' --print-timeout ${AGY_TIMEOUT:-40m} > '$JOB_DIR/agy.stdout' 2> '$JOB_DIR/agy.stderr'; echo rc=\$? > '$JOB_DIR/agy.rc'"

ssh recording-host "cat '$JOB_DIR/agy.rc'"
scp -q "free:$JOB_DIR/output.srt" "$OUT_SRT"
scp -q "free:$JOB_DIR/notes.json" "$OUT_NOTES"
[ -s "$OUT_SRT" ] || { echo "agy output.srt is empty (headless auto-deny or model failure); see $JOB_DIR" >&2; exit 3; }
[ -s "$OUT_NOTES" ] || { echo "agy notes.json is empty; see $JOB_DIR" >&2; exit 3; }
echo "job_dir=$JOB_DIR"
