#!/usr/bin/env python3
"""Transactional no-upload rerender of the 7/22 official-replay target clip."""

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
REPLAY_BASENAME = "22966160_20260722-19-34-50.mp4"
REPLAY_SHA256 = (
    "0eb2778dc53e5eabbccae089e5db92d3fb3662d90e1dd2ddbe7765436718989a"
)
SOURCE_ALIAS_ID = "20260722-official-replay-bv1fjg16xex6"
TITLE = (
    "【李豆沙】南町当面追问：为什么提到我就要“最最最喜欢”？"
    "越解释越像海王"
)
RUN_ROOT = BASE / "reports/operator/2026-07-22-official-replay-target-6e458c2-r2"

os.environ["AUTOSLICE_BASE"] = str(BASE)
sys.path.insert(0, str(REPO))
from scripts import free_session_autoslice as runner  # noqa: E402


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ledger_snapshot() -> dict[str, object]:
    path = BASE / "reports/upload_ledger.jsonl"
    return {
        "path": str(path),
        "exists": path.is_file(),
        "size": path.stat().st_size if path.is_file() else None,
        "sha256": sha256(path) if path.is_file() else None,
    }


def restore_delivery(backup: Path, delivery: Path) -> None:
    failed = RUN_ROOT / "delivery-failed-attempt"
    if delivery.exists():
        os.replace(delivery, failed)
    shutil.copytree(backup, delivery)


def intro_roster() -> dict[str, dict[str, object]]:
    document = json.loads(
        (REPO / "assets/lidousha/intro/branding_intro.v1.json").read_text()
    )
    return {
        str(row["intro_id"]): {
            "sha256": str(row["video"]["sha256"]),
            "duration_ms": int(row["video"]["duration_ms"]),
        }
        for row in document["intros"]
    }


def verify_source_truth(chat_authority: dict[str, object], spec: dict) -> dict:
    audit = chat_authority.get("source_subtitle_truth_audit")
    if not isinstance(audit, dict) or audit.get("status") not in {
        "APPLIED",
        "ALREADY_SATISFIED",
    }:
        raise RuntimeError("official replay source truth did not apply")
    if audit.get("failures"):
        raise RuntimeError("official replay source truth has failures")
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


