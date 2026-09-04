"""Independent raw-audio forced choice for confusable subtitle entities.

The ordinary ASR/AGY/CPA transcript may already have seen nearby chat.  It is
therefore ineligible to arbitrate a conflict between that chat and a proper
name.  This module renders a short audio-only (black-frame) clip and asks a
sandboxed AGY High job to choose among canonical entities from the syllables.
Every result is hash-bound; uncertainty fails closed in the caller.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess

# 保留 urllib.request 导入：F21 的 fallback 单测通过
# ``monkeypatch.setattr(verifier_module.urllib.request, "urlopen", ...)``
# 打在这个模块可见的 urllib 上；真正的请求在 agy_gemini_client 里发出，
# 两边引用的是同一个 urllib.request 模块对象，所以 patch 依然生效。
import urllib.request  # noqa: F401 - keeps the F21 urlopen monkeypatch seam
from dataclasses import dataclass, field, replace
from functools import partial
import threading
from typing import Any, Callable, Mapping

from scripts.gemini_slice_jingting import (
    agy_subprocess_env,
    parse_timeout_seconds,
    strip_markdown_fence,
    timely_terms_context,
)
from src.autoslice import agy_gemini_client, gemini_backup_policy
from src.autoslice.acoustic_witness_protocol import (
    BLIND_PINYIN_PROTOCOL,
    contains_han_text,
)
from src.autoslice.llm_client import extract_json_object
from src.autoslice.exact_source_transcript_contract import (
    OBSERVATION_SCHEMA as EXACT_SOURCE_OBSERVATION_SCHEMA,
    REQUEST_SCHEMA as EXACT_SOURCE_REQUEST_SCHEMA,
    exact_source_transcript_prompt,
    valid_exact_source_transcript_request,
)
from src.autoslice.exact_source_transcript_provider import (
    normalize_exact_source_transcript_provider_attempts,
    seal_exact_source_transcript_capability_unavailable,
    seal_exact_source_transcript_provider_failure,
)
from src.autoslice.exact_source_transcript_provider_policy import (
    build_provider_route as _build_exact_provider_route,
)
from src.autoslice.exact_source_transcript_runtime import (
    provider_manifest_path as _exact_provider_manifest_path,
    seal_provider_observation as _seal_exact_provider_observation,
    serve_manifest as _serve_exact_source_transcript_manifest,
    store_manifest as _store_exact_source_transcript_manifest,
)
from src.autoslice import entity_audio_gemini_web as _gemini_web
from src.autoslice.entity_audio_gemini_web import (
    _EntityProviderOutcome,
    _provider_artifact_bindings,
    _provider_provenance,
    run_gemini_web_fallback_from_verifier as _run_gemini_web_fallback,
    run_gemini_web_fallback_if_enabled as _run_gemini_web_if_enabled,
)

ENTITY_AUDIO_AGY_MODEL_ENV = "ENTITY_AUDIO_AGY_MODEL"
ENTITY_AUDIO_MODEL = os.environ.get(
    ENTITY_AUDIO_AGY_MODEL_ENV, "Gemini 3.6 Flash (High)"
)
ENTITY_AUDIO_TIMEOUT = "10m"

# AGY remains the preferred high-confidence audio witness.  Direct Gemini API
# is a bounded availability fallback for candidate-blind witness requests only;
# it never sees current/proposed text and can never authorize a mutation.
GEMINI_API_URL = agy_gemini_client.GEMINI_API_URL
ENTITY_AUDIO_API_MODEL_ENV = "ENTITY_AUDIO_GEMINI_API_MODEL"
ENTITY_AUDIO_API_MODEL_DEFAULT = "gemini-3.6-flash"
ENTITY_AUDIO_API_REQUEST_MAX_BYTES = 20_000_000
# F21（维护者）：显式关掉 AGY 那一环时的开关。默认不设 = AGY 仍是
# 首选；设为 1 时链直接从免费 3 key 起跑。key 顺序本身不变：AGY 订阅 →
# 免费 3 key 轮换 → 政策门控付费 backup（7/19 裁定，同一 Gemini 模型的配额
# 顺序，不是不同 provider 的证据等级）。
ENTITY_AUDIO_DISABLE_AGY_ENV = "ENTITY_AUDIO_DISABLE_AGY"

ENTITY_AUDIO_GEMINI_WEB_ENABLED_ENV = _gemini_web.ENTITY_AUDIO_GEMINI_WEB_ENABLED_ENV
ENTITY_AUDIO_GEMINI_WEB_MODEL_ENV = _gemini_web.ENTITY_AUDIO_GEMINI_WEB_MODEL_ENV
ENTITY_AUDIO_GEMINI_WEB_COMMAND_ENV = _gemini_web.ENTITY_AUDIO_GEMINI_WEB_COMMAND_ENV
ENTITY_AUDIO_GEMINI_WEB_PYTHON_ENV = _gemini_web.ENTITY_AUDIO_GEMINI_WEB_PYTHON_ENV
ENTITY_AUDIO_GEMINI_WEB_PROFILE_ENV = _gemini_web.ENTITY_AUDIO_GEMINI_WEB_PROFILE_ENV
ENTITY_AUDIO_GEMINI_WEB_BROWSER_ENV = _gemini_web.ENTITY_AUDIO_GEMINI_WEB_BROWSER_ENV
ENTITY_AUDIO_GEMINI_WEB_XVFB_ENV = _gemini_web.ENTITY_AUDIO_GEMINI_WEB_XVFB_ENV
ENTITY_AUDIO_GEMINI_WEB_USER_ENV = _gemini_web.ENTITY_AUDIO_GEMINI_WEB_USER_ENV
ENTITY_AUDIO_GEMINI_WEB_TIMEOUT_ENV = _gemini_web.ENTITY_AUDIO_GEMINI_WEB_TIMEOUT_ENV
_terminate_web_process_group = _gemini_web.terminate_web_process_group

# Phase 1 acoustic-witness architecture (维护者 ruling): the audio
# model is a WITNESS, not a judge. In witness mode it never sees any
# candidate text — it dictates suspected pinyin syllables only; hanzi
# word-choice reasoning belongs to the CPA judge downstream.
WITNESS_REQUEST_SCHEMA = "subtitle-span-acoustic-witness-request.v1"
WITNESS_SCHEMA = "subtitle-span-acoustic-witness.v1"
# The prompt contract is part of the acoustic-cache identity.  A 
# production incident proved why: the old prompt embedded one valid pinyin
# example and AGY copied it verbatim for unrelated audio.  Audio bytes alone
# are not a sufficient cache key when the dictation instructions change.
WITNESS_PROMPT_CONTRACT = "candidate-free-toneless-pinyin-neutral-length.v3"
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
    if request.get("schema_version") == EXACT_SOURCE_REQUEST_SCHEMA:
        return {
            "schema_version": EXACT_SOURCE_OBSERVATION_SCHEMA,
            "request_sha256": request.get("request_sha256"),
            "status": "INVALID",
            "candidate_blind": True,
            "reason_code": reason,
            "mutation_authorized": False,
            **({"detail": detail[-500:]} if detail else {}),
        }
    if request.get("schema_version") == WITNESS_REQUEST_SCHEMA:
        return {
            "schema_version": WITNESS_SCHEMA,
            "witness_protocol": BLIND_PINYIN_PROTOCOL,
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
    delivery_mode: str = "agy",
    target_audio_start_ms: int | None,
    target_audio_end_ms: int | None,
    syllable_count_hint: int | None = None,
) -> str:
    """Pure-dictation witness prompt. Deliberately shows NO candidate text,
    no adjacent transcript, and no glossary — anything textual would prime
    the dictation. The witness reports suspected pinyin only."""

    if delivery_mode == "gemini_api":
        source_line = (
            "Use only the attached audio clip. There is no viewer chat, subtitle,\n"
            "title card, or other visual text to consult."
        )
        output_head = (
            "Reply with exactly one JSON object (no markdown fences, no other text):"
        )
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
Neutral length hint: {syllable_count_hint if syllable_count_hint is not None else "unknown"} target syllables.
This hint carries no candidate wording; trust the audio if the count differs.
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
    context_before: str = "",
    context_after: str = "",
    target_audio_start_ms: int | None = None,
    target_audio_end_ms: int | None = None,
    acoustic_fit_mode: bool = False,
) -> str:
    neutral_candidates = sorted(candidates, key=lambda row: str(row.get("canonical") or "").lower())
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


_EXPLICIT_AGY_QUOTA_EXHAUSTED_RX = agy_gemini_client.EXPLICIT_AGY_QUOTA_RX

# This lane's granular non-zero classification is now the shared one.
_classify_agy_failure = agy_gemini_client.classify_agy_failure


@dataclass
class _AgyQuotaCircuitBreaker:
    """Run-local breaker for an explicit, account-wide AGY quota response.

    This state is deliberately attached to one local verifier instance.  A
    producer builds that verifier once and reuses it for every cue in the run,
    while a later producer process starts closed again and can observe quota
    recovery.  Only the typed ``AGY_QUOTA_EXHAUSTED`` category may open it.
    """

    _quota_exhausted: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def is_open(self) -> bool:
        with self._lock:
            return self._quota_exhausted

    def observe_failure(self, category: str) -> bool:
        if category != "AGY_QUOTA_EXHAUSTED":
            return False
        with self._lock:
            self._quota_exhausted = True
        return True


_configured_free_keys = agy_gemini_client.free_api_keys
_api_failure_category = agy_gemini_client.classify_gemini_failure


def _entity_api_model() -> str:
    return (
        os.environ.get(ENTITY_AUDIO_API_MODEL_ENV)
        or ENTITY_AUDIO_API_MODEL_DEFAULT
    )


def _gemini_api_observe_witness(
    *, audio_path: Path, prompt: str, key: str, model: str
) -> str:
    """One inline-audio Gemini request; the key exists only in its header."""

    try:
        timeout_seconds = int(
            os.environ.get("ENTITY_GEMINI_API_TIMEOUT_SECONDS", "180")
        )
    except ValueError:
        timeout_seconds = 180
    return strip_markdown_fence(
        agy_gemini_client.generate_content(
            prompt=prompt,
            key=key,
            model=model,
            inline_data=audio_path.read_bytes(),
            mime_type="audio/mpeg",
            thinking={
                "thinkingLevel": os.environ.get("ENTITY_GEMINI_THINKING_LEVEL", "low")
            },
            timeout_seconds=timeout_seconds,
            max_request_bytes=ENTITY_AUDIO_API_REQUEST_MAX_BYTES,
            degrade_on_400=True,
        )
    )


def _run_gemini_api_fallback(
    *,
    audio_path: Path,
    prompt: str,
    response_path: Path,
    model: str,
    provider_failures: list[dict[str, Any]],
    purpose: str = gemini_backup_policy.CANDIDATE_BLIND_AUDIO_WITNESS_PURPOSE,
    item_key: str | None = None,
) -> agy_gemini_client.LadderOutcome:
    """Try free keys in receipt-backed rounds, then the policy-gated paid key.

    The ladder mechanics now live in ``agy_gemini_client``; what stays here is
    this lane's own attempt body (observe -> persist -> parse) and its receipt
    row shape, both unchanged.
    """

    def observe(attempt_key: str) -> Any:
        raw = _gemini_api_observe_witness(
            audio_path=audio_path,
            prompt=prompt,
            key=attempt_key,
            model=model,
        )
        response_path.write_text(
            (raw or "") if (raw or "").endswith("\n") else (raw or "") + "\n",
            encoding="utf-8",
        )
        if not raw or len(raw.encode("utf-8")) > 2_000_000:
            raise ValueError("empty or oversized Gemini API output")
        return extract_json_object(raw)

    def record_failure(failure: agy_gemini_client.GeminiAttemptFailure) -> None:
        provider_failures.append(
            {
                "provider": "gemini_api",
                "key_tier": failure.key_tier,
                "key_ordinal": failure.key_ordinal,
                "attempt_round": failure.attempt_round,
                "model": model,
                "category": failure.category,
                "error_type": failure.error_type,
                **(
                    {"http_status": failure.http_status}
                    if failure.http_status is not None
                    else {}
                ),
            }
        )

    def record_paid_skipped(key_ordinal: int, attempt_round: int, gate_reason: str) -> None:
        provider_failures.append(
            {
                "provider": "gemini_api",
                "key_tier": gemini_backup_policy.PAID_KEY_TIER,
                "key_ordinal": key_ordinal,
                "attempt_round": attempt_round,
                "category": f"PAID_BACKUP_SKIPPED:{gate_reason}",
            }
        )

    outcome = agy_gemini_client.run_gemini_key_ladder(
        item_key=item_key or _sha256(audio_path),
        observe=observe,
        purpose=purpose,
        record_failure=record_failure,
        record_paid_skipped=record_paid_skipped,
    )
    return outcome


_ACOUSTIC_CACHE_SCHEMA = "witness-acoustic-cache.v4"
_CACHEABLE_WITNESS_PROVIDERS = frozenset({"agy", "gemini_api"})


def _witness_cache_prompt_identity(
    request: Mapping[str, Any], *, recording_date: str
) -> str:
    prompt = _witness_prompt(
        recording_date=recording_date,
        delivery_mode="agy",
        syllable_count_hint=request.get("syllable_count_hint"),
        target_audio_start_ms=request.get("target_audio_start_ms"),
        target_audio_end_ms=request.get("target_audio_end_ms"),
    )
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def _witness_acoustic_cache_path(
    output_dir: Path,
    clip_sha256: str,
    prompt_identity_sha256: str,
    *,
    provider: str = "agy",
    model: str = ENTITY_AUDIO_MODEL,
) -> Path:
    """Global content-addressed cache entry for one witness audio clip.

    成本裁定（维护者，3 天 $40 案）：付费声学证人 88% 的消耗来自
    重产轮次对**同一段音频**的重复听写——witness 请求按设计不携带候选
    （纯听写），答案只由音频决定，request_sha 里的文本漂移不改变问题本身。
    键严格绑定音频片 sha256、完整 prompt sha、provider 和 model；中性
    音节提示、问题 markers、provider 或 model 任一漂移都必须 miss。
    BASE 从候选包目录上溯（out/<date>/<cid> → BASE），主树与 V15
    恢复树各自命中自己的缓存。
    """

    cache_identity = _json_sha256(
        {
            "schema_version": _ACOUSTIC_CACHE_SCHEMA,
            "prompt_contract": WITNESS_PROMPT_CONTRACT,
            "audio_clip_sha256": clip_sha256,
            "prompt_identity_sha256": prompt_identity_sha256,
            "provider": provider,
            "model": model,
        }
    )
    # Production packages are BASE/out/<date>/<candidate>.  Shallow test or
    # one-off output directories must not escape upward into a shared parent,
    # otherwise unrelated jobs with identical fixture bytes can cross-hit.
    base = (
        output_dir.parents[2]
        if len(output_dir.parents) >= 3 and output_dir.parents[1].name == "out"
        else output_dir
    )
    return (
        base
        / "cache"
        / "witness-acoustic"
        / clip_sha256[:2]
        / f"{cache_identity}.json"
    )


def _serve_witness_acoustic_cache(
    *,
    output_dir: Path,
    clip_sha256: str,
    job_dir: Path,
    expected_provider: str = "agy",
    expected_model: str,
    expected_prompt_identity_sha256: str,
) -> _EntityProviderOutcome | None:
    """Return a synthetic provider outcome from the acoustic cache, or None.

    只回放 OBSERVED 的原始听写；prompt/response 工件拷贝进当前 job_dir，
    下游照常重算全部 sha 与报告校验——缓存只省 provider 调用，不省验证。
    """

    if (
        expected_provider not in _CACHEABLE_WITNESS_PROVIDERS
        or not re.fullmatch(r"[0-9a-f]{64}", clip_sha256)
        or not re.fullmatch(r"[0-9a-f]{64}", expected_prompt_identity_sha256)
        or not expected_model
    ):
        return None
    entry_path = _witness_acoustic_cache_path(
        output_dir,
        clip_sha256,
        expected_prompt_identity_sha256,
        provider=expected_provider,
        model=expected_model,
    )
    try:
        entry = json.loads(entry_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(entry, Mapping):
        return None
    observed = entry.get("observed")
    if (
        entry.get("schema_version") != _ACOUSTIC_CACHE_SCHEMA
        or entry.get("prompt_contract") != WITNESS_PROMPT_CONTRACT
        or entry.get("audio_clip_sha256") != clip_sha256
        or entry.get("provider") != expected_provider
        or entry.get("model") != expected_model
        or entry.get("prompt_identity_sha256")
        != expected_prompt_identity_sha256
        or not isinstance(observed, dict)
        or observed.get("schema_version") != WITNESS_SCHEMA
        or observed.get("status") != "OBSERVED"
        or entry.get("observed_sha256") != _json_sha256(observed)
    ):
        return None
    prompt_src = entry_path.with_suffix(".prompt.json")
    response_src = entry_path.with_suffix(".response.json")
    if not prompt_src.is_file() or not response_src.is_file():
        return None
    if (
        _sha256(prompt_src) != expected_prompt_identity_sha256
        or entry.get("provider_prompt_sha256") != expected_prompt_identity_sha256
    ):
        return None
    if _sha256(response_src) != entry.get("provider_response_sha256"):
        return None
    try:
        response_observed = extract_json_object(
            strip_markdown_fence(response_src.read_text(encoding="utf-8"))
        )
    except (OSError, TypeError, ValueError):
        return None
    if response_observed != observed:
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
        paid_policy_stamp=(
            dict(entry["paid_policy_stamp"])
            if isinstance(entry.get("paid_policy_stamp"), Mapping)
            else None
        ),
        provider_failures=[],
        served_from_cache=True,
    )


def _store_witness_acoustic_cache(
    *,
    output_dir: Path,
    clip_sha256: str,
    observed: Mapping[str, Any],
    outcome: _EntityProviderOutcome,
    prompt_identity_sha256: str,
) -> None:
    """Best-effort provider/model-isolated successful witness write-through."""

    if (
        outcome.provider not in _CACHEABLE_WITNESS_PROVIDERS
        or not outcome.model
        or observed.get("schema_version") != WITNESS_SCHEMA
        or observed.get("status") != "OBSERVED"
        or not isinstance(outcome.observed, Mapping)
        or dict(outcome.observed) != dict(observed)
    ):
        return

    try:
        provider_prompt_sha256 = _sha256(outcome.prompt_path)
        provider_response_sha256 = _sha256(outcome.response_path)
        if provider_prompt_sha256 != prompt_identity_sha256:
            return
        response_observed = extract_json_object(
            strip_markdown_fence(outcome.response_path.read_text(encoding="utf-8"))
        )
        if response_observed != dict(observed):
            return
        entry_path = _witness_acoustic_cache_path(
            output_dir,
            clip_sha256,
            prompt_identity_sha256,
            provider=outcome.provider,
            model=outcome.model,
        )
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
                    "prompt_identity_sha256": prompt_identity_sha256,
                    "provider_prompt_sha256": provider_prompt_sha256,
                    "provider_response_sha256": provider_response_sha256,
                    "observed": dict(observed),
                    "observed_sha256": _json_sha256(dict(observed)),
                    "provider": outcome.provider,
                    "model": outcome.model,
                    "key_tier": outcome.accepted_key_tier,
                    **(
                        {"paid_policy_stamp": dict(outcome.paid_policy_stamp)}
                        if outcome.paid_policy_stamp
                        else {}
                    ),
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
    except (OSError, TypeError, ValueError):
        pass


def _replay_provider_witness_cache(
    *,
    audio_path: Path,
    job_dir: Path,
    prompt: str,
    provider: str,
    model: str,
    provider_failures: list[dict[str, Any]],
) -> _EntityProviderOutcome | None:
    """Replay only the exact current provider/model question, preserving fresh routing."""

    cached = _serve_witness_acoustic_cache(
        output_dir=job_dir.parent.parent,
        clip_sha256=_sha256(audio_path),
        job_dir=job_dir,
        expected_provider=provider,
        expected_model=model,
        expected_prompt_identity_sha256=hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
    )
    if cached is None:
        return None
    return replace(cached, provider_failures=list(provider_failures))


def _entity_observation_prompt(
    *,
    request: Mapping[str, Any],
    candidates: list[Any],
    recording_date: str,
    timely_context: str,
) -> tuple[str, bool, bool]:
    """Build the provider prompt and return the routing modes it establishes."""

    schema_version = request.get("schema_version")
    witness_mode = schema_version == WITNESS_REQUEST_SCHEMA
    exact_transcript_mode = schema_version == EXACT_SOURCE_REQUEST_SCHEMA
    sentence_mode = schema_version in {
        "chat-read-aloud-verification-request.v1",
        "subtitle-span-acoustic-check-request.v1",
    }
    acoustic_fit_mode = schema_version == "subtitle-span-acoustic-check-request.v1"
    if exact_transcript_mode:
        prompt = exact_source_transcript_prompt(
            request=request, timeline_binding=request.get("timeline_binding") or {}
        )
    elif witness_mode:
        prompt = _witness_prompt(
            recording_date=recording_date,
            delivery_mode="agy", syllable_count_hint=request.get("syllable_count_hint"),
            target_audio_start_ms=request.get("target_audio_start_ms"),
            target_audio_end_ms=request.get("target_audio_end_ms"),
        )
    else:
        prompt = _prompt(
            candidates=[dict(row) for row in candidates if isinstance(row, dict)],
            recording_date=recording_date,
            timely_context=timely_context,
            sentence_mode=sentence_mode,
            context_before=str(request.get("context_before") or ""),
            context_after=str(request.get("context_after") or ""),
            target_audio_start_ms=request.get("target_audio_start_ms"),
            target_audio_end_ms=request.get("target_audio_end_ms"),
            acoustic_fit_mode=acoustic_fit_mode,
        )
    return prompt, witness_mode, exact_transcript_mode


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
    agy_quota_circuit: _AgyQuotaCircuitBreaker,
    exact_cache_replay: Callable[
        [str, str, list[dict[str, Any]]], Mapping[str, Any] | None
    ] | None = None,
) -> _EntityProviderOutcome:
    """Run preferred AGY, then bounded direct API for blind audio evidence."""

    prompt, witness_mode, exact_transcript_mode = _entity_observation_prompt(
        request=request,
        candidates=candidates,
        recording_date=recording_date,
        timely_context=timely_context,
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
    accepted_key_ordinal: int | None = None
    configured_key_count = 0
    paid_policy_stamp: Mapping[str, Any] | None = None
    provider_failures: list[dict[str, Any]] = []
    response_path = job_dir / "verdict.raw.json"
    cached_outcome = (
        _replay_provider_witness_cache(
            audio_path=audio_path,
            job_dir=job_dir,
            prompt=prompt,
            provider="agy",
            model=model,
            provider_failures=provider_failures,
        )
        if witness_mode
        else None
    )
    if cached_outcome is not None:
        return cached_outcome
    if os.environ.get(ENTITY_AUDIO_DISABLE_AGY_ENV, "").strip() == "1":
        provider_failures.append(
            {
                "provider": "agy",
                "category": "AGY_DISABLED_BY_ENV",
                "attempted": False,
            }
        )
    elif agy_quota_circuit.is_open():
        provider_failures.append(
            {
                "provider": "agy",
                "category": "AGY_QUOTA_EXHAUSTED",
                "circuit_breaker": "OPEN",
                "attempted": False,
            }
        )
    else:
        # F21：wsl 产线上根本没有 agy 二进制。这不是"子进程炸了"，是"这一环
        # 不存在"——回执必须说清楚，否则运维会去查一个不存在的 AGY 故障，而
        # 真正的 provider 是下面的 Gemini。分类归统一客户端。
        verdict_path = job_dir / "verdict.json"
        agy_output_ready = True
        if exact_transcript_mode:
            try:
                verdict_path.unlink(missing_ok=True)
            except OSError as exc:
                agy_output_ready = False
                provider_failures.append(
                    {
                        "provider": "agy",
                        "category": "AGY_STALE_OUTPUT_CLEANUP_FAILED",
                        "error_type": type(exc).__name__,
                        "attempted": False,
                    }
                )
        run = agy_gemini_client.run_local_agy(
            agy_gemini_client.agy_argv(
                binary,
                job_dir=job_dir,
                model=model,
                prompt=short_prompt,
                print_timeout=timeout,
            ),
            cwd=job_dir,
            env=agy_subprocess_env(),
            timeout=parse_timeout_seconds(timeout) + 120,
        ) if agy_output_ready else None
        completed = run.completed if run is not None else None
        if run is not None and completed is None:
            provider_failures.append(
                {
                    "provider": "agy",
                    "category": run.failure_category,
                    "error_type": run.launch_error_type,
                }
            )
        elif completed is not None:
            (job_dir / "agy.stdout").write_text(completed.stdout, encoding="utf-8")
            (job_dir / "agy.stderr").write_text(completed.stderr, encoding="utf-8")
            raw_response = (
                verdict_path.read_text(encoding="utf-8", errors="replace")
                if verdict_path.is_file()
                else completed.stdout
            )
            response_path.write_text(raw_response, encoding="utf-8")
            failure_category = _classify_agy_failure(
                completed.returncode, completed.stdout, completed.stderr
            )
            # An explicit quota response is authoritative even if a buggy CLI
            # wrapper exits zero.  Checking it before verdict.json also avoids
            # accepting a stale file left in a retried job directory.
            if failure_category == "AGY_QUOTA_EXHAUSTED":
                failure: dict[str, Any] = {
                    "provider": "agy",
                    "category": failure_category,
                }
                agy_quota_circuit.observe_failure(failure_category)
                failure.update(
                    {
                        "circuit_breaker": "TRIPPED",
                        "attempted": True,
                    }
                )
                provider_failures.append(failure)
            elif completed.returncode != 0:
                provider_failures.append(
                    {
                        "provider": "agy",
                        "category": failure_category,
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

    web_outcome = _run_gemini_web_if_enabled(
        observed is None and witness_mode, request, audio_path, job_dir,
        recording_date, provider_failures,
        fallback=_run_gemini_web_fallback,
    )
    if web_outcome is not None:
        return web_outcome

    if observed is None and (witness_mode or exact_transcript_mode):
        provider = "gemini_api"
        model_used = _entity_api_model()
        api_audio_path = job_dir / "input.gemini-api.mp3"
        api_prompt_path = job_dir / "prompt.gemini-api.md"
        api_response_path = job_dir / "verdict.gemini-api.raw.json"
        api_prompt = (
            prompt
            if exact_transcript_mode
            else _witness_prompt(
                recording_date=recording_date,
                delivery_mode="gemini_api",
                syllable_count_hint=request.get("syllable_count_hint"),
                target_audio_start_ms=request.get("target_audio_start_ms"),
                target_audio_end_ms=request.get("target_audio_end_ms"),
            )
        )
        cached_exact = (
            exact_cache_replay(provider, model_used, provider_failures)
            if exact_transcript_mode and exact_cache_replay is not None
            else None
        )
        if cached_exact is not None:
            route = cached_exact.get("provider_route")
            return _EntityProviderOutcome(
                observed=cached_exact, provider=provider, model=model_used,
                prompt_path=api_prompt_path, response_path=api_response_path,
                accepted_key_tier=(route or {}).get("accepted_key_tier"),
                paid_policy_stamp=(route or {}).get("paid_backup_policy"),
                provider_failures=list(provider_failures),
                accepted_key_ordinal=(route or {}).get("accepted_key_ordinal"),
                configured_key_count=(route or {}).get("configured_key_count", 0),
                served_from_cache=True,
            )
        cached_outcome = _replay_provider_witness_cache(
            audio_path=audio_path,
            job_dir=job_dir,
            prompt=api_prompt,
            provider=provider,
            model=model_used,
            provider_failures=provider_failures,
        )
        if cached_outcome is not None:
            return cached_outcome
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
                {
                    "provider": "gemini_api",
                    "category": "GEMINI_API_AUDIO_EXTRACTION_FAILED",
                }
            )
        else:
            api_prompt_path.write_text(api_prompt, encoding="utf-8")
            ladder = _run_gemini_api_fallback(
                audio_path=api_audio_path,
                prompt=api_prompt,
                response_path=api_response_path,
                model=model_used,
                provider_failures=provider_failures,
                purpose=(
                    gemini_backup_policy.CANDIDATE_BLIND_EXACT_TRANSCRIPT_PURPOSE
                    if exact_transcript_mode
                    else gemini_backup_policy.CANDIDATE_BLIND_AUDIO_WITNESS_PURPOSE
                ),
                item_key=_sha256(audio_path) if exact_transcript_mode else None,
            )
            observed = ladder.observed
            accepted_key_tier = ladder.accepted_key_tier
            accepted_key_ordinal = ladder.accepted_key_ordinal
            configured_key_count = ladder.configured_key_count
            paid_policy_stamp = ladder.paid_policy_stamp
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
        accepted_key_ordinal=accepted_key_ordinal,
        configured_key_count=configured_key_count,
    )


_PINYIN_SYLLABLE_RX = re.compile(r"^(?:[a-zü]+|\?)$")


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
    # 静音证词：删除提案的时窗里确实无语音时，
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
        and not contains_han_text(json.dumps(dict(observed), ensure_ascii=False))
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
    # redundant self-count that models routinely get off by one (
    # four clean supporting witnesses on 1209_1410 were all invalidated by
    # this arithmetic). The recount below is authoritative; a mismatch is
    # disclosed, never fatal.
    self_count_mismatch = report_valid and syllable_count != len(tokens)
    # Physical plausibility backstop: Mandarin peaks near ~9 syllables/s.
    # A rate far above that means the dictation overflowed the target span
    # (1.12s target, 14 syllables) — poisoned evidence, retriable.
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
        "witness_protocol": BLIND_PINYIN_PROTOCOL,
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
        **_provider_provenance(outcome),
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
    source_stat_binding: tuple[int, int, int, int, int, int, int]
    binary: str
    model: str
    timeout: str
    timely_context: str
    agy_quota_circuit: _AgyQuotaCircuitBreaker = field(
        default_factory=_AgyQuotaCircuitBreaker
    )

    def __call__(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        return _verify_local_audio_request(verifier=self, request=request)

    def exact_source_transcript(
        self, request: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        return _verify_local_audio_request(verifier=self, request=request)

    def probe_witness_cache(
        self, request: Mapping[str, Any]
    ) -> Mapping[str, Any] | None:
        """Replay a physically identical blind witness without provider I/O.

        A miss is ``None``; this seam never crops, writes, or calls AGY/Gemini.
        """

        from src.autoslice.entity_witness_cache_probe import (
            probe_cached_local_audio_witness,
        )

        return probe_cached_local_audio_witness(verifier=self, request=request)

    def probe_exact_source_transcript_cache(
        self, request: Mapping[str, Any]
    ) -> Mapping[str, Any] | None:
        if not valid_exact_source_transcript_request(request):
            return None
        job_dir = self.output_dir / "entity_verdicts" / request["request_sha256"][:20]
        for provider, model in (("agy", self.model), ("gemini_api", _entity_api_model())):
            cached = _serve_exact_provider_cache(
                provider, model, verifier=self, request=request, job_dir=job_dir,
                audio_path=job_dir / "input.mp4",
            )
            if cached is not None:
                return cached
        return None


def _serve_exact_provider_cache(
    provider: str,
    model: str,
    failures: list[dict[str, Any]] | None = None,
    *,
    verifier: _LocalAudioVerifier,
    request: Mapping[str, Any],
    job_dir: Path,
    audio_path: Path,
) -> Mapping[str, Any] | None:
    return _serve_exact_source_transcript_manifest(
        manifest_path=_exact_provider_manifest_path(
            job_dir, provider=provider, model=model
        ),
        audio_path=audio_path, request=request, source_media=verifier.source_media,
        source_media_sha256=verifier.source_sha256,
        source_stat_binding=verifier.source_stat_binding,
        expected_models={provider: model}, served_from_cache=True,
        current_provider_failures=failures,
    )


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
                # transcribe past the markers (1.12s target, 14
                # heard syllables) and poisons the judge. Context stays a
                # text-side input to the judge, never witness audio.
                # 裁剪边界量化到 100ms 网格（维护者 成本追问）：
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


def _serve_local_verdict_manifest(
    *,
    manifest_path: Path,
    audio_path: Path,
    request: Mapping[str, Any],
    request_sha256: str,
    verifier: _LocalAudioVerifier,
    witness_mode: bool,
    expected_prompt_identity_sha256: str | None,
) -> Mapping[str, Any] | None:
    if not manifest_path.is_file() or not audio_path.is_file():
        return None
    try:
        cached = json.loads(manifest_path.read_text(encoding="utf-8"))
        prompt_sha256 = str(cached.get("prompt_sha256") or "")
        prompt_artifact_valid = any(
            path.is_file() and _sha256(path) == prompt_sha256
            for path in manifest_path.parent.glob("prompt*")
        )
        if not (
            cached.get("schema_version") == "entity-audio-verdict-manifest.v1"
            and cached.get("request_sha256") == request_sha256
            and cached.get("request_payload_sha256")
            == _json_sha256(dict(request))
            and cached.get("source_media_sha256") == verifier.source_sha256
            and cached.get("audio_clip_sha256") == _sha256(audio_path)
            and cached.get("provider") == "agy"
            and cached.get("model") == verifier.model
            and prompt_artifact_valid
            and (
                not witness_mode
                or (
                    cached.get("witness_prompt_contract")
                    == WITNESS_PROMPT_CONTRACT
                    and cached.get("witness_prompt_identity_sha256")
                    == expected_prompt_identity_sha256
                )
            )
        ):
            return None
        verdict = cached.get("verdict")
        if isinstance(verdict, Mapping) and verdict.get("reason_code") in {
            "ENTITY_AUDIO_PROVIDER_FAILED",
            "ENTITY_VERIFIER_ERROR",
            "ENTITY_AUDIO_RESPONSE_INVALID",
            "ENTITY_AUDIO_CROP_FAILED",
        }:
            return None
        return verdict if isinstance(verdict, Mapping) else None
    except (OSError, ValueError, TypeError):
        return None


def _exact_provider_failure_receipt(
    request: Mapping[str, Any], provider_failures: list[Mapping[str, Any]]
) -> Mapping[str, Any]:
    if any(
        row.get("category") == "AGY_STALE_OUTPUT_CLEANUP_FAILED"
        for row in provider_failures
    ):
        return _uncertain(request, "EXACT_SOURCE_TRANSCRIPT_PROVIDER_RESULT_INVALID")
    attempts = normalize_exact_source_transcript_provider_attempts(
        provider_failures
    )
    if not attempts:
        return seal_exact_source_transcript_capability_unavailable(request=request)
    try:
        return seal_exact_source_transcript_provider_failure(
            request=request, provider_failures=attempts
        )
    except ValueError:
        return _uncertain(request, "EXACT_SOURCE_TRANSCRIPT_PROVIDER_RESULT_INVALID")


def _persist_exact_source_transcript(
    *,
    verifier: _LocalAudioVerifier,
    request: Mapping[str, Any],
    span: _PreparedAudioSpan,
    outcome: _EntityProviderOutcome,
    audio_path: Path,
) -> Mapping[str, Any]:
    try:
        provider_route = _build_exact_provider_route(
            provider=outcome.provider, model=outcome.model,
            audio_clip_sha256=_sha256(audio_path),
            accepted_key_tier=outcome.accepted_key_tier,
            accepted_key_ordinal=outcome.accepted_key_ordinal,
            configured_key_count=outcome.configured_key_count,
            paid_backup_policy=outcome.paid_policy_stamp,
            provider_failures=outcome.provider_failures,
        )
    except (TypeError, ValueError):
        return _uncertain(request, "EXACT_SOURCE_TRANSCRIPT_PROVIDER_ROUTE_INVALID")
    verdict = _seal_exact_provider_observation(
        request=request,
        observed=outcome.observed if isinstance(outcome.observed, Mapping) else {},
        source_media_sha256=verifier.source_sha256,
        audio_path=audio_path,
        provider=outcome.provider,
        model=outcome.model,
        prompt_path=outcome.prompt_path,
        response_path=outcome.response_path,
        timeline_binding=span.timeline_binding,
        key_tier=outcome.accepted_key_tier,
        provider_route=provider_route,
    )
    if verdict is None:
        return _uncertain(request, "EXACT_SOURCE_TRANSCRIPT_REPORT_INVALID")
    _store_exact_source_transcript_manifest(
        manifest_path=_exact_provider_manifest_path(
            audio_path.parent, provider=outcome.provider, model=outcome.model
        ),
        request=request,
        source_media_sha256=verifier.source_sha256,
        source_stat_binding=verifier.source_stat_binding,
        audio_path=audio_path,
        prompt_path=outcome.prompt_path,
        response_path=outcome.response_path,
        observation=verdict,
    )
    return verdict


def _verify_local_audio_request(
    *,
    verifier: _LocalAudioVerifier,
    request: Mapping[str, Any],
) -> Mapping[str, Any]:
    request_sha = str(request.get("request_sha256") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", request_sha):
        return _uncertain(request, "ENTITY_AUDIO_REQUEST_INVALID")
    witness_mode = request.get("schema_version") == WITNESS_REQUEST_SCHEMA
    exact_transcript_mode = request.get("schema_version") == EXACT_SOURCE_REQUEST_SCHEMA
    candidates = request.get("candidate_entities")
    if exact_transcript_mode:
        if not valid_exact_source_transcript_request(request):
            return _uncertain(request, "EXACT_SOURCE_TRANSCRIPT_REQUEST_INVALID")
        candidates = []
    elif witness_mode:
        # A dictation witness must not carry candidates at all — their mere
        # presence in the request would prime the transcription.
        forbidden_text_channels = {
            "candidate_entities", "current_cue", "proposed_cue",
            "matched_audio_text", "context_before", "context_after",
            "suspect", "replacement",
        }
        hint = request.get("syllable_count_hint")
        if (
            request.get("witness_protocol") != BLIND_PINYIN_PROTOCOL
            or (
                hint is not None
                and (
                    isinstance(hint, bool)
                    or not isinstance(hint, int)
                    or hint <= 0
                )
            )
        ):
            return _uncertain(request, "WITNESS_REQUEST_PROTOCOL_INVALID")
        if any(key in request for key in forbidden_text_channels):
            return _uncertain(request, "WITNESS_REQUEST_CARRIES_CANDIDATES")
        candidates = []
    elif not isinstance(candidates, list) or len(candidates) < 2:
        return _uncertain(request, "ENTITY_AUDIO_CANDIDATES_INVALID")
    context_mode = witness_mode or exact_transcript_mode or (
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
    span = _prepare_audio_span(
        request=request,
        context_mode=context_mode,
        source_media_timeline_offset_ms=source_media_timeline_offset_ms,
        source_duration_ms=verifier.source_duration_ms,
        witness_mode=witness_mode or exact_transcript_mode,
    )
    if span is None:
        return _uncertain(request, "ENTITY_AUDIO_SPAN_INVALID")
    acoustic_prompt_identity = _witness_cache_prompt_identity(
        span.observed_request, recording_date=verifier.recording_date
    ) if witness_mode else None
    cached_verdict = (
        _serve_exact_provider_cache(
            "agy", verifier.model, verifier=verifier, request=request,
            job_dir=job_dir, audio_path=audio_path,
        )
        if exact_transcript_mode
        else _serve_local_verdict_manifest(
            manifest_path=manifest_path,
            audio_path=audio_path,
            request=request,
            request_sha256=request_sha,
            verifier=verifier,
            witness_mode=witness_mode,
            expected_prompt_identity_sha256=acoustic_prompt_identity,
        )
    )
    if cached_verdict is not None:
        return cached_verdict
    crop_succeeded, crop_error = _crop_black_frame_audio(
        source_media=verifier.source_media,
        audio_path=audio_path,
        start_ms=span.crop_start_ms,
        end_ms=span.crop_end_ms,
    )
    if not crop_succeeded:
        return _uncertain(request, "ENTITY_AUDIO_CROP_FAILED", crop_error)

    acoustic_clip_sha = _sha256(audio_path) if witness_mode else None
    replay_exact_cache = partial(
        _serve_exact_provider_cache,
        verifier=verifier, request=request, job_dir=job_dir, audio_path=audio_path,
    )
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
        agy_quota_circuit=verifier.agy_quota_circuit,
        exact_cache_replay=replay_exact_cache if exact_transcript_mode else None,
    )
    acoustic_cache_hit = outcome.served_from_cache
    observed = outcome.observed
    if exact_transcript_mode and outcome.served_from_cache and (
        isinstance(observed, Mapping)
        and observed.get("schema_version") == EXACT_SOURCE_OBSERVATION_SCHEMA
    ):
        return observed
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
        if exact_transcript_mode:
            return _exact_provider_failure_receipt(request, provider_failures)
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
    if exact_transcript_mode:
        return _persist_exact_source_transcript(
            verifier=verifier, request=request, span=span, outcome=outcome,
            audio_path=audio_path,
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
                prompt_identity_sha256=_sha256(outcome.prompt_path),
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
            **_provider_artifact_bindings(outcome, _sha256),
            **(
                {
                    "witness_prompt_contract": WITNESS_PROMPT_CONTRACT,
                    "witness_prompt_identity_sha256": _sha256(outcome.prompt_path),
                }
                if witness_mode
                else {}
            ),
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
        **_provider_artifact_bindings(outcome, _sha256),
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
    source_stat_before = _source_stat_binding(source_media)
    source_sha256 = _sha256(source_media)
    source_stat_after = _source_stat_binding(source_media)
    if source_stat_before != source_stat_after:
        raise RuntimeError("source media stat binding drifted while hashing")
    binary = agy_gemini_client.resolve_local_agy_binary(agy_bin)
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
        source_stat_binding=source_stat_after,
        binary=binary,
        model=model,
        timeout=timeout,
        timely_context=timely,
    )


def _source_stat_binding(path: Path) -> tuple[int, int, int, int, int, int, int]:
    info = os.lstat(path)
    if not stat.S_ISREG(info.st_mode):
        raise RuntimeError("source media must be a regular non-symlink file")
    return (
        int(info.st_dev),
        int(info.st_ino),
        int(info.st_mode),
        int(info.st_uid),
        int(info.st_size),
        int(info.st_mtime_ns),
        int(info.st_ctime_ns),
    )
