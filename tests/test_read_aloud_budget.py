"""近失仲裁预算分配（2026-07-20「脑海里根本没有冒出熊猫二字啊」案）。

病：仲裁帽 3 按证据到达序先到先得——晚段真念读（score 0.624/precision
0.875）被早段低分近失占光名额，静默出局零审计。类解：先全量收集、按
(score, precision) 排序、同 cue 跨度去重后再消费预算。与拼音扫描器的
cap 排序修复同一铁律。
"""

from __future__ import annotations

from src.autoslice.chat_evidence import ChatEvidence
from src.autoslice.chat_proposals import _discover_chat_proposals
from src.autoslice.jingting_chunker import parse_srt_cues


def _srt(*rows: tuple[int, int, str]) -> str:
    blocks = []
    for index, (start, end, text) in enumerate(rows, start=1):
        def fmt(ms: int) -> str:
            h, rem = divmod(ms, 3_600_000)
            m, rem = divmod(rem, 60_000)
            s, milli = divmod(rem, 1_000)
            return f"{h:02d}:{m:02d}:{s:02d},{milli:03d}"

        blocks.append(f"{index}\n{fmt(start)} --> {fmt(end)}\n{text}")
    return "\n\n".join(blocks) + "\n"


# 每条：互不相干的字符集，cue = 权威的 N 字前缀（截尾型念读），N 决定分数。
# N=5 → score≈0.66，N=6 → ≈0.74，N=7 → ≈0.81；全部落在近失带
# （score≥0.55, coverage≥0.50, common≥4）且 extent<0.82 不构成 full。
_CASES = [
    ("甲乙丙丁戊己庚辛壬癸", 5, 10_000),
    ("子丑寅卯辰巳午未申酉", 5, 20_000),
    ("春夏秋冬风花雪月山川", 6, 30_000),
    ("金木水火土雷电云雨霜", 7, 40_000),  # 最强，但证据序最晚
]


def _fixture() -> tuple[list[ChatEvidence], list, list[str]]:
    rows = []
    evidence = []
    for authority, n, at_ms in _CASES:
        rows.append((at_ms, at_ms + 2_000, authority[:n]))
        evidence.append(ChatEvidence("danmaku", at_ms - 3_000, authority, "观众"))
    srt = _srt(*rows)
    cues = [c for c in parse_srt_cues(srt) if c.text.strip()]
    return evidence, cues, [c.text for c in cues]


class _RecordingVerifier:
    def __init__(self) -> None:
        self.requests: list[dict] = []

    def __call__(self, request: dict) -> dict:
        self.requests.append(request)
        return {
            "schema_version": "chat-entity-verdict.v1",
            "request_sha256": request["request_sha256"],
            "status": "UNCERTAIN",
        }


def test_strongest_late_near_miss_wins_budget() -> None:
    """最强近失哪怕最后到达也必须进前 3；最弱的被挤出，而非先到先得。"""

    evidence, cues, texts = _fixture()
    verifier = _RecordingVerifier()
    discovery = _discover_chat_proposals(
        evidence,
        cues=cues,
        texts=texts,
        entity_groups=[],
        max_cues=3,
        support_srt_texts=[],
        entity_verifier=verifier,
    )
    arbitrated = [req["exact_text"] for req in verifier.requests]
    assert len(arbitrated) == 3
    assert "金木水火土雷电云雨霜" in arbitrated  # 晚到的最强者
    assert "春夏秋冬风花雪月山川" in arbitrated
    # 两个 N=5 弱者只能进一个
    weak = {"甲乙丙丁戊己庚辛壬癸", "子丑寅卯辰巳午未申酉"}
    assert len(weak & set(arbitrated)) == 1
    assert len(discovery.read_aloud_arbitrations) == 3


def test_same_span_deduped_to_best_candidate() -> None:
    """同一 cue 跨度多条弹幕抢仲裁时只送最高分那条（真没想到熊猫案形态）。"""

    srt = _srt((10_000, 12_000, "甲乙丙丁戊己庚"))
    cues = [c for c in parse_srt_cues(srt) if c.text.strip()]
    texts = [c.text for c in cues]
    evidence = [
        ChatEvidence("danmaku", 7_000, "甲乙丙丁戊己庚辛壬癸酉戌", "弱"),  # N7/12
        ChatEvidence("danmaku", 8_000, "甲乙丙丁戊己庚辛壬癸", "强"),  # N7/10 更高分
    ]
    verifier = _RecordingVerifier()
    _discover_chat_proposals(
        evidence,
        cues=cues,
        texts=texts,
        entity_groups=[],
        max_cues=3,
        support_srt_texts=[],
        entity_verifier=verifier,
    )
    same_span = [r for r in verifier.requests if r["cue_indexes"] == [1]]
    assert len(same_span) == 1
    assert same_span[0]["exact_text"] == "甲乙丙丁戊己庚辛壬癸"


class TestCausalReadFloor:
    """念读因果下界（Ivan 2026-07-20）：发送+2s 之前开始的 cue 不可能在念它。"""

    def test_physically_impossible_bind_excluded(self) -> None:
        srt = _srt((10_000, 12_000, "甲乙丙丁戊己庚"))
        cues = [c for c in parse_srt_cues(srt) if c.text.strip()]
        texts = [c.text for c in cues]
        verifier = _RecordingVerifier()
        # 发送于 cue 开始前 1s：渲染+反应+推流不可能在 1s 内完成
        _discover_chat_proposals(
            [ChatEvidence("danmaku", 9_000, "甲乙丙丁戊己庚辛壬癸", "快手")],
            cues=cues, texts=texts, entity_groups=[],
            max_cues=3, support_srt_texts=[], entity_verifier=verifier,
        )
        assert verifier.requests == []

    def test_plausible_bind_still_enters(self) -> None:
        srt = _srt((10_000, 12_000, "甲乙丙丁戊己庚"))
        cues = [c for c in parse_srt_cues(srt) if c.text.strip()]
        texts = [c.text for c in cues]
        verifier = _RecordingVerifier()
        # 发送于 cue 开始前 2.5s：过弱界，正常进近失仲裁
        _discover_chat_proposals(
            [ChatEvidence("danmaku", 7_500, "甲乙丙丁戊己庚辛壬癸", "正常")],
            cues=cues, texts=texts, entity_groups=[],
            max_cues=3, support_srt_texts=[], entity_verifier=verifier,
        )
        assert len(verifier.requests) == 1
