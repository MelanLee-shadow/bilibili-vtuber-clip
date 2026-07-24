import json
from pathlib import Path

from src.autoslice.publish_staging import _stage_publish_draft
from src.autoslice.recovery_title_authority import (
    ROOT,
    build_recovery_title_authority,
)


def test_publish_staging_preserves_typed_same_bv_title_authority(
    tmp_path: Path,
) -> None:
    candidate_id = "auto_193450_1475_1543"
    evidence = (
        ROOT
        / "reports/authorized_uploads/2026-07-22-v8-final"
        / f"{candidate_id}.public_verify.json"
    )
    authority = build_recovery_title_authority(
        candidate_id=candidate_id,
        evidence_path=evidence,
        expected_evidence_sha256=(
            "sha256:"
            "c3af4c8485ca2cf3f17c1d4a660c53cd4f1d23a3f9d254e77924d9875a07d771"
        ),
    )
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"media")

    record = _stage_publish_draft(
        {
            "status": "MATERIALIZED",
            "media_path": str(media),
            "artifact_hashes": {},
        },
        candidate_id=candidate_id,
        title=str(authority["title"]),
        cues=[],
        run_ffmpeg=False,
        title_llm_call=None,
        skip_cover=True,
        recovery_title_authority=authority,
    )

    assert record is not None
    staging = record["publish_staging"]
    assert staging["title"] == authority["title"]
    assert staging["title_source"] == (
        "recovery_verified_same_bv_public_title"
    )
    assert staging["title_authority_status"] == (
        "RESOLVED_RECOVERY_PUBLIC"
    )
    assert staging["recovery_title_authority"] == authority
    publish = json.loads(
        Path(staging["publish_json_path"]).read_text(encoding="utf-8")
    )
    assert publish["title"] == authority["title"]
    assert publish["recovery_title_authority"] == authority
