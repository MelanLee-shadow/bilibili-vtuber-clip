"""Fail-closed successor for a sealed text-only reviewed-baseline replay.

It is deliberately *not* a speaker classifier.  It can only rebind an old
READY speaker artifact when hash-bound diagnostic and release grids, the
operator ledger, and the truth diff prove every retained cue and explicit
drop. Labels and decision metadata are copied, never inferred.
"""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Callable, Mapping, Protocol

from scripts.apply_speaker_turn_overrides import Cue, sha256_file, write_ass, write_srt
from scripts.apply_subtitle_text_overrides import parse_srt
from src.autoslice.speaker_common import SPEAKER_FINALIZATION_SCHEMA


class ReviewedTextOnlySpeakerSuccessorError(RuntimeError):
    """The old speaker evidence cannot be safely rebound."""


class _RegularBinding(Protocol):
    path: Path
    sha256: str


_SHA = re.compile(r"(?:sha256:)?([0-9a-f]{64})\Z")
_LABEL = re.compile(r"^\[([^\]]+)\]\s*(.+)\Z")
_SUCCESSOR_SCHEMA = "reviewed-baseline-text-only-speaker-successor.v1"


def build_text_only_speaker_successor_fields(
    *,
    finalizer: Callable[..., object] | None,
    original_speaker_finalizer: object,
    candidate_id: str,
    record: Mapping[str, object],
    old_record_sha256: str,
    baseline_config: Mapping[str, object],
    baseline_manifest_parent: Path,
    reviewed_baseline_path: Path,
    reviewed_baseline_sha256: str,
    expected_media_sha256: str,
    regular_binding: Callable[..., _RegularBinding],
    replay_error: type[Exception],
    error_factory: Callable[[str], Exception],
) -> dict[str, object]:
    """Build the private-only speaker successor adapter for replay.

    A canonical finalizer supplied by a test owns its own adapter shape and is
    therefore left untouched.  The production finalizer receives one typed
    wrapper that only replaces a provider's speaker guess with a sealed
    text-only successor; every binding and failure code stays in that lane.
    """

    if finalizer is not None:
        return {}
    if not callable(original_speaker_finalizer):
        raise error_factory("REPLAY_FINALIZER_ADAPTERS_INVALID")

    from src.autoslice import speaker_guess

    def replay_speaker_finalizer(**kwargs: object) -> dict[str, object]:
        generated = original_speaker_finalizer(**kwargs)
        if generated.get("status") != speaker_guess.SPEAKER_GUESS_STATUS:
            return generated
        try:
            old_speaker_path_raw = record.get("speaker_finalization_manifest_path")
            if not isinstance(old_speaker_path_raw, str):
                raise ReviewedTextOnlySpeakerSuccessorError("OLD_MANIFEST_MISSING")
            old_speaker_binding = regular_binding(
                Path(old_speaker_path_raw), label="OLD_SPEAKER_MANIFEST"
            )
            lanes = baseline_config.get("operator_truth_lanes")
            ledger_descriptor = lanes.get("decision_ledger") if isinstance(lanes, Mapping) else None
            if not isinstance(ledger_descriptor, Mapping) or not isinstance(ledger_descriptor.get("path"), str):
                raise ReviewedTextOnlySpeakerSuccessorError("LEDGER_MISSING")
            ownership = baseline_config.get("operator_text_full_ownership")
            if not isinstance(ownership, Mapping) or ownership.get("speaker_authority") != "NOT_CLAIMED_TEXT_ONLY":
                raise ReviewedTextOnlySpeakerSuccessorError("SPEAKER_AUTHORITY_SCOPE_INVALID")
            ledger_binding = regular_binding(
                baseline_manifest_parent / str(ledger_descriptor["path"]),
                label="OPERATOR_DECISION_LEDGER",
            )
            diagnostic_descriptor = lanes.get("pipeline_diagnostic") if isinstance(lanes, Mapping) else None
            diff_descriptor = lanes.get("diff_receipt") if isinstance(lanes, Mapping) else None
            if (
                not isinstance(diagnostic_descriptor, Mapping)
                or not isinstance(diff_descriptor, Mapping)
                or not isinstance(diagnostic_descriptor.get("path"), str)
                or not isinstance(diff_descriptor.get("path"), str)
            ):
                raise ReviewedTextOnlySpeakerSuccessorError("TRUTH_LANES_MISSING")
            diagnostic_binding = regular_binding(
                baseline_manifest_parent / str(diagnostic_descriptor["path"]),
                label="PIPELINE_DIAGNOSTIC",
            )
            diff_binding = regular_binding(
                baseline_manifest_parent / str(diff_descriptor["path"]),
                label="OPERATOR_TRUTH_DIFF",
            )
            descriptor_sha = str(ledger_descriptor.get("sha256") or "").removeprefix("sha256:")
            ownership_sha = str(ownership.get("decision_ledger_sha256") or "").removeprefix("sha256:")
            diagnostic_sha = str(diagnostic_descriptor.get("sha256") or "").removeprefix("sha256:")
            diff_sha = str(diff_descriptor.get("sha256") or "").removeprefix("sha256:")
            release_descriptor = lanes.get("release_truth") if isinstance(lanes, Mapping) else None
            release_sha = (
                str(release_descriptor.get("srt_sha256") or "").removeprefix("sha256:")
                if isinstance(release_descriptor, Mapping) else ""
            )
            baseline_sha = str(reviewed_baseline_sha256 or "").removeprefix("sha256:")
            if (
                len(descriptor_sha) != 64
                or ledger_binding.sha256.removeprefix("sha256:") != descriptor_sha
                or ownership_sha != descriptor_sha
                or len(diagnostic_sha) != 64
                or diagnostic_binding.sha256.removeprefix("sha256:") != diagnostic_sha
                or len(diff_sha) != 64
                or diff_binding.sha256.removeprefix("sha256:") != diff_sha
                or len(release_sha) != 64
                or release_sha != baseline_sha
                or str(ownership.get("pipeline_srt_sha256") or "").removeprefix("sha256:") != diagnostic_sha
                or str(ownership.get("diagnostic_diff_sha256") or "").removeprefix("sha256:") != diff_sha
                or str(ownership.get("baseline_sha256") or "").removeprefix("sha256:") != baseline_sha
            ):
                raise ReviewedTextOnlySpeakerSuccessorError("TRUTH_LANE_BINDING_INVALID")
            return materialize_text_only_speaker_successor(
                candidate_id=candidate_id, old_record=record,
                old_record_sha256=old_record_sha256,
                old_manifest_path=old_speaker_binding.path,
                old_manifest_sha256=old_speaker_binding.sha256,
                old_diagnostic_path=diagnostic_binding.path,
                old_diagnostic_sha256=diagnostic_binding.sha256,
                reviewed_baseline_path=reviewed_baseline_path,
                reviewed_baseline_sha256=reviewed_baseline_sha256,
                ledger_path=ledger_binding.path, ledger_sha256=ledger_binding.sha256,
                truth_diff_path=diff_binding.path, truth_diff_sha256=diff_binding.sha256,
                new_plain_srt=Path(str(kwargs["text_srt_path"])),
                new_media=Path(str(kwargs["media_path"])),
                expected_media_sha256=expected_media_sha256,
                output_srt=Path(str(kwargs["output_srt_path"])),
                output_ass=Path(str(kwargs["output_ass_path"])),
                output_manifest=Path(str(kwargs["output_manifest_path"])),
            )
        except (ReviewedTextOnlySpeakerSuccessorError, replay_error) as exc:
            # Do not let the source-fact provider see guessed speaker evidence.
            # The code is deliberately closed and replay-specific so operations
            # can distinguish this from title or provider failures.
            raise error_factory(
                "REPLAY_FROZEN_TITLE_AUTHORITY_DRIFT_"
                "SOURCE_FACT_REVIEW_SPEAKER_GUESS_REQUIRES_HUMAN_REVIEW"
            ) from exc

    return {"run_speaker_finalization": replay_speaker_finalizer}


