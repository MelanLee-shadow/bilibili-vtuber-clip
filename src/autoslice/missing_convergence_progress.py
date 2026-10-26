"""Persist an exact unresolved whole-cue question and its original raw answer.

Only an actual UNRESOLVED/no-edit response is retained here. This cache cannot
provide KEEP, a replacement, or publication authority. The original request
and raw completion stay intact; controller wording is telemetry, while new
substantive inputs establish a new question.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping

from src.autoslice.llm_client import LlmJsonParseError, extract_json_object

SCHEMA = "missing-convergence-no-progress.v1"
_DESCRIPTION_KEYS = frozenset({
    "input_contract_normalizations", "coalesced_discovery_descriptions",
    "exact_release_adjudication", "cpa_resource_pressure",
    "why", "reason", "evidence_cue_ids", "evidence_cue_indexes",
})


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def identity(*, finding: Mapping[str, Any], model: object, material: Mapping[str, Any]) -> dict[str, Any] | None:
    if not isinstance(model, Mapping) or not model:
        return None
    value = {"schema_version": SCHEMA, "model": dict(model), "material": dict(material),
             "finding": {k: v for k, v in finding.items() if k not in _DESCRIPTION_KEYS}}
    try:
        return {"sha256": digest(value), "input": value}
    except (TypeError, ValueError):
        return None


def cache_path(binding: Mapping[str, Any] | None) -> Path | None:
    base = os.environ.get("AUTOSLICE_BASE")
    if not base or binding is None:
        return None
    return Path(base) / 'cache' / 'missing-convergence-no-progress' / (binding['sha256'] + '.json')


def _unresolved(payload: Mapping[str, Any]) -> bool:
    from src.autoslice.semantic_answer import answer_selected
    return (not answer_selected(payload) and payload.get("replacement_text") in {None, ""}
            and payload.get("decision") == "UNRESOLVED")


def read(binding: Mapping[str, Any] | None) -> dict[str, Any] | None:
    path = cache_path(binding)
    if path is None or not path.exists():
        return None
    # A corrupt entry must not cause blind provider retries or become authority.
    try:
        if path.is_symlink():
            raise ValueError("symlink")
        entry = json.loads(path.read_text())
        if entry.get("schema_version") != SCHEMA or entry.get("binding") != binding:
            raise ValueError("binding")
        prompt, completion = entry["prompt"], entry["completion"]
        audit = entry["audit"]
        payload = extract_json_object(completion)
        if not (isinstance(prompt, str) and isinstance(completion, str)
                and hashlib.sha256(prompt.encode()).hexdigest() == entry["prompt_sha256"]
                and hashlib.sha256(completion.encode()).hexdigest() == entry["completion_sha256"]
                and digest(audit) == entry["audit_sha256"]
                and _unresolved(payload)
                and audit.get("status") == "UNRESOLVED"
                and audit.get("decision") == "UNRESOLVED"
                and audit.get("mutation_authorized") is False
                and audit.get("completion_sha256") == "sha256:" + entry["completion_sha256"]
                and audit.get("prompt_sha256") == "sha256:" + entry["prompt_sha256"]):
            raise ValueError("receipt")
        result = dict(audit)
        from .llm_client import cached_completion
        result["original_raw_completion"] = cached_completion(completion, binding['input']['model'])
        result["served_from_cache"] = True
        result["no_new_evidence"] = {
            "schema_version": SCHEMA, "status": "SAME_QUESTION_REPLAY",
            "input_sha256": binding["sha256"], "original_prompt_sha256": audit["prompt_sha256"],
            "original_completion_sha256": audit["completion_sha256"],
            "provider_calls": 0, "mutation_authorized": False,
        }
        return result
    except (OSError, ValueError, KeyError, TypeError, LlmJsonParseError) as exc:
        raise ValueError("MISSING_CONVERGENCE_PROGRESS_CACHE_INVALID") from exc


def save(binding: Mapping[str, Any] | None, *, prompt: str, completion: str | None,
         audit: Mapping[str, Any]) -> None:
    path = cache_path(binding)
    if path is None or completion is None or audit.get("status") != "UNRESOLVED":
        return
    if audit.get("served_from_cache") is True:
        return  # The original observation stays immutable on re-consumption.
    if not _unresolved(extract_json_object(completion)):
        return
    entry = {"schema_version": SCHEMA, "binding": binding, "prompt": prompt,
             "completion": completion, "audit": dict(audit), "audit_sha256": digest(audit),
             "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
             "completion_sha256": hashlib.sha256(completion.encode()).hexdigest()}
    path.parent.mkdir(parents=True, exist_ok=True)
    # Create/replace only our cache entry, never the original production receipt.
    temp = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as stream:
            temp = Path(stream.name)
            json.dump(entry, stream, ensure_ascii=False, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if temp is not None and temp.exists():
            temp.unlink()
