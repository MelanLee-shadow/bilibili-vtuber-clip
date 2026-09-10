"""Recall guidance must not silently rewrite genuine ordinary nouns."""
import json

from src.autoslice.final_review_auditor import audit_final_subtitles


def test_each_occurrence_is_reviewed_without_a_global_qq_replacement():
    text = (
        "1\n00:00:00,000 --> 00:00:01,280\n都怪 QQ 群哦\n\n"
        "2\n00:00:01,480 --> 00:00:02,980\n都是 kmx 所说的\n"
    )
    prompts = []

    def observer(prompt):
        prompts.append(prompt)
        return json.dumps({"findings": []})

    findings = audit_final_subtitles(text, llm_call=observer,
                                    extract_json=json.loads, glossary_text="kmx：口播kimo熊")
    assert findings == []  # No hidden deterministic QQ-group -> name mutation.
    assert len(prompts) == 1
    assert "不能替代逐次称呼检查" in prompts[0]
    assert "同一 ASR，只是候选和语境，不是独立声学证明" in " ".join(prompts[0].split())
    assert "都怪 QQ 群哦" in prompts[0] and "都是 kmx 所说的" in prompts[0]


def test_genuine_group_discussion_remains_an_unchanged_candidate():
    text = "1\n00:00:00,000 --> 00:00:02,000\nkmx 把群号发到 QQ 群了\n"
    findings = audit_final_subtitles(text, llm_call=lambda _: '{"findings": []}',
                                    extract_json=json.loads, glossary_text="kmx")
    assert findings == []
