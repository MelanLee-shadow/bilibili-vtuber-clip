"""Offline, hash-bound 维护者 batch review and explicit persona projection promotion.

Transport authentication belongs to the operator retaining 维护者's original
message. This module verifies its exact bytes, never infers authority from prose.
Production runners/providers are deliberately not imported.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Literal, cast

from . import persona_facts as pf
from .asset_write_bindings import asset_binding_surfaces


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def encode(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


def document(path: Path) -> pf.JSONValue:
    def pairs(items: list[tuple[str, pf.JSONValue]]) -> dict[str, pf.JSONValue]:
        result: dict[str, pf.JSONValue] = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result
    value: pf.JSONValue = json.loads(path.read_bytes(), object_pairs_hook=pairs)
    return value


@dataclass(frozen=True)
class ReviewItem:
    index: int
    fact_id: str
    revision: str


@dataclass(frozen=True)
class ReviewBatch:
    schema_version: str
    profile_id: str
    ledger_sha256: str
    extraction_receipt_sha256: str
    recorded_at: str
    items: tuple[ReviewItem, ...]
    summary_sha256: str


def _receipt(path: Path) -> pf.ExtractionReceipt:
    receipt = cast(pf.ExtractionReceipt, pf._decode(document(path), pf.ExtractionReceipt))
    if receipt.schema_version != pf.EXTRACTION_RECEIPT_SCHEMA:
        raise ValueError("Invalid extraction receipt schema")
    if len(receipt.proposals) != len(receipt.outcomes):
        raise ValueError("Proposal/outcome length mismatch")
    for proposal, outcome in zip(receipt.proposals, receipt.outcomes, strict=True):
        if (outcome.proposal_claim_fingerprint != pf.claim_fingerprint(proposal.claim)
                or (outcome.outcome == "SCREENED_OUT") != bool(outcome.reasons)):
            raise ValueError("Inconsistent screening receipt")
    return receipt


def prepare_batch(ledger_path: Path, receipt_path: Path, *, profile_id: str,
                  recorded_at: str) -> tuple[ReviewBatch, bytes]:
    """Keep original extraction numbering, including holes for screened-out rows."""
    ledger = pf.load_persona_fact_ledger(ledger_path, profile_id=profile_id)
    receipt = _receipt(receipt_path)
    proposed = pf.append_screened(ledger, receipt.proposals, receipt.outcomes, recorded_at=recorded_at)
    heads = {row.claim_fingerprint: row for row in pf.current_revisions(proposed)}
    items: list[ReviewItem] = []
    seen: set[str] = set()
    lines = ["# 人设提案批量审核", "", f"频道：{profile_id}；候选：{receipt.candidate_id}",
             f"台账 SHA256：{sha256(ledger_path.read_bytes())}",
             f"抽取回执 SHA256：{sha256(receipt_path.read_bytes())}", "",
             "只有 维护者 原始回复可以审定。全收仅接受本页可审核项；收除第 N 条将该项延后；",
             "拒 N 默认仅拒本次证据（EVIDENCE_ONLY）。筛掉项、重复项及已接受事实不重新审定。", ""]
    for index, (proposal, outcome) in enumerate(zip(receipt.proposals, receipt.outcomes, strict=True), 1):
        head = heads.get(outcome.proposal_claim_fingerprint)
        eligible = (outcome.outcome != "SCREENED_OUT" and head is not None
                    and head.status in {"PROPOSED", "DEFERRED", "STALE_EVIDENCE"}
                    and head.fact_id not in seen)
        if eligible and head is not None:
            items.append(ReviewItem(index, head.fact_id, head.revision))
            seen.add(head.fact_id)
        lines.extend([f"## {index}. {proposal.statement_zh}",
                      "可审核" if eligible else f"无需本次决定：{outcome.outcome}",
                      f"筛选码：{', '.join(outcome.reasons) or '无'}"])
        for evidence in proposal.evidence:
            lines.extend([f"> {evidence.verbatim_quote.replace(chr(10), chr(10) + '> ')}",
                          f"出处：{evidence.bvid or '尚无 BV'} / {evidence.recording_date} / "
                          f"cue {evidence.cue_index_start}–{evidence.cue_index_end}",
                          f"说话人：{evidence.speaker_basis}；语境：{evidence.context_kind}"])
            if evidence.speaker_basis == "UNIFORM_HOST_SINGLE_SPEAKER_SESSION":
                lines.append("说话人未经声纹证实。")
        if outcome.outcome == "CONFLICT_PROPOSED":
            lines.append(f"CONFLICT：与 {outcome.target_fact_id} 极性相反；接受本条不会撤回旧事实。")
        lines.append("")
    summary = ("\n".join(lines) + "\n").encode()
    return ReviewBatch("persona-review-batch.v1", profile_id, sha256(ledger_path.read_bytes()),
                       sha256(receipt_path.read_bytes()), recorded_at, tuple(items), sha256(summary)), summary


def verify_message(authority: pf.HumanAuthorityEvidence, raw_message: bytes) -> str:
    pf._validate_authority(authority)
    message = raw_message.decode("utf-8")
    if sha256(raw_message) != authority.raw_user_message_sha256 or message != authority.raw_excerpt:
        raise ValueError("Original message bytes/hash/excerpt mismatch")
    return message


def parse_reply(batch: ReviewBatch, message: str, *, authority: pf.HumanAuthorityEvidence
                ) -> tuple[pf.ReviewDecision, ...]:
    verify_message(authority, message.encode())
    lines = message.strip().splitlines()
    available = {item.index: item for item in batch.items}
    if not lines or not available:
        raise ValueError("Empty reply or no reviewable items")

    def indices(value: str) -> set[int]:
        match = re.fullmatch(r"\s*第?\s*([0-9]+(?:\s*[、,，]\s*[0-9]+)*)\s*条?\s*", value)
        if match is None:
            raise ValueError("Use explicit numeric item indices")
        numbers = [int(part) for part in re.split(r"\s*[、,，]\s*", match[1])]
        if len(set(numbers)) != len(numbers) or not set(numbers) <= available.keys():
            raise ValueError("Duplicate, unavailable or unknown item index")
        return set(numbers)

    choices: dict[int, str]
    command = lines[0].strip()
    if command == "全收":
        choices = dict.fromkeys(available, "ACCEPT")
    elif command.startswith("收除"):
        excluded = indices(command[2:])
        choices = {index: "DEFER" if index in excluded else "ACCEPT" for index in available}
    elif command.startswith("拒") or command.startswith("缓"):
        choices = dict.fromkeys(indices(command[1:]), "REJECT" if command[0] == "拒" else "DEFER")
    else:
        raise ValueError("Unsupported reply; use 全收 / 收除第 N 条 / 拒 N / 缓 N")
    edits: dict[int, str] = {}
    scopes: dict[int, pf.RejectionScope] = {}
    for line in lines[1:]:
        match = re.fullmatch(r"(改写|rejection_scope)\s*([0-9]+)\s*[：:]\s*(.+)", line.strip())
        if match is None:
            raise ValueError("Unknown reply clause")
        index = int(match[2])
        if index not in choices:
            raise ValueError("Clause targets an undecided item")
        if match[1] == "改写":
            if choices[index] != "ACCEPT" or index in edits:
                raise ValueError("Rewrite requires one ACCEPT decision")
            edits[index] = match[3]
        else:
            if choices[index] != "REJECT" or index in scopes:
                raise ValueError("rejection_scope requires one REJECT decision")
            scopes[index] = cast(pf.RejectionScope, pf._decode(match[3], pf.RejectionScope))
    return tuple(pf.ReviewDecision(available[index].fact_id,
        cast(Literal["ACCEPT", "REJECT", "DEFER"], choices[index]), edits.get(index),
        scopes.get(index, "EVIDENCE_ONLY") if choices[index] == "REJECT" else None, authority)
        for index in sorted(choices))


def review_candidate(ledger_path: Path, batch_dir: Path, *, authority: pf.HumanAuthorityEvidence,
                     raw_message: bytes) -> pf.PersonaFactLedger:
    batch = cast(ReviewBatch, pf._decode(document(batch_dir / "batch.json"), ReviewBatch))
    rebuilt, summary = prepare_batch(ledger_path, batch_dir / "extraction-receipt.json",
                                    profile_id=batch.profile_id, recorded_at=batch.recorded_at)
    if rebuilt != batch or (batch_dir / "review-summary.md").read_bytes() != summary:
        raise ValueError("Review batch, summary or ledger drift; prepare a new batch")
    reply = verify_message(authority, raw_message)
    decisions = parse_reply(batch, reply, authority=authority)
    original = pf.load_persona_fact_ledger(ledger_path, profile_id=batch.profile_id)
    receipt = _receipt(batch_dir / "extraction-receipt.json")
    proposed = pf.append_screened(original, receipt.proposals, receipt.outcomes, recorded_at=batch.recorded_at)
    result = pf.apply_review_decisions(proposed, decisions, recorded_at=authority.source_user_message_timestamp)
    pf.verify_append_only(original, result)
    return result


def atomic_compare_write(path: Path, *, before: bytes, after: bytes) -> None:
    """Serialize cooperating writers, compare exact preimage, replace atomically."""
    if path.is_symlink() or not path.is_file():
        raise ValueError("Target must be an existing regular file")
    lock = path.with_name(path.name + ".lock")
    descriptor = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        if path.is_symlink() or path.read_bytes() != before:
            raise ValueError("Concurrent target drift")
        if before == after:
            return
        name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
                name = temporary.name
                temporary.write(after)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.chmod(name, path.stat().st_mode & 0o777)
            os.replace(name, path)
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if name is not None and os.path.exists(name):
                os.unlink(name)


@dataclass(frozen=True)
class ReceiptBinding:
    candidate_id: str
    surface: pf.BindingSurfaceKind
    receipt_path: str
    receipt_sha256: str
    asset_path_pointer: str
    asset_sha256_pointer: str
    pinned_deploy_sha: str | None
    exact_contract: bool
    terminal: bool


@dataclass(frozen=True)
class InflightInventory:
    schema_version: str
    profile_id: str
    scope: str
    captured_at: str
    complete: bool
    entries: tuple[ReceiptBinding, ...]


@dataclass(frozen=True)
class BindingReadback:
    binding: ReceiptBinding
    matches_current_bytes: bool | None
    reason: str


@dataclass(frozen=True)
class PromotionPlan:
    schema_version: str
    profile_id: str
    ledger_sha256: str
    current_persona_sha256: str
    projection_sha256: str
    inventory_sha256: str | None
    inventory_scope: str | None
    surfaces: tuple[pf.AssetBindingSurface, ...]
    inflight: tuple[BindingReadback, ...]
    blockers: tuple[str, ...]
    decision: pf.WriteDecisionKind


def _pointer(value: pf.JSONValue, pointer: str) -> pf.JSONValue:
    if not pointer.startswith("/"):
        raise ValueError("Receipt pointer must be an explicit JSON pointer")
    for part in pointer[1:].split("/"):
        key = part.replace("~1", "/").replace("~0", "~")
        if isinstance(value, dict):
            value = value[key]
        elif isinstance(value, list) and key.isdecimal():
            value = value[int(key)]
        else:
            raise ValueError("Receipt pointer does not resolve")
    return value


def promotion_plan(*, repo_root: Path, profile_id: str, ledger_path: Path,
                   persona_path: Path, inventory_path: Path | None) -> PromotionPlan:
    ledger = pf.load_persona_fact_ledger(ledger_path, profile_id=profile_id)
    projection = pf.render_persona_projection(ledger, consumer="TITLE_COVER")
    current = sha256(persona_path.read_bytes())
    surfaces = asset_binding_surfaces("persona", repo_root=repo_root, profile_id=profile_id)
    blockers: list[str] = []
    readbacks: list[BindingReadback] = []
    inventory_sha: str | None = None
    scope: str | None = None
    if inventory_path is None:
        blockers.append("INFLIGHT_INVENTORY_REQUIRED")
    else:
        inventory_sha = sha256(inventory_path.read_bytes())
        inventory = cast(InflightInventory, pf._decode(document(inventory_path), InflightInventory))
        if (inventory.schema_version != "persona-inflight-inventory.v1" or inventory.profile_id != profile_id
                or not inventory.scope or not inventory.captured_at or not inventory.complete):
            raise ValueError("Invalid/incomplete inflight inventory")
        scope = inventory.scope
        seen: set[tuple[str, str, str]] = set()
        for binding in inventory.entries:
            identity = (binding.candidate_id, binding.receipt_path, binding.asset_sha256_pointer)
            if not binding.candidate_id or identity in seen or binding.surface not in {s.kind for s in surfaces}:
                raise ValueError("Duplicate/invalid inflight receipt identity or surface")
            seen.add(identity)
            matches: bool | None = None
            reason = "VERIFIED"
            receipt_path = (inventory_path.parent / binding.receipt_path).resolve()
            try:
                if sha256(receipt_path.read_bytes()) != binding.receipt_sha256:
                    raise ValueError("Receipt hash drift")
                value = document(receipt_path)
                bound_path = _pointer(value, binding.asset_path_pointer)
                digest = _pointer(value, binding.asset_sha256_pointer)
                if not isinstance(bound_path, str) or not isinstance(digest, str):
                    raise ValueError("Receipt lacks explicit asset byte binding")
                target = Path(bound_path)
                if not target.is_absolute():
                    target = repo_root / target
                if target.resolve() != persona_path.resolve() or re.fullmatch(r"(?:sha256:)?[0-9a-f]{64}", digest) is None:
                    raise ValueError("Receipt asset identity mismatch")
                matches = digest.removeprefix("sha256:") == current
            except (OSError, ValueError, KeyError, IndexError) as exc:
                reason = str(exc)
            readbacks.append(BindingReadback(binding, matches, reason))
            if not binding.terminal:
                if matches is None:
                    blockers.append(f"UNVERIFIED_INFLIGHT:{binding.candidate_id}")
                if binding.exact_contract or binding.pinned_deploy_sha is not None:
                    blockers.append(f"PINNED_OR_EXACT_INFLIGHT:{binding.candidate_id}")
    return PromotionPlan("persona-promotion-plan.v1", profile_id, sha256(ledger_path.read_bytes()),
        current, sha256(projection), inventory_sha, scope, surfaces, tuple(readbacks), tuple(blockers),
        "DEFER_UNTIL_TERMINAL" if blockers else "DEPLOY_CLASS_CHANGE" if surfaces else "WRITE_ANYTIME")


def promote(*, plan: PromotionPlan, expected_plan_sha256: str, deployment_epoch: str,
            repo_root: Path, ledger_path: Path, persona_path: Path, inventory_path: Path) -> None:
    """Apply only an explicitly reviewed, freshly re-read deployment plan."""
    fresh = promotion_plan(repo_root=repo_root, profile_id=plan.profile_id, ledger_path=ledger_path,
                           persona_path=persona_path, inventory_path=inventory_path)
    if (not deployment_epoch.strip() or fresh != plan or sha256(encode(asdict(fresh))) != expected_plan_sha256
            or fresh.blockers):
        raise ValueError("Promotion requires an unchanged plan, deployment epoch and no inflight blockers")
    before = persona_path.read_bytes()
    ledger_bytes = ledger_path.read_bytes()
    if sha256(before) != fresh.current_persona_sha256 or sha256(ledger_bytes) != fresh.ledger_sha256:
        raise ValueError("Promotion input drift")
    ledger = pf.load_persona_fact_ledger(ledger_path, profile_id=plan.profile_id)
    after = pf.render_persona_projection(ledger, consumer="TITLE_COVER")
    if sha256(after) != fresh.projection_sha256:
        raise ValueError("Projection drift")
    atomic_compare_write(persona_path, before=before, after=after)
