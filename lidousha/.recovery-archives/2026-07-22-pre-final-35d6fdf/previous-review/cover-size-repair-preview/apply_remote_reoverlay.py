#!/usr/bin/env python3
"""One-shot, hash-bound no-upload repair for the reviewed 2026-07-22 cover.

The existing images.edit background remains immutable.  This operator renders a
new local title layer, binds it transactionally to the already-delivered video
and active documents, updates the review manifest, and writes an audit receipt.
"""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Mapping


REPO = Path(os.environ.get("AUTOSLICE_REPO", "/opt/bilive/autoslice/recovery/2026-07-22/full-rerun/repo"))
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

# Import the concrete runner first so RunnerProxy users resolve this isolated
# runtime rather than the production repo beside it.
import scripts.free_session_autoslice as runner  # noqa: E402
from scripts.authorized_upload import (  # noqa: E402
    DEFAULT_UPLOAD_LOCK,
    exclusive_upload_lock,
)
from src.autoslice.cover_generation import (  # noqa: E402
    COVER_MIN_TALK_FONT_SIZE,
    LidoushaCoverArtDirection,
    _overlay_lidousha_cover_title,
)
from src.autoslice.cover_repair import (  # noqa: E402
    _active_cover_documents,
    _bind_repaired_cover,
    _cover_binding_valid,
    _validate_repaired_cover_generation,
)


EXPECTED_BASE = Path("/opt/bilive/autoslice/recovery/2026-07-22/full-rerun")
EXPECTED_COMMIT = "3f0722eca2772f626bcbc71f65dd1bda119ce707"
PLAN_SCHEMA = "reviewed-cover-text-reoverlay-plan.v1"


class RepairError(RuntimeError):
    pass


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise RepairError(f"required regular file is missing: {path}")
    return sha256_bytes(path.read_bytes())


def normalized_sha(value: object) -> str:
    text = str(value or "")
    if text.startswith("sha256:"):
        text = text[7:]
    if len(text) != 64 or any(c not in "0123456789abcdef" for c in text):
        raise RepairError(f"invalid SHA-256: {value!r}")
    return text


def require_hash(path: Path, expected: object, label: str) -> None:
    actual = sha256_file(path)
    if actual != normalized_sha(expected):
        raise RepairError(f"{label} hash drift: {path}: expected={expected} actual={actual}")


