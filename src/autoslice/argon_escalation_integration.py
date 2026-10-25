"""Ordinary finding -> reviewed Argon engine -> ordinary CPA evidence bridge.

No status supplied by a finding can manufacture a hearing attempt. The bridge
reads the normal verifier's exact manifest and raw files, then retains immutable
copies for cold recovery. Argon remains a candidate-blind transcript, not pinyin,
human truth, a speaker identity, or an independent mutation authority.
"""

from __future__ import annotations

import copy
import math
import os
import re
from pathlib import Path
import time
from typing import Mapping
from dataclasses import dataclass

from . import argon_escalation_storage as store
from .argon_escalation_contract import (
    AudioSpec,
    ContractError,
    Finding,
    OrdinaryAttempt,
    canonical,
    file_sha,
    recommend_escalation,
    require,
    sha,
    strict_json,
    window,
)
from .argon_escalation_runtime import ArgonRuntime
from .argon_escalation_witness import ArgonEscalationWitness
from .native_witness_artifact_contract import native_witness_artifact_contract

EVIDENCE_SCHEMA = "argon-consumer-evidence.v1"


@dataclass(frozen=True)
class ArgonSourceBindingContract:
    spec: AudioSpec

    @property
    def payload(self):
        return {"source_path": str(self.spec.source_path), **self.spec.finding.binding(),
                "context_source_ms": list(self.spec.context_source_ms)}

    def matches(self, recorded):
        return (isinstance(recorded.get("source_path"), str)
                and {k: v for k, v in recorded.items() if k != "source_path"}
                == {k: v for k, v in self.payload.items() if k != "source_path"})


def argon_source_binding_contract(spec):
    return ArgonSourceBindingContract(spec)


@dataclass(frozen=True)
class ArgonConsumerContract:
    spec: AudioSpec
    result: Mapping | None = None

    @property
    def request_binding(self):
        return {"argon_finding_id": self.spec.finding.finding_id,
                "argon_context_source_ms": list(self.spec.context_source_ms),
                "argon_source_media_sha256": self.spec.finding.source_media_sha256}

    @property
    def material_identity(self):
        b = self.result["binding"]
        return {"kind": "argon_acquisition", "source_media_sha256": b["source_media_sha256"],
                "audio_clip_sha256": b["input_audio_sha256"],
                "acquisition_request_sha256": self.result["inner_request_sha256"],
                "target_start_ms": b["target_source_ms"][0], "target_end_ms": b["target_source_ms"][1],
                "crop_start_ms": b["context_source_ms"][0], "crop_end_ms": b["context_source_ms"][1]}

    def envelope(self, root, outcome):
        result = outcome["result"]
        payload = {"schema_version": EVIDENCE_SCHEMA, "consumer_root": str(root),
                   "source_binding": store.get_json(root / "source-binding.json"),
                   "engine_root": outcome["engine_root"], "task_id": result["task_id"],
                   "result_sha256": result["result_sha256"]}
        return {**payload, "envelope_sha256": sha(canonical(payload))}


def argon_consumer_contract(spec, result=None):
    return ArgonConsumerContract(spec, result)


def argon_session_binding_contract(source_sha, request, finding):
    offset = request["source_media_timeline_offset_ms"]
    target = (request["matched_start_ms"] + offset, request["matched_end_ms"] + offset)
    identity = {"source": source_sha, "target": target, "base": request["base_text_sha256"],
                "suspect": request["suspect"],
                "span": [finding.get("span_start_codepoint"), finding.get("span_end_codepoint")],
                "repair_class": request["repair_class"]}
    return target, sha(canonical(identity))


def ordinary_history_event_contract(spec, ordinary, raw_judge):
    event = {"finding": spec.finding.binding(), "ordinary": ordinary,
             "judge_after_audio": bool(raw_judge and not raw_judge["request"]["prompt"].startswith("# 字幕文字闭集裁决")),
             "judge_record": None}
    return event


_DIAGNOSED_TRIGGER_SCHEMA = "argon-diagnosed-upgrade-trigger.v1"
_DIAGNOSED_TRIGGER_REASONS = frozenset({"READING_CHAT", "LYRICS_REFERENCE_CONFLICT"})


def argon_trigger_contract(spec, recommendation, *, version=_DIAGNOSED_TRIGGER_SCHEMA):
    payload = {"finding": spec.finding.binding(), "recommendation": recommendation}
    if version is not None:
        payload["schema_version"] = version
    return payload


def argon_config_contract(config):
    """Validate the sole public construction shape before any runtime is built."""
    if config is None or config is False or (isinstance(config, Mapping) and config.get("enabled") is False):
        return None
    require(isinstance(config, Mapping) and config.get("enabled") is True, "ARGON_OPT_IN_INVALID")
    bools = ("allow_paid", "allow_realtime_fallback")
    limits = {"request_timeout_s": 600, "slot_wait_s": 10800}
    require(set(config) <= {"enabled", *bools, *limits, "comparison_plan", "comparison_grant", "comparison_case_ids"},
            "ARGON_CONFIG_INVALID")
    for name in bools:
        require(name not in config or type(config[name]) is bool, "ARGON_CONFIG_INVALID")
    for name, limit in limits.items():
        require(name not in config or type(config[name]) in (int, float)
                and math.isfinite(config[name]) and 0 < config[name] <= limit, "ARGON_CONFIG_INVALID")
    return config


