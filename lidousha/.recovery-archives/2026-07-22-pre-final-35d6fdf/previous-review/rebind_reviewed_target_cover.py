#!/usr/bin/env python3
"""Rebind the already-reviewed target cover to subtitle-redelivery video bytes."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import sys
import time
from pathlib import Path


BASE = Path("/opt/bilive/autoslice/recovery/2026-07-22/full-rerun")
REPO = BASE / "repo"
DATE = "2026-07-22"
CANDIDATE_ID = "auto_193450_670_909"
COMMIT = "6e458c292f63f14da3e96a273930d713b9e66ac1"
TITLE = (
    "【李豆沙】南町当面追问：为什么提到我就要“最最最喜欢”？"
    "越解释越像海王"
)
EXPECTED_COVER_SHA256 = (
    "969b7efdf6cf244a25b0f6add1e2e041cb46c72e5a808264a7b9e7a2d0578e42"
)
RERUN_ROOT = (
    BASE
    / "reports/operator/2026-07-22-official-replay-target-6e458c2-adopt-clean"
)
RECEIPT_PATH = RERUN_ROOT / "reviewed-cover-rebind-receipt.json"

os.environ["AUTOSLICE_BASE"] = str(BASE)
sys.path.insert(0, str(REPO))
from scripts import free_session_autoslice as runner  # noqa: E402


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def state_record(state: dict) -> dict:
    rows = [
        row
        for row in state.get("picks", [])
        if isinstance(row, dict) and row.get("candidate_id") == CANDIDATE_ID
    ]
    if len(rows) != 1:
        raise RuntimeError("target state record is missing or duplicated")
    return rows[0]


def main() -> int:
    if RECEIPT_PATH.exists():
        raise RuntimeError("reviewed cover rebind receipt already exists")
    deployed = (REPO / "DEPLOYED_COMMIT").read_text().split()[0]
    if deployed != COMMIT or not (BASE / "DISABLED").exists():
        raise RuntimeError("isolated runtime/kill-switch boundary drifted")
    rerun_receipt = json.loads((RERUN_ROOT / "receipt.json").read_text())
    if (
        rerun_receipt.get("status") != "COMPLETED"
        or rerun_receipt.get("commit") != COMMIT
        or rerun_receipt.get("upload_enabled") is not False
    ):
        raise RuntimeError("subtitle rerun receipt is not final")

    state = runner.read_state(DATE)
    rec = state_record(state)
    prior_state = json.loads((RERUN_ROOT / "state-before.json").read_text())
    prior = state_record(prior_state)
    if rec.get("title") != TITLE or prior.get("title") != TITLE:
        raise RuntimeError("reviewed title drifted")

    paths = runner.delivered_paths(DATE, rec)
    if paths is None:
        raise RuntimeError("target delivery is unavailable")
    mp4, cover = paths
    cover_hash = sha256(cover)
    media_hash = sha256(mp4)
    if cover_hash != EXPECTED_COVER_SHA256:
        raise RuntimeError("reviewed cover bytes drifted")
    if str(rec.get("video_sha256") or "").removeprefix("sha256:") != media_hash:
        raise RuntimeError("state video hash does not match redelivery")

    generation = copy.deepcopy(prior.get("cover_generation"))
    if not isinstance(generation, dict):
        raise RuntimeError("prior reviewed cover generation is missing")
    if (
        generation.get("title") != TITLE
        or generation.get("candidate_id") != CANDIDATE_ID
        or str(generation.get("final_cover_sha256") or "").removeprefix(
            "sha256:"
        )
        != EXPECTED_COVER_SHA256
        or generation.get("cover_text")
        != "为什么提到我\n就要“最最最喜欢”？"
        or generation.get("background_style") != "cobalt-comic-burst"
    ):
        raise RuntimeError("prior reviewed generation authority drifted")

    generation_root = (
        BASE
        / "out"
        / DATE
        / CANDIDATE_ID
        / "cover_repair/generations"
        / f"reviewed-rebind-{media_hash[:16]}"
    )
    if generation_root.exists():
        raise RuntimeError("immutable reviewed rebind generation already exists")
    generation_root.mkdir(parents=True)
    generated_cover = generation_root / "final.cover.png"
    shutil.copy2(cover, generated_cover)
    generation.update(
        {
            "final_cover": str(generated_cover),
            "final_cover_sha256": "sha256:" + EXPECTED_COVER_SHA256,
            "reviewed_rebind": {
                "schema_version": "reviewed-cover-byte-rebind.v1",
                "source_plan_sha256": (
                    prior.get("reviewed_cover_repair") or {}
                ).get("plan_sha256"),
                "source_cover_sha256": "sha256:" + EXPECTED_COVER_SHA256,
                "target_media_sha256": "sha256:" + media_hash,
                "upload_enabled": False,
            },
        }
    )
    generation_path = generated_cover.with_suffix(".cover_generation.json")
    generation_path.write_text(
        json.dumps(generation, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n"
    )

    upload_ledger = BASE / "reports/upload_ledger.jsonl"
    upload_before = sha256(upload_ledger)
    runner._bind_repaired_cover(
        DATE,
        rec,
        mp4,
        cover,
        generated_cover,
    )
    if not runner._cover_binding_valid(DATE, rec, mp4, cover):
        raise RuntimeError("reviewed cover rebind did not validate")
    if sha256(cover) != EXPECTED_COVER_SHA256:
        raise RuntimeError("cover bytes changed during rebind")
    if sha256(upload_ledger) != upload_before:
        raise RuntimeError("upload ledger changed during reviewed cover rebind")

    rec["reviewed_cover_rebind"] = {
        "schema_version": "reviewed-cover-byte-rebind.v1",
        "status": "FINALIZED",
        "source_plan_sha256": (
            prior.get("reviewed_cover_repair") or {}
        ).get("plan_sha256"),
        "source_cover_sha256": "sha256:" + EXPECTED_COVER_SHA256,
        "target_media_sha256": "sha256:" + media_hash,
        "cover_binding_path": rec["cover_binding_path"],
        "cover_binding_sha256": rec["cover_binding_sha256"],
        "receipt": str(RECEIPT_PATH),
        "upload_enabled": False,
        "finalized_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    runner.write_state(DATE, state)
    runner.write_reports(DATE, state)

    receipt = {
        "schema_version": "reviewed-cover-byte-rebind-receipt.v1",
        "status": "FINALIZED",
        "date": DATE,
        "candidate_id": CANDIDATE_ID,
        "commit": COMMIT,
        "title": TITLE,
        "source_plan_sha256": rec["reviewed_cover_rebind"][
            "source_plan_sha256"
        ],
        "media_path": str(mp4),
        "media_sha256": "sha256:" + media_hash,
        "cover_path": str(cover),
        "cover_sha256": "sha256:" + EXPECTED_COVER_SHA256,
        "generation_path": str(generation_path),
        "generation_sha256": "sha256:" + sha256(generation_path),
        "binding_path": rec["cover_binding_path"],
        "binding_sha256": rec["cover_binding_sha256"],
        "upload_ledger_sha256": "sha256:" + upload_before,
        "upload_enabled": False,
    }
    RECEIPT_PATH.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    print(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
