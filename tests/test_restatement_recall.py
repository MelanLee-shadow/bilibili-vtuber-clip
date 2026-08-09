from __future__ import annotations

import inspect
import json

import src.autoslice.restatement_recall as rr
from src.autoslice.restatement_recall import (
    RestatementCue,
    discover_restatement_findings,
    find_restatement_pairs,
    merge_restatement_priority_findings,
    strip_leading_connectors,
)


def _cue(index: int, start: float, text: str, label: str | None = "李豆沙"):
    return RestatementCue(index=index, start_seconds=start, label=label, text=text)


def test_flagship_cue17_cue29_pair_is_detected_with_production_defaults() -> None:
    cues = [
        _cue(17, 36.3, "这是我的小孩就是了", label="连线"),
        _cue(20, 43.7, "不小心把她小孩杀了"),
        _cue(29, 58.8, "这是我今天的宣言"),
    ]
    pairs = find_restatement_pairs(cues)
    assert [(p.early_index, p.late_index) for p in pairs] == [(17, 29)]
    pair = pairs[0]
    assert pair.prefix_run >= 3
    assert pair.similarity >= 0.45


def test_garbled_early_cue_label_is_not_a_hard_prefilter() -> None:
    # cue17 is machine-labeled 连线; the early side must still qualify.
    cues = [_cue(1, 0.0, "这是我的小孩就是了", label="连线"), _cue(4, 20.0, "这是我今天的宣言")]
    assert find_restatement_pairs(cues)


def test_faithful_restart_is_still_proposed_for_witness_noop() -> None:
    # cue76/79 shape: ASR faithfully caught the interrupted start. The witness
    # is the layer that decides no repair is needed; detection still pairs it.
    cues = [_cue(76, 100.0, "我肯定是杀", label="李豆沙"), _cue(79, 104.0, "我肯定是先杀莉亚")]
    assert find_restatement_pairs(cues)


def test_short_disfluency_restart_is_excluded_by_min_chars() -> None:
    cues = [_cue(7, 14.3, "我是"), _cue(9, 15.6, "我是今天争取骗一骗女主播的沙豆李")]
    assert find_restatement_pairs(cues) == []


def test_connector_prefix_does_not_fake_an_anchor() -> None:
    cues = [_cue(88, 10.0, "然后我看就是"), _cue(94, 20.0, "然后我就在旁边默默")]
    assert find_restatement_pairs(cues) == []


def test_identical_repeat_is_not_a_repair_pair() -> None:
    cues = [_cue(1, 0.0, "再也不相信真善美了"), _cue(4, 30.0, "再也不相信真善美了")]
    assert find_restatement_pairs(cues) == []


def test_guest_late_cue_cannot_be_the_restatement_side() -> None:
    cues = [
        _cue(17, 36.3, "这是我的小孩就是了", label="连线"),
        _cue(29, 58.8, "这是我今天的宣言", label="连线"),
    ]
    assert find_restatement_pairs(cues) == []


def test_gap_beyond_window_is_excluded() -> None:
    cues = [_cue(16, 0.0, "我这把平时贪生怕死不做任务"), _cue(68, 103.0, "我这把会很努力做任务的")]
    assert find_restatement_pairs(cues) == []


def test_missing_pinyin_backend_returns_empty(monkeypatch) -> None:
    monkeypatch.setattr(rr, "_lazy_pinyin", None)
    cues = [_cue(17, 36.3, "这是我的小孩就是了"), _cue(29, 58.8, "这是我今天的宣言")]
    assert find_restatement_pairs(cues) == []


def test_strip_leading_connectors_iterates_and_keeps_content() -> None:
    assert strip_leading_connectors("然后呃我看就是") == "我看就是"
    assert strip_leading_connectors("这是我今天的宣言") == "这是我今天的宣言"


def _srt_time(seconds: float) -> str:
    total_ms = round(seconds * 1000)
    ms = total_ms % 1000
    total_s = total_ms // 1000
    s = total_s % 60
    total_m = total_s // 60
    m = total_m % 60
    h = total_m // 60
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _srt_block(index: int, start_s: float, text: str) -> str:
    end_s = start_s + 1.0
    return f"{index}\n{_srt_time(start_s)} --> {_srt_time(end_s)}\n{text}\n"


def _flagship_srt() -> str:
    """29-cue transcript: cue17 (连线, garbled) is restated by cue29 (李豆沙)."""

    blocks: list[str] = []
    for idx in range(1, 30):
        if idx == 17:
            text = "[连线] 这是我的小孩就是了"
            start_s = 36.3
        elif idx == 20:
            text = "不小心把她小孩杀了"
            start_s = 43.7
        elif idx == 29:
            text = "[李豆沙] 这是我今天的宣言"
            start_s = 58.8
        else:
            # Below min_early_chars/min_late_chars on both sides so filler
            # cues can never pair with each other or with the flagship pair.
            text = "填充"
            start_s = idx * 2.0
        blocks.append(_srt_block(idx, start_s, text))
    return "\n".join(blocks)


