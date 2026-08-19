"""Hash-bound source-composition authority for channel covers.

The final-host gate protects the generated pixels, but it is too late to decide
whether the selected livestream frame should have been redrawn in the first
place.  This module asks CPA (AGY only through the shared fallback router) to
locate Li Dousha in the exact extracted reference and to make that route choice
before any image generation is attempted.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from PIL import Image

from src.autoslice.surface_canon import CHANNEL_PROFILE


SCHEMA_VERSION = "lidousha-cover-source-composition-verification.v1"
AUTHORITY = "CPA_PRIMARY_HASH_BOUND_SOURCE_COMPOSITION"

# 场景分叉（维护者 02:20 逐字裁定）：「如果是截图封面的话，当然不要求
# 李豆沙在画面里占主要部分，毕竟是游戏截图，只要截图足够有趣就行，主体肯定会
# 会是游戏。」——单人主导框架只对谈话场成立；游戏场问的是**小窗可见 + 画面有趣**。
# 未证明是游戏场的一律按 talk 走（fail-closed），talk 提问与判据逐字节不变。
TALK_SCENE = "talk"
GAME_SCENE = "game"
COVER_SCENE_KINDS = (TALK_SCENE, GAME_SCENE)

_BOOL_FIELDS = (
    "source_face_complete",
    "faithful_crop_can_make_dominant",
    "source_carries_story_reaction",
    "cpa_redraw_recommended",
)
_GAME_BOOL_FIELDS = (
    "host_window_visible",
    "frame_is_interesting",
    "cpa_redraw_recommended",
)
_SCENE_BOOL_FIELDS = {
    TALK_SCENE: _BOOL_FIELDS,
    GAME_SCENE: _GAME_BOOL_FIELDS,
}

# 竖屏源不走截图（维护者 逐字裁定）：「并不是所有的都需要截图，特别是
# 竖屏直播，通常不适合截图，只能重绘。」
#
# 阈值依据（几何，不是标定）：成品封面固定 16:9（0.5625 的 h/w）。从一张 h/w=r
# 的源里裁一条满宽 16:9，只用得到 0.5625/r 的画面高度。r=1.2 时已经只剩 46.9%
# ——超过一半的直播画面被丢掉，"忠实裁切"这个前提本身就不成立；再往上（3:4 竖屏
# r=1.333 剩 42%，9:16 手机竖屏 r=1.778 剩 32%）只会更糟。实测事故
# `auto_230125_960_1072`（源帧 1920×3414，r=1.7781）正是这样把 16:9 窗口对准
# 形心后从眼睛处切断。横版侧留足余量：16:9=0.5625、4:3=0.75、1:1=1.0 全部远低于
# 1.2，所以"横版源但人物在画面上部"不会被这条判据误伤（那是整脸门的辖区）。
VERTICAL_SOURCE_MIN_ASPECT_RATIO = 1.2
COVER_OUTPUT_ASPECT_RATIO = 1080 / 1920


def source_frame_is_vertical(
    size: object,
    *,
    min_ratio: float = VERTICAL_SOURCE_MIN_ASPECT_RATIO,
) -> bool:
    """Return whether one source frame is too tall for a 16:9 screenshot.

    Fail-open on unknown geometry: a missing or unparseable size is not proof
    of a vertical source, and the ordinary scoring route still owns that clip.
    """

    if not isinstance(size, (list, tuple)) or len(size) != 2:
        return False
    try:
        width = float(size[0])
        height = float(size[1])
    except (TypeError, ValueError):
        return False
    if not (math.isfinite(width) and math.isfinite(height)) or width <= 0 or height <= 0:
        return False
    return (height / width) >= float(min_ratio)


def source_frame_aspect_ratio(size: object) -> float | None:
    """Return one source frame's h/w, or ``None`` when geometry is unknown."""

    if not isinstance(size, (list, tuple)) or len(size) != 2:
        return None
    try:
        width = float(size[0])
        height = float(size[1])
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(width) and math.isfinite(height)) or width <= 0 or height <= 0:
        return None
    return round(height / width, 4)


