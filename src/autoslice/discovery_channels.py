"""One task entrance for discovery channels (ASR rules R1--R6).

Channels nominate; they never supply votes, mutation or release authority. Raw
JSON is confined to the legacy serialization boundary. The registry, task and
closed ledger passed between discovery and the scheduler are immutable values.
"""
from __future__ import annotations

from .review_errors import ContractRejection

from dataclasses import dataclass
from enum import StrEnum
import hashlib
import json
from pathlib import Path
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Callable, Protocol, TypeAlias, cast

if TYPE_CHECKING:
    from src.autoslice.finding_closure_ports import (
        DiscoveryInputs, DiscoveryPorts, PreparedFinding, ScanRequest, ScanResult, ValidationRequest,
        InputUpdateResult,
    )
    from src.autoslice.finding_closure import ClosureState
    from src.autoslice.finding_closure_shell import FileLedger

from src.autoslice.scan_coverage import CueVersion, cue_versions

Json: TypeAlias = None | bool | int | float | str | list['Json'] | dict[str, 'Json']
JsonObject: TypeAlias = dict[str, Json]
NONE_DISCOVERY_ONLY = 'NONE_DISCOVERY_ONLY'
TRUSTED_PRIORITY_CANDIDATE = object()


class DiscoveryContractError(ContractRejection):
    """An explicit discovery contract rejection, never a generic runtime error."""


class Channel(StrEnum):
    TEXT = 'A'
    BLIND_SHORT = 'B'
    FULL_TRANSCRIPT = 'C0'
    RANGE_TRANSCRIPT = 'C1'
    RESTATEMENT = 'D'
    PRONOUN = 'E'
    OPEN_LEDGER = 'F'
    LANGUAGE = 'G'
    GIFT = 'H'
    POSTBURN = 'I'


ASR_CHANNELS = frozenset({Channel.FULL_TRANSCRIPT, Channel.RANGE_TRANSCRIPT, Channel.POSTBURN})


@dataclass(frozen=True)
class ChannelSpec:
    channel: Channel
    producer: str
    first_scan: bool
    recompute_without_call: bool


CHANNELS = (
    ChannelSpec(Channel.TEXT, 'final_review_auditor.audit_final_subtitles', True, False),
    ChannelSpec(Channel.BLIND_SHORT, 'microcue_acoustic_discovery.discover_microcue_findings', True, True),
    ChannelSpec(Channel.FULL_TRANSCRIPT, 'native_context_witness.observe_range_context', True, True),
    ChannelSpec(Channel.RANGE_TRANSCRIPT, 'independent_context_adjudication.validate_range_acoustic_observation', True, True),
    ChannelSpec(Channel.RESTATEMENT, 'restatement_recall.discover_restatement_findings', True, True),
    ChannelSpec(Channel.PRONOUN, 'pronoun_consistency.discover_candidate_pronoun_findings', True, False),
    ChannelSpec(Channel.OPEN_LEDGER, 'span_finding_obligations.pending_findings', True, True),
    ChannelSpec(Channel.LANGUAGE, 'audio_language_discovery.current_language_discovery_context', True, True),
    ChannelSpec(Channel.GIFT, 'editorial_subtitle.gift_nominations', True, True),
    ChannelSpec(Channel.POSTBURN, 'witness_disagreement.find_witness_disagreements', False, True),
)

# DESIGN-V2 / TASK-3: strength is the heterogeneous channel set, never three
# repetitions of the text prompt. C's core, shell and cold reader import these.
DEFAULT_TEXT_LANES = 1
SCAN_CHANNELS = tuple(spec.channel for spec in CHANNELS if spec.first_scan)
DEFAULT_SCAN_N = len(SCAN_CHANNELS)


def scan_channels() -> tuple[str, ...]:
    return tuple(str(channel) for channel in SCAN_CHANNELS)


def blind_scan_complete(srt_text: str, audit: object) -> bool:
    return bool(isinstance(audit, Mapping) and audit.get('status') == 'PASS'
        and audit.get('srt_sha256') == 'sha256:' + _sha(srt_text))


def language_scan_complete(authority: object) -> bool:
    audit = authority.get('audio_language_discovery_audit') if isinstance(authority, Mapping) else None
    scan = audit.get('scan') if isinstance(audit, Mapping) else None
    return bool(isinstance(scan, Mapping) and scan.get('status') == 'OBSERVED')


def json_value(value: object) -> Json:
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, (list, tuple)):
        return [json_value(v) for v in value]
    if isinstance(value, Mapping) and all(isinstance(k, str) for k in value):
        return {str(k): json_value(v) for k, v in value.items()}
    raise DiscoveryContractError('DISCOVERY_NON_JSON_VALUE')


def json_object(value: object) -> JsonObject:
    result = json_value(value)
    if not isinstance(result, dict):
        raise DiscoveryContractError('DISCOVERY_OBJECT_REQUIRED')
    return result


