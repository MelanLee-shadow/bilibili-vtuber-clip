"""截图优先、重绘兜底的回归金丝雀。

设计稿：内部设计文档留存（cover-route-screenshot-first-forensics）D 节。
六条一一对应：

1. 负向 · BV1E93L6rErV「1411 角落小人」——**P1 不许放开的边界**；
2. 负向 · 空面板游戏 UI——放宽游戏场后仍必须被拦；
3. 保真 · 非游戏 talk 的两份提问逐字节等价（prompt sha 钉死）；
4. 正向 · 8/7–8/8 被 ``SOURCE_COMPOSITION_CROP_NOT_AUTHORIZED`` 打掉的真实形状必须放行；
5. 降级 · 先落"全幅不裁海报"而不是直接重绘；
6. 可达 · 小窗分支命中 ≥1。

全部合成 fixture，零 provider 调用：见证一律用注入的 ``image_probe`` /
``final_host_identity_verifier`` seam。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from PIL import Image

from src.autoslice import publish_staging
from src.autoslice.cover_generation import LidoushaCoverArtDirection
from src.autoslice.cover_scene_binding import (
    cover_scene_kind,
    source_composition_scene_kwargs,
)
from src.autoslice.cover_source_composition import (
    GAME_SCENE,
    TALK_SCENE,
    _QUESTION_PREFIX,
    extract_authority_source_crop,
    extract_authority_source_crop_or_full_frame,
    resolve_cover_scene_kind,
    source_composition_recommends_redraw,
    source_composition_scene_kind,
    source_composition_supports_subject,
    validate_source_composition_verification,
    verify_source_composition,
)


# ─────────────────────────── 共用 fixture 工具 ───────────────────────────

def _sha(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _probe(verdict: dict[str, object]):
    """A CPA-shaped witness double.  No network, no CPA, no AGY."""

    def probe(image_path: Path, question: str, **_kwargs):
        probe.last_question = question
        return {
            "status": "OBSERVED",
            "provider": "cpa",
            "image_sha256": hashlib.sha256(Path(image_path).read_bytes()).hexdigest(),
            "answer": json.dumps(verdict, ensure_ascii=False),
            "routing": {
                "preferred_provider": "cpa",
                "selected_provider": "cpa",
                "fallback_used": False,
            },
        }

    probe.last_question = ""
    return probe


def _reference(tmp_path: Path) -> Path:
    reference = tmp_path / "reference.png"
    Image.new("RGB", (1920, 1080), (15, 25, 35)).save(reference)
    return reference


def _verified(reference: Path, verdict: dict[str, object], **kwargs) -> dict[str, object]:
    verification = verify_source_composition(
        reference_path=reference,
        reference_sha256=_sha(reference),
        story_hook="她刚说完就被电",
        title="【主播】话音刚落小主暴毙",
        image_probe=_probe(verdict),
        **kwargs,
    )
    assert verification["status"] == "PASS", verification
    assert validate_source_composition_verification(
        verification, reference_sha256=_sha(reference)
    )
    return verification


def _receipt(tmp_path: Path, verification: dict[str, object]) -> dict[str, str]:
    path = tmp_path / "source-composition.json"
    path.write_text(
        json.dumps(verification, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {"path": str(path), "sha256": _sha(path)}


def _art_direction() -> LidoushaCoverArtDirection:
    return LidoushaCoverArtDirection(
        role="shy_cute_default",
        expression_en="shocked",
        background_style="cobalt-comic-burst",
        layout="banner",
        hook_color="yellow",
        is_song=False,
        cover_punch=("小主暴毙",),
    )


# 8/7–8/8 两场 5 条被 CROP_NOT_AUTHORIZED 打掉的候选，共同的真实 verdict 形状：
# 两个几何布尔为真、故事反应为假，因此 `_verdict_is_coherent` 强制
# cpa_redraw_recommended=True（报告 B3「死因唯一」的机器复刻）。
_REAL_8_8_DEMOTED_VERDICT = {
    "lidousha_bbox_frac": [0.36, 0.10, 0.74, 0.92],
    "source_face_complete": True,
    "faithful_crop_can_make_dominant": True,
    "source_carries_story_reaction": False,
    "cpa_redraw_recommended": True,
    "reason": (
        "…脸部完整，上半身…均可紧裁并放大为第一主体；但她呈闭眼微笑挥手姿态，"
        "持麦手没有可辨识的颤抖，也无高铁回想或紧张追梦的反应依据。"
    ),
}

# BV1E93L6rErV：她只是右下角小头像，忠实裁切也成不了大主体。
_1411_CORNER_VERDICT = {
    "lidousha_bbox_frac": [0.79, 0.70, 0.96, 0.98],
    "source_face_complete": True,
    "faithful_crop_can_make_dominant": False,
    "source_carries_story_reaction": False,
    "cpa_redraw_recommended": True,
    "reason": "主播只在右下角，16:9 裁切仍带入大量游戏 UI，无法成为大号第一主体",
}


def _route(
    *,
    verification: dict[str, object] | None,
    frame_selection: dict[str, object],
    cover_mode: str = "auto",
    source_frame_size: tuple[int, int] | None = None,
    story_contract: object = None,
    reference_authority: dict[str, object] | None = None,
) -> tuple[str, dict[str, object]]:
    cover_generation: dict[str, object] = {}
    if source_frame_size is not None:
        cover_generation["reference_frame_size"] = source_frame_size
    return publish_staging._build_lidousha_cover_route(
        cover_generation=cover_generation,
        story_contract=story_contract,
        title="【主播】话音刚落小主暴毙",
        cover_text="话音刚落\n小主暴毙",
        cover_mode=cover_mode,
        art_direction=_art_direction(),
        punch_allowed=True,
        frame_selection=frame_selection,
        reference_authority=reference_authority,
        source_composition_verification=verification,
        enforce_final_host_identity=True,
    )


# ───────────────── ① 负向：1411 角落小人（P1 的边界） ─────────────────

def test_canary_1_1411_corner_avatar_still_routes_to_redraw(tmp_path):
    """P1 收窄授权式**不得**把角落小人放进截图路线。"""

    reference = _reference(tmp_path)
    verification = _verified(reference, _1411_CORNER_VERDICT)

    assert source_composition_recommends_redraw(verification) is True
    assert source_composition_supports_subject(verification) is False

    treatment, route = _route(
        verification=verification,
        frame_selection={
            "status": "SELECTED",
            "best_ms": 30_000,
            # 全批最高分也不许翻案：否决在分数之前。
            "candidates": [{"score": 8.17, "emotion": 1.0}],
            "subject_confident": False,
            "motion_dispersion_frac": 0.651,
        },
    )
    assert treatment == "cpa_redraw"
    assert route["source_composition_redraw_recommended"] is True

    # 强制 screenshot/polish 模式同样不能绕过几何否决。
    for forced in ("screenshot", "polish"):
        forced_treatment, _ = _route(
            verification=verification,
            frame_selection={
                "status": "SELECTED",
                "best_ms": 30_000,
                "candidates": [{"score": 8.17, "emotion": 1.0}],
                "subject_confident": False,
                "motion_dispersion_frac": 0.651,
            },
            cover_mode=forced,
        )
        assert forced_treatment == "cpa_redraw", forced


def test_canary_1b_final_pixel_gate_still_rejects_a_corner_avatar_cover(tmp_path):
    """裁切放开后，角落小人由**事后像素门**承担否决（D0 的替代保护）。"""

    from src.autoslice.cover_host_identity_gate import (
        verify_final_host_identity,
    )
    import src.autoslice.visual_witness as visual_witness

    reference = _reference(tmp_path)
    final_cover = tmp_path / "final.png"
    Image.new("RGB", (1920, 1080), (30, 40, 50)).save(final_cover)

    corner_verdict = {
        "source_lidousha_located": True,
        "primary_subject_is_lidousha": True,
        "primary_subject_matches_other_source_participant": False,
        "primary_subject_is_visually_dominant": False,
        "primary_subject_face_is_large_and_clear": False,
        "primary_subject_carries_story_reaction": True,
        "excessive_dead_space": False,
        "meaningless_dominant_decoration": False,
        "thumbnail_has_clear_click_hook": True,
        "primary_subject_identity": "李豆沙",
        "identity_conflicts": [],
        "composition_conflicts": ["主体过小/角落小人"],
        "reason": "她缩在右下角，需要寻找才能发现",
    }
    monkey = _probe(corner_verdict)
    original = visual_witness.image_vision_probe
    visual_witness.image_vision_probe = monkey
    try:
        verification = verify_final_host_identity(
            final_cover_path=final_cover,
            final_cover_sha256=_sha(final_cover),
            reference_path=reference,
        )
    finally:
        visual_witness.image_vision_probe = original

    assert verification["status"] == "FAIL"
    assert verification["reason_code"] == "FINAL_COVER_SUBJECT_PROMINENCE_FAILED"


# ───────────────── ② 负向：7/22 空面板游戏 UI ─────────────────

def test_canary_2_empty_game_panel_is_vetoed_in_both_scenes(tmp_path):
    """空面板：talk 判据下重绘；标成游戏场后由 frame_is_interesting 拦下。"""

    reference = _reference(tmp_path)

    # 非游戏场判定：几何否决照旧（她不在画面里能被忠实裁大）。
    talk_verification = _verified(
        reference,
        {
            "lidousha_bbox_frac": [0.86, 0.78, 0.98, 0.97],
            "source_face_complete": True,
            "faithful_crop_can_make_dominant": False,
            "source_carries_story_reaction": False,
            "cpa_redraw_recommended": True,
            "reason": "只有空的游戏面板和角落两个小头像",
        },
    )
    talk_treatment, _ = _route(
        verification=talk_verification,
        frame_selection={
            "status": "SELECTED",
            "best_ms": 30_000,
            "candidates": [{"score": 4.47, "emotion": 0.0}],
            "subject_confident": False,
            "motion_dispersion_frac": 0.6033,
            "camera_window_bbox_frac": [0.80, 0.72, 0.99, 0.99],
        },
    )
    assert talk_treatment == "cpa_redraw"

    # 游戏场：小窗可见但画面无聊 → 见证自己判重绘，路由否决。
    game_verification = _verified(
        reference,
        {
            "lidousha_bbox_frac": [0.80, 0.72, 0.99, 0.99],
            "host_window_visible": True,
            "frame_is_interesting": False,
            "cpa_redraw_recommended": True,
            "reason": "空的等待面板，没有战况、结算或任何事件",
        },
        scene_kind=GAME_SCENE,
    )
    assert source_composition_scene_kind(game_verification) == GAME_SCENE
    assert source_composition_recommends_redraw(game_verification) is True
    game_treatment, _ = _route(
        verification=game_verification,
        frame_selection={
            "status": "SELECTED",
            "best_ms": 30_000,
            "candidates": [{"score": 4.47, "emotion": 0.0}],
            "subject_confident": False,
            "motion_dispersion_frac": 0.6033,
            "camera_window_bbox_frac": [0.80, 0.72, 0.99, 0.99],
        },
    )
    assert game_treatment == "cpa_redraw"

    # 就算路由被绕过，物化端也不授权。
    with pytest.raises(ValueError, match="SOURCE_COMPOSITION_CROP_NOT_AUTHORIZED"):
        extract_authority_source_crop(
            reference_path=reference,
            output_path=tmp_path / "base.png",
            frame_ms=1,
            verification=game_verification,
            verification_receipt_path=tmp_path / "r.json",
            verification_receipt_sha256="sha256:" + "0" * 64,
        )


def test_canary_2b_final_game_gate_rejects_an_empty_panel(tmp_path):
    """游戏场终检没有放松反垃圾：空面板 / 无点击钩子照样 FAIL。"""

    from src.autoslice.cover_host_identity_gate import (
        verify_final_host_identity,
    )
    import src.autoslice.visual_witness as visual_witness

    reference = _reference(tmp_path)
    final_cover = tmp_path / "final.png"
    Image.new("RGB", (1920, 1080), (30, 40, 50)).save(final_cover)

    base = {
        "source_lidousha_located": True,
        "host_window_visible_in_final": True,
        "host_window_identity_matches": True,
        # 她确实不是主体——这正是 维护者 8/9 说的"主体肯定是游戏"，不该扣分。
        "primary_subject_is_visually_dominant": False,
        "frame_is_interesting": True,
        "excessive_dead_space": False,
        "meaningless_dominant_decoration": False,
        "thumbnail_has_clear_click_hook": True,
        "primary_subject_identity": "游戏画面 + 右下角李豆沙小窗",
        "identity_conflicts": [],
        "composition_conflicts": [],
        "reason": "结算界面写着通关，右下小窗里是她",
    }

    def _verify(verdict):
        original = visual_witness.image_vision_probe
        visual_witness.image_vision_probe = _probe(verdict)
        try:
            return verify_final_host_identity(
                final_cover_path=final_cover,
                final_cover_sha256=_sha(final_cover),
                reference_path=reference,
                scene_kind=GAME_SCENE,
            )
        finally:
            visual_witness.image_vision_probe = original

    # 基线：她不占主要部分也能过——这就是 维护者 8/9 裁定的机器面。
    passing = _verify(base)
    assert passing["status"] == "PASS"
    assert passing["scene_kind"] == GAME_SCENE

    for field, value, expected in (
        ("frame_is_interesting", False, "FINAL_COVER_GAME_SCENE_COMPOSITION_FAILED"),
        ("excessive_dead_space", True, "FINAL_COVER_GAME_SCENE_COMPOSITION_FAILED"),
        ("meaningless_dominant_decoration", True, "FINAL_COVER_GAME_SCENE_COMPOSITION_FAILED"),
        ("thumbnail_has_clear_click_hook", False, "FINAL_COVER_GAME_SCENE_COMPOSITION_FAILED"),
        ("host_window_visible_in_final", False, "FINAL_HOST_IDENTITY_MISMATCH"),
        # 多人场冒名（7/14 142 事故的游戏场对应物）仍然全额否决。
        ("host_window_identity_matches", False, "FINAL_HOST_IDENTITY_MISMATCH"),
        ("source_lidousha_located", False, "FINAL_HOST_IDENTITY_MISMATCH"),
    ):
        verdict = dict(base)
        verdict[field] = value
        receipt = _verify(verdict)
        assert receipt["status"] == "FAIL", field
        assert receipt["reason_code"] == expected, field


# ───────────────── ③ 保真：talk 提问逐字节等价 ─────────────────

# 这两个 sha 是 base 7d08564（本次改动前）的 prompt 原文摘要。任何对 talk 提问的
# 改写——哪怕只多一个空格——都会在这里红掉；游戏场分叉只准新增分支，不准污染谈话场。
_TALK_SOURCE_QUESTION_SHA = (
    "249721671a5f12bb519b35d33fe6bff98c974a37ac69aee0de1a9ce168f550f5"
)
_TALK_HOST_GATE_QUESTION_SHA = (
    "450d585621dd38d4469591a5b98863c35aaaba0a3ea052362a3cd6e9686ba769"
)


def test_canary_3_talk_prompts_are_byte_identical_to_base():
    from src.autoslice.cover_host_identity_gate import _QUESTION as HOST_QUESTION

    assert (
        hashlib.sha256(_QUESTION_PREFIX.encode("utf-8")).hexdigest()
        == _TALK_SOURCE_QUESTION_SHA
    )
    assert (
        hashlib.sha256(HOST_QUESTION.encode("utf-8")).hexdigest()
        == _TALK_HOST_GATE_QUESTION_SHA
    )


def test_canary_3b_talk_receipt_and_call_shape_are_unchanged(tmp_path):
    """talk 场不多带 scene_kind：回执形状、提问串、kwargs 全部零变化。"""

    reference = _reference(tmp_path)
    probe = _probe(_REAL_8_8_DEMOTED_VERDICT)
    verification = verify_source_composition(
        reference_path=reference,
        reference_sha256=_sha(reference),
        story_hook="她刚说完就被电",
        title="【主播】话音刚落小主暴毙",
        image_probe=probe,
    )
    assert "scene_kind" not in verification
    assert source_composition_scene_kind(verification) == TALK_SCENE
    assert probe.last_question == (
        _QUESTION_PREFIX
        + "\n故事钩子：她刚说完就被电"
        + "\n投稿标题：【主播】话音刚落小主暴毙"
    )
    # 没有小窗 / 没有 RESOLVED 游戏场 → 一个 kwarg 都不多传。
    assert source_composition_scene_kwargs({"camera_window_bbox_frac": None}) == {}
    assert source_composition_scene_kwargs(None) == {}


def test_canary_3c_scene_resolution_fails_closed_to_talk(tmp_path, monkeypatch):
    resolved = {
        "schema_version": "session-game-context.v1",
        "occurrence_policy": "GAME_TERM_EXISTS_NOT_CUE_OCCURRENCE_OR_MUTATION_AUTHORITY",
        "recording_date": "2026-08-08",
        "status": "RESOLVED",
        "qualifying_game_ids": ["g"],
        "game": {"game_id": "g", "canonical": "G", "aliases": [], "terms": []},
    }
    window = {"camera_window_bbox_frac": [0.80, 0.72, 0.99, 0.99]}

    assert resolve_cover_scene_kind(
        session_game_context=resolved, frame_selection=window
    ) == GAME_SCENE
    # 缺任何一条都退回 talk。
    assert resolve_cover_scene_kind(
        session_game_context=resolved, frame_selection={"camera_window_bbox_frac": None}
    ) == TALK_SCENE
    assert resolve_cover_scene_kind(
        session_game_context={**resolved, "status": "AMBIGUOUS"}, frame_selection=window
    ) == TALK_SCENE
    assert resolve_cover_scene_kind(
        session_game_context=None, frame_selection=window
    ) == TALK_SCENE

    # env 绑定面：文件缺失/损坏/被 disable 一律 talk。
    monkeypatch.delenv("LIDOUSHA_SESSION_GAME_CONTEXT", raising=False)
    assert cover_scene_kind(window) == TALK_SCENE
    broken = tmp_path / "ctx.json"
    broken.write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("LIDOUSHA_SESSION_GAME_CONTEXT", str(broken))
    assert cover_scene_kind(window) == TALK_SCENE
    good = tmp_path / "good.json"
    good.write_text(json.dumps(resolved), encoding="utf-8")
    monkeypatch.setenv("LIDOUSHA_SESSION_GAME_CONTEXT", str(good))
    assert cover_scene_kind(window) == GAME_SCENE
    monkeypatch.setenv("LIDOUSHA_DISABLE_SESSION_GAME_CONTEXT", "1")
    assert cover_scene_kind(window) == TALK_SCENE


# ───────────────── ④ 正向：8/8 五条真实形状必须放行 ─────────────────

def test_canary_4_story_reaction_false_no_longer_blocks_the_crop(tmp_path):
    """P1 的正向钉。**revert P1 这条必红**：恢复四布尔 AND 会让
    ``extract_authority_source_crop`` 对同一份 verdict 抛
    ``SOURCE_COMPOSITION_CROP_NOT_AUTHORIZED``，两个断言同时失败。"""

    reference = _reference(tmp_path)
    verification = _verified(reference, _REAL_8_8_DEMOTED_VERDICT)
    # 报告 B3 的死因复刻：两个几何布尔为真，只有故事反应为假。
    assert verification["verdict"]["source_carries_story_reaction"] is False
    assert verification["verdict"]["cpa_redraw_recommended"] is True
    receipt = _receipt(tmp_path, verification)

    evidence = extract_authority_source_crop(
        reference_path=reference,
        output_path=tmp_path / "base.png",
        frame_ms=12_345,
        verification=verification,
        verification_receipt_path=Path(receipt["path"]),
        verification_receipt_sha256=receipt["sha256"],
    )
    assert evidence["status"] == "HASH_BOUND_CPA_IDENTITY_CROP"
    assert evidence["crop_applied"] is True
    assert evidence["authority_bbox_frac"] == [0.36, 0.10, 0.74, 0.92]

    # 走生产入口（P3 包装器）时同样必须是真裁切，而不是退到全幅兜底。
    wrapped = extract_authority_source_crop_or_full_frame(
        reference_path=reference,
        output_path=tmp_path / "base2.png",
        frame_ms=12_345,
        verification=verification,
        verification_receipt_path=Path(receipt["path"]),
        verification_receipt_sha256=receipt["sha256"],
    )
    assert wrapped["status"] == "HASH_BOUND_CPA_IDENTITY_CROP"


@pytest.mark.parametrize(
    "score,emotional,expected",
    [
        (4.91, True, "screenshot_direct"),   # 8/8 auto_200130_1323_1603
        (4.46, True, "screenshot_direct"),   # 8/8 auto_213135_469_710
        (3.55, False, "screenshot_polish"),  # 8/7 auto_200736_298_383
        (3.02, False, "screenshot_polish"),  # 8/8 auto_210131_1576_1802
        (3.46, False, "screenshot_polish"),  # 8/8 auto_230125_960_1072
    ],
)
def test_canary_4b_the_five_demoted_candidates_route_to_screenshot(
    tmp_path, score, emotional, expected
):
    """8/7–8/8 五条 CROP_NOT_AUTHORIZED 降级案的真实分数重放。"""

    reference = _reference(tmp_path)
    verification = _verified(reference, _REAL_8_8_DEMOTED_VERDICT)
    treatment, route = _route(
        verification=verification,
        frame_selection={
            "status": "SELECTED",
            "best_ms": 30_000,
            "candidates": [{"score": score, "emotion": 1.0 if emotional else 0.0}],
            # 36/36 实测：运动几何置信通道全灭，授权只能来自见证。
            "subject_confident": False,
            "motion_dispersion_frac": 0.655,
        },
    )
    assert treatment == expected
    assert route["source_composition_redraw_recommended"] is False


# ───────────────── ⑤ 降级：先落全幅不裁海报 ─────────────────

def test_canary_5_unauthorized_crop_degrades_to_full_frame_not_redraw(tmp_path):
    reference = _reference(tmp_path)
    verification = _verified(
        reference,
        {
            "lidousha_bbox_frac": [0.30, 0.10, 0.70, 0.90],
            # 脸被切 → 裁切不授权，但源帧本身仍是 hash-bound 的真实瞬间。
            "source_face_complete": False,
            "faithful_crop_can_make_dominant": True,
            "source_carries_story_reaction": True,
            "cpa_redraw_recommended": True,
            "reason": "下巴被字幕带切掉，紧裁会露出断脸",
        },
    )
    receipt = _receipt(tmp_path, verification)
    output = tmp_path / "base.png"

    # 未包装的执行端仍然明确拒绝——授权语义没有被偷偷放宽。
    with pytest.raises(ValueError, match="SOURCE_COMPOSITION_CROP_NOT_AUTHORIZED"):
        extract_authority_source_crop(
            reference_path=reference,
            output_path=output,
            frame_ms=1,
            verification=verification,
            verification_receipt_path=Path(receipt["path"]),
            verification_receipt_sha256=receipt["sha256"],
        )

    evidence = extract_authority_source_crop_or_full_frame(
        reference_path=reference,
        output_path=output,
        frame_ms=1,
        verification=verification,
        verification_receipt_path=Path(receipt["path"]),
        verification_receipt_sha256=receipt["sha256"],
    )
    assert evidence["status"] == "HASH_BOUND_FULL_FRAME_NO_CROP_COMPOSITOR"
    assert evidence["crop_applied"] is False
    assert evidence["full_frame_preserved"] is True
    assert evidence["crop_fallback_reason_code"] == "SOURCE_COMPOSITION_CROP_NOT_AUTHORIZED"
    assert evidence["source_sha256"] == _sha(reference)
    assert Image.open(output).size == (1920, 1080)

    # 海报底板必须真的整幅进卡，而不是 fit_crop 把她切掉。
    from src.autoslice.cover_screenshot_poster import (
        _compose_screenshot_poster_background,
    )

    poster = _compose_screenshot_poster_background(
        output,
        tmp_path / "poster.png",
        art_direction=_art_direction(),
        preserve_full_frame=bool(evidence["full_frame_preserved"]),
    )
    assert poster["source_frame_transform"]["card_fit"] == "full_frame"
    assert poster["source_frame_transform"]["full_frame_preserved"] is True


def test_canary_5b_an_invalid_receipt_never_degrades_to_full_frame(tmp_path):
    """fail-closed 射程不变：回执本身不可信绝不退到全幅。"""

    reference = _reference(tmp_path)
    verification = _verified(reference, _REAL_8_8_DEMOTED_VERDICT)
    tampered = json.loads(json.dumps(verification))
    tampered["verdict"]["lidousha_bbox_frac"] = [0.9, 0.9, 0.1, 0.1]

    with pytest.raises(ValueError, match="SOURCE_COMPOSITION_VERIFICATION_INVALID"):
        extract_authority_source_crop_or_full_frame(
            reference_path=reference,
            output_path=tmp_path / "base.png",
            frame_ms=1,
            verification=tampered,
            verification_receipt_path=tmp_path / "r.json",
            verification_receipt_sha256="sha256:" + "0" * 64,
        )


# ───────────────── ⑥ 可达：小窗分支命中 ≥1 ─────────────────

def _game_verification(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    reference = _reference(tmp_path)
    verification = _verified(
        reference,
        {
            "lidousha_bbox_frac": [0.80, 0.72, 0.99, 0.99],
            "host_window_visible": True,
            "frame_is_interesting": True,
            "cpa_redraw_recommended": False,
            "reason": "右下小窗里是她，画面正显示抱团团灭的结算",
        },
        scene_kind=GAME_SCENE,
    )
    return reference, verification


def test_canary_6_camera_window_branch_is_reachable_in_a_game_scene(tmp_path):
    """C5 的死代码复活：见证在场时这条分支第一次真正可达。"""

    _reference_path, verification = _game_verification(tmp_path)
    # 游戏场故意不把她算成"可信主体"——这正是让分数分支让位的机制。
    assert source_composition_supports_subject(verification) is False
    assert source_composition_recommends_redraw(verification) is False

    frame_selection = {
        "status": "SELECTED",
        "best_ms": 30_000,
        "candidates": [{"score": 8.17, "emotion": 1.0}],
        "subject_confident": False,
        "motion_dispersion_frac": 0.651,
        "camera_window_bbox_frac": [0.80, 0.72, 0.99, 0.99],
    }
    treatment, route = _route(
        verification=verification, frame_selection=frame_selection
    )
    assert treatment == "screenshot_polish"
    assert "game scene: host camera window visible" in route["selected_rationale"]

    # 同一条片按 talk 判据（她能被裁成大主体）走的是分数分支——证明本分支
    # 在改动前对有见证的候选不可达。
    talk_reference = tmp_path / "talk.png"
    Image.new("RGB", (1920, 1080), (11, 22, 33)).save(talk_reference)
    talk_verification = _verified(talk_reference, _REAL_8_8_DEMOTED_VERDICT)
    talk_treatment, talk_route = _route(
        verification=talk_verification, frame_selection=frame_selection
    )
    assert talk_treatment == "screenshot_direct"
    assert "camera window" not in talk_route["selected_rationale"]


def test_canary_6b_game_scene_materializes_the_whole_frame(tmp_path):
    """维护者 8/9：主体本来就是游戏——不许把小窗 1.38x 裁出来当封面。"""

    reference, verification = _game_verification(tmp_path)
    receipt = _receipt(tmp_path, verification)

    _art, output, evidence = publish_staging._screenshot_base_and_crop(
        media_path=tmp_path / "motion-must-not-be-read.mp4",
        candidate_id="game-scene",
        ai_dir=tmp_path,
        reference_path=reference,
        frame_selection={
            "best_ms": 12_345,
            "subject_confident": False,
            "camera_window_bbox_frac": [0.80, 0.72, 0.99, 0.99],
        },
        art_direction=_art_direction(),
        relationship_visual_required=False,
        source_composition_verification=verification,
        source_composition_receipt=receipt,
    )
    assert evidence["status"] == "HASH_BOUND_GAME_SCENE_FULL_FRAME"
    assert evidence["scene_kind"] == GAME_SCENE
    assert evidence["crop_applied"] is False
    assert evidence["full_frame_preserved"] is True
    assert evidence["host_window_bbox_frac"] == [0.80, 0.72, 0.99, 0.99]
    assert evidence["source_sha256"] == _sha(reference)
    assert Image.open(output).size == (1920, 1080)


# ───────────────── C9：拒绝理由必须带真实判据 ─────────────────

def test_rejected_alternatives_carry_this_frames_real_evidence(tmp_path):
    reference = _reference(tmp_path)
    verification = _verified(reference, _REAL_8_8_DEMOTED_VERDICT)
    _treatment, route = _route(
        verification=verification,
        frame_selection={
            "status": "SELECTED",
            "best_ms": 30_000,
            "candidates": [{"score": 4.91, "emotion": 1.0}],
            "subject_confident": False,
            "motion_dispersion_frac": 0.348,
        },
    )
    rejected = route["rejected_alternatives"]
    assert len(rejected) == 2
    for row in rejected:
        reason = str(row["rejected_reason"])
        assert "frame_score=4.91" in reason
        assert "source_carries_story_reaction=False" in reason
        # 见证原文逐字进证据，不做转述。
        assert "持麦手没有可辨识的颤抖" in reason
    for row in route["alternatives"]:
        assert row["per_frame_evidence"]


# ───────────────── C7：返修不得静默把截图换成重绘 ─────────────────

def _delivered_screenshot_record(tmp_path: Path) -> tuple[dict, Path]:
    # 生产实形：ai_background 是海报底板，repair_screenshot_cover.py 要吃的是
    # 它同目录的 <candidate>.screenshot-polished.png。
    pixels = tmp_path / "auto_x.screenshot-poster.png"
    Image.new("RGB", (16, 9), (1, 1, 1)).save(pixels)
    return (
        {
            "candidate_id": "auto_x",
            # 权威是活动记录自己的 cover_generation（初始生产就写在这里），
            # 不是重绘 lane 才写的 <cover>.cover_generation.json。
            "cover_generation": {
                "route_decision": {
                    "selected_treatment": "screenshot_polish",
                    "actual_treatment": "screenshot_polish",
                    "execution_status": "READY",
                },
                "ai_background": str(pixels),
            },
        },
        pixels,
    )


def test_repair_refuses_to_displace_a_recoverable_screenshot_route(tmp_path):
    from src.autoslice import cover_repair_route_lineage as lineage

    rec, pixels = _delivered_screenshot_record(tmp_path)

    with pytest.raises(ValueError, match="COVER_SCREENSHOT_ROUTE_REPAIR_REQUIRED"):
        lineage.refuse_recoverable_screenshot_route(rec)

    # polish 输入在场时，报的是 repair_screenshot_cover.py 真正要吃的那份。
    polished = tmp_path / "auto_x.screenshot-polished.png"
    Image.new("RGB", (16, 9), (2, 2, 2)).save(polished)
    assert lineage.recoverable_screenshot_route(rec) == ("screenshot_polish", polished)
    polished.unlink()

    # polish 被拒降级成 direct 的成品仍是真截图，同样必须让路。
    degraded_direct = json.loads(json.dumps(rec))
    degraded_direct["cover_generation"]["route_decision"][
        "actual_treatment"
    ] = "screenshot_direct"
    with pytest.raises(ValueError, match="COVER_SCREENSHOT_ROUTE_REPAIR_REQUIRED"):
        lineage.refuse_recoverable_screenshot_route(degraded_direct)

    # 像素已经不在盘上 → 没有可继承的截图，重绘是唯一出路，不阻断交付，
    # 但被顶替的路线必须 typed 披露。
    pixels.unlink()
    lineage.refuse_recoverable_screenshot_route(rec)
    enriched: dict[str, object] = {}
    suffix = lineage.record_screenshot_route_displacement(
        enriched, rec["cover_generation"]
    )
    assert "displaced route screenshot_polish" in suffix
    displacement = enriched["cover_repair_route_displacement"]
    assert displacement["prior_selected_treatment"] == "screenshot_polish"
    assert displacement["replaced_with"] == "cpa_redraw"
    assert displacement["screenshot_repair_tool"] == "scripts/repair_screenshot_cover.py"

    # 原路线本来就是重绘 → 零披露、零阻断。
    assert (
        lineage.record_screenshot_route_displacement(
            {}, {"route_decision": {"selected_treatment": "cpa_redraw"}}
        )
        == ""
    )


def test_repair_still_runs_for_demoted_and_blocked_screenshot_attempts(tmp_path):
    """降级过/被阻断的截图尝试**不算**可保留的截图，返修照常放行。

    否则一条 `actual=cpa_redraw` 的降级封面、或一条 BLOCKED 的截图尝试
    （`ai_background` 在门检之前就已写盘）会把自己的返修永久锁死。
    """

    from src.autoslice import cover_repair_route_lineage as lineage

    rec, _pixels = _delivered_screenshot_record(tmp_path)

    demoted = json.loads(json.dumps(rec))
    demoted["cover_generation"]["route_decision"]["actual_treatment"] = "cpa_redraw"
    assert lineage.recoverable_screenshot_route(demoted) is None
    lineage.refuse_recoverable_screenshot_route(demoted)

    blocked = json.loads(json.dumps(rec))
    blocked["cover_generation"]["route_decision"]["actual_treatment"] = None
    blocked["cover_generation"]["route_decision"]["execution_status"] = "BLOCKED"
    assert lineage.recoverable_screenshot_route(blocked) is None
    lineage.refuse_recoverable_screenshot_route(blocked)

    # 初始 v1 证据没有 actual_treatment/execution_status：按 selected 兜底。
    legacy = {
        "cover_generation": {
            "route_decision": {"selected_treatment": "screenshot_direct"},
            "ai_background": rec["cover_generation"]["ai_background"],
        }
    }
    assert lineage.recoverable_screenshot_route(legacy) == (
        "screenshot_direct",
        Path(rec["cover_generation"]["ai_background"]),
    )


# ───────────── ⑦ 竖屏源 → 重绘（维护者 逐字裁定）─────────────
#
# 「并不是所有的都需要截图，特别是竖屏直播，通常不适合截图，只能重绘。」
# 实测事故：`auto_230125_960_1072`（BV1Bau16nEyq）源帧 1920×3414，16:9 窗口
# 对准形心后从眼睛处切断。以下三条钉住：判据成立、判据优先级、判据不误伤。

_VERTICAL_SOURCE_SIZE = (1920, 3414)      # 真实事故几何，h/w = 1.7781
_LANDSCAPE_SOURCE_SIZE = (1920, 1080)     # 16:9，h/w = 0.5625


def _hash_bound_authority() -> dict[str, object]:
    return {
        "candidate_id": "auto_230125_960_1072",
        "source_sha256": "sha256:" + "a" * 64,
        "reference_png_sha256": "sha256:" + "b" * 64,
        "visible_participant_ids": [],
    }


def test_canary_7a_vertical_source_routes_to_redraw_as_normal_route(tmp_path):
    """竖版源即使拿到 4.91 强名场面分也走重绘，且回执不得记成降级。"""

    reference = _reference(tmp_path)
    verification = _verified(reference, _REAL_8_8_DEMOTED_VERDICT)
    treatment, route = _route(
        verification=verification,
        frame_selection={
            "status": "SELECTED",
            "best_ms": 30_000,
            "candidates": [{"score": 4.91, "emotion": 1.0}],
            "subject_confident": True,
            "motion_dispersion_frac": 0.21,
        },
        source_frame_size=_VERTICAL_SOURCE_SIZE,
    )
    assert treatment == "cpa_redraw"

    assert route["vertical_source_redraw"] is True
    assert route["source_frame_size"] == [1920, 3414]
    assert route["source_frame_aspect_ratio"] == 1.7781
    assert route["vertical_source_min_aspect_ratio"] == 1.2

    selected = next(
        row for row in route["alternatives"] if row["status"] == "SELECTED"
    )
    # 正常路由，不是"帧不够好"。措辞与逐帧证据都必须这么说。
    assert "redraw is the normal route here (维护者 2026-08-10)" in selected["rationale"]
    assert "1.7781 >= 1.20" in selected["rationale"]
    rejected = next(
        row for row in route["alternatives"] if row["treatment"] == "screenshot_direct"
    )
    assert "vertical_source_redraw=true" in rejected["rejected_reason"]
    assert "非降级" in rejected["rejected_reason"]


_RELATIONSHIP_STORY_CONTRACT = {
    "schema_version": "lidousha-story-contract.v1",
    # relationship_visual_safety_evidence 的真实门：CONFIRMED + ≥2 participants。
    "relation_state": "CONFIRMED",
    "participants": [
        {"canonical_id": "lidousha"},
        {"canonical_id": "sekiseki"},
    ],
}


@pytest.mark.parametrize(
    "kwargs",
    [
        # hash-bound 见证帧：旧序里这条无条件 return screenshot_direct，
        # 竖版判据放在它后面就等于对恢复重放整条失效——而重放正是事故现场。
        {"reference_authority": _hash_bound_authority()},
        # 关系钩子：同上，也在竖版判据之后。
        {"story_contract": _RELATIONSHIP_STORY_CONTRACT},
        # 运营强制截图：竖版源上"强制"也造不出能用的 16:9 裁切。
        {"cover_mode": "screenshot"},
    ],
)
def test_canary_7b_vertical_beats_every_screenshot_forcing_branch(tmp_path, kwargs):
    reference = _reference(tmp_path)
    verification = _verified(reference, _REAL_8_8_DEMOTED_VERDICT)
    # 分数刻意压到 0：这样**只有**被测的那条强制分支能产出 screenshot_direct，
    # 下面的标定评分路径在同样输入下只会给 cpa_redraw。否则对照组会被评分路径
    # 顺带满足，"优先级"就没被证明过。
    frame_selection = {
        "status": "SELECTED",
        "best_ms": 30_000,
        "candidates": [{"score": 0.0, "emotion": 0.0}],
        "subject_confident": True,
        "motion_dispersion_frac": 0.21,
    }
    baseline, _ = _route(
        verification=verification,
        frame_selection=frame_selection,
        source_frame_size=_LANDSCAPE_SOURCE_SIZE,
    )
    assert baseline == "cpa_redraw"

    # 对照组证明这条强制分支**真的会开火**：横版同参数必须落在 screenshot_direct。
    control, control_route = _route(
        verification=verification,
        frame_selection=frame_selection,
        source_frame_size=_LANDSCAPE_SOURCE_SIZE,
        **kwargs,
    )
    assert control == "screenshot_direct", control_route["selected_rationale"]

    treatment, _route_decision = _route(
        verification=verification,
        frame_selection=frame_selection,
        source_frame_size=_VERTICAL_SOURCE_SIZE,
        **kwargs,
    )
    assert treatment == "cpa_redraw"


@pytest.mark.parametrize(
    "size",
    [
        _LANDSCAPE_SOURCE_SIZE,   # 16:9   h/w = 0.5625
        (1440, 1080),             # 4:3    h/w = 0.75
        (1080, 1080),             # 1:1    h/w = 1.0
        (1920, 2303),             # h/w = 1.1995，刚好在阈值下
        None,                     # 几何未知 → fail-open，仍由分数决定
    ],
)
def test_canary_7c_landscape_and_unknown_geometry_are_not_touched(tmp_path, size):
    """别误伤"横版源但人物在画面上部"——那是整脸门的辖区，不是这条判据的。"""

    reference = _reference(tmp_path)
    verification = _verified(reference, _REAL_8_8_DEMOTED_VERDICT)
    treatment, route = _route(
        verification=verification,
        frame_selection={
            "status": "SELECTED",
            "best_ms": 30_000,
            "candidates": [{"score": 4.46, "emotion": 1.0}],
            "subject_confident": False,
            "motion_dispersion_frac": 0.655,
        },
        source_frame_size=size,
    )
    assert treatment == "screenshot_direct"
    assert route["vertical_source_redraw"] is False
