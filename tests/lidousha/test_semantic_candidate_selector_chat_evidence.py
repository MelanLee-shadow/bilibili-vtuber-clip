import hashlib
import html
import json
from pathlib import Path

import pytest

import src.autoslice.semantic_candidate_selector as selector
from src.autoslice.review_evidence import SourceCue
from src.autoslice.selection_scorecard import selection_scorecard_is_valid
from src.autoslice.semantic_candidate_selector import (
    SEMANTIC_CHAT_ALGORITHM_ID,
    SEMANTIC_CHAT_MAX_CHAINS_PER_WINDOW,
    SEMANTIC_CHAT_POLICY_SHA256,
    select_semantic_session_candidates_covered,
)


def _xml(entries: list[tuple[float, str]]) -> str:
    body = "\n".join(
        f'<d p="{offset:.3f},1,25,16777215,0,0,0,0">{html.escape(text)}</d>'
        for offset, text in entries
    )
    return f"<?xml version='1.0' encoding='utf-8'?><i>{body}</i>"


def _cues(start_ms: int, end_ms: int, *, step_ms: int = 5_000) -> list[SourceCue]:
    return [
        SourceCue(
            cue_id=f"c{position}",
            source_start_ms=at_ms,
            source_end_ms=min(end_ms, at_ms + step_ms - 250),
            text=f"第{position}句直播字幕",
            language="zh",
            kind="speech",
            confidence=1.0,
        )
        for position, at_ms in enumerate(range(start_ms, end_ms, step_ms), start=1)
    ]


def _recall(
    tmp_path: Path,
    entries: list[tuple[float, str]],
    *,
    start_ms: int = 0,
    end_ms: int = 180_000,
) -> tuple[str, dict[str, object], bytes]:
    xml_path = tmp_path / "chat.xml"
    xml_bytes = _xml(entries).encode()
    xml_path.write_bytes(xml_bytes)
    prompts: list[str] = []

    def llm(prompt: str) -> str:
        prompts.append(prompt)
        return json.dumps({"candidates": []}, ensure_ascii=False)

    _selected, diagnostics = select_semantic_session_candidates_covered(
        _cues(start_ms, end_ms),
        llm_call=llm,
        max_candidates=5,
        danmaku_xml=xml_path,
    )
    assert len(prompts) == 1
    return prompts[0], diagnostics, xml_bytes


def test_20260809_request_action_reaction_chat_reaches_recall_prompt(tmp_path) -> None:
    """8/9 22:12 段：burst 的前三条样本漏掉了真正的互动触发弹幕。"""

    request = "正面，趴着地上，双手撑着脸，然后小腿乱晃这种。主要是正面有一种撒娇的感觉！"
    entries = [
        (1322.044, "[李豆沙_哇嗷]"),
        (1327.263, "？？"),
        (1327.509, "问号"),
        (1342.849, request),
        (1354.821, "打call"),
        (1372.754, request),
        (1404.206, "萌"),
        (1405.237, "正面可以看见的吧？"),
        (1407.849, "可爱捏"),
        (1408.125, "可爱捏"),
        (1409.135, "可爱捏"),
        (1411.718, "我去 萌之萌之"),
        (1414.370, "可爱捏"),
        (1415.645, "可爱捏"),
    ]
    xml_path = tmp_path / "22966160_20260809-22-12-34.xml"
    xml_bytes = _xml(entries).encode()
    xml_path.write_bytes(xml_bytes)
    prompts: list[str] = []

    def llm(prompt: str) -> str:
        prompts.append(prompt)
        return json.dumps({"candidates": []}, ensure_ascii=False)

    _selected, diagnostics = select_semantic_session_candidates_covered(
        _cues(1_320_000, 1_440_000),
        llm_call=llm,
        max_candidates=5,
        # This reproduces the old burst surface: only the first three samples
        # from the bucket, none of which is the actual request.
        danmaku_hints="22:00 x14: [李豆沙_哇嗷] / ？？ / 问号",
        danmaku_xml=xml_path,
    )

    assert len(prompts) == 1
    prompt = prompts[0]
    assert request in prompt
    assert "x2" in prompt
    assert "可爱捏" in prompt
    assert "22:34 打call" not in prompt
    source_sha256 = "sha256:" + hashlib.sha256(xml_bytes).hexdigest()
    assert source_sha256 in prompt
    assert diagnostics["semantic_chat_evidence"]["source_sha256"] == source_sha256
    assert diagnostics["semantic_chat_evidence"]["selected_chain_count"] >= 1
    assert diagnostics["semantic_chat_evidence"]["policy_sha256"] == (SEMANTIC_CHAT_POLICY_SHA256)
    assert diagnostics["semantic_chat_evidence"]["algorithm_id"] == SEMANTIC_CHAT_ALGORITHM_ID


