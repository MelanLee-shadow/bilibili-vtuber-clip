"""Sealed no-provider source-fact bridge for C3 only.

The historical KEEP receipt remains diagnostic evidence.  This module never
relabels it as a current CPA/provider result: it emits a distinct terminal
preservation receipt after replaying the sealed C3 speaker truth.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from src.autoslice.repository_asset_authority import require_repository_asset_authority

CANDIDATE_ID = "auto_220021_561_670"
DECISION = "OPERATOR_FASTLANE_TERMINAL_SOURCE_FACT_PRESERVATION"
SCHEMA = "fastlane-c3-terminal-source-fact-preservation-authority.v1"
REVIEW_SCHEMA = "lidousha-source-fact-review.v1"
ASSET = Path("assets/lidousha/fastlane_c3_terminal_source_fact_preservation/auto_220021_561_670.v1.json")


class C3TerminalSourceFactPreservationError(ValueError):
    pass


def _canonical(value: object) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _text(value: str) -> str:
    return _bytes(value.encode())


def _sha(value: object, *, label: str) -> str:
    answer = str(value or "")
    if not answer.startswith("sha256:") or len(answer) != 71 or any(c not in "0123456789abcdef" for c in answer[7:]):
        raise C3TerminalSourceFactPreservationError(f"{label} hash is invalid")
    return answer


def load_authority(*, repo_root: Path) -> dict[str, Any]:
    path = repo_root / ASSET
    try:
        payload = path.read_bytes()
        require_repository_asset_authority(repo_root=repo_root, relative_path=ASSET, observed_bytes=payload)
        document = json.loads(payload.decode())
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise C3TerminalSourceFactPreservationError("C3 terminal source-fact authority is unavailable or unsealed") from exc
    if not isinstance(document, dict):
        raise C3TerminalSourceFactPreservationError("C3 terminal source-fact authority is invalid")
    body = dict(document)
    claimed = _sha(body.pop("authority_sha256", None), label="authority")
    if _canonical(body) != claimed:
        raise C3TerminalSourceFactPreservationError("C3 terminal source-fact authority hash drifts")
    document["authority_sha256"] = claimed
    return document


def _validate(authority: Mapping[str, Any], *, title: str, selection_hook: str, selection_scorecard: object, clip_context_prompt: str, subtitle_text: str, speaker_srt: bytes, speaker_finalization: bytes, speaker_evidence: object) -> dict[str, Any]:
    expected = {"schema_version", "candidate_id", "line947", "surfaces", "historical_keep_receipt", "direct_host_cues", "authority_sha256"}
    if set(authority) != expected or authority.get("schema_version") != SCHEMA or authority.get("candidate_id") != CANDIDATE_ID:
        raise C3TerminalSourceFactPreservationError("C3 terminal authority schema/candidate drifts")
    line = authority["line947"]
    surfaces = authority["surfaces"]
    old = authority["historical_keep_receipt"]
    rows = authority["direct_host_cues"]
    if not all(isinstance(x, Mapping) for x in (line, surfaces, old)) or not isinstance(rows, list):
        raise C3TerminalSourceFactPreservationError("C3 terminal authority sections are invalid")
    if line != {"raw_event_line_sha256": "sha256:e64d4409aaf36193c27f3d67cd8e3fae69a6d3ae543a29a6c26f57c77d61c2aa", "message_content_sha256": "sha256:0e0e69e54fc06c88296536c6dfbca947181170873529c5de508a2af39aa93f6b", "durable_ruling_sha256": "sha256:29bc6e523645ecfbd9dbf15a5bea912dd741a6dced0b54ca41d4f906cebfde49"}:
        raise C3TerminalSourceFactPreservationError("C3 line947 layer drifts")
    required = {"title", "title_sha256", "selection_hook", "selection_hook_sha256", "selection_scorecard_sha256", "clip_context_prompt_sha256", "text_srt_sha256", "speaker_srt_sha256", "speaker_finalization_sha256", "speaker_evidence_sha256"}
    if set(surfaces) != required or title != surfaces.get("title") or selection_hook != surfaces.get("selection_hook") or _text(title) != _sha(surfaces.get("title_sha256"), label="title") or _text(selection_hook) != _sha(surfaces.get("selection_hook_sha256"), label="hook") or _canonical(selection_scorecard) != _sha(surfaces.get("selection_scorecard_sha256"), label="scorecard") or _text(clip_context_prompt) != _sha(surfaces.get("clip_context_prompt_sha256"), label="clip context") or _bytes(subtitle_text.encode()) != _sha(surfaces.get("text_srt_sha256"), label="text SRT") or _bytes(speaker_srt) != _sha(surfaces.get("speaker_srt_sha256"), label="speaker SRT") or _bytes(speaker_finalization) != _sha(surfaces.get("speaker_finalization_sha256"), label="speaker finalization") or _canonical(speaker_evidence) != _sha(surfaces.get("speaker_evidence_sha256"), label="speaker evidence"):
        raise C3TerminalSourceFactPreservationError("C3 terminal current surface drifts")
    old_body = dict(old)
    receipt_sha = _sha(old_body.pop("receipt_sha256", None), label="historical KEEP receipt")
    if _canonical(old_body) != receipt_sha or old.get("schema_version") != REVIEW_SCHEMA or old.get("status") != "PASS" or old.get("decision") != "KEEP" or old.get("final_title") != title or old.get("final_selection_hook") != selection_hook:
        raise C3TerminalSourceFactPreservationError("C3 historical KEEP receipt drifts")
    if [row.get("cue") for row in rows] != [1, 2, 4, 5, 6, 8, 9, 10, 11]:
        raise C3TerminalSourceFactPreservationError("C3 direct host cue selection drifts")
    from src.autoslice.jingting_chunker import parse_srt_cues
    from src.autoslice.speaker_common import HOST_SPEAKER
    cues = parse_srt_cues(subtitle_text)
    speaker_rows = parse_srt_cues(speaker_srt.decode())
    for row in rows:
        cue = cues[int(row["cue"]) - 1]
        speaker = speaker_rows[int(row["cue"]) - 1]
        if set(row) != {"cue", "start_ms", "end_ms", "text", "speaker"} or (cue.start_ms, cue.end_ms, cue.text) != (row["start_ms"], row["end_ms"], row["text"]) or speaker.text != f"[{HOST_SPEAKER}] {cue.text}" or row["speaker"] != HOST_SPEAKER:
            raise C3TerminalSourceFactPreservationError("C3 direct host cue evidence drifts")
    return {"schema_version": "fastlane-c3-terminal-source-fact-preservation-consumption.v1", "status": "VALID", "candidate_id": CANDIDATE_ID, "authority_sha256": authority["authority_sha256"], "historical_keep_receipt_sha256": receipt_sha, "text_srt_sha256": surfaces["text_srt_sha256"], "speaker_srt_sha256": surfaces["speaker_srt_sha256"], "speaker_finalization_sha256": surfaces["speaker_finalization_sha256"], "speaker_evidence_sha256": surfaces["speaker_evidence_sha256"]}


def build_review(*, repo_root: Path, title: str, selection_hook: str, selection_scorecard: object, clip_context_prompt: str, subtitle_text: str, speaker_srt: bytes, speaker_finalization: bytes, speaker_evidence: object) -> dict[str, Any]:
    authority = load_authority(repo_root=repo_root)
    consumption = _validate(authority, title=title, selection_hook=selection_hook, selection_scorecard=selection_scorecard, clip_context_prompt=clip_context_prompt, subtitle_text=subtitle_text, speaker_srt=speaker_srt, speaker_finalization=speaker_finalization, speaker_evidence=speaker_evidence)
    review = {"schema_version": REVIEW_SCHEMA, "status": "PASS", "decision": DECISION, "original_selection_hook": selection_hook, "original_title": title, "final_selection_hook": selection_hook, "final_title": title, "historical_keep_receipt": authority["historical_keep_receipt"], "fastlane_terminal_source_fact_preservation": consumption}
    review["receipt_sha256"] = _canonical(review)
    return review


def validate_review(review: object, *, repo_root: Path, title: str, selection_hook: str, selection_scorecard: object, clip_context_prompt: str, final_reviewed_srt_path: Path, speaker_evidence: object) -> bool:
    if not isinstance(review, Mapping):
        return False
    try:
        root = final_reviewed_srt_path.parent.resolve(strict=True)
        if final_reviewed_srt_path.is_symlink() or not final_reviewed_srt_path.is_file(): return False
        stem = final_reviewed_srt_path.name.removesuffix(".srt")
        paths = [root / f"{stem}.speaker.srt", root / f"{stem}.speaker-finalization.json", root / f"{stem}.line947-speaker-authority.json"]
        if any(x.is_symlink() or not x.is_file() for x in paths): return False
        from src.autoslice.fastlane_c3_speaker_authority import ASSET as UPSTREAM, load_c3_line947_authority
        load_c3_line947_authority(repo_root=repo_root)
        if paths[2].read_bytes() != (repo_root / UPSTREAM).read_bytes(): return False
        expected = build_review(repo_root=repo_root, title=title, selection_hook=selection_hook, selection_scorecard=selection_scorecard, clip_context_prompt=clip_context_prompt, subtitle_text=final_reviewed_srt_path.read_text(), speaker_srt=paths[0].read_bytes(), speaker_finalization=paths[1].read_bytes(), speaker_evidence=speaker_evidence)
    except (OSError, ValueError): return False
    return dict(review) == expected
