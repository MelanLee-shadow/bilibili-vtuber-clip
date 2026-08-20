from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from src.autoslice import qixi_corrected_package_finalization as finalization
from src.autoslice import final_human_review
from src.autoslice.package_relocation_contract import (
    PackageRelocationError,
    project_uniform_host_locators,
)
from src.autoslice.recovery_title_authority import build_recovery_publication_authorities
from src.autoslice.source_fact_review import review_and_repair_source_facts
from scripts import build_manual_review_manifest as manual_review_manifest
from scripts.build_manual_review_manifest import DailyManifestError, build_manual
from scripts.audit_lidousha_review_package import _audit_manual_corrected_same_bv_receipt


CID = "auto_113022_354_496"
TITLE = "【李豆沙】李豆沙公布七夕安排，中午甜甜甜晚上苦苦苦，一套PUA直播要让kmx集体分号！"


def _sha(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _write(root: Path, name: str, payload: bytes, *, role: str = "package") -> dict[str, object]:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return {
        "source": name,
        "sha256": _sha(path),
        "bytes": len(payload),
        "target": name,
        "target_role": role,
    }


def _publication() -> dict:
    registry = Path("assets/lidousha/daily_same_bv_publication_authority.v1.json")
    registry_sha = _sha(registry)
    return build_recovery_publication_authorities(
        candidate_ids={CID}, registry_path=registry, expected_registry_sha256=registry_sha,
        repo_root=Path.cwd(),
    )[CID]


def _sealed_test_repo(tmp_path: Path, authority: dict) -> Path:
    """Materialize a deployed-authority repo with the real same-BV evidence."""

    repo = tmp_path / "repo"
    repo.mkdir()
    publication = authority["recovery_publication_authority"]
    for relative in (
        publication["registry_repo_path"],
        publication["source_public_verify_repo_path"],
    ):
        source = Path.cwd() / relative
        destination = repo / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source.read_bytes())
    authority_path = repo / finalization.AUTHORITY_RELATIVE_PATH
    authority_path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(authority, ensure_ascii=False, sort_keys=True).encode()
    authority_path.write_bytes(payload)
    deployed = {
        "schema_version": "deployed-authority-manifest.v1",
        "deployed_commit": "0" * 40,
        "entries": {
            finalization.AUTHORITY_RELATIVE_PATH.as_posix(): {
                "bytes": len(payload),
                "sha256": "sha256:" + hashlib.sha256(payload).hexdigest(),
            }
        },
    }
    deployed["manifest_sha256"] = finalization._canonical_sha(deployed)
    (repo / "DEPLOYED_COMMIT").write_text("0" * 40 + "\n", encoding="utf-8")
    (repo / "DEPLOYED_AUTHORITY_MANIFEST.json").write_text(
        json.dumps(deployed, ensure_ascii=False), encoding="utf-8"
    )
    return repo


def test_repo_stale_qixi_authority_is_disabled_before_any_finalization(
    tmp_path: Path,
) -> None:
    """The old 562b/0d21 package cannot be finalized from this repo epoch."""

    with pytest.raises(finalization.QixiCorrectedPackageError, match="not a regular file"):
        finalization.plan_finalization(
            repo_root=Path.cwd(),
            release_root=tmp_path,
            evidence_root=tmp_path,
            target=tmp_path / "new-package",
        )


