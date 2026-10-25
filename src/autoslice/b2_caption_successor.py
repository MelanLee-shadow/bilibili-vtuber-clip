"""Candidate-locked B2 nested-media caption successor.

The private stage validates the already-reviewed predecessor, the independent
source-attribution evidence, and 维护者's conditional correction authorization.
It can render a private corrected artifact but cannot publish or mutate Bilibili.
"""

from __future__ import annotations

from src.autoslice.evidence_identity import file_sha256 as _evidence_file_sha256
from array import array
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from src.autoslice.recut_materialization import _burn_preview_subtitles
from src.autoslice.subtitle_audio_correspondence import TimedCue, parse_timed_srt

ROOT = Path(__file__).resolve().parents[2]
AUTHORITY_RELATIVE = Path(
    "assets/lidousha/b2_caption_successor/"
    "auto_120029_1409_1578.caption-successor.v1.json"
)
CANDIDATE_ID = "auto_120029_1409_1578"
_SHA_RE = re.compile(r"(?:sha256:)?[0-9a-f]{64}\Z")


class B2CaptionSuccessorError(ValueError):
    """A B2 successor input or output is absent, drifted, or unsafe."""


def sha256_file(path: Path, *, prefixed: bool = True) -> str:
    digest = _evidence_file_sha256(path)
    value = digest
    return f"sha256:{value}" if prefixed else value


def _regular(path: Path, *, code: str) -> Path:
    if path.is_symlink():
        raise B2CaptionSuccessorError(code)
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise B2CaptionSuccessorError(code) from exc
    if not resolved.is_file():
        raise B2CaptionSuccessorError(code)
    return resolved


def _json(path: Path, *, code: str) -> dict[str, Any]:
    path = _regular(path, code=code)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise B2CaptionSuccessorError(code) from exc
    if not isinstance(value, dict):
        raise B2CaptionSuccessorError(code)
    return value


def _expected_sha(value: object, *, code: str) -> str:
    if not isinstance(value, str) or _SHA_RE.fullmatch(value) is None:
        raise B2CaptionSuccessorError(code)
    return "sha256:" + value.removeprefix("sha256:")


def load_authority(*, repo_root: Path = ROOT) -> dict[str, Any]:
    value = _json(repo_root / AUTHORITY_RELATIVE, code="B2_AUTHORITY_INVALID")
    if not (
        value.get("schema_version") == "b2-caption-successor-authority.v1"
        and value.get("candidate_id") == CANDIDATE_ID
        and isinstance(value.get("predecessor"), dict)
        and isinstance(value.get("source_attribution"), dict)
        and isinstance(value.get("authorization"), dict)
        and isinstance(value.get("branding_intro"), dict)
        and isinstance(value.get("mutation"), dict)
        and value.get("private_stage")
        == {
            "upload_allowed": False,
            "publication_authority": False,
            "provider_calls": 0,
            "asr_calls": 0,
            "model_downloads": 0,
        }
    ):
        raise B2CaptionSuccessorError("B2_AUTHORITY_INVALID")
    return value


def _verify(path: Path, descriptor: Mapping[str, object], *, code: str) -> Path:
    resolved = _regular(path, code=code)
    if sha256_file(resolved) != _expected_sha(descriptor.get("sha256"), code=code):
        raise B2CaptionSuccessorError(code)
    return resolved


def verify_predecessor(
    package_root: Path, authority: Mapping[str, object]
) -> dict[str, Path]:
    root = package_root.resolve(strict=True)
    if package_root.is_symlink() or not root.is_dir():
        raise B2CaptionSuccessorError("B2_PREDECESSOR_ROOT_INVALID")
    predecessor = authority.get("predecessor")
    if not isinstance(predecessor, Mapping):
        raise B2CaptionSuccessorError("B2_PREDECESSOR_AUTHORITY_INVALID")
    paths: dict[str, Path] = {}
    for label, descriptor in predecessor.items():
        if not isinstance(label, str) or not isinstance(descriptor, Mapping):
            raise B2CaptionSuccessorError("B2_PREDECESSOR_AUTHORITY_INVALID")
        filename = descriptor.get("filename")
        if not isinstance(filename, str) or Path(filename).name != filename:
            raise B2CaptionSuccessorError("B2_PREDECESSOR_AUTHORITY_INVALID")
        paths[label] = _verify(
            root / filename,
            descriptor,
            code=f"B2_PREDECESSOR_{label.upper()}_HASH_DRIFT",
        )
    return paths


