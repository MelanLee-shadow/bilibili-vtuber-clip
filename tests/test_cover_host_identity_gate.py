from __future__ import annotations

import hashlib
import json
from pathlib import Path

from PIL import Image

from src.autoslice.cover_host_identity_gate import (
    IVAN_SELF_INCONSISTENT_RULING,
    SELF_INCONSISTENT_DISREGARDED_STATUS,
    SELF_INCONSISTENT_REASON_CODE,
    SELF_INCONSISTENT_REFUSED_STATUS,
    disregard_self_inconsistent_host_identity_witness,
    final_host_identity_witness_unavailable,
    host_identity_verdict_contradictions,
    validate_final_host_identity_verification,
    verify_lidousha_final_host_identity,
)
from src.autoslice.cover_source_composition import (
    verify_lidousha_source_composition,
)


def _sha(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _fake_witness(answer: dict[str, object]):
    def probe(image_path: Path, _question: str, **_kwargs):
        return {
            "status": "OBSERVED",
            "provider": "cpa",
            "image_sha256": hashlib.sha256(
                Path(image_path).read_bytes()
            ).hexdigest(),
            "answer": json.dumps(answer, ensure_ascii=False),
        }

    return probe


def _source_composition_redraw(**kwargs):
    reference_path = Path(kwargs["reference_path"])

    def probe(image_path: Path, _question: str, **_probe_kwargs):
        return {
            "status": "OBSERVED",
            "provider": "cpa",
            "image_sha256": hashlib.sha256(
                Path(image_path).read_bytes()
            ).hexdigest(),
            "answer": json.dumps(
                {
                    "lidousha_bbox_frac": [0.79, 0.70, 0.96, 0.98],
                    "source_face_complete": True,
                    "faithful_crop_can_make_dominant": False,
                    "source_carries_story_reaction": False,
                    "cpa_redraw_recommended": True,
                    "reason": "主播只在角落，源图不承载标题反应",
                },
                ensure_ascii=False,
            ),
            "routing": {
                "preferred_provider": "cpa",
                "selected_provider": "cpa",
                "fallback_used": False,
            },
        }

    return verify_lidousha_source_composition(
        reference_path=reference_path,
        reference_sha256=str(kwargs["reference_sha256"]),
        story_hook=str(kwargs.get("story_hook") or ""),
        title=str(kwargs.get("title") or ""),
        image_probe=probe,
    )


def test_wrong_source_participant_as_protagonist_fails_closed(
    tmp_path, monkeypatch
):
    from src.autoslice import cpa_frame_witness

    reference = tmp_path / "source.png"
    final = tmp_path / "final.png"
    Image.new("RGB", (1920, 1080), (20, 30, 40)).save(reference)
    Image.new("RGB", (1920, 1080), (50, 60, 70)).save(final)
    monkeypatch.setattr(
        cpa_frame_witness,
        "image_vision_probe",
        _fake_witness(
            {
                "source_lidousha_located": True,
                "primary_subject_is_lidousha": False,
                "primary_subject_matches_other_source_participant": True,
                "primary_subject_is_visually_dominant": True,
                "primary_subject_face_is_large_and_clear": True,
                "primary_subject_carries_story_reaction": True,
                "excessive_dead_space": False,
                "meaningless_dominant_decoration": False,
                "thumbnail_has_clear_click_hook": True,
                "primary_subject_identity": "乙乙Sol",
                "identity_conflicts": ["红金角饰与乙乙一致"],
                "composition_conflicts": [],
                "reason": "主角沿用了乙乙而不是主播",
            }
        ),
    )

    receipt = verify_lidousha_final_host_identity(
        final_cover_path=final,
        final_cover_sha256=_sha(final),
        reference_path=reference,
    )

    assert receipt["status"] == "FAIL"
    assert receipt["reason_code"] == "FINAL_HOST_IDENTITY_MISMATCH"
    assert not validate_final_host_identity_verification(
        {
            "final_cover_sha256": _sha(final),
            "final_host_identity_verification": receipt,
        }
    )


def test_source_final_lidousha_identity_match_passes_and_binds_hash(
    tmp_path, monkeypatch
):
    from src.autoslice import cpa_frame_witness

    reference = tmp_path / "source.png"
    final = tmp_path / "final.png"
    Image.new("RGB", (1920, 1080), (20, 30, 40)).save(reference)
    Image.new("RGB", (1920, 1080), (80, 90, 100)).save(final)
    monkeypatch.setattr(
        cpa_frame_witness,
        "image_vision_probe",
        _fake_witness(
            {
                "source_lidousha_located": True,
                "primary_subject_is_lidousha": True,
                "primary_subject_matches_other_source_participant": False,
                "primary_subject_is_visually_dominant": True,
                "primary_subject_face_is_large_and_clear": True,
                "primary_subject_carries_story_reaction": True,
                "excessive_dead_space": False,
                "meaningless_dominant_decoration": False,
                "thumbnail_has_clear_click_hook": True,
                "primary_subject_identity": "主播",
                "identity_conflicts": [],
                "composition_conflicts": [],
                "reason": "主角与源图主播一致",
            }
        ),
    )

    receipt = verify_lidousha_final_host_identity(
        final_cover_path=final,
        final_cover_sha256=_sha(final),
        reference_path=reference,
    )
    generation = {
        "final_cover_sha256": _sha(final),
        "final_host_identity_verification": receipt,
    }

    assert receipt["status"] == "PASS"
    assert validate_final_host_identity_verification(generation)
    generation["final_cover_sha256"] = "sha256:" + "0" * 64
    assert not validate_final_host_identity_verification(generation)


def test_1411_small_lower_corner_lidousha_and_dead_space_fails_closed(
    tmp_path, monkeypatch
):
    """Identity alone cannot release the 7/26 1411-style bad thumbnail."""

    from src.autoslice import cpa_frame_witness

    reference = tmp_path / "source.png"
    final = tmp_path / "final.png"
    Image.new("RGB", (1920, 1080), (20, 30, 40)).save(reference)
    Image.new("RGB", (1920, 1080), (120, 18, 28)).save(final)
    monkeypatch.setattr(
        cpa_frame_witness,
        "image_vision_probe",
        _fake_witness(
            {
                "source_lidousha_located": True,
                "primary_subject_is_lidousha": True,
                "primary_subject_matches_other_source_participant": False,
                "primary_subject_is_visually_dominant": False,
                "primary_subject_face_is_large_and_clear": False,
                "primary_subject_carries_story_reaction": False,
                "excessive_dead_space": True,
                "meaningless_dominant_decoration": True,
                "thumbnail_has_clear_click_hook": False,
                "primary_subject_identity": "主播",
                "identity_conflicts": [],
                "composition_conflicts": [
                    "主播缩在右下角且主体过小",
                    "大片空白与无意义红条压过人物",
                ],
                "reason": "身份是主播，但主体不显眼且构图不值得点击",
            }
        ),
    )

    receipt = verify_lidousha_final_host_identity(
        final_cover_path=final,
        final_cover_sha256=_sha(final),
        reference_path=reference,
    )

    assert receipt["status"] == "FAIL"
    assert receipt["reason_code"] == "FINAL_COVER_SUBJECT_PROMINENCE_FAILED"
    assert receipt["final_cover_sha256"] == _sha(final)
    assert not validate_final_host_identity_verification(
        {
            "final_cover_sha256": _sha(final),
            "final_host_identity_verification": receipt,
        }
    )


def test_agy_identity_receipt_requires_disclosed_cpa_failure():
    digest = "b" * 64
    witness = {
        "status": "OBSERVED",
        "provider": "agy",
        "image_sha256": digest,
        "routing": {
            "preferred_provider": "cpa",
            "selected_provider": "agy",
            "fallback_used": True,
            "primary_status": "UNAVAILABLE",
            "primary_reason_code": "VISION_CALL_FAILED",
            "primary_receipt": {
                "status": "UNAVAILABLE",
                "provider": "cpa",
                "reason_code": "VISION_CALL_FAILED",
            },
        },
    }
    generation = {
        "final_cover_sha256": "sha256:" + "a" * 64,
        "final_host_identity_verification": {
            "schema_version": (
                "lidousha-cover-final-host-identity-verification.v3"
            ),
            "authority": (
                "CPA_PRIMARY_HASH_BOUND_SOURCE_FINAL_IDENTITY_AND_PROMINENCE_COMPARISON"
            ),
            "status": "PASS",
            "final_cover_sha256": "sha256:" + "a" * 64,
            "comparison_sha256": "sha256:" + digest,
            "witness": witness,
        },
    }

    assert validate_final_host_identity_verification(generation)
    witness["routing"].pop("primary_receipt")
    assert not validate_final_host_identity_verification(generation)


def test_cpa_redraw_blocks_before_ready_when_host_identity_is_wrong(
    tmp_path, monkeypatch
):
    from src.autoslice import publish_staging
    from tests.test_cover_frame_selection import (
        _write_synthetic_performance_clip,
    )

    media = _write_synthetic_performance_clip(tmp_path)
    monkeypatch.setenv("CPA_BASE_URL", "https://cpa.example.test/v1")
    monkeypatch.setenv("CPA_API_KEY", "test-key")
    monkeypatch.setenv("AUTOSLICE_COVER_MODE", "cpa")

    def fake_image_edit(**kwargs):
        Image.new("RGB", (1920, 1080), (90, 30, 70)).save(
            kwargs["output_path"]
        )
        return {
            "status": "AI_BACKGROUND_READY",
            "selected_model": "gpt-image-2",
            "attempted_models": ["gpt-image-2"],
        }

    def wrong_identity(**_kwargs):
        return {
            "schema_version": (
                "lidousha-cover-final-host-identity-verification.v3"
            ),
            "status": "FAIL",
            "reason_code": "FINAL_HOST_IDENTITY_MISMATCH",
        }

    result = publish_staging._stage_lidousha_ai_cover(
        {"status": "MATERIALIZED", "media_path": str(media)},
        media_path=media,
        candidate_id="pink-multi-person-regression",
        title="【主播】小姐姐布下迷魂阵，我是甲甲啊",
        cover_text="小姐姐布下迷魂阵\n我是甲甲啊",
        run_ffmpeg=True,
        image_edit=fake_image_edit,
        final_host_identity_verifier=wrong_identity,
        source_composition_verifier=_source_composition_redraw,
        enforce_final_host_identity=True,
    )

    assert result["status"] == "BLOCKED_AI_COVER_REQUIRED"
    assert result["reason_codes"] == [
        "COVER_FINAL_HOST_IDENTITY_UNVERIFIED"
    ]
    generation = result["cover_generation"]
    assert generation["route_decision"]["host_identity_required"] is True
    assert generation["route_decision"]["actual_treatment"] is None
    assert generation["route_decision"]["execution_status"] == "BLOCKED"


def test_cpa_redraw_does_not_ship_source_pixels_when_identity_witness_is_down(
    tmp_path, monkeypatch
):
    from src.autoslice import publish_staging
    from src.autoslice.cover_route_evidence import (
        validate_cover_route_decision,
    )
    from tests.test_cover_frame_selection import (
        _write_synthetic_performance_clip,
    )

    media = _write_synthetic_performance_clip(tmp_path)
    monkeypatch.setenv("CPA_BASE_URL", "https://cpa.example.test/v1")
    monkeypatch.setenv("CPA_API_KEY", "test-key")
    monkeypatch.setenv("AUTOSLICE_COVER_MODE", "cpa")
    image_calls = 0
    identity_calls = 0

    def fake_image_edit(**kwargs):
        nonlocal image_calls
        image_calls += 1
        Image.new("RGB", (1920, 1080), (90, 30, 70)).save(
            kwargs["output_path"]
        )
        return {
            "status": "AI_BACKGROUND_READY",
            "selected_model": "gpt-image-2",
            "attempted_models": ["gpt-image-2"],
        }

    def unavailable_identity(**_kwargs):
        nonlocal identity_calls
        identity_calls += 1
        return {
            "schema_version": (
                "lidousha-cover-final-host-identity-verification.v3"
            ),
            "status": "FAIL",
            "reason_code": "HOST_IDENTITY_WITNESS_UNAVAILABLE",
        }

    result = publish_staging._stage_lidousha_ai_cover(
        {"status": "MATERIALIZED", "media_path": str(media)},
        media_path=media,
        candidate_id="identity-witness-down",
        title="【主播】突然开起日语人称翻译大会",
        cover_text="日语人称翻译大会",
        run_ffmpeg=True,
        image_edit=fake_image_edit,
        final_host_identity_verifier=unavailable_identity,
        source_composition_verifier=_source_composition_redraw,
        enforce_final_host_identity=True,
    )

    assert result["status"] == "BLOCKED_AI_COVER_REQUIRED"
    assert image_calls == 1
    assert identity_calls == 1
    generation = result["cover_generation"]
    route = generation["route_decision"]
    assert generation["method"] == "images.edit"
    assert generation["cover_origin"] == "AI_REDRAW"
    assert "cpa_redraw" not in generation
    assert generation["source_composition_verification"]["verdict"][
        "cpa_redraw_recommended"
    ] is True
    assert route["selected_treatment"] == "cpa_redraw"
    assert route["actual_treatment"] is None
    assert route["execution_status"] == "BLOCKED"
    assert generation["status"] == "BLOCKED"
    assert result["reason_codes"] == ["COVER_FINAL_HOST_IDENTITY_UNVERIFIED"]
    assert not validate_cover_route_decision(generation)


def test_cpa_redraw_retries_once_and_recovers_host_identity(
    tmp_path, monkeypatch
):
    from src.autoslice import publish_staging
    from tests.test_cover_frame_selection import (
        _write_synthetic_performance_clip,
    )

    media = _write_synthetic_performance_clip(tmp_path)
    monkeypatch.setenv("CPA_BASE_URL", "https://cpa.example.test/v1")
    monkeypatch.setenv("CPA_API_KEY", "test-key")
    monkeypatch.setenv("AUTOSLICE_COVER_MODE", "cpa")
    image_calls = 0
    identity_calls = 0

    def fake_image_edit(**kwargs):
        nonlocal image_calls
        image_calls += 1
        Image.new("RGB", (1920, 1080), (30 * image_calls, 40, 80)).save(
            kwargs["output_path"]
        )
        return {
            "status": "AI_BACKGROUND_READY",
            "selected_model": "gpt-image-2",
            "attempted_models": ["gpt-image-2"],
        }

    def identity_verifier(*, final_cover_sha256: str, **_kwargs):
        nonlocal identity_calls
        identity_calls += 1
        if identity_calls == 1:
            return {
                "schema_version": (
                    "lidousha-cover-final-host-identity-verification.v3"
                ),
                "status": "FAIL",
                "reason_code": "FINAL_HOST_IDENTITY_MISMATCH",
            }
        comparison_hash = "b" * 64
        return {
            "schema_version": (
                "lidousha-cover-final-host-identity-verification.v3"
            ),
            "authority": (
                "CPA_PRIMARY_HASH_BOUND_SOURCE_FINAL_IDENTITY_AND_PROMINENCE_COMPARISON"
            ),
            "status": "PASS",
            "final_cover_sha256": final_cover_sha256,
            "comparison_sha256": "sha256:" + comparison_hash,
            "witness": {
                "status": "OBSERVED",
                "provider": "cpa",
                "image_sha256": comparison_hash,
            },
        }

    result = publish_staging._stage_lidousha_ai_cover(
        {"status": "MATERIALIZED", "media_path": str(media)},
        media_path=media,
        candidate_id="pink-multi-person-auto-recovery",
        title="【主播】小姐姐布下迷魂阵，我是甲甲啊",
        cover_text="小姐姐布下迷魂阵\n我是甲甲啊",
        run_ffmpeg=True,
        image_edit=fake_image_edit,
        final_host_identity_verifier=identity_verifier,
        source_composition_verifier=_source_composition_redraw,
        enforce_final_host_identity=True,
    )

    assert result["status"] == "AI_COVER_READY"
    assert image_calls == identity_calls == 2
    generation = result["cover_generation"]
    assert generation["host_identity_retry"]["status"] == "PASS"
    assert generation["final_host_identity_verification"]["status"] == "PASS"
    assert generation["route_decision"]["execution_status"] == "READY"


def test_1411_prominence_failure_retries_with_stronger_prompt_then_stays_pending(
    tmp_path, monkeypatch
):
    from src.autoslice import publish_staging
    from tests.test_cover_frame_selection import (
        _write_synthetic_performance_clip,
    )

    media = _write_synthetic_performance_clip(tmp_path)
    monkeypatch.setenv("CPA_BASE_URL", "https://cpa.example.test/v1")
    monkeypatch.setenv("CPA_API_KEY", "test-key")
    monkeypatch.setenv("AUTOSLICE_COVER_MODE", "cpa")
    prompts: list[str] = []
    verification_calls = 0

    def fake_image_edit(**kwargs):
        prompts.append(str(kwargs["prompt"]))
        Image.new("RGB", (1920, 1080), (115, 20, 30)).save(
            kwargs["output_path"]
        )
        return {
            "status": "AI_BACKGROUND_READY",
            "selected_model": "gpt-image-2",
            "attempted_models": ["gpt-image-2"],
        }

    def small_corner_identity_match(**_kwargs):
        nonlocal verification_calls
        verification_calls += 1
        return {
            "schema_version": (
                "lidousha-cover-final-host-identity-verification.v3"
            ),
            "status": "FAIL",
            "reason_code": "FINAL_COVER_SUBJECT_PROMINENCE_FAILED",
            "detail": "主播缩在右下角，大片空白和无意义红条压过主体",
        }

    result = publish_staging._stage_lidousha_ai_cover(
        {"status": "MATERIALIZED", "media_path": str(media)},
        media_path=media,
        candidate_id="auto_152944_1411_1463",
        title="【主播】刚解释完为什么被电，话音刚落小主就暴毙",
        cover_text="话音刚落\n小主暴毙",
        run_ffmpeg=True,
        image_edit=fake_image_edit,
        final_host_identity_verifier=small_corner_identity_match,
        source_composition_verifier=_source_composition_redraw,
        enforce_final_host_identity=True,
    )

    assert result["status"] == "BLOCKED_AI_COVER_REQUIRED"
    assert result["reason_codes"] == [
        "COVER_FINAL_HOST_IDENTITY_UNVERIFIED"
    ]
    assert len(prompts) == verification_calls == 2
    assert "MANDATORY SUBJECT PROMINENCE" in prompts[0]
    assert "Never shrink her into a corner" in prompts[0]
    assert "Never place her as a small lower-corner figure" in prompts[1]
    assert "meaningless solid-color/red bars" in prompts[1]
    generation = result["cover_generation"]
    assert generation["host_identity_retry"]["status"] == (
        "FAILED_FINAL_IDENTITY"
    )
    assert generation["route_decision"]["actual_treatment"] is None
    assert generation["route_decision"]["execution_status"] == "BLOCKED"


def _published_carry_verification() -> dict[str, object]:
    return {
        "schema_version": (
            "lidousha-cover-final-host-identity-verification.v2"
        ),
        "authority": (
            "CPA_PRIMARY_HASH_BOUND_SOURCE_FINAL_IDENTITY_COMPARISON"
        ),
        "status": "PASS",
        "final_cover_sha256": "sha256:" + "a" * 64,
        "comparison_sha256": "sha256:" + "b" * 64,
        "witness": {"provider": "cpa", "image_sha256": "b" * 64},
    }


def _published_carry_generation() -> dict[str, object]:
    return {
        "carried_forward_from_published_record": True,
        "final_cover_sha256": "sha256:" + "a" * 64,
        "final_host_identity_verification": _published_carry_verification(),
    }


def test_published_carry_v2_witness_accepted_for_byte_identical_reuse():
    assert validate_final_host_identity_verification(
        _published_carry_generation()
    )


def test_published_carry_clause_requires_carry_flag():
    generation = _published_carry_generation()
    generation.pop("carried_forward_from_published_record")
    assert not validate_final_host_identity_verification(generation)


def test_published_carry_clause_pins_exact_v2_generation_pair():
    for key, value in (
        ("schema_version", "lidousha-cover-final-host-identity-verification.v1"),
        (
            "authority",
            "CPA_PRIMARY_HASH_BOUND_SOURCE_FINAL_IDENTITY_AND_PROMINENCE_COMPARISON",
        ),
    ):
        generation = _published_carry_generation()
        generation["final_host_identity_verification"][key] = value
        assert not validate_final_host_identity_verification(generation)


def test_published_carry_clause_keeps_hash_and_status_gates():
    mismatched = _published_carry_generation()
    mismatched["final_cover_sha256"] = "sha256:" + "c" * 64
    assert not validate_final_host_identity_verification(mismatched)

    failed = _published_carry_generation()
    failed["final_host_identity_verification"]["status"] = "FAIL"
    assert not validate_final_host_identity_verification(failed)


# --------------------------------------------------------------------------
# Ivan 2026-08-10 亲裁：自相矛盾的见证「完全没有否决权，完全不可信」。
# 1323 打歌服置换封面 v4D 案：同一份回答里 primary_subject_is_lidousha=true
# + identity_conflicts=[] + 文字认出李豆沙，却又
# primary_subject_matches_other_source_participant=true。合成 fixture，真值盲。
# --------------------------------------------------------------------------

V4D_SHAPED_VERDICT: dict[str, object] = {
    "source_lidousha_located": True,
    "primary_subject_is_lidousha": True,
    "primary_subject_matches_other_source_participant": True,
    "primary_subject_is_visually_dominant": True,
    "primary_subject_face_is_large_and_clear": True,
    "primary_subject_carries_story_reaction": True,
    "excessive_dead_space": False,
    "meaningless_dominant_decoration": False,
    "thumbnail_has_clear_click_hook": True,
    "primary_subject_identity": "李豆沙",
    "identity_conflicts": [],
    "composition_conflicts": [],
    "reason": "主角是白发熊猫耳的李豆沙，被另一名参与者横抱",
}

EXPLICIT_NEGATION_VERDICT: dict[str, object] = {
    **V4D_SHAPED_VERDICT,
    "primary_subject_is_lidousha": False,
    "primary_subject_identity": "另一位嘉宾",
    "reason": "主角不是李豆沙",
}


def _identity_receipt(
    tmp_path: Path, monkeypatch, verdict: dict[str, object]
) -> tuple[dict[str, object], Path]:
    from src.autoslice import cpa_frame_witness

    reference = tmp_path / "source.png"
    final = tmp_path / "final.cover.png"
    Image.new("RGB", (1920, 1080), (20, 30, 40)).save(reference)
    Image.new("RGB", (1920, 1080), (33, 44, 55)).save(final)
    monkeypatch.setattr(
        cpa_frame_witness, "image_vision_probe", _fake_witness(verdict)
    )
    receipt = verify_lidousha_final_host_identity(
        final_cover_path=final,
        final_cover_sha256=_sha(final),
        reference_path=reference,
    )
    return receipt, final


def _joint_qc_receipt_file(
    tmp_path: Path, cover: Path, **overrides: object
) -> Path:
    """A PASS title+cover joint-QC receipt bound byte-for-byte to ``cover``."""

    title = "【李豆沙】打歌服没召唤出来，公主抱倒是先来了"
    verdict = {
        "lidousha_primary": True,
        "thumbnail_readable": True,
        "single_clear_hook": True,
        "text_overcrowded": False,
        "title_cover_aligned": True,
        "physical_text_line_count": 2,
        "unrelated_or_misleading_elements": [],
        "reason": "主体是李豆沙，两行大字清晰，与标题同一件事",
        "pass": True,
    }
    receipt: dict[str, object] = {
        "schema_version": "lidousha-title-cover-joint-qc.v1",
        "candidate_id": "auto_200130_1323_1603",
        "title": title,
        "title_sha256": "sha256:"
        + hashlib.sha256(title.encode("utf-8")).hexdigest(),
        "cover_path": str(cover.resolve()),
        "cover_sha256": _sha(cover),
        "preferred_provider": "cpa",
        "selected_provider": "cpa",
        "witness": {
            "schema_version": "cpa-frame-witness.v1",
            "provider": "cpa",
            "status": "OBSERVED",
            "model": "gemini-3.6-flash",
            "image_path": str(cover.resolve()),
            "image_sha256": _sha(cover),
            "answer": json.dumps(verdict, ensure_ascii=False),
        },
        "verdict": verdict,
        "status": "PASS",
        "pass": True,
    }
    receipt.update(overrides)
    path = tmp_path / "title-cover-joint-qc.json"
    path.write_text(json.dumps(receipt, ensure_ascii=False), encoding="utf-8")
    return path


def test_1323_v4d_shaped_witness_is_self_inconsistent_and_neither_vetoes_nor_passes(
    tmp_path, monkeypatch
):
    receipt, final = _identity_receipt(tmp_path, monkeypatch, V4D_SHAPED_VERDICT)
    generation = {
        "final_cover": str(final),
        "final_cover_sha256": _sha(final),
        "final_host_identity_verification": receipt,
    }

    assert receipt["status"] == "FAIL"
    assert receipt["reason_code"] == SELF_INCONSISTENT_REASON_CODE
    disclosure = receipt["self_inconsistent_witness"]
    assert disclosure["status"] == SELF_INCONSISTENT_REFUSED_STATUS
    assert disclosure["reason_code"] == "CORROBORATING_EVIDENCE_MISSING"
    assert disclosure["authority"] == IVAN_SELF_INCONSISTENT_RULING
    assert [item["name"] for item in disclosure["contradictions"]] == [
        "PROTAGONIST_IS_HOST_AND_OTHER_PARTICIPANT"
    ]
    # 原文保留，不得改写或删除。
    assert disclosure["disregarded_verdict"] == V4D_SHAPED_VERDICT
    assert json.loads(disclosure["disregarded_witness_answer"]) == V4D_SHAPED_VERDICT
    assert receipt["verdict"] == V4D_SHAPED_VERDICT
    # 没有否决权 ≠ 可以通过：缺承接证据仍然 fail-closed。
    assert not validate_final_host_identity_verification(generation)
    # 自不一致不是见证缺席，不得走"见证掉线→降级发源截图"那条路。
    assert not final_host_identity_witness_unavailable(generation)


def test_self_inconsistent_witness_is_disregarded_when_joint_qc_carries_the_call(
    tmp_path, monkeypatch
):
    receipt, final = _identity_receipt(tmp_path, monkeypatch, V4D_SHAPED_VERDICT)
    generation = {
        "final_cover": str(final),
        "final_cover_sha256": _sha(final),
        "final_host_identity_verification": receipt,
    }
    qc_path = _joint_qc_receipt_file(tmp_path, final)

    disclosure = disregard_self_inconsistent_host_identity_witness(
        generation, joint_qc_receipt_path=qc_path
    )

    assert disclosure["status"] == SELF_INCONSISTENT_DISREGARDED_STATUS
    evidence = disclosure["corroborating_evidence"]
    assert evidence["kind"] == "title_cover_joint_qc"
    assert evidence["receipt_path"] == str(qc_path)
    assert evidence["receipt_sha256"] == "sha256:" + hashlib.sha256(
        qc_path.read_bytes()
    ).hexdigest()
    assert evidence["cover_sha256"] == _sha(final)
    assert disclosure["disregarded_verdict"] == V4D_SHAPED_VERDICT
    assert validate_final_host_identity_verification(generation)
    # 顶层仍是 FAIL/自不一致——门放行不等于把矛盾洗成 PASS。
    assert receipt["status"] == "FAIL"
    assert receipt["reason_code"] == SELF_INCONSISTENT_REASON_CODE
    assert not final_host_identity_witness_unavailable(generation)


def test_explicit_identity_negation_keeps_full_veto_even_with_perfect_joint_qc(
    tmp_path, monkeypatch
):
    """primary_subject_is_lidousha=False 是明确否定，不是矛盾。本机制不得放行。"""

    receipt, final = _identity_receipt(
        tmp_path, monkeypatch, EXPLICIT_NEGATION_VERDICT
    )
    generation = {
        "final_cover": str(final),
        "final_cover_sha256": _sha(final),
        "final_host_identity_verification": receipt,
    }
    qc_path = _joint_qc_receipt_file(tmp_path, final)

    assert receipt["status"] == "FAIL"
    assert receipt["reason_code"] == "FINAL_HOST_IDENTITY_MISMATCH"
    assert "self_inconsistent_witness" not in receipt
    assert host_identity_verdict_contradictions(EXPLICIT_NEGATION_VERDICT) == []

    disclosure = disregard_self_inconsistent_host_identity_witness(
        generation, joint_qc_receipt_path=qc_path
    )
    assert disclosure["status"] == SELF_INCONSISTENT_REFUSED_STATUS
    assert disclosure["reason_code"] == "SELF_INCONSISTENCY_ABSENT"
    assert not validate_final_host_identity_verification(generation)


def test_nonempty_identity_conflicts_is_a_negation_not_a_contradiction(
    tmp_path, monkeypatch
):
    """列得出冲突特征就是可读的反对意见，保留完整否决权。"""

    verdict = {
        **V4D_SHAPED_VERDICT,
        "identity_conflicts": ["主角的角饰与另一位参与者一致"],
    }
    receipt, final = _identity_receipt(tmp_path, monkeypatch, verdict)
    generation = {
        "final_cover": str(final),
        "final_cover_sha256": _sha(final),
        "final_host_identity_verification": receipt,
    }
    qc_path = _joint_qc_receipt_file(tmp_path, final)

    assert receipt["reason_code"] == "FINAL_HOST_IDENTITY_MISMATCH"
    assert host_identity_verdict_contradictions(verdict) == []
    disclosure = disregard_self_inconsistent_host_identity_witness(
        generation, joint_qc_receipt_path=qc_path
    )
    assert disclosure["reason_code"] == "SELF_INCONSISTENCY_ABSENT"
    assert not validate_final_host_identity_verification(generation)


def test_source_location_failure_is_not_a_contradiction():
    """跨轴张力不入枚举：源图定位失败在身份轴上是明确否定。"""

    assert (
        host_identity_verdict_contradictions(
            {**V4D_SHAPED_VERDICT, "source_lidousha_located": False}
        )
        == []
    )


def test_composition_conflicts_alone_never_trigger_the_disregard():
    """构图轴没有互为否定的字段对，列出构图问题不算自相矛盾。"""

    assert (
        host_identity_verdict_contradictions(
            {
                **V4D_SHAPED_VERDICT,
                "primary_subject_matches_other_source_participant": False,
                "composition_conflicts": ["主体偏小"],
                "excessive_dead_space": True,
            }
        )
        == []
    )


def test_disregard_refuses_every_unbound_joint_qc_receipt(tmp_path, monkeypatch):
    receipt, final = _identity_receipt(tmp_path, monkeypatch, V4D_SHAPED_VERDICT)
    other = tmp_path / "other.cover.png"
    Image.new("RGB", (1920, 1080), (7, 7, 7)).save(other)

    def fresh_generation() -> dict[str, object]:
        return {
            "final_cover": str(final),
            "final_cover_sha256": _sha(final),
            "final_host_identity_verification": json.loads(json.dumps(receipt)),
        }

    missing = fresh_generation()
    disclosure = disregard_self_inconsistent_host_identity_witness(
        missing, joint_qc_receipt_path=tmp_path / "nope.json"
    )
    assert disclosure["reason_code"] == "CORROBORATING_INPUT_UNREADABLE"
    assert not validate_final_host_identity_verification(missing)

    for label, overrides in (
        ("failed", {"status": "FAIL", "pass": False}),
        ("not_pass", {"pass": False}),
        ("other_cover_bytes", {"cover_sha256": _sha(other)}),
        ("unknown_generation", {"schema_version": "lidousha-title-cover-joint-qc.v0"}),
        ("verdict_rewritten", {"verdict": {"lidousha_primary": True, "pass": True}}),
        ("agy_provider", {"selected_provider": "agy"}),
    ):
        generation = fresh_generation()
        qc_path = _joint_qc_receipt_file(tmp_path, final, **overrides)
        disclosure = disregard_self_inconsistent_host_identity_witness(
            generation, joint_qc_receipt_path=qc_path
        )
        assert disclosure["reason_code"] == "CORROBORATING_RECEIPT_UNBOUND", label
        assert not validate_final_host_identity_verification(generation), label

    unparseable = fresh_generation()
    bad = tmp_path / "bad.json"
    bad.write_text("not json", encoding="utf-8")
    disclosure = disregard_self_inconsistent_host_identity_witness(
        unparseable, joint_qc_receipt_path=bad
    )
    assert disclosure["reason_code"] == "CORROBORATING_RECEIPT_UNPARSEABLE"
    assert not validate_final_host_identity_verification(unparseable)


def test_disregard_branch_still_requires_the_full_hash_bound_witness_call(
    tmp_path, monkeypatch
):
    """免的只是那一票，不是整套绑定——否则就成了"没有见证也能过"的侧门。"""

    receipt, final = _identity_receipt(tmp_path, monkeypatch, V4D_SHAPED_VERDICT)
    qc_path = _joint_qc_receipt_file(tmp_path, final)

    def released() -> dict[str, object]:
        generation = {
            "final_cover": str(final),
            "final_cover_sha256": _sha(final),
            "final_host_identity_verification": json.loads(json.dumps(receipt)),
        }
        disregard_self_inconsistent_host_identity_witness(
            generation, joint_qc_receipt_path=qc_path
        )
        assert validate_final_host_identity_verification(generation)
        return generation

    for mutate in (
        lambda g: g.__setitem__("final_cover_sha256", "sha256:" + "e" * 64),
        lambda g: g["final_host_identity_verification"].__setitem__(
            "comparison_sha256", "sha256:" + "f" * 64
        ),
        lambda g: g["final_host_identity_verification"]["witness"].__setitem__(
            "image_sha256", "0" * 64
        ),
        lambda g: g["final_host_identity_verification"]["witness"].__setitem__(
            "provider", "agy"
        ),
        lambda g: g["final_host_identity_verification"].__setitem__(
            "schema_version", "lidousha-cover-final-host-identity-verification.v2"
        ),
        lambda g: g["final_host_identity_verification"].pop("witness"),
        lambda g: g["final_host_identity_verification"][
            "self_inconsistent_witness"
        ].__setitem__("status", SELF_INCONSISTENT_REFUSED_STATUS),
        lambda g: g["final_host_identity_verification"][
            "self_inconsistent_witness"
        ].__setitem__("authority", "我自己批的"),
        lambda g: g["final_host_identity_verification"][
            "self_inconsistent_witness"
        ]["corroborating_evidence"].pop("receipt"),
    ):
        generation = released()
        mutate(generation)
        assert not validate_final_host_identity_verification(generation)


def test_validator_rederives_the_contradiction_from_the_preserved_answer(
    tmp_path, monkeypatch
):
    """不许把"明确否定"改写成矛盾形状再走本分支：verdict 必须逐字等于回答原文。"""

    receipt, final = _identity_receipt(
        tmp_path, monkeypatch, EXPLICIT_NEGATION_VERDICT
    )
    generation = {
        "final_cover": str(final),
        "final_cover_sha256": _sha(final),
        "final_host_identity_verification": receipt,
    }
    # 改写 verdict 成矛盾形状，但见证回答原文仍是明确否定。
    receipt["reason_code"] = SELF_INCONSISTENT_REASON_CODE
    receipt["verdict"] = dict(V4D_SHAPED_VERDICT)
    qc_path = _joint_qc_receipt_file(tmp_path, final)
    disclosure = disregard_self_inconsistent_host_identity_witness(
        generation, joint_qc_receipt_path=qc_path
    )
    assert disclosure["reason_code"] == "VERDICT_IS_NOT_THE_PARSED_WITNESS_ANSWER"
    assert not validate_final_host_identity_verification(generation)

    # 绕过 builder，直接手工塞一份"合规形状"的放行披露：validator 必须独立
    # 重算矛盾并发现 verdict 与回答原文不符。
    good = _joint_qc_receipt_file(tmp_path, final)
    receipt["self_inconsistent_witness"] = {
        "schema_version": (
            "lidousha-cover-host-identity-self-inconsistent-witness.v1"
        ),
        "status": SELF_INCONSISTENT_DISREGARDED_STATUS,
        "authority": IVAN_SELF_INCONSISTENT_RULING,
        "contradictions": host_identity_verdict_contradictions(V4D_SHAPED_VERDICT),
        "disregarded_verdict": dict(V4D_SHAPED_VERDICT),
        "corroborating_evidence": {
            "kind": "title_cover_joint_qc",
            "receipt_path": str(good),
            "receipt_sha256": "sha256:"
            + hashlib.sha256(good.read_bytes()).hexdigest(),
            "schema_version": "lidousha-title-cover-joint-qc.v1",
            "cover_sha256": _sha(final),
            "receipt": json.loads(good.read_text(encoding="utf-8")),
        },
    }
    assert not validate_final_host_identity_verification(generation)


def test_disregard_refuses_when_final_cover_bytes_drifted(tmp_path, monkeypatch):
    receipt, final = _identity_receipt(tmp_path, monkeypatch, V4D_SHAPED_VERDICT)
    generation = {
        "final_cover": str(final),
        "final_cover_sha256": "sha256:" + "d" * 64,
        "final_host_identity_verification": receipt,
    }
    disclosure = disregard_self_inconsistent_host_identity_witness(
        generation, joint_qc_receipt_path=_joint_qc_receipt_file(tmp_path, final)
    )
    assert disclosure["reason_code"] == "FINAL_COVER_BYTES_DRIFTED"
    assert not validate_final_host_identity_verification(generation)


def test_disregard_lane_never_rides_the_v2_published_carry_clause(
    tmp_path, monkeypatch
):
    """冻结出版结转(v2)只结转 PASS 的既成证据，不许借道自不一致豁免。"""

    receipt, final = _identity_receipt(tmp_path, monkeypatch, V4D_SHAPED_VERDICT)
    generation = {
        "final_cover": str(final),
        "final_cover_sha256": _sha(final),
        "final_host_identity_verification": receipt,
    }
    disregard_self_inconsistent_host_identity_witness(
        generation, joint_qc_receipt_path=_joint_qc_receipt_file(tmp_path, final)
    )
    assert validate_final_host_identity_verification(generation)

    # 同一份"矛盾+已挂承接证据"的回执，改挂成 v2 结转世代 → 必须红。
    generation["carried_forward_from_published_record"] = True
    receipt["schema_version"] = (
        "lidousha-cover-final-host-identity-verification.v2"
    )
    receipt["authority"] = (
        "CPA_PRIMARY_HASH_BOUND_SOURCE_FINAL_IDENTITY_COMPARISON"
    )
    assert not validate_final_host_identity_verification(generation)
