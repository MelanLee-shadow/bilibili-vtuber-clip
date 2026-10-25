"""Upload tag suggester: proper-noun determinism, 维护者's searchability bar, and
the fail-safe pipeline entry (generate_upload_tags must never block delivery)."""
import json
import pytest


import scripts.suggest_upload_tags as st
from src.autoslice.llm_client import LlmCallError
from src.autoslice.surface_canon import CHANNEL_PROFILE


def _srt(tmp_path, text_lines):
    path = tmp_path / "clip.srt"
    blocks = []
    for i, line in enumerate(text_lines, start=1):
        blocks.append(f"{i}\n00:00:{i:02d},000 --> 00:00:{i:02d},900\n{line}")
    path.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")
    return path


def test_zhinv_rule_fires_on_zhinu_mishearing():
    # 铁律: 字幕「直女」永远是「侄女」误听, 同样触发 侄女/百合/女同。
    hits = st.scan_proper_nouns("标题", "我是直女\n真的")
    tags = {h.tag for h in hits}
    assert {"侄女", "百合", "女同"} <= tags


def test_nv_tong_shi_does_not_fire_yuri():
    hits = st.scan_proper_nouns("和几十个女同事结婚", "女同事都是朋友\n女同\n事结婚")
    assert not {"百合", "女同"} & {h.tag for h in hits}


def test_cross_cue_condensed_matching_recovers_split_term():
    # 实案: 「梦限大」被字幕跨 cue 截断成「梦\n限大」, 连体视图必须救回。
    hits = st.scan_proper_nouns("标题", "我们要看那个梦\n限大现在推出了")
    assert "梦限大" in {h.tag for h in hits}


def test_142_needs_digit_boundary():
    assert "伊索尔" not in {h.tag for h in st.scan_proper_nouns("", "房间号1420号")}
    assert "伊索尔" in {h.tag for h in st.scan_proper_nouns("", "今天142说的故事")}


def test_proper_tags_use_searchable_canonical_names():

    tags = {h.tag for h in st.scan_proper_nouns("", "大N老师来了\n豆町天下第一")}
    assert "南町" in tags
    assert "大N老师" not in tags and "豆町" not in tags


def test_merge_caps_and_dedupes():
    proper = [st.ProperHit(tag=f"专名{i}") for i in range(10)]
    content = [{"tag": "可爱", "why": ""}]
    final = st.merge_tags(st.BASE_TAGS, proper, content, 12)
    assert len(final) == 10  # four fixed slots + six dynamic slots
    assert final[: len(st.BASE_TAGS)] == list(st.BASE_TAGS)
    assert len(set(t.casefold() for t in final)) == len(final)


def test_f19_important_content_ip_canary_disappears_when_whitelist_is_disabled():
    body = "今天重看了战斗吧！歌姬，里面这一段真的很有意思"

    enabled = st.scan_proper_nouns("一个不机械塞专名的标题", body)
    disabled = st.scan_proper_nouns(
        "一个不机械塞专名的标题",
        body,
        important_content_ip_rules=(),
    )

    assert "战斗吧歌姬" in {hit.tag for hit in enabled}
    assert "战斗吧歌姬" not in {hit.tag for hit in disabled}
    hit = next(hit for hit in enabled if hit.tag == "战斗吧歌姬")
    assert hit.rules == ["battle_girl_project"]
    assert "战斗吧！歌姬" not in {item.tag for item in enabled}


def test_f19_important_content_ip_is_a_final_subtitle_tag_candidate(tmp_path):
    srt = _srt(tmp_path, ["刚刚聊到战斗吧歌姬", "接着说别的话"])
    out = st.generate_upload_tags("标题不机械插入节目名", srt, use_llm=False)

    assert out["status"] == "OK"
    assert out["important_content_ips"] == ["战斗吧歌姬"]
    assert out["final_tags"][:4] == list(st.BASE_TAGS)
    assert "战斗吧歌姬" in out["final_tags"]
    assert len(out["final_tags"]) <= len(st.BASE_TAGS) + st.MAX_DYNAMIC_TAGS


