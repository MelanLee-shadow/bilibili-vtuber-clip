"""Persist actual CPA attempts and the reason for continuing an unresolved doubt.

Three unresolved answers increase scrutiny; they are not a call cap. An exact
question/model/input replay returns its original raw answer. A changed question
is permitted, including on the same audio, with an explicit useful plan after
three unresolved answers. This module never supplies mutation or release rights.
"""
from __future__ import annotations

from .review_errors import ContractRejection

import copy
import time
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Callable, Mapping

from src.autoslice.jingting_chunker import parse_srt_cues
from src.autoslice.llm_client import extract_json_object
from src.autoslice.semantic_answer import answer_selected, proposal_status
from src.autoslice.audio_cache_io import atomic_write

SCHEMA = 'semantic-cpa-attempt.v1'
# Durable STARTED and legacy ERROR lack proof of non-dispatch. Their material
# is already offered, not new evidence; this never turns them into COMPLETED.
_MATERIAL_ALREADY_OFFERED = frozenset({'STARTED', 'COMPLETED', 'ERROR', 'OUTCOME_UNKNOWN'})
RECORDER_GATE_SCHEMA = 'recorder-needs-audio-gate.v1'
RECORDER_ROOT = Path('cache') / 'semantic-cpa-attempts'
ACTIONS = frozenset({'reframe_question', 'correct_localization', 'expand_context',
                     'change_language', 'inspect_structured_text', 'inspect_screen',
                     'faithful_approximation'})


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def continuation_plan(value: object) -> dict[str, str] | None:
    if not isinstance(value, Mapping) or not isinstance(value.get('action'), str) or value['action'] not in ACTIONS:
        return None
    fields = ('action', 'question', 'why_previous_failed', 'expected_gain', 'verification')
    if not all(isinstance(value.get(k), str) and value[k].strip() for k in fields):
        return None
    return {k: value[k].strip() for k in fields}


