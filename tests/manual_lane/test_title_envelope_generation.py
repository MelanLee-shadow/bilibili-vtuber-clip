"""Step 6 distinguishes literal new bodies from legacy complete titles."""

from __future__ import annotations

import json

import pytest
from fakes import FakeCpa, FakeRemote, judge_fail, make_ctx, srt

from src.autoslice import channel_envelope
from src.autoslice.channel_envelope import render_talk_title
from src.autoslice.manual_lane import normalize
from src.autoslice.manual_lane import stage_impl as si
from src.autoslice.manual_lane.runner import StepOutcome

HOST = "李豆沙"
GUEST = "雾深Girimi"
BODY = "她听完那个故事突然停住了"


def _candidate(field, text, on_stage):
    return {field: text, "on_stage": on_stage, "punchline_person": "她听完故事的反应",
            "swap_check": {"holds_with_other_host": False, "reason": "换主播不成立"}}


def _answer(*candidates):
    return json.dumps({"candidates": list(candidates), "recommended": 0, "cover_lines": ["突然停住", "听完故事"]},
                      ensure_ascii=False)


def _pick(tmp_path, participants, answers, *, judge=(), speaker=None):
    cpa = FakeCpa(answers, judge=judge)
    session = {"recording_date": "2026-10-24", "speaker_mode": "uniform_host", "enablement_evidence": None,
               "participants": [[name] for name in participants], "own_uploader_mids": [1]}
    ctx = make_ctx(tmp_path, remote=FakeRemote(), cpa=cpa, session=session)
    (ctx.clip_dir / "subs").mkdir(parents=True, exist_ok=True)
    (ctx.clip_dir / "subs/final.srt").write_text(srt(3), encoding="utf-8")
    if speaker is not None:
        (ctx.clip_dir / "speaker").mkdir(parents=True, exist_ok=True)
        (ctx.clip_dir / "speaker/speaker.srt").write_text(speaker, encoding="utf-8")
    return ctx, cpa, si.pick_title(ctx)


@pytest.mark.parametrize(("participants", "on_stage", "body", "partners"), [
    ([HOST], [HOST], "【直播回放】" + BODY, ()),
    ([HOST], [HOST], "【雨夜闲聊】" + BODY, ()),
    ([HOST], [HOST], "【李豆沙】" + BODY, ()),
    ([HOST, GUEST], [HOST, GUEST], BODY, (GUEST,)),
    ([HOST, "礼墨Sumi", "奈奈莉娅"], ["奈奈莉娅", HOST, "礼墨Sumi", "奈奈莉娅"], BODY,
     ("奈奈莉娅", "礼墨Sumi")),
])
def test_explicit_body_reaches_judge_and_persisted_title_unchanged(
    tmp_path, participants, on_stage, body, partners,
):
    response = _answer(_candidate("title_body", body, on_stage))
    ctx, cpa, picked = _pick(tmp_path, participants, [response] * 3)
    assert not isinstance(picked, StepOutcome), picked
    expected = render_talk_title(normalize.canon_text(body)[0], partners)
    data = json.loads((ctx.clip_dir / "title/candidates.json").read_text(encoding="utf-8"))
    assert data["rounds"] == ["title"] and not data["forced"]
    assert data["chosen"]["checks"]["ok"] and data["chosen"]["accepted"]
    assert data["candidates"][0]["title"] == data["chosen"]["title"] == picked[0] == expected
    assert (ctx.clip_dir / "title/title.txt").read_text(encoding="utf-8") == expected + "\n"
    assert (ctx.clip_dir / "cpa/title.completion.txt").read_text(encoding="utf-8") == response
    judge_prompts = [p for call, p in zip(cpa.calls, cpa.prompts, strict=True) if "title-judge" in call]
    assert len(judge_prompts) == 2 and all(f"标题：{expected}" in p for p in judge_prompts)
    assert "title-regen-1" not in cpa.calls


