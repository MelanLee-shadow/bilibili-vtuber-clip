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
from typing import Any, Callable, Mapping

from scripts.gemini_slice_jingting import (
    agy_subprocess_env,
    parse_timeout_seconds,
    strip_markdown_fence,
    timely_terms_context,
)
from src.autoslice import gemini_backup_policy


ENTITY_AUDIO_MODEL = "Gemini 3.5 Flash (High)"
ENTITY_AUDIO_TIMEOUT = "10m"

# Gemini API 直连兜底（Ivan 2026-07-14：付费 API key 当然能裁决音频——AGY
# 订阅配额断供不得阻塞实体裁决）。同一验收逻辑、同一 prompt 语义，仅载体
# 不同：免费 3 key 永远先试，付费走 gemini_backup_policy 门（strike/例外/
# 帽/入帐），key 只存在于内存 header。
GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
ENTITY_AUDIO_API_MODEL_ENV = "ENTITY_AUDIO_GEMINI_API_MODEL"
ENTITY_AUDIO_API_MODEL_DEFAULT = "gemini-3.5-flash"
ENTITY_AUDIO_API_REQUEST_MAX_BYTES = 20_000_000


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
    return {
        "schema_version": "chat-entity-verdict.v1",
        "request_sha256": request.get("request_sha256"),
        "status": "UNCERTAIN",
        "reason_code": reason,
        **({"detail": detail[-500:]} if detail else {}),
    }


