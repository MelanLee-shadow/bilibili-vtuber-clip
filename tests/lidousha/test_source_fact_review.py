import hashlib
import json
from copy import deepcopy
from pathlib import Path

import pytest

import src.autoslice.reviewed_subtitle_baseline_registry as baseline_registry
import src.autoslice.source_fact_staging as source_fact_staging
from src.autoslice.llm_client import LlmCallError
from src.autoslice.publish_staging import (
    _bind_normal_source_entity_projection,
    _stage_publish_draft,
)
from src.autoslice.review_evidence import SourceCue
from src.autoslice.source_fact_review import (
    _finalize_receipt,
    _valid_changed_surface,
    review_and_repair_source_facts,
    source_fact_review_passes,
    validate_source_fact_review,
)
from src.autoslice.story_contract import build_story_contract


class _MalformedSafeReasonError(RuntimeError):
    safe_reason = "raw-secret-safe-reason"


@pytest.fixture(autouse=True)
def _allow_precommit_exact_interval_authority(monkeypatch: pytest.MonkeyPatch) -> None:
    """Repository sealing has dedicated tests; this file exercises source facts."""

    monkeypatch.setattr(
        baseline_registry,
        "require_repository_asset_authority",
        lambda **_kwargs: None,
    )


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _present_speaker_evidence(transcript: str) -> dict[str, object]:
    policy_ids = {
        "alignment": "speaker_cue_subsegment_alignment/v1",
        "text": "compact_ws/v1",
        "timing": "half_open_integer_ms_exact/v1",
    }
    alignment = {
        "schema_version": "speaker-cue-subsegment-alignment.v1",
        "policy_ids": policy_ids,
        "rows": [
            {
                "cue_id": "1",
                "segments": [{"segment_index": 1, "speaker": "李豆沙", "text": "测试字幕"}],
            }
        ],
    }
    return {
        "state": "PresentValid",
        "policy_ids": policy_ids,
        "plain_srt_sha256": "sha256:" + "1" * 64,
        "speaker_final_srt_sha256": "sha256:" + "2" * 64,
        "alignment": alignment,
        "alignment_sha256": _canonical_sha256(alignment),
        "speaker_transcript": transcript,
        "speaker_transcript_sha256": "sha256:"
        + hashlib.sha256(transcript.encode("utf-8")).hexdigest(),
    }


def _absent_speaker_evidence() -> dict[str, object]:
    return {
        "state": "AbsentAuthorized",
        "reason": "speaker_mode_uniform_host",
        "policy_ids": {
            "alignment": "speaker_cue_subsegment_alignment/v1",
            "text": "compact_ws/v1",
            "timing": "half_open_integer_ms_exact/v1",
            "absence": "speaker_mode_uniform_host/v1",
        },
    }


def _completion(
    *,
    status: str,
    final_hook: str,
    final_title: str,
    supported_by: list[str],
    changed_surfaces: list[dict[str, object]] | None = None,
    selection_scorecard_review: dict[str, str] | None = None,
    addressee_attribution: list[dict[str, object]] | None = None,
    source_entity_candidates: list[dict[str, object]] | None = None,
) -> str:
    payload: dict[str, object] = {
        "schema_version": "lidousha-source-fact-review.v1",
        "status": status,
        "final_selection_hook": final_hook,
        "final_title": final_title,
        "supported_by": supported_by,
        "changed_surfaces": changed_surfaces or [],
        # F12：受话人归属判项在每份新回应里都必须存在（缺键=形状无效）。
        # 这些历史用例都没有说话人转写，判项按 UNVERIFIABLE 车道留空。
        "addressee_attribution": addressee_attribution or [],
        "selection_scorecard_review": (
            selection_scorecard_review
            or {
                "status": "NOT_NEEDED",
                "reason": "selection hook remains unchanged",
            }
        ),
        "summary": "相邻的同片文字证据足以完成事实裁决。",
    }
    if source_entity_candidates is not None:
        payload["source_entity_candidates"] = source_entity_candidates
    return json.dumps(payload, ensure_ascii=False)


def _completed_fact_fixture(tmp_path):
    calls = []
    srt = tmp_path / "final.srt"
    transcript = "技能可以叫李姐拉拉吗\n不行，我这个应该叫李姐网"
    srt.write_text("1\n00:00:00,000 --> 00:00:01,000\n" + transcript + "\n")
    hook, title = "弹幕提议把技能叫李姐拉拉。", "【李豆沙】短标题"

    def call(prompt):
        calls.append(prompt)
        current_hook, current_title = call.test_surfaces
        return _completion(
            status="REPAIR", final_hook=current_hook,
            final_title="【李豆沙】弹幕提议把技能叫李姐拉拉，主播随即拒绝",
            supported_by=["final_transcript"], changed_surfaces=[{
                "artifact": "title", "before": current_title.split("】", 1)[1], "after": "弹幕提议把技能叫李姐拉拉，主播随即拒绝",
                "reason": "补足最终字幕明确支持的完整事件。",
                "evidence": ["技能可以叫李姐拉拉吗", "不行，我这个应该叫李姐网"],
            }],
        )

    call.cpa_cache_identity = {"transport": "cpa_command", "models": ["fixture-model"], "effort": "high"}
    call.test_surfaces = [hook, title]
    call.provider_runtime_binding = {"provider_transport": "runtime_cpa", "provider_credential_source": str(tmp_path / "cpa.env"), "provider_endpoint_host": "fixture.invalid"}
    kwargs = dict(selection_hook=hook, title=title, final_transcript=transcript,
                  clip_context_prompt="", selection_scorecard=None, llm_call=call,
                  title_repair_allowed=False, candidate_id="ordinary", final_reviewed_srt_path=srt,
                  story_contract={"source_media_sha256s": ["sha256:" + "a" * 64]})
    return kwargs, call, calls


@pytest.mark.parametrize('change', [None, 'srt', 'title', 'hook', 'scorecard', 'receipt'])
def test_pinned_complete_editorial_stage_revalidates_without_a_new_fact_call(tmp_path, change):
    cid, hook, title, transcript = 'retained-editorial', '聊读春雪的感受。', '【李豆沙】分享读春雪时的真实感受', '读春雪很有意思'
    srt = tmp_path/'candidate.srt'
    srt.write_text('1\n00:00:00,000 --> 00:00:02,000\n' + transcript + '\n')
    story = build_story_contract(candidate_id=cid, selection_hook=hook, transcript_text=transcript,
        selection_scorecard=None, session_relation_authority=None)
    review = review_and_repair_source_facts(selection_hook=hook, title=title, final_transcript=transcript,
        clip_context_prompt=story['clip_context_prompt'], speaker_evidence=_absent_speaker_evidence(),
        candidate_id=cid, final_reviewed_srt_path=srt,
        llm_call=lambda _: _completion(status='KEEP', final_hook=hook, final_title=title, supported_by=['final_transcript']))
    original = deepcopy(review)
    if change == 'srt':
        transcript = '改变后的最终字幕'
        srt.write_text('1\n00:00:00,000 --> 00:00:02,000\n' + transcript + '\n')
    elif change == 'title': title += '！'
    elif change == 'hook': hook += '改变内容'
    elif change == 'scorecard': story['selection_scorecard'] = {'changed': True}
    elif change == 'receipt': review['passes'][0]['response_sha256'] = 'sha256:'+'f'*64
    calls = []
    result = source_fact_staging.resolve_initial_source_fact_review(candidate_id=cid,
        title_source='llm', title=title, selection_hook=hook, story_contract=story,
        record={'speaker_mode':'uniform_host', 'subtitle_path':str(srt)},
        cues=[SourceCue('cue-1',0,2000,transcript,'zh','speech',1.0)],
        source_fact_llm_call=lambda p: calls.append(p), recovery_publication_authority=None,
        title_authority_status='RESOLVED_CPA', title_llm_enabled=True,
        prior_authority_error=None, completed_review=review)
    assert calls == []
    if change is None:
        assert result.review == original and result.authority_error is None
        assert review == original
    else:
        assert result.review is None and 'EXISTING_SOURCE_FACT_REVIEW_STALE' in result.authority_error


def test_model_bound_raw_fallback_keeps_hidden_source_material_in_shared_key(tmp_path):
    kwargs, call, calls = _completed_fact_fixture(tmp_path)
    del call.provider_runtime_binding
    first = review_and_repair_source_facts(**kwargs)
    assert first['reason_code'] == 'SOURCE_FACT_TITLE_AUTHORITY_REQUIRED'
    assert review_and_repair_source_facts(**kwargs) == first and len(calls) == 1
    kwargs['story_contract']['source_media_sha256s'] = ['sha256:' + 'b' * 64]
    second = review_and_repair_source_facts(**kwargs)
    assert second['reason_code'] == 'SOURCE_FACT_TITLE_AUTHORITY_REQUIRED' and len(calls) == 2
    assert calls[0] == calls[1]  # Hidden source identity is not a prompt rewrite.


def test_repaired_title_enters_a_new_run_without_review_round_noise(tmp_path):
    kwargs, original, calls = _completed_fact_fixture(tmp_path)
    kwargs['title_repair_allowed'] = True
    repaired_title = '【李豆沙】弹幕提议把技能叫李姐拉拉，主播随即拒绝'
    def call(prompt):
        if not calls:
            return original(prompt)
        calls.append(prompt)
        return _completion(status='KEEP', final_hook=kwargs['selection_hook'],
            final_title=repaired_title, supported_by=['final_transcript'])
    call.__dict__.update(original.__dict__)
    kwargs['llm_call'] = call
    first = review_and_repair_source_facts(**kwargs)
    assert first['status'] == 'PASS' and first['decision'] == 'REPAIRED' and len(calls) == 2
    kwargs['title'] = repaired_title
    resumed = review_and_repair_source_facts(**kwargs)
    assert resumed['status'] == 'PASS' and resumed['decision'] == 'KEEP' and len(calls) == 2
    assert first['passes'][1]['response_sha256'] == resumed['passes'][0]['response_sha256']
    assert first['passes'][1]['review_pass'] == 2 and resumed['passes'][0]['review_pass'] == 1
    assert all('review_pass:' not in prompt for prompt in calls)


@pytest.mark.parametrize("change", ["srt", "title", "hook", "context", "scorecard", "source", "model", "provider", "speaker"])
def test_completed_source_fact_response_reuses_title_block_and_invalidates_inputs(tmp_path, change):
    kwargs, call, calls = _completed_fact_fixture(tmp_path)
    first = review_and_repair_source_facts(**kwargs)
    root = tmp_path / ".completed-review-observations"
    original = {p: p.read_bytes() for p in root.rglob("*.json")}
    assert first["reason_code"] == "SOURCE_FACT_TITLE_AUTHORITY_REQUIRED" and original
    assert review_and_repair_source_facts(**kwargs) == first and len(calls) == 1
    changed = deepcopy(kwargs)
    changed["llm_call"] = call
    if change == "srt":
        path = changed["final_reviewed_srt_path"]
        path.write_text(path.read_text().replace("00:00:01,000", "00:00:01,500"))
    elif change == "title":
        changed["title"] = "【李豆沙】短标题变了"
        call.test_surfaces[1] = changed["title"]
    elif change == "hook":
        changed["selection_hook"] = "弹幕建议把技能叫李姐拉拉。"
        call.test_surfaces[0] = changed["selection_hook"]
    elif change == "context":
        changed["clip_context_prompt"] = "当前片内语境变化。"
    elif change == "scorecard":
        changed["selection_scorecard"] = {"status": "VALID", "dimensions": {"self_contained": 4}}
    elif change == "source":
        changed["story_contract"]["source_media_sha256s"] = ["sha256:" + "b" * 64]
    elif change == "model":
        call.cpa_cache_identity["models"] = ["another-fixture-model"]
    elif change == "provider":
        call.provider_runtime_binding["provider_endpoint_host"] = "another.invalid"
    else:
        changed["speaker_evidence"] = _absent_speaker_evidence()
    second = review_and_repair_source_facts(**changed)
    assert second["reason_code"] == "SOURCE_FACT_TITLE_AUTHORITY_REQUIRED" and len(calls) == 2
    assert review_and_repair_source_facts(**changed) == second and len(calls) == 2
    assert all(path.read_bytes() == raw for path, raw in original.items())


def test_completed_source_fact_response_damaged_observation_is_not_provider_retry(tmp_path):
    kwargs, call, calls = _completed_fact_fixture(tmp_path)
    review_and_repair_source_facts(**kwargs)
    saved = next((tmp_path / ".completed-review-observations").rglob("*.json"))
    saved.write_bytes(saved.read_bytes() + b" corrupted")
    damaged = saved.read_bytes()
    review = review_and_repair_source_facts(**kwargs)
    assert review["status"] == "FAILED" and review["reason_code"] == "COMPLETED_REVIEW_BYTES_DRIFT"
    assert len(calls) == 1 and saved.read_bytes() == damaged