def _inputs(tmp_path: Path) -> tuple[dict, dict, Path, Path]:
    release, evidence = tmp_path / "release", tmp_path / "evidence"
    release.mkdir()
    evidence.mkdir()
    (evidence / "candidate" / "package").mkdir(parents=True)
    subtitle = b"1\n00:00:00,000 --> 00:00:01,000\n\xe6\xad\xa3\xe7\xa1\xae\n"
    ass_bytes = (
        b"[Script Info]\n[V4+ Styles]\n"
        b"Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,"
        b"Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,"
        b"Alignment,MarginL,MarginR,MarginV,Encoding\n"
        b"Style: Default,Arial,36,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,0,2,20,20,20,1\n"
        b"[Events]\nFormat: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text\n"
        b"Dialogue: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,\xe6\xad\xa3\xe7\xa1\xae\n"
    )
    source_fact = review_and_repair_source_facts(
        selection_hook="七夕安排说明",
        title=TITLE,
        final_transcript="正确",
        clip_context_prompt="",
        llm_call=lambda _prompt: json.dumps({
            "schema_version": "lidousha-source-fact-review.v1", "status": "KEEP",
            "final_selection_hook": "七夕安排说明", "final_title": TITLE,
            "supported_by": ["final_transcript"], "changed_surfaces": [],
            "addressee_attribution": [],
            "selection_scorecard_review": {"status": "NOT_NEEDED", "reason": "fixture"},
            "summary": "fixture",
        }, ensure_ascii=False),
    )
    release_items = {
        "video": _write(release, "z2.mp4", b"z2-video"),
        "main": _write(release, "main.mp4", b"main-video"),
        "subtitle": _write(release, "z2.srt", subtitle),
        "ass": _write(release, "z2.ass", ass_bytes),
        "correction": _write(
            release,
            "correction.json",
            json.dumps({
                "candidate_id": CID, "upload_enabled": False,
                "after_srt_sha256": hashlib.sha256(subtitle).hexdigest(),
                "burned_media_sha256": hashlib.sha256(b"z2-video").hexdigest(),
                "delivery_branding_authority": {"branding_intro": {
                    "intro_id": "z2",
                    "intro_media_sha256": "sha256:" + "f" * 64,
                    "intro_offset_ms": 6183,
                    "status": "VERIFIED",
                }},
            }).encode(),
        ),
        "cover": _write(release, "cover.png", b"cover"),
    }
    pre = _write(release, "pre.png", b"pre")
    mask = _write(release, "mask.png", b"mask")
    background = _write(release, "background.png", b"background")
    r2_hashes = {
        "burned_video_sha256": "sha256:" + "a" * 64,
        "subtitle_sha256": "sha256:" + hashlib.sha256(subtitle).hexdigest(),
        "ass_sha256": "sha256:" + hashlib.sha256(ass_bytes).hexdigest(),
        "video_sha256": "sha256:" + hashlib.sha256(b"main-video").hexdigest(),
    }
    record = {
        "schema_version": "delivery-record.v1",
        "classification": "talk",
        "speaker_mode": "uniform_host",
        "story_contract": {"selection_hook": "七夕安排说明", "clip_context_prompt": "", "selection_scorecard": None, "source_fact_review": source_fact},
        "duration_ms": 1000,
        "boundary_audit": {"final_start_ms": 0},
        "artifact_hashes": {**r2_hashes, "publish_draft_sha256": "sha256:" + "b" * 64},
        # This is the sealed r2/Z1 carrier.  Its historical branding path is
        # intentionally outside the evidence workspace, and may be removed
        # only after its burn hash proves it is the r2 carrier superseded by
        # source Z2's whole burned_preview subtree.
        "burned_preview": {
            "status": "BURNED",
            "burned_sha256": r2_hashes["burned_video_sha256"],
            "branding_intro": {
                "intro_media_path": "/obsolete-r2-z1-stage/intro.mp4",
            },
        },
        "publish_staging": {"title": TITLE, "source_fact_review": source_fact},
    }
    publish = {
        "schema_version": "shadow-publish-draft.v1",
        "candidate_id": CID, "title": TITLE, "upload_enabled": False,
        "source_fact_review": source_fact,
        "artifact_hashes": r2_hashes,
    }
    source_burned = {
        "ass_path": str(release / "z2.ass"),
        # Source carries its complete provenance.  Correction carries only a
        # strict summary, including a normalized spelling of this bare SHA.
        "branding_intro": {
            "fallback_attempts": [],
            "intro_id": "z2",
            "intro_media_path": str(release / "intro.mp4"),
            "intro_media_sha256": "f" * 64,
            "intro_offset_ms": 6183,
            "main_duration_ms_before": 1000,
            "main_sha256_before": "e" * 64,
            "method": "fixture",
            "policy_manifest_path": str(release / "policy.json"),
            "policy_manifest_sha256": "d" * 64,
            "rotation": 0,
            "status": "VERIFIED",
            "verification": {"status": "PASS"},
        },
        "burned_sha256": _sha(release / "z2.mp4"),
        "command": ["ffmpeg", str(release / "z2.mp4")],
        "path": str(release / "z2.mp4"),
        "pillarbox_16_9": {},
        "status": "BURNED",
        "stream_contract": {},
        "subtitle_style": {},
    }
    source_cover_generation = {
        "final_cover_sha256": _sha(release / "cover.png"),
        "final_cover": str(release / "cover.png"),
        "pre_overlay_path": str(release / "pre.png"), "pre_overlay_sha256": pre["sha256"],
        "ai_background": str(release / "background.png"), "ai_background_sha256": background["sha256"],
        "rendered_text_pixels": {"mask_path": str(release / "mask.png"), "mask_sha256": mask["sha256"]},
    }
    source_record = {
        "duration_ms": 1000,
        "boundary_audit": {"final_start_ms": 0},
        "artifact_hashes": {"ass_sha256": "sha256:" + "c" * 64},
        "burned_preview": source_burned,
        "publish_staging": {"title": TITLE, "cover_generation": source_cover_generation},
    }
    source_publish = {
        "candidate_id": CID,
        "title": TITLE,
        "artifact_hashes": {
            "burned_video_sha256": "sha256:" + "d" * 64,
            "subtitle_sha256": "sha256:" + "e" * 64,
        },
        "cover_status": "AI_COVER_READY",
        "cover_generation": source_cover_generation,
    }
    release_items["source_record"] = _write(
        release, "source.record.json", json.dumps(source_record).encode()
    )
    release_items["source_publish"] = _write(
        release, "source.publish.json", json.dumps(source_publish).encode()
    )
    evidence_items = {
        "subtitle": _write(evidence, "r2.srt", subtitle),
        "main": _write(evidence, "r2.main.mp4", b"main-video"),
        "ass": _write(evidence, "r2.ass", ass_bytes),
        "cover": _write(evidence, "r2.cover.png", b"cover"),
        "record": _write(evidence, "record.json", json.dumps(record).encode()),
        "publish": _write(evidence, "publish.json", json.dumps(publish).encode()),
        "chat_authority": _write(
            evidence,
            "chat.json",
            json.dumps(
                {
                    "final_text_srt_sha256": "sha256:" + hashlib.sha256(subtitle).hexdigest(),
                    "final_speaker_srt_sha256": "sha256:" + hashlib.sha256(subtitle).hexdigest(),
                    "final_text_srt_path": str(evidence / "candidate" / "package" / "r2.srt"),
                    "final_speaker_srt_path": str(evidence / "candidate" / "package" / "r2.srt"),
                    "speaker_ass_path": None,
                    "speaker_ass_sha256": None,
                }
            ).encode(),
            role="candidate",
        ),
        "clip_context": _write(
            evidence, "clip.json", json.dumps({"recording_date": "2026-08-02"}).encode(), role="candidate"
        ),
        "redelivery_baseline": _write(evidence, "baseline.json", b"{}"),
        "cover_pre_overlay": _write(evidence, "pre.png", b"pre"),
        "cover_title_mask": _write(evidence, "mask.png", b"mask"),
        "cover_route_background": _write(evidence, "route.png", b"background"),
        "cover_reference": _write(evidence, "reference.png", b"reference"),
        "cover_host_witness": _write(evidence, "witness.png", b"witness"),
        "cover_source_composition": _write(evidence, "composition.json", b"{}"),
        "boundary_audit": _write(evidence, "boundary.json", b"{}", role="candidate"),
        "review_flags": _write(evidence, "flags.json", b"{}", role="candidate"),
    }
    # The integration package uses the same flat stem contract consumed by the
    # manual manifest builder; source/evidence paths remain deliberately
    # distinct from these final portable names.
    final_targets = {
        "video": "qixi.mp4",
        "main": "qixi.main.mp4",
        "subtitle": "qixi.srt",
        "ass": "qixi.final-sapphire72.ass",
        "cover": "qixi.cover.png",
        "record": "qixi.record.json",
        "publish": "qixi.publish.json",
        "chat_authority": f"{CID}.chat-authority.json",
        "clip_context": f"{CID}.clip-context.json",
        "redelivery_baseline": "qixi.redelivery-baseline.json",
        "cover_pre_overlay": "qixi.cover.pre-overlay.png",
        "cover_title_mask": "qixi.cover.title-mask.png",
        "cover_route_background": "qixi.cover.ai-bg.png",
        "cover_reference": "qixi.cover-reference.png",
    }
    for source in (release_items, evidence_items):
        for name, target in final_targets.items():
            if name in source:
                source[name]["target"] = target
    authority = {
        "schema_version": finalization.SCHEMA,
        "candidate_id": CID,
        "title": TITLE,
        "recovery_publication_authority": _publication(),
        "release": release_items,
        "evidence": evidence_items,
        "source_drift": {
            "source_publish_burned_video_sha256": "sha256:" + "d" * 64,
            "source_publish_subtitle_sha256": "sha256:" + "e" * 64,
            "source_record_ass_sha256": "sha256:" + "c" * 64,
            "source_burned_preview_sha256": finalization._canonical_sha(source_burned),
            "source_cover_generation_sha256": finalization._canonical_sha(source_cover_generation),
            "r2_publish_burned_video_sha256": "sha256:" + "a" * 64,
            "r2_publish_subtitle_sha256": "sha256:" + hashlib.sha256(subtitle).hexdigest(),
        },
        "roots": {"source_candidate_relative": "candidate", "source_package_relative": "candidate/package"},
    }
    authority["authority_sha256"] = finalization._canonical_sha(authority)
    return authority, finalization._verify_cross_surface(
        authority=authority, release_root=release, evidence_root=evidence
    ), release, evidence


