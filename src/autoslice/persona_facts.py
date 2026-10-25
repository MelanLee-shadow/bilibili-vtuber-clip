"""Append-only persona facts extracted from sealed final subtitles.

Ledger, migration and projection primitives (design section 3, B1).
Extraction/screening remain B2 stubs; no production consumer is connected.

Invariants carried by the types rather than by comments:

* Evidence for a fact is a verbatim quote from an exact final SRT sealed by
  ``final-review-audit.v2``, an 维护者 statement, or a migrated legacy line.
  There is no correction-record provenance: "X was misheard as Y" can only
  become a ``DirectionalSurfaceObservation`` scoped to one source interval,
  never a persona fact or a general alias.
* Only a human ``ReviewDecision`` moves a fact to ``ACCEPTED``; the reviewer
  role is ``维护者`` until 维护者 delegates otherwise.
* ``persona.md`` is a projection of accepted revisions and changes only through
  an explicit promote step, because its bytes are bound by the title stage
  snapshot, the Song proof closure, the broad fingerprint and the cover
  generation checkpoint.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, fields, is_dataclass, replace
from pathlib import Path
from typing import Literal, Protocol, Union, cast, get_args, get_origin, get_type_hints
import hashlib
import json
import re
import types
import unicodedata

PERSONA_FACTS_ASSET_KEY = "persona_facts"
PERSONA_PROJECTION_ASSET_KEY = "persona"
EXTRACTION_RECEIPT_SCHEMA = "persona-fact-extraction-receipt.v1"
REVIEW_QUEUE_SCHEMA = "channel-knowledge-review-queue.v1"
# Ledger schema is profile-scoped like other evidence: f"{profile_id}-persona-facts.v1".
LEDGER_SCHEMA_SUFFIX = "persona-facts.v1"
PROJECTION_GENERATED_SECTION_ZH = "切片中积累的已审定特质"
# Cover generation injects persona lines starting with these words into image
# prompts verbatim (cover_generation.py); machine-origin lines must not use them.
PROJECTION_RESERVED_LINE_PREFIXES: tuple[str, ...] = ("身份", "形象")

FactKind = Literal[
    "PREFERENCE",
    "TRAIT",
    "EXPERIENCE",
    "VALUE",
    "RELATIONSHIP",  # host's biographical ties; no other owner ledger holds them
    "IDENTITY_BACKGROUND",
    "APPEARANCE",
    "EDITORIAL_DIRECTIVE",
    "LEGACY_TEXT",
]
MACHINE_PROPOSABLE_KINDS: frozenset[str] = frozenset(
    {"PREFERENCE", "TRAIT", "EXPERIENCE", "VALUE", "RELATIONSHIP"}
)
Predicate = Literal[
    "LIKES",
    "DISLIKES",
    "IS",
    "HAS_HABIT",
    "DID",
    "HOLDS_VIEW",
    "FRIEND",
    "FAMILY",
    "ROOMMATE",
    "COLLEAGUE",
    "OTHER_RELATION",
    "STATES",  # legacy/editorial text rows only
]
Polarity = Literal["AFFIRM", "NEGATE"]
FactStatus = Literal[
    "PROPOSED",
    "ACCEPTED",
    "REJECTED",
    "DEFERRED",
    "STALE_EVIDENCE",
    "RETIRED",
]
RejectionScope = Literal["CLAIM", "CLAIM_UNLESS_HUMAN_EVIDENCE", "EVIDENCE_ONLY"]
EvidenceClass = Literal[
    "MACHINE_PROPOSAL",
    "ATOMIC_HUMAN_PERSONA_TRUTH",
    "LEGACY_PERSONA_MD_GRANDFATHERED",
]
# One value per member of ``FactEvidence``.  CORRECTION_RECORD is absent on
# purpose: corrections only yield ``DirectionalSurfaceObservation``.
ProvenanceKind = Literal["FINAL_SUBTITLE_TEXT", "HUMAN_TRUTH", "LEGACY_PERSONA_MD"]
# subtitle_acceptability_policy grades/choices.  They are written per
# adjudicated finding (semantic_terminal_adjudication), not per cue: a cue that
# was never disputed has no grade, and that is the clean case.
AcceptabilityGrade = Literal["A", "B", "C", "D"]
TerminalChoice = Literal["CURRENT", "PROPOSED", "CONTEXT_FIT", "UNRESOLVABLE"]
SpeakerBasis = Literal[
    "ATTRIBUTED_HOST",  # speaker-attribution.v1 HOST (opt-in artifact)
    # No attribution artifact, recording has no session_relation row, no
    # read-aloud/media overlap: may be PROPOSED with this flag shown to the reviewer.
    "UNIFORM_HOST_SINGLE_SPEAKER_SESSION",
    "UNIFORM_HOST_UNVERIFIED",  # multi-speaker session without attribution: screened out
    "ATTRIBUTED_NON_HOST",
    "UNKNOWN",
]
ContextKind = Literal[
    "LIVE_ASSERTION",
    "QUOTED",
    "READ_ALOUD",
    "ROLE_PLAY",
    "MEDIA_PLAYBACK",
    "HYPOTHETICAL",
    "GAME_OR_FICTION",
    "UNKNOWN",
]
ReviewerRole = Literal["维护者"]
EntityResolution = Literal["RESOLVED", "UNCERTAIN"]


OwnerLedgerKey = Literal[
    "streamer_registry_sources",
    "community_name_sources",
    "timely_term_seeds",
    "related_entity_seeds",
    "session_relation_ledger",
    "speech_memory_ledger",
    "entity_confusables",
    "glossary",
]
OwnerVerification = Literal["OWNER_CRAWLER_VERIFICATION", "HUMAN_REVIEW", "MACHINE_CLAIM_STATE"]
ScreenReason = Literal[
    "QUOTE_NOT_IN_SEALED_SRT",
    "SEALED_SRT_SHA_MISMATCH",
    "CUE_OVERLAPS_UNCERTAIN_ADJUDICATION",
    "SPEAKER_NOT_ATTRIBUTED_HOST",
    "CONTEXT_NOT_LIVE_ASSERTION",
    "KIND_NOT_MACHINE_PROPOSABLE",
    "ENTITY_UNRESOLVED",
    "REJECTED_FINGERPRINT_MATCH",
    "SOURCE_SRT_SUPERSEDED",
    "RESERVED_PROJECTION_PREFIX",
]
ScreenOutcomeKind = Literal["PROPOSED", "SCREENED_OUT", "EVIDENCE_APPENDED", "CONFLICT_PROPOSED"]
ProjectionConsumer = Literal["TITLE_COVER"]
BindingSurfaceKind = Literal[
    "TALK_STAGE_SNAPSHOT",
    "SONG_PROOF_CLOSURE",
    "BROAD_PIPELINE_FINGERPRINT",
    "PACKAGE_AUDIT_POLICY",
    "COVER_GENERATION_CHECKPOINT",
    "RUNTIME_CANDIDATE_BINDING",
    "AUTHORITY_RECEIPT_SHA",
]
WriteDecisionKind = Literal["WRITE_ANYTIME", "DEPLOY_CLASS_CHANGE", "DEFER_UNTIL_TERMINAL"]


@dataclass(frozen=True)
class EntityRef:
    """Points into an existing owner ledger; never creates an identity."""

    owner_key: str
    canonical: str
    resolution: EntityResolution


@dataclass(frozen=True)
class TemporalScope:
    timeless: bool
    as_of_start: str | None
    as_of_end: str | None


@dataclass(frozen=True)
class FactClaim:
    """Subject is always the profile host; other people appear only as objects."""

    kind: FactKind
    predicate: Predicate
    object_text: str
    object_entity: EntityRef | None
    polarity: Polarity
    temporal: TemporalScope


@dataclass(frozen=True)
class AdjudicatedFindingRef:
    """A terminal finding whose span overlaps the quoted cues.

    Eligible only with grade A and a choice other than UNRESOLVABLE; B (best
    reading), C (smoothed), D and CONTEXT_FIT spans cannot back a fact.
    """

    finding_id: str
    grade: AcceptabilityGrade
    choice: TerminalChoice


@dataclass(frozen=True)
class SubtitleQuoteEvidence:
    """Verbatim quote from one sealed final SRT; ``bvid`` is a join, not identity."""

    candidate_id: str
    recording_basename: str
    recording_date: str
    final_srt_sha256: str
    final_review_audit_sha256: str
    cue_index_start: int
    cue_index_end: int
    source_start_ms: int
    source_end_ms: int
    verbatim_quote: str
    overlapping_adjudicated_findings: tuple[AdjudicatedFindingRef, ...]
    speaker_basis: SpeakerBasis
    context_kind: ContextKind
    bvid: str | None
    extractor_id: str
    extractor_version: str
    extractor_prompt_sha256: str


@dataclass(frozen=True)
class HumanAuthorityEvidence:
    """Same shape as subtitle_truth_ledger ``source_evidence`` for 维护者's words."""

    reviewer: ReviewerRole
    source_thread_id: str
    source_user_message_id: str
    source_user_message_timestamp: str
    raw_user_message_sha256: str
    raw_excerpt: str


