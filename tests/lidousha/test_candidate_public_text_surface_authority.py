from __future__ import annotations

import copy
import hashlib
import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

import scripts.free_session_autoslice as runner
import scripts.repair_reviewed_covers as reviewed_covers
import src.autoslice.candidate_public_text_surface_authority as public_text_authority
import src.autoslice.manual_title_source_fact_successor as title_successor
import src.autoslice.review_package_title_audit as title_audit
from src.autoslice import publish_staging, source_fact_staging
from src.autoslice.candidate_public_text_surface_authority import (
    ALGORITHM_ID,
    AUTHORITY_DIRECTORY,
    AUTHORITY_SUFFIX,
    CandidatePublicTextSurfaceAuthorityError,
    consume_candidate_public_text_surface_authority,
    load_candidate_public_text_surface_authority,
)
from src.autoslice.publication_title_exception import candidate_title_policy_violations
from src.autoslice.repository_asset_authority import (
    DEPLOYED_AUTHORITY_MANIFEST,
    build_deployed_authority_manifest,
    repository_authority_expects_asset,
    require_repository_asset_authority,
)
from src.autoslice.review_package_title_audit import audit_candidate_public_text_surfaces
from src.autoslice.source_fact_review import (
    review_and_repair_source_facts,
    source_fact_review_passes,
    validate_source_fact_review,
)
from src.autoslice.title_policy import TitlePolicyError, validate_candidate_title_surface
from src.autoslice.title_policy import build_automatic_talk_title_prompt
from src.autoslice.story_contract import build_story_contract


CID = "auto_210739_1142_1436"
MANUAL_TITLE_CID = "auto_173005_934_1166"
SOURCE_SHA = "sha256:8069353813e0cd12624b1cdcdd419356527098b353e08f71fa4bb64f5f6046c4"
CONTEXT_SHA = "sha256:8c1b85367bfba92302fdcd12aa1400811f34651ad9573f67208f6399a4dc474d"
OLD_HOOK = (
    "没人肯跟她抱团，她当场认南天为大哥宣誓效忠，结果亲眼看着大哥暴毙后立刻以小弟身份办起丧事。"
)
NEW_HOOK = (
    "没人肯跟她抱团，她当场认南町为大哥宣誓效忠，结果亲眼看着大哥暴毙后立刻以小弟身份办起丧事。"
)
OLD_TITLE = (
    "【李豆沙】想抱团却被拒绝，小李认南天为大哥：“你说啥就是啥”，目睹大哥死后小弟立马办起丧事"
)
NEW_TITLE = (
    "【李豆沙】想抱团却被拒绝，小李认南町为大哥：“你说啥就是啥”，目睹大哥死后小弟立马办起丧事"
)
PIECES = [
    {
        "recording_basename": "22966160_20260807-21-07-39.mp4",
        "source_media_sha256": SOURCE_SHA,
        "start_ms": 1_132_390,
        "end_ms": 1_484_990,
    }
]


@pytest.fixture(autouse=True)
def _allow_precommit_public_text_authority(monkeypatch: pytest.MonkeyPatch) -> None:
    """Parse/integration tests isolate the repository seal covered below."""

    monkeypatch.setattr(
        public_text_authority,
        "repository_authority_expects_asset",
        lambda **_kwargs: True,
    )
    monkeypatch.setattr(
        public_text_authority,
        "require_repository_asset_authority",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        title_successor,
        "require_repository_asset_authority",
        lambda **_kwargs: None,
    )


