from __future__ import annotations

import base64
import copy
import hashlib
import json
import re
import shutil
import subprocess
import zlib
from pathlib import Path
from typing import Mapping

import pytest

import src.autoslice.publish_staging as publish_staging
import src.autoslice.source_fact_review as source_fact_review
import src.autoslice.source_fact_staging as source_fact_staging
from src.autoslice.deterministic_text_surface_resolution import (
    ASSET_DIRECTORY,
    AUTHORITY_FILENAME,
    CANDIDATE_ID,
    CLIP_CONTEXT_PROMPT_SHA256,
    ENTITY_CONTEXT_SHA256,
    ENTITY_PROJECTION_SHA256,
    EXACT_COMBINED_SHA256,
    EXACT_HOOK,
    EXACT_TITLE,
    FAILED_RECEIPT_FILENAME,
    FAILED_SOURCE_FACT_RECEIPT_SHA256,
    FINAL_TRANSCRIPT_SHA256,
    OLD_HOOK,
    OLD_TITLE,
    SOURCE_BASENAME,
    SOURCE_END_MS,
    SOURCE_SHA256,
    SOURCE_START_MS,
    SPEAKER_EVIDENCE_SHA256,
    SELECTION_SCORECARD_SHA256,
    DeterministicTextSurfaceResolutionError,
    canonical_sha256,
    combined_surface_sha256,
    consume_deterministic_text_surface_authority,
    load_deterministic_text_surface_authority,
    text_sha256,
    validate_deterministic_text_surface_document,
)
from src.autoslice.review_evidence import SourceCue
from src.autoslice.story_contract import build_story_contract


ROOT = Path(__file__).resolve().parents[2]
AUTHORITY_PATH = ROOT / ASSET_DIRECTORY / AUTHORITY_FILENAME
FAILED_RECEIPT_PATH = ROOT / ASSET_DIRECTORY / FAILED_RECEIPT_FILENAME
SRT_RELATIVE = Path(f"assets/lidousha/reviewed_subtitle_baselines/{CANDIDATE_ID}.reviewed.srt")
MANIFEST_RELATIVE = Path(
    f"assets/lidousha/reviewed_subtitle_baselines/{CANDIDATE_ID}.subtitle-baseline.v1.json"
)
ENTITY_RELATIVE = Path(
    f"assets/lidousha/candidate_entity_projections/{CANDIDATE_ID}.entity-projection.v1.json"
)

# Exact immutable WSL adjudication input, compressed only to keep this focused
# test readable.  Decoded: 9,834 UTF-8 bytes from clip-context artifact
# dba49c39f07ef961a1542d1ca84db55f2533bf78289ad3a078d6d38f89c76478.
_FROZEN_CLIP_CONTEXT_PROMPT_B64 = (
    "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"
)

_FROZEN_SELECTION_SCORECARD: dict[str, object] = {
    "dimensions": {
        "audience_salience": 4,
        "comedic_payoff": 4,
        "lidousha_centrality": 4,
        "persona_reversal": 3,
        "relationship_interaction": 4,
        "self_contained": 4,
        "stance_intensity": 3,
    },
    "effective_score": 91.5,
    "end_cue": 523,
    "fatigue_penalty": 0.0,
    "raw_score": 92.5,
    "reason_codes": [],
    "requested_tier": 1,
    "schema_version": "lidousha-selection-scorecard.v1",
    "start_cue": 488,
    "status": "VALID",
    "tier": 1,
    "tier_basis": "cp_positioning",
    "tier_evidence_cues": [488, 499, 503, 505, 507, 519],
    "tier_reason": (
        "从弹幕提问跳舞对象触发，完整讲述小李与新SII公主抱、第一次、负责、截图赖上这一串暧昧关系拉扯，且有明确笑点收束。"
    ),
    "uncertainty_penalty": 1.0,
    "weights": {
        "audience_salience": 15,
        "comedic_payoff": 10,
        "lidousha_centrality": 25,
        "persona_reversal": 10,
        "relationship_interaction": 15,
        "self_contained": 5,
        "stance_intensity": 20,
    },
}


def _document() -> dict[str, object]:
    return json.loads(AUTHORITY_PATH.read_text(encoding="utf-8"))


def _failed_receipt() -> dict[str, object]:
    return json.loads(FAILED_RECEIPT_PATH.read_text(encoding="utf-8"))


def _rehash(document: dict[str, object]) -> None:
    document.pop("authority_sha256", None)
    document["authority_sha256"] = canonical_sha256(document)