def main() -> int:
    deployed = (REPO / "DEPLOYED_COMMIT").read_text().split()[0]
    if deployed != COMMIT:
        raise RuntimeError(f"isolated DEPLOYED_COMMIT mismatch: {deployed}")
    if RUN_ROOT.exists():
        raise RuntimeError(f"operator root already exists: {RUN_ROOT}")
    if not (BASE / "DISABLED").exists():
        raise RuntimeError("isolated runner must remain disabled during rerender")

    state_path = runner.state_path(DATE)
    state = runner.read_state(DATE)
    picks = {
        str(row.get("candidate_id")): row
        for row in state.get("picks", [])
        if isinstance(row, dict)
    }
    pick = picks.get(CANDIDATE_ID)
    if not isinstance(pick, dict) or pick.get("status") != "review_ready":
        raise RuntimeError("target is not an active review_ready pick")
    if pick.get("title") != TITLE:
        raise RuntimeError("reviewed target title drifted")

    source_spec_path = BASE / "out" / DATE / f"spec_{CANDIDATE_ID}.json"
    spec = json.loads(source_spec_path.read_text())
    name = str(spec["delivery_name"])
    delivery = runner.profile_delivery_root() / DATE
    cover = delivery / f"{name}.cover.png"
    cover_before = sha256(cover)
    if cover_before != (
        "969b7efdf6cf244a25b0f6add1e2e041cb46c72e5a808264a7b9e7a2d0578e42"
    ):
        raise RuntimeError("reviewed target cover drifted before subtitle rerender")

    RUN_ROOT.mkdir(parents=True)
    backup_delivery = RUN_ROOT / "delivery-before"
    shutil.copytree(delivery, backup_delivery)
    shutil.copy2(state_path, RUN_ROOT / "state-before.json")
    upload_before = ledger_snapshot()
    (RUN_ROOT / "upload-ledger-before.json").write_text(
        json.dumps(upload_before, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n"
    )

    for piece in spec.get("pieces", []):
        if Path(str(piece.get("remote_media") or "")).name != REPLAY_BASENAME:
            raise RuntimeError("target spec contains an unexpected source piece")
        piece["source_media_sha256"] = "sha256:" + REPLAY_SHA256
    spec["human_truth_mode"] = "delivery"
    spec["given_title"] = TITLE
    spec["cover_diversity_slot"] = 0
    spec.pop("subtitle_regression", None)
    baseline = backup_delivery / f"{name}.srt"
    spec["subtitle_redelivery_baseline"] = {
        "schema_version": "subtitle-redelivery-baseline.v1",
        "mode": "preserve_text_outside_source_truth",
        "path": str(baseline),
        "sha256": sha256(baseline),
        "authority": (
            "2026-07-22 isolated full-replay review snapshot; preserve every "
            "cue outside the hash-bound source-truth windows"
        ),
    }
    spec_path = RUN_ROOT / f"spec_{CANDIDATE_ID}.json"
    spec_path.write_text(
        json.dumps(spec, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )

    command = [
        sys.executable,
        str(REPO / "scripts/produce_slice_package.py"),
        "--spec",
        str(spec_path),
        "--ssh-host",
        "localhost",
        "--speaker-mode",
        runner.SPEAKER_MODE,
        "--reuse-cover",
    ]
    log_path = RUN_ROOT / "producer.log"
    started = time.time()
    try:
        with log_path.open("w", encoding="utf-8") as sink:
            completed = subprocess.run(
                command,
                cwd=REPO,
                env=runner.child_env_for_date(DATE),
                stdout=sink,
                stderr=subprocess.STDOUT,
                timeout=5400,
                check=False,
            )
        if completed.returncode != 0:
            raise RuntimeError(f"producer failed rc={completed.returncode}")

        record_path = delivery / f"{name}.record.json"
        record = json.loads(record_path.read_text())
        staging = record.get("publish_staging") or {}
        if staging.get("title") != TITLE or staging.get("upload_enabled") is not False:
            raise RuntimeError("title/upload boundary drifted")
        if sha256(cover) != cover_before:
            raise RuntimeError("reviewed cover bytes changed during subtitle rerender")

        chat_path = delivery / f"{name}.chat-authority.json"
        chat_authority = json.loads(chat_path.read_text())
        truth_audit = verify_source_truth(chat_authority, spec)
        source_language = chat_authority.get(
            "final_source_language_preservation_audit"
        )
        source_language_status = (
            source_language.get("status")
            if isinstance(source_language, dict)
            else None
        )
        source_language_clean = (
            source_language_status == "CLEAN"
            and not source_language.get("unproven_foreign_introductions")
        )
        source_language_late_resolved = (
            source_language_status
            in {
                "RESOLVED_BY_REDELIVERY_BASELINE",
                "RESOLVED_BY_SOURCE_SUBTITLE_TRUTH",
            }
            and (source_language.get("deferred_resolution") or {}).get("status")
            == "PASS"
        )
        if not (source_language_clean or source_language_late_resolved):
            raise RuntimeError("deferred foreign-language finding was not resolved")
        redelivery = record.get("redelivery_baseline")
        if (
            not isinstance(redelivery, dict)
            or redelivery.get("status") not in {"APPLIED", "ALREADY_SATISFIED"}
            or redelivery.get("baseline_sha256")
            != spec["subtitle_redelivery_baseline"]["sha256"]
            or redelivery.get("failures")
        ):
            raise RuntimeError("hash-bound redelivery baseline failed")

        srt_text = (delivery / f"{name}.srt").read_text()
        required_surfaces = (
            "这不是主播最最最最喜欢的南町nightin吗，llnnhhb",
            "乱说的啊",
            "南町nightin哦",
            "所以就是，就是最最最最喜欢的南町nightin",
            "首发是病院坂灵",
        )
        if any(surface not in srt_text for surface in required_surfaces):
            raise RuntimeError("one or more reviewed subtitle surfaces are absent")
        if "nighting" in srt_text.lower():
            raise RuntimeError("superseded nighting surface survived")
        if "やさしい" in srt_text:
            raise RuntimeError("un-witnessed Japanese correction survived")

        intro = (record.get("burned_preview") or {}).get("branding_intro") or {}
        expected_intro = intro_roster().get(str(intro.get("intro_id") or ""))
        if (
            intro.get("status") != "PREPENDED"
            or expected_intro is None
            or intro.get("intro_media_sha256") != expected_intro["sha256"]
            or abs(
                int(intro.get("intro_offset_ms") or 0)
                - int(expected_intro["duration_ms"])
            )
            > 100
            or (intro.get("verification") or {}).get("full_decode") != "clean"
        ):
            raise RuntimeError("rotating talk intro contract failed")

        upload_after = ledger_snapshot()
        if upload_after != upload_before:
            raise RuntimeError("upload ledger changed during isolated rerender")

        result = {
            "status": "review_ready",
            "operator_rerun": {
                "kind": "official-replay-hash-bound-subtitle-rerender",
                "commit": COMMIT,
                "source_alias_id": SOURCE_ALIAS_ID,
                "source_media_sha256": "sha256:" + REPLAY_SHA256,
                "reuse_cover": True,
                "upload_enabled": False,
                "receipt": str(RUN_ROOT / "receipt.json"),
                "elapsed_seconds": round(time.time() - started, 3),
            },
        }
        result.update(runner.read_publish_meta(BASE / "out" / DATE / CANDIDATE_ID))
        result["title"] = TITLE
        for index, row in enumerate(state.get("picks", [])):
            if row.get("candidate_id") == CANDIDATE_ID:
                merged = dict(row)
                merged.update(result)
                state["picks"][index] = merged
                break
        runner.write_state(DATE, state)
        runner.write_reports(DATE, state)

        receipt = {
            "schema_version": "official-replay-target-rerun-receipt.v1",
            "status": "COMPLETED",
            "date": DATE,
            "candidate_id": CANDIDATE_ID,
            "commit": COMMIT,
            "title": TITLE,
            "source_alias_id": SOURCE_ALIAS_ID,
            "source_media_sha256": "sha256:" + REPLAY_SHA256,
            "source_truth_status": truth_audit["status"],
            "source_truth_ids": sorted(
                row["truth_id"]
                for key in ("applied", "satisfied")
                for row in truth_audit.get(key, [])
            ),
            "source_language_preservation": source_language,
            "redelivery_baseline": redelivery,
            "cover_sha256_before": "sha256:" + cover_before,
            "cover_sha256_after": "sha256:" + sha256(cover),
            "branding_intro": intro,
            "upload_enabled": False,
            "upload_ledger_before": upload_before,
            "upload_ledger_after": upload_after,
            "elapsed_seconds": round(time.time() - started, 3),
        }
        (RUN_ROOT / "receipt.json").write_text(
            json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n"
        )
        print(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except Exception:
        restore_delivery(backup_delivery, delivery)
        restore = state_path.with_name(state_path.name + ".operator-restore")
        shutil.copy2(RUN_ROOT / "state-before.json", restore)
        os.replace(restore, state_path)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