def ordinary_38_model(provider, model) -> bool:
    # AGY's real configured identity is a display label, not the REST model ID.
    # Preserve that label in every receipt; never relabel it as modelVersion.
    from .agy_gemini_client import DEFAULT_AGY_FLASH_HIGH, DEFAULT_AGY_FLASH_LOW
    return bool(isinstance(model, str) and (
        provider == "agy" and model in {DEFAULT_AGY_FLASH_HIGH, DEFAULT_AGY_FLASH_LOW}
        or provider in {"agy", "gemini_api"} and re.fullmatch(r"gemini-3\.8-flash(?:-high|-thinking)?", model)))


def _check_request(request: Mapping) -> None:
    require(isinstance(request, Mapping), "ARGON_CHECK_REQUEST_INVALID")
    unsigned = dict(request)
    digest = unsigned.pop("request_sha256", None)
    require(sha(canonical(unsigned)) == digest, "ARGON_CHECK_REQUEST_HASH_DRIFT")
    from .jingting_chunker import parse_srt_cues

    cues = parse_srt_cues(request["whole_clip_current_srt"])
    indexes = request["cue_indexes"]
    require(
        isinstance(indexes, list)
        and len(indexes) == 1
        and type(indexes[0]) is int
        and 1 <= indexes[0] <= len(cues),
        "ARGON_CURRENT_CUE_INVALID",
    )
    cue = cues[indexes[0] - 1]
    require(
        (cue.start_ms, cue.end_ms, cue.text)
        == (request["matched_start_ms"], request["matched_end_ms"], request["current_cue"])
        and sha(cue.text.encode()) == request["base_text_sha256"],
        "ARGON_CURRENT_CUE_DRIFT",
    )
    require(
        type(request["source_media_timeline_offset_ms"]) is int
        and request["source_media_timeline_offset_ms"] >= 0,
        "ARGON_SOURCE_CLOCK_INVALID",
    )
    window([request["matched_start_ms"], request["matched_end_ms"]])
    window([request["context_start_ms"], request["context_end_ms"]])


def spec_from_envelope(envelope: Mapping) -> AudioSpec:
    b = envelope["source_binding"]
    return AudioSpec(
        Path(b["source_path"]),
        Finding(b["source_media_sha256"], tuple(b["target_source_ms"]), b["finding_id"]),
        tuple(b["context_source_ms"]),
    )


def validate_consumer_evidence(check_request: Mapping, envelope: Mapping) -> dict:
    """Replay native bytes on every normal CPA/recorder use, including cache hits."""
    _check_request(check_request)
    require(
        isinstance(envelope, Mapping) and envelope.get("schema_version") == EVIDENCE_SCHEMA,
        "ARGON_CONSUMER_SCHEMA_INVALID",
    )
    unsigned = dict(envelope)
    supplied = unsigned.pop("envelope_sha256", None)
    require(sha(canonical(unsigned)) == supplied, "ARGON_CONSUMER_ENVELOPE_DRIFT")
    spec = spec_from_envelope(envelope)
    contract = argon_consumer_contract(spec)
    offset = check_request["source_media_timeline_offset_ms"]
    require(
        tuple(spec.finding.target_source_ms)
        == (check_request["matched_start_ms"] + offset, check_request["matched_end_ms"] + offset),
        "ARGON_CONSUMER_TARGET_DRIFT",
    )
    require(
        check_request.get("argon_finding_id") == contract.request_binding["argon_finding_id"],
        "ARGON_CONSUMER_FINDING_DRIFT",
    )
    require(
        check_request.get("argon_context_source_ms") == contract.request_binding["argon_context_source_ms"],
        "ARGON_CONSUMER_CONTEXT_DRIFT",
    )
    require(
        check_request.get("argon_source_media_sha256") == contract.request_binding["argon_source_media_sha256"],
        "ARGON_CONSUMER_SOURCE_DRIFT",
    )
    # The original source-bound consumer locates policy/credential/predecessor
    # records. engine_root is only the location of the retained evidence copy.
    consumer_root = Path(envelope["consumer_root"])
    require(
        store.get_json(consumer_root / "source-binding.json") == envelope["source_binding"],
        "ARGON_ORIGINAL_SOURCE_BINDING_DRIFT",
    )
    engine = ArgonEscalationWitness(Path(envelope["engine_root"]), read_only=True)
    raw = store.get_blob(engine.root / envelope["task_id"], envelope["result_sha256"])
    result = strict_json(raw)
    result = engine.validate_evidence(envelope["task_id"], result, expected=spec,
                                      acquisition_root=consumer_root / "batch")
    require(
        result["status"] == "OBSERVED" and (
            result["model_version"] == "gemini-4-argon" or result["mode"] == "agy"
            and result.get("provider") == "agy"
            and result.get("model_identity_basis") == "ACTUAL_CLI_MODEL_SELECTION"),
        "ARGON_CONSUMER_NOT_OBSERVED",
    )
    require(any(h["mode"] == result["mode"] and h["state"] == "COMPLETED"
                for h in result["accounting"]["histories"]), "ARGON_COMPLETED_SUBMISSION_REQUIRED")
    require((consumer_root / "trigger.json").is_file(), "ARGON_TRIGGER_MISSING")
    history = read_ordinary_history(consumer_root, spec)
    trigger = validate_trigger(consumer_root, spec, history)
    # validate_trigger has already required either a validated targeted trigger
    # or a recomputed ordinary recommendation. Empty ordinary history cannot
    # recommend an upgrade; a second HISTORY_MISSING rejection here was dead.
    observations = []
    acquired_check = check_request
    for receipt, status, flags, event in history:
        native = strict_json(store.get_blob(consumer_root, receipt))
        previous_decision = None
        if event.get("judge_record"):
            previous = strict_json(store.get_blob(consumer_root, event["judge_record"]))
            previous_decision = {
                "response_sha256": previous["completion_sha256"],
                "raw_decision": strict_json(previous["completion"]),
                "after_audio": event["judge_after_audio"],
            }
        observations.append(
            {
                "raw_sha256": receipt,
                "native": native,
                "ordinary_status": status,
                "diagnosis": flags,
                "prior_cpa_decision": previous_decision,
            }
        )
    if trigger.get("schema_version") == "argon-targeted-relisten-trigger.v1":
        from .argon_targeted_relisten import validate_trigger as validate_targeted
        # The native acquisition heard the whole physical cue. A downstream
        # candidate can nominate a different edit span without changing that
        # hearing. Validate the immutable trigger on its own original question,
        # then compare the consumer through G6, not the candidate's edit span.
        proof = validate_targeted(consumer_root, spec, trigger)
        from .audio_question_identity import same_audio_question
        same, question_deltas = same_audio_question(proof['check'], check_request,
            compare_proposed=False, strict_source=True)
        require(same, 'ARGON_TARGETED_CURRENT_CONTEXT_DRIFT')
        acquired_check = proof['check']
        observations.append({"receipt_kind": "targeted_relisten_prior",
            "raw_sha256": trigger["evidence_sha256"],
            "projection": proof["projection"], "prior_cpa_decision": proof["recovery"]["gate"],
            'question_deltas': question_deltas,
            "assisted_is_dependent_evidence": True})
    acquired_offset = acquired_check['source_media_timeline_offset_ms']
    expected_context = [max(0, acquired_check['context_start_ms'] + acquired_offset),
        min(result['binding']['source_duration_ms'], acquired_check['context_end_ms'] + acquired_offset)]
    require(list(spec.context_source_ms) == expected_context, 'ARGON_CONSUMER_CONTEXT_DRIFT')
    return {**result, "ordinary_observations": observations}


