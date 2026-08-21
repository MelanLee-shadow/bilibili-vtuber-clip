from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from src.autoslice import qixi_post_correction_public_surface as closure
from src.autoslice import qixi_post_correction_projection_paths as projection_paths
from src.autoslice import publish_staging
from src.autoslice import source_fact_staging
from src.autoslice.cover_host_identity_gate import (
    AUTHORITY as HOST_IDENTITY_AUTHORITY,
    SCHEMA_VERSION as HOST_IDENTITY_SCHEMA_VERSION,
    validate_final_host_identity_verification as real_validate_final_host_identity,
)
from src.autoslice.cover_punch_semantics import (
    SCHEMA_VERSION as PUNCH_SCHEMA_VERSION,
    review_cover_punch_semantics,
    validate_cover_punch_semantic_review as real_validate_punch,
)
from src.autoslice.cover_route_evidence import (
    build_cover_route_decision,
    record_cover_route_execution,
    validate_cover_route_decision as real_validate_route,
    validate_final_participant_verification as real_validate_participant,
    validate_rendered_text_pixel_evidence as real_validate_pixels,
)
from src.autoslice.cover_text_pixel_evidence import materialize_rendered_text_pixel_evidence
from src.autoslice.cover_title_rendering import render_title_layer, sha256_file
from src.autoslice.publish_staging_paths import private_publish_path
from src.autoslice.review_package_portable_evidence import rebuild_package_speaker_evidence
from src.autoslice.review_package_source_fact_audit import audit_story_source_fact_receipt
from src.autoslice.source_fact_review import review_and_repair_source_facts
from scripts.build_lidousha_daily_review_manifest import _validate_source_fact_receipts
from scripts.finalize_qixi_post_correction_public_surface import _parse_args


_COVER_FONT = (
    Path(__file__).parents[1] / "assets/lidousha/fonts/ZCOOLKuaiLe-Regular.ttf"
)


