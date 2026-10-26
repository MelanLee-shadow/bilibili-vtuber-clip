"""Read-only consumption of original CPA requests across retained source stages.

These facts can refuse duplicate work. They cannot replay a decision, authorize
an edit, increment a counter, or manufacture a semantic-attempt record.
"""
from __future__ import annotations

from .review_errors import ContractRejection

import hashlib
import json
import re
from copy import deepcopy
from threading import Lock
from collections.abc import Mapping
from pathlib import Path

from src.autoslice.jingting_chunker import parse_srt_cues
from src.autoslice.native_audio_budget_receipt import _no_links
from src.autoslice.semantic_retry_progress import bound_range_evidence, digest

HISTORY_KEY = "_semantic_history_sources"
EXCHANGES_SCHEMA = "retained-cpa-exchanges.v1"
OFFERED = frozenset({"STARTED", "COMPLETED", "ERROR", "OUTCOME_UNKNOWN"})


def read_bound(ref: Mapping) -> tuple[Path, bytes]:
    if not isinstance(ref, Mapping):
        raise ContractRejection("RETAINED_CPA_REFERENCE_INVALID")
    path = Path(ref["path"])
    expected = str(ref["sha256"]).removeprefix("sha256:")
    _no_links(path)
    if not path.is_absolute() or not path.is_file():
        raise ContractRejection("RETAINED_CPA_FILE_MISSING")
    raw = path.read_bytes()
    if len(expected) != 64 or hashlib.sha256(raw).hexdigest() != expected:
        raise ContractRejection("RETAINED_CPA_FILE_DRIFT")
    return path, raw


def read_review_bound(ref: Mapping) -> tuple[Path, bytes, dict]:
    """Read one bound authority and expand only review-bearing documents.

    ``read_bound`` remains the raw wire binding used for all hashes.  This
    helper is for consumers of a chat authority/parent authority: it detects
    the review-bearing surface after the raw bytes are checked, then delegates
    reference expansion to the one evidence-store implementation.  Ordinary
    state, plan, request, and exchange records retain plain JSON semantics.
    """

    path, raw = read_bound(ref)
    try:
        inline = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractRejection("RETAINED_CPA_AUTHORITY_JSON_INVALID") from exc
    if not isinstance(inline, Mapping):
        raise ContractRejection("RETAINED_CPA_AUTHORITY_OBJECT_INVALID")
    review_bearing = (
        path.name.endswith(".chat-authority.json")
        or inline.get("schema_version") == "chat-authority-audit.v2"
        or "final_review_audit" in inline
    )
    if review_bearing:
        from src.autoslice.review_evidence_store import review_from_bytes

        expanded = review_from_bytes(raw, directory=path.parent)
        if not isinstance(expanded, Mapping):
            raise ContractRejection("RETAINED_CPA_AUTHORITY_OBJECT_INVALID")
        return path, raw, dict(expanded)
    return path, raw, dict(inline)


def context_binding(context: Mapping) -> dict:
    memory = context.get("speech_memory")
    return {
        "schema_version": context.get("schema_version"),
        "whole_clip_draft_srt_sha256": context.get("whole_clip_draft_srt_sha256"),
        "speech_memory_ledger_sha256": memory.get("ledger_sha256") if isinstance(memory, Mapping) else None,
        "mutation_authorized": False,
        "candidate_id": context.get("candidate_id"),
        "context_sha256": context.get("context_sha256"),
        "source_media_sha256s": sorted({str(p["source_media_sha256"])
            for p in context.get("pieces", [])
            if isinstance(p, Mapping) and p.get("source_media_sha256")}),
    }


def _same_source(old: Mapping, current: Mapping) -> bool:
    binding = context_binding(current)
    # Context wording can change across stages. The source/candidate identity
    # and independently supplied physical offset cannot be replaced by cue IDs.
    return bool(binding["source_media_sha256s"]) and all(
        old.get(k) == binding[k] for k in ("candidate_id", "source_media_sha256s"))


def json_objects(text: str):
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", text):
        try:
            value, _ = decoder.raw_decode(text[match.start():])
        except ValueError:
            continue
        if isinstance(value, dict):
            yield value


