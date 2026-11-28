"""Post-step-7 tag mutations must always satisfy the step-7 upload tag policy.

Covers every path that adds or changes tags after step 7:
- step 9 review tag fixes (`stage_package.apply_fixes` -> `step7_tags.apply_review_tags`)
- step 7 re-run merging `reviewed_tags` with newly generated tags (`step7_tags.step7_tags`)
- published-manuscript revisions keeping online tags (`ONLINE_SOURCE`)
- owner metadata edits of a published BV (`lane_publish.metadata`)
- first-publication manifest creation & package audit (`lane_publish.manifest`, `lane_publish.audit`)
- the shared policy function (`upload_tag_policy.apply_tag_policy`)
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest
from fakes import FakeCpa, FakeRemote, make_ctx
from test_lane_publish import TITLE, _first, host, package as make_publish_pkg  # noqa: F401
from test_review_once import RD, _package, _review, _srt

from src.autoslice.authorized_upload_metadata import validate_tags
from src.autoslice.finished_media_review import checks as fmr_checks
from src.autoslice.lane_publish import audit as lp_audit
from src.autoslice.lane_publish import manifest as lp_manifest
from src.autoslice.lane_publish.cli import main as lp_main
from src.autoslice.manual_lane import stage_impl as si
from src.autoslice.manual_lane import stage_package as sp
from src.autoslice.manual_lane import step7_tags
from src.autoslice.manual_lane.adapters import RemoteResult
from src.autoslice.upload_tag_policy import load_selected_upload_tag_policy


BASE = list(load_selected_upload_tag_policy().base_tags)
TEN_TAGS = [*BASE, "专名甲", "内容1", "内容2", "内容3", "内容4", "内容5"]


def _write_step7_tags(ctx, final_tags: list[str], *, proper: list[str] | None = None, source: str | None = None) -> None:
    title = (ctx.clip_dir / "title/title.txt").read_text(encoding="utf-8").strip()
    srt = ctx.clip_dir / "subs/final.srt"
    doc: dict[str, object] = {
        "status": "OK",
        "content_review_status": "COMPLETED",
        "final_tags": list(final_tags),
        "final_tag_line": ",".join(final_tags),
        "proper_noun_tags": [{"tag": t} for t in (proper or [])],
        "input_binding": {
            "title_sha256": hashlib.sha256(title.encode("utf-8")).hexdigest(),
            "srt_path": str(srt),
            "srt_sha256": hashlib.sha256(srt.read_bytes()).hexdigest(),
            "existing_tags": list(BASE),
        },
    }
    if source is not None:
        doc["source"] = source
    for rel in ("tags/tags.json", "pkg/tags.json"):
        p = ctx.clip_dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")


def _run_fmr_a10(ctx) -> dict[str, object]:
    """Run the real FMR A10 check against the clip's current tags/tags.json."""
    evidence = ctx.clip_dir / "fmr_evidence"
    evidence.mkdir(parents=True, exist_ok=True)

    class _FmrCtx:
        def __init__(self) -> None:
            self.files = {"title": ctx.clip_dir / "title/title.txt", "srt": ctx.clip_dir / "subs/final.srt"}
            self.evidence = evidence
            self.record: dict[str, object] = {}
            self.adapters = type("A", (), {"tags": None})()

        def text(self, key: str) -> str:
            return self.files[key].read_text(encoding="utf-8")

    return fmr_checks.a10_tags(_FmrCtx(), tags_from=ctx.clip_dir / "tags/tags.json")  # type: ignore[arg-type]


# ----- witness: step 7 had 10 tags, review adds 1 more -> packaging A10 must PASS ---------------------------
def test_review_adding_11th_tag_evicts_trailing_content_tag_and_passes_a10(tmp_path, monkeypatch):
    ctx, _, _ = _package(tmp_path, monkeypatch, _review(), final=_srt([(0, 2, "你好")]), doubts=[])
    _write_step7_tags(ctx, TEN_TAGS, proper=["专名甲"])
    proposed = [*TEN_TAGS, "嗦面"]
    assert len(proposed) == 11

    applied = sp.apply_fixes(ctx, {"fixes": {"tags": proposed}})

    assert applied["tags"] is True
    saved = json.loads((ctx.clip_dir / "tags/tags.json").read_text(encoding="utf-8"))
    final = saved["final_tags"]
    assert len(final) == 10
    assert final == [*BASE, "专名甲", "内容1", "内容2", "内容3", "内容4", "嗦面"]
    assert saved["final_tag_line"] == ",".join(final)
    assert validate_tags(final) == []
    assert ctx.state.data["reviewed_tags"]["kept"] == final
    assert "内容5" in ctx.state.data["reviewed_tags"]["removed"]
    assert any("内容5" in note for note in applied["skipped"])
    # Packaging A10 must pass with the resulting tags.json (witness failed with A10 FAIL 11 tags exceed cap of 10).
    a10 = _run_fmr_a10(ctx)
    assert a10["status"] == "PASS", a10


