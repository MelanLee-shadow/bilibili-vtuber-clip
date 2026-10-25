"""Small path/text helpers shared by publish staging entry points."""

from __future__ import annotations

from src.autoslice.evidence_identity import file_sha256 as _evidence_file_sha256
from collections.abc import Callable, Mapping, Sequence
import hashlib
import json
from pathlib import Path

from src.autoslice.review_evidence import SourceCue
from src.autoslice.story_contract import cover_story_contract_binding


def editorial_input_binding(
    record: Mapping[str, object], *, candidate_id: str, title: str,
    selection_hook: str, editorial_research: object = None,
) -> dict[str, object]:
    """Describe the actual current editorial input, independent of audit annotations.

    Locators are deliberately excluded: portable/prepared consumers may relocate
    the same bytes. This is an identity, never a semantic PASS or publication grant.
    """
    def digest(value: object) -> str:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return "sha256:" + hashlib.sha256(raw).hexdigest()

    subtitle = record.get("subtitle_path")
    subtitle_sha = None
    if subtitle:
        path = Path(str(subtitle))
        if path.is_symlink() or not path.is_file():
            raise ValueError("CURRENT_EDITORIAL_SUBTITLE_INVALID")
        subtitle_sha = "sha256:" + _evidence_file_sha256(path)
    story = record.get("story_contract")
    body = {
        "schema_version": "current-editorial-input.v1",
        "candidate_id": candidate_id,
        "title_sha256": "sha256:" + hashlib.sha256(title.encode("utf-8")).hexdigest(),
        "subtitle_sha256": subtitle_sha,
        "selection_hook_sha256": "sha256:" + hashlib.sha256(selection_hook.encode("utf-8")).hexdigest(),
        "story_sha256": digest(cover_story_contract_binding(story)) if isinstance(story, Mapping) else None,
        "editorial_research_sha256": digest(editorial_research) if editorial_research is not None else None,
    }
    return {**body, "input_sha256": digest(body)}


def current_editorial_binding(
    record: Mapping[str, object], staging: Mapping[str, object], *, candidate_id: str,
) -> dict[str, object]:
    story = record.get("story_contract")
    hook = str(story.get("selection_hook") or "") if isinstance(story, Mapping) else ""
    inputs = editorial_input_binding(
        record, candidate_id=candidate_id, title=str(staging.get("title") or ""),
        selection_hook=hook, editorial_research=staging.get("editorial_research"),
    )
    cover = staging.get("cover_path")
    cover_sha = None
    if cover:
        path = Path(str(cover))
        if path.is_symlink() or not path.is_file():
            raise ValueError("CURRENT_EDITORIAL_COVER_INVALID")
        cover_sha = "sha256:" + _evidence_file_sha256(path)
    return {**inputs, "cover_sha256": cover_sha}