def _span_block(prompt: str) -> dict | None:
    marker, end = "\nSPAN_REVIEW_INPUT\n", "\nEND_SPAN_REVIEW_INPUT\n"
    if marker not in prompt:
        return None
    if prompt.count(marker) != 1 or end not in prompt:
        raise ContractRejection("RETAINED_CPA_SPAN_BLOCK_INVALID")
    block = json.loads(prompt.split(marker, 1)[1].split(end, 1)[0])
    binding = block["binding"]
    body = dict(binding)
    declared = body.pop("binding_sha256", None)
    if declared != digest(body) or binding.get("schema_version") != "subtitle-edit-span-binding.v1":
        raise ContractRejection("RETAINED_CPA_SPAN_BINDING_INVALID")
    window, offset = binding.get("cue_window_ms"), binding.get("source_offset_ms")
    if (not isinstance(window, list) or len(window) != 2
            or any(type(v) is not int for v in [*window, offset])
            or min(window[0], offset) < 0 or window[1] <= window[0]
            or binding.get("physical_cue_window_ms") != [v + offset for v in window]):
        raise ContractRejection("RETAINED_CPA_PHYSICAL_CLOCK_INVALID")
    return block


def _request_target(request: Mapping, context: Mapping, target: tuple[int, int]) -> bool:
    body = dict(request)
    declared = str(body.pop("request_sha256", "")).removeprefix("sha256:")
    if declared != digest(body):
        return False
    try:
        indexes = request["cue_indexes"]
        cues = [c for c in parse_srt_cues(request["whole_clip_current_srt"]) if c.text.strip()]
        if len(indexes) != 1 or type(indexes[0]) is not int or not 1 <= indexes[0] <= len(cues):
            return False
        cue = cues[indexes[0] - 1]
        offset = request["source_media_timeline_offset_ms"]
        return (type(offset) is int and offset >= 0
            and (cue.start_ms + offset, cue.end_ms + offset) == target
            and (request["matched_start_ms"], request["matched_end_ms"]) == (cue.start_ms, cue.end_ms)
            and request["current_cue"] == cue.text
            and request["base_text_sha256"] == hashlib.sha256(cue.text.encode()).hexdigest()
            and _same_source(request["clip_context_binding"], context))
    except (KeyError, TypeError, IndexError):
        return False


def _walk(value):
    if isinstance(value, Mapping):
        yield value
        for item in value.values():
            yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item)


def _authority_records(value: Mapping, *, context: Mapping, target: tuple[int, int], origin: dict):
    # A prepared request alone proves no dispatch. Require an actual captured
    # judge outcome with its original raw completion and compilation request.
    seen = set()
    for judge in _walk(value):
        compilation = judge.get("span_compilation")
        raw = judge.get("span_review_raw_completion")
        if not isinstance(compilation, Mapping) or not isinstance(raw, str):
            continue
        request = compilation.get("request")
        if (judge.get("status") != "JUDGED" or not isinstance(request, Mapping)
                or not _request_target(request, context, target)):
            continue
        request_sha = request["request_sha256"]
        completion_sha = hashlib.sha256(raw.encode()).hexdigest()
        if (judge.get("check_request_sha256") != request_sha
                or compilation.get("request_sha256") != request_sha
                or judge.get("completion_sha256") != completion_sha
                or compilation.get("completion_sha256") != completion_sha):
            raise ContractRejection("RETAINED_CPA_JUDGE_BINDING_INVALID")
        identity = (request_sha, completion_sha)
        if identity in seen:
            continue
        seen.add(identity)
        ranges = bound_range_evidence({"_prior_range_acoustic_observations":
            request.get("context_only_range_acoustic_observations", [])})
        from src.autoslice.acoustic_witness_adjudication import retry_judge_evidence
        witness = compilation.get("witness") or {}
        observations = retry_judge_evidence(request, witness) if witness else []
        yield {"origin": origin, "kind": "CAPTURED_ORIGINAL_JUDGE",
            "status": "COMPLETED", "request_sha256": request_sha,
            "completion_sha256": completion_sha, "ranges": ranges,
            "binding": request.get("edit_span_binding"), "observations": observations}