def _prompt(
    *,
    candidates: list[dict[str, Any]],
    recording_date: str,
    timely_context: str,
    sentence_mode: bool = False,
    delivery_mode: str = "agy",
    context_before: str = "",
    context_after: str = "",
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
        as_of = dt.datetime.combine(dt.date.fromisoformat(recording_date), dt.time(12), tzinfo=dt.timezone.utc)
        timely = timely_terms_context(as_of=as_of)
    except ValueError:
        timely = ""

    def verify(request: Mapping[str, Any]) -> Mapping[str, Any]:
        request_sha = str(request.get("request_sha256") or "")
        if not re.fullmatch(r"[0-9a-f]{64}", request_sha):
            return _uncertain(request, "ENTITY_AUDIO_REQUEST_INVALID")
        candidates = request.get("candidate_entities")
        if not isinstance(candidates, list) or len(candidates) < 2:
            return _uncertain(request, "ENTITY_AUDIO_CANDIDATES_INVALID")
        job_dir = output_dir / "entity_verdicts" / request_sha[:20]
        job_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = job_dir / "verdict.manifest.json"
        audio_path = job_dir / "input.mp4"
        if manifest_path.is_file() and audio_path.is_file():
            try:
                cached = json.loads(manifest_path.read_text(encoding="utf-8"))
                if (
                    cached.get("request_sha256") == request_sha
                    and cached.get("source_media_sha256") == source_sha256
                    and cached.get("audio_clip_sha256") == _sha256(audio_path)
                ):
                    cached_verdict = cached["verdict"]
                    # 供应商级失败绝不缓存复用（2026-07-14 配额期中毒实证：
                    # 断供期的 PROVIDER_FAILED 判决被 manifest 固化，之后每次
                    # 重试都命中缓存不再真听）。RESOLVED 与真·声学 UNCERTAIN
                    # 可复用；供应商/校验类失败必须重听。
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

        try:
            start_ms = max(0, int(request["matched_start_ms"]) - 1_500)
            end_ms = min(source_duration_ms, int(request["matched_end_ms"]) + 1_500)
        except (KeyError, TypeError, ValueError):
            return _uncertain(request, "ENTITY_AUDIO_SPAN_INVALID")
        if end_ms <= start_ms:
            return _uncertain(request, "ENTITY_AUDIO_SPAN_INVALID")
        duration_s = (end_ms - start_ms) / 1000.0
        ffmpeg = subprocess.run(
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
        if ffmpeg.returncode != 0 or not audio_path.is_file():
            return _uncertain(request, "ENTITY_AUDIO_CROP_FAILED", ffmpeg.stderr)

        prompt = _prompt(
            candidates=[dict(row) for row in candidates if isinstance(row, dict)],
            recording_date=recording_date,
            timely_context=timely,
            sentence_mode=(
                request.get("schema_version") == "chat-read-aloud-verification-request.v1"
            ),
            context_before=str(request.get("context_before") or ""),
            context_after=str(request.get("context_after") or ""),
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
                {"provider": "agy", "category": "AGY_SUBPROCESS_ERROR", "error_type": type(exc).__name__}
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
            # Gemini API 直连兜底：免费 3 key 先试，付费按政策门+入帐。
            provider = "gemini_api"
            model_used = _entity_api_model()
            api_audio_path = job_dir / "input.gemini-api.mp3"
            api_prompt_path = job_dir / "prompt.gemini-api.md"
            api_response_path = job_dir / "verdict.gemini-api.raw.json"
            extract = subprocess.run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-i",
                    str(audio_path),
                    "-vn",
                    "-ac",
                    "1",
                    "-ar",
                    "16000",
                    "-b:a",
                    "64k",
                    str(api_audio_path),
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
                api_prompt = _prompt(
                    candidates=[dict(row) for row in candidates if isinstance(row, dict)],
                    recording_date=recording_date,
                    timely_context=timely,
                    sentence_mode=(
                        request.get("schema_version") == "chat-read-aloud-verification-request.v1"
                    ),
                    context_before=str(request.get("context_before") or ""),
                    context_after=str(request.get("context_after") or ""),
                    delivery_mode="gemini_api",
                )
                api_prompt_path.write_text(api_prompt, encoding="utf-8")

                def _attempt_api_key(attempt_key: str, *, key_tier: str) -> bool:
                    nonlocal observed, accepted_key_tier
                    try:
                        raw = _gemini_api_observe_entity(
                            audio_path=api_audio_path,
                            prompt=api_prompt,
                            key=attempt_key,
                            model=model_used,
                        )
                        # 无论成败先落盘原始响应（取证；失败尝试会被下一次覆盖）
                        api_response_path.write_text(
                            (raw or "") if (raw or "").endswith("\n") else (raw or "") + "\n",
                            encoding="utf-8",
                        )
                        if not raw or len(raw.encode("utf-8")) > 2_000_000:
                            raise ValueError("empty or oversized Gemini API output")
                        candidate_observed = json.loads(raw)
                        api_response_path.write_text(
                            raw if raw.endswith("\n") else raw + "\n", encoding="utf-8"
                        )
                        observed = candidate_observed
                        accepted_key_tier = key_tier
                        return True
                    except Exception as exc:
                        provider_failures.append(
                            {
                                "provider": "gemini_api",
                                "key_tier": key_tier,
                                "category": _api_failure_category(exc),
                                "error_type": type(exc).__name__,
                            }
                        )
                        return False

                for key in _configured_free_keys():
                    if _attempt_api_key(key, key_tier=gemini_backup_policy.FREE_KEY_TIER):
                        break
                if observed is None:
                    item_key = _sha256(api_audio_path)
                    prior_strikes = gemini_backup_policy.free_chain_strikes(item_key)
                    gemini_backup_policy.record_free_chain_failure(item_key)
                    allowed, gate_reason = gemini_backup_policy.paid_attempt_allowed(
                        item_key, prior_strikes=prior_strikes
                    )
                    if allowed and _attempt_api_key(
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
            categories = ";".join(str(row.get("category")) for row in provider_failures[-4:])
            return _uncertain(request, "ENTITY_AUDIO_PROVIDER_FAILED", categories)
        canonicals = {str(row.get("canonical") or "") for row in candidates if isinstance(row, dict)}
        status = observed.get("status") if isinstance(observed, dict) else None
        canonical = observed.get("canonical_entity") if isinstance(observed, dict) else None
        confidence = observed.get("confidence") if isinstance(observed, dict) else None
        heard = str(observed.get("heard_syllables") or "").strip() if isinstance(observed, dict) else ""
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
                "source_media_sha256": source_sha256,
                "audio_clip_sha256": _sha256(audio_path),
                "prompt_sha256": _sha256(prompt_path),
                "response_sha256": _sha256(response_path),
                "model": model_used,
                "provider": provider,
                **({"key_tier": accepted_key_tier} if accepted_key_tier else {}),
                "audio_start_ms": start_ms,
                "audio_end_ms": end_ms,
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
            "source_media": str(source_media),
            "source_media_sha256": source_sha256,
            "audio_clip": str(audio_path),
            "audio_clip_sha256": _sha256(audio_path),
            "prompt_sha256": _sha256(prompt_path),
            "response_sha256": _sha256(response_path),
            "model": model_used,
            "provider": provider,
            **({"key_tier": accepted_key_tier} if accepted_key_tier else {}),
            **({"paid_backup_policy": dict(paid_policy_stamp)} if paid_policy_stamp else {}),
            **({"provider_failures": provider_failures} if provider_failures else {}),
            "verdict": verdict,
        }
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return verdict

    return verify