def _dump(value: object) -> str:
    return json.dumps(json_value(value), ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


@dataclass(frozen=True)
class SourceIdentity:
    candidate_id: str
    media_sha256s: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.candidate_id or not self.media_sha256s or any(
            len(s.removeprefix('sha256:')) != 64 or any(c not in '0123456789abcdef'
                for c in s.removeprefix('sha256:')) for s in self.media_sha256s):
            raise DiscoveryContractError('DISCOVERY_SOURCE_IDENTITY_INVALID')


@dataclass(frozen=True)
class Nomination:
    channel: Channel
    cue_index: int
    payload_json: str
    observation_id: str
    provider: str = ''
    model: str = ''
    reading: str = ''
    origin_channels: tuple[Channel, ...] = ()

    def __post_init__(self) -> None:
        if type(self.cue_index) is not int or self.cue_index < 1 or not self.observation_id:
            raise DiscoveryContractError('DISCOVERY_NOMINATION_INVALID')
        json_object(cast(object, json.loads(self.payload_json)))


@dataclass(frozen=True)
class DiscoveryBatch:
    nominations: tuple[Nomination, ...] = ()
    source: SourceIdentity | None = None
    source_offset_ms: int = 0

    def __post_init__(self) -> None:
        if type(self.source_offset_ms) is not int or self.source_offset_ms < 0:
            raise DiscoveryContractError('DISCOVERY_SOURCE_CLOCK_INVALID')


@dataclass(frozen=True)
class ClosedFinding:
    question_key: str
    materials_sha256: str


class ClosedLedger(Protocol):
    def contains(self, question_key: str, materials_sha256: str) -> bool: ...


@dataclass(frozen=True)
class ClosedFindings:
    entries: tuple[ClosedFinding, ...] = ()

    def contains(self, question_key: str, materials_sha256: str) -> bool:
        return ClosedFinding(question_key, materials_sha256) in self.entries


@dataclass(frozen=True)
class DiscoveryTask:
    question_key: str
    materials_sha256: str
    cue_index: int
    channels: tuple[Channel, ...]
    nominations: tuple[Nomination, ...]
    observation_ids: tuple[str, ...]
    source_bound: bool

    @property
    def asr_only(self) -> bool:
        return bool(self.channels) and set(self.channels) <= ASR_CHANNELS


@dataclass(frozen=True)
class DiscoveryResult:
    tasks: tuple[DiscoveryTask, ...]
    materials_only: tuple[Nomination, ...]
    excluded: tuple[str, ...]


class _Key(Protocol):
    def __call__(self, check: JsonObject) -> str: ...


class _Materials(Protocol):
    def __call__(self, check: JsonObject, *, candidates: tuple[str, ...],
        observation_ids: tuple[str, ...], contracts: JsonObject,
        model_identity: JsonObject) -> JsonObject: ...


def _check(cue: CueVersion, text: str, batch: DiscoveryBatch, srt_text: str) -> JsonObject:
    return {'matched_start_ms': cue.start_ms, 'matched_end_ms': cue.end_ms,
        'source_media_timeline_offset_ms': batch.source_offset_ms,
        'current_cue': text, 'cue_indexes': [cue.cue_index], 'whole_clip_current_srt': srt_text,
        'clip_context_binding': {'candidate_id': batch.source.candidate_id,
            'source_media_sha256s': list(batch.source.media_sha256s)} if batch.source else None}


def _origin_channels(row: Nomination) -> tuple[Channel, ...]:
    return row.origin_channels if row.channel == Channel.OPEN_LEDGER and row.origin_channels else (row.channel,)


def _asr_nomination(row: Nomination) -> bool:
    return bool(_origin_channels(row)) and set(_origin_channels(row)) <= ASR_CHANNELS


def model_identity_key(model: str) -> str:
    """Provider path aliases do not manufacture a different listening model."""
    from src.autoslice.cpa_gemini_client import enabled, judgment_identity
    if enabled():
        provider, canonical = judgment_identity('', model)
        if provider == 'gemini':
            return canonical
    return model.strip().lower().rsplit('/', 1)[-1].removesuffix(':latest').strip()


def _asr_opens(rows: tuple[Nomination, ...], current_text: str) -> bool:
    """R2's sole definition: independent ASRs agree on a different reading."""
    from src.autoslice.witness_disagreement import agreement_key, reading_key
    asr: list[tuple[Nomination, str]] = []
    for row in rows:
        if not _asr_nomination(row) or not row.reading:
            continue
        payload = json_object(cast(object, json.loads(row.payload_json)))
        provenance_value = payload.get('candidate_provenance')
        provenance = provenance_value if isinstance(provenance_value, dict) else {}
        proposal = payload.get('proposed_full_cue')
        heard = proposal if isinstance(proposal, str) else provenance.get('witness_span')
        current = current_text if isinstance(proposal, str) else provenance.get('current_span')
        if isinstance(heard, str) and isinstance(current, str) and reading_key(heard) != reading_key(current):
            asr.append((row, agreement_key(heard)))
    return any(a.provider.strip() and b.provider.strip() and a.provider.strip().lower() != b.provider.strip().lower()
        and model_identity_key(a.model) and model_identity_key(b.model)
        and model_identity_key(a.model) != model_identity_key(b.model) and a.reading == b.reading and a_words == b_words
        for i, (a, a_words) in enumerate(asr) for b, b_words in asr[i + 1:])


def union_discovery(
    srt_text: str, batch: DiscoveryBatch, *, scope_cues: frozenset[int] | None = None,
    closed_ledger: ClosedLedger = ClosedFindings(),
) -> DiscoveryResult:
    from src.autoslice.finding_context import question_key, materials_for_check
    from src.autoslice.jingting_chunker import parse_srt_cues
    cues = cue_versions(srt_text)
    texts = [c.text for c in parse_srt_cues(srt_text) if c.text.strip()]
    scope = frozenset(range(1, len(cues) + 1)) if scope_cues is None else scope_cues
    if any(type(i) is not int for i in scope) or not scope <= set(range(1, len(cues) + 1)):
        raise DiscoveryContractError('DISCOVERY_SCOPE_INVALID')
    groups: dict[int, list[Nomination]] = {}
    for row in batch.nominations:
        if row.cue_index in scope:
            if row.cue_index > len(cues):
                raise DiscoveryContractError('DISCOVERY_CUE_INVALID')
            groups.setdefault(row.cue_index, []).append(row)
    tasks: list[DiscoveryTask] = []
    materials: list[Nomination] = []
    excluded: list[str] = []
    for index, group in sorted(groups.items()):
        rows = tuple(sorted(set(group), key=lambda r: (r.channel, r.observation_id, r.payload_json)))
        channels = tuple(sorted({channel for r in rows for channel in _origin_channels(r)}))
        # A single ASR may enrich a task opened by another channel, never create it.
        if set(channels) <= ASR_CHANNELS and not _asr_opens(rows, texts[index - 1]):
            materials.extend(rows)
            continue
        check = _check(cues[index - 1], texts[index - 1], batch, srt_text)
        observations = tuple(sorted({r.observation_id for r in rows}))
        candidates = tuple(sorted({str(json_object(cast(object, json.loads(r.payload_json)))
            .get('proposed_full_cue')) for r in rows
            if isinstance(json_object(cast(object, json.loads(r.payload_json))).get('proposed_full_cue'), str)}))
        if batch.source:
            p = cast(_Key, question_key)(check)
            m = cast(_Materials, materials_for_check)(check,
                candidates=(texts[index - 1], *candidates), observation_ids=observations,
                contracts={'discovery': 'channels.v1', 'asr_rules': 'R1-R6'},
                model_identity={'role': 'discovery-no-vote'})
            material_sha = str(m['materials_sha256'])
        else:
            # Unbound legacy unit callers may inspect findings, but cannot consume
            # source-bound closed rulings or produce a reusable scientific identity.
            p = 'unbound:' + _sha(_dump([cues[index - 1].start_ms, cues[index - 1].end_ms]))
            material_sha = _sha(_dump([texts[index - 1], candidates, observations]))
        if batch.source and closed_ledger.contains(p, material_sha):
            excluded.append(p)
            continue
        tasks.append(DiscoveryTask(p, material_sha, index, channels, rows, observations,
            batch.source is not None))
    return DiscoveryResult(tuple(tasks), tuple(materials), tuple(excluded))


def source_identity(context: object, source_sha256: str | None = None) -> SourceIdentity | None:
    if not isinstance(context, Mapping):
        return None
    pieces = context.get('pieces')
    shas = tuple(sorted({str(p['source_media_sha256']).removeprefix('sha256:')
        for p in (pieces if isinstance(pieces, (list, tuple)) else ())
        if isinstance(p, Mapping) and p.get('source_media_sha256')}))
    if not shas and source_sha256:
        shas = (source_sha256.removeprefix('sha256:'),)
    candidate = context.get('candidate_id')
    return SourceIdentity(candidate, shas) if isinstance(candidate, str) and candidate and shas else None


def channel_for_row(value: object) -> Channel:
    """Legacy boundary only; production producers can specify their channel."""
    row = json_object(value)
    if row.get('witness_consistency_role') == 'QUESTION_ONLY_NOT_A_VETO':
        return Channel.BLIND_SHORT
    if str(row.get('why') or '').startswith('[候选级代词一致性逐项回执 '):
        return Channel.PRONOUN
    provenance = row.get('candidate_provenance')
    kind = provenance.get('kind') if isinstance(provenance, dict) else None
    return {'candidate_blind_acoustic_discovery': Channel.BLIND_SHORT,
        'microcue_acoustic_discovery': Channel.BLIND_SHORT,
        'session_transcript_recurrence': Channel.RESTATEMENT,
        'session_restatement': Channel.RESTATEMENT,
        'microcue_candidate_blind_acoustic_discovery': Channel.BLIND_SHORT,
        'candidate_pronoun_consistency': Channel.PRONOUN,
        'audio_language_discovery': Channel.LANGUAGE,
        'subtitle_audio_witness_disagreement': Channel.POSTBURN,
        'native_fullclip_disagreement': Channel.FULL_TRANSCRIPT,
        'range_transcript_disagreement': Channel.RANGE_TRANSCRIPT,
    }.get(str(kind), Channel.OPEN_LEDGER)


def nominations_from_rows(rows: Sequence[object], *, channel: Channel | None = None) -> tuple[Nomination, ...]:
    result: list[Nomination] = []
    for value in rows:
        row = json_object(value)
        index = row.get('cue', row.get('cue_index'))
        if type(index) is not int or index < 1:
            continue
        selected = channel or channel_for_row(row)
        provenance = row.get('candidate_provenance')
        prov = provenance if isinstance(provenance, dict) else {}
        payload = _dump(row)
        saved_task = row.get('discovery_task')
        # Reviewer labels, reasons and retry prose are not new observations.
        # Candidates and actual bound receipt ids enter M; render-only prose
        # must not reopen a closed P on identical local material.
        stable_material = {key: row.get(key) for key in
            ('proposed_full_cue', 'source_surface', 'candidate_memory_id', 'evidence_cue_ids')}
        observation_id = str(prov.get('observation_id') or prov.get('witness_request_sha256')
            or _sha(_dump([str(selected), stable_material])))
        origin_values = saved_task.get('channels') if isinstance(saved_task, dict) else None
        origins = (tuple(Channel(str(c)) for c in origin_values)
            if selected == Channel.OPEN_LEDGER and isinstance(origin_values, list) else ())
        result.append(Nomination(selected, index, payload, observation_id,
            str(prov.get('provider') or ''), str(prov.get('model') or ''),
            str(prov.get('reading_key') or ''), origins))
    return tuple(result)


def batch_from_rows(rows: Sequence[object], *, context: object = None,
    source_offset_ms: int = 0, source_sha256: str | None = None,
) -> DiscoveryBatch:
    return DiscoveryBatch(nominations_from_rows(rows), source_identity(context, source_sha256), source_offset_ms)


def task_rows(result: DiscoveryResult) -> list[JsonObject]:
    output: list[JsonObject] = []
    for task in result.tasks:
        # An independent A opening can coincide with an existing producer
        # candidate. Project that candidate once, retaining all task sources.
        # Distinct producer receipts and legacy A rows remain intact here.
        producer_candidates: set[str] = set()
        for nomination in sorted(task.nominations, key=lambda n: n.channel == Channel.TEXT):
            row = json_object(cast(object, json.loads(nomination.payload_json)))
            candidate_key = _dump({k: row.get(k) for k in ('suspect', 'proposed_full_cue',
                'span_start_codepoint', 'span_end_codepoint', 'repair_class')})
            if nomination.channel == Channel.TEXT and candidate_key in producer_candidates:
                continue
            if nomination.channel != Channel.TEXT:
                producer_candidates.add(candidate_key)
            row['_discovery_channel'] = str(nomination.channel)
            row['discovery_task'] = json_object({'physical_question_sha256': task.question_key,
                'materials_sha256': task.materials_sha256, 'channels': list(task.channels),
                'bound_observation_ids': list(task.observation_ids), 'asr_only': task.asr_only,
                'source_bound': task.source_bound,
                'nominating_models': sorted({n.model for n in task.nominations if _asr_nomination(n) and n.model})})
            if _asr_nomination(nomination):
                row['decision_authority'] = NONE_DISCOVERY_ONLY
                row['force_acoustic'] = True
                provenance = row.get('candidate_provenance')
                if isinstance(provenance, dict):
                    provenance['decision_authority'] = NONE_DISCOVERY_ONLY
                    provenance['mutation_authorized'] = False
            output.append(row)
    return output


class PriorityRow(Protocol):
    """Opaque legacy parser row; typed tasks never store its private token."""
    def __setitem__(self, key: str, value: object) -> None: ...
    def __getitem__(self, key: str) -> object: ...
    def get(self, key: str, default: object = None) -> object: ...


def trust_priority_row(value: object) -> PriorityRow:
    if not isinstance(value, Mapping):
        raise DiscoveryContractError('DISCOVERY_OBJECT_REQUIRED')
    result = cast(PriorityRow, dict(value))
    result['_trusted_priority_candidate'] = TRUSTED_PRIORITY_CANDIDATE
    return result


@dataclass(frozen=True)
class ExhaustionEvidence:
    relisten_models: tuple[str, ...] = ()
    context_exhausted: bool = False
    restatement_exhausted: bool = False
    structured_text_exhausted: bool = False
    phonetics_exhausted: bool = False


def validate_terminal_choice(task: DiscoveryTask, choice: str, evidence: ExhaustionEvidence) -> None:
    """R4 uses the existing policy; R5 adds an evidence prerequisite, no new state."""
    from src.autoslice.subtitle_acceptability_policy import load_policy
    policy = cast(Callable[[], object], load_policy)()
    choices = json_object(policy).get('terminal_choices')
    if not isinstance(choices, list) or choice not in choices:
        raise DiscoveryContractError('DISCOVERY_TERMINAL_CHOICE_INVALID')
    if choice == 'UNRESOLVABLE' and task.asr_only:
        nominators = {model_identity_key(r.model) for r in task.nominations if _asr_nomination(r)}
        if not ({key for model in evidence.relisten_models if (key := model_identity_key(model))} - nominators and evidence.context_exhausted
            and evidence.restatement_exhausted and evidence.structured_text_exhausted
            and evidence.phonetics_exhausted):
            raise DiscoveryContractError('ASR_ONLY_CANNOT_SUSTAIN_UNRESOLVABLE')


@dataclass(frozen=True)
class ObservationScan:
    nominations: tuple[Nomination, ...]
    rejection_reasons: tuple[str, ...]
    excluded_context_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class LanguageSpan:
    start_ms: int
    end_ms: int
    scripts: frozenset[str]


def language_spans_from_audit(value: object) -> tuple[LanguageSpan, ...]:
    """Project the existing language owner's already validated scan, no detector."""
    if not isinstance(value, Mapping):
        return ()
    audit = json_object(value)
    scan_value = audit.get('scan')
    scan = scan_value if isinstance(scan_value, dict) else {}
    spans = scan.get('foreign_spans', [])
    result: list[LanguageSpan] = []
    if isinstance(spans, list):
        for item in spans:
            if not isinstance(item, dict):
                continue
            a, b = item.get('start_ms'), item.get('end_ms')
            scripts = {'ja': frozenset({'han', 'kana'}), 'en': frozenset({'latin'}),
                'mixed': frozenset({'han', 'kana', 'latin', 'hangul'}),
                'other': frozenset({'han', 'kana', 'latin', 'hangul'})}.get(str(item.get('language')))
            if type(a) is int and type(b) is int and 0 <= a < b and scripts:
                result.append(LanguageSpan(a, b, scripts))
    return tuple(result)


def observation_id(value: object) -> str:
    observation = json_object(value)
    bindings_value, verdict_value = observation.get('artifact_bindings'), observation.get('verdict')
    bindings = bindings_value if isinstance(bindings_value, dict) else {}
    verdict = verdict_value if isinstance(verdict_value, dict) else {}
    return str(bindings.get('raw_response_sha256') or verdict.get('response_sha256') or '').removeprefix('sha256:')


def observation_nominations(
    srt_text: str, *, provider: str, model: str, observation_id: str,
    segments: tuple['NativeSegment', ...], window_ms: tuple[int, int],
    source_offset_ms: int, channel: Channel, expected_scripts: frozenset[str] = frozenset(),
) -> ObservationScan:
    """Already validated provider output -> questions, still no task admission.

    This pure seam is also used for recorded evidence replay. Runtime callers
    use validated_observation_nominations to reopen source/response bindings.
    """
    from src.autoslice.witness_disagreement import (
        find_witness_disagreements, native_script_mismatch, reading_key, to_priority_findings,
    )
    if channel not in ASR_CHANNELS:
        raise DiscoveryContractError('DISCOVERY_OBSERVATION_CHANNEL_INVALID')
    if native_script_mismatch(srt_text, segments, expected_scripts):
        return ObservationScan((), ('WITNESS_SCRIPT_MISMATCH',), (observation_id,))
    nominations = find_witness_disagreements(srt_text, '', intro_offset_ms=source_offset_ms,
        native_segments=segments, observation_window_ms=window_ms,
        expected_scripts=expected_scripts, include_rejected=True)
    rejected = tuple(sorted({str(n['reject_reason']) for n in nominations if n.get('reject_reason')}))
    accepted = [n for n in nominations if not n.get('reject_reason')]
    rows = to_priority_findings(accepted, final_srt_text=srt_text,
        witness_srt_sha256='sha256:' + observation_id.removeprefix('sha256:'))
    result: list[Nomination] = []
    for row, nom in zip(rows, accepted):
        row['decision_authority'] = NONE_DISCOVERY_ONLY
        provenance = row['candidate_provenance']
        # Require agreement on both the physical span and normalized full reading,
        # rather than merely two providers reporting some difference on one cue.
        key = _dump([nom['final_span_start_codepoint'], nom['final_span_end_codepoint'],
            reading_key(str(nom['proposed_full_cue'] or nom['witness_span']))])
        provenance.update(provider=provider, model=model, observation_id=observation_id,
            discovery_channel=str(channel), reading_key=key)
        result.append(Nomination(channel, int(row['cue']), _dump(row), observation_id,
            provider, model, key))
    return ObservationScan(tuple(result), rejected,
        (observation_id,) if 'WITNESS_SCRIPT_MISMATCH' in rejected else ())


from src.autoslice.witness_disagreement import NativeSegment


class _BuildCheck(Protocol):
    def __call__(self, srt_text: str, finding: JsonObject, *, clip_context: object,
        source_media_timeline_offset_ms: int,
        require_complete_utterance_support: bool = False) -> JsonObject: ...


class _ValidateRange(Protocol):
    def __call__(self, srt_text: str, current_request: JsonObject,
        observation: JsonObject) -> tuple[JsonObject | None, str | None]: ...


def validated_observation_nominations(
    srt_text: str, observations: Sequence[object], *, context: object,
    source_offset_ms: int, channel: Channel,
    language_spans: tuple[LanguageSpan, ...] = (),
) -> ObservationScan:
    from src.autoslice.final_review_auditor import build_context_adjudication_request
    from src.autoslice.independent_context_adjudication import validate_range_acoustic_observation
    from src.autoslice.jingting_chunker import parse_srt_cues
    cues = cue_versions(srt_text)
    texts = [c.text for c in parse_srt_cues(srt_text) if c.text.strip()]
    result: list[Nomination] = []
    rejected: list[str] = []
    excluded: list[str] = []
    for value in observations:
        observation = json_object(value)
        validated = None
        reason = None
        broad_value = observation.get('witness_request')
        broad = broad_value if isinstance(broad_value, dict) else {}
        a, b, offset = (broad.get(k) for k in
            ('matched_start_ms', 'matched_end_ms', 'source_media_timeline_offset_ms'))
        eligible = [cue for cue in cues if (type(a) is not int or type(b) is not int
            or type(offset) is not int or a + offset <= cue.start_ms + source_offset_ms
            and cue.end_ms + source_offset_ms <= b + offset)]
        for cue in eligible:
            check = cast(_BuildCheck, build_context_adjudication_request)(srt_text,
                {'cue_index': cue.cue_index, 'proposed_full_cue': texts[cue.cue_index - 1],
                 'suggestion': texts[cue.cue_index - 1], 'suspect': texts[cue.cue_index - 1],
                 'span_start_codepoint': 0, 'span_end_codepoint': len(texts[cue.cue_index - 1]),
                 'candidate_blind_observation_scaffolding': True},
                clip_context=context, source_media_timeline_offset_ms=source_offset_ms)
            validated, reason = cast(_ValidateRange, validate_range_acoustic_observation)(
                srt_text, check, observation)
            if validated is not None or reason not in {
                'NATIVE_RANGE_GEOMETRY_OR_CANDIDATE_DRIFT', 'RANGE_ACOUSTIC_GEOMETRY_INVALID'}:
                break  # Artifact/source failures do not depend on which cue is the anchor.
        if validated is None:
            rejected.append(reason or 'DISCOVERY_OBSERVATION_UNVALIDATED')
            continue
        verdict = json_object(validated.get('verdict'))
        start, end = verdict.get('audio_start_ms'), verdict.get('audio_end_ms')
        if type(start) is not int or type(end) is not int or end <= start:
            rejected.append('DISCOVERY_OBSERVATION_CLOCK_INVALID')
            continue
        native = verdict.get('native_observation')
        payload = native if isinstance(native, dict) else verdict
        segments_value = payload.get('native_segments', payload.get('segments', []))
        segments: list[NativeSegment] = []
        if isinstance(segments_value, list):
            for segment_value in segments_value:
                segment = json_object(segment_value)
                a, b = segment.get('start_ms'), segment.get('end_ms')
                text = segment.get('text', segment.get('transcript'))
                if type(a) is int and type(b) is int and isinstance(text, str) and text.strip():
                    segments.append(NativeSegment(start + a, start + b, text))
        if not segments:
            transcript = verdict.get('exact_transcript')
            if isinstance(transcript, str) and transcript.strip():
                segments.append(NativeSegment(start, end, transcript))
        if not segments:
            rejected.append('DISCOVERY_OBSERVATION_TRANSCRIPT_UNAVAILABLE')
            continue
        bindings = json_object(validated.get('artifact_bindings'))
        observation_id = str(bindings.get('raw_response_sha256') or verdict.get('response_sha256') or '').removeprefix('sha256:')
        if not observation_id:
            rejected.append('DISCOVERY_OBSERVATION_ID_MISSING')
            continue
        expected_scripts = frozenset(script for span in language_spans
            if span.start_ms < end and start < span.end_ms for script in span.scripts)
        if expected_scripts:
            from src.autoslice.text_scripts import script_profile
            expected_scripts |= frozenset(k for k, v in script_profile(srt_text).items() if v and k != 'other')
        scan = observation_nominations(srt_text, provider=str(validated.get('provider') or ''),
            model=str(validated.get('model') or ''), observation_id=observation_id,
            segments=tuple(segments), window_ms=(start, end), source_offset_ms=source_offset_ms,
            channel=channel, expected_scripts=expected_scripts)
        result.extend(scan.nominations)
        rejected.extend(scan.rejection_reasons)
        excluded.extend(scan.excluded_context_ids)
    return ObservationScan(tuple(result), tuple(sorted(set(rejected))), tuple(sorted(set(excluded))))


def gift_inputs(srt_text: str, *, context: object, source_offset_ms: int,
    editorial_rendered_cues: frozenset[int],
) -> tuple[Nomination, ...]:
    from src.autoslice.editorial_subtitle import gift_nominations
    produce = cast(Callable[..., object], gift_nominations)
    values = produce(srt_text, context, source_media_timeline_offset_ms=source_offset_ms)
    if not isinstance(values, list):
        raise DiscoveryContractError('DISCOVERY_GIFT_OUTPUT_INVALID')
    return tuple(n for n in nominations_from_rows(values, channel=Channel.GIFT)
        if n.cue_index not in editorial_rendered_cues)


class _ObserveRange(Protocol):
    def __call__(self, *, check_request: JsonObject, range_witness_request: JsonObject,
        provider: str) -> Sequence[object]: ...


def fullclip_prefetch_geometry(
    srt_text: str, *, source: SourceIdentity, source_offset_ms: int,
) -> tuple[JsonObject, JsonObject]:
    """Canonical C/D prefetch geometry, using the existing WIDE receipt identity."""
    from src.autoslice.acoustic_witness_adjudication import build_witness_request
    from src.autoslice.final_review_auditor import (
        _candidate_blind_missing_proposal_scaffold, build_context_adjudication_request,
    )
    from src.autoslice.context_assisted_audio import digest
    from src.autoslice.jingting_chunker import parse_srt_cues
    from src.autoslice.targeted_relisten import physical_target
    cues = [c for c in parse_srt_cues(srt_text) if c.text.strip()]
    if not cues:
        raise DiscoveryContractError('DISCOVERY_PREFETCH_GEOMETRY_INVALID')
    context: JsonObject = {'candidate_id': source.candidate_id,
        'pieces': [{'source_media_sha256': s} for s in source.media_sha256s]}
    scaffold = json_object(_legacy_call(_candidate_blind_missing_proposal_scaffold, srt_text,
        {'cue_index': 1, 'suspect': cues[0].text, 'repair_class': 'disclosure_only',
         'why': 'candidate-blind whole-source prefetch; no content decision'}))
    check = cast(_BuildCheck, build_context_adjudication_request)(srt_text, scaffold,
        clip_context=context, source_media_timeline_offset_ms=source_offset_ms,
        require_complete_utterance_support=True)
    broad = json_object(cast(Callable[[JsonObject], object], build_witness_request)(check))
    broad.pop('request_sha256', None)
    broad.pop('syllable_count_hint', None)
    broad.update(cue_indexes=list(range(1, len(cues) + 1)),
        matched_start_ms=cues[0].start_ms, matched_end_ms=cues[-1].end_ms,
        context_start_ms=cues[0].start_ms, context_end_ms=cues[-1].end_ms,
        evidence_id=cast(str, _legacy_call(digest, {
            'physical_target': _legacy_call(physical_target, check),
            'channel': 'WIDE_CANDIDATE_BLIND_AUDIO'})))
    broad['request_sha256'] = cast(Callable[[object], str], digest)(broad)
    return check, broad


def prefetch_fullclip(
    srt_text: str, *, verifier: object, source: SourceIdentity, source_offset_ms: int,
) -> tuple[JsonObject, ...]:
    """Acquire each configured MAI/MOSS full window through the existing owner.

    Its native acquisition identity owns singleflight, replay and UNKNOWN; no
    new cache directory, provider call implementation or budget is created here.
    """
    from src.autoslice.jingting_chunker import parse_srt_cues
    observe = getattr(verifier, 'observe_range_context', None)
    if not callable(observe):
        return ()
    available = getattr(observe, 'available_providers', ())
    providers = tuple(name for name in ('mai', 'moss') if name in available)
    if not any(c.text.strip() for c in parse_srt_cues(srt_text)):
        return ()
    check, broad = fullclip_prefetch_geometry(srt_text, source=source, source_offset_ms=source_offset_ms)
    from concurrent.futures import ThreadPoolExecutor
    result: list[JsonObject] = []
    if providers:
        with ThreadPoolExecutor(max_workers=len(providers)) as pool:
            pending = [pool.submit(cast(_ObserveRange, observe), check_request=check,
                range_witness_request=broad, provider=provider) for provider in providers]
            for future in pending:
                result.extend(json_object(row) for row in future.result())
    return tuple(result)


def _legacy_call(function: object, *args: object, **kwargs: object) -> object:
    """One typed boundary to existing JSON producers, without another producer."""
    if not callable(function):
        raise DiscoveryContractError('DISCOVERY_PRODUCER_UNAVAILABLE')
    return cast(Callable[..., object], function)(*args, **kwargs)


@dataclass(frozen=True, order=True)
class _RecordedCall:
    kind: str
    request_json: str
    response_json: str


class _ScanCalls:
    """Retain original calls; cold consumption has no live-call fallback."""
    def __init__(self, saved: tuple[_RecordedCall, ...] | None = None) -> None:
        self.saved = saved
        self.records: list[_RecordedCall] = []
        self.used: set[tuple[str, str]] = set()

    def call(self, kind: str, request: object, function: Callable[[], object]) -> Json:
        request_json = _dump(request)
        if self.saved is None:
            response = json_value(function())
            row = _RecordedCall(kind, request_json, _dump(response))
            if any(r.kind == kind and r.request_json == request_json and r != row for r in self.records):
                raise DiscoveryContractError('DISCOVERY_CALL_OUTCOME_CONFLICT')
            self.records.append(row)
        else:
            matches = {r.response_json for r in self.saved if r.kind == kind and r.request_json == request_json}
            if len(matches) != 1:
                raise DiscoveryContractError('DISCOVERY_COLD_CALL_MISSING_OR_AMBIGUOUS')
            try:
                response = json_value(cast(object, json.loads(next(iter(matches)))))
            except json.JSONDecodeError as exc:
                raise DiscoveryContractError('DISCOVERY_COLD_RESPONSE_JSON_INVALID') from exc
            self.used.add((kind, request_json))
        return response

    def retained(self) -> tuple[_RecordedCall, ...]:
        if self.saved is not None:
            if self.used != {(r.kind, r.request_json) for r in self.saved}:
                raise DiscoveryContractError('DISCOVERY_COLD_CALL_SET_DRIFT')
            return self.saved
        return tuple(sorted(set(self.records)))


@dataclass(frozen=True)
class _DiscoveryAdapters:
    glossary: str

    def review_glossary(self) -> str:
        return self.glossary

    def profile_asset_file(self, name: str) -> Path:
        from src.autoslice.channel_profile import load_channel_profile
        profile = cast(_DiscoveryProfile, _legacy_call(load_channel_profile, Path(__file__).resolve().parents[2]))
        return profile.asset_file(name)


class _DiscoveryProfile(Protocol):
    def asset_file(self, name: str) -> Path: ...


def _scan_verifier(delegate: object, calls: _ScanCalls, providers: tuple[str, ...]) -> object:
    def witness(request: JsonObject) -> Json:
        return calls.call('B.witness', request, lambda: _legacy_call(delegate, request))

    def probe(request: JsonObject) -> Json:
        return calls.call('B.probe', request,
            lambda: _legacy_call(getattr(delegate, 'probe_witness_cache', lambda _request: None), request))

    def observe(*, check_request: JsonObject, range_witness_request: JsonObject,
                provider: str) -> Sequence[object]:
        request = {'check_request': check_request, 'range_witness_request': range_witness_request,
            'provider': provider}
        result = calls.call('C0.range', request, lambda: _legacy_call(
            getattr(delegate, 'observe_range_context', None), **request))
        if not isinstance(result, list):
            raise DiscoveryContractError('DISCOVERY_RANGE_RESPONSE_INVALID')
        return result

    def identity() -> Json:
        return calls.call('source', {}, lambda: _legacy_call(
            getattr(delegate, 'span_termination_source_identity', None)))

    setattr(observe, 'available_providers', providers)
    setattr(witness, 'probe_witness_cache', probe)
    setattr(witness, 'observe_range_context', observe)
    setattr(witness, 'span_termination_source_identity', identity)
    return witness


def _json_list(value: object) -> list[Json]:
    result = json_value(value)
    if not isinstance(result, list):
        raise DiscoveryContractError('DISCOVERY_ARRAY_REQUIRED')
    return result


def _prepared_scan_findings(inputs: 'DiscoveryInputs', text: str, result: DiscoveryResult,
    findings: Sequence[object], closed: frozenset[tuple[str, str]],
) -> tuple['PreparedFinding', ...]:
    from src.autoslice.finding_closure_ports import RetainedObject
    output: list[PreparedFinding] = []
    by_p: dict[str, list[JsonObject]] = {}
    for value in findings:
        row = json_object(value)
        metadata = json_object(row.get('discovery_task'))
        by_p.setdefault(str(metadata['physical_question_sha256']), []).append(row)
    for task in result.tasks:
        candidates = by_p.get(task.question_key, [])
        if not candidates:
            raise DiscoveryContractError('DISCOVERY_PREPARED_TASK_DROPPED')
        candidates.sort(key=lambda row: (not isinstance(row.get('proposed_full_cue'), str), _dump(row)))
        representative = dict(candidates[0])
        representative['discovery_candidates'] = json_value(candidates)
        representative['discovery_nominations'] = json_value([
            {'channel': str(n.channel), 'cue_index': n.cue_index, 'payload_json': n.payload_json,
             'observation_id': n.observation_id, 'provider': n.provider, 'model': n.model,
             'reading': n.reading, 'origin_channels': list(n.origin_channels)} for n in task.nominations])
        prepared = inputs.prepare_finding(text, RetainedObject.from_value(representative))
        materials = prepared.materials.value()
        proposals = {cast(str, row['proposed_full_cue']) for row in candidates if isinstance(row.get('proposed_full_cue'), str)}
        if (not proposals <= set(str(v) for v in _json_list(materials.get('candidates')))
            or not set(task.observation_ids) <= set(str(v) for v in _json_list(materials.get('bound_observation_ids')))):
            # C owns canonical P/M preparation. Never silently substitute the
            # discovery M for its adjudication M or lose a source/candidate.
            raise DiscoveryContractError('DISCOVERY_PREPARE_CANDIDATES_OR_OBSERVATIONS_MISSING')
        if prepared.spec.key.physical not in {p for p, _m in closed}:
            output.append(prepared)
    if len({p.spec.key.physical for p in output}) != len(output):
        raise DiscoveryContractError('DISCOVERY_PREPARED_PHYSICAL_DUPLICATE')
    return tuple(sorted(output, key=lambda p: (p.spec.key.physical, p.spec.key.materials)))


def _closure_ledger(inputs: 'DiscoveryInputs') -> 'FileLedger':
    from src.autoslice.finding_closure_shell import FileLedger, load_initial
    root=Path(inputs.output_root)/(inputs.candidate_id+'.finding-closure')
    return FileLedger(root,load_initial(root),create=False)


def discovery_for_work(inputs: 'DiscoveryInputs', revision: str) -> 'DiscoveryInputs':
    if not revision:
        return inputs
    from src.autoslice.finding_closure_ports import RetainedObject
    from src.autoslice.finding_closure_shell import ExistingFindingOwners, load_discovery_inputs
    doc=_closure_ledger(inputs).get(revision).value()
    if doc.get('schema_version')!='discovery-input-update.v1':
        raise DiscoveryContractError('DISCOVERY_INPUT_UPDATE_SCHEMA_INVALID')
    owners=cast(ExistingFindingOwners,getattr(inputs.prepare_finding,'__self__'))
    result=load_discovery_inputs(RetainedObject.from_value(doc['discovery']),owners,live=inputs)
    if (result.source_sha256!=inputs.source_sha256 or result.candidate_id!=inputs.candidate_id
            or result.timeline_offset_ms!=inputs.timeline_offset_ms
            or result.candidate_context!=inputs.candidate_context):
        raise DiscoveryContractError('DISCOVERY_INPUT_UPDATE_SOURCE_DRIFT')
    return result


def completed_postburn_delta(inputs: 'DiscoveryInputs', text: str, *, previous: 'DiscoveryInputs',
    previous_ref: str, state: 'ClosureState') -> 'InputUpdateResult | None':
    from src.autoslice.finding_closure_ports import RetainedObject, InputUpdateResult
    from src.autoslice.finding_closure_shell import discovery_document
    from src.autoslice.producer_text_pipeline import validated_postburn_priority_findings
    def receipt(value: 'DiscoveryInputs') -> Json:
        authority=value.verified_authority.value()
        nested=authority.get('final_review_audit')
        return json_value(authority.get('postburn_subtitle_audio_review') or
            (nested.get('postburn_subtitle_audio_review') if isinstance(nested,dict) else None))
    if receipt(inputs)==receipt(previous):
        return None
    validated=_legacy_call(validated_postburn_priority_findings,final_srt_text=text,
        verified_authority_audit=inputs.verified_authority.value(),candidate_id=inputs.candidate_id,
        clip_context=inputs.candidate_context.value(),allow_clean_checkpoint=True)
    if not isinstance(validated,tuple) or len(validated)!=2:
        raise DiscoveryContractError('DISCOVERY_COMPLETED_POSTBURN_BINDING_INVALID')
    rows=_json_list(validated[0]);scope={cast(int,json_object(row)['cue_index']) for row in rows}
    nominations=list(nominations_from_rows(rows,channel=Channel.POSTBURN))
    # One ASR may enrich an already opened question; it cannot invent a fresh
    # question. Reuse the old nomination originals only for the new I's scope.
    if scope:
        ledger=_closure_ledger(inputs)
        for task in state.tasks:
            if not scope.intersection(task.spec.scope_cues):
                continue
            row=ledger.get(task.spec.finding_ref).value()
            for raw in _json_list(row.get('discovery_nominations',[])):
                n=json_object(raw)
                if n['cue_index'] not in scope:
                    continue
                nominations.append(Nomination(Channel(str(n['channel'])),cast(int,n['cue_index']),
                    str(n['payload_json']),str(n['observation_id']),str(n['provider']),str(n['model']),
                    str(n['reading']),tuple(Channel(str(c)) for c in _json_list(n['origin_channels']))))
    batch=DiscoveryBatch(tuple(nominations),source_identity(inputs.candidate_context.value(),inputs.source_sha256),
                         inputs.timeline_offset_ms)
    result=union_discovery(text,batch,scope_cues=frozenset(scope))
    prepared=_prepared_scan_findings(inputs,text,result,task_rows(result),frozenset())
    document=RetainedObject.from_value({'schema_version':'discovery-input-update.v1',
        'previous_ref':previous_ref,'srt_sha256':_sha(text),'postburn':receipt(inputs),
        'request_sha256':validated[1],'discovery':discovery_document(inputs).value(),
        'findings':[p.finding.value() for p in prepared],
        'materials_only':[json.loads(n.payload_json) for n in result.materials_only]})
    return InputUpdateResult(document,prepared)


def validate_input_update(request: 'ValidationRequest', ports: 'DiscoveryPorts') -> None:
    from src.autoslice import finding_closure as core
    from src.autoslice.finding_closure_shell import load_discovery_inputs, ExistingFindingOwners
    from src.autoslice.finding_closure_ports import RetainedObject
    event=request.event
    if not isinstance(event,core.InputsAdvanced) or not isinstance(ports,_ClosureDiscoveryPorts):
        raise DiscoveryContractError('DISCOVERY_INPUT_UPDATE_EVENT_INVALID')
    doc=request.artifacts.get(event.receipt_ref)
    obj=doc.value()
    previous=discovery_for_work(ports.inputs,request.state.discovery_ref)
    owners=cast(ExistingFindingOwners,getattr(ports.inputs.prepare_finding,'__self__'))
    latest=load_discovery_inputs(RetainedObject.from_value(obj['discovery']),owners,live=ports.inputs)
    rebuilt=completed_postburn_delta(latest,request.state.current_srt,previous=previous,
        previous_ref=request.state.discovery_ref,state=request.state)
    if (rebuilt is None or rebuilt.receipt!=doc or tuple(f.spec for f in rebuilt.findings)!=event.findings):
        raise DiscoveryContractError('DISCOVERY_INPUT_UPDATE_COLD_REPLAY_DRIFT')
    for p in rebuilt.findings:
        for d in (p.finding,p.check,p.materials,p.origin_check):
            if d is not None and request.artifacts.get(d.sha256)!=d:
                raise DiscoveryContractError('DISCOVERY_INPUT_UPDATE_ARTIFACT_DRIFT')


@dataclass(frozen=True)
class _ClosureDiscoveryPorts:
    inputs: 'DiscoveryInputs'

    def first_scan(self, request: 'ScanRequest') -> 'ScanResult':
        return self._execute(request, 'initial_scan')

    def targeted_scan(self, request: 'ScanRequest') -> 'ScanResult':
        return self._execute(request, 'targeted_scan')

    def final_scan(self, request: 'ScanRequest') -> 'ScanResult':
        return self._execute(request, 'final_scan')

    def _execute(self, request: 'ScanRequest', kind: str) -> 'ScanResult':
        from src.autoslice.finding_closure import ClosureContractError
        try:
            return self._scan(request, kind)
        except DiscoveryContractError as exc:
            raise ClosureContractError(str(exc)) from exc

    def _scan(self, request: 'ScanRequest', kind: str, *,
        saved: tuple[_RecordedCall, ...] | None = None,
        saved_providers: tuple[str, ...] | None = None,
    ) -> 'ScanResult':
        from dataclasses import asdict
        from src.autoslice import finding_closure as core
        from src.autoslice.finding_closure_ports import RetainedObject, ScanResult
        from src.autoslice.finding_closure_shell import discovery_document
        from src.autoslice.microcue_acoustic_discovery import discover_microcue_findings
        from src.autoslice.producer_text_pipeline import _run_exact_final_release_review
        inputs = discovery_for_work(self.inputs,request.work.discovery_ref)
        versions = core.cue_versions(request.srt_text)
        scope = frozenset(request.work.scope_cues)
        if (kind not in {'initial_scan', 'targeted_scan', 'final_scan'}
            or request.work.kind != kind or request.work.text_sha256 != _sha(request.srt_text)
            or request.work.scan_channels != scan_channels()
            or not scope or request.work.scope_cues != tuple(sorted(scope))
            or any(type(i) is not int for i in request.work.scope_cues)
            or not scope <= {v.cue for v in versions}
            or kind != 'targeted_scan' and scope != {v.cue for v in versions}):
            raise DiscoveryContractError('DISCOVERY_SCAN_WORK_DRIFT')
        if not language_scan_complete(inputs.verified_authority.value()):
            # Missing prerequisite scans are checked before buying discovery.
            raise DiscoveryContractError('DISCOVERY_CHANNEL_G_NOT_SCANNED')
        observe = getattr(inputs.entity_verifier, 'observe_range_context', None)
        providers = (saved_providers if saved_providers is not None else
            tuple(str(p) for p in getattr(observe, 'available_providers', ()) if p in {'mai', 'moss'}))
        calls = _ScanCalls(saved)
        verifier = _scan_verifier(inputs.entity_verifier, calls, providers)
        micro_result = _legacy_call(discover_microcue_findings, request.srt_text,
            timeline_offset_ms=inputs.timeline_offset_ms, entity_verifier=verifier)
        if not isinstance(micro_result, tuple) or len(micro_result) != 2:
            raise DiscoveryContractError('DISCOVERY_BLIND_RESULT_INVALID')
        rows = _json_list(micro_result[0])
        acoustic = json_object(micro_result[1])
        if not blind_scan_complete(request.srt_text, acoustic):
            raise DiscoveryContractError('DISCOVERY_CHANNEL_B_INCOMPLETE')
        source = source_identity(inputs.candidate_context.value(), inputs.source_sha256)
        identity = getattr(verifier, 'span_termination_source_identity')()
        if not isinstance(identity, str) or identity.removeprefix('sha256:') != inputs.source_sha256.removeprefix('sha256:'):
            raise DiscoveryContractError('DISCOVERY_ACQUISITION_SOURCE_DRIFT')
        full_source = SourceIdentity(inputs.candidate_id or (source.candidate_id if source else ''),
            (identity.removeprefix('sha256:'),))
        observations = prefetch_fullclip(request.srt_text, verifier=verifier,
            source=full_source, source_offset_ms=inputs.timeline_offset_ms)
        acoustic['fullclip_discovery_observations'] = json_value(observations)
        acoustic['fullclip_discovery_status'] = ('OBSERVED' if observations
            and all(row.get('status') == 'OBSERVED' for row in observations) else 'INCOMPLETE')
        if source is None:
            raise DiscoveryContractError('DISCOVERY_SCAN_SOURCE_MISSING')

        def llm(channel: str, prompt: str) -> str:
            response = calls.call(channel, {'prompt': prompt}, lambda: inputs.llm_call(prompt))
            if not isinstance(response, str):
                raise DiscoveryContractError('DISCOVERY_TEXT_RESPONSE_INVALID')
            return response

        results: list[DiscoveryResult] = []
        review = json_object(_legacy_call(_run_exact_final_release_review,
            srt_text=request.srt_text, correction_audit=inputs.correction_audit.value(),
            adapters=_DiscoveryAdapters(inputs.glossary_text), authoritative_chat=(),
            selection_hook='', clip_context=inputs.candidate_context.value(),
            verified_authority_audit=inputs.verified_authority.value(),
            timeline_offset_ms=inputs.timeline_offset_ms,
            priority_raw_findings=[*(row.value() for row in inputs.priority_findings), *rows],
            draft_fidelity_contexts=[row.value() for row in inputs.draft_fidelity_contexts],
            acoustic_discovery_audit=acoustic,
            source_media_sha256='sha256:' + inputs.source_sha256.removeprefix('sha256:'),
            final_review_llm=lambda prompt: llm('A', prompt),
            pronoun_audit_llm=lambda prompt: llm('E', prompt),
            scope_cues=scope, discovery_result_sink=results.append, discovery_only=True,
            discovery_contexts=(inputs.structured_context_text, inputs.candidate_context_text),
            discovery_extract_json=inputs.extract_json,
            **{key: _json_list((inputs.spec.value() if inputs.spec else {}).get(key, []))
                for key in ('prior_range_acoustic_observations', 'prior_screen_context_observations',
                    'prior_range_screen_context_observations', 'prior_exact_source_transcript_handoffs')}))
        if review.get('status') != 'DISCOVERY_COMPLETE' or review.get('scan_coverage_complete') is not True or len(results) != 1:
            raise DiscoveryContractError('DISCOVERY_SCAN_INCOMPLETE:' + _dump(review.get('reason_codes')))
        closed = frozenset((c.key.physical, c.key.materials) for c in request.work.closed_ledger)
        prepared = _prepared_scan_findings(inputs, request.srt_text, results[0],
            _json_list(review.get('findings')), closed)
        manifest = json_object({'schema_version': 'discovery-scan.v1',
            'work': asdict(request.work), 'srt_text': request.srt_text,
            'configuration_sha256': discovery_document(inputs).sha256,
            'channels': list(scan_channels()), 'text_lanes': DEFAULT_TEXT_LANES,
            'providers': list(providers), 'calls': [asdict(c) for c in calls.retained()],
            'review': review})
        anchor = RetainedObject.from_value({'channel': str(Channel.TEXT), 'manifest': manifest})
        lanes = (anchor, *(RetainedObject.from_value({'channel': str(channel),
            'manifest_ref': anchor.sha256, 'coverage': 'COMPLETE',
            'nominations': [json_object(cast(object, json.loads(n.payload_json)))
                for task in results[0].tasks for n in task.nominations if channel in _origin_channels(n)],
            'materials_only': [json_object(cast(object, json.loads(n.payload_json)))
                for n in results[0].materials_only if channel in _origin_channels(n)]})
            for channel in SCAN_CHANNELS if channel != Channel.TEXT))
        return ScanResult(prepared, tuple(v for v in versions if v.cue in scope), lanes)

    def validate_scan(self, request: 'ValidationRequest') -> None:
        from src.autoslice.finding_closure import ClosureContractError
        try:
            self._validate_scan(request)
        except DiscoveryContractError as exc:
            raise ClosureContractError(str(exc)) from exc

    def _validate_scan(self, request: 'ValidationRequest') -> None:
        from src.autoslice import finding_closure as core
        from src.autoslice.finding_closure_ports import ScanRequest
        event = request.event
        if (not isinstance(event, core.ScanCompleted) or not event.lane_refs
            or event.work_id != request.work.work_id
            or request.state.source_sha256.removeprefix('sha256:') != self.inputs.source_sha256.removeprefix('sha256:')):
            raise DiscoveryContractError('DISCOVERY_SCAN_EVENT_INVALID')
        if not set(request.work.closed_ledger) <= set(core.closed_ledger(request.state)):
            raise DiscoveryContractError('DISCOVERY_CLOSED_LEDGER_DRIFT')
        original = request.artifacts.get(event.lane_refs[0]).value()
        if original.get('channel') != str(Channel.TEXT):
            raise DiscoveryContractError('DISCOVERY_SCAN_ANCHOR_MISSING')
        manifest = json_object(original.get('manifest'))
        if manifest.get('schema_version') != 'discovery-scan.v1':
            raise DiscoveryContractError('DISCOVERY_SCAN_SCHEMA_INVALID')
        saved: list[_RecordedCall] = []
        for value in _json_list(manifest.get('calls')):
            row = json_object(value)
            if set(row) != {'kind', 'request_json', 'response_json'} or any(not isinstance(v, str) for v in row.values()):
                raise DiscoveryContractError('DISCOVERY_SCAN_CALL_INVALID')
            saved.append(_RecordedCall(str(row['kind']), str(row['request_json']), str(row['response_json'])))
        provider_values = _json_list(manifest.get('providers'))
        if any(not isinstance(p, str) or p not in {'mai', 'moss'} for p in provider_values):
            raise DiscoveryContractError('DISCOVERY_SCAN_PROVIDER_INVALID')
        text = core.text_at(request.state, request.work.text_sha256)
        rebuilt = self._scan(ScanRequest(request.work, text), request.work.kind,
            saved=tuple(saved), saved_providers=tuple(str(p) for p in provider_values))
        if (tuple(p.spec for p in rebuilt.findings) != event.findings or rebuilt.coverage != event.coverage
            or tuple(lane.sha256 for lane in rebuilt.lanes) != event.lane_refs):
            raise DiscoveryContractError('DISCOVERY_SCAN_COLD_REPLAY_DRIFT')
        # Reopen every retained candidate/check/material through the shared C
        # artifact reader, as well as replaying the original producer/parser.
        for prepared in rebuilt.findings:
            for doc in (prepared.finding, prepared.check, prepared.materials, prepared.origin_check):
                if doc is not None and request.artifacts.get(doc.sha256) != doc:
                    raise DiscoveryContractError('DISCOVERY_SCAN_PREPARED_ARTIFACT_DRIFT')

    def validate_terminal(self, request: 'ValidationRequest') -> None:
        """R5 after C/F revalidates its original F2 ruling; no model calls."""
        from src.autoslice.finding_closure import ClosureContractError
        try:
            self._validate_terminal(request)
        except DiscoveryContractError as exc:
            raise ClosureContractError(str(exc)) from exc

    def _validate_terminal(self, request: 'ValidationRequest') -> None:
        from src.autoslice import finding_closure as core
        event = request.event
        if (not isinstance(event, core.ReviewCompleted) or event.work_id != request.work.work_id
            or request.work.task is None
            or request.state.source_sha256.removeprefix('sha256:') != self.inputs.source_sha256.removeprefix('sha256:')):
            raise DiscoveryContractError('DISCOVERY_TERMINAL_EVENT_INVALID')
        original = request.artifacts.get(request.work.task.finding_ref).value()
        current = request.artifacts.get(event.spec.finding_ref).value()
        values = original.get('discovery_nominations')
        if values is None:
            metadata = original.get('discovery_task')
            if isinstance(metadata, dict) and metadata.get('asr_only') is True:
                raise DiscoveryContractError('ASR_LEDGER_ORIGINAL_TASK_MATERIAL_REQUIRED')
            return  # Legacy non-ASR tasks remain with their original F2 consumer.
        if current.get('discovery_nominations') != values:
            raise DiscoveryContractError('DISCOVERY_TERMINAL_ORIGIN_DRIFT')
        nominations: list[Nomination] = []
        for value in _json_list(values):
            row = json_object(value)
            if (type(row.get('cue_index')) is not int or not isinstance(row.get('payload_json'), str)
                or any(not isinstance(row.get(k), str) for k in ('channel', 'observation_id', 'provider', 'model', 'reading'))):
                raise DiscoveryContractError('DISCOVERY_TERMINAL_NOMINATION_INVALID')
            try:
                nominations.append(Nomination(Channel(str(row['channel'])), cast(int, row['cue_index']),
                    str(row['payload_json']), str(row['observation_id']), str(row['provider']), str(row['model']),
                    str(row['reading']), tuple(Channel(str(c)) for c in _json_list(row.get('origin_channels')))))
            except (json.JSONDecodeError, ValueError) as exc:
                # Only this explicitly decoded wire object is classified here.
                raise DiscoveryContractError('DISCOVERY_TERMINAL_NOMINATION_INVALID') from exc
        if not nominations:
            raise DiscoveryContractError('DISCOVERY_TERMINAL_NOMINATION_INVALID')
        origins = tuple(sorted({channel for n in nominations for channel in _origin_channels(n)}))
        task = DiscoveryTask(event.spec.key.physical, event.spec.key.materials, nominations[0].cue_index,
            origins, tuple(nominations), tuple(sorted({n.observation_id for n in nominations})), True)
        if not task.asr_only or event.state != core.State.TRUE_BLOCK:
            # KEEP/EDIT already passed C's original supported-current or typed
            # mutation consumer. R5 imposes its extra prerequisite on D only.
            return
        adjudication_value = current.get('exact_release_adjudication')
        if not isinstance(adjudication_value, dict):
            raise DiscoveryContractError('DISCOVERY_ASR_TERMINAL_ORIGINAL_REQUIRED')
        adjudication = adjudication_value
        review_value = adjudication.get('terminal_review')
        if not isinstance(review_value, dict):
            raise DiscoveryContractError('DISCOVERY_ASR_TERMINAL_ORIGINAL_REQUIRED')
        review = review_value
        from src.autoslice.finding_lifecycle import true_block_evidence
        from src.autoslice.llm_client import extract_json_object
        from src.autoslice.semantic_terminal_adjudication import terminal_materials
        from src.autoslice.targeted_relisten import validate_no_gain, replay_no_gain_projection, CLEAR_AUDIO_MARKER_PATTERN
        import re
        # These existing consumers reopen the original prompt/answer, physical
        # hearing artifacts and no-gain proof. No summary booleans are trusted.
        _legacy_call(true_block_evidence, adjudication)
        terminal_request = json_object(review.get('request'))
        check = json_object(terminal_request.get('check_request'))
        proof = json_object(_legacy_call(validate_no_gain, terminal_request.get('audio_gain_termination'), check))
        projection = json_object(_legacy_call(replay_no_gain_projection, proof, check, include_target_text=True))
        material_result = _legacy_call(terminal_materials, terminal_request)
        if not isinstance(material_result, tuple) or len(material_result) != 2:
            raise DiscoveryContractError('DISCOVERY_TERMINAL_MATERIALS_INVALID')
        material = json_object(material_result[0])
        answer = json_object(_legacy_call(extract_json_object, review.get('raw_completion')))
        models: set[str] = set()
        phonetic = False
        for value in _json_list(projection.get('observations')):
            observation = json_object(value)
            raw = json_object(observation.get('raw_observation'))
            if observation.get('usable_status') != 'OBSERVED' or raw.get('status') != 'OBSERVED':
                continue
            heard = raw.get('heard_pinyin')
            if observation.get('receipt_kind') == 'blind_pinyin' and isinstance(heard, str) and heard.strip():
                phonetic = True
            target_text = observation.get('target_text')
            ambiguous = (raw.get('target_audible') is False
                or isinstance(raw.get('uncertain_positions'), list) and bool(raw['uncertain_positions'])
                or isinstance(heard, str) and '?' in heard.split()
                or isinstance(target_text, str) and re.search(CLEAR_AUDIO_MARKER_PATTERN, target_text) is not None)
            info = json_object(observation.get('input_information'))
            model = info.get('listening_model') or raw.get('model')
            if ambiguous and isinstance(model, str) and model_identity_key(model):
                models.add(model)
        context = json_object(terminal_request.get('terminal_context'))
        context_sources = material.get('context_sources')
        from src.autoslice.editorial_subtitle import _events
        window = terminal_request.get('source_event_window')
        structured: list[Json] = []
        if (isinstance(window, list) and len(window) == 2
            and all(type(v) in {int, float} for v in window)):
            structured = _json_list(_legacy_call(_events, context, *window))
            if structured != terminal_request.get('events'):
                raise DiscoveryContractError('DISCOVERY_TERMINAL_STRUCTURED_SOURCE_DRIFT')
        evidence = ExhaustionEvidence(tuple(sorted(models)),
            context_exhausted=isinstance(context_sources, dict) and bool(context_sources)
                and answer.get('context_can_decide') is False,
            restatement_exhausted=isinstance(material.get('refrains'), list)
                and isinstance(material.get('whole_session_restatements'), list),
            structured_text_exhausted=bool(structured),
            phonetics_exhausted=phonetic)
        # true_block_evidence has already consumed UNRESOLVABLE under the
        # existing four-choice policy. Preserve that one definition here.
        try:
            validate_terminal_choice(task, str(answer.get('choice')), evidence)
        except ValueError as exc:
            raise DiscoveryContractError(str(exc)) from exc


def build_closure_discovery_ports(inputs: 'DiscoveryInputs') -> 'DiscoveryPorts':
    """First/targeted/final scans and their zero-call original consumer."""
    return _ClosureDiscoveryPorts(inputs)
