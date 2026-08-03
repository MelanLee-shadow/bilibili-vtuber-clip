"""Pure validation for byte-bound final-human-review evidence v2."""

from __future__ import annotations

import difflib
import re
import unicodedata
from typing import Any, Mapping


SCHEMA_VERSION = "lidousha-final-human-review-evidence.v2"
ARTIFACT_FIELDS = ("video", "subtitle", "cover")
CHECK_FIELDS = (
    "final_burned_full_playback",
    "subtitle_audio",
    "silence_hallucination",
    "boundary_closure",
    "title_story",
    "cover_identity",
    "cover_story",
    "intro_timing",
)
CHECK_ANCHORS = {
    "final_burned_full_playback": "00:00.000-EOS",
    "subtitle_audio": "FULL_VIDEO_AUDIO_SUBTITLE",
    "silence_hallucination": "KNOWN_SILENCE_AND_FULL_VIDEO",
    "boundary_closure": "CLIP_START_AND_EOS",
    "title_story": "FINAL_TITLE_AND_FULL_STORY",
    "cover_identity": "FINAL_COVER",
    "cover_story": "FINAL_COVER_AND_STORY",
    "intro_timing": "INTRO_TO_MAIN_TRANSITION",
}
CHECK_SPECIFIC_TERMS: dict[str, tuple[tuple[str, ...], ...]] = {
    "final_burned_full_playback": (
        ("00:00", "开头", "start", "begin"),
        ("eos", "结尾", "结束", "end"),
    ),
    "subtitle_audio": (
        ("字幕", "subtitle", "字卡"),
        ("听到", "发声", "音频", "audio", "speech", "台词"),
    ),
    "silence_hallucination": (
        ("静音", "无声", "没说话", "silence", "silent"),
        ("字幕", "幻听", "hallucination", "cue"),
    ),
    "boundary_closure": (
        ("开头", "起点", "start", "结尾", "收尾", "end", "eos"),
        (
            "完整",
            "自然",
            "话题",
            "句子",
            "closure",
            "sentence",
            "topic",
        ),
    ),
    "title_story": (
        ("标题", "title"),
        ("故事", "内容", "反转", "争论", "story", "topic"),
    ),
    "cover_identity": (
        ("封面", "cover"),
        (
            "人物",
            "身份",
            "立绘",
            "左侧",
            "右侧",
            "identity",
            "character",
        ),
    ),
    "cover_story": (
        ("封面", "cover"),
        ("文字", "叙事", "故事", "关系", "story", "text"),
    ),
    "intro_timing": (
        ("片头", "intro"),
        ("正片", "切入", "衔接", "transition", "main"),
    ),
}

_EVIDENCE_FIELDS = frozenset(
    {
        "schema_version",
        "bindings",
        "reviewer_kind",
        "reviewed_by",
        "reviewed_at",
        "approval_quote",
        "items",
    }
)
_BINDING_FIELDS = frozenset(
    {
        "review_contract_sha256",
        "review_manifest",
        "package_audit",
        "items",
    }
)
_ITEM_BINDING_FIELDS = frozenset(
    {"candidate_id", "record", "artifacts"}
)
_FILE_BINDING_FIELDS = frozenset({"path", "sha256"})
_ITEM_FIELDS = frozenset(
    {
        "candidate_id",
        "checks",
        "subtitle_review_points",
        "cover_story_claims",
    }
)
_POINT_FIELDS = frozenset({"point_id", "observation"})
_CLAIM_FIELDS = frozenset(
    {"claim", "presentation", "observation"}
)
_OBSERVATION_FIELDS = frozenset({"anchor", "detail"})
_PLACEHOLDER_RX = re.compile(
    r"(?:\b(?:todo|tbd|n/?a|placeholder)\b|待填|占位|"
    r"<[^>]+>|\{\{[^}]+\}\})",
    re.IGNORECASE,
)
_GENERIC_RX = re.compile(
    r"^(?:"
    r"pass|ok|okay|done|checked|inspected|reviewed|watched|confirmed|"
    r"通过|已通过|检查|已检查|检查完成|审阅|已审阅|审阅完成|"
    r"观看|已观看|观看完成|确认|已确认|无误|没问题|正常|一致|"
    r"(?:已)?(?:检查|审阅|观看|核对|确认)"
    r"(?:最终)?(?:视频|字幕|封面|音频|内容|该点|此点|该项)?"
    r"(?:完成|通过|无误|正常|一致)?"
    r")$",
    re.IGNORECASE,
)
_GENERIC_NUMBERED_RX = re.compile(
    r"^(?:(?:编号|第)?[0-9一二三四五六七八九十]+"
    r"(?:号|项|点|条)?)*(?:均|都|全部|全都)?"
    r"(?:无异常|没有异常|没异常|无问题|没有问题|没问题|"
    r"正常|通过|pass|ok|okay)$",
    re.IGNORECASE,
)
_GENERIC_TERMS = (
    "completed",
    "confirmed",
    "inspected",
    "reviewed",
    "checked",
    "watched",
    "normal",
    "correct",
    "final",
    "audio",
    "video",
    "cover",
    "subtitle",
    "content",
    "pass",
    "done",
    "okay",
    "ok",
    "检查完成",
    "审阅完成",
    "观看完成",
    "核对完成",
    "确认完成",
    "已检查",
    "已审阅",
    "已观看",
    "已核对",
    "已确认",
    "最终",
    "视频",
    "字幕",
    "封面",
    "音频",
    "内容",
    "该点",
    "此点",
    "该项",
    "检查",
    "审阅",
    "观看",
    "核对",
    "确认",
    "完成",
    "通过",
    "无误",
    "无异常",
    "没有异常",
    "没异常",
    "无问题",
    "没有问题",
    "没问题",
    "编号",
    "均",
    "都",
    "全部",
    "全都",
    "正常",
    "一致",
    "已",
)


