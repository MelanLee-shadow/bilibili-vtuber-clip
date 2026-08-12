import hashlib
import json
from pathlib import Path

import pytest

from scripts.build_lidousha_daily_review_manifest import (
    DailyManifestError,
    _atomic_project_bytes,
    _sha256,
    _resolve_final_burn_artifacts,
    _resolve_final_cover,
    _rebuild_package_speaker_evidence,
    _lane_manifest_contract_fields,
    _source_fact_manifest_fields,
    _sync_record_bound_candidate_artifacts,
    _sync_declared_artifact,
    _validate_source_fact_receipts,
    build,
)
from src.autoslice.source_fact_review import review_and_repair_source_facts
from src.autoslice.speaker_common import SPEAKER_FINALIZATION_SCHEMA
from src.autoslice.surface_canon import CHANNEL_PROFILE


def _keep_completion(hook: str, title: str) -> str:
    return json.dumps(
        {
            "schema_version": "lidousha-source-fact-review.v1",
            "status": "KEEP",
            "final_selection_hook": hook,
            "final_title": title,
            "supported_by": ["final_transcript", "structured_chat"],
            "changed_surfaces": [],
            # F12：判项必填；无说话人转写 -> UNVERIFIABLE 车道，留空数组。
            "addressee_attribution": [],
            "summary": "字幕与结构化弹幕共同支持现有派生事实。",
        },
        ensure_ascii=False,
    )


def test_sync_declared_artifact_replaces_stale_package_copy(
    tmp_path: Path,
) -> None:
    source = tmp_path / "candidate" / "chat.json"
    target = tmp_path / "candidate" / "package" / "chat.json"
    source.parent.mkdir()
    target.parent.mkdir()
    source.write_bytes(b"current authority\n")
    target.write_bytes(b"stale authority\n")

    _sync_declared_artifact(
        package_root=target.parent,
        target=target,
        source=source,
        declared_sha256="sha256:" + _sha256(source),
        label="chat authority",
    )

    assert target.read_bytes() == source.read_bytes()


def test_sync_declared_artifact_refuses_unbound_source_without_touching_target(
    tmp_path: Path,
) -> None:
    source = tmp_path / "candidate" / "chat.json"
    target = tmp_path / "candidate" / "package" / "chat.json"
    source.parent.mkdir()
    target.parent.mkdir()
    source.write_bytes(b"unexpected authority\n")
    target.write_bytes(b"previous package authority\n")

    with pytest.raises(DailyManifestError, match="chat authority sha drift"):
        _sync_declared_artifact(
            package_root=target.parent,
            target=target,
            source=source,
            declared_sha256="sha256:" + "0" * 64,
            label="chat authority",
        )

    assert target.read_bytes() == b"previous package authority\n"


def test_sync_declared_artifact_accepts_exact_package_when_parent_missing(
    tmp_path: Path,
) -> None:
    source = tmp_path / "candidate" / "chat.json"
    target = tmp_path / "candidate" / "package" / "chat.json"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"portable exact authority\n")

    _sync_declared_artifact(
        package_root=target.parent,
        target=target,
        source=source,
        declared_sha256="sha256:" + _sha256(target),
        label="chat authority",
    )

    assert target.read_bytes() == b"portable exact authority\n"


def test_sync_declared_artifact_rejects_package_symlink(
    tmp_path: Path,
) -> None:
    source = tmp_path / "candidate" / "chat.json"
    target = tmp_path / "candidate" / "package" / "chat.json"
    source.parent.mkdir()
    target.parent.mkdir()
    source.write_bytes(b"authority\n")
    target.symlink_to(source)

    with pytest.raises(DailyManifestError, match="package path contains a symlink"):
        _sync_declared_artifact(
            package_root=target.parent,
            target=target,
            source=source,
            declared_sha256="sha256:" + _sha256(source),
            label="chat authority",
        )


