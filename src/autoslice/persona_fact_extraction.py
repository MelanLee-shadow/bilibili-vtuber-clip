"""Offline, deterministic screening of sealed-subtitle persona proposals.

Only the injected extractor reasons about claims. Evidence identity, speaker and
context exclusions are reconstructed from the whitelisted source artifacts.
This module never writes a knowledge asset or acquires a runner lock.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, replace
import hashlib
import json
from pathlib import Path
import tempfile
from typing import cast, get_args
import unicodedata

from . import persona_facts as pf
from .jingting_chunker import parse_srt_cues
from .session_relation_authority import resolve_session_relation

Json = pf.JSONValue
JsonMap = dict[str, Json]
Extractor = Callable[[pf.SealedFinalSubtitle], Sequence[pf.ProposedFact]]


@dataclass(frozen=True)
class CpaPersonaFactExtractor:
    """Inject the project's CPA text callable; tests inject a fake callable.

    Provider configuration and credentials stay with the existing CPA adapter.
    The prompt reads only the final SRT. No ledger/persona/title feeds back into
    extraction; source identity fields are filled locally, never by the model.
    """

    llm_call: Callable[[str], str]
    extractor_id: str = "cpa-persona-facts"
    extractor_version: str = "v1"

    def __call__(self, sealed: pf.SealedFinalSubtitle) -> tuple[pf.ProposedFact, ...]:
        raw = sealed.final_srt_path.read_bytes()
        prompt = (
            "从最终字幕提取本频道主播明确自述的偏好、性格、经历、价值观、传记性关系。"
            "不要把他人的话、念弹幕、扮演、假设、作品或播放内容变成主播事实。没有事实就返回空数组。"
            "只提案，绝不接受事实。逐字引用必须来自指定 cue；不补造人物关系。"
            "kind/predicate 闭集：PREFERENCE=LIKES|DISLIKES；TRAIT=IS|HAS_HABIT；"
            "EXPERIENCE=DID；VALUE=HOLDS_VIEW；RELATIONSHIP=FRIEND|FAMILY|ROOMMATE|COLLEAGUE|OTHER_RELATION。"
            "context_kind=LIVE_ASSERTION|QUOTED|READ_ALOUD|ROLE_PLAY|MEDIA_PLAYBACK|HYPOTHETICAL|GAME_OR_FICTION|UNKNOWN。"
            "只返回 JSON 对象，格式：{\"facts\":[{\"claim\":{\"kind\":\"PREFERENCE\","
            "\"predicate\":\"LIKES\",\"object_text\":\"唱歌\",\"object_entity\":null,"
            "\"polarity\":\"AFFIRM\",\"temporal\":{\"timeless\":true,\"as_of_start\":null,\"as_of_end\":null}},"
            "\"statement_zh\":\"喜欢唱歌\",\"cue_index_start\":1,\"cue_index_end\":1,"
            "\"verbatim_quote\":\"字幕逐字原话\",\"context_kind\":\"LIVE_ASSERTION\"}]}\n\n最终字幕：\n"
            + raw.decode("utf-8-sig")
        )
        payload: Json = json.loads(self.llm_call(prompt))
        doc = _mapping(payload)
        if set(doc) != {"facts"}:
            raise ValueError("Extractor response contains non-proposal fields")
        proposals: list[pf.ProposedFact] = []
        for row in rows(doc["facts"]):
            if set(row) != {"claim", "statement_zh", "cue_index_start", "cue_index_end", "verbatim_quote", "context_kind"}:
                raise ValueError("Invalid extractor fact fields")
            claim = cast(pf.FactClaim, pf._decode(row["claim"], pf.FactClaim))
            context = cast(pf.ContextKind, pf._decode(row["context_kind"], pf.ContextKind))
            evidence = pf.SubtitleQuoteEvidence(sealed.candidate_id, sealed.recording_basename,
                sealed.recording_date, digest(raw), bare_sha(sealed.final_review_audit_sha256),
                integer(row["cue_index_start"]), integer(row["cue_index_end"]), 0, 0,
                text(row["verbatim_quote"]), (), "UNKNOWN", context, None,
                self.extractor_id, self.extractor_version, digest(prompt.encode()))
            proposals.append(pf.ProposedFact(claim, text(row["statement_zh"]), (evidence,)))
        return tuple(proposals)

    def propose(self, sealed: pf.SealedFinalSubtitle) -> Sequence[pf.ProposedFact]:
        return self(sealed)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def bare_sha(value: str) -> str:
    return value.removeprefix("sha256:")


def document(path: Path) -> JsonMap:
    def unique(pairs: list[tuple[str, Json]]) -> JsonMap:
        result: JsonMap = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result
    data: Json = json.loads(path.read_bytes(), object_pairs_hook=unique)
    if not isinstance(data, dict):
        raise ValueError("Expected a JSON object")
    return data


def rows(value: Json) -> list[JsonMap]:
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise ValueError("Expected object rows")
    return cast(list[JsonMap], value)


def text(value: Json) -> str:
    if not isinstance(value, str):
        raise ValueError("Expected text")
    return value


def integer(value: Json) -> int:
    if type(value) is not int:
        raise ValueError("Expected integer")
    return value


def _mapping(value: Json) -> JsonMap:
    if not isinstance(value, dict):
        raise ValueError("Expected object")
    return value


def _canonical(value: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKC", value)
                   if not c.isspace() and not unicodedata.category(c).startswith("P"))


@dataclass(frozen=True)
class ScreeningSources:
    """Explicit optional inputs; absence of a session ledger is not proof of solo."""

    session_relation_ledger: Path | None = None
    publication_registry: Path | None = None
    read_aloud_candidates: Path | None = None
    media_context: Path | None = None
    source_offset_ms: int = 0
    # Exact published SRT bindings, when available, are separate from BV joins.
    current_source_bindings: Path | None = None


@dataclass(frozen=True)
class ScreeningResult:
    proposals: tuple[pf.ProposedFact, ...]
    outcomes: tuple[pf.ScreenOutcome, ...]


def _cue_ids(row: JsonMap) -> set[int]:
    if "cue_indexes" in row:
        value = row["cue_indexes"]
        if not isinstance(value, list):
            raise ValueError("Invalid finding cue indexes")
        return {integer(v) for v in value}
    if "cue_index" in row:
        return {integer(row["cue_index"])}
    if "start_cue" in row and "end_cue" in row:
        return set(range(integer(row["start_cue"]), integer(row["end_cue"]) + 1))
    return set()


def _walk(value: Json) -> list[JsonMap]:
    found: list[JsonMap] = []
    if isinstance(value, dict):
        found.append(value)
        # Never interpret raw prompts, completions or context transcripts.
        for key, child in value.items():
            if key not in {"request", "raw_completion", "prompt", "raw", "terminal_context"}:
                found.extend(_walk(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_walk(child))
    return found


def _findings(audit: JsonMap, extra: JsonMap | None, ids: set[int]) -> tuple[pf.AdjudicatedFindingRef, ...]:
    result: list[pf.AdjudicatedFindingRef] = []
    sources: list[Json] = [audit.get(k) for k in
                           ("findings", "resolved_findings", "unresolved_findings_disclosed")]
    if extra is not None:
        sources.append(extra)
    def visit(value: Json) -> None:
        if isinstance(value, list):
            for child in value:
                visit(child)
            return
        if not isinstance(value, dict):
            return
        row = value
        hit = _cue_ids(row)
        terminal = row.get("terminal_review", row)
        # Native released findings nest the terminal result under one of these
        # adjudications (final_review_contract.is_release_resolved_keep).
        for key in ("exact_release_adjudication", "context_audio_adjudication"):
            adjudication = row.get(key)
            if isinstance(adjudication, dict) and "terminal_review" in adjudication:
                terminal = adjudication["terminal_review"]
                break
        rebuilt = row.get("rebuilt_finding")
        if not hit and isinstance(rebuilt, dict):
            hit = _cue_ids(rebuilt)
        if hit:
            if not hit & ids:
                return
            terminal = _mapping(terminal)
            grade, choice = terminal.get("grade"), terminal.get("choice")
            # A disputed cue without a complete terminal grade is ineligible.
            if grade not in get_args(pf.AcceptabilityGrade) or choice not in get_args(pf.TerminalChoice):
                grade, choice = "D", "UNRESOLVABLE"
            ref = pf.AdjudicatedFindingRef(
                str(row.get("finding_id") or row.get("check_id") or f"cue:{min(hit)}"),
                cast(pf.AcceptabilityGrade, grade), cast(pf.TerminalChoice, choice))
            if ref not in result:
                result.append(ref)
            # A finding is one record. Its request/context/embedded rebuilt row
            # must not be reinterpreted as additional ungraded findings.
            return
        for key, child in row.items():
            if key not in {"request", "raw_completion", "prompt", "raw", "terminal_context"}:
                visit(child)
    for source in sources:
        visit(source)
    return tuple(result)


def _publication(sealed: pf.SealedFinalSubtitle, sources: ScreeningSources) -> tuple[str | None, bool]:
    bvid: str | None = None
    superseded = False
    if sources.publication_registry is not None:
        # Explicit path suppresses the owner's implicit runtime registry lookup.
        from .publication_registry import load_publication_registry
        registry = cast(JsonMap, load_publication_registry(sources.publication_registry))
        matches = [r for r in rows(registry.get("entries"))
                   if r.get("candidate_id") == sealed.candidate_id
                   and r.get("recording_date") == sealed.recording_date
                   and r.get("status") == "published"]
        bvs = {text(r.get("bvid")) for r in matches}
        if len(bvs) > 1:
            raise ValueError("Ambiguous candidate publication")
        bvid = next(iter(bvs), None)
    if sources.current_source_bindings is not None:
        bindings = document(sources.current_source_bindings)
        if bindings.get("schema_version") != "persona-published-source-bindings.v1":
            raise ValueError("Invalid published source bindings")
        matched = [r for r in rows(bindings.get("entries"))
                   if r.get("candidate_id") == sealed.candidate_id or (bvid and r.get("bvid") == bvid)]
        if not matched:
            raise ValueError("Missing current source binding")
        superseded = any(bare_sha(text(r.get("final_srt_sha256"))) != bare_sha(sealed.final_srt_sha256)
                         for r in matched)
    return bvid, superseded


def _overlap_rows(path: Path | None, key: str, ids: set[int], start: int, end: int,
                  srt_sha: str, offset: int = 0) -> list[JsonMap]:
    if path is None:
        return []
    doc = document(path)
    # Artifacts with a binding must match; never use context from an old SRT.
    bound = doc.get("srt_sha256", doc.get("final_srt_sha256", doc.get("final_output_srt_sha256", doc.get("output_srt_sha256"))))
    if bound is None or bare_sha(text(bound)) != srt_sha:
        raise ValueError("Context SRT binding mismatch")
    result: list[JsonMap] = []
    values = rows(doc[key]) if key in doc else []
    if key == "candidates" and doc.get("schema_version") == "chat-authority-audit.v2":
        values += [row for name in ("applied", "superseded_chat_proposals", "read_aloud_arbitrations")
                   for row in rows(doc.get(name, []))]
    if key not in doc and not values:
        raise ValueError("Context artifact has no supported rows")
    for row in values:
        hit = bool(_cue_ids(row) & ids)
        window = row.get("source_window_ms")
        if isinstance(window, list) and len(window) == 2:
            hit |= start < integer(window[1]) and end > integer(window[0])
        elif "matched_start_ms" in row and "matched_end_ms" in row:
            # Native chat proposal clocks are clip-local; media source windows
            # are recording-local and already include acoustic padding.
            hit |= start - offset < integer(row["matched_end_ms"]) and end - offset > integer(row["matched_start_ms"])
        if hit:
            result.append(row)
    return result


def _rejected(claim: pf.FactClaim, evidence: tuple[pf.SubtitleQuoteEvidence, ...],
              ledger: pf.PersonaFactLedger) -> bool:
    fingerprint = pf.claim_fingerprint(claim)
    for head in pf.current_revisions(ledger):
        if head.status != "REJECTED" or head.claim_fingerprint != fingerprint:
            continue
        if head.rejection_scope in {"CLAIM", "CLAIM_UNLESS_HUMAN_EVIDENCE"}:
            return True
        old = {(e.final_srt_sha256, e.cue_index_start, e.cue_index_end, e.verbatim_quote)
               for e in head.evidence if isinstance(e, pf.SubtitleQuoteEvidence)}
        if any((e.final_srt_sha256, e.cue_index_start, e.cue_index_end, e.verbatim_quote) in old
               for e in evidence):
            return True
    key = _canonical(claim.object_entity.canonical if claim.object_entity else claim.object_text)
    return any(rule.kind == claim.kind and (rule.predicate is None or rule.predicate == claim.predicate)
               and key.startswith(_canonical(rule.object_key_prefix)) for rule in ledger.suppressions)


def screen(proposals: Sequence[pf.ProposedFact], *, ledger: pf.PersonaFactLedger,
           sealed: pf.SealedFinalSubtitle, sources: ScreeningSources = ScreeningSources()) -> ScreeningResult:
    """Rebuild all gate-relevant evidence; return machine proposals, never decisions."""
    heads = pf.current_revisions(ledger)
    raw = sealed.final_srt_path.read_bytes()
    sha = digest(raw)
    audit_raw = sealed.final_review_audit_path.read_bytes()
    audit = document(sealed.final_review_audit_path)
    valid_seal = (bare_sha(sealed.final_srt_sha256) == sha
                  and bare_sha(sealed.final_review_audit_sha256) == digest(audit_raw)
                  and audit.get("schema_version") == "final-review-audit.v2"
                  and bare_sha(text(audit.get("reviewed_srt_sha256", ""))) == sha
                  and audit.get("status") == "CLEAN" and audit.get("release_gate") == "PASS")
    cues = parse_srt_cues(raw.decode("utf-8-sig"))
    by_id = {int(c.index): c for c in cues}
    if len(by_id) != len(cues) or not cues:
        raise ValueError("Invalid/duplicate SRT cue ids")
    extra = document(sealed.terminal_adjudication_path) if sealed.terminal_adjudication_path else None
    if extra is not None and bare_sha(text(extra.get("srt_sha256", extra.get("final_srt_sha256", "")))) != sha:
        raise ValueError("Terminal adjudication SRT binding mismatch")
    attribution = document(sealed.speaker_attribution_path) if sealed.speaker_attribution_path else None
    if attribution is not None and (attribution.get("schema_version") != "speaker-attribution.v1"
                                   or bare_sha(text(attribution.get("srt_sha256", ""))) != sha):
        raise ValueError("Speaker attribution SRT binding mismatch")
    solo = False
    if sources.session_relation_ledger is not None:
        solo = resolve_session_relation(ledger_path=sources.session_relation_ledger,
            date=sealed.recording_date, recording_path=sealed.recording_basename) is None
    bvid, superseded = _publication(sealed, sources)
    entity = document(sealed.entity_projection_path) if sealed.entity_projection_path else None
    uncertain: set[str] = set()
    canonicals: set[tuple[str, str]] = set()
    if entity is not None:
        bound = entity.get("final_srt_sha256", entity.get("srt_sha256"))
        if bound is not None and bare_sha(text(bound)) != sha:
            raise ValueError("Entity projection SRT binding mismatch")
        for row in _walk(entity):
            keeps = row.get("uncertain_keep_canonicals", [])
            if isinstance(keeps, list):
                uncertain.update(text(v) for v in keeps)
            if row.get("canonical") and row.get("owner_key"):
                canonicals.add((text(row["owner_key"]), text(row["canonical"])))
                if row.get("status", row.get("resolution")) in {"UNCERTAIN", "UNKNOWN"}:
                    uncertain.add(text(row["canonical"]))
    normalized: list[pf.ProposedFact] = []
    outcomes: list[pf.ScreenOutcome] = []
    for proposal in proposals:
        reasons: list[pf.ScreenReason] = []
        def reject(reason: pf.ScreenReason) -> None:
            if reason not in reasons:
                reasons.append(reason)
        if not valid_seal:
            reject("SEALED_SRT_SHA_MISMATCH")
        if superseded:
            reject("SOURCE_SRT_SUPERSEDED")
        if proposal.claim.kind not in pf.MACHINE_PROPOSABLE_KINDS:
            reject("KIND_NOT_MACHINE_PROPOSABLE")
        if (proposal.statement_zh.lstrip(" \t-*#").startswith(pf.PROJECTION_RESERVED_LINE_PREFIXES)
                or any(s in proposal.statement_zh for s in ("\r", "\n", "<!--"))):
            reject("RESERVED_PROJECTION_PREFIX")
        ref = proposal.claim.object_entity
        if ((ref and (ref.resolution != "RESOLVED" or ref.canonical in uncertain
                      or (ref.owner_key, ref.canonical) not in canonicals))
                or any(name and name in proposal.claim.object_text for name in uncertain)):
            reject("ENTITY_UNRESOLVED")
        rebuilt: list[pf.SubtitleQuoteEvidence] = []
        if not proposal.evidence:
            reject("QUOTE_NOT_IN_SEALED_SRT")
        for evidence in proposal.evidence:
            if not isinstance(evidence, pf.SubtitleQuoteEvidence):
                raise ValueError("Only FINAL_SUBTITLE_TEXT evidence is accepted from machines")
            if (bare_sha(evidence.final_srt_sha256) != sha
                    or bare_sha(evidence.final_review_audit_sha256) != digest(audit_raw)
                    or evidence.candidate_id != sealed.candidate_id
                    or evidence.recording_basename != sealed.recording_basename
                    or evidence.recording_date != sealed.recording_date):
                reject("SEALED_SRT_SHA_MISMATCH")
            ids = set(range(evidence.cue_index_start, evidence.cue_index_end + 1))
            selected = [by_id[i] for i in sorted(ids) if i in by_id]
            joined = "\n".join(c.text for c in selected)
            if (not ids or ids - by_id.keys() or not evidence.verbatim_quote
                    or evidence.verbatim_quote not in joined):
                reject("QUOTE_NOT_IN_SEALED_SRT")
                rebuilt.append(evidence)
                continue
            start = min(c.start_ms for c in selected) + sources.source_offset_ms
            end = max(c.end_ms for c in selected) + sources.source_offset_ms
            findings = _findings(audit, extra, ids)
            if any(f.grade != "A" or f.choice in {"CONTEXT_FIT", "UNRESOLVABLE"} for f in findings):
                reject("CUE_OVERLAPS_UNCERTAIN_ADJUDICATION")
            context = evidence.context_kind
            read_aloud = _overlap_rows(sources.read_aloud_candidates, "candidates", ids, start, end, sha, sources.source_offset_ms)
            media = _overlap_rows(sources.media_context, "segments", ids, start, end, sha)
            if read_aloud:
                context = "READ_ALOUD"
            if any(r.get("kind") == "MEDIA" for r in media):
                context = "MEDIA_PLAYBACK"
            elif any(r.get("kind") == "UNKNOWN" for r in media):
                context = "UNKNOWN"
            basis: pf.SpeakerBasis = "UNKNOWN"
            if attribution is not None:
                labels = {integer(r.get("cue_index")): text(r.get("speaker"))
                          for r in rows(attribution.get("cues"))}
                voices = {labels.get(i, "UNKNOWN") for i in ids}
                if voices == {"HOST"}:
                    basis = "ATTRIBUTED_HOST"
                elif voices & {"OTHER", "NON_HOST"}:
                    basis = "ATTRIBUTED_NON_HOST"
                if "MEDIA" in voices:
                    context = "MEDIA_PLAYBACK"
                # Embedded media screening in the ordinary attribution artifact.
                media_screen = attribution.get("media_screen")
                if isinstance(media_screen, dict):
                    for segment in rows(media_screen.get("segments", [])):
                        window = segment.get("source_window_ms")
                        intersects = bool(_cue_ids(segment) & ids)
                        if isinstance(window, list) and len(window) == 2:
                            intersects |= start < integer(window[1]) and end > integer(window[0])
                        if intersects and segment.get("kind") != "LIVE":
                            context = "MEDIA_PLAYBACK" if segment.get("kind") == "MEDIA" else "UNKNOWN"
            elif solo and not read_aloud and not any(r.get("kind") != "LIVE" for r in media):
                basis = "UNIFORM_HOST_SINGLE_SPEAKER_SESSION"
            elif not solo:
                basis = "UNIFORM_HOST_UNVERIFIED"
            if basis not in {"ATTRIBUTED_HOST", "UNIFORM_HOST_SINGLE_SPEAKER_SESSION"}:
                reject("SPEAKER_NOT_ATTRIBUTED_HOST")
            if context != "LIVE_ASSERTION":
                reject("CONTEXT_NOT_LIVE_ASSERTION")
            rebuilt.append(replace(evidence, source_start_ms=start, source_end_ms=end,
                overlapping_adjudicated_findings=findings, speaker_basis=basis, context_kind=context,
                bvid=bvid, final_srt_sha256=sha, final_review_audit_sha256=digest(audit_raw)))
        item = replace(proposal, evidence=tuple(rebuilt))
        if _rejected(item.claim, item.evidence, ledger):
            reject("REJECTED_FINGERPRINT_MATCH")
        fingerprint = pf.claim_fingerprint(item.claim)
        if not reasons:
            # Reuse B1's semantic constraints (including kind/predicate pairs)
            # before an eligible row can enter the review queue.
            pf.current_revisions(replace(ledger, revisions=(pf.PersonaFactRevision(
                "pf-" + fingerprint, "r1", "PROPOSED", item.claim, fingerprint, item.statement_zh,
                pf.RenderSlot(pf.PROJECTION_GENERATED_SECTION_ZH, 0), item.evidence,
                "MACHINE_PROPOSAL", None, None, None, "SCREENING"),), suppressions=()))
        target = next((h for h in heads if h.status == "ACCEPTED" and h.claim_fingerprint == fingerprint), None)
        opposite = pf.claim_fingerprint(replace(item.claim, polarity="NEGATE" if item.claim.polarity == "AFFIRM" else "AFFIRM"))
        conflict = next((h for h in heads if h.status == "ACCEPTED" and h.claim_fingerprint == opposite), None)
        outcome: pf.ScreenOutcomeKind = "PROPOSED"
        if reasons:
            outcome = "SCREENED_OUT"
        elif target:
            outcome = "EVIDENCE_APPENDED"
        elif conflict:
            outcome = "CONFLICT_PROPOSED"
        normalized.append(item)
        outcomes.append(pf.ScreenOutcome(fingerprint, outcome, tuple(reasons),
                                        target.fact_id if target else conflict.fact_id if conflict else None))
    return ScreeningResult(tuple(normalized), tuple(outcomes))


def observations_from_corrections(sealed: pf.SealedFinalSubtitle, *, source_offset_ms: int = 0) -> tuple[pf.DirectionalSurfaceObservation, ...]:
    """Explicit corrections are owner-review observations, never fact evidence."""
    audit = document(sealed.final_review_audit_path)
    if (digest(sealed.final_review_audit_path.read_bytes()) != bare_sha(sealed.final_review_audit_sha256)
            or digest(sealed.final_srt_path.read_bytes()) != bare_sha(sealed.final_srt_sha256)):
        raise ValueError("Correction observation source binding mismatch")
    result: list[pf.DirectionalSurfaceObservation] = []
    for row in rows(audit.get("sealed_corrections", [])):
        result.append(pf.DirectionalSurfaceObservation(text(row.get("heard_surface")),
            text(row.get("resolved_canonical")), "HEARD_AS_TO_RESOLVED", "SOURCE_INTERVAL",
            sealed.recording_basename, integer(row.get("source_start_ms")),
            integer(row.get("source_end_ms")), sealed.candidate_id, text(row.get("adjudication_ref"))))
    # Native exact-final audit repair receipts use clip-local matched_* clocks.
    native = audit.get("exact_final_cpa_self_heal")
    if isinstance(native, dict):
        final_texts = {cue.text for cue in parse_srt_cues(sealed.final_srt_path.read_text(encoding="utf-8-sig"))}
        for pass_row in rows(native.get("passes", [])):
            for row in rows(pass_row.get("repairs", [])):
                if row.get("action") != "REPLACE_CUE_TEXT" or row.get("after") not in final_texts:
                    continue
                observation = pf.DirectionalSurfaceObservation(text(row.get("before")), text(row.get("after")),
                    "HEARD_AS_TO_RESOLVED", "SOURCE_INTERVAL", sealed.recording_basename,
                    integer(row.get("matched_start_ms")) + source_offset_ms,
                    integer(row.get("matched_end_ms")) + source_offset_ms, sealed.candidate_id,
                    text(row.get("finding_sha256", row.get("request_sha256"))))
                if observation not in result:
                    result.append(observation)
    return tuple(result)


def append_screened(ledger: pf.PersonaFactLedger, proposals: Sequence[pf.ProposedFact],
                    outcomes: Sequence[pf.ScreenOutcome], *, recorded_at: str) -> pf.PersonaFactLedger:
    """Pure in-memory candidate ledger; B2 does not persist this into assets."""
    if len(proposals) != len(outcomes):
        raise ValueError("Proposal/outcome length mismatch")
    result = ledger
    for proposal, outcome in zip(proposals, outcomes, strict=True):
        if outcome.proposal_claim_fingerprint != pf.claim_fingerprint(proposal.claim):
            raise ValueError("Proposal/outcome identity mismatch")
        if outcome.outcome == "SCREENED_OUT":
            continue
        heads = pf.current_revisions(result)
        head = next((h for h in heads if h.claim_fingerprint == outcome.proposal_claim_fingerprint), None)
        if head:
            fresh = tuple(e for e in proposal.evidence if e not in head.evidence)
            if not fresh:
                continue
            row = replace(head, revision=f"r{sum(r.fact_id == head.fact_id for r in result.revisions) + 1}",
                supersedes=head.revision, evidence=(*head.evidence, *fresh), recorded_at=recorded_at)
            if head.status != "ACCEPTED":
                row = replace(row, status="PROPOSED", rejection_scope=None, decided_by=None)
        else:
            row = pf.PersonaFactRevision("pf-" + outcome.proposal_claim_fingerprint, "r1", "PROPOSED",
                proposal.claim, outcome.proposal_claim_fingerprint, proposal.statement_zh,
                pf.RenderSlot(pf.PROJECTION_GENERATED_SECTION_ZH, len(heads)), proposal.evidence,
                "MACHINE_PROPOSAL", None, None, None, recorded_at)
        result = replace(result, revisions=(*result.revisions, row))
    pf.verify_append_only(ledger, result)
    return result


def render_summary(receipt: pf.ExtractionReceipt, sealed: pf.SealedFinalSubtitle, *, current_source_bound: bool = False) -> str:
    lines = ["# 人设提案批量审核", "", f"候选：{receipt.candidate_id}；日期：{sealed.recording_date}",
             f"最终字幕 SHA256：{receipt.final_srt_sha256}", "", "本页只供审核；没有新事实被接受或写回资产。", ""]
    if not current_source_bound:
        lines.extend(["BV 仅作历史发布连接；未提供当前发布 SRT 绑定，版本新旧未核验。", ""])
    for index, (proposal, outcome) in enumerate(zip(receipt.proposals, receipt.outcomes, strict=True), 1):
        lines.extend([f"## {index}. {proposal.statement_zh}", "", f"建议：{outcome.outcome}；状态：PROPOSED（筛掉项只保留回执）",
                      f"拒绝码：{', '.join(outcome.reasons) or '无'}", ""])
        for e in proposal.evidence:
            lines.extend([f"> {e.verbatim_quote.replace(chr(10), chr(10) + '> ')}", "",
                f"出处：{e.bvid or '尚无 BV'} / {e.recording_date} / cue {e.cue_index_start}–{e.cue_index_end}",
                f"说话人：{e.speaker_basis}；语境：{e.context_kind}", ""])
            if e.speaker_basis == "UNIFORM_HOST_SINGLE_SPEAKER_SESSION":
                lines.extend(["说话人未经声纹证实。", ""])
        if outcome.outcome == "CONFLICT_PROPOSED":
            lines.extend([f"CONFLICT：与 {outcome.target_fact_id} 极性相反，请 公开规则裁定。", ""])
    for owner in receipt.owner_proposals:
        observation = owner.observation
        if observation:
            lines.extend([f"方向性观察：{observation.heard_surface} → {observation.resolved_canonical}；"
                f"录播 {observation.recording_basename}，源区间 {observation.source_start_ms}–{observation.source_end_ms} ms。",
                f"建议交 {owner.target} 人工审核；{owner.verification}。", ""])
    return "\n".join(lines)


def run_extraction(*, sealed: pf.SealedFinalSubtitle, ledger: pf.PersonaFactLedger,
                   sources: ScreeningSources, extractor: Extractor, output_dir: Path,
                   extractor_id: str, extractor_version: str) -> Path:
    """Cache extraction by SRT; changed screening inputs re-screen without a model call."""
    # The caller supplies a dedicated output directory, disjoint from every input.
    inputs = [sealed.final_srt_path, sealed.final_review_audit_path, sealed.speaker_attribution_path,
              sealed.terminal_adjudication_path, sealed.entity_projection_path,
              sources.session_relation_ledger, sources.publication_registry, sources.read_aloud_candidates,
              sources.media_context, sources.current_source_bindings]
    destination = output_dir.resolve()
    if any(p is not None and (p.resolve() == destination or destination in p.resolve().parents)
           for p in inputs):
        raise ValueError("Output directory overlaps read-only inputs")
    sha = bare_sha(sealed.final_srt_sha256)
    if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
        raise ValueError("Invalid SRT sha256")
    batch = output_dir / sha
    receipt_path = batch / "extraction-receipt.json"
    identity = {"candidate_id": sealed.candidate_id, "recording_date": sealed.recording_date,
                "recording_basename": sealed.recording_basename, "profile_id": ledger.profile_id,
                "extractor_id": extractor_id, "extractor_version": extractor_version}
    input_identity = json.dumps({"identity": identity, "sealed": asdict(sealed),
        "screening_policy_sha256": digest(Path(__file__).read_bytes()),
        "sources": asdict(sources), "ledger": asdict(ledger),
        "files": [(str(p), digest(p.read_bytes())) for p in inputs if p is not None]},
        ensure_ascii=False, sort_keys=True, default=str)
    fingerprint = digest(input_identity.encode())
    completion_path = batch / "complete.json"
    if completion_path.exists():
        complete = document(completion_path)
        files = _mapping(complete.get("files"))
        if (complete.get("input_sha256") == fingerprint
                and set(files) == {"extraction-receipt.json", "channel_knowledge_review_queue.jsonl", "review-summary.md"}
                and all(
                (batch / name).is_file() and digest((batch / name).read_bytes()) == value
                for name, value in files.items())):
            return batch
    cache_path = batch / "raw-proposals.json"
    batch.mkdir(parents=True, exist_ok=True)
    if cache_path.exists():
        cache = document(cache_path)
        if cache.get("identity") != identity:
            raise ValueError("This SRT already has a different extraction identity; use a separate output directory")
        proposals = cast(tuple[pf.ProposedFact, ...], pf._decode(cache.get("proposals"), tuple[pf.ProposedFact, ...]))
    else:
        # Check source bindings before an injected adapter could spend a request.
        raw_srt = sealed.final_srt_path.read_bytes()
        audit = document(sealed.final_review_audit_path)
        if (digest(raw_srt) != sha or digest(sealed.final_review_audit_path.read_bytes()) != bare_sha(sealed.final_review_audit_sha256)
                or audit.get("schema_version") != "final-review-audit.v2" or audit.get("status") != "CLEAN"
                or audit.get("release_gate") != "PASS" or bare_sha(text(audit.get("reviewed_srt_sha256", ""))) != sha):
            raise ValueError("Sealed SRT/audit not eligible for extraction")
        proposals = tuple(extractor(sealed))
        # Validate the injected callable's output just like the saved-JSON boundary.
        serialized: Json = json.loads(json.dumps([asdict(p) for p in proposals], ensure_ascii=False))
        proposals = cast(tuple[pf.ProposedFact, ...], pf._decode(serialized, tuple[pf.ProposedFact, ...]))
        with cache_path.open("x", encoding="utf-8") as handle:
            json.dump({"identity": identity, "proposals": [asdict(p) for p in proposals]}, handle, ensure_ascii=False)
    result = screen(proposals, ledger=ledger, sealed=sealed, sources=sources)
    observations = () if any("SEALED_SRT_SHA_MISMATCH" in outcome.reasons for outcome in result.outcomes) else observations_from_corrections(
        sealed, source_offset_ms=sources.source_offset_ms)
    owners = tuple(pf.OwnerLedgerProposal("observation-" + digest(json.dumps(asdict(o), sort_keys=True).encode()),
        "entity_confusables", o.heard_surface, (), o, "HUMAN_REVIEW", None)
        for o in observations)
    receipt = pf.ExtractionReceipt(pf.EXTRACTION_RECEIPT_SCHEMA, sealed.candidate_id, sha,
                                   extractor_id, extractor_version, result.proposals, result.outcomes, owners)
    queue = batch / "channel_knowledge_review_queue.jsonl"
    entries: list[str] = []
    for proposal, outcome in zip(receipt.proposals, receipt.outcomes, strict=True):
        if outcome.outcome != "SCREENED_OUT":
            entries.append(json.dumps({"schema_version": pf.REVIEW_QUEUE_SCHEMA, "status": "PROPOSED",
                "target": "persona_facts", "proposal": asdict(proposal), "screen": asdict(outcome)}, ensure_ascii=False))
    entries.extend(json.dumps({"schema_version": pf.REVIEW_QUEUE_SCHEMA, "status": "PROPOSED",
                              "owner_proposal": asdict(owner)}, ensure_ascii=False) for owner in owners)
    def atomic(path: Path, content: str) -> None:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=batch, delete=False) as handle:
            handle.write(content)
            temporary = Path(handle.name)
        temporary.replace(path)
    atomic(queue, "\n".join(entries) + ("\n" if entries else ""))
    atomic(batch / "review-summary.md", render_summary(receipt, sealed, current_source_bound=sources.current_source_bindings is not None))
    atomic(receipt_path, json.dumps(asdict(receipt), ensure_ascii=False, indent=2) + "\n")
    artifacts = (receipt_path, queue, batch / "review-summary.md")
    atomic(completion_path, json.dumps({"input_sha256": fingerprint,
        "files": {p.name: digest(p.read_bytes()) for p in artifacts}}, sort_keys=True) + "\n")
    return batch