def vertical_source_redraw_reason(
    size: object,
    *,
    min_ratio: float = VERTICAL_SOURCE_MIN_ASPECT_RATIO,
) -> str | None:
    """Route reason when the source is vertical, else ``None``.

    这是**正常路由，不是降级**：帧本身可以完全合格，只是装不进 16:9。回执措辞
    因此写「redraw is the normal route here」并带上实测比例与阈值，别让下游把它
    读成选帧失败或质量不合格。
    """

    if not source_frame_is_vertical(size, min_ratio=min_ratio):
        return None
    return (
        "vertical source is unsuitable for a screenshot cover; redraw is the "
        "normal route here (维护者 2026-08-10) — source h/w="
        f"{source_frame_aspect_ratio(size):.4f} >= {float(min_ratio):.2f}"
    )


def vertical_source_decision_inputs(size: object) -> dict[str, object]:
    """Disclose the measured geometry and the threshold in the route receipt."""

    return {
        "source_frame_size": (
            [int(size[0]), int(size[1])]
            if isinstance(size, (list, tuple)) and len(size) == 2
            else None
        ),
        "source_frame_aspect_ratio": source_frame_aspect_ratio(size),
        "vertical_source_min_aspect_ratio": VERTICAL_SOURCE_MIN_ASPECT_RATIO,
        "vertical_source_redraw": source_frame_is_vertical(size),
    }


def read_source_frame_size(path: object) -> tuple[int, int] | None:
    """Measure one already-extracted reference frame; ``None`` when unreadable."""

    if not path:
        return None
    try:
        with Image.open(Path(str(path))) as image:
            return (int(image.width), int(image.height))
    except Exception:
        return None


def normalize_cover_scene_kind(value: object) -> str:
    """Fail-closed scene normalization: anything unproven is a talk scene."""

    text = str(value or "").strip().lower()
    return text if text in COVER_SCENE_KINDS else TALK_SCENE


def resolve_cover_scene_kind(
    *,
    session_game_context: object = None,
    frame_selection: Mapping[str, object] | None = None,
) -> str:
    """Classify one clip's cover scene from evidence that already exists.

    没有新探测器：游戏场 = ①本场 `session-game-context.v1` 已 RESOLVED（会话级，
    `src/autoslice/game_context.py` 既有产物）**且** ②本条选帧探到了固定位置的
    面捕小窗 `camera_window_bbox_frac`（`cover_frame_selection.py` 既有产物，
    正是"全屏游戏 + 角落小窗"这个版式的判据）。两条缺一即 talk——游戏场里的纯
    杂谈切片没有小窗，不会被误判进放宽面。
    """

    if not isinstance(session_game_context, Mapping):
        return TALK_SCENE
    if session_game_context.get("schema_version") != "session-game-context.v1":
        return TALK_SCENE
    if session_game_context.get("status") != "RESOLVED":
        return TALK_SCENE
    if not isinstance(frame_selection, Mapping):
        return TALK_SCENE
    if not _valid_bbox(frame_selection.get("camera_window_bbox_frac")):
        return TALK_SCENE
    return GAME_SCENE


def _sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _extract_json_object(answer: str) -> Mapping[str, object]:
    start = answer.index("{")
    end = answer.rindex("}") + 1
    value = json.loads(answer[start:end])
    if not isinstance(value, Mapping):
        raise ValueError("source-composition verdict is not a JSON object")
    return value


def _valid_bbox(value: object) -> bool:
    if not isinstance(value, list) or len(value) != 4:
        return False
    if not all(
        isinstance(part, (int, float))
        and not isinstance(part, bool)
        and math.isfinite(float(part))
        for part in value
    ):
        return False
    x0, y0, x1, y1 = (float(part) for part in value)
    return bool(
        0.0 <= x0 < x1 <= 1.0
        and 0.0 <= y0 < y1 <= 1.0
        and (x1 - x0) >= 0.01
        and (y1 - y0) >= 0.01
    )


