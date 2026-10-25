"""Focused proof-closure fingerprints for the unattended runner."""

from __future__ import annotations

import hashlib
import ast
import re
import json
import os
from collections.abc import Callable, Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any


TALK_DEPENDENCY_FIELD = "talk_dependency_snapshot"
TALK_DEPENDENCY_SCHEMA = "talk-dependency-snapshot.v1"
# The runner/deploy/queue machinery records provenance but is not a media proof
# dependency.  Roots are the consumers which actually create/check each surface.
TALK_STAGE_ROOTS = {
    "source": ("producer_source_media", "diarized_transcription", "source_integrity"),
    "boundary": ("producer_boundary_resolution", "producer_boundary_review_stage", "producer_source_boundary_review"),
    "speaker": ("speaker_finalizer", "speaker_evidence", "speaker_guess"),
    "text": ("producer_text_pipeline", "producer_text_finalization", "producer_source_truth_authority"),
    "title": ("title_policy", "publish_staging:_resolve_automatic_title", "story_contract"),
    "cover": ("cover_generation", "cover_route_evidence", "cover_title_rendering", "cover_repair"),
    "package": ("producer_package_finalization", "subtitle_rendering", "review_package_boundary_contract", "subtitle_validation"),
}


def stage_consumer_paths(repo_root: Path, stage: str) -> list[Path]:
    paths = []
    for consumer in TALK_STAGE_ROOTS[stage]:
        module, separator, symbol = consumer.partition(":")
        path = repo_root / "src" / "autoslice" / f"{module}.py"
        paths.append(Path(f"{path}:{symbol}") if separator else path)
    return paths


def failure_dependency_stages(kind: object, stage: object = None) -> tuple[str, ...]:
    """Unknown errors require diagnosis; they never inherit the whole deploy."""
    classified = {
        "content_boundary": ("source", "boundary", "text"),
        "speaker_evidence": ("source", "speaker"),
        "subtitle_authority": ("source", "boundary", "speaker", "text"),
        "subtitle_entity_consistency": ("text",),
        "title_fact_consistency": ("text", "title"),
        "story_contract": ("text", "title"),
        "selection_rescore": ("text", "title"),
        "final_review_contract": ("boundary", "text", "package"),
        "content_duration": ("source", "boundary", "package"),
        "source_media": ("source",),
        "cover_route_regeneration": ("title", "cover", "package"),
        "title_failed": ("title", "cover", "package"),
    }
    if kind in classified:
        return classified[kind]
    if kind in {"provider_transient", "runtime_prerequisite"}:
        labels = set(re.split(r"[^a-z]+", str(stage or "").lower()))
        if "cover" in labels:
            return ("cover",)
        if "title" in labels:
            return ("title",)
        if "speaker" in labels:
            return ("speaker",)
        if "boundary" in labels:
            return ("boundary",)
        if labels & {"review", "subtitle", "transcript", "transcription", "asr", "text", "chat"}:
            return ("source", "text")
    return ()


def _module_surface(root: Path, path: Path, parsed: dict) -> tuple[dict, dict, ast.Module, dict]:
    if path in parsed:
        return parsed[path]
    package = ".".join(path.relative_to(root).with_suffix("").parts[:-1])
    parsed[path] = _parse_module(str(path), package, path.read_bytes())
    return parsed[path]


