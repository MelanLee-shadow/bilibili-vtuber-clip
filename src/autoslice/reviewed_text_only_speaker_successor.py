"""Fail-closed successor for a sealed text-only reviewed-baseline replay.

It is deliberately *not* a speaker classifier.  It can only rebind an old
READY speaker artifact when a reviewed baseline proves that its cue grid is
unchanged and specifies every text delta.  Labels and decision metadata are
copied byte-for-byte in meaning, never inferred.
"""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Mapping

from scripts.apply_speaker_turn_overrides import Cue, sha256_file, write_ass, write_srt
from scripts.apply_subtitle_text_overrides import parse_srt
from src.autoslice.speaker_common import SPEAKER_FINALIZATION_SCHEMA


class ReviewedTextOnlySpeakerSuccessorError(RuntimeError):
    """The old speaker evidence cannot be safely rebound."""


_SHA = re.compile(r"(?:sha256:)?([0-9a-f]{64})\Z")
_LABEL = re.compile(r"^\[([^\]]+)\]\s*(.+)\Z")
_SUCCESSOR_SCHEMA = "reviewed-baseline-text-only-speaker-successor.v1"


def _fail(code: str) -> None:
    raise ReviewedTextOnlySpeakerSuccessorError(
        f"REPLAY_TEXT_ONLY_SPEAKER_SUCCESSOR_{code}"
    )


def _digest(value: object, *, code: str) -> str:
    match = _SHA.fullmatch(str(value or ""))
    if match is None:
        _fail(code)
    return "sha256:" + match.group(1)


def _binding(path: Path, expected: object, *, code: str) -> str:
    if path.is_symlink() or not path.is_file():
        _fail(code)
    actual = "sha256:" + sha256_file(path)
    if actual != _digest(expected, code=code):
        _fail(code)
    return actual


def _json(path: Path, *, code: str) -> dict[str, object]:
    if path.is_symlink() or not path.is_file():
        _fail(code)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        _fail(code)
    if not isinstance(value, dict):
        _fail(code)
    return value


def _path(value: object, *, code: str) -> Path:
    if not isinstance(value, str) or not value:
        _fail(code)
    path = Path(value)
    if not path.is_absolute():
        _fail(code)
    return path


def _ledger_deltas(path: Path, *, candidate_id: str, old_sha: str, new_sha: str) -> dict[int, str]:
    ledger = _json(path, code="LEDGER_INVALID")
    if ledger.get("candidate_id") != candidate_id or _digest(
        ledger.get("pipeline_srt_sha256"), code="LEDGER_INVALID"
    ) != old_sha:
        _fail("LEDGER_INVALID")
    rows = ledger.get("cue_decisions")
    if not isinstance(rows, list) or not rows:
        _fail("LEDGER_INVALID")
    result: dict[int, str] = {}
    for index, raw in enumerate(rows, start=1):
        if not isinstance(raw, Mapping) or raw.get("cue") != index:
            _fail("LEDGER_INVALID")
        disposition = raw.get("disposition")
        if disposition == "OPERATOR_UNCHANGED_FREEZE":
            continue
        if disposition != "OPERATOR_EXACT_TEXT" or raw.get("decision_authority") != "LEDGER_OPERATOR_AUTHORITY":
            _fail("LEDGER_SCOPE_INVALID")
        text = raw.get("release_text")
        if not isinstance(text, str) or not text.strip() or index in result:
            _fail("LEDGER_SCOPE_INVALID")
        result[index] = text.strip()
    if not result:
        _fail("LEDGER_SCOPE_INVALID")
    return result