@dataclass(frozen=True)
class LegacyLineEvidence:
    persona_md_git_blob_sha: str
    line_start: int
    line_end: int


FactEvidence = Union[SubtitleQuoteEvidence, HumanAuthorityEvidence, LegacyLineEvidence]


@dataclass(frozen=True)
class RenderSlot:
    section_zh: str
    order: int


@dataclass(frozen=True)
class PersonaFactRevision:
    """One append-only row; a status change is a new row that ``supersedes`` the old."""

    fact_id: str
    revision: str
    status: FactStatus
    claim: FactClaim
    claim_fingerprint: str
    statement_zh: str
    render: RenderSlot
    evidence: tuple[FactEvidence, ...]
    evidence_class: EvidenceClass
    supersedes: str | None
    decided_by: HumanAuthorityEvidence | None
    rejection_scope: RejectionScope | None
    recorded_at: str


@dataclass(frozen=True)
class SuppressionRule:
    """Reviewer-written rule that keeps a family of rejected claims from returning."""

    rule_id: str
    kind: FactKind
    predicate: Predicate | None
    object_key_prefix: str
    authority: HumanAuthorityEvidence
    recorded_at: str


@dataclass(frozen=True)
class PersonaFactLedger:
    """``assets/<profile>/persona_facts.v1.json``; never in any fingerprint list."""

    schema_version: str
    profile_id: str
    revisions: tuple[PersonaFactRevision, ...]
    suppressions: tuple[SuppressionRule, ...]


