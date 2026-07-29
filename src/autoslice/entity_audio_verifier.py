"""Independent raw-audio forced choice for confusable subtitle entities.

The ordinary ASR/AGY/CPA transcript may already have seen nearby chat.  It is
therefore ineligible to arbitrate a conflict between that chat and a proper
name.  This module renders a short audio-only (black-frame) clip and asks a
sandboxed AGY High job to choose among canonical entities from the syllables.
Every result is hash-bound; uncertainty fails closed in the caller.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from scripts.gemini_slice_jingting import (
    agy_subprocess_env,
    parse_timeout_seconds,
    strip_markdown_fence,
    timely_terms_context,
)
from src.autoslice import gemini_backup_policy
from src.autoslice.llm_client import extract_json_object


ENTITY_AUDIO_MODEL = "Gemini 3.6 Flash (High)"
ENTITY_AUDIO_TIMEOUT = "10m"

# Gemini API 直连兜底（Ivan 2026-07-14：付费 API key 当然能裁决音频——AGY
# 订阅配额断供不得阻塞实体裁决）。同一验收逻辑、同一 prompt 语义，仅载体
# 不同：免费 3 key 永远先试，付费走 gemini_backup_policy 门（strike/例外/
# 帽/入帐），key 只存在于内存 header。
GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
ENTITY_AUDIO_API_MODEL_ENV = "ENTITY_AUDIO_GEMINI_API_MODEL"
ENTITY_AUDIO_API_MODEL_DEFAULT = "gemini-3.6-flash"
# 免费层按模型独立计 RPD（Ivan 2026-07-27：每 key 每模型 20 RPD，轮换
# 3.6/3.5 = 40 RPD/key）。仅在同 key 主模型报 429 时换备用模型再试同 key；
# 付费层始终只用主模型。听写证人是无声调拼音任务，7/21 金丝雀里 3.5 的
# 乱种问题在实名/词表场景，不适用此处；模型串照常钉进 verdict/manifest。
ENTITY_AUDIO_API_MODEL_FALLBACK_ENV = "ENTITY_AUDIO_GEMINI_API_MODEL_FALLBACK"
ENTITY_AUDIO_API_MODEL_FALLBACK_DEFAULT = "gemini-3.5-flash"
ENTITY_AUDIO_API_REQUEST_MAX_BYTES = 20_000_000

# Phase 1 acoustic-witness architecture (Ivan 2026-07-25 ruling): the audio
# model is a WITNESS, not a judge. In witness mode it never sees any
# candidate text — it dictates suspected pinyin syllables only; hanzi
# word-choice reasoning belongs to the CPA judge downstream.
WITNESS_REQUEST_SCHEMA = "subtitle-span-acoustic-witness-request.v1"
WITNESS_SCHEMA = "subtitle-span-acoustic-witness.v1"
# The prompt contract is part of the acoustic-cache identity.  A 2026-07-29
# production incident proved why: the old prompt embedded one valid pinyin
# example and AGY copied it verbatim for unrelated audio.  Audio bytes alone
# are not a sufficient cache key when the dictation instructions change.
WITNESS_PROMPT_CONTRACT = "candidate-free-toneless-pinyin-no-example.v2"
_LEGACY_PROMPT_COPY_PINYIN = "zhe ge shi he tian yi de lian dong o"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _uncertain(request: Mapping[str, Any], reason: str, detail: str = "") -> dict[str, Any]:
    if request.get("schema_version") == WITNESS_REQUEST_SCHEMA:
        return {
            "schema_version": WITNESS_SCHEMA,
            "request_sha256": request.get("request_sha256"),
            "status": "UNCERTAIN",
            "reason_code": reason,
            **({"detail": detail[-500:]} if detail else {}),
        }
    if request.get("schema_version") == "subtitle-span-acoustic-check-request.v1":
        return {
            "schema_version": "subtitle-span-acoustic-check-verdict.v1",
            "request_sha256": request.get("request_sha256"),
            "status": "UNCERTAIN",
            "reason_code": reason,
            **({"detail": detail[-500:]} if detail else {}),
        }
    return {
        "schema_version": "chat-entity-verdict.v1",
        "request_sha256": request.get("request_sha256"),
        "status": "UNCERTAIN",
        "reason_code": reason,
        **({"detail": detail[-500:]} if detail else {}),
    }


def _witness_prompt(
    *,
    recording_date: str,
    delivery_mode: str,
    target_audio_start_ms: int | None,
    target_audio_end_ms: int | None,
) -> str:
    """Pure-dictation witness prompt. Deliberately shows NO candidate text,
    no adjacent transcript, and no glossary — anything textual would prime
    the dictation. The witness reports suspected pinyin only."""

    if delivery_mode == "gemini_api":
        source_line = (
            "Use only the attached audio clip. There is no viewer chat, subtitle,\n"
            "title card, or other visual text to consult."
        )
        output_head = "Reply with exactly one JSON object (no markdown fences, no other text):"
        output_tail = ""
    else:
        source_line = (
            "Use only `input.mp4` in this job directory. Its frames are deliberately black:\n"
            "there is no viewer chat, subtitle, title card, or other visual text to copy."
        )
        output_head = "Write `verdict.json` as JSON only:"
        output_tail = (
            "No markdown fences, no other files, no shell, terminal, browser, web, or search."
        )
    return f"""# Raw-audio dictation witness (Mandarin livestream)

{source_line}
You are a dictation witness, not a judge. Listen to the target interval
several times and report ONLY what the syllables sound like, as toneless
Hanyu Pinyin. Do not guess words, do not normalize to plausible phrases,
do not output any Chinese characters anywhere.

Recording date: {recording_date}
The target interval occupies {target_audio_start_ms if target_audio_start_ms is not None else "unknown"} ms
through {target_audio_end_ms if target_audio_end_ms is not None else "unknown"} ms in the attached clip.
Audio before/after the target is context for speech-rate and speaker only —
never merge its syllables into the target report.

{output_head}
{{
  "schema_version": "subtitle-span-acoustic-witness.v1",
  "status": "OBSERVED" or "UNCERTAIN",
  "target_audible": true or false,
  "heard_pinyin": "<lowercase toneless pinyin syllables separated by spaces>",
  "uncertain_positions": [0-based indexes of syllables you are unsure about],
  "syllable_count": <integer, length of heard_pinyin>,
  "confidence": 0.0,
  "reason": "short acoustic note (English or pinyin only, no Chinese characters)"
}}

Rules:
- heard_pinyin must contain ONLY lowercase pinyin syllables separated by
  single spaces. If a stretch is unintelligible, write "?" for that syllable
  and list its index in uncertain_positions.
- Use OBSERVED when the target interval is audible enough to attempt a
  dictation, even a partial one. Use UNCERTAIN only when the target interval
  itself cannot be assessed at all.
