"""Human literal sequences with independently observed word-time allocation."""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

from src.autoslice.jingting_chunker import parse_srt_cues
from src.autoslice.source_subtitle_truth import MIN_CUE_OVERLAP_MS, _entry_local_windows, _load_source_aliases, _render
from src.autoslice.chat_evidence import normalize_chat_text, normalize_srt_owner_payload_window


class HumanSequenceAlignmentError(ValueError):
    pass


def digest(value) -> str:
    return 'sha256:' + hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _read_binding(root: Path, binding: dict) -> bytes:
    path = root / binding['path']
    if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()) or not path.is_file():
        raise HumanSequenceAlignmentError('HUMAN_SEQUENCE_ALIGNMENT_ASSET_INVALID')
    raw = path.read_bytes()
    if 'sha256:' + hashlib.sha256(raw).hexdigest() != binding['sha256']:
        raise HumanSequenceAlignmentError('HUMAN_SEQUENCE_ALIGNMENT_ASSET_DRIFT')
    return raw


def load_plans(ledger_path: Path | None, *, spec: dict, durations: list[int]) -> list[dict]:
    if ledger_path is None:
        return []
    ledger = json.loads(ledger_path.read_bytes())
    aliases = _load_source_aliases(ledger)
    plans = []
    for entry in ledger['entries']:
        binding = entry.get('timing_alignment')
        if binding is None:
            continue
        windows = _entry_local_windows(entry, pieces=spec['pieces'], durations=durations, source_aliases=aliases)
        if not windows:
            continue
        if (len(windows) != 1 or windows[0]['source_overlap_start_ms'] != entry['source_start_ms']
                or windows[0]['source_overlap_end_ms'] != entry['source_end_ms']):
            continue  # A different candidate does not inherit this whole-window edit.
        if (entry.get('knowledge_type') != 'SOURCE_INTERVAL_OBSERVATION'
                or entry.get('decision_authority') != 'REVIEWER_OPERATOR_TRUTH'
                or entry.get('assertion_state') != 'VERIFIED_ACTIVE'):
            raise HumanSequenceAlignmentError('HUMAN_SEQUENCE_ALIGNMENT_CONTENT_AUTHORITY_INVALID')
        raw = _read_binding(ledger_path.parent, binding)
        manifest = json.loads(raw)
        root = ledger_path.parent / Path(binding['path']).parent
        original_entry = {k:v for k,v in entry.items() if k != 'timing_alignment'}
        if (manifest.get('schema_version') != 'human-sequence-timing-alignment.v1'
                or manifest.get('observation_sha256') != digest(original_entry)):
            raise HumanSequenceAlignmentError('HUMAN_SEQUENCE_ALIGNMENT_OBSERVATION_DRIFT')
        _read_binding(root, manifest['prompt'])
        provider = manifest.get('provider_runtime_binding') or {}
        if (provider.get('provider_transport') not in {'runtime_cpa','ssh_runtime_cpa'}
                or provider.get('provider_http_status') != 200
                or not provider.get('provider_model') or not provider.get('provider_endpoint_host')
                or not provider.get('provider_effort')):
            raise HumanSequenceAlignmentError('HUMAN_SEQUENCE_ALIGNMENT_CPA_BINDING_MISSING')
        answer = json.loads(_read_binding(root, manifest['completion']))
        provenance = json.loads(_read_binding(root, manifest['piece_provenance']))
        padded = json.loads(_read_binding(root, manifest['padded_provenance']))
        offset = manifest['recording_offset_ms']
        reference_sha = manifest['source_padded_sha256'].removeprefix('sha256:')
        original_sha = entry['source_media_sha256'].removeprefix('sha256:')
        if (type(offset) is not int or provenance.get('start_ms') != offset
                or str(provenance.get('source_media_binding') or provenance.get('source_sha256')).removeprefix('sha256:') != original_sha
                or str(provenance.get('output_sha256')).removeprefix('sha256:') != reference_sha
                or str(padded.get('output_sha256')).removeprefix('sha256:') != reference_sha
                or len(padded.get('inputs', [])) != 1
                or str(padded['inputs'][0]['sha256']).removeprefix('sha256:') != reference_sha):
            raise HumanSequenceAlignmentError('HUMAN_SEQUENCE_ALIGNMENT_REFERENCE_CLOCK_DRIFT')
        bcut = json.loads(_read_binding(root, manifest['bcut']))
        if bcut.get('provider') != 'bcut':
            raise HumanSequenceAlignmentError('HUMAN_SEQUENCE_ALIGNMENT_TIMING_PROVIDER_INVALID')
        starts = {word['start_time'] + offset for utterance in bcut['utterances']
                  for word in utterance.get('words', []) if type(word.get('start_time')) is int}
        independent_words = []
        for item in manifest['mai']:
            receipt = json.loads(_read_binding(root, item))
            evidence, native_binding = receipt['evidence'], receipt['binding']
            begin, end = item['padded_window_ms']
            if (evidence.get('provider') != 'mai' or evidence.get('status') != 'OK'
                    or evidence.get('candidate_exposure') != 'none'
                    or native_binding.get('duration_ms') != end - begin
                    or native_binding.get('input_audio_sha256') != evidence.get('input_audio_sha256')
                    or digest(evidence).removeprefix('sha256:') != str(receipt['evidence_sha256']).removeprefix('sha256:')):
                raise HumanSequenceAlignmentError('HUMAN_SEQUENCE_ALIGNMENT_NATIVE_RECEIPT_INVALID')
            for segment in evidence['native_segments']:
                for word in segment.get('words', []):
                    start, finish = word['start_ms'], word['end_ms']
                    if type(start) is not int or type(finish) is not int or not 0 <= start < finish <= end - begin:
                        raise HumanSequenceAlignmentError('HUMAN_SEQUENCE_ALIGNMENT_NATIVE_WORD_INVALID')
                    independent_words.append((begin + start + offset, begin + finish + offset))
        human = entry['observation']['human_observation_sequence']
        units = answer.get('units', [])
        if (answer.get('status') != 'SUPPORTED' or answer.get('needs_more_timing_evidence') is not False
                or not 2 <= len(units) <= 8 or len(units) != len(human)):
            raise HumanSequenceAlignmentError('HUMAN_SEQUENCE_ALIGNMENT_NOT_RESOLVED')
        local = []
        previous = entry['source_start_ms']
        for index, (unit, observed) in enumerate(zip(units, human)):
            start, end = unit['start_padded_ms'], unit['end_padded_ms']
            if (type(start) is not int or type(end) is not int or start >= end
                    or start + offset != previous or unit.get('text') != observed['text']
                    or not str(unit.get('timing_basis') or '').strip()):
                raise HumanSequenceAlignmentError('HUMAN_SEQUENCE_ALIGNMENT_UNIT_DRIFT')
            if index and (start + offset not in starts or not any(
                    left <= start + offset <= right for left, right in independent_words)):
                raise HumanSequenceAlignmentError('HUMAN_SEQUENCE_ALIGNMENT_BOUNDARY_UNOBSERVED')
            local_start = windows[0]['start_ms'] + start + offset - entry['source_start_ms']
            local.append({'start_ms':local_start, 'end_ms':local_start + end - start,
                          'text':observed['text'], 'role':observed['type']})
            previous = end + offset
        if previous != entry['source_end_ms']:
            raise HumanSequenceAlignmentError('HUMAN_SEQUENCE_ALIGNMENT_RANGE_DRIFT')
        plans.append({'observation_id':entry['truth_id'], 'alignment_binding':binding,
                      'source_media_sha256':entry['source_media_sha256'], 'units':local,
                      'content_authority':'REVIEWER_LITERAL_SEQUENCE', 'timing_authority':'NATIVE_WORD_ALLOCATION_CPA'})
    return plans