def material_identity(check_request: Mapping, envelope: Mapping) -> dict:
    result = validate_consumer_evidence(check_request, envelope)
    # Response-byte or report-path changes cannot create another acquisition.
    # Native response/result hashes are still fully verified above.
    return argon_consumer_contract(spec_from_envelope(envelope), result).material_identity


def semantic_projection(value, check_request: Mapping):
    """Canonical CPA meaning identity for relocated wrappers of one acquisition."""
    if isinstance(value, Mapping):
        if value.get("schema_version") == EVIDENCE_SCHEMA:
            return {
                "schema_version": EVIDENCE_SCHEMA,
                "acquisition": material_identity(check_request, value),
            }
        return {
            key: semantic_projection(item, check_request)
            for key, item in value.items()
            if not (key == "response_sha256" and value.get("argon_escalation_evidence"))
        }
    if isinstance(value, (list, tuple)):
        return [semantic_projection(item, check_request) for item in value]
    return value


def transcript_witness(check_request: Mapping, envelope: Mapping) -> dict:
    from .acoustic_witness_adjudication import build_witness_request

    result = validate_consumer_evidence(check_request, envelope)
    b, hearing = result["binding"], result["hearing"]
    offset = check_request["source_media_timeline_offset_ms"]
    return {
        "schema_version": "subtitle-span-acoustic-witness.v1",
        "witness_protocol": "candidate_blind_transcript",
        "status": "OBSERVED",
        "candidate_exposure": "none",
        "exact_transcript": hearing["transcript"],
        "target_audible": True,
        "provider": result.get("provider", "gemini_api"),
        "model": result.get("selected_model_label") if result["mode"] == "agy" else result["model_version"],
        "request_sha256": build_witness_request(check_request)["request_sha256"],
        "source_media_sha256": b["source_media_sha256"],
        "audio_clip_sha256": b["input_audio_sha256"],
        "response_sha256": result["response_sha256"],
        "audio_start_ms": b["context_source_ms"][0],
        "audio_end_ms": b["context_source_ms"][1],
        "timeline_binding": {
            "schema_version": "subtitle-audio-timeline-binding.v1",
            "source_media_timeline_offset_ms": offset,
            "delivery_local": {
                "target_start_ms": check_request["matched_start_ms"],
                "target_end_ms": check_request["matched_end_ms"],
            },
            "source_media": {
                "target_start_ms": b["target_source_ms"][0],
                "target_end_ms": b["target_source_ms"][1],
                "crop_start_ms": b["context_source_ms"][0],
                "crop_end_ms": b["context_source_ms"][1],
            },
        },
        "argon_escalation_evidence": dict(envelope),
        "mutation_authorized": False,
    }


