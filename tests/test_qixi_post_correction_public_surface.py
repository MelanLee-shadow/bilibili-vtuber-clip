from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path

import pytest
from PIL import Image

from src.autoslice import qixi_post_correction_public_surface as closure
from src.autoslice import qixi_post_correction_modes as closure_modes
from src.autoslice.qixi_transaction_core import exclusive_runner_commit
from src.autoslice.repository_asset_authority import build_deployed_authority_manifest
from src.autoslice import qixi_post_correction_projection_paths as projection_paths
from src.autoslice import qixi_post_correction_public_artifact_recovery as basename_recovery
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
from src.autoslice.story_contract import cover_story_contract_binding
from scripts.build_lidousha_daily_review_manifest import _validate_source_fact_receipts
from scripts import finalize_qixi_post_correction_public_surface as closure_cli
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
    background = root / "covers_ai_original" / "new.background.png"
    cover.parent.mkdir(parents=True, exist_ok=True)
    background.parent.mkdir(parents=True, exist_ok=True)
    cover.write_bytes(b"new-cover")
    background.write_bytes(b"new-background")
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
        "ai_background": str(background),
        "ai_background_sha256": _sha(background.read_bytes()),
        "cover_text_mode": "punch",
        "cover_text": "女友感",
        "rendered_lines": ["女友感"],
        "art_direction": {"cover_punch_semantic_review": {}},
    }
    staged = copy.deepcopy(record)
    hashes = dict(staged["artifact_hashes"])
    hashes["cover_sha256"] = generation["final_cover_sha256"]
    hashes["ai_background_sha256"] = generation["ai_background_sha256"]
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
    assert result["status"] == "PLAN_PASS"
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
    assert _parse_args([]).mode is closure_modes.Mode.PLAN
    assert _parse_args(["--dry-run"]).mode is closure_modes.Mode.PLAN
    assert _parse_args(["--plan"]).mode is closure_modes.Mode.PLAN
    assert _parse_args(["--diagnose"]).mode is closure_modes.Mode.DIAGNOSE
    assert _parse_args(["--full-dry-run"]).mode is closure_modes.Mode.FULL_DRY_RUN
    assert _parse_args(["--apply"]).mode is closure_modes.Mode.APPLY
    with pytest.raises(SystemExit):
        _parse_args(["--runtime-root", "/tmp/not-authorized"])


def test_cli_full_dry_run_blocked_is_nonzero_but_diagnose_is_observational(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        closure_cli,
        "run",
        lambda mode, **_kwargs: {"status": "FULL_DRY_RUN_BLOCKED"}
        if mode is closure_modes.Mode.FULL_DRY_RUN
        else {"status": "DIAGNOSE_COMPLETE"},
    )
    assert closure_cli.main(["--full-dry-run"]) == 2
    assert closure_cli.main(["--diagnose"]) == 0


def test_after_image_matrix_has_fixed_ids_and_collects_independent_failures(
    tmp_path: Path,
) -> None:
    repo, runtime, authority, paths = _fixture(tmp_path)
    inputs = closure.validate_runtime(authority, repo_root=repo, runtime_root=runtime)
    stage = Path(tempfile.mkdtemp(prefix="matrix-stage-", dir=paths["record"].parent))
    try:
        targets, _ = closure._build_after_image(
            inputs,
            stage_root=stage,
            source_fact_llm_call=lambda _prompt: pytest.fail("provider must not run"),
            stage_publish=_fake_stage,
        )
        before = closure_modes._safe_before(targets)
        targets[paths["delivery"]] = b"{}"
        fixed_targets = {paths["record"], paths["delivery"], paths["publish"], paths["state"]}
        cover_target = next(path for path in targets if path not in fixed_targets)
        targets[cover_target] = b"forged-cover"
        matrix, _ = closure_modes.collect_matrix(
            authority=authority,
            repo_root=repo,
            runtime_root=runtime,
            before=before,
            after=targets,
        )
    finally:
        closure._remove_private_stage(stage)
    predicates = matrix["predicates"]
    assert [row["name"] for row in predicates] == list(closure_modes.MATRIX_PREDICATE_IDS)
    by_name = {row["name"]: row for row in predicates}
    assert by_name["record_delivery_media_closure"]["status"] == "FAIL"
    assert by_name["cover_materialized_hashes"]["status"] == "FAIL"
    assert by_name["state_exact_diff"]["status"] == "PASS"


def test_after_image_matrix_marks_generation_dependents_not_evaluated(
    tmp_path: Path,
) -> None:
    repo, runtime, authority, paths = _fixture(tmp_path)
    inputs = closure.validate_runtime(authority, repo_root=repo, runtime_root=runtime)
    stage = Path(tempfile.mkdtemp(prefix="matrix-stage-", dir=paths["record"].parent))
    try:
        targets, _ = closure._build_after_image(
            inputs,
            stage_root=stage,
            source_fact_llm_call=lambda _prompt: pytest.fail("provider must not run"),
            stage_publish=_fake_stage,
        )
        before = closure_modes._safe_before(targets)
        record = json.loads(targets[paths["record"]])
        publish = json.loads(targets[paths["publish"]])
        record["publish_staging"]["cover_generation"] = None
        publish["cover_generation"] = None
        targets[paths["record"]] = closure._json_bytes(record)
        targets[paths["delivery"]] = closure._json_bytes(record)
        targets[paths["publish"]] = closure._json_bytes(publish)
        matrix, _ = closure_modes.collect_matrix(
            authority=authority,
            repo_root=repo,
            runtime_root=runtime,
            before=before,
            after=targets,
        )
    finally:
        closure._remove_private_stage(stage)
    by_name = {row["name"]: row for row in matrix["predicates"]}
    assert [row["name"] for row in matrix["predicates"]] == list(
        closure_modes.MATRIX_PREDICATE_IDS
    )
    assert by_name["cover_generation_shape"]["status"] == "FAIL"
    for predicate_id in (
        "cover_route",
        "cover_rendered_text_pixels",
        "cover_final_host_identity",
        "cover_final_participant_identity",
        "cover_punch_semantics",
        "cover_materialized_hashes",
        "cover_projection",
        "publish_artifact_hashes",
        "immutable_target_scope",
    ):
        assert by_name[predicate_id]["status"] == "NOT_EVALUATED"
    assert by_name["source_fact_public_mirrors"]["status"] == "PASS"
    assert by_name["manual_title_authority_mapping"]["status"] == "PASS"
    assert by_name["state_exact_diff"]["status"] == "FAIL"


def test_full_dry_run_stages_privately_and_never_writes_targets(tmp_path: Path) -> None:
    repo, runtime, authority, paths = _fixture(tmp_path)
    before = {name: path.read_bytes() for name, path in paths.items()}
    result = closure_modes.run(
        closure_modes.Mode.FULL_DRY_RUN,
        repo_root=repo,
        runtime_root=runtime,
        authority=authority,
        stage_publish=_fake_stage,
        source_fact_llm_call=lambda _prompt: pytest.fail("provider must not run"),
    )
    assert result["status"] == "FULL_DRY_RUN_PASS"
    assert result["formal_validation"] == "PASS"
    assert [row["name"] for row in result["matrix"]["predicates"]] == list(
        closure_modes.MATRIX_PREDICATE_IDS
    )
    assert all(path.read_bytes() == before[name] for name, path in paths.items())
    assert not list(paths["record"].parent.glob(".qixi-public-surface-stage-*"))