def _parse_srt(path: Path, *, code: str) -> list[TimedCue]:
    try:
        return parse_timed_srt(path.read_text(encoding="utf-8-sig"), label=code)
    except (OSError, UnicodeError, ValueError) as exc:
        raise B2CaptionSuccessorError(code) from exc


def _affected_rows(mutation: Mapping[str, object]) -> dict[int, tuple[int, int, str]]:
    raw = mutation.get("affected_source_cues")
    if not isinstance(raw, Mapping):
        raise B2CaptionSuccessorError("B2_MUTATION_AUTHORITY_INVALID")
    rows: dict[int, tuple[int, int, str]] = {}
    for key, value in raw.items():
        if not (
            isinstance(key, str)
            and key.isdigit()
            and isinstance(value, list)
            and len(value) == 3
            and isinstance(value[0], int)
            and isinstance(value[1], int)
            and isinstance(value[2], str)
        ):
            raise B2CaptionSuccessorError("B2_MUTATION_AUTHORITY_INVALID")
        rows[int(key)] = (value[0], value[1], value[2])
    return rows


def expected_corrected_cues(
    source: list[TimedCue], authority: Mapping[str, object]
) -> list[TimedCue]:
    mutation = authority.get("mutation")
    if not isinstance(mutation, Mapping):
        raise B2CaptionSuccessorError("B2_MUTATION_AUTHORITY_INVALID")
    source_count = mutation.get("source_cue_count")
    release_count = mutation.get("release_cue_count")
    if len(source) != source_count or [cue.index for cue in source] != [
        str(i) for i in range(1, len(source) + 1)
    ]:
        raise B2CaptionSuccessorError("B2_SOURCE_SRT_GRID_DRIFT")
    affected = _affected_rows(mutation)
    for cue_index, expected in affected.items():
        cue = source[cue_index - 1]
        if (cue.start_ms, cue.end_ms, cue.text) != expected:
            raise B2CaptionSuccessorError("B2_AFFECTED_SOURCE_CUE_DRIFT")
    dropped_raw = mutation.get("drop_source_cues")
    overlap = mutation.get("overlap_replacement")
    if not (
        isinstance(dropped_raw, list)
        and all(
            isinstance(value, int) and not isinstance(value, bool)
            for value in dropped_raw
        )
        and isinstance(overlap, Mapping)
        and isinstance(overlap.get("source_cue"), int)
        and isinstance(overlap.get("start_ms"), int)
        and isinstance(overlap.get("end_ms"), int)
        and isinstance(overlap.get("text"), str)
    ):
        raise B2CaptionSuccessorError("B2_MUTATION_AUTHORITY_INVALID")
    dropped = set(dropped_raw)
    if dropped != {45, 46, 47, 48, 50, 51} or overlap.get("source_cue") != 49:
        raise B2CaptionSuccessorError("B2_MUTATION_SCOPE_DRIFT")
    result: list[TimedCue] = []
    for cue in source:
        source_index = int(cue.index)
        if source_index in dropped:
            continue
        if source_index == int(overlap["source_cue"]):
            cue = TimedCue(
                index=str(len(result) + 1),
                start_ms=int(overlap["start_ms"]),
                end_ms=int(overlap["end_ms"]),
                text=str(overlap["text"]),
            )
        else:
            cue = TimedCue(
                index=str(len(result) + 1),
                start_ms=cue.start_ms,
                end_ms=cue.end_ms,
                text=cue.text,
            )
        result.append(cue)
    if len(result) != release_count:
        raise B2CaptionSuccessorError("B2_RELEASE_SRT_COUNT_DRIFT")
    if any(left.end_ms > right.start_ms for left, right in zip(result, result[1:])):
        raise B2CaptionSuccessorError("B2_RELEASE_SRT_OVERLAP")
    return result