def test_legacy_complete_title_is_checked_without_rerendering(tmp_path):
    complete = render_talk_title(BODY, (GUEST,))
    response = _answer(_candidate("title", complete, [HOST, GUEST]))
    ctx, _, picked = _pick(tmp_path, [HOST, GUEST], [response])
    assert not isinstance(picked, StepOutcome), picked
    data = json.loads((ctx.clip_dir / "title/candidates.json").read_text(encoding="utf-8"))
    assert data["chosen"]["accepted"] and data["chosen"]["title"] == complete == picked[0]


def test_anonymous_speaker_is_a_role_not_a_partner_and_bad_cover_is_regenerated(tmp_path):
    bad = json.loads(_answer(_candidate("title", "【李豆沙×连线】" + BODY, [HOST, "连线"])))
    bad["cover_lines"] = ["连线：听我说", "她停住了"]
    good = _answer(_candidate("title_body", BODY, [HOST]))
    ctx, cpa, picked = _pick(tmp_path, [HOST, "连线"], [json.dumps(bad, ensure_ascii=False), good])
    assert not isinstance(picked, StepOutcome), picked
    assert picked[0] == render_talk_title(BODY)
    assert "title-regen-1" in cpa.calls
    assert "本场参与者名单（正式名）：李豆沙\n" in cpa.title_prompts[0]
    assert "连线是身份未知的角色标签" in cpa.prompts[cpa.calls.index("title-regen-1")]
    from src.autoslice.manual_lane.stage_package import description_text
    assert "连线" not in description_text(picked[0])
    data = json.loads((ctx.clip_dir / "title/candidates.json").read_text(encoding="utf-8"))
    assert "placeholder_identity" in data["candidates"][0]["checks"]["failed"]


@pytest.mark.parametrize('bad_body', ['游戏里先让连线放心，李豆沙却慌了', '李豆沙被连线质问，连忙解释',
                                      '李豆沙和连线约好一起走，转眼就被发现', '连线说不是我杀的，李豆沙愣住了'])
def test_a_role_label_in_the_title_body_is_not_forced_as_a_person(tmp_path, bad_body):
    bad = _answer(_candidate("title_body", bad_body, [HOST]))
    good = _answer(_candidate("title_body", BODY, [HOST]))
    ctx, cpa, picked = _pick(tmp_path, [HOST, "连线"], [bad, good])
    assert not isinstance(picked, StepOutcome), picked
    assert picked[0] == render_talk_title(BODY) and "title-regen-1" in cpa.calls
    data = json.loads((ctx.clip_dir / "title/candidates.json").read_text(encoding="utf-8"))
    assert "placeholder_identity" in data["candidates"][0]["checks"]["failed"]


def test_exhausted_title_rounds_never_force_a_role_label_as_a_name(tmp_path):
    bad = _candidate("title_body", "她先和连线约好，转眼却被发现了", [HOST])
    good = _candidate("title_body", BODY, [HOST])
    response = _answer(bad, good)
    ctx, _, picked = _pick(tmp_path, [HOST, "连线"], [response] * 3,
                            judge=[judge_fail("仍需改进")] * 3)
    assert not isinstance(picked, StepOutcome), picked
    result = json.loads((ctx.clip_dir / "title/candidates.json").read_text(encoding="utf-8"))
    assert result["forced"] and picked[0] == render_talk_title(BODY)
    assert all("placeholder_identity" in c["checks"]["failed"]
               for c in result["candidates"] if "连线" in c["title"])


@pytest.mark.parametrize('text', ['她和连线嘉宾一起玩', '她把连线关掉了', '连线中突然笑场', '正在连线'])
def test_call_actions_and_anonymous_guest_roles_are_not_fake_names(text):
    from src.autoslice.manual_lane.config import placeholder_as_person
    assert not placeholder_as_person({'participants': [[HOST], ['连线']]}, text)


