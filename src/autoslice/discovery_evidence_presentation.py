"""Lossless sharing of repeated source observations in discovery prompts.

Only the presentation changes. Every ordered witness row, including its own
request identity, expands to the original validated record for cache/budget
identity. The original evidence and its acquisition ledgers are untouched.
"""
from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, TypedDict, cast

from .review_errors import ContractRejection

if TYPE_CHECKING:
    from .finding_context import JsonValue


class SourceContext(TypedDict):
    range_audio: list[dict[str, JsonValue]]
    screen: list[JsonValue]


class SharedSourceContext(SourceContext):
    schema_version: str
    representation_rule: str
    shared_observations: dict[str, dict[str, JsonValue]]

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


def compact_source_context(rows: SourceContext) -> SourceContext | SharedSourceContext:
    """Share only byte-equivalent bodies after their ordinary validation."""
    bodies: dict[str, dict[str, JsonValue]] = {}
    counts: dict[str, int] = {}
    keys: list[str] = []
    for row in rows["range_audio"]:
        body = {key: value for key, value in row.items()
                if key != "witness_request_sha256"}
        key = hashlib.sha256(_json(body).encode()).hexdigest()
        bodies[key] = body
        counts[key] = counts.get(key, 0) + 1
        keys.append(key)
    shared = {key: value for key, value in bodies.items() if counts[key] > 1}
    if not shared:
        return rows
    value: SharedSourceContext = {
        "schema_version": SCHEMA,
        "representation_rule": RULE,
        "shared_observations": shared,
        "range_audio": [
            {"shared_observation_ref": key,
             "witness_request_sha256": row["witness_request_sha256"]}
            if key in shared else row
            for key, row in zip(keys, rows["range_audio"])
        ],
        "screen": rows["screen"],
    }
    # Small repeated rows may cost less to spell out. Never grow a prompt.
    return value if len(_json(value)) < len(_json(rows)) else rows


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


def render_source_context(rows: SourceContext, srt_text: str) -> tuple[str, dict[str, JsonValue]]:
    """Render the validated observations and bind the receipt to exact bytes."""
    serialized = json.dumps(compact_source_context(rows), ensure_ascii=False, sort_keys=True)
    audit: dict[str, JsonValue] = {
        "status": "VALIDATED_CONTEXT_ONLY" if any(rows.values()) else "NO_VALIDATED_CONTEXT",
        "current_srt_sha256": "sha256:" + hashlib.sha256(srt_text.encode()).hexdigest(),
        "range_count": len(rows["range_audio"]), "screen_frame_count": len(rows["screen"]),
        "context_sha256": "sha256:" + hashlib.sha256(serialized.encode()).hexdigest(),
        "mutation_authorized": False, "provider_calls": 0,
    }
    if not any(rows.values()):
        return "", audit
    return (
        "\n\n已验证与当前字幕、源时钟绑定的原始画面、范围听写及局部声学证据：\n"
        "这些是可能出错的来源观察，不能直接授权改字。画面中的游戏文字不是全部主播口述；"
        "整块听写也不是单 cue 的逐字真相。比较角色称呼、自我纠正、游戏叙述和主播插话，"
        "发现实际疑点后仍由正常逐项裁决处理，保留有意义的重复。\n" + serialized,
        audit,
    )