def validate_corrected_srt(
    *, source_srt: Path, corrected_srt: Path, authority: Mapping[str, object]
) -> list[TimedCue]:
    source = _parse_srt(source_srt, code="B2_SOURCE_SRT_INVALID")
    expected = expected_corrected_cues(source, authority)
    actual = _parse_srt(corrected_srt, code="B2_CORRECTED_SRT_INVALID")
    if actual != expected:
        raise B2CaptionSuccessorError("B2_CORRECTED_SRT_PROJECTION_DRIFT")
    source_authority = authority.get("source_attribution")
    if not isinstance(source_authority, Mapping):
        raise B2CaptionSuccessorError("B2_CORRECTED_SRT_AUTHORITY_INVALID")
    expected_sha = _expected_sha(
        source_authority.get("corrected_srt_sha256"),
        code="B2_CORRECTED_SRT_AUTHORITY_INVALID",
    )
    if sha256_file(corrected_srt) != expected_sha:
        raise B2CaptionSuccessorError("B2_CORRECTED_SRT_HASH_DRIFT")
    return actual


def verify_evidence(
    *,
    attribution_path: Path,
    result_path: Path,
    authorization_path: Path,
    authority: Mapping[str, object],
) -> dict[str, dict[str, Any]]:
    source_authority = authority.get("source_attribution")
    authorization_authority = authority.get("authorization")
    if not isinstance(source_authority, Mapping) or not isinstance(
        authorization_authority, Mapping
    ):
        raise B2CaptionSuccessorError("B2_EVIDENCE_AUTHORITY_INVALID")
    attribution_path = _verify(
        attribution_path,
        {"sha256": source_authority.get("attribution_sha256")},
        code="B2_ATTRIBUTION_HASH_DRIFT",
    )
    result_path = _verify(
        result_path,
        {"sha256": source_authority.get("result_sha256")},
        code="B2_ATTRIBUTION_RESULT_HASH_DRIFT",
    )
    authorization_path = _verify(
        authorization_path,
        {"sha256": authorization_authority.get("sha256")},
        code="B2_AUTHORIZATION_HASH_DRIFT",
    )
    attribution = _json(attribution_path, code="B2_ATTRIBUTION_INVALID")
    result = _json(result_path, code="B2_ATTRIBUTION_RESULT_INVALID")
    authorization = _json(authorization_path, code="B2_AUTHORIZATION_INVALID")
    original = authority.get("original_publication")
    if not isinstance(original, Mapping):
        raise B2CaptionSuccessorError("B2_EVIDENCE_AUTHORITY_INVALID")
    if not (
        attribution.get("status") == source_authority.get("status")
        and attribution.get("candidate_id") == CANDIDATE_ID
        and isinstance(attribution.get("output_srt"), Mapping)
        and attribution["output_srt"].get("sha256")
        == str(source_authority.get("corrected_srt_sha256")).removeprefix("sha256:")
        and result.get("status") == source_authority.get("status")
        and result.get("output_srt_sha256")
        == str(source_authority.get("corrected_srt_sha256")).removeprefix("sha256:")
        and authorization.get("candidate_id") == CANDIDATE_ID
        and authorization.get("original_bvid") == original.get("bvid")
        and authorization.get("original_cid") == original.get("cid")
        and authorization.get("status") == authorization_authority.get("required_status")
    ):
        raise B2CaptionSuccessorError("B2_EVIDENCE_CONTENT_DRIFT")
    return {
        "attribution": attribution,
        "result": result,
        "authorization": authorization,
    }


def _write_json(path: Path, value: Mapping[str, object]) -> None:
    payload = (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        descriptor = -1
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _branding_context(
    *, intro_path: Path, authority: Mapping[str, object], repo_root: Path
) -> dict[str, object]:
    intro_authority = authority.get("branding_intro")
    if not isinstance(intro_authority, Mapping):
        raise B2CaptionSuccessorError("B2_BRANDING_AUTHORITY_INVALID")
    intro = _regular(intro_path, code="B2_BRANDING_INTRO_MISSING")
    expected_sha = _expected_sha(
        intro_authority.get("media_sha256"), code="B2_BRANDING_AUTHORITY_INVALID"
    )
    if sha256_file(intro) != expected_sha:
        raise B2CaptionSuccessorError("B2_BRANDING_INTRO_HASH_DRIFT")
    manifest = _regular(
        repo_root / "assets/lidousha/intro/branding_intro.v1.json",
        code="B2_BRANDING_MANIFEST_MISSING",
    )
    intro_id = intro_authority.get("intro_id")
    offset = intro_authority.get("recorded_intro_offset_ms")
    if (
        not isinstance(intro_id, str)
        or not isinstance(offset, int)
        or isinstance(offset, bool)
    ):
        raise B2CaptionSuccessorError("B2_BRANDING_AUTHORITY_INVALID")
    selected = {
        "intro_id": intro_id,
        "media_path": intro,
        "media_sha256": expected_sha.removeprefix("sha256:"),
    }
    return {
        **selected,
        "manifest_path": manifest,
        "manifest_sha256": sha256_file(manifest, prefixed=False),
        "candidates": [selected],
        "rotation_mode": "recorded-delivery-authority",
        "recorded_delivery_binding": {
            "intro_id": intro_id,
            "intro_media_sha256": expected_sha.removeprefix("sha256:"),
            "intro_offset_ms": offset,
        },
    }



def _decode_mono_pcm(path: Path) -> bytes:
    completed = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(path),
            "-map",
            "0:a:0",
            "-ac",
            "1",
            "-ar",
            "48000",
            "-f",
            "s16le",
            "pipe:1",
        ],
        check=False,
        capture_output=True,
        timeout=600,
    )
    if completed.returncode != 0 or not completed.stdout:
        raise B2CaptionSuccessorError("B2_TIMEBASE_PCM_DECODE_FAILED")
    return completed.stdout