def test_resolve_final_cover_uses_exact_state_and_record_binding(
    tmp_path: Path,
) -> None:
    covers = tmp_path / "covers"
    covers.mkdir()
    stale = covers / "candidate.ai-title.cover.png"
    current = covers / "candidate.screenshot-title.cover.png"
    stale.write_bytes(b"stale ai route\n")
    current.write_bytes(b"current screenshot route\n")
    current_sha = _sha256(current)

    resolved = _resolve_final_cover(
        package_root=tmp_path,
        pick={
            "cover_path": str(current),
            "cover_sha256": "sha256:" + current_sha,
        },
        cover_generation={
            "final_cover": str(current),
            "final_cover_sha256": "sha256:" + current_sha,
        },
    )

    assert resolved == "covers/candidate.screenshot-title.cover.png"


def test_resolve_final_cover_refuses_state_record_route_drift(
    tmp_path: Path,
) -> None:
    covers = tmp_path / "covers"
    covers.mkdir()
    ai = covers / "candidate.ai-title.cover.png"
    screenshot = covers / "candidate.screenshot-title.cover.png"
    ai.write_bytes(b"ai route\n")
    screenshot.write_bytes(b"screenshot route\n")

    with pytest.raises(
        DailyManifestError,
        match="state/record final cover binding drift",
    ):
        _resolve_final_cover(
            package_root=tmp_path,
            pick={
                "cover_path": str(screenshot),
                "cover_sha256": "sha256:" + _sha256(screenshot),
            },
            cover_generation={
                "final_cover": str(ai),
                "final_cover_sha256": "sha256:" + _sha256(ai),
            },
        )


def test_resolve_final_cover_accepts_hash_identical_delivery_alias(
    tmp_path: Path,
) -> None:
    covers = tmp_path / "covers"
    delivery = tmp_path / "delivery"
    covers.mkdir()
    delivery.mkdir()
    packaged = covers / "final.cover.png"
    alias = delivery / "标题.cover.png"
    packaged.write_bytes(b"same repaired cover bytes\n")
    alias.write_bytes(packaged.read_bytes())
    expected = _sha256(packaged)

    resolved = _resolve_final_cover(
        package_root=tmp_path,
        pick={
            "cover_path": str(alias),
            "cover_sha256": "sha256:" + expected,
        },
        cover_generation={
            "final_cover": str(packaged),
            "final_cover_sha256": "sha256:" + expected,
        },
    )

    assert resolved == "covers/final.cover.png"


def test_resolve_final_cover_materializes_repaired_delivery_alias(
    tmp_path: Path,
) -> None:
    covers = tmp_path / "covers"
    delivery = tmp_path / "delivery"
    covers.mkdir()
    delivery.mkdir()
    alias = delivery / "标题.cover.png"
    alias.write_bytes(b"same-bv repaired cover bytes\n")
    expected = _sha256(alias)

    resolved = _resolve_final_cover(
        package_root=tmp_path,
        pick={
            "cover_path": str(alias),
            "cover_sha256": "sha256:" + expected,
        },
        cover_generation={
            "final_cover": str(tmp_path / "generation" / "final.cover.png"),
            "final_cover_sha256": "sha256:" + expected,
        },
    )

    assert resolved == "covers/final.cover.png"
    assert (covers / "final.cover.png").read_bytes() == alias.read_bytes()


def test_sync_record_bound_candidate_artifacts_makes_evidence_portable(
    tmp_path: Path,
) -> None:
    candidate_root = tmp_path / "candidate"
    package_root = candidate_root / "replacement_recuts"
    package_root.mkdir(parents=True)
    candidate_id = "auto_test"
    chat = candidate_root / f"{candidate_id}.chat-authority.json"
    context = candidate_root / f"{candidate_id}.clip-context.json"
    chat.write_bytes(b"current chat authority\n")
    context.write_bytes(b"current clip context\n")
    (package_root / chat.name).write_bytes(b"stale chat authority\n")

    resolved = _sync_record_bound_candidate_artifacts(
        package_root=package_root,
        candidate_id=candidate_id,
        record_doc={
            "artifact_hashes": {
                "chat_authority_audit_sha256": "sha256:" + _sha256(chat),
                "clip_context_file_sha256": "sha256:" + _sha256(context),
            }
        },
    )

    assert resolved == {
        "chat_authority": chat.name,
        "clip_context": context.name,
    }
    assert (package_root / chat.name).read_bytes() == chat.read_bytes()
    assert (package_root / context.name).read_bytes() == context.read_bytes()


