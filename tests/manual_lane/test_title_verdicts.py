"""The channel owner's verdicts on candidate titles (assets/lidousha/title_owner_verdicts.v1.json, title_verdicts): the
asset's shape, the two texts rendered from it without the clip being written, and what the title prompt carries of it.
Every case runs on fakes: no model, no network."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from fakes import FakeCpa, FakeRemote, make_ctx, srt

from src.autoslice.manual_lane import stage_impl as si
from src.autoslice.manual_lane import title_verdicts
from src.autoslice.manual_lane.runner import StepOutcome
from src.autoslice.title_editorial_method import AuthorityError

ASSET = title_verdicts.VERDICTS
TEST_CLIP_IDS = ("1676030", "1672390", "289470")     # the three test clips: their verdicts must never be in the asset


def _clip(no, cid, bodies, comment, grades=(1, -1)):
    return {"no": no, "cid": cid, "main_line": f"片{no}的主线", "punchline": f"片{no}的笑点",
            "owner_comment": comment,
            "candidates": [{"letter": "AB"[i], "title_body": body, "grade": grades[i],
                            "label": {1: "还可以", -1: "差", None: "（没提到）"}[grades[i]]}
                           for i, body in enumerate(bodies)]}


def _write(path, clips, **top):
    path.write_text(json.dumps({"schema_version": "title-owner-verdicts.v1", "recorded_at": "2026-11-10T00:00:00Z",
                                "_doc": "x", "clips": clips, **top}, ensure_ascii=False), encoding="utf-8")
    return path


@pytest.fixture
def small(tmp_path, monkeypatch):
    path = _write(tmp_path / "verdicts.json", [
        _clip(1, "alpha", ["甲好标题", "甲差标题"], "甲的原话"),
        _clip(2, "beta", ["乙好标题", "乙差标题"], "乙的原话"),
        _clip(3, "beta_trim", ["丙好标题", "丙差标题"], "丙的原话")])
    monkeypatch.setattr(title_verdicts, "VERDICTS", path)
    return path


# ----- the asset ------------------------------------------------------------------------------------------------

def _private_content():
    """The owner's verdicts are private: the public export ships the asset with no clips, and a test about the real
    content has nothing to read there."""
    if not title_verdicts.load_verdicts().clips:
        pytest.skip("public export: title_owner_verdicts.v1.json carries no clips")


def test_the_asset_loads_and_has_the_shape_the_renderers_read():
    _private_content()
    verdicts = title_verdicts.load_verdicts()
    # he adds batches and later candidates to the same file: it only grows
    assert len(verdicts.clips) >= 20 and [c["no"] for c in verdicts.clips] == list(range(1, len(verdicts.clips) + 1))
    assert all(len(c["candidates"]) >= 4 and c["owner_comment"] for c in verdicts.clips)
    assert verdicts.source.sha256 == hashlib.sha256(ASSET.read_bytes()).hexdigest()
    top = json.loads(ASSET.read_text(encoding="utf-8"))
    assert top["schema_version"] == "title-owner-verdicts.v1" and top["recorded_at"].endswith("Z")
    assert top["_doc"].startswith("频道主 维护者 对候选标题的逐条评语")


def test_no_test_clip_has_an_entry_in_the_asset():
    for clip in title_verdicts.load_verdicts().clips:
        assert not any(test_id in clip["cid"] for test_id in TEST_CLIP_IDS), clip["cid"]


@pytest.mark.parametrize("damage,fragment", [
    (lambda top: top.update(schema_version="other"), "schema_version"),
    (lambda top: top.update(clips={}), "clips is not a list"),
    (lambda top: top["clips"][0].pop("owner_comment"), "owner_comment"),
    (lambda top: top["clips"][0].update(cid=""), "cid is empty"),
    (lambda top: top["clips"][0].update(no="1"), "no is not an integer"),
    (lambda top: top["clips"][0].update(candidates=[]), "candidates"),
    (lambda top: top["clips"][0]["candidates"][0].update(grade="好"), "grade"),
    (lambda top: top["clips"][0]["candidates"][0].update(grade=True), "grade"),
    (lambda top: top["clips"][0]["candidates"][0].pop("label"), "label"),
    (lambda top: top["clips"][0]["candidates"][0].update(title_body="  "), "title_body is empty"),
])
def test_a_malformed_asset_is_an_authority_error(tmp_path, monkeypatch, damage, fragment):
    top = json.loads(ASSET.read_text(encoding="utf-8"))
    damage(top)
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(top, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(title_verdicts, "VERDICTS", path)
    with pytest.raises(AuthorityError, match=fragment):
        title_verdicts.load_verdicts()


def test_a_missing_or_unreadable_asset_is_an_authority_error(tmp_path, monkeypatch):
    monkeypatch.setattr(title_verdicts, "VERDICTS", tmp_path / "absent.json")
    with pytest.raises(AuthorityError, match="unreadable"):
        title_verdicts.load_verdicts()
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(title_verdicts, "VERDICTS", broken)
    with pytest.raises(AuthorityError, match="not JSON"):
        title_verdicts.load_verdicts()


def test_a_grade_of_none_is_neither_liked_nor_disliked_but_its_label_is_in_the_corpus(tmp_path, monkeypatch):
    path = _write(tmp_path / "v.json", [_clip(1, "alpha", ["没提到的标题", "好标题"], "原话", grades=(None, 1))])
    monkeypatch.setattr(title_verdicts, "VERDICTS", path)
    liked = title_verdicts.liked_and_disliked("elsewhere")
    assert "- 好标题" in liked and "没提到的标题" not in liked
    assert "- 没提到的标题 → （没提到）" in title_verdicts.corpus("elsewhere")


# ----- the two texts, and the clip left out -----------------------------------------------------------------------
def test_liked_and_disliked_lists_the_accepted_and_the_rejected_with_his_words(small):
    text = title_verdicts.liked_and_disliked("elsewhere")
    liked, disliked = text.split("\n\n他判差的（差／尤其差），以及他说的毛病：\n")
    assert liked == "他认可的（很不错／还可以）：\n- 甲好标题\n- 乙好标题\n- 丙好标题"
    assert disliked == "- 甲差标题　他的原话：甲的原话\n- 乙差标题　他的原话：乙的原话\n- 丙差标题　他的原话：丙的原话"


def test_corpus_is_clip_by_clip_with_labels_and_his_words(small):
    assert title_verdicts.corpus("elsewhere").split("\n\n")[0] == (
        "### 片 1\n这段讲的是：片1的主线\n- 甲好标题 → 还可以\n- 甲差标题 → 差\n他的原话：甲的原话")


@pytest.mark.parametrize("cid,gone,kept", [
    ("alpha", ["甲"], ["乙", "丙"]),
    ("beta", ["乙"], ["甲", "丙"]),                  # exact match only: beta_trim is another clip
    ("beta_trim", ["乙", "丙"], ["甲"]),             # a trimmed successor leaves its source clip out as well
    ("alpha_trim", ["甲"], ["乙", "丙"]),
])
def test_both_texts_leave_out_the_current_clip_and_its_trim_source(small, cid, gone, kept):
    for text in (title_verdicts.liked_and_disliked(cid), title_verdicts.corpus(cid)):
        assert not any(g in text for g in gone), (cid, text)
        assert all(k in text for k in kept), (cid, text)


def test_only_the_exact_cid_is_excluded():
    assert title_verdicts.excluded_cids("a") == {"a"}
    assert title_verdicts.excluded_cids("a_trim") == {"a_trim", "a"}
    assert title_verdicts.excluded_cids("a_trim_trim") == {"a_trim_trim", "a_trim", "a"}
    assert title_verdicts.excluded_cids("a_r2") == {"a_r2", "a"}
    assert title_verdicts.excluded_cids("a_trim_r2") == {"a_trim_r2", "a_trim", "a"}
    assert title_verdicts.excluded_cids("a_r2_r3") == {"a_r2_r3", "a_r2", "a"}
    assert title_verdicts.excluded_cids("a_r2_trim") == {"a_r2_trim", "a_r2", "a"}
    assert title_verdicts.excluded_cids("a_trim_b") == {"a_trim_b"}       # not an ending: another clip


def test_the_real_asset_texts_leave_out_the_clip_asked_about():
    _private_content()
    verdicts = title_verdicts.load_verdicts()
    clip = verdicts.clips[12]
    liked, corpus = verdicts.liked_and_disliked(clip["cid"]), verdicts.corpus(clip["cid"])
    for text in (liked, corpus):
        assert clip["owner_comment"] not in text
        assert not any(c["title_body"].strip() in text for c in clip["candidates"])
    assert "### 片 13\n" not in corpus and corpus.count("### 片 ") == 19


# ----- what step 6 does with it ----------------------------------------------------------------------------------
def _cand(body):
    return {"title_body": body, "on_stage": ["李豆沙"], "punchline_person": "她越长大越讨厌看那种有规矩的",
            "swap_check": {"holds_with_other_host": False, "reason": "换主播不成立"}}


def _answer(*cands):
    return json.dumps({"candidates": list(cands), "recommended": 0, "cover_lines": ["破防了", "半分钟"]},
                      ensure_ascii=False)


def _pick(tmp_path, *, cid="clip", **options):
    cpa = FakeCpa([_answer(_cand("她半分钟后真的破防了"))], **options)
    ctx = make_ctx(tmp_path, remote=FakeRemote(), cpa=cpa)
    ctx.state.data["clip"]["cid"] = cid
    (ctx.clip_dir / "subs").mkdir(parents=True, exist_ok=True)
    (ctx.clip_dir / "subs/final.srt").write_text(srt(3), encoding="utf-8")
    return ctx, cpa, si.pick_title(ctx)


def test_the_title_prompt_carries_his_verdicts_after_the_main_line_block_but_not_the_current_clips(tmp_path):
    _private_content()
    clip = title_verdicts.load_verdicts().clips[12]
    other = next(c for c in title_verdicts.load_verdicts().clips
                 if c is not clip and any(x["grade"] is not None and x["grade"] <= -1 for x in c["candidates"]))
    _ctx, cpa, picked = _pick(tmp_path, cid=clip["cid"])
    assert not isinstance(picked, StepOutcome), picked
    prompt = cpa.title_prompts[0]
    heading = "## 频道主逐条评过的候选（体会口味用；只学取舍，不照抄句式，不搬内容）"
    assert prompt.count(heading) == 1
    assert prompt.index("## 本片主线（已由两次独立阅读核对") < prompt.index(heading) < prompt.index("## 本流水线的输入")
    assert "他认可的（很不错／还可以）：" in prompt and "他判差的（差／尤其差），以及他说的毛病：" in prompt
    assert other["owner_comment"] in prompt                      # the others' words are there
    assert clip["owner_comment"] not in prompt
    assert not any(c["title_body"].strip() in prompt for c in clip["candidates"])


def test_every_title_round_carries_the_verdicts(tmp_path):
    bad = _cand("她半分钟后真的破防了")
    bad["swap_check"]["holds_with_other_host"] = True
    cpa = FakeCpa([_answer(bad), _answer(_cand("她半分钟后真的破防了"))])
    ctx = make_ctx(tmp_path, remote=FakeRemote(), cpa=cpa)
    (ctx.clip_dir / "subs").mkdir(parents=True, exist_ok=True)
    (ctx.clip_dir / "subs/final.srt").write_text(srt(3), encoding="utf-8")
    assert not isinstance(si.pick_title(ctx), StepOutcome)
    assert len(cpa.title_prompts) == 2
    assert all("## 频道主逐条评过的候选" in p for p in cpa.title_prompts)


def test_the_assets_hash_is_recorded_with_the_authorities(tmp_path):
    ctx, _cpa, picked = _pick(tmp_path)
    record = json.loads((ctx.clip_dir / "title/candidates.json").read_text(encoding="utf-8"))
    mine = [a for a in record["authorities"] if a["name"] == "assets/lidousha/title_owner_verdicts.v1.json"]
    assert len(mine) == 1 and mine[0]["sha256"] == hashlib.sha256(ASSET.read_bytes()).hexdigest()
    assert Path(mine[0]["path"]) == ASSET
    assert any("title_owner_verdicts.v1.json " + mine[0]["sha256"][:12] in line for line in picked[2])


@pytest.mark.parametrize("damage", ["missing", "broken"])
def test_a_missing_or_broken_asset_stops_the_title_step_before_any_call(tmp_path, monkeypatch, damage):
    path = tmp_path / "gone" / "v.json"
    if damage == "broken":
        path.parent.mkdir()
        path.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(title_verdicts, "VERDICTS", path)
    _ctx, cpa, picked = _pick(tmp_path / "w")
    assert isinstance(picked, StepOutcome) and picked.reason == "TITLE_AUTHORITY_MISSING"
    assert "title_owner_verdicts.v1.json" in picked.note and cpa.calls == []