def test_full_dry_run_failure_writes_sanitized_receipt_and_cleans_stage(tmp_path: Path) -> None:
    repo, runtime, authority, paths = _fixture(tmp_path)
    before = {name: path.read_bytes() for name, path in paths.items()}

    def stage_fails(*_args: object, **_kwargs: object) -> None:
        return None

    result = closure_modes.run(
        closure_modes.Mode.FULL_DRY_RUN,
        repo_root=repo,
        runtime_root=runtime,
        authority=authority,
        stage_publish=stage_fails,
        source_fact_llm_call=lambda _prompt: pytest.fail("provider must not run"),
        _test_deployed_seal={
            "deployed_commit": "b" * 40,
            "authority_file_sha256": "sha256:" + "c" * 64,
            "deployed_manifest_sha256": "sha256:" + "d" * 64,
            "deployed_manifest_file_sha256": "sha256:" + "e" * 64,
        },
    )
    receipt = Path(str(result["diagnostic_receipt"]))
    assert result["status"] == "FULL_DRY_RUN_BLOCKED"
    assert result["formal_validation"] == "NOT_RUN"
    assert receipt.is_file() and receipt.stat().st_mode & 0o777 == 0o600
    data = json.loads(receipt.read_text())
    assert data["schema_version"] == "qixi-public-surface-diagnostic.v2"
    assert data["operation_mode"] == "FULL_DRY_RUN"
    assert "prompt" not in receipt.read_text().lower()
    assert all(path.read_bytes() == before[name] for name, path in paths.items())
    assert not list(paths["record"].parent.glob(".qixi-public-surface-stage-*"))


def test_runtime_failure_survives_an_unsafe_diagnostic_receipt_sink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, runtime, authority, paths = _fixture(tmp_path)
    root = closure_modes.diagnostic_root(
        candidate_root=paths["record"].parent.parent,
        authority_sha256=authority["authority_sha256"],
    )
    (root / "foreign").symlink_to(root / "missing")
    monkeypatch.setattr(
        closure,
        "validate_runtime",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            closure.QixiPostCorrectionPublicSurfaceError("runtime preflight rejected")
        ),
    )
    result = closure_modes.run(
        closure_modes.Mode.FULL_DRY_RUN,
        repo_root=repo,
        runtime_root=runtime,
        authority=authority,
        _test_deployed_seal={
            "deployed_commit": "b" * 40,
            "authority_file_sha256": "sha256:" + "c" * 64,
            "deployed_manifest_sha256": "sha256:" + "d" * 64,
            "deployed_manifest_file_sha256": "sha256:" + "e" * 64,
        },
    )
    assert result["status"] == "FULL_DRY_RUN_BLOCKED"
    assert result["formal_validation"] == "NOT_RUN"
    assert result["diagnostic_receipt_status"] == "UNAVAILABLE"


def test_diagnostic_receipt_identity_is_bound_to_operation_mode(tmp_path: Path) -> None:
    repo, runtime_root, authority, _paths = _fixture(tmp_path)
    matrix, runtime = closure_modes.collect_matrix(
        authority=authority, repo_root=repo, runtime_root=runtime_root
    )
    assert runtime is not None
    test_seal = {
        "deployed_commit": "b" * 40,
        "authority_file_sha256": "sha256:" + "c" * 64,
        "deployed_manifest_sha256": "sha256:" + "d" * 64,
        "deployed_manifest_file_sha256": "sha256:" + "e" * 64,
    }
    evidence = closure_modes._provider_evidence(
        source_fact=closure_modes.ProviderAttemptStatus.NOT_ATTEMPTED,
        cover=closure_modes.ProviderAttemptStatus.UNKNOWN,
    )
    full = closure_modes._failure_receipt(
        closure, repo_root=repo, authority=authority, runtime=runtime, matrix=matrix,
        stage_manifest=[], provider_evidence=evidence,
        operation_mode=closure_modes.Mode.FULL_DRY_RUN, test_seal=test_seal,
    )
    apply = closure_modes._failure_receipt(
        closure, repo_root=repo, authority=authority, runtime=runtime, matrix=matrix,
        stage_manifest=[], provider_evidence=evidence,
        operation_mode=closure_modes.Mode.APPLY, test_seal=test_seal,
    )
    assert full != apply
    assert json.loads(full.read_text())["operation_mode"] == "FULL_DRY_RUN"
    assert json.loads(apply.read_text())["operation_mode"] == "APPLY"


def test_apply_constructs_default_source_fact_provider_only_when_stage_calls_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, runtime, authority, _paths = _fixture(tmp_path)
    constructed: list[str] = []

    def default_provider(_runtime_root: Path | None = None) -> object:
        constructed.append("constructed")
        return lambda prompt: "default:" + prompt

    def fake_finalize(**kwargs: object) -> dict[str, object]:
        assert constructed == []
        callback = kwargs["source_fact_llm_call"]
        return {"result": callback("sealed prompt")}  # type: ignore[operator]

    monkeypatch.setattr(closure, "_default_source_fact_llm", default_provider)
    monkeypatch.setattr(closure, "_finalize_legacy", fake_finalize)
    result = closure_modes._run_apply(
        closure, repo_root=repo, runtime_root=runtime, normalized=authority,
        source_fact_llm_call=None, stage_publish=None, test_seal=None,
    )
    assert result == {"result": "default:sealed prompt"}
    assert constructed == ["constructed"]


def test_bad_deployed_seal_cannot_create_a_diagnostic_namespace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, runtime_root, authority, paths = _fixture(tmp_path)
    matrix, runtime = closure_modes.collect_matrix(
        authority=authority, repo_root=repo, runtime_root=runtime_root
    )
    assert runtime is not None
    monkeypatch.setattr(
        closure_modes,
        "_deployed_seal",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("bad deployed seal")),
    )
    with pytest.raises(RuntimeError, match="bad deployed seal"):
        closure_modes._failure_receipt(
            closure, repo_root=repo, authority=authority, runtime=runtime, matrix=matrix,
            stage_manifest=[],
            provider_evidence=closure_modes._provider_evidence(
                source_fact=closure_modes.ProviderAttemptStatus.NOT_ATTEMPTED,
                cover=closure_modes.ProviderAttemptStatus.UNKNOWN,
            ),
            operation_mode=closure_modes.Mode.FULL_DRY_RUN,
            test_seal=None,
        )
    assert not (paths["record"].parent.parent / "qixi_post_correction_diagnostics").exists()


def test_stage_failure_survives_private_manifest_diagnostic_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, runtime, authority, _paths = _fixture(tmp_path)
    monkeypatch.setattr(
        closure_modes,
        "_partial_stage_manifest",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("private inventory drift")),
    )
    result = closure_modes.run(
        closure_modes.Mode.FULL_DRY_RUN,
        repo_root=repo,
        runtime_root=runtime,
        authority=authority,
        stage_publish=lambda *_args, **_kwargs: None,
        _test_deployed_seal={
            "deployed_commit": "b" * 40,
            "authority_file_sha256": "sha256:" + "c" * 64,
            "deployed_manifest_sha256": "sha256:" + "d" * 64,
            "deployed_manifest_file_sha256": "sha256:" + "e" * 64,
        },
    )
    assert result["status"] == "FULL_DRY_RUN_BLOCKED"
    assert result["formal_validation"] == "NOT_RUN"
    assert result["diagnostic_stage_manifest_status"] == "UNAVAILABLE"
    receipt = json.loads(Path(str(result["diagnostic_receipt"])).read_text())
    assert receipt["stage_manifest"] == []
    assert receipt["stage_manifest_status"] == "UNAVAILABLE"