def _verdict_is_coherent(
    verdict: Mapping[str, object], *, scene_kind: str = TALK_SCENE
) -> bool:
    scene = normalize_cover_scene_kind(scene_kind)
    if not _valid_bbox(verdict.get("lidousha_bbox_frac")):
        return False
    if not all(
        isinstance(verdict.get(key), bool) for key in _SCENE_BOOL_FIELDS[scene]
    ):
        return False
    if not isinstance(verdict.get("reason"), str) or not str(
        verdict.get("reason") or ""
    ).strip():
        return False

    if scene == GAME_SCENE:
        # 游戏场没有"她能否成为大主体"这个判据（维护者 8/9：主体本来就是游戏）。
        # 唯一的一致性要求：小窗可见且画面有趣时不得同时建议整张重绘。
        game_screenshot_safe = bool(
            verdict.get("host_window_visible") is True
            and verdict.get("frame_is_interesting") is True
        )
        return verdict.get("cpa_redraw_recommended") is (
            not game_screenshot_safe
        )

    screenshot_safe = bool(
        verdict.get("source_face_complete") is True
        and verdict.get("faithful_crop_can_make_dominant") is True
        and verdict.get("source_carries_story_reaction") is True
    )
    # The witness cannot recommend keeping source pixels while also saying
    # that the face is cut, cannot become dominant, or does not carry the
    # story reaction.  Such a contradiction is not a route decision.
    return verdict.get("cpa_redraw_recommended") is (not screenshot_safe)


def _answer_valid(answer: str, *, scene_kind: str = TALK_SCENE) -> bool:
    try:
        return _verdict_is_coherent(
            _extract_json_object(answer), scene_kind=scene_kind
        )
    except (ValueError, json.JSONDecodeError):
        return False


def _routing_valid(witness: Mapping[str, object]) -> bool:
    routing = witness.get("routing")
    if not isinstance(routing, Mapping):
        return False
    if routing.get("preferred_provider") != "cpa":
        return False
    fallback = routing.get("fallback_used")
    selected = str(routing.get("selected_provider") or "")
    provider = str(witness.get("provider") or "")
    if fallback is False:
        return selected == provider == "cpa"
    if fallback is True:
        return bool(
            selected == provider == "agy"
            and str(routing.get("primary_status") or "")
            in {"UNAVAILABLE", "OBSERVED_UNUSABLE"}
            and str(routing.get("primary_reason_code") or "").strip()
            and isinstance(routing.get("primary_receipt"), Mapping)
        )
    return False


_QUESTION_PREFIX = (
    f"这是待制作{CHANNEL_PROFILE.display_name}切片封面的、已经 hash-bound 的 SOURCE REFERENCE。"
    "只判断这张源图本身，不假设后续生成器会修正构图。先按当场服装、"
    f"{CHANNEL_PROFILE.cover_identity.locator_zh}定位{CHANNEL_PROFILE.display_name}；"
    f"不要把{CHANNEL_PROFILE.cover_identity.composition_decoys_zh} 当成她。"
    f"给出{CHANNEL_PROFILE.display_name}完整可见区域的归一化 bbox=[x0,y0,x1,y1]，坐标必须在 0..1 且紧包住"
    "她的脸和承担反应的上半身。判断脸是否完整；在不生成、不补画、不扭曲身份且不裁掉"
    "关键反应的前提下，能否只靠 16:9 裁切让她成为大号第一主体；源图中的表情/动作是否"
    "确实承载给定故事反应。只要脸不完整、无法忠实裁成大主体、或源图不承载故事反应，"
    "就必须 cpa_redraw_recommended=true。不要因为运动框、弹幕或游戏画面显眼而放行。"
    "只输出 JSON："
    '{"lidousha_bbox_frac":[0.0,0.0,1.0,1.0],'
    '"source_face_complete":true|false,'
    '"faithful_crop_can_make_dominant":true|false,'
    '"source_carries_story_reaction":true|false,'
    '"cpa_redraw_recommended":true|false,'
    '"reason":"简短中文像素依据"}'
)