def test_project_supersedes_sealed_r2_z1_burned_preview_before_locator_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    authority, artifacts, _release, _evidence = _inputs(tmp_path)
    monkeypatch.setattr(finalization, "verify_chat_authority_final_surfaces", lambda *_a, **_k: True)
    target = tmp_path / "final-package"
    record, publish, chat = finalization._project_documents(
        authority=authority,
        artifacts=artifacts,
        candidate_root=target,
        release_mappings=(
            (str(_release), str(target / "replacement_recuts")),
            (str(_release.parent), str(target)),
            (str(Path.cwd()), str(Path.cwd())),
        ),
        release_workspace_root=str(_release.parent),
        evidence_mappings=(
            (str(_evidence), str(target / "replacement_recuts")),
            (str(_evidence.parent), str(target)),
            (str(Path.cwd()), str(Path.cwd())),
        ),
        evidence_workspace_root=str(_evidence.parent),
    )
    assert record["burned_preview"]["burned_sha256"] == artifacts["video"][1]
    assert record["burned_preview"]["branding_intro"]["intro_id"] == "z2"
    assert record["burned_preview"]["branding_intro"]["intro_media_sha256"] == "f" * 64
    assert record["burned_preview"]["branding_intro"]["intro_media_path"] == str(
        _release / "intro.mp4"
    )
    assert "/obsolete-r2-z1-stage" not in json.dumps(record, ensure_ascii=False)
    assert record["subtitle_path"].startswith(str(target))
    assert publish["video_path"].startswith(str(target))
    assert chat["burn_binding"]["burned_media_path"].startswith(str(target))
    assert chat["final_text_srt_path"] == str(target / "replacement_recuts" / "qixi.srt")
    assert chat["final_speaker_srt_path"] == str(target / "replacement_recuts" / "qixi.srt")
    assert record["recovery_publication_authority"] == authority["recovery_publication_authority"]
    assert publish["cover_status"] == "AI_COVER_READY"
    assert publish["cover_generation"]["final_cover"].startswith(str(target))


