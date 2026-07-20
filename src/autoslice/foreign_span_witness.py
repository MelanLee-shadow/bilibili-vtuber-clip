"""Independent Gemini audio witness for foreign-script subtitle blocks.

The language-preservation and script-consistency audits fail closed when the
final subtitle carries foreign-language or Latin-phrase content the Chinese
ASR draft never witnessed.  Ivan 2026-07-19: foreign (e.g. Japanese)
transcription capability IS the same Gemini chain the pipeline already uses
(AGY subscription → free keys → paid key, one model family), so an
independent listen over the exact blocked cue interval is an acceptable
machine witness.  The witness only confirms evidence — it never rewrites
text.  A mismatch, an unparseable observation, or a provider failure keeps
the block for the correction lane or a reviewed override.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
import subprocess
import unicodedata
from pathlib import Path
from typing import Any, Callable

from src.autoslice import gemini_backup_policy
from src.autoslice.agy_lrc_alignment import _gemini_api_observe, _gemini_keys
from src.autoslice.jingting_chunker import parse_srt_cues
from src.autoslice.subtitle_fidelity import _JAPANESE_KANA_RX

FOREIGN_WITNESS_SCHEMA = "foreign-span-audio-witness.v1"
CLUSTER_RETRANSCRIPTION_SCHEMA = "foreign-cluster-retranscription.v1"
LANGUAGE_WITNESSED_STATUS = "WITNESSED_FOREIGN_AUDIO_TRANSCRIPTION"
MIXED_PHRASE_WITNESSED_STATUS = "WITNESSED_MIXED_PHRASE_AUDIO"

_MIN_VERBATIM_SIMILARITY = 0.60
_MIN_KANA_SIMILARITY = 0.55
_SPAN_PAD_MS = 400
_ALLOWED_LANGUAGES = frozenset({"zh", "ja", "en", "mixed", "none"})
_KEEP_TEXT_RX = re.compile(r"[0-9A-Za-z㐀-鿿ぁ-ゖァ-ヺー]+")

_PROMPT_TEMPLATE = """The attached audio is an untrusted live-stream span of {duration_ms} ms.
Transcribe EXACTLY what is audibly spoken, in the original spoken language
and native script (Japanese stays in kana/kanji, Chinese in hanzi, English
in Latin letters).  Do not translate, do not guess unheard words, and do
not describe non-speech sounds.  Respond ONLY with JSON:
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


def _is_quota_error(exc: Exception) -> bool:
    """HTTP 429 / quota-class detection across urllib+requests error shapes."""

    status = getattr(exc, "code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    if status == 429:
        return True
    text = f"{type(exc).__name__}: {exc}".lower()
    return "429" in text or "resource_exhausted" in text or "quota" in text


def _observe_with_key_ladder(
    *,
    audio_path: Path,
    prompt: str,
    observe: Callable[..., str],
) -> tuple[dict[str, str], str]:
    """Free keys in order, then the policy-gated paid key. Returns (obs, tier)."""

    audio_sha = hashlib.sha256(audio_path.read_bytes()).hexdigest()
    failures: list[str] = []
    quota_flags: list[bool] = []
    for key in _gemini_keys():
        try:
            return (
                _parse_observation(observe(audio_path=audio_path, prompt=prompt, key=key)),
                gemini_backup_policy.FREE_KEY_TIER,
            )
        except Exception as exc:  # each key is an independent failover lane
            failures.append(type(exc).__name__)
            quota_flags.append(_is_quota_error(exc))
    gemini_backup_policy.record_free_chain_failure(audio_sha)
    if quota_flags and all(quota_flags):
        # Ivan 2026-07-20: a fully-429 free chain is deterministic quota
        # exhaustion — the paid backup steps in the SAME round; the >=3
        # strikes gate applies only to non-quota failure classes.
        allowed, gate_reason = gemini_backup_policy.paid_attempt_allowed(
            audio_sha,
            prior_strikes=gemini_backup_policy.MIN_FREE_CHAIN_STRIKES,
        )
        gate_reason = f"QUOTA_FASTPATH:{gate_reason}"
    else:
        allowed, gate_reason = gemini_backup_policy.paid_attempt_allowed(audio_sha)
    if allowed:
        try:
            observation = _parse_observation(
                observe(
                    audio_path=audio_path,
                    prompt=prompt,
                    key=str(gemini_backup_policy.paid_backup_key()),
                )
            )
            gemini_backup_policy.record_paid_use(
                audio_sha, purpose="foreign_span_witness"
            )
            return observation, gemini_backup_policy.PAID_KEY_TIER
        except Exception as exc:
            failures.append(type(exc).__name__)
    else:
        failures.append(f"PAID_BACKUP_SKIPPED:{gate_reason}")
    raise RuntimeError("WITNESS_PROVIDERS_FAILED: " + ",".join(failures[-4:]) or "none")


def _witness_rows(
    *,
    media_path: Path,
    rows: list[dict[str, Any]],
    mode: str,
    out_root: Path,
    cid: str,
    observe: Callable[..., str] | None,
) -> list[dict[str, Any]]:
    observe_fn = observe if observe is not None else _gemini_api_observe
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
            observation, key_tier = _observe_with_key_ladder(
                audio_path=audio_path,
                prompt=_PROMPT_TEMPLATE.format(duration_ms=duration_ms),
                observe=observe_fn,
            )
        except Exception as exc:
            result["failure"] = f"{type(exc).__name__}: {exc}"[:200]
            continue
        transcript = observation["exact_transcript"]
        result["audible_language"] = observation["audible_language"]
        result["speaker_impression"] = observation["speaker_impression"]
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
    observe_fn = observe if observe is not None else _gemini_api_observe
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
            observation, key_tier = _observe_with_key_ladder(
                audio_path=audio_path,
                prompt=_PROMPT_TEMPLATE.format(duration_ms=duration_ms),
                observe=observe_fn,
            )
        except Exception as exc:
            row["failure"] = f"{type(exc).__name__}: {exc}"[:200]
            continue
        transcript = " ".join(observation["exact_transcript"].split())
        row["audible_language"] = observation["audible_language"]
        row["speaker_impression"] = observation["speaker_impression"]
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