def test_stage_manifest_failure_preserves_completed_formal_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, runtime, authority, _paths = _fixture(tmp_path)
    monkeypatch.setattr(
        closure_modes,
        "_stage_manifest",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("manifest serialization drift")),
    )
    result = closure_modes.run(
        closure_modes.Mode.FULL_DRY_RUN,
        repo_root=repo,
        runtime_root=runtime,
        authority=authority,
        stage_publish=_fake_stage,
        source_fact_llm_call=lambda _prompt: pytest.fail("provider must not run"),
        _test_deployed_seal={
            "deployed_commit": "b" * 40,
            "authority_file_sha256": "sha256:" + "c" * 64,
            "deployed_manifest_sha256": "sha256:" + "d" * 64,
            "deployed_manifest_file_sha256": "sha256:" + "e" * 64,
        },
    )
    assert result["status"] == "FULL_DRY_RUN_BLOCKED"
    assert result["formal_validation"] == "PASS"
    assert result["diagnostic_stage_manifest_status"] == "UNAVAILABLE"
    matrix = {row["name"]: row for row in result["matrix"]["predicates"]}
    assert matrix["stage_build"]["status"] == "PASS"
    assert matrix["formal_after_image"]["status"] == "PASS"
    receipt = json.loads(Path(str(result["diagnostic_receipt"])).read_text())
    assert receipt["stage_manifest_status"] == "UNAVAILABLE"


def test_full_dry_run_cleanup_failure_blocks_a_formally_valid_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, runtime, authority, paths = _fixture(tmp_path)
    monkeypatch.setattr(
        closure,
        "_remove_private_stage",
        lambda _stage: (_ for _ in ()).throw(OSError("cleanup unavailable")),
    )
    result = closure_modes.run(
        closure_modes.Mode.FULL_DRY_RUN,
        repo_root=repo,
        runtime_root=runtime,
        authority=authority,
        stage_publish=_fake_stage,
        source_fact_llm_call=lambda _prompt: pytest.fail("provider must not run"),
        _test_deployed_seal={
            "deployed_commit": "b" * 40,
            "authority_file_sha256": "sha256:" + "c" * 64,
            "deployed_manifest_sha256": "sha256:" + "d" * 64,
            "deployed_manifest_file_sha256": "sha256:" + "e" * 64,
        },
    )
    assert result["status"] == "FULL_DRY_RUN_BLOCKED"
    assert result["formal_validation"] == "PASS"
    matrix = {row["name"]: row for row in result["matrix"]["predicates"]}
    assert matrix["stage_cleanup"] == {
        "name": "stage_cleanup", "status": "FAIL", "reason": "UNEXPECTED_EXCEPTION"
    }
    assert Path(str(result["diagnostic_receipt"])).is_file()
    assert list(paths["record"].parent.glob(".qixi-public-surface-stage-*"))


def test_full_dry_run_preserves_formal_failure_when_cleanup_also_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, runtime, authority, _paths = _fixture(tmp_path)
    monkeypatch.setattr(
        closure,
        "_remove_private_stage",
        lambda _stage: (_ for _ in ()).throw(OSError("cleanup unavailable")),
    )
    result = closure_modes.run(
        closure_modes.Mode.FULL_DRY_RUN,
        repo_root=repo,
        runtime_root=runtime,
        authority=authority,
        stage_publish=lambda *_args, **_kwargs: None,
        source_fact_llm_call=lambda _prompt: pytest.fail("provider must not run"),
        _test_deployed_seal={
            "deployed_commit": "b" * 40,
            "authority_file_sha256": "sha256:" + "c" * 64,
            "deployed_manifest_sha256": "sha256:" + "d" * 64,
            "deployed_manifest_file_sha256": "sha256:" + "e" * 64,
        },
    )
    assert result["status"] == "FULL_DRY_RUN_BLOCKED"
    assert result["formal_validation"] == "NOT_RUN"
    matrix = {row["name"]: row for row in result["matrix"]["predicates"]}
    assert matrix["stage_build"]["status"] == "FAIL"
    assert matrix["stage_cleanup"]["status"] == "FAIL"
    assert Path(str(result["diagnostic_receipt"])).is_file()


def test_full_dry_run_stage_observation_reports_multiple_raw_failures_before_first_gate(
    tmp_path: Path,
) -> None:
    repo, runtime, authority, _paths = _fixture(tmp_path)

    def multiple_stage_drifts(record: dict[str, object], **kwargs: object) -> dict[str, object]:
        staged = _fake_stage(record, **kwargs)
        staged["publish_staging"]["title"] = "wrong returned title"
        staged["publish_staging"]["source_fact_review"] = {
            **staged["publish_staging"]["source_fact_review"],
            "diagnostic_drift": "returned",
        }
        staged["story_contract"]["source_fact_review"] = {
            **staged["story_contract"]["source_fact_review"],
            "diagnostic_drift": "story",
        }
        return staged

    result = closure_modes.run(
        closure_modes.Mode.FULL_DRY_RUN,
        repo_root=repo,
        runtime_root=runtime,
        authority=authority,
        stage_publish=multiple_stage_drifts,
        source_fact_llm_call=lambda _prompt: pytest.fail("provider must not run"),
        _test_deployed_seal={
            "deployed_commit": "b" * 40,
            "authority_file_sha256": "sha256:" + "c" * 64,
            "deployed_manifest_sha256": "sha256:" + "d" * 64,
            "deployed_manifest_file_sha256": "sha256:" + "e" * 64,
        },
    )
    assert result["status"] == "FULL_DRY_RUN_BLOCKED"
    assert result["formal_validation"] == "NOT_RUN"
    by_name = {row["name"]: row for row in result["matrix"]["predicates"]}
    assert by_name["stage_manual_title_consumed"]["status"] == "FAIL"
    assert by_name["stage_raw_source_fact_mirrors"]["status"] == "FAIL"
    assert by_name["stage_raw_story_source_fact_mirrors"]["status"] == "FAIL"
    assert by_name["stage_raw_cover_generation_mirrors"]["status"] == "PASS"


def test_provider_unavailable_is_not_recorded_as_an_attempt(tmp_path: Path) -> None:
    repo, runtime, authority, _paths = _fixture(tmp_path)

    def requires_source_provider(_record: dict[str, object], **kwargs: object) -> dict[str, object]:
        source_call = kwargs["source_fact_llm_call"]
        assert callable(source_call)
        source_call("provider-required")
        raise AssertionError("unreachable")

    result = closure_modes.run(
        closure_modes.Mode.FULL_DRY_RUN,
        repo_root=repo,
        runtime_root=runtime,
        authority=authority,
        stage_publish=requires_source_provider,
        source_fact_llm_call=None,
        _test_deployed_seal={
            "deployed_commit": "b" * 40,
            "authority_file_sha256": "sha256:" + "c" * 64,
            "deployed_manifest_sha256": "sha256:" + "d" * 64,
            "deployed_manifest_file_sha256": "sha256:" + "e" * 64,
        },
    )
    receipt = json.loads(Path(str(result["diagnostic_receipt"])).read_text())
    assert result["status"] == "FULL_DRY_RUN_BLOCKED"
    assert receipt["provider_evidence"] == {
        "source_fact": {"attempt_status": "NOT_ATTEMPTED", "receipt_sha256s": []},
        "cover": {"attempt_status": "UNKNOWN", "receipt_sha256s": []},
    }


def test_provider_response_is_not_mislabeled_as_a_persisted_receipt(
    tmp_path: Path,
) -> None:
    repo, runtime, authority, _paths = _fixture(tmp_path)

    raw_response = "unsealed response prompt=do-not-disclose-token-abc123"

    def calls_source_then_fails(_record: dict[str, object], **kwargs: object) -> dict[str, object]:
        assert kwargs["source_fact_llm_call"]("provider payload") == raw_response
        raise closure.QixiPostCorrectionPublicSurfaceError("stage rejected after provider call")

    result = closure_modes.run(
        closure_modes.Mode.FULL_DRY_RUN,
        repo_root=repo,
        runtime_root=runtime,
        authority=authority,
        stage_publish=calls_source_then_fails,
        source_fact_llm_call=lambda _prompt: raw_response,
        _test_deployed_seal={
            "deployed_commit": "b" * 40,
            "authority_file_sha256": "sha256:" + "c" * 64,
            "deployed_manifest_sha256": "sha256:" + "d" * 64,
            "deployed_manifest_file_sha256": "sha256:" + "e" * 64,
        },
    )
    receipt = json.loads(Path(str(result["diagnostic_receipt"])).read_text())
    assert receipt["provider_evidence"] == {
        "source_fact": {"attempt_status": "ATTEMPTED", "receipt_sha256s": []},
        "cover": {"attempt_status": "UNKNOWN", "receipt_sha256s": []},
    }
    assert raw_response not in json.dumps(receipt, ensure_ascii=False)