def test_generate_upload_tags_ok_with_stub_llm(tmp_path):
    srt = _srt(tmp_path, ["我是直女", "好可爱"])
    def stub(prompt):
        return json.dumps({"tags": [{"tag": "可爱", "why": "字幕说好可爱"}, {"tag": "彩排", "why": "硬毙词必须被过滤"}]})
    out = st.generate_upload_tags(f"{CHANNEL_PROFILE.talk_title_prefix}标题", srt, llm_call=stub)
    assert out["status"] == "OK" and out["engine"] == st.ENGINE_VERSION
    assert "侄女" in out["final_tags"] and "可爱" in out["final_tags"]
    assert "彩排" not in out["final_tags"]
    assert out["final_tag_line"].startswith(",".join(st.BASE_TAGS))


def test_generate_upload_tags_degrades_without_llm(tmp_path):
    srt = _srt(tmp_path, ["我是直女"])

    def broken(prompt):
        raise LlmCallError("cpa down")

    out = st.generate_upload_tags(f"{CHANNEL_PROFILE.talk_title_prefix}标题", srt, llm_call=broken)
    assert out["status"] == "OK_NO_LLM"
    assert "侄女" in out["final_tags"]  # 专名层照常


def test_generate_upload_tags_never_raises(tmp_path):
    out = st.generate_upload_tags("标题", tmp_path / "missing.srt", use_llm=False)
    assert out["status"] == "FAILED" and out["final_tags"] == []
    assert "error" in out


def test_title_only_mode_scans_title(tmp_path):
    out = st.generate_upload_tags(f"{CHANNEL_PROFILE.talk_title_prefix}侄女卖姬太舒适了", None, use_llm=False)
    assert out["status"] == "OK"
    assert "侄女" in out["final_tags"]


def test_cpa_command_effort_is_low_same_model_chain():
    """(维护者): tag suggestion is a non-load-bearing content layer
    (unknown_songs-style fuzzy world-knowledge pick, discarded by downstream
    validation on a bad guess) -- no other config shares this constant, so
    dropping medium -> low is zero-blast-radius.  Pin the model chain too so
    a future edit can't silently widen it while touching effort."""

    assert "'gpt-6.1-sol'" in st.CPA_COMMAND
    assert st.CPA_COMMAND.split()[-1] == "medium"


def test_generate_upload_tags_default_transport_wires_the_low_effort_command(
    tmp_path, monkeypatch
):
    """With llm_call=None, generate_upload_tags must build its call from the
    real CPA_COMMAND (not a stand-in), and still degrade to a valid OK_NO_LLM
    schema (fail-safe pipeline contract untouched by the effort change).  The
    hermetic pytest guard blocks the real `llm_via_cpa.sh` transport, so a
    successful degrade here also proves CPA_COMMAND -- not some other
    template -- is what actually got dispatched."""

    captured = {}
    real_build_llm_call = st.build_llm_call

    def _capture_and_delegate(config):
        captured["config"] = config
        return real_build_llm_call(config)

    monkeypatch.setattr(st, "build_llm_call", _capture_and_delegate)

    srt = _srt(tmp_path, ["我是直女"])
    out = st.generate_upload_tags(
        f"{CHANNEL_PROFILE.talk_title_prefix}标题", srt, timeout=5.0
    )

    assert captured["config"].command_template == st.CPA_COMMAND
    assert out["status"] == "OK_NO_LLM"
    assert "侄女" in out["final_tags"]  # 专名层不受内容层降级影响
    assert any("TEST_HERMETIC_LLM_BLOCKED" in w for w in out["warnings"])


def test_content_tags_use_bound_runtime_credentials_only_for_command_child(tmp_path, monkeypatch):
    captured = {}
    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path))
    def read_env(root):
        captured["root"] = root
        return {"CPA_BASE_URL": "https://example.invalid", "CPA_API_KEY": "private-test-key"}
    monkeypatch.setattr(st, "runtime_cpa_command_environment", read_env)
    def build(config):
        captured["config"] = config
        return lambda _prompt: '{"tags":[{"tag":"月饼","why":"讨论月饼口味"}]}'
    monkeypatch.setattr(st, "build_llm_call", build)
    tags, warnings = st.llm_content_tags("月饼", "月饼太甜", [], 30)
    assert [row["tag"] for row in tags] == ["月饼"]
    assert warnings == []
    assert captured["root"] == tmp_path
    assert captured["config"].runtime_root == str(tmp_path)
    assert captured["config"].command_child_env["CPA_API_KEY"] == "private-test-key"
    assert "private-test-key" not in repr(captured["config"])