- Never output Chinese characters, candidate words, or any field not listed.
{output_tail}
"""


def _prompt(
    *,
    candidates: list[dict[str, Any]],
    recording_date: str,
    timely_context: str,
    sentence_mode: bool = False,
    delivery_mode: str = "agy",
    context_before: str = "",
    context_after: str = "",
    target_audio_start_ms: int | None = None,
    target_audio_end_ms: int | None = None,
    acoustic_fit_mode: bool = False,
) -> str:
    neutral_candidates = sorted(candidates, key=lambda row: str(row.get("canonical") or "").lower())
    if delivery_mode == "gemini_api":
        source_line = (
            "Use only the attached audio clip. There is no viewer chat, subtitle,\n"
            "title card, or other visual text to consult."
        )
        output_head = "Reply with exactly one JSON object (no markdown fences, no other text):"
        output_tail = (
            "Use RESOLVED only when one candidate is acoustically clear with confidence at\n"
            "least 0.80. Otherwise use UNCERTAIN."
        )
    else:
        source_line = (
            "Use only `input.mp4` in this job directory. Its frames are deliberately black:\n"
            "there is no viewer chat, subtitle, title card, or other visual text to copy."
        )
        output_head = "Write `verdict.json` as JSON only:"
        output_tail = (
            "Use RESOLVED only when one candidate is acoustically clear with confidence at\n"
            "least 0.80. Otherwise use UNCERTAIN. No markdown fences, no other files, no\n"
            "shell, terminal, browser, web, or search."
        )
    if sentence_mode:
        if acoustic_fit_mode:
            return f"""# Raw-audio subtitle-span acoustic compatibility check

{source_line}
Listen to the target cue several times. The two candidate sentences were already
constructed by code. Do not choose by meaning and do not rewrite either sentence.
Report only how well each candidate fits the literal target audio. Adjacent audio
and text establish continuity, but another occurrence outside the target offsets
is never evidence that the phrase occurred inside the target.

Recording date: {recording_date}
Candidate sentences (closed set; IDs are immutable):
{json.dumps(neutral_candidates, ensure_ascii=False, indent=2, sort_keys=True)}

Adjacent spoken lines (context only, never text authority):
- before: {(context_before or "（无）")!s}
- after: {(context_after or "（无）")!s}
The target cue occupies {target_audio_start_ms if target_audio_start_ms is not None else "unknown"} ms
through {target_audio_end_ms if target_audio_end_ms is not None else "unknown"} ms in the attached clip.

Fit labels:
- SUPPORTED: the target audio positively supports this candidate;
- PLAUSIBLE: compatible with the target audio but not uniquely clear;
- INCOMPATIBLE: literal target syllables contradict this candidate;
- UNRESOLVED: audio is too weak to judge.
Semantic plausibility must never turn acoustically incompatible syllables into
SUPPORTED or PLAUSIBLE. Report the syllables actually heard before assigning fits.

{output_head}
{{
  "schema_version": "entity-audio-observation.v1",
  "status": "OBSERVED" or "UNCERTAIN",
  "target_audible": true or false,
  "heard_syllables": "literal syllables/phonetic observation",
  "current_fit": "SUPPORTED|PLAUSIBLE|INCOMPATIBLE|UNRESOLVED",
  "proposed_fit": "SUPPORTED|PLAUSIBLE|INCOMPATIBLE|UNRESOLVED",
  "confidence_current": 0.0,
  "confidence_proposed": 0.0,
  "reason": "short acoustic explanation"
}}

Use OBSERVED when the target interval is audible enough to assign all fields,
even when both candidates are only PLAUSIBLE or UNRESOLVED. Use UNCERTAIN when
the target interval itself cannot be assessed. Do not apply a semantic winner
and do not emit candidate_id, canonical_entity, or any rewritten text. No
markdown fences or other output.
"""
        return f"""# Raw-audio spoken-sentence forced choice

{source_line}
Listen to the complete audio several times and decide which candidate sentence
is actually spoken (the host may be reading viewer chat aloud). Do not infer
the answer from which sentence would make more sense.

Recording date: {recording_date}
Candidate sentences (neutral list):
{json.dumps(neutral_candidates, ensure_ascii=False, indent=2, sort_keys=True)}

{timely_context or 'No active date-bounded timely-term snapshot.'}

Adjacent spoken lines (discourse frame, transcribed from the same audio; they
are context, not text authority):
- before: {(context_before or "（无）")!s}
- after: {(context_after or "（无）")!s}
The target cue occupies {target_audio_start_ms if target_audio_start_ms is not None else "unknown"} ms
through {target_audio_end_ms if target_audio_end_ms is not None else "unknown"} ms in the attached clip.
When the syllables are genuinely ambiguous between candidates, discourse fit
with the adjacent lines MAY break the tie. Clearly incompatible syllables
still always lose, regardless of discourse fit.
Report the syllables you actually hear before the choice.

{output_head}
{{
  "schema_version": "entity-audio-observation.v1",
  "status": "RESOLVED" or "UNCERTAIN",
  "canonical_entity": "one exact candidate sentence above, verbatim, or null",
  "heard_syllables": "literal syllables/phonetic observation",
  "confidence": 0.0,
  "reason": "short acoustic explanation"
}}

{output_tail}
"""
    return f"""# Raw-audio proper-name forced choice

{source_line}
Listen to the complete audio several times and decide which candidate name is
actually spoken. Do not infer the answer from what would make sense.

Recording date: {recording_date}
Candidate entities (neutral list; aliases/readings are spelling aids only):
{json.dumps(neutral_candidates, ensure_ascii=False, indent=2, sort_keys=True)}

{timely_context or 'No active date-bounded timely-term snapshot.'}

Recency and franchise context may change prior probability, but incompatible
syllables always win. In particular, a current title and an older title can both
be valid candidates. Report the syllables you actually hear before the choice.

{output_head}
{{
  "schema_version": "entity-audio-observation.v1",
  "status": "RESOLVED" or "UNCERTAIN",
  "canonical_entity": "one exact canonical above, or null",
  "heard_syllables": "literal syllables/phonetic observation",
  "confidence": 0.0,
  "reason": "short acoustic explanation"
}}

