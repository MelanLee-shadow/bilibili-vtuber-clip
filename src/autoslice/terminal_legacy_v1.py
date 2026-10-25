"""Frozen inline-v1 receipt reader. Never used to produce a new request.

Only receipts without terminal_contract_version use this historical renderer;
the current producer/validator contract lives in semantic_terminal_adjudication.
"""

from .review_errors import ContractRejection
import json
import hashlib
from functools import lru_cache
from .semantic_terminal_adjudication import KEEP_ROUTES, MARKER
from .subtitle_acceptability_policy import validate_policy, load_policy
from .meaning_difference import equivalence_key
from .jingting_chunker import parse_srt_cues
from .refrain_detection import detect_refrains, refrain_support


def current_support_refs(request):
    # Frozen prompt reconstruction retains its old comparison view. The
    # consumer reads routes/B support from these same materials: a later,
    # relaxed support predicate never authorizes an old answer. Guards (clear
    # audio) follow their current authority definition; nothing is rewritten.
    from .semantic_terminal_adjudication import current_support_refs as current
    return current(request, legacy=True)


@lru_cache(maxsize=32)
def _refrains(srt_text, policy_json):
    return detect_refrains(parse_srt_cues(srt_text), policy=json.loads(policy_json))


@lru_cache(maxsize=16)
def _session_restatements(srt_text):
    from src.autoslice.restatement_recall import discover_restatement_findings
    session_cues = parse_srt_cues(srt_text)
    end = max((c.end_ms for c in session_cues), default=0) / 1000
    return discover_restatement_findings(srt_text, max_gap_seconds=max(90, end))

def candidate_evidence_support(text, observations, *, cues=(), policy=None, span_text=None, refrains=None):
    """Deterministic support summary, never votes or a release authority.

    Provider transcripts/word projections retain their original source labels;
    matching words in subtitle context alone is not acoustic support.
    """
    policy = policy or load_policy()
    full_key = equivalence_key(text, policy)
    span_key = equivalence_key(span_text, policy) if span_text else ''
    matches = []
    def visit(value, origin, usable=False):
        if isinstance(value, dict):
            usable = value.get('status') in {'OBSERVED', 'UNCERTAIN'} or usable
            for field, child in value.items():
                if usable and field in {'transcript', 'exact_transcript', 'full_transcript', 'heard'} and isinstance(child, str):
                    heard = equivalence_key(child, policy)
                    if full_key and full_key in heard:
                        matches.append({'origin': origin, 'field': field, 'quote': child, 'scope': 'CUE_TEXT'})
                    elif span_key and span_key in heard:
                        matches.append({'origin': origin, 'field': field, 'quote': child,
                            'scope': 'TARGET_SPAN_ONLY_OUTSIDE_OBLIGATIONS_RETAINED'})
                elif isinstance(child, (dict, list)):
                    visit(child, origin, usable)
        elif isinstance(value, list):
            for child in value:
                visit(child, origin, usable)
    for i, row in enumerate(observations):
        visit(row, row.get('observation_id', row.get('response_sha256', str(i))))
    if refrains is None:
        refrains = detect_refrains(cues, policy=policy) if cues else []
    support = refrain_support(text, refrains, policy=policy)
    return {'transcript_matches': matches, 'refrain_support': support,
        'scope': ('CUE_TEXT' if any(m['scope'] == 'CUE_TEXT' for m in matches)
            else 'TARGET_SPAN_ONLY_OUTSIDE_OBLIGATIONS_RETAINED' if span_key else 'CUE_TEXT'),
        'supported': bool(matches or support), 'authority': 'EVIDENCE_ONLY'}


def _without_planning(value):
    if isinstance(value, dict):
        return {k: _without_planning(v) for k, v in value.items()
            if 'needs_audio' not in k and k not in {'prompt', 'raw_completion', 'raw_response',
                'prior_audio_adjudication', 'history_audio_recovery'}}
    if isinstance(value, list):
        return [_without_planning(v) for v in value]
    return value


