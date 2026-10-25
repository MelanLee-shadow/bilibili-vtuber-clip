"""Final-surface ownership for late CPA source-language repairs."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Any, Mapping

from src.autoslice.jingting_chunker import parse_srt_cues


def _text_sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _receipt_sha(receipt: Mapping[str, Any]) -> str:
    return "sha256:" + _text_sha(json.dumps(
        dict(receipt), ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ))


def _applied_receipts(audit: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    rows = audit.get("cpa_adjudication_rows")
    count = audit.get("applied_count")
    if (not isinstance(rows, list) or type(count) is not int or count <= 0
            or any(not isinstance(row, Mapping) for row in rows)):
        raise ValueError("FINAL_CPA_REPLAY_APPLIED_ROWS_INVALID")
    applied = [row for row in rows if row.get("choice") == "PROPOSED"]
    if len(applied) != count or any(row.get("resolved") is not True for row in applied):
        raise ValueError("FINAL_CPA_REPLAY_APPLIED_COUNT_MISMATCH")
    indexes = [row.get("cue_index") for row in applied]
    if any(type(index) is not int or index <= 0 for index in indexes) or len(set(indexes)) != len(indexes):
        raise ValueError("FINAL_CPA_REPLAY_CUE_BINDING_INVALID")
    return applied


def _owner_audit(
    authority: Mapping[str, Any], surface: str, receipt_sha: str,
) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    """Locate one original audit, never an owner-shaped substitute or a cache."""
    candidates: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
    direct = authority.get(
        "final_source_language_preservation_audit" if surface == "source_language"
        else "foreign_script_consistency_audit"
    )
    if isinstance(direct, Mapping):
        candidates.append((direct, direct))
    discovery = authority.get("audio_language_discovery_audit")
    if surface == "foreign_script" and isinstance(discovery, Mapping):
        from src.autoslice.audio_language_discovery import SCHEMA, _audit_sha
        if (discovery.get("schema_version") != SCHEMA
                or discovery.get("discovery_sha256") != _audit_sha(discovery)
                or discovery.get("time_domain") != "padded_local"
                or not isinstance(discovery.get("adjudications"), list)):
            raise ValueError("FINAL_CPA_REPLAY_DISCOVERY_BINDING_INVALID")
        retained = authority.get("retained_source_input")
        if (isinstance(retained, Mapping) and retained.get("padded_sha256") is not None
                and discovery.get("source_media_sha256") != retained["padded_sha256"]):
            raise ValueError("FINAL_CPA_REPLAY_SOURCE_MEDIA_DRIFT")
        total = 0
        for audit in discovery["adjudications"]:
            if not isinstance(audit, Mapping):
                raise ValueError("FINAL_CPA_REPLAY_DISCOVERY_BINDING_INVALID")
            count = audit.get("applied_count")
            if count is not None and (type(count) is not int or count < 0):
                raise ValueError("FINAL_CPA_REPLAY_APPLIED_COUNT_MISMATCH")
            if count:
                total += len(_applied_receipts(audit))
            candidates.append((audit, audit))
        if type(discovery.get("applied_count")) is not int or total != discovery["applied_count"]:
            raise ValueError("FINAL_CPA_REPLAY_APPLIED_COUNT_MISMATCH")
    matches = [(audit, container) for audit, container in candidates
               if isinstance(audit.get("cpa_adjudication_rows"), list)
               for row in audit["cpa_adjudication_rows"]
               if isinstance(row, Mapping) and _receipt_sha(row) == receipt_sha]
    if len(matches) != 1:
        raise ValueError("FINAL_CPA_REPLAY_ORIGINAL_AUDIT_NOT_UNIQUE")
    audit, container = matches[0]
    if (surface == "foreign_script" and isinstance(discovery, Mapping)
            and audit is not direct and not isinstance(audit.get("output_srt"), str)
            and audit.get("applied_count")):
        # The discovery writer saves its complete output once plus every
        # stage's output digest. An applied stage's own complete input/output
        # is the authenticated generation of that one history, never a guess.
        container = next(generation for stage, generation in discovery_generations(discovery)
                         if stage is audit)
    return audit, container


def _unapply(output: str, receipts: list[Mapping[str, Any]]) -> str:
    """Render one stage input from its output in the foreign writer's own format."""
    from src.autoslice.foreign_span_witness import _ms_to_srt_ts

    cues = parse_srt_cues(output)
    if not cues or any(str(cue.index) != str(index) for index, cue in enumerate(cues, 1)):
        raise ValueError("FINAL_CPA_REPLAY_OUTPUT_GRID_INVALID")
    replacements = {}
    for receipt in receipts:
        index = receipt["cue_index"]
        before, after = receipt.get("current"), receipt.get("proposed")
        if (index > len(cues) or not isinstance(before, str) or not before
                or not isinstance(after, str) or not after.strip() or before == after
                or cues[index - 1].text != after):
            raise ValueError("FINAL_CPA_REPLAY_OUTPUT_EFFECT_DRIFT")
        replacements[index] = before
    return "\n\n".join(
        f"{index}\n{_ms_to_srt_ts(cue.start_ms)} --> {_ms_to_srt_ts(cue.end_ms)}\n"
        f"{replacements.get(index, cue.text)}"
        for index, cue in enumerate(cues, 1)
    ) + "\n"