def _sha256_json(value: object) -> str:
    return (
        "sha256:"
        + hashlib.sha256(
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )


def _prompt(*, hook: str = OLD_HOOK, pieces: object = PIECES) -> str:
    return "\n".join(
        [
            f"clip_context_sha256: {CONTEXT_SHA}",
            f"candidate_id: {CID}",
            "recording_date: 2026-08-07",
            f"selection_hook: {hook}",
            "source_pieces: "
            + json.dumps(pieces, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            "整片初稿（不是文字 authority）：",
            "大白老师仍是未裁定的字幕 cue，本 authority 不应读取或改写它。",
        ]
    )


def _contract(*, hook: str = OLD_HOOK, prompt: str | None = None) -> dict[str, object]:
    return {
        "candidate_id": CID,
        "selection_hook": hook,
        "source_media_sha256s": [SOURCE_SHA],
        "clip_context_binding": {"context_sha256": CONTEXT_SHA},
        "clip_context_prompt": prompt if prompt is not None else _prompt(),
    }


def _asset_path(root: Path) -> Path:
    return root / "assets" / "lidousha" / AUTHORITY_DIRECTORY / f"{CID}{AUTHORITY_SUFFIX}"


def _copy_asset(root: Path) -> Path:
    source = _asset_path(Path(__file__).resolve().parents[2])
    target = _asset_path(root)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(source.read_bytes())
    return target


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _init_git_repository(root: Path) -> None:
    root.mkdir()
    _git(root, "init", "--quiet")
    _git(root, "config", "user.name", "Public Text Authority Test")
    _git(root, "config", "user.email", "public-text-authority@example.invalid")
    _git(root, "commit", "--quiet", "--allow-empty", "-m", "initial")


def _reseal(document: dict[str, object]) -> None:
    body = dict(document)
    body.pop("authority_sha256", None)
    document["authority_sha256"] = _sha256_json(body)


def test_repository_seal_requires_exact_committed_or_deployed_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        public_text_authority,
        "repository_authority_expects_asset",
        repository_authority_expects_asset,
    )
    monkeypatch.setattr(
        public_text_authority,
        "require_repository_asset_authority",
        require_repository_asset_authority,
    )
    relative = Path("assets/lidousha") / AUTHORITY_DIRECTORY / f"{CID}{AUTHORITY_SUFFIX}"

    git_root = tmp_path / "git-repo"
    _init_git_repository(git_root)
    git_asset = _copy_asset(git_root)
    assert load_candidate_public_text_surface_authority(CID, root=git_root) is None
    _git(git_root, "add", relative.as_posix())
    _git(git_root, "commit", "--quiet", "-m", "seal public text authority")
    assert load_candidate_public_text_surface_authority(CID, root=git_root) is not None
    git_asset.write_bytes(git_asset.read_bytes() + b"\n")
    with pytest.raises(CandidatePublicTextSurfaceAuthorityError, match="UNSEALED"):
        load_candidate_public_text_surface_authority(CID, root=git_root)

    deployed_root = tmp_path / "deployed"
    deployed_root.mkdir()
    deployed_asset = _copy_asset(deployed_root)
    deployed_commit = "a" * 40
    (deployed_root / "DEPLOYED_COMMIT").write_text(deployed_commit + "\n", encoding="utf-8")
    manifest = build_deployed_authority_manifest(
        repo_root=deployed_root,
        deployed_commit=deployed_commit,
        relative_paths=[relative],
    )
    (deployed_root / DEPLOYED_AUTHORITY_MANIFEST).write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    assert load_candidate_public_text_surface_authority(CID, root=deployed_root) is not None
    deployed_asset.write_bytes(deployed_asset.read_bytes() + b"\n")
    with pytest.raises(CandidatePublicTextSurfaceAuthorityError, match="UNSEALED"):
        load_candidate_public_text_surface_authority(CID, root=deployed_root)


def test_real_authority_is_exact_public_only_and_does_not_claim_cue19() -> None:
    authority = load_candidate_public_text_surface_authority(CID)
    assert authority is not None
    assert authority.resolved_selection_hook == NEW_HOOK
    assert authority.resolved_title == NEW_TITLE
    assert authority.equivalent_surfaces == ("南町", "南天")
    assert "大白老师" not in _asset_path(Path(__file__).resolve().parents[2]).read_text()
    authority.require_artifact_text(artifact_kind="title", text=NEW_TITLE)
    authority.require_artifact_text(artifact_kind="cover", text="认南町为大哥")
    authority.require_artifact_text(artifact_kind="cover", text="认大哥")
    with pytest.raises(CandidatePublicTextSurfaceAuthorityError, match="FORBIDDEN"):
        authority.require_artifact_text(artifact_kind="cover", text="认南天为大哥")
    with pytest.raises(CandidatePublicTextSurfaceAuthorityError, match="EXACT_TITLE"):
        authority.require_artifact_text(artifact_kind="publication", text="【李豆沙】南町")
    with pytest.raises(CandidatePublicTextSurfaceAuthorityError, match="OUT_OF_SCOPE"):
        authority.require_artifact_text(artifact_kind="subtitle", text="南天\n大白老师")


def test_manual_title_authority_is_exact_and_never_becomes_subtitle_or_upload_authority() -> None:
    authority = load_candidate_public_text_surface_authority(MANUAL_TITLE_CID)
    assert authority is not None
    assert authority.is_manual_title_resolution
    assert authority.title_source == (
        "deterministic_candidate_manual_title_resolution+ivan_exact_title"
    )
    assert authority.resolved_selection_hook == authority.input_selection_hook
    assert authority.resolved_title == "【李豆沙】经小李判断，薇欧拉对阿拉蕾就是铁暗恋！"
    assert authority.user_authorization == {
        "quote": "这条切片标题改为“经小李判断，薇欧拉对阿拉蕾就是铁暗恋！”我记得之前应该明确说过上头这种词汇没有任何信息量，不能放在标题里，你再看看之前那些说的起标题和绘制封面的skill怎么搞的？其他没有问题。",
        "timestamp": "2026-08-19T00:08:52.249Z",
    }
    authority.require_artifact_text(artifact_kind="title", text=authority.resolved_title)
    authority.require_artifact_text(artifact_kind="cover", text="薇欧拉对阿拉蕾\n就是铁暗恋！")
    with pytest.raises(CandidatePublicTextSurfaceAuthorityError, match="FORBIDDEN_ENTITY_SURFACE"):
        authority.require_artifact_text(artifact_kind="publication", text=authority.superseded_title)
    with pytest.raises(CandidatePublicTextSurfaceAuthorityError, match="OUT_OF_SCOPE"):
        authority.require_artifact_text(artifact_kind="subtitle", text="铁暗恋")


def test_real_manual_title_successor_asset_is_exactly_sealed_to_the_refresh_chain() -> None:
    binding = title_successor._load_successor_authority(repo_root=Path(__file__).resolve().parents[2])
    assert binding["public_text_authority"]["authority_sha256"] == (
        "sha256:001c8aab4581db7a6875f293ea2a597fcdf633bffdb5b097d6fbc8edeb1bc192"
    )
    assert binding["source_fact_refresh_authority"]["authority_sha256"] == (
        "sha256:940df85848a0e6675aca760b51c7086962974c09132b9555a307c87c505c618f"
    )
    assert binding["predecessor"]["story_contract_sha256"] == (
        "sha256:d465a06cc1e0ec8a8dd3d325639689a20ddd5ef4b6ff4f715c95a40e739863ef"
    )
    assert binding["successor"] == {
        "story_contract_sha256": "sha256:40556530460502238105924764cbdbbf0516d3b7f4a04c7e2c361b2f3a6e2df6",
        "source_fact_review_sha256": "sha256:c5ec3a3d68411c0db786dc6006bb765cf15227736a6842f3f89dba7c95e3dfb8",
    }
    assert binding["allowed_json_pointers"] == ["/source_fact_review"]


def test_consumption_binds_original_prompt_source_and_exact_selected_interval() -> None:
    authority = load_candidate_public_text_surface_authority(CID)
    assert authority is not None
    receipt = consume_candidate_public_text_surface_authority(
        authority,
        candidate_id=CID,
        selection_hook=OLD_HOOK,
        story_contract=_contract(),
    )
    assert receipt["selected_interval"] == {
        "absolute_start_ms": 1_142_160,
        "absolute_end_ms": 1_437_390,
    }
    assert authority.source_pieces[0]["start_ms"] == 1_132_390
    assert authority.source_pieces[0]["end_ms"] == 1_484_990
    assert receipt["subtitle_text_mutation_authorized"] is False
    assert receipt["speaker_label_mutation_authorized"] is False
    assert receipt["upload_authorized"] is False
    assert receipt["registry_hold_released"] is False
    with pytest.raises(CandidatePublicTextSurfaceAuthorityError, match="BINDING"):
        consume_candidate_public_text_surface_authority(
            authority,
            candidate_id=CID,
            selection_hook=OLD_HOOK + "漂移",
            story_contract=_contract(hook=OLD_HOOK + "漂移"),
        )
    with pytest.raises(CandidatePublicTextSurfaceAuthorityError, match="CLIP_CONTEXT"):
        consume_candidate_public_text_surface_authority(
            authority,
            candidate_id=CID,
            selection_hook=OLD_HOOK,
            story_contract=_contract(prompt=_prompt(pieces=[])),
        )


def test_missing_malformed_symlink_and_self_hash_tamper_fail_explicitly(
    tmp_path: Path,
) -> None:
    with pytest.raises(CandidatePublicTextSurfaceAuthorityError, match="REQUIRED_BUT_MISSING"):
        load_candidate_public_text_surface_authority(CID, root=tmp_path)
    path = _asset_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("{broken", encoding="utf-8")
    with pytest.raises(CandidatePublicTextSurfaceAuthorityError, match="UNREADABLE"):
        load_candidate_public_text_surface_authority(CID, root=tmp_path)
    path.unlink()
    path.symlink_to(_asset_path(Path(__file__).resolve().parents[2]))
    with pytest.raises(CandidatePublicTextSurfaceAuthorityError, match="FILE_INVALID"):
        load_candidate_public_text_surface_authority(CID, root=tmp_path)
    path.unlink()
    _copy_asset(tmp_path)
    document = json.loads(path.read_text(encoding="utf-8"))
    document["resolved_surfaces"]["title"] += "篡改"
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(CandidatePublicTextSurfaceAuthorityError, match="HASH_MISMATCH"):
        load_candidate_public_text_surface_authority(CID, root=tmp_path)


def test_algorithm_canary_and_selected_interval_are_fail_closed(tmp_path: Path) -> None:
    path = _copy_asset(tmp_path)
    document = json.loads(path.read_text(encoding="utf-8"))
    document["algorithm_id"] = ALGORITHM_ID + ".changed-without-revision"
    _reseal(document)
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(CandidatePublicTextSurfaceAuthorityError, match="SCHEMA_INVALID"):
        load_candidate_public_text_surface_authority(CID, root=tmp_path)

    document["algorithm_id"] = ALGORITHM_ID
    document["candidate_binding"]["selected_interval"] = {
        "absolute_start_ms": 1_000,
        "absolute_end_ms": 2_000,
    }
    _reseal(document)
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(CandidatePublicTextSurfaceAuthorityError, match="OUTSIDE_CONTEXT"):
        load_candidate_public_text_surface_authority(CID, root=tmp_path)


def test_unrelated_candidate_and_ordinary_nantian_phrase_are_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_if_repository_is_probed(**_kwargs: object) -> bool:
        raise AssertionError("an unrelated absent candidate must not invoke repository tooling")

    monkeypatch.setattr(
        public_text_authority,
        "repository_authority_expects_asset",
        fail_if_repository_is_probed,
    )
    assert load_candidate_public_text_surface_authority("auto_unrelated", root=tmp_path) is None
    text = "今天聊南天门的历史"
    assert text == "今天聊南天门的历史"


def test_loader_withholds_truth_and_rejects_unknown_truth_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTOSLICE_HUMAN_TRUTH_MODE", "withheld")
    assert load_candidate_public_text_surface_authority(CID) is None
    monkeypatch.setenv("AUTOSLICE_HUMAN_TRUTH_MODE", "typo")
    with pytest.raises(CandidatePublicTextSurfaceAuthorityError, match="TRUTH_MODE_INVALID"):
        load_candidate_public_text_surface_authority(CID)


def test_extracted_prompt_is_byte_equivalent_to_the_previous_inline_contract() -> None:
    hook = "主播拒绝了一次具体请求。"
    transcript = "完整最终字幕样本"
    ip = "内容提及的重要 IP：测试\n"
    context = "clip_context_sha256: sha256:test"
    persona = "PERSONA_SENTINEL"
    style = "STYLE_SENTINEL"
    hook_clause = hook.split("。", 1)[0].strip("，,；;。！？!?：: ")
    hook_contract = (
        f"\n选片主钩子（这是为什么选中本片，权威高于后续陪衬话题）: {hook}\n"
        f"标题必须保留第一分句的核心事件: {hook_clause}\n"
        "同时输出 selection_hook_anchor：从该第一分句原样复制的 2–12 字具体短语，"
        "避开‘李豆沙/小李/主播/直播/弹幕/观众/自己/这个/那个/然后/时候/表演’等泛词；"
        "该短语必须逐字出现在标题里。不得把片段后半段的陪衬话题偷换成主标题。\n"
    )
    expected = (
        "为一条李豆沙(B站虚拟主播)的直播切片起中文标题。\n"
        "最重要的原则：观众是因为'这是李豆沙'才点进来的,不是因为内容——标题必须围绕李豆沙本人"
        "(她的反应、气质、口癖、梗、名字谐音),切片内容只是辅助素材。引人注目为先。\n"
        f"\n李豆沙特质:\n{persona}\n"
        f"\n标题风格规范与历史标题范例(严格模仿这个风格):\n{style}\n"
        f"\n本切片转写内容节选(辅助素材): {transcript}\n{ip}{hook_contract}"
        "\n同一份 hash-bound 长程语境（用于整片回指、口癖和专名候选；它本身不授权改字幕）：\n"
        f"{context}\n"
        "硬性要求：含【李豆沙】前缀后 12–49 字"
        "（Ivan 手定语料的主力带是 25–45 字的三拍叙事，不要为了凑短把梗压没；"
        "只有梗足够硬的短爆点才走 20 字以下）；"
        "禁用空洞夸张词(炸裂/震惊/天花板/绝了/犯规/太顶),"
        "更不许用'X到犯规/炸裂/离谱'这种万能后缀——标题必须具体到这条切片里到底发生了什么"
        "(描述性的'越看越离谱/越整越离谱'这类是可以的,禁的是空洞的'X到离谱'后缀)。\n"
        '只输出一个 JSON 对象：{"title": "标题", "selection_hook_anchor": "第一分句中的具体短语"}'
    )
    assert (
        build_automatic_talk_title_prompt(
            selection_hook=hook,
            transcript_sample=transcript,
            important_ip_prompt=ip,
            clip_context_prompt=context,
            persona_asset=persona,
            style_asset=style,
        )
        == expected
    )


def test_title_cover_and_publication_chokes_consume_the_same_authority() -> None:
    audit = validate_candidate_title_surface(CID, NEW_TITLE)
    assert audit is not None
    assert audit["authority_scope"] == ("GENERATED_PUBLIC_TEXT_ONLY_NO_SUBTITLE_OR_SPEAKER_REVIEW")
    validate_candidate_title_surface(CID, "只在封面写认南町为大哥", artifact_kind="cover")
    with pytest.raises(TitlePolicyError, match="FORBIDDEN"):
        validate_candidate_title_surface(CID, OLD_TITLE)
    assert "candidate_public_or_reviewed_title_surface_conflict" in (
        candidate_title_policy_violations(
            candidate_id=CID,
            title=OLD_TITLE,
            lane="talk",
            source_fact_review=None,
        )
    )


def _source_fact_keep(*, title: str = NEW_TITLE) -> str:
    return json.dumps(
        {
            "schema_version": "lidousha-source-fact-review.v1",
            "status": "KEEP",
            "final_selection_hook": NEW_HOOK,
            "final_title": title,
            "supported_by": ["final_transcript", "same_clip_context"],
            "changed_surfaces": [],
            "addressee_attribution": [],
            "selection_scorecard_review": {
                "status": "NOT_NEEDED",
                "reason": "selection hook remains unchanged",
            },
            "summary": "公共文案只消费候选级专名替换，字幕不在本授权范围。",
        },
        ensure_ascii=False,
    )


def test_source_fact_context_is_public_only_and_receipt_rebinds_without_reviewed_srt() -> None:
    def cpa(prompt: str) -> str:
        assert "GENERATED_PUBLIC_TEXT_ONLY" not in prompt
        assert "不代表整份 SRT 或 speaker 已人工复审" in prompt
        assert "大白老师仍是未裁定的字幕 cue" in prompt
        return _source_fact_keep()

    review = review_and_repair_source_facts(
        selection_hook=NEW_HOOK,
        title=NEW_TITLE,
        final_transcript="南天是初稿错写；cue19 暂写大白老师，仍未获本 authority 裁定。",
        clip_context_prompt=_prompt(),
        llm_call=cpa,
        candidate_id=CID,
        final_reviewed_srt_path=None,
    )
    assert source_fact_review_passes(review)
    context = review["entity_context"]
    assert context["authority_scope"] == (
        "GENERATED_PUBLIC_TEXT_ONLY_NO_SUBTITLE_OR_SPEAKER_REVIEW"
    )
    assert context["subtitle_text_mutation_authorized"] is False
    assert context["speaker_label_mutation_authorized"] is False
    assert "final_reviewed_srt_sha256" not in context
    assert validate_source_fact_review(
        review,
        selection_hook=NEW_HOOK,
        title=NEW_TITLE,
        final_transcript="南天是初稿错写；cue19 暂写大白老师，仍未获本 authority 裁定。",
        clip_context_prompt=_prompt(),
        candidate_id=CID,
        final_reviewed_srt_path=None,
    )
    assert not validate_source_fact_review(
        review,
        selection_hook=NEW_HOOK,
        title=NEW_TITLE,
        final_transcript="南天是初稿错写；cue19 暂写大白老师，仍未获本 authority 裁定。",
        clip_context_prompt=_prompt(pieces=[]),
        candidate_id=CID,
        final_reviewed_srt_path=None,
    )


def test_source_fact_provider_cannot_reintroduce_nantian() -> None:
    review = review_and_repair_source_facts(
        selection_hook=NEW_HOOK,
        title=NEW_TITLE,
        final_transcript="机器字幕仍可能出现南天。",
        clip_context_prompt=_prompt(),
        llm_call=lambda _prompt_text: _source_fact_keep(title=OLD_TITLE),
        candidate_id=CID,
    )
    assert not source_fact_review_passes(review)
    assert review["reason_code"] == "CPA_ENTITY_SURFACE_RESPONSE_INVALID"


def _clip_context_payload() -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": "lidousha-clip-context.v1",
        "candidate_id": CID,
        "recording_date": "2026-08-07",
        "selection_hook": OLD_HOOK,
        "pieces": PIECES,
        "session_relation_authority": None,
        "whole_clip_draft_srt": "机器初稿仍有南天；其他 cue 不在本 authority 范围。",
        "whole_clip_draft_srt_sha256": "sha256:"
        + hashlib.sha256("机器初稿仍有南天；其他 cue 不在本 authority 范围。".encode()).hexdigest(),
        "structured_chat": [],
        "topic_resolution": {},
        "session_topic_authorities": [],
        "speech_memory": {"entries": [], "ledger_sha256": "sha256:" + "1" * 64},
        "retrieval_budget": {
            "whole_clip_transcript_char_cap": 60000,
            "whole_clip_transcript_truncated": False,
            "structured_chat_row_cap": 240,
            "structured_chat_total_rows": 0,
            "structured_chat_selected_rows": 0,
            "structured_chat_truncated": False,
            "structured_chat_selection_policy": "all_sc_gift_guard_then_temporal_danmaku_sampling",
        },
        "mutation_authorized": False,
    }
    payload["context_sha256"] = CONTEXT_SHA
    return payload


