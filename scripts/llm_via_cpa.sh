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

# 2026-08-10 事故：上游 ChatGPT OAuth 三把凭据同时 usage_limit_reached，流量落到
# 次级 leg，同一分钟里 200/400/408/503 混着来（实测 sol 成功率掉到 ~80%）。终审
# 面按 attempts_per_model=1 调用，于是 sol→5.5→5.4 三枪打空就把整条候选判死，
# 一次 15–55 分钟的 produce 全废、runner 再无限重试、重试又继续烧配额。
#
# 因此把「空补全重试」和「服务类故障重试」拆开：
#  - CPA_TRANSIENT_ATTEMPTS_PER_MODEL（默认 3）只对**服务类**失败生效
#    （408/429/5xx、curl 连接/超时类退出码、空补全），即使 attempts_per_model=1
#    也照常退避重试——调用方要的是"一个模型一次逻辑尝试"，不是"抖一下就放弃"。
#    2026-08-10 补：空补全（推理模型 output_text 为空的已知怪癖）此前被单独一
#    条分支按 attempts_per_model 封顶，于是**恰恰是 attempts_per_model=1 的终审
#    面**（boundary/auditor/pronoun/裁决 共十条腿全走它）一次空补全就换模型、
#    三枪打空判死。空补全本来就在 transient_failure() 的服务类里，那条特例分支
#    只是把 floor 抹掉了——已删除。
#  - 其它 4xx 是确定性拒绝（分组无权、请求非法），同模型重试只会浪费时间，
#    立即换下一个模型。
# 退避是指数 + 抖动，总睡眠有上限，保证三个模型跑完仍远小于调用方的 600s 超时。
TRANSIENT_ATTEMPTS_PER_MODEL="${CPA_TRANSIENT_ATTEMPTS_PER_MODEL:-3}"
BACKOFF_BASE_SECONDS="${CPA_BACKOFF_BASE_SECONDS:-2}"
BACKOFF_MAX_TOTAL_SECONDS="${CPA_BACKOFF_MAX_TOTAL_SECONDS:-30}"
# Hard wall-clock stop for the whole chain.  attempts_per_model=1 used to be the
# only thing keeping the final-review stage inside its 600s caller timeout; the
# transient floor above would break that guarantee on its own, so the deadline
# now carries it explicitly.  It gates **every** dispatch after the first (not
# just same-model retries), so no request can start past the deadline and the
# worst case is 400s + one in-flight curl (--max-time 180) = 580s < 600s.
# The first attempt always runs, so a deadline can never make this a no-op.
# Set to 0 to disable.
DEADLINE_SECONDS="${CPA_DEADLINE_SECONDS:-400}"

if ! [[ "$ATTEMPTS_PER_MODEL" =~ ^[1-9][0-9]*$ ]]; then
  echo "attempts_per_model must be a positive integer" >&2
  exit 2
fi

if ! [[ "$TRANSIENT_ATTEMPTS_PER_MODEL" =~ ^[1-9][0-9]*$ ]]; then
  echo "CPA_TRANSIENT_ATTEMPTS_PER_MODEL must be a positive integer" >&2
  exit 2
fi

if ! [[ "$BACKOFF_BASE_SECONDS" =~ ^[0-9]+$ ]]; then
  echo "CPA_BACKOFF_BASE_SECONDS must be a non-negative integer" >&2
  exit 2
fi

if ! [[ "$BACKOFF_MAX_TOTAL_SECONDS" =~ ^[0-9]+$ ]]; then
  echo "CPA_BACKOFF_MAX_TOTAL_SECONDS must be a non-negative integer" >&2
  exit 2
fi

if ! [[ "$DEADLINE_SECONDS" =~ ^[0-9]+$ ]]; then
  echo "CPA_DEADLINE_SECONDS must be a non-negative integer" >&2
  exit 2
fi

SLEPT_TOTAL=0
TOTAL_ATTEMPTS=0
DEADLINE_HIT=0
SECONDS=0

deadline_reached() {
  [[ "$DEADLINE_SECONDS" -gt 0 && "$SECONDS" -ge "$DEADLINE_SECONDS" ]]
}