def test_r2_burned_preview_supersession_rejects_mismatched_r2_hash() -> None:
    record = {"burned_preview": {"burned_sha256": "sha256:" + "a" * 64}}
    publish = {"artifact_hashes": {"burned_video_sha256": "sha256:" + "b" * 64}}
    authority = {
        "source_drift": {"r2_publish_burned_video_sha256": "sha256:" + "b" * 64}
    }
    with pytest.raises(finalization.QixiCorrectedPackageError, match="supersession hash differs"):
        finalization._without_superseded_r2_burned_preview(
            record=record,
            publish=publish,
            authority=authority,
        )


def _source_burned_branding_summary_inputs() -> tuple[dict, dict]:
    source_burned = {
        "branding_intro": {
            "fallback_attempts": [],
            "intro_id": "z2",
            "intro_media_path": "/sealed/source/intro.mp4",
            "intro_media_sha256": "a" * 64,
            "intro_offset_ms": 6183,
            "status": "VERIFIED",
            "verification": {"status": "PASS"},
        }
    }
    correction = {
        "intro_id": "z2",
        "intro_media_sha256": "sha256:" + "a" * 64,
        "intro_offset_ms": 6183,
        "status": "VERIFIED",
    }
    return source_burned, correction


def test_source_burned_branding_summary_accepts_full_source_and_normalized_sha() -> None:
    source_burned, correction = _source_burned_branding_summary_inputs()
    finalization._validate_source_burned_branding_summary(
        source_burned=source_burned,
        correction_branding=correction,
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("intro_id", "another-intro"),
        ("intro_media_sha256", "b" * 64),
        ("intro_offset_ms", 6184),
        ("status", "FAILED"),
    ],
)
def test_source_burned_branding_summary_rejects_each_value_mismatch(
    field: str, value: object
) -> None:
    source_burned, correction = _source_burned_branding_summary_inputs()
    correction[field] = value
    with pytest.raises(finalization.QixiCorrectedPackageError, match="branding summary differs"):
        finalization._validate_source_burned_branding_summary(
            source_burned=source_burned,
            correction_branding=correction,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("intro_id", 6183),
        ("intro_media_sha256", 6183),
        ("intro_offset_ms", True),
        ("status", 6183),
    ],
)
def test_source_burned_branding_summary_rejects_correction_value_types(
    field: str, value: object
) -> None:
    source_burned, correction = _source_burned_branding_summary_inputs()
    correction[field] = value
    with pytest.raises(finalization.QixiCorrectedPackageError):
        finalization._validate_source_burned_branding_summary(
            source_burned=source_burned,
            correction_branding=correction,
        )


def test_source_burned_branding_summary_rejects_extra_correction_key() -> None:
    source_burned, correction = _source_burned_branding_summary_inputs()
    correction["unapproved"] = "extra"
    with pytest.raises(finalization.QixiCorrectedPackageError, match="summary schema differs"):
        finalization._validate_source_burned_branding_summary(
            source_burned=source_burned,
            correction_branding=correction,
        )


def test_non_superseded_r2_outside_path_remains_a_strict_locator_failure(
    tmp_path: Path,
) -> None:
    authority, artifacts, release, evidence = _inputs(tmp_path)
    record_path, _sha256, _bytes, target, role = artifacts["record"]
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["story_contract"]["unrelated_historical_path"] = "/outside-r2-evidence/forbidden.json"
    record_path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    changed_artifacts = dict(artifacts)
    changed_artifacts["record"] = (
        record_path,
        _sha(record_path),
        record_path.stat().st_size,
        target,
        role,
    )
    target_root = tmp_path / "final-package"
    with pytest.raises(
        finalization.QixiCorrectedPackageError,
        match="fresh evidence locator contract failed: record: frozen path escapes source workspace",
    ):
        finalization._project_documents(
            authority=authority,
            artifacts=changed_artifacts,
            candidate_root=target_root,
            release_mappings=(
                (str(release), str(target_root / "replacement_recuts")),
                (str(release.parent), str(target_root)),
                (str(Path.cwd()), str(Path.cwd())),
            ),
            release_workspace_root=str(release.parent),
            evidence_mappings=(
                (str(evidence), str(target_root / "replacement_recuts")),
                (str(evidence.parent), str(target_root)),
                (str(Path.cwd()), str(Path.cwd())),
            ),
            evidence_workspace_root=str(evidence.parent),
        )


