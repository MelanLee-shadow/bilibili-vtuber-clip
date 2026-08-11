#!/usr/bin/env python3
"""Provenance-bound manual scorecard rescore for one repaired candidate.

This is a recovery wrapper around the production
``semantic_candidate_selector.rescore_candidate_scorecard`` primitive.  It
does not alter runner state, a candidate spec, or a source-fact receipt.  The
default CLI mode validates every byte/hash binding and prints a deterministic
preflight document without constructing or calling a provider.  A provider
call requires both ``--execute-provider-call`` and the exact environment
authority named by ``EXECUTION_AUTHORITY_ENV``.

On success the wrapper writes one create-only
``source-fact-rescore-scorecard.v1`` receipt containing the newly calibrated
scorecard and canonical input/output hashes.  Consumers still have to rebuild
the candidate through the normal source-fact/package gates; this receipt is
not fabricated runner state and is not publication authority.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

from src.autoslice.jingting_chunker import parse_srt_cues
from src.autoslice.llm_client import LlmConfig, build_llm_call
from src.autoslice.review_evidence import SourceCue
from src.autoslice.selection_scorecard import (
    load_selected_selection_calibration_policy,
    selection_scorecard_is_valid,
)
from src.autoslice.semantic_candidate_selector import (
    RESCORE_SCORECARD_SCHEMA,
    rescore_candidate_scorecard,
)
from src.autoslice.source_fact_rescore_provenance import (
    MANUAL_RESCORE_COMMAND_TEMPLATE,
    MANUAL_RESCORE_MODEL,
    MANUAL_RESCORE_PROVIDER,
    MANUAL_RESCORE_TIMEOUT_SECONDS,
    MANUAL_RESCORE_TRANSPORT,
    SourceFactRescoreProvenanceError,
    load_committed_correction_authority,
    validate_correction_authority,
)
from src.autoslice.source_fact_review import (
    validate_source_fact_rescore_candidate_receipt,
)


PROVIDER_CONFIG_SCHEMA = "manual-source-fact-rescore-provider.v1"
EXECUTION_AUTHORITY_ENV = "AUTOSLICE_MANUAL_RESCORE_EXECUTION_AUTHORITY"
EXECUTION_AUTHORITY_VALUE = "SOURCE_FACT_RESCORE_AUTHORIZED"
PREFLIGHT_SCHEMA = "manual-source-fact-rescore-preflight.v1"
CPA_PROVIDER = MANUAL_RESCORE_PROVIDER
CPA_MODEL = MANUAL_RESCORE_MODEL
CPA_TRANSPORT = MANUAL_RESCORE_TRANSPORT
# llm_via_cpa.sh stops new dispatches after 400s and permits one already
# running curl to consume up to 180s: <=580s.  The production caller contract
# is therefore exactly 600s; a shorter value kills valid failover, while a
# longer value hides drift from the audited recovery lane.
CPA_CALLER_TIMEOUT_SECONDS = MANUAL_RESCORE_TIMEOUT_SECONDS
CPA_COMMAND_TEMPLATE = MANUAL_RESCORE_COMMAND_TEMPLATE
_CANDIDATE_ID_RE = re.compile(r"auto_[0-9]+_[0-9]+_[0-9]+\Z")
_SAFE_PROVIDER_RE = re.compile(r"[A-Za-z0-9._:/+-]+\Z")
ROOT = Path(__file__).resolve().parents[1]


class ManualScorecardRescoreError(RuntimeError):
    """The manual rescore could not preserve its provenance contract."""


@dataclass(frozen=True)
class _BoundFile:
    path: Path
    sha256: str


@dataclass(frozen=True)
class _ValidatedInputs:
    candidate_id: str
    reviewed_srt: _BoundFile
    source_fact_receipt: _BoundFile | None
    correction_authority: _BoundFile | None
    stale_scorecard: _BoundFile
    provider_config: _BoundFile
    calibration_policy: _BoundFile
    corrected_hook: str
    source_start_ms: int
    source_end_ms: int
    cues: tuple[SourceCue, ...]
    receipt: Mapping[str, object] | None
    authority: Mapping[str, object] | None
    scorecard: Mapping[str, object]
    provider: Mapping[str, object]
    input_bindings: Mapping[str, object]
    input_sha256: str


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _bytes_sha256(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _normalized_sha256(value: object, *, label: str) -> str:
    candidate = str(value or "").strip().lower()
    if candidate.startswith("sha256:"):
        candidate = candidate[7:]
    if len(candidate) != 64 or any(char not in "0123456789abcdef" for char in candidate):
        raise ManualScorecardRescoreError(f"{label} is not a sha256 digest")
    return "sha256:" + candidate


def _regular_input(path: Path, *, label: str) -> tuple[Path, bytes]:
    absolute = path.absolute()
    try:
        metadata = absolute.lstat()
        resolved = absolute.resolve(strict=True)
    except OSError as exc:
        raise ManualScorecardRescoreError(f"{label} is missing: {absolute}") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or absolute.is_symlink()
        or resolved != absolute
    ):
        raise ManualScorecardRescoreError(
            f"{label} must be a regular non-symlink path: {absolute}"
        )
    try:
        return resolved, resolved.read_bytes()
    except OSError as exc:
        raise ManualScorecardRescoreError(f"{label} is unreadable: {resolved}") from exc


def _bind_file(path: Path, expected_sha256: object, *, label: str) -> tuple[_BoundFile, bytes]:
    resolved, raw = _regular_input(path, label=label)
    expected = _normalized_sha256(expected_sha256, label=f"expected {label} sha256")
    observed = _bytes_sha256(raw)
    if observed != expected:
        raise ManualScorecardRescoreError(
            f"{label} bytes drifted: expected {expected}, observed {observed}"
        )
    return _BoundFile(resolved, observed), raw


def _json_object(raw: bytes, *, label: str) -> dict[str, object]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ManualScorecardRescoreError(f"{label} is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ManualScorecardRescoreError(f"{label} must contain one JSON object")
    return value


def _validate_provider_config(value: Mapping[str, object]) -> dict[str, object]:
    expected_keys = {"schema_version", "provider", "model", "llm_config"}
    if set(value) != expected_keys or value.get("schema_version") != PROVIDER_CONFIG_SCHEMA:
        raise ManualScorecardRescoreError("provider config schema or field set is invalid")
    provider = value.get("provider")
    model = value.get("model")
    llm_config = value.get("llm_config")
    if (
        not isinstance(provider, str)
        or not _SAFE_PROVIDER_RE.fullmatch(provider)
        or not isinstance(model, str)
        or not _SAFE_PROVIDER_RE.fullmatch(model)
        or not isinstance(llm_config, Mapping)
    ):
        raise ManualScorecardRescoreError("provider/model identity is invalid")
    if provider != CPA_PROVIDER:
        raise ManualScorecardRescoreError("provider config is not the CPA authority")
    if model != CPA_MODEL:
        raise ManualScorecardRescoreError(
            f"provider model must be the exact manual-rescore model {CPA_MODEL}"
        )
    command_keys = {"transport", "command_template", "timeout_seconds"}
    if set(llm_config) != command_keys or llm_config.get("transport") != CPA_TRANSPORT:
        raise ManualScorecardRescoreError(
            "provider llm_config must be the explicit command transport contract"
        )
    command_template = llm_config.get("command_template")
    timeout = llm_config.get("timeout_seconds")
    if (
        not isinstance(command_template, str)
        or command_template != CPA_COMMAND_TEMPLATE
    ):
        raise ManualScorecardRescoreError("provider command template drifted")
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or float(timeout) != CPA_CALLER_TIMEOUT_SECONDS
    ):
        raise ManualScorecardRescoreError(
            "provider caller timeout must be exactly 600s for the <=580s CPA bridge"
        )
    return {
        "schema_version": PROVIDER_CONFIG_SCHEMA,
        "provider": provider,
        "model": model,
        "llm_config": {
            "transport": CPA_TRANSPORT,
            "command_template": command_template,
            "timeout_seconds": CPA_CALLER_TIMEOUT_SECONDS,
        },
    }


def _reviewed_srt_cues(
    raw: bytes,
    *,
    source_start_ms: int,
    source_end_ms: int,
) -> tuple[SourceCue, ...]:
    try:
        text = raw.decode("utf-8")
    except UnicodeError as exc:
        raise ManualScorecardRescoreError("reviewed final SRT is not UTF-8") from exc
    parsed = parse_srt_cues(text)
    if not parsed:
        raise ManualScorecardRescoreError("reviewed final SRT has no parseable cues")
    expected_indices = [str(index) for index in range(1, len(parsed) + 1)]
    if [cue.index for cue in parsed] != expected_indices:
        raise ManualScorecardRescoreError("reviewed final SRT cue indices are not contiguous")
    prior_end = -1
    duration_ms = source_end_ms - source_start_ms
    for cue in parsed:
        if (
            not cue.text.strip()
            or cue.start_ms < 0
            or cue.end_ms <= cue.start_ms
            or cue.start_ms < prior_end
            or cue.end_ms > duration_ms
        ):
            raise ManualScorecardRescoreError(
                "reviewed final SRT cue timeline is invalid or outside the source interval"
            )
        prior_end = cue.end_ms
    return tuple(
        SourceCue(
            cue_id=f"reviewed_final_{index:04d}",
            source_start_ms=source_start_ms + cue.start_ms,
            source_end_ms=source_start_ms + cue.end_ms,
            text=cue.text.strip(),
            language="zh",
            kind="speech",
            confidence=1.0,
        )
        for index, cue in enumerate(parsed, start=1)
    )


def _validate_output_target(output: Path) -> Path:
    absolute = output.absolute()
    try:
        absolute.lstat()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise ManualScorecardRescoreError(f"output target cannot be inspected: {absolute}") from exc
    else:
        raise ManualScorecardRescoreError(f"output already exists (create-only): {absolute}")
    parent = absolute.parent
    try:
        metadata = parent.lstat()
        resolved_parent = parent.resolve(strict=True)
    except OSError as exc:
        raise ManualScorecardRescoreError(f"output parent is missing: {parent}") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or parent.is_symlink()
        or resolved_parent != parent
    ):
        raise ManualScorecardRescoreError(
            f"output parent must be an existing non-symlink directory: {parent}"
        )
    return absolute


def validate_manual_rescore_inputs(
    *,
    candidate_id: str,
    reviewed_final_srt: Path,
    reviewed_final_srt_sha256: str,
    corrected_hook: str,
    source_fact_receipt: Path | None,
    source_fact_receipt_sha256: str | None,
    stale_scorecard: Path,
    stale_scorecard_sha256: str,
    source_start_ms: int,
    source_end_ms: int,
    provider_config: Path,
    provider_config_sha256: str,
    correction_authority: Path | None = None,
    correction_authority_sha256: str | None = None,
    stale_hook: str | None = None,
) -> _ValidatedInputs:
    """Validate every source/provider/calibration binding without a provider call."""

    if not isinstance(candidate_id, str) or not _CANDIDATE_ID_RE.fullmatch(candidate_id):
        raise ManualScorecardRescoreError("candidate_id is invalid")
    if not isinstance(corrected_hook, str) or not corrected_hook.strip():
        raise ManualScorecardRescoreError("corrected hook is empty")
    if (
        isinstance(source_start_ms, bool)
        or not isinstance(source_start_ms, int)
        or isinstance(source_end_ms, bool)
        or not isinstance(source_end_ms, int)
        or source_start_ms < 0
        or source_end_ms <= source_start_ms
    ):
        raise ManualScorecardRescoreError("source interval is invalid")

    reviewed_binding, reviewed_raw = _bind_file(
        reviewed_final_srt,
        reviewed_final_srt_sha256,
        label="reviewed final SRT",
    )
    scorecard_binding, scorecard_raw = _bind_file(
        stale_scorecard,
        stale_scorecard_sha256,
        label="stale scorecard",
    )
    provider_binding, provider_raw = _bind_file(
        provider_config,
        provider_config_sha256,
        label="provider config",
    )
    scorecard = _json_object(scorecard_raw, label="stale scorecard")
    provider = _validate_provider_config(
        _json_object(provider_raw, label="provider config")
    )
    cues = _reviewed_srt_cues(
        reviewed_raw,
        source_start_ms=source_start_ms,
        source_end_ms=source_end_ms,
    )

    reviewed_transcript = "\n".join(cue.text for cue in cues)
    reviewed_transcript_sha256 = _bytes_sha256(reviewed_transcript.encode("utf-8"))

    receipt_binding: _BoundFile | None = None
    receipt: Mapping[str, object] | None = None
    authority_binding: _BoundFile | None = None
    authority: Mapping[str, object] | None = None
    if correction_authority is not None or correction_authority_sha256 is not None:
        if source_fact_receipt is not None or source_fact_receipt_sha256 is not None:
            raise ManualScorecardRescoreError(
                "choose exactly one stale source-fact receipt or correction authority"
            )
        if correction_authority is None or correction_authority_sha256 is None:
            raise ManualScorecardRescoreError(
                "correction authority path and SHA-256 are both required"
            )
        authority_binding, authority_raw = _bind_file(
            correction_authority,
            correction_authority_sha256,
            label="candidate correction authority",
        )
        try:
            authority = validate_correction_authority(
                _json_object(authority_raw, label="candidate correction authority"),
                candidate_id=candidate_id,
            )
            committed, committed_path, committed_file_sha256 = (
                load_committed_correction_authority(
                    repo_root=ROOT,
                    candidate_id=candidate_id,
                )
            )
        except SourceFactRescoreProvenanceError as exc:
            raise ManualScorecardRescoreError(str(exc)) from exc
        if (
            authority_binding.path != committed_path
            or authority_binding.sha256 != committed_file_sha256
            or authority != committed
        ):
            raise ManualScorecardRescoreError(
                "candidate correction authority is not the committed repository asset"
            )
        if stale_hook != authority.get("original_selection_hook"):
            raise ManualScorecardRescoreError(
                "stale hook does not match the candidate correction authority"
            )
        if corrected_hook != authority.get("corrected_selection_hook"):
            raise ManualScorecardRescoreError(
                "corrected hook does not match the candidate correction authority"
            )
        if _canonical_sha256(scorecard) != authority.get(
            "stale_selection_scorecard_sha256"
        ):
            raise ManualScorecardRescoreError(
                "stale scorecard does not match the candidate correction authority"
            )
        reviewed_authority = authority.get("reviewed_final_srt")
        source_authority = authority.get("source")
        assert isinstance(reviewed_authority, Mapping)
        assert isinstance(source_authority, Mapping)
        if (
            reviewed_binding.sha256 != reviewed_authority.get("sha256")
            or len(cues) != reviewed_authority.get("cue_count")
        ):
            raise ManualScorecardRescoreError(
                "reviewed final SRT does not match the candidate correction authority"
            )
        if (
            source_start_ms != source_authority.get("absolute_start_ms")
            or source_end_ms != source_authority.get("absolute_end_ms")
        ):
            raise ManualScorecardRescoreError(
                "source interval does not match the candidate correction authority"
            )
        input_mode = "candidate_correction_authority"
        original_hook = str(authority["original_selection_hook"])
    else:
        if (
            source_fact_receipt is None
            or source_fact_receipt_sha256 is None
            or stale_hook is not None
        ):
            raise ManualScorecardRescoreError(
                "stale source-fact receipt path and SHA-256 are required"
            )
        receipt_binding, receipt_raw = _bind_file(
            source_fact_receipt,
            source_fact_receipt_sha256,
            label="stale source-fact receipt",
        )
        receipt = _json_object(receipt_raw, label="stale source-fact receipt")
        original_hook = receipt.get("original_selection_hook")
        original_title = receipt.get("original_title")
        if not isinstance(original_hook, str) or not isinstance(original_title, str):
            raise ManualScorecardRescoreError(
                "stale source-fact receipt has no original hook/title binding"
            )
        if not validate_source_fact_rescore_candidate_receipt(
            receipt,
            selection_hook=original_hook,
            title=original_title,
            selection_scorecard=scorecard,
            candidate_id=candidate_id,
            final_reviewed_srt_path=reviewed_binding.path,
        ):
            raise ManualScorecardRescoreError("stale source-fact receipt chain is invalid")
        rescore_block = receipt.get("rescore_candidate")
        assert isinstance(rescore_block, Mapping)
        if rescore_block.get("repaired_selection_hook") != corrected_hook:
            raise ManualScorecardRescoreError(
                "corrected hook does not match the source-fact rescore candidate"
            )
        passes = receipt.get("passes")
        assert isinstance(passes, list) and passes and isinstance(passes[-1], Mapping)
        if passes[-1].get("final_transcript_sha256") != reviewed_transcript_sha256:
            raise ManualScorecardRescoreError(
                "reviewed final SRT text does not match the stale source-fact receipt"
            )
        input_mode = "source_fact_stale_receipt"

    calibration = load_selected_selection_calibration_policy()
    calibration_path, calibration_raw = _regular_input(
        calibration.source_path,
        label="selected selection calibration policy",
    )
    calibration_observed = _bytes_sha256(calibration_raw)
    if calibration_observed != calibration.source_sha256:
        raise ManualScorecardRescoreError("selection calibration changed while loading")
    calibration_binding = _BoundFile(calibration_path, calibration_observed)

    input_bindings: dict[str, object] = {
        "input_mode": input_mode,
        "candidate_id": candidate_id,
        "reviewed_final_srt_sha256": reviewed_binding.sha256,
        "reviewed_final_cue_count": len(cues),
        "reviewed_final_transcript_sha256": reviewed_transcript_sha256,
        "corrected_hook": corrected_hook,
        "corrected_hook_sha256": _bytes_sha256(corrected_hook.encode("utf-8")),
        "original_selection_hook": original_hook,
        "original_selection_hook_sha256": _bytes_sha256(
            original_hook.encode("utf-8")
        ),
        "source_interval": {
            "absolute_start_ms": source_start_ms,
            "absolute_end_ms": source_end_ms,
        },
        "stale_scorecard_file_sha256": scorecard_binding.sha256,
        "stale_scorecard_sha256": _canonical_sha256(scorecard),
        "provider_config_file_sha256": provider_binding.sha256,
        "provider": provider["provider"],
        "model": provider["model"],
        "transport": provider["llm_config"]["transport"],  # type: ignore[index]
        "provider_command_template_sha256": _bytes_sha256(
            str(provider["llm_config"]["command_template"]).encode("utf-8")  # type: ignore[index]
        ),
        "provider_timeout_seconds": provider["llm_config"]["timeout_seconds"],  # type: ignore[index]
        "selection_calibration_sha256": calibration_binding.sha256,
    }
    if receipt_binding is not None and receipt is not None:
        input_bindings.update(
            {
                "source_fact_receipt_file_sha256": receipt_binding.sha256,
                "source_fact_receipt_sha256": receipt.get("receipt_sha256"),
            }
        )
    if authority_binding is not None and authority is not None:
        input_bindings.update(
            {
                "correction_authority_file_sha256": authority_binding.sha256,
                "correction_authority_sha256": authority.get("authority_sha256"),
            }
        )
    return _ValidatedInputs(
        candidate_id=candidate_id,
        reviewed_srt=reviewed_binding,
        source_fact_receipt=receipt_binding,
        correction_authority=authority_binding,
        stale_scorecard=scorecard_binding,
        provider_config=provider_binding,
        calibration_policy=calibration_binding,
        corrected_hook=corrected_hook,
        source_start_ms=source_start_ms,
        source_end_ms=source_end_ms,
        cues=cues,
        receipt=receipt,
        authority=authority,
        scorecard=scorecard,
        provider=provider,
        input_bindings=input_bindings,
        input_sha256=_canonical_sha256(input_bindings),
    )


def _preflight_document(validated: _ValidatedInputs) -> dict[str, object]:
    return {
        "schema_version": PREFLIGHT_SCHEMA,
        "status": "VALIDATED_NO_PROVIDER_CALL",
        "candidate_id": validated.candidate_id,
        "input_bindings": dict(validated.input_bindings),
        "input_sha256": validated.input_sha256,
        "execution_authority_env": EXECUTION_AUTHORITY_ENV,
        "next_action": (
            "set the exact execution authority and pass --execute-provider-call; "
            "the output remains create-only"
        ),
    }


def _assert_inputs_unchanged(validated: _ValidatedInputs) -> None:
    bindings: list[tuple[_BoundFile, str]] = [
        (validated.reviewed_srt, "reviewed final SRT"),
        (validated.stale_scorecard, "stale scorecard"),
        (validated.provider_config, "provider config"),
        (validated.calibration_policy, "selection calibration policy"),
    ]
    if validated.source_fact_receipt is not None:
        bindings.append(
            (validated.source_fact_receipt, "stale source-fact receipt")
        )
    if validated.correction_authority is not None:
        bindings.append(
            (validated.correction_authority, "candidate correction authority")
        )
    for binding, label in bindings:
        _, raw = _regular_input(binding.path, label=label)
        if _bytes_sha256(raw) != binding.sha256:
            raise ManualScorecardRescoreError(
                f"{label} bytes drifted after validation"
            )


def _provider_call_from_config(provider: Mapping[str, object]) -> Callable[[str], str]:
    llm_config = provider["llm_config"]
    assert isinstance(llm_config, Mapping)
    template = str(llm_config["command_template"]).replace(
        "{model}", str(provider["model"])
    )
    return build_llm_call(
        LlmConfig(
            transport="command",
            model=str(provider["model"]),
            command_template=template,
            timeout_seconds=float(llm_config["timeout_seconds"]),
        )
    )


def run_manual_rescore(
    *,
    output: Path,
    execute_provider_call: bool = False,
    environ: Mapping[str, str] | None = None,
    llm_call: Callable[[str], str] | None = None,
    **input_kwargs: object,
) -> dict[str, object]:
    """Validate, optionally call the provider, and create one bound receipt."""

    validated = validate_manual_rescore_inputs(**input_kwargs)  # type: ignore[arg-type]
    output_path = _validate_output_target(output)
    if not execute_provider_call:
        return _preflight_document(validated)
    selected_environ = os.environ if environ is None else environ
    if selected_environ.get(EXECUTION_AUTHORITY_ENV) != EXECUTION_AUTHORITY_VALUE:
        raise ManualScorecardRescoreError(
            f"provider execution requires {EXECUTION_AUTHORITY_ENV}="
            f"{EXECUTION_AUTHORITY_VALUE}"
        )

    provider_call = llm_call or _provider_call_from_config(validated.provider)
    outcome = rescore_candidate_scorecard(
        candidate_id=validated.candidate_id,
        cues=validated.cues,
        repaired_hook=validated.corrected_hook,
        clip_context_prompt="",
        llm_call=provider_call,
        stale_scorecard=validated.scorecard,
        start_cue=1,
        end_cue=len(validated.cues),
    )
    if outcome.get("outcome") != "RESCORED" or not selection_scorecard_is_valid(
        outcome.get("selection_scorecard")
    ):
        raise ManualScorecardRescoreError(
            "rescore primitive did not produce a valid calibrated scorecard: "
            f"{outcome.get('outcome')}/{outcome.get('reason_code')}"
        )
    _assert_inputs_unchanged(validated)
    _validate_output_target(output_path)

    new_scorecard = outcome["selection_scorecard"]
    receipt: dict[str, object] = {
        "schema_version": RESCORE_SCORECARD_SCHEMA,
        "status": "RESCORED",
        "candidate_id": validated.candidate_id,
        "input_bindings": dict(validated.input_bindings),
        "input_sha256": validated.input_sha256,
        "selection_scorecard": new_scorecard,
        "selection_scorecard_sha256": _canonical_sha256(new_scorecard),
        "primitive_output_sha256": _canonical_sha256(outcome),
        "start_cue": outcome.get("start_cue"),
        "end_cue": outcome.get("end_cue"),
        "authority": {
            "kind": "MANUAL_RECOVERY_RESCORE_ONLY",
            "runner_state_mutated": False,
            "publication_authority": False,
        },
    }
    receipt["output_sha256"] = _canonical_sha256(receipt)
    serialized = (
        json.dumps(receipt, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2)
        + "\n"
    ).encode("utf-8")
    try:
        descriptor = os.open(
            output_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
    except FileExistsError as exc:
        raise ManualScorecardRescoreError(
            f"output already exists (create-only): {output_path}"
        ) from exc
    try:
        with os.fdopen(descriptor, "wb") as sink:
            written = sink.write(serialized)
            sink.flush()
            os.fsync(sink.fileno())
        if written != len(serialized):
            raise ManualScorecardRescoreError("short write while creating rescore receipt")
    except Exception:
        # A partially created path is intentionally left in place: create-only
        # failure evidence must not be silently replaced by a later attempt.
        raise
    return receipt


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-id", required=True)
    parser.add_argument(
        "--reviewed-final-srt",
        type=Path,
        required=True,
        help="absolute path to the exact operator-reviewed final SRT",
    )
    parser.add_argument(
        "--reviewed-final-srt-sha256",
        required=True,
        help="expected SHA-256 of the exact SRT file bytes",
    )
    parser.add_argument("--corrected-hook", required=True)
    parser.add_argument(
        "--source-fact-receipt",
        type=Path,
        help="exact REPAIR_SCORECARD_STALE source-fact receipt JSON",
    )
    parser.add_argument(
        "--source-fact-receipt-sha256",
        help="expected SHA-256 of the receipt file bytes",
    )
    parser.add_argument(
        "--stale-scorecard",
        type=Path,
        required=True,
        help="JSON file whose root object is the stale selection scorecard itself",
    )
    parser.add_argument(
        "--stale-scorecard-sha256",
        required=True,
        help="expected SHA-256 of the stale-scorecard file bytes",
    )
    parser.add_argument("--source-start-ms", type=int, required=True)
    parser.add_argument("--source-end-ms", type=int, required=True)
    parser.add_argument("--provider-config", type=Path, required=True)
    parser.add_argument("--provider-config-sha256", required=True)
    parser.add_argument(
        "--correction-authority",
        type=Path,
        help=(
            "candidate-scoped committed Ivan correction authority; mutually "
            "exclusive with --source-fact-receipt"
        ),
    )
    parser.add_argument("--correction-authority-sha256")
    parser.add_argument(
        "--stale-hook",
        help="old incorrect hook; required by correction-authority mode",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--execute-provider-call",
        action="store_true",
        help=(
            "perform the single provider call; additionally requires "
            f"{EXECUTION_AUTHORITY_ENV}={EXECUTION_AUTHORITY_VALUE}"
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = run_manual_rescore(
            candidate_id=args.candidate_id,
            reviewed_final_srt=args.reviewed_final_srt,
            reviewed_final_srt_sha256=args.reviewed_final_srt_sha256,
            corrected_hook=args.corrected_hook,
            source_fact_receipt=args.source_fact_receipt,
            source_fact_receipt_sha256=args.source_fact_receipt_sha256,
            stale_scorecard=args.stale_scorecard,
            stale_scorecard_sha256=args.stale_scorecard_sha256,
            source_start_ms=args.source_start_ms,
            source_end_ms=args.source_end_ms,
            provider_config=args.provider_config,
            provider_config_sha256=args.provider_config_sha256,
            correction_authority=args.correction_authority,
            correction_authority_sha256=args.correction_authority_sha256,
            stale_hook=args.stale_hook,
            output=args.output,
            execute_provider_call=args.execute_provider_call,
        )
    except ManualScorecardRescoreError as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