# Service-class failures are worth waiting out; everything else is not.
transient_failure() {
  local http_code="$1" curl_exit="$2" empty_completion="$3"
  if [[ "$empty_completion" == "1" ]]; then
    return 0
  fi
  case "$curl_exit" in
    # 7 connect refused, 28 operation timeout, 35 TLS connect, 52 empty reply,
    # 55/56 send/recv failure — all transport-level, all worth a retry.
    7|28|35|52|55|56) return 0 ;;
  esac
  case "$http_code" in
    408|429|5??) return 0 ;;
    4??) return 1 ;;
  esac
  # No usable status (transport died before a response, or curl too old to
  # report one): unclassifiable, so take the conservative branch and retry.
  # Fail-closing a whole candidate on an unreadable failure is the expensive
  # mistake; one extra request is the cheap one.
  return 0
}

backoff_sleep() {
  local attempt="$1"
  local delay=$((BACKOFF_BASE_SECONDS << (attempt - 1)))
  if [[ "$delay" -gt 0 ]]; then
    # Jitter keeps concurrent producers from re-colliding on the same second.
    delay=$((delay + RANDOM % (delay + 1)))
  fi
  local remaining=$((BACKOFF_MAX_TOTAL_SECONDS - SLEPT_TOTAL))
  if [[ "$remaining" -le 0 ]]; then
    return 1
  fi
  if [[ "$delay" -gt "$remaining" ]]; then
    delay="$remaining"
  fi
  SLEPT_TOTAL=$((SLEPT_TOTAL + delay))
  if [[ "$delay" -gt 0 ]]; then
    sleep "$delay"
  fi
  return 0
}

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
if [[ "$DEADLINE_HIT" == "1" ]]; then
  break
fi
build_body "$MODEL"
attempt=0
while :; do
  if [[ "$TOTAL_ATTEMPTS" -gt 0 ]] && deadline_reached; then
    echo "[cpa] deadline ${DEADLINE_SECONDS}s reached before model=${MODEL}; giving up" >&2
    DEADLINE_HIT=1
    break
  fi
  attempt=$((attempt + 1))
  TOTAL_ATTEMPTS=$((TOTAL_ATTEMPTS + 1))
  HTTP_CODE=000
  CURL_EXIT=0
  EMPTY_COMPLETION=0
  # CPA sits behind Cloudflare, which 403s (error 1010) non-browser user agents.
  HTTP_CODE="$(curl -sS --fail-with-body --max-time 180 \
      -o "$RESP_FILE" -w '%{http_code}' \
      -H @"$HEADER_FILE" \
      -d @"$BODY_FILE" \
      "${CPA_BASE_URL%/}/responses")" && CURL_EXIT=0 || CURL_EXIT=$?
  if [[ "$CURL_EXIT" -eq 0 ]]; then
    if python3 - "$RESP_FILE" "$COMPLETION_FILE" <<'PY'
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
      echo "[cpa] model=${MODEL} attempt=${attempt} http=${HTTP_CODE} curl_exit=0 result=ok" >&2
      exit 0
    fi
    EMPTY_COMPLETION=1
  fi
  echo "[cpa] model=${MODEL} attempt=${attempt} http=${HTTP_CODE} curl_exit=${CURL_EXIT} empty_completion=${EMPTY_COMPLETION} result=failed" >&2
  if transient_failure "$HTTP_CODE" "$CURL_EXIT" "$EMPTY_COMPLETION"; then
    max_attempts="$ATTEMPTS_PER_MODEL"
    if [[ "$TRANSIENT_ATTEMPTS_PER_MODEL" -gt "$max_attempts" ]]; then
      max_attempts="$TRANSIENT_ATTEMPTS_PER_MODEL"
    fi
  else
    # Deterministic rejection (bad request, unauthorized, group without the
    # requested capability): the same request will be refused again.  Fail over
    # immediately instead of burning the model's attempt budget.
    echo "[cpa] model=${MODEL} deterministic http=${HTTP_CODE}, no same-model retry" >&2
    max_attempts=1
  fi
  if [[ "$attempt" -ge "$max_attempts" ]]; then
    echo "CPA /responses failed ${attempt}x on ${MODEL}, trying next model" >&2
    break
  fi
  if ! backoff_sleep "$attempt"; then
    echo "[cpa] model=${MODEL} backoff budget exhausted, trying next model" >&2
    echo "CPA /responses failed ${attempt}x on ${MODEL}, trying next model" >&2
    break
  fi
done
done
echo "CPA /responses failed on all models: $MODELS" >&2
exit 1