@pytest.mark.parametrize('label', ['连线', '连线嘉宾', '嘉宾', '未知', 'unknown', '未具名嘉宾', '未知嘉宾', '匿名嘉宾'])
def test_unknown_guest_does_not_erase_the_speaker_or_visual_role(label):
    from src.autoslice.manual_lane.config import participant_description, participant_names
    from src.autoslice.manual_lane.step_io import linked_session
    session = {"participants": [[HOST], [label]]}
    assert participant_names(session) == [HOST]
    assert participant_names(session, visual=True) == [HOST, "未具名嘉宾"]
    if label != "连线":
        assert participant_description(session) == "李豆沙。有未具名嘉宾，标签不是名字"
    assert linked_session(session)


def test_unknown_role_is_not_imported_as_a_proper_name_by_subtitle_and_speaker_inputs(tmp_path):
    from src.autoslice.manual_lane import name_match, step3_adjudicate, step4_speaker
    ctx = make_ctx(tmp_path, remote=FakeRemote(), cpa=FakeCpa([]), session={
        'participants': [[HOST], ['连线']], 'recording_date': '2026-10-24',
        'speaker_mode': 'uniform_host', 'enablement_evidence': None, 'own_uploader_mids': [1]})
    names = name_match.entries(ctx.session['participants'])
    assert '连线' not in [name for name, _aliases in names]
    assert step3_adjudicate._participant_line(ctx) == '李豆沙。连线方身份未知，标签不是名字'
    speaker_names, _ = step4_speaker.speaker_context_lines(ctx)
    assert '连线方身份未知，标签不是名字' in speaker_names


def test_title_prompt_projects_anonymous_speaker_as_a_role_without_changing_subtitles(tmp_path):
    speaker = srt(3).replace('句子1', '[连线] 句子1', 1)
    good = _answer(_candidate('title_body', BODY, [HOST]))
    ctx, cpa, picked = _pick(tmp_path, [HOST, '连线'], [good], speaker=speaker)
    assert not isinstance(picked, StepOutcome), picked
    assert '[未具名嘉宾]' in cpa.prompts[0] and '[连线]' not in cpa.prompts[0]
    assert (ctx.clip_dir / 'speaker/speaker.srt').read_text(encoding='utf-8') == speaker


def test_legacy_guest_order_is_preserved_when_on_stage_lists_the_same_people_in_another_order(tmp_path):
    guests = ["礼墨Sumi", "奈奈莉娅"]
    complete = render_talk_title(BODY, guests)
    response = _answer(_candidate("title", complete, [HOST, guests[1], guests[0], guests[1]]))
    ctx, cpa, picked = _pick(tmp_path, [HOST, *guests], [response] * 3)
    assert not isinstance(picked, StepOutcome), picked
    data = json.loads((ctx.clip_dir / "title/candidates.json").read_text(encoding="utf-8"))
    assert not data["forced"] and data["chosen"]["checks"]["ok"] and data["chosen"]["accepted"]
    assert data["chosen"]["title"] == picked[0] == complete
    assert (ctx.clip_dir / "title/title.txt").read_text(encoding="utf-8") == complete + "\n"
    assert not any("title-regen" in call for call in cpa.calls)


@pytest.mark.parametrize("body", [BODY, "", None])
def test_dual_representation_is_an_explicit_rejection(tmp_path, body):
    candidate = _candidate("title_body", body, [HOST])
    candidate["title"] = render_talk_title(BODY)
    ctx, cpa, result = _pick(tmp_path, [HOST], [_answer(candidate)])
    assert isinstance(result, StepOutcome) and not result.ok
    assert result.reason == "CPA_RESPONSE_UNPARSEABLE" and "title_body" in result.note and "title" in result.note
    assert not any("title-judge" in call for call in cpa.calls)
    assert not (ctx.clip_dir / "title/title.txt").exists()