# 游戏场提问（维护者 02:20 裁定的落地面）。与 talk 版的差别是**故意**的：
# 删掉「让她成为大号第一主体」与「不要因为游戏画面显眼而放行」——这两句正是把
# 游戏场恒判重绘的那两句；改问 维护者 给的两个判据：小窗里能不能认出她、这张游戏
# 画面本身够不够有趣。她占画面比例在这里只作披露，不参与放行。
_GAME_QUESTION_PREFIX = (
    f"这是待制作{CHANNEL_PROFILE.display_name}切片封面的、已经 hash-bound 的 SOURCE REFERENCE。"
    "本条是**游戏直播场**：画面主体本来就是游戏，"
    f"不要求{CHANNEL_PROFILE.display_name}在画面里占主要部分。"
    f"先按当场服装、{CHANNEL_PROFILE.cover_identity.locator_zh}在画面中定位{CHANNEL_PROFILE.display_name}"
    f"的面捕小窗/立绘；不要把{CHANNEL_PROFILE.cover_identity.composition_decoys_zh} 当成她。"
    "给出该小窗的归一化 bbox=[x0,y0,x1,y1]，坐标必须在 0..1 且紧包住小窗里她的脸和上半身。"
    "然后只判断两件事：①host_window_visible——小窗确实存在、没有被遮挡或裁掉，"
    "里面的人可以被认出就是她（哪怕很小）；②frame_is_interesting——这张游戏画面本身"
    "是否承载一个看得出来的事件（战况、结算、道具、失误、名场面、可读的关键 UI 文字），"
    "而不是空面板、加载页、菜单、纯黑/纯色过渡或什么都没发生的静止画面。"
    "两者都为真才 cpa_redraw_recommended=false；任一为假就必须 cpa_redraw_recommended=true。"
    "只输出 JSON："
    '{"lidousha_bbox_frac":[0.0,0.0,1.0,1.0],'
    '"host_window_visible":true|false,'
    '"frame_is_interesting":true|false,'
    '"cpa_redraw_recommended":true|false,'
    '"reason":"简短中文像素依据"}'
)

_SCENE_QUESTION_PREFIX = {
    TALK_SCENE: _QUESTION_PREFIX,
    GAME_SCENE: _GAME_QUESTION_PREFIX,
}


def verify_source_composition(
    *,
    reference_path: Path,
    reference_sha256: str,
    story_hook: str,
    title: str,
    base_url: str = "",
    api_key: str = "",
    image_probe: Callable[..., dict[str, object]] | None = None,
    scene_kind: str = TALK_SCENE,
) -> dict[str, object]:
    """Return a strict source-composition receipt bound to reference bytes."""

    scene = normalize_cover_scene_kind(scene_kind)
    reference_path = Path(reference_path)
    receipt: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "authority": AUTHORITY,
        "reference_path": str(reference_path),
        "expected_reference_sha256": str(reference_sha256),
        "story_hook": str(story_hook),
        "title": str(title),
    }
    if scene != TALK_SCENE:
        # talk 回执逐字节保持既有形状（保真钉）；只有游戏场才多带 scene_kind。
        receipt["scene_kind"] = scene
    try:
        actual_reference_sha256 = _sha256(reference_path)
    except OSError as exc:
        receipt.update(
            status="FAIL",
            reason_code="SOURCE_COMPOSITION_REFERENCE_UNAVAILABLE",
            detail=f"{type(exc).__name__}: {exc}",
        )
        return receipt
    receipt["reference_sha256"] = actual_reference_sha256
    if actual_reference_sha256 != str(reference_sha256):
        receipt.update(
            status="FAIL",
            reason_code="SOURCE_COMPOSITION_REFERENCE_HASH_MISMATCH",
            detail=(
                f"expected {reference_sha256}, got {actual_reference_sha256}"
            ),
        )
        return receipt

    if image_probe is None:
        from src.autoslice.visual_witness import image_vision_probe

        image_probe = image_vision_probe
    question = (
        _SCENE_QUESTION_PREFIX[scene]
        + "\n故事钩子："
        + (str(story_hook).strip() or "未提供；只按标题的明确事件判断")
        + "\n投稿标题："
        + str(title).strip()
    )
    try:
        witness = image_probe(
            reference_path,
            question,
            api_base=base_url,
            api_key=api_key,
            answer_validator=(
                _answer_valid
                if scene == TALK_SCENE
                else lambda answer: _answer_valid(answer, scene_kind=scene)
            ),
        )
    except Exception as exc:
        receipt.update(
            status="FAIL",
            reason_code="SOURCE_COMPOSITION_WITNESS_EXCEPTION",
            detail=f"{type(exc).__name__}: {exc}",
        )
        return receipt
    receipt["witness"] = witness
    receipt["witness_receipt_sha256"] = _canonical_sha256(witness)
    witness_image_sha = (
        "sha256:" + str(witness.get("image_sha256") or "")
    )
    if (
        witness.get("status") != "OBSERVED"
        or witness_image_sha != actual_reference_sha256
        or not _routing_valid(witness)
    ):
        receipt.update(
            status="FAIL",
            reason_code="SOURCE_COMPOSITION_WITNESS_UNAVAILABLE",
            detail=str(
                witness.get("error")
                or witness.get("reason_code")
                or witness.get("status")
                or "invalid provider routing or image hash"
            ),
        )
        return receipt
    try:
        verdict = dict(
            _extract_json_object(str(witness.get("answer") or ""))
        )
    except (ValueError, json.JSONDecodeError) as exc:
        receipt.update(
            status="FAIL",
            reason_code="SOURCE_COMPOSITION_VERDICT_UNPARSEABLE",
            detail=f"{type(exc).__name__}: {exc}",
        )
        return receipt
    receipt["verdict"] = verdict
    if not _verdict_is_coherent(verdict, scene_kind=scene):
        receipt.update(
            status="FAIL",
            reason_code="SOURCE_COMPOSITION_VERDICT_INVALID",
            detail="bbox, boolean fields, reason, or route recommendation is invalid",
        )
        return receipt
    receipt["status"] = "PASS"
    return receipt