def test_source_fact_receipt_must_match_all_package_surfaces(
    tmp_path: Path,
) -> None:
    hook = "弹幕提议把技能叫甲甲拉拉，主播随即拒绝。"
    title = CHANNEL_PROFILE.talk_title_prefix + "弹幕提议把技能叫“甲甲拉拉”，主播随即拒绝"
    transcript = "技能可以叫甲甲拉拉吗\n不行，我这个应该叫甲甲网"
    context = "- superchat: 技能可以叫甲甲拉拉吗"
    receipt = review_and_repair_source_facts(
        selection_hook=hook,
        title=title,
        final_transcript=transcript,
        clip_context_prompt=context,
        llm_call=lambda _prompt: _keep_completion(hook, title),
    )
    subtitle = tmp_path / "candidate.srt"
    subtitle.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n"
        "技能可以叫甲甲拉拉吗\n\n"
        "2\n00:00:01,000 --> 00:00:02,000\n"
        "不行，我这个应该叫甲甲网\n",
        encoding="utf-8",
    )
    record = {
        "speaker_mode": "uniform_host",
        "story_contract": {
            "selection_hook": hook,
            "clip_context_prompt": context,
            "source_fact_review": receipt,
        },
        "publish_staging": {
            "title": title,
            "source_fact_review": receipt,
        },
    }
    publish = {"title": title, "source_fact_review": receipt}

    speaker_evidence, speaker_manifest = _rebuild_package_speaker_evidence(
        package_root=tmp_path,
        record_doc=record,
        subtitle_path=subtitle,
        speaker_srt_path=None,
    )
    assert speaker_evidence["state"] == "AbsentAuthorized"
    assert speaker_manifest is None

    assert (
        _validate_source_fact_receipts(
            record_doc=record,
            publish_doc=publish,
            subtitle_path=subtitle,
            speaker_evidence=speaker_evidence,
        )
        == receipt["receipt_sha256"]
    )

    publish["source_fact_review"] = None
    with pytest.raises(DailyManifestError, match="receipt missing"):
        _validate_source_fact_receipts(
            record_doc=record,
            publish_doc=publish,
            subtitle_path=subtitle,
            speaker_evidence=speaker_evidence,
        )


def test_package_speaker_evidence_rejects_present_invalid_bytes(
    tmp_path: Path,
) -> None:
    subtitle = tmp_path / "candidate.srt"
    subtitle.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n测试字幕\n",
        encoding="utf-8",
    )
    speaker_srt = tmp_path / "candidate.speaker-final.srt"
    speaker_srt.write_text("not an SRT\n", encoding="utf-8")
    source_manifest = tmp_path / "source" / "candidate.speaker.json"
    source_manifest.parent.mkdir()
    source_manifest.write_text("{}\n", encoding="utf-8")
    record = {
        "speaker_mode": "required",
        "speaker_review_srt_path": str(tmp_path / "outside.speaker.srt"),
        "speaker_finalization_manifest_path": str(source_manifest),
        "speaker_finalization_manifest_sha256": ("sha256:" + _sha256(source_manifest)),
        "speaker_finalization": {},
        "artifact_hashes": {
            "speaker_review_srt_sha256": "sha256:" + _sha256(speaker_srt),
        },
    }

    with pytest.raises(
        DailyManifestError,
        match="source-fact speaker evidence rejected",
    ):
        _rebuild_package_speaker_evidence(
            package_root=tmp_path,
            record_doc=record,
            subtitle_path=subtitle,
            speaker_srt_path=speaker_srt,
        )


