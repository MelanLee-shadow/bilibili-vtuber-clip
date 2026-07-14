"""Upload tag suggester: proper-noun determinism, Ivan's searchability bar, and
the fail-safe pipeline entry (generate_upload_tags must never block delivery)."""
import json

import pytest

import scripts.suggest_upload_tags as st
from src.autoslice.llm_client import LlmCallError


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
    # Ivan: 专名只出正主名 — 大N老师/豆町 只是触发面。
    tags = {h.tag for h in st.scan_proper_nouns("", "大N老师来了\n豆町天下第一")}
    assert "南町" in tags
    assert "大N老师" not in tags and "豆町" not in tags


def test_merge_caps_and_dedupes():
    proper = [st.ProperHit(tag=f"专名{i}") for i in range(10)]
    content = [{"tag": "可爱", "why": ""}]
    final = st.merge_tags(st.BASE_TAGS, proper, content, 12)
    assert len(final) == 12
    assert final[: len(st.BASE_TAGS)] == list(st.BASE_TAGS)
    assert len(set(t.casefold() for t in final)) == len(final)


def test_generate_upload_tags_ok_with_stub_llm(tmp_path):
    srt = _srt(tmp_path, ["我是直女", "好可爱"])
    stub = lambda prompt: json.dumps({"tags": [{"tag": "可爱", "why": "字幕说好可爱"}, {"tag": "彩排", "why": "硬毙词必须被过滤"}]})
    out = st.generate_upload_tags("【李豆沙】标题", srt, llm_call=stub)
    assert out["status"] == "OK" and out["engine"] == st.ENGINE_VERSION
    assert "侄女" in out["final_tags"] and "可爱" in out["final_tags"]
    assert "彩排" not in out["final_tags"]  # Ivan 硬毙词
    assert out["final_tag_line"].startswith(",".join(st.BASE_TAGS))


def test_generate_upload_tags_degrades_without_llm(tmp_path):
    srt = _srt(tmp_path, ["我是直女"])

    def broken(prompt):
        raise LlmCallError("cpa down")

    out = st.generate_upload_tags("【李豆沙】标题", srt, llm_call=broken)
    assert out["status"] == "OK_NO_LLM"
    assert "侄女" in out["final_tags"]  # 专名层照常


def test_generate_upload_tags_never_raises(tmp_path):
    out = st.generate_upload_tags("标题", tmp_path / "missing.srt", use_llm=False)
    assert out["status"] == "FAILED" and out["final_tags"] == []
    assert "error" in out


def test_title_only_mode_scans_title(tmp_path):
    out = st.generate_upload_tags("【李豆沙】侄女卖姬太舒适了", None, use_llm=False)
    assert out["status"] == "OK"
    assert "侄女" in out["final_tags"]