@pytest.mark.parametrize("failure", ["transport", "incomplete"])
def test_completed_source_fact_response_retries_failure_without_caching_it(tmp_path, failure):
    kwargs, original, calls = _completed_fact_fixture(tmp_path)

    def call(prompt):
        if not calls:
            calls.append(prompt)
            if failure == "transport":
                raise RuntimeError("offline transport failed")
            return '{"status":"REPAIR"}'
        return original(prompt)

    call.__dict__.update(original.__dict__)
    kwargs["llm_call"] = call
    first = review_and_repair_source_facts(**kwargs)
    if failure == 'incomplete':
        assert first['reason_code'] == 'CPA_TEXT_REVIEW_INVALID'
        assert first['passes'][-1]['failure_layer'] == 'CONTRACT_ERROR'
        assert len(calls) == 1 and not first['provider_retries']
        saved = list((tmp_path / '.completed-review-observations').rglob('*.json'))
        original_bytes = {path: path.read_bytes() for path in saved}
        assert len(saved) == 1 and json.loads(saved[0].read_text())['response'] == '{"status":"REPAIR"}'
        resumed = review_and_repair_source_facts(**kwargs)
        assert resumed == first and len(calls) == 1
        assert all(path.read_bytes() == raw for path, raw in original_bytes.items())
        return
    if failure == "transport":
        assert first["reason_code"] == "CPA_TEXT_REVIEW_CALL_FAILED"
        assert not list((tmp_path / ".completed-review-observations").rglob("*.json"))
        first = review_and_repair_source_facts(**kwargs)
    assert first["reason_code"] == "SOURCE_FACT_TITLE_AUTHORITY_REQUIRED" and len(calls) == 2
    resumed = review_and_repair_source_facts(**kwargs)
    assert resumed["reason_code"] == first["reason_code"]
    assert resumed["passes"] == first["passes"] and not resumed["provider_retries"]
    assert len(calls) == 2


def test_completed_source_fact_response_keep_parser_pass_still_validates_current_srt(tmp_path):
    kwargs, original, calls = _completed_fact_fixture(tmp_path)
    kwargs["title"] = "【李豆沙】弹幕提议把技能叫李姐拉拉，主播随即拒绝"

    def call(prompt):
        calls.append(prompt)
        return _completion(status="KEEP", final_hook=kwargs["selection_hook"], final_title=kwargs["title"], supported_by=["final_transcript"])

    call.__dict__.update(original.__dict__)
    kwargs["llm_call"] = call
    first = review_and_repair_source_facts(**kwargs)
    assert first["status"] == "PASS"
    assert review_and_repair_source_facts(**kwargs) == first and len(calls) == 1


def test_game_relationship_title_repair_preserves_context_in_final_surface() -> None:
    hook = "游戏对白里闺蜜问别先结婚，小李连声说太暧昧了。"
    old = "【李豆沙】闺蜜问别先结婚，上帝视角的小李连声说太暧昧了"
    corrected = "【李豆沙】游戏里闺蜜问别先结婚，小李连声说太暧昧了"
    transcript = "这应该是Galgame吧\n你不会抛下我先跑去结婚的对吧\n有点太暧昧了"
    calls = []

    def cpa(prompt: str) -> str:
        calls.append(prompt)
        assert "投稿标题必须脱离 selection_hook" in prompt
        assert "钩子已写游戏、标题没直接说是主播本人" in prompt
        assert "现实经历同样不得凭空改成游戏剧情" in prompt
        if len(calls) == 1:
            return _completion(
                status="REPAIR", final_hook=hook, final_title=corrected,
                supported_by=["final_transcript"],
                changed_surfaces=[{
                    "artifact": "title", "before": old, "after": corrected,
                    "reason": "标题独立展示时缺少游戏语境，会上升为现实闺蜜关系。",
                    "evidence": ["final_transcript: 这应该是Galgame吧"],
                }],
            )
        return _completion(status="KEEP", final_hook=hook, final_title=corrected,
                           supported_by=["final_transcript"])

    review = review_and_repair_source_facts(
        selection_hook=hook, title=old, final_transcript=transcript,
        clip_context_prompt="", llm_call=cpa,
    )
    assert review["decision"] == "REPAIRED"
    assert len(calls) == 2
    assert validate_source_fact_review(
        review, selection_hook=hook, title=corrected,
        final_transcript=transcript, clip_context_prompt="",
    )
    assert not validate_source_fact_review(
        review, selection_hook=hook, title=old,
        final_transcript=transcript, clip_context_prompt="",
    )


def test_adjacent_structured_chat_supports_beidian_keep() -> None:
    hook = "刚用“喜欢点兔”解释自己为什么被电，小李就连声撤回。"
    title = "【李豆沙】刚用“喜欢点兔”解释自己为什么被电，小李就连声撤回"
    context = "\n".join(
        [
            "- danmaku @1576ms: 小李你怎么被点了！",
            "- danmaku @4115ms: 电",
            "- danmaku @35314ms: 这个雷可以劈小k",
            "- danmaku @43401ms: 大大方方电",
        ]
    )

    def cpa(prompt: str) -> str:
        assert "小李你怎么被点了！" in prompt
        assert "danmaku @4115ms: 电" in prompt
        assert "这个雷可以劈小k" in prompt
        assert hook in prompt and title in prompt
        return _completion(
            status="KEEP",
            final_hook=hook,
            final_title=title,
            supported_by=["final_transcript", "structured_chat"],
        )

    review = review_and_repair_source_facts(
        selection_hook=hook,
        title=title,
        final_transcript="小李你怎么被点了\n可能因为我喜欢看点兔吧",
        clip_context_prompt=context,
        llm_call=cpa,
    )

    assert source_fact_review_passes(review)
    assert review["decision"] == "KEEP"
    assert review["final_selection_hook"] == hook
    assert review["final_title"] == title
    assert len(review["passes"]) == 1
    assert "entity_context" not in review
    assert "entity_context_sha256" not in review["passes"][0]


def test_source_fact_prompt_forbids_birthday_forwarding_role_drift() -> None:
    hook = "SC说转发佐伯沙弥香生日能拿菲尔兹奖。"
    title = "【李豆沙】SC说转发佐伯沙弥香生日能拿菲尔兹奖"

    def cpa(prompt: str) -> str:
        assert "不得概括为“转发某人的生日能得奖”" in prompt
        assert "必须保留“消息/信息”作为转发宾语" in prompt
        return _completion(
            status="KEEP",
            final_hook=hook,
            final_title=title,
            supported_by=["final_transcript"],
        )

    review_and_repair_source_facts(
        selection_hook=hook,
        title=title,
        final_transcript="今天是她的生日\n转发这条信息能得奖",
        clip_context_prompt="",
        llm_call=cpa,
    )


def test_relation_policy_changes_request_hash_but_not_context_binding() -> None:
    hook = "李豆沙聊起南町的直播趣事。"
    title = "【李豆沙】聊起南町的直播趣事"
    transcript = "李豆沙聊起南町的直播趣事"
    context = "- danmaku @1000ms event=chat: 南町"
    prompts: list[str] = []

    def cpa(prompt: str) -> str:
        prompts.append(prompt)
        return _completion(
            status="KEEP",
            final_hook=hook,
            final_title=title,
            supported_by=["final_transcript"],
        )

    legacy = review_and_repair_source_facts(
        selection_hook=hook,
        title=title,
        final_transcript=transcript,
        clip_context_prompt=context,
        llm_call=cpa,
    )
    restricted = review_and_repair_source_facts(
        selection_hook=hook,
        title=title,
        final_transcript=transcript,
        clip_context_prompt=context,
        llm_call=cpa,
        story_contract={"relation_state": "UNKNOWN", "relation_claim_allowed": False},
    )

    assert source_fact_review_passes(legacy)
    assert source_fact_review_passes(restricted)
    assert "关系缺失或未确认" not in prompts[0]
    assert "关系缺失或未确认" in prompts[1]
    assert restricted["passes"][0]["request_sha256"] == (
        "sha256:" + hashlib.sha256(prompts[1].encode("utf-8")).hexdigest()
    )
    assert restricted["passes"][0]["request_sha256"] != legacy["passes"][0]["request_sha256"]
    assert restricted["passes"][0]["clip_context_prompt_sha256"] == (
        legacy["passes"][0]["clip_context_prompt_sha256"]
    )


def test_unconfirmed_relation_repair_converges_to_neutral_public_copy() -> None:
    bad_hook = "李豆沙和南町联动聊天。"
    bad_title = "【李豆沙】和南町联动聊天"
    fixed_hook = "李豆沙聊起南町。"
    fixed_title = "【李豆沙】聊起南町的趣事"
    contract = build_story_contract(
        candidate_id="relation-neutral-repair",
        selection_hook=bad_hook,
        transcript_text="李豆沙聊起南町。",
        selection_scorecard=None,
        session_relation_authority=None,
    )
    responses = iter(
        [
            _completion(
                status="REPAIR",
                final_hook=fixed_hook,
                final_title=fixed_title,
                supported_by=["final_transcript"],
                changed_surfaces=[
                    {
                        "artifact": "selection_hook",
                        "before": "李豆沙和南町联动聊天",
                        "after": "李豆沙聊起南町",
                        "reason": "关系未确认，只保留字幕支持的具体聊天事实。",
                        "evidence": ["final_transcript: 李豆沙聊起南町。"],
                    },
                    {
                        "artifact": "title",
                        "before": "和南町联动聊天",
                        "after": "聊起南町",
                        "reason": "关系未确认，只保留字幕支持的具体聊天事实。",
                        "evidence": ["final_transcript: 李豆沙聊起南町。"],
                    },
                ],
            ),
            _completion(
                status="KEEP",
                final_hook=fixed_hook,
                final_title=fixed_title,
                supported_by=["final_transcript"],
            ),
        ]
    )
    review = review_and_repair_source_facts(
        selection_hook=bad_hook,
        title=bad_title,
        final_transcript="李豆沙聊起南町。",
        clip_context_prompt="",
        llm_call=lambda _prompt: next(responses),
        story_contract=contract,
    )

    assert source_fact_review_passes(review)
    assert review["decision"] == "REPAIRED"
    assert "联动" not in review["final_selection_hook"]
    assert "联动" not in review["final_title"]


def test_changed_surface_evidence_can_bind_exact_speaker_transcript_rows() -> None:
    row = {
        "artifact": "title",
        "before": "小李求莉亚放过自己",
        "after": "莉亚求小李放过自己",
        "reason": "说话人标签证明求饶者是连线一方。",
        "evidence": [
            "speaker_transcript: 5 [连线] 我想活着",
            "speaker_transcript: 7 [连线] 你放过我好吗",
        ],
    }
    speaker_transcript = "\n".join(
        [
            "4 [李豆沙] 莉亚，活着",
            "5 [连线] 我想活着",
            "7 [连线] 你放过我好吗",
        ]
    )

    assert _valid_changed_surface(
        row,
        before_surface="【李豆沙】小李求莉亚放过自己",
        after_surface="【李豆沙】莉亚求小李放过自己",
        final_transcript="莉亚，活着\n我想活着\n你放过我好吗",
        clip_context_prompt="",
        speaker_transcript=speaker_transcript,
    )

    assert not _valid_changed_surface(
        {**row, "evidence": ["speaker_transcript: 5 [李豆沙] 我想活着"]},
        before_surface="【李豆沙】小李求莉亚放过自己",
        after_surface="【李豆沙】莉亚求小李放过自己",
        final_transcript="莉亚，活着\n我想活着\n你放过我好吗",
        clip_context_prompt="",
        speaker_transcript=speaker_transcript,
    )


def test_uniform_host_final_transcript_citation_tolerates_invented_line_number() -> None:
    """8/17-8/19 生产实证（七夕/图书馆/8-09 candidate）：uniform_host 下没有

    带编号的 speaker_transcript 可以锚定，判官会在引用 final_transcript
    时习惯性带上自己编的行号（如 "final_transcript: 18 我周三就是七夕
    那天"），而 final_transcript 本身（build_addressee_evidence）逐字
    不带编号。这里必须剥掉一层判官自造的编号前缀再比对，但被引用的实际
    文字仍必须逐字命中 final_transcript——不放宽真正的证据门槛。
    """

    row = {
        "artifact": "selection_hook",
        "before": "李豆沙公布七夕安排：中午用甜歌把观众甜腻，晚上再用苦情歌唱到大家集体封号，完成一套七夕PUA。",
        "after": "李豆沙公布七夕安排：中午用甜歌把观众甜腻，晚上再用苦情歌唱到大家集体分号分号，完成一套七夕PUA。",
        "reason": "“封号”把“分号分号”的同音梗改成了无证据的账号封禁含义，应恢复为规范词面。",
        "evidence": [
            "final_transcript: 18 我周三就是七夕那天",
            "final_transcript: 19 我准备中午唱甜甜甜",
        ],
    }
    final_transcript = "\n".join(
        [
            "我周三就是七夕那天",
            "我准备中午唱甜甜甜",
        ]
    )

    assert _valid_changed_surface(
        row,
        before_surface=row["before"],
        after_surface=row["after"],
        final_transcript=final_transcript,
        clip_context_prompt="",
        speaker_transcript=None,
    )