def _frozen_clip_context_prompt() -> str:
    encoded = base64.b64decode(_FROZEN_CLIP_CONTEXT_PROMPT_B64)
    return zlib.decompress(encoded).decode("utf-8")


def _final_transcript(reviewed_srt_path: Path) -> str:
    text = reviewed_srt_path.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
    cues = []
    for block in re.split(r"\n{2,}", text.strip()):
        lines = block.splitlines()
        cues.append("\n".join(lines[2:]).strip())
    return "\n".join(cue for cue in cues if cue)


def _source_cues(reviewed_srt_path: Path) -> list[SourceCue]:
    return [
        SourceCue(
            cue_id=str(index),
            source_start_ms=(index - 1) * 1_000,
            source_end_ms=index * 1_000,
            text=text,
            language="zh",
        )
        for index, text in enumerate(
            _final_transcript(reviewed_srt_path).splitlines(),
            start=1,
        )
    ]


def _sealed_repo(tmp_path: Path, name: str) -> Path:
    root = tmp_path / name
    for relative in (
        ASSET_DIRECTORY / AUTHORITY_FILENAME,
        ASSET_DIRECTORY / FAILED_RECEIPT_FILENAME,
        SRT_RELATIVE,
        MANIFEST_RELATIVE,
        ENTITY_RELATIVE,
    ):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, target)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=tests",
            "-c",
            "user.email=tests@example.invalid",
            "commit",
            "-qm",
            "seal deterministic authority",
        ],
        check=True,
    )
    return root


def _consume(
    root: Path,
    *,
    reviewed_srt_path: Path | None = None,
    final_transcript: str | None = None,
    clip_context_prompt: str | None = None,
    selection_scorecard: object = None,
    entity_context: object = None,
) -> dict[str, object]:
    authority = load_deterministic_text_surface_authority(CANDIDATE_ID, root=root)
    assert authority is not None
    repository_srt_path = root / SRT_RELATIVE
    return consume_deterministic_text_surface_authority(
        authority,
        candidate_id=CANDIDATE_ID,
        source_recording_basename=SOURCE_BASENAME,
        source_sha256=SOURCE_SHA256,
        absolute_source_start_ms=SOURCE_START_MS,
        absolute_source_end_ms=SOURCE_END_MS,
        reviewed_srt_path=reviewed_srt_path or repository_srt_path,
        speaker_evidence=copy.deepcopy(authority.document["uniform_host_authority"]["evidence"]),
        failed_source_fact_review=copy.deepcopy(authority.failed_source_fact_receipt),
        original_title=OLD_TITLE,
        original_selection_hook=OLD_HOOK,
        final_transcript=(
            final_transcript
            if final_transcript is not None
            else _final_transcript(repository_srt_path)
        ),
        clip_context_prompt=(
            clip_context_prompt
            if clip_context_prompt is not None
            else _frozen_clip_context_prompt()
        ),
        selection_scorecard=(
            selection_scorecard
            if selection_scorecard is not None
            else copy.deepcopy(_FROZEN_SELECTION_SCORECARD)
        ),
        entity_context=(
            entity_context
            if entity_context is not None
            else copy.deepcopy(authority.failed_source_fact_receipt["entity_context"])
        ),
    )


