#!/bin/bash
# LLM command-transport bridge: {prompt_file} {completion_file} [models] [effort].
# Calls the CPA endpoint using CPA_BASE_URL / CPA_API_KEY from the environment.
# Used by the correction, song-hint and title stages so workflow tests exercise
# the REAL CPA endpoint, mirroring the mandatory real-CPA cover chain.
#
# Optional argv 3/4 (2026-07-10, Ivan): pin a per-stage model chain + reasoning
# effort at the call site (callers shlex-split the template, so a quoted chain
# stays one argument).  Precedence: explicit arg > CPA_CHAT_MODELS env > default.
# Stage assignment lives at the call sites: gpt-5.6-sol for deep/open-ended work
# (semantic recall, correction adjudication, titles, boundary review),
# gpt-5.6-terra for structured picks (cover art direction).  gpt-5.6-luna is
# enabled on CPA since 2026-08-02 and owns high-volume structured judgment
# (closed-set entity picks, read-aloud arbitration) at effort=max — A/B'd
# consistent with sol on those shapes (P1/P4), while boundary-style deep
# semantics stays sol (A/B P2: sol matched the live-approved anchor).
#
# IMPORTANT (2026-07-04): gpt-5.x are native Responses-API reasoning models.
# Requesting gpt-5.5 on /chat/completions MISROUTES on the CPA proxy (503
# auth_unavailable / empty content); the /responses API routes it correctly and
# gpt-5.5 works.  This mirrors how ~/Project/ASR-orchestra talks to CPA
# (api_mode="responses", reasoning_effort="medium").  NEVER bypass CPA to hit
# the upstream provider directly.
set -euo pipefail
set +x
umask 077

PROMPT_FILE="$1"
COMPLETION_FILE="$2"
# Model failover (Ivan-approved order, 2026-07-10): gpt-5.6-sol first, then
# gpt-5.5 (the Ivan-required fallback), then gpt-5.4 (auth_unavailable 503
# happens routinely while codex-pro is rate-limited).  mini/compact are NOT
# acceptable fallbacks (Ivan 2026-07-04).
MODELS="${3:-${CPA_CHAT_MODELS:-${CPA_CHAT_MODEL:-gpt-5.6-sol gpt-5.5 gpt-5.4}}}"
EFFORT="${4:-${CPA_REASONING_EFFORT:-medium}}"
ATTEMPTS_PER_MODEL="${5:-3}"

if ! [[ "$ATTEMPTS_PER_MODEL" =~ ^[1-9][0-9]*$ ]]; then
  echo "attempts_per_model must be a positive integer" >&2
  exit 2
fi

if [[ -z "${CPA_BASE_URL:-}" || -z "${CPA_API_KEY:-}" ]]; then
  echo "CPA_BASE_URL/CPA_API_KEY missing" >&2
  exit 2
fi

BODY_FILE=""
RESP_FILE=""
HEADER_FILE=""
cleanup() {
  local path
  for path in "$BODY_FILE" "$RESP_FILE" "$HEADER_FILE"; do
    [[ -z "$path" ]] || rm -f -- "$path" || true
  done
}
trap cleanup EXIT

BODY_FILE="$(mktemp)"
RESP_FILE="$(mktemp)"
HEADER_FILE="$(mktemp)"

# Keep the bearer out of curl's argv (and out of every later child process).
# printf is a bash builtin, so expanding the key here does not create another
# process whose arguments can be inspected.  curl reads all request headers
# from the mode-0600 temporary file instead.
{
  builtin printf 'Authorization: Bearer %s\n' "$CPA_API_KEY"
  builtin printf 'Content-Type: application/json\n'
  builtin printf 'User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)\n'
} > "$HEADER_FILE"
unset CPA_API_KEY

build_body() {
python3 - "$PROMPT_FILE" "$1" "$EFFORT" > "$BODY_FILE" <<'PY'
import json, sys
prompt = open(sys.argv[1], encoding="utf-8").read()
print(json.dumps({
    "model": sys.argv[2],
    "input": prompt,
    "reasoning": {"effort": sys.argv[3]},
    "max_output_tokens": 16000,
}, ensure_ascii=False))
PY
}

# The Responses endpoint intermittently returns a completed response with an
# empty output_text (reasoning model quirk); retry a few times per model, then
# fail over to the next model in MODELS.
for MODEL in $MODELS; do
build_body "$MODEL"
attempt=0
while :; do
  attempt=$((attempt + 1))
  # CPA sits behind Cloudflare, which 403s (error 1010) non-browser user agents.
  if curl -sS --fail-with-body --max-time 180 \
      -H @"$HEADER_FILE" \
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
    exit 0
  fi
  if [[ "$attempt" -ge "$ATTEMPTS_PER_MODEL" ]]; then
    echo "CPA /responses failed ${attempt}x on ${MODEL}, trying next model" >&2
    break
  fi
done
done
echo "CPA /responses failed on all models: $MODELS" >&2
exit 1
