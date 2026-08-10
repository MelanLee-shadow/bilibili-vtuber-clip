"""Independent AGY audio witness for foreign-script subtitle blocks.

The language-preservation and script-consistency audits fail closed when the
final subtitle carries foreign-language or Latin-phrase content the Chinese
ASR draft never witnessed.  Ivan 2026-07-19: foreign (e.g. Japanese)
transcription capability belongs to AGY, while CPA remains a text-only judge.
An independent AGY listen over the exact blocked cue interval is therefore an
acceptable machine witness.  The witness only supplies a candidate-blind
transcript; it never chooses between subtitle surfaces.  A mismatch is sent to
CPA, while an unparseable observation or provider failure stays retryable.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import subprocess
import unicodedata
from pathlib import Path
from typing import Any, Callable

# gemini_backup_policy 现在由 agy_gemini_client 调用；这里保留导入是因为单测
# 通过 fsw.gemini_backup_policy 打闸门 seam，而两边引用的是同一个模块对象。
from src.autoslice import (  # noqa: F401 - keeps the policy monkeypatch seam
    agy_gemini_client,
    gemini_backup_policy,
)
from src.autoslice.agy_lrc_alignment import (
    GEMINI_API_AUDIO_LRC_MODEL,
    _gemini_api_observe,
    _gemini_keys,
)
from src.autoslice.foreign_audio_witness_cache import (
    load_successful_observation,
    store_successful_observation,
    witness_identity,
)
from src.autoslice.foreign_closed_set_rebuild import (
    rebuild_foreign_closed_set,
)
from src.autoslice.jingting_chunker import parse_srt_cues
from src.autoslice.subtitle_fidelity import _JAPANESE_KANA_RX
from scripts.gemini_slice_jingting import (
    agy_subprocess_env,
    parse_timeout_seconds,
    strip_markdown_fence,
)

FOREIGN_WITNESS_SCHEMA = "foreign-span-audio-witness.v1"
CLUSTER_RETRANSCRIPTION_SCHEMA = "foreign-cluster-retranscription.v1"
LANGUAGE_WITNESSED_STATUS = "WITNESSED_FOREIGN_AUDIO_TRANSCRIPTION"
LANGUAGE_PRESERVATION_CPA_STATUS = "CPA_ADJUDICATED_FOREIGN_SPEAKER_AUDIO"
MIXED_PHRASE_WITNESSED_STATUS = "WITNESSED_MIXED_PHRASE_AUDIO"
MIXED_PHRASE_CPA_STATUS = "CPA_ADJUDICATED_MIXED_PHRASE_AUDIO"

_MIN_VERBATIM_SIMILARITY = 0.60
_MIN_KANA_SIMILARITY = 0.55
_SPAN_PAD_MS = 400
_ALLOWED_LANGUAGES = frozenset({"zh", "ja", "en", "mixed", "none"})
_KEEP_TEXT_RX = re.compile(r"[0-9A-Za-z㐀-鿿ぁ-ゖァ-ヺー]+")
_AGY_MODEL = os.environ.get("FOREIGN_WITNESS_AGY_MODEL", "Gemini 3.6 Flash (High)")
_AGY_TIMEOUT = os.environ.get("FOREIGN_WITNESS_AGY_TIMEOUT", "10m")
_AGY_WITNESS_ALGORITHM_ID = "agy-foreign-span-candidate-blind-v1"
_GEMINI_WITNESS_ALGORITHM_ID = (
    "gemini-api-foreign-span-candidate-blind-v1"
)

_PROMPT_TEMPLATE = """The black-frame input.mp4 is an untrusted live-stream span of {duration_ms} ms.
Transcribe EXACTLY what is audibly spoken, in the original spoken language
and native script (Japanese stays in kana/kanji, Chinese in hanzi, English
in Latin letters).  Do not translate, do not guess unheard words, and do
not describe non-speech sounds.  Write verdict.json as this JSON object only:
{{"audible_language": "zh"|"ja"|"en"|"mixed"|"none",
 "exact_transcript": "<verbatim transcript, empty when no speech>",
 "speaker_impression": "single_live_voice"|"media_playback"|"both"|"uncertain"}}"""


def _extract_span_audio(
    media_path: Path, start_ms: int, end_ms: int, output_path: Path
) -> None:
    span_start = max(0, int(start_ms) - _SPAN_PAD_MS)
    span_end = int(end_ms) + _SPAN_PAD_MS
    completed = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-ss", f"{span_start / 1000:.3f}", "-to", f"{span_end / 1000:.3f}",
            "-i", str(media_path),
            "-vn", "-ac", "1", "-ar", "16000", "-c:a", "libmp3lame", "-b:a", "64k",
            str(output_path),
        ],
        check=False, capture_output=True, text=True, timeout=300,
    )
    if completed.returncode != 0 or not output_path.is_file():
        raise RuntimeError(f"WITNESS_AUDIO_EXTRACTION_FAILED: {completed.stderr[-160:]}")


def _normalized(text: str) -> str:
    folded = unicodedata.normalize("NFKC", str(text or "")).casefold()
    return "".join(_KEEP_TEXT_RX.findall(folded))


def _kana_only(text: str) -> str:
    return "".join(_JAPANESE_KANA_RX.findall(str(text or "")))


def _similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def _parse_observation(raw: str) -> dict[str, str]:
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("witness observation is not an object")
    language = str(payload.get("audible_language") or "").strip().lower()
    if language not in _ALLOWED_LANGUAGES:
        raise ValueError(f"witness language {language!r} is not recognized")
    return {
        "audible_language": language,
        "exact_transcript": str(payload.get("exact_transcript") or ""),
        "speaker_impression": str(payload.get("speaker_impression") or "uncertain"),
    }


_is_quota_error = agy_gemini_client.is_quota_error


def _observe_with_key_ladder(
    *,
    audio_path: Path,
    prompt: str,
    observe: Callable[..., str],
) -> tuple[dict[str, str], str]:
    """Free keys in order, then the policy-gated paid key. Returns (obs, tier).

    Ivan 2026-07-20: a fully-429 free chain is deterministic quota exhaustion
    — the paid backup steps in the SAME round; the >=3 strikes gate applies
    only to non-quota failure classes.  That is this lane's ``quota_fastpath``
    knob on the shared ladder; the entity/LRC lanes keep the round-based form.
    """

    audio_sha = hashlib.sha256(audio_path.read_bytes()).hexdigest()
    failures: list[str] = []

    def observe_and_parse(key: str) -> dict[str, str]:
        return _parse_observation(observe(audio_path=audio_path, prompt=prompt, key=key))

    outcome = agy_gemini_client.run_gemini_key_ladder(
        item_key=audio_sha,
        observe=observe_and_parse,
        purpose="foreign_span_witness",
        record_failure=lambda failure: failures.append(failure.error_type),
        record_paid_skipped=(
            lambda _ordinal, _round, gate_reason: failures.append(
                f"PAID_BACKUP_SKIPPED:{gate_reason}"
            )
        ),
        quota_fastpath=True,
        quota_predicate=_is_quota_error,
        silent_when_paid_unconfigured=False,
        strike_on_empty_chain=True,
    )
    if outcome.accepted:
        return outcome.observed, outcome.accepted_key_tier
    raise RuntimeError("WITNESS_PROVIDERS_FAILED: " + ",".join(failures[-4:]) or "none")


def _observe_with_agy(
    *,
    audio_path: Path,
    prompt: str,
) -> dict[str, str]:
    """Run the only production audio-capable witness without candidate text."""

    job_dir = audio_path.parent / f"{audio_path.stem}.agy-job"
    job_dir.mkdir(parents=True, exist_ok=True)
    input_path = job_dir / "input.mp4"
    prompt_path = job_dir / "prompt.md"
    verdict_path = job_dir / "verdict.json"
    verdict_path.unlink(missing_ok=True)
    prompt_path.write_text(prompt, encoding="utf-8")
    try:
        duration = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", str(audio_path),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(
            f"AGY_FOREIGN_WITNESS_MEDIA_PROBE_FAILED:{type(exc).__name__}"
        ) from exc
    try:
        duration_s = max(0.1, float(duration.stdout.strip()))
    except (TypeError, ValueError):
        duration_s = 0.0
    if duration.returncode != 0 or duration_s <= 0:
        raise RuntimeError("AGY_FOREIGN_WITNESS_MEDIA_INVALID")
    try:
        rendered = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-t", f"{duration_s:.3f}", "-i",
                "color=c=black:s=320x240:r=10", "-i", str(audio_path),
                "-map", "0:v:0", "-map", "1:a:0", "-c:v", "libx264",
                "-preset", "veryfast", "-pix_fmt", "yuv420p", "-c:a", "aac",
                "-b:a", "128k", "-shortest", str(input_path),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=180,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(
            f"AGY_FOREIGN_WITNESS_MEDIA_RENDER_FAILED:{type(exc).__name__}"
        ) from exc
    if rendered.returncode != 0 or not input_path.is_file():
        raise RuntimeError(
            "AGY_FOREIGN_WITNESS_MEDIA_RENDER_FAILED: " + rendered.stderr[-160:]
        )
    run = agy_gemini_client.run_local_agy(
        agy_gemini_client.agy_argv(
            agy_gemini_client.resolve_local_agy_executable(),
            job_dir=job_dir,
            model=_AGY_MODEL,
            prompt=(
                "Open prompt.md with view_file and follow it exactly. Use only "
                "prompt.md and input.mp4. Write verdict.json in this directory. "
                "Do not use shell, terminal, browser, web, or search."
            ),
            print_timeout=_AGY_TIMEOUT,
        ),
        cwd=job_dir,
        env=agy_subprocess_env(),
        timeout=parse_timeout_seconds(_AGY_TIMEOUT) + 120,
    )
    if run.completed is None:
        # 缺席 vs 炸了：分类由统一客户端给，AGY_BINARY_ABSENT 让运维不再去查
        # 一台本来就不该装 AGY 的机器。下游照旧落 Gemini。
        raise RuntimeError(
            f"AGY_FOREIGN_WITNESS_SUBPROCESS_FAILED:{run.failure_category}:"
            f"{run.launch_error_type}"
        ) from run.launch_error
    completed = run.completed
    (job_dir / "agy.stdout").write_text(completed.stdout, encoding="utf-8")
    (job_dir / "agy.stderr").write_text(completed.stderr, encoding="utf-8")
    (job_dir / "agy.rc").write_text(str(completed.returncode) + "\n", encoding="utf-8")
    raw = (
        verdict_path.read_text(encoding="utf-8", errors="replace")
        if verdict_path.is_file()
        else completed.stdout
    )
    (job_dir / "verdict.raw.json").write_text(raw, encoding="utf-8")
    if completed.returncode != 0:
        raise RuntimeError(
            f"AGY_FOREIGN_WITNESS_FAILED:rc={completed.returncode}:"
            + completed.stderr[-160:]
        )
    try:
        return _parse_observation(strip_markdown_fence(raw))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"AGY_FOREIGN_WITNESS_INVALID_OUTPUT:{type(exc).__name__}"
        ) from exc


def _observe_audio(
    *,
    audio_path: Path,
    prompt: str,
    observe: Callable[..., str] | None,
) -> tuple[dict[str, str], str, str | None, dict[str, Any] | None]:
    """Prefer AGY, then use a hash-bound Gemini API audio witness."""

    if observe is not None:
        observation, key_tier = _observe_with_key_ladder(
            audio_path=audio_path,
            prompt=prompt,
            observe=observe,
        )
        return observation, "injected_test_observer", key_tier, None
    identity = witness_identity(
        audio_path=audio_path,
        prompt=prompt,
        model=_AGY_MODEL,
        algorithm_id=_AGY_WITNESS_ALGORITHM_ID,
    )
    cached, cache_evidence = load_successful_observation(identity=identity)
    if cached is not None:
        try:
            observation = _parse_observation(json.dumps(cached, ensure_ascii=False))
        except (TypeError, ValueError, json.JSONDecodeError):
            observation = None
        if observation is not None:
            return observation, "agy_success_cache", None, cache_evidence
    try:
        observation = _observe_with_agy(audio_path=audio_path, prompt=prompt)
    except Exception as agy_exc:
        if not _gemini_keys():
            raise
        fallback_identity = witness_identity(
            audio_path=audio_path,
            prompt=prompt,
            model=GEMINI_API_AUDIO_LRC_MODEL,
            algorithm_id=_GEMINI_WITNESS_ALGORITHM_ID,
        )
        cached, fallback_cache_evidence = load_successful_observation(
            identity=fallback_identity
        )
        if cached is not None:
            try:
                fallback_observation = _parse_observation(
                    json.dumps(cached, ensure_ascii=False)
                )
            except (TypeError, ValueError, json.JSONDecodeError):
                fallback_observation = None
            if fallback_observation is not None:
                return (
                    fallback_observation,
                    "gemini_api_success_cache",
                    None,
                    {
                        **fallback_cache_evidence,
                        "provider_fallback_used": True,
                        "requested_provider": "agy",
                        "agy_failure_category": _agy_failure_category(agy_exc),
                    },
                )
        fallback_observation, key_tier = _observe_with_key_ladder(
            audio_path=audio_path,
            prompt=prompt,
            observe=_gemini_api_observe,
        )
        fallback_cache_evidence = store_successful_observation(
            identity=fallback_identity,
            observation=fallback_observation,
            source_provenance={
                "kind": "live_accepted_gemini_api_audio",
                "provider_fallback_used": True,
                "requested_provider": "agy",
                "agy_failure_category": _agy_failure_category(agy_exc),
                "key_tier": key_tier,
            },
        )
        return (
            fallback_observation,
            "gemini_api",
            key_tier,
            {
                **fallback_cache_evidence,
                "provider_fallback_used": True,
                "requested_provider": "agy",
                "agy_failure_category": _agy_failure_category(agy_exc),
            },
        )
    cache_evidence = store_successful_observation(
        identity=identity,
        observation=observation,
        source_provenance={
            "kind": "live_accepted_agy_process",
            "process_returncode": 0,
        },
    )
    return observation, "agy", None, cache_evidence


def _agy_failure_category(exc: Exception) -> str:
    diagnostic = f"{type(exc).__name__}: {exc}".casefold()
    if any(token in diagnostic for token in ("quota", "429", "rate limit")):
        return "AGY_QUOTA_EXHAUSTED"
    if "timeout" in diagnostic or "timed out" in diagnostic:
        return "AGY_TIMEOUT"
    return "AGY_PROVIDER_FAILED"


def _witness_rows(
    *,
    media_path: Path,
    rows: list[dict[str, Any]],
    mode: str,
    out_root: Path,
    cid: str,
    observe: Callable[..., str] | None,
) -> list[dict[str, Any]]:
    audio_dir = out_root / f"{cid}.foreign-witness"
    audio_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    for row in rows:
        start_ms = row.get("start_ms")
        end_ms = row.get("end_ms")
        claim_text = str(row.get("attempted") or row.get("text") or "")
        result: dict[str, Any] = {
            "cue_index": row.get("cue_index"),
            "start_ms": start_ms,
            "end_ms": end_ms,
            "mode": mode,
            "witnessed": False,
        }
        results.append(result)
        if (
            not isinstance(start_ms, int)
            or not isinstance(end_ms, int)
            or start_ms >= end_ms
            or not claim_text.strip()
        ):
            result["failure"] = "ROW_NOT_ADDRESSABLE"
            continue
        audio_path = audio_dir / f"span_{start_ms}_{end_ms}.mp3"
        try:
            _extract_span_audio(media_path, start_ms, end_ms, audio_path)
            duration_ms = end_ms - start_ms + 2 * _SPAN_PAD_MS
            observation, provider, key_tier, cache_evidence = _observe_audio(
                audio_path=audio_path,
                prompt=_PROMPT_TEMPLATE.format(duration_ms=duration_ms),
                observe=observe,
            )
        except Exception as exc:
            result["failure"] = f"{type(exc).__name__}: {exc}"[:200]
            continue
        transcript = observation["exact_transcript"]
        result["audible_language"] = observation["audible_language"]
        result["exact_transcript"] = transcript[:500]
        result["speaker_impression"] = observation["speaker_impression"]
        result["provider"] = provider
        if cache_evidence is not None:
            result.update(cache_evidence)
        if key_tier is not None:
            result["key_tier"] = key_tier
        result["audio_sha256"] = hashlib.sha256(audio_path.read_bytes()).hexdigest()
        if mode == "kana":
            similarity = _similarity(_kana_only(transcript), _kana_only(claim_text))
            result["kana_similarity"] = round(similarity, 4)
            result["witnessed"] = (
                observation["audible_language"] in {"ja", "mixed"}
                and similarity >= _MIN_KANA_SIMILARITY
            )
        else:
            similarity = _similarity(_normalized(transcript), _normalized(claim_text))
            result["text_similarity"] = round(similarity, 4)
            result["witnessed"] = similarity >= _MIN_VERBATIM_SIMILARITY
    return results


def _persist_witness(out_root: Path, cid: str, kind: str, rows: list[dict[str, Any]]) -> None:
    path = out_root / f"{cid}.foreign-witness.json"
    document: dict[str, Any] = {"schema_version": FOREIGN_WITNESS_SCHEMA}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            document = loaded
    except (OSError, ValueError):
        pass
    document[kind] = rows
    path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def witness_language_preservation_audit(
    *,
    media_path: Path,
    audit: dict[str, Any],
    out_root: Path,
    cid: str,
    observe: Callable[..., str] | None = None,
) -> None:
    """Try to witness kana introductions by listening to their source spans."""

    if not str(audit.get("status", "")).startswith("BLOCKED_UNPROVEN_FOREIGN_"):
        return
    rows = audit.get("unproven_foreign_introductions") or []
    if not rows:
        return
    try:
        results = _witness_rows(
            media_path=media_path,
            rows=rows,
            mode="kana",
            out_root=out_root,
            cid=cid,
            observe=observe,
        )
    except Exception as exc:  # the witness lane must never crash the producer
        audit["audio_witness_error"] = f"{type(exc).__name__}: {exc}"[:200]
        return
    audit["audio_witness_rows"] = results
    _persist_witness(out_root, cid, "language_preservation", results)
    if results and all(row.get("witnessed") for row in results):
        audit["status"] = LANGUAGE_WITNESSED_STATUS


def adjudicate_language_preservation_audit(
    *,
    media_path: Path,
    srt_text: str,
    audit: dict[str, Any],
    out_root: Path,
    cid: str,
    llm_call: Callable[[str], str] | None,
    observe: Callable[..., str] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Let AGY witness introduced foreign speech and CPA own the final choice.

    The source-language guard detects kana introduced after the draft but is
    not a word-choice authority.  A close candidate-blind AGY transcription
    can deterministically witness the existing cue; every mismatch becomes a
    closed CURRENT/PROPOSED hearing where CPA may keep the contextual repair or
    select AGY's bounded transcript.  AGY never selects delivered text.
    """

    if not str(audit.get("status") or "").startswith(
        "BLOCKED_UNPROVEN_FOREIGN_"
    ):
        return srt_text, audit
    rows = [
        row
        for row in (audit.get("unproven_foreign_introductions") or [])
        if isinstance(row, dict)
    ]
    if not rows:
        return srt_text, audit
    try:
        results = _witness_rows(
            media_path=media_path,
            rows=rows,
            mode="kana",
            out_root=out_root,
            cid=cid,
            observe=observe,
        )
    except Exception as exc:
        audit["audio_witness_error"] = f"{type(exc).__name__}: {exc}"[:200]
        return srt_text, audit
    audit["audio_witness_rows"] = results
    _persist_witness(out_root, cid, "language_preservation", results)

    cues = parse_srt_cues(srt_text)
    cue_by_index = {index: cue for index, cue in enumerate(cues, start=1)}
    replacements: dict[int, str] = {}
    adjudication_rows: list[dict[str, Any]] = []
    all_resolved = len(results) == len(rows)
    cpa_hearing_count = 0

    from src.autoslice.acoustic_pinyin import text_pinyin_tokens as _pinyin_tokens
    from src.autoslice.acoustic_witness_adjudication import adjudicate_with_witness

    for result in results:
        cue_index = result.get("cue_index")
        cue = cue_by_index.get(cue_index)
        receipt: dict[str, Any] = {
            "cue_index": cue_index,
            "resolved": False,
            "decision_authority": None,
        }
        adjudication_rows.append(receipt)
        if cue is None:
            receipt["reason_code"] = "CUE_NOT_FOUND"
            all_resolved = False
            continue
        if result.get("witnessed") is True:
            receipt.update(
                {
                    "resolved": True,
                    "choice": "CURRENT",
                    "decision_authority": "VERBATIM_AUDIO_WITNESS",
                    "reason_code": "FOREIGN_READING_MATCH",
                }
            )
            continue
        transcript = " ".join(str(result.get("exact_transcript") or "").split())
        language = str(result.get("audible_language") or "")
        heard_tokens = _pinyin_tokens(transcript) if transcript else None
        if (
            not transcript
            or language == "none"
            or not heard_tokens
            or llm_call is None
        ):
            receipt["reason_code"] = (
                "CPA_JUDGE_UNAVAILABLE"
                if llm_call is None
                else "AUDIO_TRANSCRIPT_UNUSABLE"
            )
            all_resolved = False
            continue
        before = "\n".join(
            prior.text
            for prior in cues[max(0, int(cue_index) - 4) : int(cue_index) - 1]
        )
        after = "\n".join(
            following.text
            for following in cues[int(cue_index) : int(cue_index) + 3]
        )
        witness = {
            "schema_version": "subtitle-span-acoustic-witness.v1",
            "witness_protocol": "blind_pinyin",
            "status": "OBSERVED",
            "request_sha256": hashlib.sha256(
                json.dumps(
                    {
                        "audio_sha256": result.get("audio_sha256"),
                        "cue_index": cue_index,
                        "transcript": transcript,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest(),
            "target_audible": True,
            "heard_pinyin": " ".join(heard_tokens),
            "syllable_count": len(heard_tokens),
            "uncertain_positions": [],
            "confidence": 0.85,
        }
        check_request = {
            "current_cue": cue.text,
            "proposed_cue": transcript,
            "suspect": "",
            "replacement": "",
            "repair_class": "foreign_source_language_preservation",
            "candidate_provenance": {
                "kind": "bounded_candidate_blind_audio_transcript",
                "audio_sha256": result.get("audio_sha256"),
                "audible_language": language,
            },
            "orthography_authority": {
                "status": "PASS",
                "provenance_kind": "bounded_audio_transcript",
            },
            "reason": (
                "the source-language detector found newly introduced foreign "
                "script, but it has no word-choice authority; CPA must choose "
                "between the contextual current cue and candidate-blind AGY "
                "transcription"
            ),
            "context_before": before,
            "context_after": after,
        }
        cpa_hearing_count += 1
        repaired, policy_branch, adjudication = adjudicate_with_witness(
            check_request=check_request,
            witness=witness,
            llm_call=llm_call,
        )
        judge = adjudication.get("judge") or {}
        choice = judge.get("choice")
        proposed_text = transcript
        if policy_branch == "JUDGE_REJECTS_CLOSED_SET":
            rebuilt, rebuild_audit = rebuild_foreign_closed_set(
                current=cue.text,
                rejected=transcript,
                before=before,
                after=after,
                witness=witness,
                judge_reason=str(judge.get("reason") or ""),
                llm_call=llm_call,
            )
            receipt["proposal_rebuild"] = rebuild_audit
            if rebuilt is not None:
                proposed_text = rebuilt
                rebuilt_request = {
                    **check_request,
                    "proposed_cue": rebuilt,
                    "candidate_provenance": {
                        "kind": "cpa_context_proposal",
                        "mutation_authorized": False,
                        "prompt_sha256": rebuild_audit.get("prompt_sha256"),
                        "audio_sha256": result.get("audio_sha256"),
                    },
                }
                cpa_hearing_count += 1
                repaired, policy_branch, adjudication = adjudicate_with_witness(
                    check_request=rebuilt_request,
                    witness=witness,
                    llm_call=llm_call,
                )
                judge = adjudication.get("judge") or {}
                choice = judge.get("choice")
                receipt["rejected_proposed"] = transcript
        receipt.update(
            {
                "choice": choice,
                "policy_branch": policy_branch,
                "decision_authority": "CPA_JUDGE",
                "current": cue.text,
                "proposed": proposed_text,
                "adjudication": adjudication,
            }
        )
        if choice == "CURRENT" and judge.get("status") == "JUDGED":
            receipt["resolved"] = True
        elif (
            repaired
            and choice == "PROPOSED"
            and judge.get("status") == "JUDGED"
        ):
            replacements[int(cue_index)] = proposed_text
            receipt["resolved"] = True
        else:
            receipt["reason_code"] = "CPA_ADJUDICATION_DID_NOT_RESOLVE"
            all_resolved = False

    audit["cpa_adjudication_rows"] = adjudication_rows
    if not all_resolved:
        return srt_text, audit
    output = srt_text
    if replacements:
        rendered = []
        for index, cue in enumerate(cues, start=1):
            text = replacements.get(index, cue.text)
            rendered.append(
                f"{index}\n{_ms_to_srt_ts(cue.start_ms)} --> "
                f"{_ms_to_srt_ts(cue.end_ms)}\n{text}"
            )
        output = "\n\n".join(rendered) + "\n"
    audit["status"] = (
        LANGUAGE_PRESERVATION_CPA_STATUS
        if cpa_hearing_count
        else LANGUAGE_WITNESSED_STATUS
    )
    audit["decision_authority"] = (
        "CPA_JUDGE" if cpa_hearing_count else "VERBATIM_AUDIO_WITNESS"
    )
    audit["cpa_hearing_count"] = cpa_hearing_count
    audit["applied_count"] = len(replacements)
    audit["output_srt_sha256"] = hashlib.sha256(output.encode("utf-8")).hexdigest()
    return output, audit


def _ms_to_srt_ts(ms: int) -> str:
    seconds, millis = divmod(max(0, int(ms)), 1000)
    minutes, sec = divmod(seconds, 60)
    hours, minute = divmod(minutes, 60)
    return f"{hours:02d}:{minute:02d}:{sec:02d},{millis:03d}"


def retranscribe_foreign_script_cluster(
    *,
    media_path: Path,
    srt_text: str,
    audit: dict[str, Any],
    out_root: Path,
    cid: str,
    observe: Callable[..., str] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Re-transcribe Latin-salad cues of a foreign cluster from their audio.

    ``BLOCKED_MIXED_FOREIGN_SCRIPT_CLUSTER`` means a Japanese passage was
    decoded by the Chinese ASR into Latin-heavy garbage — the text itself is
    wrong, so a confirm-only witness cannot help.  Each clustered cue gets an
    independent Gemini listen over its exact interval; when the observation
    is Japanese (or mixed) speech, the heard native-script transcript
    replaces that cue's text.  Timeline is never touched, other cues are
    never touched, and every replacement carries its audio witness.  The
    caller re-audits the repaired SRT — an unrepaired cluster stays blocked.
    """

    repair_audit: dict[str, Any] = {
        "schema_version": CLUSTER_RETRANSCRIPTION_SCHEMA,
        "attempted_rows": [],
        "replaced_count": 0,
    }
    if audit.get("status") != "BLOCKED_MIXED_FOREIGN_SCRIPT_CLUSTER":
        return srt_text, repair_audit
    cluster_indexes = {
        row.get("cue_index")
        for row in audit.get("latin_heavy_cues") or []
        if isinstance(row, dict)
    }
    if not cluster_indexes:
        return srt_text, repair_audit
    audio_dir = out_root / f"{cid}.foreign-witness"
    audio_dir.mkdir(parents=True, exist_ok=True)
    cues = parse_srt_cues(srt_text)
    replacements: dict[int, str] = {}
    for index, cue in enumerate(cues, start=1):
        if index not in cluster_indexes:
            continue
        row: dict[str, Any] = {
            "cue_index": index,
            "start_ms": cue.start_ms,
            "end_ms": cue.end_ms,
            "original": cue.text,
            "replaced": False,
        }
        repair_audit["attempted_rows"].append(row)
        audio_path = audio_dir / f"cluster_{cue.start_ms}_{cue.end_ms}.mp3"
        try:
            _extract_span_audio(media_path, cue.start_ms, cue.end_ms, audio_path)
            duration_ms = cue.end_ms - cue.start_ms + 2 * _SPAN_PAD_MS
            observation, provider, key_tier, cache_evidence = _observe_audio(
                audio_path=audio_path,
                prompt=_PROMPT_TEMPLATE.format(duration_ms=duration_ms),
                observe=observe,
            )
        except Exception as exc:
            row["failure"] = f"{type(exc).__name__}: {exc}"[:200]
            continue
        transcript = " ".join(observation["exact_transcript"].split())
        row["audible_language"] = observation["audible_language"]
        row["speaker_impression"] = observation["speaker_impression"]
        row["provider"] = provider
        if cache_evidence is not None:
            row.update(cache_evidence)
        if key_tier is not None:
            row["key_tier"] = key_tier
        row["audio_sha256"] = hashlib.sha256(audio_path.read_bytes()).hexdigest()
        row["transcript"] = transcript
        japanese_shaped = bool(_kana_only(transcript)) or (
            observation["audible_language"] == "ja"
            and re.search(r"[㐀-鿿]", transcript) is not None
        )
        if (
            observation["audible_language"] in {"ja", "mixed"}
            and transcript
            and japanese_shaped
        ):
            replacements[index] = transcript
            row["replaced"] = True
        else:
            row["failure"] = "OBSERVATION_NOT_FOREIGN_SPEECH"
    repair_audit["replaced_count"] = len(replacements)
    _persist_witness(
        out_root, cid, "cluster_retranscription", repair_audit["attempted_rows"]
    )
    if not replacements:
        return srt_text, repair_audit
    rendered = []
    for index, cue in enumerate(cues, start=1):
        text = replacements.get(index, cue.text)
        rendered.append(
            f"{index}\n{_ms_to_srt_ts(cue.start_ms)} --> "
            f"{_ms_to_srt_ts(cue.end_ms)}\n{text}"
        )
    return "\n\n".join(rendered) + "\n", repair_audit


def witness_foreign_script_audit(
    *,
    media_path: Path,
    audit: dict[str, Any],
    out_root: Path,
    cid: str,
    observe: Callable[..., str] | None = None,
) -> None:
    """Try to witness mixed CJK/Latin cues as verbatim-audible speech.

    ``BLOCKED_MIXED_CJK_LATIN_PHRASE`` rows carry timeline addresses and the
    text is plausibly right, so hearing it verbatim is evidence.  The
    kana-cluster block is handled by ``retranscribe_foreign_script_cluster``
    instead — there the text itself is suspected wrong-language ASR.
    """

    if audit.get("status") != "BLOCKED_MIXED_CJK_LATIN_PHRASE":
        return
    rows = audit.get("mixed_cjk_latin_cues") or []
    if not rows:
        return
    try:
        results = _witness_rows(
            media_path=media_path,
            rows=rows,
            mode="verbatim",
            out_root=out_root,
            cid=cid,
            observe=observe,
        )
    except Exception as exc:
        audit["audio_witness_error"] = f"{type(exc).__name__}: {exc}"[:200]
        return
    audit["audio_witness_rows"] = results
    _persist_witness(out_root, cid, "foreign_script", results)
    if results and all(row.get("witnessed") for row in results):
        audit["status"] = MIXED_PHRASE_WITNESSED_STATUS


def adjudicate_foreign_script_audit(
    *,
    media_path: Path,
    srt_text: str,
    audit: dict[str, Any],
    out_root: Path,
    cid: str,
    llm_call: Callable[[str], str] | None,
    observe: Callable[..., str] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Let the audio lane propose and CPA finally judge mixed-script cues.

    A strict verbatim similarity miss is not itself a word-choice verdict:
    bounded audio transcription often returns only the English insertion while
    the SRT cue also contains a Chinese frame.  The candidate-blind transcript
    therefore becomes PROPOSED in a closed CURRENT/PROPOSED CPA hearing.  CPA
    may keep the existing code-switch or select the fresh transcript; only a
    typed JUDGED receipt resolves the gate.
    """

    if audit.get("status") != "BLOCKED_MIXED_CJK_LATIN_PHRASE":
        return srt_text, audit
    rows = [
        row
        for row in (audit.get("mixed_cjk_latin_cues") or [])
        if isinstance(row, dict)
    ]
    if not rows:
        return srt_text, audit
    try:
        results = _witness_rows(
            media_path=media_path,
            rows=rows,
            mode="verbatim",
            out_root=out_root,
            cid=cid,
            observe=observe,
        )
    except Exception as exc:
        audit["audio_witness_error"] = f"{type(exc).__name__}: {exc}"[:200]
        return srt_text, audit
    audit["audio_witness_rows"] = results
    _persist_witness(out_root, cid, "foreign_script", results)

    cues = parse_srt_cues(srt_text)
    cue_by_index = {index: cue for index, cue in enumerate(cues, start=1)}
    replacements: dict[int, str] = {}
    adjudication_rows: list[dict[str, Any]] = []
    all_resolved = len(results) == len(rows)
    cpa_hearing_count = 0

    from src.autoslice.acoustic_pinyin import text_pinyin_tokens as _pinyin_tokens
    from src.autoslice.acoustic_witness_adjudication import adjudicate_with_witness

    for result in results:
        cue_index = result.get("cue_index")
        cue = cue_by_index.get(cue_index)
        receipt: dict[str, Any] = {
            "cue_index": cue_index,
            "resolved": False,
            "decision_authority": None,
        }
        adjudication_rows.append(receipt)
        if cue is None:
            receipt["reason_code"] = "CUE_NOT_FOUND"
            all_resolved = False
            continue
        if result.get("witnessed") is True:
            receipt.update(
                {
                    "resolved": True,
                    "choice": "CURRENT",
                    "decision_authority": "VERBATIM_AUDIO_WITNESS",
                    "reason_code": "FULL_CUE_VERBATIM_MATCH",
                }
            )
            continue
        transcript = " ".join(str(result.get("exact_transcript") or "").split())
        language = str(result.get("audible_language") or "")
        heard_tokens = _pinyin_tokens(transcript) if transcript else None
        if (
            not transcript
            or language == "none"
            or not heard_tokens
            or llm_call is None
        ):
            receipt["reason_code"] = (
                "CPA_JUDGE_UNAVAILABLE"
                if llm_call is None
                else "AUDIO_TRANSCRIPT_UNUSABLE"
            )
            all_resolved = False
            continue
        before = "\n".join(
            prior.text
            for prior in cues[max(0, int(cue_index) - 4) : int(cue_index) - 1]
        )
        after = "\n".join(
            following.text
            for following in cues[int(cue_index) : int(cue_index) + 3]
        )
        witness = {
            "schema_version": "subtitle-span-acoustic-witness.v1",
            "witness_protocol": "blind_pinyin",
            "status": "OBSERVED",
            "request_sha256": hashlib.sha256(
                json.dumps(
                    {
                        "audio_sha256": result.get("audio_sha256"),
                        "cue_index": cue_index,
                        "transcript": transcript,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest(),
            "target_audible": True,
            "heard_pinyin": " ".join(heard_tokens),
            "syllable_count": len(heard_tokens),
            "uncertain_positions": [],
            "confidence": 0.85,
        }
        check_request = {
            "current_cue": cue.text,
            "proposed_cue": transcript,
            "suspect": "",
            "replacement": "",
            "repair_class": "foreign_script_retranscription",
            "candidate_provenance": {
                "kind": "bounded_candidate_blind_audio_transcript",
                "audio_sha256": result.get("audio_sha256"),
                "audible_language": language,
            },
            "orthography_authority": {
                "status": "PASS",
                "provenance_kind": "bounded_audio_transcript",
            },
            "reason": (
                "mixed CJK/Latin cue failed strict full-cue verbatim similarity; "
                "CPA must choose between the current cue and candidate-blind "
                "bounded audio transcription"
            ),
            "context_before": before,
            "context_after": after,
        }
        cpa_hearing_count += 1
        repaired, policy_branch, adjudication = adjudicate_with_witness(
            check_request=check_request,
            witness=witness,
            llm_call=llm_call,
        )
        judge = adjudication.get("judge") or {}
        choice = judge.get("choice")
        proposed_text = transcript
        if policy_branch == "JUDGE_REJECTS_CLOSED_SET":
            rebuilt, rebuild_audit = rebuild_foreign_closed_set(
                current=cue.text,
                rejected=transcript,
                before=before,
                after=after,
                witness=witness,
                judge_reason=str(judge.get("reason") or ""),
                llm_call=llm_call,
            )
            receipt["proposal_rebuild"] = rebuild_audit
            if rebuilt is not None:
                proposed_text = rebuilt
                rebuilt_request = {
                    **check_request,
                    "proposed_cue": rebuilt,
                    "candidate_provenance": {
                        "kind": "cpa_context_proposal",
                        "mutation_authorized": False,
                        "prompt_sha256": rebuild_audit.get("prompt_sha256"),
                        "audio_sha256": result.get("audio_sha256"),
                    },
                }
                cpa_hearing_count += 1
                repaired, policy_branch, adjudication = adjudicate_with_witness(
                    check_request=rebuilt_request,
                    witness=witness,
                    llm_call=llm_call,
                )
                judge = adjudication.get("judge") or {}
                choice = judge.get("choice")
                receipt["rejected_proposed"] = transcript
        receipt.update(
            {
                "choice": choice,
                "policy_branch": policy_branch,
                "decision_authority": "CPA_JUDGE",
                "current": cue.text,
                "proposed": proposed_text,
                "adjudication": adjudication,
            }
        )
        if choice == "CURRENT" and judge.get("status") == "JUDGED":
            receipt["resolved"] = True
        elif (
            repaired
            and choice == "PROPOSED"
            and judge.get("status") == "JUDGED"
        ):
            replacements[int(cue_index)] = proposed_text
            receipt["resolved"] = True
        else:
            receipt["reason_code"] = "CPA_ADJUDICATION_DID_NOT_RESOLVE"
            all_resolved = False

    audit["cpa_adjudication_rows"] = adjudication_rows
    if not all_resolved:
        return srt_text, audit
    output = srt_text
    if replacements:
        rendered = []
        for index, cue in enumerate(cues, start=1):
            text = replacements.get(index, cue.text)
            rendered.append(
                f"{index}\n{_ms_to_srt_ts(cue.start_ms)} --> "
                f"{_ms_to_srt_ts(cue.end_ms)}\n{text}"
            )
        output = "\n\n".join(rendered) + "\n"
    audit["status"] = (
        MIXED_PHRASE_CPA_STATUS
        if cpa_hearing_count
        else MIXED_PHRASE_WITNESSED_STATUS
    )
    audit["decision_authority"] = (
        "CPA_JUDGE" if cpa_hearing_count else "VERBATIM_AUDIO_WITNESS"
    )
    audit["cpa_hearing_count"] = cpa_hearing_count
    audit["applied_count"] = len(replacements)
    audit["output_srt_sha256"] = hashlib.sha256(output.encode("utf-8")).hexdigest()
    return output, audit