def _archive_records(ref: Mapping, *, context: Mapping, target: tuple[int, int]):
    path, raw = read_bound(ref)
    manifest = json.loads(raw)
    if manifest.get("schema_version") == "retained-partial-evidence.v1":
        manifest = manifest["cpa_exchanges"]
    if manifest.get("schema_version") != EXCHANGES_SCHEMA:
        raise ContractRejection("RETAINED_CPA_ARCHIVE_SCHEMA_INVALID")
    validate_exchange_manifest(manifest)
    old_contexts = [json.loads(read_bound(ref)[1]) for ref in
                    manifest.get("source_contexts", [manifest["source_context"]])]
    if any(not _same_source(context_binding(old), context) for old in old_contexts):
        raise ContractRejection("RETAINED_CPA_ARCHIVE_SOURCE_DRIFT")
    context_hashes = {digest(context_binding(old)) for old in old_contexts}
    for item in manifest["exchanges"]:
        request_path, request_raw = read_bound(item["request"])
        _, prompt_raw = read_bound(item["prompt"])
        request = json.loads(request_raw)
        if (request.get("schema_version") != "private-cpa-text-exchange.v1"
                or request.get("status") != "STARTED"
                or request.get("mutation_authorized") is not False
                or request.get("production_attempt_id") not in manifest.get("production_attempt_ids", [manifest.get("production_attempt_id")])
                or request.get("prompt_sha256") != hashlib.sha256(prompt_raw).hexdigest()
                or not request.get("model_identity")):
            raise ContractRejection("RETAINED_CPA_ARCHIVE_REQUEST_INVALID")
        status = "OUTCOME_UNKNOWN"
        completion_sha = None
        if item.get("terminal") is not None:
            _, terminal_raw = read_bound(item["terminal"])
            terminal = json.loads(terminal_raw)
            status = terminal.get("status")
            if status not in OFFERED:
                raise ContractRejection("RETAINED_CPA_ARCHIVE_TERMINAL_INVALID")
            if status == "COMPLETED":
                _, completion = read_bound(item["completion"])
                completion_sha = hashlib.sha256(completion).hexdigest()
                if terminal.get("completion_sha256") != completion_sha:
                    raise ContractRejection("RETAINED_CPA_ARCHIVE_COMPLETION_INVALID")
        prompt = prompt_raw.decode("utf-8")
        block = _span_block(prompt)
        if block is None:
            continue  # Retained in the manifest; never guess a target from context.
        binding = block["binding"]
        if binding.get("source_context_sha256") not in context_hashes:
            raise ContractRejection("RETAINED_CPA_ARCHIVE_CONTEXT_DRIFT")
        if tuple(binding["physical_cue_window_ms"]) != target:
            continue
        observations = block.get("observations", [])
        if any(o.get("source_media_sha256") != manifest["source_media_sha256"]
               for o in observations):
            raise ContractRejection("RETAINED_CPA_ARCHIVE_AUDIO_SOURCE_DRIFT")
        ranges = bound_range_evidence({"_prior_range_acoustic_observations":
            [v for v in json_objects(prompt)
             if v.get("schema_version") == "range-acoustic-context-observation.v1"]})
        yield {"origin": {"manifest": str(path), "request": str(request_path),
                          "request_sha256": hashlib.sha256(request_raw).hexdigest()},
               "kind": "ORIGINAL_TEXT_EXCHANGE", "status": status,
               "request_sha256": block["request_sha256"], "completion_sha256": completion_sha,
               "binding": binding, "ranges": ranges, "observations": observations}


def submitted_history(*, finding: Mapping, context: Mapping, target: tuple[int, int]) -> list[dict]:
    records = list(_authority_records(finding, context=context, target=target,
                                     origin={"kind": "EMBEDDED_ORIGINAL_JUDGE"}))
    for ref in finding.get(HISTORY_KEY, []):
        if ref.get("kind") == "authority":
            path, _, authority = read_review_bound(ref)
            records.extend(_authority_records(authority, context=context, target=target,
                origin={"path": str(path), "sha256": ref["sha256"]}))
        elif ref.get("kind") == "exchanges":
            records.extend(_archive_records(ref, context=context, target=target))
        else:
            raise ContractRejection("RETAINED_CPA_HISTORY_KIND_INVALID")
    return records


def range_seen(identity: Mapping, records: list[dict]) -> bool:
    keys = ("source_media", "audio_clip", "provider", "model")
    return any(row["status"] in OFFERED and any(
        all(old.get(k) == identity.get(k) for k in keys) for old in row["ranges"]) for row in records)


def _audio_key(observation: Mapping) -> tuple:
    # Repacking the response or changing delivery/SRT wrappers is no acquisition.
    return tuple(observation.get(k) for k in
                 ("source_media_sha256", "audio_clip_sha256", "provider", "model"))


def evidence_seen(evidence: Mapping, records: list[dict]) -> bool:
    return any(row["status"] in OFFERED and any(
        _audio_key(old) == _audio_key(evidence) for old in row["observations"]) for row in records)