def valid_argon_witness(check_request: Mapping, witness: Mapping) -> bool:
    """The existing persistent witness binding can retain a native Argon row.

    This verifies observation bytes only; the caller still verifies its own
    current/proposed CPA decision and never promotes the observation to truth.
    """
    try:
        envelope = check_request["argon_escalation_evidence"]
        require(
            witness.get("argon_escalation_evidence") == envelope, "ARGON_WITNESS_ENVELOPE_DRIFT"
        )
        expected = transcript_witness(check_request, envelope)
        require(canonical(witness) == canonical(expected), "ARGON_WITNESS_PROJECTION_DRIFT")
        return True
    except (KeyError, TypeError, ValueError, OSError):
        return False


def argon_adjudication_marked(adjudication: Mapping) -> bool:
    return (
        any(
            isinstance(adjudication.get(key), Mapping)
            and adjudication[key].get("argon_escalation_evidence") is not None
            for key in ("request", "verdict")
        )
        or adjudication.get("argon_escalation") is not None
    )


def valid_argon_adjudication(adjudication: Mapping) -> bool:
    """A span resolution keeps raw needs_audio; it is not an old blanket PASS."""
    from .span_phoneme_resolution import marked, valid_adjudication
    if marked(adjudication):
        return valid_adjudication(adjudication)
    from .exact_final_witness_authority import binding_from_adjudication
    return binding_from_adjudication(adjudication) is not None


def bind_evidence(request: Mapping, envelope: Mapping) -> dict:
    output = copy.deepcopy(dict(request))
    output["argon_escalation_evidence"] = dict(envelope)
    output.pop("request_sha256", None)
    output["request_sha256"] = sha(canonical(output))
    result = validate_consumer_evidence(output, envelope)
    # Do not flatten the original hearing into a false pinyin source.
    source = {
        "kind": "argon_target_transcript",
        "argon_escalation_evidence": dict(envelope),
        "mutation_authorized": False,
    }
    candidates = output.setdefault("proposed_candidates", [])
    if not any(
        row.get("source", {}).get("kind") == "argon_target_transcript" for row in candidates
    ):
        candidates.append({"text": result["hearing"]["transcript"], "source": source})
    output.pop("request_sha256", None)
    output["request_sha256"] = sha(canonical(output))
    return output


def read_ordinary_history(root: Path, spec: AudioSpec):
    rows = []
    for path in sorted((root / "ordinary-history").glob("*.json")):
        event = store.get_json(path)
        value = dict(event)
        event_sha = value.pop("event_sha256")
        require(sha(canonical(value)) == event_sha, "ARGON_ORDINARY_HISTORY_DRIFT")
        require(event["finding"] == spec.finding.binding(), "ARGON_ORDINARY_OTHER_FINDING")
        bundle = event["ordinary"]
        raw = {k: store.get_blob(root, v) for k, v in bundle["artifacts"].items()}
        manifest = strict_json(raw["manifest"])
        contract = native_witness_artifact_contract(bundle["witness_request"], manifest)
        require(
            manifest["source_media_sha256"] == spec.finding.source_media_sha256
            and contract.bytes_match(raw),
            "ARGON_ORDINARY_HISTORY_BYTES",
        )
        wr = dict(bundle["witness_request"])
        wr_sha = wr.pop("request_sha256")
        require(sha(canonical(wr)) == wr_sha, "ARGON_ORDINARY_HISTORY_REQUEST")
        replay = bundle.get("cache_replay")
        if replay is None:
            require(
                contract.request_matches(),
                "ARGON_ORDINARY_HISTORY_REQUEST",
            )
        else:
            require(
                replay.get("schema_version") == "witness-metadata-cache-replay.v1"
                and replay.get("status") == "PASS"
                and replay.get("provider_call_count") == 0
                and replay["replayed_manifest_request_sha256"] == manifest["request_sha256"]
                and replay["current_witness_request_sha256"] == wr_sha
                and all(
                    replay.get(k) == manifest.get(k)
                    for k in (
                        "source_media_sha256",
                        "audio_clip_sha256",
                        "prompt_sha256",
                        "response_sha256",
                        "provider",
                        "model",
                    )
                )
                and replay["source_media_geometry"] == manifest["timeline_binding"]["source_media"],
                "ARGON_ORDINARY_HISTORY_CACHE_REPLAY",
            )
        offset = wr["source_media_timeline_offset_ms"]
        require(
            (wr["matched_start_ms"] + offset, wr["matched_end_ms"] + offset)
            == tuple(spec.finding.target_source_ms),
            "ARGON_ORDINARY_HISTORY_TARGET",
        )
        require(
            contract.physical_target == list(spec.finding.target_source_ms),
            "ARGON_ORDINARY_HISTORY_TARGET",
        )
        require(
            ordinary_38_model(manifest["provider"], manifest["model"])
            and manifest["provider"] in {"agy", "gemini_api"},
            "ARGON_ORDINARY_HISTORY_MODEL",
        )
        native = strict_json(raw["response"])
        require(native["status"] == bundle["raw_status"], "ARGON_ORDINARY_HISTORY_STATUS")
        from .acoustic_witness_protocol import _witness_report_error

        if native["status"] == "UNCERTAIN":
            require(
                contract.uncertain_valid(native, manifest["verdict"]),
                "ARGON_ORDINARY_HISTORY_STATUS",
            )
        else:
            require(
                _witness_report_error(bundle["witness_request"], native) is None,
                "ARGON_ORDINARY_HISTORY_STATUS",
            )
        decision = event.get("judge_record")
        if decision is not None:
            decision = strict_json(store.get_blob(root, decision))
            require(
                decision["status"] == "COMPLETED"
                and sha(decision["completion"].encode()) == decision["completion_sha256"]
                and sha(canonical(decision["request"])) == decision["request_sha256"],
                "ARGON_ORDINARY_HISTORY_JUDGE_INVALID",
            )
            require(
                event.get("judge_after_audio")
                is ordinary_history_event_contract(spec, bundle, decision)["judge_after_audio"],
                "ARGON_ORDINARY_JUDGE_PHASE_DRIFT",
            )
            decision = strict_json(decision["completion"])
        # A text-first NEITHER predates listening. It may diagnose chat/lyrics,
        # but only the actual post-audio judge is an unresolved ordinary-chain
        # attempt. Replaying that same request is not a new attempt.
        after_audio = event.get("judge_after_audio") is True
        status = (
            "NEITHER"
            if after_audio and decision and decision.get("choice") == "NEITHER"
            else bundle["raw_status"]
        )
        diagnosis = decision.get("argon_escalation") if decision else None
        flags = {k: False for k in ("reading_chat", "lyrics_reference_conflict")}
        if (
            isinstance(diagnosis, dict)
            and isinstance(diagnosis.get("reason"), str)
            and diagnosis["reason"].strip()
        ):
            for k in flags:
                flags[k] = diagnosis.get(k) is True
        rows.append((bundle["artifacts"]["response"], status, flags, event))
    return rows


