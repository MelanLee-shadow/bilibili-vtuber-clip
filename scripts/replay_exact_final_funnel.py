#!/usr/bin/env python3
"""Private, offline exact-final evidence projection and replay. Never calls a model."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
from pathlib import Path
import sys
import subprocess
import types
from collections import Counter
import os

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.autoslice.review_evidence_store import load_review


def sha(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':')).encode()).hexdigest()


def walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def project_run(directory, *, runtime=False):
    review_path = next(directory.glob('*.review-flags.json'))
    authority_path = next(directory.glob('*.chat-authority.json'))
    review = load_review(review_path)
    authority = load_review(authority_path)
    rows = []
    requests = {}
    for finding in review['findings']:
        audit = finding.get('exact_release_adjudication') or {}
        records = list(walk(audit))
        for value in records:
            if value.get('schema_version') == 'subtitle-span-acoustic-check-request.v1':
                # Question identity needs only its defining text/clock/source fields.
                keys = ('schema_version', 'request_sha256', 'current_cue', 'proposed_cue',
                    'base_text_sha256', 'matched_start_ms', 'matched_end_ms',
                    'source_media_timeline_offset_ms', 'clip_context_binding',
                    'context_start_ms', 'context_end_ms', 'edit_span_binding',
                    'evidence_id', 'cue_indexes', 'require_complete_utterance_support')
                requests[value['request_sha256']] = {k: value[k] for k in keys if k in value}
        raw_answers = []
        for value in records:
            for key in ('raw_completion', 'completion'):
                raw = value.get(key)
                if isinstance(raw, str) and raw not in raw_answers:
                    raw_answers.append(raw)
        rows.append({
            'finding': {k: finding.get(k) for k in ('cue_index', 'repair_class', 'suspect',
                'suggestion', 'proposed_full_cue', 'content_risk', 'risk_basis', 'why',
                'base_text_sha256')},
            'reason_code': audit.get('reason_code') or audit.get('policy_branch'),
            'status': audit.get('status'), 'policy_branch': audit.get('policy_branch'),
            'argon': audit.get('argon_escalation'),
            'request_sha256': (audit.get('request') or {}).get('request_sha256'),
            'retry': audit.get('semantic_retry_progress'),
            'raw_answers': raw_answers,
            'terminal_records': [value['terminal_review'] for value in records
                if isinstance(value.get('terminal_review'), dict)],
            'diagnostics': [{k: value[k] for k in ('schema_version', 'status', 'reason_code',
                'request_sha256', 'original_request_sha256', 'prompt_sha256',
                'dispatch_outcome', 'error_type', 'decision', 'choice', 'reason',
                'detail', 'diagnostic', 'gate_source') if k in value}
                for value in records if value.get('reason_code') or value.get('diagnostic')],
            'argons': list({sha(value): value for value in records
                if value.get('task_id') and value.get('accounting')}.values()),
        })
    srt = next((v['whole_clip_current_srt'] for v in walk(review)
        if isinstance(v.get('whole_clip_current_srt'), str)
        and 'sha256:' + hashlib.sha256(v['whole_clip_current_srt'].encode()).hexdigest()
            == review['reviewed_srt_sha256']), None)
    # Preserve the old validator's real non-finding gates verbatim for its guard.
    guard = {k: v for k, v in review.items() if k not in (
        'findings', 'resolved_findings', 'unresolved_findings_disclosed', 'discovery',
        'candidate_blind_acoustic_discovery', 'candidate_pronoun_consistency_audit',
        'source_context_discovery_audit')}
    guard['findings'] = [r['finding'] for r in rows]
    guard['discovery'] = review['discovery']
    result = {'run': directory.name, 'source_sha256': hashlib.sha256(review_path.read_bytes()).hexdigest(),
        'srt': srt, 'rows': rows, 'requests': requests, 'guard_review': guard,
        'passes': authority.get('exact_final_cpa_self_heal', {}).get('passes', []),
        'review_pass_count': review.get('cpa_review_resource_pressure', {}).get('review_pass_count'),
        'producer_log': (directory / 'producer.log').read_text()}
    if runtime:
        result['_runtime_review'] = review
        result['_runtime_authority'] = authority
    return result


def finding_class(row):
    reason = row['reason_code'] or ''
    diagnostics = json.dumps(row['diagnostics'], ensure_ascii=False)
    argon = row.get('argon') or {}
    if row['status'] == 'MISSING_PROPOSAL_UNRESOLVED' or reason in {
            'CONTINUATION_VALUE_PLAN_REQUIRED', 'CONTINUATION_PLAN_ALREADY_UNRESOLVED'}:
        return '3'
    if reason == 'HISTORY_CONVERGENCE_AUDIO_GATE_INVALID' or (
            reason == 'ARGON_INTEGRATION_REJECTED' and 'DRIFT' in diagnostics):
        return '4'
    if reason in {'JUDGE_UNCERTAIN_KEEP_CURRENT', 'JUDGE_REQUESTS_MORE_AUDIO'}:
        return '5'
    if argon.get('status') in {'UNCERTAIN', 'NO_SPEECH'} or reason in {
            'ARGON_HEARD_UNCERTAIN', 'ARGON_HEARD_NO_SPEECH'}:
        return '2'
    if argon.get('status') in {'ADMISSION_PENDING', 'PENDING', 'UNKNOWN', 'FAILED_PRE_DISPATCH', 'FAILED'} or any(
            key in reason + diagnostics for key in ('ARGON_PROVIDER_SLOT_BUSY',
                'ARGON_AGY_CAPABILITY_UNSUPPORTED', 'ARGON_NOT_ON_FREE_TIER')):
        return '1'
    if argon.get('status') == 'RESPONSE_REJECTED':
        return '新类:ARGON_RESPONSE_REJECTED'
    return '新类:' + reason


def legacy_guard(run):
    """Execute the original responsible release validator on this same fixture."""
    source = subprocess.run(['git', 'show', '8ee91d718:src/autoslice/final_review_contract.py'],
        check=True, capture_output=True, text=True).stdout
    module = types.ModuleType('src.autoslice._exact_funnel_legacy')
    module.__package__ = 'src.autoslice'
    exec(compile(source, '8ee91d718/final_review_contract.py', 'exec'), module.__dict__)
    audit = run['guard_review']
    try:
        module.validate_final_review_release(audit, expected_srt_sha256=audit['reviewed_srt_sha256'])
    except module.FinalReviewContractError as exc:
        return exc.reason_code
    return 'PASS'


def replay_run(run):
    from src.autoslice.meaning_difference import classify_difference
    from src.autoslice.subtitle_acceptability_policy import load_policy
    from src.autoslice.semantic_terminal_adjudication import validate_terminal_answer
    policy = load_policy()
    runtime_routes = exercise_recorded_funnel(run) if '_runtime_review' in run else {}
    results = []
    for ordinal, row in enumerate(run['rows'], 1):
        cls = finding_class(row)
        request = run['requests'].get(row.get('request_sha256'), {})
        current = request.get('current_cue') or row['finding'].get('suspect') or ''
        proposed = request.get('proposed_cue') or row['finding'].get('proposed_full_cue') or current
        diff = classify_difference(current, proposed, glossary_terms=(), policy=policy)
        recorded_closure = False
        rejected = []
        for record in row.get('terminal_records', []):
            # A prior CURRENT/needs_audio answer to an earlier prompt cannot be
            # relabelled as the response to this new material-exhaustion prompt.
            try:
                raw = json.loads(record['raw_completion'])
                selected = validate_terminal_answer(record['request'], raw)
                if selected in {'CURRENT', 'CONTEXT_FIT'}:
                    from src.autoslice.final_review_contract import is_release_resolved_keep
                    # No scalar CURRENT shortcut: require the real saved typed keep.
                    full = record.get('saved_finding')
                    recorded_closure = bool(full and is_release_resolved_keep(full))
            except (ValueError, TypeError, KeyError, OSError) as exc:
                rejected.append(str(exc))
        argon = row.get('argon') or {}
        if cls == '1' and argon.get('status') == 'ADMISSION_PENDING' and not argon.get('job_name'):
            route = '重新申请Argon（共用等待）→仍未决且停轮则进终裁'
        elif cls == '1' and argon.get('job_name'):
            route = '取回原Argon批任务（有界GET）→仍未决且停轮则进终裁'
        elif cls == '4':
            route = '已有听音按G1/G6复用、候选重裁→仍未决且停轮则进终裁'
        else:
            route = '进终裁'
        if recorded_closure:
            route = '已有回答即可收口'
        results.append({'ordinal': ordinal, 'cue': row['finding']['cue_index'],
            'repair_class': row['finding']['repair_class'], 'reason': row['reason_code'],
            'class': cls, 'route': route, 'recorded_closure': recorded_closure,
            'needs_new_terminal': not recorded_closure, 'current': current, 'proposed': proposed,
            'difference': diff, 'terminal_records': len(row.get('terminal_records', [])),
            'raw_answers': len(row['raw_answers']), 'recorded_terminal_rejections': rejected,
            'runtime_route': runtime_routes.get(ordinal)})
    return {'run': run['run'], 'rows': results, 'counts': dict(sorted(Counter(r['class'] for r in results).items())),
        'direct': sum(r['recorded_closure'] for r in results),
        'new_terminal': sum(r['needs_new_terminal'] for r in results), 'legacy_guard': legacy_guard(run)}


def exercise_recorded_funnel(run):
    """Execute the real funnel one row at a time, with a recorded-only seam.

    No runtime root is installed, no provider adapter exists, and an absent
    matching recorded response raises before any transport. The source-only
    context is labelled as a projection; absent whole-session input is not
    invented. Original observations stay in their own immutable audit scope.
    """
    from src.autoslice.exact_final_terminal_funnel import (
        applied_pass_receipt, review_pass_receipt, terminalize_review)
    review, authority = run['_runtime_review'], run['_runtime_authority']
    current = next(value for value in walk(review)
        if value.get('whole_clip_current_srt') == run['srt'] and value.get('clip_context_binding'))
    bound = current['clip_context_binding']
    context = {'schema_version': 'offline-source-context-projection.v1',
        'candidate_id': bound['candidate_id'],
        'pieces': [{'source_media_sha256': s} for s in bound['source_media_sha256s']],
        'replay_context_scope': 'SOURCE_BINDING_AND_CURRENT_CLIP_ONLY'}
    applied = (authority.get('exact_final_cpa_self_heal') or {}).get('passes') or []
    receipts = [applied_pass_receipt(p) for p in applied]
    receipts.append(review_pass_receipt(pass_index=(receipts[-1]['pass_index'] + 1 if receipts else 1),
        srt_text=run['srt'], audit=review, repaired_text=run['srt'], repairs=[]))
    routes = {}
    original_base = os.environ.pop('AUTOSLICE_BASE', None)
    try:
        for ordinal, finding in enumerate(review['findings'], 1):
            calls = []
            recordings = run['rows'][ordinal-1].get('terminal_records') or []
            def recorded_only(prompt):
                calls.append(hashlib.sha256(prompt.encode()).hexdigest())
                for record in recordings:
                    if record.get('prompt') == prompt and isinstance(record.get('raw_completion'), str):
                        return record['raw_completion']
                raise ValueError('RECORDED_TERMINAL_REQUIRED')
            result = terminalize_review(run['srt'], {'findings': [finding],
                'validated_finding_count': 1, 'reason_codes': ['FINAL_REVIEW_UNRESOLVED_FINDINGS']},
                pass_receipts=receipts, adapters={'clip_context': context,
                    'source_media_timeline_offset_ms': current['source_media_timeline_offset_ms'],
                    'judge_llm_call': recorded_only})
            attempt = result['terminal_funnel']['attempts'][0]
            routes[ordinal] = {'terminal_prompt_reached': len(calls) == 1,
                'terminal_prompt_sha256': calls[0] if calls else None,
                'reason_code': attempt.get('reason_code'), 'new_model_calls': 0}
    finally:
        if original_base is not None:
            os.environ['AUTOSLICE_BASE'] = original_base
    return routes


LEFT_COLUMNS = {'negation_polarity': '否定与正反', 'referent': '指代对象/谁做了什么',
    'number_time': '数字、数量、时间、日期', 'proper_name': '人名、专名、作品名、梗名',
    'action': '动作本身', 'evaluation_stance': '评价与立场', 'derogatory': '贬义或冒犯他人的词',
    'object_place': '物品/地点', 'syntax_role': '句法角色/是不是词', 'persona_action': '人设相关动作'}


def render_replay(results, directories):
    lines = ['# 终审停轮离线回放', '',
        '第9步现场目录：' + '、'.join(directories) + '。只处理本次已交付原件；没有调用模型。', '',
        '每条都按统一不变量进入原终裁。旧CURRENT、规划OBSERVE、Argon OBSERVED或UNCERTAIN均不改签成新终裁。'
        '下列“需新终裁”是缺少实际新终裁原回答，不是假定内容为D；这是离线路由回放，不产生生产放行回执。', '',
        '|片|类1|类2|类3|类4|类5|新类|可直接收口|需新终裁|旧代码守卫|',
        '|---|---:|---:|---:|---:|---:|---:|---:|---:|---|']
    for run in results:
        counts = run['counts']
        lines.append('|' + '|'.join([run['run'], *(str(counts.get(str(i), 0)) for i in range(1, 6)),
            str(sum(v for k,v in counts.items() if k.startswith('新类:'))), str(run['direct']),
            str(run['new_terminal']), run['legacy_guard'] + ' BLOCK']) + '|')
    for run in results:
        lines += ['', '## ' + run['run'], '',
            '|条|cue / repair_class|原reason code|类|新路由|已录回答可收口|', '|---:|---|---|---|---|---|']
        for row in run['rows']:
            lines.append(f"|{row['ordinal']}|{row['cue']} / {row['repair_class']}|{row['reason']}|{row['class']}|{row['route']}|"
                + ('是' if row['recorded_closure'] else '否：需新终裁') + '|')
        checked = [r for r in run['rows'] if r['runtime_route']]
        if checked:
            lines += ['', f"真实新funnel离线执行：{sum(r['runtime_route']['terminal_prompt_reached'] for r in checked)}/{len(checked)}条到达原终裁prompt；"
                '只读已录回答，缺匹配原回答在transport前记RECORDED_TERMINAL_REQUIRED，新增模型调用0。']
            lines.extend(f"- 第{r['ordinal']}条前置拒绝：{r['runtime_route']['reason_code']}（不冒称已终裁）。"
                for r in checked if not r['runtime_route']['terminal_prompt_reached'])
        lines += ['', '### 仍可能涉及D的待裁差异', '',
            '以下只列定稿左列对应的竞争读法，仍须终裁判断整篇语境能否定下、写出的句子是否真的主张错误意思。'
            '繁简、同义、缩短以及单纯缺字不能直接判D；未分类字面差异不足以指认定稿左列。']
        found = False
        for row in run['rows']:
            columns = [LEFT_COLUMNS[f] for f in row['difference']['features'] if f in LEFT_COLUMNS]
            if columns:
                found = True
                lines.append(f"- 第{row['ordinal']}条 cue{row['cue']} / {row['repair_class']}："
                    f"「{row['current']}」与「{row['proposed']}」；左列形式检测：{'、'.join(columns)}。"
                    '尚无本次终裁，语境是否能定下未知；不能据此认定D。')
        if not found:
            lines.append('无已定位到左列的竞争读法；仍需终裁，不能从缺候选断言没有D风险。')
        lexical = [str(r['ordinal']) for r in run['rows'] if r['difference']['meaning_carrying']
            and not any(f in LEFT_COLUMNS for f in r['difference']['features'])]
        if lexical:
            lines.append('仅未分类词义差异（需终裁定位，不自动判D）：第' + '、'.join(lexical) + '条。')
        new = [r for r in run['rows'] if r['class'].startswith('新类:')]
        lines += ['', '### 新类', '']
        descriptions = {
            '新类:ARGON_RESPONSE_REJECTED': 'Argon返回了INVALID_JSON，原协议拒收且没有合法hearing；不同于类1的排队/未回，也不是类2的合法UNCERTAIN听音。',
            '新类:RetryNeedsPlan': 'editorial_rendering已进入编辑裁决，但仅保存RetryNeedsPlan异常类名、未保留具体继续门原因；不同于类3的缺失提案阶段K04。',
        }
        lines += ([f"- 第{r['ordinal']}条 {r['class']}：" + descriptions.get(r['class'], '不属于已有五类原因。')
            + '统一停轮终裁覆盖全部未决finding，不将坏回包/异常变成观察，也不绕过原技术/硬禁门。' for r in new]
            or ['无。'])
    lines += ['', '计数采用互斥主因：姐姐位4份Argon UNCERTAIN中，cue24 disclosure_only以最终K04归类3，类2主因3条，另1条重叠在类3。',
        '旧代码守卫读取8ee91d718原final_review_contract，并在相同小夹具的原发现/完整非发现门上执行；未把压缩证据冒充生产原件。', '']
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--extract', type=Path)
    parser.add_argument('--fixture', type=Path)
    parser.add_argument('--evidence', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.extract:
        result = project_run(args.extract)
        # Full repair receipts are not used by the private replay; retain their identity.
        result['passes'] = [{k: r[k] for k in ('pass_index', 'input_srt_sha256', 'output_srt_sha256')}
            | {'receipt_sha256': sha(r), 'repairs_sha256': sha(r['repairs'])} for r in result['passes']]
        args.fixture.parent.mkdir(parents=True, exist_ok=True)
        args.fixture.write_bytes(gzip.compress(json.dumps(result, ensure_ascii=False,
            separators=(',', ':')).encode(), mtime=0))
        print(args.fixture, args.fixture.stat().st_size, 'bytes', len(result['rows']), 'findings')
    if args.evidence:
        directories = sorted(p for p in args.evidence.iterdir() if p.is_dir())
        results = [replay_run(project_run(p, runtime=True)) for p in directories]
        args.output.write_text(render_replay(results, [p.name for p in directories]))
        args.output.with_suffix('.json').write_text(json.dumps(results, ensure_ascii=False, indent=2))
        for run in results:
            print(run['run'], run['counts'], 'direct', run['direct'], 'new_terminal', run['new_terminal'],
                'legacy', run['legacy_guard'])


if __name__ == '__main__':
    main()