def discovery_generations(
    discovery: Mapping[str, Any],
) -> list[tuple[Mapping[str, Any], dict[str, str]]]:
    """Every applied discovery stage with its authenticated complete generation.

    ``repair_audio_language_candidates`` threads one SRT through its stages and
    saves the complete final output, the original input digest and each stage's
    output digest. Walking back from the saved output, un-applying each applied
    stage's receipts must reproduce the preceding stage digest exactly and end
    at the saved input digest; an unsaved, reordered or drifted generation
    fails. A single applied stage is the same rule with one generation.
    """
    output = discovery.get("output_srt")
    input_sha = discovery.get("input_srt_sha256")
    stages = discovery.get("adjudications")
    if (not isinstance(output, str) or not output
            or discovery.get("output_srt_sha256") != _text_sha(output)
            or not isinstance(input_sha, str) or re.fullmatch(r"[0-9a-f]{64}", input_sha) is None
            or not isinstance(stages, list)):
        raise ValueError("FINAL_CPA_REPLAY_TEXT_BINDING_INVALID")
    text, generations = output, []
    for stage in reversed(stages):
        if not isinstance(stage, Mapping):
            raise ValueError("FINAL_CPA_REPLAY_DISCOVERY_BINDING_INVALID")
        declared = stage.get("output_srt_sha256")
        if declared is not None and declared != _text_sha(text):
            raise ValueError("FINAL_CPA_REPLAY_GENERATION_DRIFT")
        if stage.get("applied_count"):
            before = _unapply(text, _applied_receipts(stage))
            generations.append((stage, {"input_srt_sha256": _text_sha(before),
                                        "output_srt": text, "output_srt_sha256": _text_sha(text)}))
            text = before
    if _text_sha(text) != input_sha:
        raise ValueError("FINAL_CPA_REPLAY_INPUT_HASH_MISMATCH")
    return list(reversed(generations))


def _replay_texts(
    audit: Mapping[str, Any], container: Mapping[str, Any],
) -> tuple[str, str, list[Mapping[str, Any]]]:
    output = container.get("output_srt")
    input_sha = container.get("input_srt_sha256")
    if (not isinstance(output, str) or not output
            or not isinstance(input_sha, str) or re.fullmatch(r"[0-9a-f]{64}", input_sha) is None
            or container.get("output_srt_sha256") != _text_sha(output)
            or audit.get("output_srt_sha256") != _text_sha(output)):
        raise ValueError("FINAL_CPA_REPLAY_TEXT_BINDING_INVALID")
    receipts = _applied_receipts(audit)
    # This is the normal foreign writer's rendering, not an approximate reverse
    # edit. The saved full-input digest must authenticate every reconstructed byte.
    original = _unapply(output, receipts)
    if _text_sha(original) != input_sha:
        raise ValueError("FINAL_CPA_REPLAY_INPUT_HASH_MISMATCH")
    if "input_srt" in container and container["input_srt"] != original:
        raise ValueError("FINAL_CPA_REPLAY_INPUT_TEXT_DRIFT")
    return original, output, receipts