def history_recommendation(root: Path, spec: AudioSpec, rows=None) -> dict:
    rows = read_ordinary_history(root, spec) if rows is None else rows
    # One hearing followed by its judge is one chain attempt, not two votes.
    # Distinct genuine post-audio NEITHER judgments can establish repeated
    # failure even when they legitimately reuse the same ordinary audio.
    judged_neither = {
        receipt
        for receipt, status, _, event in rows
        if status == "NEITHER" and event.get("judge_after_audio")
    }
    unique = {}
    for receipt, status, flags, event in rows:
        if status == "NEITHER" and event.get("judge_after_audio"):
            record = strict_json(store.get_blob(root, event["judge_record"]))
            attempt = sha(
                canonical(
                    {
                        "request_sha256": record["request_sha256"],
                        "completion_sha256": record["completion_sha256"],
                    }
                )
            )
        else:
            if receipt in judged_neither:
                continue
            attempt = receipt
        unique.pop(attempt, None)
        unique[attempt] = (status, flags)
    suffix = []
    flags = {"reading_chat": False, "lyrics_reference_conflict": False}
    for attempt, (status, flags) in unique.items():
        if status not in {"UNCERTAIN", "NEITHER"}:
            suffix = []
        else:
            suffix.append(OrdinaryAttempt(spec.finding, attempt, status, attempt))
    return recommend_escalation(spec.finding, suffix, **flags)


def validate_trigger(root: Path, spec: AudioSpec, history=None) -> dict:
    trigger = store.get_json(root / "trigger.json")
    if trigger.get("schema_version") == "argon-targeted-relisten-trigger.v1":
        from .argon_targeted_relisten import validate_trigger as validate_targeted
        validate_targeted(root, spec, trigger)
        return trigger
    expected = history_recommendation(root, spec, history)
    version = trigger.get("schema_version")
    require(version in {None, _DIAGNOSED_TRIGGER_SCHEMA}, "ARGON_TRIGGER_SCHEMA_INVALID")
    expected_trigger = argon_trigger_contract(spec, expected, version=version)
    if version is not None:
        require(bool(set(expected["reasons"]) & _DIAGNOSED_TRIGGER_REASONS),
                "ARGON_DIAGNOSED_TRIGGER_REQUIRED")
    require(
        expected["recommend_upgrade"]
        and canonical(trigger) == canonical(expected_trigger),
        "ARGON_TRIGGER_HISTORY_INVALID",
    )
    return trigger


class ArgonIntegration:
    def __init__(
        self,
        *,
        source_media: Path,
        source_sha256: str,
        source_duration_ms: int,
        ordinary_output_dir: Path,
        runtime: ArgonRuntime,
        ordinary_verifier=None,
    ):
        self.source = Path(source_media).absolute()
        self.source_sha = source_sha256
        self.duration = source_duration_ms
        self.ordinary_root = Path(ordinary_output_dir).absolute()
        self.runtime = runtime
        self.ordinary_verifier = ordinary_verifier

    def session(self, request: Mapping, finding: Mapping) -> "FindingSession":
        _check_request(request)
        require(file_sha(self.source) == self.source_sha, "ARGON_RUNTIME_SOURCE_DRIFT")
        offset = request["source_media_timeline_offset_ms"]
        target, finding_id = argon_session_binding_contract(self.source_sha, request, finding)
        context = (
            max(0, request["context_start_ms"] + offset),
            min(self.duration, request["context_end_ms"] + offset),
        )
        # Candidate/why text does not reset this issue. Distinct suspect slots do
        # not pool failures even when they occur inside the same cue.
        spec = AudioSpec(self.source, Finding(self.source_sha, target, finding_id), context)
        root = self.runtime.base / "cache" / "argon-consumer" / finding_id
        # A changed candidate span/class or neighboring cue grid is not a new
        # hearing. Locate a retained targeted task by its own saved G6 question,
        # including pending/UNKNOWN tasks; never create another dispatch merely
        # because the current candidate produces a different namespace key.
        from .audio_question_identity import same_audio_question
        candidates = [root, *(p.parent for p in sorted(root.parent.glob('*/source-binding.json'))
            if p.parent != root)]
        for retained in candidates:
            binding_path, trigger_path = retained / 'source-binding.json', retained / 'trigger.json'
            if not binding_path.is_file() or not trigger_path.is_file():
                continue
            binding = store.get_json(binding_path)
            if (binding.get('source_media_sha256') != self.source_sha
                    or binding.get('target_source_ms') != list(target)):
                continue
            trigger = store.get_json(trigger_path)
            if trigger.get('schema_version') != 'argon-targeted-relisten-trigger.v1':
                continue
            proof = strict_json(store.get_blob(retained, trigger['evidence_sha256']))
            if same_audio_question(proof['check'], request, compare_proposed=False, strict_source=True)[0]:
                spec = AudioSpec(self.source, Finding(self.source_sha, target, binding['finding_id']),
                    tuple(binding['context_source_ms']))
                validate_trigger(retained, spec)
                root = retained
                break
        return FindingSession(self, root, spec)