def _sha(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _json(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()


def _descriptor(path: Path) -> dict[str, object]:
    payload = path.read_bytes()
    return {"path": str(path), "sha256": _sha(payload), "bytes": len(payload)}


def _sealed_descriptor(path: Path) -> dict[str, object]:
    return {**_descriptor(path), "mode": path.stat().st_mode & 0o777}


_UNIFORM_HOST_EVIDENCE = {
    "state": "AbsentAuthorized",
    "reason": "speaker_mode_uniform_host",
    "policy_ids": {
        "alignment": "speaker_cue_subsegment_alignment/v1",
        "text": "compact_ws/v1",
        "timing": "half_open_integer_ms_exact/v1",
        "absence": "speaker_mode_uniform_host/v1",
    },
}


def _keep_source_fact_receipt(
    *,
    subtitle_path: Path,
    selection_hook: str,
    title: str,
    context_prompt: str,
    scorecard: object,
) -> dict[str, object]:
    return review_and_repair_source_facts(
        selection_hook=selection_hook,
        title=title,
        final_transcript="女友感",
        clip_context_prompt=context_prompt,
        selection_scorecard=scorecard,
        candidate_id=closure.CANDIDATE_ID,
        final_reviewed_srt_path=subtitle_path,
        speaker_evidence=_UNIFORM_HOST_EVIDENCE,
        llm_call=lambda _prompt: json.dumps(
            {
                "schema_version": "lidousha-source-fact-review.v1",
                "status": "KEEP",
                "final_selection_hook": selection_hook,
                "final_title": title,
                "supported_by": ["final_transcript"],
                "changed_surfaces": [],
                "addressee_attribution": [],
                "selection_scorecard_review": {
                    "status": "NOT_NEEDED",
                    "reason": "未改钩子",
                },
                "summary": "最终字幕保留女友感。",
            },
            ensure_ascii=False,
        ),
    )


@pytest.fixture(autouse=True)
def _synthetic_cover_validators(monkeypatch: pytest.MonkeyPatch) -> None:
    """Transaction fixtures use a byte-only cover; real schemas get a gate test."""

    monkeypatch.setattr(projection_paths, "validate_cover_route_decision", lambda *_a, **_k: True)
    monkeypatch.setattr(projection_paths, "validate_rendered_text_pixel_evidence", lambda *_a, **_k: True)
    monkeypatch.setattr(projection_paths, "validate_final_host_identity_verification", lambda *_a, **_k: True)
    monkeypatch.setattr(projection_paths, "validate_final_participant_verification", lambda *_a, **_k: True)
    monkeypatch.setattr(projection_paths, "validate_cover_punch_semantic_review", lambda *_a, **_k: True)


def _repository(repo: Path) -> None:
    for relative in (
        "assets/lidousha/sealed_subtitle_corrections/auto_123655_771_844.v1.json",
        "assets/lidousha/sealed_subtitle_corrections/auto_123655_771_844.diagnostic-diff.v1.json",
        "assets/lidousha/manual_title_overrides.v1.json",
    ):
        source = Path(__file__).parents[1] / relative
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "assets"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@example.invalid", "commit", "-qm", "seal"],
        cwd=repo,
        check=True,
    )


def _fixture(tmp_path: Path) -> tuple[Path, Path, dict[str, object], dict[str, Path]]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _repository(repo)
    runtime = tmp_path / "runtime"
    package = runtime / "out" / closure.RECORDING_DATE / closure.CANDIDATE_ID / "replacement_recuts"
    package.mkdir(parents=True)
    delivery = runtime / "repo" / "lidousha" / closure.RECORDING_DATE
    delivery.mkdir(parents=True)
    candidate = package.parent
    clip = candidate / f"{closure.CANDIDATE_ID}.clip-context.json"
    main = package / f"{closure.CANDIDATE_ID}.recut.mp4"
    srt = package / f"{closure.CANDIDATE_ID}.recut.srt"
    ass = package / f"{closure.CANDIDATE_ID}.recut.final-sapphire72.ass"
    burn = package / f"{closure.CANDIDATE_ID}.recut.burned-final-sapphire72.mp4"
    correction = package / f"{closure.CANDIDATE_ID}.human-text-correction.json"
    publish = package / f"{closure.CANDIDATE_ID}.recut.publish.json"
    cover = package / "covers" / f"{closure.CANDIDATE_ID}.cover.png"
    chat = candidate / f"{closure.CANDIDATE_ID}.chat-authority.json"
    timing = package / f"{closure.CANDIDATE_ID}.timing.json"
    for path, payload in ((main, b"main"), (srt, "1\n00:00:00,000 --> 00:00:01,000\n女友感\n".encode()), (ass, b"ass"), (burn, b"burn"), (cover, b"cover"), (chat, b"chat"), (timing, b"timing")):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    context = {
        "schema_version": "lidousha-clip-context.v1",
        "candidate_id": closure.CANDIDATE_ID,
        "recording_date": closure.RECORDING_DATE,
        "selection_hook": "女友感",
        "pieces": [{"start_ms": 0, "end_ms": 1000, "source_media_sha256": "sha256:" + "0" * 64, "recording_basename": "fixture.mp4"}],
        "session_relation_authority": None,
        "whole_clip_draft_srt": srt.read_text(),
        "whole_clip_draft_srt_sha256": _sha(srt.read_text().encode()),
        "structured_chat": [],
        "topic_resolution": {},
        "session_topic_authorities": [],
        "speech_memory": {"entries": [], "ledger_sha256": "sha256:" + "1" * 64},
        "retrieval_budget": {"whole_clip_transcript_truncated": False},
        "mutation_authorized": False,
    }
    context["context_sha256"] = closure._canonical_sha256(context)
    clip.write_bytes(_json(context))
    correction.write_bytes(_json({"candidate_id": closure.CANDIDATE_ID, "after_srt_sha256": _sha(srt.read_bytes())}))
    context_prompt, scorecard = "fixture context", {}
    source_fact = _keep_source_fact_receipt(
        subtitle_path=srt,
        selection_hook="女友感",
        title=closure.PUBLIC_TITLE,
        context_prompt=context_prompt,
        scorecard=scorecard,
    )
    record = {
        "status": "MATERIALIZED",
        "media_path": str(main),
        "subtitle_path": str(srt),
        "subtitle_ass_path": str(ass),
        "human_text_correction_manifest_path": str(correction),
        "human_text_correction_manifest_sha256": _sha(correction.read_bytes()),
        "artifact_hashes": {"video_sha256": _sha(main.read_bytes()), "subtitle_sha256": _sha(srt.read_bytes()), "ass_sha256": _sha(ass.read_bytes()), "burned_video_sha256": _sha(burn.read_bytes())},
        "clip_context_path": str(clip),
        "selection_scorecard": {},
        "session_relation_authority": None,
        "speaker_mode": "uniform_host",
        "story_contract": {"selection_hook": "女友感", "source_media_sha256s": ["sha256:" + "0" * 64], "boundary_semantic_review": {}, "human_boundary_authority": "test", "clip_context_prompt": context_prompt, "selection_scorecard": scorecard, "source_fact_review": source_fact},
        "publish_staging": {"title": "old"},
    }
    record_path = package / f"{closure.CANDIDATE_ID}.record.json"
    record_path.write_bytes(_json(record))
    delivery_record = delivery / "delivery.record.json"
    delivery_record.write_bytes(record_path.read_bytes())
    publish.write_bytes(
        _json(
            {
                "schema_version": "shadow-publish-draft.v1",
                "candidate_id": closure.CANDIDATE_ID,
                "title": "旧 LLM 标题",
                "title_source": "llm+lidousha_style_asset",
                "title_authority_status": "RESOLVED_LLM",
                "title_authority_error": None,
                "title_policy_violations": [],
                "video_path": str(main),
                "artifact_hashes": dict(record["artifact_hashes"]),
                "upload_enabled": False,
            }
        )
    )
    state_path = runtime / "state" / f"{closure.RECORDING_DATE}.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_bytes(_json({"picks": [{"candidate_id": closure.CANDIDATE_ID, "title": "old"}]}))
    artifacts = {name: _descriptor(path) for name, path in {"record": record_path, "delivery_record": delivery_record, "publish": publish, "main": main, "srt": srt, "ass": ass, "burn": burn, "correction": correction, "cover": cover, "chat": chat, "clip_context": clip, "timing": timing}.items()}
    repository_inputs = {}
    for name, relative in {
        "subtitle_authority": "assets/lidousha/sealed_subtitle_corrections/auto_123655_771_844.v1.json",
        "diagnostic": "assets/lidousha/sealed_subtitle_corrections/auto_123655_771_844.diagnostic-diff.v1.json",
        "manual_title_overrides": "assets/lidousha/manual_title_overrides.v1.json",
    }.items():
        item = _descriptor(repo / relative)
        item["path"] = relative
        repository_inputs[name] = item
    state_descriptor = _descriptor(state_path)
    state_descriptor["mode"] = state_path.stat().st_mode & 0o777
    authority: dict[str, object] = {"schema_version": closure.SCHEMA_VERSION, "candidate_id": closure.CANDIDATE_ID, "recording_date": closure.RECORDING_DATE, "upload_enabled": False, "manual_title": closure.MANUAL_TITLE, "runtime_root": str(runtime), "state_file": state_descriptor, "artifacts": artifacts, "repository_inputs": repository_inputs, "source_fact_preimage": {"clip_context_prompt_sha256": _sha(context_prompt.encode()), "clip_context_prompt_bytes": len(context_prompt.encode()), "source_fact_receipt_sha256": source_fact["receipt_sha256"], "speaker_evidence": _UNIFORM_HOST_EVIDENCE, "speaker_evidence_sha256": closure._canonical_sha256(_UNIFORM_HOST_EVIDENCE)}, "sealed_before": {"record": _sealed_descriptor(record_path), "delivery_record": _sealed_descriptor(delivery_record), "publish": _sealed_descriptor(publish), "state": _sealed_descriptor(state_path)}}
    authority["authority_sha256"] = closure._canonical_sha256(authority)
    return repo, runtime, authority, {"record": record_path, "delivery": delivery_record, "publish": publish, "state": state_path, "srt": srt, "burn": burn}


def _fake_stage(record: dict[str, object], **kwargs: object) -> dict[str, object]:
    root = Path(str(kwargs["private_artifact_root"]))
    publish_path = Path(str(kwargs["private_publish_json_path"]))
    cover = root / "covers" / "new.cover.png"
    cover.parent.mkdir(parents=True, exist_ok=True)
    cover.write_bytes(b"new-cover")
    story = closure._story_contract_rebuilder(
        record,
        srt_path=Path(str(record["subtitle_path"])),
        clip_context_path=Path(str(record["clip_context_path"])),
    )(str(record["story_contract"]["selection_hook"]))
    receipt = _keep_source_fact_receipt(
        subtitle_path=Path(str(record["subtitle_path"])),
        selection_hook=str(story["selection_hook"]),
        title=closure.PUBLIC_TITLE,
        context_prompt=str(story["clip_context_prompt"]),
        scorecard=story["selection_scorecard"],
    )
    generation = {
        "final_cover": str(cover),
        "final_cover_sha256": _sha(cover.read_bytes()),
        "cover_text_mode": "punch",
        "cover_text": "女友感",
        "rendered_lines": ["女友感"],
        "art_direction": {"cover_punch_semantic_review": {}},
    }
    staged = copy.deepcopy(record)
    hashes = dict(staged["artifact_hashes"])
    hashes["cover_sha256"] = generation["final_cover_sha256"]
    staged["artifact_hashes"] = hashes
    story["source_fact_review"] = receipt
    staged["story_contract"] = story
    manual_projection = {
        "title": closure.PUBLIC_TITLE,
        "title_source": "ivan_manual_override",
        "title_authority_status": "RESOLVED_MANUAL",
        "recovery_publication_authority": None,
        "title_authority_error": None,
        "title_policy_violations": [],
        "important_content_ips": [],
        "title_story_audit": {"status": "PASS", "candidate_id": closure.CANDIDATE_ID},
        "entity_projection_audit": None,
        "cover_entity_projection_audit": None,
        "source_fact_review": receipt,
        "manual_title_repair_authority_consumption": None,
        "manual_title_keep_authority_consumption": None,
        "public_text_surface_authority_consumption": None,
        "cover_status": "AI_COVER_READY",
        "cover_path": str(cover),
        "cover_text": "女友感",
        "cover_generation": generation,
        "reason_codes": [],
        "upload_enabled": False,
    }
    staged["publish_staging"] = {
        "status": "STAGED",
        **manual_projection,
        "publish_json_path": str(publish_path),
    }
    publish = {
        "schema_version": "shadow-publish-draft.v1",
        "candidate_id": closure.CANDIDATE_ID,
        "video_path": str(record["media_path"]),
        "artifact_hashes": hashes,
        **manual_projection,
    }
    publish_path.write_bytes(_json(publish))
    return staged


def _materialize_real_cover_generation(tmp_path: Path, story: dict[str, object]) -> tuple[dict[str, object], dict[Path, bytes]]:
    """Create a production-shape cover whose real validators can replay."""

    tmp_path.mkdir(parents=True, exist_ok=True)
    pre_overlay = tmp_path / "pre-overlay.png"
    final_cover = tmp_path / "final-cover.png"
    Image.new("RGB", (1920, 1080), (244, 238, 220)).save(pre_overlay)
    cover_text = "女友感"
    render_spec = {
        "schema_version": "lidousha-cover-title-render-spec.v1",
        "font_file_name": _COVER_FONT.name,
        "font_file_sha256": sha256_file(_COVER_FONT),
        "font_face_index": 0,
        "layer_size": [1200, 420],
        "rendered_layer_size": [1200, 420],
        "angle_degrees": 0,
        "lines": [
            {
                "x": 20,
                "y": 20,
                "font_size": 120,
                "segment_pad": 10,
                "segments": [{"text": cover_text, "fill": [255, 198, 41]}],
                "outlines": [
                    {"width": 10, "color": [18, 36, 79]},
                    {"width": 5, "color": [255, 255, 255]},
                ],
            }
        ],
    }
    layer = render_title_layer(render_spec, font_path=_COVER_FONT)
    with Image.open(pre_overlay) as source:
        image = source.convert("RGB")
    image.paste(layer, (360, 100), layer)
    image.save(final_cover)
    pixels = materialize_rendered_text_pixel_evidence(
        final_cover_path=final_cover,
        pre_overlay_path=pre_overlay,
        font_path=_COVER_FONT,
        render_spec=render_spec,
        paste_xy=(360, 100),
        font_size=120,
        rendered_text=cover_text,
    )
    punch, punch_review = review_cover_punch_semantics(
        title=closure.PUBLIC_TITLE,
        cover_text=cover_text,
        story_hook=str(story["selection_hook"]),
        punch=(cover_text,),
        llm_call=lambda _prompt: json.dumps(
            {
                "schema_version": PUNCH_SCHEMA_VERSION,
                "status": "PASS",
                "final_punch": {"main": cover_text, "sub": None},
                "stranger_can_infer_event": True,
                "contains_concrete_subject": True,
                "contains_action_or_conflict": True,
                "no_fabricated_fact": True,
                "story_summary": "主播回应了女友感这个话题。",
                "click_motivation": "观众可以看到主播如何回应这个话题。",
            },
            ensure_ascii=False,
        ),
    )
    assert punch == (cover_text,), json.dumps(punch_review, ensure_ascii=False)
    final_sha = sha256_file(final_cover)
    generation: dict[str, object] = {
        "title": closure.PUBLIC_TITLE,
        "cover_text_mode": "punch",
        "cover_text": cover_text,
        "rendered_lines": [cover_text],
        "rendered_text_pixels": pixels,
        "font_size": 120,
        "font_selection": {"font": _COVER_FONT.name, "glyph_risk": []},
        "angle_degrees": 0,
        "method": "images.edit",
        "cover_origin": "AI_REDRAW",
        "image_generation_planned": True,
        "image_generation_attempted": True,
        "image_generation_used": True,
        "ai_background": str(pre_overlay),
        "ai_background_sha256": sha256_file(pre_overlay),
        "pre_overlay_path": str(pre_overlay),
        "pre_overlay_sha256": pixels["pre_overlay_sha256"],
        "overlay_position": pixels["overlay_position"],
        "final_cover": str(final_cover),
        "final_cover_sha256": final_sha,
        "art_direction": {"cover_punch_semantic_review": punch_review},
        "final_host_identity_verification": {
            "schema_version": HOST_IDENTITY_SCHEMA_VERSION,
            "status": "PASS",
            "authority": HOST_IDENTITY_AUTHORITY,
            "final_cover_sha256": final_sha,
            "comparison_sha256": "sha256:" + "1" * 64,
            "witness": {"image_sha256": "1" * 64, "provider": "cpa"},
        },
    }
    generation["route_decision"] = build_cover_route_decision(
        selected_treatment="cpa_redraw",
        selected_rationale="fixture redraw with full final-pixel evidence",
        story_contract=story,
        reference_authority=None,
        decision_inputs={"cover_mode": "cpa"},
        title=closure.PUBLIC_TITLE,
        cover_text=cover_text,
    )
    record_cover_route_execution(
        generation,
        actual_treatment="cpa_redraw",
        execution_status="READY",
        image_generation_attempted=True,
        image_generation_used=True,
    )
    return generation, {
        pre_overlay: pre_overlay.read_bytes(),
        final_cover: final_cover.read_bytes(),
        Path(str(pixels["mask_path"])): Path(str(pixels["mask_path"])).read_bytes(),
    }


def test_dry_run_validates_exact_inputs_without_stage_residue(tmp_path: Path) -> None:
    repo, runtime, authority, paths = _fixture(tmp_path)
    result = closure.finalize(apply=False, repo_root=repo, runtime_root=runtime, authority=authority)
    assert result["status"] == "DRY_RUN_PASS"
    assert not list(paths["record"].parent.glob(".qixi-public-surface-stage-*"))
    assert paths["record"].read_bytes() == paths["delivery"].read_bytes()


def test_private_publish_path_never_opens_an_arbitrary_write_sink(tmp_path: Path) -> None:
    private_root = tmp_path / "private"
    private_root.mkdir()
    with pytest.raises(ValueError, match="requires a private artifact root"):
        private_publish_path(
            media_path=tmp_path / "media.mp4",
            private_artifact_root=None,
            private_publish_json_path=tmp_path / "outside.json",
            stage_cover=None,
        )
    with pytest.raises(ValueError, match="escapes the private artifact root"):
        private_publish_path(
            media_path=tmp_path / "media.mp4",
            private_artifact_root=private_root,
            private_publish_json_path=tmp_path / "outside.json",
            stage_cover=None,
        )


def test_cli_defaults_to_dry_run_and_exposes_no_runtime_path_flags() -> None:
    assert _parse_args([]).apply is False
    assert _parse_args(["--dry-run"]).apply is False
    assert _parse_args(["--apply"]).apply is True
    with pytest.raises(SystemExit):
        _parse_args(["--runtime-root", "/tmp/not-authorized"])


def test_apply_projects_identical_source_fact_and_rejects_foreign_drift(tmp_path: Path) -> None:
    repo, runtime, authority, paths = _fixture(tmp_path)
    original_srt, original_burn = paths["srt"].read_bytes(), paths["burn"].read_bytes()
    result = closure.finalize(apply=True, repo_root=repo, runtime_root=runtime, authority=authority, source_fact_llm_call=lambda _prompt: "{}", _stage_publish=_fake_stage)
    assert result["status"] == "APPLIED"
    record = json.loads(paths["record"].read_text())
    publish = json.loads(paths["publish"].read_text())
    state = json.loads(paths["state"].read_text())
    assert record["publish_staging"]["source_fact_review"] == record["story_contract"]["source_fact_review"] == publish["source_fact_review"] == state["picks"][0]["source_fact_review"]
    assert record["publish_staging"]["title"] == publish["title"] == state["picks"][0]["title"] == closure.PUBLIC_TITLE
    assert paths["delivery"].read_bytes() == paths["record"].read_bytes()
    assert paths["srt"].read_bytes() == original_srt and paths["burn"].read_bytes() == original_burn
    assert closure.finalize(apply=True, repo_root=repo, runtime_root=runtime, authority=authority, _stage_publish=_fake_stage)["status"] == "APPLIED"
    paths["publish"].write_bytes(b"foreign")
    with pytest.raises(closure.QixiPostCorrectionPublicSurfaceError, match="drift"):
        closure.finalize(apply=True, repo_root=repo, runtime_root=runtime, authority=authority, _stage_publish=_fake_stage)


def test_real_publish_stage_replays_manual_title_public_fields(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, runtime, authority, paths = _fixture(tmp_path)
    provider_calls: list[str] = []

    def canonical_cover(
        _record: object, *, private_artifact_root: Path, **_kwargs: object
    ) -> dict[str, object]:
        cover = private_artifact_root / "covers" / "canonical.cover.png"
        cover.parent.mkdir(parents=True, exist_ok=True)
        cover.write_bytes(b"canonical-cover")
        generation = {
            "final_cover": str(cover),
            "final_cover_sha256": _sha(cover.read_bytes()),
            "cover_text_mode": "punch",
            "cover_text": "女友感",
            "rendered_lines": ["女友感"],
            "art_direction": {"cover_punch_semantic_review": {}},
        }
        return {
            "status": "AI_COVER_READY",
            "cover_path": str(cover),
            "cover_sha256": generation["final_cover_sha256"],
            "cover_generation": generation,
            "reason_codes": [],
        }

    source_fact_response = json.dumps(
        {
            "schema_version": "lidousha-source-fact-review.v1",
            "status": "KEEP",
            "final_selection_hook": "女友感",
            "final_title": closure.PUBLIC_TITLE,
            "supported_by": ["final_transcript"],
            "changed_surfaces": [],
            "addressee_attribution": [],
            "selection_scorecard_review": {"status": "NOT_NEEDED", "reason": "未改钩子"},
            "summary": "最终字幕保留女友感。",
        },
        ensure_ascii=False,
    )
    monkeypatch.setattr(publish_staging, "_stage_lidousha_ai_cover", canonical_cover)
    # This is deliberately a generic provider-path fixture.  Its synthetic
    # predecessor is not the candidate-sealed Qixi authority preimage, so it
    # must opt out explicitly rather than weakening that production loader.
    monkeypatch.setattr(
        source_fact_staging, "load_qixi_operator_exact_title_authority", lambda _candidate_id: None
    )

    def source_fact_provider(_prompt: str) -> str:
        provider_calls.append("called")
        return source_fact_response

    assert closure.finalize(
        apply=True,
        repo_root=repo,
        runtime_root=runtime,
        authority=authority,
        _stage_publish=publish_staging._stage_publish_draft,
        source_fact_llm_call=source_fact_provider,
    )["status"] == "APPLIED"
    assert provider_calls == ["called"]
    record = json.loads(paths["record"].read_text())
    publish = json.loads(paths["publish"].read_text())
    staging = record["publish_staging"]
    for key in set(staging) - {"status", "publish_json_path"}:
        assert publish[key] == staging[key]
    assert staging["title_source"] == "ivan_manual_override"
    assert staging["title_authority_status"] == "RESOLVED_MANUAL"
    assert staging["title_authority_error"] is None
    assert staging["title_policy_violations"] == []
    assert staging["title_story_audit"]["status"] == "PASS"


@pytest.mark.parametrize("cover_mode", ("screenshot", "cpa"))
def test_canonical_cover_routes_prepare_only_under_private_artifact_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cover_mode: str
) -> None:
    """Both cover entry routes must never create legacy public sidecars first."""

    package_root = tmp_path / "package"
    private_root = tmp_path / "private-artifacts"
    media = package_root / "candidate.mp4"
    media.parent.mkdir()
    media.write_bytes(b"media")
    monkeypatch.setenv("AUTOSLICE_COVER_MODE", cover_mode)
    if cover_mode == "cpa":
        monkeypatch.setenv("CPA_BASE_URL", "https://cpa.example.test/v1")
        monkeypatch.setenv("CPA_API_KEY", "test-key")

    def block_after_reference_setup(
        *_args: object, **kwargs: object
    ) -> tuple[None, None, None, dict[str, object]]:
        assert Path(str(kwargs["cover_refs_dir"])).is_relative_to(private_root)
        return None, None, None, {"status": "BLOCKED_AI_COVER_REQUIRED"}

    monkeypatch.setattr(publish_staging, "_prepare_lidousha_cover_reference", block_after_reference_setup)
    result = publish_staging._stage_lidousha_ai_cover(
        {"status": "MATERIALIZED", "media_path": str(media)},
        media_path=media,
        candidate_id=closure.CANDIDATE_ID,
        title=closure.PUBLIC_TITLE,
        cover_text="女友感",
        run_ffmpeg=True,
        private_artifact_root=private_root,
    )
    assert result["status"] == "BLOCKED_AI_COVER_REQUIRED"
    assert sorted(path.relative_to(private_root).as_posix() for path in private_root.iterdir()) == [
        "cover_refs",
        "covers",
        "covers_ai_original",
        "evidence",
    ]
    assert not any(package_root.glob("cover_refs"))
    assert not any(package_root.glob("covers"))
    assert not any(package_root.glob("covers_ai_original"))
    assert not any(package_root.glob("evidence"))


def test_canonical_daily_and_package_source_fact_replays_accept_uniform_host_after_image(
    tmp_path: Path,
) -> None:
    repo, runtime, authority, paths = _fixture(tmp_path)
    closure.finalize(
        apply=True,
        repo_root=repo,
        runtime_root=runtime,
        authority=authority,
        _stage_publish=_fake_stage,
    )
    record = json.loads(paths["record"].read_text())
    publish = json.loads(paths["publish"].read_text())
    package_root = paths["record"].parent
    speaker_evidence = rebuild_package_speaker_evidence(
        root=package_root,
        item={"subtitle_srt": paths["srt"].name},
        record=record,
        subtitle_path=paths["srt"],
    )
    receipt_sha256 = _validate_source_fact_receipts(
        record_doc=record,
        publish_doc=publish,
        subtitle_path=paths["srt"],
        speaker_evidence=speaker_evidence,
    )
    assert receipt_sha256 == record["story_contract"]["source_fact_review"]["receipt_sha256"]
    issues = audit_story_source_fact_receipt(
        root=package_root,
        manifest={"source_fact_review_sha256": receipt_sha256},
        item={"subtitle_srt": paths["srt"].name},
        subtitle_path=paths["srt"],
        publish_path=paths["publish"],
        record_path=paths["record"],
        record=record,
        story_contract=record["story_contract"],
        artifact_title=publish["title"],
        final_transcript="女友感",
    )
    assert issues == ()


def test_after_image_replays_real_cover_route_pixel_host_and_punch_gates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The after-image gate must call real cover validators, not test stubs."""

    repo, runtime, authority, paths = _fixture(tmp_path)
    record = json.loads(paths["record"].read_text())
    story = closure._story_contract_rebuilder(
        record,
        srt_path=paths["srt"],
        clip_context_path=Path(str(record["clip_context_path"])),
    )(str(record["story_contract"]["selection_hook"]))
    generation, after = _materialize_real_cover_generation(paths["record"].parent / "real-cover", story)
    monkeypatch.setattr(projection_paths, "validate_cover_route_decision", real_validate_route)
    monkeypatch.setattr(projection_paths, "validate_rendered_text_pixel_evidence", real_validate_pixels)
    monkeypatch.setattr(projection_paths, "validate_final_host_identity_verification", real_validate_final_host_identity)
    monkeypatch.setattr(projection_paths, "validate_final_participant_verification", real_validate_participant)
    monkeypatch.setattr(projection_paths, "validate_cover_punch_semantic_review", real_validate_punch)
    closure._validate_replayed_cover(
        generation,
        story=story,
        package_root=paths["record"].parent,
        after=after,
    )


def test_uniform_host_preimage_rejects_speaker_sidecar_claim_before_stage(tmp_path: Path) -> None:
    repo, runtime, authority, paths = _fixture(tmp_path)
    record = json.loads(paths["record"].read_text())
    record["speaker_review_srt_path"] = "invented.srt"
    paths["record"].write_bytes(_json(record))
    paths["delivery"].write_bytes(paths["record"].read_bytes())
    authority["artifacts"]["record"] = _descriptor(paths["record"])
    authority["artifacts"]["delivery_record"] = _descriptor(paths["delivery"])
    authority["sealed_before"]["record"] = _sealed_descriptor(paths["record"])
    authority["sealed_before"]["delivery_record"] = _sealed_descriptor(paths["delivery"])
    authority["authority_sha256"] = closure._canonical_sha256(
        {key: value for key, value in authority.items() if key != "authority_sha256"}
    )
    with pytest.raises(closure.QixiPostCorrectionPublicSurfaceError, match="preimage"):
        closure.finalize(
            apply=True,
            repo_root=repo,
            runtime_root=runtime,
            authority=authority,
            _stage_publish=_fake_stage,
        )


def test_existing_legacy_cover_sidecar_stays_immutable(tmp_path: Path) -> None:
    repo, runtime, authority, paths = _fixture(tmp_path)
    conflict = paths["record"].parent / "covers" / "new.cover.png"
    conflict.parent.mkdir(parents=True, exist_ok=True)
    conflict.write_bytes(b"foreign-cover")
    assert closure.finalize(
        apply=True,
        repo_root=repo,
        runtime_root=runtime,
        authority=authority,
        _stage_publish=_fake_stage,
    )["status"] == "APPLIED"
    assert conflict.read_bytes() == b"foreign-cover"


def test_public_artifact_namespace_collision_blocks_before_staging(tmp_path: Path) -> None:
    repo, runtime, authority, paths = _fixture(tmp_path)
    namespace = closure._public_artifact_root(paths["record"].parent, authority)
    collision = projection_paths.public_artifact_path(namespace, "covers/new.cover.png")
    collision.write_bytes(b"foreign")
    before = paths["record"].read_bytes()
    with pytest.raises(closure.QixiPostCorrectionPublicSurfaceError, match="namespace already exists"):
        closure.finalize(
            apply=True,
            repo_root=repo,
            runtime_root=runtime,
            authority=authority,
            _stage_publish=_fake_stage,
        )
    assert paths["record"].read_bytes() == before


def test_public_artifact_mapping_is_injective_for_delimiter_bearing_names(tmp_path: Path) -> None:
    _repo, _runtime, authority, paths = _fixture(tmp_path)
    namespace = closure._public_artifact_root(paths["record"].parent, authority)
    assert projection_paths.public_artifact_path(
        namespace, "covers/a--b.png"
    ) != projection_paths.public_artifact_path(
        namespace, "covers--a/b.png"
    )


def test_stage_publish_self_locator_is_only_allowed_in_record_staging(tmp_path: Path) -> None:
    repo, runtime, authority, paths = _fixture(tmp_path)

    def bad_self_locator(record: dict[str, object], **kwargs: object) -> dict[str, object]:
        staged = _fake_stage(record, **kwargs)
        staged["untrusted_publish_pointer"] = str(kwargs["private_publish_json_path"])
        return staged

    before = paths["record"].read_bytes()
    with pytest.raises(closure.QixiPostCorrectionPublicSurfaceError, match="self-locator"):
        closure.finalize(
            apply=True,
            repo_root=repo,
            runtime_root=runtime,
            authority=authority,
            _stage_publish=bad_self_locator,
        )
    assert paths["record"].read_bytes() == before


def test_busy_runner_lock_blocks_before_private_stage_or_target_write(tmp_path: Path) -> None:
    repo, runtime, authority, paths = _fixture(tmp_path)
    before = paths["record"].read_bytes()
    lock = runtime / "runner.lock"
    with lock.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(closure.QixiPostCorrectionPublicSurfaceError, match="runner.lock is busy"):
            closure.finalize(
                apply=True,
                repo_root=repo,
                runtime_root=runtime,
                authority=authority,
                _stage_publish=lambda *_args, **_kwargs: pytest.fail("stage ran while lock busy"),
            )
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    assert paths["record"].read_bytes() == before
    assert not list(paths["record"].parent.glob(".qixi-public-surface-stage-*"))


def test_private_stage_copy_uses_distinct_inode_and_cannot_mutate_sealed_source(
    tmp_path: Path,
) -> None:
    source = tmp_path / "sealed.srt"
    stage = tmp_path / "private" / "sealed.srt"
    stage.parent.mkdir()
    source.write_bytes(b"sealed subtitle")
    source.chmod(0o640)
    source_before = (source.stat().st_dev, source.stat().st_ino, source.stat().st_mode & 0o777, _sha(source.read_bytes()))
    projection_paths.copy_sealed_stage_file(source, stage)
    stage_snapshot = (stage.stat().st_dev, stage.stat().st_ino, stage.stat().st_mode & 0o777, _sha(stage.read_bytes()))
    assert stage_snapshot[0:1] == source_before[0:1]
    assert stage_snapshot[1] != source_before[1]
    assert stage_snapshot[2:] == source_before[2:]
    stage.write_bytes(b"stage mutation")
    assert (source.stat().st_dev, source.stat().st_ino, source.stat().st_mode & 0o777, _sha(source.read_bytes())) == source_before


def test_backed_up_owned_temp_is_adopted_after_precheckpoint_process_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, runtime, authority, paths = _fixture(tmp_path)
    original_stage = closure._create_staged_inode

    def crash_after_owned_temp(entry: object) -> object:
        snapshot = original_stage(entry)
        if isinstance(entry, dict) and entry.get("role") == "record":
            raise KeyboardInterrupt("crash after staged temp")
        return snapshot

    monkeypatch.setattr(closure, "_create_staged_inode", crash_after_owned_temp)
    with pytest.raises(KeyboardInterrupt, match="staged temp"):
        closure.finalize(
            apply=True,
            repo_root=repo,
            runtime_root=runtime,
            authority=authority,
            _stage_publish=_fake_stage,
        )
    root = closure._journal_root_from_authority(authority)
    journal = json.loads((root / "journal.json").read_text())
    record_entry = next(row for row in journal["entries"] if row["role"] == "record")
    assert record_entry["phase"] == "BACKED_UP"
    temp = closure._entry_staged_path(record_entry)
    assert temp.is_file() and (temp.with_name(temp.name + ".owner")).is_file()
    monkeypatch.setattr(closure, "_create_staged_inode", original_stage)
    assert closure.finalize(
        apply=True, repo_root=repo, runtime_root=runtime, authority=authority
    )["status"] == "APPLIED"
    assert paths["record"].read_bytes() == paths["delivery"].read_bytes()
    assert not temp.exists()
    assert not temp.with_name(temp.name + ".owner").exists()


@pytest.mark.parametrize("mutation", ("foreign_bytes", "symlink", "foreign_inode"))
def test_backed_up_temp_adoption_rejects_unowned_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    repo, runtime, authority, paths = _fixture(tmp_path)
    original_stage = closure._create_staged_inode

    def crash_after_owned_temp(entry: object) -> object:
        snapshot = original_stage(entry)
        if isinstance(entry, dict) and entry.get("role") == "record":
            raise KeyboardInterrupt("crash after staged temp")
        return snapshot

    monkeypatch.setattr(closure, "_create_staged_inode", crash_after_owned_temp)
    with pytest.raises(KeyboardInterrupt, match="staged temp"):
        closure.finalize(
            apply=True,
            repo_root=repo,
            runtime_root=runtime,
            authority=authority,
            _stage_publish=_fake_stage,
        )
    root = closure._journal_root_from_authority(authority)
    journal = json.loads((root / "journal.json").read_text())
    entry = next(row for row in journal["entries"] if row["role"] == "record")
    temp = closure._entry_staged_path(entry)
    if mutation == "foreign_bytes":
        temp.write_bytes(b"foreign")
    elif mutation == "symlink":
        foreign = tmp_path / "foreign-temp"
        foreign.write_bytes(b"foreign")
        temp.unlink()
        temp.symlink_to(foreign)
    else:
        foreign = tmp_path / "same-bytes-temp"
        foreign.write_bytes(temp.read_bytes())
        foreign.chmod(temp.stat().st_mode & 0o777)
        os.replace(foreign, temp)
    monkeypatch.setattr(closure, "_create_staged_inode", original_stage)
    with pytest.raises(closure.QixiPostCorrectionPublicSurfaceError, match="uncheckpointed staged transaction"):
        closure.finalize(apply=True, repo_root=repo, runtime_root=runtime, authority=authority)
    assert paths["record"].read_bytes() == closure._unb64(entry["before_bytes_b64"], label="test")


@pytest.mark.parametrize("crash_on_target", [1, 2])
def test_crash_during_install_reenters_from_prepared_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, crash_on_target: int
) -> None:
    repo, runtime, authority, paths = _fixture(tmp_path)
    original_replace = closure.os.replace
    writes = 0

    def crash_after_one(source: Path | str, target: Path | str) -> None:
        nonlocal writes
        if str(source).endswith(".tmp"):
            writes += 1
            if writes == crash_on_target:
                raise RuntimeError("simulated crash")
        original_replace(source, target)

    monkeypatch.setattr(closure.os, "replace", crash_after_one)
    with pytest.raises(RuntimeError, match="simulated crash"):
        closure.finalize(apply=True, repo_root=repo, runtime_root=runtime, authority=authority, _stage_publish=_fake_stage)

    journal = closure._journal_root_from_authority(authority) / "journal.json"
    assert json.loads(journal.read_text())["status"] == "PREPARED"
    assert not list(paths["record"].parent.glob(".qixi-public-surface-stage-*"))
    monkeypatch.setattr(closure.os, "replace", original_replace)
    assert closure.finalize(apply=True, repo_root=repo, runtime_root=runtime, authority=authority)["status"] == "APPLIED"
    assert paths["record"].read_bytes() == paths["delivery"].read_bytes()


@pytest.mark.parametrize("mutation", ["foreign_target", "symlink_backup"])
def test_prepared_transaction_rejects_foreign_target_or_backup_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    repo, runtime, authority, paths = _fixture(tmp_path)
    original_stage = closure._create_staged_inode

    def hold_before_record_install(entry: object) -> object:
        if isinstance(entry, dict) and entry.get("role") == "record":
            raise RuntimeError("hold prepared")
        return original_stage(entry)

    monkeypatch.setattr(closure, "_create_staged_inode", hold_before_record_install)
    with pytest.raises(RuntimeError, match="hold prepared"):
        closure.finalize(apply=True, repo_root=repo, runtime_root=runtime, authority=authority, _stage_publish=_fake_stage)
    root = closure._journal_root_from_authority(authority)
    if mutation == "foreign_target":
        paths["record"].write_bytes(b"foreign")
        expected = "foreign target"
    else:
        journal = json.loads((root / "journal.json").read_text())
        entry = next(row for row in journal["entries"] if row["role"] == "record")
        backup = root / "backups" / entry["backup_name"]
        backup.unlink()
        backup.symlink_to(tmp_path / "foreign-backup")
        expected = "backup"
    monkeypatch.setattr(closure, "_create_staged_inode", original_stage)
    with pytest.raises(closure.QixiPostCorrectionPublicSurfaceError, match=expected):
        closure.finalize(apply=True, repo_root=repo, runtime_root=runtime, authority=authority)


def test_prepared_transaction_rejects_same_bytes_foreign_inode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, runtime, authority, paths = _fixture(tmp_path)
    original_stage = closure._create_staged_inode
    monkeypatch.setattr(
        closure,
        "_create_staged_inode",
        lambda _entry: (_ for _ in ()).throw(RuntimeError("hold prepared")),
    )
    with pytest.raises(RuntimeError, match="hold prepared"):
        closure.finalize(
            apply=True,
            repo_root=repo,
            runtime_root=runtime,
            authority=authority,
            _stage_publish=_fake_stage,
        )
    replacement = tmp_path / "same-record-bytes.json"
    replacement.write_bytes(paths["record"].read_bytes())
    replacement.chmod(paths["record"].stat().st_mode & 0o777)
    os.replace(replacement, paths["record"])
    monkeypatch.setattr(closure, "_create_staged_inode", original_stage)
    with pytest.raises(closure.QixiPostCorrectionPublicSurfaceError, match="foreign target inode"):
        closure.finalize(apply=True, repo_root=repo, runtime_root=runtime, authority=authority)


def test_committed_backup_residue_is_verified_then_cleaned(tmp_path: Path) -> None:
    repo, runtime, authority, _paths = _fixture(tmp_path)
    assert closure.finalize(
        apply=True,
        repo_root=repo,
        runtime_root=runtime,
        authority=authority,
        _stage_publish=_fake_stage,
    )["status"] == "APPLIED"
    root = closure._journal_root_from_authority(authority)
    journal = json.loads((root / "journal.json").read_text())
    backups = root / "backups"
    backups.mkdir(mode=0o700)
    for entry in journal["entries"]:
        before = closure._unb64(entry["before_bytes_b64"], label="test")
        if before is not None:
            (backups / entry["backup_name"]).write_bytes(before)
    # Model a crash after cleanup already removed one owned backup inode.
    first = next(backups.iterdir())
    first.unlink()
    assert closure.finalize(apply=True, repo_root=repo, runtime_root=runtime, authority=authority)["status"] == "APPLIED"
    assert not backups.exists()


def test_resealed_journal_cannot_target_immutable_subtitle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, runtime, authority, paths = _fixture(tmp_path)
    original_stage = closure._create_staged_inode

    def stop_before_install(entry: object) -> None:
        raise RuntimeError("hold prepared")

    monkeypatch.setattr(closure, "_create_staged_inode", stop_before_install)
    with pytest.raises(RuntimeError, match="hold prepared"):
        closure.finalize(apply=True, repo_root=repo, runtime_root=runtime, authority=authority, _stage_publish=_fake_stage)
    journal_path = closure._journal_root_from_authority(authority) / "journal.json"
    journal = json.loads(journal_path.read_text())
    entry = next(row for row in journal["entries"] if row["role"] == "cover_artifact")
    entry["target"] = str(paths["srt"])
    entry["before_bytes_b64"] = closure._b64(paths["srt"].read_bytes())
    entry["before_sha256"] = _sha(paths["srt"].read_bytes())
    entry["before_mode"] = paths["srt"].stat().st_mode & 0o777
    entry["before_device"] = paths["srt"].stat().st_dev
    entry["before_inode"] = paths["srt"].stat().st_ino
    journal["journal_sha256"] = closure._journal_sha256(journal)
    journal_path.write_bytes(_json(journal))
    monkeypatch.setattr(closure, "_create_staged_inode", original_stage)
    with pytest.raises(closure.QixiPostCorrectionPublicSurfaceError, match="cover predecessor is not create-only"):
        closure.finalize(apply=True, repo_root=repo, runtime_root=runtime, authority=authority)


def test_resealed_journal_cannot_change_an_unrelated_state_field(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, runtime, authority, _paths = _fixture(tmp_path)
    original_stage = closure._create_staged_inode
    monkeypatch.setattr(
        closure,
        "_create_staged_inode",
        lambda _entry: (_ for _ in ()).throw(RuntimeError("hold prepared")),
    )
    with pytest.raises(RuntimeError, match="hold prepared"):
        closure.finalize(apply=True, repo_root=repo, runtime_root=runtime, authority=authority, _stage_publish=_fake_stage)
    journal_path = closure._journal_root_from_authority(authority) / "journal.json"
    journal = json.loads(journal_path.read_text())
    state = next(row for row in journal["entries"] if row["role"] == "state")
    after = json.loads(closure._unb64(state["after_bytes_b64"], label="test") or b"{}")
    after["unrelated"] = "tampered"
    payload = _json(after)
    state["after_bytes_b64"], state["after_sha256"] = closure._b64(payload), _sha(payload)
    journal["journal_sha256"] = closure._journal_sha256(journal)
    journal_path.write_bytes(_json(journal))
    monkeypatch.setattr(closure, "_create_staged_inode", original_stage)
    with pytest.raises(closure.QixiPostCorrectionPublicSurfaceError, match="state closure drifts"):
        closure.finalize(apply=True, repo_root=repo, runtime_root=runtime, authority=authority)


@pytest.mark.parametrize("role", ["record", "delivery_record", "publish", "state"])
def test_resealed_journal_cannot_replace_a_sealed_predecessor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    repo, runtime, authority, _paths = _fixture(tmp_path)
    monkeypatch.setattr(
        closure,
        "_create_staged_inode",
        lambda _entry: (_ for _ in ()).throw(RuntimeError("hold prepared")),
    )
    with pytest.raises(RuntimeError, match="hold prepared"):
        closure.finalize(apply=True, repo_root=repo, runtime_root=runtime, authority=authority, _stage_publish=_fake_stage)
    journal_path = closure._journal_root_from_authority(authority) / "journal.json"
    journal = json.loads(journal_path.read_text())
    entry = next(row for row in journal["entries"] if row["role"] == role)
    replacement = b"self-rehashed but unsealed predecessor"
    entry["before_bytes_b64"] = closure._b64(replacement)
    entry["before_sha256"] = _sha(replacement)
    journal["journal_sha256"] = closure._journal_sha256(journal)
    journal_path.write_bytes(_json(journal))
    with pytest.raises(closure.QixiPostCorrectionPublicSurfaceError, match="sealed predecessor bytes drift"):
        closure.finalize(apply=True, repo_root=repo, runtime_root=runtime, authority=authority)


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ("ass_locator", "record/delivery media closure"),
        ("ass_hash", "final media binding"),
        ("record_unrelated", "record closure mutates an unrelated field"),
        ("publish_title", "source-fact/title/cover mirrors"),
    ],
)
def test_resealed_journal_cannot_widen_the_after_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str, expected: str
) -> None:
    repo, runtime, authority, _paths = _fixture(tmp_path)
    monkeypatch.setattr(
        closure,
        "_create_staged_inode",
        lambda _entry: (_ for _ in ()).throw(RuntimeError("hold prepared")),
    )
    with pytest.raises(RuntimeError, match="hold prepared"):
        closure.finalize(apply=True, repo_root=repo, runtime_root=runtime, authority=authority, _stage_publish=_fake_stage)
    journal_path = closure._journal_root_from_authority(authority) / "journal.json"
    journal = json.loads(journal_path.read_text())
    entries = {entry["role"]: entry for entry in journal["entries"]}
    record = json.loads(closure._unb64(entries["record"]["after_bytes_b64"], label="test") or b"{}")
    delivery = json.loads(closure._unb64(entries["delivery_record"]["after_bytes_b64"], label="test") or b"{}")
    publish = json.loads(closure._unb64(entries["publish"]["after_bytes_b64"], label="test") or b"{}")
    if mutation == "ass_locator":
        record["subtitle_ass_path"] = "/foreign.ass"
        delivery["subtitle_ass_path"] = "/foreign.ass"
    elif mutation == "ass_hash":
        record["artifact_hashes"]["ass_sha256"] = "sha256:" + "f" * 64
        delivery["artifact_hashes"]["ass_sha256"] = "sha256:" + "f" * 64
    elif mutation == "record_unrelated":
        record["status"] = "FOREIGN"
        delivery["status"] = "FOREIGN"
    else:
        publish["title"] = "foreign title"
    for role, value in (("record", record), ("delivery_record", delivery), ("publish", publish)):
        payload = _json(value)
        entries[role]["after_bytes_b64"] = closure._b64(payload)
        entries[role]["after_sha256"] = _sha(payload)
    journal["journal_sha256"] = closure._journal_sha256(journal)
    journal_path.write_bytes(_json(journal))
    with pytest.raises(closure.QixiPostCorrectionPublicSurfaceError, match=expected):
        closure.finalize(apply=True, repo_root=repo, runtime_root=runtime, authority=authority)


@pytest.mark.parametrize("crash_on_target", [1, 2, 3, 4, 5])
def test_process_crash_after_rename_resumes_owned_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, crash_on_target: int
) -> None:
    repo, runtime, authority, paths = _fixture(tmp_path)
    original_replace = closure.os.replace
    installs = 0

    def crash_after_rename(source: Path | str, target: Path | str) -> None:
        nonlocal installs
        original_replace(source, target)
        if str(source).endswith(".tmp"):
            installs += 1
            if installs == crash_on_target:
                raise KeyboardInterrupt("simulated process crash after rename")

    monkeypatch.setattr(closure.os, "replace", crash_after_rename)
    with pytest.raises(KeyboardInterrupt, match="process crash"):
        closure.finalize(apply=True, repo_root=repo, runtime_root=runtime, authority=authority, _stage_publish=_fake_stage)
    journal_path = closure._journal_root_from_authority(authority) / "journal.json"
    journal = json.loads(journal_path.read_text())
    assert journal["status"] == "PREPARED"
    assert any(entry["phase"] == "INSTALLING" for entry in journal["entries"])
    monkeypatch.setattr(closure.os, "replace", original_replace)
    assert closure.finalize(apply=True, repo_root=repo, runtime_root=runtime, authority=authority)["status"] == "APPLIED"
    assert paths["record"].read_bytes() == paths["delivery"].read_bytes()


def test_committed_cleanup_residue_is_a_typed_applied_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, runtime, authority, _paths = _fixture(tmp_path)
    original_cleanup = closure._cleanup_committed_backups
    monkeypatch.setattr(
        closure,
        "_cleanup_committed_backups",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("cleanup unavailable")),
    )
    result = closure.finalize(
        apply=True,
        repo_root=repo,
        runtime_root=runtime,
        authority=authority,
        _stage_publish=_fake_stage,
    )
    assert result["status"] == "APPLIED_WITH_CLEANUP_RESIDUE"
    root = closure._journal_root_from_authority(authority)
    assert json.loads((root / "journal.json").read_text())["status"] == "COMMITTED"
    assert (root / "final-receipt.json").is_file()
    monkeypatch.setattr(closure, "_cleanup_committed_backups", original_cleanup)
    assert closure.finalize(apply=True, repo_root=repo, runtime_root=runtime, authority=authority)["status"] == "APPLIED"


def test_installing_checkpoint_requires_its_durable_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, runtime, authority, _paths = _fixture(tmp_path)
    original_checkpoint = closure._checkpoint_entry

    def crash_after_installing_checkpoint(
        root: Path, journal: dict[str, object], index: int, **changes: object
    ) -> None:
        original_checkpoint(root, journal, index, **changes)
        entries = journal["entries"]
        assert isinstance(entries, list)
        if changes.get("phase") == "INSTALLING" and entries[index]["role"] == "record":
            raise KeyboardInterrupt("crash after installing checkpoint")

    monkeypatch.setattr(closure, "_checkpoint_entry", crash_after_installing_checkpoint)
    with pytest.raises(KeyboardInterrupt, match="installing checkpoint"):
        closure.finalize(apply=True, repo_root=repo, runtime_root=runtime, authority=authority, _stage_publish=_fake_stage)
    root = closure._journal_root_from_authority(authority)
    journal = json.loads((root / "journal.json").read_text())
    installing = next(
        entry for entry in journal["entries"] if entry["phase"] == "INSTALLING" and entry["role"] == "record"
    )
    (root / "backups" / installing["backup_name"]).unlink()
    monkeypatch.setattr(closure, "_checkpoint_entry", original_checkpoint)
    with pytest.raises(closure.QixiPostCorrectionPublicSurfaceError, match="backup"):
        closure.finalize(apply=True, repo_root=repo, runtime_root=runtime, authority=authority)


def test_installing_staged_symlink_is_rejected_before_target_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, runtime, authority, paths = _fixture(tmp_path)
    original_checkpoint = closure._checkpoint_entry

    def crash_after_installing_checkpoint(
        root: Path, journal: dict[str, object], index: int, **changes: object
    ) -> None:
        original_checkpoint(root, journal, index, **changes)
        entries = journal["entries"]
        assert isinstance(entries, list)
        if changes.get("phase") == "INSTALLING" and entries[index]["role"] == "record":
            raise KeyboardInterrupt("crash after installing checkpoint")

    monkeypatch.setattr(closure, "_checkpoint_entry", crash_after_installing_checkpoint)
    with pytest.raises(KeyboardInterrupt, match="installing checkpoint"):
        closure.finalize(apply=True, repo_root=repo, runtime_root=runtime, authority=authority, _stage_publish=_fake_stage)
    root = closure._journal_root_from_authority(authority)
    journal = json.loads((root / "journal.json").read_text())
    installing = next(
        entry for entry in journal["entries"] if entry["phase"] == "INSTALLING" and entry["role"] == "record"
    )
    staged = closure._entry_staged_path(installing)
    staged.unlink()
    foreign = paths["record"].parent / "foreign-stage"
    foreign.write_bytes(b"foreign")
    staged.symlink_to(foreign)
    before = paths["record"].read_bytes()
    monkeypatch.setattr(closure, "_checkpoint_entry", original_checkpoint)
    with pytest.raises(closure.QixiPostCorrectionPublicSurfaceError, match="staged transaction target"):
        closure.finalize(apply=True, repo_root=repo, runtime_root=runtime, authority=authority)
    assert paths["record"].read_bytes() == before


def test_staging_fchmod_race_never_unlinks_a_foreign_symlink_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "record.json"
    target.write_bytes(b"preimage")
    foreign = tmp_path / "foreign"
    foreign.write_bytes(b"foreign")
    entry = {
        "target": str(target),
        "staged_name": ".record.json.qixi-public-surface-000.tmp",
        "after_bytes_b64": closure._b64(b"after"),
        "after_sha256": _sha(b"after"),
        "after_mode": 0o644,
    }
    original_fchmod = closure.os.fchmod

    def replace_before_fchmod(fd: int, mode: int) -> None:
        staged = closure._entry_staged_path(entry)
        staged.unlink()
        staged.symlink_to(foreign)
        original_fchmod(fd, mode)

    monkeypatch.setattr(closure.os, "fchmod", replace_before_fchmod)
    with pytest.raises(closure.QixiPostCorrectionPublicSurfaceError, match="ownership drifted"):
        closure._create_staged_inode(entry)
    monkeypatch.setattr(closure.os, "fchmod", original_fchmod)
    staged = closure._entry_staged_path(entry)
    assert staged.is_symlink() and staged.resolve() == foreign
    assert foreign.read_bytes() == b"foreign"


def test_atomic_replace_fchmod_race_preserves_foreign_temp_and_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "record.json"
    target.write_bytes(b"sealed target")
    target.chmod(0o640)
    foreign = tmp_path / "foreign-temp"
    foreign.write_bytes(b"foreign temp")
    foreign.chmod(0o600)
    foreign_snapshot = (foreign.read_bytes(), foreign.stat().st_mode & 0o777)
    target_snapshot = (target.read_bytes(), target.stat().st_mode & 0o777)
    original_fchmod = closure.os.fchmod

    def replace_before_fchmod(fd: int, mode: int) -> None:
        temporary = next(tmp_path.glob(".record.json.qixi-public-surface-*"))
        os.replace(foreign, temporary)
        original_fchmod(fd, mode)

    monkeypatch.setattr(closure.os, "fchmod", replace_before_fchmod)
    with pytest.raises(closure.QixiPostCorrectionPublicSurfaceError, match="atomic transaction temporary ownership drifted"):
        closure._atomic_replace(target, b"after")
    assert (target.read_bytes(), target.stat().st_mode & 0o777) == target_snapshot
    temporary = next(tmp_path.glob(".record.json.qixi-public-surface-*"))
    assert (temporary.read_bytes(), temporary.stat().st_mode & 0o777) == foreign_snapshot


def test_atomic_replace_pre_replace_recheck_preserves_foreign_temp_and_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "record.json"
    target.write_bytes(b"sealed target")
    foreign = tmp_path / "foreign-temp"
    foreign.write_bytes(b"foreign temp")
    foreign_snapshot = (foreign.read_bytes(), foreign.stat().st_mode & 0o777)
    target_snapshot = (target.read_bytes(), target.stat().st_mode & 0o777)
    original_lstat = closure.os.lstat
    swapped = False

    def replace_before_recheck(path: Path | str):
        nonlocal swapped
        candidate = Path(path)
        if (
            not swapped
            and candidate.parent == tmp_path
            and candidate.name.startswith(".record.json.qixi-public-surface-")
        ):
            swapped = True
            os.replace(foreign, candidate)
        return original_lstat(path)

    monkeypatch.setattr(closure.os, "lstat", replace_before_recheck)
    with pytest.raises(closure.QixiPostCorrectionPublicSurfaceError, match="atomic transaction temporary ownership drifted"):
        closure._atomic_replace(target, b"after")
    assert (target.read_bytes(), target.stat().st_mode & 0o777) == target_snapshot
    temporary = next(tmp_path.glob(".record.json.qixi-public-surface-*"))
    assert (temporary.read_bytes(), temporary.stat().st_mode & 0o777) == foreign_snapshot


def test_journal_root_symlink_is_rejected_before_provider_or_public_write(tmp_path: Path) -> None:
    repo, runtime, authority, paths = _fixture(tmp_path)
    root = closure._journal_root_from_authority(authority)
    root.parent.mkdir(parents=True)
    root.symlink_to(tmp_path / "foreign")
    before = paths["record"].read_bytes()
    with pytest.raises(closure.QixiPostCorrectionPublicSurfaceError, match="journal root is unsafe"):
        closure.finalize(apply=True, repo_root=repo, runtime_root=runtime, authority=authority, _stage_publish=_fake_stage)
    assert paths["record"].read_bytes() == before
