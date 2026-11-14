"""Lossless sharing of repeated source observations in discovery prompts.

Only the presentation changes. Every ordered witness row, including its own
request identity, expands to the original validated record for cache/budget
identity. The original evidence and its acquisition ledgers are untouched.
"""
from __future__ import annotations
from typing import TYPE_CHECKING

import hashlib
import json
from typing import cast

from .contract_rejection import ContractRejection

if TYPE_CHECKING:
    from .retained_json_graph import JsonValue


SCHEMA = "discovery-shared-observations.v1"
RULE = (
    "A range_audio row with shared_observation_ref means the complete object "
    "in shared_observations at that key, plus that row's witness_request_sha256. "
    "Keep every row and its order. Shared text is repeated evidence, not extra "
    "independent agreement or authority."
)


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def expand_source_context(value: object) -> object:
    """Recover exact material identity; malformed references fail closed."""
    if not isinstance(value, dict) or value.get("schema_version") != SCHEMA:
        return value

    def require(ok: bool) -> None:
        if not ok:
            raise ContractRejection("DISCOVERY_SHARED_OBSERVATION_INVALID")

    require(set(value) == {"schema_version", "representation_rule",
                           "shared_observations", "range_audio", "screen"})
    require(value["representation_rule"] == RULE)
    shared = cast(dict[str, dict[str, "JsonValue"]], value["shared_observations"])
    require(isinstance(shared, dict) and bool(shared))
    require(isinstance(value["range_audio"], list) and isinstance(value["screen"], list))
    for key, body in shared.items():
        require(isinstance(body, dict) and "witness_request_sha256" not in body
                and "shared_observation_ref" not in body)
        require(key == hashlib.sha256(_json(body).encode()).hexdigest())
    counts = dict.fromkeys(shared, 0)
    rows: list[dict[str, JsonValue]] = []
    for row in value["range_audio"]:
        require(isinstance(row, dict))
        if "shared_observation_ref" not in row:
            rows.append(row)
            continue
        require(set(row) == {"shared_observation_ref", "witness_request_sha256"})
        key = row["shared_observation_ref"]
        require(isinstance(key, str) and key in shared)
        reference = cast(str, key)
        counts[reference] += 1
        rows.append({**shared[reference], "witness_request_sha256": row["witness_request_sha256"]})
    require(all(count >= 2 for count in counts.values()))
    return {"range_audio": rows, "screen": value["screen"]}


