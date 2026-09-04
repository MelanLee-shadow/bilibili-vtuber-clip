#!/bin/bash
# LLM command-transport bridge: {prompt_file} {completion_file}.
# Builds an OpenAI-compatible chat body locally, pipes it to the `free` host
# over ssh, curls Groq there (GROQ_API_KEY stays in recording-host:/runtime/.env),
# and writes the completion text back locally. Used by cpa_semantic_qa_llm.py
# and the song-hint stage via --llm-command.
set -euo pipefail

PROMPT_FILE="$1"
COMPLETION_FILE="$2"
MODEL="${GROQ_MODEL:-llama-3.3-70b-versatile}"

BODY_FILE="$(mktemp)"
RESP_FILE="$(mktemp)"
trap 'rm -f "$BODY_FILE" "$RESP_FILE"' EXIT

python3 - "$PROMPT_FILE" "$MODEL" > "$BODY_FILE" <<'PY'
import json, sys
prompt = open(sys.argv[1], encoding="utf-8").read()
print(json.dumps({
    "model": sys.argv[2],
    "messages": [{"role": "user", "content": prompt}],
    "temperature": 0.2,
    "max_tokens": 1024,
}, ensure_ascii=False))
PY

ssh -o BatchMode=yes -o ConnectTimeout=10 free '
  KEY=$(grep "^GROQ_API_KEY=" /opt/bilive/.env | cut -d= -f2-)
  curl -sS --max-time 90 https://api.groq.com/openai/v1/chat/completions \
    -H "Authorization: Bearer $KEY" \
    -H "Content-Type: application/json" \
    -d @-' < "$BODY_FILE" > "$RESP_FILE"

python3 - "$RESP_FILE" "$COMPLETION_FILE" <<'PY'
import json, sys
payload = json.load(open(sys.argv[1], encoding="utf-8"))
if "error" in payload:
    sys.stderr.write(f"groq error: {payload['error']}\n")
    raise SystemExit(4)
content = payload["choices"][0]["message"]["content"]
if not isinstance(content, str) or not content.strip():
    sys.stderr.write("empty completion\n")
    raise SystemExit(4)
open(sys.argv[2], "w", encoding="utf-8").write(content)
PY