def _fail(code: str) -> None:
    raise ReviewedTextOnlySpeakerSuccessorError(
        f"REPLAY_TEXT_ONLY_SPEAKER_SUCCESSOR_{code}"
    )


def _digest(value: object, *, code: str) -> str:
    match = _SHA.fullmatch(str(value or ""))
    if match is None:
        _fail(code)
    return "sha256:" + match.group(1)


def _raw_digest(value: object, *, code: str) -> str:
    return _digest(value, code=code).removeprefix("sha256:")


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


def _sealed_release_mapping(
    *, candidate_id: str, old_cues: list[Cue], release_cues: list[Cue],
    old_sha: str, release_sha: str, ledger_path: Path, ledger_sha: str,
    truth_diff_path: Path, truth_diff_sha: str,
) -> tuple[list[tuple[int, int]], list[int], dict[int, str]]:
    """Return the only permitted diagnostic-to-release cue map.

    The v3 registry normally validates this contract at load time.  This
    adapter repeats the material facts because it is explicitly rebinding old
    speaker evidence and must not trust an already-loaded configuration.
    """

    ledger = _json(ledger_path, code="LEDGER_INVALID")
    # The exact diff bytes are part of this successor proof, not an incidental
    # input to an earlier registry load.
    _binding(truth_diff_path, truth_diff_sha, code="TRUTH_DIFF_BINDING")
    diff = _json(truth_diff_path, code="TRUTH_DIFF_INVALID")
    rows = ledger.get("cue_decisions")
    diff_rows = diff.get("rows")
    authority = ledger.get("operator_authority")
    if (
        ledger.get("schema_version") != "operator-reviewed-subtitle-decisions.v3"
        or ledger.get("candidate_id") != candidate_id
        or ledger.get("report_scope") != "EXHAUSTIVE"
        or _digest(ledger.get("pipeline_srt_sha256"), code="LEDGER_INVALID") != old_sha
        or not isinstance(authority, Mapping)
        or authority.get("kind") != "IVAN_OPERATOR"
        or not isinstance(authority.get("evidence_ref"), str)
        or not authority["evidence_ref"].strip()
        or not isinstance(rows, list)
        or not isinstance(diff_rows, list)
        or len(rows) != len(old_cues)
        or len(diff_rows) != len(old_cues)
        or diff.get("schema_version") != "operator-reviewed-subtitle-truth-diff.v2"
        or diff.get("candidate_id") != candidate_id
        or _digest(diff.get("pipeline_srt_sha256"), code="TRUTH_DIFF_INVALID") != old_sha
        or _digest(diff.get("release_truth_srt_sha256"), code="TRUTH_DIFF_INVALID") != release_sha
        or _digest(diff.get("decision_ledger_sha256"), code="TRUTH_DIFF_INVALID") != ledger_sha
    ):
        _fail("SEALED_TRUTH_CONTRACT_INVALID")

    mapping: list[tuple[int, int]] = []
    dropped: list[int] = []
    deltas: dict[int, str] = {}
    release_cursor = 0
    for ordinal, (old, ledger_row, diff_row) in enumerate(zip(old_cues, rows, diff_rows, strict=True), start=1):
        if not isinstance(ledger_row, Mapping) or not isinstance(diff_row, Mapping) or old.source_index != ordinal:
            _fail("SEALED_TRUTH_CONTRACT_INVALID")
        disposition = ledger_row.get("disposition")
        if ledger_row.get("cue") != ordinal:
            _fail("SEALED_TRUTH_CONTRACT_INVALID")
        expected: dict[str, object] = {
            "cue": ordinal, "source_index": str(old.source_index),
            "start_ms": _srt_ms(old.start), "end_ms": _srt_ms(old.end),
            "pipeline_text": old.text.strip(), "disposition": disposition,
        }
        if disposition == "OPERATOR_DROP":
            if (
                set(ledger_row) != {"cue", "disposition", "decision_authority", "drop_reason"}
                or ledger_row.get("decision_authority") != "LEDGER_OPERATOR_AUTHORITY"
                or not isinstance(ledger_row.get("drop_reason"), str)
                or not ledger_row["drop_reason"].strip()
            ):
                _fail("SEALED_DROP_INVALID")
            expected.update({
                "release_cue_index": None, "release_truth_text": None,
                "decision_authority": dict(authority), "drop_reason": ledger_row["drop_reason"].strip(),
            })
            dropped.append(ordinal)
        else:
            if release_cursor >= len(release_cues):
                _fail("RELEASE_GRID_DRIFT")
            release = release_cues[release_cursor]
            release_index = release_cursor + 1
            if (
                release.source_index != release_index
                or (release.start, release.end) != (old.start, old.end)
                or not release.text.strip()
            ):
                _fail("RELEASE_GRID_DRIFT")
            expected.update({"release_cue_index": release_index, "release_truth_text": release.text.strip()})
            if disposition == "OPERATOR_UNCHANGED_FREEZE":
                if set(ledger_row) != {"cue", "disposition"} or release.text.strip() != old.text.strip():
                    _fail("SEALED_TRUTH_CONTRACT_INVALID")
            elif disposition == "OPERATOR_EXACT_TEXT":
                if (
                    set(ledger_row) != {"cue", "disposition", "release_text", "decision_authority"}
                    or ledger_row.get("decision_authority") != "LEDGER_OPERATOR_AUTHORITY"
                    or ledger_row.get("release_text") != release.text.strip()
                ):
                    _fail("SEALED_TEXT_DELTA_INVALID")
                expected["decision_authority"] = dict(authority)
                deltas[ordinal] = release.text.strip()
            else:
                _fail("SEALED_TRUTH_CONTRACT_INVALID")
            mapping.append((ordinal, release_index))
            release_cursor += 1
        if dict(diff_row) != expected:
            _fail("TRUTH_DIFF_DRIFT")
    if (
        release_cursor != len(release_cues)
        or len({new for _, new in mapping}) != len(mapping)
        or [new for _, new in mapping] != list(range(1, len(mapping) + 1))
        or not (deltas or dropped)
    ):
        _fail("NON_BIJECTIVE_RELEASE_MAP")
    return mapping, dropped, deltas