def test_device_accident_heat_without_request_yields_zero_chains(tmp_path) -> None:
    prompt, diagnostics, _xml_bytes = _recall(
        tmp_path,
        [
            (10.0, "耳朵掉了"),
            (11.0, "手机没电了"),
            (12.0, "耳机又掉了"),
            (13.0, "手机没电了"),
            (14.0, "耳朵掉了"),
        ],
    )

    evidence = diagnostics["semantic_chat_evidence"]
    assert evidence["selected_chain_count"] == 0
    assert "本窗没有满足有界问句/动作请求规则" in prompt
    assert "耳朵掉了" not in prompt
    assert "手机没电了" not in prompt


def test_song_call_spam_cannot_open_a_talk_chain(tmp_path) -> None:
    prompt, diagnostics, _xml_bytes = _recall(
        tmp_path,
        [
            (10.0, "打call"),
            (11.0, "好听"),
            (12.0, "安可"),
            (13.0, "666"),
            (14.0, "啊啊啊"),
            (15.0, "好听好听"),
            (16.0, "李豆沙李豆沙李豆沙"),
            (17.0, "❤️豆沙🧡豆沙💛豆沙💚豆沙"),
        ],
    )

    assert diagnostics["semantic_chat_evidence"]["selected_chain_count"] == 0
    assert "- trigger " not in prompt


def test_reaction_only_repetition_cannot_open_a_talk_chain(tmp_path) -> None:
    prompt, diagnostics, _xml_bytes = _recall(
        tmp_path,
        [(10.0 + index, "可爱捏") for index in range(8)] + [(30.0, "可以可以"), (31.0, "可以可以")],
    )

    assert diagnostics["semantic_chat_evidence"]["selected_chain_count"] == 0
    assert "可爱捏" not in prompt
    assert "可以可以" not in prompt


def test_request_outside_actual_cue_window_is_not_exposed(tmp_path) -> None:
    outside_request = "来点趴地晃腿动作吗？"
    prompt, diagnostics, _xml_bytes = _recall(
        tmp_path,
        [(10.0, outside_request), (110.0, "可爱捏")],
        start_ms=100_000,
        end_ms=160_000,
    )

    assert outside_request not in prompt
    assert diagnostics["semantic_chat_evidence"]["window_start_ms"] == 100_000
    assert diagnostics["semantic_chat_evidence"]["selected_chain_count"] == 0


def test_duplicate_requests_are_clustered_and_total_chains_are_capped(tmp_path) -> None:
    entries = [
        (index * 70.0 + delta, f"请求{index:02d}：请试试做一个动作吗？")
        for index in range(16)
        for delta in (0.0, 2.0)
    ]
    prompt, diagnostics, _xml_bytes = _recall(
        tmp_path,
        entries,
        end_ms=1_200_000,
    )

    evidence = diagnostics["semantic_chat_evidence"]
    assert evidence["eligible_trigger_group_count"] == 16
    assert evidence["selected_chain_count"] == SEMANTIC_CHAT_MAX_CHAINS_PER_WINDOW
    assert prompt.count("- trigger ") == SEMANTIC_CHAT_MAX_CHAINS_PER_WINDOW
    assert prompt.count(" x2:") == SEMANTIC_CHAT_MAX_CHAINS_PER_WINDOW
    assert "请求11" in prompt
    assert "请求12" not in prompt