def test_missing_runtime_tag_credentials_remain_a_typed_content_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path))
    def unavailable(_root):
        raise st.LlmCallError("LLM_RUNTIME_CPA_ENV_UNSAFE")
    monkeypatch.setattr(st, "runtime_cpa_command_environment", unavailable)
    tags, warnings = st.llm_content_tags("月饼", "月饼太甜", [], 30)
    assert tags == []
    assert "LLM_RUNTIME_CPA_ENV_UNSAFE" in warnings[0]


@pytest.mark.parametrize("status", ["OK_NO_LLM", "FAILED"])
def test_failed_content_tags_cannot_publish_only_base_tags(status):
    from src.autoslice.authorized_upload_metadata import validate_tag_generation
    problems = validate_tag_generation({"status": status, "final_tags": list(st.BASE_TAGS)})
    assert problems and "CONTENT_TAG_GENERATION_INCOMPLETE" in problems[0]
    assert validate_tag_generation({"status": "OK", "final_tags": list(st.BASE_TAGS)}) == []
    assert validate_tag_generation({"status": status, "final_tags": [*st.BASE_TAGS, "月饼"]}) == []


def test_skipped_topic_pass_is_distinct_from_successful_empty_review(tmp_path):
    from src.autoslice.authorized_upload_metadata import validate_tag_generation
    srt_path = _srt(tmp_path, ["讨论月饼口味"])
    skipped = st.generate_upload_tags("月饼", srt_path, use_llm=False)
    assert skipped["content_review_status"] == "NOT_REQUESTED"
    assert validate_tag_generation(skipped)
    reviewed = st.generate_upload_tags("月饼", srt_path, llm_call=lambda _p: '{"tags":[]}')
    assert reviewed["content_review_status"] == "COMPLETED"
    assert validate_tag_generation(reviewed) == []
def test_source_entity_candidates_are_cpa_selected_and_bound_to_final_srt(tmp_path):
    srt = _srt(tmp_path, [
        "因为我觉得三岛由纪夫最好入门的一本书就是春雪",
    ])
    candidates = [
        {
            "tag": "三岛由纪夫",
            "role": "author",
            "editorial_role": "necessary_context",
            "status": "CONFIRMED",
            "surfaces": ["三岛由纪夫"],
            "evidence": ["因为我觉得三岛由纪夫最好入门的一本书"],
            "source": {"path": "source-manifest.json", "sha256": "sha256:" + "a" * 64},
        },
        {
            "tag": "春雪",
            "role": "work",
            "editorial_role": "primary_content",
            "status": "CONFIRMED",
            "surfaces": ["春雪", "《春雪》"],
            "evidence": ["就是春雪"],
            "source": {"url": "https://www.shinchosha.co.jp/book/105049/"},
        },
    ]

    def stub(prompt):
        assert "三岛由纪夫" in prompt and "春雪" in prompt
        return json.dumps(
            {
                "tags": [{"tag": "读书", "why": "字幕明确在分享书句子"}],
                "entity_decisions": [
                    {
                        "tag": "三岛由纪夫",
                        "action": "INCLUDE",
                        "content_role": "necessary_context",
                        "why": "作者被明确点名且是本片文学话题中心",
                        "evidence": ["因为我觉得三岛由纪夫最好入门的一本书"],
                    },
                    {
                        "tag": "春雪",
                        "action": "INCLUDE",
                        "content_role": "primary_content",
                        "why": "作品被明确点名并围绕它展开",
                        "evidence": ["就是春雪"],
                    },
                ],
            },
            ensure_ascii=False,
        )

    out = st.generate_upload_tags(
        "【李豆沙】分享《春雪》的句子",
        srt,
        llm_call=stub,
        source_entity_candidates=candidates,
    )

    assert out["status"] == "OK"
    assert out["source_entity_tags"] == ["三岛由纪夫", "春雪"]
    assert out["final_tags"][:4] == list(st.BASE_TAGS)
    assert {"三岛由纪夫", "春雪", "读书"} <= set(out["final_tags"])
    assert {row["action"] for row in out["source_entity_decisions"]} == {"INCLUDE"}
    assert {row["content_role"] for row in out["source_entity_decisions"]} == {
        "necessary_context",
        "primary_content",
    }
    assert out["input_binding"]["srt_sha256"].startswith("sha256:")
    assert out["input_binding"]["title_sha256"].startswith("sha256:")


