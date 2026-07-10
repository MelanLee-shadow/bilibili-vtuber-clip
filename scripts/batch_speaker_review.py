#!/usr/bin/env python3
"""Build a hash-bound, no-upload speaker-separated review package.

The input plan names already-final clean recuts and text-final SRT files.  This
runner never touches the existing delivery, publish record, upload manifest, or
public video.  It is intentionally resumable because CAM++ analysis across a
full day can take a long time.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.apply_speaker_turn_overrides import (  # noqa: E402
    GUEST_WHITE_STYLE,
    LDS_SAPPHIRE_STYLE,
    SPEAKER_SUBTITLE_STYLE_ID,
    atomic_write_text,
    sha256_file,
)
from scripts.produce_slice_package import run_speaker_finalizer  # noqa: E402
from scripts.run_auto_review_shadow_pipeline import (  # noqa: E402
    _burn_preview_subtitles,
)


PLAN_SCHEMA = "lidousha-speaker-review-batch-plan.v1"
RESULT_SCHEMA = "lidousha-speaker-review-item.v1"
BATCH_SCHEMA = "lidousha-speaker-review-batch.v1"
SHA256_RE = re.compile(r"(?:sha256:)?([0-9a-f]{64})\Z")
REQUIRED_ARTIFACTS = {
    "video",
    "text_final_srt",
    "speaker_srt",
    "ass",
    "speaker_manifest",
}


class BatchSpeakerReviewError(RuntimeError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _expected_digest(value: object, field: str) -> str:
    match = SHA256_RE.fullmatch(str(value or ""))
    if not match:
        raise BatchSpeakerReviewError(f"{field} must be a SHA-256 digest")
    return match.group(1)


def _safe_review_name(value: object) -> str:
    name = str(value or "").strip()
    if not name or name in {".", ".."} or Path(name).name != name:
        raise BatchSpeakerReviewError(f"invalid review_name: {value!r}")
    if name.endswith((".mp4", ".srt", ".ass", ".json")):
        raise BatchSpeakerReviewError("review_name must not include a file suffix")
    return name


def validate_plan(document: object) -> dict[str, Any]:
    if not isinstance(document, dict) or document.get("schema_version") != PLAN_SCHEMA:
        raise BatchSpeakerReviewError(f"plan schema must be {PLAN_SCHEMA}")
    if document.get("upload_authorized") is not False:
        raise BatchSpeakerReviewError("plan must explicitly set upload_authorized=false")
    entries = document.get("entries")
    if not isinstance(entries, list) or not entries:
        raise BatchSpeakerReviewError("plan entries must be a non-empty list")
    expected_count = document.get("expected_count")
    if expected_count is not None and expected_count != len(entries):
        raise BatchSpeakerReviewError(
            f"plan expected_count={expected_count!r} does not match {len(entries)} entries"
        )
    candidate_ids: set[str] = set()
    review_names: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for position, raw in enumerate(entries, start=1):
        if not isinstance(raw, Mapping):
            raise BatchSpeakerReviewError(f"entry {position} must be an object")
        candidate_id = str(raw.get("candidate_id") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", candidate_id):
            raise BatchSpeakerReviewError(f"entry {position} has invalid candidate_id")
        review_name = _safe_review_name(raw.get("review_name"))
        if candidate_id in candidate_ids:
            raise BatchSpeakerReviewError(f"duplicate candidate_id: {candidate_id}")
        if review_name in review_names:
            raise BatchSpeakerReviewError(f"duplicate review_name: {review_name}")
        candidate_ids.add(candidate_id)
        review_names.add(review_name)
        override_path = str(raw.get("speaker_override_path") or "").strip()
        override_digest_raw = raw.get("speaker_override_sha256")
        if bool(override_path) != bool(override_digest_raw):
            raise BatchSpeakerReviewError(
                f"entry {position} must bind speaker_override_path and speaker_override_sha256 together"
            )
        normalized.append(
            {
                **dict(raw),
                "candidate_id": candidate_id,
                "review_name": review_name,
                "source_media_sha256": _expected_digest(
                    raw.get("source_media_sha256"), f"entry {position} source_media_sha256"
                ),
                "text_final_srt_sha256": _expected_digest(
                    raw.get("text_final_srt_sha256"), f"entry {position} text_final_srt_sha256"
                ),
                "speaker_override_path": override_path or None,
                "speaker_override_sha256": (
                    _expected_digest(override_digest_raw, f"entry {position} speaker_override_sha256")
                    if override_path
                    else None
                ),
            }
        )
    return {**document, "entries": normalized}


def _write_json(path: Path, document: object) -> None:
    atomic_write_text(path, json.dumps(document, ensure_ascii=False, indent=2) + "\n")


def _verify_bound_file(path_value: object, expected: str, field: str) -> Path:
    path = Path(str(path_value or "")).resolve(strict=True)
    actual = sha256_file(path)
    if actual != expected:
        raise BatchSpeakerReviewError(
            f"{field} hash drift: expected {expected}, got {actual}: {path}"
        )
    return path


def _artifact(path: Path) -> dict[str, object]:
    return {"path": str(path.resolve()), "sha256": sha256_file(path), "bytes": path.stat().st_size}


def _expected_artifact_paths(output_dir: Path, review_name: str) -> dict[str, Path]:
    return {
        "video": output_dir / f"{review_name}.mp4",
        "text_final_srt": output_dir / f"{review_name}.text-final.srt",
        "speaker_srt": output_dir / f"{review_name}.speaker.srt",
        "ass": output_dir / f"{review_name}.ass",
        "speaker_manifest": output_dir / f"{review_name}.speaker.json",
    }


def _validate_ass_style_contract(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    style_lines = [line for line in text.splitlines() if line.startswith("Style: ")]
    expected_styles = [
        f"Style: LDS,{LDS_SAPPHIRE_STYLE}",
        f"Style: GUEST,{GUEST_WHITE_STYLE}",
    ]
    if style_lines != expected_styles:
        raise BatchSpeakerReviewError(f"ASS style contract drift: {path}")
    if "&H0000FFFF" in text or "_OVERLAP" in text:
        raise BatchSpeakerReviewError(f"ASS contains retired speaker style: {path}")
    for line in text.splitlines():
        if not line.startswith("Dialogue: "):
            continue
        fields = line.split(",", 9)
        if len(fields) != 10 or fields[3] not in {"LDS", "GUEST"}:
            raise BatchSpeakerReviewError(f"ASS dialogue uses an invalid style: {line[:160]}")
        if fields[7] not in {"0", "142"}:
            raise BatchSpeakerReviewError(f"ASS dialogue uses an invalid MarginV: {line[:160]}")


def _validate_speaker_manifest(
    path: Path,
    *,
    entry: Mapping[str, object],
    artifacts: Mapping[str, Mapping[str, object]],
) -> None:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("status") != "READY" or manifest.get("production_ready") is not True:
        raise BatchSpeakerReviewError(f"speaker manifest is not READY: {path}")
    if manifest.get("subtitle_style") != SPEAKER_SUBTITLE_STYLE_ID:
        raise BatchSpeakerReviewError(f"speaker manifest style drift: {path}")
    if manifest.get("speaker_taxonomy") != "binary_visual_host_vs_guest":
        raise BatchSpeakerReviewError(f"speaker manifest taxonomy drift: {path}")
    if manifest.get("source_media_sha256") != entry["source_media_sha256"]:
        raise BatchSpeakerReviewError(f"speaker manifest media binding drift: {path}")
    if manifest.get("text_final_srt_sha256") != entry["text_final_srt_sha256"]:
        raise BatchSpeakerReviewError(f"speaker manifest text binding drift: {path}")
    if artifacts["text_final_srt"]["sha256"] != entry["text_final_srt_sha256"]:
        raise BatchSpeakerReviewError(f"packaged text-final SRT drift: {path}")
    if manifest.get("speaker_override_sha256") != entry.get("speaker_override_sha256"):
        raise BatchSpeakerReviewError(f"speaker manifest override binding drift: {path}")
    if manifest.get("host_identity_aliases") != ["李豆沙", "shadow"]:
        raise BatchSpeakerReviewError(f"speaker manifest host aliases drift: {path}")
    if manifest.get("output_review_srt_sha256") != artifacts["speaker_srt"]["sha256"]:
        raise BatchSpeakerReviewError(f"speaker manifest SRT output drift: {path}")
    if manifest.get("output_ass_sha256") != artifacts["ass"]["sha256"]:
        raise BatchSpeakerReviewError(f"speaker manifest ASS output drift: {path}")


def _result_is_reusable(path: Path, entry: Mapping[str, object]) -> bool:
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
        if result.get("schema_version") != RESULT_SCHEMA or result.get("status") != "READY":
            return False
        if result.get("candidate_id") != entry["candidate_id"]:
            return False
        if result.get("review_name") != entry["review_name"]:
            return False
        if result.get("source_media_sha256") != entry["source_media_sha256"]:
            return False
        if result.get("text_final_srt_sha256") != entry["text_final_srt_sha256"]:
            return False
        if result.get("speaker_override_sha256") != entry.get("speaker_override_sha256"):
            return False
        if result.get("subtitle_style") != SPEAKER_SUBTITLE_STYLE_ID:
            return False
        if result.get("upload_authorized") is not False:
            return False
        artifacts = result.get("artifacts")
        if not isinstance(artifacts, Mapping) or set(artifacts) != REQUIRED_ARTIFACTS:
            return False
        expected_paths = _expected_artifact_paths(path.parent, str(entry["review_name"]))
        for name, artifact in artifacts.items():
            if not isinstance(artifact, Mapping):
                return False
            artifact_path = Path(str(artifact["path"]))
            if artifact_path.resolve() != expected_paths[name].resolve():
                return False
            if (
                not artifact_path.is_file()
                or sha256_file(artifact_path) != artifact["sha256"]
                or artifact_path.stat().st_size != artifact["bytes"]
            ):
                return False
        _validate_ass_style_contract(expected_paths["ass"])
        _validate_speaker_manifest(
            expected_paths["speaker_manifest"], entry=entry, artifacts=artifacts
        )
        return True
    except (OSError, TypeError, ValueError, KeyError, BatchSpeakerReviewError):
        return False


def _link_or_copy(source: Path, target: Path) -> None:
    target.unlink(missing_ok=True)
    try:
        os.link(source, target)
    except OSError:
        shutil.copy2(source, target)


def build_review_item(
    entry: Mapping[str, object],
    *,
    output_dir: Path,
    speaker_python: Path,
    resume: bool,
) -> dict[str, object]:
    candidate_id = str(entry["candidate_id"])
    review_name = str(entry["review_name"])
    source_media = _verify_bound_file(
        entry.get("media_path"), str(entry["source_media_sha256"]), "source media"
    )
    text_final = _verify_bound_file(
        entry.get("text_srt_path"), str(entry["text_final_srt_sha256"]), "text-final SRT"
    )
    override_value = entry.get("speaker_override_path")
    override_path = Path(str(override_value)).resolve(strict=True) if override_value else None
    expected_override = entry.get("speaker_override_sha256")
    if override_path is not None:
        expected = _expected_digest(expected_override, "speaker_override_sha256")
        if sha256_file(override_path) != expected:
            raise BatchSpeakerReviewError(f"speaker override hash drift: {override_path}")
    elif expected_override:
        raise BatchSpeakerReviewError("speaker_override_sha256 is set without a path")

    result_path = output_dir / f"{review_name}.result.json"
    if resume and result_path.is_file() and _result_is_reusable(result_path, entry):
        return json.loads(result_path.read_text(encoding="utf-8"))

    paths = _expected_artifact_paths(output_dir, review_name)
    for path in (*paths.values(), result_path):
        path.unlink(missing_ok=True)

    work_dir = output_dir / ".work" / candidate_id
    work_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(text_final, paths["text_final_srt"])
    speaker_manifest = run_speaker_finalizer(
        host="localhost",
        candidate_id=candidate_id,
        media_path=source_media,
        text_srt_path=text_final,
        output_srt_path=paths["speaker_srt"],
        output_ass_path=paths["ass"],
        output_manifest_path=paths["speaker_manifest"],
        work_dir=work_dir / "speaker",
        override_path=override_path,
        speaker_python=speaker_python,
    )
    _validate_ass_style_contract(paths["ass"])

    render_source = work_dir / "render-source.mp4"
    _link_or_copy(source_media, render_source)
    try:
        render = _burn_preview_subtitles(
            {
                "status": "MATERIALIZED",
                "media_path": str(render_source),
                "subtitle_path": str(paths["text_final_srt"]),
                "subtitle_ass_path": str(paths["ass"]),
                "subtitle_style": SPEAKER_SUBTITLE_STYLE_ID,
                "artifact_hashes": {"ass_sha256": "sha256:" + sha256_file(paths["ass"])},
            },
            run_ffmpeg=True,
        )
        burned = (render or {}).get("burned_preview") if isinstance(render, dict) else None
        if not isinstance(burned, Mapping) or burned.get("status") != "BURNED":
            raise BatchSpeakerReviewError(f"speaker preview burn failed: {burned}")
        burned_path = Path(str(burned.get("path")))
        os.replace(burned_path, paths["video"])
    finally:
        render_source.unlink(missing_ok=True)

    # Close the long-run TOCTOU window.  The model and ffmpeg may spend many
    # minutes reading these inputs; a hash change at any point invalidates the
    # whole item instead of producing a mixed-era READY artifact.
    _verify_bound_file(source_media, str(entry["source_media_sha256"]), "source media post-run")
    _verify_bound_file(text_final, str(entry["text_final_srt_sha256"]), "text-final SRT post-run")
    if override_path is not None and sha256_file(override_path) != entry["speaker_override_sha256"]:
        raise BatchSpeakerReviewError(f"speaker override post-run hash drift: {override_path}")

    decisions = speaker_manifest.get("final_decisions") or []
    speaker_counts = {
        "李豆沙": sum(item.get("speaker") == "李豆沙" for item in decisions),
        "连线": sum(item.get("speaker") == "连线" for item in decisions),
    }
    artifacts = {name: _artifact(path) for name, path in paths.items()}
    _validate_speaker_manifest(
        paths["speaker_manifest"], entry=entry, artifacts=artifacts
    )
    result: dict[str, object] = {
        "schema_version": RESULT_SCHEMA,
        "status": "READY",
        "candidate_id": candidate_id,
        "review_name": review_name,
        "title": str(entry.get("title") or ""),
        "bvid": str(entry.get("bvid") or ""),
        "source_media": str(source_media),
        "source_media_sha256": sha256_file(source_media),
        "text_final_srt": str(text_final),
        "text_final_srt_sha256": sha256_file(text_final),
        "speaker_override": str(override_path) if override_path else None,
        "speaker_override_sha256": sha256_file(override_path) if override_path else None,
        "subtitle_style": SPEAKER_SUBTITLE_STYLE_ID,
        "speaker_counts": speaker_counts,
        "source_cue_count": speaker_manifest.get("source_cue_count"),
        "output_cue_count": speaker_manifest.get("output_cue_count"),
        "upload_authorized": False,
        "artifacts": artifacts,
        "completed_at": _utc_now(),
    }
    _write_json(result_path, result)
    return result


def _write_review_csv(path: Path, results: list[Mapping[str, object]]) -> None:
    buffer = io.StringIO()
    fields = [
        "review_name", "candidate_id", "bvid", "title", "status", "李豆沙_cues",
        "连线_cues", "video", "text_final_srt", "speaker_srt", "ass", "speaker_manifest",
    ]
    writer = csv.DictWriter(buffer, fieldnames=fields)
    writer.writeheader()
    for result in results:
        artifacts = result.get("artifacts") or {}
        counts = result.get("speaker_counts") or {}
        writer.writerow(
            {
                "review_name": result.get("review_name"),
                "candidate_id": result.get("candidate_id"),
                "bvid": result.get("bvid"),
                "title": result.get("title"),
                "status": result.get("status"),
                "李豆沙_cues": counts.get("李豆沙"),
                "连线_cues": counts.get("连线"),
                "video": (artifacts.get("video") or {}).get("path"),
                "text_final_srt": (artifacts.get("text_final_srt") or {}).get("path"),
                "speaker_srt": (artifacts.get("speaker_srt") or {}).get("path"),
                "ass": (artifacts.get("ass") or {}).get("path"),
                "speaker_manifest": (artifacts.get("speaker_manifest") or {}).get("path"),
            }
        )
    atomic_write_text(path, buffer.getvalue())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument(
        "--speaker-python",
        type=Path,
        default=Path(os.environ.get("AUTOSLICE_SPEAKER_PYTHON", "/opt/bilive/autoslice/venv-diar/bin/python")),
    )
    args = parser.parse_args(argv)

    plan_path = args.plan.resolve(strict=True)
    plan = validate_plan(json.loads(plan_path.read_text(encoding="utf-8")))
    plan_sha256 = sha256_file(plan_path)
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    batch_path = output_dir / "batch-manifest.json"
    if batch_path.is_file():
        old = json.loads(batch_path.read_text(encoding="utf-8"))
        if old.get("plan_sha256") != plan_sha256:
            raise BatchSpeakerReviewError("output directory is bound to a different plan hash")

    results: list[dict[str, object]] = []
    failures: list[dict[str, str]] = []
    for entry in plan["entries"]:
        try:
            result = build_review_item(
                entry,
                output_dir=output_dir,
                speaker_python=args.speaker_python,
                resume=args.resume,
            )
            results.append(result)
        except Exception as exc:
            failures.append(
                {
                    "candidate_id": str(entry["candidate_id"]),
                    "review_name": str(entry["review_name"]),
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
        batch = {
            "schema_version": BATCH_SCHEMA,
            "status": "BLOCKED" if failures else "RUNNING",
            "date": plan.get("date"),
            "plan": str(plan_path),
            "plan_sha256": plan_sha256,
            "subtitle_style": SPEAKER_SUBTITLE_STYLE_ID,
            "style_contract": {
                "李豆沙": LDS_SAPPHIRE_STYLE,
                "连线": GUEST_WHITE_STYLE,
                "shadow_identity": "李豆沙",
                "production_labels": ["李豆沙", "连线"],
            },
            "upload_authorized": False,
            "expected_count": len(plan["entries"]),
            "ready_count": len(results),
            "failures": failures,
            "items": results,
            "updated_at": _utc_now(),
        }
        _write_json(batch_path, batch)

    _write_review_csv(output_dir / "review_manifest.csv", results)
    batch["status"] = "READY" if not failures and len(results) == len(plan["entries"]) else "BLOCKED"
    batch["completed_at"] = _utc_now()
    _write_json(batch_path, batch)
    print(json.dumps({"status": batch["status"], "ready": len(results), "failed": len(failures)}, ensure_ascii=False))
    return 0 if batch["status"] == "READY" else 3


if __name__ == "__main__":
    raise SystemExit(main())
