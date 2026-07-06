#!/bin/bash
# LLM command-transport bridge: {prompt_file} {completion_file}.
# Calls the CPA endpoint using CPA_BASE_URL / CPA_API_KEY from the environment.
# Used by the correction, song-hint and title stages so workflow tests exercise
# the REAL CPA endpoint, mirroring the mandatory real-CPA cover chain.
#
# IMPORTANT (2026-07-04): gpt-5.x are native Responses-API reasoning models.
# Requesting gpt-5.5 on /chat/completions MISROUTES on the CPA proxy (503
# auth_unavailable / empty content); the /responses API routes it correctly and
# gpt-5.5 works.  This mirrors how ~/Project/ASR-orchestra talks to CPA
# (api_mode="responses", reasoning_effort="medium").  NEVER bypass CPA to hit
# the upstream provider directly.
set -euo pipefail

PROMPT_FILE="$1"
COMPLETION_FILE="$2"
MODEL="${CPA_CHAT_MODEL:-gpt-5.5}"
EFFORT="${CPA_REASONING_EFFORT:-medium}"

if [[ -z "${CPA_BASE_URL:-}" || -z "${CPA_API_KEY:-}" ]]; then
  echo "CPA_BASE_URL/CPA_API_KEY missing" >&2
  exit 2
fi

BODY_FILE="$(mktemp)"
RESP_FILE="$(mktemp)"
trap 'rm -f "$BODY_FILE" "$RESP_FILE"' EXIT

python3 - "$PROMPT_FILE" "$MODEL" "$EFFORT" > "$BODY_FILE" <<'PY'
import json, sys
prompt = open(sys.argv[1], encoding="utf-8").read()
print(json.dumps({
    "model": sys.argv[2],
    "input": prompt,
    "reasoning": {"effort": sys.argv[3]},
    "max_output_tokens": 16000,
}, ensure_ascii=False))
PY

# The Responses endpoint intermittently returns a completed response with an
# empty output_text (reasoning model quirk); retry a few times before failing.
attempt=0
while :; do
  attempt=$((attempt + 1))
  # CPA sits behind Cloudflare, which 403s (error 1010) non-browser user agents.
  if curl -sS --fail-with-body --max-time 180 \
      -H "Authorization: Bearer ${CPA_API_KEY}" \
      -H "Content-Type: application/json" \
      -H "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)" \
      -d @"$BODY_FILE" \
      "${CPA_BASE_URL%/}/responses" > "$RESP_FILE" \
     && python3 - "$RESP_FILE" "$COMPLETION_FILE" <<'PY'
import json, sys
payload = json.load(open(sys.argv[1], encoding="utf-8"))
# Responses API: prefer the convenience output_text, else walk output[] for the
# assistant message's output_text parts (skipping reasoning items).
text = payload.get("output_text")
if not text:
    parts = []
    for item in payload.get("output", []) or []:
        if item.get("type") == "message":
            for c in item.get("content", []) or []:
                if c.get("type") in ("output_text", "text") and c.get("text"):
                    parts.append(c["text"])
    text = "".join(parts)
if not isinstance(text, str) or not text.strip():
    raise SystemExit(f"empty completion content (status={payload.get('status')})")
open(sys.argv[2], "w", encoding="utf-8").write(text)
PY
  then
    break
  fi
  if [[ "$attempt" -ge 5 ]]; then
    echo "CPA /responses failed after ${attempt} attempts" >&2
    exit 1
  fi
done
