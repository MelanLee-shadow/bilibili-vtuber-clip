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
import subprocess
from typing import Any, Callable, Mapping

from scripts.gemini_slice_jingting import (
    agy_subprocess_env,
    parse_timeout_seconds,
    strip_markdown_fence,
    timely_terms_context,
)


ENTITY_AUDIO_MODEL = "Gemini 3.5 Flash (High)"
ENTITY_AUDIO_TIMEOUT = "10m"


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
) -> str:
    neutral_candidates = sorted(candidates, key=lambda row: str(row.get("canonical") or "").lower())
    if sentence_mode:
        return f"""# Raw-audio spoken-sentence forced choice

Use only `input.mp4` in this job directory. Its frames are deliberately black:
there is no viewer chat, subtitle, title card, or other visual text to copy.
Listen to the complete audio several times and decide which candidate sentence
is actually spoken (the host may be reading viewer chat aloud). Do not infer
the answer from which sentence would make more sense.

Recording date: {recording_date}
Candidate sentences (neutral list):
{json.dumps(neutral_candidates, ensure_ascii=False, indent=2, sort_keys=True)}

{timely_context or 'No active date-bounded timely-term snapshot.'}

Judge ONLY by the syllables you hear; incompatible syllables always lose.
Report the syllables you actually hear before the choice.

Write `verdict.json` as JSON only:
{{
  "schema_version": "entity-audio-observation.v1",
  "status": "RESOLVED" or "UNCERTAIN",
  "canonical_entity": "one exact candidate sentence above, verbatim, or null",
  "heard_syllables": "literal syllables/phonetic observation",
  "confidence": 0.0,
  "reason": "short acoustic explanation"
}}

Use RESOLVED only when one candidate is acoustically clear with confidence at
least 0.80. Otherwise use UNCERTAIN. No markdown fences, no other files, no
shell, terminal, browser, web, or search.
"""
    return f"""# Raw-audio proper-name forced choice

Use only `input.mp4` in this job directory. Its frames are deliberately black:
there is no viewer chat, subtitle, title card, or other visual text to copy.
Listen to the complete audio several times and decide which candidate name is
actually spoken. Do not infer the answer from what would make sense.

Recording date: {recording_date}
Candidate entities (neutral list; aliases/readings are spelling aids only):
{json.dumps(neutral_candidates, ensure_ascii=False, indent=2, sort_keys=True)}

{timely_context or 'No active date-bounded timely-term snapshot.'}

Recency and franchise context may change prior probability, but incompatible
syllables always win. In particular, a current title and an older title can both
be valid candidates. Report the syllables you actually hear before the choice.

Write `verdict.json` as JSON only:
{{
  "schema_version": "entity-audio-observation.v1",
  "status": "RESOLVED" or "UNCERTAIN",
  "canonical_entity": "one exact canonical above, or null",
  "heard_syllables": "literal syllables/phonetic observation",
  "confidence": 0.0,
  "reason": "short acoustic explanation"
}}

Use RESOLVED only when one candidate is acoustically clear with confidence at
least 0.80. Otherwise use UNCERTAIN. No markdown fences, no other files, no
shell, terminal, browser, web, or search.
"""


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
                    return cached["verdict"]
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
        )
        prompt_path = job_dir / "prompt.md"
        prompt_path.write_text(prompt, encoding="utf-8")
        short_prompt = (
            "Open prompt.md with view_file and follow it exactly. Use only prompt.md and input.mp4. "
            "Write verdict.json in this directory. Do not use shell, terminal, browser, or web."
        )
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
            return _uncertain(request, "ENTITY_AUDIO_PROVIDER_FAILED", str(exc))
        (job_dir / "agy.stdout").write_text(completed.stdout, encoding="utf-8")
        (job_dir / "agy.stderr").write_text(completed.stderr, encoding="utf-8")
        verdict_path = job_dir / "verdict.json"
        raw_response = (
            verdict_path.read_text(encoding="utf-8", errors="replace")
            if verdict_path.is_file()
            else completed.stdout
        )
        response_path = job_dir / "verdict.raw.json"
        response_path.write_text(raw_response, encoding="utf-8")
        if completed.returncode != 0:
            return _uncertain(request, "ENTITY_AUDIO_PROVIDER_FAILED", completed.stderr)
        try:
            observed = json.loads(strip_markdown_fence(raw_response))
        except (TypeError, ValueError) as exc:
            return _uncertain(request, "ENTITY_AUDIO_RESPONSE_INVALID", str(exc))
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
                "model": model,
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
            "model": model,
            "verdict": verdict,
        }
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return verdict

    return verify