@dataclass(frozen=True)
class SealedFinalSubtitle:
    """Eligibility event: exact final SRT bytes bound by final-review-audit.v2."""

    candidate_id: str
    recording_basename: str
    recording_date: str
    final_srt_path: Path
    final_srt_sha256: str
    final_review_audit_path: Path
    final_review_audit_sha256: str
    speaker_attribution_path: Path | None
    terminal_adjudication_path: Path | None
    entity_projection_path: Path | None


@dataclass(frozen=True)
class ProposedFact:
    """Extractor output before screening; never written to the ledger as-is."""

    claim: FactClaim
    statement_zh: str
    evidence: tuple[SubtitleQuoteEvidence, ...]


@dataclass(frozen=True)
class DirectionalSurfaceObservation:
    """A correction seen in one interval.  There is no global or reverse scope."""

    heard_surface: str
    resolved_canonical: str
    direction: Literal["HEARD_AS_TO_RESOLVED"]
    scope: Literal["SOURCE_INTERVAL"]
    recording_basename: str
    source_start_ms: int
    source_end_ms: int
    candidate_id: str
    adjudication_ref: str


@dataclass(frozen=True)
class OwnerLedgerProposal:
    """Runtime review-queue row for an existing owner; not read as knowledge."""

    proposal_id: str
    target: OwnerLedgerKey
    surface: str
    evidence: tuple[SubtitleQuoteEvidence, ...]
    observation: DirectionalSurfaceObservation | None
    verification: OwnerVerification
    resulting_owner_revision: str | None