def apply_plans(srt_text: str, plans: list[dict], *, timeline_offset_ms: int = 0) -> tuple[str, dict]:
    cues = parse_srt_cues(srt_text)
    used = []
    changed = False
    for plan in plans:
        units = [{**unit, 'start_ms':unit['start_ms'] - timeline_offset_ms,
                  'end_ms':unit['end_ms'] - timeline_offset_ms} for unit in plan['units']]
        start, end = units[0]['start_ms'], units[-1]['end_ms']
        selected = [i for i,cue in enumerate(cues) if cue.start_ms < end and cue.end_ms > start]
        if not selected:
            continue
        if (cues[selected[0]].start_ms != start or cues[selected[-1]].end_ms != end
                or selected != list(range(selected[0], selected[-1] + 1))):
            raise HumanSequenceAlignmentError('HUMAN_SEQUENCE_ALIGNMENT_CUE_SCOPE_DRIFT')
        used.append(plan)
        if [(cues[i].start_ms,cues[i].end_ms,cues[i].text) for i in selected] == [
                (u['start_ms'],u['end_ms'],u['text']) for u in units]:
            continue
        before_outside = [(c.start_ms,c.end_ms,c.text) for i,c in enumerate(cues) if i not in selected]
        replacement = [replace(cues[selected[0]], start_ms=u['start_ms'], end_ms=u['end_ms'], text=u['text']) for u in units]
        cues = [*cues[:selected[0]], *replacement, *cues[selected[-1] + 1:]]
        after_outside = [(c.start_ms,c.end_ms,c.text) for c in cues if c.end_ms <= start or c.start_ms >= end]
        if before_outside != after_outside:
            raise HumanSequenceAlignmentError('HUMAN_SEQUENCE_ALIGNMENT_OUTSIDE_MUTATION')
        changed = True
    output = _render(cues, [cue.text for cue in cues]) if changed else srt_text
    return output, {'schema_version':'human-sequence-aligned-display.v1',
                    'status':'APPLIED' if output != srt_text else 'SATISFIED',
                    'input_srt_sha256':'sha256:' + hashlib.sha256(srt_text.encode()).hexdigest(),
                    'output_srt_sha256':'sha256:' + hashlib.sha256(output.encode()).hexdigest(),
                    'records':used, 'provider_calls':0, 'audio_modified':False}