def test_package_speaker_evidence_rebuilds_exact_package_bytes(
    tmp_path: Path,
) -> None:
    package_root = tmp_path / "package"
    package_root.mkdir()
    subtitle = package_root / "candidate.srt"
    subtitle.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n测试字幕\n",
        encoding="utf-8",
    )
    speaker_srt = package_root / "candidate.speaker-final.srt"
    speaker_srt.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n[李豆沙] 测试字幕\n",
        encoding="utf-8",
    )
    plain_sha = _sha256(subtitle)
    speaker_sha = _sha256(speaker_srt)
    manifest = {
        "schema_version": SPEAKER_FINALIZATION_SCHEMA,
        "status": "READY",
        "production_ready": True,
        "text_final_srt_sha256": plain_sha,
        "output_review_srt_sha256": speaker_sha,
        "source_cue_count": 1,
        "output_cue_count": 1,
        "final_decisions": [
            {
                "source_index": 1,
                "start": "00:00:00,000",
                "end": "00:00:01,000",
                "speaker": "李豆沙",
                "text": "测试字幕",
                "decision_source": "ivan_reviewed_truth",
                "layer": 0,
                "placement": "main",
            }
        ],
    }
    source_manifest = tmp_path / "producer" / "candidate.speaker-final.json"
    source_manifest.parent.mkdir()
    source_manifest.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    record = {
        "speaker_mode": "required",
        "speaker_review_srt_path": "/producer/candidate.speaker-final.srt",
        "speaker_finalization_manifest_path": str(source_manifest),
        "speaker_finalization_manifest_sha256": ("sha256:" + _sha256(source_manifest)),
        "speaker_finalization": manifest,
        "artifact_hashes": {
            "subtitle_sha256": "sha256:" + plain_sha,
            "speaker_review_srt_sha256": "sha256:" + speaker_sha,
        },
    }

    evidence, packaged_manifest = _rebuild_package_speaker_evidence(
        package_root=package_root,
        record_doc=record,
        subtitle_path=subtitle,
        speaker_srt_path=speaker_srt,
    )

    assert evidence["state"] == "PresentValid"
    assert evidence["speaker_transcript"] == "1 [李豆沙] 测试字幕"
    assert packaged_manifest == package_root / source_manifest.name
    assert packaged_manifest.read_bytes() == source_manifest.read_bytes()


def test_song_manifest_does_not_require_talk_source_fact_receipt(
    tmp_path: Path,
) -> None:
    subtitle = tmp_path / "song.srt"
    subtitle.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n歌词\n",
        encoding="utf-8",
    )

    assert (
        _source_fact_manifest_fields(
            lane="song",
            record_doc={"classification": "song"},
            publish_doc={"title": "【主播·歌】《测试歌曲》"},
            subtitle_path=subtitle,
        )
        == {}
    )
    assert (
        _lane_manifest_contract_fields(
            lane="song",
            record_doc={"classification": "song"},
            publish_doc={"title": "【主播·歌】《测试歌曲》"},
            subtitle_path=subtitle,
        )
        == {}
    )


def test_resolve_final_burn_artifacts_uses_legacy_sapphire72_names_when_uniform(
    tmp_path: Path,
) -> None:
    stem = "auto_test.recut"
    (tmp_path / f"{stem}.burned-final-sapphire72.mp4").write_bytes(b"video")
    (tmp_path / f"{stem}.final-sapphire72.ass").write_bytes(b"ass")

    burned, ass, speaker_srt = _resolve_final_burn_artifacts(
        package_root=tmp_path, stem=stem, uniform_fallback=True
    )

    assert burned.name == f"{stem}.burned-final-sapphire72.mp4"
    assert ass.name == f"{stem}.final-sapphire72.ass"
    assert speaker_srt is None