def _runtime_contract(hook: str) -> dict[str, object]:
    contract = build_story_contract(
        candidate_id=CID,
        selection_hook=hook,
        transcript_text="机器字幕仍有南天；其他 cue 不在本 authority 范围。",
        selection_scorecard=None,
        session_relation_authority=None,
        source_media_sha256s=[SOURCE_SHA],
    )
    contract["clip_context_binding"] = {
        "context_sha256": CONTEXT_SHA,
        "mutation_authorized": False,
    }
    contract["clip_context_prompt"] = _prompt()
    return contract


def test_stage_publish_draft_rerenders_exact_surface_without_title_provider_or_upload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    media = tmp_path / "candidate.recut.mp4"
    media.write_bytes(b"media")
    cover = tmp_path / "cover.png"
    cover.write_bytes(b"cover")
    title_calls: list[str] = []
    source_fact_calls: list[str] = []
    rebuilt_hooks: list[str] = []

    def title_provider(prompt: str) -> str:
        title_calls.append(prompt)
        raise AssertionError("candidate authority must preempt title provider")

    def source_fact_provider(prompt: str) -> str:
        source_fact_calls.append(prompt)
        return _source_fact_keep()

    def rebuild(hook: str) -> dict[str, object]:
        rebuilt_hooks.append(hook)
        return _runtime_contract(hook)

    def stage_cover(_record: object, **kwargs: object) -> dict[str, object]:
        assert kwargs["title"] == NEW_TITLE
        return {
            "status": "AI_COVER_READY",
            "cover_path": str(cover),
            "cover_generation": {
                "status": "READY",
                "rendered_lines": ["认南町为大哥"],
            },
            "reason_codes": [],
        }

    monkeypatch.setattr(
        source_fact_staging,
        "build_addressee_evidence",
        lambda *_args: (
            "机器字幕仍有南天；其他 cue 不在本 authority 范围。",
            type(
                "Evidence",
                (),
                {
                    "transcript": None,
                    "speaker_evidence": {
                        "state": "AbsentAuthorized",
                        "reason": "speaker_mode_uniform_host",
                        "policy_ids": {
                            "alignment": "speaker_cue_subsegment_alignment/v1",
                            "text": "compact_ws/v1",
                            "timing": "half_open_integer_ms_exact/v1",
                            "absence": "speaker_mode_uniform_host/v1",
                        },
                    },
                },
            )(),
        ),
    )
    result = publish_staging._stage_publish_draft(
        {
            "status": "MATERIALIZED",
            "media_path": str(media),
            "artifact_hashes": {},
            "story_contract": _runtime_contract(OLD_HOOK),
            "speaker_mode": "uniform_host",
        },
        candidate_id=CID,
        title="machine fallback must not survive",
        cues=[],
        run_ffmpeg=False,
        title_llm_call=title_provider,
        source_fact_llm_call=source_fact_provider,
        selection_hook=OLD_HOOK,
        story_contract_rebuilder=rebuild,
        stage_cover=stage_cover,
    )
    assert result is not None
    staging = result["publish_staging"]
    assert title_calls == []
    assert len(source_fact_calls) == 1
    assert rebuilt_hooks == [NEW_HOOK, NEW_HOOK]
    assert result["story_contract"]["selection_hook"] == NEW_HOOK
    assert staging["title"] == NEW_TITLE
    assert staging["title_source"] == (
        "deterministic_candidate_public_surface_resolution+ivan_exact_substitution"
    )
    assert staging["title_authority_status"] == ("RESOLVED_PUBLIC_TEXT_SURFACE_AUTHORITY")
    assert staging["cover_entity_projection_audit"]["status"] == "PASS"
    receipt = staging["public_text_surface_authority_consumption"]
    assert receipt["upload_authorized"] is False
    assert receipt["registry_hold_released"] is False
    assert staging["upload_enabled"] is False
    publish = json.loads(Path(staging["publish_json_path"]).read_text(encoding="utf-8"))
    assert publish["title"] == NEW_TITLE
    assert publish["public_text_surface_authority_consumption"] == receipt
    assert publish["upload_enabled"] is False


