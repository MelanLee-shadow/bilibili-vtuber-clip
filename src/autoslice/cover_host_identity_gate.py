"""Final-pixel host identity and subject-prominence gate.

The image generator may copy the wrong person from a multi-person reference
and then add one or two panda-like details. It may also preserve the host's
identity while shrinking her into a corner, leaving dead space, or adding
meaningless graphic bars. A prompt is not evidence that the result is a useful
thumbnail. This module builds a hash-bound SOURCE/FINAL comparison image and
asks CPA for a distinct strict identity plus composition verdict. AGY is a
fallback witness only when CPA vision is unavailable.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Callable, Mapping, MutableMapping, NamedTuple, Sequence

from PIL import Image, ImageDraw, ImageOps

from src.autoslice.surface_canon import CHANNEL_PROFILE


SCHEMA_VERSION = "lidousha-cover-final-host-identity-verification.v3"
AUTHORITY = (
    "CPA_PRIMARY_HASH_BOUND_SOURCE_FINAL_IDENTITY_AND_PROMINENCE_COMPARISON"
)
# 冻结出版结转条款（2026-08-01，1013 r18 案）：字节等同复用一张**已发布**
# 封面时，其发布时点的身份见证是该字节的既成证据；对同一字节按今日更严的
# v3 门重考属于对冻结资产做 live 政策重算（7/27 裁定禁止），且视觉裁判对
# 边界样本非确定（r17 PASS / r18 FAIL 同字节）。仅当 bundle 显式声明
# carried_forward_from_published_record 且见证恰为下列历史世代对时才放行，
# 其余（哈希绑定、PASS、provider 路由）与 v3 同标准。
PUBLISHED_CARRY_SCHEMA_VERSION = (
    "lidousha-cover-final-host-identity-verification.v2"
)
PUBLISHED_CARRY_AUTHORITY = (
    "CPA_PRIMARY_HASH_BOUND_SOURCE_FINAL_IDENTITY_COMPARISON"
)
UNAVAILABLE_REASON_CODES = frozenset(
    {
        "HOST_IDENTITY_WITNESS_UNAVAILABLE",
        "HOST_IDENTITY_VERDICT_UNPARSEABLE",
        "HOST_IDENTITY_VERIFIER_EXCEPTION",
        "HOST_IDENTITY_VERIFIER_MISSING",
    }
)
# 自不一致 **不是** 见证缺席。它绝不进 UNAVAILABLE_REASON_CODES：那个集合会让
# publish_staging 的 `_keep_source_pixels_when_identity_witness_is_down` 把重绘
# 降级成源截图像素。一条互相打架的回答说明见证**到场了**，只是不可信；把它当
# 缺席等于给自己开一条"矛盾即降级发原图"的侧门。
SELF_INCONSISTENT_REASON_CODE = "FINAL_HOST_IDENTITY_WITNESS_SELF_INCONSISTENT"

# Ivan 2026-08-10 逐字裁定（本机制的唯一授权来源）。
IVAN_SELF_INCONSISTENT_RULING = (
    "Ivan 2026-08-10 逐字裁定：「只要自相矛盾，当然就认为这个完全没有否决权，"
    "完全不可信就完事了。」"
)
# 先例：7/27 BV1ec3A6bEWF 事故轮已立同名机制（R-裁定-06「自不一致的测量没有
# 否决权」，当时给的是出版登记/听写门）。本次是 Ivan 亲口把它扩到封面身份门。
SELF_INCONSISTENT_PRECEDENT = (
    "R-裁定-06 自不一致的测量没有否决权（2026-07-27 刘若莎案 / BV1ec3A6bEWF）"
)
SELF_INCONSISTENT_SCHEMA_VERSION = (
    "lidousha-cover-host-identity-self-inconsistent-witness.v1"
)
SELF_INCONSISTENT_DISREGARDED_STATUS = "SELF_INCONSISTENT_WITNESS_DISREGARDED"
SELF_INCONSISTENT_REFUSED_STATUS = "SELF_INCONSISTENT_DISREGARD_REFUSED"
# 承接证据只认这一族回执，且只认本模块知道怎么逐条重放的世代。Ivan 的裁定写的
# 是 `lidousha-title-cover-joint-qc.*`；新增一个世代必须同时补它的重放规则，
# 否则未知世代按 fail-closed 拒绝，而不是靠前缀通配放行。
JOINT_QC_SCHEMA_VERSIONS = frozenset({"lidousha-title-cover-joint-qc.v1"})
JOINT_QC_WITNESS_SCHEMA_VERSION = "cpa-frame-witness.v1"


class _Contradiction(NamedTuple):
    """One enumerated pair of assertions that cannot both hold in one answer."""

    name: str
    fields: tuple[str, ...]
    why: str
    holds: Callable[[Mapping[str, object]], bool]


def _protagonist_is_host_and_other_participant(verdict: Mapping[str, object]) -> bool:
    """主角同时"是李豆沙"和"是另一位参与者"，且自称零冲突特征。

    互斥理由：见证问卷把 `primary_subject_matches_other_source_participant`
    定义为"右图主角其实延续的是左图**其他**参与者，而不是李豆沙"。它与
    `primary_subject_is_lidousha` 是同一命题的正反两面——A 与 ¬A。第三个合取项
    `identity_conflicts == []` 是 Ivan 的护栏：见证一旦列出了冲突特征，那是
    **明确否定**（"我看到她带着别人的特征"），是可读的反对意见，必须保留完整
    否决权；只有连一条冲突都举不出来、却仍勾上反面断言时，才是纯粹的自相矛盾。
    第四个合取项 `source_lidousha_located is True` 同理：源图定位失败是身份轴上
    的明确否定，不是矛盾，照样有否决权。

    这正是 1323 打歌服置换封面 v4D 的形状：同一份回答里
    `primary_subject_is_lidousha: true` + `identity_conflicts: []` + 文字描述
    明确认出李豆沙，却又 `primary_subject_matches_other_source_participant: true`。
    """

    return (
        verdict.get("source_lidousha_located") is True
        and verdict.get("primary_subject_is_lidousha") is True
        and verdict.get("primary_subject_matches_other_source_participant") is True
        and isinstance(verdict.get("identity_conflicts"), list)
        and not verdict["identity_conflicts"]
    )


# 全 schema 扫描结论：`_QUESTION` 契约里只有身份轴存在严格的 A ∧ ¬A 对。
# 逐条记下被考虑并**排除**的候选，免得后人以为是漏扫：
#
# * `source_lidousha_located=False` ∧ `primary_subject_is_lidousha=True`
#   —— 跨轴张力，不是同一命题的正反面。"我在左图找不到她"与"右图主角是她"可以
#   同时成立（凭外形认人而非凭源图延续），而且前者本身就是身份轴上的明确否定。
#   纳入它等于把"源图定位失败"洗成可放行，正面击穿反混种设计。
# * `primary_subject_is_lidousha=False` ∧ `..._matches_other_source_participant=False`
#   —— 不互斥：主角可以既不是她也不是任何源图参与者（凭空捏的人）。而且这是
#   明确否定，Ivan 的护栏 4 直接禁止本机制放行它。
# * `composition_conflicts` 非空 ∧ 六个构图布尔全绿
#   —— 构图布尔是分项判断，conflicts 是自由文本清单；列出"字略挤"之类的次要
#   意见并不构成对任一布尔的反面断言。且构图轴没有一对互为否定的字段，纳入它
#   只会把"机器说构图有问题"洗掉，属于扩权。
# * `excessive_dead_space=True` ∧ `primary_subject_is_visually_dominant=True`
#   —— 大留白与主体显眼可以并存（干净文字区就是刻意留白，问卷明说这合理）。
# * `thumbnail_has_clear_click_hook=True` ∧ `primary_subject_carries_story_reaction=False`
#   —— 钩子可以由文案/场景承担而非表情，不是同一命题。
_SELF_CONTRADICTIONS: tuple[_Contradiction, ...] = (
    _Contradiction(
        name="PROTAGONIST_IS_HOST_AND_OTHER_PARTICIPANT",
        fields=(
            "source_lidousha_located",
            "primary_subject_is_lidousha",
            "primary_subject_matches_other_source_participant",
            "identity_conflicts",
        ),
        why=(
            "同一份回答同时断言主角是李豆沙、且主角其实是另一位源图参与者，"
            "还自称零冲突特征——A 与 ¬A 不能同真"
        ),
        holds=_protagonist_is_host_and_other_participant,
    ),
)


def host_identity_verdict_contradictions(
    verdict: object,
) -> list[dict[str, object]]:
    """Enumerate same-answer mutually exclusive assertions in one verdict.

    只认**同一份回答内部**的互斥；跨回答/跨轮的分歧不是自相矛盾，走各自的门。
    """

    if not isinstance(verdict, Mapping):
        return []
    found: list[dict[str, object]] = []
    for contradiction in _SELF_CONTRADICTIONS:
        if not contradiction.holds(verdict):
            continue
        found.append(
            {
                "name": contradiction.name,
                "fields": list(contradiction.fields),
                "asserted": {
                    field: copy.deepcopy(verdict.get(field))
                    for field in contradiction.fields
                },
                "why": contradiction.why,
            }
        )
    return found


def _sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _extract_json_object(answer: str) -> Mapping[str, object]:
    start = answer.index("{")
    end = answer.rindex("}") + 1
    value = json.loads(answer[start:end])
    if not isinstance(value, Mapping):
        raise ValueError("verdict is not a JSON object")
    return value


_BOOLEAN_VERDICT_FIELDS = (
    "source_lidousha_located",
    "primary_subject_is_lidousha",
    "primary_subject_matches_other_source_participant",
    "primary_subject_is_visually_dominant",
    "primary_subject_face_is_large_and_clear",
    "primary_subject_carries_story_reaction",
    "excessive_dead_space",
    "meaningless_dominant_decoration",
    "thumbnail_has_clear_click_hook",
)


def _identity_answer_valid(answer: str) -> bool:
    try:
        verdict = _extract_json_object(answer)
    except (ValueError, json.JSONDecodeError):
        return False
    return bool(
        all(isinstance(verdict.get(key), bool) for key in _BOOLEAN_VERDICT_FIELDS)
        and isinstance(verdict.get("identity_conflicts"), list)
        and isinstance(verdict.get("composition_conflicts"), list)
        and all(
            isinstance(value, str) and value.strip()
            for key in ("identity_conflicts", "composition_conflicts")
            for value in verdict[key]
        )
        and isinstance(verdict.get("reason"), str)
        and str(verdict.get("reason") or "").strip()
    )


def _comparison_path(final_cover_path: Path) -> Path:
    return final_cover_path.with_name(
        final_cover_path.stem + ".host-identity-witness.png"
    )


def _build_comparison(
    *, reference_path: Path, final_cover_path: Path, output_path: Path
) -> None:
    """Render a deterministic source/final contact sheet for the witness."""

    canvas = Image.new("RGB", (1920, 620), (10, 16, 34))
    draw = ImageDraw.Draw(canvas)
    draw.rectangle((0, 0, 960, 72), fill=(22, 66, 124))
    draw.rectangle((960, 0, 1920, 72), fill=(116, 38, 68))
    draw.text((24, 22), "SOURCE REFERENCE", fill="white")
    draw.text((984, 22), "FINAL COVER", fill="white")
    with Image.open(reference_path) as source_image:
        source = ImageOps.contain(source_image.convert("RGB"), (940, 528))
    with Image.open(final_cover_path) as final_image:
        final = ImageOps.contain(final_image.convert("RGB"), (940, 528))
    canvas.paste(source, (10 + (940 - source.width) // 2, 82))
    canvas.paste(final, (970 + (940 - final.width) // 2, 82))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output_path, format="PNG", optimize=False)


_QUESTION = (
    "左侧是同一切片的 SOURCE REFERENCE，右侧是待发布 FINAL COVER。"
    f"请先在左图用可见名牌、服装和外形定位{CHANNEL_PROFILE.display_name}；若名牌可见，必须以名牌为准。"
    f"{CHANNEL_PROFILE.display_name}是{CHANNEL_PROFILE.cover_identity.gate_appearance_zh}。特别注意：不要把左图其他人物"
    f"{CHANNEL_PROFILE.cover_identity.gate_rival_note_zh}误认成{CHANNEL_PROFILE.display_name}；给别的角色"
    f"{CHANNEL_PROFILE.cover_identity.gate_imitation_zh}也不算身份正确。再判断右图最大、最显眼、承担故事反应的主角是否确实"
    f"延续左图{CHANNEL_PROFILE.display_name}，而不是其他参与者。次要人物可以存在，但主角必须是{CHANNEL_PROFILE.display_name}。"
    f"这是信息流缩略图终检，不只验身份：{CHANNEL_PROFILE.display_name}不能缩在角落或小到需要寻找；脸部必须"
    "足够大、完整、清楚，且她的表情/动作必须承担标题所讲事件的反应。通常脸或上半身"
    "应形成第一视觉焦点；仅仅能认出她不算通过。专门留给已渲染标题的干净文字区是合理"
    "留白，但标题以外不得有大片死空白、无意义纯色红条/色块、装饰噪声或与故事无关的"
    "强元素压过人物。陌生观众只看右图时应立即知道看谁、看到一个明确点击钩子。"
    "不确定就 FAIL。只输出 JSON："
    '{"source_lidousha_located":true|false,'
    '"primary_subject_is_lidousha":true|false,'
    '"primary_subject_matches_other_source_participant":true|false,'
    '"primary_subject_is_visually_dominant":true|false,'
    '"primary_subject_face_is_large_and_clear":true|false,'
    '"primary_subject_carries_story_reaction":true|false,'
    '"excessive_dead_space":true|false,'
    '"meaningless_dominant_decoration":true|false,'
    '"thumbnail_has_clear_click_hook":true|false,'
    '"primary_subject_identity":"简短身份",'
    '"identity_conflicts":["冲突特征"],'
    '"composition_conflicts":["主体过小/角落小人/死空白/无意义装饰等"],'
    '"reason":"简短中文说明"}'
)


def _pending_self_inconsistency_disclosure(
    *,
    verdict: Mapping[str, object],
    witness_answer: str,
    contradictions: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    """Typed disclosure for a contradiction with no successor evidence yet."""

    return {
        "schema_version": SELF_INCONSISTENT_SCHEMA_VERSION,
        "status": SELF_INCONSISTENT_REFUSED_STATUS,
        "reason_code": "CORROBORATING_EVIDENCE_MISSING",
        "authority": IVAN_SELF_INCONSISTENT_RULING,
        "precedent": SELF_INCONSISTENT_PRECEDENT,
        "contradictions": copy.deepcopy(list(contradictions)),
        # 原文保留：矛盾的回答本身是证物，不得改写或删除。
        "disregarded_verdict": copy.deepcopy(dict(verdict)),
        "disregarded_witness_answer": witness_answer,
    }


def _joint_qc_corroboration_problems(
    receipt: object,
    *,
    final_cover_sha256: str,
) -> list[str]:
    """Replay every bond of a title+cover joint-QC receipt, purely.

    这是 `scripts/authorized_upload.py` 的 title+cover QC 门的同款重放（本模块
    自带一份，不从 scripts/ 导入）。必须自带：同 BV 置换的 manifest 门把该回执
    列为**可选**，所以在 1323 这类 same-BV 场景里，本 validator 是这份承接证据
    唯一的执法点，不能指望上传时再查。
    """

    problems: list[str] = []
    if not isinstance(receipt, Mapping):
        return ["joint-QC receipt is not an object"]
    if receipt.get("schema_version") not in JOINT_QC_SCHEMA_VERSIONS:
        problems.append("joint-QC schema_version is not a replayable generation")
    expected_cover_sha = str(final_cover_sha256 or "")
    if not expected_cover_sha.startswith("sha256:"):
        problems.append("final cover sha256 is not a sha256: digest")
    if receipt.get("cover_sha256") != expected_cover_sha:
        problems.append("joint-QC cover_sha256 does not bind the final cover bytes")
    title = receipt.get("title")
    if not isinstance(title, str) or not title.strip():
        problems.append("joint-QC title is empty")
    elif receipt.get("title_sha256") != "sha256:" + hashlib.sha256(
        title.encode("utf-8")
    ).hexdigest():
        problems.append("joint-QC title_sha256 does not bind its own title")
    if receipt.get("selected_provider") != "cpa":
        problems.append("joint-QC selected_provider must be cpa")
    preferred_provider = receipt.get("preferred_provider")
    if preferred_provider is not None and preferred_provider != "cpa":
        problems.append("joint-QC preferred_provider must be cpa")

    witness = receipt.get("witness")
    if not isinstance(witness, Mapping):
        problems.append("joint-QC has no CPA witness object")
        witness = {}
    else:
        if witness.get("schema_version") != JOINT_QC_WITNESS_SCHEMA_VERSION:
            problems.append("joint-QC witness schema_version is invalid")
        if witness.get("provider") != "cpa":
            problems.append("joint-QC witness provider must be cpa")
        if witness.get("status") != "OBSERVED":
            problems.append("joint-QC CPA witness was not OBSERVED")
        if not str(witness.get("model") or "").strip():
            problems.append("joint-QC CPA witness has no model")
        if witness.get("image_path") != receipt.get("cover_path"):
            problems.append("joint-QC witness image_path is not the receipt cover")
        witness_sha = str(witness.get("image_sha256") or "")
        if not witness_sha.startswith("sha256:"):
            witness_sha = "sha256:" + witness_sha if witness_sha else ""
        if witness_sha != expected_cover_sha:
            problems.append("joint-QC witness image_sha256 is not the final cover")

    verdict = receipt.get("verdict")
    if not isinstance(verdict, Mapping):
        problems.append("joint-QC has no verdict object")
        verdict = {}
    for key, expected in (
        ("lidousha_primary", True),
        ("thumbnail_readable", True),
        ("single_clear_hook", True),
        ("text_overcrowded", False),
        ("title_cover_aligned", True),
        ("pass", True),
    ):
        if verdict.get(key) is not expected:
            problems.append(f"joint-QC verdict.{key} must be {str(expected).lower()}")
    line_count = verdict.get("physical_text_line_count")
    if isinstance(line_count, bool) or not isinstance(line_count, int) or line_count not in (1, 2):
        problems.append("joint-QC verdict.physical_text_line_count must be 1 or 2")
    if verdict.get("unrelated_or_misleading_elements") != []:
        problems.append("joint-QC verdict.unrelated_or_misleading_elements must be empty")
    if not str(verdict.get("reason") or "").strip():
        problems.append("joint-QC verdict.reason must be non-empty")

    answer = witness.get("answer") if isinstance(witness, Mapping) else None
    try:
        answer_verdict = json.loads(answer) if isinstance(answer, str) else None
    except ValueError:
        answer_verdict = None
    if answer_verdict != (dict(verdict) if isinstance(verdict, Mapping) else verdict):
        problems.append("joint-QC verdict is not the exact parsed CPA witness answer")

    if receipt.get("status") != "PASS":
        problems.append("joint-QC status must be PASS")
    if receipt.get("pass") is not True:
        problems.append("joint-QC top-level pass must be true")
    return problems


def verify_lidousha_final_host_identity(
    *,
    final_cover_path: Path,
    final_cover_sha256: str,
    reference_path: Path,
    base_url: str = "",
    api_key: str = "",
) -> dict[str, object]:
    """Return a fail-closed CPA-primary verdict bound to source/final bytes."""

    final_cover_path = Path(final_cover_path)
    reference_path = Path(reference_path)
    verification: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "authority": AUTHORITY,
        "final_cover_path": str(final_cover_path),
        "reference_path": str(reference_path),
    }
    try:
        actual_final_sha = _sha256(final_cover_path)
        reference_sha = _sha256(reference_path)
    except OSError as exc:
        verification.update(
            status="FAIL",
            reason_code="IDENTITY_INPUT_UNAVAILABLE",
            detail=f"{type(exc).__name__}: {exc}",
        )
        return verification
    verification.update(
        final_cover_sha256=actual_final_sha,
        reference_sha256=reference_sha,
    )
    if actual_final_sha != str(final_cover_sha256):
        verification.update(
            status="FAIL",
            reason_code="FINAL_COVER_HASH_MISMATCH",
            detail=f"expected {final_cover_sha256}, got {actual_final_sha}",
        )
        return verification

    comparison_path = _comparison_path(final_cover_path)
    try:
        _build_comparison(
            reference_path=reference_path,
            final_cover_path=final_cover_path,
            output_path=comparison_path,
        )
    except Exception as exc:
        verification.update(
            status="FAIL",
            reason_code="IDENTITY_COMPARISON_BUILD_FAILED",
            detail=f"{type(exc).__name__}: {exc}",
        )
        return verification

    from src.autoslice.visual_witness import image_vision_probe

    comparison_sha = _sha256(comparison_path)
    witness = image_vision_probe(
        comparison_path,
        _QUESTION,
        api_base=base_url,
        api_key=api_key,
        answer_validator=_identity_answer_valid,
    )
    verification.update(
        comparison_path=str(comparison_path),
        comparison_sha256=comparison_sha,
        witness=witness,
        preferred_witness_provider="cpa",
        selected_witness_provider=witness.get("provider"),
    )
    witness_sha = (
        "sha256:" + str(witness.get("image_sha256"))
        if witness.get("image_sha256")
        else ""
    )
    if witness.get("status") != "OBSERVED" or witness_sha != comparison_sha:
        verification.update(
            status="FAIL",
            reason_code="HOST_IDENTITY_WITNESS_UNAVAILABLE",
            detail=str(witness.get("error") or witness.get("status") or ""),
        )
        return verification
    try:
        verdict = dict(_extract_json_object(str(witness.get("answer") or "")))
    except (ValueError, json.JSONDecodeError) as exc:
        verification.update(
            status="FAIL",
            reason_code="HOST_IDENTITY_VERDICT_UNPARSEABLE",
            detail=f"{type(exc).__name__}: {exc}",
        )
        return verification
    verification["verdict"] = verdict
    contradictions = host_identity_verdict_contradictions(verdict)
    if contradictions:
        # 自不一致 → 这份 verdict 整体不可信：既不能否决，也不能当通过证据。
        # 默认仍然 fail-closed（本函数不认识承接证据），只把矛盾 typed 披露出来；
        # 是否放行由 `disregard_self_inconsistent_host_identity_witness` 显式挂上
        # 第三方证据后再由 validator 判定。
        verification.update(
            status="FAIL",
            reason_code=SELF_INCONSISTENT_REASON_CODE,
            detail=(
                "host identity witness contradicts itself in one answer: "
                + ", ".join(str(item["name"]) for item in contradictions)
            ),
            self_inconsistent_witness=_pending_self_inconsistency_disclosure(
                verdict=verdict,
                witness_answer=str(witness.get("answer") or ""),
                contradictions=contradictions,
            ),
        )
        return verification
    identity_conflicts = verdict.get("identity_conflicts")
    composition_conflicts = verdict.get("composition_conflicts")
    identity_passed = bool(
        verdict.get("source_lidousha_located") is True
        and verdict.get("primary_subject_is_lidousha") is True
        and verdict.get("primary_subject_matches_other_source_participant")
        is False
        and isinstance(identity_conflicts, list)
        and not identity_conflicts
    )
    composition_passed = bool(
        verdict.get("primary_subject_is_visually_dominant") is True
        and verdict.get("primary_subject_face_is_large_and_clear") is True
        and verdict.get("primary_subject_carries_story_reaction") is True
        and verdict.get("excessive_dead_space") is False
        and verdict.get("meaningless_dominant_decoration") is False
        and verdict.get("thumbnail_has_clear_click_hook") is True
        and isinstance(composition_conflicts, list)
        and not composition_conflicts
    )
    if identity_passed and composition_passed:
        verification["status"] = "PASS"
    elif identity_passed:
        verification.update(
            status="FAIL",
            reason_code="FINAL_COVER_SUBJECT_PROMINENCE_FAILED",
            detail=str(
                verdict.get("reason")
                or composition_conflicts
                or "host is identifiable but not a dominant clickworthy subject"
            ),
        )
    else:
        verification.update(
            status="FAIL",
            reason_code="FINAL_HOST_IDENTITY_MISMATCH",
            detail=str(
                verdict.get("reason")
                or identity_conflicts
                or "identity mismatch"
            ),
        )
    return verification


def disregard_self_inconsistent_host_identity_witness(
    cover_generation: MutableMapping[str, object],
    *,
    joint_qc_receipt_path: Path | str,
) -> dict[str, object]:
    """Attach the successor evidence that carries a disregarded witness's job.

    Ivan 2026-08-10 亲裁：自相矛盾的见证「完全没有否决权、完全不可信」。它既不
    否决也不放行，门的结论改由其余独立证据承担——同包的 title+cover 联合 QC
    回执。两者都缺就仍然 fail-closed。

    这是 integrator 的**显式**一步：不接进 publish_staging，也不接进 runner。
    生成时刻根本没有联合 QC 回执，自动挂载只会变成"矛盾即自动放行"。

    磁盘 I/O 在这里做（读回执、核对成品封面真实字节）；`validate_final_host_
    identity_verification` 保持纯函数，对内嵌副本逐条重放同样的绑定。
    """

    verification = cover_generation.get("final_host_identity_verification")
    receipt_path = Path(joint_qc_receipt_path)

    def _refuse(reason_code: str, detail: object) -> dict[str, object]:
        base: dict[str, object]
        if isinstance(verification, Mapping) and isinstance(
            verification.get("self_inconsistent_witness"), Mapping
        ):
            base = copy.deepcopy(dict(verification["self_inconsistent_witness"]))
        else:
            base = {
                "schema_version": SELF_INCONSISTENT_SCHEMA_VERSION,
                "authority": IVAN_SELF_INCONSISTENT_RULING,
                "precedent": SELF_INCONSISTENT_PRECEDENT,
                "contradictions": [],
            }
        base.update(
            status=SELF_INCONSISTENT_REFUSED_STATUS,
            reason_code=reason_code,
            detail=detail,
            corroborating_receipt_path=str(receipt_path),
        )
        if isinstance(verification, MutableMapping):
            verification["self_inconsistent_witness"] = base
        return base

    if not isinstance(verification, Mapping):
        return _refuse("IDENTITY_VERIFICATION_MISSING", "no verification receipt")
    contradictions = host_identity_verdict_contradictions(verification.get("verdict"))
    if not contradictions:
        # 没有确凿矛盾就没有可豁免的东西。明确否定（identity_conflicts 非空、
        # 或 primary_subject_is_lidousha=False）走这条分支被拒——它们保留完整
        # 否决权，本机制不得替它们开门。
        return _refuse(
            "SELF_INCONSISTENCY_ABSENT",
            "verdict is not self-inconsistent; its verdict keeps full veto power",
        )
    if verification.get("reason_code") != SELF_INCONSISTENT_REASON_CODE:
        return _refuse(
            "IDENTITY_VERIFICATION_REASON_CODE_MISMATCH",
            str(verification.get("reason_code") or ""),
        )
    witness = verification.get("witness")
    try:
        parsed_answer = (
            dict(_extract_json_object(str(witness.get("answer") or "")))
            if isinstance(witness, Mapping)
            else None
        )
    except (ValueError, json.JSONDecodeError):
        parsed_answer = None
    if parsed_answer != dict(verification.get("verdict") or {}):
        # 防伪造：改写 verdict 就能把一条"明确否定"装成"自相矛盾"。verdict 必须
        # 仍是见证回答的逐字解析结果。validator 也独立再查一遍。
        return _refuse(
            "VERDICT_IS_NOT_THE_PARSED_WITNESS_ANSWER",
            "preserved verdict diverges from the witness answer verbatim",
        )

    final_cover_sha256 = str(cover_generation.get("final_cover_sha256") or "")
    try:
        actual_cover_sha = _sha256(Path(str(cover_generation.get("final_cover") or "")))
        receipt_bytes = receipt_path.read_bytes()
    except OSError as exc:
        return _refuse("CORROBORATING_INPUT_UNREADABLE", f"{type(exc).__name__}: {exc}")
    if actual_cover_sha != final_cover_sha256:
        return _refuse(
            "FINAL_COVER_BYTES_DRIFTED",
            f"declared {final_cover_sha256}, on disk {actual_cover_sha}",
        )
    try:
        receipt = json.loads(receipt_bytes.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        return _refuse("CORROBORATING_RECEIPT_UNPARSEABLE", f"{type(exc).__name__}: {exc}")
    problems = _joint_qc_corroboration_problems(
        receipt, final_cover_sha256=actual_cover_sha
    )
    if problems:
        return _refuse("CORROBORATING_RECEIPT_UNBOUND", problems)

    disclosure = copy.deepcopy(dict(verification["self_inconsistent_witness"])) if isinstance(
        verification.get("self_inconsistent_witness"), Mapping
    ) else _pending_self_inconsistency_disclosure(
        verdict=dict(verification.get("verdict") or {}),
        witness_answer=str(
            (verification.get("witness") or {}).get("answer")
            if isinstance(verification.get("witness"), Mapping)
            else ""
        ),
        contradictions=contradictions,
    )
    disclosure.update(
        status=SELF_INCONSISTENT_DISREGARDED_STATUS,
        reason_code=None,
        detail=None,
        contradictions=copy.deepcopy(contradictions),
        corroborating_evidence={
            "kind": "title_cover_joint_qc",
            "receipt_path": str(receipt_path),
            "receipt_sha256": "sha256:" + hashlib.sha256(receipt_bytes).hexdigest(),
            "schema_version": receipt.get("schema_version"),
            "cover_sha256": receipt.get("cover_sha256"),
            "receipt": copy.deepcopy(receipt),
        },
    )
    if isinstance(verification, MutableMapping):
        verification["self_inconsistent_witness"] = disclosure
    return disclosure


def _self_inconsistent_disregard_valid(
    verification: Mapping[str, object],
    *,
    final_cover_sha256: str,
) -> bool:
    """Pure replay of the disregard branch: contradiction + successor evidence."""

    # 本豁免只对**当前 v3 世代**的见证成立，绝不搭冻结出版结转（v2）那条线：
    # ①矛盾对的互斥性是从 v3 `_QUESTION` 的字段定义推出来的，v2 世代从没承诺
    # 过同一套语义；②结转条款的全部理由是"发布时点那份见证 PASS 过，对这串
    # 字节是既成证据"——一份结转过来的 FAIL/自不一致回执什么都没证成，让它进
    # 豁免通道等于把机制扩到 Ivan 没裁过的世代上。
    if (
        verification.get("schema_version") != SCHEMA_VERSION
        or verification.get("authority") != AUTHORITY
    ):
        return False
    if verification.get("status") != "FAIL":
        return False
    if verification.get("reason_code") != SELF_INCONSISTENT_REASON_CODE:
        return False
    verdict = verification.get("verdict")
    if not isinstance(verdict, Mapping):
        return False
    # 矛盾判定永远从**保留的原文 verdict** 现算，绝不信披露里的任何布尔标记。
    contradictions = host_identity_verdict_contradictions(verdict)
    if not contradictions:
        return False
    # verdict 必须仍是见证回答的逐字解析结果，否则可以靠改写 verdict 把一条
    # "明确否定"伪装成"自相矛盾"再走本分支放行。
    witness = verification.get("witness")
    if not isinstance(witness, Mapping):
        return False
    try:
        parsed_answer = dict(_extract_json_object(str(witness.get("answer") or "")))
    except (ValueError, json.JSONDecodeError):
        return False
    if parsed_answer != dict(verdict):
        return False
    disclosure = verification.get("self_inconsistent_witness")
    if not isinstance(disclosure, Mapping):
        return False
    if disclosure.get("schema_version") != SELF_INCONSISTENT_SCHEMA_VERSION:
        return False
    if disclosure.get("status") != SELF_INCONSISTENT_DISREGARDED_STATUS:
        return False
    if disclosure.get("authority") != IVAN_SELF_INCONSISTENT_RULING:
        return False
    if list(disclosure.get("contradictions") or []) != contradictions:
        return False
    if dict(disclosure.get("disregarded_verdict") or {}) != dict(verdict):
        return False
    evidence = disclosure.get("corroborating_evidence")
    if not isinstance(evidence, Mapping) or evidence.get("kind") != "title_cover_joint_qc":
        return False
    if not str(evidence.get("receipt_path") or "").strip():
        return False
    if not str(evidence.get("receipt_sha256") or "").startswith("sha256:"):
        return False
    receipt = evidence.get("receipt")
    if not isinstance(receipt, Mapping):
        return False
    if evidence.get("schema_version") != receipt.get("schema_version"):
        return False
    if evidence.get("cover_sha256") != receipt.get("cover_sha256"):
        return False
    return not _joint_qc_corroboration_problems(
        receipt, final_cover_sha256=final_cover_sha256
    )


def validate_final_host_identity_verification(
    cover_generation: Mapping[str, object],
) -> bool:
    """Validate receipt shape and exact final-cover hash binding."""

    verification = cover_generation.get("final_host_identity_verification")
    if not isinstance(verification, Mapping):
        return False
    witness = verification.get("witness")
    comparison_sha = str(verification.get("comparison_sha256") or "")
    witness_sha = (
        "sha256:" + str(witness.get("image_sha256"))
        if isinstance(witness, Mapping) and witness.get("image_sha256")
        else ""
    )
    provider = str(witness.get("provider") or "") if isinstance(witness, Mapping) else ""
    routing = witness.get("routing") if isinstance(witness, Mapping) else None
    primary_receipt = (
        routing.get("primary_receipt") if isinstance(routing, Mapping) else None
    )
    provider_route_valid = bool(
        provider == "cpa"
        or (
            provider == "agy"
            and isinstance(routing, Mapping)
            and routing.get("preferred_provider") == "cpa"
            and routing.get("fallback_used") is True
            and routing.get("primary_status") != "OBSERVED"
            and isinstance(primary_receipt, Mapping)
            and primary_receipt.get("provider") == "cpa"
        )
    )
    generation_pin_valid = (
        verification.get("schema_version") == SCHEMA_VERSION
        and verification.get("authority") == AUTHORITY
    ) or (
        # 冻结出版结转条款：仅字节等同结转 bundle + 恰为 v2 历史世代对。
        cover_generation.get("carried_forward_from_published_record") is True
        and verification.get("schema_version")
        == PUBLISHED_CARRY_SCHEMA_VERSION
        and verification.get("authority") == PUBLISHED_CARRY_AUTHORITY
    )
    # 结论面二选一：①见证自己给出 PASS；②见证自相矛盾被整体作废，结论由
    # 第三方承接证据承担（Ivan 2026-08-10 亲裁）。除这一项外，其余每条合取
    # ——世代锁、成品字节绑定、对比图哈希绑定、provider 路由——一律照旧。
    # 否则一份"矛盾形状"但根本没打过见证的回执就能只凭联合 QC 放行，正是
    # Ivan 禁的"没有 witness 也能过"的侧门。
    verdict_lane_valid = verification.get(
        "status"
    ) == "PASS" or _self_inconsistent_disregard_valid(
        verification,
        final_cover_sha256=str(cover_generation.get("final_cover_sha256") or ""),
    )
    return bool(
        generation_pin_valid
        and verdict_lane_valid
        and verification.get("final_cover_sha256")
        == cover_generation.get("final_cover_sha256")
        and comparison_sha.startswith("sha256:")
        and witness_sha == comparison_sha
        and provider_route_valid
    )


def final_host_identity_witness_unavailable(
    cover_generation: Mapping[str, object],
) -> bool:
    """Distinguish missing identity evidence from an observed mismatch."""

    verification = cover_generation.get("final_host_identity_verification")
    return bool(
        isinstance(verification, Mapping)
        and verification.get("status") == "FAIL"
        and verification.get("reason_code") in UNAVAILABLE_REASON_CODES
    )