def test_resolve_final_burn_artifacts_uses_speaker_names_when_not_uniform(
    tmp_path: Path,
) -> None:
    stem = "auto_test.recut"
    (tmp_path / f"{stem}.burned-final-speaker.mp4").write_bytes(b"video")
    (tmp_path / f"{stem}.speaker-final.ass").write_bytes(b"ass")
    (tmp_path / f"{stem}.speaker-final.srt").write_bytes(b"srt")

    burned, ass, speaker_srt = _resolve_final_burn_artifacts(
        package_root=tmp_path, stem=stem, uniform_fallback=False
    )

    assert burned.name == f"{stem}.burned-final-speaker.mp4"
    assert ass.name == f"{stem}.speaker-final.ass"
    assert speaker_srt is not None
    assert speaker_srt.name == f"{stem}.speaker-final.srt"


def test_resolve_final_burn_artifacts_refuses_missing_speaker_family(
    tmp_path: Path,
) -> None:
    stem = "auto_test.recut"
    # Only the legacy sapphire72 burn exists — the pre-8/7-flip shape a
    # speaker-finalized package must never silently accept.
    (tmp_path / f"{stem}.burned-final-sapphire72.mp4").write_bytes(b"video")

    with pytest.raises(DailyManifestError, match="required package file missing"):
        _resolve_final_burn_artifacts(package_root=tmp_path, stem=stem, uniform_fallback=False)


