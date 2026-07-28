import json
from pathlib import Path

from src.autoslice.publish_staging import _stage_publish_draft
from src.autoslice.recovery_title_authority import (
    ROOT,
    build_recovery_publication_authorities,
    expected_recovery_publish_title,
)


def test_publish_staging_preserves_typed_same_bv_title_authority(
    tmp_path: Path,
) -> None:
    captured_cover: list[dict] = []

    def fake_stage_cover(record, **kwargs):
        captured_cover.append(kwargs)
        cover = tmp_path / "cover.png"
        cover.write_bytes(b"cover")
        return {
            "status": "AI_COVER_READY",
            "cover_path": str(cover),
            "cover_generation": {"status": "OK"},
            "reason_codes": [],
        }

    candidate_id = "auto_193450_1475_1543"
    authority = build_recovery_publication_authorities(
        candidate_ids={candidate_id},
        registry_path=(
            ROOT
            / "assets/lidousha/recovery_publication_authority.v1.json"
        ),
        expected_registry_sha256=(
            "sha256:"
                "0bbb26c63c30b1e30af13e33d5513c49aa10b98afa8730ee9761f59865317e30"
        ),
    )[candidate_id]
    title = expected_recovery_publish_title(authority)
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"media")

    record = _stage_publish_draft(
        {
            "status": "MATERIALIZED",
            "media_path": str(media),
            "artifact_hashes": {},
        },
        candidate_id=candidate_id,
        title=title,
        cues=[],
        run_ffmpeg=False,
        title_llm_call=None,
        recovery_publication_authority=authority,
        stage_cover=fake_stage_cover,
    )

    assert record is not None
    staging = record["publish_staging"]
    assert staging["title"] == title
    assert staging["title_source"] == (
        "recovery_verified_same_bv_public_title"
    )
    assert staging["title_authority_status"] == (
        "RESOLVED_RECOVERY_PUBLIC"
    )
    assert staging["recovery_publication_authority"] == authority
    publish = json.loads(
        Path(staging["publish_json_path"]).read_text(encoding="utf-8")
    )
    assert publish["title"] == title
    assert publish["recovery_publication_authority"] == authority
    assert captured_cover[0]["title"] == title
    assert captured_cover[0]["punch_allowed"] is True