def _srt_ms(value: str) -> int:
    try:
        hh, mm, rest = value.split(":")
        ss, msec = rest.split(",")
        return ((int(hh) * 60 + int(mm)) * 60 + int(ss)) * 1000 + int(msec)
    except (ValueError, AttributeError):
        _fail("CUE_TIMING_DRIFT")


def _validate_old_speaker_against_diagnostic(
    *, old_cues: list[Cue], speaker_cues: list[Cue], decisions: object,
) -> list[Mapping[str, object]]:
    """Verify every old speaker row before any DROP filtering occurs."""

    if not isinstance(decisions, list) or len(decisions) != len(old_cues):
        _fail("DECISIONS_INVALID")
    if len(speaker_cues) != len(old_cues):
        _fail("CUE_COUNT_DRIFT")
    verified: list[Mapping[str, object]] = []
    for index, (old, labelled, raw) in enumerate(zip(old_cues, speaker_cues, decisions, strict=True), start=1):
        if (
            not isinstance(raw, Mapping)
            or old.source_index != index
            or labelled.source_index != index
            or (old.start, old.end) != (labelled.start, labelled.end)
        ):
            _fail("CUE_INDEX_OR_TIMING_DRIFT")
        label = _LABEL.fullmatch(labelled.text)
        if label is None or label.group(2) != old.text or (
            raw.get("source_index"), raw.get("start"), raw.get("end"),
            raw.get("speaker"), raw.get("text"), raw.get("layer"), raw.get("placement"),
        ) != (index, old.start, old.end, label.group(1), old.text, 0, "main"):
            _fail("SPEAKER_LABEL_OR_DECISION_DRIFT")
        if (
            not isinstance(raw.get("decision_source"), str)
            or not raw["decision_source"]
            or raw.get("authority") is not None and not isinstance(raw.get("authority"), (str, Mapping))
            or raw.get("note") is not None and not isinstance(raw.get("note"), str)
            or raw.get("speaker_detail") is not None and not isinstance(raw.get("speaker_detail"), str)
        ):
            _fail("SPEAKER_DECISION_METADATA_DRIFT")
        verified.append(raw)
    return verified