def test_produce_talk_projects_resolved_public_hook_into_state_and_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The queued hook is input provenance, not the final public projection."""

    date = "2026-08-07"
    base = tmp_path / "autoslice"
    repo = tmp_path / "repo"
    (base / "logs").mkdir(parents=True)
    repo.mkdir()
    monkeypatch.setattr(runner, "BASE", base)
    monkeypatch.setattr(runner, "REPO_ROOT", repo)
    monkeypatch.setattr(runner, "child_env_for_date", lambda _date: {})
    monkeypatch.setattr(
        runner,
        "talk_pipeline_fingerprint",
        lambda _candidate_id: "sha256:test",
    )

    authority = load_candidate_public_text_surface_authority(CID)
    assert authority is not None
    story_contract = _runtime_contract(NEW_HOOK)
    receipt = consume_candidate_public_text_surface_authority(
        authority,
        candidate_id=CID,
        selection_hook=NEW_HOOK,
        story_contract=story_contract,
    )
    title_source = (
        "deterministic_candidate_public_surface_resolution+ivan_exact_substitution"
    )
    publish_staging = {
        "title": NEW_TITLE,
        "title_source": title_source,
        "title_authority_status": "RESOLVED_PUBLIC_TEXT_SURFACE_AUTHORITY",
        "public_text_surface_authority_consumption": receipt,
        "upload_enabled": False,
        "cover_status": "AI_COVER_READY",
        "cover_generation": {"rendered_lines": ["认南町为大哥"]},
    }
    tamper_publish_receipt = {"enabled": False}

    class Completed:
        returncode = 0

    def fake_run(_command: object, **kwargs: object) -> Completed:
        recuts = base / "out" / date / CID / "replacement_recuts"
        recuts.mkdir(parents=True, exist_ok=True)
        emitted_staging = dict(publish_staging)
        if tamper_publish_receipt["enabled"]:
            emitted_staging["public_text_surface_authority_consumption"] = {
                **receipt,
                "registry_hold_released": True,
            }
        publish = {
            "candidate_id": CID,
            **emitted_staging,
            "artifact_hashes": {},
        }
        (recuts / f"{CID}.recut.publish.json").write_text(
            json.dumps(publish, ensure_ascii=False), encoding="utf-8"
        )
        (recuts / f"{CID}.record.json").write_text(
            json.dumps(
                {
                    "candidate_id": CID,
                    "story_contract": story_contract,
                    "publish_staging": publish_staging,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        sink = kwargs["stdout"]
        sink.write('{"red_flags": [], "boundary_repairs": []}\n')
        sink.flush()
        return Completed()

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    from src.autoslice import speaker_guess

    monkeypatch.setattr(
        speaker_guess,
        "finalize_delivered_talk_status",
        lambda result, **_kwargs: result.update(status="review_ready"),
    )

    result = runner.produce_talk(
        date,
        {
            "cid": CID,
            "segment_path": "/recordings/22966160_20260807-21-07-39.mp4",
            "seg_dur_ms": 2_000_000,
            "start_ms": 1_142_160,
            "end_ms": 1_437_390,
            "hook": OLD_HOOK,
        },
    )

    assert result["hook"] == NEW_HOOK
    assert OLD_HOOK not in json.dumps(result, ensure_ascii=False)

    report_result = dict(result)
    report_result.update(
        status="failed",
        failure_kind="test_projection",
        failure_stage="test_projection",
    )
    runner.write_reports(
        date,
        {
            "status": "no_delivery",
            "picks": [report_result],
            "songs": [],
            "pending_talk": [],
            "pending_song": [],
        },
    )
    summary = (repo / "lidousha" / date / "AUTOSLICE_SUMMARY.md").read_text(
        encoding="utf-8"
    )
    assert NEW_HOOK in summary
    assert OLD_HOOK not in summary

    tamper_publish_receipt["enabled"] = True
    blocked = runner.produce_talk(
        date,
        {
            "cid": CID,
            "segment_path": "/recordings/22966160_20260807-21-07-39.mp4",
            "seg_dur_ms": 2_000_000,
            "start_ms": 1_142_160,
            "end_ms": 1_437_390,
            "hook": OLD_HOOK,
        },
    )
    assert blocked["status"] == "title_failed"
    assert blocked["hook"] == ""
    assert blocked["title_authority_status"] == (
        "UNRESOLVED_PUBLIC_TEXT_RESULT_PROJECTION"
    )
    assert "PUBLIC_TEXT_RESULT_MIRROR_DRIFT" in blocked["title_authority_error"]
    assert OLD_HOOK not in json.dumps(blocked, ensure_ascii=False)
    assert not (base / "out" / date / CID / "replacement_recuts").exists()


def test_read_publish_meta_without_candidate_authority_keeps_legacy_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Even a bad truth-mode setting must not introduce candidate-authority
    # behavior on the absent-authority path; the legacy metadata projection is
    # byte-for-byte independent of this optional choke point.
    monkeypatch.setenv("AUTOSLICE_HUMAN_TRUTH_MODE", "invalid-for-ordinary")
    work_dir = tmp_path / "auto_ordinary_100_200"
    recuts = work_dir / "replacement_recuts"
    recuts.mkdir(parents=True)
    (recuts / "auto_ordinary_100_200.recut.publish.json").write_text(
        json.dumps(
            {
                "candidate_id": "auto_ordinary_100_200",
                "title": "【李豆沙】普通候选标题",
                "title_source": "automatic",
                "title_authority_status": "RESOLVED_AUTOMATIC",
                "title_authority_error": None,
                "cover_status": "AI_COVER_READY",
                "cover_path": "/tmp/ordinary.cover.png",
                "cover_generation": {"status": "READY"},
                "artifact_hashes": {
                    "cover_sha256": "a" * 64,
                    "burned_video_sha256": "b" * 64,
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    assert runner.read_publish_meta(work_dir) == {
        "title": "【李豆沙】普通候选标题",
        "title_source": "automatic",
        "title_authority_status": "RESOLVED_AUTOMATIC",
        "title_authority_error": None,
        "cover_status": "AI_COVER_READY",
        "cover_path": "/tmp/ordinary.cover.png",
        "cover_sha256": "a" * 64,
        "cover_generation": {"status": "READY"},
        "video_sha256": "b" * 64,
    }


def test_talk_fingerprint_scopes_authority_to_one_candidate_and_withholds_truth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(runner, "pipeline_fingerprint", lambda: "sha256:" + "a" * 64)
    monkeypatch.delenv("AUTOSLICE_HUMAN_TRUTH_MODE", raising=False)
    target_before = runner.talk_pipeline_fingerprint(CID)
    neighbor_before = runner.talk_pipeline_fingerprint("auto_neighbor")
    path = _copy_asset(tmp_path)
    target_after = runner.talk_pipeline_fingerprint(CID)
    assert target_after != target_before
    assert runner.talk_pipeline_fingerprint("auto_neighbor") == neighbor_before
    path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    assert runner.talk_pipeline_fingerprint(CID) != target_after
    path.unlink()
    assert runner.talk_pipeline_fingerprint(CID) == target_before

    path = _copy_asset(tmp_path)
    monkeypatch.setenv("AUTOSLICE_HUMAN_TRUTH_MODE", "withheld")
    withheld = runner.talk_pipeline_fingerprint(CID)
    path.write_text("withheld bytes changed", encoding="utf-8")
    assert runner.candidate_public_text_surface_authority_path(CID) is None
    assert runner.talk_pipeline_fingerprint(CID) == withheld


def test_current_package_audit_rebuilds_consumption_title_cover_and_no_upload(
    tmp_path: Path,
) -> None:
    authority = load_candidate_public_text_surface_authority(CID)
    assert authority is not None
    contract = _runtime_contract(NEW_HOOK)
    receipt = consume_candidate_public_text_surface_authority(
        authority,
        candidate_id=CID,
        selection_hook=NEW_HOOK,
        story_contract=contract,
    )
    source = "deterministic_candidate_public_surface_resolution+ivan_exact_substitution"
    generation = {"rendered_lines": ["认南町为大哥"]}
    staging = {
        "title": NEW_TITLE,
        "title_source": source,
        "cover_generation": generation,
        "public_text_surface_authority_consumption": receipt,
        "upload_enabled": False,
    }
    publish = dict(staging)
    publish_path = tmp_path / "candidate.publish.json"
    publish_path.write_text(json.dumps(publish, ensure_ascii=False), encoding="utf-8")
    record = {"story_contract": contract, "publish_staging": staging}
    assert (
        audit_candidate_public_text_surfaces(
            candidate_id=CID,
            item_title=NEW_TITLE,
            publish_path=publish_path,
            record_path=tmp_path / "candidate.record.json",
            record=record,
            publish_staging=staging,
        )
        == ()
    )

    publish["cover_generation"] = {"rendered_lines": ["认南天为大哥"]}
    publish_path.write_text(json.dumps(publish, ensure_ascii=False), encoding="utf-8")
    issues = audit_candidate_public_text_surfaces(
        candidate_id=CID,
        item_title=NEW_TITLE,
        publish_path=publish_path,
        record_path=None,
        record=record,
        publish_staging=staging,
    )
    assert [issue.code for issue in issues] == ["CANDIDATE_PUBLIC_TEXT_SURFACE_INVALID"]

    publish = dict(staging)
    publish["public_text_surface_authority_consumption"] = {
        **receipt,
        "registry_hold_released": True,
    }
    publish_path.write_text(json.dumps(publish, ensure_ascii=False), encoding="utf-8")
    issues = audit_candidate_public_text_surfaces(
        candidate_id=CID,
        item_title=NEW_TITLE,
        publish_path=publish_path,
        record_path=None,
        record=record,
        publish_staging=staging,
    )
    assert [issue.code for issue in issues] == ["CANDIDATE_PUBLIC_TEXT_CONSUMPTION_DRIFT"]


def test_manual_title_cover_repair_projects_canonical_consumption_to_all_active_views(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cover transaction must stage the same sealed receipt in every view."""

    sealed = load_candidate_public_text_surface_authority(MANUAL_TITLE_CID)
    assert sealed is not None and sealed.is_manual_title_resolution
    prompt = "manual title repair test prompt"
    contract = {
        "candidate_id": MANUAL_TITLE_CID,
        "selection_hook": sealed.input_selection_hook,
        "source_media_sha256s": sorted(
            {str(piece["source_media_sha256"]) for piece in sealed.source_pieces}
        ),
        "clip_context_binding": {"context_sha256": sealed.clip_context_sha256},
        "clip_context_prompt": prompt,
    }
    authority = replace(
        sealed,
        story_contract_sha256=_sha256_json(contract),
        clip_context_prompt_sha256="sha256:"
        + hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
    )
    receipt = consume_candidate_public_text_surface_authority(
        authority,
        candidate_id=MANUAL_TITLE_CID,
        selection_hook=str(contract["selection_hook"]),
        story_contract=contract,
    )
    old_title = authority.superseded_title
    final_title = authority.resolved_title
    cover_lines = ["薇欧拉对阿拉蕾", "就是铁暗恋！"]

    def active_document(*, shadow: bool) -> dict[str, object]:
        view: dict[str, object] = {
            "title": old_title,
            "title_source": "job_title",
            "upload_enabled": False,
            "reason_codes": [],
        }
        document: dict[str, object] = {
            "schema_version": "shadow-publish-draft.v1" if shadow else "delivery-record.v1",
            "candidate_id": MANUAL_TITLE_CID,
            "artifact_hashes": {"burned_video_sha256": "sha256:" + "1" * 64},
        }
        if shadow:
            document.update(view)
        else:
            document["story_contract"] = contract
            document["publish_staging"] = view
        return document

    delivery, source_record, publish = (
        reviewed_covers._invalidate_document(
            active_document(shadow=shadow),
            title=final_title,
            cover_text="\n".join(cover_lines),
            public_text_consumption=receipt,
            title_source=authority.title_source,
        )
        for shadow in (False, False, True)
    )
    for document in (delivery, source_record, publish):
        view = document if document["schema_version"] == "shadow-publish-draft.v1" else document["publish_staging"]
        assert view["title"] == final_title
        assert view["title_source"] == authority.title_source
        assert view["public_text_surface_authority_consumption"] == receipt
        view["cover_generation"] = {"rendered_lines": cover_lines}

    publish_path = tmp_path / "candidate.publish.json"
    publish_path.write_text(json.dumps(publish, ensure_ascii=False), encoding="utf-8")
    staging = source_record["publish_staging"]
    monkeypatch.setattr(
        title_audit,
        "load_candidate_public_text_surface_authority",
        lambda candidate_id: authority if candidate_id == MANUAL_TITLE_CID else None,
    )
    assert (
        audit_candidate_public_text_surfaces(
            candidate_id=MANUAL_TITLE_CID,
            item_title=final_title,
            publish_path=publish_path,
            record_path=tmp_path / "candidate.record.json",
            record=source_record,
            publish_staging=staging,
        )
        == ()
    )

    def audit_with(staging_view: dict[str, object], publish_view: dict[str, object], title: str):
        publish_path.write_text(json.dumps(publish_view, ensure_ascii=False), encoding="utf-8")
        return audit_candidate_public_text_surfaces(
            candidate_id=MANUAL_TITLE_CID,
            item_title=title,
            publish_path=publish_path,
            record_path=tmp_path / "candidate.record.json",
            record={"story_contract": contract, "publish_staging": staging_view},
            publish_staging=staging_view,
        )

    missing_staging = json.loads(json.dumps(staging))
    missing_publish = json.loads(json.dumps(publish))
    missing_staging.pop("public_text_surface_authority_consumption")
    missing_publish.pop("public_text_surface_authority_consumption")
    assert [issue.code for issue in audit_with(missing_staging, missing_publish, final_title)] == [
        "CANDIDATE_PUBLIC_TEXT_CONSUMPTION_DRIFT"
    ]

    tampered_staging = json.loads(json.dumps(staging))
    tampered_publish = json.loads(json.dumps(publish))
    tampered_staging["public_text_surface_authority_consumption"]["resolved_title"] = old_title
    tampered_publish["public_text_surface_authority_consumption"]["resolved_title"] = old_title
    assert [issue.code for issue in audit_with(tampered_staging, tampered_publish, final_title)] == [
        "CANDIDATE_PUBLIC_TEXT_CONSUMPTION_DRIFT"
    ]

    wrong_source_staging = json.loads(json.dumps(staging))
    wrong_source_publish = json.loads(json.dumps(publish))
    wrong_source_staging["title_source"] = "job_title"
    wrong_source_publish["title_source"] = "job_title"
    assert [issue.code for issue in audit_with(wrong_source_staging, wrong_source_publish, final_title)] == [
        "CANDIDATE_PUBLIC_TEXT_CONSUMPTION_DRIFT"
    ]

    old_staging = json.loads(json.dumps(staging))
    old_publish = json.loads(json.dumps(publish))
    old_staging["title"] = old_title
    old_publish["title"] = old_title
    assert [issue.code for issue in audit_with(old_staging, old_publish, old_title)] == [
        "CANDIDATE_PUBLIC_TEXT_SURFACE_INVALID"
    ]