@lru_cache(maxsize=512)
def _parse_module(filename: str, package: str, content: bytes):
    # Reuse pure AST analysis across candidates only when the exact current
    # source bytes match. Neither deployment ID nor mtime grants cache reuse.
    tree = ast.parse(content, filename=filename)
    symbols, imports = {}, {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            symbols[node.name] = node
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                for name in ast.walk(target):
                    if isinstance(name, ast.Name):
                        symbols[name.id] = node
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imports[alias.asname or alias.name.split(".")[0]] = (alias.name, None)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level:
                parents = package.split(".")
                module = ".".join(parents[:len(parents) - node.level + 1] + ([module] if module else []))
            for alias in node.names:
                imports[alias.asname or alias.name] = (module, alias.name)
    return symbols, imports, tree, {}


def _nested_imports(nodes: list[ast.AST], package: str) -> dict[str, set[tuple[str, str | None]]]:
    bindings: dict[str, set[tuple[str, str | None]]] = {}
    for child in nodes:
        if isinstance(child, ast.Import):
            for alias in child.names:
                bindings.setdefault(alias.asname or alias.name.split(".")[0], set()).add((alias.name, None))
        elif isinstance(child, ast.ImportFrom):
            module = child.module or ""
            if child.level:
                parents = package.split(".")
                module = ".".join(parents[:len(parents) - child.level + 1] + ([module] if module else []))
            for alias in child.names:
                bindings.setdefault(alias.asname or alias.name, set()).add((module, alias.name))
    return bindings


def _dependency_surface(repo_root: Path, path: Path, symbol: str | None, parsed: dict):
    key = (path, symbol)
    if key in parsed:
        return parsed[key]
    symbols, imports, tree, surface_cache = _module_surface(repo_root, path, parsed)
    if symbol in surface_cache:
        parsed[key] = surface_cache[symbol]
        return parsed[key]
    if symbol is not None and symbol in imports:
        result = (None, set(), {imports[symbol]})
    else:
        selected = symbols.get(symbol) if symbol is not None else tree
        node = selected if selected is not None else tree
        children = list(ast.walk(node))
        package = ".".join(path.relative_to(repo_root).with_suffix("").parts[:-1])
        bindings = {name: {binding} for name, binding in imports.items()}
        for name, candidates in _nested_imports(children, package).items():
            bindings.setdefault(name, set()).update(candidates)
        attributes = {child.value: child.attr for child in children if isinstance(child, ast.Attribute)}
        references, edges = set(), set()
        for child in children:
            if not isinstance(child, ast.Name) or not isinstance(child.ctx, ast.Load):
                continue
            if child.id in symbols:
                references.add(child.id)
            for module, imported in bindings.get(child.id, ()):
                edges.add((module, attributes.get(child) if imported is None else imported))
        result = (ast.dump(node, include_attributes=False).encode(), references, edges)
    # A later snapshot rereads each file before selecting this byte-bound AST.
    surface_cache[symbol] = result
    parsed[key] = result
    return result


def dependency_fingerprint(
    *, repo_root: Path, roots: list[Path], config: Mapping[str, object],
    parsed: dict | None = None,
) -> str:
    # Follow the imported function/constant actually used by each consumer.
    # A full-module walk would pull queue/admin/private-repair code back into
    # every stage and recreate the old all-autoslice deployment dependency.
    parsed = {} if parsed is None else parsed
    pending = []
    for path in roots:
        filename, separator, symbol = str(path).partition(":")
        pending.append((Path(filename), symbol if separator else None))
    full_roots = {path for path, symbol in pending if symbol is None}
    visited, surfaces = set(), {}
    while pending:
        path, symbol = pending.pop()
        if symbol is not None and path in full_roots:
            continue
        if (path, symbol) in visited:
            continue
        visited.add((path, symbol))
        label = path.relative_to(repo_root).as_posix() if path.is_relative_to(repo_root) else str(path)
        if not path.is_file():
            surfaces[f"{label}:{symbol or '*'}"] = b"MISSING"
            continue
        if path.suffix != ".py" or not path.is_relative_to(repo_root):
            surfaces[label] = path.read_bytes()
            continue
        surface, references, edges = _dependency_surface(repo_root, path, symbol, parsed)
        if surface is not None:
            surfaces[f"{label}:{symbol or '*'}"] = surface
        if path not in full_roots:
            pending.extend((path, name) for name in references)
        for module, imported in edges:
            if not module.startswith(("src.autoslice", "scripts.", "ops.")):
                continue
            module_path = repo_root.joinpath(*module.split(".")).with_suffix(".py")
            child_path = repo_root.joinpath(*module.split("."), imported).with_suffix(".py") if imported else None
            if child_path is not None and child_path.is_file():
                pending.append((child_path, None))
            elif module_path.is_file():
                pending.append((module_path, imported))
    hasher = hashlib.sha256(b"proof-dependencies.v1\0")
    for label, surface in sorted(surfaces.items()):
        hasher.update(label.encode() + b"\0" + surface + b"\0")
    hasher.update(json.dumps(config, sort_keys=True, separators=(",", ":")).encode())
    return "sha256:" + hasher.hexdigest()


def talk_dependency_snapshot(
    *, repo_root: Path, candidate_id: str, stage_assets: Mapping[str, list[Path]],
    stage_config: Mapping[str, Mapping[str, object]], source_path: Path | None,
    record: Mapping[str, object],
) -> dict[str, object]:
    """Capture media-input identity separately from deployment provenance.

    Source stat is an invalidation hint, never a raw-byte PASS receipt.  Actual
    media/checkpoint consumers still validate their source/SRT hashes.
    """
    source_identity: object = None
    if source_path is not None:
        try:
            stat = source_path.stat()
            source_identity = [source_path.name, stat.st_size, stat.st_mtime_ns]
        except OSError:
            source_identity = [source_path.name, "UNAVAILABLE"]
    source_config = {
        "source": source_identity,
        "start_ms": record.get("start_ms"), "end_ms": record.get("end_ms"),
        "given_end_ms": record.get("given_end_ms"),
        "source_sha256": record.get("source_sha256"),
        "filler_proposals": record.get("filler_proposals") or [],
        "merge_gap_removals": record.get("merge_gap_removals") or [],
    }
    fingerprints = {}
    parsed: dict = {}
    for stage, modules in TALK_STAGE_ROOTS.items():
        roots = stage_consumer_paths(repo_root, stage)
        roots.extend(stage_assets.get(stage, []))
        config = {**stage_config.get(stage, {}), **source_config}
        if stage in {"boundary", "text", "title", "cover", "package"}:
            # These inputs describe the text, not retry counters or old verdicts.
            config["text_inputs"] = {
                "selection_hook": record.get("selection_hook") or record.get("hook") or "",
                **{key: record.get(key) for key in (
                    "filler_proposal_srt_sha256", "structured_chat_source_sha256", "structured_chat_sha256",
                )},
            }
        input_keys = {
            "source": ("given_end_authority", "given_end_mode"),
            "boundary": ("given_end_authority", "given_end_mode"),
            "speaker": ("speaker_routing_claim", "speaker_routing_claim_sha256", "speaker_routing_candidate", "speaker_session_context_sha256"),
            "text": ("watched_media", "nested_media_caption_dedup", "nested_media_acoustic", "song_name_candidates", "segment_scene_context", "session_relation_authority"),
            "title": ("given_title",),
            "cover": ("given_title", "cover_diversity_slot"),
            "package": ("given_title",),
        }
        config["consumer_inputs"] = {key: record.get(key) for key in input_keys[stage]}
        fingerprints[stage] = dependency_fingerprint(repo_root=repo_root, roots=roots, config=config, parsed=parsed)
    return {"schema_version": TALK_DEPENDENCY_SCHEMA, "candidate_id": candidate_id, "stages": fingerprints}


def changed_dependency_stages(previous: object, current: Mapping[str, object]) -> tuple[str, ...] | None:
    if previous is None:
        return None
    if not isinstance(previous, Mapping):
        raise ValueError("talk dependency snapshot type is invalid")
    if previous.get("schema_version") != TALK_DEPENDENCY_SCHEMA or previous.get("candidate_id") != current.get("candidate_id"):
        raise ValueError("talk dependency snapshot identity is invalid")
    stages = previous.get("stages")
    if not isinstance(stages, Mapping) or set(stages) != set(TALK_STAGE_ROOTS):
        raise ValueError("talk dependency snapshot stages are invalid")
    if any(not isinstance(value, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None for value in stages.values()):
        raise ValueError("talk dependency snapshot fingerprint is invalid")
    return tuple(stage for stage in TALK_STAGE_ROOTS if stages[stage] != current["stages"][stage])


def pipeline_fingerprint(
    *, repo_root: Path, profile_tool: Callable[[str], Path], channel_profile: Any,
    exclusions: set[str], speaker_authority: Callable[[], object],
    speaker_authority_errors: tuple[type[BaseException], ...],
) -> str:
    """Hash all deployed Talk-capable proof surfaces and provider authority."""

    hasher = hashlib.sha256()
    explicit = {
        "scripts/session_autoslice.py", "scripts/free_asr_client.py",
        "scripts/apply_subtitle_text_overrides.py", "scripts/cpa_semantic_qa_llm.py",
        "scripts/gemini_slice_jingting.py", "scripts/llm_via_cpa.sh",
        "scripts/produce_slice_package.py", "scripts/repair_reviewed_covers.py",
        "scripts/resume_frozen_talk_package.py", "scripts/run_auto_review_shadow_pipeline.py",
        "scripts/run_full_session_selector_cpa_shadow.py",
    }
    paths = [repo_root / relative for relative in explicit]
    paths.append(profile_tool("cover_regenerator"))
    paths.extend(channel_profile.fingerprint_paths(repo_root=repo_root))
    autoslice_src = repo_root / "src" / "autoslice"
    paths.extend(
        path for path in (autoslice_src.rglob("*.py") if autoslice_src.is_dir() else [])
        if path.relative_to(repo_root).as_posix() not in exclusions
    )

    def label(path: Path) -> str:
        try:
            return path.relative_to(repo_root).as_posix()
        except ValueError:
            return str(path)

    for path in (path for path in paths if not path.is_file()):
        hasher.update(label(path).encode("utf-8") + b"\0MISSING\0")
    for path in sorted((path for path in paths if path.is_file()), key=label):
        hasher.update(label(path).encode("utf-8") + b"\0")
        hasher.update(path.read_bytes())
        hasher.update(b"\0")
    for key in (
        "AUTOSLICE_CORRECTION_MODE",
        "AUTOSLICE_LOCAL_AUDIO_WITNESS_PROVIDER",
        "AUTOSLICE_LOCAL_AUDIO_WITNESS_MAX_WINDOWS",
        "AUTOSLICE_LOCAL_AUDIO_WITNESS_MAX_AUDIO_MS",
        "AUTOSLICE_SPEAKER_ROUTING_PROVIDER_COMMAND_JSON",
        "AUTOSLICE_SPEAKER_ROUTING_PROVIDER_NAME",
        "AUTOSLICE_SPEAKER_ROUTING_PROVIDER_ALGORITHM_ID",
        "AUTOSLICE_SPEAKER_ROUTING_PROVIDER_ARTIFACTS_JSON",
    ):
        hasher.update(b"runtime-config\0" + key.encode("utf-8") + b"\0")
        hasher.update(os.environ.get(key, "").encode("utf-8") + b"\0")
    try:
        authority = speaker_authority()
    except speaker_authority_errors as exc:
        hasher.update(f"provider-authority-unavailable:{type(exc).__name__}".encode())
    else:
        if authority is not None:
            hasher.update(json.dumps(authority, sort_keys=True, separators=(",", ":")).encode())
    return "sha256:" + hasher.hexdigest()


def song_pipeline_fingerprint(
    *, repo_root: Path, profile_id: str, channel_profile: Any,
    profile_tool: Callable[[str], Path], profile_asset_file: Callable[[str], Path],
    profile_asset_directory: Callable[[str], Path], policy: dict[str, object],
) -> str:
    """Hash only the Song proof closure, excluding Talk-only recovery code."""

    explicit = {
        "scripts/cpa_semantic_qa_llm.py", "scripts/free_asr_client.py",
        "scripts/gemini_slice_jingting.py", "scripts/llm_via_cpa.sh",
        "scripts/run_full_session_selector_cpa_shadow.py", "src/autoslice/agy_lrc_alignment.py",
        "src/autoslice/boundary_resolver.py", "src/autoslice/candidate_selection.py",
        "src/autoslice/channel_profile.py", "src/autoslice/content_evidence.py",
        "src/autoslice/cover_emote.py", "src/autoslice/cover_generation.py",
        "src/autoslice/cpa_image_edit.py",
        "src/autoslice/cpa_semantic_qa.py", "src/autoslice/danmaku_evidence.py",
        "src/autoslice/delivery_recovery.py", "src/autoslice/full_session_candidate_selector.py",
        "src/autoslice/full_session_transcription.py", "src/autoslice/gemini_backup_policy.py",
        "src/autoslice/host_vocal_proof.py", "src/autoslice/jingting_chunker.py",
        "src/autoslice/llm_client.py", "src/autoslice/publish_staging.py",
        "src/autoslice/produce_dispatch.py", "src/autoslice/producer_batch_projection.py",
        "src/autoslice/producer_batch_runner_integration.py",
        "src/autoslice/producer_batch_transaction.py",
        "src/autoslice/producer_delivery_prepare.py",
        "src/autoslice/producer_delivery_transaction.py",
        "src/autoslice/published_song_history.py", "src/autoslice/render_qa.py",
        "src/autoslice/review_evidence.py", "src/autoslice/semantic_candidate_selector.py",
        "src/autoslice/source_context_executor.py", "src/autoslice/source_context_planner.py",
        "src/autoslice/source_integrity.py", "src/autoslice/style_profile.py",
        "src/autoslice/subtitle_rendering.py", "src/autoslice/title_policy.py",
        "src/autoslice/channel_envelope.py",
        "src/autoslice/runner_state_writeback.py",
        "src/autoslice/upload_tag_policy.py", "src/autoslice/verified_io.py",
        "src/autoslice/visual_song_discovery.py",
    }
    # Controller/queue/deployment edits cannot repair a completed acoustic
    # proof.  Preserve the old broad hash on records as historical provenance.
    controller_paths = {
        "src/autoslice/delivery_recovery.py", "src/autoslice/candidate_selection.py",
        "src/autoslice/produce_dispatch.py", "src/autoslice/producer_batch_projection.py",
        "src/autoslice/producer_batch_runner_integration.py", "src/autoslice/producer_batch_transaction.py",
        "src/autoslice/runner_state_writeback.py",
    }
    paths: list[Path] = [repo_root / relative for relative in explicit - controller_paths]
    paths.extend((repo_root / "src" / "autoslice").glob("song_*.py"))
    if "emote_library" in channel_profile.asset_files:
        paths.append(profile_asset_file("emote_library"))
    paths.extend((
        repo_root / "profiles" / profile_id / "profile.json", profile_tool("cover_regenerator"),
        profile_asset_file("cover_identity_prompt"), profile_asset_file("known_songs"),
        profile_asset_file("published_songs"), profile_asset_file("persona"),
        profile_asset_file("slice_selection_metric"), profile_asset_file("title_policy"),
        profile_asset_file("title_style"), profile_asset_file("upload_tag_policy"),
        profile_asset_file("voiceprint_profile"),
    ))
    fonts = profile_asset_directory("fonts")
    paths.extend(fonts.rglob("*") if fonts.is_dir() else [])
    return dependency_fingerprint(repo_root=repo_root, roots=list(set(paths)), config=policy)