def validate_current_editorial_binding(
    record: Mapping[str, object], staging: Mapping[str, object], *, candidate_id: str,
    publish: Mapping[str, object] | None = None,
) -> None:
    """Replay a normal staging binding before the prepared package consumes it.

    Historical packages without the new field retain their existing native
    gates; once emitted, the field cannot be absent or drift on another surface.
    """
    declared = staging.get("current_binding")
    if declared is None:
        generation = staging.get("cover_generation")
        if staging.get("title_selection") is not None or (isinstance(generation, Mapping) and generation.get("input_binding") is not None) or (publish is not None and publish.get("current_binding") is not None):
            raise ValueError("CURRENT_EDITORIAL_BINDING_MISSING")
        return
    actual = current_editorial_binding(record, staging, candidate_id=candidate_id)
    if declared != actual:
        raise ValueError("CURRENT_EDITORIAL_BINDING_DRIFT")
    if record.get("candidate_id") != candidate_id or staging.get("candidate_id") != candidate_id:
        raise ValueError("CURRENT_EDITORIAL_CANDIDATE_DRIFT")
    selection = staging.get("title_selection")
    if isinstance(selection, Mapping) and (
        selection.get("final_title") != staging.get("title")
        or selection.get("final_title_sha256") != actual["title_sha256"]
        or selection.get("final_input_sha256") != actual["input_sha256"]
    ):
        raise ValueError("CURRENT_EDITORIAL_SELECTION_DRIFT")
    if publish is not None:
        if any(publish.get(key) != staging.get(key) for key in (
            "candidate_id", "title", "cover_status", "cover_generation", "current_binding", "title_selection", "title_generation_attempts",
        )):
            raise ValueError("CURRENT_EDITORIAL_PUBLISH_DRIFT")
        if publish.get("subtitle_sha256") != actual["subtitle_sha256"]:
            raise ValueError("CURRENT_EDITORIAL_SUBTITLE_DRIFT")
    for attempt in staging.get("title_generation_attempts") or []:
        if not isinstance(attempt, Mapping):
            raise ValueError("CURRENT_EDITORIAL_TITLE_ATTEMPT_INVALID")
        original_inputs = attempt.get("input_binding")
        if isinstance(original_inputs, Mapping) and (
            not isinstance(attempt.get("request_text"), str)
            or (attempt.get("response_sha256") is not None and not isinstance(attempt.get("response_text"), str))
        ):
            raise ValueError("CURRENT_EDITORIAL_TITLE_ORIGINAL_MISSING")
        if isinstance(original_inputs, Mapping) and (
            original_inputs.get("candidate_id") != candidate_id
            or original_inputs.get("subtitle_sha256") != actual["subtitle_sha256"]
        ):
            raise ValueError("CURRENT_EDITORIAL_TITLE_INPUT_DRIFT")
        for kind in ("request", "response"):
            raw = attempt.get(kind + "_text")
            if raw is not None:
                encoded = json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
                if "sha256:" + hashlib.sha256(encoded).hexdigest() != attempt.get(kind + "_sha256"):
                    raise ValueError("CURRENT_EDITORIAL_TITLE_ORIGINAL_DRIFT")
    from .title_cover_quotes import validate_title_skill_selection
    validate_title_skill_selection(record, staging)
    generation = staging.get("cover_generation")
    if isinstance(generation, Mapping) and actual["cover_sha256"] is not None:
        if generation.get("method") == "skill.candidates":
            from .title_cover_candidates import validate_choice_bundle
            problems = validate_choice_bundle(generation, final_cover=Path(str(staging["cover_path"])))
            if problems:
                raise ValueError("CURRENT_EDITORIAL_COVER_CHOICE_INVALID:" + "; ".join(problems))
        if generation.get("final_cover_sha256") not in (None, actual["cover_sha256"]):
            raise ValueError("CURRENT_EDITORIAL_COVER_DRIFT")
        generated_inputs = generation.get("input_binding")
        if staging.get("cover_status") == "AI_COVER_READY" and generated_inputs is not None:
            if generated_inputs != {key: value for key, value in actual.items() if key != "cover_sha256"}:
                raise ValueError("CURRENT_EDITORIAL_GENERATION_DRIFT")
        for key in ("request", "response"):
            expected = generation.get(key + "_sha256")
            if expected is not None:
                path = Path(str(generation.get(key + "_path") or ""))
                if path.is_symlink() or not path.is_file() or "sha256:" + _evidence_file_sha256(path) != expected:
                    raise ValueError("CURRENT_EDITORIAL_NETWORK_EVIDENCE_DRIFT")
    tags = record.get("upload_tags")
    if isinstance(tags, Mapping) and isinstance(tags.get("input_binding"), Mapping):
        bound = tags["input_binding"]
        if bound.get("title_sha256") != actual["title_sha256"] or bound.get("srt_sha256") != actual["subtitle_sha256"]:
            raise ValueError("CURRENT_EDITORIAL_TAG_DRIFT")


def validate_staged_editorial_files(
    record: Mapping[str, object], staging: Mapping[str, object], *, candidate_id: str,
    subtitle_path: Path,
) -> None:
    """Normal finalizer/prepare choke point, before any delivery bytes are copied."""
    generation = staging.get("cover_generation")
    if staging.get("current_binding") is None and staging.get("title_selection") is None and not (isinstance(generation, Mapping) and generation.get("input_binding") is not None):
        return
    path = Path(str(staging.get("publish_json_path") or ""))
    if path.is_symlink() or not path.is_file():
        raise ValueError("CURRENT_EDITORIAL_PUBLISH_MISSING")
    publish = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(publish, Mapping):
        raise ValueError("CURRENT_EDITORIAL_PUBLISH_INVALID")
    validate_current_editorial_binding(
        {**record, "subtitle_path": str(subtitle_path)}, staging,
        candidate_id=candidate_id, publish=publish,
    )