def test_shards_share_one_xml_read_and_parse_but_bind_each_prompt(tmp_path, monkeypatch) -> None:
    xml_path = tmp_path / "long.xml"
    xml_bytes = _xml(
        [
            (100.0, "来点第一个动作吗？"),
            (105.0, "来点第一个动作吗？"),
            (2_000.0, "来点第二个动作吗？"),
            (2_005.0, "来点第二个动作吗？"),
        ]
    ).encode()
    xml_path.write_bytes(xml_bytes)
    counts = {"read": 0, "parse": 0}
    original_isolated_read = selector.read_source_bytes_isolated
    original_parse = selector.parse_blrec_danmaku_xml

    def counted_isolated_read(path: Path):
        if path == xml_path:
            counts["read"] += 1
        return original_isolated_read(path)

    def counted_parse(xml_text: str):
        counts["parse"] += 1
        return original_parse(xml_text)

    monkeypatch.setattr(selector, "read_source_bytes_isolated", counted_isolated_read)
    monkeypatch.setattr(selector, "parse_blrec_danmaku_xml", counted_parse)
    prompts: list[str] = []

    def llm(prompt: str) -> str:
        prompts.append(prompt)
        return json.dumps({"candidates": []}, ensure_ascii=False)

    _selected, diagnostics = select_semantic_session_candidates_covered(
        _cues(0, 3_600_000, step_ms=20_000),
        llm_call=llm,
        max_candidates=8,
        danmaku_xml=xml_path,
    )

    source_sha256 = "sha256:" + hashlib.sha256(xml_bytes).hexdigest()
    assert counts == {"read": 1, "parse": 1}
    assert len(prompts) == 2
    assert all(source_sha256 in prompt for prompt in prompts)
    assert all(SEMANTIC_CHAT_POLICY_SHA256 in prompt for prompt in prompts)
    assert diagnostics["semantic_chat_evidence"]["source_sha256"] == source_sha256
    assert diagnostics["semantic_chat_evidence"]["shard_count"] == 2
    shard_receipts = [shard["semantic_chat_evidence"] for shard in diagnostics["shards"]]
    assert all(receipt["source_sha256"] == source_sha256 for receipt in shard_receipts)
    assert len({receipt["evidence_sha256"] for receipt in shard_receipts}) == 2


def test_isolated_read_timeout_is_explicit_in_diagnostics(tmp_path, monkeypatch) -> None:
    xml_path = tmp_path / "blocked.xml"
    xml_path.write_text("<i />", encoding="utf-8")

    def blocked(_path: Path):
        raise selector.IsolatedSourceReadError(
            "SOURCE_READ_TIMEOUT",
            {"source_path": str(xml_path), "reason_code": "SOURCE_READ_TIMEOUT"},
        )

    monkeypatch.setattr(selector, "read_source_bytes_isolated", blocked)
    _selected, diagnostics = select_semantic_session_candidates_covered(
        _cues(0, 60_000),
        llm_call=lambda _prompt: json.dumps({"candidates": []}),
        max_candidates=5,
        danmaku_xml=xml_path,
    )

    evidence = diagnostics["semantic_chat_evidence"]
    assert evidence["status"] == "SOURCE_READ_TIMEOUT"
    assert evidence["isolated_read"]["reason_code"] == "SOURCE_READ_TIMEOUT"


def test_preloaded_chat_bytes_must_bind_the_declared_xml_path(tmp_path) -> None:
    declared = tmp_path / "declared.xml"
    other = tmp_path / "other.xml"
    declared.write_text("<i />", encoding="utf-8")
    other.write_text("<i />", encoding="utf-8")
    wrong_read = selector.read_source_bytes_isolated(other, spool_root=tmp_path / "spool")

    items, evidence = selector._load_hash_bound_semantic_chat(
        declared,
        source_read=wrong_read,
    )

    assert items == ()
    assert evidence["status"] == "SOURCE_BINDING_MISMATCH"


