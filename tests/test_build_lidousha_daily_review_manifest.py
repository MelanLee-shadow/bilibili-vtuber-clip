import json
from pathlib import Path

import pytest

from scripts.build_lidousha_daily_review_manifest import (
    DailyManifestError,
    _sha256,
    _resolve_final_cover,
    _lane_manifest_contract_fields,
    _source_fact_manifest_fields,
    _sync_record_bound_candidate_artifacts,
    _sync_declared_artifact,
    _validate_source_fact_receipts,
)
from src.autoslice.source_fact_review import review_and_repair_source_facts
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
            target=target,
            source=source,
            declared_sha256="sha256:" + "0" * 64,
            label="chat authority",
        )

    assert target.read_bytes() == b"previous package authority\n"


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
            "final_cover": str(
                tmp_path / "generation" / "final.cover.png"
            ),
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

    assert _validate_source_fact_receipts(
        record_doc=record,
        publish_doc=publish,
        subtitle_path=subtitle,
    ) == receipt["receipt_sha256"]

    publish["source_fact_review"] = None
    with pytest.raises(DailyManifestError, match="receipt missing"):
        _validate_source_fact_receipts(
            record_doc=record,
            publish_doc=publish,
            subtitle_path=subtitle,
        )


def test_song_manifest_does_not_require_talk_source_fact_receipt(
    tmp_path: Path,
) -> None:
    subtitle = tmp_path / "song.srt"
    subtitle.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n歌词\n",
        encoding="utf-8",
    )

    assert _source_fact_manifest_fields(
        lane="song",
        record_doc={"classification": "song"},
        publish_doc={"title": "【主播·歌】《测试歌曲》"},
        subtitle_path=subtitle,
    ) == {}
    assert _lane_manifest_contract_fields(
        lane="song",
        record_doc={"classification": "song"},
        publish_doc={"title": "【主播·歌】《测试歌曲》"},
        subtitle_path=subtitle,
    ) == {}