def test_review_cannot_drop_fixed_base_tags_or_leave_case_insensitive_duplicates(tmp_path, monkeypatch):
    ctx, _, _ = _package(tmp_path, monkeypatch, _review(), final=_srt([(0, 2, "你好")]), doubts=[])
    start = [*BASE, "BanG Dream", "内容1", "内容2", "内容3", "内容4", "内容5"]
    _write_step7_tags(ctx, start, proper=["BanG Dream"])

    # Review drops two base tags, introduces a case-variant duplicate, and adds two new tags.
    proposed = [BASE[0], BASE[1], "BanG Dream", "bang dream", "内容1", "内容2", "内容3", "内容4", "内容5", "新1", "新2"]
    applied = sp.apply_fixes(ctx, {"fixes": {"tags": proposed}})

    assert applied["tags"] is True
    final = json.loads((ctx.clip_dir / "tags/tags.json").read_text(encoding="utf-8"))["final_tags"]
    assert len(final) == 10
    assert final[: len(BASE)] == BASE
    assert "新1" in final and "新2" in final
    assert "BanG Dream" in final and "bang dream" not in final
    assert "内容4" not in final and "内容5" not in final
    assert validate_tags(final) == []
    assert _run_fmr_a10(ctx)["status"] == "PASS"


@pytest.mark.parametrize(
    "bad_tags",
    [
        [*BASE, "带,逗号"],
        [*BASE, "带，中文逗号"],
        [*BASE, "带\n换行"],
        [*BASE, "超" * 21],
        [],
        [*BASE, "   "],
    ],
)
def test_review_malformed_tags_are_refused_with_reason_and_leave_tags_untouched(tmp_path, monkeypatch, bad_tags):
    ctx, _, _ = _package(tmp_path, monkeypatch, _review(), final=_srt([(0, 2, "你好")]), doubts=[])
    _write_step7_tags(ctx, TEN_TAGS, proper=["专名甲"])
    before = (ctx.clip_dir / "tags/tags.json").read_bytes()

    applied = sp.apply_fixes(ctx, {"fixes": {"tags": bad_tags}})

    assert applied["tags"] is False
    assert (ctx.clip_dir / "tags/tags.json").read_bytes() == before
    assert "reviewed_tags" not in ctx.state.data
    assert any("review tags not applied" in line for line in applied["skipped"])


def test_review_tags_on_published_revision_are_refused_with_stated_reason(tmp_path, monkeypatch):
    ctx, _, _ = _package(tmp_path, monkeypatch, _review(), final=_srt([(0, 2, "你好")]), doubts=[])
    _write_step7_tags(ctx, TEN_TAGS, source=step7_tags.ONLINE_SOURCE)
    before = (ctx.clip_dir / "tags/tags.json").read_bytes()

    applied = sp.apply_fixes(ctx, {"fixes": {"tags": [*TEN_TAGS, "嗦面"]}})

    assert applied["tags"] is False
    assert (ctx.clip_dir / "tags/tags.json").read_bytes() == before
    assert "reviewed_tags" not in ctx.state.data
    assert any("published" in line for line in applied["skipped"])


# ----- step 7 re-run after rewind merging reviewed_tags with freshly generated tags -------------------------
def test_step7_rerun_caps_dedups_and_keeps_base_tags_even_with_oversized_or_case_variant_reviewed_tags(tmp_path):
    def fake_tool(name, body, remote):
        if name == "s7-tags":
            generated = [*BASE, "BanG Dream", "内容5", "新生成1", "新生成2"]
            remote.files[f"{RD}/tags/tags.json"] = json.dumps(
                {"status": "OK", "final_tags": generated, "final_tag_line": ",".join(generated)},
                ensure_ascii=False,
            ).encode()
        return RemoteResult(0, "", "")

    ctx = make_ctx(tmp_path, remote=FakeRemote(on_script=fake_tool), cpa=FakeCpa([]))
    (ctx.clip_dir / "title").mkdir(parents=True, exist_ok=True)
    (ctx.clip_dir / "subs").mkdir(parents=True, exist_ok=True)
    (ctx.clip_dir / "title/title.txt").write_text("【李豆沙】标题\n", encoding="utf-8")
    (ctx.clip_dir / "subs/final.srt").write_text(_srt([(0, 2, "你好")]), encoding="utf-8")
    # Legacy/corrupted reviewed_tags: 11 tags, missing one base tag, case-variant duplicate of generated tag.
    ctx.state.put(
        "reviewed_tags",
        {
            "kept": [BASE[0], BASE[1], BASE[2], "bang dream", "保1", "保2", "保3", "保4", "保5", "保6", "超额"],
            "removed": ["内容5"],
        },
    )

    out = si.step7_tags(ctx)

    assert out.ok, out.note
    final = json.loads((ctx.clip_dir / "tags/tags.json").read_text(encoding="utf-8"))["final_tags"]
    assert len(final) == 10
    assert final[: len(BASE)] == BASE
    assert "内容5" not in final
    assert "bang dream" in final and "BanG Dream" not in final
    assert validate_tags(final) == []


