from __future__ import annotations

import json
import hashlib
from pathlib import Path

from PIL import Image

from scripts.audit_lidousha_review_package import (
    _audit_source_truth_owner_attestations,
    audit_package,
)
from src.autoslice.boundary_semantic_review import (
    cue_grid_sha256,
    semantic_review_sha256,
)
from src.autoslice.clip_context import build_clip_context
from src.autoslice.cover_route_evidence import (
    build_cover_route_decision,
    record_cover_route_execution,
)
from src.autoslice.cover_text_pixel_evidence import (
    materialize_rendered_text_pixel_evidence,
)
from src.autoslice.cover_title_rendering import (
    SCHEMA_VERSION as COVER_TITLE_RENDER_SPEC_SCHEMA,
    render_title_layer,
    sha256_file,
)
from src.autoslice.selection_scorecard import normalize_selection_scorecard
from src.autoslice.jingting_chunker import parse_srt_cues
from src.autoslice.review_package_ass_audit import audit_review_package_ass
from src.autoslice.review_package_boundary_contract import (
    audit_boundary_contract,
)
from src.autoslice.recovery_title_authority import (
    build_recovery_publication_authorities,
    expected_recovery_publish_title,
)
from src.autoslice.story_contract import build_story_contract


REPO_ROOT = Path(__file__).resolve().parents[1]
COVER_FONT = (
    REPO_ROOT / "assets/lidousha/fonts/ZCOOLKuaiLe-Regular.ttf"
)


def test_optional_source_truth_does_not_require_final_owner_attestation():
    issues: list[dict] = []
    chat = {
        "source_subtitle_truth_audit": {
            "applied": [
                {
                    "truth_id": "optional-mop-up",
                    "required": False,
                }
            ],
            "satisfied": [],
        }
    }

    _audit_source_truth_owner_attestations(
        issues=issues,
        stem="optional",
        chat_authority_path=None,
        chat_authority=chat,
        record_path=None,
        record={},
    )

    assert "SOURCE_TRUTH_FINAL_OWNER_ATTESTATION_MISSING" not in {
        issue["code"] for issue in issues
    }

    chat["source_subtitle_truth_audit"]["applied"][0]["required"] = True
    required_issues: list[dict] = []
    _audit_source_truth_owner_attestations(
        issues=required_issues,
        stem="required",
        chat_authority_path=None,
        chat_authority=chat,
        record_path=None,
        record={},
    )
    assert "SOURCE_TRUTH_FINAL_OWNER_ATTESTATION_MISSING" in {
        issue["code"] for issue in required_issues
    }