@dataclass(frozen=True)
class ScreenOutcome:
    proposal_claim_fingerprint: str
    outcome: ScreenOutcomeKind
    reasons: tuple[ScreenReason, ...]
    target_fact_id: str | None


@dataclass(frozen=True)
class ExtractionReceipt:
    """Per sealed SRT, content-addressed; keeps screened-out proposals for audit."""

    schema_version: str
    candidate_id: str
    final_srt_sha256: str
    extractor_id: str
    extractor_version: str
    proposals: tuple[ProposedFact, ...]
    outcomes: tuple[ScreenOutcome, ...]
    owner_proposals: tuple[OwnerLedgerProposal, ...]


@dataclass(frozen=True)
class ReviewDecision:
    """维护者's batch answer, one row per fact; the only path to ACCEPTED."""

    fact_id: str
    decision: Literal["ACCEPT", "REJECT", "DEFER"]
    edited_statement_zh: str | None
    rejection_scope: RejectionScope | None
    authority: HumanAuthorityEvidence


@dataclass(frozen=True)
class AssetBindingSurface:
    kind: BindingSurfaceKind
    code_ref: str
    detail: str


@dataclass(frozen=True)
class InflightBinding:
    candidate_id: str
    surface: BindingSurfaceKind
    bound_sha256: str
    pinned_deploy_sha: str | None


@dataclass(frozen=True)
class WriteDecision:
    asset_key: str
    decision: WriteDecisionKind
    surfaces: tuple[AssetBindingSurface, ...]
    blocking_inflight: tuple[InflightBinding, ...]


class PersonaFactExtractor(Protocol):
    """Reads only the sealed subtitle bundle; never titles, hooks or persona.md."""

    def propose(self, sealed: SealedFinalSubtitle) -> Sequence[ProposedFact]:
        ...


