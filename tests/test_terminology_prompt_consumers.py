"""The repair and fallback CPA paths must keep project term meanings."""
import hashlib
import json

import pytest

from src.autoslice.acoustic_witness_adjudication import (
    build_witness_request, judge_word_choice,
)
from src.autoslice.acoustic_witness_availability import pending_acoustic_witness
from src.autoslice.final_review_auditor import (
    _derive_single_span_edit, build_context_adjudication_request,
)
from src.autoslice.missing_proposal_bootstrap import (
    bootstrap_missing_proposal, converge_missing_proposal,
)
from src.autoslice.term_authority import terminology_prompt_context


@pytest.mark.parametrize("action", ["bootstrap", "converge", "judge"])
def test_normal_local_consumers_receive_meaning_and_hard_canon(action, monkeypatch):
    monkeypatch.delenv("AUTOSLICE_BASE", raising=False)
    current = "我会说立语"
    srt = "1\n00:00:01,000 --> 00:00:02,000\n" + current + "\n"
    finding = {"cue_index": 1, "suspect": "立语", "why": "测试专名语义"}
    prompts = []

    def call(prompt):
        prompts.append(prompt)
        if action == "bootstrap":
            return json.dumps({"status": "UNRESOLVED", "proposed_cue": ""})
        if action == "converge":
            return json.dumps({"decision": "KEEP_EXISTING", "replacement_text": ""})
        return json.dumps({"choice": "CURRENT", "needs_audio": False, "reason": "梗语义"})

    if action == "judge":
        request = build_context_adjudication_request(
            srt, {**finding, "suggestion": "日语", "proposed_full_cue": "我会说日语"}
        )
        witness_request = build_witness_request(request)
        before = json.dumps(witness_request, sort_keys=True)
        result = judge_word_choice(llm_call=call, check_request=request,
                                  witness=pending_acoustic_witness(witness_request))
        assert result["choice"] == "CURRENT"
        assert json.dumps(witness_request, sort_keys=True) == before
        assert "立语" not in json.dumps(witness_request, ensure_ascii=False)
    else:
        kwargs = dict(srt_text=srt, finding=finding, structured_chat="", llm_call=call,
                      derive_single_span_edit=_derive_single_span_edit)
        if action == "converge":
            result, audit = converge_missing_proposal(**kwargs, proposal_audit={})
            assert result is not None and audit["decision"] == "KEEP_EXISTING"
            assert audit["prompt_sha256"] == "sha256:" + hashlib.sha256(prompts[0].encode()).hexdigest()
        else:
            bootstrap_missing_proposal(**kwargs)
    assert len(prompts) == 1
    assert "日语的戏仿说法" in prompts[0]
    assert "她认真说「日语」时保持日语原样" in prompts[0]
    assert "「直女」一律写作「侄女」" in prompts[0]


def test_doctor_nickname_context_is_recalled_for_both_registered_candidate_surfaces():
    contexts = {
        text: terminology_prompt_context(text)
        for text in (
            "得问陆医生喜不喜欢吃五仁",
            "得问露医生喜不喜欢吃五仁",
        )
    }

    for text, context in contexts.items():
        lines = [line for line in context.splitlines() if "医生" in line]
        assert lines == [
            "- 人名：露蒂斯、露医生、陆医生（露医生是露蒂斯的别名，来自咕咕诊所医生形象；露蒂丝是既有名字写法。字幕保留实际说出的称呼，不能因为同一人物就把露医生展开成露蒂斯。陆医生作为普通陆姓医生词面保留，不能由露医生全局替换或反向替换；具体归属按本句 source truth 与语境判定。）"
        ], text
        assert "露医生是露蒂斯的别名" in lines[0]
        assert "咕咕诊所医生形象" in lines[0]
        assert "不能由露医生全局替换或反向替换" in lines[0]


def test_doctor_alias_is_a_long_lived_registered_person_name_without_expansion():
    from src.autoslice.term_authority import (
        expected_value_respell_pairs, registered_person_names, removed_registered_names,
    )
    names = registered_person_names()
    assert {"露蒂斯", "露医生"} <= names
    assert removed_registered_names("露医生说她喜欢五仁", "陆医生说她喜欢五仁") == ("露医生",)
    pairs = expected_value_respell_pairs()
    assert ("露医生", "露蒂斯") not in pairs
    assert ("陆医生", "露医生") not in pairs
    assert "露医生是露蒂斯的别名" in terminology_prompt_context("我问露蒂斯了吗")