class FindingSession:
    def __init__(self, owner: ArgonIntegration, root: Path, spec: AudioSpec):
        self.owner, self.root, self.spec = owner, root, spec
        self.advanced = False
        self.audit = None
        self.envelope = None
        store.directory(root)
        contract = argon_source_binding_contract(spec)
        binding = contract.payload
        path = root / "source-binding.json"
        try:
            store.put_json(path, binding)
        except ContractError as exc:
            if exc.reason != "IMMUTABLE_ARTIFACT_CONFLICT":
                raise
            # 41:112: one physical source is identified by its bytes (checked
            # by ArgonIntegration.session) and this finding's source window;
            # the media path is only where a run reads it. A later output
            # root with the same identity reuses the original record; any
            # other difference is still a conflict.
            existing = store.get_json(path)
            require(
                contract.matches(existing),
                "IMMUTABLE_ARTIFACT_CONFLICT",
            )

    def mark_request(self, request: Mapping) -> dict:
        output = dict(request)
        output.pop("span_termination_upgrade", None)
        output.update(
            **argon_consumer_contract(self.spec).request_binding,
            argon_upgrade_available=True,
        )
        output.pop("request_sha256", None)
        output["request_sha256"] = sha(canonical(output))
        return bind_evidence(output, self.envelope) if self.envelope else output

    def pending_audit(self, request: Mapping, prior=None) -> dict:
        state = (self.audit or {}).get('status')
        reason_code = {'UNCERTAIN': 'ARGON_HEARD_UNCERTAIN',
            'NO_SPEECH': 'ARGON_HEARD_NO_SPEECH'}.get(state,
                'ARGON_ESCALATION_PENDING_OR_UNRESOLVED')
        questions = None
        if isinstance(prior, Mapping):
            from .span_phoneme_resolution import outside_question_packet
            try:
                questions = outside_question_packet(request, prior.get("verdict") or {},
                    (prior.get("witness_judge") or {}).get("judge") or {})
            except (ValueError, TypeError, KeyError, AttributeError):
                pass  # Original unresolved finding and raw prior remain intact.
        return {
            **({"span_pending_questions": questions} if questions is not None else {}),
            "schema_version": "subtitle-span-adjudication.v1",
            "status": "UNCERTAIN",
            "repaired": False,
            "timing_immutable": True,
            "request": dict(request),
            "reason_code": reason_code,
            "policy_branch": reason_code,
            'argon_uncertainty_evidence': {
                'status': state, 'reason': (self.audit or {}).get('reason'),
                'hearing': copy.deepcopy((self.audit or {}).get('hearing')),
                'can_sustain_question_alone': False,
            } if state in {'UNCERTAIN', 'NO_SPEECH'} else None,
            "argon_escalation": self.audit,
            "prior_audio_adjudication": prior,
            "mutation_authority": {
                "schema_version": "subtitle-correction-mutation-authority.v1",
                "status": "NOT_APPLIED",
                "basis": "ARGON_NOT_A_DECISION",
            },
        }

    def infrastructure_blocked_audit(self, request: Mapping, prior=None) -> dict:
        """Argon ended on a credential/config/request failure: not exhausted hearing."""
        audit = self.pending_audit(request, prior)
        audit.update(reason_code="ARGON_INFRASTRUCTURE_BLOCKED", policy_branch="ARGON_INFRASTRUCTURE_BLOCKED",
                     infrastructure_reason=(self.audit or {}).get("reason"),
                     operator_action_required=True)
        return audit

    def _ordinary(self, request: Mapping, verdict: Mapping) -> dict | None:
        from .acoustic_witness_adjudication import build_witness_request
        from .acoustic_witness_protocol import _witness_report_error

        from .acoustic_witness_adjudication import TEXT_FIRST_REASON

        if verdict.get("reason_code") == TEXT_FIRST_REASON:
            return None
        wr = build_witness_request(request)
        path = (
            self.owner.ordinary_root
            / "entity_verdicts"
            / wr["request_sha256"][:20]
            / "verdict.manifest.json"
        )
        cache_replay = verdict.get("witness_cache_replay")
        if cache_replay is not None:
            probe = getattr(self.owner.ordinary_verifier, "probe_witness_cache", None)
            require(callable(probe), "ARGON_ORDINARY_CACHE_PROBE_MISSING")
            replayed = probe(wr)
            require(
                isinstance(replayed, Mapping) and canonical(replayed) == canonical(verdict),
                "ARGON_ORDINARY_CACHE_REPLAY_DRIFT",
            )
            path = Path(replayed["witness_cache_replay"]["replayed_manifest_path"])
        if not path.exists():
            return None  # Bare UNCERTAIN/configuration failure is not a hearing.
        manifest_raw = path.read_bytes()
        manifest = strict_json(manifest_raw)
        contract = native_witness_artifact_contract(wr, manifest)
        require(
            (
                cache_replay is not None
                or (
                    contract.request_matches()
                )
            )
            and manifest.get("source_media_sha256") == self.owner.source_sha,
            "ARGON_ORDINARY_MANIFEST_DRIFT",
        )
        # The source is identified by its bytes (manifest sha above, current
        # source bytes in session()); a manifest written under another output
        # root names that root's path for the same media, not another source.
        # Default provider/model selection remains unchanged. Only a genuinely
        # completed ordinary 3.8 hearing qualifies as this upgrade's antecedent.
        model = manifest.get("model")
        if (
            not isinstance(model, str)
            or not ordinary_38_model(manifest.get("provider"), model)
            or manifest.get("provider") not in {"agy", "gemini_api"}
        ):
            return None
        original = dict(manifest["verdict"])
        supplied = {
            k: v for k, v in verdict.items() if k not in {"served_from_cache", "witness_protocol"}
        }
        require(
            cache_replay is not None or all(original.get(k) == v for k, v in supplied.items()),
            "ARGON_ORDINARY_VERDICT_DRIFT",
        )
        artifacts = {"manifest": manifest_raw}
        # The ordinary cache probe has verified the physical input.mp4 beside
        # the returned manifest. An immutable relocated manifest must retain
        # its original path, not be rewritten to pretend it was acquired here.
        audio = path.parent / "input.mp4" if cache_replay is not None else Path(manifest["audio_clip"])
        require(audio.parent == path.parent and not audio.is_symlink(), "ARGON_ORDINARY_AUDIO_PATH")
        artifacts["audio"] = audio.read_bytes()
        require(
            sha(artifacts["audio"]) == manifest["audio_clip_sha256"], "ARGON_ORDINARY_AUDIO_DRIFT"
        )
        # A witness acoustic-cache hit (41: reuse before any new listen) keeps
        # the same bytes as byte-identical copies (prompt.md plus
        # prompt.acoustic-cache.json, response.acoustic-cache.json). Identity is
        # the manifest SHA; identical copies are one artifact, not ambiguity.
        acoustic_hit = (manifest.get("acoustic_cache") or {}).get("hit") is True
        for name, patterns in (("prompt", ("prompt*",)),
                               ("response", ("verdict.raw*",) + (("response.acoustic-cache*",) if acoustic_hit else ()))):
            matches = sorted({
                p
                for pattern in patterns
                for p in path.parent.glob(pattern)
                if p.is_file() and not p.is_symlink() and file_sha(p) == manifest[name + "_sha256"]
            })
            require(bool(matches) and len({p.read_bytes() for p in matches}) == 1,
                    "ARGON_ORDINARY_" + name.upper() + "_DRIFT")
            artifacts[name] = matches[0].read_bytes()
        # Source and physical geometry are checked independently of model text.
        # 41:112: the same physical target is compared on the physical source
        # clock. A local/padded/delivery offset is only each record's own
        # representation, so the manifest must project onto itself and onto
        # this finding's source target (spec target = request local + offset).
        projected = contract.projected_target
        require(
            projected is not None
            and projected
            == contract.physical_target
            == list(self.spec.finding.target_source_ms),
            "ARGON_ORDINARY_CLOCK_DRIFT",
        )
        raw = strict_json(artifacts["response"])
        if raw.get("status") == "UNCERTAIN":
            require(
                contract.uncertain_valid(raw, original),
                "ARGON_ORDINARY_UNCERTAIN_INVALID",
            )
        elif _witness_report_error(wr, raw) is not None:
            return None  # Invalid/incomplete schema is not an unclear hearing.
        require(
            not any(k in raw for k in ("current_cue", "proposed_cue", "candidate_entities")),
            "ARGON_ORDINARY_NOT_BLIND",
        )
        return {
            "witness_request": wr,
            "cache_replay": dict(cache_replay) if cache_replay is not None else None,
            "raw_status": raw["status"],
            "artifacts": {name: store.blob(self.root, value) for name, value in artifacts.items()},
        }

    def _judge_raw(self, request: Mapping, judge: Mapping | None, recorder) -> dict | None:
        if not isinstance(judge, Mapping) or recorder is None or judge.get("status") != "JUDGED":
            return None
        if judge.get("check_request_sha256") != request["request_sha256"]:
            return None
        for row in recorder._entries():
            if (
                row["status"] == "COMPLETED"
                and row.get("completion_sha256") == judge.get("completion_sha256")
                and sha(row["request"]["prompt"].encode()) == judge.get("prompt_sha256")
            ):
                require(
                    row["request"]["material"]["srt_sha256"]
                    == sha(request["whole_clip_current_srt"].encode()),
                    "ARGON_ORDINARY_JUDGE_SOURCE_DRIFT",
                )
                payload = strict_json(row["completion"])
                require(payload.get("choice") == judge.get("choice"), "ARGON_ORDINARY_JUDGE_DRIFT")
                return row
        return None

    def _history(self):
        return read_ordinary_history(self.root, self.spec)

    def recommendation(self) -> dict:
        return history_recommendation(self.root, self.spec)

    def consider(self, request, verdict, judge, recorder) -> bool:
        """Returns True only when ordinary evidence actually triggered this task."""
        if self.envelope is not None or self.advanced:
            return self.audit is not None
        with store.locked(self.root):
            ordinary = self._ordinary(request, verdict)
            if ordinary is not None:
                raw_judge = self._judge_raw(request, judge, recorder)
                event = ordinary_history_event_contract(self.spec, ordinary, raw_judge)
                event["judge_record"] = store.blob(self.root, canonical(raw_judge)) if raw_judge else None
                digest = sha(canonical(event))
                duplicate = any(row[3]["event_sha256"] == digest for row in self._history())
                if not duplicate:
                    store.put_json(
                        self.root / "ordinary-history" / f"{time.time_ns()}-{digest}.json",
                        {**event, "event_sha256": digest},
                    )
            recommendation = self.recommendation()
            # New repeated-unresolved admission comes from actual relisten
            # projection, not two text judges reusing the same old hearing.
            if not (set(recommendation["reasons"]) & _DIAGNOSED_TRIGGER_REASONS):
                return False
            store.put_json(
                self.root / "trigger.json",
                argon_trigger_contract(self.spec, recommendation),
            )
        self.advance()
        return True

    def consider_completed_chain(self, request: Mapping, judge: Mapping, recorder) -> bool:
        """Record the actual later CPA decision after new independent evidence.

        The original 3.8 receipt is independently rebound by its ordinary cache
        validator; another provider's transcript is not relabelled as 3.8.
        Repeated calls with the same judge are deduplicated by the old contract.
        """
        from .acoustic_witness_adjudication import build_witness_request
        if self.envelope is not None or self.advanced:
            return False
        probe = getattr(self.owner.ordinary_verifier, "probe_witness_cache", None)
        original = probe(build_witness_request(request)) if callable(probe) else None
        if not isinstance(original, Mapping):
            return False
        return self.consider(request, original, judge, recorder)

    def consider_targeted_relisten(self, *, check, original_request, prior, recovery, projection) -> bool:
        if self.envelope is not None or self.advanced:
            return self.audit is not None
        if projection["effective_distinct_observations"] < 3:
            return False
        from .argon_targeted_relisten import make_trigger
        with store.locked(self.root):
            if (self.root / "trigger.json").exists():
                validate_trigger(self.root, self.spec)
            else:
                trigger = make_trigger(self.root, self.spec, check=check,
                    original_request=original_request, prior=prior, recovery=recovery,
                    projection=projection)
                store.put_json(self.root / "trigger.json", trigger)
        self.advance()
        return True

    def resume(self) -> bool:
        if not (self.root / "trigger.json").exists():
            return False
        with store.locked(self.root):
            validate_trigger(self.root, self.spec)
        self.advance()
        return True

    def advance(self):
        if self.advanced:
            return
        self.advanced = True
        outcome = self.owner.runtime.advance(root=self.root / "batch", spec=self.spec)
        result = outcome["result"]
        self.audit = {
            "engine_root": outcome["engine_root"],
            "task_id": result["task_id"],
            "status": result["status"],
            "job_name": result["job_name"],
            "reason": result["reason"],
            "response_sha256": result["response_sha256"],
            "accounting": result["accounting"],
            "usage_receipt": result["usage_receipt"],
            "prior_key_attempts": outcome.get("prior_key_attempts", []),
            "dispatches_this_advance": outcome.get("dispatches_this_advance", 0),
            "new_usable_observations_this_advance": outcome.get("new_usable_observations_this_advance", 0),
            'advance_operation': outcome.get('advance_operation'),
            'provider_slot_wait_s': outcome.get('provider_slot_wait_s'),
            "hearing": result.get("hearing"),
            # The runtime's shared refusal classification of the last route
            # (present only for a definitive zero-inference refusal).
            "route_refusal": result.get("route_refusal"),
            "mutation_authorized": False,
        }
        store.blob(self.root, canonical(self.audit))
        if result["status"] == "OBSERVED":
            self.envelope = argon_consumer_contract(self.spec).envelope(self.root, outcome)


def build_argon_integration(
    *,
    config,
    source_media,
    source_duration_ms,
    ordinary_output_dir,
    ordinary_verifier=None,
    runtime_root=None,
    credential_loader=None,
    transport_factory=None,
):
    config = argon_config_contract(config)
    if config is None:
        return None
    root = runtime_root or os.environ.get("AUTOSLICE_BASE")
    require(root is not None, "ARGON_RUNTIME_REQUIRED")
    options = {}
    if credential_loader is not None:
        options["credential_loader"] = credential_loader
    if transport_factory is not None:
        options["transport_factory"] = transport_factory
    return ArgonIntegration(
        source_media=source_media,
        source_sha256=file_sha(Path(source_media)),
        source_duration_ms=source_duration_ms,
        ordinary_output_dir=ordinary_output_dir,
        ordinary_verifier=ordinary_verifier,
        runtime=ArgonRuntime(runtime_root=Path(root), config=config, **options),
    )