def test_symlinked_source_artifact_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "source"
    root.mkdir()
    (root / "real").write_bytes(b"x")
    (root / "link").symlink_to(root / "real")
    descriptor = {"source": "link", "sha256": "sha256:" + hashlib.sha256(b"x").hexdigest(), "bytes": 1, "target": "x", "target_role": "package"}
    with pytest.raises(finalization.QixiCorrectedPackageError, match="symlink"):
        finalization._artifact(root, descriptor, label="artifact")


def test_source_root_symlink_and_target_prefix_collision_are_refused(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(source)
    with pytest.raises(finalization.QixiCorrectedPackageError, match="non-symlink"):
        finalization._source_root(alias, label="release")
    existing = tmp_path / "final"
    existing.mkdir()
    with pytest.raises(finalization.QixiCorrectedPackageError, match="create-only"):
        finalization._validate_create_only_target(existing)
    target = source / "new-final"
    assert finalization._target_overlaps_source(target, source) is True


def test_authority_hash_or_extra_key_is_refused(tmp_path: Path) -> None:
    authority, _artifacts, _release, _evidence = _inputs(tmp_path)
    authority["unexpected"] = True
    with pytest.raises(finalization.QixiCorrectedPackageError, match="keys"):
        finalization._authority(authority, repo_root=Path.cwd())
    authority.pop("unexpected")
    authority["authority_sha256"] = "sha256:" + "0" * 64
    with pytest.raises(finalization.QixiCorrectedPackageError, match="hash"):
        finalization._authority(authority, repo_root=Path.cwd())


def test_uniform_host_locator_projection_reuses_contract_and_rejects_unknown_paths() -> None:
    mappings = (
        ("/source/package", "/target/package"),
        ("/source/candidate", "/target/candidate"),
        ("/source/repo", "/target/repo"),
    )
    record = {
        "media_path": "/source/package/main.mp4",
        "subtitle_path": "/source/package/final.srt",
        "burned_preview": {
            "path": "/source/package/burned.mp4",
            "ass_path": "/source/package/final.ass",
            "command": ["ffmpeg", "/source/package/main.mp4"],
        },
    }
    projected = project_uniform_host_locators(
        record, kind="record", mappings=mappings, source_workspace_root="/source"
    )
    assert projected["media_path"] == "/target/package/main.mp4"
    assert projected["burned_preview"]["command"] == record["burned_preview"]["command"]
    record["unexpected"] = "/source/package/not-a-locator"
    with pytest.raises(PackageRelocationError, match="unknown external-host path"):
        project_uniform_host_locators(
            record, kind="record", mappings=mappings, source_workspace_root="/source"
        )


def test_uniform_host_locator_rejects_role_crossing_and_preserves_frozen_chat() -> None:
    mappings = (
        ("/source/package", "/target/package"),
        ("/source/candidate", "/target/candidate"),
        ("/source/repo", "/target/repo"),
    )
    with pytest.raises(PackageRelocationError, match="wrong root role"):
        project_uniform_host_locators(
            {"media_path": "/source/candidate/not-a-package.mp4"},
            kind="record",
            mappings=mappings,
            source_workspace_root="/source",
        )
    chat = {
        "final_text_srt_path": "/source/package/final.srt",
        "burn_binding": {"burned_media_path": "/source/package/z1.mp4"},
        "owner_ledger": {"historical_path": "/source/recordings/ledger.json"},
    }
    projected = project_uniform_host_locators(
        chat, kind="chat", mappings=mappings, source_workspace_root="/source"
    )
    assert projected["final_text_srt_path"] == "/target/package/final.srt"
    assert projected["owner_ledger"] == chat["owner_ledger"]
    chat["owner_ledger"]["historical_path"] = "/tmp/unrelated.json"
    with pytest.raises(PackageRelocationError, match="escapes source workspace"):
        project_uniform_host_locators(
            chat, kind="chat", mappings=mappings, source_workspace_root="/source"
        )


def test_planned_locator_closure_rejects_unmaterialized_target_basename(
    tmp_path: Path,
) -> None:
    authority, artifacts, release, evidence = _inputs(tmp_path)
    target = tmp_path / "final-package"
    record, publish, chat = finalization._project_documents(
        authority=authority,
        artifacts=artifacts,
        candidate_root=target,
        release_mappings=(
            (str(release), str(target / "replacement_recuts")),
            (str(release.parent), str(target)),
            (str(Path.cwd()), str(Path.cwd())),
        ),
        release_workspace_root=str(release.parent),
        evidence_mappings=(
            (str(evidence), str(target / "replacement_recuts")),
            (str(evidence.parent), str(target)),
            (str(Path.cwd()), str(Path.cwd())),
        ),
        evidence_workspace_root=str(evidence.parent),
    )
    chat["final_text_srt_path"] = str(target / "replacement_recuts" / "r2.srt")
    with pytest.raises(finalization.QixiCorrectedPackageError, match="outside the materialized plan"):
        finalization._assert_planned_locator_closure(
            record=record,
            publish=publish,
            chat=chat,
            artifacts=artifacts,
            candidate_root=target,
        )


def test_cli_does_not_accept_an_arbitrary_repo_root() -> None:
    cli = Path("scripts/finalize_qixi_corrected_package.py").read_text(encoding="utf-8")
    assert 'add_argument("--repo-root"' not in cli


def test_real_finalizer_receipt_wiring_through_manual_audit_and_final_human(
    tmp_path: Path,
) -> None:
    """Receipt wiring only; the deployed Qixi package separately faces all 23 audit gates."""
    authority, _artifacts, release, evidence = _inputs(tmp_path)
    repo = _sealed_test_repo(tmp_path, authority)
    target = tmp_path / "materialized"
    receipt = finalization.finalize(
        repo_root=repo,
        release_root=release,
        evidence_root=evidence,
        target=target,
        apply=True,
    )

    package = target / "replacement_recuts"
    assert receipt["mode"] == "APPLIED"
    assert receipt["after_image_sha256"]["record"]["bytes"] != receipt["artifacts"]["record"]["bytes"]
    assert finalization.validate_applied_receipt(
        receipt, package_root=package, repo_root=repo
    ) == receipt
    manifest = build_manual(
        package,
        operator="test-operator",
        note="synthetic closure wiring only",
        qixi_repo_root=repo,
    )
    assert manifest["items"][0]["manual_corrected_same_bv"] == receipt[
        "manual_corrected_same_bv"
    ]
    issues: list[dict] = []
    _audit_manual_corrected_same_bv_receipt(
        root=package, item=manifest["items"][0], issues=issues, stem="qixi", qixi_repo_root=repo
    )
    assert issues == []
    final_human_review._validate_manual_corrected_same_bv_item(
        item=manifest["items"][0],
        candidate_id=CID,
        package_root=package,
        qixi_repo_root=repo,
    )
    (package / "qixi-corrected-package-finalization.json").write_text("{}\n", encoding="utf-8")
    _audit_manual_corrected_same_bv_receipt(
        root=package, item=manifest["items"][0], issues=issues, stem="qixi", qixi_repo_root=repo
    )
    assert [issue["code"] for issue in issues] == ["MANUAL_CORRECTED_SAME_BV_RECEIPT_INVALID"]
    with pytest.raises(
        final_human_review.FinalHumanReviewError,
        match="FINAL_HUMAN_REVIEW_MANUAL_CORRECTED_RECEIPT_INVALID",
    ):
        final_human_review._validate_manual_corrected_same_bv_item(
            item=manifest["items"][0],
            candidate_id=CID,
            package_root=package,
            qixi_repo_root=repo,
        )
    receipt["after_image_sha256"]["record"]["bytes"] += 1
    with pytest.raises(finalization.QixiCorrectedPackageError, match="hash drifts"):
        finalization.validate_applied_receipt(receipt, package_root=package, repo_root=repo)


def test_invalid_typed_receipt_rejects_before_candidate_evidence_sync(
    tmp_path: Path,
) -> None:
    authority, _artifacts, release, evidence = _inputs(tmp_path)
    repo = _sealed_test_repo(tmp_path, authority)
    target = tmp_path / "materialized"
    finalization.finalize(
        repo_root=repo,
        release_root=release,
        evidence_root=evidence,
        target=target,
        apply=True,
    )
    package = target / "replacement_recuts"
    candidate_chat = target / f"{CID}.chat-authority.json"
    candidate_clip = target / f"{CID}.clip-context.json"
    package_chat = package / f"{CID}.chat-authority.json"
    package_clip = package / f"{CID}.clip-context.json"
    assert candidate_chat.is_file() and candidate_clip.is_file()
    assert not package_chat.exists() and not package_clip.exists()
    (package / "qixi-corrected-package-finalization.json").write_text("{}\n", encoding="utf-8")
    with pytest.raises(DailyManifestError, match="manual corrected same-BV receipt"):
        build_manual(
            package,
            operator="test-operator",
            note="reject before package mutation",
            qixi_repo_root=repo,
        )
    assert not package_chat.exists() and not package_clip.exists()


def test_typed_receipt_drift_during_sync_writes_no_later_sidecars_or_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A legal candidate sync cannot turn an already-replayed receipt into a mixed manifest."""

    authority, _artifacts, release, evidence = _inputs(tmp_path)
    repo = _sealed_test_repo(tmp_path, authority)
    target = tmp_path / "materialized"
    finalization.finalize(
        repo_root=repo,
        release_root=release,
        evidence_root=evidence,
        target=target,
        apply=True,
    )
    package = target / "replacement_recuts"
    receipt_path = package / "qixi-corrected-package-finalization.json"
    assert receipt_path.is_file()
    cover_sidecars = (
        "qixi.cover.pre-overlay.png",
        "qixi.cover.ai-bg.png",
        "qixi.cover.title-mask.png",
    )
    cover_before = {
        name: (package / name).read_bytes() for name in cover_sidecars
    }

    original_sync = manual_review_manifest._sync_record_bound_candidate_artifacts

    def sync_then_tamper(**kwargs: object) -> dict[str, str]:
        projected = original_sync(**kwargs)
        receipt_path.write_text("{}\n", encoding="utf-8")
        return projected

    monkeypatch.setattr(
        manual_review_manifest,
        "_sync_record_bound_candidate_artifacts",
        sync_then_tamper,
    )
    monkeypatch.setattr(
        manual_review_manifest,
        "_verified_cover_artifact",
        lambda *_args, **_kwargs: pytest.fail(
            "receipt drift must reject before any cover sidecar projection"
        ),
    )
    with pytest.raises(DailyManifestError) as raised:
        build_manual(
            package,
            operator="test-operator",
            note="TOCTOU receipt drift canary",
            qixi_repo_root=repo,
        )
    assert str(raised.value) == "manual corrected same-BV receipt drifted during packaging"
    assert not (package / "review_manifest.json").exists()
    assert {
        name: (package / name).read_bytes() for name in cover_sidecars
    } == cover_before


def test_dry_run_creates_neither_target_nor_staging_residue(tmp_path: Path) -> None:
    authority, _artifacts, release, evidence = _inputs(tmp_path)
    repo = _sealed_test_repo(tmp_path, authority)
    target = tmp_path / "materialized"
    plan = finalization.finalize(
        repo_root=repo,
        release_root=release,
        evidence_root=evidence,
        target=target,
    )
    assert plan["mode"] == "DRY_RUN"
    assert not target.exists()
    assert list(tmp_path.glob(".materialized.qixi-finalize-*")) == []


def test_generated_write_failure_rolls_back_owned_stage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    authority, _artifacts, release, evidence = _inputs(tmp_path)
    repo = _sealed_test_repo(tmp_path, authority)
    target = tmp_path / "materialized"

    def fail_write(_path: Path, _value: object, *, owned: object) -> None:
        raise finalization.QixiCorrectedPackageError("injected generated write failure")

    monkeypatch.setattr(finalization, "_write_json_new", fail_write)
    with pytest.raises(finalization.QixiCorrectedPackageError, match="injected generated write failure"):
        finalization.finalize(
            repo_root=repo,
            release_root=release,
            evidence_root=evidence,
            target=target,
            apply=True,
        )
    assert not target.exists()
    assert list(tmp_path.glob(".materialized.qixi-finalize-*")) == []


def test_target_appearing_before_commit_is_not_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    authority, _artifacts, release, evidence = _inputs(tmp_path)
    repo = _sealed_test_repo(tmp_path, authority)
    target = tmp_path / "materialized"
    original_assert = finalization._assert_context_authority_unchanged
    calls = 0

    def assert_then_create(context: object) -> None:
        nonlocal calls
        original_assert(context)
        calls += 1
        if calls == 2:
            target.mkdir()

    monkeypatch.setattr(finalization, "_assert_context_authority_unchanged", assert_then_create)
    with pytest.raises(finalization.QixiCorrectedPackageError, match="target appeared"):
        finalization.finalize(
            repo_root=repo,
            release_root=release,
            evidence_root=evidence,
            target=target,
            apply=True,
        )
    assert target.is_dir()
    assert list(tmp_path.glob(".materialized.qixi-finalize-*")) == []


def test_authority_swap_after_execution_plan_rejects_before_target_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    authority, _artifacts, release, evidence = _inputs(tmp_path)
    repo = _sealed_test_repo(tmp_path, authority)
    target = tmp_path / "materialized"
    authority_path = repo / finalization.AUTHORITY_RELATIVE_PATH
    alternate_cover = release / "cover-swapped.png"
    alternate_cover.write_bytes(b"swapped-cover")
    swapped = json.loads(authority_path.read_text(encoding="utf-8"))
    swapped["release"]["cover"].update(
        {
            "source": alternate_cover.name,
            "sha256": _sha(alternate_cover),
            "bytes": alternate_cover.stat().st_size,
        }
    )
    swapped_without_hash = dict(swapped)
    swapped_without_hash.pop("authority_sha256")
    swapped["authority_sha256"] = finalization._canonical_sha(swapped_without_hash)
    original_write = finalization._write_json_new
    swapped_once = False

    def write_then_swap(path: Path, value: object, *, owned: object) -> None:
        nonlocal swapped_once
        original_write(path, value, owned=owned)
        if not swapped_once:
            authority_path.write_text(json.dumps(swapped, ensure_ascii=False), encoding="utf-8")
            swapped_once = True

    monkeypatch.setattr(finalization, "_write_json_new", write_then_swap)
    with pytest.raises(finalization.QixiCorrectedPackageError, match="authority changed after planning"):
        finalization.finalize(
            repo_root=repo,
            release_root=release,
            evidence_root=evidence,
            target=target,
            apply=True,
        )
    assert swapped_once
    assert not target.exists()
    assert list(tmp_path.glob(".materialized.qixi-finalize-*")) == []


def test_deployed_authority_epoch_swap_rejects_before_target_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    authority, _artifacts, release, evidence = _inputs(tmp_path)
    repo = _sealed_test_repo(tmp_path, authority)
    target = tmp_path / "materialized"
    authority_path = repo / finalization.AUTHORITY_RELATIVE_PATH
    original_authority_bytes = authority_path.read_bytes()
    original_assert = finalization._assert_context_authority_unchanged
    calls = 0

    def assert_then_advance_epoch(context: object) -> None:
        nonlocal calls
        original_assert(context)
        calls += 1
        if calls != 1:
            return
        manifest_path = repo / "DEPLOYED_AUTHORITY_MANIFEST.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["deployed_commit"] = "1" * 40
        manifest_without_hash = dict(manifest)
        manifest_without_hash.pop("manifest_sha256")
        manifest["manifest_sha256"] = finalization._canonical_sha(manifest_without_hash)
        (repo / "DEPLOYED_COMMIT").write_text("1" * 40 + "\n", encoding="utf-8")
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

    monkeypatch.setattr(
        finalization,
        "_assert_context_authority_unchanged",
        assert_then_advance_epoch,
    )
    with pytest.raises(finalization.QixiCorrectedPackageError, match="authority epoch changed"):
        finalization.finalize(
            repo_root=repo,
            release_root=release,
            evidence_root=evidence,
            target=target,
            apply=True,
        )
    assert calls == 1
    assert authority_path.read_bytes() == original_authority_bytes
    assert not target.exists()
    assert list(tmp_path.glob(".materialized.qixi-finalize-*")) == []


def test_mid_copy_regular_replacement_refuses_rollback_without_deleting_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    authority, _artifacts, release, evidence = _inputs(tmp_path)
    repo = _sealed_test_repo(tmp_path, authority)
    target = tmp_path / "materialized"
    original_copy = finalization._copy_checked
    replacement_path: Path | None = None

    def copy_then_replace(
        source: Path,
        destination: Path,
        expected_sha: str,
        *,
        owned: dict[Path, tuple[int, int, int]],
    ) -> None:
        nonlocal replacement_path
        original_copy(source, destination, expected_sha, owned=owned)
        if replacement_path is None:
            destination.unlink()
            destination.write_bytes(b"unowned replacement")
            replacement_path = destination
            raise finalization.QixiCorrectedPackageError("injected mid-copy failure")

    monkeypatch.setattr(finalization, "_copy_checked", copy_then_replace)
    with pytest.raises(finalization.QixiCorrectedPackageError, match="ownership inode changed"):
        finalization.finalize(
            repo_root=repo,
            release_root=release,
            evidence_root=evidence,
            target=target,
            apply=True,
        )
    assert replacement_path is not None and replacement_path.read_bytes() == b"unowned replacement"
    assert not target.exists()


def test_mid_copy_symlink_replacement_refuses_rollback_without_deleting_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    authority, _artifacts, release, evidence = _inputs(tmp_path)
    repo = _sealed_test_repo(tmp_path, authority)
    target = tmp_path / "materialized"
    external = tmp_path / "external"
    external.write_bytes(b"outside stage")
    original_copy = finalization._copy_checked
    replacement_path: Path | None = None

    def copy_then_replace(
        source: Path,
        destination: Path,
        expected_sha: str,
        *,
        owned: dict[Path, tuple[int, int, int]],
    ) -> None:
        nonlocal replacement_path
        original_copy(source, destination, expected_sha, owned=owned)
        if replacement_path is None:
            destination.unlink()
            destination.symlink_to(external)
            replacement_path = destination
            raise finalization.QixiCorrectedPackageError("injected mid-copy failure")

    monkeypatch.setattr(finalization, "_copy_checked", copy_then_replace)
    with pytest.raises(finalization.QixiCorrectedPackageError, match="ownership inode changed"):
        finalization.finalize(
            repo_root=repo,
            release_root=release,
            evidence_root=evidence,
            target=target,
            apply=True,
        )
    assert replacement_path is not None and replacement_path.is_symlink()
    assert external.read_bytes() == b"outside stage"
    assert not target.exists()


def test_manifest_receipt_replay_never_reloads_the_hashed_receipt_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    authority, _artifacts, release, evidence = _inputs(tmp_path)
    repo = _sealed_test_repo(tmp_path, authority)
    target = tmp_path / "materialized"
    receipt = finalization.finalize(
        repo_root=repo,
        release_root=release,
        evidence_root=evidence,
        target=target,
        apply=True,
    )
    package = target / "replacement_recuts"
    manifest = build_manual(
        package,
        operator="test-operator",
        note="single receipt-read canary",
        qixi_repo_root=repo,
    )
    receipt_path = package / finalization.RECEIPT_FILENAME
    original_load = finalization._load_object
    receipt_loads = 0

    def swap_if_reloaded(path: Path, *, label: str) -> dict:
        nonlocal receipt_loads
        if path == receipt_path:
            receipt_loads += 1
            receipt_path.write_text("{}\n", encoding="utf-8")
        return original_load(path, label=label)

    monkeypatch.setattr(finalization, "_load_object", swap_if_reloaded)
    assert finalization.validate_manifest_bound_applied_receipt(
        manifest["items"][0],
        candidate_id=CID,
        package_root=package,
        repo_root=repo,
    ) is True
    assert receipt_loads == 0
    assert finalization.validate_applied_receipt(
        receipt, package_root=package, repo_root=repo
    ) == receipt