def stopped_evidence_summary(finding):
    """Project saved evidence, not the recursively repeated audit/request trees.

    The proof retains the entire stopped row and its hash. Every distinct
    diagnostic, hearing and judge decision remains represented with its own
    source/request/response identity; old audit observations are not promoted
    to the validated projection or counted as new listening.
    """
    if not isinstance(finding, dict):
        return None
    fields = {'status', 'reason_code', 'policy_branch', 'detail', 'reason', 'choice', 'decision',
        'request_sha256', 'check_request_sha256', 'prompt_sha256', 'completion_sha256',
        'response_sha256', 'source_media_sha256', 'audio_clip_sha256', 'task_id',
        'transcript', 'exact_transcript', 'heard', 'heard_pinyin', 'uncertainties',
        'target_audible', 'candidate_exposure', 'witness_protocol', 'grade', 'best_guess'}
    seen = {}
    def visit(value):
        if isinstance(value, dict):
            selected = {key: child for key, child in value.items() if key in fields
                and (not isinstance(child, (dict, list)) or key == 'uncertainties')}
            if any(key in selected for key in ('reason_code', 'policy_branch', 'choice', 'decision',
                    'transcript', 'exact_transcript', 'heard', 'heard_pinyin', 'task_id',
                    'reason', 'uncertainties')):
                encoded = json.dumps(selected, ensure_ascii=False, sort_keys=True)
                seen[hashlib.sha256(encoded.encode()).hexdigest()] = selected
            for child in value.values():
                if isinstance(child, (dict, list)):
                    visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
    visit(finding.get('exact_release_adjudication') or {})
    return {'finding': {k: finding.get(k) for k in ('cue_index', 'repair_class', 'suspect',
            'suggestion', 'proposed_full_cue', 'why', 'content_risk', 'risk_basis')},
        'recorded_evidence': list(seen.values()),
        'authority': 'ORIGINAL_STOP_AUDIT_ONLY_NOT_A_NEW_OR_VALIDATED_HEARING'}


def terminal_observation_projection(request):
    """Canonical model/guard view; remove planning payloads in one place."""
    projection = request['audio_gain_termination']['projection']
    if any('raw_observation' in row and 'target_text' not in row for row in projection['observations']):
        from src.autoslice.targeted_relisten import replay_no_gain_projection
        projection = replay_no_gain_projection(request['audio_gain_termination'], request['check_request'],
            include_target_text=True)
    return _without_planning(projection)