{output_tail}
"""


def _configured_free_keys() -> list[str]:
    """Return up to three distinct configured free keys, values never logged."""

    return list(
        dict.fromkeys(
            value
            for name in ("GEMINI_API_KEY", "GEMINI_API_KEY_2", "GEMINI_API_KEY_3")
            if (value := os.environ.get(name))
        )
    )


def _classify_agy_failure(returncode: int, stdout: str, stderr: str) -> str:
    diagnostic = f"{stdout}\n{stderr}".casefold()
    if any(marker in diagnostic for marker in ("quota", "429", "rate limit", "too many requests")):
        return "AGY_QUOTA_EXHAUSTED"
    if any(marker in diagnostic for marker in ("timeout", "timed out")):
        return "AGY_TIMEOUT"
    return f"AGY_FAILED_RC_{returncode}"


def _api_failure_category(exc: Exception) -> str:
    status = getattr(exc, "code", None)
    if status == 429:
        return "GEMINI_API_QUOTA_EXHAUSTED"
    if status in {401, 403}:
        return "GEMINI_API_AUTH_FAILED"
    if isinstance(exc, (TimeoutError, subprocess.TimeoutExpired)):
        return "GEMINI_API_TIMEOUT"
    if isinstance(exc, (json.JSONDecodeError, ValueError)):
        return "GEMINI_API_INVALID_OUTPUT"
    return "GEMINI_API_REQUEST_FAILED"


def _entity_api_model() -> str:
    return os.environ.get(ENTITY_AUDIO_API_MODEL_ENV) or ENTITY_AUDIO_API_MODEL_DEFAULT


def _entity_api_model_fallback() -> str | None:
    """Free-tier quota-rotation sibling model; empty env disables rotation."""

    raw = os.environ.get(ENTITY_AUDIO_API_MODEL_FALLBACK_ENV)
    if raw is not None and not raw.strip():
        return None
    return (raw or ENTITY_AUDIO_API_MODEL_FALLBACK_DEFAULT).strip() or None


def _gemini_api_observe_entity(*, audio_path: Path, prompt: str, key: str, model: str) -> str:
    """One Gemini API generateContent call with the cropped clip audio inline.

    The key exists only in the in-memory ``x-goog-api-key`` header; callers
    persist only bounded structural failure categories.
    """

    audio_b64 = base64.b64encode(audio_path.read_bytes()).decode("ascii")
    body = {
        "contents": [
            {
                "parts": [
                    {"text": prompt},
                    {"inline_data": {"mime_type": "audio/mpeg", "data": audio_b64}},
                ]
            }
        ],
        "generationConfig": {
            "temperature": 0.1,
            # 65536 = gemini-3.5-flash 文档上限(思考 token 计入输出上限，
            # 2026-07-14 梦限大案：8192 被长思考吃光正文为空)。
            "maxOutputTokens": 65_536,
            "responseMimeType": "application/json",
            # 几个候选名的强制二选一不需要深思(Ivan 2026-07-14)；Gemini 3.x
            # 用 thinking_level 控深度(minimal/low/medium/high)。
            "thinkingConfig": {
                "thinkingLevel": os.environ.get("ENTITY_GEMINI_THINKING_LEVEL", "low")
            },
        },
    }

    def _post(request_body: dict) -> dict:
        request_bytes = json.dumps(request_body, ensure_ascii=False).encode("utf-8")
        if len(request_bytes) > ENTITY_AUDIO_API_REQUEST_MAX_BYTES:
            raise RuntimeError("GEMINI_API_REQUEST_TOO_LARGE")
        request = urllib.request.Request(
            GEMINI_API_URL.format(model=urllib.parse.quote(model, safe="")),
            data=request_bytes,
            headers={"content-type": "application/json", "x-goog-api-key": key},
        )
        try:
            timeout_seconds = int(os.environ.get("ENTITY_GEMINI_API_TIMEOUT_SECONDS", "180"))
        except ValueError:
            timeout_seconds = 180
        timeout_seconds = min(600, max(30, timeout_seconds))
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            return json.load(response)

    try:
        payload = _post(body)
    except urllib.error.HTTPError as exc:
        # 字段兼容保险：thinkingConfig 若被该 API 版本拒绝(400)，去掉后同 key
        # 重试一次——绝不让一个可选字段烧掉整条 key 链。
        if exc.code == 400 and "thinkingConfig" in body.get("generationConfig", {}):
            degraded = json.loads(json.dumps(body))
            degraded["generationConfig"].pop("thinkingConfig", None)
            payload = _post(degraded)
        else:
            raise
    candidates = payload.get("candidates") if isinstance(payload, dict) else None
    candidate = candidates[0] if isinstance(candidates, list) and candidates else None
    content = candidate.get("content") if isinstance(candidate, dict) else None
    parts = content.get("parts") if isinstance(content, dict) else None
    if not isinstance(parts, list):
        return ""
    return strip_markdown_fence(
        "".join(str(part.get("text") or "") for part in parts if isinstance(part, dict))
    )


@dataclass(frozen=True)
class _EntityProviderOutcome:
    observed: Any
    provider: str
    model: str
    prompt_path: Path
    response_path: Path
    accepted_key_tier: str | None
    paid_policy_stamp: Mapping[str, Any] | None
    provider_failures: list[dict[str, Any]]


_ACOUSTIC_CACHE_SCHEMA = "witness-acoustic-cache.v2"


def _witness_acoustic_cache_path(output_dir: Path, clip_sha256: str) -> Path:
    """Global content-addressed cache entry for one witness audio clip.

    成本裁定（Ivan 2026-07-27，3 天 $40 案）：付费声学证人 88% 的消耗来自
    重产轮次对**同一段音频**的重复听写——witness 请求按设计不携带候选
    （纯听写），答案只由音频决定，request_sha 里的文本漂移不改变问题本身。
    键=音频片 sha256，但 entry schema 与 prompt contract 也必须精确匹配；
    这样纯文本候选漂移仍然免调用，听写指令升级则自动淘汰旧结果。
    BASE 从候选包目录上溯（out/<date>/<cid> → BASE），主树与 V15
    恢复树各自命中自己的缓存。
    """

    base = output_dir.parents[2] if len(output_dir.parents) >= 3 else output_dir
    return (
        base
        / "cache"
        / "witness-acoustic"
        / clip_sha256[:2]
        / f"{clip_sha256}.json"
    )


def _serve_witness_acoustic_cache(
    *,
    output_dir: Path,
    clip_sha256: str,
    job_dir: Path,
) -> _EntityProviderOutcome | None:
    """Return a synthetic provider outcome from the acoustic cache, or None.

    只回放 OBSERVED 的原始听写；prompt/response 工件拷贝进当前 job_dir，
    下游照常重算全部 sha 与报告校验——缓存只省 provider 调用，不省验证。
    """

    entry_path = _witness_acoustic_cache_path(output_dir, clip_sha256)
    try:
        entry = json.loads(entry_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    observed = entry.get("observed")
    if (
        entry.get("schema_version") != _ACOUSTIC_CACHE_SCHEMA
        or entry.get("prompt_contract") != WITNESS_PROMPT_CONTRACT
        or entry.get("audio_clip_sha256") != clip_sha256
        or not isinstance(observed, dict)
        or observed.get("status") != "OBSERVED"
    ):
        return None
    prompt_src = entry_path.with_suffix(".prompt.json")
    response_src = entry_path.with_suffix(".response.json")
    if not prompt_src.is_file() or not response_src.is_file():
        return None
    prompt_path = job_dir / "prompt.acoustic-cache.json"
    response_path = job_dir / "response.acoustic-cache.json"
    try:
        prompt_path.write_bytes(prompt_src.read_bytes())
        response_path.write_bytes(response_src.read_bytes())
    except OSError:
        return None
    return _EntityProviderOutcome(
        observed=dict(observed),
        provider=str(entry.get("provider") or "acoustic_cache"),
        model=str(entry.get("model") or ""),
        prompt_path=prompt_path,
        response_path=response_path,
        accepted_key_tier=entry.get("key_tier") or None,
        paid_policy_stamp=None,
        provider_failures=[],
    )


def _store_witness_acoustic_cache(
    *,
    output_dir: Path,
    clip_sha256: str,
    observed: Mapping[str, Any],
    outcome: _EntityProviderOutcome,
) -> None:
    """Best-effort write-through; cache absence must never fail production."""

    try:
        entry_path = _witness_acoustic_cache_path(output_dir, clip_sha256)
        entry_path.parent.mkdir(parents=True, exist_ok=True)
        entry_path.with_suffix(".prompt.json").write_bytes(
            outcome.prompt_path.read_bytes()
        )
        entry_path.with_suffix(".response.json").write_bytes(
            outcome.response_path.read_bytes()
        )
        entry_path.write_text(
            json.dumps(
                {
                    "schema_version": _ACOUSTIC_CACHE_SCHEMA,
                    "prompt_contract": WITNESS_PROMPT_CONTRACT,
                    "audio_clip_sha256": clip_sha256,
                    "observed": dict(observed),
                    "provider": outcome.provider,
                    "model": outcome.model,
                    "key_tier": outcome.accepted_key_tier,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
    except OSError:
        pass


def _observe_entity_audio(
    *,
    request: Mapping[str, Any],
    candidates: list[Any],
    audio_path: Path,
    job_dir: Path,
    recording_date: str,
    timely_context: str,
    binary: str,
    model: str,
    timeout: str,
) -> _EntityProviderOutcome:
    """Run the AGY provider, then the policy-gated Gemini API fallback."""

    candidate_rows = [dict(row) for row in candidates if isinstance(row, dict)]
    witness_mode = request.get("schema_version") == WITNESS_REQUEST_SCHEMA
    sentence_mode = request.get("schema_version") in {
        "chat-read-aloud-verification-request.v1",
        "subtitle-span-acoustic-check-request.v1",
    }
    acoustic_fit_mode = (
        request.get("schema_version") == "subtitle-span-acoustic-check-request.v1"
    )
    if witness_mode:
        prompt = _witness_prompt(
            recording_date=recording_date,
            delivery_mode="agy",
            target_audio_start_ms=request.get("target_audio_start_ms"),
            target_audio_end_ms=request.get("target_audio_end_ms"),
        )
    else:
        prompt = _prompt(
            candidates=candidate_rows,
            recording_date=recording_date,
            timely_context=timely_context,
            sentence_mode=sentence_mode,
            context_before=str(request.get("context_before") or ""),
            context_after=str(request.get("context_after") or ""),
            target_audio_start_ms=request.get("target_audio_start_ms"),
            target_audio_end_ms=request.get("target_audio_end_ms"),
            acoustic_fit_mode=acoustic_fit_mode,
        )
    prompt_path = job_dir / "prompt.md"
    prompt_path.write_text(prompt, encoding="utf-8")
    short_prompt = (
        "Open prompt.md with view_file and follow it exactly. Use only prompt.md and input.mp4. "
        "Write verdict.json in this directory. Do not use shell, terminal, browser, or web."
    )
    observed: Any = None
    provider = "agy"
    model_used = model
    accepted_key_tier: str | None = None
    paid_policy_stamp: Mapping[str, Any] | None = None
    provider_failures: list[dict[str, Any]] = []
    response_path = job_dir / "verdict.raw.json"
    try:
        completed = subprocess.run(
            [
                binary,
                "--sandbox",
        "--dangerously-skip-permissions",
                "--add-dir",
                str(job_dir),
                "--model",
                model,
                "-p",
                short_prompt,
                "--print-timeout",
                timeout,
            ],
            cwd=job_dir,
            env=agy_subprocess_env(),
            check=False,
            capture_output=True,
            text=True,
            timeout=parse_timeout_seconds(timeout) + 120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        provider_failures.append(
            {
                "provider": "agy",
                "category": "AGY_SUBPROCESS_ERROR",
                "error_type": type(exc).__name__,
            }
        )
    else:
        (job_dir / "agy.stdout").write_text(completed.stdout, encoding="utf-8")
        (job_dir / "agy.stderr").write_text(completed.stderr, encoding="utf-8")
        verdict_path = job_dir / "verdict.json"
        raw_response = (
            verdict_path.read_text(encoding="utf-8", errors="replace")
            if verdict_path.is_file()
            else completed.stdout
        )
        response_path.write_text(raw_response, encoding="utf-8")
        if completed.returncode != 0:
            provider_failures.append(
                {
                    "provider": "agy",
                    "category": _classify_agy_failure(
                        completed.returncode, completed.stdout, completed.stderr
                    ),
                }
            )
        else:
            try:
                observed = json.loads(strip_markdown_fence(raw_response))
            except (TypeError, ValueError) as exc:
                provider_failures.append(
                    {
                        "provider": "agy",
                        "category": "AGY_INVALID_OUTPUT",
                        "error_type": type(exc).__name__,
                    }
                )

    if observed is None:
        provider = "gemini_api"
        model_used = _entity_api_model()
        api_audio_path = job_dir / "input.gemini-api.mp3"
        api_prompt_path = job_dir / "prompt.gemini-api.md"
        api_response_path = job_dir / "verdict.gemini-api.raw.json"
        extract = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(audio_path), "-vn", "-ac", "1", "-ar", "16000",
                "-b:a", "64k", str(api_audio_path),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=180,
        )
        if extract.returncode != 0 or not api_audio_path.is_file():
            provider_failures.append(
                {"provider": "gemini_api", "category": "GEMINI_API_AUDIO_EXTRACTION_FAILED"}
            )
        else:
            if witness_mode:
                api_prompt = _witness_prompt(
                    recording_date=recording_date,
                    delivery_mode="gemini_api",
                    target_audio_start_ms=request.get("target_audio_start_ms"),
                    target_audio_end_ms=request.get("target_audio_end_ms"),
                )
            else:
                api_prompt = _prompt(
                    candidates=candidate_rows,
                    recording_date=recording_date,
                    timely_context=timely_context,
                    sentence_mode=sentence_mode,
                    context_before=str(request.get("context_before") or ""),
                    context_after=str(request.get("context_after") or ""),
                    target_audio_start_ms=request.get("target_audio_start_ms"),
                    target_audio_end_ms=request.get("target_audio_end_ms"),
                    acoustic_fit_mode=acoustic_fit_mode,
                    delivery_mode="gemini_api",
                )
            api_prompt_path.write_text(api_prompt, encoding="utf-8")

            def attempt_api_key(
                attempt_key: str,
                *,
                key_tier: str,
                attempt_model: str | None = None,
            ) -> bool:
                nonlocal observed, accepted_key_tier, model_used
                attempt_model = attempt_model or model_used
                try:
                    raw = _gemini_api_observe_entity(
                        audio_path=api_audio_path,
                        prompt=api_prompt,
                        key=attempt_key,
                        model=attempt_model,
                    )
                    # Persist the raw provider response for bounded forensic evidence.
                    api_response_path.write_text(
                        (raw or "") if (raw or "").endswith("\n") else (raw or "") + "\n",
                        encoding="utf-8",
                    )
                    if not raw or len(raw.encode("utf-8")) > 2_000_000:
                        raise ValueError("empty or oversized Gemini API output")
                    try:
                        observed = extract_json_object(raw)
                    except Exception as exc:
                        raise ValueError("Gemini API output had no valid JSON object") from exc
                    accepted_key_tier = key_tier
                    model_used = attempt_model
                    return True
                except Exception as exc:
                    provider_failures.append(
                        {
                            "provider": "gemini_api",
                            "key_tier": key_tier,
                            "model": attempt_model,
                            "category": _api_failure_category(exc),
                            "error_type": type(exc).__name__,
                        }
                    )
                    return False

            item_key = _sha256(api_audio_path)
            # 免费链轮次：纯额度类失败（429 快败）在同一次运行内连续补足
            # 「同项失败≥3轮」的政策线（quota_exhausted_round docstring 记有
            # 2026-07-18 交付事故根因）；非额度失败保持单轮。轮数有界——
            # ledger 不可写的环境 strikes 永远读 0，绝不允许无界循环。
            round_start = len(provider_failures)
            fallback_model = _entity_api_model_fallback()
            primary_model = model_used
            for key in _configured_free_keys():
                if attempt_api_key(
                    key,
                    key_tier=gemini_backup_policy.FREE_KEY_TIER,
                    attempt_model=primary_model,
                ):
                    break
                # 同 key 换模型只在主模型 429 时进行：RPD 按模型独立计，
                # 换模型才有新配额；其他错误换模型不产生新信息。
                last = provider_failures[-1] if provider_failures else {}
                if (
                    fallback_model
                    and fallback_model != primary_model
                    and last.get("category") == "GEMINI_API_QUOTA_EXHAUSTED"
                    and attempt_api_key(
                        key,
                        key_tier=gemini_backup_policy.FREE_KEY_TIER,
                        attempt_model=fallback_model,
                    )
                ):
                    break
            quota_fastpath = False
            if observed is None:
                gemini_backup_policy.record_free_chain_failure(item_key)
                round_categories = [
                    failure.get("category")
                    for failure in provider_failures[round_start:]
                    if failure.get("provider") == "gemini_api"
                ]
                # Ivan 2026-07-20（取代 7/19 同 run 连补 3 轮的过渡机制）：
                # 纯 429 配额轮=确定性耗尽证据，付费当轮直接顶上；
                # >=3 strikes 门只管非配额类失败。
                quota_fastpath = gemini_backup_policy.quota_exhausted_round(
                    round_categories
                )
            if observed is None:
                if quota_fastpath:
                    allowed, gate_reason = gemini_backup_policy.paid_attempt_allowed(
                        item_key,
                        prior_strikes=gemini_backup_policy.MIN_FREE_CHAIN_STRIKES,
                    )
                    gate_reason = f"QUOTA_FASTPATH:{gate_reason}"
                else:
                    allowed, gate_reason = gemini_backup_policy.paid_attempt_allowed(item_key)
                if allowed and attempt_api_key(
                    str(gemini_backup_policy.paid_backup_key()),
                    key_tier=gemini_backup_policy.PAID_KEY_TIER,
                ):
                    paid_policy_stamp = gemini_backup_policy.record_paid_use(
                        item_key, purpose="entity_audio_verdict"
                    )
                elif not allowed and gate_reason != "PAID_KEY_NOT_CONFIGURED":
                    provider_failures.append(
                        {
                            "provider": "gemini_api",
                            "key_tier": gemini_backup_policy.PAID_KEY_TIER,
                            "category": f"PAID_BACKUP_SKIPPED:{gate_reason}",
                        }
                    )
        if observed is not None:
            response_path = api_response_path
            prompt_path = api_prompt_path

    return _EntityProviderOutcome(
        observed=observed,
        provider=provider,
        model=model_used,
        prompt_path=prompt_path,
        response_path=response_path,
        accepted_key_tier=accepted_key_tier,
        paid_policy_stamp=paid_policy_stamp,
        provider_failures=provider_failures,
    )


_PINYIN_SYLLABLE_RX = re.compile(r"^(?:[a-zü]+|\?)$")
_CJK_RX = re.compile(r"[㐀-鿿]")


def _subtitle_acoustic_witness_verdict(
    *,
    request: Mapping[str, Any],
    request_sha: str,
    observed: Mapping[str, Any],
    outcome: "_EntityProviderOutcome",
    source_sha256: str,
    audio_path: Path,
    start_ms: int,
    end_ms: int,
    timeline_binding: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate a pure-dictation witness report; any hanzi or candidate
    leakage invalidates it (the witness must never do word choice)."""

    heard = str(observed.get("heard_pinyin") or "").strip().lower()
    tokens = heard.split()
    confidence = observed.get("confidence")
    uncertain_positions = observed.get("uncertain_positions")
    syllable_count = observed.get("syllable_count")
    # 静音证词（2026-07-27 1160 咳咳案）：删除提案的时窗里确实无语音时，
    # 空听写 + target_audible=False 是完整有效的观察，不是报告缺陷——
    # 强制非空会把「确认无声」翻译成 WITNESS_REPORT_INVALID→UNCERTAIN，
    # 删除类发现永久卡死。有声报告仍必须逐音节过拼音正则。
    silence_observation = (
        not tokens and observed.get("target_audible") is False
    )
    # Reject the exact demonstration phrase that contaminated the old prompt.
    # This guard also makes copied v1 artifacts fail closed even if an operator
    # accidentally moves one outside the versioned cache contract.
    if heard == _LEGACY_PROMPT_COPY_PINYIN:
        return _uncertain(
            request,
            "WITNESS_PROMPT_COPY_DETECTED",
            "legacy demonstration phrase was copied instead of dictated",
        )
    report_valid = (
        observed.get("schema_version") == WITNESS_SCHEMA
        and observed.get("status") == "OBSERVED"
        and isinstance(observed.get("target_audible"), bool)
        and (bool(tokens) or silence_observation)
        and all(_PINYIN_SYLLABLE_RX.fullmatch(token) for token in tokens)
        and not _CJK_RX.search(json.dumps(dict(observed), ensure_ascii=False))
        and not any(
            key in observed
            for key in (
                "candidate_id",
                "canonical_entity",
                "proposed_cue",
                "rewritten_text",
                "current_fit",
                "proposed_fit",
            )
        )
        and not isinstance(confidence, bool)
        and isinstance(confidence, (int, float))
        and 0.0 <= float(confidence) <= 1.0
        and isinstance(uncertain_positions, list)
        and all(
            not isinstance(v, bool) and isinstance(v, int) and 0 <= v < len(tokens)
            for v in uncertain_positions
        )
        and not isinstance(syllable_count, bool)
        and isinstance(syllable_count, int)
        and (syllable_count > 0 or silence_observation)
    )
    # The witness's substance is heard_pinyin itself; syllable_count is a
    # redundant self-count that models routinely get off by one (2026-07-26:
    # four clean supporting witnesses on 1209_1410 were all invalidated by
    # this arithmetic). The recount below is authoritative; a mismatch is
    # disclosed, never fatal.
    self_count_mismatch = report_valid and syllable_count != len(tokens)
    # Physical plausibility backstop: Mandarin peaks near ~9 syllables/s.
    # A rate far above that means the dictation overflowed the target span
    # (2026-07-26: 1.12s target, 14 syllables) — poisoned evidence, retriable.
    try:
        target_span_s = max(
            0.001,
            (int(request["matched_end_ms"]) - int(request["matched_start_ms"]))
            / 1000.0,
        )
    except (KeyError, TypeError, ValueError):
        target_span_s = None
    if (
        report_valid
        and target_span_s is not None
        and (len(tokens) - 2) / target_span_s > 9.0
    ):
        return _uncertain(
            request,
            "WITNESS_IMPLAUSIBLE_SYLLABLE_RATE",
            f"{len(tokens)} syllables over {target_span_s:.2f}s target",
        )
    if not report_valid:
        return _uncertain(
            request,
            "WITNESS_REPORT_INVALID",
            str(observed.get("reason") or ""),
        )
    return {
        "schema_version": WITNESS_SCHEMA,
        "request_sha256": request_sha,
        "status": "OBSERVED",
        "target_audible": bool(observed["target_audible"]),
        "heard_pinyin": " ".join(tokens),
        "uncertain_positions": [int(v) for v in uncertain_positions],
        "syllable_count": len(tokens),
        "self_count_mismatch": self_count_mismatch,
        "confidence": float(confidence),
        "reason": str(observed.get("reason") or "")[:300],
        "source_media_sha256": source_sha256,
        "audio_clip_sha256": _sha256(audio_path),
        "prompt_sha256": _sha256(outcome.prompt_path),
        "response_sha256": _sha256(outcome.response_path),
        "model": outcome.model,
        "provider": outcome.provider,
        **({"key_tier": outcome.accepted_key_tier} if outcome.accepted_key_tier else {}),
        "audio_start_ms": start_ms,
        "audio_end_ms": end_ms,
        "timeline_binding": dict(timeline_binding),
    }