def required_owners(audit: dict) -> list[dict]:
    return [{'owner_kind':'human_sequence_alignment', 'owner_id':p['observation_id'], 'required':True,
             'local_windows':[{'start_ms':p['units'][0]['start_ms'], 'end_ms':p['units'][-1]['end_ms']}],
             'owner_scope_sha256':digest(p)} for p in audit.get('records', [])]


def protected_indexes(audit: dict, text: str, *, timeline_offset_ms: int = 0) -> set[int]:
    units = [unit for plan in audit.get('records',[]) for unit in plan['units']]
    return {index for index,cue in enumerate(parse_srt_cues(text),1)
            if any((cue.start_ms + timeline_offset_ms,cue.end_ms + timeline_offset_ms,normalize_chat_text(cue.text))
                   == (u['start_ms'],u['end_ms'],normalize_chat_text(u['text'])) for u in units)}


def resolve_literal_findings(audit: dict, text: str, findings: list[dict], *, timeline_offset_ms: int):
    """A lexical correction cannot replace an already verified human literal.

    This owns literal words, not their contextual meaning, truth or safety.
    Meaning-reversal, factual and harm concerns remain with content review.
    """
    if not audit.get('records'):
        return list(findings),[]
    cues=parse_srt_cues(text)
    if not verify_surfaces(audit,text=text,speaker=text,delivery_start_ms=timeline_offset_ms,
                           delivery_end_ms=timeline_offset_ms+max(c.end_ms for c in cues)):
        raise HumanSequenceAlignmentError('HUMAN_SEQUENCE_CURRENT_LITERAL_DRIFT')
    owned=protected_indexes(audit,text,timeline_offset_ms=timeline_offset_ms)
    pending,resolved=[],[]
    for finding in findings:
        index=finding.get('cue_index')
        risk=finding.get('content_risk')
        proposed=finding.get('proposed_full_cue')
        lexical_conflict=(finding.get('kind') in {'nonword','entity','self_ref'}
                          and finding.get('repair_class') in {'phonetic','source_backed_entity'}
                          and isinstance(proposed,str)
                          and type(index) is int and 1<=index<=len(cues)
                          and normalize_chat_text(proposed)!=normalize_chat_text(cues[index-1].text))
        from src.autoslice.final_review_contract import is_noncritical_disclosed
        if index in owned and risk=='noncritical_uncertainty' and lexical_conflict and is_noncritical_disclosed(finding):
            resolved.append({**finding,'resolution':'VERIFIED_HUMAN_LITERAL_SEQUENCE',
                'source_sequence_bindings':[p['alignment_binding'] for p in audit['records']],
                'mutation_authorized':False})
        else:
            pending.append(dict(finding))
    return pending,resolved


def validate_records(audit: dict, *, ledger_path: Path | None, spec: dict, durations: list[int]) -> None:
    expected = {digest(plan) for plan in load_plans(ledger_path, spec=spec, durations=durations)}
    if any(digest(plan) not in expected for plan in audit.get('records', [])):
        raise HumanSequenceAlignmentError('HUMAN_SEQUENCE_ALIGNMENT_CURRENT_SOURCE_DRIFT')


def verify_surfaces(audit: dict, *, text: str, speaker: str, delivery_start_ms: int, delivery_end_ms: int) -> bool:
    for plan in audit.get('records', []):
        for unit in plan['units']:
            start, end = unit['start_ms'] - delivery_start_ms, unit['end_ms'] - delivery_start_ms
            if start < 0 or end > delivery_end_ms - delivery_start_ms:
                return False
            for surface, strip in ((text,False),(speaker,True)):
                matching = [cue for cue in parse_srt_cues(surface) if cue.start_ms < end and cue.end_ms > start]
                if len(matching) != 1 or (matching[0].start_ms, matching[0].end_ms) != (start,end):
                    return False
                payload = normalize_srt_owner_payload_window(surface, start_ms=start, end_ms=end,
                    min_overlap_ms=MIN_CUE_OVERLAP_MS, strip_speaker_labels=strip)
                if payload != normalize_chat_text(unit['text']):
                    return False
    return True
