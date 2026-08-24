"""Regression gates for the one permitted root-reviewed c6 public surface."""

from dataclasses import replace
import copy
import hashlib
import json
from pathlib import Path
import subprocess

import pytest

import src.autoslice.candidate_public_text_surface_authority as module
from src.autoslice.candidate_public_text_surface_authority import (
    CandidatePublicTextSurfaceAuthorityError,
    consume_candidate_public_text_surface_authority,
    load_candidate_public_text_surface_authority,
)


CID = "auto_120032_753_816"
ASSET = Path("assets/lidousha/candidate_public_text_surface_authorities") / f"{CID}.public-text-surface-authority.v1.json"


def _raw() -> dict[str, object]:
    return json.loads(ASSET.read_text(encoding="utf-8"))


def _sha(value: object) -> str:
    return "sha256:" + hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _runtime_authority():
    authority = load_candidate_public_text_surface_authority(CID)
    assert authority is not None and authority.is_root_reviewed_resolution
    prompt = "c6 exact clip context"
    story = {
        "candidate_id": CID,
        "selection_hook": authority.input_selection_hook,
        "source_media_sha256s": [str(authority.source_pieces[0]["source_media_sha256"])],
        "clip_context_binding": {"context_sha256": authority.clip_context_sha256},
        "clip_context_prompt": prompt,
        "unrelated_immutable_story_field": {"must": "not drift"},
    }
    return replace(
        authority,
        story_contract_sha256=_sha(story),
        clip_context_prompt_sha256="sha256:" + hashlib.sha256(prompt.encode()).hexdigest(),
    ), story


def test_c6_root_reviewed_authority_is_exact_and_public_only() -> None:
    authority = load_candidate_public_text_surface_authority(CID)
    assert authority is not None
    assert authority.user_authorization["timestamp"] == module.ROOT_REVIEWED_RULING_TIMESTAMP
    assert authority.decision_authorization == {
        "decision_owner": "Codex root", "source_scope": "IVAN_REQUESTED_REDO",
        "ruling_sha256": module.ROOT_REVIEWED_RULING_SHA256,
        "ruling_locator": module.ROOT_REVIEWED_RULING_LOCATOR,
    }
    authority.require_artifact_text(artifact_kind="cover", text="弹幕错写坏结果\n小李只听过大结果")
    with pytest.raises(CandidatePublicTextSurfaceAuthorityError, match="EXACT_COVER"):
        authority.require_artifact_text(artifact_kind="cover", text="错写")
    with pytest.raises(CandidatePublicTextSurfaceAuthorityError, match="OUT_OF_SCOPE"):
        authority.require_artifact_text(artifact_kind="subtitle", text="不得改字幕")


def test_c6_root_reviewed_runtime_allows_only_exact_hook_substitution() -> None:
    authority, story = _runtime_authority()
    old = consume_candidate_public_text_surface_authority(authority, candidate_id=CID, selection_hook=authority.input_selection_hook, story_contract=story)
    resolved_story = dict(story); resolved_story["selection_hook"] = authority.resolved_selection_hook
    resolved = consume_candidate_public_text_surface_authority(authority, candidate_id=CID, selection_hook=authority.resolved_selection_hook, story_contract=resolved_story)
    assert old["input_selection_hook_sha256"] == resolved["input_selection_hook_sha256"]
    assert resolved["observed_selection_hook_sha256"] != resolved["input_selection_hook_sha256"]
    assert resolved["resolved_cover_lines"] == ["弹幕错写坏结果", "小李只听过大结果"]
    drifted = dict(resolved_story); drifted["unrelated_immutable_story_field"] = {"must": "drifted"}
    with pytest.raises(CandidatePublicTextSurfaceAuthorityError, match="RUNTIME_BINDING"):
        consume_candidate_public_text_surface_authority(authority, candidate_id=CID, selection_hook=authority.resolved_selection_hook, story_contract=drifted)