def read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RepairError(f"{label} is unreadable: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RepairError(f"{label} must be a JSON object: {path}")
    return value


def canonical_record_sha(record: Mapping[str, Any]) -> str:
    return sha256_bytes(
        json.dumps(
            record,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def assert_no_upload_processes() -> None:
    markers = (b"authorized_upload.py\x00upload", b"do_upload.sh", b"biliup")
    offenders: list[str] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            command = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        if any(marker in command for marker in markers):
            offenders.append(f"{entry.name}:{command.replace(bytes([0]), b' ')[:240].decode(errors='replace')}")
    if offenders:
        raise RepairError(f"upload-capable process is active: {offenders}")


def acquire_runner_lock(path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        os.close(descriptor)
        raise RepairError(f"isolated runner lock is busy: {path}") from exc
    return descriptor


def validate_plan(plan_path: Path) -> tuple[dict[str, Any], str]:
    if plan_path.is_symlink() or not plan_path.is_file():
        raise RepairError(f"plan must be a regular file: {plan_path}")
    raw = plan_path.read_bytes()
    plan = json.loads(raw)
    if not isinstance(plan, dict) or plan.get("schema_version") != PLAN_SCHEMA:
        raise RepairError(f"plan schema must be {PLAN_SCHEMA}")
    if plan.get("upload_enabled") is not False or plan.get("date") != "2026-07-22":
        raise RepairError("plan date/no-upload boundary is invalid")
    if plan.get("candidate_id") != "auto_193450_670_909":
        raise RepairError("plan candidate is outside this one-shot operator")
    return plan, sha256_bytes(raw)


def validate_envelope(plan: Mapping[str, Any], state: Mapping[str, Any]) -> tuple[dict[str, Any], Path, Path]:
    matches = [
        row
        for row in state.get("picks", []) or []
        if isinstance(row, dict) and row.get("candidate_id") == plan["candidate_id"]
    ]
    if len(matches) != 1:
        raise RepairError(f"state candidate cardinality is {len(matches)}, expected 1")
    record = matches[0]
    if record.get("title") != plan.get("title"):
        raise RepairError("state title drifted from the reviewed plan")
    actual_record_sha = canonical_record_sha(record)
    if actual_record_sha != normalized_sha(plan.get("expected_state_record_sha256")):
        raise RepairError(
            f"state record drift: expected={plan.get('expected_state_record_sha256')} actual={actual_record_sha}"
        )

    media = Path(str(plan["media"]["path"]))
    cover = Path(str(plan["active_cover"]["path"]))
    require_hash(media, plan["media"]["sha256"], "media")
    require_hash(cover, plan["active_cover"]["sha256"], "active cover")
    if record.get("cover_path") != str(cover) or normalized_sha(record.get("cover_sha256")) != sha256_file(cover):
        raise RepairError("state active-cover authority drifted")

    source = plan["source_generation"]
    source_manifest_path = Path(str(source["path"]))
    source_binding_path = Path(str(source["binding_path"]))
    background = Path(str(source["ai_background"]))
    require_hash(source_manifest_path, source["sha256"], "source generation")
    require_hash(source_binding_path, source["binding_sha256"], "source binding")
    require_hash(background, source["ai_background_sha256"], "AI background")
    if record.get("cover_generation_path") != str(source_manifest_path):
        raise RepairError("state source-generation pointer drifted")
    if record.get("cover_binding_path") != str(source_binding_path):
        raise RepairError("state source-binding pointer drifted")
    if not _cover_binding_valid(str(plan["date"]), record, media, cover):
        raise RepairError("current cover binding is invalid before repair")

    media_sha = "sha256:" + sha256_file(media)
    active_documents = _active_cover_documents(
        date=str(plan["date"]),
        candidate_id=str(plan["candidate_id"]),
        title=str(plan["title"]),
        mp4=media,
        media_sha256=media_sha,
    )
    expected_documents = {
        (str(row["path"]), normalized_sha(row["sha256"]))
        for row in plan["active_documents"]
    }
    actual_documents = {(str(path), sha256_file(path)) for path, _ in active_documents}
    if actual_documents != expected_documents:
        raise RepairError(f"active documents drifted: expected={expected_documents} actual={actual_documents}")

    review_manifest = Path(str(plan["review_manifest"]["path"]))
    require_hash(review_manifest, plan["review_manifest"]["sha256"], "review manifest")
    ledger = Path(str(plan["upload_ledger"]["path"]))
    require_hash(ledger, plan["upload_ledger"]["sha256"], "upload ledger")
    return record, media, cover


def render_generation(
    *, plan: Mapping[str, Any], plan_path: Path, plan_sha: str, generated_cover: Path
) -> tuple[dict[str, Any], Path, Path]:
    source = plan["source_generation"]
    source_manifest_path = Path(str(source["path"]))
    source_generation = read_json(source_manifest_path, "source generation")
    if source_generation.get("font_size") != plan["active_cover"]["font_size"]:
        raise RepairError("source manifest no longer records the reviewed 91px failure")
    if source_generation.get("layout") != plan["active_cover"]["layout"]:
        raise RepairError("source layout drifted")

    overlay_plan = plan["reviewed_overlay"]
    art = source_generation.get("art_direction")
    if not isinstance(art, dict) or art.get("is_song") is not False:
        raise RepairError("source art direction is missing or not a talk cover")
    direction = LidoushaCoverArtDirection(
        role=str(art["role"]),
        expression_en=str(art["expression_en"]),
        background_style=str(art["background_style"]),
        layout=str(overlay_plan["layout"]),
        hook_color=str(art["hook_color"]),
        is_song=False,
        hook_word=str(overlay_plan["hook_word"]),
    )
    overlay = _overlay_lidousha_cover_title(
        Path(str(source["ai_background"])),
        generated_cover,
        cover_text=str(overlay_plan["cover_text"]),
        art_direction=direction,
    )
    expected_lines = list(overlay_plan["rendered_lines"])
    if overlay.get("rendered_lines") != expected_lines:
        raise RepairError(f"rendered lines drifted: {overlay.get('rendered_lines')}")
    if overlay.get("layout") != overlay_plan["layout"]:
        raise RepairError(f"rendered layout drifted: {overlay.get('layout')}")
    if overlay.get("font_size") != overlay_plan["reviewed_preview_font_size"]:
        raise RepairError(f"reviewed 241px render drifted: {overlay.get('font_size')}")
    if overlay.get("font_size", 0) < max(
        int(overlay_plan["minimum_font_size"]), COVER_MIN_TALK_FONT_SIZE
    ):
        raise RepairError("rendered talk title is below the hard readability floor")

    generated_sha = sha256_file(generated_cover)
    generation = copy.deepcopy(source_generation)
    generation.pop("reviewed_rebind", None)
    generation.update(overlay)
    generation.update(
        {
            "cover_text": str(overlay_plan["cover_text"]),
            "cover_punch": [],
            "final_cover": str(generated_cover),
            "final_cover_sha256": "sha256:" + generated_sha,
        }
    )
    generation["art_direction"] = {
        **art,
        "layout": str(overlay_plan["layout"]),
        "hook_word": str(overlay_plan["hook_word"]),
        "cover_punch": [],
    }
    generation["reviewed_text_reoverlay"] = {
        "schema_version": PLAN_SCHEMA,
        "plan_path": str(plan_path),
        "plan_sha256": plan_sha,
        "source_generation_path": str(source_manifest_path),
        "source_generation_sha256": "sha256:" + sha256_file(source_manifest_path),
        "source_binding_path": str(source["binding_path"]),
        "source_binding_sha256": "sha256:" + normalized_sha(source["binding_sha256"]),
        "source_cover_sha256": "sha256:" + normalized_sha(plan["active_cover"]["sha256"]),
        "ai_background_sha256": "sha256:" + normalized_sha(source["ai_background_sha256"]),
        "new_image_request_made": False,
        "upload_enabled": False,
        "applied_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    generation_path = generated_cover.with_suffix(".cover_generation.json")
    runner._atomic_write_json_file(generation_path, generation)
    validated, validated_path = _validate_repaired_cover_generation(
        cover=generated_cover,
        title=str(plan["title"]),
        candidate_id=str(plan["candidate_id"]),
    )
    if validated != generation or validated_path != generation_path:
        raise RepairError("new immutable generation failed canonical validation")

    from PIL import Image

    with Image.open(generated_cover) as image:
        rgb = image.convert("RGB")
        feed_qa = generated_cover.with_name("qa.feed-center-4x3.png")
        thumb_qa = generated_cover.with_name("qa.thumbnail-320x180.png")
        rgb.crop((240, 0, 1680, 1080)).save(feed_qa)
        rgb.resize((320, 180), Image.Resampling.LANCZOS).save(thumb_qa)
    return generation, feed_qa, thumb_qa


def update_review_manifest(
    *, plan: Mapping[str, Any], generation: Mapping[str, Any], cover_sha: str
) -> Path:
    path = Path(str(plan["review_manifest"]["path"]))
    review = read_json(path, "review manifest")
    items = [
        row
        for row in review.get("items", []) or []
        if isinstance(row, dict) and row.get("candidate_id") == plan["candidate_id"]
    ]
    if len(items) != 1:
        raise RepairError(f"review manifest candidate cardinality is {len(items)}, expected 1")
    item = items[0]
    item["cover_generation"] = {
        "method": generation["method"],
        "model": generation["model"],
        "background_style": generation["background_style"],
        "cover_text": generation["cover_text"],
        "rendered_lines": generation["rendered_lines"],
        "layout": generation["layout"],
        "font_size": generation["font_size"],
        "min_talk_font_size": generation["min_talk_font_size"],
        "final_cover_sha256": "sha256:" + cover_sha,
        "reviewed_text_reoverlay": generation["reviewed_text_reoverlay"],
    }
    runner._atomic_write_json_file(path, review)
    return path


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        raise SystemExit(f"usage: {argv[0]} PLAN.json")
    if runner.BASE.resolve() != EXPECTED_BASE or REPO.resolve() != (EXPECTED_BASE / "repo"):
        raise RepairError(f"wrong isolated runtime: base={runner.BASE} repo={REPO}")
    disabled = EXPECTED_BASE / "DISABLED"
    if not disabled.is_file():
        raise RepairError(f"isolated kill switch is not active: {disabled}")
    deployed_commit = (REPO / "DEPLOYED_COMMIT").read_text(encoding="utf-8").split()[0]
    if deployed_commit != EXPECTED_COMMIT:
        raise RepairError(f"isolated code commit drift: {deployed_commit}")
    if DEFAULT_UPLOAD_LOCK != EXPECTED_BASE / "upload.lock":
        raise RepairError(f"wrong upload lock: {DEFAULT_UPLOAD_LOCK}")

    plan_path = Path(argv[1]).resolve(strict=True)
    plan, plan_sha = validate_plan(plan_path)
    receipt_dir = EXPECTED_BASE / "reports/operator/2026-07-22-cover-title-scale-3f0722e"
    receipt_path = receipt_dir / "receipt.json"
    if receipt_path.exists():
        raise RepairError(f"completed receipt already exists: {receipt_path}")

    runner_lock_fd = acquire_runner_lock(EXPECTED_BASE / "runner.lock")
    try:
        with exclusive_upload_lock(DEFAULT_UPLOAD_LOCK):
            assert_no_upload_processes()
            ledger_path = Path(str(plan["upload_ledger"]["path"]))
            ledger_before = ledger_path.read_bytes()
            state = read_json(EXPECTED_BASE / "state/2026-07-22.json", "isolated state")
            record, media, cover = validate_envelope(plan, state)

            generation_dir = (
                EXPECTED_BASE
                / "out/2026-07-22"
                / str(plan["candidate_id"])
                / "cover_repair/generations"
                / f"reviewed-title-scale-{plan_sha[:16]}"
            )
            generation_dir.mkdir(parents=False, exist_ok=False)
            generated_cover = generation_dir / "final.cover.png"
            generation, feed_qa, thumb_qa = render_generation(
                plan=plan,
                plan_path=plan_path,
                plan_sha=plan_sha,
                generated_cover=generated_cover,
            )

            superseded = record.get("superseded_cover_authorities")
            if not isinstance(superseded, list):
                superseded = []
                record["superseded_cover_authorities"] = superseded
            superseded.append(
                {
                    "schema_version": "superseded-cover-authority.v1",
                    "reason": "COVER_TITLE_TOO_SMALL_91PX",
                    "cover_sha256": record.get("cover_sha256"),
                    "cover_generation_path": record.get("cover_generation_path"),
                    "cover_generation_sha256": record.get("cover_generation_sha256"),
                    "cover_binding_path": record.get("cover_binding_path"),
                    "cover_binding_sha256": record.get("cover_binding_sha256"),
                    "reviewed_cover_repair": copy.deepcopy(record.get("reviewed_cover_repair")),
                    "reviewed_cover_rebind": copy.deepcopy(record.get("reviewed_cover_rebind")),
                    "superseded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                }
            )
            record.pop("reviewed_cover_repair", None)
            record.pop("reviewed_cover_rebind", None)

            _bind_repaired_cover(
                str(plan["date"]), record, media, cover, generated_cover
            )
            cover_sha = sha256_file(cover)
            record["reviewed_cover_text_reoverlay"] = {
                "schema_version": PLAN_SCHEMA,
                "status": "FINALIZED",
                "plan_path": str(plan_path),
                "plan_sha256": plan_sha,
                "commit": EXPECTED_COMMIT,
                "source_cover_sha256": "sha256:" + normalized_sha(plan["active_cover"]["sha256"]),
                "cover_sha256": "sha256:" + cover_sha,
                "font_size": generation["font_size"],
                "layout": generation["layout"],
                "feed_qa": str(feed_qa),
                "thumbnail_qa": str(thumb_qa),
                "receipt": str(receipt_path),
                "upload_enabled": False,
                "finalized_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }

            runner.write_state(str(plan["date"]), state)
            review_manifest = update_review_manifest(
                plan=plan, generation=generation, cover_sha=cover_sha
            )
            runner.write_reports(str(plan["date"]), state)

            persisted_state = read_json(
                EXPECTED_BASE / "state/2026-07-22.json", "persisted isolated state"
            )
            persisted = next(
                row
                for row in persisted_state["picks"]
                if row.get("candidate_id") == plan["candidate_id"]
            )
            if not _cover_binding_valid(str(plan["date"]), persisted, media, cover):
                raise RepairError("new cover binding is invalid after state persistence")
            require_hash(media, plan["media"]["sha256"], "media after repair")
            if ledger_path.read_bytes() != ledger_before:
                raise RepairError("upload ledger changed during no-upload cover repair")
            assert_no_upload_processes()

            active_documents = _active_cover_documents(
                date=str(plan["date"]),
                candidate_id=str(plan["candidate_id"]),
                title=str(plan["title"]),
                mp4=media,
                media_sha256="sha256:" + sha256_file(media),
            )
            receipt = {
                "schema_version": "reviewed-cover-text-reoverlay-receipt.v1",
                "status": "COMPLETED_NO_UPLOAD",
                "date": plan["date"],
                "candidate_id": plan["candidate_id"],
                "commit": EXPECTED_COMMIT,
                "plan_path": str(plan_path),
                "plan_sha256": plan_sha,
                "operator_script": {
                    "path": str(Path(__file__).resolve()),
                    "sha256": sha256_file(Path(__file__).resolve()),
                },
                "old_cover_sha256": plan["active_cover"]["sha256"],
                "new_cover_path": str(cover),
                "new_cover_sha256": cover_sha,
                "media_path": str(media),
                "media_sha256": sha256_file(media),
                "font_size_before": plan["active_cover"]["font_size"],
                "font_size_after": generation["font_size"],
                "layout_before": plan["active_cover"]["layout"],
                "layout_after": generation["layout"],
                "cover_text": generation["cover_text"],
                "rendered_lines": generation["rendered_lines"],
                "ai_background_sha256": normalized_sha(plan["source_generation"]["ai_background_sha256"]),
                "new_image_request_made": False,
                "cover_binding_valid": True,
                "active_documents": [
                    {"path": str(path), "sha256": sha256_file(path)}
                    for path, _ in active_documents
                ],
                "review_manifest": {
                    "path": str(review_manifest),
                    "sha256": sha256_file(review_manifest),
                },
                "feed_qa": {"path": str(feed_qa), "sha256": sha256_file(feed_qa)},
                "thumbnail_qa": {"path": str(thumb_qa), "sha256": sha256_file(thumb_qa)},
                "upload_ledger": {
                    "path": str(ledger_path),
                    "sha256": sha256_bytes(ledger_before),
                    "unchanged": True,
                },
                "upload_enabled": False,
                "completed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            runner._atomic_write_json_file(receipt_path, receipt)
            print(json.dumps(receipt, ensure_ascii=False, indent=2))
    finally:
        fcntl.flock(runner_lock_fd, fcntl.LOCK_UN)
        os.close(runner_lock_fd)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