def validate_package_editorial_binding(
    record: Mapping[str, object], publish: Mapping[str, object], *, candidate_id: str,
    subtitle_path: Path, cover_path: Path, request_path: Path | None = None,
    response_path: Path | None = None, choice_path: Path | None = None,
    speaker_srt_path: Path | None = None,
) -> None:
    """Replay current identity on package-owned bytes, without opening old locators.

    The package resolver must first prove these are regular package-internal
    files. Mirrors stay intact; only this read-only view uses portable locators.
    """
    staging = record.get("publish_staging")
    if not isinstance(staging, Mapping):
        generation = publish.get("cover_generation")
        if (
            publish.get("current_binding") is not None
            or publish.get("title_selection") is not None
            or (isinstance(generation, Mapping) and generation.get("input_binding") is not None)
        ):
            raise ValueError("CURRENT_EDITORIAL_STAGING_MISSING")
        return
    generation = staging.get("cover_generation")
    generation = dict(generation) if isinstance(generation, Mapping) else {}
    # Check the original mirror before introducing package-local aliases.
    if staging.get("current_binding") is not None and generation != publish.get("cover_generation"):
        raise ValueError("CURRENT_EDITORIAL_PUBLISH_DRIFT")
    for key, path in (("request", request_path), ("response", response_path)):
        if generation.get(key + "_sha256") is not None:
            if path is None:
                raise ValueError("CURRENT_EDITORIAL_NETWORK_EVIDENCE_MISSING")
            generation[key + "_path"] = str(path)
    if generation.get("method") == "skill.candidates":
        if choice_path is None:
            raise ValueError("CURRENT_EDITORIAL_COVER_CHOICE_PATH_MISSING")
        generation["candidate_selection"] = {
            **generation["candidate_selection"], "choose_path": str(choice_path),
        }
    current_record = {**record, "subtitle_path": str(subtitle_path)}
    if (staging.get("title_selection") or {}).get("title_cover_skill") is not None and record.get("speaker_review_srt_path"):
        if speaker_srt_path is None:
            raise ValueError("CURRENT_EDITORIAL_TITLE_SPEAKER_PATH_MISSING")
        current_record["speaker_review_srt_path"] = str(speaker_srt_path)
    current_staging = {**staging, "cover_path": str(cover_path), "cover_generation": generation}
    current_publish = {**publish, "cover_path": str(cover_path), "cover_generation": generation}
    validate_current_editorial_binding(
        current_record, current_staging,
        candidate_id=candidate_id, publish=current_publish,
    )


def materialized_artifact_root(materialized_recut: Mapping[str, object], media_path: Path) -> Path:
    manifest_value = materialized_recut.get("manifest_path")
    if isinstance(manifest_value, str) and manifest_value:
        manifest_path = Path(manifest_value)
        return manifest_path.parent.parent if manifest_path.parent.name in {"recuts", "media"} else manifest_path.parent
    return media_path.parent.parent if media_path.parent.name in {"recuts", "media"} else media_path.parent


def staged_transcript_sample(record: Mapping[str, object], cues: Sequence[SourceCue]) -> str:
    subtitle_path = record.get("subtitle_path")
    if isinstance(subtitle_path, str) and Path(subtitle_path).is_file():
        try:
            from src.autoslice.jingting_chunker import parse_srt_cues

            sample = " ".join(" ".join(cue.text.split()) for cue in parse_srt_cues(Path(subtitle_path).read_text(encoding="utf-8")) if cue.text.strip())[:600]
            if sample:
                return sample
        except OSError:
            pass
    return " ".join(cue.text.strip() for cue in cues if cue.text.strip())[:600]


def private_publish_path(
    *, media_path: Path, private_artifact_root: Path | None, private_publish_json_path: Path | None, stage_cover: object
) -> Path:
    if private_publish_json_path is not None and private_artifact_root is None:
        raise ValueError("private publish output requires a private artifact root")
    if private_artifact_root is not None and stage_cover is not None:
        raise ValueError("private artifact staging requires the canonical cover stage")
    if private_artifact_root is None:
        return media_path.with_suffix(".publish.json")
    output = private_publish_json_path or private_artifact_root / media_path.with_suffix(".publish.json").name
    try:
        output.relative_to(private_artifact_root)
    except ValueError as exc:
        raise ValueError("private publish output escapes the private artifact root") from exc
    return output


def stage_cover_with_private_root(
    stage_cover: Callable[..., dict[str, object]], record: Mapping[str, object], *, private_artifact_root: Path | None, kwargs: dict[str, object]
) -> dict[str, object]:
    if private_artifact_root is not None:
        kwargs["private_artifact_root"] = private_artifact_root
    return stage_cover(record, **kwargs)