def claim_fingerprint(claim: FactClaim) -> str:
    """sha256 of canonical (kind, predicate, object_key, polarity)."""

    object_key = claim.object_entity.canonical if claim.object_entity is not None else "".join(
        char for char in unicodedata.normalize("NFKC", claim.object_text)
        if not char.isspace() and not unicodedata.category(char).startswith("P")
    )
    canonical = json.dumps({"kind": claim.kind, "predicate": claim.predicate,
                            "object_key": object_key, "polarity": claim.polarity},
                           ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def screen_proposals(
    proposals: Sequence[ProposedFact],
    *,
    ledger: PersonaFactLedger,
    sealed: SealedFinalSubtitle,
) -> tuple[ScreenOutcome, ...]:
    """Deterministic screen; can propose, append evidence or screen out, never accept."""

    raise NotImplementedError


def observations_from_corrections(sealed: SealedFinalSubtitle) -> tuple[DirectionalSurfaceObservation, ...]:
    """Turn correction records into interval-scoped observations for owner review."""

    raise NotImplementedError


def append_screened(
    ledger: PersonaFactLedger,
    proposals: Sequence[ProposedFact],
    outcomes: Sequence[ScreenOutcome],
    *,
    recorded_at: str,
) -> PersonaFactLedger:
    """Append PROPOSED / EVIDENCE_APPENDED rows; SCREENED_OUT stays in the receipt."""

    raise NotImplementedError


def apply_review_decisions(
    ledger: PersonaFactLedger,
    decisions: Sequence[ReviewDecision],
    *,
    recorded_at: str,
) -> PersonaFactLedger:
    """Append ACCEPTED/REJECTED/DEFERRED revisions backed by 维护者's message hash."""

    _validate_ledger(ledger)
    result = ledger
    seen: set[str] = set()
    for decision in decisions:
        if decision.fact_id in seen:
            raise ValueError("Duplicate review decision")
        seen.add(decision.fact_id)
        _validate_authority(decision.authority)
        heads = {row.fact_id: row for row in current_revisions(result)}
        if decision.fact_id not in heads:
            raise ValueError("Unknown review fact")
        head = heads[decision.fact_id]
        status: FactStatus
        if decision.decision == "ACCEPT":
            status = "ACCEPTED"
        elif decision.decision == "REJECT":
            status = "REJECTED"
        elif decision.decision == "DEFER":
            status = "DEFERRED"
        else:
            raise ValueError("Invalid review decision")
        revision = replace(
            head, revision=f"r{sum(row.fact_id == head.fact_id for row in result.revisions) + 1}",
            status=status, statement_zh=decision.edited_statement_zh if decision.edited_statement_zh is not None else head.statement_zh,
            supersedes=head.revision, decided_by=decision.authority,
            rejection_scope=decision.rejection_scope, recorded_at=recorded_at,
        )
        updated = replace(result, revisions=(*result.revisions, revision))
        verify_append_only(result, updated)
        result = updated
    return result


def verify_append_only(previous: PersonaFactLedger, current: PersonaFactLedger) -> None:
    """Validate unchanged prefixes and every new revision's transition."""
    _validate_ledger(previous)
    _validate_ledger(current)
    if previous.schema_version != current.schema_version or previous.profile_id != current.profile_id:
        raise ValueError("Ledger identity changed")
    if current.revisions[:len(previous.revisions)] != previous.revisions:
        raise ValueError("Revisions are not an unchanged prefix")
    if current.suppressions[:len(previous.suppressions)] != previous.suppressions:
        raise ValueError("Suppressions are not an unchanged prefix")
    if previous.revisions and any(row.evidence_class == "LEGACY_PERSONA_MD_GRANDFATHERED"
                                  for row in current.revisions[len(previous.revisions):]):
        raise ValueError("Legacy migration cannot be appended to an existing ledger")


def current_revisions(ledger: PersonaFactLedger) -> tuple[PersonaFactRevision, ...]:
    """Fold validated supersedes chains in first-fact order."""
    _validate_ledger(ledger)
    heads: dict[str, PersonaFactRevision] = {}
    for row in ledger.revisions:
        heads[row.fact_id] = row
    return tuple(heads.values())


def _machine_origin(row: PersonaFactRevision) -> bool:
    return row.evidence_class == "MACHINE_PROPOSAL" or any(isinstance(e, SubtitleQuoteEvidence) for e in row.evidence)


def _validate_authority(authority: HumanAuthorityEvidence) -> None:
    if not isinstance(authority, HumanAuthorityEvidence) or authority.reviewer != "维护者":
        raise ValueError("Only 维护者 HumanAuthorityEvidence may decide a fact")
    if not all((authority.source_thread_id, authority.source_user_message_id,
                authority.source_user_message_timestamp, authority.raw_excerpt)) or re.fullmatch(r"[0-9a-f]{64}", authority.raw_user_message_sha256) is None:
        raise ValueError("Incomplete human authority evidence")


def _validate_ledger(ledger: PersonaFactLedger) -> None:
    if not ledger.profile_id or ledger.schema_version != f"{ledger.profile_id}-{LEDGER_SCHEMA_SUFFIX}":
        raise ValueError("Ledger schema/profile mismatch")
    if not isinstance(ledger.revisions, tuple) or not isinstance(ledger.suppressions, tuple):
        raise ValueError("Ledger collections must be immutable tuples")
    heads: dict[str, PersonaFactRevision] = {}
    revisions: set[tuple[str, str]] = set()
    transitions: dict[FactStatus, frozenset[FactStatus]] = {
        "PROPOSED": frozenset({"PROPOSED", "ACCEPTED", "REJECTED", "DEFERRED"}),
        "DEFERRED": frozenset({"PROPOSED", "ACCEPTED", "REJECTED", "DEFERRED"}),
        "REJECTED": frozenset({"PROPOSED"}),
        "ACCEPTED": frozenset({"ACCEPTED", "RETIRED", "STALE_EVIDENCE"}),
        "STALE_EVIDENCE": frozenset({"PROPOSED", "ACCEPTED", "REJECTED", "DEFERRED", "RETIRED"}),
        "RETIRED": frozenset(),
    }
    legacy: list[PersonaFactRevision] = []
    for row in ledger.revisions:
        if (not row.fact_id or not row.revision or not row.recorded_at
                or row.status not in transitions or row.claim.kind not in get_args(FactKind)
                or row.claim.predicate not in get_args(Predicate) or row.claim.polarity not in get_args(Polarity)
                or not isinstance(row.evidence, tuple) or not row.evidence
                or any(not isinstance(e, (SubtitleQuoteEvidence, HumanAuthorityEvidence, LegacyLineEvidence)) for e in row.evidence)):
            raise ValueError("Invalid fact revision")
        if re.fullmatch(r"[A-Za-z0-9_.:-]+", row.fact_id) is None or re.fullmatch(r"[A-Za-z0-9_.:-]+", row.revision) is None:
            raise ValueError("Unsafe projection identity")
        for evidence_item in row.evidence:
            if isinstance(evidence_item, HumanAuthorityEvidence):
                _validate_authority(evidence_item)
            if isinstance(evidence_item, SubtitleQuoteEvidence) and not isinstance(evidence_item.overlapping_adjudicated_findings, tuple):
                raise ValueError("Evidence findings must be immutable tuples")
        if row.claim_fingerprint != claim_fingerprint(row.claim):
            raise ValueError("Claim fingerprint mismatch")
        if row.evidence_class not in get_args(EvidenceClass):
            raise ValueError("Invalid evidence class")
        if row.evidence_class == "ATOMIC_HUMAN_PERSONA_TRUTH" and not any(isinstance(e, HumanAuthorityEvidence) for e in row.evidence):
            raise ValueError("Human fact requires human evidence")
        if row.claim.object_entity is not None and row.claim.object_entity.resolution != "RESOLVED" and row.status == "ACCEPTED":
            raise ValueError("Unresolved entity cannot be accepted")
        machine = _machine_origin(row)
        if machine and row.claim.kind not in MACHINE_PROPOSABLE_KINDS:
            raise ValueError("Machine origin cannot assert this fact kind")
        predicates: dict[FactKind, frozenset[Predicate]] = {
            "PREFERENCE": frozenset({"LIKES", "DISLIKES"}), "TRAIT": frozenset({"IS", "HAS_HABIT"}),
            "EXPERIENCE": frozenset({"DID"}), "VALUE": frozenset({"HOLDS_VIEW"}),
            "RELATIONSHIP": frozenset({"FRIEND", "FAMILY", "ROOMMATE", "COLLEAGUE", "OTHER_RELATION"}),
            "IDENTITY_BACKGROUND": frozenset({"IS"}), "APPEARANCE": frozenset({"IS"}),
            "EDITORIAL_DIRECTIVE": frozenset({"STATES"}), "LEGACY_TEXT": frozenset({"STATES"}),
        }
        if row.claim.predicate not in predicates[row.claim.kind]:
            raise ValueError("Predicate is not valid for fact kind")
        if machine and (row.statement_zh.lstrip(" \t-*#").startswith(PROJECTION_RESERVED_LINE_PREFIXES)
                        or "\n" in row.statement_zh or "\r" in row.statement_zh or "<!--" in row.statement_zh):
            raise ValueError("Reserved or multiline machine projection")
        if row.decided_by is not None:
            _validate_authority(row.decided_by)
        grandfathered = row.evidence_class == "LEGACY_PERSONA_MD_GRANDFATHERED"
        if grandfathered:
            if (row.status != "ACCEPTED" or row.claim.kind not in {"LEGACY_TEXT", "EDITORIAL_DIRECTIVE"}
                    or row.supersedes is not None or len(row.evidence) != 1
                    or not isinstance(row.evidence[0], LegacyLineEvidence) or row.decided_by is not None):
                raise ValueError("Invalid grandfathered migration")
            legacy.append(row)
        elif row.status == "ACCEPTED" and row.decided_by is None:
            raise ValueError("ACCEPTED requires 维护者 authority")
        if row.status in {"REJECTED", "DEFERRED"} and row.decided_by is None:
            raise ValueError("Review status requires 维护者 authority")
        if row.status == "REJECTED":
            if row.rejection_scope not in get_args(RejectionScope):
                raise ValueError("REJECTED requires rejection_scope")
        elif row.rejection_scope is not None:
            raise ValueError("rejection_scope only belongs to REJECTED")
        identity = (row.fact_id, row.revision)
        if identity in revisions:
            raise ValueError("Duplicate revision")
        revisions.add(identity)
        head = heads.get(row.fact_id)
        if head is None:
            if row.supersedes is not None or row.status not in {"PROPOSED", "ACCEPTED"}:
                raise ValueError("Invalid initial revision")
        else:
            if row.supersedes != head.revision or row.status not in transitions[head.status]:
                raise ValueError("Invalid status transition or supersedes fork")
            if row.claim != head.claim or row.claim_fingerprint != head.claim_fingerprint:
                raise ValueError("A revision cannot change fact identity")
            if head.status == "REJECTED":
                if head.rejection_scope == "CLAIM":
                    raise ValueError("Rejected claim is suppressed")
                if head.rejection_scope == "CLAIM_UNLESS_HUMAN_EVIDENCE" and not any(isinstance(e, HumanAuthorityEvidence) and e not in head.evidence for e in row.evidence):
                    raise ValueError("Reproposal requires new human evidence")
                if not any(e not in head.evidence for e in row.evidence):
                    raise ValueError("Reproposal requires new evidence")
            if head.status == "ACCEPTED" and row.status == "ACCEPTED":
                if (row.statement_zh != head.statement_zh or row.render != head.render
                        or row.decided_by != head.decided_by or row.evidence_class != head.evidence_class
                        or row.evidence[:len(head.evidence)] != head.evidence):
                    raise ValueError("Evidence append cannot rewrite accepted projection/authority")
        heads[row.fact_id] = row
    if legacy:
        # Blob identity proves the grandfathered exception covers exact original
        # bytes, including headers, blank lines and the final newline.
        body = b""
        blob = ""
        for index, row in enumerate(legacy, 1):
            evidence = cast(LegacyLineEvidence, row.evidence[0])
            if evidence.line_start != index or evidence.line_end != index or row.render.order != index:
                raise ValueError("Legacy line order mismatch")
            if blob and evidence.persona_md_git_blob_sha != blob:
                raise ValueError("Mixed legacy blobs")
            blob = evidence.persona_md_git_blob_sha
            body += row.statement_zh.encode("utf-8")
        if _git_blob_sha(body) != blob:
            raise ValueError("Legacy bytes do not match their git blob")
    suppression_ids: set[str] = set()
    for rule in ledger.suppressions:
        _validate_authority(rule.authority)
        if not rule.rule_id or rule.rule_id in suppression_ids:
            raise ValueError("Invalid suppression identity")
        suppression_ids.add(rule.rule_id)


def render_persona_projection(ledger: PersonaFactLedger, *, consumer: ProjectionConsumer) -> bytes:
    """Render only accepted heads, preserving legacy lines including newlines."""
    if consumer != "TITLE_COVER":
        raise ValueError("Unsupported persona projection consumer")
    accepted = sorted((row for row in current_revisions(ledger) if row.status == "ACCEPTED"),
                      key=lambda row: (row.render.order, row.fact_id))
    raw = [row for row in accepted if row.claim.kind in {"LEGACY_TEXT", "EDITORIAL_DIRECTIVE"}]
    facts = [row for row in accepted if row not in raw]
    text = "".join(row.statement_zh for row in raw)
    if facts:
        text += ("" if not text else "\n" if text.endswith("\n") else "\n\n")
        text += f"## {PROJECTION_GENERATED_SECTION_ZH}\n"
        for row in facts:
            text += f"- {row.statement_zh} <!-- pf:{row.fact_id}@{row.revision} -->\n"
    return text.encode("utf-8")


def _git_blob_sha(body: bytes) -> str:
    return hashlib.sha1(f"blob {len(body)}\0".encode() + body).hexdigest()


def migrate_legacy_persona_md(text: str, *, git_blob_sha: str, profile_id: str, recorded_at: str) -> PersonaFactLedger:
    """One grandfathered row per exact line; no human approval is invented."""
    if _git_blob_sha(text.encode("utf-8")) != git_blob_sha:
        raise ValueError("Legacy git blob SHA mismatch")
    rows: list[PersonaFactRevision] = []
    section = ""
    for index, line in enumerate(text.splitlines(keepends=True), 1):
        if line.startswith("#"):
            section = line.rstrip("\r\n")
        directive = any(word in line for word in ("规则", "必须", "严禁", "禁止", "一律", "默认", "仅当")) or (
            any(word in section for word in ("规则", "映射")) and line.lstrip().startswith("- ")
        )
        kind: FactKind = "EDITORIAL_DIRECTIVE" if directive else "LEGACY_TEXT"
        claim = FactClaim(kind, "STATES", line, None, "AFFIRM", TemporalScope(True, None, None))
        rows.append(PersonaFactRevision(
            f"legacy-line-{index:03}", "r1", "ACCEPTED", claim, claim_fingerprint(claim), line,
            RenderSlot(section, index), (LegacyLineEvidence(git_blob_sha, index, index),),
            "LEGACY_PERSONA_MD_GRANDFATHERED", None, None, None, recorded_at,
        ))
    ledger = PersonaFactLedger(f"{profile_id}-{LEDGER_SCHEMA_SUFFIX}", profile_id, tuple(rows), ())
    _validate_ledger(ledger)
    return ledger


# Strict JSON boundary: deserialize only the declared frozen data classes and
# Literal unions. Unknown fields and scalar coercion (e.g. bool as int) fail.
JSONValue = Union[None, bool, int, float, str, list["JSONValue"], dict[str, "JSONValue"]]


def _decode(value: JSONValue, annotation: object) -> object:
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin in (Union, types.UnionType):
        for choice in args:
            try:
                return _decode(value, choice)
            except (ValueError, TypeError):
                pass
        raise ValueError("Invalid union value")
    if origin is Literal:
        if value not in args or not isinstance(value, str):
            raise ValueError("Invalid enum value")
        return value
    if origin is tuple:
        if not isinstance(value, list) or len(args) != 2 or args[1] is not Ellipsis:
            raise ValueError("Invalid tuple array")
        return tuple(_decode(item, args[0]) for item in value)
    if annotation is type(None):
        if value is not None:
            raise ValueError("Expected null")
        return None
    if annotation in (str, int, bool, float):
        if type(value) is not annotation:
            raise ValueError("Invalid scalar type")
        return value
    if isinstance(annotation, type) and is_dataclass(annotation):
        if not isinstance(value, dict) or set(value) != {field.name for field in fields(annotation)}:
            raise ValueError("Invalid dataclass fields")
        hints = get_type_hints(annotation)
        decoded = [_decode(value[field.name], hints[field.name]) for field in fields(annotation)]
        result: object = annotation(*decoded)
        return result
    raise ValueError("Unsupported ledger field type")


def load_persona_fact_ledger(path: Path, *, profile_id: str) -> PersonaFactLedger:
    def unique_pairs(pairs: list[tuple[str, JSONValue]]) -> dict[str, JSONValue]:
        result: dict[str, JSONValue] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result

    value: JSONValue = json.loads(path.read_bytes(), object_pairs_hook=unique_pairs)
    ledger = cast(PersonaFactLedger, _decode(value, PersonaFactLedger))
    if ledger.profile_id != profile_id:
        raise ValueError("Ledger profile mismatch")
    _validate_ledger(ledger)
    return ledger


def asset_binding_surfaces(asset_key: str) -> tuple[AssetBindingSurface, ...]:
    """Compatibility entry point; binding definitions live in their own module."""
    from .asset_write_bindings import asset_binding_surfaces as derive
    return derive(asset_key)


def plan_knowledge_write(asset_key: str, *, inflight: Sequence[InflightBinding]) -> WriteDecision:
    from .asset_write_bindings import plan_knowledge_write as plan
    return plan(asset_key, inflight=inflight)