def _validate_replay_witness(audit: Mapping[str, Any], receipt: Mapping[str, Any], output: str) -> None:
    from src.autoslice.foreign_span_witness import _transcript_witness
    from src.autoslice.acoustic_witness_adjudication import _transcript_candidates

    observations = audit.get("audio_witness_rows")
    matches = [item for item in observations if isinstance(item, dict)
               and item.get("cue_index") == receipt["cue_index"]] if isinstance(observations, list) else []
    if len(matches) != 1:
        raise ValueError("FINAL_CPA_REPLAY_WITNESS_NOT_UNIQUE")
    observation = matches[0]
    cue = parse_srt_cues(output)[receipt["cue_index"] - 1]
    if (observation.get("start_ms") != cue.start_ms or observation.get("end_ms") != cue.end_ms
            or observation.get("failure") or observation.get("witnessed") is not False
            or not isinstance(observation.get("exact_transcript"), str)
            or not observation["exact_transcript"].strip()):
        raise ValueError("FINAL_CPA_REPLAY_WITNESS_TARGET_DRIFT")
    adjudication = receipt.get("adjudication")
    judge = adjudication.get("judge") if isinstance(adjudication, Mapping) else None
    if not isinstance(adjudication, Mapping) or not isinstance(judge, Mapping):
        raise ValueError("FINAL_CPA_REPLAY_ADJUDICATION_MISSING")
    transcript = " ".join(observation["exact_transcript"].split())
    witness = _transcript_witness(observation, transcript=transcript)
    if (adjudication.get("schema_version") != "acoustic-witness-adjudication.v1"
            or adjudication.get("witness_protocol") != "candidate_blind_transcript"
            or adjudication.get("witness_status") != "OBSERVED"
            or adjudication.get("witness_request_sha256") != witness["request_sha256"]
            or judge.get("schema_version") != "acoustic-witness-adjudication.v1"
            or judge.get("needs_audio") is not False or judge.get("needs_audio_requested") is True
            or judge.get("decision_contract") != "current-proposed-candidate-id-neither.v1"):
        raise ValueError("FINAL_CPA_REPLAY_WITNESS_OR_JUDGE_DRIFT")
    evidence = adjudication.get("closed_set_structured_evidence")
    if (not isinstance(evidence, Mapping) or judge.get("closed_set_structured_evidence") != evidence
            or not isinstance(evidence.get("source_candidates"), list)
            or receipt.get("proposal_rebuild") is not None):
        raise ValueError("FINAL_CPA_REPLAY_CLOSED_SET_UNSUPPORTED")
    candidates = _transcript_candidates({
        "current_cue": receipt["current"], "proposed_cue": transcript,
        "proposed_candidates": evidence["source_candidates"],
        "candidate_provenance": {"kind": "bounded_candidate_blind_audio_transcript",
                                 "audio_sha256": observation.get("audio_sha256"),
                                 "audible_language": observation.get("audible_language")},
    })
    candidate_id = judge.get("candidate_id")
    selected = candidates.get(candidate_id) if isinstance(candidate_id, str) else None
    if (selected is None or selected["text"] != receipt["proposed"]
            or judge.get("selected_candidate_text") != selected["text"]
            or judge.get("selected_candidate_sources") != selected["sources"]
            or receipt.get("selected_candidate_sources") != selected["sources"]):
        raise ValueError("FINAL_CPA_REPLAY_SELECTED_CANDIDATE_DRIFT")
    # Completion bytes were not embedded by this historical writer. Preserve
    # its typed trust contract; do not pretend these digests were recomputed.
    if any(not isinstance(judge.get(key), str) or re.fullmatch(r"[0-9a-f]{64}", judge[key]) is None
           for key in ("prompt_sha256", "completion_sha256")):
        raise ValueError("FINAL_CPA_REPLAY_JUDGE_HASH_MISSING")