@pytest.mark.parametrize(
    ("mode", "expected_status"),
    [("missing", "SOURCE_MISSING"), ("malformed", "SOURCE_UNPARSEABLE")],
)
def test_missing_or_malformed_xml_is_explicit_in_diagnostics(
    tmp_path, mode: str, expected_status: str
) -> None:
    xml_path = tmp_path / f"{mode}.xml"
    if mode == "malformed":
        xml_path.write_text("<i><d broken", encoding="utf-8")

    _selected, diagnostics = select_semantic_session_candidates_covered(
        _cues(0, 60_000),
        llm_call=lambda _prompt: json.dumps({"candidates": []}),
        max_candidates=5,
        danmaku_xml=xml_path,
    )

    evidence = diagnostics["semantic_chat_evidence"]
    assert evidence["status"] == expected_status
    assert evidence["policy_sha256"] == SEMANTIC_CHAT_POLICY_SHA256
    if mode == "malformed":
        assert evidence["source_sha256"].startswith("sha256:")


def test_scorecard_persists_chat_policy_and_evidence_fingerprints(tmp_path) -> None:
    xml_path = tmp_path / "chat.xml"
    xml_path.write_text(_xml([(1.0, "来点趴地晃腿动作吗？")]), encoding="utf-8")

    def llm(_prompt: str) -> str:
        return json.dumps(
            {
                "candidates": [
                    {
                        "start_cue": 1,
                        "end_cue": 12,
                        "kind": "talk",
                        "event_key": "动作互动",
                        "hook": "观众让她现场做动作",
                        "confidence": 0.9,
                        "selection_scorecard": {
                            "tier": 2,
                            "tier_basis": "personal_stance",
                            "tier_reason": "候选窗内互动",
                            "tier_evidence_cues": [1, 2],
                            "dimensions": {
                                "lidousha_centrality": 4,
                                "stance_intensity": 2,
                                "audience_salience": 2,
                                "relationship_interaction": 3,
                                "persona_reversal": 3,
                                "comedic_payoff": 3,
                                "self_contained": 3,
                            },
                            "uncertainty_penalty": 0,
                            "fatigue_penalty": 0,
                        },
                    }
                ]
            },
            ensure_ascii=False,
        )

    selected, diagnostics = select_semantic_session_candidates_covered(
        _cues(0, 60_000),
        llm_call=llm,
        max_candidates=5,
        danmaku_xml=xml_path,
    )

    scorecard = diagnostics["scorecards"][selected[0].anchor.candidate_id]
    provenance = scorecard["semantic_recall_chat_evidence"]
    assert selection_scorecard_is_valid(scorecard)
    assert provenance["policy_sha256"] == SEMANTIC_CHAT_POLICY_SHA256
    assert provenance["source_sha256"].startswith("sha256:")
    assert provenance["evidence_sha256"].startswith("sha256:")


def test_source_drift_changes_hash_bound_evidence_identity(tmp_path) -> None:
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    first_dir.mkdir()
    second_dir.mkdir()
    _prompt, first, _bytes = _recall(
        first_dir,
        [(10.0, "来点趴地晃腿动作吗？")],
    )
    _prompt, second, _bytes = _recall(
        second_dir,
        [(10.0, "来点飞吻动作吗？")],
    )

    old_identity = first["semantic_chat_evidence"]
    current_identity = second["semantic_chat_evidence"]
    assert old_identity["schema_version"] == "semantic-recall-chat-evidence.v1"
    assert old_identity["algorithm_id"] == "bounded-request-reaction.v1"
    assert old_identity["policy_sha256"] == current_identity["policy_sha256"]
    assert old_identity["source_sha256"] != current_identity["source_sha256"]
    assert old_identity["evidence_sha256"] != current_identity["evidence_sha256"]
