"""C3-only binding for the accepted exact-final private replay stage."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from src.autoslice.redelivery_source_binding import V2RedeliverySourceBinding


CANDIDATE_ID = "auto_220021_561_670"
RECORDING_DATE = "2026-08-13"
_SHA = re.compile(r"sha256:[0-9a-f]{64}\Z")
_PADDED = re.compile(r"padded_(\d+)_(\d+)\.mp4\Z")
_AUTHORITY = Path("assets/lidousha/fastlane_c3_deployable_baseline_authority")
_PROPOSAL_FILE_SHA = "sha256:0efb0425ea6355413578b49e8248cd49d3d73a78986fedf9e00835f343473e3d"
_PROPOSAL_SEAL = "sha256:f7705695e2d3902c9ad4716987da40779b7e3ceb0d46e016736ed6dd938697c1"
_INTERVAL_FILE_SHA = "sha256:c25505b51185ee679cfc8ef7f1b9031e7c166ec0f3b6d1f2715531d9c95fbe0f"
_INTERVAL_SEAL = "sha256:ead96ae15f7953c20cc8ea92bdfd472ee7d92de23f5d71cf63e1fc38c8549442"
_ACCEPTANCE_FILE_SHA = "sha256:b1587a31e4523d1f0d4e69d6a4a3eecf1799f899d58a060136087574f8048edd"
_ACCEPTANCE_SEAL = "sha256:9e3ff0d577b1190d4a75faf842b4def2379e12d0232dd39c74ace10fa422bef9"
_BASELINE_MANIFEST_SHA = "sha256:fa1fee5f2ec29556d8713414963df32f3bdf2a0f2b6cdd56efc71937509adf30"
_FINAL_GRID = {
    "path": "assets/lidousha/fastlane_c3_final_grid_consumption/auto_220021_561_670.v1.json",
    "authority_sha256": "sha256:6800bbb1bb12267abdfc6484148bd531ab7173b8187a144fc46a53ca6df860a6",
    "text_srt_sha256": "sha256:d70a96c402df8313264ef6ca145d69d5dbb74e7a2c48eb512e78f3f417e8eecc",
    "speaker_srt_sha256": "sha256:51eef37bf38a2e1a58e4412115c5f2a9699904df80a70f6d8a406d6dde206153",
    "ass_sha256": "sha256:d3160f325648915db0ebb6f4f05e48cc6e6cf25caccf726759c654d02829838a",
    "burned_video_sha256": "sha256:c5d49bdff6842298f9a9cc1907044faa20dc8cae3d7717e99e80bd51aa2f9faf",
    "delivery_media_sha256": "sha256:2e94ba7ae18e64903baca4cadb647b9545915778b19082e3cd0582d32e20c84e",
}


class C3PrivateStageAuthorityError(ValueError):
    """The C3-only final-source geometry is not bound to accepted authority."""


@dataclass(frozen=True, slots=True)
class C3PrivateStageGeometry:
    """The accepted final source interval, distinct from its broad media pad."""

    binding: V2RedeliverySourceBinding
    source_local_start_ms: int
    source_local_end_ms: int
    record_local_start_ms: int
    record_local_end_ms: int


def _canonical_sha256(value: object) -> str:
    return "sha256:" + hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _regular_read(path: Path, *, repo_root: Path, label: str) -> tuple[bytes, str]:
    try:
        relative = path.relative_to(repo_root)
    except ValueError as exc:
        raise C3PrivateStageAuthorityError("C3_STAGE_AUTHORITY_PATH_INVALID") from exc
    cursor = repo_root
    try:
        for part in relative.parts:
            cursor /= part
            observed = os.lstat(cursor)
            if stat.S_ISLNK(observed.st_mode):
                raise C3PrivateStageAuthorityError("C3_STAGE_AUTHORITY_SYMLINK")
        if not stat.S_ISREG(observed.st_mode):
            raise C3PrivateStageAuthorityError("C3_STAGE_AUTHORITY_NOT_REGULAR")
        payload = path.read_bytes()
    except C3PrivateStageAuthorityError:
        raise
    except OSError as exc:
        raise C3PrivateStageAuthorityError("C3_STAGE_AUTHORITY_UNAVAILABLE") from exc
    return payload, "sha256:" + hashlib.sha256(payload).hexdigest()


def _document(path: Path, *, repo_root: Path, label: str) -> tuple[dict, str]:
    raw, digest = _regular_read(path, repo_root=repo_root, label=label)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise C3PrivateStageAuthorityError("C3_STAGE_AUTHORITY_JSON_INVALID") from exc
    if not isinstance(value, dict):
        raise C3PrivateStageAuthorityError("C3_STAGE_AUTHORITY_JSON_INVALID")
    return value, digest


def _sealed(document: Mapping[str, object], *, field: str, label: str) -> None:
    unsigned = dict(document)
    declared = unsigned.pop(field, None)
    if not isinstance(declared, str) or _SHA.fullmatch(declared) is None:
        raise C3PrivateStageAuthorityError(f"C3_STAGE_{label}_SEAL_INVALID")
    if declared != _canonical_sha256(unsigned):
        raise C3PrivateStageAuthorityError(f"C3_STAGE_{label}_SEAL_DRIFT")


def resolve_c3_private_stage_geometry(plan: object) -> C3PrivateStageGeometry | None:
    """Return C3's accepted final binding, or leave every other candidate alone."""

    candidate_id = getattr(plan, "candidate_id")
    recording_date = getattr(plan, "date")
    if candidate_id != CANDIDATE_ID or recording_date != RECORDING_DATE:
        return None
    baseline = getattr(plan, "baseline")
    manifest_path = Path(getattr(baseline, "manifest_path"))
    try:
        repo_root = manifest_path.parents[3]
    except IndexError as exc:
        raise C3PrivateStageAuthorityError("C3_STAGE_REPOSITORY_ROOT_INVALID") from exc
    authority_root = repo_root / _AUTHORITY
    _manifest_bytes, manifest_file_sha = _regular_read(
        manifest_path, repo_root=repo_root, label="BASELINE_MANIFEST"
    )
    proposal, proposal_file_sha = _document(authority_root / f"{CANDIDATE_ID}.v1.json", repo_root=repo_root, label="PROPOSAL")
    interval, interval_file_sha = _document(authority_root / f"{CANDIDATE_ID}.exact-interval.v1.json", repo_root=repo_root, label="INTERVAL")
    accepted, acceptance_file_sha = _document(authority_root / f"{CANDIDATE_ID}.accepted.v1.json", repo_root=repo_root, label="ACCEPTANCE")
    if proposal_file_sha != _PROPOSAL_FILE_SHA:
        raise C3PrivateStageAuthorityError("C3_STAGE_PROPOSAL_FILE_DRIFT")
    if interval_file_sha != _INTERVAL_FILE_SHA:
        raise C3PrivateStageAuthorityError("C3_STAGE_INTERVAL_FILE_DRIFT")
    if acceptance_file_sha != _ACCEPTANCE_FILE_SHA:
        raise C3PrivateStageAuthorityError("C3_STAGE_ACCEPTANCE_FILE_DRIFT")
    if manifest_file_sha != _BASELINE_MANIFEST_SHA:
        raise C3PrivateStageAuthorityError("C3_STAGE_BASELINE_MANIFEST_DRIFT")
    _sealed(proposal, field="authority_sha256", label="PROPOSAL")
    _sealed(interval, field="authority_sha256", label="INTERVAL")
    _sealed(accepted, field="acceptance_sha256", label="ACCEPTANCE")
    if proposal.get("authority_sha256") != _PROPOSAL_SEAL:
        raise C3PrivateStageAuthorityError("C3_STAGE_PROPOSAL_SEAL_DRIFT")
    if interval.get("authority_sha256") != _INTERVAL_SEAL:
        raise C3PrivateStageAuthorityError("C3_STAGE_INTERVAL_SEAL_DRIFT")
    if accepted.get("acceptance_sha256") != _ACCEPTANCE_SEAL:
        raise C3PrivateStageAuthorityError("C3_STAGE_ACCEPTANCE_SEAL_DRIFT")
    if (
        accepted.get("accepted") is not True
        or accepted.get("candidate_id") != CANDIDATE_ID
        or accepted.get("recording_date") != RECORDING_DATE
        or proposal.get("candidate_id") != CANDIDATE_ID
        or proposal.get("recording_date") != RECORDING_DATE
        or interval.get("candidate_id") != CANDIDATE_ID
        or interval.get("recording_date") != RECORDING_DATE
    ):
        raise C3PrivateStageAuthorityError("C3_STAGE_IDENTITY_DRIFT")
    proposal_ref = accepted.get("proposal")
    interval_ref = accepted.get("exact_interval")
    baseline_ref = proposal.get("baseline")
    final_grid = proposal.get("final_grid")
    if not all(isinstance(value, Mapping) for value in (proposal_ref, interval_ref, baseline_ref, final_grid)):
        raise C3PrivateStageAuthorityError("C3_STAGE_AUTHORITY_BINDING_INVALID")
    if (
        proposal_ref.get("file_sha256") != proposal_file_sha
        or proposal_ref.get("authority_sha256") != proposal.get("authority_sha256")
        or interval_ref.get("file_sha256") != interval_file_sha
        or interval_ref.get("authority_sha256") != interval.get("authority_sha256")
        or baseline_ref.get("manifest_path") != str(manifest_path.relative_to(repo_root))
        or baseline_ref.get("manifest_sha256") != _BASELINE_MANIFEST_SHA
    ):
        raise C3PrivateStageAuthorityError("C3_STAGE_AUTHORITY_BINDING_DRIFT")
    config = getattr(baseline, "config")
    source_start = config.get("absolute_source_start_ms")
    source_end = config.get("absolute_source_end_ms")
    if (
        not isinstance(source_start, int) or isinstance(source_start, bool)
        or not isinstance(source_end, int) or isinstance(source_end, bool)
        or source_end <= source_start
        or interval.get("local_start_ms") != 0
        or interval.get("local_end_ms") != source_end - source_start
        or interval.get("final_cue_end_ms") != 108_940
        or config.get("sha256") != baseline_ref.get("reviewed_srt_sha256", "").removeprefix("sha256:")
        or dict(final_grid) != _FINAL_GRID
        or config.get("sha256") != _FINAL_GRID["text_srt_sha256"].removeprefix("sha256:")
        or not isinstance(getattr(plan, "expected_video_sha256", None), str)
        or getattr(plan, "expected_video_sha256") != _FINAL_GRID["delivery_media_sha256"]
    ):
        raise C3PrivateStageAuthorityError("C3_STAGE_FINAL_INTERVAL_DRIFT")
    padded = Path(getattr(plan, "padded_path"))
    match = _PADDED.fullmatch(padded.name)
    record_start, record_end = getattr(plan, "local_start_ms"), getattr(plan, "local_end_ms")
    if (
        match is None
        or not isinstance(record_start, int) or isinstance(record_start, bool)
        or not isinstance(record_end, int) or isinstance(record_end, bool)
    ):
        raise C3PrivateStageAuthorityError("C3_STAGE_RECORD_GEOMETRY_INVALID")
    padded_start, padded_end = (int(value) for value in match.groups())
    source_length = source_end - source_start
    if (
        padded_start + record_start != source_start
        or padded_start + record_end != source_end
        or not padded_start < source_start < source_end < padded_end
        or record_end - record_start != source_length
        or interval.get("source_witness_start_ms") != record_start
        or interval.get("source_witness_end_ms") != record_end
        or accepted.get("forbidden_operations") != ["provider", "state", "ssh", "deploy", "upload", "new_content"]
    ):
        raise C3PrivateStageAuthorityError("C3_STAGE_BROAD_PADDED_GEOMETRY_INVALID")
    return C3PrivateStageGeometry(
        binding=V2RedeliverySourceBinding(
            absolute_source_start_ms=source_start,
            absolute_source_end_ms=source_end,
            source_recording_basename=str(config.get("source_recording_basename") or ""),
            source_sha256=str(config.get("source_sha256") or ""),
            content_absolute_start_ms=padded_start,
            content_absolute_end_ms=padded_end,
            padded_content_start_ms=0,
            padded_content_end_ms=padded_end - padded_start,
        ),
        source_local_start_ms=0,
        source_local_end_ms=source_length,
        record_local_start_ms=record_start,
        record_local_end_ms=record_end,
    )