def _zncc(
    left: array,
    right: array,
    *,
    left_start: int,
    right_start: int,
    count: int,
    step: int,
) -> float:
    xs = left[left_start : left_start + count : step]
    ys = right[right_start : right_start + count : step]
    if len(xs) != len(ys) or not xs:
        return 0.0
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    numerator = denominator_x = denominator_y = 0.0
    for x, y in zip(xs, ys):
        dx = x - mean_x
        dy = y - mean_y
        numerator += dx * dy
        denominator_x += dx * dx
        denominator_y += dy * dy
    if not denominator_x or not denominator_y:
        return 0.0
    return numerator / math.sqrt(denominator_x * denominator_y)


def verify_predecessor_audio_timebase(
    predecessor_video: Path,
    successor_video: Path,
) -> dict[str, object]:
    predecessor_pcm = _decode_mono_pcm(predecessor_video)
    successor_pcm = _decode_mono_pcm(successor_video)
    if len(predecessor_pcm) != len(successor_pcm) or len(predecessor_pcm) % 2:
        raise B2CaptionSuccessorError("B2_TIMEBASE_PCM_LENGTH_DRIFT")
    predecessor = array("h")
    successor = array("h")
    predecessor.frombytes(predecessor_pcm)
    successor.frombytes(successor_pcm)
    if sys.byteorder != "little":
        predecessor.byteswap()
        successor.byteswap()
    sample_rate = 48000
    window_samples = sample_rate
    rows: list[dict[str, object]] = []
    for start_seconds in (20, 60, 100, 140):
        start = start_seconds * sample_rate
        if start + window_samples + 48 >= len(predecessor):
            raise B2CaptionSuccessorError("B2_TIMEBASE_WINDOW_OUT_OF_RANGE")
        candidates = [
            (
                _zncc(
                    predecessor,
                    successor,
                    left_start=start,
                    right_start=start + lag,
                    count=window_samples,
                    step=12,
                ),
                lag,
            )
            for lag in range(-48, 49)
            if start + lag >= 0
        ]
        correlation, lag = max(candidates)
        rows.append(
            {
                "start_seconds": start_seconds,
                "best_lag_samples": lag,
                "best_lag_ms": lag * 1000 / sample_rate,
                "correlation": correlation,
            }
        )
    if any(
        row["best_lag_samples"] != 0 or float(row["correlation"]) < 0.99999
        for row in rows
    ):
        raise B2CaptionSuccessorError("B2_TIMEBASE_NOT_ZERO_LAG_EQUIVALENT")
    return {
        "schema_version": "b2-predecessor-audio-timebase-equivalence.v1",
        "status": "PASS_ZERO_LAG_FOUR_WINDOWS",
        "sample_rate": sample_rate,
        "sample_count": len(predecessor),
        "predecessor_pcm_sha256": "sha256:"
        + hashlib.sha256(predecessor_pcm).hexdigest(),
        "successor_pcm_sha256": "sha256:"
        + hashlib.sha256(successor_pcm).hexdigest(),
        "windows": rows,
    }


