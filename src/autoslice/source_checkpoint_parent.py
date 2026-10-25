"""Explicit immutable SOURCE parents for a new ordinary producer attempt.

A failed producer has no completed package record. Its native SOURCE checkpoint
and later accounting are independent inputs; neither grants a release verdict.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from collections.abc import Mapping
from pathlib import Path

from src.autoslice.review_evidence_store import review_from_bytes

SCHEMA = "retained-source-checkpoint-input.v1"
# These are delivery contracts, not SOURCE inputs: hardlink the old recut,
# restore the complete old CURRENT, or replay/clamp its reviewed interval.
SOURCE_ENDPOINT_DELIVERY_FIELDS = (
    "existing_final_recut", "retained_current_text", "subtitle_redelivery_baseline",
)
SOURCE_ENDPOINT_BOUNDARY_FIELDS = (
    "given_end_ms", "given_end_mode", "given_end_authority", "semantic_tail_trim_cap_ms",
)
SOURCE_AUTHORITY_FIELDS = (
    "schema_version", "mutation_authorized",
    "post_transcript_entity_output_srt_sha256", "final_output_srt_sha256",
)


def _need(condition, message):
    if not condition:
        raise ValueError("SOURCE_CHECKPOINT_PARENT_" + message)


def _valid_cpa_journal_row(row):
    """Keep typed legacy events and Gemini's native kindless observations.

    ``agy_gemini_client.generate_content`` writes the audio transport pair
    and audio/vision format receipts through ``cpa_runtime.record_cpa_event``.
    They are observations only, never adjudication or release authority.
    """
    if not isinstance(row, dict):
        return False
    if row.get("kind"):
        return True  # Preserve the existing kind-bearing journal contract.
    if "kind" in row:
        return False
    event = row.get("event_type")
    if event not in ("audio_model_request", "audio_http_attempt",
                     "audio_format_attempt", "vision_format_attempt"):
        return False
    required = {"provider", "model", "status", "observation_input_sha256",
                "production_attempt_id"}
    if not required.issubset(row) or row["provider"] != "gemini_api":
        return False
    digest = row["observation_input_sha256"]
    if (not isinstance(row["model"], str) or not row["model"].strip()
            or not isinstance(digest, str) or len(digest) != 64
            or any(c not in "0123456789abcdef" for c in digest)):
        return False
    if event in ("audio_model_request", "audio_http_attempt"):
        return (type(row.get("format_attempt")) is int and row["format_attempt"] >= 1
                and type(row.get("elapsed_seconds")) in (int, float)
                and row["elapsed_seconds"] >= 0)
    # ``structured_output.format_retry_call`` emits all three outcomes,
    # including interrupted requests. Do not discard UNKNOWN or rejections.
    return ({"attempt", "json_mode", "schema_name", "schema_sha256"}.issubset(row)
            and type(row["attempt"]) is int and row["attempt"] in (1, 2)
            and type(row["json_mode"]) is bool
            and row["status"] in ("RETURNED", "FORMAT_REJECTED", "OUTCOME_UNKNOWN")
            and (row["status"] != "FORMAT_REJECTED"
                 or {"error", "completion_sha256"}.issubset(row)))


def binding(path: Path) -> dict:
    path = Path(path)
    _need(path.is_absolute() and path.is_file()
          and not any(p.is_symlink() for p in (path, *path.parents)), "FILE_INVALID")
    from .evidence_identity import file_sha256
    digest = file_sha256(path)
    return {"path": str(path), "sha256": "sha256:" + digest, "bytes": path.stat().st_size}


def bound(value: object, *, as_json=True):
    _need(isinstance(value, Mapping) and set(value) == {"path", "sha256", "bytes"}, "BINDING_INVALID")
    path = Path(str(value["path"]))
    _need(binding(path) == dict(value), "BYTES_DRIFT")
    if not as_json:
        return path
    raw = path.read_bytes()
    # Check the path and raw digest above before expanding a compact chat
    # authority.  Checkpoints, plans, and ordinary state documents retain
    # their historical inline JSON semantics.
    inline = json.loads(raw)
    review_bearing = (
        path.name.endswith(".chat-authority.json")
        or (isinstance(inline, Mapping)
            and (inline.get("schema_version") == "chat-authority-audit.v2"
                 or "final_review_audit" in inline))
    )
    result = review_from_bytes(raw, directory=path.parent) if review_bearing else inline
    _need(isinstance(result, dict), "OBJECT_INVALID")
    return result


def _checkpoint_binding(value):
    _need(isinstance(value, Mapping) and set(value) == {"path", "sha256"}, "CHECKPOINT_BINDING_INVALID")
    result = binding(Path(str(value["path"])))
    _need(result["sha256"] == value["sha256"], "CHECKPOINT_BYTES_DRIFT")
    return result


def _usage(authority):
    from src.autoslice.producer_text_pipeline import _retained_parent_cpa_usage_seed
    if "cpa_adjudication_usage" in authority:
        return _retained_parent_cpa_usage_seed(authority), "cpa_adjudication_usage"
    review = authority.get("final_review_audit") or {}
    count = review.get("context_adjudication_count")
    _need(review.get("schema_version") == "final-review-audit.v1"
          and type(count) is int and count >= 0, "CPA_USAGE_MISSING")
    # This is the ordinary v1 adjudication counter, not total HTTP transport.
    # Preserve the separate complete transport journal below, including UNKNOWN.
    return {"provider_adjudication_count": count,
            "prior_provider_adjudication_count": count, "policy": "SOFT"}, (
                "final_review_audit.context_adjudication_count")


def _derive(parent_spec: dict, checkpoint: dict, live: dict, *, accept_source_endpoint=False):
    from src.autoslice.producer_text_checkpoint import SOURCE_SCHEMA, load_post_transcript_text
    cid = parent_spec.get("candidate_id")
    _need(checkpoint.get("schema_version") == SOURCE_SCHEMA
          and checkpoint.get("candidate_id") == cid
          and checkpoint.get("time_domain") == "padded_local"
          and checkpoint.get("release_authorized") is False, "SCOPE_INVALID")
    artifacts = {key: _checkpoint_binding(checkpoint.get(key))
                 for key in ("srt", "authority", "context", "padded")}
    frozen = bound(artifacts["authority"])
    context = bound(artifacts["context"])
    _need(context.get("candidate_id") == cid
          and context.get("recording_date") == parent_spec.get("date"), "CANDIDATE_DRIFT")
    source_text = bound(artifacts["srt"], as_json=False).read_text(encoding="utf-8")
    _need(load_post_transcript_text(frozen) is not None, "SOURCE_TEXT_MISSING")
    if accept_source_endpoint:
        # This lane restores the checkpoint's frozen SOURCE, not live CURRENT.
        # Its retained post-transcript snapshot binds its own bytes above; a
        # later source-truth stage may legitimately make SOURCE differ from it.
        _need(frozen.get("schema_version") == "chat-authority-audit.v2"
              and frozen.get("mutation_authorized") is not True
              and frozen.get("final_output_srt_sha256") == artifacts["srt"]["sha256"][7:],
              "SOURCE_AUTHORITY_DRIFT")
    else:
        _need(frozen.get("schema_version") == live.get("schema_version") == "chat-authority-audit.v2"
              and frozen.get("mutation_authorized") is not True
              and live.get("mutation_authorized") is not True
              and frozen.get("post_transcript_entity_output_srt_sha256")
              == live.get("post_transcript_entity_output_srt_sha256")
              and frozen.get("final_output_srt_sha256")
              == live.get("final_output_srt_sha256") == artifacts["srt"]["sha256"][7:],
              "SOURCE_AUTHORITY_DRIFT")
    _need(hashlib.sha256(source_text.encode()).hexdigest()
          == artifacts["srt"]["sha256"][7:], "TEXT_DRIFT")
    pieces, old = parent_spec.get("pieces"), context.get("pieces")
    _need(isinstance(pieces, list) and pieces and isinstance(old, list)
          and len(pieces) == len(old), "PIECES_INVALID")
    for piece, previous in zip(pieces, old):
        _need(all(piece.get(key) == previous.get(key) for key in ("start_ms", "end_ms"))
              and Path(str(piece.get("remote_media"))).name == previous.get("recording_basename")
              and str(piece.get("source_media_sha256")).removeprefix("sha256:")
                  == str(previous.get("source_media_sha256")).removeprefix("sha256:"),
              "SOURCE_CLOCK_DRIFT")
    previous_usage, _ = _usage(frozen)
    latest_usage, usage_field = _usage(live)
    _need(latest_usage["provider_adjudication_count"]
          >= previous_usage["provider_adjudication_count"], "CPA_HISTORY_REGRESSION")
    return artifacts, latest_usage, usage_field


def checkpoint_parent_paths(parent: Path, cid: str) -> dict[str, Path]:
    """Native files a producer run leaves in ``output_root/<cid>`` for its SOURCE parent.

    The builder below and runtime GC's published-input closure share this
    one naming; neither keeps its own list.
    """
    return {"source_checkpoint": parent / f"{cid}.source-text-checkpoint.json",
            "live_authority": parent / f"{cid}.chat-authority.json",
            "native_budget": parent / "native-audio-budget.json",
            "cpa_journal": parent / f"{cid}.cpa-usage.jsonl"}


def _native_budget_binding(parent_spec, native_path, padded_binding):
    """Resolve a resumed account by production identity, never by a directory scan.

    Keep an occupied output receipt as the immutable parent, as before. Runs
    that used an inherited account need not have written an output receipt at
    all; their later history lives in the runtime's candidate/source account.
    """
    if native_path.exists() or native_path.is_symlink():
        return binding(native_path)
    from src.autoslice.native_audio_budget_receipt import (
        _require_budget_history_extension, _validate_existing,
    )
    runtime = str(parent_spec.get("runtime_root") or "")
    _need(Path(runtime).is_absolute()
          and os.environ.get("AUTOSLICE_BASE", runtime) == runtime, "ACCOUNT_NAMESPACE_DRIFT")
    cid = str(parent_spec.get("candidate_id") or "")
    _need(cid not in ("", ".", "..") and Path(cid).name == cid, "CANDIDATE_DRIFT")
    source_sha = padded_binding["sha256"][7:]
    account = Path(runtime) / ".native-audio-resume" / cid / source_sha
    result = binding(account / "native-audio-budget.json")
    receipt = bound(result)
    _validate_existing(receipt, source_media_sha256=source_sha)
    # Self-extension checks full event structure and provider summaries, for
    # both v2 prefixes and already registered v3 branch histories.
    _require_budget_history_extension(receipt, receipt)
    _need(receipt["budget"]["source_media"] == padded_binding["path"], "NATIVE_SOURCE_DRIFT")
    manifest = bound(binding(account / "parent.json"))
    _need(manifest.get("schema_version") == "prepared-native-audio-parents.v1"
          and manifest.get("candidate_id") == cid
          and manifest.get("source_media_sha256") == source_sha
          and isinstance(manifest.get("parents"), list), "NATIVE_ACCOUNT_SCOPE_DRIFT")
    for row in manifest["parents"]:
        digest = row.get("budget_sha256") if isinstance(row, Mapping) else None
        _need(isinstance(digest, str) and digest.startswith("sha256:")
              and len(digest) == 71 and all(c in "0123456789abcdef" for c in digest[7:]),
              "NATIVE_ACCOUNT_PARENT_DRIFT")
        archived = binding(account / "parents" / (digest[7:] + ".json"))
        _need(archived["sha256"] == digest, "NATIVE_ACCOUNT_PARENT_DRIFT")
        previous = bound(archived)
        _validate_existing(previous, source_media_sha256=source_sha)
        _require_budget_history_extension(previous, receipt)
    return result


def build_source_endpoint_rebuild_authority(parent_spec: Mapping) -> dict:
    """Keep the same-BV closure and source identity, without an old endpoint.

    Replay the existing publication/source bridge before discarding its anchor.
    A title-only authority alone does not bind the parent's recording clock.
    """
    from src.autoslice.first_publication_boundary import bind_published_recall_to_spec
    from src.autoslice.new_bv_repair_authority import (
        build_new_bv_repair_authority, validate_new_bv_repair_authority,
    )
    cid = str(parent_spec.get("candidate_id") or "")
    previous = validate_new_bv_repair_authority(
        parent_spec.get("recovery_publication_authority"), candidate_id=cid,
        expected_final_title=parent_spec.get("given_title"))
    snapshot = previous.get("review_manifest_snapshot")
    kwargs = {
        "candidate_id": cid, "title": previous["title"],
        "review_manifest_snapshot": Path(snapshot["path"]) if snapshot else None,
    }
    publication = Path(previous["source_publication"]["path"])
    bridge = {k: v for k, v in parent_spec.items() if k not in SOURCE_ENDPOINT_BOUNDARY_FIELDS}
    bridge["recovery_publication_authority"] = build_new_bv_repair_authority(
        publication, **kwargs, published_recall_anchor=True)
    _need(bind_published_recall_to_spec(bridge), "UNVERIFIED_BOUNDARY_CHANGE")
    return build_new_bv_repair_authority(publication, **kwargs)


def build_source_checkpoint_parent(*, parent_spec_path: Path, checkpoint_path: Path,
                                   accept_source_endpoint: bool = False) -> dict:
    """Build only from native files; no caller-supplied usage counts or verdicts."""
    parent_spec_binding, checkpoint_binding = binding(parent_spec_path), binding(checkpoint_path)
    parent_spec, checkpoint = bound(parent_spec_binding), bound(checkpoint_binding)
    cid = str(parent_spec.get("candidate_id") or "")
    native_files = checkpoint_parent_paths(Path(str(parent_spec.get("output_root") or "")) / cid, cid)
    _need(checkpoint_path == native_files["source_checkpoint"], "CHECKPOINT_LOCATION_DRIFT")
    live_binding = binding(native_files["live_authority"])
    live = bound(live_binding)
    artifacts, usage, usage_field = _derive(
        parent_spec, checkpoint, live, accept_source_endpoint=accept_source_endpoint)
    padded = Path(artifacts["padded"]["path"])
    native = _native_budget_binding(parent_spec, native_files["native_budget"], artifacts["padded"])
    budget = bound(native)
    from src.autoslice.native_audio_budget_receipt import _validate_existing
    _validate_existing(budget, source_media_sha256=artifacts["padded"]["sha256"][7:])
    journal = binding(native_files["cpa_journal"])
    # A complete immutable journal remains distinct from the adjudication counter.
    rows = [json.loads(line) for line in bound(journal, as_json=False).read_text().splitlines()
            if line.strip()]
    _need(all(_valid_cpa_journal_row(row) for row in rows),
          "CPA_JOURNAL_INVALID")
    source_shas = [piece["source_media_sha256"] for piece in parent_spec["pieces"]]
    result = {
        "schema_version": SCHEMA, "candidate_id": cid,
        "parent_spec": parent_spec_binding, "source_checkpoint": checkpoint_binding,
        "live_authority": live_binding, "native_budget": native, "cpa_journal": journal,
        "cpa_usage": usage, "cpa_usage_source_field": usage_field,
        "padded_sha256": artifacts["padded"]["sha256"],
        "parent_materialization_path": str(padded),
        "parent_materialization_sha256": artifacts["padded"]["sha256"],
        "parent_materialization_duration_ms": sum(p["end_ms"] - p["start_ms"] for p in parent_spec["pieces"]),
        "parent_materialization_provenance_path": str(padded.with_suffix(".provenance.json")),
        "parent_materialization_provenance_sha256": binding(padded.with_suffix(".provenance.json"))["sha256"],
        "source_media_sha256s": source_shas,
        "release_authorized": False,
    }
    if accept_source_endpoint:
        frozen = bound(artifacts["authority"])
        result["source_authority_observation"] = {
            "schema_version": "source-checkpoint-authority-observation.v1",
            "source_authority_basis": "FROZEN_SOURCE_CHECKPOINT",
            "frozen_authority": artifacts["authority"], "source_srt": artifacts["srt"],
            "live_authority_role": "ACCOUNTING_AND_DRIFT_DISCLOSURE_ONLY",
            "fields": {key: {"frozen": frozen.get(key), "live": live.get(key)}
                       for key in SOURCE_AUTHORITY_FIELDS},
            "differing_fields": [key for key in SOURCE_AUTHORITY_FIELDS
                                 if frozen.get(key) != live.get(key)],
        }
    return result


def validate_source_checkpoint_parent(spec: Mapping, *, runtime=False) -> dict | None:
    config = spec.get("retained_source_input")
    if not isinstance(config, Mapping) or config.get("schema_version") != SCHEMA:
        return None
    bound(config.get("parent_spec"))
    bound(config.get("source_checkpoint"))
    source_endpoint_rebuild = "accept_source_endpoint" in spec
    if source_endpoint_rebuild:
        _need(spec["accept_source_endpoint"] is True, "SPEC_SCOPE_DRIFT")
    rebuilt = build_source_checkpoint_parent(
        parent_spec_path=Path(config["parent_spec"]["path"]),
        checkpoint_path=Path(config["source_checkpoint"]["path"]),
        accept_source_endpoint=source_endpoint_rebuild)
    _need(dict(config) == rebuilt, "HISTORY_DRIFT")
    parent_spec = bound(config["parent_spec"])
    account_root = str(parent_spec.get("runtime_root") or "")
    _need(Path(account_root).is_absolute()
          and os.environ.get("AUTOSLICE_BASE", account_root) == account_root,
          "ACCOUNT_NAMESPACE_DRIFT")
    # The successor may change only its new output and the verified recall
    # authority/mode. Source clock, human truth, owner and runtime stay bound.
    allowed = {"output_root", "recovery_publication_authority", "given_end_ms",
               "given_end_mode", "given_end_authority", "semantic_tail_trim_cap_ms",
               "retained_source_input", "source_text_checkpoint",
               "existing_subtitle_surface", "source_witness_extension"}
    if source_endpoint_rebuild:
        _need(not any(k in spec for k in SOURCE_ENDPOINT_DELIVERY_FIELDS), "SPEC_SCOPE_DRIFT")
        _need(not any(k in spec for k in SOURCE_ENDPOINT_BOUNDARY_FIELDS[:3]), "SPEC_SCOPE_DRIFT")
        # Normal producer preflight materializes this default after validation.
        _need("semantic_tail_trim_cap_ms" not in spec or (
            runtime and type(spec["semantic_tail_trim_cap_ms"]) is int
            and spec["semantic_tail_trim_cap_ms"] == 0), "SPEC_SCOPE_DRIFT")
        _need("source_witness_extension" not in spec, "SPEC_SCOPE_DRIFT")
        _need(spec.get("recovery_publication_authority")
              == build_source_endpoint_rebuild_authority(parent_spec), "UNVERIFIED_BOUNDARY_CHANGE")
        allowed.update({"accept_source_endpoint", *SOURCE_ENDPOINT_DELIVERY_FIELDS})
    if runtime:
        allowed.update({"source_piece_clock", "required_boundary_owners", "boundary_search_scope",
                        "existing_subtitle_surface_receipt"})
    def immutable(value):
        return {k: v for k, v in value.items() if k not in allowed}
    _need(immutable(spec) == immutable(parent_spec), "SPEC_SCOPE_DRIFT")
    boundary_fields = {"recovery_publication_authority", "given_end_ms", "given_end_mode",
                       "given_end_authority", "semantic_tail_trim_cap_ms"}
    if not source_endpoint_rebuild and any(spec.get(k) != parent_spec.get(k) for k in boundary_fields):
        from src.autoslice.first_publication_boundary import bind_published_recall_to_spec
        _need(bind_published_recall_to_spec(dict(spec)), "UNVERIFIED_BOUNDARY_CHANGE")
    output = Path(str(spec.get("output_root") or ""))
    parent_output = Path(str(parent_spec.get("output_root") or ""))
    _need(output.is_absolute() and ".." not in output.parts
          and not any(p.is_symlink() for p in (output, *output.parents))
          and output != parent_output and not output.is_relative_to(parent_output)
          and not parent_output.is_relative_to(output), "OUTPUT_NOT_ISOLATED")
    checkpoint = bound(config["source_checkpoint"])
    declared = {"path": config["source_checkpoint"]["path"],
                "sha256": config["source_checkpoint"]["sha256"]}
    _need(spec.get("source_text_checkpoint") == declared, "DECLARATION_DRIFT")
    surface = spec.get("existing_subtitle_surface") or {}
    _need(surface.get("path") == checkpoint["srt"]["path"]
          and surface.get("sha256") == checkpoint["srt"]["sha256"]
          and surface.get("source_media_sha256") == checkpoint["padded"]["sha256"],
          "SUBTITLE_SURFACE_DRIFT")
    if spec.get("source_witness_extension") is not None:
        from src.autoslice.source_witness_extension import validate_plan
        validate_plan(spec)
    return dict(config)


def consume_source_checkpoint_parent(*, spec, cid, padded, out_root):
    from src.autoslice.retained_source_input import RetainedSourceInput
    from src.autoslice.prepared_native_audio_recovery import (
        PreparedNativeAudioState, _parent_inputs, inherit_prepared_native_audio,
    )
    from src.autoslice.producer_delivery_prepare import resolve_talk_delivery_runtime_root
    config = validate_source_checkpoint_parent(spec, runtime=True)
    _need(config is not None and config["candidate_id"] == cid, "CANDIDATE_DRIFT")
    _need(binding(padded)["sha256"] == config["padded_sha256"]
          and str(padded) == config["parent_materialization_path"], "MEDIA_DRIFT")
    parent = Path(config["live_authority"]["path"]).parent
    runtime = resolve_talk_delivery_runtime_root({**spec, "output_root": str(out_root)})
    receipt_path = Path(config["native_budget"]["path"])
    if receipt_path.parent == parent:
        state = inherit_prepared_native_audio(cid=cid, parent=parent, source=padded, runtime_root=runtime)
    else:
        # The source account is already inherited. Reuse its verified caches
        # and restore the whole ledger without registering it as its own parent
        # or requiring a receipt in the later text-only output directory.
        from src.autoslice.native_audio_budget_receipt import resume_native_audio_budget
        from src.autoslice.supplement_audio_budget import ensure_budget
        account = receipt_path.parent
        receipt, _ = _parent_inputs(parent=account, cid=cid, source=padded,
                                    source_sha=config["padded_sha256"][7:])
        limits = receipt["budget"]
        ensure_budget(padded, max_windows=limits["max_windows"], max_audio_ms=limits["max_audio_ms"],
                      enforce_limits=limits.get("enforce_limits", False))
        resume_native_audio_budget(source_media=padded, source_media_sha256=config["padded_sha256"][7:],
                                  receipt_path=receipt_path, require_existing=True)
        history = limits.get("account_history")
        roots = (account / "history-cache" / history["plan"]["incoming"]["sha256"][7:],) if history else ()
        state = PreparedNativeAudioState(account, receipt_path, roots)
    proof = {key: copy.deepcopy(config[key]) for key in (
        "source_checkpoint", "live_authority", "native_budget", "cpa_journal",
        "cpa_usage_source_field")}
    proof.update(schema_version="source-checkpoint-parent-consumption.v1",
                 candidate_id=cid, release_authorized=False,
                 native_budget_receipt=str(state.budget_receipt),
                 native_cache_root=str(state.cache_root),
                 inherited_cpa_adjudication_count=config["cpa_usage"]["provider_adjudication_count"])
    return RetainedSourceInput(state, copy.deepcopy(config["cpa_usage"]), proof)