def source_composition_scene_kind(verification: object) -> str:
    """Read the scene a receipt was actually asked under (absent → talk)."""

    if not isinstance(verification, Mapping):
        return TALK_SCENE
    return normalize_cover_scene_kind(verification.get("scene_kind"))


def validate_source_composition_verification(
    verification: object,
    *,
    reference_sha256: str,
) -> bool:
    """Revalidate a source-composition receipt at its consumption point."""

    if not isinstance(verification, Mapping):
        return False
    witness = verification.get("witness")
    verdict = verification.get("verdict")
    if not isinstance(witness, Mapping) or not isinstance(verdict, Mapping):
        return False
    # 一份回执不能自称游戏场却带着 talk 提问的答案（反之亦然）：两套字段集互不
    # 重叠，coherence 自带这道结构绑定，无需再信任任何自述标签。未知 scene 标签
    # 直接判非法，避免拼错的标签被静默当成 talk 放行。
    if "scene_kind" in verification and (
        verification.get("scene_kind") not in COVER_SCENE_KINDS
    ):
        return False
    scene = source_composition_scene_kind(verification)
    witness_image_sha = "sha256:" + str(witness.get("image_sha256") or "")
    return bool(
        verification.get("schema_version") == SCHEMA_VERSION
        and verification.get("authority") == AUTHORITY
        and verification.get("status") == "PASS"
        and verification.get("expected_reference_sha256")
        == reference_sha256
        and verification.get("reference_sha256") == reference_sha256
        and witness_image_sha == reference_sha256
        and verification.get("witness_receipt_sha256")
        == _canonical_sha256(witness)
        and _routing_valid(witness)
        and _verdict_is_coherent(verdict, scene_kind=scene)
    )


def source_composition_recommends_redraw(verification: object) -> bool:
    """witness 的否决权收窄到两个**几何**布尔。

    此前只要 `cpa_redraw_recommended` 为真就整张重绘，而该字段是三个条件的或：
    脸不完整 / 无法忠实裁成大主体 / **源图不承载故事反应**。第三条是故事判断不是
    几何判断，而 70-cover.md 的分工是「源帧没拍到的故事由 narrative_presentation
    → COVER_TEXT 承担，不是像素义务」——反应缺失的正确出路是降级 polish 或换帧
    重选，不是单独触发整张重画。前两条则是真几何不可能（角落小豆沙 polish 完还是
    角落小豆沙，下游显著性门必死），保留否决。
    """

    if not isinstance(verification, Mapping):
        return False
    verdict = verification.get("verdict")
    if not isinstance(verdict, Mapping):
        return False
    if source_composition_scene_kind(verification) == GAME_SCENE:
        # 游戏场（维护者）：几何否决只剩「小窗不可见」与「画面无聊」。
        # "她不能成为大主体"在这里不是缺陷，是这类封面的定义。
        return (
            verdict.get("host_window_visible") is False
            or verdict.get("frame_is_interesting") is False
        )
    return (
        verdict.get("source_face_complete") is False
        or verdict.get("faithful_crop_can_make_dominant") is False
    )