@pytest.mark.parametrize("path,value", [
    (("candidate_binding", "candidate_id"), "auto_other_1"),
    (("candidate_binding", "recording_date"), "2026-08-15"),
    (("candidate_binding", "clip_context_sha256"), "sha256:" + "0" * 64),
    (("candidate_binding", "story_contract_sha256"), "sha256:" + "0" * 64),
    (("candidate_binding", "clip_context_prompt_sha256"), "sha256:" + "0" * 64),
    (("decision_authorization", "decision_owner"), "IVAN_EXACT_TEXT"),
    (("decision_authorization", "source_scope"), "GENERIC"),
    (("decision_authorization", "ruling_sha256"), "sha256:" + "0" * 64),
    (("decision_authorization", "ruling_locator"), "elsewhere:1"),
    (("user_authorization", "quote"), "different quote"),
    (("user_authorization", "timestamp"), "2026-08-19T00:08:53.249Z"),
    (("resolved_surfaces", "title_sha256"), "sha256:" + "0" * 64),
    (("resolved_surfaces", "selection_hook_sha256"), "sha256:" + "0" * 64),
    (("resolved_surfaces", "cover_lines_sha256"), "sha256:" + "0" * 64),
    (("scope", "upload_authorized"), True),
    (("scope", "subtitle_text_mutation_authorized"), True),
    (("scope", "speaker_label_mutation_authorized"), True),
    (("scope", "registry_hold_released"), True),
])
def test_c6_root_reviewed_schema_rejects_authority_drift(path: tuple[str, str], value: object) -> None:
    raw = copy.deepcopy(_raw())
    raw[path[0]][path[1]] = value  # type: ignore[index]
    with pytest.raises(CandidatePublicTextSurfaceAuthorityError):
        module._freeze_authority(raw)


@pytest.mark.parametrize("field,value", [("absolute_start_ms", 753001), ("absolute_end_ms", 815999)])
def test_c6_root_reviewed_rejects_interval_drift(field: str, value: int) -> None:
    raw = _raw(); raw["candidate_binding"]["selected_interval"][field] = value  # type: ignore[index]
    with pytest.raises(CandidatePublicTextSurfaceAuthorityError, match="ROOT_DECISION"):
        module._freeze_authority(raw)


def test_c6_root_reviewed_rejects_source_piece_drift() -> None:
    raw = _raw(); raw["candidate_binding"]["clip_context_source_pieces"][0]["source_media_sha256"] = "sha256:" + "0" * 64  # type: ignore[index]
    with pytest.raises(CandidatePublicTextSurfaceAuthorityError, match="ROOT_DECISION"):
        module._freeze_authority(raw)


def test_other_candidate_cannot_use_root_reviewed_algorithm() -> None:
    raw = copy.deepcopy(_raw())
    raw["candidate_binding"]["candidate_id"] = "auto_999999_1_2"  # type: ignore[index]
    with pytest.raises(CandidatePublicTextSurfaceAuthorityError, match="ROOT_DECISION"):
        module._freeze_authority(raw)


def test_c6_root_reviewed_file_must_be_git_sealed(tmp_path: Path) -> None:
    repo = tmp_path / "repo"; repo.mkdir()
    subprocess.run(["git", "init", "--quiet", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "test"], check=True)
    asset = repo / ASSET; asset.parent.mkdir(parents=True)
    asset.write_bytes(ASSET.read_bytes())
    subprocess.run(["git", "-C", str(repo), "add", ASSET.as_posix()], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "--quiet", "-m", "seal c6"], check=True)
    assert load_candidate_public_text_surface_authority(CID, root=repo) is not None
    asset.write_bytes(asset.read_bytes() + b"\n")
    with pytest.raises(CandidatePublicTextSurfaceAuthorityError, match="UNSEALED"):
        load_candidate_public_text_surface_authority(CID, root=repo)