def test_source_entity_candidate_needs_exact_final_text_and_no_phonetic_guess(tmp_path):
    srt = _srt(tmp_path, ["我喜欢看三岛的东西"])
    out = st.generate_upload_tags(
        "【李豆沙】聊读书",
        srt,
        use_llm=False,
        source_entity_candidates=[
            {
                "tag": "三岛由纪夫",
                "role": "author",
                "status": "CONFIRMED",
                "surfaces": ["三岛由纪夫"],
            },
        ],
    )
    assert out["status"] == "OK"
    assert out["source_entity_candidates"] == []
    assert out["source_entity_tags"] == []
    assert any("not-in-final-text" in warning for warning in out["warnings"])


def test_normal_consumer_can_preserve_existing_tag_set_while_reviewing_entities(tmp_path):
    srt = _srt(tmp_path, [
        "因为我觉得三岛由纪夫最好入门的一本书就是春雪",
    ])
    existing = [
        "李豆沙",
        "虚拟主播",
        "虚拟UP主",
        "直播切片",
        "春雪",
        "读书",
        "文学摘抄",
        "择偶标准",
    ]
    candidates = [
        {
            "tag": "三岛由纪夫",
            "role": "author",
            "editorial_role": "necessary_context",
            "status": "CONFIRMED",
            "surfaces": ["三岛由纪夫"],
            "evidence": ["因为我觉得三岛由纪夫最好入门的一本书"],
            "source": {
                "path": "source-manifest.json",
                "sha256": "sha256:" + "a" * 64,
            },
            "research_basis": "作者作品关系由既有来源核实；是否入标签交 CPA",
        },
    ]

    def stub(prompt):
        assert "necessary_context" in prompt
        return json.dumps(
            {
                "tags": [],
                "entity_decisions": [
                    {
                        "tag": "三岛由纪夫",
                        "action": "INCLUDE",
                        "content_role": "necessary_context",
                        "why": "作者是理解当前作品入口的必要背景",
                        "evidence": ["因为我觉得三岛由纪夫最好入门的一本书"],
                    }
                ],
            },
            ensure_ascii=False,
        )

    out = st.generate_upload_tags(
        "【李豆沙】分享《春雪》的句子",
        srt,
        llm_call=stub,
        source_entity_candidates=candidates,
        existing_tags=existing,
    )

    assert out["status"] == "OK"
    assert out["final_tags"][:8] == existing
    assert out["final_tags"][8:] == ["三岛由纪夫"]
    assert out["input_binding"]["existing_tags"] == existing
    assert out["source_entity_decisions"][0]["content_role"] == "necessary_context"