def source_composition_supports_subject(verification: object) -> bool:
    """witness 是否给出了可用作**置信来源**的主体证据。

    这才是 memory 里那条「真脸部识别放大要接 CPA 视觉裁判」的本意：
    运动几何分不清竖版手游列和皮套（辣妹案）时，让 CPA 视觉找到真脸以便忠实放大。
    字段清单本身就是铁证——纯否决器不需要返回 `lidousha_bbox_frac`，更不需要
    `_bbox_crop_box` 那个带脸部余量的 16:9 裁切实现。
    """

    if not isinstance(verification, Mapping):
        return False
    verdict = verification.get("verdict")
    if not isinstance(verdict, Mapping):
        return False
    if source_composition_scene_kind(verification) == GAME_SCENE:
        # 游戏场恒 False，且这是**故意**的：`subject_confident` 的语义就是
        # 「她能当大主体」，而 维护者 8/9 明说游戏场不要求这个。返回 False 让路由
        # 落到既有的 camera-window 分支（`publish_staging.py` 小窗回归，7/25 维护者
        # 授权）——那条分支在见证在场时原本永远不可达，正是 C5 的死代码。
        return False
    return bool(
        verdict.get("faithful_crop_can_make_dominant") is True
        and _valid_bbox(verdict.get("lidousha_bbox_frac"))
    )


def source_composition_bbox(verification: object) -> list[float] | None:
    if not isinstance(verification, Mapping):
        return None
    verdict = verification.get("verdict")
    bbox = verdict.get("lidousha_bbox_frac") if isinstance(verdict, Mapping) else None
    if not _valid_bbox(bbox):
        return None
    assert isinstance(bbox, list)
    return [float(value) for value in bbox]


def _bbox_crop_box(
    bbox_frac: Sequence[float], *, width: int, height: int
) -> tuple[int, int, int, int]:
    """Expand an identity bbox into a bounded 16:9 crop with face margin."""

    x0, y0, x1, y1 = (float(value) for value in bbox_frac)
    bbox_w = (x1 - x0) * width
    bbox_h = (y1 - y0) * height
    center_x = ((x0 + x1) / 2.0) * width
    center_y = ((y0 + y1) / 2.0) * height
    crop_w = max(bbox_w * 1.38, bbox_h * 1.38 * 16.0 / 9.0)
    crop_h = crop_w * 9.0 / 16.0
    if crop_h > height:
        crop_h = float(height)
        crop_w = crop_h * 16.0 / 9.0
    if crop_w > width:
        crop_w = float(width)
        crop_h = crop_w * 9.0 / 16.0
    left = min(max(0.0, center_x - crop_w / 2.0), width - crop_w)
    top = min(max(0.0, center_y - crop_h / 2.0), height - crop_h)
    right = left + crop_w
    bottom = top + crop_h
    return (
        int(round(left)),
        int(round(top)),
        int(round(right)),
        int(round(bottom)),
    )


CROP_NOT_AUTHORIZED = "SOURCE_COMPOSITION_CROP_NOT_AUTHORIZED"
BBOX_INVALID = "SOURCE_COMPOSITION_BBOX_INVALID"
NO_CROP_COMPOSITOR = "HASH_BOUND_FULL_FRAME_NO_CROP_COMPOSITOR"
# 只有这两种失败可以退到"全幅不裁"：它们说的都是**裁切**这一步不可行，源帧本身
# 仍是 hash-bound 的真实瞬间。回执本身不可信（VERIFICATION_INVALID）绝不在此列
# ——那是 fail-closed 的射程，退到全幅等于用一份废回执放行像素。
_FULL_FRAME_FALLBACK_REASONS = (CROP_NOT_AUTHORIZED, BBOX_INVALID)


