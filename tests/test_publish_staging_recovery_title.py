import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.autoslice import publish_staging
from src.autoslice import source_fact_staging
import src.autoslice.reviewed_subtitle_baseline_registry as baseline_registry
from src.autoslice.publish_staging import _stage_publish_draft
from src.autoslice.recovery_title_authority import (
    ROOT,
    build_recovery_publication_authorities,
    expected_recovery_publish_title,
)
from src.autoslice.source_fact_rescore_provenance import PROVENANCE_FIELD


@pytest.fixture(autouse=True)
def _allow_precommit_exact_interval_authority(monkeypatch: pytest.MonkeyPatch) -> None:
    """Repository sealing has dedicated tests; this file exercises staging."""

    monkeypatch.setattr(
        baseline_registry,
        "require_repository_asset_authority",
        lambda **_kwargs: None,
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
        registry_path=(ROOT / "assets/lidousha/recovery_publication_authority.v1.json"),
        expected_registry_sha256=(
            "sha256:0bbb26c63c30b1e30af13e33d5513c49aa10b98afa8730ee9761f59865317e30"
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
    assert staging["title_source"] == ("recovery_verified_same_bv_public_title")
    assert staging["title_authority_status"] == ("RESOLVED_RECOVERY_PUBLIC")
    assert staging["recovery_publication_authority"] == authority
    publish = json.loads(Path(staging["publish_json_path"]).read_text(encoding="utf-8"))
    assert publish["title"] == title
    assert publish["recovery_publication_authority"] == authority
    assert captured_cover[0]["title"] == title
    assert captured_cover[0]["punch_allowed"] is True


def test_publish_staging_mirrors_source_fact_rescore_provenance(
    tmp_path: Path,
) -> None:
    media = tmp_path / "candidate.recut.mp4"
    media.write_bytes(b"media")
    cover = tmp_path / "cover.png"
    cover.write_bytes(b"cover")
    provenance = {
        "schema_version": "source-fact-scorecard-rescore-provenance.v1",
        "candidate_id": "candidate",
        "provenance_sha256": "sha256:" + "a" * 64,
    }

    def fake_stage_cover(_record, **_kwargs):
        return {
            "status": "AI_COVER_READY",
            "cover_path": str(cover),
            "cover_generation": {"status": "READY"},
            "reason_codes": [],
        }

    record = _stage_publish_draft(
        {
            "status": "MATERIALIZED",
            "media_path": str(media),
            "artifact_hashes": {},
            PROVENANCE_FIELD: provenance,
        },
        candidate_id="candidate",
        title="【李豆沙】测试标题正文足够长",
        cues=[],
        run_ffmpeg=False,
        title_llm_call=None,
        stage_cover=fake_stage_cover,
    )

    assert record is not None
    staging = record["publish_staging"]
    publish = json.loads(Path(staging["publish_json_path"]).read_text(encoding="utf-8"))
    assert staging[PROVENANCE_FIELD] == provenance
    assert publish[PROVENANCE_FIELD] == provenance


def test_publish_staging_passes_single_read_speaker_evidence_to_source_fact(
    tmp_path: Path,
    monkeypatch,
) -> None:
    media = tmp_path / "candidate.recut.mp4"
    media.write_bytes(b"media")
    cover = tmp_path / "cover.png"
    cover.write_bytes(b"cover")
    evidence = {
        "state": "AbsentAuthorized",
        "reason": "speaker_mode_uniform_host",
        "policy_ids": {
            "alignment": "speaker_cue_subsegment_alignment/v1",
            "text": "compact_ws/v1",
            "timing": "half_open_integer_ms_exact/v1",
            "absence": "speaker_mode_uniform_host/v1",
        },
    }
    build_calls: list[tuple[object, object]] = []
    review_calls: list[dict[str, object]] = []

    def fake_build(record, cues):
        build_calls.append((record, cues))
        return (
            "1 原始字幕",
            SimpleNamespace(transcript=None, speaker_evidence=evidence),
        )

    def fake_review(**kwargs):
        review_calls.append(kwargs)
        return {
            "schema_version": "lidousha-source-fact-review.v1",
            "status": "PASS",
            "decision": "KEEP",
            "final_selection_hook": kwargs["selection_hook"],
            "final_title": kwargs["title"],
        }

    def fake_stage_cover(_record, **_kwargs):
        return {
            "status": "AI_COVER_READY",
            "cover_path": str(cover),
            "cover_generation": {"status": "READY"},
            "reason_codes": [],
        }

    monkeypatch.setattr(
        source_fact_staging,
        "build_addressee_evidence",
        fake_build,
    )
    monkeypatch.setattr(
        source_fact_staging,
        "review_and_repair_source_facts",
        fake_review,
    )

    cues: list[object] = []
    record = _stage_publish_draft(
        {
            "status": "MATERIALIZED",
            "speaker_mode": "uniform_host",
            "media_path": str(media),
            "artifact_hashes": {},
        },
        candidate_id="candidate-speaker-evidence",
        title="【李豆沙】测试标题正文足够长",
        cues=cues,
        run_ffmpeg=False,
        title_llm_call=None,
        selection_hook="原始字幕是一件可核验的具体事情。",
        source_fact_llm_call=lambda _prompt: "unused",
        stage_cover=fake_stage_cover,
    )

    assert record is not None
    assert len(build_calls) == 1
    assert build_calls[0][1] is cues
    assert len(review_calls) == 1
    assert review_calls[0]["final_transcript"] == "1 原始字幕"
    assert review_calls[0]["speaker_transcript"] is None
    assert review_calls[0]["speaker_evidence"] is evidence


def test_reused_published_cover_carries_all_artifact_hashes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    candidate_id = "candidate"
    media = tmp_path / f"{candidate_id}.recut.mp4"
    media.write_bytes(b"media")
    covers = tmp_path / "covers"
    covers.mkdir()
    cover = covers / f"{candidate_id}.ai-title.cover.png"
    cover.write_bytes(b"cover")
    cover_sha256 = "sha256:" + hashlib.sha256(cover.read_bytes()).hexdigest()
    ai_background_sha256 = "sha256:" + "a" * 64
    reference_sha256 = "sha256:" + "b" * 64
    (covers / f"{candidate_id}.published-cover-generation.json").write_text(
        json.dumps(
            {
                "final_cover_sha256": cover_sha256,
                "ai_background_sha256": ai_background_sha256,
                "reference_sha256": reference_sha256,
                "route_decision": {},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        publish_staging,
        "validate_cover_route_decision",
        lambda *_args, **_kwargs: True,
    )

    record = _stage_publish_draft(
        {
            "status": "MATERIALIZED",
            "media_path": str(media),
            "artifact_hashes": {},
        },
        candidate_id=candidate_id,
        title="【李豆沙】测试标题正文足够长",
        cues=[],
        run_ffmpeg=False,
        title_llm_call=None,
        skip_cover=True,
    )

    assert record is not None
    hashes = record["artifact_hashes"]
    assert record["publish_staging"]["cover_path"] == str(cover)
    assert hashes["cover_sha256"] == cover_sha256
    assert hashes["ai_background_sha256"] == ai_background_sha256
    assert hashes["cover_reference_sha256"] == reference_sha256


def test_candidate_projection_blocks_wrong_name_in_visible_cover_text(
    tmp_path: Path,
) -> None:
    candidate_id = "auto_223750_578_734"
    media = tmp_path / f"{candidate_id}.recut.mp4"
    media.write_bytes(b"media")
    cover = tmp_path / "cover.png"
    cover.write_bytes(b"cover")

    def wrong_name_cover(_record, **_kwargs):
        return {
            "status": "AI_COVER_READY",
            "cover_path": str(cover),
            "cover_generation": {
                "status": "READY",
                "rendered_lines": ["莉亚求小李放过她"],
            },
            "reason_codes": [],
        }

    record = _stage_publish_draft(
        {
            "status": "MATERIALIZED",
            "media_path": str(media),
            "artifact_hashes": {},
        },
        candidate_id=candidate_id,
        title="unused because the candidate has an Ivan title override",
        cues=[],
        run_ffmpeg=False,
        title_llm_call=None,
        stage_cover=wrong_name_cover,
    )

    assert record is not None
    staging = record["publish_staging"]
    assert staging["title_authority_status"] == "RESOLVED_MANUAL"
    assert staging["entity_projection_audit"]["status"] == "PASS"
    assert staging["cover_status"] == "BLOCKED_ENTITY_SURFACE_PROJECTION"
    assert staging["cover_path"] is None
    assert staging["reason_codes"] == ["COVER_ENTITY_SURFACE_PROJECTION_FAILED"]
    failure = staging["cover_generation"]["entity_projection_audit"]
    assert failure["status"] == "FAIL"
    assert "expected=莉娅" in failure["detail"]