def test_legacy_bare_title_is_not_reinterpreted_as_a_new_body(tmp_path):
    response = _answer(_candidate("title", BODY, [HOST]))
    ctx, cpa, picked = _pick(tmp_path, [HOST], [response] * 3)
    assert not isinstance(picked, StepOutcome), picked  # existing exhausted-round forced choice
    data = json.loads((ctx.clip_dir / "title/candidates.json").read_text(encoding="utf-8"))
    assert data["forced"] and not data["chosen"]["accepted"] and data["chosen"]["title"] == BODY
    assert "prefix" in data["chosen"]["checks"]["failed"]
    assert not any("title-judge" in call for call in cpa.calls)


def test_body_canon_is_applied_before_rendering_and_raw_answer_is_retained(tmp_path):
    body = "kimo熊来了她突然听得停住了"
    normalized, hits = normalize.canon_text(body)
    assert hits and normalized != body
    response = _answer(_candidate("title_body", body, [HOST]))
    ctx, _, picked = _pick(tmp_path, [HOST], [response])
    assert not isinstance(picked, StepOutcome), picked
    assert picked[0] == render_talk_title(normalized)
    assert (ctx.clip_dir / "cpa/title.completion.txt").read_text(encoding="utf-8") == response


@pytest.mark.parametrize(("participants", "bad", "failed", "reason"), [
    ([HOST], _candidate("title", "【李豆沙×】" + BODY, [HOST]), "publish_gate", "talk_title_prefix_missing"),
    ([HOST, GUEST], _candidate("title", render_talk_title(BODY), [HOST, GUEST]), "prefix_names", "前缀未写出场者"),
    ([HOST, GUEST], _candidate("title", render_talk_title(BODY, (GUEST,)), [HOST]), "prefix_names", "前缀未写出场者"),
    ([HOST], _candidate("title_body", BODY, []), "on_stage_known", "on_stage 为空"),
    ([HOST], _candidate("title_body", BODY, [HOST, "未知参与者"]), "on_stage_known", "出场者不在参与者名单"),
    ([HOST, "未登记嘉宾"], _candidate("title_body", BODY, [HOST, "未登记嘉宾"]), "publish_gate",
     "talk_partner_unresolved"),
    ([HOST, "错误×名字"], _candidate("title_body", BODY, [HOST, "错误×名字"]), "prefix_names", "前缀未写出场者"),
    ([HOST], _candidate("title_body", "很长很长的一句话" * 8, [HOST]), "publish_gate",
     "publish_title_length_out_of_bounds"),
])
def test_bad_candidate_remains_rejected_and_a_later_good_one_is_chosen(tmp_path, participants, bad, failed, reason):
    good_body = "她回过神后又补了一句话"
    response = _answer(bad, _candidate("title_body", good_body, [HOST]))
    ctx, cpa, picked = _pick(tmp_path, participants, [response])
    assert not isinstance(picked, StepOutcome), picked
    data = json.loads((ctx.clip_dir / "title/candidates.json").read_text(encoding="utf-8"))
    rejected, accepted = data["candidates"]
    assert not rejected["checks"]["ok"] and failed in rejected["checks"]["failed"]
    assert any(reason in text for text in rejected["checks"]["reasons"])
    assert accepted["checks"]["ok"] and data["chosen"]["title"] == render_talk_title(good_body)
    assert all(f"标题：{rejected['title']}" not in prompt
               for call, prompt in zip(cpa.calls, cpa.prompts, strict=True) if "title-judge" in call)


@pytest.mark.parametrize("value", ["", None, 42, []])
def test_empty_or_nonstring_body_uses_existing_no_candidate_path(tmp_path, value):
    response = _answer(_candidate("title_body", value, [HOST]))
    _, _, result = _pick(tmp_path, [HOST], [response] * 3)
    assert isinstance(result, StepOutcome) and not result.ok and result.reason == "TITLE_NO_CANDIDATE"