def terminal_materials(request, *, historical_index=False):
    """Produce ALL model materials and their exact citation index together.

    Schema integration seam: terminal_schema(request) must use
    list(terminal_materials(request)[1]) as evidence_refs.items.enum.
    Values are JSON paths into the returned materials, never guessed aliases.
    """
    policy = validate_policy(request['terminal_policy'])
    check = request['check_request']
    proof = request['audio_gain_termination']
    cues = parse_srt_cues(check.get('whole_clip_current_srt', ''))
    refrains = _refrains(check.get('whole_clip_current_srt', ''), json.dumps(policy, sort_keys=True))
    session_restatements = []
    sessions = request['terminal_context'].get('whole_session_transcripts', [])
    for row in sessions:
        findings, receipt = _session_restatements(row['srt_text'])
        session_restatements.append({'recording_sha256': row['recording_sha256'],
            'srt_sha256': row['srt_sha256'], 'scope': 'WHOLE_SESSION_SOURCE_CLOCK_CONTEXT_ONLY',
            'candidates': findings, 'refrain_context': receipt.get('refrain_context', []),
            'refrain_excluded_pairs': receipt.get('refrain_excluded_pairs', [])})
    current = request.get('current_cue', check.get('current_cue', ''))
    data = {'policy': policy, 'current': current,
        'proposed': check.get('proposed_cue'), 'cue_index': request.get('cue_index'),
        'whole_clip_srt': check.get('whole_clip_current_srt', ''),
        'whole_session_and_session_context': request['terminal_context'],
        'whole_session_transcript_status': 'PROVIDED' if sessions else 'NOT_PROVIDED',
        'whole_session_restatements': session_restatements,
        'glossary': request['terminal_glossary'],
        'refrains': refrains, 'current_refrain_support': refrain_support(current, refrains, policy=policy),
        'observations': terminal_observation_projection(request)['observations'], 'audio': request.get('audio', {}),
        'material_exhaustion': proof.get('material_exhaustion'),
        'no_gain_proof_sha256': proof.get('proof_sha256'),
        'context_sources': proof['context_sources'], 'events': request.get('events', []),
        'context_before': request.get('context_before'), 'context_after': request.get('context_after'),
        'current_support_summary': candidate_evidence_support(current,
            proof['projection']['observations'], cues=cues, policy=policy, refrains=refrains),
        'editorial_request_sha256': hashlib.sha256(json.dumps(request, ensure_ascii=False,
            sort_keys=True, separators=(',', ':')).encode()).hexdigest()}
    data['keep_routes'] = KEEP_ROUTES
    data['current_support_observation_refs'] = current_support_refs(request)
    from src.autoslice.targeted_relisten import terminal_clear_audio_catalogue
    data['clear_audio_constraints'] = terminal_clear_audio_catalogue(terminal_observation_projection(request))
    if proof.get('stopped_finding') is not None:
        data.update(stopped_finding_evidence=stopped_evidence_summary(proof['stopped_finding']),
            stop_reason_code=proof['stop_reason_code'])
    data = _without_planning(data)
    from .prompt_materials import terminal_material_index
    refs = terminal_material_index(data, named_materials=not historical_index)
    return data, refs


def terminal_prompt(request):
    if request['audio_gain_termination'].get('material_exhaustion') is None:
        raise ContractRejection('TERMINAL_MATERIAL_EXHAUSTION_REQUIRED')
    data, refs = terminal_materials(request, historical_index=True)
    return (MARKER + '\n'
        '材料已按原合同核验穷尽，本轮必须终裁，不再规划补听。先 B 后 C/D：先综合多路宽窗、原生词时间、'
        'refrain、后文重述、整场转写、场次语境、词表与音素；证据能定下读法就在 B 档收口。'
        'Argon 只是稍高权威；自相矛盾的窄窗证人可以提疑，不能构成冲突或维持未决。'
        '反复台词是 refrain，其变体作语境支持，不能用后一次覆盖前一句。清楚原话必须保留。'
        'clear_audio_constraints 是同一观察投影中的可靠目标原话，输出必须保留其中清楚的片段；'
        '若拟改字与其冲突，采用与清楚原话一致的读法完成终裁，不留未决。窄窗拼音不能单独维持阻挡。\n'
        '选项读 policy.terminal_choices。CURRENT 保留；PROPOSED 采用已提供候选；CONTEXT_FIT '
        '仅对听不清部分按整篇语境拟合，按 policy.c_rendering 尽量保留原文、只改到通顺；'
        '只有被省部分完全听不清且留下的话与交际作用没有说错才可缩短，不补造事实、姓名或立场。'
        '无来源专名只差人名用字/是否人名时保留现稿，在 best_guess 内部给最好猜测，不拦下；'
        '有真实来源才按来源写，嘉宾称呼不进入主播词表。\n'
        'UNRESOLVABLE 只在 B 档全部证据仍定不下、写出来的话会把否定/指代/对象/立场说错且'
        '语境也定不下时使用；不能因少了完全听不清的片段或不影响意思的差异判 D。'
        '用 comparison_text 与 CURRENT 的确定性含义差异核对。不得用当前稿默认正确绕过清楚原音冲突。\n'
        '只回 JSON：choice、grade(A/B/C/D)、reason、evidence_refs（实际 observation_id 或 context_refs）、'
        'evidence_decisive（布尔）、context_can_decide（布尔）、comparison_text、name_only（布尔）、'
        'written_meaning_error（会主张什么错误的具体意思）、best_guess（内部）；无来源姓名保留时必须给 '
        'name_span=[原稿姓名槽起点,终点]；有来源时给 name_source_ref（场次字段名）、name_source_text '
        '（来源逐字引文）、sourced_name（引文中的确切称呼），写出该来源称呼，槽外清楚原话照原话。保留原稿时另给完整 '
        'keep_route，取值和适用档位只读 keep_routes。B_EVIDENCE_SUPPORTS_CURRENT 用于证据直接支持现稿，'
        'evidence_refs 至少引用 current_support_observation_refs 中一条真实观察，不要求虚构另一种读法；'
        'C_MEANING_EQUIVALENT 用于仍有不影响意思的不确定读法，必须给 meaning_equivalence 对象：'
        'status=EQUIVALENT；current_meaning/alternative_meaning/shared_meaning/uncertain_detail/context_basis '
        '均为非空字符串；evidence_cue_ids 为相关字幕 cue 的整数编号数组（不是证据编号或字符串）。'
        '改字和UNRESOLVABLE的keep_route为null。改字时同时给既有 typed 编辑字段：'
        'status=RESOLVED、mode=faithful_paraphrase、edit_start/edit_end（原 cue Unicode 半开区间）、'
        'replacement、reliable_fragments[{text,basis}]、evidence_refs、meaning_basis、'
        'literal_uncertainty、no_new_facts=true；只在真实来源支持时可用既有 bound_event_spelling 模式。'
        'UNRESOLVED_BY_AUDIO 等仅供内部审计，任何观众文字不得出现。\n'
        + '可引用编号（只用这些精确编号，禁止自行拼 JSON 路径）：' + json.dumps(refs, ensure_ascii=False, sort_keys=True) + '\n'
        + json.dumps(data, ensure_ascii=False, sort_keys=True))