def test_normal_consumer_loads_hash_bound_entity_projection_without_manual_candidates(tmp_path):
    srt = _srt(tmp_path, ["因为我觉得三岛由纪夫最好入门的一本书就是《春雪》"])
    title = "【李豆沙】分享《春雪》的句子"
    editorial_research = {
        "schema": "editorial-research-consumption.v1",
        "version": "title-tag-editorial-method.v1",
        "path": "candidate-applications.json",
        "sha256": "sha256:" + "a" * 64,
        "source_kind": "hash_bound_research_projection",
        "status": "APPLICABLE",
        "applicability": "文学作品和作者检索入口由来源研究核实，是否入标签交 CPA。",
        "inapplicable_reason": "无其他未绑定研究报告作为候选来源。",
        "candidate_projection": {
            "status": "APPLICABLE",
            "source_path": "source-manifest.json",
            "source_sha256": "sha256:" + "b" * 64,
        },
        "source_entity_candidates": [
            {
                "tag": "三岛由纪夫",
                "role": "author",
                "editorial_role": "necessary_context",
                "status": "CONFIRMED",
                "surfaces": ["三岛由纪夫"],
                "evidence": ["因为我觉得三岛由纪夫最好入门"],
                "source": {
                    "path": "https://www.shinchosha.co.jp/book/105049/",
                    "sha256": "sha256:" + "c" * 64,
                },
                "research_basis": "作者与作品关系已由来源核实",
            },
            {
                "tag": "春雪",
                "role": "work",
                "editorial_role": "primary_content",
                "status": "VERIFIED",
                "surfaces": ["春雪", "《春雪》"],
                "evidence": ["就是《春雪》"],
            },
        ],
    }
    sidecar = {
        "schema_version": "shadow-publish-draft.v1",
        "title": title,
        "title_sha256": st._sha256_text(title),
        "subtitle_path": str(srt),
        "subtitle_sha256": st._sha256_file(srt),
        "editorial_research": editorial_research,
    }
    sidecar_path = srt.with_suffix(".publish.json")
    sidecar_path.write_text(json.dumps(sidecar, ensure_ascii=False), encoding="utf-8")

    def stub(prompt):
        assert "三岛由纪夫" in prompt and "春雪" in prompt
        assert "作者与作品关系已由来源核实" in prompt
        assert "inapplicable_reason" in prompt
        return json.dumps(
            {
                "tags": [],
                "entity_decisions": [
                    {
                        "tag": "三岛由纪夫",
                        "action": "INCLUDE",
                        "content_role": "necessary_context",
                        "why": "作者是本片明确作品分享的检索入口",
                        "evidence": ["因为我觉得三岛由纪夫最好入门"],
                    },
                    {
                        "tag": "春雪",
                        "action": "INCLUDE",
                        "content_role": "primary_content",
                        "why": "作品在正文中被明确分享",
                        "evidence": ["就是《春雪》"],
                    },
                ],
            },
            ensure_ascii=False,
        )

    out = st.generate_upload_tags(title, srt, llm_call=stub)

    assert out["status"] == "OK"
    assert out["source_entity_tags"] == ["三岛由纪夫", "春雪"]
    assert out["source_entity_candidate_projection"]["status"] == "APPLIED"
    assert out["source_entity_candidate_projection"]["sidecar_path"] == str(sidecar_path)
    assert out["editorial_research_consumption"]["path"] == "candidate-applications.json"
    assert {"三岛由纪夫", "春雪"} <= set(out["final_tags"])


def test_normal_consumer_rejects_drifted_sidecar_and_does_not_guess_names(tmp_path):
    srt = _srt(tmp_path, ["我喜欢看三岛的东西"])
    title = "【李豆沙】聊读书"
    sidecar = {
        "title": "【李豆沙】旧标题",
        "title_sha256": st._sha256_text("【李豆沙】旧标题"),
        "subtitle_sha256": st._sha256_file(srt),
        "source_entity_candidates": [
            {
                "tag": "三岛由纪夫",
                "role": "author",
                "status": "CONFIRMED",
                "surfaces": ["三岛由纪夫"],
            }
        ],
    }
    srt.with_suffix(".publish.json").write_text(
        json.dumps(sidecar, ensure_ascii=False), encoding="utf-8"
    )
    out = st.generate_upload_tags(title, srt, use_llm=False)

    assert out["status"] == "OK"
    assert out["source_entity_candidates"] == []
    assert out["source_entity_tags"] == []
    assert out["source_entity_candidate_projection"]["status"] == "UNAVAILABLE"
    assert any("title-drift" in warning for warning in out["warnings"])


@pytest.mark.parametrize("drift", ["missing_srt", "conflicting_srt"])
def test_normal_entity_sidecar_requires_current_srt_binding(tmp_path, drift):
    srt = _srt(tmp_path, ["因为我觉得三岛由纪夫最好入门的一本书就是春雪"])
    title = "【李豆沙】分享《春雪》的句子"
    sidecar = {
        "title": title,
        "title_sha256": st._sha256_text(title),
        "source_entity_candidates": [{
            "tag": "三岛由纪夫", "role": "author", "status": "CONFIRMED",
            "surfaces": ["三岛由纪夫"],
        }],
    }
    if drift == "conflicting_srt":
        sidecar["subtitle_sha256"] = st._sha256_file(srt)
        sidecar["input_binding"] = {"srt_sha256": "sha256:" + "0" * 64}
    srt.with_suffix(".publish.json").write_text(json.dumps(sidecar), encoding="utf-8")
    candidates, receipt, _, warnings = st._load_normal_source_entity_projection(
        title=title, srt_path=srt,
    )
    assert candidates == []
    assert receipt["status"] == "UNAVAILABLE"
    assert any("unbound" in warning or "srt-hash-drift" in warning for warning in warnings)