def extract_authority_source_crop_or_full_frame(
    *,
    reference_path: Path,
    output_path: Path,
    frame_ms: int,
    verification: Mapping[str, object],
    verification_receipt_path: Path,
    verification_receipt_sha256: str,
) -> dict[str, object]:
    """Crop when authorized, else keep the uncropped hash-bound frame.

    把"重绘是兜底"从口号变成控制流事实（治 C6）。此前截图物化只要
    裁不出来就整条降级 `cpa_redraw`，而关系路线早就在用
    ``HASH_BOUND_FULL_FRAME_NO_CROP_COMPOSITOR``——同一份 hash-bound 源帧，不裁，
    整幅装进海报底板。裁不动 ≠ 这一帧不能当封面，所以先落全幅，重绘留到全幅也
    失败之后。

    放宽的是**尝试权**不是**验收**：全幅成品照样要过 polish 整脸门、缩略图文字门
    和 `cover_host_identity_gate` 的最终像素显著性门；角落小人只是从"不许尝试"
    变成"尝试后被拒"，公开面一寸没松。
    """

    try:
        return extract_authority_source_crop(
            reference_path=reference_path,
            output_path=output_path,
            frame_ms=frame_ms,
            verification=verification,
            verification_receipt_path=verification_receipt_path,
            verification_receipt_sha256=verification_receipt_sha256,
        )
    except ValueError as exc:
        if str(exc) not in _FULL_FRAME_FALLBACK_REASONS:
            raise
        fallback_reason = str(exc)
    evidence = _extract_full_frame_no_crop(
        reference_path=Path(reference_path),
        reference_sha256=_sha256(Path(reference_path)),
        output_path=output_path,
        frame_ms=frame_ms,
        verification=verification,
        verification_receipt_path=verification_receipt_path,
        verification_receipt_sha256=verification_receipt_sha256,
    )
    evidence["status"] = NO_CROP_COMPOSITOR
    evidence["crop_fallback_reason_code"] = fallback_reason
    return evidence


def _extract_full_frame_no_crop(
    *,
    reference_path: Path,
    reference_sha256: str,
    output_path: Path,
    frame_ms: int,
    verification: Mapping[str, object],
    verification_receipt_path: Path,
    verification_receipt_sha256: str,
) -> dict[str, object]:
    """Write the exact reference frame as the screenshot base, uncropped."""

    with Image.open(reference_path) as raw_image:
        source = raw_image.convert("RGB")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    source.save(output_path, format="PNG", optimize=False)
    return {
        "schema": "cover-frame-transfer.v2",
        "status": "HASH_BOUND_FULL_FRAME",
        "frame_ms": int(frame_ms),
        "source_path": str(reference_path),
        "source_sha256": reference_sha256,
        "reference_sha256": reference_sha256,
        "crop_applied": False,
        "full_frame_preserved": True,
        "source_size": [source.width, source.height],
        "zoom": 1.0,
        "camera_window_crop": False,
        "authority_identity_crop": False,
        "authority_bbox_frac": source_composition_bbox(verification),
        "motion_bbox_role": "CANDIDATE_ONLY_NOT_AUTHORITY",
        "source_composition_schema_version": SCHEMA_VERSION,
        "source_composition_witness_sha256": str(
            verification.get("witness_receipt_sha256") or ""
        ),
        "source_composition_receipt_path": str(verification_receipt_path),
        "source_composition_receipt_sha256": verification_receipt_sha256,
        "crop_output_sha256": _sha256(output_path),
    }


def _extract_game_scene_full_frame(
    *,
    reference_path: Path,
    reference_sha256: str,
    output_path: Path,
    frame_ms: int,
    verification: Mapping[str, object],
    verification_receipt_path: Path,
    verification_receipt_sha256: str,
) -> dict[str, object]:
    """Keep the whole game frame — the game IS the subject (维护者).

    「如果是截图封面的话，当然不要求李豆沙在画面里占主要部分，毕竟是游戏截图，
    只要截图足够有趣就行，主体肯定会会是游戏。」把小窗 1.38x 裁出来当封面恰好
    做反了：那样丢掉的正是 维护者 要的游戏画面，而且等于用截图重演一次"角落小人
    放大成大头"——重绘 lane 做这件事本来就更强。所以游戏场用**原样全幅**，
    她的小窗 bbox 只作披露，供最终身份门定位。
    """

    verdict = verification["verdict"]
    assert isinstance(verdict, Mapping)
    if (
        verdict.get("host_window_visible") is not True
        or verdict.get("frame_is_interesting") is not True
    ):
        raise ValueError(CROP_NOT_AUTHORIZED)
    bbox = source_composition_bbox(verification)
    if bbox is None:
        raise ValueError(BBOX_INVALID)
    evidence = _extract_full_frame_no_crop(
        reference_path=reference_path,
        reference_sha256=reference_sha256,
        output_path=output_path,
        frame_ms=frame_ms,
        verification=verification,
        verification_receipt_path=verification_receipt_path,
        verification_receipt_sha256=verification_receipt_sha256,
    )
    evidence["status"] = "HASH_BOUND_GAME_SCENE_FULL_FRAME"
    evidence["scene_kind"] = GAME_SCENE
    evidence["host_window_bbox_frac"] = bbox
    return evidence