def submitted_question(prompt: str, ranges: list[dict], records: list[dict], plan: Mapping | None) -> dict | None:
    if not records:
        return None
    block = _span_block(prompt)
    if block is None:
        return None
    binding = block["binding"]
    keys = ("physical_cue_window_ms", "current_cue_sha256", "proposed_cue_sha256", "codepoint_span")
    same = [row for row in records if row["status"] in OFFERED
            and isinstance(row.get("binding"), Mapping)
            and all(row["binding"].get(k) == binding.get(k) for k in keys)]
    if not same:
        return None
    observations = block.get("observations", [])
    if any(not evidence_seen(o, same) for o in observations):
        return None
    if any(not range_seen(r, same) for r in ranges):
        return None
    # An explicit new question keeps the existing continuation-plan admission.
    # A cosmetics-only expand_context cannot rediscover these original bytes.
    unknown = [row for row in same if row["status"] != "COMPLETED"]
    if not unknown and plan and plan.get("action") != "expand_context":
        return None
    if not observations and not ranges:
        return None
    prior = (unknown or same)[-1]
    return {"status": ("PRIOR_CALL_OUTCOME_UNKNOWN" if unknown else "PRIOR_SOURCE_QUESTION_ALREADY_OFFERED"),
            "origin": prior["origin"], "original_request_sha256": prior["request_sha256"],
            "provider_calls": 0, "mutation_authorized": False}


def sources_from_audit(audit: Mapping | None) -> list[dict]:
    """Carry explicit original pointers through normal source and final callers."""
    audit = audit or {}
    refs = list(audit.get("semantic_history_sources") or [])
    proof = audit.get("retained_reviewed_draft") or {}
    paths = proof.get("history_paths") or {}
    if paths.get("parent_authority") and proof.get("parent_authority_sha256"):
        refs.append({"kind": "authority", "path": paths["parent_authority"],
                     "sha256": proof["parent_authority_sha256"]})
    refs.extend(proof.get("semantic_history_sources") or [])
    refs.extend(partial_proof_from_audit(audit).get("semantic_history_sources") or [])
    return list({digest(ref): ref for ref in refs}.values())


def validate_exchange_manifest(manifest: Mapping) -> dict[str, int]:
    """Verify all original transport records, including unscoped and UNKNOWN."""
    if manifest.get("schema_version") != EXCHANGES_SCHEMA:
        raise ContractRejection("RETAINED_CPA_ARCHIVE_SCHEMA_INVALID")
    for context in manifest.get("source_contexts", [manifest["source_context"]]):
        read_bound(context)
    counts = {"COMPLETED": 0, "OUTCOME_UNKNOWN": 0, "ERROR": 0, "STARTED": 0}
    seen = set()
    for item in manifest["exchanges"]:
        path, raw = read_bound(item["request"])
        if str(path) in seen:
            raise ContractRejection("RETAINED_CPA_DUPLICATE_ARCHIVE_POINTER")
        seen.add(str(path))
        request = json.loads(raw)
        _, prompt = read_bound(item["prompt"])
        if (request.get("schema_version") != "private-cpa-text-exchange.v1"
                or request.get("status") != "STARTED"
                or request.get("mutation_authorized") is not False
                or request.get("production_attempt_id") not in manifest.get("production_attempt_ids", [manifest.get("production_attempt_id")])
                or request.get("prompt_sha256") != hashlib.sha256(prompt).hexdigest()
                or not request.get("model_identity")):
            raise ContractRejection("RETAINED_CPA_ARCHIVE_REQUEST_INVALID")
        terminal = item.get("terminal")
        completion = read_bound(item["completion"])[1] if item.get("completion") else None
        if terminal is None:
            if (path.parent / "terminal.json").exists():
                raise ContractRejection("RETAINED_CPA_UNKNOWN_TERMINAL_DRIFT")
            counts["OUTCOME_UNKNOWN"] += 1
            continue
        status = json.loads(read_bound(terminal)[1])
        if status.get("status") not in OFFERED:
            raise ContractRejection("RETAINED_CPA_ARCHIVE_TERMINAL_INVALID")
        if status["status"] == "COMPLETED" and (completion is None
                or status.get("completion_sha256") != hashlib.sha256(completion).hexdigest()):
            raise ContractRejection("RETAINED_CPA_ARCHIVE_COMPLETION_INVALID")
        counts[status["status"]] += 1
    return counts