def _self_hashed_receipt(value: dict[str, object]) -> dict[str, object]:
    result = copy.deepcopy(value)
    result.pop("receipt_sha256", None)
    result["receipt_sha256"] = _sha256_json(result)
    return result


def _manual_successor_shape() -> tuple[dict[str, object], dict[str, object], dict[str, object], dict[str, object]]:
    """A frozen receipt-only successor shape; no provider or title rewrite."""

    authority = load_candidate_public_text_surface_authority(MANUAL_TITLE_CID)
    assert authority is not None
    historical = _self_hashed_receipt(
        {
            "schema_version": "lidousha-source-fact-review.v1",
            "status": "PASS",
            "decision": "REPAIRED",
            "original_title": authority.superseded_title,
            "final_title": authority.superseded_title,
            "original_selection_hook": authority.input_selection_hook,
            "final_selection_hook": authority.input_selection_hook,
        }
    )
    predecessor = {
        "candidate_id": MANUAL_TITLE_CID,
        "selection_hook": authority.input_selection_hook,
        "source_media_sha256s": sorted(
            {str(piece["source_media_sha256"]) for piece in authority.source_pieces}
        ),
        "clip_context_binding": {"context_sha256": authority.clip_context_sha256},
        "clip_context_prompt": "successor test prompt",
        "source_fact_review": historical,
    }
    refresh = _self_hashed_receipt(
        {
            "schema_version": "lidousha-source-fact-review.v1",
            "status": "PASS",
            "decision": "CANDIDATE_PUBLIC_TEXT_SOURCE_FACT_REFRESH",
            "original_title": historical["original_title"],
            "final_title": authority.resolved_title,
            "original_selection_hook": historical["original_selection_hook"],
            "final_selection_hook": authority.input_selection_hook,
            "historical_provider_receipt": historical,
            "candidate_public_text_source_fact_refresh": {
                "schema_version": "candidate-public-text-source-fact-refresh-consumption.v1",
                "status": "VALID",
                "candidate_id": MANUAL_TITLE_CID,
                "authority_sha256": "sha256:" + "9" * 64,
            },
        }
    )
    successor = copy.deepcopy(predecessor)
    successor["source_fact_review"] = refresh
    refresh_binding = {
        "receipt_sha256": historical["receipt_sha256"],
        "original_title": historical["original_title"],
        "final_title": historical["final_title"],
        "original_selection_hook": historical["original_selection_hook"],
        "final_selection_hook": historical["final_selection_hook"],
        "status": historical["status"],
        "decision": historical["decision"],
    }
    successor_authority = {
        "schema_version": title_successor.SCHEMA_VERSION,
        "candidate_id": MANUAL_TITLE_CID,
        "recording_date": "2026-08-11",
        "public_text_authority": {
            "relative_path": "assets/lidousha/candidate_public_text_surface_authorities/auto_173005_934_1166.public-text-surface-authority.v1.json",
            "authority_sha256": authority.authority_sha256,
        },
        "source_fact_refresh_authority": {
            "relative_path": "assets/lidousha/candidate_source_fact_refresh_authorities/auto_173005_934_1166.v1.json",
            "authority_sha256": "sha256:" + "9" * 64,
        },
        "predecessor": {
            "story_contract_sha256": _sha256_json(predecessor),
            "historical_provider_receipt_sha256": historical["receipt_sha256"],
        },
        "successor": {
            "story_contract_sha256": _sha256_json(successor),
            "source_fact_review_sha256": refresh["receipt_sha256"],
        },
        "allowed_json_pointers": ["/source_fact_review"],
        "authority_sha256": "sha256:" + "8" * 64,
    }
    return predecessor, successor, refresh_binding, successor_authority


