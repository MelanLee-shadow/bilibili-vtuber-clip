"""Shared formal/observational after-image predicates for the Qixi closure.

The public-surface transaction retains the authority and journal plumbing.
This module owns only pure replay predicates so the formal validator and the
diagnostic matrix cannot silently diverge.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.autoslice.qixi_post_correction_projection_paths import (
    package_cover_paths,
    replayed_cover_gate_callables,
)
from src.autoslice.qixi_post_correction_diagnostics import sha256_bytes
from src.autoslice.qixi_post_correction_sealed_replay import sealed_chat_authority_bytes
from src.autoslice.review_package_portable_evidence import rebuild_package_speaker_evidence
from src.autoslice.source_fact_review import validate_source_fact_review


AFTER_IMAGE_PREDICATE_IDS = (
    "after_image_target_roles",
    "after_image_json_documents",
    "record_delivery_media_closure",
    "final_media_hashes",
    "source_fact_public_mirrors",
    "manual_title_authority_mapping",
    "immutable_record_hashes",
    "record_mutation_scope",
    "story_contract_replay",
    "source_fact_receipt_replay",
    "cover_generation_shape",
    "cover_route",
    "cover_rendered_text_pixels",
    "cover_final_host_identity",
    "cover_final_participant_identity",
    "cover_punch_semantics",
    "cover_materialized_hashes",
    "cover_projection",
    "publish_mutation_scope",
    "publish_artifact_hashes",
    "state_exact_diff",
    "immutable_target_scope",
)


@dataclass(frozen=True, slots=True)
class AfterImageContext:
    authority: Mapping[str, object]
    paths: dict[str, Path]
    state_path: Path
    fixed: set[Path]
    before: Mapping[Path, bytes | None]
    after: Mapping[Path, bytes]
    record: dict[str, object]
    delivery: dict[str, object]
    publish: dict[str, object]
    before_record: dict[str, object]
    before_publish: dict[str, object]
    before_state: dict[str, object]
    state: dict[str, object]


@dataclass(frozen=True, slots=True)
class AfterImageGate:
    predicate_id: str
    check: Callable[[], None]
    dependencies: tuple[str, ...] = ()
    not_required: Callable[[], bool] | None = None


def _cover_gate_map(
    closure: Any,
    *,
    generation: object,
    story: object,
    package_root: Path,
    after: Mapping[Path, bytes],
) -> dict[str, Callable[[], None]]:
    """Keep every cover gate present even when its generation dependency fails."""

    cover_gates = (
        replayed_cover_gate_callables(
            generation,
            story=story if isinstance(story, Mapping) else {},
            package_root=package_root,
            after=after,
        )
        if isinstance(generation, Mapping)
        else ()
    )
    indexed = {predicate_id: check for predicate_id, check in cover_gates}

    def unavailable() -> None:
        _error(closure, "journal cover generation is invalid")

    return {
        predicate_id: indexed.get(predicate_id, unavailable)
        for predicate_id in (
            "cover_route",
            "cover_rendered_text_pixels",
            "cover_final_host_identity",
            "cover_final_participant_identity",
            "cover_punch_semantics",
            "cover_materialized_hashes",
        )
    }


def _error(closure: Any, message: str) -> None:
    raise closure.QixiPostCorrectionPublicSurfaceError(message)


def _generation_owned_hashes(
    closure: Any,
    *,
    generation: object,
    after: Mapping[Path, bytes],
) -> dict[str, str]:
    """Return the only mutable record artifact hashes owned by a new cover."""

    if not isinstance(generation, Mapping):
        _error(closure, "journal cover generation is invalid")
    owned: dict[str, str] = {}
    for artifact_hash_key, generation_hash_key, path_key in (
        ("cover_sha256", "final_cover_sha256", "final_cover"),
        ("ai_background_sha256", "ai_background_sha256", "ai_background"),
    ):
        digest, raw_path = generation.get(generation_hash_key), generation.get(path_key)
        if not isinstance(digest, str) or not isinstance(raw_path, str):
            _error(closure, "journal cover-owned artifact hash binding drifts")
        path = Path(raw_path)
        if path not in after or sha256_bytes(after[path]) != digest:
            _error(closure, "journal cover-owned artifact hash binding drifts")
        owned[artifact_hash_key] = digest
    return owned


def _expected_public_artifact_hashes(
    closure: Any,
    *,
    context: AfterImageContext,
    generation: object,
) -> dict[str, str]:
    """Project the sealed record hash map through the canonical cover stage."""

    before_hashes = context.before_record.get("artifact_hashes")
    record_hashes = context.record.get("artifact_hashes")
    if not isinstance(before_hashes, Mapping) or not isinstance(record_hashes, Mapping):
        _error(closure, "journal predecessor artifact hashes are missing")
    expected = {str(key): str(value) for key, value in before_hashes.items()}
    expected.update(_generation_owned_hashes(closure, generation=generation, after=context.after))
    if dict(record_hashes) != expected:
        _error(closure, "journal immutable artifact hash drifts")
    return expected


def build_context(
    closure: Any,
    *,
    authority: Mapping[str, object],
    before: Mapping[Path, bytes | None],
    after: Mapping[Path, bytes],
) -> AfterImageContext:
    paths, state_path, fixed = validate_target_roles(closure, authority=authority, after=after)
    try:
        values = {
            "record": json.loads(after[paths["record"]].decode("utf-8")),
            "delivery": json.loads(after[paths["delivery_record"]].decode("utf-8")),
            "publish": json.loads(after[paths["publish"]].decode("utf-8")),
            "before_record": json.loads((before[paths["record"]] or b"").decode("utf-8")),
            "before_publish": json.loads((before[paths["publish"]] or b"").decode("utf-8")),
            "before_state": json.loads((before[state_path] or b"").decode("utf-8")),
            "state": json.loads(after[state_path].decode("utf-8")),
        }
    except (UnicodeError, ValueError) as exc:
        raise closure.QixiPostCorrectionPublicSurfaceError(
            "journal after-image JSON is invalid"
        ) from exc
    if not all(isinstance(value, dict) for value in values.values()):
        _error(closure, "journal after-image document type drifts")
    return AfterImageContext(
        authority=authority,
        paths=paths,
        state_path=state_path,
        fixed=fixed,
        before=before,
        after=after,
        record=values["record"],
        delivery=values["delivery"],
        publish=values["publish"],
        before_record=values["before_record"],
        before_publish=values["before_publish"],
        before_state=values["before_state"],
        state=values["state"],
    )


def validate_target_roles(
    closure: Any,
    *,
    authority: Mapping[str, object],
    after: Mapping[Path, bytes],
) -> tuple[dict[str, Path], Path, set[Path]]:
    """Validate only target-role scope before decoding any after-image JSON."""

    artifacts = authority["artifacts"]
    assert isinstance(artifacts, Mapping)
    paths = {
        role: Path(str(closure._descriptor(value, label=f"artifact {role}")["path"]))
        for role, value in artifacts.items()
    }
    state_path = Path(str(closure._state_descriptor(authority["state_file"])["path"]))
    fixed = {paths["record"], paths["delivery_record"], paths["publish"], state_path}
    if not fixed.issubset(after) or any(
        path in after
        for path in (
            paths["main"],
            paths["srt"],
            paths["ass"],
            paths["burn"],
            paths["correction"],
        )
    ):
        _error(closure, "journal after-image target roles drift")
    return paths, state_path, fixed


def gate_callables(closure: Any, context: AfterImageContext) -> tuple[AfterImageGate, ...]:
    """Return the fixed predicate IDs used by formal replay and diagnostics."""

    artifacts = context.authority["artifacts"]
    assert isinstance(artifacts, Mapping)
    record, delivery, publish = context.record, context.delivery, context.publish
    before_record, before_publish, before_state = (
        context.before_record,
        context.before_publish,
        context.before_state,
    )
    paths = context.paths
    hashes = record.get("artifact_hashes")
    staging, story = record.get("publish_staging"), record.get("story_contract")
    source_fact = staging.get("source_fact_review") if isinstance(staging, Mapping) else None
    generation = staging.get("cover_generation") if isinstance(staging, Mapping) else None

    def cover_generation_shape() -> None:
        if not isinstance(generation, Mapping):
            _error(closure, "journal cover generation is invalid")

    def participant_not_required() -> bool:
        if not isinstance(generation, Mapping):
            return False
        route = generation.get("route_decision")
        participants = route.get("required_participant_ids") if isinstance(route, Mapping) else None
        return not bool(participants)

    def record_delivery_media() -> None:
        if (
            record != delivery
            or record.get("media_path") != str(paths["main"])
            or record.get("subtitle_path") != str(paths["srt"])
            or record.get("subtitle_ass_path") != str(paths["ass"])
            or record.get("human_text_correction_manifest_path") != str(paths["correction"])
            or record.get("human_text_correction_manifest_sha256")
            != artifacts["correction"]["sha256"]
        ):
            _error(closure, "journal record/delivery media closure drifts")

    def final_media_hashes() -> None:
        if not isinstance(hashes, Mapping) or any(
            hashes.get(key) != artifacts[role]["sha256"]
            for key, role in (
                ("video_sha256", "main"),
                ("subtitle_sha256", "srt"),
                ("ass_sha256", "ass"),
                ("burned_video_sha256", "burn"),
            )
        ):
            _error(closure, "journal final media binding drifts")

    def source_fact_mirrors() -> None:
        if (
            not isinstance(staging, Mapping)
            or not isinstance(story, Mapping)
            or staging.get("title") != closure.PUBLIC_TITLE
            or publish.get("title") != closure.PUBLIC_TITLE
            or staging.get("upload_enabled") is not False
            or not isinstance(source_fact, Mapping)
            or source_fact.get("status") != "PASS"
            or story.get("source_fact_review") != source_fact
            or publish.get("source_fact_review") != source_fact
            or publish.get("cover_generation") != generation
            or publish.get("upload_enabled") is not False
        ):
            _error(closure, "journal source-fact/title/cover mirrors drift")

    def manual_title_mapping() -> None:
        if not isinstance(staging, Mapping) or not isinstance(source_fact, Mapping):
            _error(closure, "journal manual title authority projection drifts")
        closure._validate_manual_title_projection(
            staging, publish, source_fact=source_fact, public_title=closure.PUBLIC_TITLE
        )

    def immutable_record_hashes() -> None:
        _expected_public_artifact_hashes(
            closure, context=context, generation=generation
        )

    def record_mutation_scope() -> None:
        if any(
            before_record.get(key) != record.get(key)
            for key in set(before_record) | set(record)
            if key not in {"story_contract", "publish_staging", "artifact_hashes"}
        ):
            _error(closure, "journal record closure mutates an unrelated field")

    def story_contract() -> None:
        hook = story.get("selection_hook") if isinstance(story, Mapping) else None
        if not isinstance(hook, str):
            hook = (before_record.get("story_contract") or {}).get("selection_hook")
        try:
            expected = (
                closure._story_contract_rebuilder(
                    before_record,
                    srt_path=paths["srt"],
                    clip_context_path=paths["clip_context"],
                )(hook)
                if isinstance(hook, str)
                else None
            )
        except Exception as exc:
            raise closure.QixiPostCorrectionPublicSurfaceError(
                "journal StoryContract input cannot replay"
            ) from exc
        if isinstance(expected, dict):
            expected["source_fact_review"] = source_fact
        if not isinstance(hook, str) or story != expected:
            _error(closure, "journal StoryContract replay drifts")

    def source_fact_receipt() -> None:
        if not isinstance(story, Mapping) or not isinstance(source_fact, Mapping):
            _error(closure, "journal source-fact receipt replay drifts")
        hook = story.get("selection_hook") or (before_record.get("story_contract") or {}).get(
            "selection_hook"
        )
        package_root = paths["record"].parent
        try:
            speaker_evidence = rebuild_package_speaker_evidence(
                root=package_root,
                item={"subtitle_srt": paths["srt"].name},
                record=before_record,
                subtitle_path=paths["srt"],
            )
        except (OSError, ValueError) as exc:
            raise closure.QixiPostCorrectionPublicSurfaceError(
                "journal uniform-host speaker evidence replay drifts"
            ) from exc
        from src.autoslice.qixi_operator_exact_title_source_fact import (
            validate_public_surface_receipt,
        )

        if not validate_public_surface_receipt(
            source_fact,
            generic_validator=validate_source_fact_review,
            selection_hook=hook,
            title=closure.PUBLIC_TITLE,
            final_transcript="\n".join(cue.text for cue in closure._source_cues(paths["srt"])),
            clip_context_prompt=str(story.get("clip_context_prompt") or ""),
            selection_scorecard=story.get("selection_scorecard"),
            final_reviewed_srt_path=paths["srt"],
            record=record,
            speaker_evidence=speaker_evidence,
            repo_root=closure.ROOT,
            sealed_chat_authority_bytes=sealed_chat_authority_bytes(),
        ):
            _error(closure, "journal source-fact receipt replay drifts")

    def cover_projection() -> None:
        cover_path = generation.get("final_cover") if isinstance(generation, Mapping) else None
        cover_sha = generation.get("final_cover_sha256") if isinstance(generation, Mapping) else None
        if (
            not isinstance(cover_path, str)
            or not isinstance(staging, Mapping)
            or staging.get("cover_path") != cover_path
            or publish.get("cover_path") != cover_path
            or not isinstance(hashes, Mapping)
            or hashes.get("cover_sha256") != cover_sha
            or Path(cover_path) not in context.after
        ):
            _error(closure, "journal cover projection drifts")

    def publish_mutation_scope() -> None:
        allowed = {
            "title", "title_source", "title_authority_status", "recovery_publication_authority",
            "title_authority_error", "title_policy_violations", "important_content_ips",
            "title_story_audit", "entity_projection_audit", "cover_entity_projection_audit",
            "source_fact_review", "manual_title_repair_authority_consumption",
            "manual_title_keep_authority_consumption", "public_text_surface_authority_consumption",
            "video_path", "cover_text", "cover_generation", "cover_path", "cover_status",
            "reason_codes", "artifact_hashes", "upload_enabled",
            "source_fact_scorecard_rescore_provenance",
        }
        if any(
            before_publish.get(key) != publish.get(key)
            for key in set(before_publish) | set(publish)
            if key not in allowed
        ):
            _error(closure, "journal publish closure mutates an unrelated field")

    def publish_hashes() -> None:
        publish_hashes = publish.get("artifact_hashes")
        expected = _expected_public_artifact_hashes(
            closure, context=context, generation=generation
        )
        if not isinstance(publish_hashes, Mapping) or dict(publish_hashes) != expected:
            _error(closure, "journal publish artifact hash projection drifts")

    def state_exact_diff() -> None:
        picks = before_state.get("picks")
        indices = [
            index
            for index, pick in enumerate(picks or [])
            if isinstance(pick, Mapping) and pick.get("candidate_id") == closure.CANDIDATE_ID
        ]
        if (
            not isinstance(picks, list)
            or len(indices) != 1
            or not closure._state_diff_is_exact(
                before_state, context.state, pick_index=indices[0], candidate_id=closure.CANDIDATE_ID
            )
            or context.state != closure._project_state_pick(
                before_state,
                pick_index=indices[0],
                candidate_id=closure.CANDIDATE_ID,
                record=record,
                publish=publish,
            )
        ):
            _error(closure, "journal state closure drifts")

    def immutable_target_scope() -> None:
        package_root = paths["record"].parent
        referenced = package_cover_paths(generation, package_root=package_root)
        referenced.update(package_cover_paths(staging.get("cover_path") if isinstance(staging, Mapping) else None, package_root=package_root))
        referenced.update(package_cover_paths(publish.get("cover_path"), package_root=package_root))
        extras = set(context.after) - context.fixed
        if not extras.issubset(referenced) or any(
            not closure._within(path, package_root) for path in extras
        ):
            _error(closure, "journal cover sidecar inventory drifts")

    gate_map = _cover_gate_map(
        closure,
        generation=generation,
        story=story,
        package_root=paths["record"].parent,
        after=context.after,
    )
    return (
        AfterImageGate("record_delivery_media_closure", record_delivery_media),
        AfterImageGate("final_media_hashes", final_media_hashes),
        AfterImageGate("source_fact_public_mirrors", source_fact_mirrors),
        AfterImageGate("manual_title_authority_mapping", manual_title_mapping),
        AfterImageGate("immutable_record_hashes", immutable_record_hashes),
        AfterImageGate("record_mutation_scope", record_mutation_scope),
        AfterImageGate("story_contract_replay", story_contract),
        AfterImageGate("source_fact_receipt_replay", source_fact_receipt),
        AfterImageGate("cover_generation_shape", cover_generation_shape),
        AfterImageGate("cover_route", gate_map["cover_route"], ("cover_generation_shape",)),
        AfterImageGate(
            "cover_rendered_text_pixels",
            gate_map["cover_rendered_text_pixels"],
            ("cover_generation_shape",),
        ),
        AfterImageGate(
            "cover_final_host_identity",
            gate_map["cover_final_host_identity"],
            ("cover_generation_shape",),
        ),
        AfterImageGate(
            "cover_final_participant_identity",
            gate_map["cover_final_participant_identity"],
            ("cover_generation_shape",),
            participant_not_required,
        ),
        AfterImageGate(
            "cover_punch_semantics",
            gate_map["cover_punch_semantics"],
            ("cover_generation_shape", "story_contract_replay"),
        ),
        AfterImageGate(
            "cover_materialized_hashes",
            gate_map["cover_materialized_hashes"],
            ("cover_generation_shape",),
        ),
        AfterImageGate("cover_projection", cover_projection, ("cover_generation_shape",)),
        AfterImageGate("publish_mutation_scope", publish_mutation_scope),
        AfterImageGate("publish_artifact_hashes", publish_hashes, ("cover_generation_shape",)),
        AfterImageGate("state_exact_diff", state_exact_diff),
        AfterImageGate("immutable_target_scope", immutable_target_scope, ("cover_generation_shape",)),
    )


def validate_after_image(
    closure: Any,
    *,
    authority: Mapping[str, object],
    before: Mapping[Path, bytes | None],
    after: Mapping[Path, bytes],
) -> set[Path]:
    """Run the same ordered gates fail-closed, returning permitted targets."""

    context = build_context(closure, authority=authority, before=before, after=after)
    for gate in gate_callables(closure, context):
        gate.check()
    generation = context.record["publish_staging"]["cover_generation"]
    assert isinstance(generation, Mapping)
    referenced = package_cover_paths(generation, package_root=context.paths["record"].parent)
    referenced.update(
        package_cover_paths(
            context.record["publish_staging"].get("cover_path"),
            package_root=context.paths["record"].parent,
        )
    )
    referenced.update(
        package_cover_paths(context.publish.get("cover_path"), package_root=context.paths["record"].parent)
    )
    return context.fixed | referenced