def test_discover_restatement_findings_flagship_produces_one_finding() -> None:
    findings, receipt = discover_restatement_findings(_flagship_srt())

    assert len(findings) == 1
    finding = findings[0]
    assert finding["cue"] == 17
    assert finding["kind"] == "context"
    assert finding["suspect"] == "[连线] 这是我的小孩就是了"
    assert finding["proposed_full_cue"] == "[连线] 这是我今天的宣言"
    assert finding["repair_class"] == "phonetic"
    assert finding["evidence_cue_ids"] == [29]

    provenance = finding["candidate_provenance"]
    assert provenance["kind"] == "session_restatement"
    assert provenance["mutation_authorized"] is False
    assert provenance["source_cue"] == 29
    assert provenance["similarity"] >= 0.45
    assert provenance["prefix_run"] >= 3
    assert provenance["late_text_sha256"].startswith("sha256:")

    assert receipt["schema_version"] == "restatement-candidates.v1"
    assert receipt["status"] == "PASS"
    assert receipt["finding_count"] == 1
    assert receipt["findings"][0]["cue_index"] == 17
    assert receipt["findings"][0]["finding_sha256"].startswith("sha256:")


def test_discover_restatement_findings_uniform_host_period_has_no_label() -> None:
    srt = "\n".join(
        [
            _srt_block(1, 0.0, "这是我的小孩就是了"),
            _srt_block(2, 10.0, "无关填充"),
            _srt_block(3, 20.0, "这是我今天的宣言"),
        ]
    )
    findings, receipt = discover_restatement_findings(srt)

    assert len(findings) == 1
    finding = findings[0]
    assert finding["cue"] == 1
    assert finding["suspect"] == "这是我的小孩就是了"
    assert finding["proposed_full_cue"] == "这是我今天的宣言"
    assert receipt["status"] == "PASS"


def test_discover_restatement_findings_fails_open_on_detector_error(monkeypatch) -> None:
    def _boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(rr, "find_restatement_pairs", _boom)
    findings, receipt = discover_restatement_findings(_flagship_srt())

    assert findings == []
    assert receipt["status"] == "ERROR"
    assert receipt["reason_code"] == "RESTATEMENT_DISCOVERY_ERROR"
    assert receipt["error_type"] == "RuntimeError"


def test_merge_restatement_priority_findings_appends_and_persists_receipt(
    tmp_path,
) -> None:
    microcue_findings = [{"cue": 5, "kind": "context", "why": "microcue stub"}]
    microcue_audit = {"schema_version": "microcue-candidate-blind-acoustic-discovery.v1"}

    merged_findings, merged_audit = merge_restatement_priority_findings(
        _flagship_srt(),
        microcue_findings,
        microcue_audit,
        out_root=tmp_path,
        cid="auto_200736_298_383",
    )

    assert merged_findings[0] == microcue_findings[0]
    assert len(merged_findings) == 2
    assert merged_findings[1]["cue"] == 17
    assert merged_audit is microcue_audit

    receipt_path = tmp_path / "auto_200736_298_383.restatement-candidates.json"
    assert receipt_path.exists()
    persisted = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert persisted["status"] == "PASS"
    assert persisted["finding_count"] == 1


def test_merge_restatement_priority_findings_fails_open_to_microcue_findings_only(
    tmp_path, monkeypatch
) -> None:
    def _boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(rr, "find_restatement_pairs", _boom)
    microcue_findings = [{"cue": 5, "kind": "context", "why": "microcue stub"}]
    microcue_audit = {"schema_version": "microcue-candidate-blind-acoustic-discovery.v1"}

    merged_findings, merged_audit = merge_restatement_priority_findings(
        _flagship_srt(),
        microcue_findings,
        microcue_audit,
        out_root=tmp_path,
        cid="auto_error_case",
    )

    assert merged_findings == microcue_findings
    assert merged_audit is microcue_audit

    receipt_path = tmp_path / "auto_error_case.restatement-candidates.json"
    persisted = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert persisted["status"] == "ERROR"


def test_producer_text_pipeline_wires_restatement_findings_into_review_closure() -> None:
    # Negative canary: reverting the pipeline wiring (dropping the call
    # inside review_exact_final_srt) must turn this test red.
    # 2026-08-10（F20）：微 cue 发现 + 重述注入合并成
    # delivery_fast_path.discover_priority_findings 一个调用点，快路径才能
    # 在真值全所有权时整条跳过；金丝雀跟着搬到那一层，语义不变。
    import src.autoslice.delivery_fast_path as delivery_fast_path
    import src.autoslice.producer_text_pipeline as producer_text_pipeline

    assert (
        delivery_fast_path.merge_restatement_priority_findings
        is merge_restatement_priority_findings
    )
    assert (
        producer_text_pipeline.discover_priority_findings
        is delivery_fast_path.discover_priority_findings
    )
    source = inspect.getsource(producer_text_pipeline.run_text_pipeline)
    assert "discover_priority_findings" in source
    assert "merge_restatement_priority_findings" in inspect.getsource(
        delivery_fast_path.discover_priority_findings
    )