def _build_daily_talk_package(
    tmp_path: Path,
    *,
    speaker_finalized: bool,
) -> tuple[Path, Path, Path, str]:
    """Replicate the on-disk shape of a real daily talk package.

    Mirrors the free fixtures ``auto_210739_1142_1436`` /
    ``auto_220747_488_680`` (2026-08-08 provisional-upload blocked report):
    a talk candidate whose ``replacement_recuts/`` carries either the legacy
    uniform_host sapphire72 burn or the AUTOSLICE_SPEAKER_MODE=auto
    speaker-finalized burn family, never both.
    """

    candidate_id = "auto_test_1000_1100"
    day_root = tmp_path / "2026-08-08"
    package_root = day_root / "replacement_recuts"
    package_root.mkdir(parents=True)
    stem = f"{candidate_id}.recut"

    hook = "弹幕问起白板上的涂鸦，主播随口解释了来历。"
    title = CHANNEL_PROFILE.talk_title_prefix + "弹幕问起涂鸦来历"
    transcript_line = "这块涂鸦是上次乱画的"
    clip_context_prompt = "- superchat: 这个涂鸦哪来的"
    receipt = review_and_repair_source_facts(
        selection_hook=hook,
        title=title,
        final_transcript=transcript_line,
        clip_context_prompt=clip_context_prompt,
        llm_call=lambda _prompt: _keep_completion(hook, title),
    )

    subtitle = package_root / f"{stem}.srt"
    subtitle.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n" + transcript_line + "\n",
        encoding="utf-8",
    )

    chat_authority_source = day_root / f"{candidate_id}.chat-authority.json"
    clip_context_source = day_root / f"{candidate_id}.clip-context.json"
    clip_context_source.write_bytes(b"{}\n")
    if speaker_finalized:
        chat_authority_doc = {
            "final_text_srt_sha256": "1" * 64,
            "final_speaker_srt_sha256": "2" * 64,
            "speaker_ass_path": str(package_root / f"{stem}.speaker-final.ass"),
            "speaker_ass_sha256": "3" * 64,
        }
    else:
        text_sha256 = hashlib.sha256(subtitle.read_bytes()).hexdigest()
        chat_authority_doc = {
            "final_text_srt_sha256": text_sha256,
            "final_speaker_srt_sha256": text_sha256,
            "speaker_ass_path": None,
            "speaker_ass_sha256": None,
        }
    chat_authority_source.write_text(
        json.dumps(chat_authority_doc, ensure_ascii=False),
        encoding="utf-8",
    )

    if speaker_finalized:
        (package_root / f"{stem}.burned-final-speaker.mp4").write_bytes(b"speaker-burn-bytes")
        (package_root / f"{stem}.speaker-final.ass").write_bytes(b"[Events]\nspeaker-ass\n")
        (package_root / f"{stem}.speaker-final.srt").write_text(
            "1\n00:00:00,000 --> 00:00:01,000\n[LDS] " + transcript_line + "\n",
            encoding="utf-8",
        )
    else:
        (package_root / f"{stem}.burned-final-sapphire72.mp4").write_bytes(b"uniform-burn-bytes")
        (package_root / f"{stem}.final-sapphire72.ass").write_bytes(b"[Events]\nuniform-ass\n")

    covers_dir = package_root / "covers"
    covers_dir.mkdir()
    cover_path = covers_dir / "final.cover.png"
    cover_path.write_bytes(b"cover-bytes")

    generation_dir = tmp_path / "generation"
    generation_dir.mkdir(exist_ok=True)
    pre_overlay_source = generation_dir / "pre-overlay.png"
    pre_overlay_source.write_bytes(b"pre-overlay-bytes")
    background_source = generation_dir / "background.png"
    background_source.write_bytes(b"background-bytes")
    mask_source = generation_dir / "mask.png"
    mask_source.write_bytes(b"mask-bytes")

    cover_generation = {
        "final_cover": str(cover_path),
        "final_cover_sha256": "sha256:" + _sha256(cover_path),
        "pre_overlay_path": str(pre_overlay_source),
        "pre_overlay_sha256": "sha256:" + _sha256(pre_overlay_source),
        "ai_background": str(background_source),
        "ai_background_sha256": "sha256:" + _sha256(background_source),
        "rendered_text_pixels": {
            "mask_path": str(mask_source),
            "mask_sha256": "sha256:" + _sha256(mask_source),
        },
    }

    record_doc = {
        "classification": "talk",
        # This synthetic fixture exercises burn-family/package naming only; it
        # carries no producer speaker-final manifest, so source-fact authority
        # is explicitly the authorized-absence lane in both naming variants.
        "speaker_mode": "uniform_host",
        "artifact_hashes": {
            "chat_authority_audit_sha256": ("sha256:" + _sha256(chat_authority_source)),
            "clip_context_file_sha256": ("sha256:" + _sha256(clip_context_source)),
        },
        "story_contract": {
            "selection_hook": hook,
            "clip_context_prompt": clip_context_prompt,
            "source_fact_review": receipt,
        },
        "publish_staging": {
            "title": title,
            "source_fact_review": receipt,
        },
    }
    (package_root / f"{candidate_id}.record.json").write_text(
        json.dumps(record_doc, ensure_ascii=False), encoding="utf-8"
    )

    publish_doc = {
        "title": title,
        "source_fact_review": receipt,
        "cover_generation": cover_generation,
    }
    (package_root / f"{stem}.publish.json").write_text(
        json.dumps(publish_doc, ensure_ascii=False), encoding="utf-8"
    )

    state_path = day_root / "state.json"
    state_path.write_text(
        json.dumps(
            {
                "date": "2026-08-08",
                "status": "review_ready",
                "picks": [
                    {
                        "candidate_id": candidate_id,
                        "status": "review_ready",
                        "rc": 0,
                        "cover_path": str(cover_path),
                        "cover_sha256": ("sha256:" + _sha256(cover_path)),
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    deployed_commit_file = tmp_path / "DEPLOYED_COMMIT"
    deployed_commit_file.write_text("abc123deadbeef\n", encoding="utf-8")

    return package_root, state_path, deployed_commit_file, candidate_id


def test_build_uniform_host_package_keeps_legacy_sapphire72_naming(
    tmp_path: Path,
) -> None:
    package_root, state_path, deployed_commit_file, candidate_id = _build_daily_talk_package(
        tmp_path, speaker_finalized=False
    )

    manifest = build(package_root, state_path, deployed_commit_file, candidate_id)

    stem = f"{candidate_id}.recut"
    item = manifest["items"][0]
    assert item["video"] == f"{stem}.burned-final-sapphire72.mp4"
    assert item["ass_path"] == f"{stem}.final-sapphire72.ass"
    assert item["speaker_srt"] == item["subtitle_srt"]
    assert item["speaker_srt_sha256"] == item["sha256"]["subtitle_srt"]


def test_build_speaker_finalized_package_uses_speaker_artifact_family(
    tmp_path: Path,
) -> None:
    package_root, state_path, deployed_commit_file, candidate_id = _build_daily_talk_package(
        tmp_path, speaker_finalized=True
    )

    manifest = build(package_root, state_path, deployed_commit_file, candidate_id)

    stem = f"{candidate_id}.recut"
    item = manifest["items"][0]
    assert item["video"] == f"{stem}.burned-final-speaker.mp4"
    assert item["ass_path"] == f"{stem}.speaker-final.ass"
    assert item["speaker_srt"] == f"{stem}.speaker-final.srt"
    assert item["speaker_srt"] != item["subtitle_srt"]
    assert (package_root / item["speaker_srt"]).is_file()
    assert item["speaker_srt_sha256"] == _sha256(package_root / f"{stem}.speaker-final.srt")


def test_build_accepts_individually_green_pick_during_publication_closure(
    tmp_path: Path,
) -> None:
    package_root, state_path, deployed_commit_file, candidate_id = _build_daily_talk_package(
        tmp_path, speaker_finalized=True
    )
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["status"] = "publication_in_progress"
    state_path.write_text(
        json.dumps(state, ensure_ascii=False),
        encoding="utf-8",
    )

    manifest = build(package_root, state_path, deployed_commit_file, candidate_id)

    assert manifest["items"][0]["candidate_id"] == candidate_id


def test_build_uses_exact_packaged_title_mask_when_source_path_is_missing(
    tmp_path: Path,
) -> None:
    package_root, state_path, deployed_commit_file, candidate_id = _build_daily_talk_package(
        tmp_path, speaker_finalized=True
    )
    publish_path = package_root / f"{candidate_id}.recut.publish.json"
    publish = json.loads(publish_path.read_text(encoding="utf-8"))
    pixels = publish["cover_generation"]["rendered_text_pixels"]
    source = Path(pixels["mask_path"])
    packaged = package_root / "covers_ai_original" / source.name
    packaged.parent.mkdir(exist_ok=True)
    packaged.write_bytes(source.read_bytes())
    source.unlink()

    manifest = build(package_root, state_path, deployed_commit_file, candidate_id)

    item = manifest["items"][0]
    assert item["cover_title_mask"] == f"covers_ai_original/{packaged.name}"
    assert _sha256(package_root / item["cover_title_mask"]) == str(
        pixels["mask_sha256"]
    ).removeprefix("sha256:")


def test_build_rejects_cover_parent_symlink_without_outside_write(
    tmp_path: Path,
) -> None:
    package_root, state_path, deployed_commit_file, candidate_id = _build_daily_talk_package(
        tmp_path, speaker_finalized=True
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    canary = outside / "pre-overlay.png"
    canary.write_bytes(b"outside canary")
    (package_root / "covers_ai_original").symlink_to(outside, target_is_directory=True)

    with pytest.raises(DailyManifestError, match="contains a symlink"):
        build(package_root, state_path, deployed_commit_file, candidate_id)

    assert canary.read_bytes() == b"outside canary"
    assert not (outside / "background.png").exists()
    assert not (outside / "mask.png").exists()


def test_build_rejects_title_mask_symlink_without_outside_write(
    tmp_path: Path,
) -> None:
    package_root, state_path, deployed_commit_file, candidate_id = _build_daily_talk_package(
        tmp_path, speaker_finalized=True
    )
    publish = json.loads(
        (package_root / f"{candidate_id}.recut.publish.json").read_text(encoding="utf-8")
    )
    generation = publish["cover_generation"]
    portable = package_root / "covers_ai_original"
    portable.mkdir()
    for key in ("pre_overlay_path", "ai_background"):
        source = Path(generation[key])
        (portable / source.name).write_bytes(source.read_bytes())
    outside = tmp_path / "outside-mask.png"
    outside.write_bytes(b"outside mask canary")
    mask_name = Path(generation["rendered_text_pixels"]["mask_path"]).name
    (portable / mask_name).symlink_to(outside)

    with pytest.raises(DailyManifestError, match="contains a symlink"):
        build(package_root, state_path, deployed_commit_file, candidate_id)

    assert outside.read_bytes() == b"outside mask canary"


def test_build_rejects_same_stem_target_symlink_without_outside_write(
    tmp_path: Path,
) -> None:
    package_root, state_path, deployed_commit_file, candidate_id = _build_daily_talk_package(
        tmp_path, speaker_finalized=True
    )
    upload_stem = f"{candidate_id}.recut.burned-final-speaker"
    outside = tmp_path / "outside-record.json"
    outside.write_bytes(b"outside record canary")
    (package_root / f"{upload_stem}.record.json").symlink_to(outside)

    with pytest.raises(DailyManifestError, match="contains a symlink"):
        build(package_root, state_path, deployed_commit_file, candidate_id)

    assert outside.read_bytes() == b"outside record canary"


def test_candidate_id_traversal_is_rejected_before_package_write(
    tmp_path: Path,
) -> None:
    package_root = tmp_path / "replacement_recuts"
    package_root.mkdir()
    outside = tmp_path / "escape.chat-authority.json"

    with pytest.raises(DailyManifestError, match="safe path component"):
        _sync_record_bound_candidate_artifacts(
            package_root=package_root,
            candidate_id="../escape",
            record_doc={"artifact_hashes": {}},
        )

    assert not outside.exists()


def test_atomic_projection_failure_preserves_previous_package_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package_root = tmp_path / "replacement_recuts"
    package_root.mkdir()
    target = package_root / "authority.json"
    target.write_bytes(b"previous authority")

    def fail_replace(_source: object, _target: object) -> None:
        raise OSError("synthetic replace failure")

    monkeypatch.setattr(
        "scripts.build_lidousha_daily_review_manifest.os.replace",
        fail_replace,
    )
    with pytest.raises(DailyManifestError, match="atomic package projection failed"):
        _atomic_project_bytes(
            package_root=package_root,
            relative=target.name,
            payload=b"new authority",
            label="test authority",
        )

    assert target.read_bytes() == b"previous authority"
    assert list(package_root.glob(".authority.json.tmp-*")) == []


def _rewrite_batch_status(state_path: Path, status: str) -> None:
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["status"] = status
    state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


@pytest.mark.parametrize(
    "batch_status",
    [
        "publication_in_progress",
        "ready_unpublished",
        "ready_unpublished_with_failures",
    ],
)
def test_build_accepts_publication_closure_batch_statuses(
    tmp_path: Path, batch_status: str
) -> None:
    """A day's second upload must still be buildable.

    ``batch_terminal_state`` promotes the publication-closure status onto the
    top-level ``status`` as soon as the day has one verified publication, so a
    frozen ``review_ready`` pick would otherwise become permanently
    unreviewable (2026-08-07 ``ready_unpublished_with_failures`` refusal).
    """

    package_root, state_path, deployed_commit_file, candidate_id = _build_daily_talk_package(
        tmp_path, speaker_finalized=True
    )
    _rewrite_batch_status(state_path, batch_status)

    manifest = build(package_root, state_path, deployed_commit_file, candidate_id)

    assert manifest["batch_status"] == batch_status
    assert manifest["items"][0]["video"] == (f"{candidate_id}.recut.burned-final-speaker.mp4")


@pytest.mark.parametrize(
    "batch_status",
    ["processing", "retry_wait", "no_delivery", "recovery_incomplete", "published"],
)
def test_build_still_refuses_non_reviewable_batch_statuses(
    tmp_path: Path, batch_status: str
) -> None:
    package_root, state_path, deployed_commit_file, candidate_id = _build_daily_talk_package(
        tmp_path, speaker_finalized=True
    )
    _rewrite_batch_status(state_path, batch_status)

    with pytest.raises(DailyManifestError, match="batch status not reviewable"):
        build(package_root, state_path, deployed_commit_file, candidate_id)