def test_step7_fresh_run_rejects_policy_violating_tool_output(tmp_path):
    def bad_tool(name, body, remote):
        if name == "s7-tags":
            too_many = [*TEN_TAGS, "第11个"]
            remote.files[f"{RD}/tags/tags.json"] = json.dumps(
                {"status": "OK", "final_tags": too_many, "final_tag_line": ",".join(too_many)},
                ensure_ascii=False,
            ).encode()
        return RemoteResult(0, "", "")

    ctx = make_ctx(tmp_path, remote=FakeRemote(on_script=bad_tool), cpa=FakeCpa([]))
    (ctx.clip_dir / "title").mkdir(parents=True, exist_ok=True)
    (ctx.clip_dir / "subs").mkdir(parents=True, exist_ok=True)
    (ctx.clip_dir / "title/title.txt").write_text("【李豆沙】标题\n", encoding="utf-8")
    (ctx.clip_dir / "subs/final.srt").write_text(_srt([(0, 2, "你好")]), encoding="utf-8")

    out = si.step7_tags(ctx)

    assert not out.ok and out.reason == "TAGS_POLICY_INVALID"
    assert "exceed the cap of 10" in out.note


# ----- owner edits of published BV (lane_publish metadata-plan) & manifest / audit --------------------------
def test_lane_publish_metadata_plan_enforces_channel_cap_casefold_dedup_and_base_tags(host, tmp_path, capsys):  # noqa: F811
    _pkg, receipt = _first(host, tmp_path, capsys)
    bvid = receipt["bvid"]
    host.archives[bvid]["tag"] = ",".join(TEN_TAGS)
    host._publish(bvid)
    capsys.readouterr()

    def plan_tags(tag_line: str, idx: int) -> tuple[int, str]:
        rc = lp_main(
            [
                "metadata-plan",
                "--bvid",
                bvid,
                "--tags",
                tag_line,
                "--authorized-by",
                "维护者",
                "--quote",
                "加标签",
                "--out",
                str(tmp_path / f"plan-{idx}.json"),
                "--journal",
                str(tmp_path / f"journal-{idx}.jsonl"),
            ]
        )
        return rc, capsys.readouterr().err

    # 11 tags (previously accepted by hardcoded TAGS_MAX=12) must be refused with cap reason.
    rc, err = plan_tags(",".join([*TEN_TAGS, "嗦面"]), 1)
    assert rc == 2 and "exceed the cap of 10" in err

    # Case-insensitive duplicate must be refused.
    rc, err = plan_tags(",".join([*BASE, "BanG Dream", "bang dream"]), 2)
    assert rc == 2 and "duplicates" in err

    # Dropping a live fixed base tag must be refused.
    rc, err = plan_tags(",".join([*BASE[:-1], "内容1", "嗦面"]), 3)
    assert rc == 2 and BASE[-1] in err

    # Chinese comma separator inside a tag must be refused.
    rc, err = plan_tags(",".join([*BASE, "坏，标签"]), 4)
    assert rc == 2 and "separator" in err

    # Valid 10-tag replacement keeping all live base tags succeeds.
    rc, err = plan_tags(",".join([*TEN_TAGS[:-1], "嗦面"]), 5)
    assert rc == 0, err