def freeze_parent_exchanges(*, output_root: Path, source_contexts: list[dict],
                           source_media_sha256: str) -> bytes | None:
    """Inventory real parent exchanges, including non-JUDGED and failed replies."""
    paths = sorted((output_root / "cpa-text-exchanges").glob("*/request.json"))
    if not paths:
        return None
    def ref(path):
        from .evidence_identity import file_sha256
        _no_links(path)
        return {"path": str(path), "sha256": "sha256:" + file_sha256(path)}
    exchanges, attempts = [], set()
    for path in paths:
        request = json.loads(path.read_bytes())
        attempts.add(request["production_attempt_id"])
        folder = path.parent
        exchanges.append({"request": ref(path), "prompt": ref(folder / "prompt.txt"),
            "terminal": ref(folder / "terminal.json") if (folder / "terminal.json").exists() else None,
            "completion": ref(folder / "completion.txt") if (folder / "completion.txt").exists() else None})
    manifest = {"schema_version": EXCHANGES_SCHEMA, "source_context": source_contexts[0],
        "source_contexts": source_contexts, "source_media_sha256": source_media_sha256.removeprefix("sha256:"),
        "production_attempt_ids": sorted(attempts), "exchanges": exchanges}
    validate_exchange_manifest(manifest)
    return (json.dumps(manifest, ensure_ascii=False, sort_keys=True) + "\n").encode()


def _validated_partial_usage(proof: Mapping, *, source_media_sha256, candidate_id):
    """Revalidate original records; a saved 'verified' or 'SOFT' flag is not authority."""
    from src.autoslice.retained_partial_evidence import validate_partial_inputs, SCHEMA
    if (proof.get("schema_version") != SCHEMA
            or proof.get("partial_logical_usage_status") != "UNKNOWN_PRESERVED_IN_ORIGINAL_EVENTS"
            or proof.get("partial_logical_adjudication_count") is not None
            or not isinstance(source_media_sha256, str)
            or not isinstance(candidate_id, str) or not candidate_id):
        raise ContractRejection("RETAINED_PARTIAL_USAGE_DRIFT")
    _, raw = read_bound(proof.get("manifest"))
    manifest = json.loads(raw)
    if (manifest.get("candidate_id") != candidate_id
            or str(manifest.get("source", {}).get("sha256", "")).removeprefix("sha256:")
                != source_media_sha256.removeprefix("sha256:")
            or proof.get("parent_authority") != manifest.get("parent_authority")
            or proof.get("raw_cpa_usage") != manifest.get("raw_cpa_usage")):
        raise ContractRejection("RETAINED_PARTIAL_PARENT_DRIFT")
    _, checked, _, events, counts = validate_partial_inputs(
        reference=proof["manifest"], candidate_id=candidate_id,
        source=Path(manifest["source"]["path"]),
        parent_authority_sha256=manifest["parent_authority"]["sha256"])
    if (proof.get("raw_usage_event_count") != len(events)
            or proof.get("original_exchange_counts") != counts
            or checked.get("partial_logical_usage_status") != proof["partial_logical_usage_status"]):
        raise ContractRejection("RETAINED_PARTIAL_USAGE_DRIFT")
    _, _, parent = read_review_bound(manifest["parent_authority"])
    count = (parent.get("cpa_adjudication_usage") or {}).get("provider_adjudication_count")
    if (type(count) is not int or count < 0
            or checked.get("partial_logical_adjudication_count") is not None
            or proof.get("parent_logical_adjudication_count", count) != count):
        raise ContractRejection("RETAINED_PARTIAL_USAGE_DRIFT")
    return {"exact_count":None, "known_lower_bound":count,
            "partial_increment_unknown":True, "manifest":dict(proof["manifest"])}