def reconcile_one_ms_probe_rounding(
    *,
    branding: Mapping[str, object],
    intro_authority: Mapping[str, object],
    timebase_equivalence: Mapping[str, object],
) -> dict[str, object]:
    actual = branding.get("intro_offset_ms")
    expected = intro_authority.get("recorded_intro_offset_ms")
    if (
        isinstance(actual, bool)
        or not isinstance(actual, int)
        or isinstance(expected, bool)
        or not isinstance(expected, int)
        or abs(actual - expected) != 1
        or timebase_equivalence.get("status") != "PASS_ZERO_LAG_FOUR_WINDOWS"
    ):
        raise B2CaptionSuccessorError("B2_BRANDING_OFFSET_NOT_RECONCILABLE")
    reconciled = dict(branding)
    reconciled["actual_probe_intro_offset_ms"] = actual
    reconciled["intro_offset_ms"] = expected
    reconciled["probe_rounding_delta_ms"] = actual - expected
    reconciled["timebase_reconciliation"] = dict(timebase_equivalence)
    reconciled["recorded_delivery_binding"] = {
        "intro_id": intro_authority.get("intro_id"),
        "intro_media_sha256": str(intro_authority.get("media_sha256", "")).removeprefix(
            "sha256:"
        ),
        "intro_offset_ms": expected,
    }
    return reconciled


def _remove_failed_materializer_outputs(burned: Mapping[str, object]) -> None:
    for key in ("path", "ass_path"):
        raw = burned.get(key)
        if not isinstance(raw, str) or not raw:
            continue
        path = Path(raw)
        path.unlink(missing_ok=True)
        shutil.rmtree(path.with_name(path.name + ".intro-work"), ignore_errors=True)