def test_lane_publish_manifest_and_audit_reject_policy_violating_tags(host, tmp_path, capsys):  # noqa: F811
    pkg = make_publish_pkg(tmp_path / "pkg_bad", extra={"upload_tags": {"final_tags": [*TEN_TAGS, "第11个"]}})
    aud = lp_audit.audit(pkg)
    assert aud["passed"] is False
    assert any("exceed the cap of 10" in issue for issue in aud["issues"])

    pkg_ok = make_publish_pkg(tmp_path / "pkg_ok", extra={"upload_tags": {"final_tags": TEN_TAGS}})
    audit_path = tmp_path / "audit.json"
    audit_path.write_text(json.dumps(lp_audit.audit(pkg_ok), ensure_ascii=False), encoding="utf-8")
    with pytest.raises(lp_manifest.Refused) as exc_info:
        lp_manifest.make(
            video=pkg_ok / f"{pkg_ok.name}.mp4",
            cover=pkg_ok / f"{pkg_ok.name}.cover.png",
            package_audit=audit_path,
            title=TITLE,
            tags=",".join([*TEN_TAGS, "第11个"]),
            final_human_review=None,
            authorized_by="维护者",
            quote="ok",
            out=tmp_path / "manifest.json",
        )
    assert "exceed the cap of 10" in str(exc_info.value)

    # Dropping a fixed base tag that is present in record.json must also be refused.
    with pytest.raises(lp_manifest.Refused) as base_exc:
        lp_manifest.make(
            video=pkg_ok / f"{pkg_ok.name}.mp4",
            cover=pkg_ok / f"{pkg_ok.name}.cover.png",
            package_audit=audit_path,
            title=TITLE,
            tags=",".join([*BASE[:-1], "内容1"]),
            final_human_review=None,
            authorized_by="维护者",
            quote="ok",
            out=tmp_path / "manifest-missing-base.json",
        )
    assert BASE[-1] in str(base_exc.value)


# ----- direct unit test of shared apply_tag_policy ----------------------------------------------------------
def test_shared_apply_tag_policy_respects_custom_policy_and_deterministic_tiers():
    from src.autoslice.upload_tag_policy import apply_tag_policy

    base_policy = load_selected_upload_tag_policy()
    custom = replace(base_policy, base_tags=("主播A", "切片"), max_tags_default=5, max_dynamic_tags=3, max_tag_chars=8)

    prev = ["主播A", "切片", "专名1", "旧内容1", "旧内容2"]
    # Add 2 new tags -> 旧内容2 and 旧内容1 give way (proper noun 专名1 kept, base kept).
    res = apply_tag_policy(
        [*prev, "新1", "新2"],
        previous=prev,
        priority_tags=["专名1"],
        policy=custom,
        on_overflow="evict",
        prefer_new=True,
    )
    assert res.ok is True
    assert res.tags == ("主播A", "切片", "专名1", "新1", "新2")
    assert res.dropped == ("旧内容1", "旧内容2")
    assert validate_tags(list(res.tags), policy=custom) == []

    # Same input in refuse mode fails with cap reason.
    refused = apply_tag_policy([*prev, "新1"], previous=prev, policy=custom, on_overflow="refuse")
    assert refused.ok is False and any("exceed the cap of 5" in r for r in refused.reasons)

    # Finding 7: proper-noun priority tag is evicted ONLY when no general tag remains.
    all_prio = ["主播A", "切片", "专名1", "专名2", "专名3"]
    prio_overflow = apply_tag_policy(
        [*all_prio, "专名4"],
        previous=all_prio,
        priority_tags=["专名1", "专名2", "专名3", "专名4"],
        policy=custom,
        on_overflow="evict",
        prefer_new=True,
    )
    assert prio_overflow.ok is True
    assert prio_overflow.tags == ("主播A", "切片", "专名1", "专名2", "专名4")
    assert prio_overflow.dropped == ("专名3",)


# ----- Finding 7: unified eviction order & recording across both merge modes --------------------------------
def test_step7_rerun_merge_keeps_priority_proper_noun_over_general_tags_and_records_eviction(tmp_path):
    reviewed = [*BASE, *(f"内容{i}" for i in range(6))]
    generated = [*BASE, "真实专名"]

    def fake_tool(name, body, remote):
        if name == "s7-tags":
            remote.files[f"{RD}/tags/tags.json"] = json.dumps(
                {
                    "status": "OK",
                    "final_tags": generated,
                    "final_tag_line": ",".join(generated),
                    "proper_noun_tags": [{"tag": "真实专名"}],
                },
                ensure_ascii=False,
            ).encode()
        return RemoteResult(0, "", "")

    ctx = make_ctx(tmp_path, remote=FakeRemote(on_script=fake_tool), cpa=FakeCpa([]))
    (ctx.clip_dir / "title").mkdir(parents=True, exist_ok=True)
    (ctx.clip_dir / "subs").mkdir(parents=True, exist_ok=True)
    (ctx.clip_dir / "title/title.txt").write_text("【李豆沙】标题\n", encoding="utf-8")
    (ctx.clip_dir / "subs/final.srt").write_text(_srt([(0, 2, "你好")]), encoding="utf-8")
    ctx.state.put("reviewed_tags", {"kept": reviewed, "removed": []})

    out = si.step7_tags(ctx)

    assert out.ok, out.note
    saved = json.loads((ctx.clip_dir / "tags/tags.json").read_text(encoding="utf-8"))
    assert saved["final_tags"] == [*BASE, "内容0", "内容1", "内容2", "内容3", "内容4", "真实专名"]
    assert "内容5" in ctx.state.data["reviewed_tags"]["removed"]
    assert ctx.state.data["reviewed_tags"]["kept"] == saved["final_tags"]
    assert any("内容5" in line for line in out.process)
    assert validate_tags(saved["final_tags"]) == []