class FinalHumanReviewEvidenceError(ValueError):
    """The bound evidence is structurally invalid or not the receipt source."""

    def __init__(self, reason_code: str, detail: str = "") -> None:
        self.reason_code = reason_code
        self.detail = detail
        super().__init__(
            reason_code if not detail else f"{reason_code}: {detail}"
        )


def _require_fields(
    value: Mapping[str, object],
    expected: frozenset[str],
    *,
    reason: str,
    detail: str = "",
) -> None:
    if set(value) != set(expected):
        raise FinalHumanReviewEvidenceError(reason, detail)


def _format_ms(value: object) -> str:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < 0
    ):
        raise FinalHumanReviewEvidenceError(
            "FINAL_HUMAN_REVIEW_BOUND_EVIDENCE_INVALID",
            f"invalid review point timestamp: {value!r}",
        )
    hours, remainder = divmod(value, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, milliseconds = divmod(remainder, 1_000)
    return (
        f"{hours:02d}:{minutes:02d}:{seconds:02d}."
        f"{milliseconds:03d}"
    )


def _point_anchor(point: Mapping[str, object]) -> str:
    return (
        f"{_format_ms(point.get('final_video_start_ms'))}-"
        f"{_format_ms(point.get('final_video_end_ms'))}"
    )


def _normalized(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def _probe(detail: str, phrases: tuple[str, ...]) -> str:
    value = _normalized(detail)
    for phrase in sorted(
        {_normalized(row) for row in phrases if row},
        key=len,
        reverse=True,
    ):
        value = value.replace(phrase, "")
    value = re.sub(
        r"(?:(?:编号|第)?[0-9一二三四五六七八九十]+"
        r"(?:号|项|点|条)?|\b(?:no|number)\s*[0-9]+\b)",
        "",
        value,
        flags=re.IGNORECASE,
    )
    return re.sub(r"[\W_]+", "", value, flags=re.UNICODE)


def _observation(
    value: object,
    *,
    anchor: str,
    label: str,
    seen: list[tuple[str, str]],
    phrases: tuple[str, ...] = (),
    terms: tuple[tuple[str, ...], ...] = (),
) -> str:
    if not isinstance(value, Mapping):
        raise FinalHumanReviewEvidenceError(
            "FINAL_HUMAN_REVIEW_BOUND_EVIDENCE_OBSERVATION_INVALID",
            label,
        )
    _require_fields(
        value,
        _OBSERVATION_FIELDS,
        reason=(
            "FINAL_HUMAN_REVIEW_BOUND_EVIDENCE_OBSERVATION_INVALID"
        ),
        detail=label,
    )
    detail = value.get("detail")
    if (
        value.get("anchor") != anchor
        or not isinstance(detail, str)
        or detail != detail.strip()
        or len(detail) < 12
        or any(ord(character) < 32 for character in detail)
        or _PLACEHOLDER_RX.search(detail)
    ):
        raise FinalHumanReviewEvidenceError(
            "FINAL_HUMAN_REVIEW_BOUND_EVIDENCE_OBSERVATION_INVALID",
            label,
        )
    normalized_detail = _normalized(detail)
    if any(
        not any(_normalized(term) in normalized_detail for term in choices)
        for choices in terms
    ):
        raise FinalHumanReviewEvidenceError(
            "FINAL_HUMAN_REVIEW_BOUND_EVIDENCE_OBSERVATION_GENERIC",
            label,
        )
    probe = _probe(detail, (*phrases, anchor))
    residue = probe
    for term in _GENERIC_TERMS:
        residue = residue.replace(_normalized(term), "")
    if (
        not probe
        or _GENERIC_RX.fullmatch(probe)
        or _GENERIC_NUMBERED_RX.fullmatch(probe)
        or not residue
    ):
        raise FinalHumanReviewEvidenceError(
            "FINAL_HUMAN_REVIEW_BOUND_EVIDENCE_OBSERVATION_GENERIC",
            label,
        )
    for previous, previous_label in seen:
        near_duplicate = (
            min(len(probe), len(previous)) >= 12
            and difflib.SequenceMatcher(
                None, probe, previous, autojunk=False
            ).ratio()
            >= 0.94
        )
        if probe == previous or near_duplicate:
            raise FinalHumanReviewEvidenceError(
                "FINAL_HUMAN_REVIEW_BOUND_EVIDENCE_OBSERVATION_REUSED",
                f"{label} duplicates {previous_label}",
            )
    seen.append((probe, label))
    return f"{anchor} — {detail}"


def _validate_binding_shape(value: object, label: str) -> None:
    if not isinstance(value, Mapping):
        raise FinalHumanReviewEvidenceError(
            "FINAL_HUMAN_REVIEW_BOUND_EVIDENCE_BINDINGS_INVALID",
            label,
        )
    _require_fields(
        value,
        _FILE_BINDING_FIELDS,
        reason="FINAL_HUMAN_REVIEW_BOUND_EVIDENCE_BINDINGS_INVALID",
        detail=label,
    )


def _validate_bindings(
    raw: object,
    *,
    expected: Mapping[str, object],
) -> None:
    if not isinstance(raw, Mapping):
        raise FinalHumanReviewEvidenceError(
            "FINAL_HUMAN_REVIEW_BOUND_EVIDENCE_BINDINGS_INVALID"
        )
    _require_fields(
        raw,
        _BINDING_FIELDS,
        reason="FINAL_HUMAN_REVIEW_BOUND_EVIDENCE_BINDINGS_INVALID",
    )
    items = raw.get("items")
    if not isinstance(items, list):
        raise FinalHumanReviewEvidenceError(
            "FINAL_HUMAN_REVIEW_BOUND_EVIDENCE_BINDINGS_INVALID",
            "items",
        )
    for index, item in enumerate(items):
        if not isinstance(item, Mapping):
            raise FinalHumanReviewEvidenceError(
                "FINAL_HUMAN_REVIEW_BOUND_EVIDENCE_BINDINGS_INVALID",
                f"items[{index}]",
            )
        _require_fields(
            item,
            _ITEM_BINDING_FIELDS,
            reason=(
                "FINAL_HUMAN_REVIEW_BOUND_EVIDENCE_BINDINGS_INVALID"
            ),
            detail=f"items[{index}]",
        )
        _validate_binding_shape(
            item.get("record"), f"items[{index}].record"
        )
        artifacts = item.get("artifacts")
        if not isinstance(artifacts, Mapping):
            raise FinalHumanReviewEvidenceError(
                "FINAL_HUMAN_REVIEW_BOUND_EVIDENCE_BINDINGS_INVALID",
                f"items[{index}].artifacts",
            )
        _require_fields(
            artifacts,
            frozenset(ARTIFACT_FIELDS),
            reason=(
                "FINAL_HUMAN_REVIEW_BOUND_EVIDENCE_BINDINGS_INVALID"
            ),
            detail=f"items[{index}].artifacts",
        )
        for artifact in ARTIFACT_FIELDS:
            _validate_binding_shape(
                artifacts.get(artifact),
                f"items[{index}].artifacts.{artifact}",
            )
    if dict(raw) != dict(expected):
        raise FinalHumanReviewEvidenceError(
            "FINAL_HUMAN_REVIEW_BOUND_EVIDENCE_BINDINGS_MISMATCH"
        )


def _validate_checks(
    raw: object,
    *,
    candidate_id: str,
    receipt: Mapping[str, object],
    seen: list[tuple[str, str]],
) -> None:
    if not isinstance(raw, Mapping):
        raise FinalHumanReviewEvidenceError(
            "FINAL_HUMAN_REVIEW_BOUND_EVIDENCE_CHECKS_INVALID",
            candidate_id,
        )
    _require_fields(
        raw,
        frozenset(CHECK_FIELDS),
        reason="FINAL_HUMAN_REVIEW_BOUND_EVIDENCE_CHECKS_INVALID",
        detail=candidate_id,
    )
    for check in CHECK_FIELDS:
        projected = _observation(
            raw.get(check),
            anchor=CHECK_ANCHORS[check],
            label=f"{candidate_id}.checks.{check}",
            seen=seen,
            phrases=(candidate_id, check),
            terms=CHECK_SPECIFIC_TERMS[check],
        )
        if receipt.get(check) != {
            "status": "PASS",
            "evidence": projected,
        }:
            raise FinalHumanReviewEvidenceError(
                "FINAL_HUMAN_REVIEW_BOUND_EVIDENCE_RECEIPT_MISMATCH",
                f"{candidate_id}.checks.{check}",
            )


def _validate_points(
    raw: object,
    *,
    candidate_id: str,
    receipt: list[dict[str, object]],
    seen: list[tuple[str, str]],
) -> None:
    if not isinstance(raw, list) or len(raw) != len(receipt):
        raise FinalHumanReviewEvidenceError(
            "FINAL_HUMAN_REVIEW_BOUND_EVIDENCE_POINTS_INVALID",
            candidate_id,
        )
    for index, (point, receipt_point) in enumerate(
        zip(raw, receipt, strict=True)
    ):
        if not isinstance(point, Mapping):
            raise FinalHumanReviewEvidenceError(
                "FINAL_HUMAN_REVIEW_BOUND_EVIDENCE_POINTS_INVALID",
                f"{candidate_id}[{index}]",
            )
        _require_fields(
            point,
            _POINT_FIELDS,
            reason="FINAL_HUMAN_REVIEW_BOUND_EVIDENCE_POINTS_INVALID",
            detail=f"{candidate_id}[{index}]",
        )
        point_id = receipt_point["point_id"]
        if point.get("point_id") != point_id:
            raise FinalHumanReviewEvidenceError(
                "FINAL_HUMAN_REVIEW_BOUND_EVIDENCE_POINTS_INVALID",
                f"{candidate_id}[{index}]",
            )
        projected = _observation(
            point.get("observation"),
            anchor=_point_anchor(receipt_point),
            label=f"{candidate_id}.subtitle_review_points[{point_id}]",
            seen=seen,
            phrases=(
                candidate_id,
                str(point_id),
                str(receipt_point["expectation"]),
            ),
            terms=(
                (
                    "听到",
                    "发声",
                    "无声",
                    "静音",
                    "heard",
                    "audio",
                    "silent",
                ),
                ("字幕", "烧录", "cue", "subtitle"),
            ),
        )
        if receipt_point["evidence"] != projected:
            raise FinalHumanReviewEvidenceError(
                "FINAL_HUMAN_REVIEW_BOUND_EVIDENCE_RECEIPT_MISMATCH",
                f"{candidate_id}.subtitle_review_points[{point_id}]",
            )


def _claim_terms(
    presentation: object,
) -> tuple[tuple[str, ...], ...]:
    if presentation == "SOURCE_FRAME":
        return (
            ("封面", "画面", "源帧", "cover", "frame"),
            (
                "可见",
                "人物",
                "立绘",
                "左",
                "右",
                "visible",
                "character",
            ),
        )
    return (
        ("封面", "大字", "文字", "cover", "text"),
        (
            "叙事",
            "故事",
            "关系",
            "内容",
            "story",
            "topic",
        ),
    )


def _validate_claims(
    raw: object,
    *,
    candidate_id: str,
    receipt: list[dict[str, str]],
    seen: list[tuple[str, str]],
) -> None:
    if not isinstance(raw, list) or len(raw) != len(receipt):
        raise FinalHumanReviewEvidenceError(
            "FINAL_HUMAN_REVIEW_BOUND_EVIDENCE_CLAIMS_INVALID",
            candidate_id,
        )
    for index, (claim, receipt_claim) in enumerate(
        zip(raw, receipt, strict=True)
    ):
        if not isinstance(claim, Mapping):
            raise FinalHumanReviewEvidenceError(
                "FINAL_HUMAN_REVIEW_BOUND_EVIDENCE_CLAIMS_INVALID",
                f"{candidate_id}[{index}]",
            )
        _require_fields(
            claim,
            _CLAIM_FIELDS,
            reason="FINAL_HUMAN_REVIEW_BOUND_EVIDENCE_CLAIMS_INVALID",
            detail=f"{candidate_id}[{index}]",
        )
        text = receipt_claim["claim"]
        presentation = receipt_claim["presentation"]
        if (
            claim.get("claim") != text
            or claim.get("presentation") != presentation
        ):
            raise FinalHumanReviewEvidenceError(
                "FINAL_HUMAN_REVIEW_BOUND_EVIDENCE_CLAIMS_INVALID",
                f"{candidate_id}[{index}]",
            )
        projected = _observation(
            claim.get("observation"),
            anchor=f"FINAL_COVER/{presentation}",
            label=f"{candidate_id}.cover_story_claims[{index}]",
            seen=seen,
            phrases=(candidate_id, text, presentation),
            terms=_claim_terms(presentation),
        )
        if receipt_claim["evidence"] != projected:
            raise FinalHumanReviewEvidenceError(
                "FINAL_HUMAN_REVIEW_BOUND_EVIDENCE_RECEIPT_MISMATCH",
                f"{candidate_id}.cover_story_claims[{index}]",
            )


def validate_bound_review_evidence(
    evidence: object,
    *,
    review_contract_sha256: str,
    package_evidence: Mapping[str, Mapping[str, object]],
    reviewer_metadata: Mapping[str, str],
    receipt_items: list[dict[str, Any]],
) -> None:
    """Require evidence v2 to be the exact source of the normalized receipt."""

    if not isinstance(evidence, Mapping):
        raise FinalHumanReviewEvidenceError(
            "FINAL_HUMAN_REVIEW_BOUND_EVIDENCE_FIELDS_INVALID"
        )
    _require_fields(
        evidence,
        _EVIDENCE_FIELDS,
        reason="FINAL_HUMAN_REVIEW_BOUND_EVIDENCE_FIELDS_INVALID",
    )
    if evidence.get("schema_version") != SCHEMA_VERSION:
        raise FinalHumanReviewEvidenceError(
            "FINAL_HUMAN_REVIEW_BOUND_EVIDENCE_SCHEMA_INVALID"
        )
    for field, expected in reviewer_metadata.items():
        if evidence.get(field) != expected:
            raise FinalHumanReviewEvidenceError(
                "FINAL_HUMAN_REVIEW_BOUND_EVIDENCE_METADATA_MISMATCH",
                field,
            )
    expected_bindings = {
        "review_contract_sha256": review_contract_sha256,
        "review_manifest": package_evidence["review_manifest"],
        "package_audit": package_evidence["package_audit"],
        "items": [
            {
                "candidate_id": item["candidate_id"],
                "record": item["record"],
                "artifacts": item["artifacts"],
            }
            for item in receipt_items
        ],
    }
    _validate_bindings(evidence.get("bindings"), expected=expected_bindings)

    items = evidence.get("items")
    if not isinstance(items, list) or len(items) != len(receipt_items):
        raise FinalHumanReviewEvidenceError(
            "FINAL_HUMAN_REVIEW_BOUND_EVIDENCE_CANDIDATE_SET_MISMATCH"
        )
    seen: list[tuple[str, str]] = []
    for index, (item, receipt_item) in enumerate(
        zip(items, receipt_items, strict=True)
    ):
        if not isinstance(item, Mapping):
            raise FinalHumanReviewEvidenceError(
                "FINAL_HUMAN_REVIEW_BOUND_EVIDENCE_ITEM_INVALID",
                f"items[{index}]",
            )
        _require_fields(
            item,
            _ITEM_FIELDS,
            reason="FINAL_HUMAN_REVIEW_BOUND_EVIDENCE_ITEM_INVALID",
            detail=f"items[{index}]",
        )
        candidate_id = receipt_item["candidate_id"]
        if item.get("candidate_id") != candidate_id:
            raise FinalHumanReviewEvidenceError(
                "FINAL_HUMAN_REVIEW_BOUND_EVIDENCE_CANDIDATE_SET_MISMATCH",
                f"items[{index}]",
            )
        _validate_checks(
            item.get("checks"),
            candidate_id=candidate_id,
            receipt=receipt_item["checks"],
            seen=seen,
        )
        _validate_points(
            item.get("subtitle_review_points"),
            candidate_id=candidate_id,
            receipt=receipt_item["subtitle_review_points"],
            seen=seen,
        )
        _validate_claims(
            item.get("cover_story_claims"),
            candidate_id=candidate_id,
            receipt=receipt_item["cover_story_claims"],
            seen=seen,
        )