def materialize_private_successor(
    *,
    predecessor_package: Path,
    corrected_srt: Path,
    attribution_path: Path,
    result_path: Path,
    authorization_path: Path,
    intro_path: Path,
    output: Path,
    repo_root: Path = ROOT,
    materializer=_burn_preview_subtitles,
    timebase_verifier=verify_predecessor_audio_timebase,
) -> Path:
    authority = load_authority(repo_root=repo_root)
    predecessor = verify_predecessor(predecessor_package, authority)
    corrected_srt = _regular(corrected_srt, code="B2_CORRECTED_SRT_MISSING")
    validate_corrected_srt(
        source_srt=predecessor["final_srt"],
        corrected_srt=corrected_srt,
        authority=authority,
    )
    evidence = verify_evidence(
        attribution_path=attribution_path,
        result_path=result_path,
        authorization_path=authorization_path,
        authority=authority,
    )
    target = output.absolute()
    if target.exists() or target.is_symlink():
        raise B2CaptionSuccessorError("B2_PRIVATE_OUTPUT_EXISTS")
    target.mkdir(mode=0o700, parents=True)
    try:
        video = target / f"{CANDIDATE_ID}.recut.mp4"
        srt = target / f"{CANDIDATE_ID}.recut.srt"
        cover = target / f"{CANDIDATE_ID}.recut.burned-final-sapphire72.cover.png"
        shutil.copy2(predecessor["recut_video"], video)
        shutil.copy2(corrected_srt, srt)
        shutil.copy2(predecessor["cover"], cover)
        materialized_record = {
            "status": "MATERIALIZED",
            "media_path": str(video),
            "subtitle_path": str(srt),
            "subtitle_style": "lidousha-final-sapphire72",
            "artifact_hashes": {},
        }
        branding_context = _branding_context(
            intro_path=intro_path, authority=authority, repo_root=repo_root
        )
        record = materializer(
            materialized_record,
            run_ffmpeg=True,
            branding_intro=branding_context,
        )
        burned = record.get("burned_preview") if isinstance(record, Mapping) else None
        intro_authority = authority.get("branding_intro")
        if not isinstance(intro_authority, Mapping):
            raise B2CaptionSuccessorError("B2_BRANDING_AUTHORITY_INVALID")
        if not isinstance(burned, Mapping):
            raise B2CaptionSuccessorError("B2_CANONICAL_MATERIALIZER_FAILED")
        branding: Mapping[str, object] | None = None
        if burned.get("status") != "BURNED":
            if not (
                burned.get("reason_code") == "BRANDING_INTRO_FAILED"
                and burned.get("error")
                == "BrandingIntroError: reburned intro offset drifted from the existing delivery binding"
            ):
                raise B2CaptionSuccessorError("B2_CANONICAL_MATERIALIZER_FAILED")
            _remove_failed_materializer_outputs(burned)
            relaxed_context = dict(branding_context)
            relaxed_context.pop("recorded_delivery_binding", None)
            record = materializer(
                materialized_record,
                run_ffmpeg=True,
                branding_intro=relaxed_context,
            )
            burned = (
                record.get("burned_preview") if isinstance(record, Mapping) else None
            )
            if not isinstance(burned, Mapping) or burned.get("status") != "BURNED":
                raise B2CaptionSuccessorError("B2_CANONICAL_MATERIALIZER_RETRY_FAILED")
            final_video = _regular(
                Path(str(burned.get("path"))), code="B2_BURNED_VIDEO_MISSING"
            )
            actual_branding = burned.get("branding_intro")
            if not isinstance(actual_branding, Mapping):
                raise B2CaptionSuccessorError("B2_BRANDING_OUTPUT_DRIFT")
            branding = reconcile_one_ms_probe_rounding(
                branding=actual_branding,
                intro_authority=intro_authority,
                timebase_equivalence=timebase_verifier(
                    predecessor["burned_video"], final_video
                ),
            )
        ass = _regular(Path(str(burned.get("ass_path"))), code="B2_ASS_MISSING")
        final_video = _regular(
            Path(str(burned.get("path"))), code="B2_BURNED_VIDEO_MISSING"
        )
        if branding is None:
            actual_branding = burned.get("branding_intro")
            if not isinstance(actual_branding, Mapping):
                raise B2CaptionSuccessorError("B2_BRANDING_OUTPUT_DRIFT")
            branding = actual_branding
        if not (
            isinstance(branding, Mapping)
            and branding.get("status") == "PREPENDED"
            and branding.get("intro_id") == intro_authority.get("intro_id")
            and str(branding.get("intro_media_sha256", "")).removeprefix("sha256:")
            == str(intro_authority.get("media_sha256")).removeprefix("sha256:")
            and branding.get("intro_offset_ms")
            == intro_authority.get("recorded_intro_offset_ms")
            and isinstance(branding.get("verification"), Mapping)
            and branding["verification"].get("full_decode") == "clean"
        ):
            raise B2CaptionSuccessorError("B2_BRANDING_OUTPUT_DRIFT")
        receipt = {
            "schema_version": "b2-caption-private-successor.v1",
            "status": "PRIVATE_SUCCESSOR_READY_FOR_FORMAL_PACKAGE",
            "candidate_id": CANDIDATE_ID,
            "title": authority["title"],
            "original_publication": authority["original_publication"],
            "authority": {
                "path": AUTHORITY_RELATIVE.as_posix(),
                "sha256": sha256_file(repo_root / AUTHORITY_RELATIVE),
            },
            "predecessor_package": {
                "review_manifest_sha256": sha256_file(predecessor["review_manifest"]),
                "package_audit_sha256": sha256_file(predecessor["package_audit"]),
            },
            "source_attribution": {
                "attribution_sha256": sha256_file(attribution_path),
                "result_sha256": sha256_file(result_path),
                "status": evidence["attribution"]["status"],
            },
            "authorization": {
                "sha256": sha256_file(authorization_path),
                "status": evidence["authorization"]["status"],
            },
            "artifacts": {
                "recut_video": {"path": video.name, "sha256": sha256_file(video)},
                "srt": {"path": srt.name, "sha256": sha256_file(srt)},
                "ass": {"path": ass.name, "sha256": sha256_file(ass)},
                "burned_video": {
                    "path": final_video.name,
                    "sha256": sha256_file(final_video),
                },
                "cover": {"path": cover.name, "sha256": sha256_file(cover)},
            },
            "branding_intro": dict(branding),
            "source_cue_count": authority["mutation"]["source_cue_count"],
            "release_cue_count": authority["mutation"]["release_cue_count"],
            "dropped_source_cues": authority["mutation"]["drop_source_cues"],
            "overlap_replacement": authority["mutation"]["overlap_replacement"],
            "provider_calls": 0,
            "asr_calls": 0,
            "model_downloads": 0,
            "upload_allowed": False,
            "publication_authority": False,
            "upload_performed": False,
        }
        _write_json(target / "b2-caption-private-successor.v1.json", receipt)
        return target
    except BaseException:
        shutil.rmtree(target, ignore_errors=True)
        raise