def test_step7_rerun_merge_reviewed_general_tags_beat_regenerated_general_tags_and_record_eviction(tmp_path):
    reviewed = [*BASE, *(f"保{i}" for i in range(6))]
    generated = [*BASE, "重算普通"]

    def fake_tool(name, body, remote):
        if name == "s7-tags":
            remote.files[f"{RD}/tags/tags.json"] = json.dumps(
                {"status": "OK", "final_tags": generated, "final_tag_line": ",".join(generated)},
                ensure_ascii=False,
            ).encode()
        return RemoteResult(0, "", "")

    ctx = make_ctx(tmp_path, remote=FakeRemote(on_script=fake_tool), cpa=FakeCpa([]))
    (ctx.clip_dir / "title").mkdir(parents=True, exist_ok=True)
    (ctx.clip_dir / "subs").mkdir(parents=True, exist_ok=True)
    (ctx.clip_dir / "title/title.txt").write_text("【李豆沙】标题\n", encoding="utf-8")
    (ctx.clip_dir / "subs/final.srt").write_text(_srt([(0, 2, "你好")]), encoding="utf-8")
    ctx.state.put("reviewed_tags", {"kept": reviewed, "removed": []})

    out = si.step7_tags(ctx)

    assert out.ok, out.note
    saved = json.loads((ctx.clip_dir / "tags/tags.json").read_text(encoding="utf-8"))
    assert saved["final_tags"] == reviewed
    assert "重算普通" in ctx.state.data["reviewed_tags"]["removed"]
    assert any("重算普通" in line for line in out.process)


def test_review_merge_keeps_priority_proper_noun_when_new_general_tags_overflow(tmp_path, monkeypatch):
    ctx, _, _ = _package(tmp_path, monkeypatch, _review(), final=_srt([(0, 2, "你好")]), doubts=[])
    previous = [*BASE, "专名甲", *(f"旧内容{i}" for i in range(5))]
    _write_step7_tags(ctx, previous, proper=["专名甲"])
    proposed = [*previous, *(f"新内容{i}" for i in range(6))]

    applied = sp.apply_fixes(ctx, {"fixes": {"tags": proposed}})

    assert applied["tags"] is True
    final = json.loads((ctx.clip_dir / "tags/tags.json").read_text(encoding="utf-8"))["final_tags"]
    assert len(final) == 10
    assert final == [*BASE, "专名甲", "新内容0", "新内容1", "新内容2", "新内容3", "新内容4"]
    assert "专名甲" not in ctx.state.data["reviewed_tags"]["removed"]
    for dropped in (*(f"旧内容{i}" for i in range(5)), "新内容5"):
        assert dropped in ctx.state.data["reviewed_tags"]["removed"]
    assert any("新内容5" in line for line in applied["skipped"])
    assert validate_tags(final) == []


# ----- Finding 8: manifest protects each fixed base tag present in record individually ----------------------
def test_lane_publish_manifest_protects_partial_fixed_base_tags_individually(host, tmp_path):  # noqa: F811
    pkg = make_publish_pkg(tmp_path / "pkg_partial", extra={"upload_tags": {"final_tags": [BASE[0], "内容甲"]}})
    report = lp_audit.audit(pkg)
    assert report["passed"]
    audit_path = tmp_path / "audit.json"
    audit_path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(lp_manifest.Refused) as exc_info:
        lp_manifest.make(
            video=pkg / f"{pkg.name}.mp4",
            cover=pkg / f"{pkg.name}.cover.png",
            package_audit=audit_path,
            title=TITLE,
            tags="内容乙",
            final_human_review=None,
            authorized_by="维护者",
            quote="授权",
            out=tmp_path / "manifest-drop-partial-base.json",
        )
    assert BASE[0] in str(exc_info.value)

    kept = lp_manifest.make(
        video=pkg / f"{pkg.name}.mp4",
        cover=pkg / f"{pkg.name}.cover.png",
        package_audit=audit_path,
        title=TITLE,
        tags=f"{BASE[0]},内容乙",
        final_human_review=None,
        authorized_by="维护者",
        quote="授权",
        out=tmp_path / "manifest-keep-partial-base.json",
    )
    assert kept["tags"] == [BASE[0], "内容乙"]


