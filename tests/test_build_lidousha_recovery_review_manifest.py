from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts.build_lidousha_recovery_review_manifest import (
    ManifestBuildError,
    build_manifest,
)
from src.autoslice.cover_route_evidence import (
    build_cover_route_decision,
    record_cover_route_execution,
)
from src.autoslice.recovery_title_authority import (
    ROOT,
    build_recovery_publication_authorities,
    expected_recovery_publish_title,
)


def _sha(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def test_builder_reprojects_record_title_and_exact_cover_evidence(
    tmp_path: Path,
) -> None:
    root = tmp_path / "2026-07-22"
    root.mkdir()
    stem = "当面对质"
    candidate_id = "auto_193450_1475_1543"
    authority = build_recovery_publication_authorities(
        candidate_ids={candidate_id},
        registry_path=(
            ROOT
            / "assets/lidousha/recovery_publication_authority.v1.json"
        ),
        expected_registry_sha256=(
            "sha256:"
            "be9ffbd42008b94d9e47ea714e1fae5d032f576bb0e71841624df3b77ea53757"
        ),
    )[candidate_id]
    title = expected_recovery_publish_title(authority)
    files = {}
    for suffix, payload in (
        ("mp4", b"video"),
        ("cover.png", b"cover"),
        ("cover.title-mask.png", b"mask"),
        ("cover.pre-overlay.png", b"pre-overlay"),
        ("cover.route-background.png", b"route-background"),
        ("srt", b"1\n00:00:00,000 --> 00:00:01,000\nhello\n"),
        (
            "speaker.srt",
            "1\n00:00:00,000 --> 00:00:01,000\n[李豆沙] hello\n".encode(),
        ),
        (
            "speaker.ass",
            b"[Events]\n"
            b"Dialogue: 0,0:00:00.00,0:00:01.00,LDS,,0,0,0,,hello\n",
        ),
        ("clip-context.json", b"{}\n"),
        ("subtitle-regression.json", b"{}\n"),
        ("chat-authority.json", b"{}\n"),
        (
            "publish.json",
            json.dumps(
                {
                    "title": title,
                    "recovery_publication_authority": authority,
                },
                ensure_ascii=False,
            ).encode("utf-8"),
        ),
        ("redelivery-baseline.json", b"{}\n"),
    ):
        path = root / f"{stem}.{suffix}"
        path.write_bytes(payload)
        files[suffix] = path
    files["chat-authority.json"].write_text(
        json.dumps(
            {
                "final_speaker_srt_sha256": _sha(files["speaker.srt"]),
                "speaker_ass_sha256": _sha(files["speaker.ass"]),
            }
        ),
        encoding="utf-8",
    )
    ass = tmp_path / "final.ass"
    ass.write_bytes(files["speaker.ass"].read_bytes())
    story_contract = {
        "candidate_id": candidate_id,
        "selection_hook": "当面对质",
        "relation_state": "UNKNOWN",
        "participants": [],
    }
    generation: dict[str, object] = {
        "story_contract": story_contract,
        "title": title,
        "cover_text": "对质",
        "method": "screenshot_direct",
        "cover_origin": "SOURCE_SCREENSHOT",
        "reference_sha256": "sha256:" + "1" * 64,
        "reference_authority": {},
        "final_cover_sha256": _sha(files["cover.png"]),
        "pre_overlay_sha256": _sha(files["cover.pre-overlay.png"]),
        "ai_background_sha256": _sha(
            files["cover.route-background.png"]
        ),
        "rendered_text_pixels": {
            "mask_sha256": _sha(files["cover.title-mask.png"]),
            "pre_overlay_sha256": _sha(
                files["cover.pre-overlay.png"]
            ),
        },
    }
    generation["route_decision"] = build_cover_route_decision(
        selected_treatment="screenshot_direct",
        selected_rationale="真实帧已经表达钩子",
        story_contract=story_contract,
        reference_authority={},
        decision_inputs={"composition_strength": "STRONG"},
        title=title,
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
        "recovery_publication_authority": authority,
        "publish_staging": {
            "title": title,
            "recovery_publication_authority": authority,
            "cover_generation": generation,
        },
        "artifact_hashes": {
            "burned_video_sha256": _sha(files["mp4"]),
            "cover_sha256": _sha(files["cover.png"]),
            "subtitle_sha256": _sha(files["srt"]),
            "ass_sha256": _sha(files["speaker.ass"]),
            "publish_draft_sha256": _sha(files["publish.json"]),
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
        "delivery_rerun_plan": {
            "schema_version": "recovery-review-talk-rerun-plan.v7",
            "recovery_publication_authorities_by_candidate": {
                candidate_id: authority
            },
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
    assert first["items"][0]["title"] == title
    assert first["exact_candidate_ids"] == [candidate_id]
    assert first["cover_route_attestations"][0]["final_cover_sha256"] == _sha(
        files["cover.png"]
    )
    assert first["items"][0]["cover_route_summary"]["actual_treatment"] == (
        "screenshot_direct"
    )
    assert first["items"][0]["ass_path"] == f"{stem}.speaker.ass"
    assert first["items"][0]["ass_sha256"] == _sha(
        files["speaker.ass"]
    )
    assert first["items"][0]["speaker_srt"] == f"{stem}.speaker.srt"
    assert first["items"][0]["speaker_srt_sha256"] == _sha(
        files["speaker.srt"]
    )
    assert first["items"][0]["publish_json"] == f"{stem}.publish.json"

    original_ass = files["speaker.ass"].read_bytes()
    files["speaker.ass"].write_bytes(original_ass + b"; drift\n")
    with pytest.raises(ManifestBuildError, match="speaker ASS hash drift"):
        build_manifest(
            package_root=root,
            state=state,
            deployed_commit="a" * 40,
            created_at="2026-07-23T00:30:00+00:00",
        )
    files["speaker.ass"].write_bytes(original_ass)

    original_speaker_srt = files["speaker.srt"].read_bytes()
    files["speaker.srt"].write_bytes(original_speaker_srt + b"\n")
    with pytest.raises(
        ManifestBuildError, match="speaker SRT differs from chat authority"
    ):
        build_manifest(
            package_root=root,
            state=state,
            deployed_commit="a" * 40,
            created_at="2026-07-23T00:45:00+00:00",
        )
    files["speaker.srt"].write_bytes(original_speaker_srt)

    record["publish_staging"]["title"] = "重跑后的标题"
    record_path.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(
        ManifestBuildError,
        match="recovery publication authority invalid",
    ):
        build_manifest(
            package_root=root,
            state=state,
            deployed_commit="b" * 40,
            created_at="2026-07-23T01:00:00+00:00",
        )