def _subtitle_acoustic_verdict(
    *,
    request: Mapping[str, Any],
    request_sha: str,
    observed: Mapping[str, Any],
    outcome: _EntityProviderOutcome,
    source_sha256: str,
    audio_path: Path,
    start_ms: int,
    end_ms: int,
    timeline_binding: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate a text-free acoustic-fit report and attach replay evidence."""

    fit_values = {"SUPPORTED", "PLAUSIBLE", "INCOMPATIBLE", "UNRESOLVED"}
    heard = str(observed.get("heard_syllables") or "").strip()
    current_fit = str(observed.get("current_fit") or "")
    proposed_fit = str(observed.get("proposed_fit") or "")
    confidence_current = observed.get("confidence_current")
    confidence_proposed = observed.get("confidence_proposed")
    report_valid = (
        observed.get("schema_version") == "entity-audio-observation.v1"
        and observed.get("status") == "OBSERVED"
        and isinstance(observed.get("target_audible"), bool)
        and current_fit in fit_values
        and proposed_fit in fit_values
        and bool(heard)
        and not any(
            key in observed
            for key in ("candidate_id", "canonical_entity", "proposed_cue", "rewritten_text")
        )
        and not isinstance(confidence_current, bool)
        and isinstance(confidence_current, (int, float))
        and 0.0 <= float(confidence_current) <= 1.0
        and not isinstance(confidence_proposed, bool)
        and isinstance(confidence_proposed, (int, float))
        and 0.0 <= float(confidence_proposed) <= 1.0
    )
    if not report_valid:
        return _uncertain(
            request,
            "ENTITY_AUDIO_UNCERTAIN",
            str(observed.get("reason") or ""),
        )
    return {
        "schema_version": "subtitle-span-acoustic-check-verdict.v1",
        "request_sha256": request_sha,
        "status": "OBSERVED",
        "target_audible": bool(observed["target_audible"]),
        "heard_syllables": heard,
        "current_fit": current_fit,
        "proposed_fit": proposed_fit,
        "confidence_current": float(confidence_current),
        "confidence_proposed": float(confidence_proposed),
        "reason": str(observed.get("reason") or ""),
        "source_media_sha256": source_sha256,
        "audio_clip_sha256": _sha256(audio_path),
        "prompt_sha256": _sha256(outcome.prompt_path),
        "response_sha256": _sha256(outcome.response_path),
        "model": outcome.model,
        "provider": outcome.provider,
        **({"key_tier": outcome.accepted_key_tier} if outcome.accepted_key_tier else {}),
        "audio_start_ms": start_ms,
        "audio_end_ms": end_ms,
        "timeline_binding": dict(timeline_binding),
    }


@dataclass(frozen=True)
class _PreparedAudioSpan:
    target_start_ms: int
    target_end_ms: int
    crop_start_ms: int
    crop_end_ms: int
    observed_request: dict[str, Any]
    timeline_binding: dict[str, Any]


@dataclass(frozen=True)
class _LocalAudioVerifier:
    source_media: Path
    output_dir: Path
    recording_date: str
    source_duration_ms: int
    source_sha256: str
    binary: str
    model: str
    timeout: str
    timely_context: str

    def __call__(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        return _verify_local_audio_request(verifier=self, request=request)


def _prepare_audio_span(
    *,
    request: Mapping[str, Any],
    context_mode: bool,
    source_media_timeline_offset_ms: int,
    source_duration_ms: int,
    witness_mode: bool = False,
) -> _PreparedAudioSpan | None:
    try:
        delivery_target_start_ms = int(request["matched_start_ms"])
        delivery_target_end_ms = int(request["matched_end_ms"])
        if context_mode:
            delivery_context_start_ms = int(request["context_start_ms"])
            delivery_context_end_ms = int(request["context_end_ms"])
            target_start_ms = (
                delivery_target_start_ms + source_media_timeline_offset_ms
            )
            target_end_ms = delivery_target_end_ms + source_media_timeline_offset_ms
            source_context_start_ms = (
                delivery_context_start_ms + source_media_timeline_offset_ms
            )
            source_context_end_ms = (
                delivery_context_end_ms + source_media_timeline_offset_ms
            )
            if witness_mode:
                # The dictation witness hears ONLY the target (±400ms onset/
                # offset pad). Feeding it the whole context window makes it
                # transcribe past the markers (2026-07-26: 1.12s target, 14
                # heard syllables) and poisons the judge. Context stays a
                # text-side input to the judge, never witness audio.
                # 裁剪边界量化到 100ms 网格（Ivan 2026-07-27 成本追问）：
                # cue 边界几十毫秒的轮间漂移会让裁剪字节不同、声学缓存
                # 失配重付费。松垫本就 400ms，±50ms 的格点吸附无实质影响，
                # 换来漂移轮的缓存命中。向外取整：起点下取、终点上取。
                crop_start_ms = max(0, (target_start_ms - 400) // 100 * 100)
                crop_end_ms = min(
                    source_duration_ms,
                    -((target_end_ms + 400) // -100) * 100,
                )
            else:
                crop_start_ms = max(
                    0, min(target_start_ms, source_context_start_ms)
                )
                crop_end_ms = min(
                    source_duration_ms,
                    max(target_end_ms, source_context_end_ms),
                )
        else:
            target_start_ms = delivery_target_start_ms
            target_end_ms = delivery_target_end_ms
            crop_start_ms = max(0, target_start_ms - 1_500)
            crop_end_ms = min(source_duration_ms, target_end_ms + 1_500)
    except (KeyError, TypeError, ValueError):
        return None
    if (
        target_end_ms <= target_start_ms
        or crop_end_ms <= crop_start_ms
        or crop_end_ms - crop_start_ms > 30_000
        or target_start_ms < crop_start_ms
        or target_end_ms > crop_end_ms
    ):
        return None
    observed_request = dict(request)
    observed_request["target_audio_start_ms"] = target_start_ms - crop_start_ms
    observed_request["target_audio_end_ms"] = target_end_ms - crop_start_ms
    timeline_binding: dict[str, Any] = {}
    if context_mode:
        timeline_binding = {
            "schema_version": "subtitle-audio-timeline-binding.v1",
            "source_media_timeline_offset_ms": source_media_timeline_offset_ms,
            "delivery_local": {
                "target_start_ms": delivery_target_start_ms,
                "target_end_ms": delivery_target_end_ms,
                "context_start_ms": delivery_context_start_ms,
                "context_end_ms": delivery_context_end_ms,
            },
            "source_media": {
                "target_start_ms": target_start_ms,
                "target_end_ms": target_end_ms,
                "crop_start_ms": crop_start_ms,
                "crop_end_ms": crop_end_ms,
            },
        }
        observed_request["timeline_binding"] = timeline_binding
    return _PreparedAudioSpan(
        target_start_ms=target_start_ms,
        target_end_ms=target_end_ms,
        crop_start_ms=crop_start_ms,
        crop_end_ms=crop_end_ms,
        observed_request=observed_request,
        timeline_binding=timeline_binding,
    )


def _crop_black_frame_audio(
    *,
    source_media: Path,
    audio_path: Path,
    start_ms: int,
    end_ms: int,
) -> tuple[bool, str]:
    duration_s = (end_ms - start_ms) / 1000.0
    completed = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            f"{start_ms / 1000.0:.3f}",
            "-t",
            f"{duration_s:.3f}",
            "-i",
            str(source_media),
            "-f",
            "lavfi",
            "-t",
            f"{duration_s:.3f}",
            "-i",
            "color=c=black:s=320x240:r=10",
            "-map",
            "1:v:0",
            "-map",
            "0:a:0",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-shortest",
            str(audio_path),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=180,
    )
    if completed.returncode != 0 or not audio_path.is_file():
        return False, completed.stderr
    return True, ""


def _verify_local_audio_request(
    *,
    verifier: _LocalAudioVerifier,
    request: Mapping[str, Any],
) -> Mapping[str, Any]:
    request_sha = str(request.get("request_sha256") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", request_sha):
        return _uncertain(request, "ENTITY_AUDIO_REQUEST_INVALID")
    witness_mode = request.get("schema_version") == WITNESS_REQUEST_SCHEMA
    candidates = request.get("candidate_entities")
    if witness_mode:
        # A dictation witness must not carry candidates at all — their mere
        # presence in the request would prime the transcription.
        if candidates is not None:
            return _uncertain(request, "WITNESS_REQUEST_CARRIES_CANDIDATES")
        candidates = []
    elif not isinstance(candidates, list) or len(candidates) < 2:
        return _uncertain(request, "ENTITY_AUDIO_CANDIDATES_INVALID")
    context_mode = witness_mode or (
        request.get("schema_version") == "subtitle-span-acoustic-check-request.v1"
    )
    source_media_timeline_offset_ms = 0
    if context_mode:
        raw_offset = request.get("source_media_timeline_offset_ms")
        if (
            isinstance(raw_offset, bool)
            or not isinstance(raw_offset, int)
            or raw_offset < 0
        ):
            return _uncertain(request, "ENTITY_AUDIO_TIMELINE_OFFSET_INVALID")
        source_media_timeline_offset_ms = raw_offset
        bound_payload = dict(request)
        bound_payload.pop("request_sha256", None)
        if _json_sha256(bound_payload) != request_sha:
            return _uncertain(request, "ENTITY_AUDIO_REQUEST_HASH_MISMATCH")
    job_dir = verifier.output_dir / "entity_verdicts" / request_sha[:20]
    job_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = job_dir / "verdict.manifest.json"
    audio_path = job_dir / "input.mp4"
    if manifest_path.is_file() and audio_path.is_file():
        try:
            cached = json.loads(manifest_path.read_text(encoding="utf-8"))
            if (
                cached.get("request_sha256") == request_sha
                and cached.get("source_media_sha256") == verifier.source_sha256
                and cached.get("audio_clip_sha256") == _sha256(audio_path)
            ):
                cached_verdict = cached["verdict"]
                # Provider failures must be retried rather than poisoning cache.
                if not (
                    isinstance(cached_verdict, dict)
                    and cached_verdict.get("reason_code")
                    in {
                        "ENTITY_AUDIO_PROVIDER_FAILED",
                        "ENTITY_VERIFIER_ERROR",
                        "ENTITY_AUDIO_RESPONSE_INVALID",
                        "ENTITY_AUDIO_CROP_FAILED",
                    }
                ):
                    return cached_verdict
        except (OSError, ValueError, KeyError, TypeError):
            pass

    span = _prepare_audio_span(
        request=request,
        context_mode=context_mode,
        source_media_timeline_offset_ms=source_media_timeline_offset_ms,
        source_duration_ms=verifier.source_duration_ms,
        witness_mode=witness_mode,
    )
    if span is None:
        return _uncertain(request, "ENTITY_AUDIO_SPAN_INVALID")
    crop_succeeded, crop_error = _crop_black_frame_audio(
        source_media=verifier.source_media,
        audio_path=audio_path,
        start_ms=span.crop_start_ms,
        end_ms=span.crop_end_ms,
    )
    if not crop_succeeded:
        return _uncertain(request, "ENTITY_AUDIO_CROP_FAILED", crop_error)

    acoustic_clip_sha = _sha256(audio_path) if witness_mode else None
    acoustic_cache_hit = False
    outcome = None
    if acoustic_clip_sha is not None:
        cached_outcome = _serve_witness_acoustic_cache(
            output_dir=verifier.output_dir,
            clip_sha256=acoustic_clip_sha,
            job_dir=job_dir,
        )
        if cached_outcome is not None:
            outcome = cached_outcome
            acoustic_cache_hit = True
    if outcome is None:
        outcome = _observe_entity_audio(
            request=span.observed_request,
            candidates=candidates,
            audio_path=audio_path,
            job_dir=job_dir,
            recording_date=verifier.recording_date,
            timely_context=verifier.timely_context,
            binary=verifier.binary,
            model=verifier.model,
            timeout=verifier.timeout,
        )
    observed = outcome.observed
    provider_failures = outcome.provider_failures
    if observed is None:
        (job_dir / "provider-failures.json").write_text(
            json.dumps(
                {
                    "reason_code": "ENTITY_AUDIO_PROVIDER_FAILED",
                    "failures": provider_failures,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        categories = ";".join(
            str(row.get("category")) for row in provider_failures[-4:]
        )
        return _uncertain(request, "ENTITY_AUDIO_PROVIDER_FAILED", categories)
    canonicals = {
        str(row.get("canonical") or "") for row in candidates if isinstance(row, dict)
    }
    status = observed.get("status") if isinstance(observed, dict) else None
    confidence = observed.get("confidence") if isinstance(observed, dict) else None
    heard = (
        str(observed.get("heard_syllables") or "").strip()
        if isinstance(observed, dict)
        else ""
    )
    if context_mode:
        verdict_builder = (
            _subtitle_acoustic_witness_verdict
            if witness_mode
            else _subtitle_acoustic_verdict
        )
        verdict = verdict_builder(
            request=request,
            request_sha=request_sha,
            observed=observed if isinstance(observed, dict) else {},
            outcome=outcome,
            source_sha256=verifier.source_sha256,
            audio_path=audio_path,
            start_ms=span.crop_start_ms,
            end_ms=span.crop_end_ms,
            timeline_binding=span.timeline_binding,
        )
        if (
            acoustic_clip_sha is not None
            and not acoustic_cache_hit
            and isinstance(observed, dict)
            and verdict.get("status") == "OBSERVED"
        ):
            _store_witness_acoustic_cache(
                output_dir=verifier.output_dir,
                clip_sha256=acoustic_clip_sha,
                observed=observed,
                outcome=outcome,
            )
        manifest = {
            "schema_version": "entity-audio-verdict-manifest.v1",
            "request_sha256": request_sha,
            "request_payload_sha256": _json_sha256(dict(request)),
            "source_media": str(verifier.source_media),
            "source_media_sha256": verifier.source_sha256,
            "audio_clip": str(audio_path),
            "audio_clip_sha256": _sha256(audio_path),
            **(
                {
                    "acoustic_cache": {
                        "hit": True,
                        "clip_sha256": acoustic_clip_sha,
                    }
                }
                if acoustic_cache_hit
                else {}
            ),
            "prompt_sha256": _sha256(outcome.prompt_path),
            "response_sha256": _sha256(outcome.response_path),
            "model": outcome.model,
            "provider": outcome.provider,
            **({"key_tier": outcome.accepted_key_tier} if outcome.accepted_key_tier else {}),
            **(
                {"paid_backup_policy": dict(outcome.paid_policy_stamp)}
                if outcome.paid_policy_stamp
                else {}
            ),
            **({"provider_failures": provider_failures} if provider_failures else {}),
            "timeline_binding": span.timeline_binding,
            "verdict": verdict,
        }
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return verdict
    canonical = observed.get("canonical_entity") if isinstance(observed, dict) else None
    resolved = (
        observed.get("schema_version") == "entity-audio-observation.v1"
        and status == "RESOLVED"
        and canonical in canonicals
        and not isinstance(confidence, bool)
        and isinstance(confidence, (int, float))
        and confidence >= 0.80
        and bool(heard)
    )
    verdict: dict[str, Any]
    if resolved:
        verdict = {
            "schema_version": "chat-entity-verdict.v1",
            "request_sha256": request_sha,
            "status": "RESOLVED",
            "canonical_entity": canonical,
            "authority_kind": "audio_forced_choice",
            "confidence": float(confidence),
            "heard_syllables": heard,
            "reason": str(observed.get("reason") or ""),
            "source_media_sha256": verifier.source_sha256,
            "audio_clip_sha256": _sha256(audio_path),
            "prompt_sha256": _sha256(outcome.prompt_path),
            "response_sha256": _sha256(outcome.response_path),
            "model": outcome.model,
            "provider": outcome.provider,
            **({"key_tier": outcome.accepted_key_tier} if outcome.accepted_key_tier else {}),
            "audio_start_ms": span.crop_start_ms,
            "audio_end_ms": span.crop_end_ms,
        }
    else:
        verdict = _uncertain(
            request,
            "ENTITY_AUDIO_UNCERTAIN",
            str(observed.get("reason") or "") if isinstance(observed, dict) else "",
        )
    manifest = {
        "schema_version": "entity-audio-verdict-manifest.v1",
        "request_sha256": request_sha,
        "request_payload_sha256": _json_sha256(dict(request)),
        "source_media": str(verifier.source_media),
        "source_media_sha256": verifier.source_sha256,
        "audio_clip": str(audio_path),
        "audio_clip_sha256": _sha256(audio_path),
        "prompt_sha256": _sha256(outcome.prompt_path),
        "response_sha256": _sha256(outcome.response_path),
        "model": outcome.model,
        "provider": outcome.provider,
        **({"key_tier": outcome.accepted_key_tier} if outcome.accepted_key_tier else {}),
        **(
            {"paid_backup_policy": dict(outcome.paid_policy_stamp)}
            if outcome.paid_policy_stamp
            else {}
        ),
        **({"provider_failures": provider_failures} if provider_failures else {}),
        "verdict": verdict,
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return verdict


def build_local_audio_entity_verifier(
    *,
    source_media: Path,
    output_dir: Path,
    recording_date: str,
    source_duration_ms: int,
    agy_bin: str | None = None,
    model: str = ENTITY_AUDIO_MODEL,
    timeout: str = ENTITY_AUDIO_TIMEOUT,
) -> Callable[[Mapping[str, Any]], Mapping[str, Any]]:
    """Return a memoized callback suitable for chat_authority.entity_verifier."""

    source_media = source_media.resolve()
    output_dir = output_dir.resolve()
    source_sha256 = _sha256(source_media)
    binary = agy_bin or os.environ.get("AGY_BIN", str(Path.home() / ".local/bin/agy"))
    try:
        as_of = dt.datetime.combine(
            dt.date.fromisoformat(recording_date),
            dt.time(12),
            tzinfo=dt.timezone.utc,
        )
        timely = timely_terms_context(as_of=as_of)
    except ValueError:
        timely = ""
    return _LocalAudioVerifier(
        source_media=source_media,
        output_dir=output_dir,
        recording_date=recording_date,
        source_duration_ms=source_duration_ms,
        source_sha256=source_sha256,
        binary=binary,
        model=model,
        timeout=timeout,
        timely_context=timely,
    )