# ----- Finding 9: raw generator final_tags validated before any merge (never coerced) -----------------------
@pytest.mark.parametrize("bad_final_tags", ["猫狗兔", [*BASE, 123], [*BASE, "   "], [], None])
def test_step7_rejects_bad_generated_final_tags_before_merge(tmp_path, bad_final_tags):
    def fake_tool(name, body, remote):
        if name == "s7-tags":
            remote.files[f"{RD}/tags/tags.json"] = json.dumps(
                {"status": "OK", "final_tags": bad_final_tags},
                ensure_ascii=False,
            ).encode()
        return RemoteResult(0, "", "")

    ctx = make_ctx(tmp_path, remote=FakeRemote(on_script=fake_tool), cpa=FakeCpa([]))
    (ctx.clip_dir / "title").mkdir(parents=True, exist_ok=True)
    (ctx.clip_dir / "subs").mkdir(parents=True, exist_ok=True)
    (ctx.clip_dir / "title/title.txt").write_text("【李豆沙】标题\n", encoding="utf-8")
    (ctx.clip_dir / "subs/final.srt").write_text(_srt([(0, 2, "你好")]), encoding="utf-8")
    ctx.state.put("reviewed_tags", {"kept": list(BASE), "removed": []})

    out = si.step7_tags(ctx)

    assert not out.ok and out.reason == "TAGS_POLICY_INVALID"
    assert "generated final_tags" in out.note


# ----- Finding 10: metadata-plan keeps unchanged legacy tags, validates when changed ------------------------
def test_lane_publish_metadata_plan_keeps_unchanged_legacy_tags_and_validates_changed_tags(host, tmp_path, capsys):  # noqa: F811
    from src.autoslice.lane_publish import metadata as lp_metadata

    _pkg, receipt = _first(host, tmp_path, capsys)
    bvid = receipt["bvid"]
    live = [*BASE, *(f"内容{i}" for i in range(7))]  # 11 legacy tags
    host.archives[bvid]["tag"] = ",".join(live)
    host._publish(bvid)
    capsys.readouterr()

    common = dict(
        bvid=bvid,
        cid=None,
        title="【主播】新标题",
        desc_file=None,
        fix_source_line=False,
        authorized_by="维护者",
        quote="只改标题",
        out=tmp_path / "plan.json",
        journal=tmp_path / "journal.jsonl",
        dry_run=True,
    )
    omitted = lp_metadata.plan(tags=None, **common)
    assert omitted["target"]["tags"] == live

    explicit_same = lp_metadata.plan(tags=",".join(live), **common)
    assert explicit_same["target"]["tags"] == live

    with pytest.raises(lp_metadata.Refused) as changed_exc:
        lp_metadata.plan(tags=",".join([*live[:-1], "新内容"]), **common)
    assert "exceed the cap of 10" in str(changed_exc.value)


# ----- Finding 13: missing or null final_tags in tags.json refused cleanly in skipped -----------------------
@pytest.mark.parametrize("payload", [{"final_tags": None}, {}, {"final_tags": "猫狗"}, {"final_tags": []}])
def test_review_refuses_missing_or_invalid_final_tags_in_tags_json_without_crashing(tmp_path, monkeypatch, payload):
    ctx, _, _ = _package(tmp_path, monkeypatch, _review(), final=_srt([(0, 2, "你好")]), doubts=[])
    path = ctx.clip_dir / "tags/tags.json"
    raw = json.dumps(payload, ensure_ascii=False)
    path.write_text(raw, encoding="utf-8")

    applied = sp.apply_fixes(ctx, {"fixes": {"tags": [*BASE, "内容甲"]}})

    assert applied["tags"] is False
    assert path.read_text(encoding="utf-8") == raw
    assert "reviewed_tags" not in ctx.state.data
    assert any("review tags not applied" in line for line in applied["skipped"])


