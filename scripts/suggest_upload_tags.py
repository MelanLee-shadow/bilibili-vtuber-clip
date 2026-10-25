#!/usr/bin/env python3
"""Generate upload tags from the delivered title and complete final subtitles.

The selected profile supplies four base tags, deterministic proper-name/IP
rules and the CPA content prompt.  Registered people and characters use the
deterministic layer.  Content tags may include confirmed game/work names and
specific topics supported by the current clip. Explicit current-title/SRT-bound
entity candidates are individually selected by CPA; unknown proper names stay
unknown rather than being guessed from phonetic resemblance.

Merge order is base tags, configured editorial overrides, deterministic
proper names/IPs, then content tags.  Existing length, duplicate, known-name
and profile limits apply; content tags can use at most six dynamic slots.
The ordinary package/correction callers store the generated result in the
current record.  Generation itself does not edit a published archive;
authorized delivery still validates the current package and tag result.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.autoslice.cpa_model_ladder import TOP_CPA_MODEL, cpa_call_timeout_seconds
from src.autoslice.llm_client import (
    LlmCallError,
    LlmConfig,
    build_llm_call,
    extract_json_object,
    runtime_cpa_command_environment,
)
from src.autoslice.source_fact_review import EDITORIAL_RESEARCH_METHOD_PROVENANCE
from src.autoslice.normal_text_generation_checkpoint import (
    completed_generation, subtitle_dependency,
)

# Channel names, proper-noun mappings, and prompt policy are selected through
# the active profile. Keep these compatibility constants so existing callers
# do not need to know where the policy bytes live.
from src.autoslice.upload_tag_policy import (
    TermRule,
    load_selected_upload_tag_policy,
)

_UPLOAD_TAG_POLICY = load_selected_upload_tag_policy()
BASE_TAGS = _UPLOAD_TAG_POLICY.base_tags
MAX_TAGS_DEFAULT = _UPLOAD_TAG_POLICY.max_tags_default
MAX_DYNAMIC_TAGS = _UPLOAD_TAG_POLICY.max_dynamic_tags
MAX_TAG_CHARS = _UPLOAD_TAG_POLICY.max_tag_chars
MAX_DYNAMIC_TAG_CHARS = _UPLOAD_TAG_POLICY.max_dynamic_tag_chars
TERM_RULES = _UPLOAD_TAG_POLICY.term_rules
IMPORTANT_CONTENT_IP_RULES = _UPLOAD_TAG_POLICY.important_content_ips
_THEME_ALLOWED = set(_UPLOAD_TAG_POLICY.theme_allowed)
_BANNED_CONTENT_TAGS = set(_UPLOAD_TAG_POLICY.banned_content_tags)
CONTENT_PROMPT = _UPLOAD_TAG_POLICY.content_prompt_template

# Keep the normal tag consumer on the current CPA policy lane.  The adapter
# rejects dated GPT-5 routes before credentials/network use; this is an
# explicit GPT-6.1 Sol request so ordinary production calls exercise the same
# current provider policy as the other semantic packaging stages.
CPA_COMMAND = f"bash {ROOT}/scripts/llm_via_cpa.sh {{prompt_file}} {{completion_file}} '{TOP_CPA_MODEL}' medium"

_KNOWN_PROPER_SURFACES: tuple[str, ...] = tuple(
    sorted(
        {
            surface
            for rule in (*TERM_RULES, *IMPORTANT_CONTENT_IP_RULES)
            for surface in rule.tags
        }
        | set(_UPLOAD_TAG_POLICY.known_proper_surfaces_extra),
        key=len,
        reverse=True,
    )
)


@dataclass
class ProperHit:
    tag: str
    rules: list[str] = field(default_factory=list)
    evidence: dict[str, int] = field(default_factory=dict)
    hits: int = 0
    title_hit: bool = False
    notes: list[str] = field(default_factory=list)


def parse_srt_text(srt_path: Path) -> str:
    lines: list[str] = []
    for raw in srt_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.isdigit() or "-->" in line:
            continue
        lines.append(line)
    return "\n".join(lines)


def scan_proper_nouns(
    title: str,
    body: str,
    *,
    term_rules: tuple[TermRule, ...] | None = None,
    important_content_ip_rules: tuple[TermRule, ...] | None = None,
) -> list[ProperHit]:
    # 专名常被字幕跨 cue 截断(实案: "梦\n限大"), 逐行文本会漏; 额外在去空白
    # 连体文本上匹配一次, 每个 pattern 取两种视图的最大命中数(不相加防重复计数)。
    condensed_body = re.sub(r"[\s，。？！—…·]+", "", body)
    merged: dict[str, ProperHit] = {}
    selected_term_rules = TERM_RULES if term_rules is None else term_rules
    selected_ip_rules = (
        IMPORTANT_CONTENT_IP_RULES
        if important_content_ip_rules is None
        else important_content_ip_rules
    )
    for rule in (*selected_term_rules, *selected_ip_rules):
        evidence: dict[str, int] = {}
        title_hit = False
        for pattern in rule.patterns:
            regex = re.compile(pattern, re.IGNORECASE)
            body_hits = regex.findall(body)
            condensed_hits = regex.findall(condensed_body)
            title_hits = regex.findall(title)
            count = max(len(body_hits), len(condensed_hits)) + len(title_hits)
            if count:
                # findall with lookarounds returns matched strings; use pattern as surface key
                surface = next((h for h in (*body_hits, *condensed_hits, *title_hits) if isinstance(h, str) and h), pattern)
                evidence[surface] = evidence.get(surface, 0) + count
                title_hit = title_hit or bool(title_hits)
        total = sum(evidence.values())
        if total < rule.min_hits:
            continue
        for tag in rule.tags:
            hit = merged.setdefault(tag, ProperHit(tag=tag))
            hit.rules.append(rule.name)
            for surface, count in evidence.items():
                hit.evidence[surface] = hit.evidence.get(surface, 0) + count
            hit.hits += total
            hit.title_hit = hit.title_hit or title_hit
            if rule.note and rule.note not in hit.notes:
                hit.notes.append(rule.note)
    ranked = sorted(merged.values(), key=lambda h: (h.title_hit, h.hits), reverse=True)
    return ranked


def _sha256_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


_NORMAL_ENTITY_PROJECTION_SCHEMA = "normal-source-entity-candidates.v1"
_NORMAL_ENTITY_SIDECAR_MAX_BYTES = 512 * 1024
_NORMAL_ENTITY_SIDECAR_SUFFIXES = (".publish.json", ".record.json")
_NORMAL_ENTITY_CONTAINER_KEYS = (
    "publish_staging",
    "source_fact_review",
    "story_contract",
    "selection_scorecard",
    "editorial_research",
    "candidate_projection",
    "current_binding",
)


def _hash_body(value: object) -> str:
    """Return the comparable body of a sha256 value, or an empty string."""

    text = str(value or "").strip().lower()
    if text.startswith("sha256:"):
        text = text.split(":", 1)[1]
    return text if re.fullmatch(r"[0-9a-f]{64}", text) else ""


def _hash_matches(value: object, actual: str) -> bool:
    expected = _hash_body(value)
    return bool(expected) and expected == _hash_body(actual)


def _sidecar_sections(sidecar: Mapping[str, object]):
    """Yield only known normal-record sections, with a bounded depth.

    The normal consumer must never turn a filesystem/report scan into entity
    authority.  Keeping this walk to the existing publish/record fields makes
    the sidecar an explicit projection interface rather than a second ledger.
    """

    queue: list[tuple[Mapping[str, object], str, int]] = [(sidecar, "root", 0)]
    seen: set[int] = set()
    while queue:
        section, path, depth = queue.pop(0)
        marker = id(section)
        if marker in seen:
            continue
        seen.add(marker)
        yield section, path
        if depth >= 3:
            continue
        for key in _NORMAL_ENTITY_CONTAINER_KEYS:
            child = section.get(key)
            if isinstance(child, Mapping):
                queue.append((child, f"{path}.{key}", depth + 1))
        for key in ("applications", "candidate_applications"):
            rows = section.get(key)
            if isinstance(rows, list):
                for index, row in enumerate(rows[:8]):
                    if isinstance(row, Mapping):
                        queue.append((row, f"{path}.{key}[{index}]", depth + 1))


def _bounded_research_receipt(value: object) -> dict[str, object] | None:
    """Project research method metadata without copying a report wholesale."""

    if not isinstance(value, Mapping):
        return None
    receipt: dict[str, object] = {}
    for key in (
        "schema",
        "schema_version",
        "path",
        "sha256",
        "source_path",
        "source_sha256",
        "version",
        "source_kind",
        "asset_key",
        "applicable",
        "applicability",
        "applicable_constraints",
        "inapplicable_reason",
        "status",
        "scope",
        "method",
        "input_sha256",
        "research_method_sources",
    ):
        if key not in value or value[key] is None:
            continue
        item = value[key]
        if isinstance(item, (str, int, float, bool)):
            receipt[key] = item
        elif key in {"applicable_constraints"} and isinstance(item, list):
            receipt[key] = [str(row)[:240] for row in item[:8] if str(row).strip()]
        elif key == "research_method_sources" and isinstance(item, list):
            receipt[key] = [
                {
                    field: str(row[field]).strip()[:1024]
                    for field in ("id", "schema", "version", "path", "sha256", "url", "role")
                    if row.get(field) is not None and str(row[field]).strip()
                }
                for row in item[:6]
                if isinstance(row, Mapping)
            ]
    projection = value.get("candidate_projection")
    if isinstance(projection, Mapping):
        bounded_projection: dict[str, object] = {}
        for key in (
            "status",
            "source_kind",
            "path",
            "source_sha256",
            "version",
            "applicability",
            "inapplicable_reason",
        ):
            if key in projection and projection[key] is not None:
                bounded_projection[key] = projection[key]
        if bounded_projection:
            receipt["candidate_projection"] = bounded_projection
    return receipt or None


def _sidecar_bound_title(sidecar: Mapping[str, object]) -> str:
    for section, _path in _sidecar_sections(sidecar):
        for key in ("title", "final_title"):
            value = section.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _sidecar_declared_hashes(
    sidecar: Mapping[str, object], *, kind: str
) -> list[object]:
    keys = (
        ("title_sha256", "title_hash")
        if kind == "title"
        else ("subtitle_sha256", "srt_sha256", "subtitle_hash", "srt_hash")
    )
    values: list[object] = []
    for section, _path in _sidecar_sections(sidecar):
        for key in keys:
            if key in section and section[key] is not None:
                values.append(section[key])
        artifact_hashes = section.get("artifact_hashes")
        if isinstance(artifact_hashes, Mapping):
            for key in keys:
                if key in artifact_hashes and artifact_hashes[key] is not None:
                    values.append(artifact_hashes[key])
        input_binding = section.get("input_binding")
        if isinstance(input_binding, Mapping):
            for key in keys:
                if key in input_binding and input_binding[key] is not None:
                    values.append(input_binding[key])
    return values


def _raw_sidecar_entity_candidates(
    sidecar: Mapping[str, object], *, sidecar_path: Path, sidecar_sha256: str
) -> tuple[list[dict[str, object]], dict[str, object] | None]:
    """Collect explicit candidate projections from known record fields only."""

    default_source = {
        "path": str(sidecar_path),
        "sha256": sidecar_sha256,
        "source_kind": "hash_bound_normal_sidecar",
    }
    rows: list[dict[str, object]] = []
    research_receipt: dict[str, object] | None = None
    for section, _path in _sidecar_sections(sidecar):
        candidate_value = None
        for key in ("source_entity_candidates", "entity_candidates"):
            value = section.get(key)
            if isinstance(value, list):
                candidate_value = value
                break
        if isinstance(section.get("editorial_research"), Mapping):
            research_receipt = research_receipt or _bounded_research_receipt(
                section["editorial_research"]
            )
        elif "schema" in section and (
            "editorial" in str(section.get("schema") or "").lower()
            or "editorial" in str(section.get("version") or "").lower()
        ):
            research_receipt = research_receipt or _bounded_research_receipt(section)
        if not isinstance(candidate_value, list):
            continue
        inherited_applicability = str(section.get("applicability") or "").strip()
        inherited_basis = str(section.get("research_basis") or "").strip()
        for raw in candidate_value[:12]:
            if not isinstance(raw, Mapping):
                rows.append(raw)  # normalization records the bounded warning
                continue
            row = dict(raw)
            if not isinstance(row.get("source"), Mapping):
                row["source"] = dict(default_source)
            if inherited_applicability and not row.get("applicability"):
                row["applicability"] = inherited_applicability
            if inherited_basis and not row.get("research_basis"):
                row["research_basis"] = inherited_basis
            rows.append(row)
    return rows, research_receipt


def _load_normal_source_entity_projection(
    *, title: str, srt_path: Path | None
) -> tuple[list[dict[str, object]], dict[str, object], dict[str, object] | None, list[str]]:
    """Load a current-title/SRT-bound candidate projection from normal sidecars.

    ``publish.json`` is written by normal staging before the existing producer
    calls ``generate_upload_tags``; ``record.json`` supports later reprocessing.
    Both files are same-stem and are checked byte-for-byte against the current
    call.  Missing or drifting sidecars are disclosed and never guessed around.
    """

    title_sha256 = _sha256_text(title)
    srt_sha256 = _sha256_file(srt_path) if srt_path and srt_path.is_file() else None
    base_receipt: dict[str, object] = {
        "schema_version": _NORMAL_ENTITY_PROJECTION_SCHEMA,
        "status": "UNAVAILABLE",
        "source_kind": "same_stem_publish_or_record",
        "sidecar_path": None,
        "sidecar_sha256": None,
        "title_sha256": title_sha256,
        "srt_sha256": srt_sha256,
        "candidate_count": 0,
    }
    warnings: list[str] = []
    if srt_path is None:
        base_receipt["reason"] = "no_final_srt"
        return [], base_receipt, None, warnings
    fallback_receipt: dict[str, object] | None = None
    for suffix in _NORMAL_ENTITY_SIDECAR_SUFFIXES:
        sidecar_path = srt_path.with_suffix(suffix)
        if not sidecar_path.exists():
            continue
        if sidecar_path.is_symlink() or not sidecar_path.is_file():
            warnings.append(f"normal-entity-sidecar-rejected:{sidecar_path.name}:not-regular")
            continue
        try:
            if sidecar_path.stat().st_size > _NORMAL_ENTITY_SIDECAR_MAX_BYTES:
                warnings.append(f"normal-entity-sidecar-rejected:{sidecar_path.name}:too-large")
                continue
            raw_bytes = sidecar_path.read_bytes()
            sidecar = json.loads(raw_bytes.decode("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            warnings.append(f"normal-entity-sidecar-rejected:{sidecar_path.name}:{type(exc).__name__}")
            continue
        if not isinstance(sidecar, Mapping):
            warnings.append(f"normal-entity-sidecar-rejected:{sidecar_path.name}:not-object")
            continue
        sidecar_sha256 = "sha256:" + hashlib.sha256(raw_bytes).hexdigest()
        bound_title = _sidecar_bound_title(sidecar)
        title_hashes = _sidecar_declared_hashes(sidecar, kind="title")
        srt_hashes = _sidecar_declared_hashes(sidecar, kind="srt")
        if bound_title and bound_title != title:
            warnings.append(f"normal-entity-sidecar-rejected:{sidecar_path.name}:title-drift")
            continue
        if title_hashes and not all(_hash_matches(value, title_sha256) for value in title_hashes):
            warnings.append(f"normal-entity-sidecar-rejected:{sidecar_path.name}:title-hash-drift")
            continue
        if srt_hashes and (
            srt_sha256 is None
            or not all(_hash_matches(value, srt_sha256) for value in srt_hashes)
        ):
            warnings.append(f"normal-entity-sidecar-rejected:{sidecar_path.name}:srt-hash-drift")
            continue
        if (not bound_title and not title_hashes) or not srt_hashes:
            warnings.append(f"normal-entity-sidecar-rejected:{sidecar_path.name}:unbound")
            continue
        rows, research_receipt = _raw_sidecar_entity_candidates(
            sidecar, sidecar_path=sidecar_path, sidecar_sha256=sidecar_sha256
        )
        receipt = dict(base_receipt)
        receipt.update(
            {
                "status": "BOUND_NO_CANDIDATES" if not rows else "BOUND",
                "sidecar_path": str(sidecar_path),
                "sidecar_sha256": sidecar_sha256,
                "bound_title": bool(bound_title),
                "declared_title_hashes": [str(value) for value in title_hashes[:4]],
                "declared_srt_hashes": [str(value) for value in srt_hashes[:4]],
            }
        )
        if not rows:
            fallback_receipt = receipt
            continue
        return rows, receipt, research_receipt, warnings
    if fallback_receipt is not None:
        return [], fallback_receipt, None, warnings
    base_receipt["reason"] = "no-current-bound-sidecar"
    return [], base_receipt, None, warnings


def _deployed_editorial_method_receipt() -> dict[str, object] | None:
    """Read the already-deployed generic method when no record sidecar exists."""

    bindings: list[dict[str, object]] = []
    try:
        from src.autoslice.surface_canon import CHANNEL_PROFILE

        for asset_key in ("title_style", "upload_tag_policy"):
            path = CHANNEL_PROFILE.asset_file(asset_key, repo_root=ROOT)
            data = path.read_bytes()
            bindings.append(
                {
                    "asset_key": asset_key,
                    "path": str(path.relative_to(ROOT)),
                    "sha256": "sha256:" + hashlib.sha256(data).hexdigest(),
                    "bytes": len(data),
                }
            )
    except (OSError, KeyError, ValueError):
        return None
    binding_payload = json.dumps(
        bindings, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    binding_sha256 = "sha256:" + hashlib.sha256(binding_payload).hexdigest()
    return {
        "schema": "editorial-research-consumption.v1",
        "schema_version": "profile-assets-fallback.v1",
        "version": "title-tag-editorial-method.v1",
        "source_kind": "deployed_profile_assets",
        "path": "profiles/lidousha/profile.json#assets.title_style,assets.upload_tag_policy",
        "sha256": binding_sha256,
        "source_sha256": binding_sha256,
        "asset_key": "title_style+upload_tag_policy",
        "asset_bindings": bindings,
        "method": "normal_title_tag_editorial_selection",
        "applicable": True,
        "applicability": (
            "The deployed profile title-style and upload-tag policy provide the bounded "
            "normal title/tag editorial method for every ordinary record."
        ),
        "applicable_constraints": [
            "Use final SRT and same-clip context as factual authority.",
            "Use source-specific candidates only when explicitly hash-bound.",
            "Do not read undeployed reports or invent proper-noun candidates.",
        ],
        "research_method_sources": [dict(row) for row in EDITORIAL_RESEARCH_METHOD_PROVENANCE],
        "inapplicable_reason": (
            "No source-specific candidate application is bound to this normal call; "
            "the generic editorial method remains applicable."
        ),
        "candidate_projection": {
            "status": "INAPPLICABLE",
            "source_kind": "runtime_record_field",
            "path": "normal-call.source-entity-sidecar",
            "applicability": "No source-specific candidate projection was supplied.",
            "inapplicable_reason": (
                "The normal call has no current same-stem candidate sidecar; no report "
                "or unbound name list is read as a fallback."
            ),
        },
        "status": "APPLICABLE",
        "scope": "normal_title_tags",
    }


def _editorial_research_prompt_block(research: Mapping[str, object] | None) -> str:
    if not research:
        return ""
    return (
        "\n普通标题/tag 编辑方法回执（仅约束候选比较和适用范围，不是事实或标签清单）：\n"
        + json.dumps(dict(research), ensure_ascii=False, sort_keys=True)
        + "\n最终字幕和当前标题是事实 authority。请据适用范围与 inapplicable_reason 记录取舍；"
        "不要把研究建议词或 profile 资产名机械加入 tags，不要读取未部署 reports。\n"
    )


def _normalize_source_entity_candidates(
    candidates: object,
    *,
    title: str,
    body: str,
) -> tuple[list[dict[str, object]], list[str]]:
    """Validate exact, source-bound entity candidates for the CPA choice layer.

    The caller supplies the current entity authority.  A candidate is usable
    only when its canonical surface is present literally in the current title
    or final SRT (whitespace-free matching is limited to subtitle cue breaks),
    and it is explicitly marked confirmed/verified.  No phonetic or fuzzy
    expansion happens here.
    """

    if candidates is None:
        return [], []
    if not isinstance(candidates, (list, tuple)):
        return [], ["source-entity-candidates-invalid:not-a-list"]
    condensed_body = re.sub(r"\s+", "", body)
    normalized: list[dict[str, object]] = []
    warnings: list[str] = []
    seen: set[str] = set()
    for index, raw in enumerate(candidates):
        if not isinstance(raw, dict):
            warnings.append(f"source-entity-candidate-{index}-invalid:row")
            continue
        tag = str(raw.get("tag") or "").strip()
        status = str(raw.get("status") or "").strip().upper()
        role = str(raw.get("role") or "").strip()
        editorial_role = str(
            raw.get("editorial_role") or raw.get("content_role") or ""
        ).strip()
        surfaces = raw.get("surfaces")
        if surfaces is None:
            surfaces = [raw.get("surface")]
        if not isinstance(surfaces, (list, tuple)):
            surfaces = []
        surfaces = [str(surface).strip() for surface in surfaces if str(surface).strip()]
        if not _valid_tag(tag) or status not in {"CONFIRMED", "VERIFIED"} or not surfaces:
            warnings.append(f"source-entity-candidate-{index}-invalid:tag-status-surface")
            continue
        if tag.casefold() in seen:
            continue
        matched = [
            surface
            for surface in surfaces
            if surface in title or surface in body or surface.replace(" ", "") in condensed_body
        ]
        if not matched:
            warnings.append(f"source-entity-candidate-{tag}-not-in-final-text")
            continue
        evidence = raw.get("evidence")
        evidence_rows = (
            [str(row).strip() for row in evidence if str(row).strip()]
            if isinstance(evidence, (list, tuple))
            else []
        )
        source = raw.get("source")
        normalized_source = dict(source) if isinstance(source, dict) else {}
        research_basis = raw.get("research_basis")
        if isinstance(research_basis, (list, tuple)):
            research_basis = [
                str(row).strip() for row in research_basis[:4] if str(row).strip()
            ]
        elif research_basis is not None:
            research_basis = str(research_basis).strip()
        normalized.append(
            {
                "tag": tag,
                "role": role,
                "editorial_role": editorial_role,
                "status": status,
                "surfaces": surfaces,
                "matched_surfaces": matched,
                "title_hit": any(surface in title for surface in matched),
                "evidence": evidence_rows,
                "source": normalized_source,
                "research_basis": research_basis,
                "applicability": str(raw.get("applicability") or "").strip(),
                "inapplicable_reason": str(raw.get("inapplicable_reason") or "").strip(),
                "why": str(raw.get("why") or "").strip(),
            }
        )
        seen.add(tag.casefold())
    return normalized, warnings


def _source_entity_prompt_block(candidates: list[dict[str, object]]) -> str:
    if not candidates:
        return ""
    return (
        "\n已由当前最终字幕与 entity authority 核实的专名候选（只可逐项选择，不可改写、补全或猜音）：\n"
        + json.dumps(candidates, ensure_ascii=False, sort_keys=True)
        + "\n对每个候选在 entity_decisions 中给出 action=INCLUDE 或 EXCLUDE、content_role、why 和逐字 evidence。"
        "content_role 请区分 primary_content、necessary_context、incidental_mention；"
        "作者/作品只要是当前正文有意义的作品或主题检索入口，primary_content 或 necessary_context 都可 INCLUDE；"
        "不要求作者必须展开传记，也不要求它是理解故事不可缺的条件，但必须说明它与本片主题/作品分享的关系；"
        "只有偶然提及、旁逸玩笑或与当前内容入口无关的名字才 EXCLUDE。"
        "作品若是本片核心对象或必要背景可 INCLUDE；已存在于原标签的作品仍只记录取舍，不重复添加。"
        "普通 tags 仍禁止输出人名/作品名，专名决定必须放在 entity_decisions。\n"
    )


def _valid_tag(tag: str) -> bool:
    if not tag or len(tag) > MAX_TAG_CHARS:
        return False
    return not any(ch in tag for ch in ",，\n\t")


def llm_content_tags(
    title: str,
    srt_text: str,
    existing: list[str],
    timeout: float,
    llm_call=None,
    source_entity_candidates: list[dict[str, object]] | None = None,
    entity_decisions_out: list[dict[str, object]] | None = None,
    editorial_research: Mapping[str, object] | None = None,
    generation_srt_path: Path | None = None,
) -> tuple[list[dict], list[str]]:
    """Returns (content_tags, warnings); LLM failure degrades to ([], [reason])."""
    if not srt_text.strip():
        srt_text = "（本条没有字幕存档，只有标题。只在标题本身能支撑时出词，出不了就给空列表。）"
    source_entity_candidates = source_entity_candidates or []
    prompt = (
        CONTENT_PROMPT.format(existing_tags="、".join(existing), title=title, srt_text=srt_text)
        + _editorial_research_prompt_block(editorial_research)
        + _source_entity_prompt_block(source_entity_candidates)
    )
    try:
        runtime_root = os.environ.get("AUTOSLICE_BASE") or None
        call = llm_call
        if call is None:
            child_env = (
                runtime_cpa_command_environment(Path(runtime_root))
                if runtime_root else os.environ.copy()
            )
            call = build_llm_call(LlmConfig(
                transport="command", command_template=CPA_COMMAND, timeout_seconds=max(timeout, cpa_call_timeout_seconds()),
                runtime_root=runtime_root, command_child_env=child_env,
            ))
            # Bind the same frozen environment actually supplied to the command,
            # without exposing credentials or copying arbitrary adapter state.
            endpoint = child_env.get("CPA_BASE_URL", "").strip().rstrip("/")
            call.provider_runtime_binding = {  # type: ignore[attr-defined]
                "provider_transport": "cpa_command",
                "provider_endpoint_sha256": _sha256_text(endpoint) if endpoint else "",
            }
        from src.autoslice.structured_output import response_contract
        with response_contract():
            completion = (
                completed_generation(
                    artifact_root=generation_srt_path.parent, candidate_id=generation_srt_path.stem,
                    lane="tags", attempt=1, prompt=prompt, call=call,
                    dependencies=lambda: subtitle_dependency(generation_srt_path),
                )
                if generation_srt_path is not None else call(prompt)
            )
        payload = extract_json_object(completion)
    except (LlmCallError, ValueError) as exc:
        if entity_decisions_out is not None:
            entity_decisions_out.extend(
                {
                    "tag": row["tag"],
                    "action": "UNDECIDED",
                    "why": f"CPA 内容层失败：{exc}",
                    "evidence": [],
                    "candidate_status": row.get("status"),
                    "candidate_role": row.get("role"),
                    "content_role": row.get("editorial_role"),
                }
                for row in source_entity_candidates
            )
        return [], [f"LLM内容层失败(降级为仅专名): {exc}"]
    warnings: list[str] = []
    accepted: list[dict] = []
    seen = {tag.casefold() for tag in existing}
    candidate_by_tag = {
        str(row["tag"]).casefold(): row for row in source_entity_candidates
    }
    decisions_by_tag: dict[str, dict[str, object]] = {}
    raw_decisions = payload.get("entity_decisions")
    if isinstance(raw_decisions, list):
        for item in raw_decisions:
            if not isinstance(item, dict):
                continue
            tag = str(item.get("tag") or "").strip()
            key = tag.casefold()
            candidate = candidate_by_tag.get(key)
            if candidate is None or tag != str(candidate["tag"]):
                warnings.append(f"弃(未核实的 entity_decision): {tag!r}")
                continue
            action = str(item.get("action") or "").strip().upper()
            why = str(item.get("why") or "").strip()
            content_role = str(
                item.get("content_role")
                or item.get("editorial_role")
                or candidate.get("editorial_role")
                or ""
            ).strip()
            evidence = item.get("evidence")
            evidence_rows = (
                [str(row).strip() for row in evidence if str(row).strip()]
                if isinstance(evidence, list)
                else []
            )
            if action not in {"INCLUDE", "EXCLUDE"} or not why:
                warnings.append(f"弃(entity_decision格式): {tag!r}")
                continue
            bound_evidence = all(
                row in title or row in srt_text or row.replace(" ", "") in re.sub(r"\s+", "", srt_text)
                for row in evidence_rows
            )
            if action == "INCLUDE" and (not evidence_rows or not bound_evidence):
                warnings.append(f"弃(entity_decision无逐字证据): {tag!r}")
                continue
            decisions_by_tag[key] = {
                "tag": str(candidate["tag"]),
                "action": action,
                "why": why,
                "evidence": evidence_rows,
                "candidate_status": candidate.get("status"),
                "candidate_role": candidate.get("role"),
                "content_role": content_role,
            }
    if entity_decisions_out is not None:
        for candidate in source_entity_candidates:
            key = str(candidate["tag"]).casefold()
            decision = decisions_by_tag.get(key)
            if decision is None:
                decision = {
                    "tag": candidate["tag"],
                    "action": "UNDECIDED",
                    "why": "CPA 未返回该候选的逐项决定",
                    "evidence": [],
                    "candidate_status": candidate.get("status"),
                    "candidate_role": candidate.get("role"),
                    "content_role": candidate.get("editorial_role"),
                }
            entity_decisions_out.append(decision)
    for item in payload.get("tags") or []:
        if not isinstance(item, dict):
            continue
        tag = str(item.get("tag") or "").strip().strip("#＃")
        why = str(item.get("why") or "").strip()
        if not _valid_tag(tag) or len(tag) > MAX_DYNAMIC_TAG_CHARS:
            warnings.append(f"弃(格式/长度): {tag!r}")
            continue
        if tag in _BANNED_CONTENT_TAGS:
            warnings.append(f"弃(维护者硬毙: 过专一没人搜): {tag!r}")
            continue
        if tag.casefold() in seen:
            continue
        if tag not in _THEME_ALLOWED and any(
            surface.casefold() in tag.casefold()
            for surface in (
                *_KNOWN_PROPER_SURFACES,
                *(str(row["tag"]) for row in source_entity_candidates),
            )
            if len(surface) >= 2
        ):
            warnings.append(f"弃(疑似专名, 专名只走确定层): {tag!r}")
            continue
        seen.add(tag.casefold())
        accepted.append({"tag": tag, "why": why})
        if len(accepted) >= MAX_DYNAMIC_TAGS:
            break
    return accepted, warnings


def _accepted_source_entity_hits(
    candidates: list[dict[str, object]], decisions: list[dict[str, object]]
) -> list[ProperHit]:
    candidate_by_tag = {str(row["tag"]).casefold(): row for row in candidates}
    hits: list[ProperHit] = []
    for decision in decisions:
        if str(decision.get("action") or "").upper() != "INCLUDE":
            continue
        tag = str(decision.get("tag") or "").strip()
        candidate = candidate_by_tag.get(tag.casefold())
        if candidate is None or tag != str(candidate["tag"]):
            continue
        evidence_rows = [str(row) for row in decision.get("evidence") or []]
        hits.append(
            ProperHit(
                tag=tag,
                rules=["source-entity-cpa"],
                evidence={row: 1 for row in evidence_rows},
                hits=len(evidence_rows),
                title_hit=bool(candidate.get("title_hit")),
                notes=[
                    str(decision.get("why") or ""),
                    f"role={candidate.get('role') or 'unspecified'}",
                    f"content_role={decision.get('content_role') or candidate.get('editorial_role') or 'unspecified'}",
                ],
            )
        )
    return hits


def merge_tags(
    base: tuple[str, ...],
    proper: list[ProperHit],
    content: list[dict],
    max_total: int,
    *,
    max_dynamic: int = MAX_DYNAMIC_TAGS,
) -> list[str]:
    final: list[str] = []
    seen: set[str] = set()
    effective_limit = min(max_total, len(base) + max_dynamic)
    for tag in list(base) + [h.tag for h in proper] + [c["tag"] for c in content]:
        key = tag.casefold()
        if key in seen or not _valid_tag(tag):
            continue
        seen.add(key)
        final.append(tag)
        if len(final) >= effective_limit:
            break
    return final


def suggest_for_slice(
    slice_id: str,
    title: str,
    srt_path: Path | None,
    *,
    bvid: str = "",
    use_llm: bool = True,
    max_tags: int = MAX_TAGS_DEFAULT,
    timeout: float = cpa_call_timeout_seconds(),
    suppress_tags: dict[str, str] | None = None,
    add_tags: dict[str, str] | None = None,
    important_content_ip_rules: tuple[TermRule, ...] | None = None,
    source_entity_candidates: list[dict[str, object]] | tuple[dict[str, object], ...] | None = None,
    existing_tags: list[str] | tuple[str, ...] | None = None,
    llm_call=None,
) -> dict:
    """suppress_tags/add_tags: 人工裁定通道 {tag: 理由}。

 用于成品字幕尚未修复、但 公开规则已裁定事实的场合(例: 03 全片"安晚"=
 大N老师误听且安晚未出场 → suppress 安晚awa + add 南町)。裁定记录进
 输出; 字幕修复换源后应去掉裁定重算。
 """
    suppress_tags = suppress_tags or {}
    add_tags = add_tags or {}
    # 早期已发布切片可能没有字幕存档(成品字幕只烧在视频里) — title-only 模式:
    # 专名层只扫标题, LLM 层被明确告知没有字幕、宁缺毋滥。
    srt_text = parse_srt_text(srt_path) if srt_path else ""
    auto_entity_rows, entity_projection, editorial_research, projection_warnings = (
        _load_normal_source_entity_projection(title=title, srt_path=srt_path)
    )
    explicit_entity_rows = list(source_entity_candidates or [])
    combined_entity_rows = explicit_entity_rows + auto_entity_rows
    normalized_entities, entity_warnings = _normalize_source_entity_candidates(
        combined_entity_rows,
        title=title,
        body=srt_text,
    )
    if editorial_research is None:
        editorial_research = _deployed_editorial_method_receipt()
    entity_projection = dict(entity_projection)
    entity_projection["candidate_count"] = len(normalized_entities)
    if normalized_entities and entity_projection.get("status") == "BOUND":
        entity_projection["status"] = "APPLIED"
    active_ip_rules = (
        IMPORTANT_CONTENT_IP_RULES
        if important_content_ip_rules is None
        else important_content_ip_rules
    )
    proper = scan_proper_nouns(
        title,
        srt_text,
        important_content_ip_rules=active_ip_rules,
    )
    overridden = [h.tag for h in proper if h.tag in suppress_tags]
    proper = [h for h in proper if h.tag not in suppress_tags]
    # 人工裁定的补充 tag 排在专名最前(人工判断优先于统计排序)。
    proper = [
        ProperHit(tag=tag, rules=["维护者-override"], notes=[reason], title_hit=True)
        for tag, reason in add_tags.items()
        if tag not in {h.tag for h in proper}
    ] + proper
    base_tags = tuple(existing_tags) if existing_tags is not None else BASE_TAGS
    existing = list(base_tags) + [h.tag for h in proper]
    content: list[dict] = []
    warnings: list[str] = []
    entity_decisions: list[dict[str, object]] = []
    if use_llm:
        content, warnings = llm_content_tags(
            title,
            srt_text,
            existing,
            timeout,
            llm_call=llm_call,
            source_entity_candidates=normalized_entities,
            entity_decisions_out=entity_decisions,
            editorial_research=editorial_research,
            generation_srt_path=srt_path,
        )
    elif normalized_entities:
        entity_decisions = [
            {
                "tag": row["tag"],
                "action": "UNDECIDED",
                "why": "LLM内容层关闭，未进行专名相关性裁决",
                "evidence": [],
                "candidate_status": row.get("status"),
                "candidate_role": row.get("role"),
                "content_role": row.get("editorial_role"),
            }
            for row in normalized_entities
        ]
    entity_proper = _accepted_source_entity_hits(normalized_entities, entity_decisions)
    proper = entity_proper + proper
    warnings.extend(projection_warnings)
    warnings.extend(entity_warnings)
    for tag in overridden:
        warnings.append(f"人工裁定移除: {tag!r} — {suppress_tags[tag]}")
    final = merge_tags(base_tags, proper, content, max_tags)
    return {
        "id": slice_id,
        "bvid": bvid,
        "title": title,
        "srt": str(srt_path) if srt_path else "",
        "base_tags": list(base_tags),
        "proper_noun_tags": [
            {
                "tag": h.tag,
                "rules": h.rules,
                "hits": h.hits,
                "title_hit": h.title_hit,
                "evidence": h.evidence,
                "notes": h.notes,
            }
            for h in proper
        ],
        "important_content_ips": [
            h.tag
            for h in proper
            if any(
                rule_name in {rule.name for rule in active_ip_rules}
                for rule_name in h.rules
            )
        ],
        "source_entity_candidates": normalized_entities,
        "source_entity_decisions": entity_decisions,
        "source_entity_tags": [hit.tag for hit in entity_proper],
        "source_entity_candidate_projection": entity_projection,
        "editorial_research_consumption": editorial_research,
        "content_tags": content,
        "warnings": warnings,
        "overrides": {"suppressed": suppress_tags, "added": add_tags},
        "final_tags": final,
        "final_tag_line": ",".join(final),
        "overflow_tags": [h.tag for h in proper if h.tag not in final]
        + [c["tag"] for c in content if c["tag"] not in final],
    }


ENGINE_VERSION = "suggest-upload-tags.v2"


def generate_upload_tags(
    title: str,
    srt_path: Path | None,
    *,
    use_llm: bool = True,
    llm_call=None,
    max_tags: int = MAX_TAGS_DEFAULT,
    timeout: float = cpa_call_timeout_seconds(),
    source_entity_candidates: list[dict[str, object]] | tuple[dict[str, object], ...] | None = None,
    existing_tags: list[str] | tuple[str, ...] | None = None,
) -> dict:
    """流水线入口(produce_slice_package / apply_subtitle_correction 用)。

    针对成品标题+成品字幕生成 record.json 可存的 tag 块。生成失败返回结构化
    状态而不 raise；调用方保留失败证据。新稿发布门会拒绝明确跳过或失败的内容
    审查且只剩基础标签的结果，不能把基础位回退当作完整内容标签验收。status:
      OK        — 专名层+LLM 内容层齐全
      OK_NO_LLM — LLM 内容层失败, 仅专名层(降级可用)
      FAILED    — 引擎级失败, final_tags 为空
    """
    try:
        srt_sha256 = _sha256_file(srt_path) if srt_path else None
        result = suggest_for_slice(
            "package", title, srt_path, use_llm=use_llm, max_tags=max_tags,
            timeout=timeout, llm_call=llm_call,
            source_entity_candidates=source_entity_candidates,
            existing_tags=existing_tags,
        )
        if srt_path and _sha256_file(srt_path) != srt_sha256:
            raise ValueError("CURRENT_TAG_SUBTITLE_CHANGED_DURING_GENERATION")
        degraded = any("LLM内容层失败" in w for w in result["warnings"])
        return {
            "engine": ENGINE_VERSION,
            "status": "OK_NO_LLM" if (use_llm and degraded) else "OK",
            "content_review_status": (
                "FAILED" if degraded else "COMPLETED" if use_llm else "NOT_REQUESTED"
            ),
            "final_tags": result["final_tags"],
            "final_tag_line": result["final_tag_line"],
            "proper_noun_tags": result["proper_noun_tags"],
            "important_content_ips": result["important_content_ips"],
            "content_tags": result["content_tags"],
            "source_entity_candidates": result["source_entity_candidates"],
            "source_entity_decisions": result["source_entity_decisions"],
            "source_entity_tags": result["source_entity_tags"],
            "source_entity_candidate_projection": result["source_entity_candidate_projection"],
            "editorial_research_consumption": result["editorial_research_consumption"],
            "input_binding": {
                "title_sha256": _sha256_text(title),
                "srt_path": str(srt_path) if srt_path else "",
                "srt_sha256": srt_sha256,
                "existing_tags": list(existing_tags) if existing_tags is not None else list(BASE_TAGS),
            },
            "warnings": result["warnings"],
        }
    except Exception as exc:  # noqa: BLE001 — 交付路径上的兜底边界
        return {
            "engine": ENGINE_VERSION,
            "status": "FAILED",
            "error": f"{type(exc).__name__}: {exc}",
            "final_tags": [],
            "final_tag_line": "",
        }


def render_markdown(results: list[dict]) -> str:
    lines = [
        '# 切片 tag 建议 — 公开规则口径',
        "",
        f"- 基础位({len(BASE_TAGS)} 个): `{','.join(BASE_TAGS)}`",
        "- 专名层=确定性规则(glossary/roster, 只出可搜索正主名), 内容层=CPA LLM(贴内容×通用可搜); 专名绝不由 LLM 产出。",
        f"- 封顶 {MAX_TAGS_DEFAULT} 个: 基础位占 {len(BASE_TAGS)}, 内容位最多 {MAX_DYNAMIC_TAGS} 个。",
        "- tag 以最终成品字幕为准; 字幕待修条目的人工裁定见各条「人工裁定」标注, 换源后去裁定重算。",
        "",
    ]
    for r in results:
        lines.append(f"## {r['id']} {r['bvid']} {r['title']}")
        lines.append("")
        if r["proper_noun_tags"]:
            lines.append("**专名层**:")
            for h in r["proper_noun_tags"]:
                ev = " ".join(f"{s}×{c}" for s, c in h["evidence"].items())
                extra = f"（{'; '.join(h['notes'])}）" if h["notes"] else ""
                star = "★标题命中 " if h["title_hit"] else ""
                lines.append(f"- `{h['tag']}` — {star}{ev}{extra}")
        else:
            lines.append("**专名层**: 无命中")
        lines.append("")
        if r["content_tags"]:
            lines.append("**内容层(LLM)**:")
            for c in r["content_tags"]:
                lines.append(f"- `{c['tag']}` — {c['why']}")
        if r["warnings"]:
            lines.append("")
            lines.append("**告警**: " + "; ".join(r["warnings"]))
        lines.append("")
        lines.append(f"**合并后(≤{MAX_TAGS_DEFAULT})**: `{r['final_tag_line']}`")
        if r["overflow_tags"]:
            lines.append(f"　被挤掉: {', '.join(r['overflow_tags'])}")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch", required=True, help="JSON: [{id,title,srt,bvid?}, ...]")
    parser.add_argument("--out-json", default="")
    parser.add_argument("--out-md", default="")
    parser.add_argument("--no-llm", action="store_true", help="仅专名层(离线/测试)")
    parser.add_argument("--max-tags", type=int, default=MAX_TAGS_DEFAULT)
    parser.add_argument("--timeout", type=float, default=cpa_call_timeout_seconds())
    args = parser.parse_args()

    entries = json.loads(Path(args.batch).read_text(encoding="utf-8"))
    results = []
    for entry in entries:
        result = suggest_for_slice(
            str(entry["id"]),
            entry["title"],
            Path(entry["srt"]) if entry.get("srt") else None,
            bvid=entry.get("bvid", ""),
            use_llm=not args.no_llm,
            max_tags=args.max_tags,
            timeout=args.timeout,
            suppress_tags=entry.get("suppress_tags"),
            add_tags=entry.get("add_tags"),
            source_entity_candidates=entry.get("source_entity_candidates"),
        )
        results.append(result)
        print(f"[{result['id']}] {result['final_tag_line']}", file=sys.stderr)
    if args.out_json:
        Path(args.out_json).write_text(
            json.dumps(results, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
        )
    markdown = render_markdown(results)
    if args.out_md:
        Path(args.out_md).write_text(markdown + "\n", encoding="utf-8")
    else:
        print(markdown)
    return 0


if __name__ == "__main__":
    sys.exit(main())