def bound_range_evidence(finding: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Stable byte identities for ranges already checked by the normal builder."""
    identities = {}
    for row in finding.get('_prior_range_acoustic_observations') or []:
        if not isinstance(row, Mapping):
            continue
        bindings, verdict = row.get('artifact_bindings'), row.get('verdict')
        if not isinstance(bindings, Mapping) or not isinstance(verdict, Mapping):
            continue
        identity = {name: str(bindings.get(name + '_sha256') or bindings.get(name) or '').removeprefix('sha256:')
                    for name in ('source_media', 'audio_clip', 'raw_response')}
        if any(len(v) != 64 or any(c not in '0123456789abcdef' for c in v) for v in identity.values()):
            continue
        identity.update(provider=row.get('provider'), model=row.get('model'),
                        provider_response_sha256=verdict.get('response_sha256'))
        identities[digest(identity)] = identity
    return [identities[key] for key in sorted(identities)]


def _range_seen(identity: Mapping[str, Any], rows: list[dict[str, Any]]) -> bool:
    # Legacy records saved the full provider context in the prompt even when
    # their material hash was captured before that context was injected.
    keys = ('source_media', 'audio_clip', 'provider', 'model')
    for row in rows:
        if row.get('status') not in _MATERIAL_ALREADY_OFFERED:
            continue
        material = row['request'].get('material') or {}
        if any(isinstance(prior, Mapping) and all(prior.get(k) == identity.get(k) for k in keys)
               for prior in material.get('bound_range_evidence', [])):
            return True
        prompt = row['request'].get('prompt')
        if isinstance(prompt, str) and all(
            isinstance(identity.get(k), str) and identity[k] and identity[k] in prompt for k in keys
        ):
            return True
    return False


def with_novel_range_plan(*, srt_text: str, finding: Mapping[str, Any],
                         clip_context: Mapping[str, Any] | None, offset_ms: int) -> dict[str, Any]:
    """Plan one check of supplied, validated evidence absent from this issue's history.

    This never obtains evidence or proposes words. A previously heard range,
    missing history, invalid evidence or an existing plan cannot open this seam.
    The normal recorder still owns dispatch, replay and unknown-outcome gates.
    """
    output = dict(finding)
    if continuation_plan(finding.get('continuation_plan')) is not None:
        return output
    evidence = bound_range_evidence(finding)
    if not evidence:
        return output
    recorder = SemanticAttemptRecorder(srt_text=srt_text, finding=finding,
                                       clip_context=clip_context, offset_ms=offset_ms)
    try:
        rows = recorder._entries()
    except RetryNeedsPlan:
        return output  # The ordinary recorder retains the invalid-history block.
    if sum(row.get('status') == 'COMPLETED' and _uncertain(row['completion'], row['request'].get('prompt')) for row in rows) < 3:
        return output
    from src.autoslice.semantic_request_history import range_seen
    try:
        submitted = recorder.submitted_history()
    except RetryNeedsPlan:
        return output
    novel = [item for item in evidence if not _range_seen(item, rows)
             and not range_seen(item, submitted)]
    if not novel:
        return output
    references = json.dumps(novel, ensure_ascii=False, sort_keys=True)
    output['continuation_plan'] = {
        'action': 'expand_context',
        'question': '用以下已验证范围原音观察核对当前目标整句及邻句关系；先生成可核验候选，再走普通闭集裁决：' + references,
        'why_previous_failed': '本目标已记录的请求未包含这些范围原始答复，重复旧窄窗或文字问题不能消费它们。',
        'expected_gain': '利用实际完整语义范围检查目标句，保留说话人、复读和停顿；证据不足仍未决。',
        'verification': '逐项核对源、音频、原始答复和目标覆盖；块听写只作语境，不当逐字时间真值，改字仍须正常CPA及typed门。',
    }
    output['semantic_retry_evidence_basis'] = {
        'status': 'VALIDATED_RANGE_NOT_IN_RECORDED_TARGET_REQUESTS',
        'evidence': novel, 'mutation_authorized': False,
    }
    return output


def _atomic(path: Path, value: object) -> None:
    atomic_write(path, json.dumps(value, ensure_ascii=False, sort_keys=True,
                                 allow_nan=False).encode())


def _target_decided_with_outside_questions(payload: Mapping[str, Any], prompt: str | None) -> bool:
    """The answer decided its bound target span; its audio questions lie outside it.

    41 (span_review: target and other relisten questions are located
    separately): a PROPOSED/CURRENT answer whose own span review says the
    target needs no audio, with every audio question located outside the
    bound codepoint span of the very request it answered, is not an
    unresolved answer for this target. The outside questions stay with the
    existing span-obligation owner. Anything unlocatable or crossing stays
    unresolved.
    """
    review = payload.get('span_review')
    if (not isinstance(prompt, str) or not isinstance(review, Mapping)
            or str(payload.get('choice') or '').upper() not in {'PROPOSED', 'CURRENT'}
            or review.get('target_needs_audio') is not False
            or review.get('all_audio_questions_located') is not True
            or not isinstance(review.get('audio_questions'), list)):
        return False
    marker = '\nSPAN_REVIEW_INPUT\n'
    if marker not in prompt:
        return False
    try:
        block = json.loads(prompt.split(marker, 1)[1].split('\nEND_SPAN_REVIEW_INPUT\n', 1)[0])
        binding = block['binding']
        start, end = binding['codepoint_span']
        if review.get('binding_sha256') != binding.get('binding_sha256'):
            return False
        for question in review['audio_questions']:
            left, right = question['codepoint_span']
            if not (type(left) is int and type(right) is int and left < right
                    and (right <= start or end <= left)):
                return False
    except (ValueError, TypeError, KeyError, IndexError):
        return False
    return True


def _uncertain(completion: str, prompt: str | None = None) -> bool:
    try:
        payload = extract_json_object(completion)
    except Exception:
        return False  # Invalid JSON/transport failure is not an uncertain answer.
    if answer_selected(payload):
        return False  # needs_audio plans acquisition; the answer was selected.
    if _target_decided_with_outside_questions(payload, prompt):
        return False
    return (payload.get('needs_audio') is True
            or str(payload.get('choice') or '').upper() in {'NEITHER', 'UNRESOLVED', 'UNCERTAIN'}
            or any(str(payload.get(k) or '').upper() in {'UNRESOLVED', 'UNCERTAIN'}
                   for k in ('status', 'decision', 'verdict')))


class RetryNeedsPlan(RuntimeError):
    pass


class CallNotDispatched(RuntimeError):
    """Adapter proof that failure occurred before entering any transport.

    Never wrap a timeout, connection failure or lost response in this type.
    An adapter may raise it only when no provider dispatch was attempted.
    """

    def __init__(self, reason_code: str):
        if not isinstance(reason_code, str) or not reason_code.isidentifier() or not reason_code.isupper():
            raise ValueError('a non-secret uppercase reason code is required')
        self.reason_code = reason_code
        super().__init__(reason_code)


def _legacy_judge_evidence_seen(identity: Mapping[str, Any], row: Mapping[str, Any]) -> bool:
    """Legacy transcript prompts embed the original byte identities, not a new schema.

    This only suppresses novelty; it cannot authorize replay or a mutation.
    Do not reinterpret, rewrite or reconstruct any old attempt record.
    """
    if row.get('status') not in _MATERIAL_ALREADY_OFFERED:
        return False
    request = row['request']
    if 'bound_judge_evidence' in request.get('material', {}):
        return False
    prompt = request.get('prompt', '')
    keys = ('source_media_sha256', 'audio_clip_sha256', 'acquisition_request_sha256') if (
        identity.get('kind') == 'argon_acquisition') else (
        'source_media_sha256', 'audio_clip_sha256', 'response_sha256')
    return all(isinstance(identity.get(k), str) and identity[k] in prompt for k in keys)


class SemanticAttemptRecorder:
    def __init__(self, *, srt_text: str, finding: Mapping[str, Any],
                 clip_context: Mapping[str, Any] | None, offset_ms: int):
        self.finding = finding
        self._clip_context = copy.deepcopy(dict(clip_context or {}))
        self.plan = continuation_plan(finding.get('continuation_plan'))
        self.events: list[dict[str, Any]] = []
        self.blocked: str | None = None
        self.historical_consumption: list[dict[str, Any]] = []
        cues = [c for c in parse_srt_cues(srt_text) if c.text.strip()]
        index = finding.get('cue_index')
        self.directory: Path | None = None
        if not (type(index) is int and 1 <= index <= len(cues)):
            return
        cue = cues[index - 1]
        context = dict(clip_context or {})
        # A stable physical target owns its retry history. Different current
        # wording and neighboring context do not erase already uncertain calls.
        source = {k: context[k] for k in ('source_sha256', 'source_media_sha256', 'source_media_sha256s',
                                        'pieces', 'source_piece_clock') if k in context}
        if not any(source.values()):
            return  # Never merge unrelated unbound media at the same cue time.
        self._physical_target = (cue.start_ms + offset_ms, cue.end_ms + offset_ms)
        self._source = copy.deepcopy(source)
        self._question_binding = {'current_cue': cue.text,
            'base_text_sha256': hashlib.sha256(cue.text.encode()).hexdigest(),
            'matched_start_ms': cue.start_ms, 'matched_end_ms': cue.end_ms,
            'source_media_timeline_offset_ms': offset_ms, 'clip_context_binding': context}
        self.issue = digest({'source': source, 'start_ms': cue.start_ms + offset_ms,
                             'end_ms': cue.end_ms + offset_ms})
        self.material = {
            'srt_sha256': hashlib.sha256(srt_text.encode()).hexdigest(),
            'structured_context_sha256': digest(context),
            'audio_observations_sha256': digest({k: v for k, v in finding.items()
                                                if 'observation' in k}),
            'question': str(finding.get('why') or ''),
            'candidate': finding.get('proposed_full_cue'),
        }
        self.bound_ranges = bound_range_evidence(finding)
        if self.bound_ranges:
            self.material['bound_range_evidence'] = self.bound_ranges
            other = {k: v for k, v in finding.items()
                     if 'observation' in k and k != '_prior_range_acoustic_observations'}
            self.material['other_audio_observations_sha256'] = digest(other)
            self.material['audio_observations_sha256'] = digest({
                'ranges': self.bound_ranges, 'other_observations': other,
            })
        base = os.environ.get('AUTOSLICE_BASE')
        if base:
            self.directory = Path(base) / 'cache' / 'semantic-cpa-attempts' / self.issue

    def update_material(self, *, srt_text: str, finding: Mapping[str, Any],
                        clip_context: Mapping[str, Any] | None, offset_ms: int) -> None:
        """Update one call's phase/material without changing its physical history."""
        replacement = SemanticAttemptRecorder(srt_text=srt_text, finding=finding,
                                             clip_context=clip_context, offset_ms=offset_ms)
        if self.directory is None or replacement.directory is None:
            return
        if replacement.issue != self.issue or replacement.directory != self.directory:
            raise ContractRejection('SEMANTIC_ATTEMPT_PHYSICAL_TARGET_DRIFT')
        self.finding, self.plan = replacement.finding, replacement.plan
        self.material, self.bound_ranges = replacement.material, replacement.bound_ranges
        self._clip_context = replacement._clip_context

    def _current_request_srt(self, check_request: Mapping[str, Any]) -> str:
        """Common physical-target/current-text contract for every recorded phase."""
        payload = dict(check_request)
        declared = str(payload.pop('request_sha256', '')).removeprefix('sha256:')
        if declared != digest(payload):
            raise ValueError('request hash')
        start, end, offset = (check_request[key] for key in (
            'matched_start_ms', 'matched_end_ms', 'source_media_timeline_offset_ms'))
        if (any(type(v) is not int for v in (start, end, offset))
                or min(start, offset) < 0 or end <= start
                or (start + offset, end + offset) != self._physical_target):
            raise ValueError('physical target')
        # Read the current self-contained request, never old files or a
        # copied cue index. Rebuilt candidates may keep a different SRT.
        current_srt = check_request['whole_clip_current_srt']
        cues = [c for c in parse_srt_cues(current_srt) if c.text.strip()]
        indexes = check_request['cue_indexes']
        if (not isinstance(indexes, list) or len(indexes) != 1
                or type(indexes[0]) is not int or not 1 <= indexes[0] <= len(cues)):
            raise ValueError('cue index')
        cue = cues[indexes[0] - 1]
        if ((cue.start_ms, cue.end_ms) != (start, end)
                or cue.text != check_request['current_cue']
                or hashlib.sha256(cue.text.encode()).hexdigest()
                != check_request['base_text_sha256']):
            raise ValueError('current cue')
        binding = check_request.get('clip_context_binding')
        if not isinstance(binding, Mapping):
            raise ValueError('source context')
        expected_sources = sorted({str(piece['source_media_sha256'])
            for piece in self._clip_context.get('pieces', [])
            if isinstance(piece, Mapping) and piece.get('source_media_sha256')})
        if (binding.get('source_media_sha256s') != expected_sources
                or any(binding.get(k) != self._clip_context.get(k)
                       for k in ('candidate_id', 'context_sha256'))):
            raise ValueError('source context drift')
        return current_srt

    def _store_audio_material(self, current_srt: str, evidence: list[dict[str, Any]]) -> None:
        # Source pieces may identify pre-recut recordings. Only an explicit
        # runtime source hash identifies the delivered audio in this contract.
        if len({row['source_media_sha256'] for row in evidence}) > 1:
            raise ValueError('conflicting witness sources')
        direct_source = self._clip_context.get('source_media_sha256')
        if direct_source is not None and any(
                row['source_media_sha256'] != str(direct_source).removeprefix('sha256:')
                for row in evidence):
            raise ValueError('witness source drift')
        unique = {digest(row): row for row in evidence}
        self.material = {**self.material,
                         'srt_sha256': hashlib.sha256(current_srt.encode()).hexdigest(),
                         'bound_judge_evidence': [unique[key] for key in sorted(unique)]}

    def _reject_audio_material(self, exc: Exception) -> None:
        self.blocked = 'SEMANTIC_JUDGE_EVIDENCE_BINDING_INVALID'
        self.events.append({'status': self.blocked, 'provider_calls': 0})
        raise RetryNeedsPlan(self.blocked) from exc

    def sync_judge_evidence(self, *, check_request: Mapping[str, Any],
                            witness: Mapping[str, Any]) -> None:
        """Bind actual common-judge evidence without changing its retry history."""
        if self.directory is None:
            return
        from src.autoslice.acoustic_witness_adjudication import retry_judge_evidence
        try:
            current_srt = self._current_request_srt(check_request)
            evidence = retry_judge_evidence(check_request, witness)
            self._store_audio_material(current_srt, evidence)
            argon = [row for row in evidence if row.get('kind') == 'argon_acquisition']
            if argon and self.plan is None:
                rows = self._entries()
                offered = {digest(item) for row in rows if row['status'] in _MATERIAL_ALREADY_OFFERED
                           for item in row['request']['material'].get('bound_judge_evidence', [])}
                if any(digest(item) not in offered for item in argon):
                    self.plan = {
                        'action': 'expand_context',
                        'question': '结合新取得且逐字节验证的Argon候选盲原音证据裁决同一目标闭集。',
                        'why_previous_failed': '此前普通听音与裁决尚未消费这一实际升级请求的回包。',
                        'expected_gain': '比较独立转写与原普通证据，不能多数表决或默认Argon正确。',
                        'verification': '重验源、音频、时钟、原始回包和实际模型；只有CPA显式决定进入原typed消费者。',
                    }
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            self._reject_audio_material(exc)

    def plan_scoped_observation_review(self, *, check_request: Mapping[str, Any],
                                       witness: Mapping[str, Any]) -> None:
        """Plan a concrete final-scope question; never relax recorder admission.

        The generic common judge still requires a caller-supplied plan. This
        final-review caller can formulate one only from validated new hearing,
        or a genuinely narrower target question not already offered. Original
        calls, failed plans and UNKNOWN material remain in the same issue.
        """
        if (self.directory is None
                or check_request.get('require_complete_utterance_support') is not True
                or check_request.get('span_localization_contract')
                != 'located-or-explicitly-unlocatable.v1'):
            return
        from src.autoslice.acoustic_witness_adjudication import retry_judge_evidence
        from src.autoslice.span_phoneme_resolution import edit_binding
        try:
            self._current_request_srt(check_request)
            binding = edit_binding(check_request)
            evidence = retry_judge_evidence(check_request, witness)
            if not binding or not evidence:
                return
            source = str(check_request.get('span_termination_source_sha256') or '').removeprefix('sha256:')
            if not source or any(item['source_media_sha256'] != source for item in evidence):
                return
            rows = self._entries()
            uncertain = [r for r in rows if r['status'] == 'COMPLETED' and _uncertain(r['completion'], r['request'].get('prompt'))]
            if len(uncertain) < 3:
                return
            offered = {digest(item) for r in rows if r['status'] in _MATERIAL_ALREADY_OFFERED
                       for item in r['request']['material'].get('bound_judge_evidence', [])}
            novel = [item for item in evidence if digest(item) not in offered
                     and not any(_legacy_judge_evidence_seen(item, r) for r in rows)]
            basis = None
            if novel and self.plan is None:
                basis = {'kind': 'VALIDATED_TARGET_HEARING_NOT_PREVIOUSLY_OFFERED',
                         'evidence': novel}
            elif not novel:
                # A same-audio refinement is not a fresh listen. Require an
                # actual completed unlocalized wider question and no offered
                # narrower one WITH supplied hearing, including STARTED/UNKNOWN
                # and legacy prompts. An earlier empty text-first catalogue
                # did not ask this scoped acoustic question.
                current_span = binding['codepoint_span']
                already_offered = False
                parents = []
                for row in rows:
                    if row['status'] not in _MATERIAL_ALREADY_OFFERED:
                        continue
                    prompt = row['request'].get('prompt', '')
                    if '\nSPAN_REVIEW_INPUT\n' not in prompt:
                        continue
                    try:
                        block = json.loads(prompt.split('\nSPAN_REVIEW_INPUT\n', 1)[1]
                                           .split('\nEND_SPAN_REVIEW_INPUT\n', 1)[0])
                        old = block['binding']
                        interval = old['codepoint_span']
                    except (ValueError, KeyError, TypeError, AttributeError):
                        continue
                    if (old.get('current_cue_sha256') != binding['current_cue_sha256']
                            or old.get('physical_cue_window_ms') != binding['physical_cue_window_ms']):
                        continue
                    scoped_hearing_present = bool(block.get('observations'))
                    if interval == current_span and scoped_hearing_present:
                        already_offered = True
                    if (row['status'] == 'COMPLETED'
                            and (interval != current_span or not scoped_hearing_present)
                            and interval[0] <= current_span[0] < current_span[1] <= interval[1]):
                        reply = extract_json_object(row['completion'])
                        scope = reply.get('span_review')
                        if not isinstance(scope, Mapping) or scope.get('status') == 'UNLOCATABLE':
                            parents.append(row['completion_sha256'])
                if parents and not already_offered:
                    basis = {'kind': 'BOUND_HEARING_UNOFFERED_FOR_THIS_LOCALIZED_QUESTION',
                             'parent_unlocalized_completion_sha256s': sorted(set(parents))}
            if basis is None:
                return
            target = {'physical_cue_window_ms': binding['physical_cue_window_ms'],
                      'codepoint_span': binding['codepoint_span'],
                      'current_span': binding['current_span'],
                      'replacement_candidate': binding['replacement'],
                      'source_media_sha256': source}
            previous_plan = self.plan
            self.plan = {
                'action': 'correct_localization',
                'question': '只裁决以下实际修改跨度，逐项定位已绑定原音子串；其余共同前缀/尾词问题单独列为义务：'
                            + json.dumps(target, ensure_ascii=False, sort_keys=True),
                'why_previous_failed': ('此前原请求未消费本次新原音答复，不能用旧applied或旧未决遮掉新分歧。'
                    if novel else '此前完整原答复在更宽范围或没有目标原音时留为未决；这个定位范围配合已绑定听写尚未实际询问。'),
                'expected_gain': '区分有支持的目标修订与独立未决部分；有分歧保留原始依据，不按模型数量当真值。',
                'verification': '只引用原观察和原子串，返回已定位或明确不可定位；重验源/时钟/原始回包及完整typed历史，未知绝不算否定。',
            }
            self.material['scoped_continuation_basis'] = {
                **basis, 'target': target, 'previous_plan': previous_plan,
                'mutation_authorized': False}
        except (ValueError, TypeError, KeyError, AttributeError, RetryNeedsPlan):
            # Planning cannot open a bad request/history. The ordinary sync and
            # recorded admission still emit their original fail-closed reason.
            return

    def retained_current_proposal(self, *, check_request, proposal_audio):
        """Reuse an actual old audio-supported hypothesis without calling again.

        Only CURRENT-identical candidates are recalled here. This cannot edit
        text or dismiss the current conflicting finding; it enters the existing
        current-only closed set so an actual judge can ask the boundary question.
        """
        if self.directory is None or self.finding.get('content_risk') != 'meaning_reversal':
            return None
        self.sync_proposal_audio(check_request=check_request, proposal_audio=proposal_audio)
        evidence = self.material.get('bound_judge_evidence') or []
        if not evidence:
            return None
        current = check_request['current_cue']
        for row in reversed(self._entries()):
            if row['status'] != 'COMPLETED' or '# 字幕缺失候选重建' not in row['request']['prompt']:
                continue
            payload = extract_json_object(row['completion'])
            supported = proposal_status(payload) == 'CURRENT_SUPPORTED'
            if supported:
                # The physical account validates source/clock, but can span
                # text revisions. Recover the wording actually asked, then G6.
                import re
                from src.autoslice.audio_question_identity import same_audio_question
                match = re.search(r'^- CURRENT:[ \t]*([^\n]+)', row['request']['prompt'], re.M)
                if match is None:
                    continue
                original = {**check_request, 'current_cue': match[1].strip(),
                    'base_text_sha256': hashlib.sha256(match[1].strip().encode()).hexdigest()}
                if not same_audio_question(original, check_request, compare_proposed=False)[0]:
                    continue
            if ((not supported and (payload.get('status') != 'PROPOSED' or payload.get('proposed_cue') != current))
                    or not all(all(item[key] in row['request']['prompt'] for key in (
                        'source_media_sha256', 'audio_clip_sha256', 'response_sha256')) for item in evidence)):
                continue
            return {'schema_version': 'recorded-current-candidate.v1',
                    'status': 'CURRENT_SUPPORTED' if supported else 'PROPOSED',
                    'issue_sha256': self.issue, 'current_request_sha256': check_request['request_sha256'],
                    'original_request_sha256': row['request_sha256'],
                    'original_prompt_sha256': hashlib.sha256(row['request']['prompt'].encode()).hexdigest(),
                    'completion_sha256': row['completion_sha256'], 'raw_completion': row['completion'],
                    'candidate': current, 'evidence': copy.deepcopy(evidence),
                    'new_provider_calls': 0, 'mutation_authorized': False}
        return None

    def plan_proposal_verification(self, *, check_request, witness):
        """Admit the first verification of an actual evidence-backed proposal.

        This is a stage in the same physical issue/ledger, not fresh evidence
        or an added budget. Legacy verification prompts and all unknown stages
        suppress repetition. Ordinary raw replay still happens before admission.
        """
        if self.directory is None:
            return
        self.material.pop('proposal_verification_basis', None)
        current = self._current_request_srt(check_request)
        evidence = self.material.get('bound_judge_evidence') or []
        ranges = bound_range_evidence({'_prior_range_acoustic_observations':
                                      check_request.get('context_only_range_acoustic_observations', [])})
        if not evidence and not ranges:
            return
        # Entries came through _entries' schema, raw/hash and physical-issue
        # validation. A parent must be a real completed proposal for this text,
        # not an invented candidate or a prior word-judge preference.
        rows = self._entries()
        proposed = check_request['proposed_cue']
        proposals = []
        for row in rows:
            if row['status'] != 'COMPLETED' or '# 字幕缺失候选重建' not in row['request']['prompt']:
                continue
            payload = extract_json_object(row['completion'])
            if (payload.get('status') == 'PROPOSED' and payload.get('proposed_cue') == proposed
                    and (row['request']['material'].get('srt_sha256') == hashlib.sha256(current.encode()).hexdigest()
                         or proposed == check_request['current_cue'])):
                proposals.append(row)
        if not proposals:
            return
        narrow = sorted({(str(x['source_media_sha256']), str(x['audio_clip_sha256']), str(x['response_sha256']))
                         for x in evidence})
        broad = sorted({(str(x['source_media']), str(x['audio_clip']), str(x['raw_response'])) for x in ranges})
        phase = {'kind': 'PROPOSAL_TO_FIRST_WORD_VERIFICATION',
                 'source_target': list(self._physical_target),
                 'current_cue_sha256': hashlib.sha256(check_request['current_cue'].encode()).hexdigest(),
                 'proposed_cue_sha256': hashlib.sha256(proposed.encode()).hexdigest(),
                 'target_span': (check_request.get('edit_span_binding') or {}).get('codepoint_span'),
                 'narrow_evidence': narrow, 'range_evidence': broad}
        phase_id = digest(phase)
        parent = next((r for r in reversed(proposals) if any(
            all(value in r['request']['prompt'] for value in triple)
            for triple in [*narrow, *broad])), None)
        if parent is None:
            return  # Evidence absent from the proposal cannot justify this handoff.
        basis = {'verification_identity_sha256': phase_id, 'phase': phase,
                 'proposal_request_sha256': parent['request_sha256'],
                 'proposal_completion_sha256': parent['completion_sha256'],
                 'new_acquisition': False, 'mutation_authorized': False}
        self.material['proposal_verification_basis'] = basis
        if self._verification_previously_offered(rows, phase):
            return
        self.plan = {
            'action': 'correct_localization',
            'question': '首次验证已生成且有真实原音材料的候选（不是重新生成/重新听音）：' + json.dumps(phase, ensure_ascii=False, sort_keys=True),
            'why_previous_failed': '此前这些材料只完成候选生成，尚未进入该具体当前目标、候选和材料组合的普通闭集验证。',
            'expected_gain': '以原观察核目标及全部外部问题，裁决候选是否成立；不将提案或模型数量当真值。',
            'verification': '重验父提案原始回包/当前SRT/源时钟及材料；仅允许未问过的首次验证，同阶段或UNKNOWN继续拒绝。',
        }

    @staticmethod
    def _verification_previously_offered(rows, phase):
        identity = digest(phase)
        for row in rows:
            if row['status'] not in _MATERIAL_ALREADY_OFFERED:
                continue
            request = row['request']
            previous = request['material'].get('proposal_verification_basis') or {}
            if previous.get('verification_identity_sha256') == identity:
                return True
            prompt = request['prompt']
            if '# 字幕选字裁决' not in prompt and '# 字幕文字闭集裁决' not in prompt:
                continue
            if '\nSPAN_REVIEW_INPUT\n' not in prompt:
                continue  # Unscoped new calls have an explicit phase identity.
            try:
                block = json.loads(prompt.split('\nSPAN_REVIEW_INPUT\n', 1)[1].split('\nEND_SPAN_REVIEW_INPUT\n', 1)[0])
                binding = block['binding']
            except (ValueError, TypeError, KeyError):
                continue
            if (binding.get('current_cue_sha256') != phase['current_cue_sha256']
                    or binding.get('proposed_cue_sha256') != phase['proposed_cue_sha256']
                    or binding.get('codepoint_span') != phase['target_span']
                    or binding.get('physical_cue_window_ms') != phase['source_target']):
                continue
            if all(all(value in prompt for value in triple)
                   for triple in [*phase['narrow_evidence'], *phase['range_evidence']]):
                return True
        return False

    def plan_requested_audio_review(self, *, check_request: Mapping[str, Any],
                                    witness: Mapping[str, Any]) -> None:
        """Open one judge question for hearing the original answers asked for.

        Only when this target's own COMPLETED CPA answers requested audio, the
        admission is past three unresolved answers, and the supplied witness
        projects validated observations never offered to this issue. Already
        offered/UNKNOWN material, an existing plan or a bad history keep the
        ordinary K04/recorder refusal; nothing here dispatches.
        """
        if self.directory is None or self.plan is not None:
            return
        from src.autoslice.acoustic_witness_adjudication import retry_judge_evidence
        try:
            requests = self.original_audio_requests()
            if not requests:
                return
            self._current_request_srt(check_request)
            evidence = retry_judge_evidence(check_request, witness)
            rows = self._entries()
            uncertain = [r for r in rows if r['status'] == 'COMPLETED' and _uncertain(r['completion'], r['request'].get('prompt'))]
            if len(uncertain) < 3 or not evidence:
                return
            offered = {digest(item) for r in rows if r['status'] in _MATERIAL_ALREADY_OFFERED
                       for item in r['request']['material'].get('bound_judge_evidence', [])}
            novel = [item for item in evidence if digest(item) not in offered
                     and not any(_legacy_judge_evidence_seen(item, r) for r in rows)]
            if not novel:
                return
            self.plan = {
                'action': 'expand_context',
                'question': '原答复要求补听；用以下新取得且逐字节验证、此前从未提供给本目标的原音观察裁决同一闭集：'
                            + json.dumps(novel, ensure_ascii=False, sort_keys=True),
                'why_previous_failed': '此前的未决答复都要求更多原音，而这些观察在当时尚未取得或未提供。',
                'expected_gain': '用实际原音区分现文与候选；仍听不清就保留未决，不按模型数量取真值。',
                'verification': '重验源/音频/时钟/原始回包；已提供、UNKNOWN 或缺原件的材料不算新证据，改字仍走typed门。',
            }
            self.material['requested_audio_review_basis'] = {
                'kind': 'ORIGINAL_NEEDS_AUDIO_ANSWERED_BY_UNOFFERED_HEARING',
                'original_audio_request_completion_sha256s': requests,
                'evidence': novel, 'mutation_authorized': False}
        except (ValueError, TypeError, KeyError, AttributeError, RetryNeedsPlan):
            return

    def original_audio_requests(self) -> list[str]:
        """Completion SHAs of this target's own COMPLETED answers asking for audio.

        Read-only over the original recorder rows: these raw CPA answers are the
        ``needs_audio=true`` obligation that a later K04 stop must not strand.
        """
        if self.directory is None:
            return []
        try:
            rows = self._entries()
        except RetryNeedsPlan:
            return []
        found = []
        for row in rows:
            if row.get('status') != 'COMPLETED':
                continue
            try:
                payload = extract_json_object(row['completion'])
            except Exception:
                continue
            if isinstance(payload, Mapping) and payload.get('needs_audio') is True:
                found.append(row['completion_sha256'])
        return found

    def needs_audio_gate(self, *, check_request: Mapping[str, Any],
                         prefer_completion_sha256: str | None = None) -> dict[str, Any] | None:
        """A pending-audio obligation backed by this target's own raw CPA answer.

        Only after the ordinary continuation stop (K04) refused the next call,
        and only from a COMPLETED row whose raw answer explicitly asks for audio
        (needs_audio=true while UNRESOLVED/UNCERTAIN). STARTED/ERROR/UNKNOWN rows
        never open it and are never re-sent (K22 is per request); they do not block
        the request it concerns. The gate binds the row file's content sha256, its
        request and raw-answer digests, the physical target (source + clock) and
        the current cue grid; ``exact_final_witness_authority`` re-opens all of
        it before the existing ``recover_semantic_audio_range`` owner may run.
        It grants collection only: no plan, no mutation, no budget.
        """
        if self.directory is None:
            return None
        k04 = {'CONTINUATION_VALUE_PLAN_REQUIRED', 'CONTINUATION_PLAN_ALREADY_UNRESOLVED'}
        # The stop must be this recorder's latest outcome: a later completed
        # call settles the question and keeps no obligation open.
        if self.blocked not in k04 or not self.events or self.events[-1].get('status') not in k04:
            return None
        from src.autoslice.evidence_identity import regular_file_sha256
        try:
            current_srt = self._current_request_srt(check_request)
            rows = self._entries()
        except (KeyError, TypeError, ValueError, AttributeError, RetryNeedsPlan):
            return None
        # K22 is per request (41:「不完整 STARTED 保留未知，不盲重发。」): an
        # outstanding STARTED/ERROR/UNKNOWN row is never re-sent and stays
        # UNKNOWN, but it does not close the target's other routes. Only a
        # COMPLETED row can be the gate (admin ruling).
        asked = []
        for row in rows:
            if row.get('status') != 'COMPLETED' or not isinstance(row.get('completion'), str):
                continue
            try:
                payload = extract_json_object(row['completion'])
            except Exception:
                continue
            state = payload.get('status') if isinstance(payload, Mapping) else None
            if isinstance(payload, Mapping) and _target_decided_with_outside_questions(
                    payload, row['request'].get('prompt')):
                continue  # Its audio need lies outside this target (span obligation).
            if not (isinstance(payload, Mapping) and payload.get('needs_audio') is True
                    and (state in {'UNRESOLVED', 'UNCERTAIN'}
                         or payload.get('decision') in {'UNRESOLVED', 'UNCERTAIN'}
                         or payload.get('choice') in {'UNCERTAIN', 'CURRENT', 'PROPOSED', 'NEITHER'})):
                continue
            asked.append((row, payload))
        if not asked:
            return None
        preferred = str(prefer_completion_sha256 or '').removeprefix('sha256:')
        chosen = next(((r, p) for r, p in asked if r.get('completion_sha256') == preferred), None)
        row, payload = chosen or max(asked, key=lambda item: item[0].get('started_ns') or 0)
        path = self.directory / (row['request_sha256'] + '.json')
        base = Path(os.environ['AUTOSLICE_BASE'])
        content = regular_file_sha256(path)
        if content is None:
            return None
        cues = [c for c in parse_srt_cues(current_srt) if c.text.strip()]
        reason = payload.get('reason') if isinstance(payload.get('reason'), str) and payload['reason'].strip() \
            else '原CPA答复要求补听（needs_audio=true）。'
        gate = {'schema_version': RECORDER_GATE_SCHEMA, 'status': 'BLOCK', 'needs_audio': True,
                'issue_sha256': self.issue,
                'physical_target': {'source': copy.deepcopy(self._source),
                                    'start_ms': self._physical_target[0], 'end_ms': self._physical_target[1]},
                'row_relpath': str(path.relative_to(base)), 'row_content_sha256': content,
                'row_request_sha256': row['request_sha256'], 'completion_sha256': row['completion_sha256'],
                'stop_reason': self.blocked, 'reason': reason.strip(),
                'cue_grid_sha256': digest([[c.start_ms, c.end_ms] for c in cues]),
                'original_request': copy.deepcopy(dict(check_request)),
                'authority': 'COLLECTION_ONLY', 'mutation_authorized': False}
        gate['gate_sha256'] = digest(gate)
        return gate

    def plan_cached_proposal_audio(self, *, check_request, proposal_audio, acquired=False):
        """One actual-audio proposal question after prior weak-text attempts.

        Audio may already have appeared in a word judge. That is not a new
        acquisition. Only a never-offered audio-bearing proposal phase gets a
        specific plan; exact/unknown prior proposal input still blocks replay.
        ``acquired`` marks audio the normal acquisition just returned for a K04
        stop (it may still be a cache hit of the owner's own record).
        """
        if self.directory is None or self.plan is not None:
            return
        try:
            self.sync_proposal_audio(check_request=check_request, proposal_audio=proposal_audio)
            evidence = self.material.get('bound_judge_evidence', [])
            if not evidence:
                return
            rows = self._entries()
            uncertain = [r for r in rows if r['status'] == 'COMPLETED' and _uncertain(r['completion'], r['request'].get('prompt'))]
            if len(uncertain) < 3:
                return
            marker = '## 音素与原语言证据（数据，不是裁决）'
            relevant = [r for r in rows if r['status'] in _MATERIAL_ALREADY_OFFERED
                        and marker in r['request'].get('prompt', '')]
            keys = ('source_media_sha256', 'audio_clip_sha256', 'response_sha256')
            if any(all(all(item[key] in r['request']['prompt'] for key in keys)
                       for item in evidence) for r in relevant):
                return
            weak = [r['completion_sha256'] for r in relevant if r['status'] == 'COMPLETED'
                    and 'CURRENT_TEXT_PINYIN_WEAK' in r['request']['prompt']]
            if not weak and not acquired:
                return
            self.plan = {
                'action': 'correct_localization',
                'question': '用本次实际缓存原音及原已争议字幕历史生成目标候选，不再从现字幕反推音素：'
                            + json.dumps(evidence, ensure_ascii=False, sort_keys=True),
                'why_previous_failed': ('此前候选生成只有文本派生拼音，未在候选生成这一问题中消费这些已验证原音。'
                    if weak else '此前原答复要求补听，但候选生成这一问题从未收到这些已验证原音。'),
                'expected_gain': '分清仍有原音支持的现文、旧冲突和位置归属；不能从单一旧guo或多数票反改。',
                'verification': '保留原音源/窗/答复与所有旧attempt，生成后仍经普通闭集和typed消费者；未知不算新证据。',
            }
            self.material['cached_audio_proposal_basis'] = {
                'kind': ('ACQUIRED_AUDIO_UNOFFERED_TO_WEAK_PROPOSAL_PHASE' if acquired
                         else 'CACHED_AUDIO_UNOFFERED_TO_WEAK_PROPOSAL_PHASE'),
                'weak_completion_sha256s': weak, 'evidence': evidence,
                'new_acquisition': bool(acquired), 'mutation_authorized': False}
        except (ValueError, TypeError, KeyError, AttributeError, RetryNeedsPlan):
            return  # Original sync/admission owns the eventual refusal.

    def sync_proposal_audio(self, *, check_request: Mapping[str, Any],
                            proposal_audio: Mapping[str, Any] | None) -> None:
        """Consume the native proposal-audio envelope before a subsequent call.

        The raw observation is neither reissued nor rewritten. Blind pinyin
        and original-language transcript receipts keep their own validators
        and request protocols, but share the existing byte-identity novelty
        accounting. Candidate text and weak text-derived pinyin add no audio.
        No continuation plan, provider budget, or previous attempt is changed.
        """
        if self.directory is None:
            return
        from src.autoslice.acoustic_witness_adjudication import (
            build_witness_request, retry_judge_evidence,
        )
        try:
            current_srt = self._current_request_srt(check_request)
            if proposal_audio is not None and not isinstance(proposal_audio, Mapping):
                raise ValueError('proposal audio envelope')
            audio = proposal_audio or {}
            if audio and (audio.get('mutation_authorized') is not False
                          or audio.get('authority') != 'EVIDENCE_ONLY'):
                raise ValueError('proposal audio is evidence only')
            evidence: list[dict[str, Any]] = []
            phonetic = audio.get('phonetic_evidence')
            if phonetic is not None:
                if not isinstance(phonetic, Mapping):
                    raise ValueError('phonetic evidence shape')
                if phonetic.get('schema_version') == 'subtitle-span-acoustic-witness.v1':
                    evidence.extend(retry_judge_evidence(check_request, phonetic))
                elif not (phonetic.get('evidence_level') == 'CURRENT_TEXT_PINYIN_WEAK'
                          and phonetic.get('audio_observed') is False):
                    raise ValueError('unknown phonetic evidence protocol')
            original = audio.get('original_language_observation')
            if original is not None:
                # Invoke the original-language owner, not the blind-pinyin
                # validator, and never manufacture one protocol from another.
                from src.autoslice.exact_source_transcript_contract import (
                    build_exact_source_transcript_request,
                    valid_exact_source_transcript_observation,
                )
                physical = build_exact_source_transcript_request(build_witness_request(check_request))
                if not valid_exact_source_transcript_observation(original, request=physical):
                    raise ValueError('original-language observation binding')
                timeline = original['timeline_binding']
                geometry = timeline['source_media']
                start, end = self._physical_target
                crop_start, crop_end = geometry['crop_start_ms'], geometry['crop_end_ms']
                if (type(crop_start) is not int or type(crop_end) is not int
                        or not 0 <= crop_start <= start < end <= crop_end
                        or (geometry['target_start_ms'], geometry['target_end_ms']) != (start, end)):
                    raise ValueError('original-language physical coverage')
                identity = {key: str(original[key]).removeprefix('sha256:').lower()
                            for key in ('source_media_sha256', 'audio_clip_sha256', 'response_sha256')}
                identity.update(target_start_ms=start, target_end_ms=end,
                                crop_start_ms=crop_start, crop_end_ms=crop_end)
                evidence.append(identity)
            self._store_audio_material(current_srt, evidence)
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            self._reject_audio_material(exc)

    def submitted_history(self) -> list[dict[str, Any]]:
        if self.directory is None:
            return []
        from src.autoslice.semantic_request_history import submitted_history
        try:
            records = submitted_history(finding=self.finding, context=self._clip_context,
                                        target=self._physical_target)
            self.historical_consumption = [dict(status=r['status'], origin=r['origin'],
                original_request_sha256=r['request_sha256']) for r in records]
            return records
        except (ValueError, TypeError, KeyError, OSError, AttributeError) as exc:
            self.blocked = 'SEMANTIC_RETAINED_HISTORY_INVALID'
            # Keep why: the exact reader failure is evidence (no secrets: the
            # history readers raise reason codes or path/key names only).
            detail = {'status': self.blocked, 'provider_calls': 0,
                      'error_type': type(exc).__name__, 'reason_detail': str(exc)[:240]}
            if detail not in self.events:
                self.events.append(detail)
            raise RetryNeedsPlan(self.blocked) from exc

    def _entries(self) -> list[dict[str, Any]]:
        if self.directory is None or not self.directory.exists():
            return []
        entries = []
        for path in sorted(self.directory.glob('*.json')):
            if path.is_symlink():
                raise RetryNeedsPlan('SEMANTIC_ATTEMPT_RECORD_INVALID')
            try:
                row = json.loads(path.read_text())
                if (row.get('schema_version') != SCHEMA or row.get('issue_sha256') != self.issue
                        or digest(row['request']) != row.get('request_sha256')
                        or path.stem != row['request_sha256']
                        or row.get('status') not in {'STARTED', 'COMPLETED', 'ERROR', 'OUTCOME_UNKNOWN', 'NOT_DISPATCHED'}
                        or type(row.get('started_ns')) is not int):
                    raise ValueError('binding')
                if row.get('status') == 'NOT_DISPATCHED':
                    if (row.get('dispatch_outcome') != 'NOT_DISPATCHED'
                            or row.get('error_type') != 'CallNotDispatched'):
                        raise ValueError('undispatched proof')
                    CallNotDispatched(row.get('reason_code'))
                if row.get('status') == 'COMPLETED':
                    if hashlib.sha256(row['completion'].encode()).hexdigest() != row.get('completion_sha256'):
                        raise ValueError('completion')
                    if 'answering_model_identity' in row and (
                            not isinstance(row['answering_model_identity'], Mapping)
                            or row.get('answer_binding_sha256') != digest({
                                'model': row['answering_model_identity'], 'completion_sha256': row['completion_sha256']})):
                        raise ValueError('answer model binding')
            except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
                self.blocked = 'SEMANTIC_ATTEMPT_RECORD_INVALID'
                raise RetryNeedsPlan(self.blocked) from exc
            entries.append(row)
        return sorted(entries, key=lambda row: row.get("started_ns", 0))

    def note_cache_replay(self, *, prompt: str, completion: str, model_identity: Mapping[str, Any]) -> None:
        """Record consumption of a verified outer cache without another dispatch."""
        rows = self._entries()
        def same_prompt(row):
            saved = row['request'].get('prompt', '')
            return saved == prompt or saved.startswith(prompt + '\n## 窄窗证人自一致性')
        unknown = next((row for row in rows
            if row['status'] in {'STARTED', 'ERROR', 'OUTCOME_UNKNOWN'} and same_prompt(row)
            and row['request'].get('model') == dict(model_identity)), None)
        if unknown is not None:
            self.blocked = 'PRIOR_CALL_OUTCOME_UNKNOWN'
            self.events.append({'status': self.blocked, 'provider_calls': 0,
                                'request_sha256': unknown['request_sha256']})
            raise RetryNeedsPlan(self.blocked)
        raw_sha = hashlib.sha256(completion.encode()).hexdigest()
        original = next((row for row in rows if row['status'] == 'COMPLETED'
            and row.get('answering_model_identity') == dict(model_identity)
            and row.get('completion_sha256') == raw_sha and same_prompt(row)), None)
        self.events.append({'status': 'EXACT_RAW_REPLAY', 'provider_calls': 0,
            'request_sha256': original['request_sha256'] if original else None,
            'completion_sha256': raw_sha, 'source': 'RULING_CACHE'})

    def wrap(self, call: Callable[[str], str] | None) -> Callable[[str], str] | None:
        if call is None or self.directory is None:
            return call
        model = getattr(call, 'cpa_cache_identity', None)
        if not isinstance(model, Mapping) or not model:
            return call  # Unknown provider identity cannot establish replay authority.
        native_retry_capable = bool(getattr(call, 'terminal_retry_capable', False))
        retry_timeout = getattr(call, 'timeout_seconds', None)

        def recorded(prompt: str) -> str:
            from src.autoslice.semantic_terminal_adjudication import MARKER
            terminal = prompt.startswith(MARKER)
            from src.autoslice.witness_self_consistency import recorded_narrow_assessment, assessment_prompt
            rows = self._entries()
            if not terminal:
                prompt += assessment_prompt(recorded_narrow_assessment(rows,
                    question=self._question_binding, physical_target=self.issue))
            is_word_verification = '# 字幕选字裁决' in prompt or '# 字幕文字闭集裁决' in prompt
            material = dict(self.material)
            if not is_word_verification:
                material.pop('proposal_verification_basis', None)
            request = copy.deepcopy({'prompt': prompt, 'model': dict(model), 'material': material,
                                     'continuation_plan': self.plan})
            other_answers = [row['completion_sha256'] for row in rows
                if row['status'] == 'COMPLETED' and row['request'].get('prompt') == prompt
                and row['request'].get('model') == dict(model)
                and row.get('answering_model_identity', dict(model)) != dict(model)]
            if other_answers:
                request['prior_other_model_answers'] = sorted(other_answers)
            request_sha = digest(request)
            if terminal:
                from src.autoslice.targeted_relisten import validate_no_gain
                proof = getattr(self, 'terminal_no_gain', None)
                if not isinstance(proof, Mapping):
                    raise RetryNeedsPlan('TERMINAL_NO_GAIN_PROOF_REQUIRED')
                validate_no_gain(proof, getattr(self, 'terminal_check_request', {}))
                request['terminal_no_gain_proof_sha256'] = proof['proof_sha256']
                request_sha = digest(request)
            rows = self._entries()
            prior = next((r for r in rows if r['request_sha256'] == request_sha), None)
            # Old ERROR rows did not prove non-dispatch. Keep them unknown,
            # as with STARTED/crashed calls; changing only a continuation plan
            # cannot authorize resending the same actual transport input.
            unknown = next((row for row in rows if row['status'] in {'STARTED', 'ERROR', 'OUTCOME_UNKNOWN'}
                            and all(row['request'].get(key) == request[key]
                                    for key in ('prompt', 'model'))), None)
            if unknown is not None:
                self.blocked = 'PRIOR_CALL_OUTCOME_UNKNOWN'
                self.events.append({'status': self.blocked, 'request_sha256': unknown['request_sha256'],
                                    'provider_calls': 0})
                raise RetryNeedsPlan(self.blocked)
            # The actual transport input is prompt + model. New material
            # fields/plan metadata must neither launder UNKNOWN nor resend an
            # already completed identical input after an upgrade. No rewrite
            # of an old row is needed to reuse its original response bytes.
            from .llm_client import completion_identity, cached_completion
            unattributed = next((row for row in rows if row['status'] == 'COMPLETED'
                and row['request'].get('prompt') == prompt
                and row['request'].get('model') == dict(model)
                and 'answering_model_identity' not in row), None)
            if unattributed is not None:
                self.blocked = 'COMPLETED_ANSWER_MODEL_UNKNOWN'
                self.events.append({'status': self.blocked, 'provider_calls': 0,
                    'request_sha256': unattributed['request_sha256']})
                raise RetryNeedsPlan(self.blocked)
            completed = next((row for row in rows if row['status'] == 'COMPLETED'
                              and row['request'].get('prompt') == prompt
                              and row.get('answering_model_identity') == dict(model)), None)
            if completed is not None:
                self.events.append({'status': 'EXACT_RAW_REPLAY',
                                    'request_sha256': completed['request_sha256'],
                                    'provider_calls': 0})
                return cached_completion(completed['completion'],
                    completed['answering_model_identity'])
            # Original archives are consumption evidence only. They cannot
            # replay an old decision as this call's current answer.
            from src.autoslice.semantic_request_history import submitted_question
            submitted = self.submitted_history()
            duplicate = None if terminal else submitted_question(prompt, self.bound_ranges, submitted, self.plan)
            if duplicate is not None:
                self.blocked = duplicate['status']
                self.events.append(duplicate)
                raise RetryNeedsPlan(self.blocked)
            phase = material.get('proposal_verification_basis')
            if isinstance(phase, Mapping) and self._verification_previously_offered(rows, phase['phase']):
                self.blocked = 'VERIFICATION_PHASE_ALREADY_OFFERED'
                self.events.append({'status': self.blocked, 'provider_calls': 0,
                                    'verification_identity_sha256': phase['verification_identity_sha256']})
                raise RetryNeedsPlan(self.blocked)
            evidence = self.material.get('bound_judge_evidence', [])
            prior_evidence = [item for row in rows if row['status'] in _MATERIAL_ALREADY_OFFERED
                              for item in row['request']['material'].get('bound_judge_evidence', [])]
            if evidence and prior_evidence and {
                    item['source_media_sha256'] for item in evidence
            } != {item['source_media_sha256'] for item in prior_evidence}:
                self.blocked = 'SEMANTIC_JUDGE_SOURCE_DRIFT'
                self.events.append({'status': self.blocked, 'provider_calls': 0})
                raise RetryNeedsPlan(self.blocked)
            # Use all offered evidence, including unresolved transport outcomes.
            # A proposal -> convergence change of prompt cannot offer the same
            # UNKNOWN attempt's audio as fresh material. Proven NOT_DISPATCHED
            # remains excluded, and genuinely different bytes may still qualify.
            seen = {digest(item) for item in prior_evidence}
            novel = [item for item in evidence if digest(item) not in seen
                     and not any(_legacy_judge_evidence_seen(item, row) for row in rows)]
            uncertain = [r for r in rows if r.get('status') == 'COMPLETED' and _uncertain(r['completion'], r['request'].get('prompt'))]
            # Each completed raw response is counted once; a cache replay is not
            # a new uncertain answer and model identities are not votes.
            count = len(uncertain)
            previous_material = uncertain[-1]['request']['material'] if uncertain else {}
            changed = [key for key in ('srt_sha256', 'structured_context_sha256', 'audio_observations_sha256')
                       if previous_material and previous_material.get(key) != self.material[key]]
            if 'bound_judge_evidence' in self.material:
                # A finding-level digest also changes when the same observation
                # is carried/reordered or gains cache metadata. At the common
                # judge boundary, only its validated evidence projection and
                # the existing source-bound range contract prove new audio.
                changed = [key for key in changed if key != 'audio_observations_sha256']
                prior_ranges = [item for row in rows if row['status'] in _MATERIAL_ALREADY_OFFERED
                                for item in row['request']['material'].get('bound_range_evidence', [])]
                range_keys = ('source_media', 'audio_clip', 'raw_response')
                if any(not _range_seen(item, rows) and not any(
                        all(item.get(k) == old.get(k) for k in range_keys)
                        for old in prior_ranges) for item in self.bound_ranges):
                    changed.append('audio_observations_sha256')
            if novel:
                changed.append('bound_judge_evidence')
            reason: dict[str, Any] = {'prior_unresolved_answers': count,
                                     'scrutiny': 'INCREASED' if count >= 3 else 'NORMAL',
                                     'continuation_plan': self.plan,
                                     'changed_material_fields': changed}
            if not terminal and count >= 3 and self.plan is None:
                self.blocked = 'CONTINUATION_VALUE_PLAN_REQUIRED'
                self.events.append({**reason, 'status': self.blocked, 'provider_calls': 0})
                raise RetryNeedsPlan(self.blocked)
            if self.plan is not None:
                previous_plans = [r['request'].get('continuation_plan') for r in uncertain]
                # Repeating a declared plan with only cosmetic prompt/counter
                # changes does not create a new experiment after repeated failure.
                # Edits elsewhere in the film do not make an already failed
                # plan a new target experiment. A meaningful reframing needs
                # a new plan, or changed structured/audio evidence.
                evidence_changed = [key for key in changed if key != 'srt_sha256']
                if (self.bound_ranges and all(_range_seen(item, rows) for item in self.bound_ranges)
                        and previous_material.get('other_audio_observations_sha256', digest({}))
                        == self.material['other_audio_observations_sha256']
                        and 'audio_observations_sha256' in evidence_changed):
                    evidence_changed.remove('audio_observations_sha256')
                if not terminal and count >= 3 and not evidence_changed and self.plan in previous_plans:
                    self.blocked = 'CONTINUATION_PLAN_ALREADY_UNRESOLVED'
                    self.events.append({**reason, 'status': self.blocked, 'provider_calls': 0})
                    raise RetryNeedsPlan(self.blocked)
                reason['basis'] = 'EXPLICIT_NEXT_QUESTION_AND_CHECK'
            elif changed:
                reason['basis'] = 'CHANGED_BOUND_MATERIAL'
            else:
                reason['basis'] = 'INITIAL_OR_REFRAMED_QUESTION'
            path = self.directory / (request_sha + '.json')
            entry = {'schema_version': SCHEMA, 'issue_sha256': self.issue,
                     'request_sha256': request_sha, 'request': request,
                     'continuation_reason': reason, 'status': 'STARTED', 'started_ns': time.time_ns()}
            if prior is not None and prior['status'] == 'NOT_DISPATCHED':
                entry['prior_not_dispatched_failures'] = [
                    *prior.get('prior_not_dispatched_failures', []),
                    {key: prior[key] for key in ('started_ns', 'error_type', 'reason_code', 'dispatch_outcome')},
                ]
            # Save why before dispatch, including on the fourth or later attempt.
            # Claim the actual transport input, not cosmetic material/plan fields.
            # A killed claimant stays unknown; only proven NOT_DISPATCHED opens
            # the next generation. No file lock spans a remote call.
            claim = digest({'prompt': prompt, 'model': dict(model),
                            **({'completed_other_model_answers': sorted(other_answers)} if other_answers else {}),
                            'not_dispatched': [r['started_ns'] for r in rows
                                if r['status'] == 'NOT_DISPATCHED'
                                and all(r['request'].get(k) == request[k] for k in ('prompt', 'model'))]})
            if not atomic_write(self.directory / '.dispatch-claims' / claim,
                                request_sha.encode(), create_only=True):
                self.blocked = 'PRIOR_CALL_OUTCOME_UNKNOWN'
                self.events.append({'status': self.blocked, 'request_sha256': request_sha,
                                    'provider_calls': 0})
                raise RetryNeedsPlan(self.blocked)
            _atomic(path, entry)
            try:
                if native_retry_capable:
                    # build_llm_call owns the same terminal scope already; an
                    # outer wrapper here would count and sleep every physical
                    # attempt twice.
                    completion = call(prompt)
                else:
                    # Plain adapters still need the terminal transport policy,
                    # but keep one recorder row for all physical attempts.
                    from src.autoslice.infra_retry_policy import terminal_retry_call
                    completion = terminal_retry_call(
                        lambda: call(prompt), timeout_seconds=retry_timeout
                    )
            except Exception as exc:
                if type(exc) is CallNotDispatched:
                    failure = {**entry, 'status': 'NOT_DISPATCHED', 'dispatch_outcome': 'NOT_DISPATCHED',
                               'error_type': 'CallNotDispatched', 'reason_code': exc.reason_code}
                    self.events.append({'status': 'NOT_DISPATCHED', 'request_sha256': request_sha,
                                        'provider_calls': 0, 'reason_code': exc.reason_code})
                else:
                    failure = {**entry, 'status': 'OUTCOME_UNKNOWN', 'dispatch_outcome': 'UNKNOWN',
                               'error_type': type(exc).__name__}
                    self.blocked = 'CALL_OUTCOME_UNKNOWN'
                    self.events.append({'status': self.blocked, 'request_sha256': request_sha,
                                        'provider_calls': None})
                _atomic(path, failure)
                raise
            answering_identity = completion_identity(model, completion)
            completion_sha = hashlib.sha256(completion.encode()).hexdigest()
            _atomic(path, {**entry, 'status': 'COMPLETED', 'completion': completion,
                           'answering_model_identity': answering_identity,
                           'answer_binding_sha256': digest({'model': answering_identity, 'completion_sha256': completion_sha}),
                           'completion_sha256': completion_sha})
            self.events.append({**reason, 'status': 'COMPLETED', 'request_sha256': request_sha,
                                'provider_calls': 1, 'unresolved_answer': _uncertain(completion, prompt)})
            return completion

        from .completed_review_checkpoint import inherit_ruling_context
        inherit_ruling_context(recorded, call)
        # Preserve the decision made above even when an adapter omitted an
        # optional timeout attribute; request_terminal_answer can then inspect
        # the wrapper without inventing a second retry layer.
        recorded.terminal_retry_capable = native_retry_capable
        if not hasattr(recorded, 'timeout_seconds'):
            recorded.timeout_seconds = retry_timeout
        recorded._semantic_attempt_recorder = self
        return recorded

    def audit(self) -> dict[str, Any]:
        return {'schema_version': 'semantic-retry-progress.v1', 'events': copy.deepcopy(self.events),
                'status': self.blocked or 'RECORDED', 'mutation_authorized': False,
                'historical_consumption': copy.deepcopy(self.historical_consumption),
                'counter_scope': 'new raw CPA UNRESOLVED/UNCERTAIN answers captured by this protocol; original archive consumption is read-only and excluded'}


def retry_context(srt_text: str, clip_context: Mapping[str, Any] | None, offset_ms: int) -> str:
    """Give ordinary discovery the actual failures needed to plan a next check."""
    summaries = []
    for index, _cue in enumerate(parse_srt_cues(srt_text), 1):
        recorder = SemanticAttemptRecorder(srt_text=srt_text, finding={'cue_index': index},
                                           clip_context=clip_context, offset_ms=offset_ms)
        if recorder.directory is None or not recorder.directory.exists():
            continue
        try:
            rows = [row for row in recorder._entries() if row.get('status') == 'COMPLETED'
                    and _uncertain(row['completion'], row['request'].get('prompt'))]
        except RetryNeedsPlan:
            summaries.append({'cue': index, 'status': 'SEMANTIC_ATTEMPT_RECORD_INVALID'})
            continue
        if len(rows) < 3:
            continue
        previous = []
        for row in rows[-3:]:
            response = extract_json_object(row['completion'])
            previous.append({'question': row['request']['material'].get('question'),
                             'continuation_plan': row['request'].get('continuation_plan'),
                             'response': {key: response[key] for key in
                                          ('status', 'decision', 'choice', 'needs_audio', 'reason', 'literal_uncertainty', 'span_review')
                                          if key in response},
                             'request_sha256': row['request_sha256']})
        summaries.append({'cue': index, 'unresolved_answers': len(rows), 'recent_attempts': previous})
    if not summaries:
        return ''
    return ('\n\n当前实际源窗口已记录的未决尝试（不是票数或逐字结论）：\n'
            '若仍有具体可验证的下一步，在finding附continuation_plan，说明旧框架/定位/语言/材料哪里不足、'
            '下一问题、预期收益与核验方法。可以超过三次；不能重复同一失败计划。'
            '若确无有价值下一步，保留未决并说明缺口，不编造继续理由。\n'
            + json.dumps(summaries, ensure_ascii=False, sort_keys=True))