# ----- Round 3 regressions: Points 7, 9, 10, 13, and lane_publish.audit published-source exemption ----------
def test_step7_rerun_merge_preserves_prior_proper_noun_identity_from_reviewed_tags(tmp_path, monkeypatch):
    """Point 7: proper-noun identity persists in reviewed_tags so step-7 re-run evicts general tags before old proper nouns."""
    ctx, _, _ = _package(tmp_path, monkeypatch, _review(), final=_srt([(0, 2, "你好")]), doubts=[])
    old_tags = [*BASE[:3], *(f"旧普通{i}" for i in range(6)), "旧专名"]
    _write_step7_tags(ctx, old_tags, proper=["旧专名"])

    # Review keeps tags (tags=None -> stores kept + proper_noun_tags in reviewed_tags).
    sp.apply_fixes(ctx, {"fixes": {"tags": None}})
    assert ctx.state.data["reviewed_tags"]["proper_noun_tags"] == ["旧专名"]

    # Step 7 re-run generates all 4 base tags but no longer flags 旧专名 in proper_noun_tags.
    def fake_tool(name, body, remote):
        if name == "s7-tags":
            remote.files[f"{RD}/tags/tags.json"] = json.dumps(
                {"status": "OK", "final_tags": list(BASE), "final_tag_line": ",".join(BASE), "proper_noun_tags": []},
                ensure_ascii=False,
            ).encode()
        return RemoteResult(0, "", "")

    ctx.adapters.remote.on_script = fake_tool
    out = si.step7_tags(ctx)

    assert out.ok, out.note
    saved = json.loads((ctx.clip_dir / "tags/tags.json").read_text(encoding="utf-8"))
    assert len(saved["final_tags"]) == 10
    assert "旧专名" in saved["final_tags"]
    assert "旧普通5" not in saved["final_tags"]
    assert "旧普通5" in ctx.state.data["reviewed_tags"]["removed"]
    assert ctx.state.data["reviewed_tags"]["proper_noun_tags"] == ["旧专名"]
    assert any("旧普通5" in line for line in out.process)


@pytest.mark.parametrize("bad_tag", ["不合法,标签", "不合法，标签", "超" * 21])
def test_step7_validates_raw_generated_tag_content_before_removed_filtering(tmp_path, bad_tag):
    """Point 9: invalid raw generator items fail before reviewed_tags.removed filters them out."""
    def fake_tool(name, body, remote):
        if name == "s7-tags":
            generated = [*BASE, bad_tag]
            remote.files[f"{RD}/tags/tags.json"] = json.dumps(
                {"status": "OK", "final_tags": generated, "final_tag_line": ",".join(generated)},
                ensure_ascii=False,
            ).encode()
        return RemoteResult(0, "", "")

    ctx = make_ctx(tmp_path, remote=FakeRemote(on_script=fake_tool), cpa=FakeCpa([]))
    (ctx.clip_dir / "title").mkdir(parents=True, exist_ok=True)
    (ctx.clip_dir / "subs").mkdir(parents=True, exist_ok=True)
    (ctx.clip_dir / "title/title.txt").write_text("【李豆沙】标题\n", encoding="utf-8")
    (ctx.clip_dir / "subs/final.srt").write_text(_srt([(0, 2, "你好")]), encoding="utf-8")
    ctx.state.put("reviewed_tags", {"kept": list(BASE), "removed": [bad_tag]})

    out = si.step7_tags(ctx)

    assert not out.ok and out.reason == "TAGS_POLICY_INVALID"
    assert "generated final_tags invalid" in out.note


def test_lane_publish_metadata_plan_treats_comma_space_live_tags_as_identical(host, tmp_path, capsys):  # noqa: F811
    """Point 10: live tag strings with spaces after commas match the stripped parsed list and skip re-validation."""
    from src.autoslice.lane_publish import metadata as lp_metadata

    _pkg, receipt = _first(host, tmp_path, capsys)
    bvid = receipt["bvid"]
    live = [*BASE, *(f"内容{i}" for i in range(7))]  # 11 legacy tags
    spaced_live = ", ".join(live)
    host.archives[bvid]["tag"] = spaced_live
    host._publish(bvid)
    capsys.readouterr()

    plan = lp_metadata.plan(
        bvid=bvid,
        cid=None,
        title="【主播】新标题",
        tags=spaced_live,
        desc_file=None,
        fix_source_line=False,
        authorized_by="维护者",
        quote="只改标题",
        out=tmp_path / "plan-spaced.json",
        journal=tmp_path / "journal-spaced.jsonl",
        dry_run=True,
    )
    assert plan["target"]["tags"] == live


