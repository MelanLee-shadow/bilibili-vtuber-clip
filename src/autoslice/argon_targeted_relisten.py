"""Receipt-bound relisten admission into the existing Argon finding/session.

This module validates evidence and recommends the authorized stronger model.
It owns no transport, credential, budget, finding identity or edit authority.
"""
from copy import deepcopy
from dataclasses import asdict
from .validation_memo import memoized_validation
from collections.abc import Mapping

from . import argon_escalation_storage as store
from .argon_escalation_contract import MODEL, ContractError, canonical, require, sha, strict_json
from .span_phoneme_resolution import edit_binding

SCHEMA = "argon-targeted-relisten-trigger.v1"


def _recommendation(spec, evidence):
    from .argon_escalation_integration import _check_request
    from .exact_final_witness_authority import history_audio_recovery_from_adjudication
    from .semantic_audio_recovery import _same_pending_audio_target
    from .targeted_relisten import evidence_projection

    check, original, prior = (evidence[k] for k in ("check", "original_request", "prior"))
    _check_request(check)
    _check_request(original)
    require(isinstance(prior, Mapping) and isinstance(prior.get("request"), Mapping),
            "ARGON_TARGETED_ORIGINAL_GATE_MISSING")
    recovery = history_audio_recovery_from_adjudication(prior, request=prior["request"])
    require(isinstance(recovery, Mapping) and recovery.get("status") == "BLOCK"
            and recovery.get("needs_audio") is True and recovery == evidence["recovery"],
            "ARGON_TARGETED_ORIGINAL_GATE_DRIFT")
    bound_original = recovery.get("original_request", prior["request"])
    require(bound_original == original and _same_pending_audio_target(original, check),
            "ARGON_TARGETED_CURRENT_GATE_DRIFT")
    require(all((original.get("clip_context_binding") or {}).get(k)
                == (check.get("clip_context_binding") or {}).get(k)
                for k in ("candidate_id", "source_media_sha256s")),
            "ARGON_TARGETED_CONTEXT_DRIFT")
    # Reopen source, actual audio, prompts, raw responses and assisted sources.
    # A saved count or caller/model label cannot create an observation.
    projection = evidence_projection(check)
    require(canonical(projection) == canonical(evidence["projection"]),
            "ARGON_TARGETED_OBSERVATION_REPLAY_DRIFT")
    physical = projection["physical_target"]
    # The Argon finding is bound to the media actually heard (often the
    # producer padded file); its recording ancestry comes from the shared,
    # validated provenance reader, never a direct padded-vs-recording compare.
    from .targeted_relisten import validated_media_recordings
    finding_source = str(spec.finding.source_media_sha256).removeprefix("sha256:")
    recordings = ([finding_source] if finding_source in physical["source_media_sha256s"]
                  else validated_media_recordings(check, str(spec.source_path), finding_source))
    require(list(spec.finding.target_source_ms) == physical["physical_target_ms"]
            and bool(recordings) and set(recordings) <= set(physical["source_media_sha256s"]),
            "ARGON_TARGETED_PHYSICAL_BINDING_DRIFT")
    require(projection["effective_distinct_observations"] >= 3
            and not any(r["receipt_kind"] == "argon_native_transcript"
                        for r in projection["observations"]),
            "ARGON_THREE_VALID_ORDINARY_OBSERVATIONS_REQUIRED")
    # Range acquisition uses a CURRENT-only blind scaffold. The original
    # pending gate retains the exact disputed edit span, without proposing
    # that edit to the hearing provider.
    span = edit_binding(original)
    gate = recovery["gate"]
    missing = gate.get("reason")
    require(isinstance(missing, str) and missing.strip(), "ARGON_MISSING_EVIDENCE_REQUIRED")
    return {
        "binding": spec.finding.binding(), "recommend_upgrade": True,
        "basis": "STRONGER_HEARING_MODEL_AFTER_THREE_VALID_OBSERVATIONS",
        "requested_model": MODEL, "physical_target": physical,
        "effective_distinct_observations": projection["effective_distinct_observations"],
        "prior_observation_ids": [r["observation_id"] for r in projection["observations"]],
        "disputed_units": check["suspect"],
        "disputed_codepoint_span": span["codepoint_span"] if span else None,
        "disputed_scope": "EXACT_EDIT_SPAN" if span else "PHYSICAL_CUE_WITH_DISPUTED_UNITS",
        "current_cue": check["current_cue"], "missing_evidence": missing,
        "input_delta": {"capability_upgrade": MODEL,
                        "context_source_ms": list(spec.context_source_ms),
                        "same_original_audio_allowed": True},
        "expected_gain": "已验证普通补听仍缺上述字音证据；使用获授权的更强Argon听力模型重新辨别同一原音。模型升级本身是继续依据，transport或key切换不是再次升级。",
        "verification": "重开源/物理窗/实际模型选择、prompt、raw和heard/uncertain；交原CPA核当前目标；清楚原音优先，保持所有旧观察与辅助来源依赖。",
        "failure_exit": "UNRESOLVED_WITH_DIAGNOSTIC",
        "count_is_truth": False, "mutation_authorized": False, "release_authorized": False,
    }


def make_trigger(root, spec, *, check, original_request, prior, recovery, projection):
    evidence = deepcopy({"check": check, "original_request": original_request,
                         "prior": prior, "recovery": recovery, "projection": projection})
    recommendation = _recommendation(spec, evidence)
    return {"schema_version": SCHEMA, "finding": spec.finding.binding(),
            "evidence_sha256": store.blob(root, canonical(evidence)),
            "recommendation": recommendation}