def materialize_text_only_speaker_successor(
    *, candidate_id: str, old_record: Mapping[str, object], old_record_sha256: str,
    old_manifest_path: Path, old_manifest_sha256: str, reviewed_baseline_path: Path,
    reviewed_baseline_sha256: str, ledger_path: Path, ledger_sha256: str,
    new_plain_srt: Path, new_media: Path, expected_media_sha256: str,
    output_srt: Path, output_ass: Path, output_manifest: Path,
) -> dict[str, object]:
    """Write a successor only when every non-text speaker fact is unchanged."""

    if old_record.get("speaker_mode") != "auto":
        _fail("OLD_RECORD_MODE_INVALID")
    embedded = old_record.get("speaker_finalization")
    if not isinstance(embedded, Mapping):
        _fail("OLD_RECORD_MANIFEST_MISSING")
    if _digest(old_record.get("speaker_finalization_manifest_sha256"), code="OLD_RECORD_MANIFEST_MISSING") != old_manifest_sha256:
        _fail("OLD_RECORD_MANIFEST_MISMATCH")
    old_manifest = _json(old_manifest_path, code="OLD_MANIFEST_INVALID")
    if "sha256:" + sha256_file(old_manifest_path) != old_manifest_sha256 or dict(embedded) != old_manifest:
        _fail("OLD_RECORD_MANIFEST_MISMATCH")
    if old_manifest.get("schema_version") != SPEAKER_FINALIZATION_SCHEMA or old_manifest.get("status") != "READY" or old_manifest.get("production_ready") is not True:
        _fail("OLD_MANIFEST_NOT_READY")
    old_plain = _path(old_manifest.get("text_final_srt"), code="OLD_PLAIN_MISSING")
    old_speaker = _path(old_manifest.get("output_review_srt"), code="OLD_SPEAKER_MISSING")
    old_plain_sha = _binding(old_plain, old_manifest.get("text_final_srt_sha256"), code="OLD_PLAIN_BINDING")
    old_speaker_sha = _binding(old_speaker, old_manifest.get("output_review_srt_sha256"), code="OLD_SPEAKER_BINDING")
    new_plain_sha = "sha256:" + sha256_file(new_plain_srt)
    if _binding(reviewed_baseline_path, reviewed_baseline_sha256, code="BASELINE_BINDING") != new_plain_sha:
        _fail("BASELINE_NEW_TEXT_MISMATCH")
    _binding(ledger_path, ledger_sha256, code="LEDGER_BINDING")
    if "sha256:" + sha256_file(new_media) != expected_media_sha256 or _digest(old_manifest.get("source_media_sha256"), code="MEDIA_BINDING") != expected_media_sha256:
        _fail("MEDIA_BINDING")

    old_cues, new_cues, speaker_cues = parse_srt(old_plain), parse_srt(new_plain_srt), parse_srt(old_speaker)
    if not (len(old_cues) == len(new_cues) == len(speaker_cues)):
        _fail("CUE_COUNT_DRIFT")
    deltas = _ledger_deltas(ledger_path, candidate_id=candidate_id, old_sha=old_plain_sha, new_sha=new_plain_sha)
    decisions = old_manifest.get("final_decisions")
    if not isinstance(decisions, list) or len(decisions) != len(old_cues):
        _fail("DECISIONS_INVALID")
    successor_cues: list[Cue] = []
    successor_decisions: list[dict[str, object]] = []
    for index, (old, new, labelled, raw) in enumerate(zip(old_cues, new_cues, speaker_cues, decisions), start=1):
        if not isinstance(raw, Mapping) or old.source_index != index or new.source_index != index or labelled.source_index != index:
            _fail("CUE_INDEX_DRIFT")
        if (old.start, old.end) != (new.start, new.end) or (old.start, old.end) != (labelled.start, labelled.end):
            _fail("CUE_TIMING_DRIFT")
        label = _LABEL.fullmatch(labelled.text)
        if label is None or raw.get("source_index") != index or raw.get("speaker") != label.group(1) or raw.get("text") != old.text:
            _fail("SPEAKER_LABEL_OR_DECISION_DRIFT")
        expected = deltas.get(index, old.text)
        if new.text != expected or (new.text != old.text) != (index in deltas):
            _fail("TEXT_DELTA_OUTSIDE_LEDGER")
        copied = dict(raw)
        copied["text"] = new.text
        successor_decisions.append(copied)
        successor_cues.append(Cue(index, new.start, new.end, label.group(1), new.text, str(raw.get("decision_source") or ""), raw.get("authority"), raw.get("note"), raw.get("speaker_detail"), int(raw.get("layer") or 0), str(raw.get("placement") or "main")))

    write_srt(successor_cues, output_srt)
    write_ass(successor_cues, output_ass, show_speaker_labels=False)
    result = deepcopy(old_manifest)
    result.update({
        "status": "READY", "production_ready": True, "source_media": str(new_media),
        "source_media_sha256": expected_media_sha256, "text_final_srt": str(new_plain_srt),
        "text_final_srt_sha256": new_plain_sha.removeprefix("sha256:"),
        "output_review_srt": str(output_srt), "output_review_srt_sha256": sha256_file(output_srt),
        "output_ass": str(output_ass), "output_ass_sha256": sha256_file(output_ass),
        "final_decisions": successor_decisions,
        "reviewed_baseline_text_only_successor": {
            "schema_version": _SUCCESSOR_SCHEMA, "candidate_id": candidate_id,
            "old_record_sha256": old_record_sha256, "old_speaker_manifest_sha256": old_manifest_sha256,
            "old_plain_srt_sha256": old_plain_sha, "old_speaker_srt_sha256": old_speaker_sha,
            "reviewed_baseline_sha256": reviewed_baseline_sha256, "operator_ledger_sha256": ledger_sha256,
            "new_plain_srt_sha256": new_plain_sha, "new_speaker_srt_sha256": "sha256:" + sha256_file(output_srt),
            "speaker_labels_inherited": True, "speaker_label_mutation_authorized": False,
            "changed_cue_indices": sorted(deltas),
        },
    })
    output_manifest.parent.mkdir(parents=True, exist_ok=True)
    output_manifest.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result