def test_uniform_host_final_transcript_citation_tolerates_invented_srt_block() -> None:
    """8/19 生产实证（七夕 auto_113022_354_496）：判官不止编行号，还编了

    整块 SRT（序号 + 时间戳行 + 文字行）来引用 final_transcript。剥掉这层
    定位外壳后，剩余文字仍必须逐字命中 final_transcript——用生产实测原文
    做 fixture，防止未来再退化成这种回归。
    """

    final_transcript = "\n".join(
        [
            "然后后天是七夕",
            "我周三就是七夕那天",
            "我准备中午唱甜甜甜",
        ]
    )
    row = {
        "artifact": "selection_hook",
        "before": "李豆沙公布七夕安排：中午用甜歌把观众甜腻，晚上再用苦情歌唱到大家集体封号，完成一套七夕PUA。",
        "after": "李豆沙公布七夕安排：中午用甜歌把观众甜腻，晚上再用苦情歌唱到大家集体分号分号，完成一套七夕PUA。",
        "reason": "“封号”把“分号分号”的同音梗改成了无证据的账号封禁含义，应恢复为规范词面。",
        "evidence": [
            "final_transcript: 19 00:00:38,870 --> 00:00:45,000\n"
            "我周三就是七夕那天\n我准备中午唱甜甜甜",
            "final_transcript: 12 00:00:22,320 --> 00:00:24,250\n然后后天是七夕",
        ],
    }

    assert _valid_changed_surface(
        row,
        before_surface=row["before"],
        after_surface=row["after"],
        final_transcript=final_transcript,
        clip_context_prompt="",
        speaker_transcript=None,
    )


def test_srt_block_shell_without_index_still_binds() -> None:
    """时间戳行前没有序号也要能剥壳——判官不总是先编个序号。"""

    row = {
        "artifact": "selection_hook",
        "before": "占位",
        "after": "占位改动",
        "reason": "占位理由",
        "evidence": [
            "final_transcript: 00:00:22,320 --> 00:00:24,250\n然后后天是七夕",
        ],
    }

    assert _valid_changed_surface(
        row,
        before_surface="占位",
        after_surface="占位改动",
        final_transcript="然后后天是七夕",
        clip_context_prompt="",
        speaker_transcript=None,
    )


def test_srt_block_shell_cannot_rescue_an_unbound_quote() -> None:
    """负向金丝雀：壳剥干净了，但引用文字在 final_transcript 里根本不存在

    ——必须继续判不绑定，证明剥壳没有放宽真正的证据门槛。
    """

    row = {
        "artifact": "selection_hook",
        "before": "占位",
        "after": "占位改动",
        "reason": "占位理由",
        "evidence": [
            "final_transcript: 5 00:00:10,000 --> 00:00:12,000\n没说过的话",
        ],
    }

    assert not _valid_changed_surface(
        row,
        before_surface="占位",
        after_surface="占位改动",
        final_transcript="七夕唱甜歌",
        clip_context_prompt="",
        speaker_transcript=None,
    )


def test_invented_line_number_cannot_rescue_an_unbound_quote() -> None:
    """剥前缀只豁免编号本身；被引用的实际文字仍必须逐字出现在

    final_transcript 里。判官编了号也编了字面就必须照旧拒绝。
    """

    row = {
        "artifact": "selection_hook",
        "before": "李豆沙提到七夕唱甜歌。",
        "after": "李豆沙提到七夕唱情歌大合唱。",
        "reason": "编号剥离不能凭空造出没说过的话。",
        "evidence": ["final_transcript: 3 七夕唱情歌大合唱"],
    }

    assert not _valid_changed_surface(
        row,
        before_surface=row["before"],
        after_surface=row["after"],
        final_transcript="七夕唱甜歌",
        clip_context_prompt="",
        speaker_transcript=None,
    )


def test_exact_unnumbered_final_transcript_quote_still_binds_first() -> None:
    """回归：既有的逐字未编号引用（auto/required 模式的历史形状）必须

    继续原样通过，不受新剥前缀分支影响。
    """

    row = {
        "artifact": "title",
        "before": "熊猫头发明“李豆沙型侄女”",
        "after": "李豆沙聊起“李豆沙型侄女”",
        "reason": "字幕逐字支持李豆沙本人聊起该称谓。",
        "evidence": ["final_transcript: 有点像那个李豆沙型侄女"],
    }

    assert _valid_changed_surface(
        row,
        before_surface=row["before"],
        after_surface=row["after"],
        final_transcript="有点像那个李豆沙型侄女",
        clip_context_prompt="",
        speaker_transcript=None,
    )


def test_uniform_host_repair_with_invented_line_numbers_converges_end_to_end() -> None:
    """端到端复现：uniform_host（speaker_transcript=None）+ 判官编号引用

    final_transcript 必须能收敛到 REPAIRED，而不是卡死在
    CPA_TEXT_REVIEW_INVALID（8/17-8/19 生产阻塞的最小复现）。
    """

    bad_hook = "李豆沙公布七夕安排：中午用甜歌把观众甜腻，晚上再用苦情歌唱到大家集体封号。"
    fixed_hook = "李豆沙公布七夕安排：中午用甜歌把观众甜腻，晚上再用苦情歌唱到大家集体分号分号。"
    bad_title = "【李豆沙】公布七夕安排：中午甜甜甜把人甜腻，晚上苦苦苦让大家封号封号"
    fixed_title = "【李豆沙】公布七夕安排：中午甜甜甜把人甜腻，晚上苦苦苦让大家分号分号"
    final_transcript = "\n".join(
        [
            "我周三就是七夕那天",
            "我准备中午唱甜甜甜",
            "晚上唱苦苦苦",
            "就是所有人都必须分号分号的这种",
        ]
    )
    responses = iter(
        [
            _completion(
                status="REPAIR",
                final_hook=fixed_hook,
                final_title=fixed_title,
                supported_by=["final_transcript"],
                changed_surfaces=[
                    {
                        "artifact": "selection_hook",
                        "before": bad_hook,
                        "after": fixed_hook,
                        "reason": "封号是无据的同音升级，恢复为字幕原词分号分号。",
                        "evidence": [
                            "final_transcript: 18 我周三就是七夕那天",
                            "final_transcript: 21 就是所有人都必须分号分号的这种",
                        ],
                    },
                    {
                        "artifact": "title",
                        "before": bad_title,
                        "after": fixed_title,
                        "reason": "标题同一处同音升级同步回正。",
                        "evidence": [
                            "final_transcript: 21 就是所有人都必须分号分号的这种",
                        ],
                    },
                ],
                addressee_attribution=[],
            ),
            _completion(
                status="KEEP",
                final_hook=fixed_hook,
                final_title=fixed_title,
                supported_by=["final_transcript"],
            ),
        ]
    )

    review = review_and_repair_source_facts(
        selection_hook=bad_hook,
        title=bad_title,
        final_transcript=final_transcript,
        clip_context_prompt="",
        speaker_transcript=None,
        speaker_evidence=_absent_speaker_evidence(),
        llm_call=lambda _prompt: next(responses),
    )

    assert source_fact_review_passes(review)
    assert review["decision"] == "REPAIRED"
    assert review["final_selection_hook"] == fixed_hook
    assert review["final_title"] == fixed_title


def test_title_policy_violation_must_be_repaired_before_keep() -> None:
    hook = (
        "SC说今天是佐伯沙弥香的生日，转发这条信息能拿菲尔兹奖，李豆沙追问自己也磕原点组怎么没有。"
    )
    long_title = (
        "【李豆沙】SC说今天是佐伯沙弥香的生日，转发这条信息能拿菲尔兹奖，"
        "百合专家小李：我也磕了怎么没有，是不是磕错了？"
    )
    fixed_title = "【李豆沙】SC说转发生日信息能拿菲尔兹奖，小李追问：我也磕原点组怎么没有"
    calls: list[str] = []

    def cpa(prompt: str) -> str:
        calls.append(prompt)
        if len(calls) == 1:
            assert "publish_title_length_out_of_bounds" in prompt
            return _completion(
                status="REPAIR",
                final_hook=hook,
                final_title=fixed_title,
                supported_by=["final_transcript"],
                changed_surfaces=[
                    {
                        "artifact": "title",
                        "before": long_title.removeprefix("【李豆沙】"),
                        "after": fixed_title.removeprefix("【李豆沙】"),
                        "reason": "压缩标题但保留获奖条件和追问。",
                        "evidence": [
                            "转发这条信息你就会获得菲尔兹奖",
                            "老师，我也磕了，我怎么没有",
                        ],
                    }
                ],
            )
        assert "deterministic_title_policy_violations: []" in prompt
        return _completion(
            status="KEEP",
            final_hook=hook,
            final_title=fixed_title,
            supported_by=["final_transcript"],
        )

    review = review_and_repair_source_facts(
        selection_hook=hook,
        title=long_title,
        final_transcript=(
            "今天是佐伯沙弥香的生日\n转发这条信息你就会获得菲尔兹奖\n老师，我也磕了，我怎么没有"
        ),
        clip_context_prompt="",
        llm_call=cpa,
        enforce_automatic_title_style=True,
    )

    assert source_fact_review_passes(review)
    assert review["decision"] == "REPAIRED"
    assert review["final_title"] == fixed_title
    assert len(review["passes"]) == 2


def test_source_fact_review_repairs_unjoined_real_quotes_without_banning_natural_quotes() -> None:
    hook = "玩自己的语音"
    bad_title = "【李豆沙】玩自己的语音，怎么“我讨厌你”还“不许讨厌我”？"
    fixed_title = "【李豆沙】玩自己的语音：我讨厌你，但你不许讨厌我"
    transcript = "我讨厌你\n不许讨厌我"
    calls: list[str] = []

    def cpa(prompt: str) -> str:
        calls.append(prompt)
        assert "事实支持不等于标题通顺" in prompt
        assert "不一律禁用引号、问号或口语省略" in prompt
        if len(calls) == 1:
            return _completion(
                status="REPAIR",
                final_hook=hook,
                final_title=fixed_title,
                supported_by=["final_transcript"],
                changed_surfaces=[
                    {
                        "artifact": "title",
                        "before": bad_title,
                        "after": fixed_title,
                        "reason": "两句原话真实，但原标题把它们并排成了需要读者自行拼接的句法。",
                        "evidence": ["我讨厌你", "不许讨厌我"],
                    }
                ],
            )
        return _completion(
            status="KEEP",
            final_hook=hook,
            final_title=fixed_title,
            supported_by=["final_transcript"],
        )

    review = review_and_repair_source_facts(
        selection_hook=hook,
        title=bad_title,
        final_transcript=transcript,
        clip_context_prompt="",
        llm_call=cpa,
        enforce_automatic_title_style=True,
    )

    assert review["decision"] == "REPAIRED"
    assert review["final_title"] == fixed_title
    assert len(calls) == 2


def test_source_fact_review_scopes_fluency_gate_to_automatic_title_style() -> None:
    hook = "玩自己的语音"
    title = "【李豆沙】玩自己的语音，怎么“我讨厌你”还“不许讨厌我”？"
    prompts: list[str] = []

    def cpa(prompt: str) -> str:
        prompts.append(prompt)
        return _completion(
            status="KEEP",
            final_hook=hook,
            final_title=title,
            supported_by=["final_transcript"],
        )

    review = review_and_repair_source_facts(
        selection_hook=hook,
        title=title,
        final_transcript="我讨厌你\n不许讨厌我",
        clip_context_prompt="",
        llm_call=cpa,
        enforce_automatic_title_style=False,
    )

    assert source_fact_review_passes(review)
    assert review["decision"] == "KEEP"
    assert review["passes"][0]["title_policy_mode"] == "baseline"
    assert len(prompts) == 1
    assert "自动文案审查的通顺要求" not in prompts[0]
    assert "当前是 baseline/manual 文案审查" in prompts[0]
    assert "事实、模态、角色、条件和因果" in prompts[0]