def test_manual_title_source_fact_successor_is_receipt_only_and_keeps_consumption_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    predecessor, successor, refresh_binding, binding = _manual_successor_shape()
    authority = load_candidate_public_text_surface_authority(MANUAL_TITLE_CID)
    assert authority is not None
    monkeypatch.setattr(title_successor, "_load_successor_authority", lambda **_k: binding)
    monkeypatch.setattr(
        title_successor,
        "_validate_refresh_asset",
        lambda **_k: {"historical_provider_receipt": refresh_binding},
    )
    assert title_successor.accepts_manual_title_source_fact_successor(
        repo_root=Path("/sealed"),
        candidate_id=MANUAL_TITLE_CID,
        public_text_authority_sha256=authority.authority_sha256,
        story_contract=successor,
    )

    legacy = replace(
        authority,
        story_contract_sha256=_sha256_json(predecessor),
        clip_context_prompt_sha256="sha256:"
        + hashlib.sha256(str(predecessor["clip_context_prompt"]).encode()).hexdigest(),
    )
    expected = consume_candidate_public_text_surface_authority(
        legacy,
        candidate_id=MANUAL_TITLE_CID,
        selection_hook=legacy.input_selection_hook,
        story_contract=predecessor,
    )
    monkeypatch.setattr(
        public_text_authority,
        "accepts_manual_title_source_fact_successor",
        lambda **_k: True,
    )
    assert consume_candidate_public_text_surface_authority(
        legacy,
        candidate_id=MANUAL_TITLE_CID,
        selection_hook=legacy.input_selection_hook,
        story_contract=successor,
    ) == expected

    for mutation in ("authority", "receipt", "historical", "extra", "missing_review"):
        tampered = copy.deepcopy(successor)
        if mutation == "authority":
            tampered["source_fact_review"]["candidate_public_text_source_fact_refresh"]["authority_sha256"] = "sha256:" + "0" * 64
        elif mutation == "receipt":
            tampered["source_fact_review"]["final_title"] = "篡改标题"
        elif mutation == "historical":
            tampered["source_fact_review"]["historical_provider_receipt"]["final_title"] = "篡改历史"
            tampered["source_fact_review"]["historical_provider_receipt"] = _self_hashed_receipt(tampered["source_fact_review"]["historical_provider_receipt"])
        elif mutation == "extra":
            tampered["unapproved"] = True
        else:
            tampered.pop("source_fact_review")
        if mutation in {"authority", "receipt", "historical"}:
            tampered["source_fact_review"] = _self_hashed_receipt(tampered["source_fact_review"])
        with pytest.raises(title_successor.ManualTitleSourceFactSuccessorError):
            title_successor.accepts_manual_title_source_fact_successor(
                repo_root=Path("/sealed"),
                candidate_id=MANUAL_TITLE_CID,
                public_text_authority_sha256=authority.authority_sha256,
                story_contract=tampered,
            )
    assert not title_successor.accepts_manual_title_source_fact_successor(
        repo_root=Path("/sealed"),
        candidate_id="auto_210739_1142_1436",
        public_text_authority_sha256=authority.authority_sha256,
        story_contract=successor,
    )


