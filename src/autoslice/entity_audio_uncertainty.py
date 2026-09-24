"""Pure uncertainty receipts, shared without invoking any provider."""
from __future__ import annotations
from typing import Any, Mapping
from .acoustic_witness_protocol import BLIND_PINYIN_PROTOCOL, WITNESS_SCHEMA
from .exact_source_transcript_contract import (
    REQUEST_SCHEMA as EXACT_SOURCE_REQUEST_SCHEMA,
    OBSERVATION_SCHEMA as EXACT_SOURCE_OBSERVATION_SCHEMA,
)
WITNESS_REQUEST_SCHEMA = "subtitle-span-acoustic-witness-request.v1"


def _uncertain(request: Mapping[str, Any], reason: str, detail: str = "") -> dict[str, Any]:
    if request.get("schema_version") == EXACT_SOURCE_REQUEST_SCHEMA:
        return {
            "schema_version": EXACT_SOURCE_OBSERVATION_SCHEMA,
            "request_sha256": request.get("request_sha256"),
            "status": "INVALID",
            "candidate_blind": True,
            "reason_code": reason,
            "mutation_authorized": False,
            **({"detail": detail[-500:]} if detail else {}),
        }
    if request.get("schema_version") == WITNESS_REQUEST_SCHEMA:
        return {
            "schema_version": WITNESS_SCHEMA,
            "witness_protocol": BLIND_PINYIN_PROTOCOL,
            "request_sha256": request.get("request_sha256"),
            "status": "UNCERTAIN",
            "reason_code": reason,
            **({"detail": detail[-500:]} if detail else {}),
        }
    if request.get("schema_version") == "subtitle-span-acoustic-check-request.v1":
        return {
            "schema_version": "subtitle-span-acoustic-check-verdict.v1",
            "request_sha256": request.get("request_sha256"),
            "status": "UNCERTAIN",
            "reason_code": reason,
            **({"detail": detail[-500:]} if detail else {}),
        }
    return {
        "schema_version": "chat-entity-verdict.v1",
        "request_sha256": request.get("request_sha256"),
        "status": "UNCERTAIN",
        "reason_code": reason,
        **({"detail": detail[-500:]} if detail else {}),
    }
