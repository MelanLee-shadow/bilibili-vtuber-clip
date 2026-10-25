"""Frozen partial evidence for an ordinary retained-draft recovery.

A stopped source pass need not have a successful checkpoint. Its original CPA
requests, UNKNOWN outcomes and native receipt remain evidence/account history;
this input never supplies reviewed text or release authority.
"""
from __future__ import annotations

from src.autoslice.evidence_identity import file_sha256 as _evidence_file_sha256
import copy
import hashlib
import json
from collections.abc import Mapping
from pathlib import Path

from src.autoslice.semantic_request_history import (
    EXCHANGES_SCHEMA, context_binding, json_objects, read_bound, read_review_bound,
)
from src.autoslice.native_audio_budget_receipt import (
    _no_links, _validate_existing, _validate_budget_history, _require_budget_history_extension,
)

SCHEMA = "retained-partial-evidence.v1"


def _ref(path: Path) -> dict:
    from .evidence_identity import file_sha256
    _no_links(path)
    return {"path": str(path), "sha256": "sha256:" + file_sha256(path)}


def _create(path: Path, raw: bytes) -> None:
    _no_links(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(raw)


def freeze_partial_evidence(*, output_root: Path, candidate_id: str, source: Path,
                            source_context: Mapping, parent_authority: Mapping,
                            parent_native_budget: Mapping, manifest_path: Path,
                            prior_range_observations=()) -> dict:
    """Create a byte manifest from stopped original output, without editing it."""
    if not output_root.is_absolute() or not manifest_path.is_absolute():
        raise ValueError("RETAINED_PARTIAL_PATH_INVALID")
    _no_links(output_root)
    # A successful checkpoint belongs to the existing completed recovery entry.
    if any((output_root / (candidate_id + suffix)).exists() for suffix in
           (".source-text-checkpoint.json", ".current-text-checkpoint.json", ".chat-authority.json")):
        raise ValueError("RETAINED_PARTIAL_HAS_SUCCESS_CHECKPOINT")
    _, context_raw = read_bound(source_context)
    context = json.loads(context_raw)
    if context.get("candidate_id") != candidate_id:
        raise ValueError("RETAINED_PARTIAL_CANDIDATE_DRIFT")
    read_bound(parent_authority)
    parent_path, _ = read_bound(parent_native_budget)
    source_ref = _ref(source)
    latest = _ref(output_root / "native-audio-budget.json")
    old, new = json.loads(parent_path.read_bytes()), json.loads(read_bound(latest)[1])
    source_sha = source_ref["sha256"].removeprefix("sha256:")
    for receipt in (old, new):
        _validate_existing(receipt, source_media_sha256=source_sha)
        _validate_budget_history(receipt["budget"])
    _require_budget_history_extension(old, new)
    exchanges = []
    attempt_ids = set()
    ranges = [copy.deepcopy(dict(r)) for r in prior_range_observations]
    for path in sorted((output_root / "cpa-text-exchanges").glob("*/request.json")):
        request = json.loads(path.read_bytes())
        prompt = path.parent / "prompt.txt"
        raw = prompt.read_bytes()
        if (request.get("schema_version") != "private-cpa-text-exchange.v1"
                or request.get("status") != "STARTED"
                or request.get("prompt_sha256") != hashlib.sha256(raw).hexdigest()):
            raise ValueError("RETAINED_PARTIAL_REQUEST_INVALID")
        attempt_ids.add(request["production_attempt_id"])
        terminal, completion = path.parent / "terminal.json", path.parent / "completion.txt"
        item = {"request": _ref(path), "prompt": _ref(prompt),
                "terminal": _ref(terminal) if terminal.exists() else None,
                "completion": _ref(completion) if completion.exists() else None}
        # A missing terminal remains UNKNOWN even if a completion file exists.
        if terminal.exists():
            verdict = json.loads(terminal.read_bytes())
            if verdict.get("status") == "COMPLETED" and (
                    not completion.is_file()
                    or verdict.get("completion_sha256") != _evidence_file_sha256(completion)):
                raise ValueError("RETAINED_PARTIAL_COMPLETION_INVALID")
        exchanges.append(item)
        ranges.extend(copy.deepcopy(v) for v in json_objects(raw.decode("utf-8"))
                      if v.get("schema_version") == "range-acoustic-context-observation.v1")
    if len(attempt_ids) != 1:
        raise ValueError("RETAINED_PARTIAL_ATTEMPT_IDENTITY_INVALID")
    usage = _ref(output_root / (candidate_id + ".cpa-usage.jsonl"))
    events = [json.loads(line) for line in read_bound(usage)[1].splitlines() if line.strip()]
    if any(row.get("production_attempt_id") not in attempt_ids for row in events):
        raise ValueError("RETAINED_PARTIAL_USAGE_ATTEMPT_DRIFT")
    inventory = []
    for directory in ("native-exact", "cache", "independent-context",
                      "gemini-range-acquisition", "gemini-range-context"):
        for path in sorted((output_root / directory).rglob("*")):
            if path.is_file() and path.suffix != ".lock":
                inventory.append(_ref(path))
    unique_ranges = {}
    for row in ranges:
        bindings = row.get("artifact_bindings")
        if isinstance(bindings, Mapping):
            key = json.dumps(dict(bindings), sort_keys=True, ensure_ascii=False)
            unique_ranges.setdefault(key, row)
    payload = {
        "schema_version": SCHEMA, "candidate_id": candidate_id,
        "status": "PARTIAL_EVIDENCE_ONLY", "fresh_review_required": True,
        "mutation_authorized": False, "release_authorized": False,
        "output_root": str(output_root), "source": source_ref,
        "parent_authority": dict(parent_authority), "parent_native_budget": dict(parent_native_budget),
        "native_budget": latest, "cache_inventory": inventory,
        "range_observations": list(unique_ranges.values()),
        "raw_cpa_usage": usage, "raw_usage_event_count": len(events),
        # Transport events are preserved in full. They cannot be converted to
        # logical adjudication admissions, nor silently counted as zero.
        "partial_logical_adjudication_count": None,
        "partial_logical_usage_status": "UNKNOWN_PRESERVED_IN_ORIGINAL_EVENTS",
        "cpa_exchanges": {
            "schema_version": EXCHANGES_SCHEMA, "source_context": dict(source_context),
            "source_media_sha256": source_sha,
            "production_attempt_id": next(iter(attempt_ids)), "exchanges": exchanges,
        },
    }
    _create(manifest_path, (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode())
    return _ref(manifest_path)


def validate_original_segmented_range(observation: Mapping, *, source: Path, cid: str) -> dict | None:
    """Revalidate the original acquisition; do not assert any target consumed it."""
    from src.autoslice.independent_context_adjudication import validate_range_acoustic_observation
    from src.autoslice.jingting_chunker import parse_srt_cues
    bindings = observation.get("artifact_bindings") or {}
    if not bindings.get("segmented_input_path"):
        return None
    _, input_raw = read_bound({"path": bindings["segmented_input_path"],
                              "sha256": bindings["segmented_input_sha256"]})
    packet = json.loads(input_raw)
    _, text_raw = read_bound({"path": bindings["reviewed_srt_path"],
                             "sha256": bindings["reviewed_srt_sha256"]})
    text = text_raw.decode("utf-8")
    cues = [c for c in parse_srt_cues(text) if c.text.strip()]
    window = packet["delivery_target_ms"]
    eligible = [i for i in packet["cue_indexes"] if type(i) is int and 1 <= i <= len(cues)
                and window[0] <= cues[i - 1].start_ms < cues[i - 1].end_ms <= window[1]]
    if not eligible:
        raise ValueError("RETAINED_PARTIAL_RANGE_GEOMETRY_INVALID")
    index = eligible[0]
    cue = cues[index - 1]
    # This ephemeral validation view exercises the ordinary original-artifact
    # validator. It is not persisted as a request or as a consumption event.
    request = {"whole_clip_current_srt": text, "cue_indexes": [index],
        "matched_start_ms": cue.start_ms, "matched_end_ms": cue.end_ms,
        "source_media_timeline_offset_ms": packet["source_media_timeline_offset_ms"],
        "clip_context_binding": {"candidate_id": cid,
            "source_media_sha256s": [_evidence_file_sha256(source)]}}
    valid, reason = validate_range_acoustic_observation(text, request, observation)
    if valid is None:
        raise ValueError("RETAINED_PARTIAL_RANGE_INVALID:" + str(reason))
    return valid


def validate_partial_inputs(*, reference: Mapping, candidate_id: str, source: Path,
                          parent_authority_sha256: str) -> dict:
    """Read and validate all original partial facts, without account mutations."""
    path, raw = read_bound(reference)
    manifest = json.loads(raw)
    if (manifest.get("schema_version") != SCHEMA
            or manifest.get("candidate_id") != candidate_id
            or manifest.get("status") != "PARTIAL_EVIDENCE_ONLY"
            or manifest.get("fresh_review_required") is not True
            or manifest.get("mutation_authorized") is not False
            or manifest.get("release_authorized") is not False
            or manifest["parent_authority"]["sha256"].removeprefix("sha256:")
            != parent_authority_sha256.removeprefix("sha256:")):
        raise ValueError("RETAINED_PARTIAL_PARENT_DRIFT")
    read_bound(manifest["parent_authority"])
    _, source_raw = read_bound(manifest["source"])
    if bytes.fromhex(_evidence_file_sha256(source)) != hashlib.sha256(source_raw).digest():
        raise ValueError("RETAINED_PARTIAL_SOURCE_DRIFT")
    source_sha = hashlib.sha256(source_raw).hexdigest()
    _, old_raw = read_bound(manifest["parent_native_budget"])
    _, new_raw = read_bound(manifest["native_budget"])
    old, new = json.loads(old_raw), json.loads(new_raw)
    for receipt in (old, new):
        _validate_existing(receipt, source_media_sha256=source_sha)
        _validate_budget_history(receipt["budget"])
    _require_budget_history_extension(old, new)
    for item in manifest["cache_inventory"]:
        read_bound(item)
    _, usage_raw = read_bound(manifest["raw_cpa_usage"])
    events = [json.loads(line) for line in usage_raw.splitlines() if line.strip()]
    if len(events) != manifest["raw_usage_event_count"]:
        raise ValueError("RETAINED_PARTIAL_USAGE_DRIFT")
    # Validate every exchange before any native-account mutation, even those
    # which do not have a scoped target and therefore cannot suppress novelty.
    from src.autoslice.semantic_request_history import validate_exchange_manifest
    exchange_counts = validate_exchange_manifest(manifest["cpa_exchanges"])
    for row in manifest["range_observations"]:
        validate_original_segmented_range(row, source=source, cid=candidate_id)
    return path, manifest, new, events, exchange_counts


def load_partial_evidence(*, reference: Mapping, candidate_id: str, source: Path,
                          parent_authority_sha256: str, runtime_root: Path) -> dict:
    """Restore the existing native account and expose original requests only."""
    path, manifest, new, events, exchange_counts = validate_partial_inputs(
        reference=reference,candidate_id=candidate_id,source=source,
        parent_authority_sha256=parent_authority_sha256)
    from src.autoslice.prepared_native_audio_recovery import inherit_prepared_native_audio
    native = inherit_prepared_native_audio(cid=candidate_id, parent=Path(manifest["output_root"]),
        source=source, runtime_root=runtime_root)
    # This is an input proof. Keep partial count UNKNOWN and parent logical
    # usage separate; importing 59 HTTP exchanges cannot increment that count.
    _, _, parent = read_review_bound(manifest["parent_authority"])
    parent_count = (parent.get("cpa_adjudication_usage") or {}).get("provider_adjudication_count")
    proof = {"schema_version": SCHEMA, "status": "PARTIAL_EVIDENCE_ONLY",
        "manifest": {"path": str(path), "sha256": reference["sha256"]},
        "parent_authority": dict(manifest["parent_authority"]),
        "fresh_review_required": True, "release_authorized": False,
        "native_original_attempt_count": new["budget"]["attempt_count"],
        "original_exchange_counts": exchange_counts,
        "raw_cpa_usage": manifest["raw_cpa_usage"], "raw_usage_event_count": len(events),
        "partial_logical_adjudication_count": None,
        "partial_logical_usage_status": manifest["partial_logical_usage_status"],
        "original_output_root": manifest["output_root"],
        "semantic_history_sources": [{"kind": "exchanges", "path": str(path), "sha256": reference["sha256"]}],
        "mutation_authorized": False}
    if type(parent_count) is int and parent_count >= 0:
        proof["parent_logical_adjudication_count"] = parent_count
    from src.autoslice.supplement_audio_budget import get_budget
    account_history = get_budget(source).snapshot().get("account_history")
    if account_history:
        # Provenance only: the plan was derived by the ordinary inheritance
        # from registered ancestry; it is not an input a later load may supply.
        proof["native_audio_history_reconciliation"] = dict(account_history["plan_reference"])
    proof["native_account_attempt_count"] = get_budget(source).snapshot()["attempt_count"]
    return {"proof": proof, "native": native}


def partial_ranges_from_audit(audit: Mapping | None) -> tuple[dict, ...]:
    from src.autoslice.semantic_request_history import partial_proof_from_audit
    proof = partial_proof_from_audit(audit)
    ref = proof.get("manifest")
    if not ref:
        return ()
    _, raw = read_bound(ref)
    return tuple(copy.deepcopy(json.loads(raw)["range_observations"]))