def with_partial_usage_guard(call, audit: Mapping | None, *,
                             usage_dependency="EXACT_COUNT_REQUIRED",
                             source_media_sha256=None, candidate_id=None):
    """Unknown logical usage only blocks consumers requiring its exact total.

    The normal caller declares SOFT_PRESSURE in code, never via an input flag.
    Actual source/parent/request validation runs before the first new transport.
    Cache reuse and the original target recorder still precede this wrapper.
    """
    if usage_dependency not in {"EXACT_COUNT_REQUIRED", "SOFT_PRESSURE"}:
        raise ContractRejection("CPA_ADJUDICATION_USAGE_INVALID")
    original_proof = partial_proof_from_audit(audit)
    if not original_proof:
        return call
    proof = deepcopy(dict(original_proof))
    binding = digest({"proof":proof, "source":source_media_sha256, "candidate":candidate_id})
    verified = None
    validation_lock = Lock()

    def require_same_lineage():
        from src.autoslice.semantic_retry_progress import CallNotDispatched
        try:
            current = digest({"proof":partial_proof_from_audit(audit),
                              "source":source_media_sha256, "candidate":candidate_id})
            if current != binding:
                raise ContractRejection("RETAINED_PARTIAL_LINEAGE_CONFLICT")
        except (ValueError,KeyError,TypeError) as exc:
            raise CallNotDispatched("RETAINED_PARTIAL_CPA_USAGE_UNKNOWN") from exc

    def guarded(prompt):
        nonlocal verified
        from src.autoslice.semantic_retry_progress import CallNotDispatched
        if usage_dependency == "EXACT_COUNT_REQUIRED":
            raise CallNotDispatched("RETAINED_PARTIAL_CPA_USAGE_UNKNOWN")
        require_same_lineage()
        with validation_lock:
            if verified is None:
                try:
                    checked = _validated_partial_usage(proof,
                        source_media_sha256=source_media_sha256,candidate_id=candidate_id)
                    require_same_lineage()
                    verified = checked
                except (OSError,ValueError,KeyError,TypeError) as exc:
                    raise CallNotDispatched("RETAINED_PARTIAL_CPA_USAGE_UNKNOWN") from exc
        current_count = ((audit or {}).get("cpa_adjudication_usage") or {}).get("provider_adjudication_count")
        if current_count is not None and (type(current_count) is not int or current_count < verified["known_lower_bound"]):
            raise CallNotDispatched("RETAINED_PARTIAL_CPA_USAGE_UNKNOWN")
        return call(prompt)
    for attr in ("cpa_cache_identity", "provider_runtime_binding", "provider_diagnostics", "cpa_resource_pressure"):
        if hasattr(call, attr):
            setattr(guarded, attr, getattr(call, attr))
    guarded.cpa_partial_usage_lineage = deepcopy(proof)
    return guarded


def preserve_partial_usage(usage: Mapping, audit: Mapping | None) -> dict:
    """Annotate the existing logical counter as a lower bound, never add HTTP rows."""
    result = dict(usage)
    proof = partial_proof_from_audit(audit)
    lineage = result.get("partial_usage_lineage")
    if not proof and isinstance(lineage, Mapping):
        proof = lineage
    if not proof:
        return result
    count = result.get("provider_adjudication_count")
    if type(count) is not int or count < 0:
        raise ContractRejection("CPA_ADJUDICATION_USAGE_INVALID")
    parent_count = proof.get("parent_logical_adjudication_count")
    if parent_count is not None and (type(parent_count) is not int or parent_count < 0 or count < parent_count):
        raise ContractRejection("CPA_ADJUDICATION_USAGE_INVALID")
    result.update(provider_adjudication_count_semantics="KNOWN_LOWER_BOUND",
                  exact_provider_adjudication_count=None,
                  known_provider_adjudication_lower_bound=count)
    result["partial_usage_lineage"] = deepcopy(dict(proof))
    return result


def partial_proof_from_audit(audit: Mapping | None) -> Mapping:
    """The same UNKNOWN usage/history proof survives normal save and reopen."""
    audit = audit or {}
    proofs = [p for p in (
        (audit.get("retained_reviewed_draft") or {}).get("partial_evidence"),
        audit.get("retained_partial_evidence"),
        (audit.get("final_review_audit") or {}).get("retained_partial_evidence"),
        (audit.get("cpa_adjudication_usage") or {}).get("partial_usage_lineage"),
        ((audit.get("final_review_audit") or {}).get("cpa_adjudication_usage") or {}).get("partial_usage_lineage"),
    ) if isinstance(p, Mapping) and p]
    if not proofs:
        if (audit.get("cpa_adjudication_usage") or {}).get("provider_adjudication_count_semantics") == "KNOWN_LOWER_BOUND":
            raise ContractRejection("RETAINED_PARTIAL_LINEAGE_CONFLICT")
        return {}
    if any(p.get("manifest") != proofs[0].get("manifest") for p in proofs):
        raise ContractRejection("RETAINED_PARTIAL_LINEAGE_CONFLICT")
    return proofs[0]