def test_private_after_image_emits_only_canonical_source_fact_receipt_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, runtime, authority, _paths = _fixture(tmp_path)
    raw_response = "completion=do-not-disclose-token-abc123"

    def provider_backed_stage(record: dict[str, object], **kwargs: object) -> dict[str, object]:
        # This proves that a callback response is never the diagnostic value:
        # only the materialized three-way source-fact mirror is observed.
        assert kwargs["source_fact_llm_call"]("private provider prompt") == raw_response
        return _fake_stage(record, **kwargs)

    monkeypatch.setattr(
        closure_modes,
        "_stage_manifest",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("force diagnostic")),
    )
    result = closure_modes.run(
        closure_modes.Mode.FULL_DRY_RUN,
        repo_root=repo,
        runtime_root=runtime,
        authority=authority,
        stage_publish=provider_backed_stage,
        source_fact_llm_call=lambda _prompt: raw_response,
        _test_deployed_seal={
            "deployed_commit": "b" * 40,
            "authority_file_sha256": "sha256:" + "c" * 64,
            "deployed_manifest_sha256": "sha256:" + "d" * 64,
            "deployed_manifest_file_sha256": "sha256:" + "e" * 64,
        },
    )
    receipt = json.loads(Path(str(result["diagnostic_receipt"])).read_text())
    hashes = receipt["provider_evidence"]["source_fact"]["receipt_sha256s"]
    assert result["status"] == "FULL_DRY_RUN_BLOCKED"
    assert receipt["provider_evidence"]["source_fact"]["attempt_status"] == "ATTEMPTED"
    assert len(hashes) == 1 and hashes[0].startswith("sha256:")
    assert raw_response not in json.dumps(receipt, ensure_ascii=False)


def test_private_after_image_rejects_mirrored_receipt_with_bad_self_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, runtime, authority, _paths = _fixture(tmp_path)

    def malformed_stage(record: dict[str, object], **kwargs: object) -> dict[str, object]:
        assert kwargs["source_fact_llm_call"]("provider prompt") == "provider completion"
        staged = _fake_stage(record, **kwargs)
        receipt = staged["publish_staging"]["source_fact_review"]
        assert isinstance(receipt, dict)
        receipt["receipt_sha256"] = "sha256:" + "0" * 64
        staged["story_contract"]["source_fact_review"] = receipt
        publish_path = Path(str(kwargs["private_publish_json_path"]))
        publish = json.loads(publish_path.read_text(encoding="utf-8"))
        publish["source_fact_review"] = receipt
        publish_path.write_bytes(_json(publish))
        return staged

    monkeypatch.setattr(
        closure_modes,
        "_stage_manifest",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("force diagnostic")),
    )
    result = closure_modes.run(
        closure_modes.Mode.FULL_DRY_RUN,
        repo_root=repo,
        runtime_root=runtime,
        authority=authority,
        stage_publish=malformed_stage,
        source_fact_llm_call=lambda _prompt: "provider completion",
        _test_deployed_seal={
            "deployed_commit": "b" * 40,
            "authority_file_sha256": "sha256:" + "c" * 64,
            "deployed_manifest_sha256": "sha256:" + "d" * 64,
            "deployed_manifest_file_sha256": "sha256:" + "e" * 64,
        },
    )
    receipt = json.loads(Path(str(result["diagnostic_receipt"])).read_text())
    assert result["status"] == "FULL_DRY_RUN_BLOCKED"
    assert receipt["provider_evidence"]["source_fact"] == {
        "attempt_status": "ATTEMPTED", "receipt_sha256s": []
    }


def test_qixi_prepare_is_private_and_provider_work_does_not_hold_runner_commit_lock(
    tmp_path: Path,
) -> None:
    repo, runtime, authority, paths = _fixture(tmp_path)
    before = {name: path.read_bytes() for name, path in paths.items()}

    def stage_while_runner_is_available(record: dict[str, object], **kwargs: object) -> dict[str, object]:
        # If prepare still held runner.lock over the provider/stage call, this
        # short competing lease would reject instead of entering.
        with exclusive_runner_commit(runtime):
            return _fake_stage(record, **kwargs)

    prepared = closure.prepare_qixi_after_image(
        repo_root=repo,
        runtime_root=runtime,
        authority=authority,
        _stage_publish=stage_while_runner_is_available,
    )
    assert prepared.root.joinpath("prepared.json").is_file()
    assert not closure._journal_root_from_authority(authority).exists()
    assert all(path.read_bytes() == before[name] for name, path in paths.items())


def test_qixi_commit_rejects_runtime_drift_before_formal_journal_or_target_write(
    tmp_path: Path,
) -> None:
    repo, runtime, authority, paths = _fixture(tmp_path)
    prepared = closure.prepare_qixi_after_image(
        repo_root=repo, runtime_root=runtime, authority=authority, _stage_publish=_fake_stage,
    )
    before_record = paths["record"].read_bytes()
    paths["state"].write_bytes(b'{"picks": []}\n')
    with pytest.raises(closure.QixiPostCorrectionPublicSurfaceError, match="candidate state preimage drifts"):
        closure.commit_qixi_after_image(
            prepared, repo_root=repo, runtime_root=runtime, authority=authority,
        )
    assert paths["record"].read_bytes() == before_record
    assert not closure._journal_root_from_authority(authority).exists()
    assert prepared.root.joinpath("prepared.json").is_file()


def test_qixi_identical_prepared_writer_accepts_byte_identical_existing_store(
    tmp_path: Path,
) -> None:
    repo, runtime, authority, _paths = _fixture(tmp_path)
    prepared = closure.prepare_qixi_after_image(
        repo_root=repo, runtime_root=runtime, authority=authority, _stage_publish=_fake_stage,
    )
    inputs = closure.validate_runtime(authority, repo_root=repo, runtime_root=runtime)
    targets, metadata = closure._read_prepared_after_image(prepared, authority=authority, inputs=inputs)
    retry = closure._write_prepared_after_image(inputs=inputs, targets=targets, metadata=metadata)
    assert retry == prepared
    assert {entry.name for entry in prepared.root.iterdir()} == {"prepared.json"}


