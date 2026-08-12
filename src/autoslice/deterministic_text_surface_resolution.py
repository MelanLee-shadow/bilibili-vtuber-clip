"""Closed, candidate-scoped publication text narrowing for one reviewed clip.

This module is intentionally not a general manual-title bypass.  It can only
replay the repository-sealed plan for ``auto_230125_1157_1229`` and only when
the source, interval, reviewed SRT, entity projection, uniform-host authority,
and the preserved FAILED source-fact receipt still match byte-for-byte.

The plan contains no free-form factual literal node.  Its small closed node set
selects a reviewed question, exact reported utterances, their reviewed order,
and one candidate-scoped entity surface.  Syntax and punctuation are emitted by
this renderer, so a plan cannot introduce a visual action, causal result, or an
unresolved participant attribution.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from src.autoslice.candidate_entity_projection import (
    CandidateEntityProjectionError,
    load_candidate_entity_projection,
)
from src.autoslice.repository_asset_authority import (
    RepositoryAssetAuthorityError,
    repository_authority_expects_asset,
    require_repository_asset_authority,
)
from src.autoslice.title_policy import publish_title_policy_violations


ROOT = Path(__file__).resolve().parents[2]
CANDIDATE_ID = "auto_230125_1157_1229"
SCHEMA_VERSION = "lidousha-deterministic-text-narrowing.v1"
PLAN_SCHEMA_VERSION = "closed-reviewed-text-surface-plan.v1"
POLICY_SCHEMA_VERSION = "deterministic-text-narrowing-policy.v1"
CONSUMPTION_SCHEMA_VERSION = "deterministic-text-narrowing-consumption.v1"
ASSET_DIRECTORY = Path("assets/lidousha/deterministic_text_surface_resolutions")
AUTHORITY_FILENAME = f"{CANDIDATE_ID}.deterministic-text-narrowing.v1.json"
FAILED_RECEIPT_FILENAME = f"{CANDIDATE_ID}.failed-source-fact.v1.json"

SOURCE_BASENAME = "22966160_20260808-23-01-25.mp4"
SOURCE_SHA256 = "sha256:6cf042681885d18ac4e3abb9548fe8ac3033f8b2e3365c44b6748fd3e4a324a3"
SOURCE_START_MS = 1_157_280
SOURCE_END_MS = 1_229_510
REVIEWED_SRT_SHA256 = "sha256:c29cc5aed6b52cca7618a88b083b9b46e86320db5595bcaa5a4ef4c57499ca7b"
ENTITY_ID = "ent_1ac8510680754aad873f02ba79444e02"
ENTITY_PROJECTION_SHA256 = "1cb31ffffe00e26cee9407a34f7267bd05f25f39081d0667ee0065e2fe9e6acd"
ENTITY_SURFACE = "xxsk"
FAILED_SOURCE_FACT_RECEIPT_SHA256 = (
    "sha256:cb372aaab107a2c7f4f73a432bbb9b75512b78f878192c02e709c79c6c8a7585"
)
SPEAKER_EVIDENCE_SHA256 = "sha256:7ad0672052878d79d0ad5f24e41ee57a6a8c401596b8954beb078ca45b39c989"
FINAL_TRANSCRIPT_SHA256 = "sha256:62dfad49e4284fa6d866f4b1a0ea38e23255a4af93ba0307e29241e8f96aa047"
CLIP_CONTEXT_PROMPT_SHA256 = (
    "sha256:9444ce2aba45614b554f435fd70b2d2aa3f21e7f1f671e1fd15170e0ee33e0a3"
)
SELECTION_SCORECARD_SHA256 = (
    "sha256:3a113c13988f72dfafdbe0a2dcc81ab16fd4cbb948fdd194d328d8e63f266685"
)
ENTITY_CONTEXT_SHA256 = "sha256:ff2c48eb2942f7de50ad537f161e9bdf9008b312683f763266f000eecfc874a5"

OLD_TITLE = (
    "【李豆沙】弹幕问有没有跳《夜蝶》的对象，小李公主抱xxsk后反问“那你要负责吗”，被奶P赖上了"
)
OLD_HOOK = "弹幕问有没有跳《夜蝶》的对象，小李讲自己公主抱xxsk后被对方一句‘你给了我你的第一次’钓到，立刻反问‘那你要负责吗’并成功截图赖上。"
EXACT_TITLE = (
    "【李豆沙】被问有没有跳《夜蝶》的对象，xxsk说“你给了我你的第一次”，李豆沙接着问“那你要负责吗”"
)
EXACT_HOOK = "李豆沙被问有没有跳《夜蝶》的对象后，讲到自己对xxsk说“我好容易就能抱起你”，又说“我确实这是我第一次这样抱别人”；xxsk说“你给了我你的第一次”，李豆沙接着问“那你要负责吗”，随后说“那我已经截图了”。"
EXACT_COMBINED_SHA256 = "sha256:1204e6b99d3c12f1cacbd111c3984bc84f146ff4ad24f764e9a84e4e321f1348"

_SHA_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_TOP_FIELDS = {
    "schema_version",
    "candidate_id",
    "scope",
    "source_binding",
    "entity_binding",
    "uniform_host_authority",
    "human_authority",
    "original_source_fact_attempt",
    "old_surfaces",
    "exact_surface_resolution",
    "closed_surface_plan",
    "policy_bundle",
    "policy_sha256",
    "authority_sha256",
}

EXPECTED_SPEAKER_EVIDENCE: dict[str, object] = {
    "policy_ids": {
        "absence": "speaker_mode_uniform_host/v1",
        "alignment": "speaker_cue_subsegment_alignment/v1",
        "text": "compact_ws/v1",
        "timing": "half_open_integer_ms_exact/v1",
    },
    "reason": "speaker_mode_uniform_host",
    "state": "AbsentAuthorized",
}

EXPECTED_PLAN: dict[str, object] = {
    "schema_version": PLAN_SCHEMA_VERSION,
    "title_nodes": [
        {"node_type": "channel_prefix", "channel_id": "lidousha"},
        {"node_type": "reviewed_question", "cue_index": 1, "form": "title_passive"},
        {
            "node_type": "reported_utterance",
            "context_cue_index": 15,
            "utterance_cue_index": 16,
            "speaker": "entity",
            "speech_act": "say",
            "order": "after_question",
        },
        {
            "node_type": "reported_utterance",
            "context_cue_index": 17,
            "utterance_cue_index": 18,
            "speaker": "host",
            "speech_act": "ask",
            "order": "next",
        },
    ],
    "hook_nodes": [
        {"node_type": "reviewed_question", "cue_index": 1, "form": "hook_postposed"},
        {
            "node_type": "addressed_utterance",
            "address_cue_index": 4,
            "utterance_cue_index": 6,
            "speaker": "host",
            "addressee": "entity",
            "order": "after_question",
        },
        {
            "node_type": "reported_utterance",
            "context_cue_index": 11,
            "utterance_cue_index": 12,
            "speaker": "host",
            "speech_act": "say",
            "order": "also",
        },
        {
            "node_type": "reported_utterance",
            "context_cue_index": 15,
            "utterance_cue_index": 16,
            "speaker": "entity",
            "speech_act": "say",
            "order": "next",
        },
        {
            "node_type": "reported_utterance",
            "context_cue_index": 17,
            "utterance_cue_index": 18,
            "speaker": "host",
            "speech_act": "ask",
            "order": "next",
        },
        {
            "node_type": "reported_utterance",
            "context_cue_index": 29,
            "utterance_cue_index": 30,
            "speaker": "host",
            "speech_act": "say",
            "order": "subsequently",
        },
    ],
}

EXPECTED_POLICY: dict[str, object] = {
    "schema_version": POLICY_SCHEMA_VERSION,
    "candidate_scope": CANDIDATE_ID,
    "allowed_node_types": [
        "addressed_utterance",
        "channel_prefix",
        "reported_utterance",
        "reviewed_question",
    ],
    "forbidden_node_capabilities": [
        "causal_result",
        "free_form_factual_literal",
        "participant_bound_unresolved_role",
        "unsupported_actor_or_addressee",
        "visual_action",
    ],
    "surface_encoding": "utf-8-nfc-no-bom-no-surrounding-whitespace-no-trailing-newline",
    "provider_replay": "never_after_response_bytes",
    "provider_retry": "transport_failure_without_provider_response_bytes_only",
    "identity_excludes": [
        "absolute_path",
        "hostname",
        "inode",
        "mount_point",
        "mtime",
        "wsl_identifier",
    ],
    "consultation": {
        "thread_id": "6a7bd35c-3980-83ea-99b5-542a40d50cfa",
        "visible_mode": "Pro",
        "prompt_sha256": "sha256:99afa279a73f1e692468d7107e43e308afcbc9f0f3f0756dfc431d5a5d7c86a6",
        "response_sha256": "sha256:5f86abef13add311336f4583e4d635f735d4d3919df77ff9eb80ad4aca984956",
    },
}

EXPECTED_HUMAN_AUTHORITY: dict[str, object] = {
    "reviewed_subtitle_entity_truth": {
        "message_id": "msg_019ff23a-8b10-7823-bb12-22073d30edd5",
        "timestamp": "2026-08-11T19:09:10.801Z",
        "quote": (
            '单人切片也很不错，唯一的一点就是0:16应该是"我对xxsk说"，以及字幕里所有的星汐换成xxsk，也就是说，'
            "在这里，应该用xxsk作为星汐seki的canonical的名字，就像lds作为李豆沙的昵称一样。其他没有提到的地方说明字幕是正确的，不用再provisional。"
        ),
        "sha256": "sha256:93db72190d02d8bbefadf53598a95046b04ac6b013e758691e4fc138733708e6",
    },
    "clip_publication_release": {
        "message_id": "msg_019ff23c-004e-7860-bb6e-ab6d01b55857",
        "timestamp": "2026-08-11T19:10:46.350Z",
        "quote": "说起来刚刚通过的这两个可以发布了，只要你根据我的人工真值改完后，可以直接去快车道发布，和你的通用车道修复并行进行",
        "sha256": "sha256:4767d2f9be0431d8d7347eb9dd2d3a989f477fed2c16ce08f7de91eee92060cd",
    },
    "exact_surface_adjudication": {"state": "ABSENT"},
    "visual_claim_authority": {"state": "ABSENT"},
}

EXPECTED_IMMUTABLE_ARTIFACT_SNAPSHOTS: list[dict[str, str]] = [
    {
        "logical_id": "vtuber-reproduce/run/20260811-6b160bf-fastlane3/solo/failed-publish.json",
        "sha256": "sha256:6c6222987371babeaea15a1205c4ba2eb9ab0428cb643f869c9af3827097d9bc",
    },
    {
        "logical_id": "vtuber-reproduce/run/20260811-6b160bf-fastlane3/solo/recut.mp4",
        "sha256": "sha256:8642fd6100e5abb38064a3f1abbb0ed7c39df0d5671f3b077e9cd8848dd02ddc",
    },
    {
        "logical_id": "vtuber-reproduce/run/20260811-6b160bf-fastlane3/solo/burned-final.mp4",
        "sha256": "sha256:8ed09b467da6ebed4adcd7277af5d43266fc928b5fc7716572cd9d7f58d67c9d",
    },
    {
        "logical_id": "vtuber-reproduce/run/20260811-6b160bf-fastlane3/solo/final.ass",
        "sha256": "sha256:41bc363b6665fd6cac13c3d1285e45b8b2151b2810450e5a50a71e05701ad7dc",
    },
    {
        "logical_id": "vtuber-reproduce/run/20260811-6b160bf-fastlane3/solo/recut-provenance.json",
        "sha256": "sha256:4687f4ba57eb3f114e8b83b54e72575bb413dc572d44a08083c1a31ac56b31cd",
    },
    {
        "logical_id": "vtuber-reproduce/run/20260811-6b160bf-fastlane3/solo/boundary-audit.json",
        "sha256": "sha256:33791adb5dea4cc7730e71f22ef11439124551d22ca9a2c94528232aaaa8b8b5",
    },
    {
        "logical_id": "vtuber-reproduce/run/20260811-6b160bf-fastlane3/solo/redelivery-baseline.json",
        "sha256": "sha256:dc425666ead69a9e69960a135278dfec3889f97837652d6c7ee52c74e6db4034",
    },
    {
        "logical_id": "vtuber-reproduce/run/20260811-6b160bf-fastlane3/solo/chat-authority.json",
        "sha256": "sha256:7da16a2ad4a332f4fe7ce855c16de6ce06c03bb2edea8ac71d5145084e45e8eb",
    },
    {
        "logical_id": "vtuber-reproduce/run/20260811-6b160bf-fastlane3/solo/review-flags.json",
        "sha256": "sha256:16224c93308e476516caa9c1ffe23df6613b90dcea0c856e4660f285b6832c39",
    },
]

_EXPECTED_CUES = {
    1: "然后主播有没有跳夜蝶的对象",
    4: "我对xxsk说",
    6: "我好容易就能抱起你",
    11: "但是我确，我跟她说",
    12: "我确实这是我第一次这样抱别人",
    15: "然后xxsk就说：啊",
    16: "你给了我你的第一次",
    17: "我说对啊",
    18: "那你要负责吗",
    29: "那你，你这样说了",
    30: "那我已经截图了",
}


class DeterministicTextSurfaceResolutionError(ValueError):
    """The candidate-only deterministic surface authority is stale or unsafe."""


@dataclass(frozen=True, slots=True)
class DeterministicTextSurfaceAuthorityV1:
    document: dict[str, object]
    failed_source_fact_receipt: dict[str, object]
    repo_root: Path
    repo_path: Path
    file_sha256: str
    failed_receipt_repo_path: Path
    failed_receipt_file_sha256: str


@dataclass(frozen=True, slots=True)
class RenderedTextSurfaces:
    title: str
    selection_hook: str


def canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def text_sha256(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def bytes_sha256(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def combined_surface_sha256(title: str, hook: str) -> str:
    value = b"title\0" + title.encode("utf-8") + b"\0hook\0" + hook.encode("utf-8")
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _error(reason: str) -> DeterministicTextSurfaceResolutionError:
    return DeterministicTextSurfaceResolutionError(f"DETERMINISTIC_TEXT_NARROWING_{reason}")


def _exact_mapping(
    value: object,
    *,
    fields: set[str],
    label: str,
) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise _error(f"{label.upper()}_SCHEMA_INVALID")
    return dict(value)


def _sha(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _SHA_RE.fullmatch(value) is None:
        raise _error(f"{label.upper()}_SHA256_INVALID")
    return value


def _clean_relative_path(value: object, *, label: str) -> Path:
    if not isinstance(value, str) or not value or value != value.strip():
        raise _error(f"{label.upper()}_REPO_PATH_INVALID")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or path.as_posix() != value:
        raise _error(f"{label.upper()}_REPO_PATH_INVALID")
    return path


def _validate_surface(
    value: object,
    *,
    expected: str,
    expected_sha256: str,
    expected_length: int,
    label: str,
) -> None:
    surface = _exact_mapping(
        value,
        fields={"value", "utf8_byte_length", "sha256"},
        label=label,
    )
    text = surface["value"]
    if (
        not isinstance(text, str)
        or text != expected
        or text != unicodedata.normalize("NFC", text)
        or text != text.strip()
        or text.startswith("\ufeff")
        or "\n" in text
        or "\r" in text
        or len(text.encode("utf-8")) != expected_length
        or surface["utf8_byte_length"] != expected_length
        or surface["sha256"] != expected_sha256
        or text_sha256(text) != expected_sha256
    ):
        raise _error(f"{label.upper()}_BYTES_MISMATCH")


def _validate_failed_receipt(value: object) -> dict[str, object]:
    receipt = _exact_mapping(
        value,
        fields={
            "decision",
            "entity_context",
            "final_selection_hook",
            "final_title",
            "original_selection_hook",
            "original_title",
            "passes",
            "provider_retries",
            "reason_code",
            "receipt_sha256",
            "schema_version",
            "speaker_evidence",
            "speaker_evidence_sha256",
            "status",
        },
        label="failed_source_fact_receipt",
    )
    body = dict(receipt)
    declared = body.pop("receipt_sha256")
    if declared != canonical_sha256(body) or declared != FAILED_SOURCE_FACT_RECEIPT_SHA256:
        raise _error("FAILED_SOURCE_FACT_RECEIPT_HASH_MISMATCH")
    if (
        receipt["schema_version"] != "lidousha-source-fact-review.v1"
        or receipt["status"] != "FAILED"
        or receipt["decision"] != "NONE"
        or receipt["reason_code"] != "CPA_TEXT_REVIEW_INVALID"
        or receipt["original_title"] != OLD_TITLE
        or receipt["final_title"] != OLD_TITLE
        or receipt["original_selection_hook"] != OLD_HOOK
        or receipt["final_selection_hook"] != OLD_HOOK
        or receipt["speaker_evidence"] != EXPECTED_SPEAKER_EVIDENCE
        or receipt["speaker_evidence_sha256"] != SPEAKER_EVIDENCE_SHA256
        or canonical_sha256(receipt["speaker_evidence"]) != SPEAKER_EVIDENCE_SHA256
    ):
        raise _error("FAILED_SOURCE_FACT_RECEIPT_STATE_MISMATCH")
    retries = receipt["provider_retries"]
    if not isinstance(retries, list) or [
        row.get("attempt") for row in retries if isinstance(row, Mapping)
    ] != [1, 2]:
        raise _error("FAILED_SOURCE_FACT_RETRY_CHAIN_MISMATCH")
    if any(
        not isinstance(row, Mapping)
        or set(row)
        != {"attempt", "reason_code", "request_sha256", "response_sha256", "review_pass"}
        or row.get("reason_code") != "CPA_TEXT_REVIEW_INVALID"
        for row in retries
    ):
        raise _error("FAILED_SOURCE_FACT_RETRY_CHAIN_MISMATCH")
    passes = receipt["passes"]
    if not isinstance(passes, list) or len(passes) != 1 or not isinstance(passes[0], Mapping):
        raise _error("FAILED_SOURCE_FACT_PASS_SHAPE_MISMATCH")
    review_pass = passes[0]
    if (
        review_pass.get("status") != "FAILED"
        or review_pass.get("reason_code") != "CPA_TEXT_REVIEW_INVALID"
        or review_pass.get("speaker_evidence_sha256") != SPEAKER_EVIDENCE_SHA256
        or review_pass.get("final_transcript_sha256") != FINAL_TRANSCRIPT_SHA256
        or review_pass.get("clip_context_prompt_sha256") != CLIP_CONTEXT_PROMPT_SHA256
        or review_pass.get("selection_scorecard_sha256") != SELECTION_SCORECARD_SHA256
        or review_pass.get("entity_context_sha256") != ENTITY_CONTEXT_SHA256
    ):
        raise _error("FAILED_SOURCE_FACT_PASS_STATE_MISMATCH")
    entity_context = receipt["entity_context"]
    if (
        not isinstance(entity_context, Mapping)
        or entity_context.get("context_sha256") != ENTITY_CONTEXT_SHA256
    ):
        raise _error("FAILED_SOURCE_FACT_ENTITY_CONTEXT_MISMATCH")
    return receipt


def _validate_runtime_adjudication_inputs(
    *,
    failed_receipt: Mapping[str, object],
    final_transcript: object,
    clip_context_prompt: object,
    selection_scorecard: object,
    entity_context: object,
) -> dict[str, str]:
    """Bind the live judge inputs to the immutable failed adjudication.

    The failed receipt stores hashes for the three potentially large inputs
    and the complete relocation-safe entity context.  The deterministic lane
    is valid only for those same live values; a surface-only replay against a
    different transcript, context prompt, scorecard, or source/entity binding
    would otherwise silently widen the Pro-approved exception.
    """

    passes = failed_receipt.get("passes")
    if not isinstance(passes, list) or len(passes) != 1 or not isinstance(passes[0], Mapping):
        raise _error("RUNTIME_ADJUDICATION_PASS_MISSING")
    review_pass = passes[0]
    if (
        not isinstance(final_transcript, str)
        or text_sha256(final_transcript) != review_pass.get("final_transcript_sha256")
        or review_pass.get("final_transcript_sha256") != FINAL_TRANSCRIPT_SHA256
    ):
        raise _error("RUNTIME_FINAL_TRANSCRIPT_MISMATCH")
    if (
        not isinstance(clip_context_prompt, str)
        or text_sha256(clip_context_prompt) != review_pass.get("clip_context_prompt_sha256")
        or review_pass.get("clip_context_prompt_sha256") != CLIP_CONTEXT_PROMPT_SHA256
    ):
        raise _error("RUNTIME_CLIP_CONTEXT_PROMPT_MISMATCH")
    try:
        scorecard_sha256 = canonical_sha256(selection_scorecard)
    except (TypeError, ValueError) as exc:
        raise _error("RUNTIME_SELECTION_SCORECARD_MISMATCH") from exc
    if (
        scorecard_sha256 != review_pass.get("selection_scorecard_sha256")
        or review_pass.get("selection_scorecard_sha256") != SELECTION_SCORECARD_SHA256
    ):
        raise _error("RUNTIME_SELECTION_SCORECARD_MISMATCH")

    frozen_entity_context = failed_receipt.get("entity_context")
    if not isinstance(entity_context, Mapping) or not isinstance(frozen_entity_context, Mapping):
        raise _error("RUNTIME_ENTITY_CONTEXT_MISMATCH")
    try:
        current_entity_context = json.loads(
            json.dumps(
                entity_context,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    except (TypeError, ValueError) as exc:
        raise _error("RUNTIME_ENTITY_CONTEXT_MISMATCH") from exc
    if current_entity_context != frozen_entity_context:
        raise _error("RUNTIME_ENTITY_CONTEXT_MISMATCH")
    entity_body = dict(current_entity_context)
    entity_context_sha256 = entity_body.pop("context_sha256", None)
    if (
        entity_context_sha256 != ENTITY_CONTEXT_SHA256
        or entity_context_sha256 != review_pass.get("entity_context_sha256")
        or canonical_sha256(entity_body) != entity_context_sha256
    ):
        raise _error("RUNTIME_ENTITY_CONTEXT_HASH_MISMATCH")
    return {
        "final_transcript_sha256": FINAL_TRANSCRIPT_SHA256,
        "clip_context_prompt_sha256": CLIP_CONTEXT_PROMPT_SHA256,
        "selection_scorecard_sha256": SELECTION_SCORECARD_SHA256,
        "entity_context_sha256": ENTITY_CONTEXT_SHA256,
    }


def _validate_bound_artifact(value: object, *, label: str) -> dict[str, object]:
    row = _exact_mapping(value, fields={"logical_id", "sha256"}, label=label)
    logical_id = row["logical_id"]
    if (
        not isinstance(logical_id, str)
        or not logical_id
        or logical_id != logical_id.strip()
        or logical_id.startswith(("/", "\\"))
        or ":\\" in logical_id
        or ".." in Path(logical_id).parts
    ):
        raise _error(f"{label.upper()}_LOGICAL_ID_INVALID")
    _sha(row["sha256"], label=label)
    return row


def validate_deterministic_text_surface_document(
    value: object,
    *,
    failed_source_fact_receipt: object,
) -> dict[str, object]:
    """Validate one authority document without consulting paths or providers."""

    document = _exact_mapping(value, fields=_TOP_FIELDS, label="authority")
    if document["schema_version"] != SCHEMA_VERSION or document["candidate_id"] != CANDIDATE_ID:
        raise _error("AUTHORITY_SCOPE_INVALID")
    if document["scope"] != {
        "candidate_scoped": True,
        "closed_plan": True,
        "exact_surface_only": True,
        "wildcard": False,
    }:
        raise _error("AUTHORITY_SCOPE_INVALID")

    source = _exact_mapping(
        document["source_binding"],
        fields={
            "source_media",
            "exact_interval",
            "reviewed_srt",
            "reviewed_srt_manifest",
            "immutable_artifact_snapshots",
        },
        label="source_binding",
    )
    media = _exact_mapping(
        source["source_media"], fields={"basename", "sha256"}, label="source_media"
    )
    interval = _exact_mapping(
        source["exact_interval"], fields={"start_ms", "end_ms", "semantics"}, label="exact_interval"
    )
    if media != {"basename": SOURCE_BASENAME, "sha256": SOURCE_SHA256} or interval != {
        "start_ms": SOURCE_START_MS,
        "end_ms": SOURCE_END_MS,
        "semantics": "half_open",
    }:
        raise _error("SOURCE_BINDING_MISMATCH")
    reviewed = _exact_mapping(
        source["reviewed_srt"], fields={"repo_path", "sha256"}, label="reviewed_srt"
    )
    manifest = _exact_mapping(
        source["reviewed_srt_manifest"],
        fields={"repo_path", "sha256"},
        label="reviewed_srt_manifest",
    )
    _clean_relative_path(reviewed["repo_path"], label="reviewed_srt")
    _clean_relative_path(manifest["repo_path"], label="reviewed_srt_manifest")
    if reviewed["sha256"] != REVIEWED_SRT_SHA256:
        raise _error("REVIEWED_SRT_BINDING_MISMATCH")
    _sha(manifest["sha256"], label="reviewed_srt_manifest")
    snapshots = source["immutable_artifact_snapshots"]
    if not isinstance(snapshots, list) or len(snapshots) != 9:
        raise _error("IMMUTABLE_ARTIFACT_SNAPSHOTS_SCHEMA_INVALID")
    validated_snapshots = [
        _validate_bound_artifact(row, label=f"immutable_artifact_{index}")
        for index, row in enumerate(snapshots)
    ]
    if len({row["logical_id"] for row in validated_snapshots}) != len(validated_snapshots):
        raise _error("IMMUTABLE_ARTIFACT_SNAPSHOT_DUPLICATE")
    if validated_snapshots != EXPECTED_IMMUTABLE_ARTIFACT_SNAPSHOTS:
        raise _error("IMMUTABLE_ARTIFACT_SNAPSHOT_MISMATCH")

    entity = _exact_mapping(
        document["entity_binding"],
        fields={
            "repo_path",
            "file_sha256",
            "projection_sha256",
            "entity_id",
            "equivalent_surfaces",
            "required_surface_type",
            "required_surface",
        },
        label="entity_binding",
    )
    _clean_relative_path(entity["repo_path"], label="entity_projection")
    _sha(entity["file_sha256"], label="entity_projection_file")
    if entity != {
        **entity,
        "projection_sha256": ENTITY_PROJECTION_SHA256,
        "entity_id": ENTITY_ID,
        "equivalent_surfaces": ["星汐Seki", "星汐", "xxsk"],
        "required_surface_type": "title_cover",
        "required_surface": ENTITY_SURFACE,
    }:
        raise _error("ENTITY_BINDING_MISMATCH")

    speaker = _exact_mapping(
        document["uniform_host_authority"],
        fields={"speaker_mode", "evidence", "evidence_sha256", "authority_policy"},
        label="uniform_host_authority",
    )
    if (
        speaker
        != {
            "speaker_mode": "uniform_host",
            "evidence": EXPECTED_SPEAKER_EVIDENCE,
            "evidence_sha256": SPEAKER_EVIDENCE_SHA256,
            "authority_policy": "speaker_mode_uniform_host/v1",
        }
        or canonical_sha256(speaker["evidence"]) != SPEAKER_EVIDENCE_SHA256
    ):
        raise _error("UNIFORM_HOST_AUTHORITY_MISMATCH")

    human = _exact_mapping(
        document["human_authority"],
        fields={
            "reviewed_subtitle_entity_truth",
            "clip_publication_release",
            "exact_surface_adjudication",
            "visual_claim_authority",
        },
        label="human_authority",
    )
    for key in ("reviewed_subtitle_entity_truth", "clip_publication_release"):
        quote = _exact_mapping(
            human[key], fields={"message_id", "timestamp", "quote", "sha256"}, label=key
        )
        if (
            not isinstance(quote["message_id"], str)
            or not quote["message_id"].startswith("msg_")
            or not isinstance(quote["timestamp"], str)
            or not quote["timestamp"].endswith("Z")
            or not isinstance(quote["quote"], str)
            or quote["sha256"] != text_sha256(quote["quote"])
        ):
            raise _error(f"{key.upper()}_MISMATCH")
    if human != EXPECTED_HUMAN_AUTHORITY:
        raise _error("HUMAN_AUTHORITY_SCOPE_INVALID")

    failed = _validate_failed_receipt(failed_source_fact_receipt)
    attempt = _exact_mapping(
        document["original_source_fact_attempt"],
        fields={
            "state",
            "reason_code",
            "receipt_repo_path",
            "receipt_file_sha256",
            "receipt_sha256",
            "request_sha256",
            "response_sha256",
            "failed_publish_snapshot",
            "retry_rule",
            "later_keep_authority",
        },
        label="original_source_fact_attempt",
    )
    _clean_relative_path(attempt["receipt_repo_path"], label="failed_source_fact_receipt")
    for key in ("receipt_file_sha256", "receipt_sha256", "request_sha256", "response_sha256"):
        _sha(attempt[key], label=f"source_fact_{key}")
    _validate_bound_artifact(attempt["failed_publish_snapshot"], label="failed_publish_snapshot")
    first_pass = failed["passes"][0]
    if (
        attempt["state"] != "FAILED"
        or attempt["reason_code"] != "CPA_TEXT_REVIEW_INVALID"
        or attempt["receipt_sha256"] != FAILED_SOURCE_FACT_RECEIPT_SHA256
        or attempt["request_sha256"] != first_pass["request_sha256"]
        or attempt["response_sha256"] != first_pass["response_sha256"]
        or attempt["retry_rule"] != "transport_failure_without_provider_response_bytes_only"
        or attempt["later_keep_authority"] != "NON_AUTHORITATIVE"
    ):
        raise _error("ORIGINAL_SOURCE_FACT_ATTEMPT_MISMATCH")

    old = _exact_mapping(
        document["old_surfaces"], fields={"title", "hook", "combined_sha256"}, label="old_surfaces"
    )
    _validate_surface(
        old["title"],
        expected=OLD_TITLE,
        expected_sha256=text_sha256(OLD_TITLE),
        expected_length=131,
        label="old_title",
    )
    _validate_surface(
        old["hook"],
        expected=OLD_HOOK,
        expected_sha256=text_sha256(OLD_HOOK),
        expected_length=193,
        label="old_hook",
    )
    if old["combined_sha256"] != combined_surface_sha256(OLD_TITLE, OLD_HOOK):
        raise _error("OLD_SURFACE_COMBINED_HASH_MISMATCH")

    exact = _exact_mapping(
        document["exact_surface_resolution"],
        fields={"state", "publication_fact_authority", "title", "hook", "combined_sha256"},
        label="exact_surface_resolution",
    )
    title_surface = _exact_mapping(
        exact["title"],
        fields={
            "value",
            "utf8_byte_length",
            "unicode_codepoint_length",
            "current_global_policy_violations",
            "current_global_policy_disposition",
            "sha256",
        },
        label="exact_title",
    )
    exact_title = title_surface["value"]
    if (
        not isinstance(exact_title, str)
        or exact_title != EXACT_TITLE
        or exact_title != unicodedata.normalize("NFC", exact_title)
        or exact_title != exact_title.strip()
        or exact_title.startswith("\ufeff")
        or "\n" in exact_title
        or "\r" in exact_title
        or len(exact_title.encode("utf-8")) != 142
        or title_surface["utf8_byte_length"] != 142
        or len(exact_title) != 50
        or title_surface["unicode_codepoint_length"] != 50
        or title_surface["current_global_policy_violations"]
        != ["publish_title_length_out_of_bounds"]
        or publish_title_policy_violations(EXACT_TITLE) != ["publish_title_length_out_of_bounds"]
        or title_surface["current_global_policy_disposition"]
        != "candidate_scoped_exact_surface_exception_required"
        or title_surface["sha256"] != text_sha256(EXACT_TITLE)
    ):
        raise _error("EXACT_TITLE_BYTES_MISMATCH")
    _validate_surface(
        exact["hook"],
        expected=EXACT_HOOK,
        expected_sha256=text_sha256(EXACT_HOOK),
        expected_length=296,
        label="exact_hook",
    )
    if (
        exact["state"] != "VALID"
        or exact["publication_fact_authority"] != "RESOLVED_EXACT_SURFACE"
        or exact["combined_sha256"] != EXACT_COMBINED_SHA256
        or combined_surface_sha256(EXACT_TITLE, EXACT_HOOK) != EXACT_COMBINED_SHA256
    ):
        raise _error("EXACT_SURFACE_STATE_MISMATCH")

    if document["closed_surface_plan"] != EXPECTED_PLAN:
        raise _error("CLOSED_SURFACE_PLAN_MISMATCH")
    if document["policy_bundle"] != EXPECTED_POLICY:
        raise _error("POLICY_BUNDLE_MISMATCH")
    if document["policy_sha256"] != canonical_sha256(EXPECTED_POLICY):
        raise _error("POLICY_HASH_MISMATCH")
    body = dict(document)
    declared = body.pop("authority_sha256")
    if declared != canonical_sha256(body):
        raise _error("AUTHORITY_HASH_MISMATCH")
    return document


def _read_regular_no_symlink(path: Path) -> bytes:
    absolute = path.absolute()
    cursor = Path(absolute.anchor)
    try:
        for part in absolute.parts[1:]:
            cursor = cursor / part
            if stat.S_ISLNK(os.lstat(cursor).st_mode):
                raise _error("ASSET_PATH_CONTAINS_SYMLINK")
        descriptor = os.open(
            absolute,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise _error("ASSET_NOT_REGULAR")
            chunks: list[bytes] = []
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    return b"".join(chunks)
                chunks.append(chunk)
        finally:
            os.close(descriptor)
    except DeterministicTextSurfaceResolutionError:
        raise
    except OSError as exc:
        raise _error("ASSET_UNREADABLE") from exc


def _read_repository_sealed_asset(root: Path, relative: Path) -> bytes:
    try:
        raw = _read_regular_no_symlink(root / relative)
        require_repository_asset_authority(
            repo_root=root,
            relative_path=relative,
            observed_bytes=raw,
        )
        return raw
    except RepositoryAssetAuthorityError as exc:
        raise _error("REPOSITORY_ASSET_UNSEALED") from exc


def load_deterministic_text_surface_authority(
    candidate_id: str,
    *,
    root: Path = ROOT,
) -> DeterministicTextSurfaceAuthorityV1 | None:
    """Load the sole repository-sealed candidate authority, or ``None`` if absent."""

    if candidate_id != CANDIDATE_ID:
        return None
    resolved_root = root.resolve(strict=True)
    relative = ASSET_DIRECTORY / AUTHORITY_FILENAME
    try:
        expected = repository_authority_expects_asset(
            repo_root=resolved_root,
            relative_path=relative,
        )
    except RepositoryAssetAuthorityError as exc:
        raise _error("REPOSITORY_AUTHORITY_INVALID") from exc
    if not expected:
        return None
    raw = _read_repository_sealed_asset(resolved_root, relative)
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise _error("AUTHORITY_UNREADABLE") from exc
    if not isinstance(document, Mapping):
        raise _error("AUTHORITY_SCHEMA_INVALID")
    attempt = document.get("original_source_fact_attempt")
    if not isinstance(attempt, Mapping):
        raise _error("ORIGINAL_SOURCE_FACT_ATTEMPT_SCHEMA_INVALID")
    receipt_relative = _clean_relative_path(
        attempt.get("receipt_repo_path"), label="failed_source_fact_receipt"
    )
    receipt_raw = _read_repository_sealed_asset(resolved_root, receipt_relative)
    if bytes_sha256(receipt_raw) != attempt.get("receipt_file_sha256"):
        raise _error("FAILED_SOURCE_FACT_RECEIPT_FILE_HASH_MISMATCH")
    try:
        failed_receipt = json.loads(receipt_raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise _error("FAILED_SOURCE_FACT_RECEIPT_UNREADABLE") from exc
    validated = validate_deterministic_text_surface_document(
        document,
        failed_source_fact_receipt=failed_receipt,
    )
    for binding_name, sha_key in (
        ("reviewed_srt", "sha256"),
        ("reviewed_srt_manifest", "sha256"),
    ):
        binding = validated["source_binding"][binding_name]
        assert isinstance(binding, Mapping)
        support_raw = _read_repository_sealed_asset(
            resolved_root,
            _clean_relative_path(binding["repo_path"], label=binding_name),
        )
        if bytes_sha256(support_raw) != binding[sha_key]:
            raise _error(f"{binding_name.upper()}_FILE_HASH_MISMATCH")
    entity = validated["entity_binding"]
    assert isinstance(entity, Mapping)
    entity_raw = _read_repository_sealed_asset(
        resolved_root,
        _clean_relative_path(entity["repo_path"], label="entity_projection"),
    )
    if bytes_sha256(entity_raw) != entity["file_sha256"]:
        raise _error("ENTITY_PROJECTION_FILE_HASH_MISMATCH")
    return DeterministicTextSurfaceAuthorityV1(
        document=validated,
        failed_source_fact_receipt=_validate_failed_receipt(failed_receipt),
        repo_root=resolved_root,
        repo_path=relative,
        file_sha256=bytes_sha256(raw),
        failed_receipt_repo_path=receipt_relative,
        failed_receipt_file_sha256=bytes_sha256(receipt_raw),
    )


def _parse_srt_cues(raw: bytes) -> dict[int, str]:
    if raw.startswith(b"\xef\xbb\xbf"):
        raise _error("REVIEWED_SRT_BOM_FORBIDDEN")
    try:
        text = raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    except UnicodeDecodeError as exc:
        raise _error("REVIEWED_SRT_UTF8_INVALID") from exc
    blocks = re.split(r"\n{2,}", text.strip())
    cues: dict[int, str] = {}
    for block in blocks:
        lines = block.splitlines()
        if len(lines) < 3 or not lines[0].isdigit() or " --> " not in lines[1]:
            raise _error("REVIEWED_SRT_SCHEMA_INVALID")
        index = int(lines[0])
        if index in cues:
            raise _error("REVIEWED_SRT_DUPLICATE_CUE")
        cues[index] = "\n".join(lines[2:])
    if cues != {index: cues[index] for index in range(1, 37) if index in cues} or len(cues) != 36:
        raise _error("REVIEWED_SRT_CUE_INDEX_DRIFT")
    for index, expected in _EXPECTED_CUES.items():
        if cues.get(index) != expected:
            raise _error(f"REVIEWED_SRT_CUE_{index}_TEXT_DRIFT")
    return cues


def render_closed_surface_plan(
    authority: DeterministicTextSurfaceAuthorityV1,
    *,
    reviewed_srt_path: Path,
) -> RenderedTextSurfaces:
    """Render exact UTF-8 surfaces from the one closed reviewed-cue plan."""

    document = validate_deterministic_text_surface_document(
        authority.document,
        failed_source_fact_receipt=authority.failed_source_fact_receipt,
    )
    raw = _read_regular_no_symlink(reviewed_srt_path)
    if bytes_sha256(raw) != REVIEWED_SRT_SHA256:
        raise _error("REVIEWED_SRT_RUNTIME_HASH_MISMATCH")
    cues = _parse_srt_cues(raw)
    projection_path = authority.repo_root / str(document["entity_binding"]["repo_path"])
    reviewed_binding = document["source_binding"]["reviewed_srt"]
    assert isinstance(reviewed_binding, Mapping)
    repository_reviewed_srt_path = authority.repo_root / str(reviewed_binding["repo_path"])
    try:
        projection = load_candidate_entity_projection(
            projection_path=projection_path,
            candidate_id=CANDIDATE_ID,
            reviewed_srt_path=repository_reviewed_srt_path,
        )
        if projection.projection_sha256 != ENTITY_PROJECTION_SHA256:
            raise _error("ENTITY_PROJECTION_RUNTIME_HASH_MISMATCH")
        entity_surface = projection.projected_surface(
            entity_id=ENTITY_ID,
            surface_type="title_cover",
        )
    except CandidateEntityProjectionError as exc:
        raise _error("ENTITY_PROJECTION_RUNTIME_INVALID") from exc
    if entity_surface != ENTITY_SURFACE:
        raise _error("ENTITY_SURFACE_RUNTIME_DRIFT")

    question = "被问有没有跳《夜蝶》的对象"
    # Exact reported utterances come from the bound reviewed cue bytes.  Only
    # connective syntax and Chinese quote glyphs are renderer-owned constants.
    title = f"【李豆沙】{question}，{entity_surface}说“{cues[16]}”，李豆沙接着问“{cues[18]}”"
    hook = (
        f"李豆沙{question}后，讲到自己对{entity_surface}说“{cues[6]}”，"
        f"又说“{cues[12]}”；{entity_surface}说“{cues[16]}”，"
        f"李豆沙接着问“{cues[18]}”，随后说“{cues[30]}”。"
    )
    if (
        title != EXACT_TITLE
        or hook != EXACT_HOOK
        or text_sha256(title) != document["exact_surface_resolution"]["title"]["sha256"]
        or text_sha256(hook) != document["exact_surface_resolution"]["hook"]["sha256"]
        or combined_surface_sha256(title, hook) != EXACT_COMBINED_SHA256
    ):
        raise _error("DETERMINISTIC_RENDER_BYTES_MISMATCH")
    forbidden = ("公主抱", "钓到", "成功", "被奶P赖上了")
    if any(token in title or token in hook for token in forbidden):
        raise _error("FORBIDDEN_CLAIM_RENDERED")
    return RenderedTextSurfaces(title=title, selection_hook=hook)


def consume_deterministic_text_surface_authority(
    authority: DeterministicTextSurfaceAuthorityV1,
    *,
    candidate_id: str,
    source_recording_basename: str,
    source_sha256: str,
    absolute_source_start_ms: int,
    absolute_source_end_ms: int,
    reviewed_srt_path: Path,
    speaker_evidence: object,
    failed_source_fact_review: object,
    original_title: str,
    original_selection_hook: str,
    final_transcript: str,
    clip_context_prompt: str,
    selection_scorecard: object,
    entity_context: object,
) -> dict[str, object]:
    """Validate live bindings and return a deterministic no-provider receipt."""

    if (
        candidate_id != CANDIDATE_ID
        or source_recording_basename != SOURCE_BASENAME
        or source_sha256.removeprefix("sha256:") != SOURCE_SHA256.removeprefix("sha256:")
        or absolute_source_start_ms != SOURCE_START_MS
        or absolute_source_end_ms != SOURCE_END_MS
    ):
        raise _error("RUNTIME_SOURCE_BINDING_MISMATCH")
    if speaker_evidence != EXPECTED_SPEAKER_EVIDENCE:
        raise _error("RUNTIME_UNIFORM_HOST_AUTHORITY_MISMATCH")
    if failed_source_fact_review != authority.failed_source_fact_receipt:
        raise _error("RUNTIME_FAILED_SOURCE_FACT_RECEIPT_MISMATCH")
    if original_title != OLD_TITLE or original_selection_hook != OLD_HOOK:
        raise _error("RUNTIME_OLD_SURFACE_MISMATCH")
    adjudication_inputs = _validate_runtime_adjudication_inputs(
        failed_receipt=authority.failed_source_fact_receipt,
        final_transcript=final_transcript,
        clip_context_prompt=clip_context_prompt,
        selection_scorecard=selection_scorecard,
        entity_context=entity_context,
    )
    rendered = render_closed_surface_plan(authority, reviewed_srt_path=reviewed_srt_path)
    payload: dict[str, object] = {
        "schema_version": CONSUMPTION_SCHEMA_VERSION,
        "status": "VALID",
        "candidate_id": CANDIDATE_ID,
        "original_source_fact_attempt": "FAILED",
        "original_source_fact_reason_code": "CPA_TEXT_REVIEW_INVALID",
        "original_source_fact_receipt_sha256": FAILED_SOURCE_FACT_RECEIPT_SHA256,
        "original_source_fact_receipt": json.loads(
            json.dumps(authority.failed_source_fact_receipt, ensure_ascii=False)
        ),
        "exact_surface_resolution": "VALID",
        "publication_fact_authority": "RESOLVED_EXACT_SURFACE",
        "provider_call_required": False,
        "title": rendered.title,
        "selection_hook": rendered.selection_hook,
        "title_sha256": text_sha256(rendered.title),
        "selection_hook_sha256": text_sha256(rendered.selection_hook),
        "combined_surface_sha256": combined_surface_sha256(rendered.title, rendered.selection_hook),
        "source_sha256": SOURCE_SHA256,
        "absolute_source_start_ms": SOURCE_START_MS,
        "absolute_source_end_ms": SOURCE_END_MS,
        "reviewed_srt_sha256": REVIEWED_SRT_SHA256,
        "entity_projection_sha256": ENTITY_PROJECTION_SHA256,
        "speaker_evidence_sha256": SPEAKER_EVIDENCE_SHA256,
        **adjudication_inputs,
        "policy_sha256": authority.document["policy_sha256"],
        "authority_repo_path": authority.repo_path.as_posix(),
        "authority_file_sha256": authority.file_sha256,
        "authority_sha256": authority.document["authority_sha256"],
    }
    payload["receipt_sha256"] = canonical_sha256(payload)
    return payload
