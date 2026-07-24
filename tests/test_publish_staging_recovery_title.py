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
        skip_cover=True,
        recovery_publication_authority=authority,
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