def test_manual_title_consume_and_package_audit_run_the_full_successor_validator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    predecessor, successor, refresh_binding, binding = _manual_successor_shape()
    authority = load_candidate_public_text_surface_authority(MANUAL_TITLE_CID)
    assert authority is not None
    legacy = replace(
        authority,
        story_contract_sha256=_sha256_json(predecessor),
        clip_context_prompt_sha256="sha256:"
        + hashlib.sha256(str(predecessor["clip_context_prompt"]).encode()).hexdigest(),
    )
    expected = consume_candidate_public_text_surface_authority(
        legacy,
        candidate_id=MANUAL_TITLE_CID,
        selection_hook=legacy.input_selection_hook,
        story_contract=predecessor,
    )

    refresh = {
        "schema_version": "candidate-public-text-source-fact-refresh-authority.v1",
        "candidate_id": MANUAL_TITLE_CID,
        "recording_date": "2026-08-11",
        "historical_provider_receipt": refresh_binding,
        "public_text_authority": {
            "relative_path": binding["public_text_authority"]["relative_path"],
            "authority_sha256": legacy.authority_sha256,
        },
        "runtime_bindings": {"story_contract_sha256": _sha256_json(predecessor)},
        "scope": {
            "candidate_scoped": True,
            "provider_call_required": False,
            "subtitle_text_mutation_authorized": False,
            "speaker_label_mutation_authorized": False,
            "upload_authorized": False,
        },
    }
    refresh["authority_sha256"] = _sha256_json(refresh)
    binding["source_fact_refresh_authority"]["authority_sha256"] = refresh["authority_sha256"]
    successor["source_fact_review"]["candidate_public_text_source_fact_refresh"]["authority_sha256"] = refresh["authority_sha256"]
    successor["source_fact_review"] = _self_hashed_receipt(successor["source_fact_review"])
    binding["successor"] = {
        "story_contract_sha256": _sha256_json(successor),
        "source_fact_review_sha256": successor["source_fact_review"]["receipt_sha256"],
    }
    binding["authority_sha256"] = _sha256_json({
        key: value for key, value in binding.items() if key != "authority_sha256"
    })
    refresh_path = tmp_path / title_successor._REFRESH_RELATIVE
    successor_path = tmp_path / title_successor.RELATIVE_PATH
    refresh_path.parent.mkdir(parents=True)
    successor_path.parent.mkdir(parents=True)
    refresh_path.write_text(json.dumps(refresh, ensure_ascii=False), encoding="utf-8")
    successor_path.write_text(json.dumps(binding, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(public_text_authority, "ROOT", tmp_path)

    actual = consume_candidate_public_text_surface_authority(
        legacy,
        candidate_id=MANUAL_TITLE_CID,
        selection_hook=legacy.input_selection_hook,
        story_contract=successor,
    )
    assert actual == expected

    cover = {"rendered_lines": ["薇欧拉对阿拉蕾", "就是铁暗恋！"]}
    staging = {
        "title": legacy.resolved_title,
        "title_source": legacy.title_source,
        "upload_enabled": False,
        "cover_generation": cover,
        "public_text_surface_authority_consumption": expected,
    }
    publish = copy.deepcopy(staging)
    delivery = {
        "schema_version": "delivery-record.v1",
        "candidate_id": MANUAL_TITLE_CID,
        "story_contract": copy.deepcopy(successor),
        "publish_staging": copy.deepcopy(staging),
    }
    source_record = {
        "schema_version": "delivery-record.v1",
        "candidate_id": MANUAL_TITLE_CID,
        "story_contract": copy.deepcopy(successor),
        "publish_staging": copy.deepcopy(staging),
    }
    assert [
        delivery["publish_staging"]["public_text_surface_authority_consumption"],
        source_record["publish_staging"]["public_text_surface_authority_consumption"],
        publish["public_text_surface_authority_consumption"],
    ] == [expected, expected, expected]
    publish_path = tmp_path / "current.publish.json"
    publish_path.write_text(json.dumps(publish, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(title_audit, "load_candidate_public_text_surface_authority", lambda _cid: legacy)
    assert audit_candidate_public_text_surfaces(
        candidate_id=MANUAL_TITLE_CID,
        item_title=legacy.resolved_title,
        publish_path=publish_path,
        record_path=tmp_path / "current.record.json",
        record=source_record,
        publish_staging=source_record["publish_staging"],
    ) == ()


def test_withheld_staging_calls_providers_without_consuming_or_leaking_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTOSLICE_HUMAN_TRUTH_MODE", "withheld")
    media = tmp_path / "withheld.recut.mp4"
    media.write_bytes(b"media")
    cover = tmp_path / "cover.png"
    cover.write_bytes(b"cover")
    generated_title = "【李豆沙】没人肯跟她抱团，认南天为大哥后亲眼看着大哥暴毙"
    title_prompts: list[str] = []
    source_prompts: list[str] = []

    def title_provider(prompt: str) -> str:
        title_prompts.append(prompt)
        assert NEW_HOOK not in prompt and NEW_TITLE not in prompt
        return json.dumps(
            {"title": generated_title, "selection_hook_anchor": "没人肯跟她抱团"},
            ensure_ascii=False,
        )

    def source_provider(prompt: str) -> str:
        source_prompts.append(prompt)
        assert "不代表整份 SRT 或 speaker 已人工复审" not in prompt
        return json.dumps(
            {
                "schema_version": "lidousha-source-fact-review.v1",
                "status": "KEEP",
                "final_selection_hook": OLD_HOOK,
                "final_title": generated_title,
                "supported_by": ["final_transcript", "same_clip_context"],
                "changed_surfaces": [],
                "addressee_attribution": [],
                "selection_scorecard_review": {
                    "status": "NOT_NEEDED",
                    "reason": "selection hook remains unchanged",
                },
                "summary": "盲测模式只使用机器输入，不读取候选人工专名裁定。",
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr(
        source_fact_staging,
        "build_addressee_evidence",
        lambda *_args: (
            "机器字幕仍有南天。",
            type(
                "Evidence",
                (),
                {
                    "transcript": None,
                    "speaker_evidence": {
                        "state": "AbsentAuthorized",
                        "reason": "speaker_mode_uniform_host",
                        "policy_ids": {
                            "alignment": "speaker_cue_subsegment_alignment/v1",
                            "text": "compact_ws/v1",
                            "timing": "half_open_integer_ms_exact/v1",
                            "absence": "speaker_mode_uniform_host/v1",
                        },
                    },
                },
            )(),
        ),
    )
    result = publish_staging._stage_publish_draft(
        {
            "status": "MATERIALIZED",
            "media_path": str(media),
            "artifact_hashes": {},
            "story_contract": _runtime_contract(OLD_HOOK),
            "speaker_mode": "uniform_host",
        },
        candidate_id=CID,
        title="machine fallback",
        cues=[],
        run_ffmpeg=False,
        title_llm_call=title_provider,
        source_fact_llm_call=source_provider,
        selection_hook=OLD_HOOK,
        story_contract_rebuilder=lambda hook: _runtime_contract(hook),
        stage_cover=lambda *_args, **_kwargs: {
            "status": "AI_COVER_READY",
            "cover_path": str(cover),
            "cover_generation": {"status": "READY", "rendered_lines": ["认大哥"]},
            "reason_codes": [],
        },
    )
    assert result is not None
    assert len(title_prompts) == len(source_prompts) == 1
    assert result["story_contract"]["selection_hook"] == OLD_HOOK
    staging = result["publish_staging"]
    assert staging["title"] == generated_title
    assert staging["public_text_surface_authority_consumption"] is None
    assert staging["entity_projection_audit"] is None