def test_qixi_resume_valid_private_prepared_store_commits_without_provider(tmp_path: Path) -> None:
    repo, runtime, authority, _paths = _fixture(tmp_path)
    prepared = closure.prepare_qixi_after_image(
        repo_root=repo, runtime_root=runtime, authority=authority, _stage_publish=_fake_stage,
    )
    calls = 0

    def provider_must_not_run(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        raise AssertionError("prepared-store resume called provider")

    result = closure._finalize_legacy(
        apply=True,
        repo_root=repo,
        runtime_root=runtime,
        authority=authority,
        source_fact_llm_call=provider_must_not_run,
        _stage_publish=_fake_stage,
    )
    assert result["status"] == "APPLIED"
    assert calls == 0
    assert prepared.root.joinpath("prepared.json").is_file()


def test_qixi_resume_rejects_ambiguous_private_prepared_stores_without_provider(
    tmp_path: Path,
) -> None:
    repo, runtime, authority, _paths = _fixture(tmp_path)
    prepared = closure.prepare_qixi_after_image(
        repo_root=repo, runtime_root=runtime, authority=authority, _stage_publish=_fake_stage,
    )
    inputs = closure.validate_runtime(authority, repo_root=repo, runtime_root=runtime)
    targets, metadata = closure._read_prepared_after_image(prepared, authority=authority, inputs=inputs)
    second = closure._write_prepared_after_image(
        inputs=inputs, targets=targets, metadata={**metadata, "retry_marker": "second"}
    )
    assert second != prepared
    calls = 0

    def provider_must_not_run(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        raise AssertionError("ambiguous prepared store called provider")

    with pytest.raises(closure.QixiPostCorrectionPublicSurfaceError, match="recovery is ambiguous"):
        closure._finalize_legacy(
            apply=True,
            repo_root=repo,
            runtime_root=runtime,
            authority=authority,
            source_fact_llm_call=provider_must_not_run,
            _stage_publish=_fake_stage,
        )
    assert calls == 0


def test_qixi_resume_rejects_invalid_private_prepared_store_without_provider(tmp_path: Path) -> None:
    repo, runtime, authority, _paths = _fixture(tmp_path)
    inputs = closure.validate_runtime(authority, repo_root=repo, runtime_root=runtime)
    root = closure._prepared_parent(inputs) / ("0" * 64)
    root.parent.mkdir(parents=True, mode=0o700)
    os.chmod(root.parent, 0o700)
    root.mkdir(mode=0o700)
    root.joinpath("prepared.json").write_bytes(b"{}\n")
    os.chmod(root / "prepared.json", 0o600)
    calls = 0

    def provider_must_not_run(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        raise AssertionError("invalid prepared store called provider")

    with pytest.raises(closure.QixiPostCorrectionPublicSurfaceError, match="prepared Qixi after-image"):
        closure._finalize_legacy(
            apply=True,
            repo_root=repo,
            runtime_root=runtime,
            authority=authority,
            source_fact_llm_call=provider_must_not_run,
            _stage_publish=_fake_stage,
        )
    assert calls == 0
    assert not closure._journal_root_from_authority(authority).exists()


def test_qixi_commit_rejects_tampered_private_prepared_store_without_target_write(
    tmp_path: Path,
) -> None:
    repo, runtime, authority, paths = _fixture(tmp_path)
    prepared = closure.prepare_qixi_after_image(
        repo_root=repo, runtime_root=runtime, authority=authority, _stage_publish=_fake_stage,
    )
    before = paths["record"].read_bytes()
    prepared.root.joinpath("prepared.json").write_text("{}\n", encoding="utf-8")
    with pytest.raises(closure.QixiPostCorrectionPublicSurfaceError, match="prepared Qixi after-image"):
        closure.commit_qixi_after_image(
            prepared, repo_root=repo, runtime_root=runtime, authority=authority,
        )
    assert paths["record"].read_bytes() == before
    assert not closure._journal_root_from_authority(authority).exists()


def test_qixi_commit_rejects_rehashed_runtime_preimage_tamper_before_journal(
    tmp_path: Path,
) -> None:
    repo, runtime, authority, paths = _fixture(tmp_path)
    prepared = closure.prepare_qixi_after_image(
        repo_root=repo, runtime_root=runtime, authority=authority, _stage_publish=_fake_stage,
    )
    before = paths["record"].read_bytes()
    document = json.loads(prepared.root.joinpath("prepared.json").read_text())
    document.pop("prepared_sha256")
    assert isinstance(document["runtime_preimages"], dict)
    assert isinstance(document["runtime_preimages"]["state"], dict)
    document["runtime_preimages"]["state"]["sha256"] = "sha256:" + "f" * 64
    replacement_digest = closure._canonical_sha256(document)
    document["prepared_sha256"] = replacement_digest
    replacement_root = prepared.root.parent / replacement_digest[7:]
    prepared.root.rename(replacement_root)
    replacement_root.joinpath("prepared.json").write_bytes(_json(document))
    replacement = closure.PreparedQixiAfterImage(
        root=replacement_root, prepared_sha256=replacement_digest
    )
    with pytest.raises(closure.QixiPostCorrectionPublicSurfaceError, match="prepared Qixi after-image binding drifts"):
        closure.commit_qixi_after_image(
            replacement, repo_root=repo, runtime_root=runtime, authority=authority,
        )
    assert paths["record"].read_bytes() == before
    assert not closure._journal_root_from_authority(authority).exists()


def test_qixi_commit_rejects_symlinked_private_prepared_store(tmp_path: Path) -> None:
    repo, runtime, authority, paths = _fixture(tmp_path)
    prepared = closure.prepare_qixi_after_image(
        repo_root=repo, runtime_root=runtime, authority=authority, _stage_publish=_fake_stage,
    )
    before = paths["record"].read_bytes()
    replacement = tmp_path / "replacement.json"
    replacement.write_text("{}\n", encoding="utf-8")
    prepared.root.joinpath("prepared.json").unlink()
    prepared.root.joinpath("prepared.json").symlink_to(replacement)
    with pytest.raises(closure.QixiPostCorrectionPublicSurfaceError, match="prepared Qixi after-image"):
        closure.commit_qixi_after_image(
            prepared, repo_root=repo, runtime_root=runtime, authority=authority,
        )
    assert paths["record"].read_bytes() == before
    assert not closure._journal_root_from_authority(authority).exists()


def test_qixi_commit_busy_lock_does_not_consume_prepared_store(tmp_path: Path) -> None:
    repo, runtime, authority, _paths = _fixture(tmp_path)
    prepared = closure.prepare_qixi_after_image(
        repo_root=repo, runtime_root=runtime, authority=authority, _stage_publish=_fake_stage,
    )
    lock_fd = os.open(runtime / "runner.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(lock_fd, fcntl.LOCK_EX)
    try:
        with pytest.raises(closure.QixiPostCorrectionPublicSurfaceError, match="runner.lock is busy"):
            closure.commit_qixi_after_image(
                prepared, repo_root=repo, runtime_root=runtime, authority=authority,
            )
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
    assert prepared.root.joinpath("prepared.json").is_file()


def test_qixi_commit_reloads_deployed_authority_inside_lease_and_rejects_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, runtime, authority, paths = _fixture(tmp_path)
    prepared = closure.prepare_qixi_after_image(
        repo_root=repo, runtime_root=runtime, authority=authority, _stage_publish=_fake_stage,
    )
    before_record = paths["record"].read_bytes()
    paths["state"].write_bytes(_json({"picks": [{"candidate_id": closure.CANDIDATE_ID, "title": "new deploy"}]}))
    deployed = copy.deepcopy(authority)
    state_descriptor = _descriptor(paths["state"])
    state_descriptor["mode"] = paths["state"].stat().st_mode & 0o777
    deployed["state_file"] = state_descriptor
    deployed["sealed_before"]["state"] = dict(state_descriptor)
    deployed["authority_sha256"] = closure._canonical_sha256(
        {key: value for key, value in deployed.items() if key != "authority_sha256"}
    )
    assert closure.validate_authority(deployed)["authority_sha256"] != authority["authority_sha256"]

    held = False
    original_lease = closure._exclusive_runner_lock

    @contextmanager
    def tracked_lease(root: Path):
        nonlocal held
        with original_lease(root):
            held = True
            try:
                yield
            finally:
                held = False

    def changed_deployed_authority(_repo: Path) -> dict[str, object]:
        assert held, "deployed authority must be reloaded under runner lease"
        return deployed

    monkeypatch.setattr(closure, "_exclusive_runner_lock", tracked_lease)
    monkeypatch.setattr(closure, "load_deployed_authority", changed_deployed_authority)
    with pytest.raises(closure.QixiPostCorrectionPublicSurfaceError, match="deployed authority drifted"):
        closure.commit_qixi_after_image(
            prepared,
            repo_root=repo,
            runtime_root=runtime,
            authority=authority,
            reload_deployed_authority=True,
        )
    assert paths["record"].read_bytes() == before_record
    assert not closure._journal_root_from_authority(authority).exists()


def test_qixi_existing_formal_journal_resumes_without_provider(tmp_path: Path) -> None:
    repo, runtime, authority, _paths = _fixture(tmp_path)
    prepared = closure.prepare_qixi_after_image(
        repo_root=repo, runtime_root=runtime, authority=authority, _stage_publish=_fake_stage,
    )
    inputs = closure.validate_runtime(authority, repo_root=repo, runtime_root=runtime)
    targets, metadata = closure._read_prepared_after_image(prepared, authority=authority, inputs=inputs)
    root = closure._journal_root(inputs)
    closure._write_prepared_journal(root, inputs=inputs, targets=targets, metadata=metadata)
    calls = 0

    def provider_must_not_run(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        raise AssertionError("existing journal resume called provider")

    result = closure._finalize_legacy(
        apply=True,
        repo_root=repo,
        runtime_root=runtime,
        authority=authority,
        source_fact_llm_call=provider_must_not_run,
        _stage_publish=_fake_stage,
    )
    assert result["status"] == "APPLIED"
    assert calls == 0


def test_qixi_existing_journal_reloads_deployed_authority_under_lease_before_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, runtime, authority, paths = _fixture(tmp_path)
    prepared = closure.prepare_qixi_after_image(
        repo_root=repo, runtime_root=runtime, authority=authority, _stage_publish=_fake_stage,
    )
    inputs = closure.validate_runtime(authority, repo_root=repo, runtime_root=runtime)
    targets, metadata = closure._read_prepared_after_image(prepared, authority=authority, inputs=inputs)
    root = closure._journal_root(inputs)
    closure._write_prepared_journal(root, inputs=inputs, targets=targets, metadata=metadata)
    before_record = paths["record"].read_bytes()
    paths["state"].write_bytes(_json({"picks": [{"candidate_id": closure.CANDIDATE_ID, "title": "new deploy"}]}))
    deployed = copy.deepcopy(authority)
    state_descriptor = _descriptor(paths["state"])
    state_descriptor["mode"] = paths["state"].stat().st_mode & 0o777
    deployed["state_file"] = state_descriptor
    deployed["sealed_before"]["state"] = dict(state_descriptor)
    deployed["authority_sha256"] = closure._canonical_sha256(
        {key: value for key, value in deployed.items() if key != "authority_sha256"}
    )
    held = False
    original_lease = closure._exclusive_runner_lock

    @contextmanager
    def tracked_lease(runtime_root: Path):
        nonlocal held
        with original_lease(runtime_root):
            held = True
            try:
                yield
            finally:
                held = False

    def changed_deployed_authority(_repo: Path) -> dict[str, object]:
        assert held, "resume must reload deployed authority under runner lease"
        return deployed

    calls = 0

    def provider_must_not_run(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        raise AssertionError("journal resume called provider")

    monkeypatch.setattr(closure, "_exclusive_runner_lock", tracked_lease)
    monkeypatch.setattr(closure, "load_deployed_authority", changed_deployed_authority)
    with pytest.raises(closure.QixiPostCorrectionPublicSurfaceError, match="deployed authority drifted"):
        closure._finalize_legacy(
            apply=True,
            repo_root=repo,
            runtime_root=runtime,
            authority=authority,
            source_fact_llm_call=provider_must_not_run,
            _reload_deployed_authority=True,
        )
    assert calls == 0
    assert paths["record"].read_bytes() == before_record
    assert json.loads(root.joinpath("journal.json").read_text())["status"] == "PREPARED"


def test_private_stage_manifest_hashes_untrusted_names_without_echoing_them(tmp_path: Path) -> None:
    stage = tmp_path / "stage"
    stage.mkdir()
    secret_name = "prompt=do-not-disclose-token-abc123.txt"
    (stage / secret_name).write_bytes(b"private stage content")
    manifest = closure_modes._partial_stage_manifest(closure, stage)
    encoded = json.dumps(manifest, ensure_ascii=False)
    assert secret_name not in encoded
    assert "private stage content" not in encoded
    assert manifest == [
        {
            "relative_role": "private_stage_artifact:0",
            "relative_path_sha256": closure_modes.sha256_bytes(secret_name.encode("utf-8")),
            "sha256": closure._file_sha256_from_bytes(b"private stage content"),
            "bytes": len(b"private stage content"),
        }
    ]


def test_partial_stage_manifest_rejects_symlink_or_snapshot_race_without_leaking_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stage = tmp_path / "stage"
    stage.mkdir()
    secret_name = "token-prompt-private.txt"
    source = stage / secret_name
    source.write_bytes(b"private bytes")
    source.unlink()
    source.symlink_to(stage / "missing")
    with pytest.raises(RuntimeError, match="symlink"):
        closure_modes._partial_stage_manifest(closure, stage)
    source.unlink()
    source.write_bytes(b"private bytes")
    original_snapshot = closure_modes._stable_regular_snapshot

    def replace_after_listing(path: Path) -> bytes:
        source.unlink()
        source.symlink_to(stage / "missing")
        return original_snapshot(path)

    monkeypatch.setattr(closure_modes, "_stable_regular_snapshot", replace_after_listing)
    with pytest.raises(RuntimeError):
        closure_modes._partial_stage_manifest(closure, stage)


def test_safe_before_rejects_same_inode_mutation_during_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "target.json"
    target.write_bytes(b"before")
    original_read = closure_modes.os.read
    mutated = False

    def mutate_after_read(fd: int, amount: int) -> bytes:
        nonlocal mutated
        payload = original_read(fd, amount)
        if payload and not mutated:
            mutated = True
            target.write_bytes(b"after!")
        return payload

    monkeypatch.setattr(closure_modes.os, "read", mutate_after_read)
    with pytest.raises(RuntimeError, match="drifted"):
        closure_modes._safe_before({target: b"after-image"})


def test_apply_prejournal_after_image_failure_writes_complete_diagnostic_matrix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, runtime, authority, paths = _fixture(tmp_path)
    before = {name: path.read_bytes() for name, path in paths.items()}
    original_build = closure._build_after_image

    def multi_fail_build(*args: object, **kwargs: object) -> tuple[dict[Path, bytes], dict[str, object]]:
        targets, metadata = original_build(*args, **kwargs)
        targets = dict(targets)
        targets[paths["delivery"]] = b"{}"
        fixed = {paths["record"], paths["delivery"], paths["publish"], paths["state"]}
        cover_target = next(path for path in targets if path not in fixed)
        targets[cover_target] = b"forged-cover"
        return targets, metadata

    monkeypatch.setattr(closure, "_build_after_image", multi_fail_build)
    with pytest.raises(closure.QixiPostCorrectionPublicSurfaceError, match="record/delivery"):
        closure_modes.run(
            closure_modes.Mode.APPLY,
            repo_root=repo,
            runtime_root=runtime,
            authority=authority,
            stage_publish=_fake_stage,
            source_fact_llm_call=lambda _prompt: pytest.fail("provider must not run"),
            _test_deployed_seal={
                "deployed_commit": "b" * 40,
                "authority_file_sha256": "sha256:" + "c" * 64,
                "deployed_manifest_sha256": "sha256:" + "d" * 64,
                "deployed_manifest_file_sha256": "sha256:" + "e" * 64,
            },
        )
    root = closure_modes.diagnostic_root(
        candidate_root=paths["record"].parent.parent,
        authority_sha256=authority["authority_sha256"],
    )
    receipt = next(root.glob("diagnostic-*.json"))
    matrix = json.loads(receipt.read_text())["matrix"]
    by_name = {row["name"]: row for row in matrix["predicates"]}
    assert [row["name"] for row in matrix["predicates"]] == list(
        closure_modes.MATRIX_PREDICATE_IDS
    )
    assert by_name["record_delivery_media_closure"]["status"] == "FAIL"
    assert by_name["cover_materialized_hashes"]["status"] == "FAIL"
    assert by_name["formal_prepare_preimages"]["status"] == "PASS"
    assert by_name["formal_after_image"]["status"] == "FAIL"
    assert all(path.read_bytes() == before[name] for name, path in paths.items())
    assert not list(paths["record"].parent.glob("qixi_post_correction_public_surface/*/journal.json"))


def test_apply_persists_no_diagnostic_if_callback_fails_and_preserves_gate_error(
    tmp_path: Path,
) -> None:
    repo, runtime, authority, paths = _fixture(tmp_path)

    def stage_fails(*_args: object, **_kwargs: object) -> None:
        return None

    def receipt_write_fails(*_args: object) -> None:
        raise OSError("diagnostic sink unavailable")

    with pytest.raises(
        closure.QixiPostCorrectionPublicSurfaceError,
        match="staged publish is unavailable",
    ):
        closure._finalize_legacy(
            apply=True,
            repo_root=repo,
            runtime_root=runtime,
            authority=authority,
            _stage_publish=stage_fails,
            _prejournal_failure=receipt_write_fails,
        )
    assert not list(paths["record"].parent.glob("qixi_post_correction_public_surface/*/journal.json"))


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_apply_build_failure_reports_cleanup_without_masking_the_gate_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cleanup_fails: bool
) -> None:
    repo, runtime, authority, paths = _fixture(tmp_path)
    before = {name: path.read_bytes() for name, path in paths.items()}
    if cleanup_fails:
        monkeypatch.setattr(
            closure,
            "_remove_private_stage",
            lambda _stage: (_ for _ in ()).throw(OSError("cleanup unavailable")),
        )
    with pytest.raises(closure.QixiPostCorrectionPublicSurfaceError, match="staged publish is unavailable"):
        closure_modes.run(
            closure_modes.Mode.APPLY,
            repo_root=repo,
            runtime_root=runtime,
            authority=authority,
            stage_publish=lambda *_args, **_kwargs: None,
            _test_deployed_seal={
                "deployed_commit": "b" * 40,
                "authority_file_sha256": "sha256:" + "c" * 64,
                "deployed_manifest_sha256": "sha256:" + "d" * 64,
                "deployed_manifest_file_sha256": "sha256:" + "e" * 64,
            },
        )
    root = closure_modes.diagnostic_root(
        candidate_root=paths["record"].parent.parent,
        authority_sha256=authority["authority_sha256"],
    )
    receipts = list(root.glob("diagnostic-*.json"))
    assert len(receipts) == 1
    receipt = json.loads(receipts[0].read_text())
    matrix = {row["name"]: row for row in receipt["matrix"]["predicates"]}
    assert matrix["stage_build"]["status"] == "FAIL"
    assert matrix["stage_cleanup"] == {
        "name": "stage_cleanup",
        "status": "FAIL" if cleanup_fails else "PASS",
        "reason": "UNEXPECTED_EXCEPTION" if cleanup_fails else "SATISFIED",
    }
    assert all(path.read_bytes() == before[name] for name, path in paths.items())
    assert not list(paths["record"].parent.glob("qixi_post_correction_public_surface/*/journal.json"))
    assert bool(list(paths["record"].parent.glob(".qixi-public-surface-stage-*"))) is cleanup_fails
    if not cleanup_fails:
        # The stage is already gone, but the receipt retains only anonymous
        # role/hash/byte evidence captured before cleanup.
        assert receipt["stage_manifest_status"] == "AVAILABLE"
        assert receipt["stage_manifest"]
        encoded = json.dumps(receipt["stage_manifest"], ensure_ascii=False)
        assert "qixi-public-surface-stage" not in encoded
        assert "prompt" not in encoded
        assert all(set(row) == {"relative_role", "relative_path_sha256", "sha256", "bytes"} for row in receipt["stage_manifest"])


def test_apply_stage_cleanup_failure_blocks_prepared_handle_and_formal_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, runtime, authority, paths = _fixture(tmp_path)
    before = {name: path.read_bytes() for name, path in paths.items()}
    monkeypatch.setattr(
        closure,
        "_remove_private_stage",
        lambda _stage: (_ for _ in ()).throw(OSError("cleanup unavailable")),
    )
    with pytest.raises(closure.QixiPostCorrectionPublicSurfaceError, match="private stage cleanup failed"):
        closure._finalize_legacy(
            apply=True,
            repo_root=repo,
            runtime_root=runtime,
            authority=authority,
            _stage_publish=_fake_stage,
        )
    root = closure._journal_root_from_authority(authority)
    assert not root.exists()
    assert not root.parent.joinpath("prepared").exists()
    assert all(path.read_bytes() == before[name] for name, path in paths.items())


def test_diagnostic_deployed_seal_keeps_internal_and_raw_authority_hashes_distinct(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "deployed"
    asset = repo / closure.RELATIVE_AUTHORITY_PATH
    asset.parent.mkdir(parents=True)
    asset.write_bytes(b'{"authority_sha256":"sha256:' + b"1" * 64 + b'"}\n')
    (repo / "DEPLOYED_COMMIT").write_text("a" * 40 + "\n")
    manifest = build_deployed_authority_manifest(
        repo_root=repo,
        deployed_commit="a" * 40,
        relative_paths=[closure.RELATIVE_AUTHORITY_PATH],
    )
    (repo / "DEPLOYED_AUTHORITY_MANIFEST.json").write_text(
        json.dumps(manifest, sort_keys=True)
    )
    seal = closure_modes._deployed_seal(closure, repo_root=repo, test_seal=None)
    assert seal["deployed_commit"] == "a" * 40
    assert seal["authority_file_sha256"] != "sha256:" + "1" * 64
    assert seal["deployed_manifest_sha256"] == manifest["manifest_sha256"]


def test_diagnostic_manifest_snapshot_rejects_same_inode_same_length_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = tmp_path / "DEPLOYED_AUTHORITY_MANIFEST.json"
    manifest.write_bytes(b"a" * 128)
    original_read = closure_modes.os.read
    mutated = False

    def mutate_after_read(fd: int, size: int) -> bytes:
        nonlocal mutated
        value = original_read(fd, size)
        if value and not mutated:
            mutated = True
            manifest.write_bytes(b"b" * 128)
        return value

    monkeypatch.setattr(closure_modes.os, "read", mutate_after_read)
    with pytest.raises(RuntimeError, match="drifted during snapshot"):
        closure_modes._stable_regular_snapshot(manifest)


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


def test_apply_rebinds_validated_cover_generation_to_terminal_story_contract(
    tmp_path: Path,
) -> None:
    """Terminal projection changes only the compact cover StoryContract view."""

    repo, runtime, authority, paths = _fixture(tmp_path)
    result = closure.finalize(
        apply=True,
        repo_root=repo,
        runtime_root=runtime,
        authority=authority,
        _stage_publish=_fake_stage,
    )
    assert result["status"] == "APPLIED"
    record = json.loads(paths["record"].read_text())
    publish = json.loads(paths["publish"].read_text())
    expected = cover_story_contract_binding(record["story_contract"])
    assert record["publish_staging"]["cover_generation"]["story_contract"] == expected
    assert publish["cover_generation"]["story_contract"] == expected


def test_cover_story_contract_reprojection_never_excuses_source_hash_drift(
    tmp_path: Path,
) -> None:
    repo, runtime, authority, paths = _fixture(tmp_path)
    before = {
        key: paths[key].read_bytes() for key in ("record", "delivery", "publish", "state")
    }

    def source_hash_drift(record: dict[str, object], **kwargs: object) -> dict[str, object]:
        staged = _fake_stage(record, **kwargs)
        generation = staged["publish_staging"]["cover_generation"]
        assert isinstance(generation, dict)
        generation["final_cover_sha256"] = "sha256:" + "f" * 64
        return staged

    with pytest.raises(closure.QixiPostCorrectionPublicSurfaceError):
        closure.finalize(
            apply=True,
            repo_root=repo,
            runtime_root=runtime,
            authority=authority,
            _stage_publish=source_hash_drift,
        )
    assert {key: paths[key].read_bytes() for key in before} == before


def test_real_publish_stage_replays_manual_title_public_fields(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, runtime, authority, paths = _fixture(tmp_path)
    provider_calls: list[str] = []

    def canonical_cover(
        _record: object, *, private_artifact_root: Path, **_kwargs: object
    ) -> dict[str, object]:
        cover = private_artifact_root / "covers" / "canonical.cover.png"
        background = private_artifact_root / "covers_ai_original" / "canonical.background.png"
        cover.parent.mkdir(parents=True, exist_ok=True)
        background.parent.mkdir(parents=True, exist_ok=True)
        cover.write_bytes(b"canonical-cover")
        background.write_bytes(b"canonical-background")
        generation = {
            "final_cover": str(cover),
            "final_cover_sha256": _sha(cover.read_bytes()),
            "ai_background": str(background),
            "ai_background_sha256": _sha(background.read_bytes()),
            "cover_text_mode": "punch",
            "cover_text": "女友感",
            # Cover builders legitimately retain tuple receipts in memory;
            # the staged publish file is the canonical JSON-domain form.
            "rendered_lines": ("女友感",),
            "cover_punch": ("女友感",),
            "art_direction": {"cover_punch_semantic_review": {}},
        }
        return {
            "status": "AI_COVER_READY",
            "cover_path": str(cover),
            "cover_sha256": generation["final_cover_sha256"],
            "ai_background_sha256": generation["ai_background_sha256"],
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
    assert staging["cover_generation"] == publish["cover_generation"]
    assert staging["cover_generation"]["rendered_lines"] == ["女友感"]
    assert staging["cover_generation"]["cover_punch"] == ["女友感"]


@pytest.mark.parametrize("drift", ("locator", "hash", "route", "semantic"))
def test_projection_rejects_returned_cover_generation_drift_before_targets(
    tmp_path: Path, drift: str
) -> None:
    repo, runtime, authority, paths = _fixture(tmp_path)

    def raw_generation_drift(record: dict[str, object], **kwargs: object) -> dict[str, object]:
        staged = _fake_stage(record, **kwargs)
        generation = staged["publish_staging"]["cover_generation"]
        assert isinstance(generation, dict)
        if drift == "locator":
            generation["final_cover"] = "/unsealed/foreign-cover.png"
        elif drift == "hash":
            generation["final_cover_sha256"] = "sha256:" + "f" * 64
        elif drift == "route":
            generation["route_decision"] = {"status": "DRIFTED"}
        else:
            generation["cover_text"] = "fabricated semantic claim"
        return staged

    before = {
        key: paths[key].read_bytes() for key in ("record", "delivery", "publish", "state")
    }
    with pytest.raises(
        closure.QixiPostCorrectionPublicSurfaceError,
        match="canonical publish and returned public surfaces drift before projection",
    ):
        closure.finalize(
            apply=True,
            repo_root=repo,
            runtime_root=runtime,
            authority=authority,
            _stage_publish=raw_generation_drift,
        )
    assert {key: paths[key].read_bytes() for key in before} == before


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


def _commit_legacy_hidden_public_surface(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, dict[str, object], dict[str, Path]]:
    repo, runtime, authority, paths = _fixture(tmp_path)
    monkeypatch.setattr(
        closure,
        "_public_artifact_root",
        projection_paths.legacy_public_artifact_root,
    )
    assert closure.finalize(
        apply=True,
        repo_root=repo,
        runtime_root=runtime,
        authority=authority,
        _stage_publish=_fake_stage,
    )["status"] == "APPLIED"
    assert Path(json.loads(paths["record"].read_text())["publish_staging"]["cover_generation"]["final_cover"]).name.startswith(".")
    return repo, runtime, authority, paths


def test_fresh_public_artifact_basename_is_daily_manifest_safe(tmp_path: Path) -> None:
    _repo, _runtime, authority, paths = _fixture(tmp_path)
    namespace = projection_paths.public_artifact_root(paths["record"].parent, authority)
    projected = projection_paths.public_artifact_path(namespace, "covers/example.cover.png")
    assert projected.name[0].isalnum()
    from scripts.build_lidousha_daily_review_manifest import _safe_component

    assert _safe_component(projected.name, label="final cover basename") == projected.name


def test_committed_hidden_public_artifacts_recover_to_safe_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _runtime, authority, paths = _commit_legacy_hidden_public_surface(tmp_path, monkeypatch)
    before_record = json.loads(paths["record"].read_text())
    before_srt, before_burn = before_record["artifact_hashes"]["subtitle_sha256"], before_record["artifact_hashes"]["burned_video_sha256"]
    result = basename_recovery.recover(apply=True, repo_root=repo, authority=authority)
    assert result["status"] == "RECOVERY_COMMITTED"
    record = json.loads(paths["record"].read_text())
    delivery = json.loads(paths["delivery"].read_text())
    state = json.loads(paths["state"].read_text())
    final_cover = Path(record["publish_staging"]["cover_generation"]["final_cover"])
    assert final_cover.name[0].isalnum() and final_cover.is_file()
    assert record == delivery
    assert record["artifact_hashes"]["subtitle_sha256"] == before_srt
    assert record["artifact_hashes"]["burned_video_sha256"] == before_burn
    pick = next(row for row in state["picks"] if row["candidate_id"] == closure.CANDIDATE_ID)
    assert pick["cover_path"] == str(final_cover)
    from scripts.build_lidousha_daily_review_manifest import _resolve_final_cover

    daily_package = paths["record"].parent / "daily-package"
    daily_package.mkdir()
    assert _resolve_final_cover(
        package_root=daily_package,
        pick=pick,
        cover_generation=record["publish_staging"]["cover_generation"],
    ).startswith("covers/qixi-public-surface-")
    assert basename_recovery.recover(apply=True, repo_root=repo, authority=authority)["status"] == "RECOVERY_ALREADY_COMMITTED"
    final_cover.write_bytes(b"drift")
    with pytest.raises(closure.QixiPostCorrectionPublicSurfaceError, match="installed target drifts"):
        basename_recovery.recover(apply=True, repo_root=repo, authority=authority)


def test_hidden_public_artifact_recovery_rejects_conflict_or_committed_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _runtime, authority, paths = _commit_legacy_hidden_public_surface(tmp_path, monkeypatch)
    legacy_record = json.loads(paths["record"].read_text())
    old_final = Path(legacy_record["publish_staging"]["cover_generation"]["final_cover"])
    relative = basename_recovery._decode_legacy_relative(
        old_final,
        legacy_namespace=projection_paths.legacy_public_artifact_root(paths["record"].parent, authority),
    )
    conflict = projection_paths.public_artifact_path(
        projection_paths.public_artifact_root(paths["record"].parent, authority), relative
    )
    conflict.write_bytes(b"foreign")
    with pytest.raises(closure.QixiPostCorrectionPublicSurfaceError, match="conflicts"):
        basename_recovery.recover(apply=True, repo_root=repo, authority=authority)
    conflict.unlink()
    paths["publish"].write_bytes(b"drift")
    with pytest.raises(closure.QixiPostCorrectionPublicSurfaceError, match="postcommit target bytes drift"):
        basename_recovery.recover(apply=True, repo_root=repo, authority=authority)


def test_hidden_public_artifact_recovery_requires_original_committed_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _runtime, authority, _paths = _commit_legacy_hidden_public_surface(tmp_path, monkeypatch)
    source_root = closure._journal_root_from_authority(authority)
    (source_root / "final-receipt.json").unlink()
    with pytest.raises(closure.QixiPostCorrectionPublicSurfaceError, match="final receipt"):
        basename_recovery.recover(apply=True, repo_root=repo, authority=authority)


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