@pytest.mark.parametrize("review_tags", [[*BASE, "内容甲"], None])
@pytest.mark.parametrize("bad_stored", [None, "MISSING", "猫狗", []])
def test_review_refuses_published_source_with_missing_or_invalid_final_tags_stating_invalid_data(
    tmp_path, monkeypatch, review_tags, bad_stored
):
    """Point 13: source=published with missing/null/invalid final_tags reports the invalid data in skipped."""
    ctx, _, _ = _package(tmp_path, monkeypatch, _review(), final=_srt([(0, 2, "你好")]), doubts=[])
    path = ctx.clip_dir / "tags/tags.json"
    doc: dict[str, object] = {"source": step7_tags.ONLINE_SOURCE}
    if bad_stored != "MISSING":
        doc["final_tags"] = bad_stored
    raw = json.dumps(doc, ensure_ascii=False)
    path.write_text(raw, encoding="utf-8")

    applied = sp.apply_fixes(ctx, {"fixes": {"tags": review_tags}})

    assert applied["tags"] is False
    assert path.read_text(encoding="utf-8") == raw
    assert "reviewed_tags" not in ctx.state.data
    assert any("final_tags is missing or invalid" in line for line in applied["skipped"])


def test_lane_publish_audit_exempts_published_revision_legacy_tags_but_still_rejects_new_overflow(host, tmp_path):  # noqa: F811
    """Audit exemption: a same-manuscript revision keeping 11 published online tags passes audit; new clips still fail."""
    legacy_11 = [*BASE, *(f"线上旧词{i}" for i in range(7))]
    rev_pkg = make_publish_pkg(
        tmp_path / "pkg_rev_legacy",
        extra={"upload_tags": {"status": "OK", "source": step7_tags.ONLINE_SOURCE, "final_tags": legacy_11}},
    )
    rev_audit = lp_audit.audit(rev_pkg)
    assert rev_audit["passed"] is True, rev_audit["issues"]
    assert rev_audit["issues"] == []

    # Same 11 tags without source=published (a first publication) must still fail audit.
    new_pkg = make_publish_pkg(
        tmp_path / "pkg_new_overflow",
        extra={"upload_tags": {"status": "OK", "final_tags": legacy_11}},
    )
    new_audit = lp_audit.audit(new_pkg)
    assert new_audit["passed"] is False
    assert any("exceed the cap of 10" in issue for issue in new_audit["issues"])


# ----- Round 4 regression: old-format reviewed_tags (no proper_noun_tags) migrates from old tags.json ------
def test_step7_rerun_migrates_proper_noun_tags_from_old_tags_json_for_legacy_reviewed_tags(tmp_path):
    """Item 6 / probe 7: pre-upgrade reviewed_tags (kept/removed only) reads proper_noun_tags from old tags.json before overwrite."""
    old = [*BASE[:3], *(f"旧普通{i}" for i in range(6)), "旧专名"]

    def fake_tool(name, body, remote):
        if name == "s7-tags":
            remote.files[f"{RD}/tags/tags.json"] = json.dumps(
                {"status": "OK", "final_tags": list(BASE), "proper_noun_tags": []},
                ensure_ascii=False,
            ).encode()
        return RemoteResult(0, "", "")

    ctx = make_ctx(tmp_path, remote=FakeRemote(on_script=fake_tool), cpa=FakeCpa([]))
    for rel, content in (
        ("title/title.txt", "【李豆沙】标题\n"),
        ("subs/final.srt", _srt([(0, 2, "你好")])),
        (
            "tags/tags.json",
            json.dumps(
                {"status": "OK", "final_tags": old, "proper_noun_tags": [{"tag": "旧专名"}]},
                ensure_ascii=False,
            ),
        ),
    ):
        file = ctx.clip_dir / rel
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(content, encoding="utf-8")

    # Old-format reviewed_tags: no proper_noun_tags key.
    ctx.state.put("reviewed_tags", {"kept": old, "removed": []})

    out = step7_tags.step7_tags(ctx)

    assert out.ok, out.note
    final = json.loads((ctx.clip_dir / "tags/tags.json").read_text(encoding="utf-8"))["final_tags"]
    assert len(final) == 10
    assert final[: len(BASE)] == BASE
    assert "旧专名" in final
    assert "旧专名" not in ctx.state.data["reviewed_tags"]["removed"]
    assert "旧普通5" not in final
    assert "旧普通5" in ctx.state.data["reviewed_tags"]["removed"]
    assert ctx.state.data["reviewed_tags"]["proper_noun_tags"] == ["旧专名"]
    assert any("旧普通5" in line for line in out.process)
    assert validate_tags(final) == []