def original_meaning_prompt(request):
    return _original_meaning_prompt(json.dumps(request, ensure_ascii=False, sort_keys=True))


@lru_cache(maxsize=8)
def _original_meaning_prompt(serialized_request):
    """The pre-route 0d19eb72 contract, reconstructed without changing raw.

    This version exposed one material JSON block, no citation table, and
    required meaning_equivalence for every keep (including grade B). Its
    projection predates the derived clear-audio/keep-route columns.
    """
    request = json.loads(serialized_request)
    data, _ = terminal_materials(request, historical_index=True)
    for key in ('keep_routes', 'current_support_observation_refs', 'clear_audio_constraints'):
        data.pop(key, None)
    data['observations'] = _without_planning(request['audio_gain_termination']['projection']['observations'])
    prefix = terminal_prompt(request).rsplit('\n', 2)[0]
    clear = ('clear_audio_constraints 是同一观察投影中的可靠目标原话，输出必须保留其中清楚的片段；'
        '若拟改字与其冲突，采用与清楚原话一致的读法完成终裁，不留未决。窄窗拼音不能单独维持阻挡。')
    prefix = prefix.replace(clear, '')
    start = prefix.index('keep_route，取值和适用档位只读 keep_routes。')
    stop = prefix.index('改字时同时给既有 typed 编辑字段：', start)
    prefix = (prefix[:start]
        + 'meaning_equivalence 对象：status=EQUIVALENT/current_meaning/alternative_meaning/shared_meaning/'
          'uncertain_detail/context_basis/evidence_cue_ids。' + prefix[stop:])
    prefix = prefix.replace('reliable_fragments[{text,basis}]、evidence_refs、meaning_basis、',
        'reliable_fragments[{text,basis}]、evidence_refs（含 audio）、meaning_basis、')
    return prefix + '\n' + json.dumps(data, ensure_ascii=False, sort_keys=True)