def test_exact_closed_plan_renders_frozen_bytes_without_provider_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _sealed_repo(tmp_path, "sealed-a")

    def provider_must_not_run(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("source-fact provider was called")

    monkeypatch.setattr(
        source_fact_review,
        "review_and_repair_source_facts",
        provider_must_not_run,
    )
    receipt = _consume(root)

    assert receipt["title"] == EXACT_TITLE
    assert len(receipt["title"]) == 50
    authority_document = json.loads(
        (root / ASSET_DIRECTORY / AUTHORITY_FILENAME).read_text(encoding="utf-8")
    )
    assert (
        authority_document["exact_surface_resolution"]["title"]["current_global_policy_disposition"]
        == "candidate_scoped_exact_surface_exception_required"
    )
    assert authority_document["exact_surface_resolution"]["title"][
        "current_global_policy_violations"
    ] == ["publish_title_length_out_of_bounds"]
    assert receipt["selection_hook"] == EXACT_HOOK
    assert receipt["title_sha256"] == (
        "sha256:97bf7997892126ce9dfeed7d5e98a047a91b570513d6ad47c05fb7285afa9951"
    )
    assert receipt["selection_hook_sha256"] == (
        "sha256:c6b71718c9651c63ff97efbb1cfb341b7f3ddaec4222f1bad736b2ac58f844c6"
    )
    assert receipt["combined_surface_sha256"] == EXACT_COMBINED_SHA256
    assert receipt["provider_call_required"] is False
    assert receipt["original_source_fact_attempt"] == "FAILED"
    assert receipt["exact_surface_resolution"] == "VALID"
    assert receipt["publication_fact_authority"] == "RESOLVED_EXACT_SURFACE"
    assert receipt["original_source_fact_receipt"] == _failed_receipt()
    assert receipt["original_source_fact_receipt"]["status"] == "FAILED"
    assert receipt["original_source_fact_receipt_sha256"] == (FAILED_SOURCE_FACT_RECEIPT_SHA256)
    assert receipt["final_transcript_sha256"] == FINAL_TRANSCRIPT_SHA256
    assert receipt["clip_context_prompt_sha256"] == CLIP_CONTEXT_PROMPT_SHA256
    assert receipt["selection_scorecard_sha256"] == SELECTION_SCORECARD_SHA256
    assert receipt["entity_context_sha256"] == ENTITY_CONTEXT_SHA256


def test_title_length_exception_requires_the_exact_frozen_surface(
    tmp_path: Path,
) -> None:
    root = _sealed_repo(tmp_path, "sealed-title-exception")
    consumption = _consume(root)
    review = source_fact_review.authorize_deterministic_text_narrowing(consumption)

    assert source_fact_review.deterministic_text_narrowing_title_policy_exception_applies(
        review,
        candidate_id=CANDIDATE_ID,
        title=EXACT_TITLE,
    )

    tampered = copy.deepcopy(consumption)
    tampered_title = EXACT_TITLE[:-1] + "呀"
    assert len(tampered_title) == len(EXACT_TITLE) == 50
    tampered["title"] = tampered_title
    tampered["title_sha256"] = text_sha256(tampered_title)
    tampered["combined_surface_sha256"] = combined_surface_sha256(
        tampered_title,
        EXACT_HOOK,
    )
    tampered.pop("receipt_sha256")
    tampered["receipt_sha256"] = canonical_sha256(tampered)
    self_consistent_review = source_fact_review.authorize_deterministic_text_narrowing(tampered)

    assert not source_fact_review.deterministic_text_narrowing_title_policy_exception_applies(
        self_consistent_review,
        candidate_id=CANDIDATE_ID,
        title=tampered_title,
    )


def test_publish_staging_consumes_exact_plan_without_source_fact_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sealed_root = _sealed_repo(tmp_path, "sealed-staging")
    authority = load_deterministic_text_surface_authority(
        CANDIDATE_ID,
        root=sealed_root,
    )
    assert authority is not None
    runtime_srt = tmp_path / "package" / f"{CANDIDATE_ID}.recut.srt"
    runtime_srt.parent.mkdir(parents=True)
    shutil.copy2(sealed_root / SRT_RELATIVE, runtime_srt)
    transcript = _final_transcript(runtime_srt)
    context_prompt = _frozen_clip_context_prompt()

    def contract(hook: str) -> dict[str, object]:
        story = build_story_contract(
            candidate_id=CANDIDATE_ID,
            selection_hook=hook,
            transcript_text=transcript,
            selection_scorecard=copy.deepcopy(_FROZEN_SELECTION_SCORECARD),
            session_relation_authority=None,
        )
        story["clip_context_prompt"] = context_prompt
        return story

    monkeypatch.setattr(
        source_fact_staging,
        "load_deterministic_text_surface_authority",
        lambda candidate_id: authority if candidate_id == CANDIDATE_ID else None,
    )

    def provider_must_not_run(_prompt: str) -> str:
        raise AssertionError("source-fact provider was called")

    cover = tmp_path / "package" / f"{CANDIDATE_ID}.cover.png"

    def stage_cover(_record: object, **_kwargs: object) -> dict[str, object]:
        cover.write_bytes(b"cover")
        return {
            "status": "AI_COVER_READY",
            "cover_path": str(cover),
            "cover_generation": {
                "status": "READY",
                "rendered_lines": ["xxsk说你给了我第一次"],
            },
            "reason_codes": [],
        }

    media = tmp_path / "package" / f"{CANDIDATE_ID}.recut.mp4"
    media.write_bytes(b"media")
    staged = publish_staging._stage_publish_draft(
        {
            "status": "MATERIALIZED",
            "speaker_mode": "uniform_host",
            "media_path": str(media),
            "subtitle_path": str(runtime_srt),
            "story_contract": contract(OLD_HOOK),
            "artifact_hashes": {},
        },
        candidate_id=CANDIDATE_ID,
        title=OLD_TITLE,
        cues=_source_cues(runtime_srt),
        run_ffmpeg=False,
        title_llm_call=None,
        selection_hook=OLD_HOOK,
        source_fact_llm_call=provider_must_not_run,
        story_contract_rebuilder=contract,
        stage_cover=stage_cover,
    )

    assert staged is not None
    source_fact = staged["story_contract"]["source_fact_review"]
    assert source_fact["decision"] == "DETERMINISTIC_TEXT_NARROWING"
    assert source_fact["final_title"] == EXACT_TITLE
    assert source_fact["final_selection_hook"] == EXACT_HOOK
    assert source_fact["blocked_source_fact_review"] == _failed_receipt()
    assert staged["publish_staging"]["title"] == EXACT_TITLE
    assert staged["publish_staging"]["title_authority_status"] == (
        "RESOLVED_DETERMINISTIC_TEXT_NARROWING"
    )
    assert staged["publish_staging"]["cover_status"] == "AI_COVER_READY"
    assert (
        "publish_title_length_out_of_bounds"
        not in staged["publish_staging"]["title_policy_violations"]
    )


def test_differently_named_relocated_runtime_srt_replays_repository_projection(
    tmp_path: Path,
) -> None:
    root = _sealed_repo(tmp_path, "sealed-relocated-runtime")
    runtime_srt = tmp_path / "package" / "renamed-final-reviewed.srt"
    runtime_srt.parent.mkdir(parents=True)
    shutil.copy2(root / SRT_RELATIVE, runtime_srt)

    assert _consume(root, reviewed_srt_path=runtime_srt) == _consume(root)


def test_differently_named_relocated_runtime_srt_byte_drift_blocks(tmp_path: Path) -> None:
    root = _sealed_repo(tmp_path, "sealed-relocated-runtime-drift")
    runtime_srt = tmp_path / "package" / "renamed-final-reviewed.srt"
    runtime_srt.parent.mkdir(parents=True)
    runtime_srt.write_bytes((root / SRT_RELATIVE).read_bytes() + b"\n")

    with pytest.raises(
        DeterministicTextSurfaceResolutionError,
        match="REVIEWED_SRT_RUNTIME_HASH_MISMATCH",
    ):
        _consume(root, reviewed_srt_path=runtime_srt)


def test_runtime_final_transcript_drift_blocks(tmp_path: Path) -> None:
    root = _sealed_repo(tmp_path, "sealed-transcript-drift")
    transcript = _final_transcript(root / SRT_RELATIVE) + "\n漂移"
    with pytest.raises(
        DeterministicTextSurfaceResolutionError,
        match="RUNTIME_FINAL_TRANSCRIPT_MISMATCH",
    ):
        _consume(root, final_transcript=transcript)


def test_runtime_clip_context_prompt_drift_blocks(tmp_path: Path) -> None:
    root = _sealed_repo(tmp_path, "sealed-context-drift")
    with pytest.raises(
        DeterministicTextSurfaceResolutionError,
        match="RUNTIME_CLIP_CONTEXT_PROMPT_MISMATCH",
    ):
        _consume(root, clip_context_prompt=_frozen_clip_context_prompt() + "\n漂移")


def test_runtime_selection_scorecard_drift_blocks(tmp_path: Path) -> None:
    root = _sealed_repo(tmp_path, "sealed-scorecard-drift")
    scorecard = copy.deepcopy(_FROZEN_SELECTION_SCORECARD)
    scorecard["effective_score"] = 91.6
    with pytest.raises(
        DeterministicTextSurfaceResolutionError,
        match="RUNTIME_SELECTION_SCORECARD_MISMATCH",
    ):
        _consume(root, selection_scorecard=scorecard)


def test_runtime_entity_context_drift_blocks(tmp_path: Path) -> None:
    root = _sealed_repo(tmp_path, "sealed-entity-context-drift")
    authority = load_deterministic_text_surface_authority(CANDIDATE_ID, root=root)
    assert authority is not None
    entity_context = copy.deepcopy(authority.failed_source_fact_receipt["entity_context"])
    assert isinstance(entity_context, Mapping)
    entity_context["final_reviewed_srt_sha256"] = "0" * 64
    with pytest.raises(
        DeterministicTextSurfaceResolutionError,
        match="RUNTIME_ENTITY_CONTEXT_MISMATCH",
    ):
        _consume(root, entity_context=entity_context)


def test_failed_receipt_snapshot_is_exact_wsl_artifact_object() -> None:
    receipt = _failed_receipt()
    body = dict(receipt)
    declared = body.pop("receipt_sha256")
    assert declared == canonical_sha256(body) == FAILED_SOURCE_FACT_RECEIPT_SHA256
    assert receipt["reason_code"] == "CPA_TEXT_REVIEW_INVALID"
    assert receipt["status"] == "FAILED"
    assert receipt["decision"] == "NONE"
    assert receipt["speaker_evidence_sha256"] == SPEAKER_EVIDENCE_SHA256
    assert [row["response_sha256"] for row in receipt["provider_retries"]] == [
        "sha256:96a8ee1c9bd50918cfd5045a0a3d73d5cd161c5fa3c4813ff334f119152f92e9",
        "sha256:ca5ad9e3decb70d536f2f54e41e447528bb138a69750a299249a57840e6aeecb",
    ]


@pytest.mark.parametrize(
    ("mutation", "error"),
    [
        (
            lambda d: d["source_binding"]["source_media"].__setitem__(
                "sha256", "sha256:" + "0" * 64
            ),
            "SOURCE_BINDING_MISMATCH",
        ),
        (
            lambda d: d["source_binding"]["exact_interval"].__setitem__(
                "end_ms", SOURCE_END_MS + 1
            ),
            "SOURCE_BINDING_MISMATCH",
        ),
        (
            lambda d: d["source_binding"]["reviewed_srt"].__setitem__(
                "sha256", "sha256:" + "1" * 64
            ),
            "REVIEWED_SRT_BINDING_MISMATCH",
        ),
        (
            lambda d: d["entity_binding"].__setitem__("projection_sha256", "0" * 64),
            "ENTITY_BINDING_MISMATCH",
        ),
        (
            lambda d: d["entity_binding"].__setitem__("required_surface", "星汐"),
            "ENTITY_BINDING_MISMATCH",
        ),
        (
            lambda d: d["uniform_host_authority"].__setitem__("speaker_mode", "required"),
            "UNIFORM_HOST_AUTHORITY_MISMATCH",
        ),
        (
            lambda d: d["source_binding"]["immutable_artifact_snapshots"][0].__setitem__(
                "sha256", "sha256:" + "2" * 64
            ),
            "IMMUTABLE_ARTIFACT_SNAPSHOT",
        ),
    ],
)
def test_source_entity_speaker_and_artifact_drift_fail_even_if_rehashed(
    mutation,
    error: str,
) -> None:
    document = _document()
    mutation(document)
    _rehash(document)
    with pytest.raises(DeterministicTextSurfaceResolutionError, match=error):
        validate_deterministic_text_surface_document(
            document,
            failed_source_fact_receipt=_failed_receipt(),
        )


@pytest.mark.parametrize(
    "replacement",
    [
        EXACT_TITLE.replace("“", "‘", 1),
        EXACT_TITLE.replace("，", ",", 1),
        EXACT_TITLE.replace("xxsk", "XXSK", 1),
        EXACT_TITLE + " ",
        EXACT_TITLE + "\n",
    ],
)
def test_any_exact_title_byte_drift_fails_after_consistent_rehash(replacement: str) -> None:
    document = _document()
    title = document["exact_surface_resolution"]["title"]
    title["value"] = replacement
    title["utf8_byte_length"] = len(replacement.encode("utf-8"))
    title["sha256"] = text_sha256(replacement)
    document["exact_surface_resolution"]["combined_sha256"] = combined_surface_sha256(
        replacement,
        EXACT_HOOK,
    )
    _rehash(document)
    with pytest.raises(DeterministicTextSurfaceResolutionError, match="EXACT_TITLE_BYTES"):
        validate_deterministic_text_surface_document(
            document,
            failed_source_fact_receipt=_failed_receipt(),
        )


@pytest.mark.parametrize(
    "forbidden",
    ["公主抱", "钓到", "成功", "xxsk说“被奶P赖上了”"],
)
def test_visual_causal_and_participant_bound_role_claims_cannot_enter_hook(
    forbidden: str,
) -> None:
    document = _document()
    changed = EXACT_HOOK + forbidden
    hook = document["exact_surface_resolution"]["hook"]
    hook["value"] = changed
    hook["utf8_byte_length"] = len(changed.encode("utf-8"))
    hook["sha256"] = text_sha256(changed)
    document["exact_surface_resolution"]["combined_sha256"] = combined_surface_sha256(
        EXACT_TITLE,
        changed,
    )
    _rehash(document)
    with pytest.raises(DeterministicTextSurfaceResolutionError, match="EXACT_HOOK_BYTES"):
        validate_deterministic_text_surface_document(
            document,
            failed_source_fact_receipt=_failed_receipt(),
        )


def test_unknown_or_free_form_plan_node_is_schema_failure() -> None:
    document = _document()
    document["closed_surface_plan"]["hook_nodes"].append(
        {"node_type": "free_form_factual_literal", "value": "任意事实"}
    )
    _rehash(document)
    with pytest.raises(
        DeterministicTextSurfaceResolutionError,
        match="CLOSED_SURFACE_PLAN_MISMATCH",
    ):
        validate_deterministic_text_surface_document(
            document,
            failed_source_fact_receipt=_failed_receipt(),
        )


def test_quote_speaker_and_order_swap_is_rejected() -> None:
    document = _document()
    title_nodes = document["closed_surface_plan"]["title_nodes"]
    title_nodes[2]["speaker"] = "host"
    title_nodes[3]["speaker"] = "entity"
    _rehash(document)
    with pytest.raises(
        DeterministicTextSurfaceResolutionError,
        match="CLOSED_SURFACE_PLAN_MISMATCH",
    ):
        validate_deterministic_text_surface_document(
            document,
            failed_source_fact_receipt=_failed_receipt(),
        )


def test_failed_source_fact_cannot_be_deleted_rewritten_or_marked_pass() -> None:
    receipt = _failed_receipt()
    receipt["status"] = "PASS"
    body = dict(receipt)
    body.pop("receipt_sha256")
    receipt["receipt_sha256"] = canonical_sha256(body)
    with pytest.raises(
        DeterministicTextSurfaceResolutionError,
        match="FAILED_SOURCE_FACT_RECEIPT_HASH_MISMATCH",
    ):
        validate_deterministic_text_surface_document(
            _document(),
            failed_source_fact_receipt=receipt,
        )


def test_clip_release_quote_cannot_become_exact_title_wildcard() -> None:
    document = _document()
    document["human_authority"]["clip_publication_release"]["exact_title_wildcard"] = True
    _rehash(document)
    with pytest.raises(
        DeterministicTextSurfaceResolutionError,
        match="CLIP_PUBLICATION_RELEASE_SCHEMA_INVALID",
    ):
        validate_deterministic_text_surface_document(
            document,
            failed_source_fact_receipt=_failed_receipt(),
        )


def test_unknown_authority_field_is_rejected() -> None:
    document = _document()
    document["hostname"] = "ROG-EYE"
    _rehash(document)
    with pytest.raises(
        DeterministicTextSurfaceResolutionError,
        match="AUTHORITY_SCHEMA_INVALID",
    ):
        validate_deterministic_text_surface_document(
            document,
            failed_source_fact_receipt=_failed_receipt(),
        )


def test_repository_seal_rejects_post_commit_byte_mutation(tmp_path: Path) -> None:
    root = _sealed_repo(tmp_path, "sealed-mutation")
    assert load_deterministic_text_surface_authority(CANDIDATE_ID, root=root) is not None
    path = root / ASSET_DIRECTORY / AUTHORITY_FILENAME
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(
        DeterministicTextSurfaceResolutionError,
        match="REPOSITORY_ASSET_UNSEALED",
    ):
        load_deterministic_text_surface_authority(CANDIDATE_ID, root=root)


def test_wsl_to_free_root_relocation_preserves_authority_identity(tmp_path: Path) -> None:
    wsl_root = _sealed_repo(tmp_path, "wsl-root")
    free_root = _sealed_repo(tmp_path, "free-root")
    wsl_receipt = _consume(wsl_root)
    free_receipt = _consume(free_root)

    assert wsl_receipt == free_receipt
    serialized = json.dumps(wsl_receipt, ensure_ascii=False, sort_keys=True)
    assert str(wsl_root) not in serialized
    assert str(free_root) not in serialized
    assert "ROG-EYE" not in serialized
    assert wsl_receipt["entity_projection_sha256"] == ENTITY_PROJECTION_SHA256
    assert hashlib.sha256(EXACT_TITLE.encode("utf-8")).hexdigest() == (
        "97bf7997892126ce9dfeed7d5e98a047a91b570513d6ad47c05fb7285afa9951"
    )
