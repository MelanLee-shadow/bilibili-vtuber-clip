"""F12 金丝雀：受骗片形状——别人挨的话不许安到主角头上。

Ivan 2026-08-08 纠错（docs/reviews/2026-08-08-truth-harvest-forensics-synthesis.md
「8/8 深夜追加:F12」）：自动 hook「李豆沙刚被劝别再受骗」事实错，cue1-3 的
「别再被骗」是连线主持对**上一位选手**说的，cue4「下一位，我们的08号」之后
本人才上场。`source_fact_review` 只验"说过没有"，不验"对谁说"。

套件是密闭的，判官本身是 mock：这些用例锁的是**机制**——
(1) 带说话人标签的转写真的进了 prompt；
(2) 受骗片形状下判官的 SUPPORTED 被确定性反证顶回（修前这类文案原样发出）；
(3) WRONG_ADDRESSEE 不许与 KEEP 共存（fail-closed 打回重生成）；
(4) 无说话人标签时只准 UNVERIFIABLE，披露不拦。
不是在测判官的判断力。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from src.autoslice.addressee_attribution import (
    ADDRESSEE_UNRESOLVED_REASON,
    MODE_SPEAKER_LABELLED,
    MODE_UNVERIFIABLE,
    build_addressee_transcripts,
    evaluate_addressee_attribution,
    host_not_yet_on_air,
    render_speaker_transcript,
    requires_addressee_attribution,
    speaker_transcript_from_record,
)
from src.autoslice.review_evidence import SourceCue
from src.autoslice.source_fact_review import (
    review_and_repair_source_facts,
    source_fact_review_passes,
)
from src.autoslice.speaker_common import GUEST_SPEAKER, HOST_SPEAKER

# 受骗片形状：主持先对上一位选手说话，主角在 cue4 之后才上场。
_SCAM_CUES = [
    (GUEST_SPEAKER, "所以你以后别再被骗了知道吗"),
    (GUEST_SPEAKER, "这种话听听就算了"),
    (GUEST_SPEAKER, "好我们下一位"),
    (GUEST_SPEAKER, "下一位，我们的08号"),
    (HOST_SPEAKER, "大家好我是李豆沙"),
    (HOST_SPEAKER, "今天来参加这个连线"),
]
_SCAM_HOOK = "刚被劝别再受骗，李豆沙立刻上场自我介绍"
_SCAM_TITLE = "【李豆沙】刚被劝别再受骗，她上场第一句就是大家好"
_SCAM_TRANSCRIPT = "\n".join(text for _speaker, text in _SCAM_CUES)
_SCAM_SPEAKER_TRANSCRIPT = render_speaker_transcript(_SCAM_CUES)
_BEFORE_HOST_EVIDENCE = ["speaker_transcript: 1 [连线] 所以你以后别再被骗了知道吗"]


def _completion(
    *,
    status: str,
    final_hook: str,
    final_title: str,
    addressee_attribution: list[dict[str, object]],
    changed_surfaces: list[dict[str, object]] | None = None,
    scorecard_review: dict[str, str] | None = None,
) -> str:
    return json.dumps(
        {
            "schema_version": "lidousha-source-fact-review.v1",
            "status": status,
            "final_selection_hook": final_hook,
            "final_title": final_title,
            "supported_by": ["final_transcript", "speaker_transcript"],
            "changed_surfaces": changed_surfaces or [],
            "addressee_attribution": addressee_attribution,
            "selection_scorecard_review": scorecard_review
            or {"status": "NOT_NEEDED", "reason": "selection hook remains unchanged"},
            "summary": "按说话人序列复核了归属断言。",
        },
        ensure_ascii=False,
    )


def _review(cpa, *, speaker_transcript: str | None) -> dict:
    return review_and_repair_source_facts(
        selection_hook=_SCAM_HOOK,
        title=_SCAM_TITLE,
        final_transcript=_SCAM_TRANSCRIPT,
        clip_context_prompt="",
        llm_call=cpa,
        speaker_transcript=speaker_transcript,
    )


def test_speaker_labelled_transcript_reaches_the_judge() -> None:
    prompts: list[str] = []

    def cpa(prompt: str) -> str:
        prompts.append(prompt)
        return _completion(
            status="KEEP",
            final_hook=_SCAM_HOOK,
            final_title=_SCAM_TITLE,
            addressee_attribution=[
                {
                    "assertion": "刚被劝别再受骗",
                    "verdict": "UNVERIFIABLE",
                    "reason": "标签序列不足以判定这句话的受话人。",
                }
            ],
        )

    review = _review(cpa, speaker_transcript=_SCAM_SPEAKER_TRANSCRIPT)
    assert source_fact_review_passes(review)
    assert "1 [连线] 所以你以后别再被骗了知道吗" in prompts[0]
    assert "5 [李豆沙] 大家好我是李豆沙" in prompts[0]
    assert "WRONG_ADDRESSEE" in prompts[0]
    assert review["passes"][0]["addressee_attribution_mode"] == MODE_SPEAKER_LABELLED
    assert review["passes"][0]["speaker_transcript_sha256"] == "sha256:" + hashlib.sha256(
        _SCAM_SPEAKER_TRANSCRIPT.encode("utf-8")
    ).hexdigest()


def test_scam_clip_shape_refuses_a_supported_attribution() -> None:
    """F12 主金丝雀：修前这条 hook 会原样发出，修后必须被顶回。"""

    calls: list[str] = []

    def cpa(_prompt: str) -> str:
        calls.append(_prompt)
        return _completion(
            status="KEEP",
            final_hook=_SCAM_HOOK,
            final_title=_SCAM_TITLE,
            addressee_attribution=[
                {
                    "assertion": "刚被劝别再受骗",
                    "verdict": "SUPPORTED",
                    "reason": "字幕里确实有人说了别再被骗。",
                    "evidence": _BEFORE_HOST_EVIDENCE,
                }
            ],
        )

    review = _review(cpa, speaker_transcript=_SCAM_SPEAKER_TRANSCRIPT)

    assert not source_fact_review_passes(review)
    assert review["reason_code"] == ADDRESSEE_UNRESOLVED_REASON
    # 形状矛盾走 provider 重试车道（同一份输入重掷形状，不重掷语义）。
    assert len(calls) == 1 + 2
    assert review["final_selection_hook"] == _SCAM_HOOK  # 未授权不落盘不变式


def test_wrong_addressee_cannot_ride_a_keep() -> None:
    def cpa(_prompt: str) -> str:
        return _completion(
            status="KEEP",
            final_hook=_SCAM_HOOK,
            final_title=_SCAM_TITLE,
            addressee_attribution=[
                {
                    "assertion": "刚被劝别再受骗",
                    "verdict": "WRONG_ADDRESSEE",
                    "actual_speaker": GUEST_SPEAKER,
                    "actual_addressee": "上一位选手",
                    "reason": "这句话在主角上场前由连线主持对上一位选手说。",
                    "evidence": _BEFORE_HOST_EVIDENCE,
                }
            ],
        )

    review = _review(cpa, speaker_transcript=_SCAM_SPEAKER_TRANSCRIPT)
    assert not source_fact_review_passes(review)
    assert review["reason_code"] == ADDRESSEE_UNRESOLVED_REASON


def test_wrong_addressee_repair_regenerates_the_hook_and_passes() -> None:
    fixed_hook = "李豆沙上场第一句就是大家好，直接自我介绍"
    fixed_title = "【李豆沙】上场第一句就是大家好，自我介绍利落得不像新人"
    calls: list[str] = []

    def cpa(prompt: str) -> str:
        calls.append(prompt)
        if len(calls) == 1:
            return _completion(
                status="REPAIR",
                final_hook=fixed_hook,
                final_title=fixed_title,
                addressee_attribution=[
                    {
                        "assertion": "刚被劝别再受骗",
                        "verdict": "WRONG_ADDRESSEE",
                        "actual_speaker": GUEST_SPEAKER,
                        "actual_addressee": "上一位选手",
                        "reason": "主角在该句之后才上场，受话人不是她。",
                        "evidence": _BEFORE_HOST_EVIDENCE,
                    }
                ],
                changed_surfaces=[
                    {
                        "artifact": "selection_hook",
                        "before": "刚被劝别再受骗，李豆沙立刻上场自我介绍",
                        "after": "李豆沙上场第一句就是大家好，直接自我介绍",
                        "reason": "删掉错归属，只保留她本人做过的事。",
                        "evidence": ["大家好我是李豆沙"],
                    },
                    {
                        "artifact": "title",
                        "before": "刚被劝别再受骗，她上场第一句就是大家好",
                        "after": "上场第一句就是大家好，自我介绍利落得不像新人",
                        "reason": "同上，标题同步去掉错归属。",
                        "evidence": ["大家好我是李豆沙", "今天来参加这个连线"],
                    },
                ],
            )
        return _completion(
            status="KEEP",
            final_hook=fixed_hook,
            final_title=fixed_title,
            addressee_attribution=[],
        )

    review = _review(cpa, speaker_transcript=_SCAM_SPEAKER_TRANSCRIPT)
    assert source_fact_review_passes(review)
    assert review["decision"] == "REPAIRED"
    assert review["final_selection_hook"] == fixed_hook
    assert review["final_title"] == fixed_title
    assert review["passes"][0]["addressee_attribution"][0]["verdict"] == "WRONG_ADDRESSEE"


def test_supported_attribution_after_the_host_is_on_air_is_accepted() -> None:
    """反向对照：主角已在场时，连线对她说的话判 SUPPORTED 必须放行。"""

    cues = [
        (HOST_SPEAKER, "我刚刚差点就信了"),
        (GUEST_SPEAKER, "所以你以后别再被骗了知道吗"),
        (HOST_SPEAKER, "知道了知道了"),
    ]
    transcript = render_speaker_transcript(cues)
    hook = "被劝别再受骗后，李豆沙连声说知道了"
    title = "【李豆沙】被劝别再受骗，她连声说知道了"

    def cpa(_prompt: str) -> str:
        return json.dumps(
            {
                "schema_version": "lidousha-source-fact-review.v1",
                "status": "KEEP",
                "final_selection_hook": hook,
                "final_title": title,
                "supported_by": ["speaker_transcript"],
                "changed_surfaces": [],
                "addressee_attribution": [
                    {
                        "assertion": "被劝别再受骗",
                        "verdict": "SUPPORTED",
                        "actual_speaker": GUEST_SPEAKER,
                        "actual_addressee": HOST_SPEAKER,
                        "reason": "主角在该句之前之后都在说话，受话人就是她。",
                        "evidence": [
                            "speaker_transcript: 2 [连线] 所以你以后别再被骗了知道吗"
                        ],
                    }
                ],
                "selection_scorecard_review": {
                    "status": "NOT_NEEDED",
                    "reason": "selection hook remains unchanged",
                },
                "summary": "说话人序列支持该归属。",
            },
            ensure_ascii=False,
        )

    review = review_and_repair_source_facts(
        selection_hook=hook,
        title=title,
        final_transcript="\n".join(text for _speaker, text in cues),
        clip_context_prompt="",
        llm_call=cpa,
        speaker_transcript=transcript,
    )
    assert source_fact_review_passes(review)
    assert review["decision"] == "KEEP"


def test_without_speaker_labels_only_unverifiable_is_allowed() -> None:
    def claiming(_prompt: str) -> str:
        return _completion(
            status="KEEP",
            final_hook=_SCAM_HOOK,
            final_title=_SCAM_TITLE,
            addressee_attribution=[
                {
                    "assertion": "刚被劝别再受骗",
                    "verdict": "SUPPORTED",
                    "reason": "我觉得是对她说的。",
                    "evidence": ["speaker_transcript: 1 [连线] 所以你以后别再被骗了知道吗"],
                }
            ],
        )

    blocked = _review(claiming, speaker_transcript=None)
    assert not source_fact_review_passes(blocked)

    def honest(_prompt: str) -> str:
        return _completion(
            status="KEEP",
            final_hook=_SCAM_HOOK,
            final_title=_SCAM_TITLE,
            addressee_attribution=[
                {
                    "assertion": "刚被劝别再受骗",
                    "verdict": "UNVERIFIABLE",
                    "reason": "本片没有说话人标签，无法判定受话人。",
                }
            ],
        )

    disclosed = _review(honest, speaker_transcript=None)
    assert source_fact_review_passes(disclosed)
    assert disclosed["passes"][0]["addressee_attribution_mode"] == MODE_UNVERIFIABLE
    assert disclosed["passes"][0]["speaker_transcript_sha256"] is None


def test_missing_addressee_key_is_shape_invalid() -> None:
    """判项必填：缺键不是"没有归属问题"，是判官没做这件事（F15 同款纪律）。"""

    def cpa(_prompt: str) -> str:
        return json.dumps(
            {
                "schema_version": "lidousha-source-fact-review.v1",
                "status": "KEEP",
                "final_selection_hook": _SCAM_HOOK,
                "final_title": _SCAM_TITLE,
                "supported_by": ["final_transcript"],
                "changed_surfaces": [],
                "selection_scorecard_review": {
                    "status": "NOT_NEEDED",
                    "reason": "unchanged",
                },
                "summary": "看起来没问题。",
            },
            ensure_ascii=False,
        )

    review = _review(cpa, speaker_transcript=_SCAM_SPEAKER_TRANSCRIPT)
    assert not source_fact_review_passes(review)
    assert review["reason_code"] == "CPA_TEXT_REVIEW_INVALID"


def test_empty_ruling_is_refused_only_when_the_judge_could_have_ruled() -> None:
    assert requires_addressee_attribution(
        selection_hook=_SCAM_HOOK, title=_SCAM_TITLE
    )
    assert not requires_addressee_attribution(
        selection_hook="她把新皮肤转了三圈", title="【李豆沙】新皮肤转了三圈"
    )
    with_labels = evaluate_addressee_attribution(
        [],
        selection_hook=_SCAM_HOOK,
        title=_SCAM_TITLE,
        speaker_transcript=_SCAM_SPEAKER_TRANSCRIPT,
        status="KEEP",
    )
    assert not with_labels.valid
    assert with_labels.reason_code == ADDRESSEE_UNRESOLVED_REASON
    without_labels = evaluate_addressee_attribution(
        [],
        selection_hook=_SCAM_HOOK,
        title=_SCAM_TITLE,
        speaker_transcript=None,
        status="KEEP",
    )
    assert without_labels.valid


def test_assertion_must_be_quoted_from_the_copy_under_review() -> None:
    state = evaluate_addressee_attribution(
        [
            {
                "assertion": "这句话文案里根本没有",
                "verdict": "UNVERIFIABLE",
                "reason": "凭空judged。",
            }
        ],
        selection_hook=_SCAM_HOOK,
        title=_SCAM_TITLE,
        speaker_transcript=_SCAM_SPEAKER_TRANSCRIPT,
        status="KEEP",
    )
    assert not state.valid


def test_unbound_speaker_evidence_is_refused() -> None:
    state = evaluate_addressee_attribution(
        [
            {
                "assertion": "刚被劝别再受骗",
                "verdict": "WRONG_ADDRESSEE",
                "reason": "编一句不在转写里的证据。",
                "evidence": ["speaker_transcript: 9 [连线] 这行根本不存在"],
            }
        ],
        selection_hook=_SCAM_HOOK,
        title=_SCAM_TITLE,
        speaker_transcript=_SCAM_SPEAKER_TRANSCRIPT,
        status="REPAIR",
    )
    assert not state.valid


def test_host_not_yet_on_air_is_narrow() -> None:
    assert host_not_yet_on_air(
        _BEFORE_HOST_EVIDENCE, speaker_transcript=_SCAM_SPEAKER_TRANSCRIPT
    )
    # 主角上场之后的连线发言不触发（她当时在场，可能真是对她说的）。
    later = render_speaker_transcript(
        [(HOST_SPEAKER, "我在呢"), (GUEST_SPEAKER, "别再被骗了")]
    )
    assert not host_not_yet_on_air(
        ["speaker_transcript: 2 [连线] 别再被骗了"], speaker_transcript=later
    )
    # 引用主角自己的发言不触发。
    assert not host_not_yet_on_air(
        ["speaker_transcript: 5 [李豆沙] 大家好我是李豆沙"],
        speaker_transcript=_SCAM_SPEAKER_TRANSCRIPT,
    )


def _record_with_speaker_srt(tmp_path: Path, body: str) -> dict[str, object]:
    path = tmp_path / "clip.speaker-final.srt"
    path.write_text(body, encoding="utf-8")
    return {
        "speaker_review_srt_path": str(path),
        "artifact_hashes": {
            "speaker_review_srt_sha256": "sha256:"
            + hashlib.sha256(path.read_bytes()).hexdigest()
        },
    }


def test_speaker_transcript_is_hash_bound_and_aligned(tmp_path: Path) -> None:
    body = (
        "1\n00:00:00,000 --> 00:00:02,000\n[连线] 所以你以后别再被骗了知道吗\n\n"
        "2\n00:00:02,000 --> 00:00:04,000\n[李豆沙] 大家好我是李豆沙\n"
    )
    record = _record_with_speaker_srt(tmp_path, body)
    cues = ["所以你以后别再被骗了知道吗", "大家好我是李豆沙"]
    assert speaker_transcript_from_record(record, cues) == (
        "1 [连线] 所以你以后别再被骗了知道吗\n2 [李豆沙] 大家好我是李豆沙"
    )

    drifted = dict(record)
    drifted["artifact_hashes"] = {"speaker_review_srt_sha256": "sha256:" + "0" * 64}
    assert speaker_transcript_from_record(drifted, cues) is None

    # 与最终字幕逐条对不上 -> 拒绝据此推理归属。
    assert speaker_transcript_from_record(record, ["完全不同的一句"]) is None
    assert speaker_transcript_from_record({}, cues) is None


def test_build_addressee_transcripts_keeps_the_plain_transcript_byte_identical(
    tmp_path: Path,
) -> None:
    cues = [
        SourceCue("c1", 0, 2_000, " 所以你以后别再被骗了知道吗 "),
        SourceCue("c2", 2_000, 4_000, ""),
        SourceCue("c3", 4_000, 6_000, "大家好我是李豆沙"),
    ]
    legacy = "\n".join(cue.text.strip() for cue in cues if cue.text.strip())
    record = _record_with_speaker_srt(
        tmp_path,
        "1\n00:00:00,000 --> 00:00:02,000\n[连线] 所以你以后别再被骗了知道吗\n\n"
        "2\n00:00:04,000 --> 00:00:06,000\n[李豆沙] 大家好我是李豆沙\n",
    )
    plain, speaker = build_addressee_transcripts(record, cues)
    assert plain == legacy
    assert speaker == (
        "1 [连线] 所以你以后别再被骗了知道吗\n2 [李豆沙] 大家好我是李豆沙"
    )
    # 没有 speaker-final 产物（uniform_host 场）时纯文字转写完全不变。
    assert build_addressee_transcripts({}, cues) == (legacy, None)