def test_missing_on_stage_is_rejected_by_the_existing_candidate_check(tmp_path):
    bad = _candidate("title_body", BODY, [HOST])
    del bad["on_stage"]
    good_body = "她回过神后又补了一句话"
    response = _answer(bad, _candidate("title_body", good_body, [HOST]))
    ctx, _, picked = _pick(tmp_path, [HOST], [response])
    assert not isinstance(picked, StepOutcome), picked
    data = json.loads((ctx.clip_dir / "title/candidates.json").read_text(encoding="utf-8"))
    assert "on_stage_known" in data["candidates"][0]["checks"]["failed"]
    assert data["chosen"]["title"] == render_talk_title(good_body)


@pytest.mark.parametrize("on_stage", [HOST, 12, [HOST, 12]])
def test_invalid_on_stage_shape_uses_existing_unparseable_path(tmp_path, on_stage):
    response = _answer(_candidate("title_body", BODY, on_stage))
    ctx, cpa, result = _pick(tmp_path, [HOST], [response])
    assert isinstance(result, StepOutcome) and not result.ok and result.reason == "CPA_RESPONSE_UNPARSEABLE"
    assert "on_stage" in result.note and not (ctx.clip_dir / "title/title.txt").exists()
    assert not any("title-judge" in call for call in cpa.calls)


def test_registry_failure_cannot_turn_a_candidate_into_a_pass(tmp_path, monkeypatch):
    def unavailable(*args, **kwargs):
        raise RuntimeError("registry unavailable")

    monkeypatch.setattr(channel_envelope, "resolve_partners", unavailable)
    response = _answer(_candidate("title_body", BODY, [HOST, GUEST]))
    ctx, cpa, picked = _pick(tmp_path, [HOST, GUEST], [response] * 3)
    assert not isinstance(picked, StepOutcome), picked  # existing exhausted-round forced choice
    data = json.loads((ctx.clip_dir / "title/candidates.json").read_text(encoding="utf-8"))
    assert data["forced"] and not data["chosen"]["accepted"]
    assert all("publish_gate_error:RuntimeError" in c["checks"]["reasons"] for c in data["candidates"])
    assert not any("title-judge" in call for call in cpa.calls)


def test_wrong_speaker_quote_is_rejected_after_body_rendering(tmp_path):
    speaker = "".join(f"{i}\n{si.fmt_ts((i - 1) * 2.0)} --> {si.fmt_ts(i * 2.0)}\n[{who}] 句子{i}\n\n"
                      for i, who in ((1, HOST), (2, "连线"), (3, HOST)))
    response = _answer(_candidate("title_body", "李豆沙说“句子2”之后突然安静了", [HOST]),
                       _candidate("title_body", BODY, [HOST]))
    ctx, _, picked = _pick(tmp_path, [HOST, GUEST], [response], speaker=speaker)
    assert not isinstance(picked, StepOutcome), picked
    data = json.loads((ctx.clip_dir / "title/candidates.json").read_text(encoding="utf-8"))
    checks = data["candidates"][0]["checks"]
    assert "quotes" in checks["failed"] and any("TITLE_QUOTE_SPEAKER_UNVERIFIED" in r for r in checks["reasons"])
    assert data["chosen"]["title"] == render_talk_title(BODY)


def test_judge_feedback_contains_the_rendered_title(tmp_path):
    second = "她回过神后又补了一句话"
    reason = "这条标题的事实归属不成立"
    answers = [_answer(_candidate("title_body", BODY, [HOST])), _answer(_candidate("title_body", second, [HOST]))]
    ctx, cpa, picked = _pick(tmp_path, [HOST], answers, judge=[judge_fail(reason)])
    assert not isinstance(picked, StepOutcome), picked
    feedback = cpa.prompts[cpa.calls.index("title-regen-1")]
    assert render_talk_title(BODY) in feedback and reason in feedback
    data = json.loads((ctx.clip_dir / "title/candidates.json").read_text(encoding="utf-8"))
    assert data["candidates"][0]["judge"]["overall"] == "FAIL" and picked[0] == render_talk_title(second)