def materialize_text_only_speaker_successor(
    *, candidate_id: str, old_record: Mapping[str, object], old_record_sha256: str,
    old_manifest_path: Path, old_manifest_sha256: str, old_diagnostic_path: Path,
    old_diagnostic_sha256: str, reviewed_baseline_path: Path,
    reviewed_baseline_sha256: str, ledger_path: Path, ledger_sha256: str,
    truth_diff_path: Path, truth_diff_sha256: str,
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
    # The old path is historical metadata only.  It may point at an obsolete
    # private stage, so never read it as evidence; the sealed diagnostic must
    # instead match the old manifest's declared text hash.
    _path(old_manifest.get("text_final_srt"), code="OLD_PLAIN_MISSING")
    old_speaker = _path(old_manifest.get("output_review_srt"), code="OLD_SPEAKER_MISSING")
    old_plain_sha = _binding(
        old_diagnostic_path, old_manifest.get("text_final_srt_sha256"),
        code="OLD_DIAGNOSTIC_MANIFEST_BINDING",
    )
    if _binding(old_diagnostic_path, old_diagnostic_sha256, code="DIAGNOSTIC_BINDING") != old_plain_sha:
        _fail("OLD_DIAGNOSTIC_PLAIN_MISMATCH")
    old_speaker_sha = _binding(old_speaker, old_manifest.get("output_review_srt_sha256"), code="OLD_SPEAKER_BINDING")
    new_plain_sha = "sha256:" + sha256_file(new_plain_srt)
    if _binding(reviewed_baseline_path, reviewed_baseline_sha256, code="BASELINE_BINDING") != new_plain_sha:
        _fail("BASELINE_NEW_TEXT_MISMATCH")
    _binding(ledger_path, ledger_sha256, code="LEDGER_BINDING")
    _binding(truth_diff_path, truth_diff_sha256, code="TRUTH_DIFF_BINDING")
    expected_media = _raw_digest(expected_media_sha256, code="MEDIA_BINDING")
    if sha256_file(new_media) != expected_media or _raw_digest(
        old_manifest.get("source_media_sha256"), code="MEDIA_BINDING"
    ) != expected_media:
        _fail("MEDIA_BINDING")

    old_cues, new_cues, speaker_cues = parse_srt(old_diagnostic_path), parse_srt(new_plain_srt), parse_srt(old_speaker)
    if len(old_cues) != len(speaker_cues) or (
        old_manifest.get("source_cue_count"), old_manifest.get("output_cue_count")
    ) != (len(old_cues), len(speaker_cues)):
        _fail("CUE_COUNT_DRIFT")
    mapping, dropped, deltas = _sealed_release_mapping(
        candidate_id=candidate_id, old_cues=old_cues, release_cues=new_cues,
        old_sha=old_plain_sha, release_sha=new_plain_sha, ledger_path=ledger_path,
        ledger_sha=ledger_sha256, truth_diff_path=truth_diff_path,
        truth_diff_sha=truth_diff_sha256,
    )
    decisions = _validate_old_speaker_against_diagnostic(
        old_cues=old_cues, speaker_cues=speaker_cues,
        decisions=old_manifest.get("final_decisions"),
    )
    successor_cues: list[Cue] = []
    successor_decisions: list[dict[str, object]] = []
    for old_index, release_index in mapping:
        old, new, labelled, raw = old_cues[old_index - 1], new_cues[release_index - 1], speaker_cues[old_index - 1], decisions[old_index - 1]
        if old.source_index != old_index or new.source_index != release_index or labelled.source_index != old_index:
            _fail("CUE_INDEX_DRIFT")
        if (old.start, old.end) != (new.start, new.end):
            _fail("CUE_TIMING_DRIFT")
        label = _LABEL.fullmatch(labelled.text)
        assert label is not None  # already proved for every old row
        expected = deltas.get(old_index, old.text)
        if new.text != expected or (new.text != old.text) != (old_index in deltas):
            _fail("TEXT_DELTA_OUTSIDE_LEDGER")
        copied = dict(raw)
        copied["source_index"] = release_index
        copied["text"] = new.text
        successor_decisions.append(copied)
        successor_cues.append(Cue(release_index, new.start, new.end, label.group(1), new.text, str(raw.get("decision_source") or ""), raw.get("authority"), raw.get("note"), raw.get("speaker_detail"), int(raw.get("layer") or 0), str(raw.get("placement") or "main")))

    write_srt(successor_cues, output_srt)
    write_ass(successor_cues, output_ass, show_speaker_labels=False)
    result = deepcopy(old_manifest)
    result.update({
        "status": "READY", "production_ready": True, "source_media": str(new_media),
        "source_media_sha256": expected_media, "text_final_srt": str(new_plain_srt),
        "text_final_srt_sha256": new_plain_sha.removeprefix("sha256:"),
        "output_review_srt": str(output_srt), "output_review_srt_sha256": sha256_file(output_srt),
        "output_ass": str(output_ass), "output_ass_sha256": sha256_file(output_ass),
        "source_cue_count": len(successor_cues), "output_cue_count": len(successor_cues),
        "final_decisions": successor_decisions,
        "reviewed_baseline_text_only_successor": {
            "schema_version": _SUCCESSOR_SCHEMA, "candidate_id": candidate_id,
            "old_record_sha256": old_record_sha256, "old_speaker_manifest_sha256": old_manifest_sha256,
            "old_plain_srt_sha256": old_plain_sha, "old_diagnostic_sha256": old_diagnostic_sha256,
            "old_speaker_srt_sha256": old_speaker_sha,
            "reviewed_baseline_sha256": reviewed_baseline_sha256, "operator_ledger_sha256": ledger_sha256,
            "operator_truth_diff_sha256": truth_diff_sha256,
            "new_plain_srt_sha256": new_plain_sha, "new_speaker_srt_sha256": "sha256:" + sha256_file(output_srt),
            "speaker_labels_inherited": True, "speaker_label_mutation_authorized": False,
            "changed_cue_indices": sorted(deltas),
            "old_to_release_index_map": [
                {"old_source_index": old_index, "release_source_index": release_index}
                for old_index, release_index in mapping
            ],
            "dropped_old_source_indices": dropped,
        },
    })
    output_manifest.parent.mkdir(parents=True, exist_ok=True)
    output_manifest.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result