def extract_authority_source_crop(
    *,
    reference_path: Path,
    output_path: Path,
    frame_ms: int,
    verification: Mapping[str, object],
    verification_receipt_path: Path,
    verification_receipt_sha256: str,
) -> dict[str, object]:
    """Crop exact reference pixels around the CPA identity bbox.

    执行端授权式收窄到与路由端一致的**两个几何布尔**（治 C1/C2）。
    此前这里是四布尔 AND，多出的两条是：

    * ``source_carries_story_reaction`` ——`9f51987` 已经把它从路由端
      的否决集合里拿掉，理由自己写在 `source_composition_recommends_redraw` 的
      docstring 里（故事由 `narrative_presentation → COVER_TEXT` 承担，
      `docs/pipeline/70-cover.md`），但执行端漏改，于是路由放行的同一类候选被后门
      原样拦回。8/7–8/8 两场 20/20 次降级全部出自这里。
    * ``cpa_redraw_recommended`` —— 它是前三者的派生量（见 `_verdict_is_coherent`
      的一致性约束），本就不该在授权式里单独出现；留着它等于把已经拿掉的第三条
      从后门再放回来。

    保留的两条是真几何不可能：脸不完整、或忠实裁切也成不了大主体（7/26 1411
    角落小人案），下游显著性门必死，提前拒绝是省钱不是收权。
    """

    reference_sha256 = _sha256(Path(reference_path))
    if not validate_source_composition_verification(
        verification,
        reference_sha256=reference_sha256,
    ):
        raise ValueError("SOURCE_COMPOSITION_VERIFICATION_INVALID")
    verdict = verification["verdict"]
    assert isinstance(verdict, Mapping)
    scene = source_composition_scene_kind(verification)
    if scene == GAME_SCENE:
        return _extract_game_scene_full_frame(
            reference_path=Path(reference_path),
            reference_sha256=reference_sha256,
            output_path=output_path,
            frame_ms=frame_ms,
            verification=verification,
            verification_receipt_path=verification_receipt_path,
            verification_receipt_sha256=verification_receipt_sha256,
        )
    if (
        verdict.get("source_face_complete") is not True
        or verdict.get("faithful_crop_can_make_dominant") is not True
    ):
        raise ValueError(CROP_NOT_AUTHORIZED)
    bbox = source_composition_bbox(verification)
    if bbox is None:
        raise ValueError(BBOX_INVALID)
    with Image.open(reference_path) as raw_image:
        source = raw_image.convert("RGB")
    crop_box = _bbox_crop_box(
        bbox,
        width=source.width,
        height=source.height,
    )
    cropped = source.crop(crop_box).resize(
        (1920, 1080),
        Image.Resampling.LANCZOS,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    cropped.save(output_path, format="PNG", optimize=False)
    witness_sha = str(verification.get("witness_receipt_sha256") or "")
    return {
        "schema": "cover-frame-transfer.v2",
        "status": "HASH_BOUND_CPA_IDENTITY_CROP",
        "frame_ms": int(frame_ms),
        "source_path": str(reference_path),
        "source_sha256": reference_sha256,
        "reference_sha256": reference_sha256,
        "crop_applied": True,
        "crop_box": list(crop_box),
        "source_size": [source.width, source.height],
        "zoom": round(source.width / max(1, crop_box[2] - crop_box[0]), 4),
        "camera_window_crop": True,
        "authority_identity_crop": True,
        "authority_bbox_frac": bbox,
        "motion_bbox_role": "CANDIDATE_ONLY_NOT_AUTHORITY",
        "source_composition_schema_version": SCHEMA_VERSION,
        "source_composition_witness_sha256": witness_sha,
        "source_composition_receipt_path": str(verification_receipt_path),
        "source_composition_receipt_sha256": verification_receipt_sha256,
        "crop_output_sha256": _sha256(output_path),
    }