def test_proposal_must_not_become_completed_naming() -> None:
    bad_hook = "弹幕又把她的技能强行命名成“李姐拉拉”。"
    bad_title = "【李豆沙】话还没说完，技能又被命名成“李姐拉拉”"
    fixed_hook = "弹幕提议把技能叫“李姐拉拉”，她随即拒绝。"
    fixed_title = "【李豆沙】话还没说完，弹幕又提议把技能叫“李姐拉拉”"
    calls: list[str] = []

    def cpa(prompt: str) -> str:
        calls.append(prompt)
        if len(calls) == 1:
            return _completion(
                status="REPAIR",
                final_hook=fixed_hook,
                final_title=fixed_title,
                supported_by=["final_transcript", "structured_chat"],
                changed_surfaces=[
                    {
                        "artifact": "selection_hook",
                        "before": "强行命名成",
                        "after": "提议把技能叫",
                        "reason": "SC 是提议，主播随后拒绝。",
                        "evidence": [
                            "技能可以叫李姐拉拉吗",
                            "我这个应该叫李姐网",
                        ],
                    },
                    {
                        "artifact": "title",
                        "before": "技能又被命名成",
                        "after": "弹幕又提议把技能叫",
                        "reason": "保留提议而非既成事实的模态。",
                        "evidence": ["技能可以叫李姐拉拉吗"],
                    },
                ],
            )
        return _completion(
            status="KEEP",
            final_hook=fixed_hook,
            final_title=fixed_title,
            supported_by=["final_transcript", "structured_chat"],
        )

    review = review_and_repair_source_facts(
        selection_hook=bad_hook,
        title=bad_title,
        final_transcript="不不不\n我这个应该叫李姐网\n不要不要",
        clip_context_prompt="- superchat: 技能可以叫李姐拉拉吗",
        llm_call=cpa,
    )

    assert source_fact_review_passes(review)
    assert review["decision"] == "REPAIRED"
    assert review["final_selection_hook"] == fixed_hook
    assert review["final_title"] == fixed_title
    assert len(review["passes"]) == 2
    assert review['passes'][1]['review_pass'] == 2
    assert all('review_pass:' not in prompt for prompt in calls)


def test_repair_accepts_source_labels_and_bound_chat_citation_format() -> None:
    bad_hook = "熊猫头发明“李豆沙型侄女”。"
    bad_title = "【李豆沙】熊猫头发明“李豆沙型侄女”"
    fixed_hook = "李豆沙聊起“李豆沙型侄女”。"
    fixed_title = "【李豆沙】李豆沙聊起“李豆沙型侄女”"
    responses = iter(
        [
            _completion(
                status="REPAIR",
                final_hook=fixed_hook,
                final_title=fixed_title,
                supported_by=["final_transcript", "structured_chat"],
                changed_surfaces=[
                    {
                        "artifact": "selection_hook",
                        "before": "熊猫头发明",
                        "after": "李豆沙聊起",
                        "reason": "弹幕先提出该称谓，字幕只支持李豆沙聊起。",
                        "evidence": [
                            "danmaku @29810ms: 这是李豆沙型侄女",
                            "final_transcript: 有点像那个李豆沙型侄女",
                        ],
                    },
                    {
                        "artifact": "title",
                        "before": "熊猫头发明",
                        "after": "李豆沙聊起",
                        "reason": "标题删除没有来源支持的身份与发明归属。",
                        "evidence": [
                            "danmaku @29810ms: 这是李豆沙型侄女",
                            "final_transcript: 有点像那个李豆沙型侄女",
                        ],
                    },
                ],
            ),
            _completion(
                status="KEEP",
                final_hook=fixed_hook,
                final_title=fixed_title,
                supported_by=["final_transcript", "structured_chat"],
            ),
        ]
    )

    review = review_and_repair_source_facts(
        selection_hook=bad_hook,
        title=bad_title,
        final_transcript="有点像那个李豆沙型侄女",
        clip_context_prompt=("- danmaku @29810ms event=: 这是李豆沙型侄女"),
        llm_call=lambda _prompt: next(responses),
    )

    assert source_fact_review_passes(review)
    assert review["decision"] == "REPAIRED"
    assert review["final_selection_hook"] == fixed_hook
    assert review["final_title"] == fixed_title


def test_repair_rejects_chat_citation_with_unbound_offset() -> None:
    review = review_and_repair_source_facts(
        selection_hook="熊猫头发明“李豆沙型侄女”。",
        title="【李豆沙】熊猫头发明“李豆沙型侄女”",
        final_transcript="有点像那个李豆沙型侄女",
        clip_context_prompt=("- danmaku @29810ms event=: 这是李豆沙型侄女"),
        llm_call=lambda _prompt: _completion(
            status="REPAIR",
            final_hook="李豆沙聊起“李豆沙型侄女”。",
            final_title="【李豆沙】李豆沙聊起“李豆沙型侄女”",
            supported_by=["final_transcript", "structured_chat"],
            changed_surfaces=[
                {
                    "artifact": "selection_hook",
                    "before": "熊猫头发明",
                    "after": "李豆沙聊起",
                    "reason": "弹幕先提出该称谓。",
                    "evidence": [
                        "danmaku @99999ms: 这是李豆沙型侄女",
                    ],
                },
                {
                    "artifact": "title",
                    "before": "熊猫头发明",
                    "after": "李豆沙聊起",
                    "reason": "标题删除无来源支持的归属。",
                    "evidence": [
                        "danmaku @99999ms: 这是李豆沙型侄女",
                    ],
                },
            ],
        ),
    )

    assert not source_fact_review_passes(review)
    assert review["passes"][0]["reason_code"] == "CPA_TEXT_REVIEW_INVALID"


def test_repair_rejects_chat_citation_with_wrong_event_or_text() -> None:
    context = "- danmaku @29810ms event=evt-1: 这是李豆沙型侄女"
    for evidence in (
        "danmaku @29810ms event=evt-2: 这是李豆沙型侄女",
        "danmaku @29810ms event=evt-1: 这是熊猫头型侄女",
    ):
        review = review_and_repair_source_facts(
            selection_hook="熊猫头发明“李豆沙型侄女”。",
            title="【李豆沙】熊猫头发明“李豆沙型侄女”",
            final_transcript="有点像那个李豆沙型侄女",
            clip_context_prompt=context,
            llm_call=lambda _prompt, evidence=evidence: _completion(
                status="REPAIR",
                final_hook="李豆沙聊起“李豆沙型侄女”。",
                final_title="【李豆沙】李豆沙聊起“李豆沙型侄女”",
                supported_by=["final_transcript", "structured_chat"],
                changed_surfaces=[
                    {
                        "artifact": "selection_hook",
                        "before": "熊猫头发明",
                        "after": "李豆沙聊起",
                        "reason": "弹幕先提出该称谓。",
                        "evidence": [evidence],
                    },
                    {
                        "artifact": "title",
                        "before": "熊猫头发明",
                        "after": "李豆沙聊起",
                        "reason": "标题删除无来源支持的归属。",
                        "evidence": [evidence],
                    },
                ],
            ),
        )

        assert not source_fact_review_passes(review)
        assert review["passes"][0]["reason_code"] == "CPA_TEXT_REVIEW_INVALID"


def test_repair_rejects_generic_context_label() -> None:
    review = review_and_repair_source_facts(
        selection_hook="熊猫头发明“李豆沙型侄女”。",
        title="【李豆沙】熊猫头发明“李豆沙型侄女”",
        final_transcript="有点像那个李豆沙型侄女",
        clip_context_prompt=("structured_chat: 这是李豆沙型侄女"),
        llm_call=lambda _prompt: _completion(
            status="REPAIR",
            final_hook="李豆沙聊起“李豆沙型侄女”。",
            final_title="【李豆沙】李豆沙聊起“李豆沙型侄女”",
            supported_by=["structured_chat"],
            changed_surfaces=[
                {
                    "artifact": "selection_hook",
                    "before": "熊猫头发明",
                    "after": "李豆沙聊起",
                    "reason": "弹幕先提出该称谓。",
                    "evidence": ["structured_chat: 这是李豆沙型侄女"],
                },
                {
                    "artifact": "title",
                    "before": "熊猫头发明",
                    "after": "李豆沙聊起",
                    "reason": "标题删除无来源支持的归属。",
                    "evidence": ["structured_chat: 这是李豆沙型侄女"],
                },
            ],
        ),
    )

    assert not source_fact_review_passes(review)
    assert review["passes"][0]["reason_code"] == "CPA_TEXT_REVIEW_INVALID"


def test_live_shaped_chat_repair_requires_canonical_rows_and_can_converge() -> None:
    bad_hook = "李豆沙正面断言弹幕已经挂了。"
    bad_title = "【李豆沙】正面断言弹幕已经挂了"
    fixed_hook = "李豆沙先看到“正面…”，后来问弹幕怎么挂了。"
    fixed_title = "【李豆沙】先看到“正面…”，后来问弹幕怎么挂了"
    context = "\n".join(
        [
            "- danmaku @3719ms event=: 正面...",
            "- danmaku @25939ms event=: 弹幕怎么挂了",
        ]
    )

    def repair_with(evidence: list[str]) -> str:
        return _completion(
            status="REPAIR",
            final_hook=fixed_hook,
            final_title=fixed_title,
            supported_by=["structured_chat", "same_clip_context"],
            changed_surfaces=[
                {
                    "artifact": "selection_hook",
                    "before": "正面断言弹幕已经挂了",
                    "after": "先看到“正面…”，后来问弹幕怎么挂了",
                    "reason": "两条弹幕只支持先后出现的文字，不支持主播作出断言。",
                    "evidence": evidence,
                },
                {
                    "artifact": "title",
                    "before": "正面断言弹幕已经挂了",
                    "after": "先看到“正面…”，后来问弹幕怎么挂了",
                    "reason": "标题改回两条同片弹幕逐字支持的事实模态。",
                    "evidence": evidence,
                },
            ],
        )

    generic = review_and_repair_source_facts(
        selection_hook=bad_hook,
        title=bad_title,
        final_transcript="正面\n弹幕怎么挂了",
        clip_context_prompt=context,
        llm_call=lambda _prompt: repair_with(
            [
                "structured_chat: 弹幕怎么挂了",
                "same_clip_context: 正面...",
            ]
        ),
    )

    assert not source_fact_review_passes(generic)
    assert generic["passes"][0]["reason_code"] == "CPA_TEXT_REVIEW_INVALID"

    responses = iter(
        [
            repair_with(
                [
                    "danmaku @25939ms event=: 弹幕怎么挂了",
                    "danmaku @3719ms event=: 正面...",
                ]
            ),
            _completion(
                status="KEEP",
                final_hook=fixed_hook,
                final_title=fixed_title,
                supported_by=["structured_chat", "same_clip_context"],
            ),
        ]
    )

    def cpa(prompt: str) -> str:
        assert "structured_chat、same_clip_context 只可出现在 supported_by" in prompt
        assert "逐字复制完整 danmaku|superchat @<offset>ms event=<id>: 行" in prompt
        assert "禁止使用 structured_chat: 或 same_clip_context: 泛化标签" in prompt
        return next(responses)

    canonical = review_and_repair_source_facts(
        selection_hook=bad_hook,
        title=bad_title,
        final_transcript="正面\n弹幕怎么挂了",
        clip_context_prompt=context,
        llm_call=cpa,
    )

    assert source_fact_review_passes(canonical)
    assert canonical["decision"] == "REPAIRED"
    assert canonical["final_selection_hook"] == fixed_hook
    assert canonical["final_title"] == fixed_title


def test_repair_rejects_source_label_bound_only_in_other_corpus() -> None:
    review = review_and_repair_source_facts(
        selection_hook="熊猫头发明“李豆沙型侄女”。",
        title="【李豆沙】熊猫头发明“李豆沙型侄女”",
        final_transcript="李豆沙聊起这个称呼",
        clip_context_prompt=("- danmaku @29810ms event=: final_transcript: 这是李豆沙型侄女"),
        llm_call=lambda _prompt: _completion(
            status="REPAIR",
            final_hook="李豆沙聊起“李豆沙型侄女”。",
            final_title="【李豆沙】李豆沙聊起“李豆沙型侄女”",
            supported_by=["final_transcript", "structured_chat"],
            changed_surfaces=[
                {
                    "artifact": "selection_hook",
                    "before": "熊猫头发明",
                    "after": "李豆沙聊起",
                    "reason": "弹幕先提出该称谓。",
                    "evidence": [
                        "final_transcript: 这是李豆沙型侄女",
                    ],
                },
                {
                    "artifact": "title",
                    "before": "熊猫头发明",
                    "after": "李豆沙聊起",
                    "reason": "标题删除无来源支持的归属。",
                    "evidence": [
                        "final_transcript: 这是李豆沙型侄女",
                    ],
                },
            ],
        ),
    )

    assert not source_fact_review_passes(review)
    assert review["passes"][0]["reason_code"] == "CPA_TEXT_REVIEW_INVALID"