def validate_final_cpa_owner(
    row: Mapping[str, Any], *, row_index: int, authority: Mapping[str, Any], srt_text: str,
    delivery_source_window: tuple[int, int] | None = None,
) -> None:
    """Replay a saved normal foreign/source-language writer without mutation.

    The caller verifies checkpoint, source-media and clock bindings. This reader
    accepts only an original bounded audit with authenticated complete text and
    a reproducible writer effect, never a bare mode/status or a guessed cache.
    """
    modes = {f"final_{surface}_cpa_adjudication": surface
             for surface in ("source_language", "foreign_script")}
    mode = row.get("mode")
    surface = modes.get(mode) if isinstance(mode, str) else None
    rows = authority.get("entity_repairs")
    if (surface is None or not isinstance(rows, list) or type(row_index) is not int
            or not 0 <= row_index < len(rows) or rows[row_index] != row):
        raise ValueError("FINAL_CPA_REPLAY_OWNER_BINDING_INVALID")
    digest = row.get(f"final_{surface}_receipt_sha256")
    if not isinstance(digest, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None:
        raise ValueError("FINAL_CPA_REPLAY_RECEIPT_HASH_INVALID")
    audit, container = _owner_audit(authority, surface, digest)
    original, output, receipts = _replay_texts(audit, container)
    for receipt in receipts:
        _validate_replay_witness(audit, receipt, output)
    digests = [_receipt_sha(receipt) for receipt in receipts]
    offset = digests.index(digest)
    first, end = row_index - offset, row_index - offset + len(receipts)
    if first < 0 or end > len(rows):
        raise ValueError("FINAL_CPA_REPLAY_OWNER_SEQUENCE_INVALID")
    registration_key = f"final_{surface}_cpa_surface_registrations"
    registrations, applied = authority.get(registration_key), authority.get("applied", [])
    if (not isinstance(registrations, list) or not isinstance(applied, list)
            or any(not isinstance(item, Mapping) or type(item.get("owner_entity_repair_index")) is not int
                   for item in registrations)):
        raise ValueError("FINAL_CPA_REPLAY_REGISTRATIONS_INVALID")
    expected_registrations = [item for item in registrations if item["owner_entity_repair_index"] < end]
    replay = {
        "entity_repairs": copy.deepcopy(rows[:first]), "applied": copy.deepcopy(applied),
        registration_key: copy.deepcopy([item for item in registrations
                                        if item["owner_entity_repair_index"] < first]),
    }
    # Undo only this exact writer invocation's supersessions. The normal writer
    # must reproduce the complete original records, including those fields.
    for previous in [*replay["entity_repairs"], *replay["applied"]]:
        reconciliation = previous.get("reconciliation") if isinstance(previous, dict) else None
        if isinstance(reconciliation, Mapping) and reconciliation.get("receipt_sha256") in digests:
            previous.pop("reconciliation")
    _register_final_cpa_repairs(replay, input_srt=original, output_srt=output,
                                cpa_audit=audit, surface=surface)
    # Selection can mark an authentic source owner outside the final story.
    # Recheck that one normal boundary annotation against the caller's validated
    # physical delivery interval; no receipt, word, owner or registration moves.
    if delivery_source_window is not None:
        if (len(delivery_source_window) != 2
                or any(type(value) is not int for value in delivery_source_window)
                or not 0 <= delivery_source_window[0] < delivery_source_window[1]):
            raise ValueError("FINAL_CPA_REPLAY_DELIVERY_WINDOW_INVALID")
        for index in range(first, end):
            actual, expected = rows[index], replay["entity_repairs"][index]
            if (actual.get("boundary_owner_rejection") == "OUTSIDE_IMMUTABLE_STORY_SCOPE"
                    and expected.get("boundary_owner_rejection") == "POST_BOUNDARY_FREEZE_FINAL_SURFACE_OWNER"
                    and actual.get("boundary_required") is False
                    and (actual["matched_end_ms"] <= delivery_source_window[0]
                         or actual["matched_start_ms"] >= delivery_source_window[1])):
                expected["boundary_owner_rejection"] = actual["boundary_owner_rejection"]
    # A source-stage owner is a later input of the ordinary boundary freezer,
    # which marks it a required story owner. Replay that same freezer for this
    # row's ordinal with the persisted frozen story window, and accept only an
    # owner the hash-bound frozen contract actually lists; nothing else moves.
    for index in range(first, end):
        actual, expected = rows[index], replay["entity_repairs"][index]
        if actual.get("boundary_owner_id") is not None and actual != expected:
            expected.update(_frozen_story_owner_annotation(
                authority, replay["entity_repairs"][:index + 1], index=index))
    if (replay["entity_repairs"] != rows[:end] or replay["applied"] != applied
            or replay[registration_key] != expected_registrations):
        raise ValueError("FINAL_CPA_REPLAY_WRITER_EFFECT_DRIFT")
    # Historical numbers belong to the original audit. Only exact physical
    # geometry and the complete selected text can prove survival after merges.
    current = [cue for cue in parse_srt_cues(srt_text)
               if cue.start_ms == row.get("matched_start_ms") and cue.end_ms == row.get("matched_end_ms")]
    if len(current) != 1 or row.get("after") != [current[0].text]:
        raise ValueError("FINAL_CPA_REPLAY_CURRENT_SURFACE_DRIFT")


def _frozen_story_owner_annotation(
    authority: Mapping[str, Any], rows: list[Any], *, index: int,
) -> dict[str, Any]:
    """The boundary freezer's own annotation of ``rows[index]``, bound to its contract."""
    from src.autoslice.producer_boundary_owner_contract import (
        freeze_story_chat_boundary_owners,
        validate_frozen_boundary_owner_contract,
    )

    try:
        frozen = validate_frozen_boundary_owner_contract(authority.get("frozen_boundary_owner_contract"))
        start, end = frozen["story_start_ms"], frozen["story_end_ms"]
        probe = {"entity_repairs": copy.deepcopy(rows)}
        produced = freeze_story_chat_boundary_owners(probe, story_start_ms=start, story_end_ms=end)
    except (RuntimeError, ValueError, KeyError, TypeError) as exc:
        raise ValueError("FINAL_CPA_REPLAY_BOUNDARY_OWNER_CONTRACT_INVALID") from exc
    annotated = probe["entity_repairs"][index]
    owner_id = annotated.get("boundary_owner_id")
    owners = [owner for owner in produced if owner.get("owner_id") == owner_id]
    if (annotated.get("boundary_required") is not True or len(owners) != 1
            or not any(isinstance(row, Mapping) and row.get("owner_id") == owner_id
                       and row.get("local_windows") == owners[0]["local_windows"]
                       for row in frozen.get("owners") or [])):
        raise ValueError("FINAL_CPA_REPLAY_BOUNDARY_OWNER_UNREGISTERED")
    return {"boundary_required": True, "boundary_owner_id": owner_id}


def _register_final_cpa_repairs(
    chat_authority_audit: dict[str, Any],
    *,
    input_srt: str,
    output_srt: str,
    cpa_audit: Mapping[str, Any],
    surface: str,
) -> None:
    """Register a late CPA repair as the owner of its final cue surface."""

    if surface not in {"source_language", "foreign_script"}:
        raise ValueError("FINAL_CPA_SURFACE_INVALID")
    surface_slug = surface.replace("_", "-")
    surface_label = surface.upper()
    error_prefix = f"FINAL_{surface_label}"
    registration_key = f"final_{surface}_cpa_surface_registrations"
    registration_schema = f"final-{surface_slug}-cpa-surface-registration.v1"
    supersession_schema = f"final-{surface_slug}-cpa-supersession.v1"
    exact_read_supersession_schema = (
        f"final-{surface_slug}-cpa-exact-read-supersession.v1"
    )
    supersession_status = f"SUPERSEDED_BY_FINAL_{surface_label}_CPA"
    owner_mode = f"final_{surface}_cpa_adjudication"
    receipt_key = f"final_{surface}_receipt_sha256"
    mutation_basis = (
        "CPA_ACOUSTIC_PRONUNCIATION_DISAMBIGUATION"
        if surface == "source_language"
        else "CPA_FOREIGN_SCRIPT_RETRANSCRIPTION"
    )
    receipts = cpa_audit.get("cpa_adjudication_rows")
    if not isinstance(receipts, list):
        return
    declared_applied = cpa_audit.get("applied_count")
    if (
        isinstance(declared_applied, bool)
        or not isinstance(declared_applied, int)
        or declared_applied <= 0
    ):
        return
    output_sha256 = hashlib.sha256(output_srt.encode("utf-8")).hexdigest()
    if cpa_audit.get("output_srt_sha256") != output_sha256:
        raise ValueError(f"{error_prefix}_OUTPUT_HASH_MISMATCH")
    input_cues = {int(cue.index): cue for cue in parse_srt_cues(input_srt)}
    rows = chat_authority_audit.setdefault("entity_repairs", [])
    if not isinstance(rows, list):
        raise ValueError(f"{error_prefix}_ENTITY_REPAIR_LEDGER_INVALID")
    applied_rows = chat_authority_audit.get("applied", [])
    if not isinstance(applied_rows, list):
        raise ValueError(f"{error_prefix}_APPLIED_LEDGER_INVALID")
    registrations = chat_authority_audit.setdefault(
        registration_key, []
    )
    if not isinstance(registrations, list):
        raise ValueError(f"{error_prefix}_REGISTRATION_LEDGER_INVALID")

    applied = 0
    for receipt in receipts:
        if not isinstance(receipt, Mapping) or receipt.get("choice") != "PROPOSED":
            continue
        adjudication = receipt.get("adjudication")
        judge = adjudication.get("judge") if isinstance(adjudication, Mapping) else None
        cue_index = receipt.get("cue_index")
        current = receipt.get("current")
        proposed = receipt.get("proposed")
        if (
            receipt.get("resolved") is not True
            or receipt.get("decision_authority") != "CPA_JUDGE"
            or not isinstance(adjudication, Mapping)
            or adjudication.get("decision_authority") != "CPA_JUDGE"
            or adjudication.get("witness_authority") != "EVIDENCE_ONLY"
            or not isinstance(judge, Mapping)
            or judge.get("status") != "JUDGED"
            or judge.get("choice") != "PROPOSED"
            or isinstance(cue_index, bool)
            or not isinstance(cue_index, int)
            or not isinstance(current, str)
            or not current
            or not isinstance(proposed, str)
            or not proposed.strip()
            or cue_index not in input_cues
            or input_cues[cue_index].text != current
        ):
            raise ValueError(f"{error_prefix}_CPA_RECEIPT_INVALID")
        cue = input_cues[cue_index]
        receipt_sha256 = "sha256:" + hashlib.sha256(
            json.dumps(
                dict(receipt),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        superseded_indexes: list[int] = []
        for index, row in enumerate(rows):
            if not isinstance(row, dict) or row.get("reconciliation"):
                continue
            expected = str(row.get("structured_exact_text") or "")
            if not expected:
                after = row.get("after")
                if isinstance(after, list) and len(after) == 1:
                    expected = str(after[0] or "")
                elif isinstance(after, str):
                    expected = after
            if (
                row.get("matched_start_ms") == cue.start_ms
                and row.get("matched_end_ms") == cue.end_ms
                and expected == current
            ):
                row["reconciliation"] = {
                    "schema_version": supersession_schema,
                    "status": supersession_status,
                    "receipt_sha256": receipt_sha256,
                    "before_sha256": "sha256:"
                    + hashlib.sha256(current.encode("utf-8")).hexdigest(),
                    "after_sha256": "sha256:"
                    + hashlib.sha256(proposed.encode("utf-8")).hexdigest(),
                    "timing_immutable": True,
                }
                superseded_indexes.append(index)
        superseded_applied_indexes: list[int] = []
        for index, row in enumerate(applied_rows):
            if not isinstance(row, dict) or row.get("reconciliation"):
                continue
            expected = str(row.get("exact_text") or "")
            if (
                row.get("matched_start_ms") == cue.start_ms
                and row.get("matched_end_ms") == cue.end_ms
                and expected
                and expected in current
                and expected not in proposed
            ):
                # A chat exact-read may own only a substring of a cue. Retire
                # it only when this immutable cue geometry and the CPA
                # before->after receipt prove that the text disappeared.
                row["reconciliation"] = {
                    "schema_version": exact_read_supersession_schema,
                    "status": supersession_status,
                    "receipt_sha256": receipt_sha256,
                    "before_sha256": "sha256:"
                    + hashlib.sha256(current.encode("utf-8")).hexdigest(),
                    "after_sha256": "sha256:"
                    + hashlib.sha256(proposed.encode("utf-8")).hexdigest(),
                    "timing_immutable": True,
                }
                superseded_applied_indexes.append(index)
        owner = {
            "mode": owner_mode,
            "decision_authority": "CPA_JUDGE",
            "mutation_authority": {
                "schema_version": "subtitle-correction-mutation-authority.v1",
                "status": "PASS",
                "basis": mutation_basis,
            },
            "cue_indexes": [cue_index],
            "matched_start_ms": cue.start_ms,
            "matched_end_ms": cue.end_ms,
            "before": [current],
            "after": [proposed],
            "structured_exact_text": proposed,
            "survived": True,
            "timing_immutable": True,
            "boundary_required": False,
            "boundary_owner_rejection": "POST_BOUNDARY_FREEZE_FINAL_SURFACE_OWNER",
            receipt_key: receipt_sha256,
            "superseded_entity_repair_indexes": superseded_indexes,
            "superseded_applied_indexes": superseded_applied_indexes,
        }
        rows.append(owner)
        registrations.append(
            {
                "schema_version": registration_schema,
                "status": "REGISTERED",
                "receipt_sha256": receipt_sha256,
                "owner_entity_repair_index": len(rows) - 1,
                "superseded_entity_repair_indexes": superseded_indexes,
                "superseded_applied_indexes": superseded_applied_indexes,
            }
        )
        applied += 1
    if applied != declared_applied:
        raise ValueError(f"{error_prefix}_APPLIED_COUNT_MISMATCH")


def register_final_source_language_cpa_repairs(
    chat_authority_audit: dict[str, Any],
    *,
    input_srt: str,
    output_srt: str,
    source_language_audit: Mapping[str, Any],
) -> None:
    """Register late CPA language repairs and retire same-window predecessors."""

    _register_final_cpa_repairs(
        chat_authority_audit,
        input_srt=input_srt,
        output_srt=output_srt,
        cpa_audit=source_language_audit,
        surface="source_language",
    )


def register_final_foreign_script_cpa_repairs(
    chat_authority_audit: dict[str, Any],
    *,
    input_srt: str,
    output_srt: str,
    foreign_script_audit: Mapping[str, Any],
) -> None:
    """Register late CPA foreign-script repairs and retire predecessors."""

    _register_final_cpa_repairs(
        chat_authority_audit,
        input_srt=input_srt,
        output_srt=output_srt,
        cpa_audit=foreign_script_audit,
        surface="foreign_script",
    )
