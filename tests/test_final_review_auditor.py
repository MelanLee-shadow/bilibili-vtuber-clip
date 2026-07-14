import json

from src.autoslice.final_review_auditor import audit_final_subtitles, route_findings


def _srt(*texts: str) -> str:
    blocks = []
    for index, text in enumerate(texts, start=1):
        blocks.append(
            f"{index}\n00:00:{index * 5:02d},000 --> 00:00:{index * 5 + 4:02d},000\n{text}"
        )
    return "\n\n".join(blocks) + "\n"


def _fake_llm(findings):
    def call(prompt):
        return json.dumps({"findings": findings}, ensure_ascii=False)

    return call


def _extract(raw):
    return json.loads(raw)


def test_auditor_validates_and_drops_unanchored_findings():
    source = _srt("欢迎季下", "正常的一句话")
    findings = audit_final_subtitles(
        source,
        llm_call=_fake_llm([
            {"cue": 1, "suspect": "季下", "kind": "nonword", "suggestion": "记下", "why": "非词"},
            {"cue": 2, "suspect": "不存在的片段", "kind": "context", "suggestion": None, "why": "x"},
            {"cue": 99, "suspect": "正常", "kind": "context", "suggestion": None, "why": "越界"},
        ]),
        extract_json=_extract,
    )

    assert len(findings) == 1
    assert findings[0]["suspect"] == "季下"


def test_router_applies_only_homophone_fixes():
    """审片员是发现器不是改写器：同音建议(季下→记下)自动应用；非同音建议
    (小雨→小李)只披露不落盘。"""
    source = _srt("欢迎季下", "今天小雨来了没")
    findings = [
        {"cue_index": 1, "suspect": "季下", "kind": "nonword", "suggestion": "记下", "why": "非词"},
        {"cue_index": 2, "suspect": "小雨", "kind": "self_ref", "suggestion": "小李", "why": "自称可疑"},
    ]

    output, audit = route_findings(source, findings)

    assert "欢迎记下" in output
    assert "小雨" in output  # 非同音建议不改写
    assert audit["applied_count"] == 1
    routed = {row["suspect"]: row["routed"] for row in audit["findings"]}
    assert routed == {"季下": "homophone_fix", "小雨": "disclosure"}


def test_router_never_touches_protected_chat_cues():
    source = _srt("欢迎季下")
    findings = [
        {"cue_index": 1, "suspect": "季下", "kind": "nonword", "suggestion": "记下", "why": "非词"}
    ]

    output, audit = route_findings(source, findings, protected_cue_indexes=[1])

    assert "季下" in output
    assert audit["applied_count"] == 0
    assert audit["findings"][0]["routed"] == "disclosure_protected"


def test_auditor_llm_failure_returns_empty():
    def broken(prompt):
        raise RuntimeError("cpa down")

    assert (
        audit_final_subtitles(_srt("一句"), llm_call=broken, extract_json=_extract) == []
    )