def test_provider_failure_fails_closed_without_same_pass_retry() -> None:
    calls = 0

    def cpa(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        raise RuntimeError("provider unavailable")

    review = review_and_repair_source_facts(
        selection_hook="测试钩子",
        title="【李豆沙】测试标题",
        final_transcript="测试字幕",
        clip_context_prompt="",
        llm_call=cpa,
    )

    assert not source_fact_review_passes(review)
    assert review["status"] == "FAILED"
    assert review["reason_code"] == "CPA_TEXT_REVIEW_CALL_FAILED"
    assert calls == 1
    assert review["provider_retries"] == []
    assert len(review["passes"]) == 1
    assert "response_sha256" not in review["passes"][0]


@pytest.mark.parametrize(
    ("error", "provider_class", "status_codes", "safe_reason"),
    [
        (
            LlmCallError(
                "provider http=429 token=source-fact-secret",
                safe_reason="LLM_COMMAND_FAILED",
            ),
            "quota",
            [429],
            "LLM_COMMAND_FAILED",
        ),
        (
            LlmCallError(
                "provider http=503 token=source-fact-secret",
                safe_reason="LLM_COMMAND_FAILED",
            ),
            "service",
            [503],
            "LLM_COMMAND_FAILED",
        ),
        (
            LlmCallError(
                "provider http=401 token=source-fact-secret",
                safe_reason="LLM_COMMAND_FAILED",
            ),
            "rejected",
            [401],
            "LLM_COMMAND_FAILED",
        ),
        (
            LlmCallError(
                "provider timed out token=source-fact-secret",
                safe_reason="LLM_COMMAND_TIMEOUT",
            ),
            "service",
            [],
            "LLM_COMMAND_TIMEOUT",
        ),
        (
            _MalformedSafeReasonError("provider exploded token=source-fact-secret"),
            "unknown",
            [],
            None,
        ),
    ],
)
def test_provider_failure_keeps_closed_diagnostics_without_secret(
    error: Exception,
    provider_class: str,
    status_codes: list[int],
    safe_reason: str | None,
) -> None:
    def cpa(_prompt: str) -> str:
        raise error

    review = review_and_repair_source_facts(
        selection_hook="测试钩子",
        title="【李豆沙】测试标题",
        final_transcript="测试字幕",
        clip_context_prompt="",
        llm_call=cpa,
    )

    failed_pass = review["passes"][0]
    assert failed_pass["status"] == "FAILED"
    assert failed_pass["reason_code"] == "CPA_TEXT_REVIEW_CALL_FAILED"
    assert failed_pass["provider_class"] == provider_class
    assert failed_pass["provider_status_codes"] == status_codes
    if safe_reason is None:
        assert "safe_reason" not in failed_pass
    else:
        assert failed_pass["safe_reason"] == safe_reason
    serialized = json.dumps(review, ensure_ascii=False)
    assert "source-fact-secret" not in serialized
    assert "token=" not in serialized
    assert "raw-secret-safe-reason" not in serialized


def test_multiple_repairs_converge_before_bounded_keep() -> None:
    responses = iter(
        [
            _completion(
                status="REPAIR",
                final_hook="小李当场被点名了。",
                final_title="【李豆沙】小李当场被点名了",
                supported_by=["final_transcript"],
                changed_surfaces=[
                    {
                        "artifact": "selection_hook",
                        "before": "当场被电了",
                        "after": "当场被点名了",
                        "reason": "按最终字幕修复。",
                        "evidence": ["小李你怎么被点了"],
                    },
                    {
                        "artifact": "title",
                        "before": "小李当场被电了",
                        "after": "小李当场被点名了",
                        "reason": "标题与修复后的钩子保持同一事实。",
                        "evidence": ["小李你怎么被点了"],
                    },
                ],
            ),
            _completion(
                status="REPAIR",
                final_hook="小李当场被叫到了。",
                final_title="【李豆沙】小李当场被叫到了",
                supported_by=["final_transcript"],
                changed_surfaces=[
                    {
                        "artifact": "selection_hook",
                        "before": "被点名了",
                        "after": "被叫到了",
                        "reason": "第二次仍要求改写。",
                        "evidence": ["小李你怎么被点了"],
                    },
                    {
                        "artifact": "title",
                        "before": "小李当场被点名了",
                        "after": "小李当场被叫到了",
                        "reason": "标题与二次修复后的钩子保持一致。",
                        "evidence": ["小李你怎么被点了"],
                    },
                ],
            ),
            _completion(
                status="KEEP",
                final_hook="小李当场被叫到了。",
                final_title="【李豆沙】小李当场被叫到了",
                supported_by=["final_transcript"],
            ),
        ]
    )
    review = review_and_repair_source_facts(
        selection_hook="小李当场被电了。",
        title="【李豆沙】小李当场被电了",
        final_transcript="小李你怎么被点了",
        clip_context_prompt="",
        llm_call=lambda _prompt: next(responses),
    )

    assert source_fact_review_passes(review)
    assert review["decision"] == "REPAIRED"
    assert review["final_selection_hook"] == "小李当场被叫到了。"
    assert review["final_title"] == "【李豆沙】小李当场被叫到了"
    assert len(review["passes"]) == 3
    assert validate_source_fact_review(
        review,
        selection_hook="小李当场被叫到了。",
        title="【李豆沙】小李当场被叫到了",
        final_transcript="小李你怎么被点了",
        clip_context_prompt="",
    )


def test_source_fact_repair_stops_after_five_nonconverging_passes() -> None:
    calls: list[str] = []

    def cpa(prompt: str) -> str:
        calls.append(prompt)
        index = len(calls) - 1
        before = f"版本{index}"
        after = f"版本{index + 1}"
        return _completion(
            status="REPAIR",
            final_hook=f"{after}。",
            final_title=f"【李豆沙】{after}",
            supported_by=["final_transcript"],
            changed_surfaces=[
                {
                    "artifact": "selection_hook",
                    "before": before,
                    "after": after,
                    "reason": "继续删除没有来源支持的事实。",
                    "evidence": ["来源原文"],
                },
                {
                    "artifact": "title",
                    "before": before,
                    "after": after,
                    "reason": "标题同步删除没有来源支持的事实。",
                    "evidence": ["来源原文"],
                },
            ],
        )

    review = review_and_repair_source_facts(
        selection_hook="版本0。",
        title="【李豆沙】版本0",
        final_transcript="来源原文",
        clip_context_prompt="",
        llm_call=cpa,
    )

    assert not source_fact_review_passes(review)
    assert review["decision"] == "REPAIR_EXHAUSTED"
    assert review["reason_code"] == "CPA_SOURCE_FACT_REPAIR_EXHAUSTED"
    assert len(review["passes"]) == 5
    assert review['passes'][-1]['review_pass'] == 5
    assert all('review_pass:' not in prompt for prompt in calls)


def test_repaired_hook_refuses_stale_selection_scorecard() -> None:
    calls = 0

    def cpa(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        return _completion(
            status="REPAIR",
            final_hook="弹幕提议把技能叫李姐拉拉，主播随即拒绝。",
            final_title="【李豆沙】弹幕提议把技能叫李姐拉拉，主播随即拒绝",
            supported_by=["final_transcript", "structured_chat"],
            changed_surfaces=[
                {
                    "artifact": "selection_hook",
                    "before": "技能已经被命名",
                    "after": "弹幕提议把技能叫",
                    "reason": "原文是提议而且主播拒绝。",
                    "evidence": ["技能可以叫李姐拉拉吗"],
                },
                {
                    "artifact": "title",
                    "before": "技能已经被命名成李姐拉拉",
                    "after": "弹幕提议把技能叫李姐拉拉，主播随即拒绝",
                    "reason": "标题也必须保留提议与拒绝的事实模态。",
                    "evidence": [
                        "技能可以叫李姐拉拉吗",
                        "不行，我这个应该叫李姐网",
                    ],
                },
            ],
            selection_scorecard_review={
                "status": "INCOMPATIBLE",
                "reason": "旧评分卡围绕已经完成的命名，核心模态已经改变。",
            },
        )

    review = review_and_repair_source_facts(
        selection_hook="技能已经被命名成李姐拉拉。",
        title="【李豆沙】技能已经被命名成李姐拉拉",
        final_transcript="不行，我这个应该叫李姐网",
        clip_context_prompt="- superchat: 技能可以叫李姐拉拉吗",
        selection_scorecard={
            "tier_basis": "audience_driven_performance",
            "tier_reason": "弹幕完成命名",
        },
        llm_call=cpa,
    )

    assert not source_fact_review_passes(review)
    assert review["reason_code"] == ("SOURCE_FACT_REPAIRED_HOOK_SCORECARD_STALE")
    assert calls == 1


def test_non_text_evidence_claim_is_invalid() -> None:
    review = review_and_repair_source_facts(
        selection_hook="小李被电。",
        title="【李豆沙】小李被电",
        final_transcript="小李你怎么被点了",
        clip_context_prompt="",
        llm_call=lambda _prompt: _completion(
            status="KEEP",
            final_hook="小李被电。",
            final_title="【李豆沙】小李被电",
            supported_by=["audio"],
        ),
    )

    assert not source_fact_review_passes(review)
    assert review["passes"][0]["reason_code"] == "CPA_TEXT_REVIEW_INVALID"


def test_persisted_receipt_rejects_bound_surface_tamper() -> None:
    hook = "小李被电后连声撤回。"
    title = "【李豆沙】小李被电后连声撤回"
    transcript = "小李你怎么被点了\n电"
    context = "- danmaku @100ms: 小李你怎么被点了\n- danmaku @200ms: 电"
    review = review_and_repair_source_facts(
        selection_hook=hook,
        title=title,
        final_transcript=transcript,
        clip_context_prompt=context,
        llm_call=lambda _prompt: _completion(
            status="KEEP",
            final_hook=hook,
            final_title=title,
            supported_by=["final_transcript", "structured_chat"],
        ),
    )

    assert validate_source_fact_review(
        review,
        selection_hook=hook,
        title=title,
        final_transcript=transcript,
        clip_context_prompt=context,
    )
    assert not validate_source_fact_review(
        review,
        selection_hook=hook,
        title=title + "！",
        final_transcript=transcript,
        clip_context_prompt=context,
    )
    assert not validate_source_fact_review(
        review,
        selection_hook=hook,
        title=title,
        final_transcript=transcript + "\n新增未审字幕",
        clip_context_prompt=context,
    )


def test_persisted_receipt_rebuilds_exact_speaker_evidence_binding() -> None:
    hook = "测试字幕形成完整事实。"
    title = "【李豆沙】测试字幕形成完整事实"
    final_transcript = "测试字幕"
    speaker_transcript = "1 [李豆沙] 测试字幕"
    evidence = _present_speaker_evidence(speaker_transcript)
    review = review_and_repair_source_facts(
        selection_hook=hook,
        title=title,
        final_transcript=final_transcript,
        speaker_transcript=speaker_transcript,
        speaker_evidence=evidence,
        clip_context_prompt="",
        llm_call=lambda _prompt: _completion(
            status="KEEP",
            final_hook=hook,
            final_title=title,
            supported_by=["final_transcript"],
        ),
    )

    assert source_fact_review_passes(review)
    assert review["speaker_evidence"] == evidence
    assert review["passes"][0]["speaker_evidence_sha256"] == review["speaker_evidence_sha256"]
    assert validate_source_fact_review(
        review,
        selection_hook=hook,
        title=title,
        final_transcript=final_transcript,
        clip_context_prompt="",
        speaker_evidence=evidence,
    )
    # New receipts may never be validated through the legacy unchecked mode.
    assert not validate_source_fact_review(
        review,
        selection_hook=hook,
        title=title,
        final_transcript=final_transcript,
        clip_context_prompt="",
    )


def test_persisted_receipt_rejects_rebuilt_speaker_evidence_mismatch() -> None:
    hook = "测试字幕形成完整事实。"
    title = "【李豆沙】测试字幕形成完整事实"
    speaker_transcript = "1 [李豆沙] 测试字幕"
    evidence = _present_speaker_evidence(speaker_transcript)
    review = review_and_repair_source_facts(
        selection_hook=hook,
        title=title,
        final_transcript="测试字幕",
        speaker_transcript=speaker_transcript,
        speaker_evidence=evidence,
        clip_context_prompt="",
        llm_call=lambda _prompt: _completion(
            status="KEEP",
            final_hook=hook,
            final_title=title,
            supported_by=["final_transcript"],
        ),
    )
    drifted = json.loads(json.dumps(evidence, ensure_ascii=False))
    drifted["speaker_final_srt_sha256"] = "sha256:" + "9" * 64

    assert not validate_source_fact_review(
        review,
        selection_hook=hook,
        title=title,
        final_transcript="测试字幕",
        clip_context_prompt="",
        speaker_evidence=drifted,
    )


def test_persisted_receipt_rejects_inconsistent_or_missing_pass_binding() -> None:
    hook = "测试字幕形成完整事实。"
    title = "【李豆沙】测试字幕形成完整事实"
    speaker_transcript = "1 [李豆沙] 测试字幕"
    evidence = _present_speaker_evidence(speaker_transcript)
    review = review_and_repair_source_facts(
        selection_hook=hook,
        title=title,
        final_transcript="测试字幕",
        speaker_transcript=speaker_transcript,
        speaker_evidence=evidence,
        clip_context_prompt="",
        llm_call=lambda _prompt: _completion(
            status="KEEP",
            final_hook=hook,
            final_title=title,
            supported_by=["final_transcript"],
        ),
    )
    inconsistent_body = json.loads(json.dumps(review, ensure_ascii=False))
    inconsistent_body.pop("receipt_sha256")
    inconsistent_body["passes"][0]["speaker_evidence_sha256"] = "sha256:" + "8" * 64
    inconsistent = _finalize_receipt(inconsistent_body)
    missing_body = json.loads(json.dumps(review, ensure_ascii=False))
    missing_body.pop("receipt_sha256")
    missing_body["passes"][0].pop("speaker_transcript_sha256")
    missing = _finalize_receipt(missing_body)

    for invalid in (inconsistent, missing):
        assert not validate_source_fact_review(
            invalid,
            selection_hook=hook,
            title=title,
            final_transcript="测试字幕",
            clip_context_prompt="",
            speaker_evidence=evidence,
        )


def test_all_null_legacy_speaker_receipt_requires_authorized_absence_when_rebuilt() -> None:
    hook = "测试字幕形成完整事实。"
    title = "【李豆沙】测试字幕形成完整事实"
    review = review_and_repair_source_facts(
        selection_hook=hook,
        title=title,
        final_transcript="测试字幕",
        clip_context_prompt="",
        llm_call=lambda _prompt: _completion(
            status="KEEP",
            final_hook=hook,
            final_title=title,
            supported_by=["final_transcript"],
        ),
    )
    common = {
        "selection_hook": hook,
        "title": title,
        "final_transcript": "测试字幕",
        "clip_context_prompt": "",
    }

    # Omitted evidence preserves direct legacy validation, while production's
    # explicit rebuild accepts NULL only for the uniform-host absence state.
    assert validate_source_fact_review(review, **common)
    assert validate_source_fact_review(
        review,
        **common,
        speaker_evidence=_absent_speaker_evidence(),
    )
    assert not validate_source_fact_review(
        review,
        **common,
        speaker_evidence=_present_speaker_evidence("1 [李豆沙] 测试字幕"),
    )


def test_publish_choke_rebuilds_story_before_cover_after_joint_repair(
    tmp_path,
) -> None:
    bad_hook = "弹幕把技能命名成李姐拉拉。"
    bad_title = "【李豆沙】弹幕把技能命名成李姐拉拉，主播继续玩游戏"
    fixed_hook = "弹幕提议把技能叫李姐拉拉，主播随即拒绝。"
    fixed_title = "【李豆沙】弹幕提议把技能叫李姐拉拉，主播随即拒绝"
    transcript = "技能可以叫李姐拉拉吗\n不行，我这个应该叫李姐网"
    context = "- superchat: 技能可以叫李姐拉拉吗"

    def contract(hook: str) -> dict[str, object]:
        value = build_story_contract(
            candidate_id="candidate-source-fact",
            selection_hook=hook,
            transcript_text=transcript,
            selection_scorecard=None,
            session_relation_authority=None,
        )
        value["clip_context_prompt"] = context
        return value

    calls = 0
    title_prompts: list[str] = []

    def cpa(prompt: str) -> str:
        nonlocal calls
        calls += 1
        assert "关系缺失或未确认" in prompt
        assert "source text 保持不变" in prompt
        if calls == 1:
            return _completion(
                status="REPAIR",
                final_hook=fixed_hook,
                final_title=fixed_title,
                supported_by=["final_transcript", "structured_chat"],
                changed_surfaces=[
                    {
                        "artifact": "selection_hook",
                        "before": "命名成",
                        "after": "提议把技能叫",
                        "reason": "SC 是提议且主播拒绝。",
                        "evidence": [
                            "技能可以叫李姐拉拉吗",
                            "不行，我这个应该叫李姐网",
                        ],
                    },
                    {
                        "artifact": "title",
                        "before": "命名成",
                        "after": "提议把技能叫",
                        "reason": "保留事件模态。",
                        "evidence": ["技能可以叫李姐拉拉吗"],
                    },
                ],
            )
        return _completion(
            status="KEEP",
            final_hook=fixed_hook,
            final_title=fixed_title,
            supported_by=["final_transcript", "structured_chat"],
        )

    cover_calls: list[tuple[dict[str, object], dict[str, object]]] = []

    def stage_cover(
        record: dict[str, object],
        **kwargs: object,
    ) -> dict[str, object]:
        cover_calls.append((record, kwargs))
        cover = tmp_path / "cover.png"
        cover.write_bytes(b"cover")
        return {
            "status": "AI_COVER_READY",
            "cover_path": str(cover),
            "cover_generation": {"status": "READY"},
            "reason_codes": [],
        }

    media = tmp_path / "candidate.recut.mp4"
    media.write_bytes(b"video")

    # 60-title L10-14: automatic titles read the complete final SRT and return
    # 2–4 compared candidates, each with a verbatim final-subtitle quote.
    srt = tmp_path / "candidate.srt"
    srt.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n技能可以叫李姐拉拉吗\n\n"
        "2\n00:00:01,000 --> 00:00:02,000\n不行，我这个应该叫李姐网\n",
        encoding="utf-8",
    )

    def title_llm(prompt: str) -> str:
        title_prompts.append(prompt)
        body = bad_title.removeprefix("【李豆沙】")
        quote = {"text": "李姐拉拉", "speaker": ""}
        return json.dumps(
            {
                "title": body,
                "candidates": [
                    {"title": body, "quote": quote, "status": "SELECTED", "rationale": "offline fixture"},
                    {
                        "title": "弹幕问技能能不能叫“李姐拉拉”",
                        "quote": quote,
                        "status": "REJECTED",
                        "rationale": "offline fixture",
                    },
                ],
                "editorial_choice": {"selected": body, "reason": "offline fixture"},
                "selection_hook_anchor": "命名成李姐拉拉",
            },
            ensure_ascii=False,
        )

    staged = _stage_publish_draft(
        {
            "status": "MATERIALIZED",
            "speaker_mode": "uniform_host",
            "media_path": str(media),
            "subtitle_path": str(srt),
            "story_contract": contract(bad_hook),
            "artifact_hashes": {},
        },
        candidate_id="candidate-source-fact",
        title=bad_title,
        cues=[SourceCue("cue-1", 0, 1_000, transcript, "zh", "speech", 1.0)],
        run_ffmpeg=False,
        title_llm_call=title_llm,
        selection_hook=bad_hook,
        source_fact_llm_call=cpa,
        story_contract_rebuilder=contract,
        stage_cover=stage_cover,
    )

    assert staged is not None
    assert staged["story_contract"]["selection_hook"] == fixed_hook
    assert staged["story_contract"]["source_fact_review"]["decision"] == ("REPAIRED")
    publish = staged["publish_staging"]
    assert publish["title"] == fixed_title
    assert publish["title_authority_status"] == ("RESOLVED_CPA_SOURCE_FACT_REPAIR")
    assert publish["cover_status"] == "AI_COVER_READY"
    assert len(cover_calls) == 1
    cover_record, cover_kwargs = cover_calls[0]
    assert cover_kwargs["title"] == fixed_title
    assert cover_record["story_contract"]["selection_hook"] == fixed_hook
    assert cover_record["story_contract"]["source_fact_review"]["decision"] == "REPAIRED"
    assert len(title_prompts) == 1
    # 60-title L10-11: the skill title prompt carries only the complete final
    # SRT as facts (not StoryContract/selection-hook text); the relation
    # constraint is still enforced by the joint source-fact review (`cpa`).
    assert "技能可以叫李姐拉拉吗" in title_prompts[0]
    assert bad_hook not in title_prompts[0]
    assert calls == 2


def test_publish_choke_blocks_cover_when_joint_review_provider_fails(
    tmp_path,
) -> None:
    hook = "李豆沙讲了一件事。"
    title = "【李豆沙】李豆沙讲了一件具体而完整的事情"
    transcript = "李豆沙讲了一件事"
    story = build_story_contract(
        candidate_id="candidate-source-fact-fail",
        selection_hook=hook,
        transcript_text=transcript,
        selection_scorecard=None,
        session_relation_authority=None,
    )
    media = tmp_path / "candidate.recut.mp4"
    media.write_bytes(b"video")

    def cover_must_not_run(*_args, **_kwargs):
        raise AssertionError("cover ran before source-fact authority")

    staged = _stage_publish_draft(
        {
            "status": "MATERIALIZED",
            "speaker_mode": "uniform_host",
            "media_path": str(media),
            "story_contract": story,
            "artifact_hashes": {},
        },
        candidate_id="candidate-source-fact-fail",
        title=title,
        cues=[SourceCue("cue-1", 0, 1_000, transcript, "zh", "speech", 1.0)],
        run_ffmpeg=False,
        title_llm_call=None,
        selection_hook=hook,
        source_fact_llm_call=lambda _prompt: (_ for _ in ()).throw(
            RuntimeError("provider unavailable")
        ),
        story_contract_rebuilder=lambda _hook: story,
        stage_cover=cover_must_not_run,
    )

    assert staged is not None
    publish = staged["publish_staging"]
    assert publish["title_authority_status"] == ("BLOCKED_SOURCE_FACT_REVIEW")
    assert publish["cover_status"] == "BLOCKED_TITLE_AUTHORITY"
    assert publish["source_fact_review"]["status"] == "FAILED"


def test_manual_exact_title_repair_requires_new_authority_before_cover(
    tmp_path,
) -> None:
    hook = "弹幕提议把技能叫李姐拉拉。"
    manual_title = "【李豆沙】短标题"
    repaired_title = "【李豆沙】弹幕提议把技能叫李姐拉拉，主播随即拒绝"
    transcript = "技能可以叫李姐拉拉吗\n不行，我这个应该叫李姐网"
    story = build_story_contract(
        candidate_id="candidate-manual-authority",
        selection_hook=hook,
        transcript_text=transcript,
        selection_scorecard=None,
        session_relation_authority=None,
    )
    media = tmp_path / "candidate.recut.mp4"
    media.write_bytes(b"video")
    calls = 0

    def cpa(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        return _completion(
            status="REPAIR",
            final_hook=hook,
            final_title=repaired_title,
            supported_by=["final_transcript"],
            changed_surfaces=[
                {
                    "artifact": "title",
                    "before": "短标题",
                    "after": "弹幕提议把技能叫李姐拉拉，主播随即拒绝",
                    "reason": "补足最终字幕明确支持的完整事件。",
                    "evidence": [
                        "技能可以叫李姐拉拉吗",
                        "不行，我这个应该叫李姐网",
                    ],
                }
            ],
        )

    def cover_must_not_run(*_args, **_kwargs):
        raise AssertionError("cover ran without a new manual title authority")

    staged = _stage_publish_draft(
        {
            "status": "MATERIALIZED",
            "speaker_mode": "uniform_host",
            "media_path": str(media),
            "story_contract": story,
            "artifact_hashes": {},
        },
        candidate_id="candidate-manual-authority",
        title=manual_title,
        cues=[SourceCue("cue-1", 0, 1_000, transcript, "zh", "speech", 1.0)],
        run_ffmpeg=False,
        title_llm_call=None,
        selection_hook=hook,
        source_fact_llm_call=cpa,
        story_contract_rebuilder=lambda _hook: story,
        stage_cover=cover_must_not_run,
    )

    assert staged is not None
    publish = staged["publish_staging"]
    assert publish["title"] == manual_title
    assert publish["title_authority_status"] == ("BLOCKED_SOURCE_FACT_REVIEW")
    assert publish["source_fact_review"]["reason_code"] == ("SOURCE_FACT_TITLE_AUTHORITY_REQUIRED")
    assert publish["cover_status"] == "BLOCKED_TITLE_AUTHORITY"
    assert calls == 1


def test_prompt_teaches_hard_meme_canon_semantics() -> None:
    hook = "小李面对百合拷问当场自曝立场，弹幕全体起立。"
    title = "【李豆沙】小李面对百合拷问当场自曝立场"
    seen: dict[str, str] = {}

    def cpa(prompt: str) -> str:
        seen["prompt"] = prompt
        return _completion(
            status="KEEP",
            final_hook=hook,
            final_title=title,
            supported_by=["final_transcript"],
        )

    review = review_and_repair_source_facts(
        selection_hook=hook,
        title=title,
        final_transcript="你真的是侄女吗，弹幕都看呆了",
        clip_context_prompt="- danmaku @1000ms: 主播是直女吗",
        llm_call=cpa,
    )

    assert review["status"] == "PASS"
    prompt = seen["prompt"]
    assert "频道固定显示规范" in prompt
    assert "「直女」一律写作「侄女」" in prompt
    assert "不得把规范词面" in prompt or "永远不得把规范词面" in prompt


def test_prompt_and_receipt_record_consumed_editorial_research() -> None:
    hook = "小李分享小说，朋友反扣成找到聊得来的男的"
    title = "【李豆沙】分享《春雪》的句子，却被小室说成找到聊得来的男的了"
    research = {
        "schema": "research-editorial-applications.v1",
        "path": "candidate-applications.json",
        "sha256": "sha256:" + "a" * 64,
        "application_id": "E04",
        "applicability": "title-and-tags",
        "applications": [
            {
                "id": "E04",
                "first_title": "小李分享几句小说，朋友却说她找到聊得来的男的了",
                "rejected_version": "小李找到男朋友了",
            }
        ],
    }
    seen: dict[str, str] = {}

    def cpa(prompt: str) -> str:
        seen["prompt"] = prompt
        return _completion(
            status="KEEP",
            final_hook=hook,
            final_title=title,
            supported_by=["final_transcript"],
        )

    review = review_and_repair_source_facts(
        selection_hook=hook,
        title=title,
        final_transcript="小李分享《春雪》的句子，小室说这下找到聊得来的男的了",
        clip_context_prompt="",
        llm_call=cpa,
        selection_scorecard={"tier_reason": "literary exchange", "editorial_research": research},
    )

    assert review["status"] == "PASS"
    assert "candidate-applications.json" in seen["prompt"]
    assert "不是 source-fact authority" in seen["prompt"]
    first_pass = review["passes"][0]
    assert first_pass["editorial_research"]["application_id"] == "E04"
    assert first_pass["editorial_research_sha256"].startswith("sha256:")


def test_inapplicable_editorial_research_is_disclosed_without_being_used_as_authority() -> None:
    hook = "小李分享小说，朋友反扣成找到聊得来的男的"
    title = "【李豆沙】分享《春雪》的句子，却被小室说成找到聊得来的男的了"
    research = {
        "schema": "editorial-research-consumption.v1",
        "schema_version": "selection-scorecard-fallback.v1",
        "version": "selection-scorecard.v1",
        "source_kind": "runtime_record_field",
        "path": "record.selection_scorecard",
        "source_sha256": "sha256:" + "b" * 64,
        "applicable": False,
        "applicability": "没有绑定本片的 source-specific research projection",
        "inapplicable_reason": "普通 record 没有 editorial_research 字段",
        "status": "INAPPLICABLE",
        "scope": "normal_title_tags",
    }
    seen: dict[str, str] = {}

    def cpa(prompt: str) -> str:
        seen["prompt"] = prompt
        return _completion(
            status="KEEP",
            final_hook=hook,
            final_title=title,
            supported_by=["final_transcript"],
        )

    review = review_and_repair_source_facts(
        selection_hook=hook,
        title=title,
        final_transcript="小李分享《春雪》的句子，小室说这下找到聊得来的男的了",
        clip_context_prompt="",
        llm_call=cpa,
        selection_scorecard={"tier_reason": "literary exchange", "editorial_research": research},
    )

    assert review["status"] == "PASS"
    assert "INAPPLICABLE" in seen["prompt"]
    assert "不要读取未部署 reports" in seen["prompt"]
    first_pass = review["passes"][0]
    assert first_pass["editorial_research"]["status"] == "INAPPLICABLE"
    assert first_pass["editorial_research"]["applicable"] is False
    assert first_pass["editorial_research_sha256"].startswith("sha256:")


def _story_entity_srt() -> Path:
    return (
        Path(__file__).resolve().parents[2]
        / "assets/lidousha/reviewed_subtitle_baselines"
        / "auto_223750_578_734.reviewed.srt"
    )


def test_source_fact_response_binds_typed_entities_to_current_srt(tmp_path: Path) -> None:
    """A provider nomination is usable only when the final subtitle still matches."""

    hook = "小李聊三岛由纪夫的《春雪》"
    title = "【李豆沙】小李聊三岛由纪夫的《春雪》"
    final_transcript = "我讲三岛由纪夫的《春雪》"
    srt = tmp_path.resolve() / "current.srt"
    srt_text = (
        "1\n00:00:00,000 --> 00:00:02,000\n"
        f"{final_transcript}\n"
    )
    srt.write_text(srt_text, encoding="utf-8")

    def cpa(_prompt: str) -> str:
        assert "source_entity_candidates" in _prompt
        assert "不要从identity/spelling-only entity_context" in _prompt
        return _completion(
            status="KEEP",
            final_hook=hook,
            final_title=title,
            supported_by=["final_transcript"],
            source_entity_candidates=[
                {
                    "tag": "三岛由纪夫",
                    "role": "author",
                    "status": "SOURCE_CONFIRMED",
                    "surfaces": ["三岛由纪夫"],
                    "evidence": [final_transcript],
                },
                {
                    "tag": "《春雪》",
                    "role": "work",
                    "status": "SOURCE_CONFIRMED",
                    "surfaces": ["《春雪》"],
                    "evidence": [final_transcript],
                },
                {
                    "tag": "未在字幕出现的人名",
                    "role": "person",
                    "status": "SOURCE_CONFIRMED",
                    "surfaces": ["未在字幕出现的人名"],
                    "evidence": ["未在字幕出现的人名"],
                },
            ],
        )

    review = review_and_repair_source_facts(
        selection_hook=hook,
        title=title,
        final_transcript=final_transcript,
        clip_context_prompt="",
        llm_call=cpa,
        candidate_id="candidate-source-entity",
        final_reviewed_srt_path=srt,
    )

    assert review["status"] == "PASS"
    assert [row["tag"] for row in review["source_entity_candidates"]] == [
        "三岛由纪夫",
        "《春雪》",
    ]
    assert review["passes"][0]["source_entity_candidate_rejections"] == [
        "source-entity-candidate-2:tag-not-in-current-final-srt-and-transcript"
    ]
    source = review["source_entity_candidates"][0]["source"]
    assert source["source_kind"] == "source_fact_cpa_final_srt"
    assert source["srt_sha256"] == source["sha256"]
    assert source["request_sha256"] == review["passes"][0]["request_sha256"]
    assert source["response_sha256"] == review["passes"][0]["response_sha256"]
    assert validate_source_fact_review(
        review,
        selection_hook=hook,
        title=title,
        final_transcript=final_transcript,
        clip_context_prompt="",
        candidate_id="candidate-source-entity",
        final_reviewed_srt_path=srt,
    )

    srt.write_text(srt_text + "\n", encoding="utf-8")
    assert not validate_source_fact_review(
        review,
        selection_hook=hook,
        title=title,
        final_transcript=final_transcript,
        clip_context_prompt="",
        candidate_id="candidate-source-entity",
        final_reviewed_srt_path=srt,
    )


def test_empty_source_entity_response_without_srt_preserves_legacy_validation() -> None:
    hook = "小李聊一段普通对话"
    title = "【李豆沙】小李聊一段普通对话"
    review = review_and_repair_source_facts(
        selection_hook=hook,
        title=title,
        final_transcript="今天聊一段普通对话",
        clip_context_prompt="",
        llm_call=lambda _prompt: _completion(
            status="KEEP",
            final_hook=hook,
            final_title=title,
            supported_by=["final_transcript"],
            source_entity_candidates=[],
        ),
    )

    assert review["status"] == "PASS"
    assert review["source_entity_candidates"] == []
    assert validate_source_fact_review(
        review,
        selection_hook=hook,
        title=title,
        final_transcript="今天聊一段普通对话",
        clip_context_prompt="",
    )


@pytest.mark.parametrize("changed", [None, "srt", "title", "scorecard", "candidate"])
def test_normal_resume_keeps_original_fact_receipt_only_for_identical_inputs(
    tmp_path: Path, changed: str | None,
) -> None:
    """Package relocation must not fabricate a new CPA request or lose its verdict."""

    cid = "fact-resume"
    hook = "小李聊三岛由纪夫的《春雪》"
    title = "【李豆沙】小李聊三岛由纪夫的《春雪》"
    transcript = "我讲三岛由纪夫的《春雪》"
    subtitle = tmp_path / "original.srt"
    subtitle.write_text("1\n00:00:00,000 --> 00:00:02,000\n" + transcript + "\n")
    story = build_story_contract(
        candidate_id=cid, selection_hook=hook, transcript_text=transcript,
        selection_scorecard=None, session_relation_authority=None,
    )
    receipt = review_and_repair_source_facts(
        selection_hook=hook, title=title, final_transcript=transcript,
        clip_context_prompt=story["clip_context_prompt"],
        speaker_evidence=_absent_speaker_evidence(),
        candidate_id=cid, final_reviewed_srt_path=subtitle,
        llm_call=lambda _prompt: _completion(
            status="KEEP", final_hook=hook, final_title=title,
            supported_by=["final_transcript"], source_entity_candidates=[{
                "tag": "三岛由纪夫", "role": "author", "status": "SOURCE_CONFIRMED",
                "surfaces": ["三岛由纪夫"], "evidence": [transcript],
            }],
        ),
    )
    frozen = deepcopy(receipt)
    copied = tmp_path / "delivery.srt"
    copied.write_bytes(subtitle.read_bytes())
    if changed == "srt":
        copied.write_text(copied.read_text() + "\n")
    if changed == "title":
        title = "【李豆沙】小李聊三岛由纪夫的《春雪》，又笑了"
    if changed == "scorecard":
        story["selection_scorecard"] = {"tier_reason": "different story"}
    if changed == "candidate":
        cid = "different-candidate"
    story["source_fact_review"] = receipt
    media = tmp_path / "resume.recut.mp4"
    media.write_bytes(b"video")
    cover = tmp_path / "cover.png"
    cover.write_bytes(b"cover")
    staged = _stage_publish_draft(
        {"status": "MATERIALIZED", "speaker_mode": "uniform_host",
         "media_path": str(media), "subtitle_path": str(copied),
         "story_contract": story, "source_fact_review": receipt, "artifact_hashes": {}},
        candidate_id=cid, title=title,
        cues=[SourceCue("cue-1", 0, 2_000, transcript, "zh", "speech", 1.0)],
        run_ffmpeg=False, title_llm_call=None, source_fact_llm_call=None,
        selection_hook=hook,
        stage_cover=lambda *_args, **_kwargs: {
            "status": "AI_COVER_READY", "cover_path": str(cover),
            "cover_generation": {"status": "READY"}, "reason_codes": [],
        },
    )
    assert staged is not None
    assert receipt == frozen
    if changed is None:
        assert staged["publish_staging"]["source_fact_review"] == frozen
        assert staged["story_contract"]["source_fact_review"] == frozen
        publish = json.loads(Path(staged["publish_staging"]["publish_json_path"]).read_text())
        assert publish["source_fact_review"] == frozen
        assert frozen["source_entity_candidates"][0]["source"]["path"] == str(subtitle)
    else:
        assert staged["publish_staging"]["source_fact_review"] is None
        assert "EXISTING_SOURCE_FACT_REVIEW_STALE" in str(
            staged["publish_staging"].get("title_authority_error")
        )


@pytest.mark.parametrize("changed", [None, "quantities", "research_method"])
def test_normal_staging_projects_source_fact_entities_into_editorial_research(
    tmp_path: Path, changed: str | None,
) -> None:
    """The ordinary stage consumer sees the same receipt without preset candidates."""

    candidate_id = "candidate-source-entity-stage"
    hook = "小李聊三岛由纪夫的《春雪》"
    title = "【李豆沙】小李聊三岛由纪夫的《春雪》"
    transcript = "我讲三岛由纪夫的《春雪》"
    root = tmp_path.resolve()
    subtitle = root / "candidate.srt"
    subtitle.write_text(
        "1\n00:00:00,000 --> 00:00:02,000\n" f"{transcript}\n",
        encoding="utf-8",
    )
    media = root / "candidate.recut.mp4"
    media.write_bytes(b"video")
    story = build_story_contract(
        candidate_id=candidate_id,
        selection_hook=hook,
        transcript_text=transcript,
        selection_scorecard={"raw_score": 75.0, "dimensions": {"comedic_payoff": 3}},
        session_relation_authority=None,
    )

    def cpa(_prompt: str) -> str:
        return _completion(
            status="KEEP",
            final_hook=hook,
            final_title=title,
            supported_by=["final_transcript"],
            source_entity_candidates=[
                {
                    "tag": "三岛由纪夫",
                    "role": "author",
                    "status": "SOURCE_CONFIRMED",
                    "surfaces": ["三岛由纪夫"],
                    "evidence": [transcript],
                },
                {
                    "tag": "《春雪》",
                    "role": "work",
                    "status": "SOURCE_CONFIRMED",
                    "surfaces": ["《春雪》"],
                    "evidence": [transcript],
                },
            ],
        )

    def stage_cover(_record: dict[str, object], **_kwargs: object) -> dict[str, object]:
        cover = root / "cover.png"
        cover.write_bytes(b"cover")
        return {
            "status": "AI_COVER_READY",
            "cover_path": str(cover),
            "cover_generation": {"status": "READY"},
            "reason_codes": [],
        }

    staged = _stage_publish_draft(
        {
            "status": "MATERIALIZED",
            "speaker_mode": "uniform_host",
            "media_path": str(media),
            "subtitle_path": str(subtitle),
            "story_contract": story,
            "artifact_hashes": {},
        },
        candidate_id=candidate_id,
        title=title,
        cues=[SourceCue("cue-1", 0, 2_000, transcript, "zh", "speech", 1.0)],
        run_ffmpeg=False,
        title_llm_call=None,
        selection_hook=hook,
        source_fact_llm_call=cpa,
        story_contract_rebuilder=lambda _hook: story,
        stage_cover=stage_cover,
    )

    assert staged is not None
    receipt = staged["story_contract"]["source_fact_review"]
    assert [row["tag"] for row in receipt["source_entity_candidates"]] == [
        "三岛由纪夫",
        "《春雪》",
    ]
    research = staged["story_contract"]["editorial_research"]
    projection = research["candidate_projection"]
    assert projection["status"] == "APPLIED"
    assert projection["path"] == "record/story_contract.source_fact_review.source_entity_candidates"
    assert [row["tag"] for row in projection["source_entity_candidates"]] == [
        "三岛由纪夫",
        "《春雪》",
    ]
    assert projection["srt_sha256"] == receipt["source_entity_candidates"][0]["source"]["srt_sha256"]
    assert projection["bound_title"] == title


    # The receipt judges an immutable scorecard input. Current entity-tag
    # projection is a downstream output, retained separately on the story.
    judged_scorecard = staged["story_contract"]["selection_scorecard"]
    assert _canonical_sha256(judged_scorecard) == receipt["passes"][-1][
        "selection_scorecard_sha256"
    ]
    frozen_receipt = deepcopy(receipt)
    current_resume = _stage_publish_draft(
        deepcopy(staged), candidate_id=candidate_id, title=title,
        cues=[SourceCue("cue-1", 0, 2_000, transcript, "zh", "speech", 1.0)],
        run_ffmpeg=False, title_llm_call=None, source_fact_llm_call=None,
        selection_hook=hook, stage_cover=stage_cover,
    )
    assert current_resume["publish_staging"]["source_fact_review"] == frozen_receipt
    assert current_resume["story_contract"]["selection_scorecard"] == judged_scorecard
    assert current_resume["story_contract"]["editorial_research"][
        "candidate_projection"
    ]["status"] == "APPLIED"

    # Reproduce older staging's overwrite using its actual derived projection.
    # Only that proven overwrite can be recovered; a changed score or research
    # method must remain stale, even with a valid original receipt present.
    historical = deepcopy(staged)
    historical_scorecard = historical["story_contract"]["selection_scorecard"]
    historical_scorecard["editorial_research"] = deepcopy(research)
    if changed == "quantities":
        historical_scorecard["raw_score"] = 76.0
    if changed == "research_method":
        historical_scorecard["editorial_research"]["method"] = "changed-method"
    resumed = _stage_publish_draft(
        historical, candidate_id=candidate_id, title=title,
        cues=[SourceCue("cue-1", 0, 2_000, transcript, "zh", "speech", 1.0)],
        run_ffmpeg=False, title_llm_call=None, source_fact_llm_call=None,
        selection_hook=hook, stage_cover=stage_cover,
    )
    assert receipt == frozen_receipt
    if changed is None:
        assert resumed["publish_staging"]["source_fact_review"] == frozen_receipt
        assert resumed["story_contract"]["selection_scorecard"] == judged_scorecard
        assert resumed["story_contract"]["editorial_research"][
            "candidate_projection"
        ]["status"] == "APPLIED"
    else:
        assert resumed["publish_staging"]["source_fact_review"] is None
        assert "EXISTING_SOURCE_FACT_REVIEW_STALE" in str(
            resumed["publish_staging"].get("title_authority_error")
        )


def test_normal_staging_does_not_project_nonempty_entities_without_srt(
    tmp_path: Path,
) -> None:
    candidate_id = "candidate-source-entity-no-srt"
    hook = "小李聊三岛由纪夫的《春雪》"
    title = "【李豆沙】小李聊三岛由纪夫的《春雪》"
    transcript = "我讲三岛由纪夫的《春雪》"
    root = tmp_path.resolve()
    media = root / "candidate.recut.mp4"
    media.write_bytes(b"video")
    story = build_story_contract(
        candidate_id=candidate_id,
        selection_hook=hook,
        transcript_text=transcript,
        selection_scorecard=None,
        session_relation_authority=None,
    )

    def cpa(_prompt: str) -> str:
        return _completion(
            status="KEEP",
            final_hook=hook,
            final_title=title,
            supported_by=["final_transcript"],
            source_entity_candidates=[
                {
                    "tag": "三岛由纪夫",
                    "role": "author",
                    "status": "SOURCE_CONFIRMED",
                    "surfaces": ["三岛由纪夫"],
                    "evidence": [transcript],
                }
            ],
        )

    def stage_cover(_record: dict[str, object], **_kwargs: object) -> dict[str, object]:
        cover = root / "cover.png"
        cover.write_bytes(b"cover")
        return {
            "status": "AI_COVER_READY",
            "cover_path": str(cover),
            "cover_generation": {"status": "READY"},
            "reason_codes": [],
        }

    staged = _stage_publish_draft(
        {
            "status": "MATERIALIZED",
            "speaker_mode": "uniform_host",
            "media_path": str(media),
            "story_contract": story,
            "artifact_hashes": {},
        },
        candidate_id=candidate_id,
        title=title,
        cues=[SourceCue("cue-1", 0, 2_000, transcript, "zh", "speech", 1.0)],
        run_ffmpeg=False,
        title_llm_call=None,
        selection_hook=hook,
        source_fact_llm_call=cpa,
        story_contract_rebuilder=lambda _hook: story,
        stage_cover=stage_cover,
    )

    assert staged is not None
    receipt = staged["story_contract"]["source_fact_review"]
    assert receipt["source_entity_candidates"] == []
    assert receipt["source_entity_candidate_rejections"] == [
        "source-entity-candidates-final-srt-unavailable"
    ]
    projection = staged["story_contract"]["editorial_research"]["candidate_projection"]
    assert projection["status"] == "INAPPLICABLE"
    assert "source_entity_candidates" not in projection


def test_publish_projection_requires_source_fact_pass_hash_binding(tmp_path: Path) -> None:
    hook = "小李聊三岛由纪夫的《春雪》"
    title = "【李豆沙】小李聊三岛由纪夫的《春雪》"
    transcript = "我讲三岛由纪夫的《春雪》"
    srt = tmp_path.resolve() / "current.srt"
    srt_text = "1\n00:00:00,000 --> 00:00:02,000\n" f"{transcript}\n"
    srt.write_text(srt_text, encoding="utf-8")
    review = review_and_repair_source_facts(
        selection_hook=hook,
        title=title,
        final_transcript=transcript,
        clip_context_prompt="",
        llm_call=lambda _prompt: _completion(
            status="KEEP",
            final_hook=hook,
            final_title=title,
            supported_by=["final_transcript"],
            source_entity_candidates=[
                {
                    "tag": "三岛由纪夫",
                    "role": "author",
                    "status": "SOURCE_CONFIRMED",
                    "surfaces": ["三岛由纪夫"],
                    "evidence": [transcript],
                }
            ],
        ),
        candidate_id="candidate-source-entity-binding",
        final_reviewed_srt_path=srt,
    )
    assert validate_source_fact_review(
        review,
        selection_hook=hook,
        title=title,
        final_transcript=transcript,
        clip_context_prompt="",
        candidate_id="candidate-source-entity-binding",
        final_reviewed_srt_path=srt,
    )

    tampered = deepcopy(review)
    tampered["source_entity_candidates"][0]["source"]["request_sha256"] = (
        "sha256:" + "0" * 64
    )
    assert not validate_source_fact_review(
        tampered,
        selection_hook=hook,
        title=title,
        final_transcript=transcript,
        clip_context_prompt="",
        candidate_id="candidate-source-entity-binding",
        final_reviewed_srt_path=srt,
    )
    projection = _bind_normal_source_entity_projection(
        normalized_research={"status": "APPLICABLE"},
        record={},
        story_contract={"source_fact_review": tampered},
        title=title,
        final_srt_text=srt_text,
        final_srt_sha256=review["source_entity_candidates"][0]["source"]["srt_sha256"],
    )
    assert projection is not None
    assert projection["candidate_projection"]["status"] == "INAPPLICABLE"
    assert "source-fact-request-response-binding-missing-or-drifted" in str(
        projection["candidate_projection"]["rejected_candidates"]
    )












def test_judge_repair_reintroducing_banned_surface_is_recanonicalized() -> None:
    hook = "小李当场自曝侄女立场，弹幕全体起立。"
    title = "【李豆沙】小李当场自曝侄女立场引发弹幕轰动"
    transcript = "我就是侄女立场怎么了，弹幕全体起立"
    passes = {"count": 0}

    def cpa(prompt: str) -> str:
        passes["count"] += 1
        if passes["count"] == 1:
            return _completion(
                status="REPAIR",
                final_hook=hook,
                final_title="【李豆沙】小李亲口承认直女立场引发弹幕轰动",
                supported_by=["final_transcript"],
                changed_surfaces=[
                    {
                        "artifact": "title",
                        "before": "当场自曝直女立场",
                        "after": "亲口承认直女立场",
                        "reason": "字幕原话是承认而非自曝",
                        "evidence": ["final_transcript: 我就是侄女立场怎么了"],
                    }
                ],
            )
        return _completion(
            status="KEEP",
            final_hook=hook,
            final_title="【李豆沙】小李亲口承认侄女立场引发弹幕轰动",
            supported_by=["final_transcript"],
        )

    review = review_and_repair_source_facts(
        selection_hook=hook,
        title=title,
        final_transcript=transcript,
        clip_context_prompt="- danmaku @900ms: 你是直女吗",
        llm_call=cpa,
    )

    assert review["status"] == "PASS"
    assert review["decision"] == "REPAIRED"
    assert "直女" not in str(review["final_title"])
    assert "侄女" in str(review["final_title"])
    assert validate_source_fact_review(
        review,
        selection_hook=str(review["final_selection_hook"]),
        title=str(review["final_title"]),
        final_transcript=transcript,
        clip_context_prompt="- danmaku @900ms: 你是直女吗",
    )


def test_entry_canonicalizes_banned_surface_in_handwritten_spec() -> None:
    raw_hook = "小李被问是不是直女，当场语塞三秒。"
    canon_hook = "小李被问是不是侄女，当场语塞三秒。"
    raw_title = "【李豆沙】小李被问是不是直女当场语塞三秒"
    canon_title = "【李豆沙】小李被问是不是侄女当场语塞三秒"
    seen: dict[str, str] = {}

    def cpa(prompt: str) -> str:
        seen["prompt"] = prompt
        return _completion(
            status="KEEP",
            final_hook=canon_hook,
            final_title=canon_title,
            supported_by=["final_transcript"],
        )

    review = review_and_repair_source_facts(
        selection_hook=raw_hook,
        title=raw_title,
        final_transcript="你是不是侄女，当场语塞了三秒",
        clip_context_prompt="- danmaku @500ms: 是不是直女",
        llm_call=cpa,
    )

    assert review["status"] == "PASS"
    assert review["decision"] == "KEEP"
    assert review["original_selection_hook"] == canon_hook
    assert review["final_title"] == canon_title
    assert f"selection_hook:\n{canon_hook}" in seen["prompt"]


def test_invalid_response_is_contract_error_without_same_pass_retry() -> None:
    hook = "小李当场语塞三秒，弹幕全体起立。"
    title = "【李豆沙】小李当场语塞三秒弹幕全体起立"
    calls = {"n": 0}

    def cpa(prompt: str) -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            return "这不是JSON"
        return _completion(
            status="KEEP",
            final_hook=hook,
            final_title=title,
            supported_by=["final_transcript"],
        )

    review = review_and_repair_source_facts(
        selection_hook=hook,
        title=title,
        final_transcript="当场语塞了三秒，弹幕全体起立",
        clip_context_prompt="",
        llm_call=cpa,
    )

    assert review['status'] == 'FAILED' and review['reason_code'] == 'CPA_TEXT_REVIEW_INVALID'
    assert review['failure_layer'] == 'CONTRACT_ERROR'
    assert calls['n'] == 1 and not review['provider_retries']
    assert review['final_title'] == title and review['final_selection_hook'] == hook
    assert review["decision"] == "NONE"
    assert len(review["passes"]) == 1
    assert review["passes"][0]["response_sha256"] == (
        "sha256:" + hashlib.sha256("这不是JSON".encode("utf-8")).hexdigest()
    )
    assert not source_fact_review_passes(review)
    assert not validate_source_fact_review(
        review,
        selection_hook=hook,
        title=title,
        final_transcript="当场语塞了三秒，弹幕全体起立",
        clip_context_prompt="",
    )


def test_unavailable_provider_is_not_retried() -> None:
    review = review_and_repair_source_facts(
        selection_hook="测试钩子",
        title="【李豆沙】测试标题",
        final_transcript="测试字幕",
        clip_context_prompt="",
        llm_call=None,
    )

    assert review["status"] == "FAILED"
    assert review["reason_code"] == "CPA_TEXT_REVIEW_UNAVAILABLE"
    assert review["provider_retries"] == []
