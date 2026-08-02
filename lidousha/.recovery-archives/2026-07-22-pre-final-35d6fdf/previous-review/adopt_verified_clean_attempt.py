#!/usr/bin/env python3
"""Adopt the fully-produced CLEAN artifact from the first 6e458c2 transaction.

The producer completed and delivered this artifact, but the task-local operator
then rejected it because its receipt accepted only RESOLVED_BY_* and omitted
the stronger CLEAN/no-findings outcome.  A later rerun failed closed on cue
segmentation drift.  This transaction verifies the first immutable delivery,
reconstructs its deterministic raw recut byte-for-byte, restores its bound out
sidecars, adopts only the target delivery files, and keeps upload disabled.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path


BASE = Path("/opt/bilive/autoslice/recovery/2026-07-22/full-rerun")
REPO = BASE / "repo"
DATE = "2026-07-22"
CANDIDATE_ID = "auto_193450_670_909"
COMMIT = "6e458c292f63f14da3e96a273930d713b9e66ac1"
SOURCE_RUN = BASE / "reports/operator/2026-07-22-official-replay-target-6e458c2"
SOURCE_DELIVERY = SOURCE_RUN / "delivery-failed-attempt"
RECONSTRUCTED_RAW = (
    BASE
    / "reports/operator/reconstruct-first-6e458c2"
    / f"{CANDIDATE_ID}.recut.mp4"
)
ADOPT_ROOT = (
    BASE / "reports/operator/2026-07-22-official-replay-target-6e458c2-adopt-clean"
)
TITLE = (
    "【李豆沙】南町当面追问：为什么提到我就要“最最最喜欢”？"
    "越解释越像海王"
)
DELIVERY_NAME = "联动对象当面追问“最最最最喜欢”是真"
REPLAY_SHA256 = (
    "0eb2778dc53e5eabbccae089e5db92d3fb3662d90e1dd2ddbe7765436718989a"
)
SOURCE_ALIAS_ID = "20260722-official-replay-bv1fjg16xex6"
EXPECTED_RAW_SHA256 = (
    "6abf51feaf677799f8cc68ff0f6d404a2e42aabf99f3758bf7a1947b5c55a176"
)
EXPECTED_BURNED_SHA256 = (
    "1cbc05943ef24ebb248810028cb25b03ae668bb09fbfb17e9659d9d898f1007d"
)
EXPECTED_COVER_SHA256 = (
    "969b7efdf6cf244a25b0f6add1e2e041cb46c72e5a808264a7b9e7a2d0578e42"
)
EXPECTED_UPLOAD_LEDGER_SHA256 = (
    "f3775e2e56cf78707fc249a159a0e97ad394c15854076e7f9315f445535ab225"
)

os.environ["AUTOSLICE_BASE"] = str(BASE)
sys.path.insert(0, str(REPO))
from scripts import free_session_autoslice as runner  # noqa: E402


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_manifest(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): sha256(path)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def atomic_copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.adopt-{os.getpid()}")
    shutil.copy2(source, temporary)
    os.replace(temporary, target)


def atomic_json(document: dict, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.adopt-{os.getpid()}")
    temporary.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, target)


def ledger_snapshot() -> dict[str, object]:
    path = BASE / "reports/upload_ledger.jsonl"
    return {
        "path": str(path),
        "exists": path.is_file(),
        "size": path.stat().st_size if path.is_file() else None,
        "sha256": sha256(path) if path.is_file() else None,
    }


def target_paths(root: Path) -> list[Path]:
    allowed = {
        ".mp4",
        ".srt",
        ".record.json",
        ".chat-authority.json",
        ".redelivery-baseline.json",
        ".cover.png",
    }
    paths = [
        path
        for path in root.iterdir()
        if path.is_file()
        and path.name.startswith(DELIVERY_NAME)
        and path.name.removeprefix(DELIVERY_NAME) in allowed
    ]
    suffixes = {path.name.removeprefix(DELIVERY_NAME) for path in paths}
    if suffixes != allowed:
        raise RuntimeError(
            f"source target sidecars drifted: expected={sorted(allowed)} "
            f"actual={sorted(suffixes)}"
        )
    return sorted(paths)


def verify_source_truth(chat: dict[str, object], spec: dict) -> dict:
    audit = chat.get("source_subtitle_truth_audit")
    if not isinstance(audit, dict) or audit.get("status") not in {
        "APPLIED",
        "ALREADY_SATISFIED",
    }:
        raise RuntimeError("source truth did not apply")
    if audit.get("failures"):
        raise RuntimeError("source truth has failures")
    rows = [
        row
        for key in ("applied", "satisfied")
        for row in audit.get(key, [])
        if isinstance(row, dict)
    ]
    piece_start = min(int(piece["start_ms"]) for piece in spec["pieces"])
    piece_end = max(int(piece["end_ms"]) for piece in spec["pieces"])
    ledger = json.loads(
        (REPO / "assets/lidousha/subtitle_truth_ledger.v1.json").read_text()
    )
    expected_ids = {
        str(row["truth_id"])
        for row in ledger["entries"]
        if row.get("recording_basename")
        == "22966160_20260722-19-35-15.mp4"
        and piece_start <= int(row["source_start_ms"])
        and int(row["source_end_ms"]) <= piece_end
    }
    actual_ids = {str(row.get("truth_id")) for row in rows}
    if not expected_ids or actual_ids != expected_ids:
        raise RuntimeError(
            f"source truth coverage drifted: expected={sorted(expected_ids)} "
            f"actual={sorted(actual_ids)}"
        )
    for row in rows:
        aliases = row.get("source_aliases")
        if not isinstance(aliases, list) or len(aliases) != 1:
            raise RuntimeError(f"{row.get('truth_id')}: alias evidence missing")
        alias = aliases[0]
        if (
            alias.get("alias_id") != SOURCE_ALIAS_ID
            or alias.get("alias_source_sha256")
            != "sha256:" + REPLAY_SHA256
            or alias.get("alias_timeline_offset_ms") != 0
        ):
            raise RuntimeError(f"{row.get('truth_id')}: alias evidence drifted")
    return audit


def verify_candidate() -> tuple[dict, dict, dict, dict]:
    if not SOURCE_DELIVERY.is_dir():
        raise RuntimeError("immutable source delivery is missing")
    spec = json.loads((SOURCE_RUN / f"spec_{CANDIDATE_ID}.json").read_text())
    if any(
        Path(str(piece.get("remote_media") or "")).name
        != "22966160_20260722-19-34-50.mp4"
        or piece.get("source_media_sha256") != "sha256:" + REPLAY_SHA256
        for piece in spec.get("pieces", [])
    ):
        raise RuntimeError("official replay source binding drifted")

    record_path = SOURCE_DELIVERY / f"{DELIVERY_NAME}.record.json"
    chat_path = SOURCE_DELIVERY / f"{DELIVERY_NAME}.chat-authority.json"
    srt_path = SOURCE_DELIVERY / f"{DELIVERY_NAME}.srt"
    media_path = SOURCE_DELIVERY / f"{DELIVERY_NAME}.mp4"
    cover_path = SOURCE_DELIVERY / f"{DELIVERY_NAME}.cover.png"
    baseline_path = SOURCE_DELIVERY / f"{DELIVERY_NAME}.redelivery-baseline.json"
    record = json.loads(record_path.read_text())
    chat = json.loads(chat_path.read_text())
    baseline = json.loads(baseline_path.read_text())
    staging = record.get("publish_staging") or {}
    if (
        staging.get("title") != TITLE
        or staging.get("upload_enabled") is not False
        or staging.get("title_authority_status") != "RESOLVED_MANUAL"
    ):
        raise RuntimeError("title/upload authority drifted")
    hashes = record.get("artifact_hashes") or {}
    expected = {
        media_path: str(hashes.get("burned_video_sha256") or "").removeprefix(
            "sha256:"
        ),
        srt_path: str(hashes.get("subtitle_sha256") or "").removeprefix("sha256:"),
        chat_path: str(hashes.get("chat_authority_audit_sha256") or "").removeprefix(
            "sha256:"
        ),
        baseline_path: str(
            hashes.get("redelivery_baseline_audit_sha256") or ""
        ).removeprefix("sha256:"),
    }
    if any(not value or sha256(path) != value for path, value in expected.items()):
        raise RuntimeError("candidate delivery hash binding failed")
    if sha256(media_path) != EXPECTED_BURNED_SHA256:
        raise RuntimeError("candidate burned media drifted")
    if sha256(cover_path) != EXPECTED_COVER_SHA256:
        raise RuntimeError("reviewed cover bytes drifted")
    if sha256(RECONSTRUCTED_RAW) != EXPECTED_RAW_SHA256:
        raise RuntimeError("deterministic raw recut reconstruction drifted")
    if str(hashes.get("video_sha256") or "").removeprefix("sha256:") != (
        EXPECTED_RAW_SHA256
    ):
        raise RuntimeError("record raw recut hash drifted")

    source_language = chat.get("final_source_language_preservation_audit")
    if (
        not isinstance(source_language, dict)
        or source_language.get("status") != "CLEAN"
        or source_language.get("unproven_foreign_introductions")
    ):
        raise RuntimeError("candidate is not source-language CLEAN")
    truth_audit = verify_source_truth(chat, spec)
    if (
        baseline.get("status") not in {"APPLIED", "ALREADY_SATISFIED"}
        or baseline.get("failures")
        or baseline.get("baseline_sha256")
        != spec["subtitle_redelivery_baseline"]["sha256"]
    ):
        raise RuntimeError("hash-bound redelivery baseline failed")

    srt_text = srt_path.read_text()
    required_surfaces = (
        "这不是主播最最最最喜欢的南町nightin吗，llnnhhb",
        "乱说的啊",
        "南町nightin哦",
        "所以就是，就是最最最最喜欢的南町nightin",
        "首发是病院坂灵",
    )
    if any(surface not in srt_text for surface in required_surfaces):
        raise RuntimeError("reviewed subtitle surface is absent")
    if any(surface in srt_text for surface in ("やさしい", "鼠神")):
        raise RuntimeError("superseded foreign/entity surface survived")
    if "nighting" in srt_text.lower():
        raise RuntimeError("superseded nighting surface survived")

    intro = (record.get("burned_preview") or {}).get("branding_intro") or {}
    manifest = json.loads(
        (REPO / "assets/lidousha/intro/branding_intro.v1.json").read_text()
    )
    roster = {
        str(row["intro_id"]): row["video"] for row in manifest["intros"]
    }
    intro_asset = roster.get(str(intro.get("intro_id") or ""))
    if (
        intro.get("status") != "PREPENDED"
        or not isinstance(intro_asset, dict)
        or intro.get("intro_media_sha256") != intro_asset.get("sha256")
        or (intro.get("verification") or {}).get("full_decode") != "clean"
    ):
        raise RuntimeError("rotating intro contract failed")
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(media_path),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-f",
            "null",
            "-",
        ],
        check=True,
        timeout=900,
    )
    return record, chat, truth_audit, spec


def restore_tree(backup: Path, current: Path, failed: Path) -> None:
    if current.exists():
        os.replace(current, failed)
    shutil.copytree(backup, current)


def main() -> int:
    deployed = (REPO / "DEPLOYED_COMMIT").read_text().split()[0]
    if deployed != COMMIT or not (BASE / "DISABLED").exists():
        raise RuntimeError("isolated runtime/kill-switch boundary drifted")
    if ADOPT_ROOT.exists():
        raise RuntimeError(f"adoption root already exists: {ADOPT_ROOT}")
    upload_before = ledger_snapshot()
    if upload_before.get("sha256") != EXPECTED_UPLOAD_LEDGER_SHA256:
        raise RuntimeError("upload ledger drifted before adoption")

    delivery = runner.profile_delivery_root() / DATE
    if file_manifest(delivery) != file_manifest(SOURCE_RUN / "delivery-before"):
        raise RuntimeError("current delivery is not the source transaction baseline")
    source_non_target = {
        name: digest
        for name, digest in file_manifest(SOURCE_DELIVERY).items()
        if not name.startswith(DELIVERY_NAME)
        and name not in {"AUTOSLICE_SUMMARY.md", "review_manifest.json"}
    }
    current_non_target = {
        name: digest
        for name, digest in file_manifest(delivery).items()
        if not name.startswith(DELIVERY_NAME)
        and name not in {"AUTOSLICE_SUMMARY.md", "review_manifest.json"}
    }
    if source_non_target != current_non_target:
        raise RuntimeError("non-target delivery bytes drifted")

    record, chat, truth_audit, spec = verify_candidate()
    recut_root = BASE / "out" / DATE / CANDIDATE_ID / "replacement_recuts"
    recut = recut_root / f"{CANDIDATE_ID}.recut.mp4"
    ass = recut.with_suffix(".final-sapphire72.ass")
    burned = recut.with_suffix(".burned-final-sapphire72.mp4")
    publish = recut.with_suffix(".publish.json")
    if (
        sha256(ass)
        != str((record.get("artifact_hashes") or {}).get("ass_sha256") or "").removeprefix(
            "sha256:"
        )
        or sha256(burned) != EXPECTED_BURNED_SHA256
        or json.loads(publish.read_text()).get("artifact_hashes", {}).get(
            "burned_video_sha256"
        )
        != "sha256:" + EXPECTED_BURNED_SHA256
    ):
        raise RuntimeError("surviving first-attempt burn/publish evidence drifted")

    ADOPT_ROOT.mkdir(parents=True)
    backup_delivery = ADOPT_ROOT / "delivery-before"
    shutil.copytree(delivery, backup_delivery)
    state_path = runner.state_path(DATE)
    shutil.copy2(state_path, ADOPT_ROOT / "state-before.json")
    (ADOPT_ROOT / "upload-ledger-before.json").write_text(
        json.dumps(upload_before, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n"
    )

    out_targets = {
        recut: RECONSTRUCTED_RAW,
        recut.with_suffix(".srt"): SOURCE_DELIVERY / f"{DELIVERY_NAME}.srt",
        recut_root / f"{CANDIDATE_ID}.redelivery-baseline.json": (
            SOURCE_DELIVERY / f"{DELIVERY_NAME}.redelivery-baseline.json"
        ),
        BASE / "out" / DATE / CANDIDATE_ID / f"{CANDIDATE_ID}.chat-authority.json": (
            SOURCE_DELIVERY / f"{DELIVERY_NAME}.chat-authority.json"
        ),
        recut.with_suffix(".record.json"): (
            SOURCE_DELIVERY / f"{DELIVERY_NAME}.record.json"
        ),
    }
    boundary_path = (
        BASE / "out" / DATE / CANDIDATE_ID / f"{CANDIDATE_ID}.boundary_audit.json"
    )
    current_provenance_path = recut.with_suffix(".provenance.json")
    mutable_out_paths = [*out_targets, boundary_path, current_provenance_path]
    out_backup = ADOPT_ROOT / "out-before"
    out_backup.mkdir()
    out_absent: list[str] = []
    for target in mutable_out_paths:
        relative = target.relative_to(BASE / "out")
        backup = out_backup / relative
        if target.is_file():
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, backup)
        else:
            out_absent.append(relative.as_posix())
    (ADOPT_ROOT / "out-absent-before.json").write_text(
        json.dumps(out_absent, indent=2) + "\n"
    )

    try:
        for target, source in out_targets.items():
            atomic_copy(source, target)
        atomic_json(record["boundary_audit"], boundary_path)

        provenance = json.loads(current_provenance_path.read_text())
        provenance["final_recut"].update(
            {
                "start_ms": 9_750,
                "end_ms": 248_990,
                "absolute_source_start_ms": 670_470,
                "absolute_source_end_ms": 909_710,
                "output_path": str(recut.resolve()),
                "output_sha256": EXPECTED_RAW_SHA256,
            }
        )
        provenance["verified_artifact_adoption"] = {
            "schema_version": "verified-artifact-adoption.v1",
            "source_transaction": str(SOURCE_RUN),
            "deterministic_reconstruction": str(RECONSTRUCTED_RAW),
            "raw_recut_sha256": "sha256:" + EXPECTED_RAW_SHA256,
            "upload_enabled": False,
        }
        atomic_json(provenance, current_provenance_path)

        for source in target_paths(SOURCE_DELIVERY):
            atomic_copy(source, delivery / source.name)

        state = runner.read_state(DATE)
        publish_meta = runner.read_publish_meta(BASE / "out" / DATE / CANDIDATE_ID)
        if (
            publish_meta.get("title") != TITLE
            or publish_meta.get("video_sha256")
            != "sha256:" + EXPECTED_BURNED_SHA256
        ):
            raise RuntimeError("first-attempt publish metadata drifted")
        result = {
            "status": "review_ready",
            "operator_rerun": {
                "kind": "verified-clean-artifact-adoption",
                "commit": COMMIT,
                "source_transaction": str(SOURCE_RUN),
                "source_media_sha256": "sha256:" + REPLAY_SHA256,
                "upload_enabled": False,
                "receipt": str(ADOPT_ROOT / "receipt.json"),
            },
        }
        result.update(publish_meta)
        result["title"] = TITLE
        matches = 0
        for index, row in enumerate(state.get("picks", [])):
            if row.get("candidate_id") == CANDIDATE_ID:
                merged = dict(row)
                merged.update(result)
                state["picks"][index] = merged
                matches += 1
        if matches != 1:
            raise RuntimeError("target state row is missing or duplicated")
        runner.write_state(DATE, state)
        runner.write_reports(DATE, state)

        if sha256(delivery / f"{DELIVERY_NAME}.mp4") != EXPECTED_BURNED_SHA256:
            raise RuntimeError("adopted delivery media hash drifted")
        if sha256(recut) != EXPECTED_RAW_SHA256:
            raise RuntimeError("adopted raw recut hash drifted")
        upload_after = ledger_snapshot()
        if upload_after != upload_before:
            raise RuntimeError("upload ledger changed during adoption")

        receipt = {
            "schema_version": "verified-clean-artifact-adoption-receipt.v1",
            "status": "COMPLETED",
            "date": DATE,
            "candidate_id": CANDIDATE_ID,
            "commit": COMMIT,
            "title": TITLE,
            "source_transaction": str(SOURCE_RUN),
            "source_media_sha256": "sha256:" + REPLAY_SHA256,
            "source_truth_status": truth_audit["status"],
            "source_truth_ids": sorted(
                row["truth_id"]
                for key in ("applied", "satisfied")
                for row in truth_audit.get(key, [])
            ),
            "source_language_preservation": chat[
                "final_source_language_preservation_audit"
            ],
            "redelivery_baseline": record["redelivery_baseline"],
            "raw_recut_sha256": "sha256:" + EXPECTED_RAW_SHA256,
            "media_sha256": "sha256:" + EXPECTED_BURNED_SHA256,
            "cover_sha256": "sha256:" + EXPECTED_COVER_SHA256,
            "branding_intro": (record.get("burned_preview") or {}).get(
                "branding_intro"
            ),
            "non_target_delivery_byte_diffs": [],
            "upload_enabled": False,
            "upload_ledger_before": upload_before,
            "upload_ledger_after": upload_after,
            "adopted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        (ADOPT_ROOT / "receipt.json").write_text(
            json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n"
        )
        print(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except Exception:
        failed = ADOPT_ROOT / "delivery-failed-attempt"
        restore_tree(backup_delivery, delivery, failed)
        restore = state_path.with_name(state_path.name + ".adoption-restore")
        shutil.copy2(ADOPT_ROOT / "state-before.json", restore)
        os.replace(restore, state_path)
        for target in mutable_out_paths:
            relative = target.relative_to(BASE / "out")
            backup = out_backup / relative
            if backup.is_file():
                atomic_copy(backup, target)
            elif relative.as_posix() in out_absent:
                target.unlink(missing_ok=True)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
