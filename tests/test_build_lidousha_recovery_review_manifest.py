from __future__ import annotations

import hashlib
import json
from pathlib import Path

from scripts.build_lidousha_recovery_review_manifest import build_manifest
from src.autoslice.cover_route_evidence import (
    build_cover_route_decision,
    record_cover_route_execution,
)


def _sha(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def test_builder_reprojects_record_title_and_exact_cover_evidence(
    tmp_path: Path,
) -> None:
    root = tmp_path / "2026-07-22"
    root.mkdir()
    stem = "当面对质"
    candidate_id = "auto_exact"
    files = {}
    for suffix, payload in (
        ("mp4", b"video"),
        ("cover.png", b"cover"),
        ("srt", b"1\n00:00:00,000 --> 00:00:01,000\nhello\n"),
        ("clip-context.json", b"{}\n"),
        ("subtitle-regression.json", b"{}\n"),
        ("chat-authority.json", b"{}\n"),
        ("redelivery-baseline.json", b"{}\n"),
    ):
        path = root / f"{stem}.{suffix}"
        path.write_bytes(payload)
        files[suffix] = path
    ass = tmp_path / "final.ass"
    ass.write_text("[Events]\n", encoding="utf-8")
    story_contract = {
        "candidate_id": candidate_id,
        "selection_hook": "当面对质",
        "relation_state": "UNKNOWN",
        "participants": [],
    }
    generation: dict[str, object] = {
        "story_contract": story_contract,
        "title": "新标题",
        "cover_text": "对质",
        "method": "screenshot_direct",
        "cover_origin": "SOURCE_SCREENSHOT",
        "reference_sha256": "sha256:" + "1" * 64,
        "reference_authority": {},
        "final_cover_sha256": _sha(files["cover.png"]),
    }
    generation["route_decision"] = build_cover_route_decision(
        selected_treatment="screenshot_direct",
        selected_rationale="真实帧已经表达钩子",
        story_contract=story_contract,
        reference_authority={},
        decision_inputs={"composition_strength": "STRONG"},
        title="新标题",
        cover_text="对质",
    )
    record_cover_route_execution(
        generation,
        actual_treatment="screenshot_direct",
        execution_status="READY",
        image_generation_attempted=False,
        image_generation_used=False,
    )
    record = {
        "story_contract": story_contract,
        "publish_staging": {
            "title": "新标题",
            "cover_generation": generation,
        },
        "artifact_hashes": {
            "burned_video_sha256": _sha(files["mp4"]),
            "cover_sha256": _sha(files["cover.png"]),
            "subtitle_sha256": _sha(files["srt"]),
        },
        "burned_preview": {"ass_path": str(ass)},
    }
    record_path = root / f"{stem}.record.json"
    record_path.write_text(json.dumps(record), encoding="utf-8")
    state = {
        "date": "2026-07-22",
        "status": "review_ready",
        "run_mode": "RECOVERY_REVIEW",
        "upload_allowed": False,
        "talk_selection_contract": {
            "mode": "EXACT_CANDIDATE_SET_NO_BACKFILL",
            "candidate_ids": [candidate_id],
        },
        "picks": [
            {
                "candidate_id": candidate_id,
                "status": "review_ready",
                "bundle_lifecycle": "CURRENT",
                "bundle_compliance": "COMPLIANT",
                "rc": 0,
            }
        ],
    }

    first = build_manifest(
        package_root=root,
        state=state,
        deployed_commit="a" * 40,
        created_at="2026-07-23T00:00:00+00:00",
    )
    assert first["items"][0]["title"] == "新标题"
    assert first["exact_candidate_ids"] == [candidate_id]
    assert first["cover_route_attestations"][0]["final_cover_sha256"] == _sha(
        files["cover.png"]
    )
    assert first["items"][0]["cover_route_summary"]["actual_treatment"] == (
        "screenshot_direct"
    )

    record["publish_staging"]["title"] = "重跑后的标题"
    record_path.write_text(json.dumps(record), encoding="utf-8")
    second = build_manifest(
        package_root=root,
        state=state,
        deployed_commit="b" * 40,
        created_at="2026-07-23T01:00:00+00:00",
    )
    assert second["items"][0]["title"] == "重跑后的标题"
    assert second["deployed_commit"] == "b" * 40
