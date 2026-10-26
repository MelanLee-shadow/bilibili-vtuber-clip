"""Incremental post-end witness reserve for an immutable SOURCE parent.

The delivery clock and native accounts stay on the original physical prefix.
Only the missing, non-deliverable reserve is prepared and transcribed.  This is
an input contract, never a subtitle, boundary, or final-media verdict.
"""
from __future__ import annotations

import copy
from dataclasses import asdict, replace
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

from src.autoslice.source_checkpoint_parent import binding, bound

FIELD = "source_witness_extension"
SCHEMA = "source-witness-extension-plan.v1"
RECEIPT = "source-witness-extension-input.v1"


def _need(ok, reason):
    if not ok:
        raise ValueError("SOURCE_WITNESS_EXTENSION_" + reason)


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()


def _write_new(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def build_plan(spec):
    from src.autoslice.producer_boundary_owner_contract import freeze_required_boundary_owner_contract
    from src.autoslice.producer_media import ffprobe_duration_ms
    parent = spec.get("retained_source_input") or {}
    _need(parent.get("schema_version") == "retained-source-checkpoint-input.v1", "SOURCE_PARENT_REQUIRED")
    pieces = spec.get("pieces")
    _need(isinstance(pieces, list) and len(pieces) == 1, "SINGLE_SOURCE_REQUIRED")
    _need(spec.get("given_end_mode") == "published_recall_anchor", "RECALL_MODE_REQUIRED")
    original = bound(parent["parent_spec"])
    _need(pieces == original["pieces"], "DELIVERY_PIECES_DRIFT")
    checkpoint = bound(parent["source_checkpoint"])
    live = bound(parent["live_authority"])
    historical = live.get("frozen_boundary_owner_contract") or {}
    _need(isinstance(historical.get("owners"), list), "OWNER_PARENT_MISSING")
    from src.autoslice.producer_boundary_owner_contract import validate_frozen_boundary_owner_contract
    validate_frozen_boundary_owner_contract(historical)
    prepared_spec, authority = copy.deepcopy(spec), copy.deepcopy(live)
    duration = pieces[0]["end_ms"] - pieces[0]["start_ms"]
    _, scope = freeze_required_boundary_owner_contract(
        spec=prepared_spec, durations=[duration], chat_authority_audit=authority,
        required_boundary_owners=copy.deepcopy(historical["owners"]))
    required = scope["required_local_source_context_end_ms"]
    _need(scope["max_recommended_end_ms"] <= duration < required,
          "WITNESS_ONLY_RANGE_REQUIRED")
    source = binding(Path(pieces[0]["remote_media"]))
    _need(source["sha256"] == pieces[0]["source_media_sha256"], "SOURCE_HASH_DRIFT")
    end = pieces[0]["start_ms"] + required
    _need(end <= ffprobe_duration_ms(Path(source["path"])), "SOURCE_COVERAGE_MISSING")
    return {
        "schema_version": SCHEMA, "candidate_id": spec["candidate_id"],
        "bvid": spec["recovery_publication_authority"]["bvid"],
        "source": source, "source_checkpoint": parent["source_checkpoint"],
        "prefix_srt": checkpoint["srt"], "prefix_media": checkpoint["padded"],
        "prefix_context_end_ms": duration,
        "tail_source_start_ms": pieces[0]["end_ms"], "tail_source_end_ms": end,
        "tail_duration_ms": required - duration, "boundary_search_scope": scope,
        "owner_set_sha256": historical["owner_set_sha256"],
        "role": "POST_END_WITNESS_ONLY_NO_DELIVERY_EXTENSION",
        "release_authorized": False,
    }


def validate_plan(spec):
    value = spec.get(FIELD)
    if value is None:
        return None
    _need(isinstance(value, dict) and value == build_plan(spec), "PLAN_DRIFT")
    return value


def account_root(spec, plan):
    # Changing output_root must never create a second claim for the same tail.
    root = Path(str(spec["runtime_root"]))
    _need(root.is_absolute() and os.environ.get("AUTOSLICE_BASE", str(root)) == str(root),
          "ACCOUNT_NAMESPACE_DRIFT")
    key = _digest({k: plan[k] for k in ("candidate_id", "source", "tail_source_start_ms",
                                      "tail_source_end_ms")})
    path = root / ".source-witness-extension" / spec["candidate_id"] / key
    _need(not any(p.is_symlink() for p in (path, *path.parents)), "ACCOUNT_PATH_INVALID")
    return path


def prepare_extension(spec, *, host, spec_parent):
    """Ordinary source preparation; only the missing tail is newly cut."""
    plan = validate_plan(spec)
    if plan is None:
        return None
    from src.autoslice.producer_source_media import prepare_source_media
    _need(host in {"localhost", "127.0.0.1", "::1"}, "LOCAL_SOURCE_REQUIRED")
    root = account_root(spec, plan)
    if (root / "prepared.json").exists():
        return _prepared(spec, plan)[1]
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    tail = {"remote_media": plan["source"]["path"],
            "source_media_sha256": plan["source"]["sha256"],
            "start_ms": plan["tail_source_start_ms"], "end_ms": plan["tail_source_end_ms"]}
    tail_spec = {"pieces": [tail]}
    media = prepare_source_media(spec=tail_spec, cid=spec["candidate_id"],
                                out_root=root, host=host, spec_parent=spec_parent)
    from src.autoslice.producer_text_pipeline import _source_piece_container_clock_matches
    _need(_source_piece_container_clock_matches(media.padded, plan["tail_duration_ms"],
                                                media.padded_duration_ms), "TAIL_CLOCK_DRIFT")
    prepared = {
        "schema_version": "source-witness-extension-media.v1",
        "source": plan["source"], "tail_media": binding(media.padded),
        "tail_provenance": binding(media.padded_provenance_path),
        "piece_provenance": binding(root / (
            f"piece_0_{plan['tail_source_start_ms']}_{plan['tail_source_end_ms']}.provenance.json")),
        "source_piece_clock": tail_spec["source_piece_clock"],
        "tail_source_start_ms": plan["tail_source_start_ms"],
        "tail_source_end_ms": plan["tail_source_end_ms"],
        "tail_duration_ms": plan["tail_duration_ms"],
    }
    path = root / "prepared.json"
    if path.exists():
        _need(json.loads(path.read_bytes()) == prepared, "PREPARED_DRIFT")
    else:
        _write_new(path, prepared)
    return prepared


def _prepared(spec, plan):
    root = account_root(spec, plan)
    path = root / "prepared.json"
    _need(path.is_file(), "PREPARE_REQUIRED")
    data = bound(binding(path))
    _need(data.get("schema_version") == "source-witness-extension-media.v1"
          and data.get("source") == plan["source"]
          and all(data.get(key) == plan[key] for key in (
              "tail_source_start_ms", "tail_source_end_ms", "tail_duration_ms")),
          "PREPARED_SCOPE_DRIFT")
    for key in ("source", "tail_media", "tail_provenance", "piece_provenance"):
        bound(data[key], as_json=False)
    from src.autoslice.producer_source_media import source_piece_clock
    from src.autoslice.producer_media import ffprobe_duration_ms
    from src.autoslice.producer_text_pipeline import _source_piece_container_clock_matches
    row = bound(data["piece_provenance"])
    physical = Path(data["tail_media"]["path"])
    duration = ffprobe_duration_ms(physical)
    _need(row["source_path"] == plan["source"]["path"]
          and "sha256:" + row["source_sha256"] == plan["source"]["sha256"]
          and row["start_ms"] == plan["tail_source_start_ms"]
          and row["end_ms"] == plan["tail_source_end_ms"]
          and data["tail_duration_ms"] == plan["tail_duration_ms"]
          and _source_piece_container_clock_matches(physical, plan["tail_duration_ms"], duration),
          "TAIL_CLOCK_DRIFT")
    _need(data["source_piece_clock"] == source_piece_clock(
        [row], [duration], padded_sha256=data["tail_media"]["sha256"][7:]), "SOURCE_CLOCK_DRIFT")
    provenance = bound(data["tail_provenance"])
    _need(provenance["output_sha256"] == data["tail_media"]["sha256"][7:]
          and provenance["output_path"] == str(physical)
          and provenance["inputs"] == [{"path": row["output_path"], "sha256": row["output_sha256"]}],
          "TAIL_PROVENANCE_DRIFT")
    bound({"path": row["output_path"], "sha256": "sha256:" + row["output_sha256"],
           "bytes": Path(row["output_path"]).stat().st_size}, as_json=False)
    return root, data


def _observe_tail(media, duration):
    """The existing BCUT transport/cache, on tail bytes only."""
    from scripts.free_asr_client import extract_audio_mp3, transcribe
    from src.autoslice.transcription_stage_cache import TranscriptionStageCache
    from src.autoslice.subtitle_draft_preparation import record_asr_boundary_origin
    sound = extract_audio_mp3(media)
    cache = TranscriptionStageCache(media, sound)
    result = cache.bcut(lambda: transcribe(sound, provider="auto", log=lambda *_: None))
    text, origin = record_asr_boundary_origin(result, media)
    return {"raw": result, "srt": text, "origin": origin,
            "duration_ms": duration, "source_media": binding(media)}


def _stage(root, name, request, invoke):
    """A durable claim cannot be cleared by a retry or a new output directory."""
    claim, receipt = root / (name + ".claim.json"), root / (name + ".result.json")
    seal = root / (name + ".binding.json")
    if receipt.exists():
        _need(seal.is_file(), "COMPLETION_BINDING_MISSING")
        completion = bound(binding(seal))
        _need(completion.get("claim") == binding(claim), "CLAIM_DRIFT")
        result = bound(completion["result"])
        _need(bound(completion["claim"]) == request and result["request"] == request, "CLAIM_DRIFT")
        return result["result"]
    _need(not claim.exists(), "OUTCOME_UNKNOWN_" + name.upper())
    _write_new(claim, request)
    result = invoke()
    _write_new(receipt, {"request": request, "result": result})
    _write_new(seal, {"claim": binding(claim), "result": binding(receipt)})
    return result


def _tail_input(root, prepared, plan):
    from src.autoslice.jingting_chunker import parse_srt_cues
    from src.autoslice.source_context_planner import JingtingJobProvenance, plan_source_context_jingting_jobs
    from src.autoslice.boundary_resolver import AnchorCandidate
    from src.autoslice.source_context_executor import execute_source_context_job
    from src.autoslice.source_context_cpa_refinement import run_source_context_cpa
    media = Path(prepared["tail_media"]["path"])
    duration = plan["tail_duration_ms"]
    request = {"schema_version": "source-witness-tail-request.v1",
               "source_media": prepared["tail_media"], "duration_ms": duration,
               "source_clock": prepared["source_piece_clock"]}
    observed = _stage(root, "asr", request, lambda: _observe_tail(media, duration))
    _need(observed["source_media"] == prepared["tail_media"]
          and observed["duration_ms"] == duration, "ASR_BINDING_DRIFT")
    from scripts.free_asr_client import to_srt
    _need(to_srt(observed["raw"]) == observed["srt"], "ASR_RAW_DRIFT")
    origin = observed["origin"]
    raw_path = Path(origin["raw_result_path"])
    _need(binding(raw_path)["sha256"] == "sha256:" + origin["raw_result_sha256"], "ASR_RAW_DRIFT")
    raw = json.loads(raw_path.read_bytes())
    _need(raw == {k: observed["raw"][k] for k in ("provider", "elapsed_s", "utterances")
                  if k in observed["raw"]}, "ASR_RAW_DRIFT")
    cues = parse_srt_cues(observed["srt"])
    _need(cues and all(0 <= c.start_ms < c.end_ms <= duration for c in cues), "TAIL_ASR_GRID_INVALID")
    draft = root / "tail.draft.srt"
    if draft.exists():
        _need(draft.read_text() == observed["srt"], "TAIL_DRAFT_DRIFT")
    else:
        fd = os.open(draft, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(observed["srt"])
    job = plan_source_context_jingting_jobs(
        [AnchorCandidate(plan["candidate_id"], 0, duration)],
        source_duration_ms=duration, pre_ms=0, post_ms=0, provider="cpa",
        provenance=JingtingJobProvenance(recording_id=plan["candidate_id"],
            source_sha256=prepared["tail_media"]["sha256"][7:], source_uri=str(media)))[0].to_manifest()
    def execute():
        capture = {}
        def cpa(media_path, draft_path, output_path):
            result = run_source_context_cpa(media_path, draft_path, output_path)
            capture["result"] = asdict(result)
            return result
        result = execute_source_context_job(job, source_video_path=media, output_dir=root / "context",
            full_source_srt_path=draft, agy_runner=cpa, refinement_required=True)
        _need(result.jingting_done and result.decision == "READY", "CONTEXT_CORRECTION_UNAVAILABLE")
        return {"execution": asdict(result), "cpa_result": capture["result"]}
    completed = _stage(root, "context", {"job": job, "draft": binding(draft)}, execute)
    result = completed["execution"]
    from src.autoslice.source_context_cpa_refinement import validate_cpa_source_context_result
    reasons = validate_cpa_source_context_result(SimpleNamespace(**completed["cpa_result"]),
        context_media_path=Path(result["context_media_path"]),
        context_draft_srt_path=Path(result["context_draft_srt_path"]),
        context_refined_srt_path=Path(result["context_refined_srt_path"]))
    _need(not reasons, "CONTEXT_CPA_DRIFT:" + ",".join(reasons))
    manifest = bound(binding(Path(result["jingting_manifest_path"])))
    _need(manifest["job_id"] == job["job_id"]
          and manifest["source_sha256"] == prepared["tail_media"]["sha256"]
          and manifest["input_sha256"] == binding(Path(result["context_draft_srt_path"]))["sha256"]
          and manifest["output_sha256"] == binding(Path(result["context_refined_srt_path"]))["sha256"],
          "CONTEXT_MANIFEST_DRIFT")
    return Path(result["context_refined_srt_path"]), completed


def consume_extension(spec, *, cues, scope, owner_contract, clip_context):
    """Join prefix and incremental evidence at the normal source-full-window gate."""
    plan = validate_plan(spec)
    if plan is None:
        return None
    from src.autoslice.source_checkpoint_parent import validate_source_checkpoint_parent
    parent = validate_source_checkpoint_parent(spec, runtime=True)
    _need(scope == plan["boundary_search_scope"], "SCOPE_DRIFT")
    _need(isinstance(owner_contract, dict)
          and owner_contract["owner_set_sha256"] == plan["owner_set_sha256"], "OWNER_DRIFT")
    from src.autoslice.producer_boundary_owner_contract import validate_frozen_boundary_owner_contract
    validate_frozen_boundary_owner_contract(owner_contract)
    from src.autoslice.jingting_chunker import parse_srt_cues
    original = Path(plan["prefix_srt"]["path"]).read_text()
    prefix = parse_srt_cues(original)
    def grid(rows):
        return [(c.start_ms, c.end_ms, c.text) for c in rows]
    _need(grid(cues) == grid(prefix), "PREFIX_DRIFT")
    _need(max(c.end_ms for c in prefix) <= plan["prefix_context_end_ms"], "PREFIX_CLOCK_DRIFT")
    root, prepared = _prepared(spec, plan)
    tail, completed = _tail_input(root, prepared, plan)
    extra = parse_srt_cues(tail.read_text())
    _need(extra and all(0 <= c.start_ms < c.end_ms <= plan["tail_duration_ms"] for c in extra),
          "TAIL_GRID_DRIFT")
    shifted = [replace(c, index=str(len(prefix) + i),
                       start_ms=c.start_ms + plan["prefix_context_end_ms"],
                       end_ms=c.end_ms + plan["prefix_context_end_ms"])
               for i, c in enumerate(extra, 1)]
    combined = [*cues, *shifted]
    from src.autoslice.boundary_semantic_review import cue_grid_sha256
    proof = {"schema_version": RECEIPT, "plan": copy.deepcopy(plan),
             "prepared": prepared, "tail_srt": binding(tail),
             "asr_result": binding(root / "asr.result.json"),
             "context_result": binding(root / "context.result.json"),
             "source_clock_segments": [
                 {"role": "IMMUTABLE_SOURCE_PREFIX", "media": plan["prefix_media"],
                  "local_start_ms": 0, "local_end_ms": plan["prefix_context_end_ms"],
                  "recording_start_ms": spec["pieces"][0]["start_ms"],
                  "recording_end_ms": plan["tail_source_start_ms"], "recording": plan["source"]},
                 {"role": "NEW_POST_END_WITNESS_RESERVE", "media": prepared["tail_media"],
                  "local_start_ms": plan["prefix_context_end_ms"],
                  "local_end_ms": plan["prefix_context_end_ms"] + plan["tail_duration_ms"],
                  "recording_start_ms": plan["tail_source_start_ms"],
                  "recording_end_ms": plan["tail_source_end_ms"], "recording": plan["source"]}],
             "full_source_grid_sha256": cue_grid_sha256(combined),
             "prefix_cue_count": len(prefix), "tail_cue_count": len(extra),
             "available_local_source_context_end_ms": plan["prefix_context_end_ms"] + plan["tail_duration_ms"],
             "cpa_usage_parent": parent["cpa_journal"], "native_usage_parent": parent["native_budget"],
             "cpa_adjudication_usage": parent["cpa_usage"],
             "prefix_media_unchanged": True, "prior_boundary_pass_inherited": False,
             "subtitle_authority": "NONE_CONTEXT_ONLY", "release_authorized": False}
    path = root / ("input." + _digest(proof) + ".json")
    if path.exists():
        _need(bound(binding(path)) == proof, "INPUT_DRIFT")
    else:
        _write_new(path, proof)
    context = copy.deepcopy(clip_context)
    proof["receipt"] = binding(path)
    return combined, context, proof


def resolver_source_cues(spec, *, cues, chat_authority_audit):
    """Replay the completed witness grid at the ordinary resolver, never dispatch."""
    review = spec.get("boundary_semantic_review") or {}
    proof = review.get(FIELD)
    _need(isinstance(proof, dict) and isinstance(proof.get("receipt"), dict),
          "REVIEW_INPUT_MISSING")
    saved = bound(proof["receipt"])
    _need(proof == {**saved, "receipt": proof["receipt"]}, "REVIEW_INPUT_DRIFT")
    # produce_slice_package adds these derived fields after the SOURCE router.
    # Remove only those additions for the unchanged parent/spec contract; the
    # review itself is checked against the reconstructed full grid below.
    consumer_spec = copy.deepcopy(spec)
    original = bound(spec["retained_source_input"]["parent_spec"])
    for key in ("clip_context", "clip_context_path", "boundary_semantic_review"):
        if key in original:
            consumer_spec[key] = copy.deepcopy(original[key])
        else:
            consumer_spec.pop(key, None)
    plan = validate_plan(consumer_spec)
    _need(plan is not None and proof["plan"] == plan, "REVIEW_PLAN_DRIFT")
    root = account_root(consumer_spec, plan)
    _need(all((root / name).is_file() for name in (
        "asr.claim.json", "asr.result.json", "asr.binding.json",
        "context.claim.json", "context.result.json", "context.binding.json",
        "tail.draft.srt")), "REVIEW_COMPLETION_REQUIRED")
    owner = (chat_authority_audit or {}).get("frozen_boundary_owner_contract")
    combined, _context, rebuilt = consume_extension(
        consumer_spec, cues=cues, scope=spec["boundary_search_scope"],
        owner_contract=owner, clip_context=spec.get("clip_context"))
    _need(rebuilt == proof
          and review.get("cue_grid_sha256") == rebuilt["full_source_grid_sha256"],
          "REVIEW_GRID_DRIFT")
    return combined