def _materialize_test_title(
    *,
    pre_overlay: Path,
    final_cover: Path,
    rendered_text: str,
    font_size: int = 120,
) -> dict[str, object]:
    split_at = max(1, len(rendered_text) // 2)
    line_texts = [
        rendered_text[:split_at],
        rendered_text[split_at:],
    ]
    render_spec: dict[str, object] = {
        "schema_version": COVER_TITLE_RENDER_SPEC_SCHEMA,
        "font_file_name": COVER_FONT.name,
        "font_file_sha256": sha256_file(COVER_FONT),
        "font_face_index": 0,
        "layer_size": [1200, 420],
        "rendered_layer_size": [1200, 420],
        "angle_degrees": 0,
        "lines": [
            {
                "x": 20,
                "y": 20 + index * 190,
                "font_size": font_size,
                "segment_pad": 10,
                "segments": [
                    {"text": line_text, "fill": [255, 198, 41]}
                ],
                "outlines": [
                    {"width": 10, "color": [18, 36, 79]},
                    {"width": 5, "color": [255, 255, 255]},
                ],
            }
            for index, line_text in enumerate(line_texts)
        ],
    }
    layer = render_title_layer(render_spec, font_path=COVER_FONT)
    with Image.open(pre_overlay) as source:
        final_image = source.convert("RGB")
    final_image.paste(layer, (360, 100), layer)
    final_image.save(final_cover)
    return materialize_rendered_text_pixel_evidence(
        final_cover_path=final_cover,
        pre_overlay_path=pre_overlay,
        font_path=COVER_FONT,
        render_spec=render_spec,
        paste_xy=(360, 100),
        font_size=font_size,
        rendered_text=rendered_text,
    )


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _passing_boundary_review(
    candidate_id: str,
    *,
    end_ms: int,
    review_scope: str = "source_full_window",
    source_review: dict[str, object] | None = None,
    cue_grid_digest: str | None = None,
    closure_text: str | None = None,
) -> dict[str, object]:
    is_final_delivery = review_scope == "final_delivery"
    request_sha256 = (
        "sha256:" + ("b" if is_final_delivery else "d") * 64
    )
    grid_sha256 = (
        cue_grid_digest
        or "sha256:" + ("c" if is_final_delivery else "e") * 64
    )
    source_separation_witness = None
    if is_final_delivery:
        assert source_review is not None
        source_endpoint = source_review["final_endpoint_binding"]
        assert isinstance(source_endpoint, dict)
        source_separation_witness = {
            "schema_version": (
                "talk-boundary-source-separation-witness.v1"
            ),
            "status": "PASS",
            "source_review_sha256": semantic_review_sha256(source_review),
            "source_request_sha256": source_review["request_sha256"],
            "source_cue_grid_sha256": source_review["cue_grid_sha256"],
            "source_recommended_end_ms": source_review[
                "recommended_end_ms"
            ],
            "source_final_start_ms": source_endpoint["final_start_ms"],
            "source_final_end_ms": source_endpoint["final_end_ms"],
            "reason_codes": [],
        }
    return {
        "schema_version": "talk-boundary-semantic-review.v1",
        "status": "PASS",
        "review_scope": review_scope,
        "candidate_id": candidate_id,
        "request_sha256": request_sha256,
        "target_ms": end_ms,
        "target_cue_index": 1,
        "recommended_end_cue_index": 1,
        "recommended_end_ms": end_ms,
        "syntax_complete": True,
        "story_closed": True,
        "next_topic_separated": True,
        "content_anchor_covered": True,
        "selector_story_witness": {
            "status": "PASS",
            "independence_group": "semantic-llm:gpt-5.6",
        },
        "evidence_cue_indexes": [1],
        "reviewer_independence_group": "semantic-llm:gpt-5.6",
        "semantic_independence_groups": ["semantic-llm:gpt-5.6"],
        "independent_semantic_vote_count": 1,
        "correlated_reviewer_disclosure": True,
        "reason_codes": [],
        "cue_grid_sha256": grid_sha256,
        "next_topic_witness_valid": True,
        "source_separation_witness": source_separation_witness,
        "final_endpoint_binding": {
            "schema_version": "talk-boundary-final-endpoint-binding.v1",
            "status": "PASS",
            "semantic_request_sha256": request_sha256,
            "recommended_end_cue_index": 1,
            "recommended_end_ms": end_ms,
            "final_closure_cue_index": 1,
            "final_snapped_end_ms": end_ms,
            "final_start_ms": 0,
            "final_end_ms": end_ms,
            "semantic_cue_grid_sha256": grid_sha256,
            "final_cue_grid_sha256": grid_sha256,
            "closure_text_sha256": (
                "sha256:"
                + hashlib.sha256(closure_text.encode("utf-8")).hexdigest()
                if closure_text is not None
                else "sha256:" + "f" * 64
            ),
            "reason_codes": [],
        },
    }


def test_exact_source_pin_boundary_authority_survives_package_audit(
    tmp_path: Path,
):
    stem = "auto_193450_1863_2056"
    closure_end_ms = 3_600
    exact_media_end_ms = 4_000
    subtitle = _write(
        tmp_path / f"{stem}.srt",
        "1\n00:00:00,000 --> 00:00:03,600\n"
        "就是刚认识暂时不太熟啊\n",
    )
    source_review = _passing_boundary_review(
        stem,
        end_ms=closure_end_ms,
    )
    source_review["final_endpoint_binding"]["final_end_ms"] = (
        exact_media_end_ms
    )
    final_cues = parse_srt_cues(subtitle.read_text(encoding="utf-8"))
    final_review = _passing_boundary_review(
        stem,
        end_ms=closure_end_ms,
        review_scope="final_delivery",
        source_review=source_review,
        cue_grid_digest=cue_grid_sha256(final_cues),
        closure_text=final_cues[-1].text,
    )
    final_review["final_endpoint_binding"]["final_end_ms"] = (
        exact_media_end_ms
    )
    issues: list[dict] = []
    human_authority = "Pro source cue 911 exact endpoint"
    record = {
        "recovery_publication_authority": {
            "boundary_end_mode": "exact_source_pin",
        },
        "boundary_audit": {
            "boundary_authority": (
                "human_source_exact_pin_plus_semantic_review"
            ),
            "manual_end_authority": human_authority,
            "manual_end_mode": "exact_source_pin",
            "boundary_semantic_review": source_review,
            "final_delivery_boundary_semantic_review": final_review,
            "final_start_ms": 0,
            "snapped_sentence_end_ms": closure_end_ms,
            "final_end_ms": exact_media_end_ms,
            "boundary_selection_lower_bound_ms": closure_end_ms,
            "delivery_coverage_lower_bound_ms": exact_media_end_ms,
            "tail_pad_coverage_bridge": {
                "status": "USED",
                "closure_lower_bound_ms": closure_end_ms,
                "delivery_lower_bound_ms": exact_media_end_ms,
                "maximum_tail_pad_ms": 400,
            },
            "delivery_coverage_verification": {
                "status": "PASS",
                "failure": None,
            },
            "frozen_required_boundary_owner_count": 0,
            "frozen_required_boundary_owners": [],
            "required_boundary_owner_verification": {
                "status": "PASS",
                "failures": [],
            },
        },
    }

    audit_boundary_contract(
        issue_adder=lambda rows, code, **fields: rows.append(
            {"code": code, **fields}
        ),
        issues=issues,
        stem=stem,
        record_path=tmp_path / f"{stem}.record.json",
        subtitle_path=subtitle,
        exact_final_review={
            "boundary_semantic_review": final_review,
        },
        record=record,
        story_contract={
            "human_boundary_authority": human_authority,
            "boundary_semantic_review": final_review,
        },
        required=True,
        is_song=False,
    )

    assert "HUMAN_BOUNDARY_AUTHORITY_DRIFT" not in {
        issue["code"] for issue in issues
    }
    record["boundary_audit"]["manual_end_mode"] = (
        "semantic_lower_bound"
    )
    drift_issues: list[dict] = []
    audit_boundary_contract(
        issue_adder=lambda rows, code, **fields: rows.append(
            {"code": code, **fields}
        ),
        issues=drift_issues,
        stem=stem,
        record_path=tmp_path / f"{stem}.record.json",
        subtitle_path=subtitle,
        exact_final_review={
            "boundary_semantic_review": final_review,
        },
        record=record,
        story_contract={
            "human_boundary_authority": human_authority,
            "boundary_semantic_review": final_review,
        },
        required=True,
        is_song=False,
    )
    assert "HUMAN_BOUNDARY_AUTHORITY_DRIFT" in {
        issue["code"] for issue in drift_issues
    }
    record["boundary_audit"]["manual_end_mode"] = "exact_source_pin"
    record["boundary_audit"]["final_end_ms"] = exact_media_end_ms + 200
    late_end_issues: list[dict] = []
    audit_boundary_contract(
        issue_adder=lambda rows, code, **fields: rows.append(
            {"code": code, **fields}
        ),
        issues=late_end_issues,
        stem=stem,
        record_path=tmp_path / f"{stem}.record.json",
        subtitle_path=subtitle,
        exact_final_review={
            "boundary_semantic_review": final_review,
        },
        record=record,
        story_contract={
            "human_boundary_authority": human_authority,
            "boundary_semantic_review": final_review,
        },
        required=True,
        is_song=False,
    )
    assert "BOUNDARY_DELIVERY_COVERAGE_INVALID" in {
        issue["code"] for issue in late_end_issues
    }


def _minimal_package(tmp_path: Path) -> Path:
    root = tmp_path / "pkg"
    (root / "subtitles").mkdir(parents=True)
    (root / "ass").mkdir()
    (root / "publish").mkdir()
    (root / "evidence").mkdir()
    (root / "covers").mkdir()

    stem = "447s_hybrid_22966160_2026-06-29-22-05-02"
    _write(
        root / "subtitles" / f"{stem}.normalized.zh.srt",
        """1
00:00:00,000 --> 00:00:10,000
却说不出你欣赏我哪一
种表情

2
00:00:52,560 --> 00:01:22,540
词曲 陈绮贞
""",
    )
    _write(
        root / "ass" / f"{stem}.sapphire48.ass",
        r"""[Script Info]
ScriptType: v4.00+

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.00,0:00:10.00,Default,,0,0,0,,却说不出你欣赏我哪一\N种表情
Dialogue: 0,0:00:52.56,0:01:22.54,Default,,0,0,0,,词曲 陈绮贞
""",
    )
    _write(root / "publish" / f"{stem}.title.txt", "【李豆沙】唱着唱着突然卡住：像在KTV录的？\n")
    _write(root / "publish" / f"{stem}.publish.json", json.dumps({"title": "唱着唱着突然卡住：像在KTV录的？"}, ensure_ascii=False))
    _write(
        root / "evidence" / f"{stem}.evidence.json",
        json.dumps(
            {
                "summary": "后半段是直播/伴奏事故，包含歌词：却说不出你欣赏我哪一种表情。",
                "transcript_excerpt": "却说不出你欣赏我哪一种表情 词曲 陈绮贞 怎么还卡了一下",
            },
            ensure_ascii=False,
        ),
    )
    _write(root / "covers" / f"{stem}.cover.png", "fake")
    (root / "review_manifest.json").write_text(
        json.dumps(
            {
                "status": "finished",
                "items": [
                    {
                        "stem": stem,
                        "title": "【李豆沙】唱着唱着突然卡住：像在KTV录的？",
                        "source_srt": f"/app/Videos/.../{stem}.jingting.srt",
                        "subtitle_srt": str(root / "subtitles" / f"{stem}.normalized.zh.srt"),
                        "ass_path": str(root / "ass" / f"{stem}.sapphire48.ass"),
                        "publish_json": str(root / "publish" / f"{stem}.publish.json"),
                        "title_txt": str(root / "publish" / f"{stem}.title.txt"),
                        "cover": str(root / "covers" / f"{stem}.cover.png"),
                        "evidence_json": str(root / "evidence" / f"{stem}.evidence.json"),
                        "cover_generation": "deterministic title + burned-frame cover",
                        "cover_regenerated_from_burn_frame": True,
                    }
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return root


def test_audit_blocks_song_package_without_lyric_alignment_ai_cover_and_title_sync(tmp_path: Path):
    root = _minimal_package(tmp_path)

    result = audit_package(root)

    codes = {issue["code"] for issue in result["issues"]}
    assert result["passed"] is False
    assert "SONG_LYRIC_SOURCE_MISSING" in codes
    assert "SONG_ALIGNMENT_REPORT_MISSING" in codes
    assert "SONG_CATALOG_TITLE_NOT_EXACT" in codes
    assert "PUBLISH_TITLE_TXT_MISMATCH" in codes
    assert "AI_COVER_EVIDENCE_MISSING" in codes
    assert "COVER_FALLBACK_NOT_FINISHED" in codes
    assert "SUBTITLE_LONG_STATIC_CUE" in codes


def test_audit_flags_ass_visual_line_count_and_length(tmp_path: Path):
    root = tmp_path / "pkg"
    stem = "138s_semantic_22966160_2026-06-29-22-35-01"
    _write(
        root / "ass" / f"{stem}.sapphire48.ass",
        r"""[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.00,0:00:20.00,Default,,0,0,0,,剩女是一个组织对剩女而且这一整行明显超过十八个字\\N是一个组织我于是准备\\N把你挂在这你刚好可以\\N跟这个白色背景融为一\\N体你就挂在这吧你作为\\N一副挂画就挂在这直播\\N间你的这个百合厨的属\\N性也非常符合本质
""",
    )
    (root / "review_manifest.json").write_text(
        json.dumps(
            {
                "status": "finished",
                "items": [
                    {
                        "stem": stem,
                        "title": "【李豆沙】测试",
                        "ass_path": str(root / "ass" / f"{stem}.sapphire48.ass"),
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = audit_package(root)

    codes = {issue["code"] for issue in result["issues"]}
    assert "SUBTITLE_ASS_TOO_MANY_VISUAL_LINES" in codes
    assert "SUBTITLE_ASS_LINE_TOO_LONG" in codes


def test_recovery_public_title_authority_is_bound_across_package_surfaces(
    tmp_path: Path,
) -> None:
    candidate_id = "auto_193450_1475_1543"
    authority = build_recovery_publication_authorities(
        candidate_ids={candidate_id},
        registry_path=(
            REPO_ROOT
            / "assets/lidousha/recovery_publication_authority.v1.json"
        ),
        expected_registry_sha256=(
            "sha256:"
            "be9ffbd42008b94d9e47ea714e1fae5d032f576bb0e71841624df3b77ea53757"
        ),
    )[candidate_id]
    title = expected_recovery_publish_title(authority)
    root = tmp_path / "pkg"
    root.mkdir()
    stem = "public-title"
    publish = root / f"{stem}.publish.json"
    publish.write_text(
        json.dumps(
            {
                "title": title,
                "recovery_publication_authority": authority,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    record = {
        "recovery_publication_authority": authority,
        "publish_staging": {
            "title": title,
            "recovery_publication_authority": authority,
        },
        "artifact_hashes": {
            "publish_draft_sha256": (
                "sha256:" + hashlib.sha256(publish.read_bytes()).hexdigest()
            )
        },
    }
    record_path = root / f"{stem}.record.json"
    record_path.write_text(
        json.dumps(record, ensure_ascii=False), encoding="utf-8"
    )
    manifest = {
        "status": "finished",
        "items": [
            {
                "stem": stem,
                "candidate_id": candidate_id,
                "title": title,
                "record": record_path.name,
                "publish_json": publish.name,
                "recovery_publication_authority": authority,
            }
        ],
    }
    manifest_path = root / "review_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )

    clean_codes = {
        issue["code"] for issue in audit_package(root)["issues"]
    }
    assert not any(
        code.startswith("RECOVERY_PUBLICATION")
        for code in clean_codes
    )

    del manifest["items"][0]["recovery_publication_authority"]
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )
    drift_codes = {
        issue["code"] for issue in audit_package(root)["issues"]
    }
    assert "RECOVERY_PUBLICATION_AUTHORITY_SURFACE_MISSING" in (
        drift_codes
    )


def test_current_review_ass_is_portable_hash_bound_and_fail_closed(
    tmp_path: Path,
) -> None:
    root = tmp_path / "pkg"
    root.mkdir()
    stem = "portable-talk"
    speaker_srt = _write(
        root / f"{stem}.speaker.srt",
        "1\n00:00:00,000 --> 00:00:01,000\n[李豆沙] 正常字幕\n",
    )
    ass = _write(
        root / f"{stem}.speaker.ass",
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, "
        "MarginV, Effect, Text\n"
        "Dialogue: 0,0:00:00.00,0:00:01.00,LDS,,0,0,0,,正常字幕\n",
    )
    manifest_path = root / "review_manifest.json"
    manifest = {
        "date": "2026-07-22",
        "run_mode": "RECOVERY_REVIEW",
        "upload_allowed": False,
        "items": [
            {
                "stem": stem,
                "speaker_srt": speaker_srt.name,
                "speaker_srt_sha256": "sha256:"
                + hashlib.sha256(speaker_srt.read_bytes()).hexdigest(),
                "ass_path": f"/missing/remote/{ass.name}",
                "ass_sha256": "sha256:"
                + hashlib.sha256(ass.read_bytes()).hexdigest(),
            }
        ],
    }

    manifest_path.write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    remote_path_result = audit_package(root)
    assert "SUBTITLE_ASS_PATH_MISSING_OR_NONPORTABLE" in {
        issue["code"] for issue in remote_path_result["issues"]
    }

    manifest["items"][0]["ass_path"] = "missing.speaker.ass"
    manifest_path.write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    missing_result = audit_package(root)
    assert "SUBTITLE_ASS_PATH_MISSING_OR_NONPORTABLE" in {
        issue["code"] for issue in missing_result["issues"]
    }

    manifest["items"][0]["ass_path"] = ass.name
    del manifest["items"][0]["ass_sha256"]
    manifest_path.write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    missing_hash_result = audit_package(root)
    assert "SUBTITLE_ASS_HASH_MISSING_OR_INVALID" in {
        issue["code"] for issue in missing_hash_result["issues"]
    }

    manifest["items"][0]["ass_sha256"] = "sha256:" + "0" * 64
    manifest_path.write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    hash_result = audit_package(root)
    assert "SUBTITLE_ASS_HASH_MISMATCH" in {
        issue["code"] for issue in hash_result["issues"]
    }

    ass.write_text("[Events]\n", encoding="utf-8")
    manifest["items"][0]["ass_sha256"] = (
        "sha256:" + hashlib.sha256(ass.read_bytes()).hexdigest()
    )
    manifest_path.write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    dialogue_result = audit_package(root)
    dialogue_codes = {
        issue["code"] for issue in dialogue_result["issues"]
    }
    assert "SUBTITLE_ASS_DIALOGUE_MISSING" in dialogue_codes
    assert "SUBTITLE_ASS_HASH_MISMATCH" not in dialogue_codes


def test_current_review_ass_rejects_package_internal_symlink(
    tmp_path: Path,
) -> None:
    root = tmp_path / "pkg"
    root.mkdir()
    speaker_srt = _write(
        root / "portable-talk.speaker.srt",
        "1\n00:00:00,000 --> 00:00:01,000\n[李豆沙] 正常字幕\n",
    )
    target = _write(
        root / "real.speaker.ass",
        "[Events]\n"
        "Dialogue: 0,0:00:00.00,0:00:01.00,LDS,,0,0,0,,正常字幕\n",
    )
    linked = root / "linked.speaker.ass"
    linked.symlink_to(target.name)
    (root / "review_manifest.json").write_text(
        json.dumps(
            {
                "date": "2026-07-22",
                "run_mode": "RECOVERY_REVIEW",
                "upload_allowed": False,
                "items": [
                    {
                        "stem": "portable-talk",
                        "speaker_srt": speaker_srt.name,
                        "speaker_srt_sha256": "sha256:"
                        + hashlib.sha256(
                            speaker_srt.read_bytes()
                        ).hexdigest(),
                        "ass_path": linked.name,
                        "ass_sha256": "sha256:"
                        + hashlib.sha256(target.read_bytes()).hexdigest(),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = audit_package(root)

    assert "SUBTITLE_ASS_PATH_MISSING_OR_NONPORTABLE" in {
        issue["code"] for issue in result["issues"]
    }


def test_current_review_ass_replays_all_speaker_events_even_after_hash_rebinding(
    tmp_path: Path,
) -> None:
    root = tmp_path / "pkg"
    root.mkdir()
    speaker_srt = _write(
        root / "talk.speaker.srt",
        "1\n00:00:00,000 --> 00:00:01,000\n[李豆沙] 第一句\n\n"
        "2\n00:00:01,000 --> 00:00:02,000\n[连线] 第二句\n",
    )
    ass = root / "talk.speaker.ass"
    valid_events = (
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, "
        "MarginV, Effect, Text\n"
        "Dialogue: 0,0:00:00.00,0:00:01.00,LDS,,0,0,0,,第一句\n"
        "Dialogue: 0,0:00:01.00,0:00:02.00,GUEST,,0,0,0,,第二句\n"
    )

    def audit_with_rebound_hashes(ass_text: str):
        ass.write_text(ass_text, encoding="utf-8")
        ass_hash = "sha256:" + hashlib.sha256(ass.read_bytes()).hexdigest()
        srt_hash = (
            "sha256:" + hashlib.sha256(speaker_srt.read_bytes()).hexdigest()
        )
        return audit_review_package_ass(
            root=root,
            item={
                "ass_path": ass.name,
                "ass_sha256": ass_hash,
                "speaker_srt": speaker_srt.name,
                "speaker_srt_sha256": srt_hash,
            },
            portable_required=True,
            max_visual_lines=2,
            max_visual_line_chars=28,
            record={"artifact_hashes": {"ass_sha256": ass_hash}},
            chat_authority={
                "speaker_ass_sha256": ass_hash,
                "final_speaker_srt_sha256": srt_hash,
            },
        )

    assert audit_with_rebound_hashes(valid_events).issues == ()

    cases = {
        "SUBTITLE_ASS_SPEAKER_SRT_TEXT_MISMATCH": valid_events.replace(
            "第一句\n", "伪造文字\n", 1
        ),
        "SUBTITLE_ASS_SPEAKER_SRT_TIMELINE_MISMATCH": valid_events.replace(
            "0:00:01.00,0:00:02.00", "0:00:01.10,0:00:02.00"
        ),
        "SUBTITLE_ASS_SPEAKER_SRT_STYLE_MISMATCH": valid_events.replace(
            "0:00:02.00,GUEST", "0:00:02.00,LDS"
        ),
        "SUBTITLE_ASS_SPEAKER_SRT_EVENT_COUNT_MISMATCH": (
            valid_events.rsplit("Dialogue:", 1)[0]
        ),
    }
    for expected_code, tampered_ass in cases.items():
        codes = {
            issue.code
            for issue in audit_with_rebound_hashes(tampered_ass).issues
        }
        assert expected_code in codes


def test_audit_does_not_flag_ai_cover_dict_when_fallback_used_false(tmp_path: Path):
    root = tmp_path / "pkg"
    stem = "447s_travel_meaning_redone_20260629"
    srt = _write(
        root / "subtitles" / f"{stem}.srt",
        "1\n00:00:00,000 --> 00:00:06,000\n却说不出你欣赏我哪一种表情\n",
    )
    ass = _write(
        root / "ass" / f"{stem}.ass",
        "[Events]\nDialogue: 0,0:00:00.00,0:00:06.00,Default,,0,0,0,,却说不出你欣赏我哪一种表情\n",
    )
    title = "【李豆沙】豆沙歌，《旅行的意义》"
    publish = _write(root / "publish" / f"{stem}.publish.json", json.dumps({"title": title}, ensure_ascii=False))
    title_txt = _write(root / "publish" / f"{stem}.title.txt", title + "\n")
    evidence = _write(
        root / "evidence" / f"{stem}.evidence.json",
        json.dumps(
            {
                "classification": "song",
                "lyrics_alignment": {"lyric_source_url": "https://example.test/lrc"},
            },
            ensure_ascii=False,
        ),
    )
    ai_bg = root / "covers_ai_original" / f"{stem}.ai-bg.png"
    ai_bg.parent.mkdir(parents=True, exist_ok=True)
    ai_bg.write_bytes(b"fake-ai-bg")
    cover = root / "covers" / f"{stem}.cover.png"
    cover.parent.mkdir(parents=True, exist_ok=True)
    cover.write_bytes(b"fake-cover")
    (root / "review_manifest.json").write_text(
        json.dumps(
            {
                "items": [
                    {
                        "stem": stem,
                        "classification": "song",
                        "source_srt": str(srt),
                        "subtitle_srt": str(srt),
                        "ass_path": str(ass),
                        "publish_json": str(publish),
                        "title_txt": str(title_txt),
                        "cover": str(cover),
                        "evidence_json": str(evidence),
                        "lyrics_alignment_report": str(evidence),
                        "ai_cover_generated": True,
                        "source_ai_background": str(ai_bg),
                        "cover_generation": {
                            "workflow": "cpa-openai-compatible-image-edit-cover",
                            "method": "images.edit",
                            "model": "gpt-image-2",
                            "image_gen_model": "cpa",
                            "fallback_used": False,
                            "ai_background": str(ai_bg),
                            "ai_background_sha256": "sha256:"
                            + hashlib.sha256(ai_bg.read_bytes()).hexdigest(),
                            "final_cover": str(cover),
                            "final_cover_sha256": "sha256:"
                            + hashlib.sha256(cover.read_bytes()).hexdigest(),
                            "attempted_models": ["gpt-image-2"],
                        },
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = audit_package(root)

    assert result["passed"] is True
    assert result["issues"] == []


def test_audit_accepts_hashed_screenshot_cover_without_ai_evidence(tmp_path: Path):
    root = tmp_path / "pkg"
    root.mkdir()
    cover = _write(root / "covers" / "talk.cover.png", "screenshot-cover")
    generation = {
        "method": "screenshot_direct",
        "reference_image": "/remote/cover_refs/talk.cover-ref.png",
        "reference_sha256": "sha256:" + "2" * 64,
        "final_cover": str(cover),
        "final_cover_sha256": "sha256:"
        + hashlib.sha256(cover.read_bytes()).hexdigest(),
        "rendered_lines": ["真实联动画面"],
        "route_decision": {
            "schema_version": "lidousha-cover-route-decision.v1",
            "selected_treatment": "screenshot_direct",
            "reason": "hash-bound frame contains both participants",
        },
    }
    (root / "review_manifest.json").write_text(
        json.dumps(
            {
                "status": "finished",
                "items": [
                    {
                        "stem": "talk",
                        "title": "【李豆沙】真实联动画面",
                        "cover": str(cover),
                        "cover_generation": generation,
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = audit_package(root)

    assert result["passed"] is True
    assert result["issues"] == []


def test_audit_accepts_portable_delivered_cover_with_record_hash(tmp_path: Path):
    root = tmp_path / "pkg"
    root.mkdir()
    delivered_cover = _write(
        root / "covers" / "human-readable-title.cover.png",
        "portable-screenshot-cover",
    )
    final_hash = "sha256:" + hashlib.sha256(
        delivered_cover.read_bytes()
    ).hexdigest()
    generation = {
        "method": "screenshot_direct",
        "reference_image": "/remote/cover_refs/auto.cover-ref.png",
        "reference_sha256": "sha256:" + "2" * 64,
        "final_cover": "/opt/runtime/covers/auto.screenshot-title.cover.png",
        "final_cover_sha256": final_hash,
        "rendered_lines": ["真实联动画面"],
        "route_decision": {
            "schema_version": "lidousha-cover-route-decision.v1",
            "selected_treatment": "screenshot_direct",
            "reason": "hash-bound frame contains both participants",
        },
    }
    (root / "review_manifest.json").write_text(
        json.dumps(
            {
                "status": "finished",
                "items": [
                    {
                        "stem": "talk",
                        "title": "【李豆沙】真实联动画面",
                        "cover": str(delivered_cover),
                        "cover_generation": generation,
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = audit_package(root)

    assert result["passed"] is True
    assert result["issues"] == []


def test_audit_rejects_portable_delivered_cover_with_wrong_bytes(tmp_path: Path):
    root = tmp_path / "pkg"
    root.mkdir()
    delivered_cover = _write(
        root / "covers" / "human-readable-title.cover.png",
        "wrong-cover-bytes",
    )
    generation = {
        "method": "screenshot_direct",
        "reference_image": "/remote/cover_refs/auto.cover-ref.png",
        "reference_sha256": "sha256:" + "2" * 64,
        "final_cover": "/opt/runtime/covers/auto.screenshot-title.cover.png",
        "final_cover_sha256": "sha256:"
        + hashlib.sha256(b"expected-cover-bytes").hexdigest(),
        "rendered_lines": ["真实联动画面"],
        "route_decision": {
            "schema_version": "lidousha-cover-route-decision.v1",
            "selected_treatment": "screenshot_direct",
            "reason": "hash-bound frame contains both participants",
        },
    }
    (root / "review_manifest.json").write_text(
        json.dumps(
            {
                "status": "finished",
                "items": [
                    {
                        "stem": "talk",
                        "title": "【李豆沙】真实联动画面",
                        "cover": str(delivered_cover),
                        "cover_generation": generation,
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = audit_package(root)

    assert result["passed"] is False
    assert {issue["code"] for issue in result["issues"]} == {
        "SCREENSHOT_COVER_EVIDENCE_MISSING"
    }


def test_audit_rejects_cpa_model_defaults_without_materialized_ai(tmp_path: Path):
    root = tmp_path / "pkg"
    root.mkdir()
    (root / "review_manifest.json").write_text(
        json.dumps(
            {
                "status": "finished",
                "items": [
                    {
                        "stem": "talk",
                        "cover_generation": {
                            "method": "images.edit",
                            "model": "gpt-image-2",
                            "image_gen_model": "cpa",
                            "fallback_used": False,
                        },
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = audit_package(root)

    assert result["passed"] is False
    assert {issue["code"] for issue in result["issues"]} == {
        "AI_COVER_EVIDENCE_MISSING"
    }


def test_audit_blocks_known_too_small_talk_cover_title(tmp_path: Path):
    root = tmp_path / "pkg"
    root.mkdir()
    ai_bg = _write(root / "covers_ai_original" / "talk.ai-bg.png", "ai")
    cover = _write(root / "covers" / "talk.cover.png", "cover")
    record = root / "talk.record.json"
    record.write_text(
        json.dumps(
            {
                "cover_generation": {
                    "method": "images.edit",
                    "model": "gpt-image-2",
                    "fallback_used": False,
                    "font_size": 91,
                    "ai_background": str(ai_bg),
                    "ai_background_sha256": "sha256:"
                    + hashlib.sha256(ai_bg.read_bytes()).hexdigest(),
                    "final_cover": str(cover),
                    "final_cover_sha256": "sha256:"
                    + hashlib.sha256(cover.read_bytes()).hexdigest(),
                    "attempted_models": ["gpt-image-2"],
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (root / "review_manifest.json").write_text(
        json.dumps(
            {
                "status": "finished_review_package_no_upload_pending_human_review",
                "items": [
                    {
                        "stem": "talk",
                        "title": "【李豆沙】南町当面追问",
                        "record": record.name,
                        "ai_cover_generated": True,
                        "cover_generation": {
                            "method": "images.edit",
                            "model": "gpt-image-2",
                            "fallback_used": False,
                        },
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = audit_package(root)

    assert result["passed"] is False
    assert {issue["code"] for issue in result["issues"]} == {
        "COVER_TITLE_TOO_SMALL"
    }


def test_audit_accepts_explicit_bounded_sapphire72_visual_contract(tmp_path: Path):
    root = tmp_path / "pkg"
    stem = "autoslice-talk"
    line = "一二三四五六七八九十甲乙丙丁戊己庚辛壬癸子丑寅卯"
    assert 18 < len(line) <= 28
    srt = _write(
        root / f"{stem}.srt",
        f"1\n00:00:00,000 --> 00:00:06,000\n{line}\n",
    )
    ass = _write(
        root / f"{stem}.ass",
        "[Events]\n"
        f"Dialogue: 0,0:00:00.00,0:00:06.00,Default,,0,0,0,,{line}\n",
    )
    cover = _write(root / "covers" / f"{stem}.cover.png", "cover")
    (root / "review_manifest.json").write_text(
        json.dumps(
            {
                "status": "finished_review_package_no_upload_pending_human_review",
                "subtitle_visual_contract": {
                    "profile": "autoslice-sapphire72",
                    "max_visual_lines": 2,
                    "max_chars_per_line": 28,
                },
                "items": [
                    {
                        "stem": stem,
                        "title": "【李豆沙】测试",
                        "subtitle_srt": str(srt),
                        "ass_path": str(ass),
                        "ai_cover_generated": True,
                        "cover": str(cover),
                        "cover_generation": {
                            "method": "screenshot_direct",
                            "reference_image": "source-bound-frame.png",
                            "reference_sha256": "sha256:" + "1" * 64,
                            "final_cover": str(cover),
                            "final_cover_sha256": "sha256:"
                            + hashlib.sha256(cover.read_bytes()).hexdigest(),
                            "rendered_lines": ["测试"],
                            "route_decision": {
                                "schema_version": "lidousha-cover-route-decision.v1",
                                "selected_treatment": "screenshot_direct",
                                "reason": "fixture preserves a strong real moment",
                            },
                        },
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = audit_package(root)

    assert result["passed"] is True
    assert result["issues"] == []


def test_audit_rejects_visual_contract_looser_than_renderer(tmp_path: Path):
    root = tmp_path / "pkg"
    root.mkdir()
    (root / "review_manifest.json").write_text(
        json.dumps(
            {
                "status": "finished_review_package_no_upload_pending_human_review",
                "subtitle_visual_contract": {
                    "max_visual_lines": 3,
                    "max_chars_per_line": 40,
                },
                "items": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = audit_package(root)

    assert result["passed"] is False
    assert {issue["code"] for issue in result["issues"]} == {
        "SUBTITLE_VISUAL_CONTRACT_INVALID"
    }


def test_story_contract_package_rejects_subtitle_drift_and_unresolved_nancho_alias(
    tmp_path: Path,
):
    root = tmp_path / "pkg"
    root.mkdir()
    stem = "nancho-collab"
    transcript = "南町nightin说非常亚撒西"
    srt = _write(
        root / f"{stem}.srt",
        f"1\n00:00:00,000 --> 00:00:04,000\n{transcript}\n",
    )
    speaker_srt = _write(
        root / f"{stem}.speaker.srt",
        f"1\n00:00:00,000 --> 00:00:04,000\n[李豆沙] {transcript}\n",
    )
    speaker_ass = _write(
        root / f"{stem}.speaker.ass",
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, "
        "MarginV, Effect, Text\n"
        f"Dialogue: 0,0:00:00.00,0:00:04.00,LDS,,0,0,0,,{transcript}\n",
    )
    speaker_ass_sha256 = "sha256:" + hashlib.sha256(
        speaker_ass.read_bytes()
    ).hexdigest()
    speaker_srt_sha256 = "sha256:" + hashlib.sha256(
        speaker_srt.read_bytes()
    ).hexdigest()
    scorecard = normalize_selection_scorecard(
        {
            "tier": 1,
            "tier_basis": "relationship_chain",
            "tier_reason": "联动关系与反转",
            "tier_evidence_cues": [1, 2],
            "dimensions": {
                "lidousha_centrality": 4,
                "stance_intensity": 3,
                "audience_salience": 4,
                "relationship_interaction": 4,
                "persona_reversal": 3,
                "comedic_payoff": 3,
                "self_contained": 4,
            },
            "uncertainty_penalty": 0,
            "fatigue_penalty": 0,
        },
        start_cue=1,
        end_cue=2,
    )
    assert scorecard is not None
    source_sha256 = "sha256:" + "1" * 64
    cover_reference_authority = {
        "candidate_id": stem,
        "content_time_ms": 1_000,
        "source_time_ms": 1_000,
        "source_sha256": source_sha256,
        "reference_png_sha256": "sha256:" + "2" * 64,
        "visible_participant_ids": ["lidousha", "nancho"],
        "required_treatment": "cpa_redraw",
        "authority": "fixture reviewed source frame",
    }
    clip_context = build_clip_context(
        candidate_id=stem,
        spec={
            "date": "2026-07-22",
            "selection_hook": "南町当面追问李豆沙最喜欢谁",
            "pieces": [
                {
                    "start_ms": 0,
                    "end_ms": 4_000,
                    "remote_media": "/recordings/collab.mp4",
                    "source_media_sha256": source_sha256,
                }
            ],
            "session_relation_authority": {
                "state": "CONFIRMED",
                "relation_id": "20260722-lidousha-nancho-live-collaboration",
            },
        },
        draft_srt=srt.read_text(encoding="utf-8"),
        authoritative_chat=(),
        topic_resolution={"status": "NO_GRAPH"},
        session_topic_authorities=(),
        speech_memory_ledger_path=(
            Path(__file__).resolve().parents[1]
            / "assets/lidousha/speech_memory_ledger.v1.json"
        ),
    )
    clip_context_path = root / f"{stem}.clip-context.json"
    clip_context_path.write_text(
        json.dumps(clip_context, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    source_boundary_review = _passing_boundary_review(
        stem,
        end_ms=4_000,
    )
    final_cues = parse_srt_cues(srt.read_bytes().decode("utf-8"))
    final_boundary_review = _passing_boundary_review(
        stem,
        end_ms=4_000,
        review_scope="final_delivery",
        source_review=source_boundary_review,
        cue_grid_digest=cue_grid_sha256(final_cues),
        closure_text=final_cues[-1].text,
    )
    contract = build_story_contract(
        candidate_id=stem,
        selection_hook="南町当面追问李豆沙最喜欢谁",
        transcript_text=transcript,
        selection_scorecard=scorecard,
        session_relation_authority={
            "state": "CONFIRMED",
            "participants": ["lidousha", "nancho"],
        },
        source_media_sha256s=[source_sha256],
        cover_reference_authority=cover_reference_authority,
        clip_context=clip_context,
        recording_date="2026-07-22",
        boundary_semantic_review=final_boundary_review,
        human_boundary_authority="fixture source-reviewed closure",
    )
    record = root / f"{stem}.record.json"
    ai_bg = root / "covers_ai_original" / f"{stem}.ai-bg.png"
    ai_bg.parent.mkdir(parents=True)
    Image.new("RGB", (1920, 1080), (244, 238, 220)).save(ai_bg)
    cover = root / "covers" / f"{stem}.cover.png"
    cover.parent.mkdir(parents=True)
    cover_text = "南町当面追问最最最最喜欢"
    rendered_text_pixels = _materialize_test_title(
        pre_overlay=ai_bg,
        final_cover=cover,
        rendered_text=cover_text,
    )
    cover_binding = {
        "schema_version": contract["schema_version"],
        "selection_hook": contract["selection_hook"],
        "relation_state": contract["relation_state"],
        "participants": contract["participants"],
        "cover_counterpart_reference_available": contract[
            "cover_counterpart_reference_available"
        ],
        "cover_reference_authority": contract["cover_reference_authority"],
        "source_media_sha256s": contract["source_media_sha256s"],
        "clip_context_binding": contract["clip_context_binding"],
        "boundary_semantic_review": contract["boundary_semantic_review"],
        "human_boundary_authority": contract["human_boundary_authority"],
        "cover_fallback_mode": contract["cover_fallback_mode"],
    }
    final_cover_sha256 = "sha256:" + hashlib.sha256(
        cover.read_bytes()
    ).hexdigest()
    generation = {
        "title": "【李豆沙】南町当面追问最最最最喜欢",
        "cover_text": cover_text,
        "rendered_lines": ["南町当面追问", "最最最最喜欢"],
        "rendered_text_pixels": rendered_text_pixels,
        "font_size": 120,
        "font_selection": {
            "font": COVER_FONT.name,
            "glyph_risk": [],
        },
        "angle_degrees": 0,
        "method": "images.edit",
        "cover_origin": "AI_REDRAW",
        "model": "gpt-image-2",
        "attempted_models": ["gpt-image-2"],
        "ai_background": str(ai_bg),
        "ai_background_sha256": "sha256:"
        + hashlib.sha256(ai_bg.read_bytes()).hexdigest(),
        "pre_overlay_path": str(ai_bg),
        "pre_overlay_sha256": rendered_text_pixels[
            "pre_overlay_sha256"
        ],
        "overlay_position": rendered_text_pixels["overlay_position"],
        "text_backing": "outline",
        "scrim": False,
        "final_cover": str(cover),
        "final_cover_sha256": final_cover_sha256,
        "reference_sha256": cover_reference_authority[
            "reference_png_sha256"
        ],
        "reference_authority": cover_reference_authority,
        "story_contract": cover_binding,
    }
    generation["route_decision"] = build_cover_route_decision(
        selected_treatment="cpa_redraw",
        selected_rationale=(
            "fixture redraw with independent final-pixel verification"
        ),
        story_contract=cover_binding,
        reference_authority=cover_reference_authority,
        decision_inputs={"cover_mode": "cpa"},
        title=generation["title"],
        cover_text=cover_text,
    )
    final_participant_verification = {
        "schema_version": (
            "lidousha-cover-final-participant-verification.v1"
        ),
        "status": "PASS",
        "authority": "fixture independent final-pixel review",
        "final_cover_sha256": final_cover_sha256,
        "visible_participant_ids": ["lidousha", "nancho"],
    }
    record_cover_route_execution(
        generation,
        actual_treatment="cpa_redraw",
        execution_status="READY",
        image_generation_attempted=True,
        image_generation_used=True,
        final_participant_verification=final_participant_verification,
    )
    chat_authority = root / f"{stem}.chat-authority.json"
    final_srt_sha256 = "sha256:" + hashlib.sha256(
        srt.read_text(encoding="utf-8").encode("utf-8")
    ).hexdigest()
    chat_authority.write_text(
        json.dumps(
            {
                "schema_version": "fixture-chat-authority.v1",
                "status": "PASS",
                "speaker_ass_sha256": speaker_ass_sha256,
                "final_speaker_srt_sha256": speaker_srt_sha256,
                "frozen_boundary_owner_contract": {
                    "schema_version": "frozen-boundary-owner-contract.v1",
                    "status": "FROZEN",
                    "story_start_ms": 0,
                    "story_end_ms": 4_000,
                    "required_owner_count": 0,
                    "owners": [],
                },
                "final_boundary_required_exclusion_count": 0,
                "final_review_audit": {
                    "schema_version": "final-review-audit.v2",
                    "status": "CLEAN",
                    "release_gate": "PASS",
                    "reviewed_srt_sha256": final_srt_sha256,
                    "discovery": {
                        "status": "COMPLETE",
                        "explicit_empty_findings": True,
                    },
                    "correction_mutation_authority": {
                        "schema_version": (
                            "subtitle-correction-mutation-audit.v1"
                        ),
                        "status": "PASS",
                        "applied_count": 0,
                        "validated_mutation_count": 0,
                        "failures": [],
                    },
                    "findings": [],
                    "validated_finding_count": 0,
                    "boundary_semantic_review": final_boundary_review,
                },
            }
        ),
        encoding="utf-8",
    )
    record.write_text(
        json.dumps(
            {
                "story_contract": contract,
                "boundary_audit": {
                    "boundary_authority": (
                        "human_source_reviewed_lower_bound_plus_semantic_review"
                    ),
                    "manual_end_authority": "fixture source-reviewed closure",
                    "manual_end_mode": "semantic_lower_bound",
                    "boundary_semantic_review": source_boundary_review,
                    "final_delivery_boundary_semantic_review": (
                        final_boundary_review
                    ),
                    "final_start_ms": 0,
                    "snapped_sentence_end_ms": 4_000,
                    "final_end_ms": 4_000,
                    "boundary_selection_lower_bound_ms": 4_000,
                    "delivery_coverage_lower_bound_ms": 4_000,
                    "tail_pad_coverage_bridge": {
                        "status": "NOT_NEEDED",
                        "closure_lower_bound_ms": 4_000,
                        "delivery_lower_bound_ms": 4_000,
                        "maximum_tail_pad_ms": 400,
                    },
                    "delivery_coverage_verification": {
                        "status": "PASS",
                        "failure": None,
                    },
                    "frozen_required_boundary_owner_count": 0,
                    "frozen_required_boundary_owners": [],
                    "required_boundary_owner_verification": {
                        "status": "PASS",
                        "failures": [],
                    },
                },
                "clip_context_path": clip_context_path.name,
                "clip_context_payload_sha256": clip_context["context_sha256"],
                "artifact_hashes": {
                    "clip_context_file_sha256": "sha256:"
                    + hashlib.sha256(
                        clip_context_path.read_bytes()
                    ).hexdigest(),
                    "chat_authority_audit_sha256": "sha256:"
                    + hashlib.sha256(
                        chat_authority.read_bytes()
                    ).hexdigest(),
                    "subtitle_sha256": final_srt_sha256,
                    "ass_sha256": speaker_ass_sha256,
                },
                "publish_staging": {
                    "title": "【李豆沙】南町当面追问最最最最喜欢",
                    "cover_text": cover_text,
                    "cover_generation": generation,
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    recorded_generation = json.loads(record.read_text(encoding="utf-8"))[
        "publish_staging"
    ]["cover_generation"]
    (root / "review_manifest.json").write_text(
        json.dumps(
                {
                    "date": "2026-07-22",
                    "status": "finished_review_package_no_upload_pending_human_review",
                "story_contract_required": True,
                "run_mode": "RECOVERY_REVIEW",
                "upload_allowed": False,
                "cover_route_attestations": [
                    {
                        "candidate_id": stem,
                        "reference_sha256": recorded_generation.get(
                            "reference_sha256"
                        ),
                        "final_cover_sha256": recorded_generation.get(
                            "final_cover_sha256"
                        ),
                        "method": recorded_generation.get("method"),
                        "route_decision": recorded_generation.get(
                            "route_decision"
                        ),
                        "reference_authority": recorded_generation.get(
                            "reference_authority"
                        ),
                    }
                ],
                "items": [
                    {
                        "stem": stem,
                        "candidate_id": stem,
                        "title": "【李豆沙】南町当面追问最最最最喜欢",
                            "subtitle_srt": srt.name,
                            "speaker_srt": speaker_srt.name,
                            "speaker_srt_sha256": speaker_srt_sha256,
                            "ass_path": speaker_ass.name,
                            "ass_sha256": speaker_ass_sha256,
                            "record": record.name,
                            "chat_authority": chat_authority.name,
                            "cover": cover.relative_to(root).as_posix(),
                            "cover_title_mask": Path(
                                rendered_text_pixels["mask_path"]
                            ).relative_to(root).as_posix(),
                            "cover_pre_overlay": ai_bg.relative_to(
                                root
                            ).as_posix(),
                            "cover_route_background": ai_bg.relative_to(
                                root
                            ).as_posix(),
                            "source_ai_background": ai_bg.relative_to(
                                root
                            ).as_posix(),
                            "ai_cover_generated": True,
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    initial_audit = audit_package(root)
    assert initial_audit["passed"] is True, json.dumps(
        initial_audit["issues"], ensure_ascii=False, indent=2
    )
    portable_record_text = record.read_text(encoding="utf-8")
    stale_external_cover = tmp_path / "stale-external-cover.png"
    stale_external_background = tmp_path / "stale-external-background.png"
    Image.new("RGB", (1920, 1080), (1, 2, 3)).save(
        stale_external_cover
    )
    Image.new("RGB", (1920, 1080), (4, 5, 6)).save(
        stale_external_background
    )
    portable_record = json.loads(portable_record_text)
    portable_generation = portable_record["publish_staging"][
        "cover_generation"
    ]
    portable_generation["final_cover"] = str(stale_external_cover)
    portable_generation["ai_background"] = str(
        stale_external_background
    )
    record.write_text(
        json.dumps(portable_record, ensure_ascii=False),
        encoding="utf-8",
    )
    portable_audit = audit_package(root)
    assert portable_audit["passed"] is True, json.dumps(
        portable_audit["issues"], ensure_ascii=False, indent=2
    )
    record.write_text(portable_record_text, encoding="utf-8")

    valid_chat_authority = chat_authority.read_text(encoding="utf-8")
    valid_record_before_final_review_tamper = record.read_text(encoding="utf-8")
    tampered_chat = json.loads(valid_chat_authority)
    tampered_chat["final_review_audit"]["status"] = "AUDITOR_UNAVAILABLE"
    tampered_chat["final_review_audit"]["release_gate"] = "BLOCK"
    tampered_chat["final_review_audit"]["discovery"] = {
        "status": "AUDITOR_UNAVAILABLE"
    }
    chat_authority.write_text(
        json.dumps(tampered_chat, ensure_ascii=False),
        encoding="utf-8",
    )
    rebound_record = json.loads(record.read_text(encoding="utf-8"))
    rebound_record["artifact_hashes"]["chat_authority_audit_sha256"] = (
        "sha256:" + hashlib.sha256(chat_authority.read_bytes()).hexdigest()
    )
    record.write_text(
        json.dumps(rebound_record, ensure_ascii=False),
        encoding="utf-8",
    )
    final_review_result = audit_package(root)
    assert final_review_result["passed"] is False
    assert "FINAL_REVIEW_DISCOVERY_INCOMPLETE" in {
        row["code"] for row in final_review_result["issues"]
    }
    chat_authority.write_text(valid_chat_authority, encoding="utf-8")
    record.write_text(
        valid_record_before_final_review_tamper,
        encoding="utf-8",
    )

    # A producer cannot make required source truth disappear by forging a
    # smaller frozen-boundary contract that happens to agree with the final
    # boundary audit.  The package auditor independently binds required truth
    # IDs to source_subtitle_truth owners.
    missing_owner_chat = json.loads(valid_chat_authority)
    missing_owner_chat["source_subtitle_truth_audit"] = {
        "status": "SATISFIED",
        "applied": [],
        "satisfied": [
            {
                "truth_id": "fixture-required-truth-r1",
                "required": True,
                "action": "replace_substring",
                "required_text": "南町nightin",
                "local_windows": [{"start_ms": 0, "end_ms": 4_000}],
            }
        ],
        "failures": [],
    }
    missing_owner_chat["final_source_truth_owner_verification"] = {
        "status": "PASS",
        "required_window_count": 1,
        "failures": [],
    }
    missing_owner_chat["final_required_decision_count"] = 1
    chat_authority.write_text(
        json.dumps(missing_owner_chat, ensure_ascii=False),
        encoding="utf-8",
    )
    missing_owner_record = json.loads(
        valid_record_before_final_review_tamper
    )
    missing_owner_record["artifact_hashes"][
        "chat_authority_audit_sha256"
    ] = "sha256:" + hashlib.sha256(chat_authority.read_bytes()).hexdigest()
    record.write_text(
        json.dumps(missing_owner_record, ensure_ascii=False),
        encoding="utf-8",
    )
    missing_owner_result = audit_package(root)
    assert "FROZEN_BOUNDARY_OWNER_CONTRACT_MISSING_OR_INVALID" in {
        row["code"] for row in missing_owner_result["issues"]
    }
    chat_authority.write_text(valid_chat_authority, encoding="utf-8")
    record.write_text(
        valid_record_before_final_review_tamper,
        encoding="utf-8",
    )

    prompt_tampered_record = json.loads(
        valid_record_before_final_review_tamper
    )
    prompt_tampered_record["story_contract"]["clip_context_prompt"] += (
        "\nforged prompt"
    )
    record.write_text(
        json.dumps(prompt_tampered_record, ensure_ascii=False),
        encoding="utf-8",
    )
    prompt_binding_result = audit_package(root)
    assert "CLIP_CONTEXT_PROMPT_BINDING_DRIFT" in {
        row["code"] for row in prompt_binding_result["issues"]
    }
    record.write_text(
        valid_record_before_final_review_tamper,
        encoding="utf-8",
    )

    manifest_path = root / "review_manifest.json"
    valid_record = record.read_text(encoding="utf-8")
    valid_manifest = manifest_path.read_text(encoding="utf-8")
    valid_srt_bytes = srt.read_bytes()
    srt.write_bytes(valid_srt_bytes.replace(b"\n", b"\r\n"))
    newline_drift_result = audit_package(root)
    newline_drift_codes = {
        issue["code"] for issue in newline_drift_result["issues"]
    }
    assert "SUBTITLE_RECORD_HASH_MISMATCH" in newline_drift_codes
    assert "FINAL_REVIEW_SRT_BINDING_MISMATCH" in newline_drift_codes
    assert "STORY_CONTRACT_SUBTITLE_HASH_DRIFT" not in newline_drift_codes
    srt.write_bytes(valid_srt_bytes)

    # Updating every raw-byte declaration is not authority to reuse an old
    # semantic receipt for a different final cue grid.
    srt.write_bytes(
        valid_srt_bytes.replace(
            b"00:00:04,000",
            b"00:00:03,900",
        )
    )
    drifted_srt_sha256 = "sha256:" + hashlib.sha256(
        srt.read_bytes()
    ).hexdigest()
    drifted_grid_chat = json.loads(valid_chat_authority)
    drifted_grid_chat["final_review_audit"][
        "reviewed_srt_sha256"
    ] = drifted_srt_sha256
    chat_authority.write_text(
        json.dumps(drifted_grid_chat, ensure_ascii=False),
        encoding="utf-8",
    )
    drifted_grid_record = json.loads(valid_record)
    drifted_grid_record["artifact_hashes"][
        "subtitle_sha256"
    ] = drifted_srt_sha256
    drifted_grid_record["artifact_hashes"][
        "chat_authority_audit_sha256"
    ] = "sha256:" + hashlib.sha256(
        chat_authority.read_bytes()
    ).hexdigest()
    record.write_text(
        json.dumps(drifted_grid_record, ensure_ascii=False),
        encoding="utf-8",
    )
    cue_grid_drift_result = audit_package(root)
    cue_grid_drift_codes = {
        issue["code"] for issue in cue_grid_drift_result["issues"]
    }
    assert "BOUNDARY_FINAL_DELIVERY_CUE_GRID_MISMATCH" in (
        cue_grid_drift_codes
    )
    assert "SUBTITLE_RECORD_HASH_MISMATCH" not in cue_grid_drift_codes
    assert "FINAL_REVIEW_SRT_BINDING_MISMATCH" not in cue_grid_drift_codes
    srt.write_bytes(valid_srt_bytes)
    chat_authority.write_text(valid_chat_authority, encoding="utf-8")
    record.write_text(valid_record, encoding="utf-8")

    valid_cover_bytes = cover.read_bytes()
    Image.open(ai_bg).save(cover)
    no_text_pixels = dict(rendered_text_pixels)
    no_text_pixels["final_cover_sha256"] = (
        "sha256:" + hashlib.sha256(cover.read_bytes()).hexdigest()
    )
    with (
        Image.open(cover) as no_text_cover,
        Image.open(Path(rendered_text_pixels["mask_path"])) as title_mask,
    ):
        rgb = no_text_cover.convert("RGB")
        masked = Image.composite(
            rgb,
            Image.new("RGB", rgb.size),
            title_mask.convert("L"),
        )
    no_text_pixels["masked_final_pixels_sha256"] = (
        "sha256:" + hashlib.sha256(masked.tobytes()).hexdigest()
    )
    no_text_record = json.loads(valid_record)
    no_text_generation = no_text_record["publish_staging"][
        "cover_generation"
    ]
    no_text_generation["final_cover_sha256"] = no_text_pixels[
        "final_cover_sha256"
    ]
    no_text_generation["rendered_text_pixels"] = no_text_pixels
    no_text_generation["final_participant_verification"][
        "final_cover_sha256"
    ] = no_text_pixels["final_cover_sha256"]
    record.write_text(
        json.dumps(no_text_record, ensure_ascii=False),
        encoding="utf-8",
    )
    no_text_manifest = json.loads(valid_manifest)
    no_text_attestation = no_text_manifest[
        "cover_route_attestations"
    ][0]
    no_text_attestation["final_cover_sha256"] = no_text_pixels[
        "final_cover_sha256"
    ]
    no_text_attestation["route_decision"] = no_text_generation[
        "route_decision"
    ]
    manifest_path.write_text(
        json.dumps(no_text_manifest, ensure_ascii=False),
        encoding="utf-8",
    )
    no_text_result = audit_package(root)
    assert "COVER_RENDERED_TEXT_PIXEL_ARTIFACT_MISMATCH" in {
        issue["code"] for issue in no_text_result["issues"]
    }
    cover.write_bytes(valid_cover_bytes)
    record.write_text(valid_record, encoding="utf-8")
    manifest_path.write_text(valid_manifest, encoding="utf-8")

    missing_cover_attestations = json.loads(valid_manifest)
    missing_cover_attestations.pop("cover_route_attestations")
    manifest_path.write_text(
        json.dumps(missing_cover_attestations, ensure_ascii=False),
        encoding="utf-8",
    )
    missing_cover_result = audit_package(root)
    missing_cover_codes = {
        issue["code"] for issue in missing_cover_result["issues"]
    }
    assert "MANIFEST_COVER_ATTESTATIONS_MISSING" in missing_cover_codes
    assert "MANIFEST_COVER_ATTESTATION_SET_MISMATCH" in missing_cover_codes
    assert "MANIFEST_COVER_ATTESTATION_MISSING" in missing_cover_codes
    manifest_path.write_text(valid_manifest, encoding="utf-8")

    extra_cover_attestation = json.loads(valid_manifest)
    extra = dict(extra_cover_attestation["cover_route_attestations"][0])
    extra["candidate_id"] = "auto_unexpected_cover"
    extra_cover_attestation["cover_route_attestations"].append(extra)
    manifest_path.write_text(
        json.dumps(extra_cover_attestation, ensure_ascii=False),
        encoding="utf-8",
    )
    extra_cover_result = audit_package(root)
    assert "MANIFEST_COVER_ATTESTATION_SET_MISMATCH" in {
        issue["code"] for issue in extra_cover_result["issues"]
    }
    manifest_path.write_text(valid_manifest, encoding="utf-8")

    invalid_boundary_record = json.loads(valid_record)
    invalid_boundary_record["story_contract"][
        "boundary_semantic_review"
    ] = None
    invalid_boundary_record["boundary_audit"] = {
        "boundary_authority": "human_source_reviewed_end",
        "manual_end_authority": "fixture source-reviewed closure",
    }
    record.write_text(
        json.dumps(invalid_boundary_record, ensure_ascii=False),
        encoding="utf-8",
    )
    boundary_result = audit_package(root)
    boundary_codes = {
        issue["code"] for issue in boundary_result["issues"]
    }
    assert "BOUNDARY_SEMANTIC_REVIEW_NOT_PASS" in boundary_codes
    assert "HUMAN_BOUNDARY_AUTHORITY_DRIFT" in boundary_codes
    record.write_text(valid_record, encoding="utf-8")

    stale_grid_record = json.loads(valid_record)
    stale_grid_review = stale_grid_record["story_contract"][
        "boundary_semantic_review"
    ]
    stale_grid_review["final_endpoint_binding"][
        "final_cue_grid_sha256"
    ] = "sha256:" + "f" * 64
    stale_grid_record["boundary_audit"][
        "final_delivery_boundary_semantic_review"
    ] = stale_grid_review
    record.write_text(
        json.dumps(stale_grid_record, ensure_ascii=False),
        encoding="utf-8",
    )
    stale_grid_result = audit_package(root)
    assert "BOUNDARY_SEMANTIC_REVIEW_NOT_PASS" in {
        issue["code"] for issue in stale_grid_result["issues"]
    }
    record.write_text(valid_record, encoding="utf-8")

    wrong_final_scope = json.loads(valid_record)
    wrong_final_scope_review = wrong_final_scope["story_contract"][
        "boundary_semantic_review"
    ]
    wrong_final_scope_review["review_scope"] = "source_full_window"
    wrong_final_scope["boundary_audit"][
        "final_delivery_boundary_semantic_review"
    ] = wrong_final_scope_review
    record.write_text(
        json.dumps(wrong_final_scope, ensure_ascii=False),
        encoding="utf-8",
    )
    wrong_final_scope_result = audit_package(root)
    assert "BOUNDARY_SEMANTIC_REVIEW_NOT_PASS" in {
        issue["code"] for issue in wrong_final_scope_result["issues"]
    }
    record.write_text(valid_record, encoding="utf-8")

    wrong_source_scope = json.loads(valid_record)
    wrong_source_scope_review = wrong_source_scope["boundary_audit"][
        "boundary_semantic_review"
    ]
    wrong_source_scope_review["review_scope"] = "final_delivery"
    rebound_final_review = wrong_source_scope["story_contract"][
        "boundary_semantic_review"
    ]
    rebound_final_review["source_separation_witness"][
        "source_review_sha256"
    ] = semantic_review_sha256(wrong_source_scope_review)
    wrong_source_scope["boundary_audit"][
        "final_delivery_boundary_semantic_review"
    ] = rebound_final_review
    record.write_text(
        json.dumps(wrong_source_scope, ensure_ascii=False),
        encoding="utf-8",
    )
    wrong_source_scope_result = audit_package(root)
    assert "BOUNDARY_SEMANTIC_REVIEW_NOT_PASS" in {
        issue["code"] for issue in wrong_source_scope_result["issues"]
    }
    record.write_text(valid_record, encoding="utf-8")

    final_review_binding_drift = json.loads(valid_record)
    final_review_binding_drift["boundary_audit"][
        "final_delivery_boundary_semantic_review"
    ]["summary"] = "unbound audit copy"
    record.write_text(
        json.dumps(final_review_binding_drift, ensure_ascii=False),
        encoding="utf-8",
    )
    final_binding_result = audit_package(root)
    assert "BOUNDARY_SEMANTIC_REVIEW_BINDING_DRIFT" in {
        issue["code"] for issue in final_binding_result["issues"]
    }
    record.write_text(valid_record, encoding="utf-8")

    invalid_delivery_coverage = json.loads(valid_record)
    invalid_delivery_coverage["boundary_audit"]["final_end_ms"] = 3_999
    record.write_text(
        json.dumps(invalid_delivery_coverage, ensure_ascii=False),
        encoding="utf-8",
    )
    delivery_coverage_result = audit_package(root)
    assert "BOUNDARY_DELIVERY_COVERAGE_INVALID" in {
        issue["code"] for issue in delivery_coverage_result["issues"]
    }
    record.write_text(valid_record, encoding="utf-8")

    for invalid_maximum_tail_pad_ms in (401, 400.0):
        invalid_tail_pad = json.loads(valid_record)
        invalid_tail_pad["boundary_audit"]["tail_pad_coverage_bridge"][
            "maximum_tail_pad_ms"
        ] = invalid_maximum_tail_pad_ms
        record.write_text(
            json.dumps(invalid_tail_pad, ensure_ascii=False),
            encoding="utf-8",
        )
        invalid_tail_pad_result = audit_package(root)
        assert "BOUNDARY_DELIVERY_COVERAGE_INVALID" in {
            issue["code"] for issue in invalid_tail_pad_result["issues"]
        }
    record.write_text(valid_record, encoding="utf-8")

    for invalid_closure_ms, bridge_status in (
        (3_999, "USED"),
        (4_000.0, "NOT_NEEDED"),
    ):
        invalid_closure_lower_bound = json.loads(valid_record)
        invalid_closure_lower_bound["boundary_audit"][
            "tail_pad_coverage_bridge"
        ].update(
            {
                "status": bridge_status,
                "closure_lower_bound_ms": invalid_closure_ms,
            }
        )
        record.write_text(
            json.dumps(invalid_closure_lower_bound, ensure_ascii=False),
            encoding="utf-8",
        )
        invalid_closure_result = audit_package(root)
        assert "BOUNDARY_DELIVERY_COVERAGE_INVALID" in {
            issue["code"] for issue in invalid_closure_result["issues"]
        }
    record.write_text(valid_record, encoding="utf-8")

    invalid_source_witness = json.loads(valid_record)
    tampered_final_review = invalid_source_witness["story_contract"][
        "boundary_semantic_review"
    ]
    tampered_final_review["source_separation_witness"][
        "source_review_sha256"
    ] = "sha256:" + "0" * 64
    invalid_source_witness["boundary_audit"][
        "final_delivery_boundary_semantic_review"
    ] = tampered_final_review
    record.write_text(
        json.dumps(invalid_source_witness, ensure_ascii=False),
        encoding="utf-8",
    )
    invalid_witness_result = audit_package(root)
    assert "BOUNDARY_SOURCE_SEPARATION_WITNESS_INVALID" in {
        issue["code"] for issue in invalid_witness_result["issues"]
    }
    record.write_text(valid_record, encoding="utf-8")

    for witness_field, drifted_value in (
        ("source_request_sha256", "sha256:" + "1" * 64),
        ("source_cue_grid_sha256", "sha256:" + "2" * 64),
        ("source_recommended_end_ms", 4_000.0),
        ("source_final_start_ms", 0.0),
        ("source_final_end_ms", 4_000.0),
    ):
        invalid_source_binding = json.loads(valid_record)
        rebound_review = invalid_source_binding["story_contract"][
            "boundary_semantic_review"
        ]
        rebound_review["source_separation_witness"][
            witness_field
        ] = drifted_value
        invalid_source_binding["boundary_audit"][
            "final_delivery_boundary_semantic_review"
        ] = rebound_review
        record.write_text(
            json.dumps(invalid_source_binding, ensure_ascii=False),
            encoding="utf-8",
        )
        invalid_source_binding_result = audit_package(root)
        assert "BOUNDARY_SOURCE_SEPARATION_WITNESS_INVALID" in {
            issue["code"]
            for issue in invalid_source_binding_result["issues"]
        }
    record.write_text(valid_record, encoding="utf-8")

    invalid_delivery_local_endpoint = json.loads(valid_record)
    nonlocal_final_review = invalid_delivery_local_endpoint[
        "story_contract"
    ]["boundary_semantic_review"]
    nonlocal_final_review["final_endpoint_binding"][
        "final_start_ms"
    ] = 1
    invalid_delivery_local_endpoint["boundary_audit"][
        "final_delivery_boundary_semantic_review"
    ] = nonlocal_final_review
    record.write_text(
        json.dumps(invalid_delivery_local_endpoint, ensure_ascii=False),
        encoding="utf-8",
    )
    invalid_local_result = audit_package(root)
    assert "BOUNDARY_FINAL_DELIVERY_ENDPOINT_INVALID" in {
        issue["code"] for issue in invalid_local_result["issues"]
    }
    record.write_text(valid_record, encoding="utf-8")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["cover_route_attestations"][0]["final_cover_sha256"] = (
        "sha256:" + "0" * 64
    )
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )
    cover_result = audit_package(root)
    assert cover_result["passed"] is False
    assert "MANIFEST_COVER_ATTESTATION_DRIFT" in {
        issue["code"] for issue in cover_result["issues"]
    }
    manifest["cover_route_attestations"][0]["final_cover_sha256"] = (
        recorded_generation["final_cover_sha256"]
    )
    manifest["items"][0]["title"] = "【李豆沙】旧标题"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )
    title_result = audit_package(root)
    assert title_result["passed"] is False
    assert "MANIFEST_ITEM_TITLE_DRIFT" in {
        issue["code"] for issue in title_result["issues"]
    }
    manifest["items"][0]["title"] = "【李豆沙】南町当面追问最最最最喜欢"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )

    srt.write_text(
        "1\n00:00:00,000 --> 00:00:04,000\n大恩老师说非常亚撒西\n",
        encoding="utf-8",
    )
    result = audit_package(root)
    codes = {issue["code"] for issue in result["issues"]}
    assert result["passed"] is False
    assert "STORY_CONTRACT_SUBTITLE_HASH_DRIFT" in codes
    assert "NANCHO_ALIAS_UNRESOLVED" in codes


def test_story_contract_is_mandatory_by_date_and_cover_alias_cannot_drift(
    tmp_path: Path,
):
    root = tmp_path / "pkg"
    root.mkdir()
    (root / "review_manifest.json").write_text(
        json.dumps(
            {
                "date": "2026-07-22",
                "status": "finished_review_package_no_upload_pending_human_review",
                "items": [{"stem": "legacy-without-contract"}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    result = audit_package(root)
    codes = {issue["code"] for issue in result["issues"]}
    assert result["passed"] is False
    assert "REVIEW_PACKAGE_UPLOAD_POLICY_INVALID" in codes
    assert "REVIEW_PACKAGE_RUN_MODE_INVALID" in codes
    assert "STORY_CONTRACT_RECORD_MISSING" in codes

    transcript = "南町nightin说非常亚撒西"
    srt = _write(
        root / "cover-drift.srt",
        f"1\n00:00:00,000 --> 00:00:04,000\n{transcript}\n",
    )
    scorecard = normalize_selection_scorecard(
        {
            "tier": 1,
            "tier_basis": "relationship_chain",
            "tier_reason": "联动关系与反转",
            "tier_evidence_cues": [1],
            "dimensions": {
                "lidousha_centrality": 4,
                "stance_intensity": 3,
                "audience_salience": 4,
                "relationship_interaction": 4,
                "persona_reversal": 3,
                "comedic_payoff": 3,
                "self_contained": 4,
            },
            "uncertainty_penalty": 0,
            "fatigue_penalty": 0,
        },
        start_cue=1,
        end_cue=1,
    )
    contract = build_story_contract(
        candidate_id="cover-drift",
        selection_hook="南町当面追问李豆沙",
        transcript_text=transcript,
        selection_scorecard=scorecard,
        session_relation_authority={
            "state": "CONFIRMED",
            "participants": ["lidousha", "nancho"],
        },
    )
    binding = {
        key: contract[key]
        for key in (
            "schema_version",
            "relation_state",
            "participants",
            "source_media_sha256s",
            "cover_fallback_mode",
        )
    }
    record = root / "cover-drift.record.json"
    record.write_text(
        json.dumps(
            {
                "story_contract": contract,
                "publish_staging": {
                    "cover_text": "大恩当面追问",
                    "cover_generation": {
                        "cover_text": "大恩当面追问",
                        "rendered_lines": ["大恩当面追问"],
                        "story_contract": binding,
                    },
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (root / "review_manifest.json").write_text(
        json.dumps(
            {
                "date": "2026-07-22",
                "run_mode": "RECOVERY_REVIEW",
                "upload_allowed": False,
                "items": [
                    {
                        "stem": "cover-drift",
                        "title": "【李豆沙】南町当面追问",
                        "subtitle_srt": srt.name,
                        "record": record.name,
                        "ai_cover_generated": True,
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    result = audit_package(root)
    codes = {issue["code"] for issue in result["issues"]}
    assert result["passed"] is False
    assert "NANCHO_ALIAS_UNRESOLVED" in codes


def test_audit_blocks_package_with_extended_invalid_review_draft_status(tmp_path: Path):
    root = tmp_path / "pkg"
    root.mkdir()
    (root / "review_manifest.json").write_text(
        json.dumps(
            {
                "status": "invalid_review_draft_song_boundary_subtitle_cover_failed",
                "items": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = audit_package(root)

    codes = {issue["code"] for issue in result["issues"]}
    assert "PACKAGE_MARKED_INVALID_REVIEW_DRAFT" in codes
    assert result["passed"] is False
    assert result["blocking_issue_count"] >= 1


def test_audit_blocks_package_with_invalid_redo_required_marker(tmp_path: Path):
    root = tmp_path / "pkg"
    root.mkdir()
    (root / "review_manifest.json").write_text(
        json.dumps({"status": "finished", "items": []}, ensure_ascii=False),
        encoding="utf-8",
    )
    (root / "INVALID_REDO_REQUIRED.json").write_text(
        json.dumps(
            {
                "status": "invalid_review_draft_song_boundary_subtitle_cover_failed",
                "reason": "Ivan review: source clip cuts the song",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = audit_package(root)

    codes = {issue["code"] for issue in result["issues"]}
    assert "PACKAGE_MARKED_INVALID_REDO_REQUIRED" in codes
    assert result["passed"] is False