def _context_window(request):
    offset = request.get("source_media_timeline_offset_ms")
    values = (request.get("context_start_ms"), request.get("context_end_ms"), offset)
    if any(type(value) is not int for value in values):
        return None
    return values[0] + offset, values[1] + offset


def _check_summary(request):
    """What a rejection audit needs to see of a compared check request."""
    clip = request.get("clip_context_binding") if isinstance(request.get("clip_context_binding"), Mapping) else {}
    srt = request.get("whole_clip_current_srt")
    provenance = request.get("candidate_provenance")
    return {
        "request_sha256": request.get("request_sha256"),
        "current_cue_sha256": sha(str(request.get("current_cue") or "").encode()),
        "base_text_sha256": request.get("base_text_sha256"),
        "suspect": request.get("suspect"), "repair_class": request.get("repair_class"),
        "cue_indexes": request.get("cue_indexes"),
        "matched_ms": [request.get("matched_start_ms"), request.get("matched_end_ms")],
        "context_ms": [request.get("context_start_ms"), request.get("context_end_ms")],
        "source_media_timeline_offset_ms": request.get("source_media_timeline_offset_ms"),
        "whole_clip_current_srt_sha256": sha(srt.encode()) if isinstance(srt, str) else None,
        "require_complete_utterance_support": request.get("require_complete_utterance_support"),
        "candidate_provenance_kind": provenance.get("kind") if isinstance(provenance, Mapping) else None,
        "clip_context_binding": {key: clip.get(key) for key in (
            "candidate_id", "source_media_sha256s", "context_sha256", "whole_clip_draft_srt_sha256")},
    }


def trigger_question_identity(bound, current):
    """G6 shared judgment for an Argon trigger: does the current check ask the
    same audio question as the check the trigger was recorded for?

    The question is the pending audio obligation of the current cue: cue text
    and base hash, the physical target and the context window on the source
    clock, and the clip/source identity (``same_audio_question`` without the
    candidate).  The candidate-derived fields -- proposed text, ``suspect``
    (whole cue in the candidate-blind scaffold, the minimal span once Argon's
    own transcript is the candidate) and ``repair_class`` -- and the
    representation fields -- cue ordinals, the local clock, the whole-film SRT
    bytes and the whole-clip context hashes, on one clock or across a
    source/delivery clock change -- are recorded as deltas, never identity
    (admin an edit in another cue, or consuming the Argon
    transcript as the candidate, must not void this target's hearing; G6 class
    ruling for the cross-clock case).  The source binding (candidate,
    recordings, piece clock where carried) stays identity.
    The disputed edit span stays checked by ``validate_trigger``.
    Returns ``(same, diagnostic)``; the diagnostic names what differed and
    summarises both compared checks for the rejection audit.
    """
    from .audio_question_identity import same_audio_question

    same, deltas = same_audio_question(bound, current, compare_proposed=False, strict_source=True)
    failed = [] if same else [deltas.get("reason") or "QUESTION_DIFFERS"]
    if _context_window(bound) is None or _context_window(bound) != _context_window(current):
        failed.append("CONTEXT_WINDOW_DIFFERS")
    for key in ("suspect", "repair_class", "whole_clip_current_srt", "candidate_provenance"):
        if bound.get(key) != current.get(key):
            deltas = {**deltas, key: "DIFFERS"}
    return not failed, {
        "failed": failed, "recorded_deltas": deltas if same else {},
        "bound_check": _check_summary(bound), "current_check": _check_summary(current),
    }


def validate_trigger(root, spec, trigger, *, current_check=None):
    require(trigger.get("schema_version") == SCHEMA, "ARGON_TARGETED_SCHEMA_INVALID")
    raw = store.get_blob(root, trigger["evidence_sha256"])
    _validate_trigger_bytes(root, spec, trigger, raw, current_check=current_check)
    return strict_json(raw)


@memoized_validation(lambda root, spec, trigger, raw, current_check=None:
    sha(canonical([str(root.absolute()), {**asdict(spec), 'source_path': str(spec.source_path)}, trigger, sha(raw), current_check])))
def _validate_trigger_bytes(root, spec, trigger, raw, *, current_check=None):
    require(trigger.get("schema_version") == SCHEMA, "ARGON_TARGETED_SCHEMA_INVALID")
    evidence = strict_json(raw)
    expected = {"schema_version": SCHEMA, "finding": spec.finding.binding(),
                "evidence_sha256": sha(canonical(evidence)),
                "recommendation": _recommendation(spec, evidence)}
    require(canonical(trigger) == canonical(expected), "ARGON_TARGETED_TRIGGER_DRIFT")
    if current_check is not None:
        from .argon_escalation_integration import _check_request
        _check_request(current_check)
        same, diagnostic = trigger_question_identity(evidence["check"], current_check)
        if not same:
            error = ContractError("ARGON_TARGETED_CURRENT_CONTEXT_DRIFT")
            # Rejection audit (admin, sister cue26): what differed
            # and a summary of the current_check actually compared.
            error.diagnostic = diagnostic
            raise error
        old_span = edit_binding(evidence["original_request"])
        current_span = edit_binding(current_check)
        if old_span is not None:
            start, end = old_span["codepoint_span"]
            if (current_check["current_cue"][start:end] != old_span["current_span"]
                    or (current_span is not None
                        and old_span["codepoint_span"] != current_span["codepoint_span"])):
                error = ContractError("ARGON_TARGETED_CURRENT_SPAN_DRIFT")
                error.diagnostic = {**diagnostic, "failed": ["DISPUTED_SPAN_DIFFERS"],
                                    "bound_span": old_span["codepoint_span"],
                                    "current_span": current_span["codepoint_span"] if current_span else None}
                raise error
    return None
