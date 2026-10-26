#!/usr/bin/env python3
"""Offline predeployment checks and bounded CPA terminal replay.

No deployment, audio acquisition or review calls. Only worker/call with the
explicit --call-cpa flag can use the network; they read production files
and writes exclusively to --state (a disposable /tmp directory on OCI3).
"""
from __future__ import annotations


import argparse
import ast
from collections import Counter
from copy import deepcopy
import hashlib
import fcntl
import faulthandler
import json
import importlib.abc
import importlib.util
import concurrent.futures
import multiprocessing
import os
import shutil
from pathlib import Path
import subprocess
import sys
import time
import datetime
import threading
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
MODULES = ('exact_final_terminal_funnel', 'semantic_terminal_adjudication', 'editorial_subtitle',
    'targeted_relisten', 'span_finding_obligations', 'final_review_contract')
SOURCE_MODULES = (*MODULES, 'span_phoneme_resolution', 'review_evidence_store',
    'editorial_edit_contract', 'response_schemas', 'audio_question_identity',
    'acoustic_witness_adjudication', 'meaning_difference', 'exact_review_parallel',
    'terminal_legacy_v1', 'span_judgment_history', 'infra_retry_policy', 'llm_client',
    'semantic_retry_progress', 'final_review_provider_budget',
    'gemini_range_context', 'independent_context_adjudication')


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(data, ensure_ascii=False, indent=2) + '\n'
    try:
        from src.autoslice.argon_escalation_storage import atomic_write
    except ImportError:
        # A counterfactual backend may predate this publisher. This is its
        # single-owner offline report, never a production review writer.
        path.write_text(raw)
    else:
        atomic_write(path, raw.encode())


_record_codec = None


def evidence_codec():
    """Storage adapter comes from this harness, even with an older backend."""
    global _record_codec
    if _record_codec is None:
        name = 'src.autoslice._gauntlet_record_codec'
        spec = importlib.util.spec_from_file_location(name, ROOT / 'src/autoslice/review_evidence_store.py')
        _record_codec = importlib.util.module_from_spec(spec)
        sys.modules[name] = _record_codec
        spec.loader.exec_module(_record_codec)
    return _record_codec


def read_record(path):
    return evidence_codec().load_review(path)


def write_record(path, value):
    from src.autoslice.review_evidence_store import persist_review
    return persist_review(path, value)


def save_case_prompt(directory, prompt):
    path = directory / 'prompt.txt'
    if path.exists() and path.read_text() != prompt:
        history = directory / 'previous' / hashlib.sha256(path.read_bytes()).hexdigest()
        history.mkdir(parents=True, exist_ok=True)
        path.replace(history / 'prompt.txt')
        answer = directory / 'answer.txt'
        if answer.exists():
            answer.replace(history / 'answer.txt')
    write_text(path, prompt)


def write_text(path, value):
    from src.autoslice.argon_escalation_storage import atomic_write
    atomic_write(path, value.encode())


def batch_terminal_prompt(prompts):
    """Losslessly factor shared materials/citations in independent prompts."""
    if len(prompts) == 1:
        return next(iter(prompts.values()))
    parts = {}
    for key, prompt in prompts.items():
        instructions, refs, data = prompt.rsplit('\n', 2)
        label, refs = refs.split('：', 1)
        parts[key] = {'instructions': instructions, 'citation_label': label,
            'citations': json.loads(refs), 'materials': json.loads(data)}
    shared = {}
    for section in ('citations', 'materials'):
        first = next(iter(parts.values()))[section]
        shared[section] = {key: value for key, value in first.items()
            if all(row[section].get(key) == value and key in row[section] for row in parts.values())}
        for row in parts.values():
            row[section] = {key: value for key, value in row[section].items() if key not in shared[section]}
    for key, row in parts.items():
        restored = restore_batch_question(shared, row)
        if restored != prompts[key]:
            raise ValueError('BATCH_TERMINAL_PROMPT_RECONSTRUCTION_DRIFT')
    return ('逐项处理独立终裁。每项的完整材料=shared.materials与该项materials合并，'
        '完整编号表=shared.citations与该项citations合并；共享部分只是相同输入的无损去重。'
        '各项遵守自己的instructions，不跨项借用非共享材料。只回JSON对象，键为请求ID，'
        '值为该项完整JSON回答，不遗漏。\n' + json.dumps({'shared': shared, 'questions': parts},
            ensure_ascii=False, separators=(',', ':')))


def restore_batch_question(shared, row):
    return row['instructions'] + '\n' + row['citation_label'] + '：' + json.dumps(
        {**shared['citations'], **row['citations']}, ensure_ascii=False, sort_keys=True,
        separators=(',', ':')) + '\n' + json.dumps({**shared['materials'], **row['materials']},
            ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def case_has_bound_answer(state, case):
    """Cold-bind a response to its actual RETURNED physical dispatch bytes."""
    from src.autoslice.llm_client import extract_json_object
    directory = Path(case['directory'])
    if not (directory / 'answer.txt').exists() or not (state / 'calls.json').exists():
        return False
    prompt = (directory / 'prompt.txt').read_text()
    try:
        answer = extract_json_object((directory / 'answer.txt').read_text())
        calls = json.loads((state / 'calls.json').read_text())
        for row in reversed(calls):
            if row.get('status') != 'RETURNED' or case['id'] not in row['ids']:
                continue
            sent = (state / f"call-{row['call']:02d}.prompt.txt").read_text()
            returned = extract_json_object((state / f"call-{row['call']:02d}.answer.txt").read_text())
            if len(row['ids']) > 1:
                batch = json.loads(sent.split('\n', 1)[1])
                sent = restore_batch_question(batch['shared'], batch['questions'][case['id']])
                returned = returned.get(case['id'])
            from src.autoslice.prompt_materials import same_prompt_materials
            if same_prompt_materials(sent, prompt) and returned == answer:
                return True
    except (OSError, ValueError, KeyError, TypeError):
        return False
    return False


def records(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from records(child)
    elif isinstance(value, list):
        for child in value:
            yield from records(child)


def source_files(evidence):
    return sorted(evidence.glob('*/*review-flags.json'))


def static_checks(args):
    state = args.state
    env = dict(os.environ)
    env.pop('PYTHONPATH', None)
    checks = []
    def run(name, command):
        p = subprocess.run(command, cwd=ROOT, env=env, text=True, capture_output=True)
        (state / (name + '.txt')).write_text(p.stdout + p.stderr)
        checks.append({'check': name, 'status': 'PASS' if p.returncode == 0 else 'FAIL', 'exit': p.returncode})
    # Reuse the actual whole-repository delayed-import scanner and its
    # no-network/no-credential fixture, rather than create a weaker scanner.
    run('6a-1-imports', [sys.executable, '-m', 'pytest', '-q', 'tests/test_no_dead_imports.py'])
    coverage = ROOT / 'tests/test_structured_schema_coverage.py'
    if coverage.exists():
        run('6a-2-schema', [sys.executable, '-m', 'pytest', '-q', str(coverage)])
    else:
        checks.append({'check': '6a-2-schema', 'status': 'NOT_MERGED',
            'detail': 'response_schemas.py / structured_output.py and their coverage test absent at pinned HEAD; no strict-schema prompts in this branch. Integration must run this same entry after merge.'})
    run('6a-3-material-index', [sys.executable, '-m', 'pytest', '-q',
        'tests/test_terminal_contract_roundtrip.py::test_material_index_covers_real_prompt_and_accepts_all_supplied_segments',
        'tests/test_terminal_contract_roundtrip.py::test_both_consumers_read_producer_index_without_another_allowlist'])
    run('6a-4-default-environment', [sys.executable, '-m', 'pytest', '-q', 'tests/test_terminal_contract_roundtrip.py'])
    run('6a-5-record-contract', [sys.executable, '-m', 'pytest', '-q', 'tests/test_terminal_rework1.py'])
    write_json(state / 'static.json', checks)
    (state / 'GAUNTLET.md').write_text('# GAUNTLET\n\n## 6a\n\n' + '\n'.join(
        '- ' + row['check'] + ': ' + row['status'] + (' — ' + row['detail'] if row.get('detail') else '')
        for row in checks) + '\n')
    return any(row['status'] == 'FAIL' for row in checks)


def original_cases(evidence, excluded=()):
    for file in source_files(evidence):
        if file.parent.name in excluded:
            continue
        payload = file.read_bytes()
        audit = evidence_codec().review_from_bytes(payload, directory=file.parent)
        source_sha = hashlib.sha256(payload).hexdigest()
        for i, row in enumerate(audit.get('findings', [])):
            old = row.get('exact_release_adjudication') or {}
            review = old.get('terminal_review') or {}
            yield {'id': file.parent.name + ':' + str(i), 'file': str(file), 'row_index': i,
                'cue_index': row['cue_index'], 'old_reason': old.get('reason_code') or old.get('policy_branch'),
                'finding': row, 'audit': audit, 'audit_sha256': source_sha, 'old_review': review}


def legacy_guard(args):
    with FullHashSnapshot():
        return _legacy_guard(args)


def _legacy_guard(args):
    from src.autoslice.semantic_terminal_adjudication import validate_terminal_answer, keep_from_terminal
    from src.autoslice.editorial_subtitle import _decision, adjudicate
    from src.autoslice.llm_client import extract_json_object
    results = []
    for case in original_cases(args.evidence, args.exclude_group):
        review = case['old_review']
        if not review:
            continue
        request = review['request']
        stage = 'validate_terminal_answer'
        try:
            answer = extract_json_object(review['raw_completion'])
            choice = validate_terminal_answer(request, answer, prompt=review['prompt'])
            start, end = answer.get('edit_start'), answer.get('edit_end')
            current = request['current_cue']
            noop = (choice == 'CONTEXT_FIT' and type(start) is int and type(end) is int
                and 0 <= start < end <= len(current)
                and isinstance(answer.get('replacement'), str)
                and current[:start] + answer['replacement'] + current[end:] == current)
            if choice == 'UNRESOLVABLE':
                # Use the actual old producer's D handler, not a fabricated
                # validator exception (D is a valid answer that blocks release).
                def call(prompt):
                    return review['raw_completion']
                call.cpa_cache_identity = request.get('model_identity')
                produced = adjudicate(request['check_request']['whole_clip_current_srt'],
                    {**case['finding'], 'exact_release_adjudication': None,
                        '_audio_gain_termination': request['audio_gain_termination'],
                        'continuation_plan': request.get('continuation_plan')},
                    check_request=request['check_request'], clip_context=request['terminal_context'],
                    entity_verifier=lambda bound: request['audio'], judge_llm_call=call, screen_read_probe=None)
                code = produced.get('reason_code') or produced.get('policy_branch') or 'PASS'
                stage = 'adjudicate D handler'
            elif choice == 'CURRENT' or noop:
                stage = 'keep_from_terminal'
                keep_from_terminal(srt_text=request['check_request']['whole_clip_current_srt'],
                    finding=case['finding'], request=request, answer=answer,
                    prompt=review['prompt'], raw=review['raw_completion'])
                code = 'PASS'
            else:
                stage = 'editorial typed mutation'
                _decision(request, answer)
                code = 'PASS'
        except Exception as exc:
            code = str(exc)
        expected = case['old_reason'] or ('PASS' if case['finding'].get('exact_release_adjudication', {}).get('repaired') else None)
        results.append({'id': case['id'], 'expected': expected, 'actual': code,
            'stage': stage, 'matches': code == expected})
    write_json(args.state / 'legacy.json', results)
    return results


def saved_inputs(case, contexts):
    review = case['old_review']
    if review:
        request = review['request']
        proof = request['audio_gain_termination']
        return (request['check_request']['whole_clip_current_srt'], request['terminal_context'],
            request['source_media_timeline_offset_ms'], proof.get('stopped_finding', case['finding']),
            request['check_request'].get('context_only_range_acoustic_observations', []))
    candidates = [r for r in records(case['finding'].get('exact_release_adjudication', {}))
        if r.get('schema_version') == 'subtitle-span-acoustic-check-request.v1']
    if not candidates:
        # A stopped editorial row can precede its first acoustic request.
        # Its audit's reviewed-SRT hash and same-clip peer request provide the
        # original whole input/context; terminal_check builds THIS row's own
        # geometry normally. No peer observation is promoted to this target.
        candidates = [r for r in records(case['audit'])
            if r.get('schema_version') == 'subtitle-span-acoustic-check-request.v1']
    if not candidates:
        raise ValueError('REPLAY_ORIGINAL_CHECK_UNAVAILABLE')
    sha = case['audit']['reviewed_srt_sha256'].removeprefix('sha256:')
    checks = [r for r in candidates if hashlib.sha256(r['whole_clip_current_srt'].encode()).hexdigest() == sha]
    if not checks:
        raise ValueError('REPLAY_CURRENT_SRT_UNAVAILABLE')
    check = checks[0]
    binding = check.get('clip_context_binding') or {}
    context = contexts.get(binding.get('clip_context_sha256')) or contexts.get(binding.get('context_sha256'))
    if context is None:
        # Full contexts are supplied by production artifacts, never fabricated
        # from a hash or an incomplete historical binding.
        raise ValueError('REPLAY_ORIGINAL_CONTEXT_UNAVAILABLE')
    return (check['whole_clip_current_srt'], context, check['source_media_timeline_offset_ms'],
        case['finding'], check.get('context_only_range_acoustic_observations', []))


def verify_material_return(check, finding, pass_receipts, *, completion=None):
    """Verify the real scheduler and terminal admission agree, without audio I/O."""
    from src.autoslice import targeted_relisten as target
    material = target.target_material_exhaustion(check)
    if material['exhausted']:
        return None
    expected = 'TARGET_MATERIAL_CHANNELS_REQUIRED:' + ','.join(material['missing_channels'])
    reasons = []
    operations = (
        lambda: (completion or target.complete_target_materials)(check=check,
            finding=deepcopy(finding), adapters={'provider_calls_allowed': False}),
        lambda: target.compile_loop_stop_no_gain(check=check, finding=finding,
            pass_receipts=pass_receipts),
    )
    for operation in operations:
        try:
            operation()
        except ValueError as exc:
            reasons.append(str(exc))
        else:
            raise ValueError('GAUNTLET_MISSING_CHANNEL_ADMITTED')
    if reasons != [expected, expected]:
        raise ValueError('GAUNTLET_MATERIAL_RETURN_CONTRACT_MISMATCH:' + repr(reasons))
    return {'status': 'EXPECTED_MATERIAL_RETURN', 'target_material': material,
        'completion_reason': reasons[0], 'terminal_admission_reason': reasons[1],
        'requested_channels': material['missing_channels'], 'judge_dispatched': False,
        'audio_owner_dispatched': False}


def prepare_cases(args):
    from src.autoslice.exact_final_terminal_funnel import terminalize_review, review_pass_receipt
    from src.autoslice.semantic_terminal_adjudication import terminal_materials
    from src.autoslice.targeted_relisten import validate_no_gain
    contexts = json.loads(args.contexts.read_text()) if args.contexts else {}
    producer_fingerprint = {name: hashlib.sha256((ROOT / 'src/autoslice' / (name + '.py')).read_bytes()).hexdigest()
        for name in SOURCE_MODULES}
    results = []
    from src.autoslice.exact_review_parallel import group_exact_review_agenda
    from src.autoslice.final_review_auditor import build_context_adjudication_request, _candidate_blind_missing_proposal_scaffold
    from itertools import groupby
    grouped = []
    for _, source_cases in groupby(original_cases(args.evidence, args.exclude_group), key=lambda c: c['file']):
        source_cases = list(source_cases)
        def question(case):
            try:
                srt, context, offset, finding, _ = saved_inputs(case, contexts)
                scaffold = finding
                if not finding.get('proposed_full_cue') and not finding.get('suggestion'):
                    scaffold = _candidate_blind_missing_proposal_scaffold(srt, finding)
                if scaffold is None:
                    return None
                return build_context_adjudication_request(srt, scaffold, clip_context=context,
                    source_media_timeline_offset_ms=offset, require_complete_utterance_support=True)
            except (ValueError, KeyError, TypeError):
                return None
        for indexes in group_exact_review_agenda(source_cases, request_for_finding=question):
            case = dict(source_cases[indexes[0]])
            case['group_cases'] = [source_cases[i] for i in indexes]
            grouped.append(case)
    for case in grouped:
        material_return = {}
        info = {key: case[key] for key in ('id', 'cue_index', 'old_reason', 'file', 'row_index')}
        info['members'] = [{key: c[key] for key in ('id', 'cue_index', 'old_reason', 'row_index')}
            for c in case['group_cases']]
        try:
            directory = args.state / 'cases' / case['id'].replace(':', '-')
            common = args.state / 'inputs' / Path(case['file']).parent.name / (case['audit_sha256'] + '.json')
            if not common.exists():
                common.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(case['file'], common)
            if hashlib.sha256(common.read_bytes()).hexdigest() != case['audit_sha256']:
                raise ValueError('REPLAY_SHARED_AUDIT_DRIFT')
            audit_ref = {'audit_file': str(common.relative_to(args.state)), 'audit_file_sha256': case['audit_sha256']}
            producer_pin = directory / 'producer-fingerprint.json'
            if (producer_pin.exists()
                    and all((directory / name).exists() for name in ('request.json', 'input.json', 'prompt.txt'))):
                from src.autoslice.semantic_terminal_adjudication import terminal_prompt
                request = read_record(directory / 'request.json')
                valid_projection = True
                try:
                    validate_no_gain(request['audio_gain_termination'], request['check_request'])
                    if json.loads(producer_pin.read_text()) != producer_fingerprint:
                        # Reuse is decided by the ordinary producer's actual
                        # output, not by a code hash or an assumed unchanged
                        # hearing set. Final cold consumption also compares the
                        # complete, freshly regenerated terminal prompt.
                        from src.autoslice.exact_final_terminal_funnel import terminal_check
                        saved = read_record(directory / 'input.json')
                        group_rows = saved.get('group_findings', [saved['finding']])
                        fresh = terminal_check(saved['srt'], group_rows[0],
                            clip_context=saved['context'], offset_ms=saved['offset'],
                            ranges=saved['ranges'], related_findings=group_rows[1:],
                            historical_findings=[*case['audit'].get('findings', []),
                                *case['audit'].get('unresolved_findings_disclosed', [])])
                        if fresh != request['check_request']:
                            raise ValueError('REPLAY_REQUEST_PRODUCER_CHANGED')
                except (ValueError, TypeError, KeyError, OSError) as exc:
                    valid_projection = False
                    info['regenerated_due_to'] = str(exc)
                if valid_projection:
                    inputs = read_record(directory / 'input.json')
                    reusable = inputs.get('audit_file_sha256') == case['audit_sha256']
                    if 'audit' in inputs:
                        reusable = inputs.pop('audit') == case['audit']
                        if reusable:
                            inputs.update(audit_ref)
                            write_record(directory / 'input.json', inputs)
                    # Only reuse the exact producer view. Actual artifacts and
                    # every consumer are reopened below during full replay.
                    if reusable:
                        # A new model projection can be rendered from the same
                        # ordinary, freshly compared check without replaying
                        # every wrapper merely to capture the identical request.
                        # The full ordinary chain below still checks exact
                        # prompt equality before consuming any returned answer.
                        rendered = terminal_prompt(request)
                        if len(rendered) > 30_000:
                            raise ValueError('TERMINAL_PROMPT_SIZE_LIMIT_EXCEEDED:' + str(len(rendered)))
                        save_case_prompt(directory, rendered)
                        write_json(producer_pin, producer_fingerprint)
                        info.update(status='PREPARED', directory=str(directory))
                        results.append(info)
                        print(info['id'], 'PREPARED_CACHED', flush=True)
                        continue
            srt, context, offset, finding, ranges = saved_inputs(case, contexts)
            group_findings = [saved_inputs(c, contexts)[3] for c in case['group_cases']]
            one = {**case['audit'], 'findings': group_findings,
                'unresolved_findings_disclosed': [], 'validated_finding_count': len(group_findings)}
            receipt = review_pass_receipt(pass_index=1, srt_text=srt, audit=one, repaired_text=srt, repairs=[])
            # Capture the actual terminal-send boundary, after its ordinary
            # request and prompt producers. No dummy judgment is allowed to
            # enter the persistent semantic recorder or masquerade as a call.
            captured = {}
            from src.autoslice.targeted_relisten import complete_target_materials
            def complete_offline(*, check, finding, adapters):
                report = verify_material_return(check, finding, [receipt],
                    completion=complete_target_materials)
                if report:
                    material_return.update(report)
                    write_record(directory / 'material-return-check.json', check)
                    write_record(directory / 'input.json', {'srt': srt, 'context': context,
                        'offset': offset, 'finding': finding, 'group_findings': group_findings,
                        **audit_ref, 'ranges': ranges, 'pass_receipts': [receipt]})
                    raise ValueError(report['completion_reason'])
                return check
            def capture_send(call, prompt, request, **kwargs):
                captured.update(prompt=prompt, request=deepcopy(request))
                return '{}'
            with patch('src.autoslice.semantic_terminal_adjudication.request_terminal_answer', capture_send), \
                    patch('src.autoslice.targeted_relisten.complete_target_materials', complete_offline):
                audit = terminalize_review(srt, one, pass_receipts=[receipt], adapters={
                    'clip_context': context, 'source_media_timeline_offset_ms': offset,
                    'prior_range_acoustic_observations': ranges,
                    'historical_findings': [*case['audit'].get('findings', []),
                        *case['audit'].get('unresolved_findings_disclosed', [])],
                    'provider_calls_allowed': True, 'judge_llm_call': lambda prompt: '{}'})
            row = audit['findings'][0]
            if row.get('terminal_funnel_error'):
                # A retained old adjudication is evidence, never the newly
                # requested send boundary after current admission failed.
                raise ValueError(row['terminal_funnel_error']['reason_code'])
            terminal = row['exact_release_adjudication']
            review = captured or terminal.get('terminal_review')
            if not review:
                raise ValueError(row.get('terminal_funnel_error', {}).get('reason_code') or terminal.get('reason_code') or 'REPLAY_REQUEST_NOT_PRODUCED')
            request = review['request']
            materials, refs = terminal_materials(request)
            assert {key for key, value in materials.items() if value is not None} <= {
                path[0] for path in refs.values()}
            for path in refs.values():
                value = materials
                for key in path:
                    value = value[key]
            write_record(directory / 'request.json', request)
            write_record(directory / 'input.json', {'srt': srt, 'context': context, 'offset': offset,
                'finding': finding, 'group_findings': group_findings,
                **audit_ref, 'ranges': ranges, 'pass_receipts': [receipt]})
            save_case_prompt(directory, review['prompt'])
            write_json(producer_pin, producer_fingerprint)
            if len(review['prompt']) > 30_000:
                raise ValueError('TERMINAL_PROMPT_SIZE_LIMIT_EXCEEDED:' + str(len(review['prompt'])))
            info.update(status='PREPARED', directory=str(directory))
        except Exception as exc:
            if material_return and str(exc) == material_return['completion_reason']:
                info.update(status='EXPECTED_MATERIAL_RETURN', reason=str(exc),
                    directory=str(directory), target_material_return=material_return)
            else:
                info.update(status='PREPARE_REJECTED', reason=str(exc))
        results.append(info)
        print(info['id'], info['status'], info.get('reason', ''), flush=True)
    write_json(args.state / 'prepared.json', results)
    return results


def call_cpa(args, cases):
    # One replay writer owns the durable real-call reservation ledger, including
    # transport failures. Never share or modify production provider_slots.
    with (args.state / 'calls.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return _call_cpa_locked(args, cases)


def _call_cpa_locked(args, cases):
    from src.autoslice.cpa_model_ladder import TOP_CPA_MODEL, cpa_call_timeout_seconds
    from src.autoslice.llm_client import runtime_cpa_command_environment, LlmConfig, _call_direct, extract_json_object, _terminal_transport_config
    calls = json.loads((args.state / 'calls.json').read_text()) if (args.state / 'calls.json').exists() else []
    # Values stay inside this process; only CPA is loaded/used. No env dump,
    # credentials in argv, model fallback, or Gemini key path.
    environment = runtime_cpa_command_environment(args.production_root)
    os.environ['TERMINAL_GAUNTLET_CPA_KEY'] = environment['CPA_API_KEY']
    config = LlmConfig(transport='direct', model=TOP_CPA_MODEL, api_base=environment['CPA_BASE_URL'],
        api_key_env='TERMINAL_GAUNTLET_CPA_KEY', api_mode='responses', reasoning_effort='medium',
        max_tokens=24000, timeout_seconds=cpa_call_timeout_seconds())
    pending = [case for case in cases if case['status'] == 'PREPARED'
        and not case_has_bound_answer(args.state, case)]
    if args.priority_group:
        pending.sort(key=lambda c: (c['id'] not in args.priority_case,
            c['id'].split(':')[0] not in args.priority_group))
    # Multiple independently rendered ordinary prompts in one inference let
    # every row be exercised while respecting the explicit physical-call cap.
    def rendered(batch):
        prompts = {case['id']: (Path(case['directory']) / 'prompt.txt').read_text() for case in batch}
        prompt = batch_terminal_prompt(prompts)
        return prompts, prompt
    batches = []
    while pending:
        batch = [pending.pop(0)]
        for candidate in list(pending):
            if len(batch) >= args.batch_size:
                break
            if len(rendered([*batch, candidate])[1]) <= 30_000:
                batch.append(candidate)
                pending.remove(candidate)
        prompts, prompt = rendered(batch)
        if len(prompt) > 30_000:
            raise ValueError('CPA_PROMPT_SIZE_LIMIT_EXCEEDED')
        batches.append((batch, prompts, prompt))
    lock = threading.Lock()

    def run(index):
        batch, prompts, prompt = batches[index]
        from src.autoslice.infra_retry_policy import terminal_retry_call, terminal_retry_scope
        from src.autoslice.cpa_runtime import bind_observation
        def dispatch():
            with lock:
                if len(calls) >= args.max_calls:
                    raise RuntimeError('CPA_REAL_CALL_LIMIT_EXHAUSTED')
                number = len(calls) + 1
                record = {'call': number, 'ids': list(prompts), 'prompt_chars': len(prompt),
                    'status': 'RESERVED_BEFORE_DISPATCH',
                    'started_utc': datetime.datetime.now(datetime.timezone.utc).isoformat()}
                calls.append(record)
                write_json(args.state / 'calls.json', calls)
                write_text(args.state / f'call-{number:02d}.prompt.txt', prompt)
            started = time.monotonic()
            from src.autoslice.response_schemas import terminal_schema, obj
            from src.autoslice.structured_output import response_contract
            schemas = {case['id']: terminal_schema(request=read_record(
                Path(case['directory']) / 'request.json')) for case in batch}
            schema = obj(schemas) if len(batch) > 1 else next(iter(schemas.values()))
            try:
                with response_contract(schema, name='terminal_gauntlet'), bind_observation(
                        args.state / 'cpa-physical-attempts.jsonl', str(number)):
                    raw = _call_direct(prompt, _terminal_transport_config(config))
                write_text(args.state / f'call-{number:02d}.answer.txt', raw)
                record['status'] = 'RETURNED'
                return raw
            except Exception as exc:
                record.update(status='OUTCOME_UNKNOWN', error_type=type(exc).__name__,
                    safe_reason=getattr(exc, 'safe_reason', None),
                    provider_diagnostics=getattr(exc, 'provider_diagnostics', {}))
                raise
            finally:
                with lock:
                    record['elapsed_seconds'] = round(time.monotonic() - started, 3)
                    write_json(args.state / 'calls.json', calls)
                print('CPA call', number, record['status'], len(batch), 'rows', flush=True)
        try:
            with terminal_retry_scope():
                raw = terminal_retry_call(dispatch)
            answers = extract_json_object(raw)
            if len(batch) == 1:
                answers = {batch[0]['id']: answers}
            for case in batch:
                if case['id'] in answers and isinstance(answers[case['id']], dict):
                    # A producer may have been regenerated while this remote
                    # request was in flight. Retain its immutable call archive,
                    # but never overwrite a newer question with that answer.
                    if (Path(case['directory']) / 'prompt.txt').read_text() != prompts[case['id']]:
                        continue
                    write_json(Path(case['directory']) / 'answer.json', answers[case['id']])
                    write_text(Path(case['directory']) / 'answer.txt', json.dumps(answers[case['id']], ensure_ascii=False))
        except Exception as exc:
            write_json(args.state / f'batch-{index:03d}.failure.json', {
                'ids': list(prompts), 'error_type': type(exc).__name__,
                'retry_reason': getattr(exc, 'terminal_retry_reason', None)})
    from src.autoslice.exact_review_parallel import execute_exact_review_groups
    execute_exact_review_groups([[i] for i in range(len(batches))], run)


def append_case_result(results, result):
    members = result.pop('members', None)
    results.append(result)
    for member in members or []:
        if member['id'] != result['id']:
            results.append({**result, **member, 'shared_question_owner': result['id']})


def unrelated_span_context(audit, check, row):
    """Classify another question's whole-clip block without changing G.

    This is a report projection, never a filtered proof or release exemption.
    Every pending origin must carry its original request and be a different
    G6 question. Missing bindings stay an ordinary failed gate.
    """
    from src.autoslice.audio_question_identity import same_audio_question
    proof = audit.get('span_obligation_review') or {}
    if proof.get('srt_sha256') != hashlib.sha256(check['whole_clip_current_srt'].encode()).hexdigest():
        return None
    if row.get('origin_span_obligation'):
        return None
    bound = {item['receipt_sha256']: item['request'] for item in records(proof)
        if item.get('receipt_sha256') and isinstance(item.get('request'), dict)}
    pending = [state for state in proof.get('states', []) if state.get('status') != 'RESOLVED']
    for state in pending:
        own = bound.get((state.get('origin') or {}).get('parent_scope_sha256'))
        if own is None or same_audio_question(own, check,
                compare_proposed=False, strict_source=True)[0]:
            return None
    return [{'obligation_id': state['obligation_id'], 'cue_index': state['cue_index']}
        for state in pending] or None


def ordinary_span_closure(case, inputs, audit):
    """Use the package owner's actual post-terminal span consumer before G."""
    from src.autoslice.span_finding_obligations import enforce_review
    source = Path(case['file'])
    authority = source.with_name(source.name.replace('review-flags.json', 'chat-authority.json'))
    if not authority.is_file():
        terminal = source.parent / 'TERMINAL.json'
        if not terminal.is_file():
            raise ValueError('REPLAY_ORIGINAL_CHAT_AUTHORITY_UNAVAILABLE')
        metadata = json.loads(terminal.read_text())
        authority = Path(metadata['output']) / (metadata['candidate_id'] + '.chat-authority.json')
    authority = read_record(authority)
    original = inputs.get('audit') or authority.get('final_review_audit') or {}
    if original and original.get('reviewed_srt_sha256') != audit.get('reviewed_srt_sha256'):
        raise ValueError('REPLAY_ORIGINAL_KEEP_SRT_DRIFT')
    from src.autoslice.final_review_contract import is_release_resolved_keep
    # The one-question replay must retain already completed production keeps,
    # just as terminalize_review does on the full package audit. They may close
    # an earlier span obligation even when another question is the target.
    closed = audit.setdefault('unresolved_findings_disclosed', [])
    known = {json.dumps(row, sort_keys=True, ensure_ascii=False) for row in closed}
    for row in original.get('unresolved_findings_disclosed') or []:
        encoded = json.dumps(row, sort_keys=True, ensure_ascii=False)
        if encoded not in known and is_release_resolved_keep(row):
            closed.append(deepcopy(row))
            known.add(encoded)
    return enforce_review(inputs['srt'], authority, audit)


def consume_cases(args, cases):
    from src.autoslice.exact_final_terminal_funnel import terminalize_review
    from src.autoslice.semantic_terminal_adjudication import validate_terminal_answer
    from src.autoslice.editorial_subtitle import validate_authority
    from src.autoslice.final_review_contract import is_release_resolved_keep, validate_final_review_release
    from src.autoslice.llm_client import extract_json_object
    from src.autoslice.span_finding_obligations import validate_release_obligations
    results = []
    for case in cases:
        result = deepcopy(case)
        steps = result['steps'] = []
        print(case['id'], 'CONSUME_START', flush=True)
        if case['status'] == 'EXPECTED_MATERIAL_RETURN':
            directory = Path(case['directory'])
            inputs = read_record(directory / 'input.json')
            check = read_record(directory / 'material-return-check.json')
            # Reopen and verify the retained target proof in the cold process;
            # no expected-result label alone can turn the gate green.
            report = verify_material_return(check, inputs['finding'], inputs['pass_receipts'])
            if not report or report != case.get('target_material_return'):
                raise ValueError('GAUNTLET_COLD_MATERIAL_RETURN_DRIFT')
            steps.extend(['complete_target_materials:EXPECTED_RETURN',
                'compile_loop_stop_no_gain:REJECT_BEFORE_TERMINAL'])
            result['target_material_return'] = report
            append_case_result(results, result)
            continue
        if case['status'] != 'PREPARED':
            append_case_result(results, result)
            continue
        directory = Path(case['directory'])
        if not case_has_bound_answer(args.state, case):
            result.update(status='ANSWER_UNAVAILABLE', reason='NO_BOUND_CPA_ANSWER_FOR_CURRENT_PRODUCER')
            append_case_result(results, result)
            continue
        raw = (directory / 'answer.txt').read_text()
        request = read_record(directory / 'request.json')
        inputs = read_record(directory / 'input.json')
        if 'audit' not in inputs:
            audit_file = args.state / inputs['audit_file']
            payload = audit_file.read_bytes()
            if hashlib.sha256(payload).hexdigest() != inputs['audit_file_sha256']:
                raise ValueError('REPLAY_SHARED_AUDIT_DRIFT')
            source_root = Path(getattr(args, 'evidence', None) or Path(case['file']).parent.parent)
            inputs['audit'] = evidence_codec().review_from_bytes(payload,
                directory=source_root / Path(case['file']).parent.name)
        stage = 'validate_terminal_answer'
        try:
            answer = extract_json_object(raw)
            choice = validate_terminal_answer(request, answer, prompt=(directory / 'prompt.txt').read_text())
            steps.append(stage + ':PASS:' + choice)
            stage = 'keep_from_terminal / editorial typed mutation'
            def replay_call(prompt):
                from src.autoslice.prompt_materials import same_prompt_materials
                if not same_prompt_materials(prompt, (directory / 'prompt.txt').read_text()):
                    raise ValueError('REPLAY_PRODUCER_PROMPT_DRIFT')
                return raw
            audit = terminalize_review(inputs['srt'], {**inputs['audit'],
                'findings': inputs.get('group_findings', [inputs['finding']]),
                'unresolved_findings_disclosed': [],
                'validated_finding_count': len(inputs.get('group_findings', [inputs['finding']]))},
                pass_receipts=inputs['pass_receipts'], adapters={'clip_context': inputs['context'],
                    'source_media_timeline_offset_ms': inputs['offset'],
                    'prior_range_acoustic_observations': inputs['ranges'],
                    'historical_findings': [*inputs['audit'].get('findings', []),
                        *inputs['audit'].get('unresolved_findings_disclosed', [])],
                    'provider_calls_allowed': True, 'judge_llm_call': replay_call})
            rows = audit.get('findings', []) + audit.get('unresolved_findings_disclosed', [])
            row = rows[0]
            terminal = row['exact_release_adjudication']
            # Keep the target row before the ordinary consumer can restore
            # another question's still-pending whole-clip span obligation.
            audit = ordinary_span_closure(case, inputs, audit)
            steps.append('ordinary_span_closure:PASS')
            from src.autoslice.review_evidence_store import persist_review, expand, record_metrics, validate_record_metrics
            packed = persist_review(directory / 'consumed-audit.json', audit)
            if expand(packed) != audit:
                raise ValueError('REPLAY_EVIDENCE_ROUNDTRIP_DRIFT')
            metrics = record_metrics(packed, prompts=[(directory / 'prompt.txt').read_text()])
            validate_record_metrics(metrics)
            result['size_metrics'] = metrics
            write_json(directory / 'size-metrics.json', metrics)
            if terminal.get('repaired'):
                authority = terminal['editorial_authority']
                validate_authority(authority, srt_text=inputs['srt'], clip_context=inputs['context'])
                steps.append(stage + ':PASS:TYPED_MUTATION')
                keep = is_release_resolved_keep(row)
            else:
                keep = is_release_resolved_keep(row)
                if not keep:
                    raise ValueError(terminal.get('reason_code') or row.get('terminal_funnel_error', {}).get('reason_code') or 'KEEP_UNRESOLVED')
                steps.append(stage + ':PASS:KEEP')
            stage = 'is_release_resolved_keep'
            steps.append(stage + ':' + str(keep))
            stage = 'span_finding_obligations'
            span_error = None
            try:
                validate_release_obligations(packed)
                steps.append(stage + ':PASS')
            except ValueError as exc:
                span_error = str(exc)
                steps.append(stage + ':REJECT:' + span_error)
            stage = 'validate_final_review_release'
            try:
                validate_final_review_release(packed, expected_srt_sha256=inputs['audit']['reviewed_srt_sha256'])
                steps.append(stage + ':PASS')
                result.update(status='SPAN_RELEASE_REJECTED' if span_error else 'RELEASE_PASS',
                    reason=span_error or 'PASS')
            except ValueError as exc:
                steps.append(stage + ':REJECT:' + str(exc))
                # Release codes come from the shared finding lifecycle: an
                # unapplied typed edit reports EDIT_REQUIRED and open span
                # obligations report REVIEW_PENDING (the legacy single code is
                # still read for historical receipts).
                from src.autoslice.finding_lifecycle import EDIT_REQUIRED, LEGACY_UNRESOLVED, REVIEW_PENDING
                unapplied = (terminal.get('repaired') and str(exc) in {EDIT_REQUIRED, LEGACY_UNRESOLVED}
                    and span_error in (None, REVIEW_PENDING, LEGACY_UNRESOLVED))
                context_pending = (unrelated_span_context(audit, request['check_request'], row)
                    if keep and str(exc) in {REVIEW_PENDING, LEGACY_UNRESOLVED}
                    and span_error in {REVIEW_PENDING, LEGACY_UNRESOLVED} else None)
                result.update(status=('TYPED_MUTATION_REQUIRES_APPLY_REVIEW' if unapplied else
                    'QUESTION_KEEP_CLIP_OBLIGATIONS_PENDING' if context_pending else 'RELEASE_REJECTED'),
                    reason=str(exc), span_reason=span_error)
                if context_pending:
                    result.update(clip_release_gate='BLOCK', unrelated_pending_obligations=context_pending)
        except Exception as exc:
            result.update(status='REJECTED', reason=str(exc), rejected_at=stage)
            steps.append(stage + ':REJECT:' + str(exc))
        append_case_result(results, result)
        write_json(getattr(args, 'progress_output', args.state / 'replay-progress.json'), results)
        print(case['id'], result['status'], result.get('reason'), flush=True)
    write_json(getattr(args, 'replay_output', args.state / 'replay.json'), results)
    return results


def coverage_report(args, counts):
    supplemental = json.loads(args.test_counts.read_text()) if args.test_counts else {}
    missing = []
    for module in MODULES:
        path = ROOT / 'src/autoslice' / (module + '.py')
        tree = ast.parse(path.read_text())
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        for fn in (node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))):
            if module == 'targeted_relisten' and fn.name not in {
                'protect_clear_audio', 'protect_clear_transcript', '_clear_audio_units',
                '_clear_audio_constraint_units', '_clear_audio_constraint_projection', '_widest_cue_block',
                '_tonal_han_units', '_clear_runs_match',
                'terminal_clear_audio_catalogue', 'sub18_meaning_equivalent_keep',
                'close_with_existing_editorial', 'compile_loop_stop_no_gain', 'validate_no_gain',
                'target_material_exhaustion', 'require_target_material_exhaustion', 'complete_target_materials'}:
                continue
            for branch in (node for node in ast.walk(fn) if isinstance(node, (ast.If, ast.For, ast.While, ast.Try))):
                owner = parents.get(branch)
                while owner is not None and not isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    owner = parents.get(owner)
                if owner is not fn:
                    continue
                blocks = [(name, getattr(branch, name, [])) for name in ('body', 'orelse', 'finalbody')]
                if isinstance(branch, ast.Try):
                    blocks.extend(('except:' + (ast.unparse(handler.type) if handler.type else '*'), handler.body)
                        for handler in branch.handlers)
                for name, block in blocks:
                    if block and not counts.get((str(path), block[0].lineno), 0):
                        line = block[0].lineno
                        tested = supplemental.get(f'{path.name}:{line}', 0)
                        entered = any(counts.get((str(path), n), 0) or supplemental.get(f'{path.name}:{n}', 0)
                            for n in range(fn.body[0].lineno, fn.end_lineno + 1))
                        if tested:
                            disposition = '补测已执行此分支；见 test-line-counts.json / test-coverage.txt'
                        elif not entered:
                            disposition = '本次停轮终裁调用链未进入的其他入口/兼容函数；未宣称覆盖该独立入口'
                        elif isinstance(block[0], ast.Raise) or (isinstance(block[0], ast.Return)
                                and isinstance(block[0].value, ast.Constant) and block[0].value.value in (False, None)):
                            disposition = '保留的拒绝/无结果分支；保持原门或不适用返回，函数真实生产方正向与代表性负向控制见测试；未计作本批覆盖'
                        else:
                            disposition = '冻结的T1–T12材料/回执种类未选择此条件；保留原处理，未将该分支计作已覆盖'
                        missing.append({'module': module, 'function': fn.name, 'line': block[0].lineno,
                            'branch': name, 'source': ast.unparse(branch).splitlines()[0], 'disposition': disposition})
    write_json(args.state / 'unexecuted-branches.json', missing)
    write_json(args.state / 'line-counts.json', {f'{Path(path).name}:{line}': count
        for (path, line), count in counts.items() if Path(path).stem in MODULES})
    return missing


def annotate_report(args):
    rows = json.loads((args.state / 'line-counts.json').read_text())
    counts = {(str(ROOT / 'src/autoslice' / key.rsplit(':', 1)[0]), int(key.rsplit(':', 1)[1])): value
        for key, value in rows.items()}
    missing = coverage_report(args, counts)
    prefix = (args.state / 'GAUNTLET-runtime.md').read_text().split('## 6c', 1)[0]
    text = ['## 6c 未执行分支（6b 行覆盖，逐项处置）', '',
        '补测覆盖以独立离线真实生产请求协议测试为准；不把补测冒称 6b 的真实调用覆盖。', '']
    text += [f'- {r["module"]}.{r["function"]}:{r["line"]} {r["branch"]} `{r["source"]}` — {r["disposition"]}' for r in missing]
    (args.state / 'GAUNTLET-runtime.md').write_text(prefix + '\n'.join(text) + '\n')


class ReleaseLineTrace(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    """Offline statement/branch line coverage for the requested source files.

    The loader inserts a counter immediately BEFORE each original statement,
    retaining its filename, line, docstring and future imports. Source files,
    prompts and replies remain unchanged. Unlike sys.settrace it adds no
    callback to millions of unrelated deepcopy calls in archived audit trees.
    Counts are statement entries (coverage), not Python line-event frequency.
    """
    def __init__(self):
        self.counts = Counter()
        self.paths = {'src.autoslice.' + name: ROOT / 'src/autoslice' / (name + '.py') for name in MODULES}
        self.active = False
        if any(name in sys.modules for name in self.paths):
            raise RuntimeError('GAUNTLET_TRACE_MUST_PRECEDE_RELEASE_MODULE_IMPORTS')
        sys.meta_path.insert(0, self)

    def hit(self, filename, line):
        if self.active:
            self.counts[(filename, line)] += 1

    def find_spec(self, fullname, path=None, target=None):
        if fullname in self.paths:
            return importlib.util.spec_from_file_location(fullname, self.paths[fullname], loader=self)
        return None

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        path = self.paths[module.__name__]
        source = path.read_text()
        symbol = '_terminal_gauntlet_statement_hit'
        if symbol in source:
            raise RuntimeError('GAUNTLET_TRACE_SYMBOL_COLLISION')
        class Instrument(ast.NodeTransformer):
            def generic_visit(self, node):
                super().generic_visit(node)
                for field, value in ast.iter_fields(node):
                    if not isinstance(value, list):
                        continue
                    replaced = []
                    for item in value:
                        docstring = (isinstance(item, ast.Expr) and isinstance(item.value, ast.Constant)
                            and isinstance(item.value.value, str))
                        future = isinstance(item, ast.ImportFrom) and item.module == '__future__'
                        if isinstance(item, ast.stmt) and not docstring and not future:
                            hit = ast.Expr(value=ast.Call(func=ast.Name(id=symbol, ctx=ast.Load()),
                                args=[ast.Constant(str(path)), ast.Constant(item.lineno)], keywords=[]))
                            replaced.append(ast.copy_location(hit, item))
                        replaced.append(item)
                    setattr(node, field, replaced)
                return node
        tree = ast.fix_missing_locations(Instrument().visit(ast.parse(source, filename=str(path))))
        module.__dict__[symbol] = self.hit
        exec(compile(tree, str(path), 'exec'), module.__dict__)

    def runfunc(self, function, *args):
        self.active = True
        try:
            return function(*args)
        finally:
            self.active = False
            sys.meta_path.remove(self)


_replay_trace = None


class FullHashSnapshot:
    """Replay-only immutable-input snapshot; every digest is rehashed at exit.

    Only repeated file I/O is cached. All production validators and their
    source-read receipts still run. An inode/stat change invalidates the row,
    including same-size changes whose mtime was restored (ctime differs).
    """
    STAT = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns',
            'st_mode', 'st_uid', 'st_gid', 'st_nlink')

    def __init__(self):
        self.files = {}
        self.hits = 0

    def stamp(self, path):
        with path.open('rb') as stream:
            opened = tuple(getattr(os.fstat(stream.fileno()), key) for key in self.STAT)
        current = tuple(getattr(path.stat(), key) for key in self.STAT)
        if path.is_symlink() or opened != current:
            raise ValueError('GAUNTLET_INPUT_CHANGED_DURING_REPLAY')
        return current

    def get(self, path, original):
        path = Path(path).absolute()
        before = self.stamp(path)
        key = str(path)
        if key in self.files:
            saved, value = self.files[key]
            if before != saved:
                raise ValueError('GAUNTLET_INPUT_CHANGED_DURING_REPLAY')
            self.hits += 1
            return value
        value = original()
        if self.stamp(path) != before:
            raise ValueError('GAUNTLET_INPUT_CHANGED_DURING_REPLAY')
        if value is not None:
            self.files[key] = (before, value)
        return value

    def __enter__(self):
        from src.autoslice import independent_context_adjudication as context
        from src.autoslice import native_audio_budget_receipt as native
        from src.autoslice.media_copy_provenance import record_source_read
        self.context, self.native = context, native
        self.original_context, self.original_native = context._file_sha256, native._source_sha256_stable
        def context_hash(path):
            if not isinstance(path, str) or not path:
                return self.original_context(path)
            if Path(path).is_symlink() or not Path(path).is_file():
                return self.original_context(path)
            value = self.get(path, lambda: self.original_context(path))
            if value is not None:
                record_source_read(Path(path), value)
            return value
        def native_hash(path, *, label):
            native._no_links(path)
            return self.get(path, lambda: self.original_native(path, label=label))
        context._file_sha256, native._source_sha256_stable = context_hash, native_hash
        return self

    def __exit__(self, *exception):
        self.context._file_sha256, self.native._source_sha256_stable = self.original_context, self.original_native
        # Full bytes, not only stat: inputs must remain the same until this
        # row's entire release chain has finished. No stale cache can pass.
        for name, (stamp, expected) in self.files.items():
            path = Path(name)
            if self.stamp(path) != stamp:
                raise ValueError('GAUNTLET_INPUT_CHANGED_DURING_REPLAY')
            digest = hashlib.sha256()
            with path.open('rb') as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b''):
                    digest.update(block)
            if self.stamp(path) != stamp or digest.hexdigest() != expected:
                raise ValueError('GAUNTLET_INPUT_CHANGED_DURING_REPLAY')


def consume_one(payload):
    """A forked offline consumer owns only its case directory; no model calls."""
    args, case = payload
    directory = Path(case.get('directory', args.state / 'cases' / case['id'].replace(':', '-')))
    args.progress_output = args.replay_output = directory / 'chain-result.json'
    _replay_trace.counts.clear()
    _replay_trace.active = True
    try:
        with FullHashSnapshot() as snapshot:
            result = consume_cases(args, [case])
        write_json(directory / 'hash-snapshot.json', {'files_fully_hashed_and_rehashed': len(snapshot.files),
            'redundant_reads_avoided': snapshot.hits, 'unchanged_at_end': True})
        return result, dict(_replay_trace.counts)
    finally:
        _replay_trace.active = False


def replay_exit_code(results):
    """Protocol failures (including record limits) must fail the CLI gate.

    Valid D decisions and unapplied edits remain content blocks, never
    publication permission. They are not malformed terminal protocols.
    """
    return int(not results or any(not (
        row['status'] in {'RELEASE_PASS', 'TYPED_MUTATION_REQUIRES_APPLY_REVIEW',
            'QUESTION_KEEP_CLIP_OBLIGATIONS_PENDING'}
        or (row['status'] == 'REJECTED' and row.get('reason') == 'TERMINAL_UNRESOLVABLE')
        or (row['status'] == 'RELEASE_REJECTED' and finding_release_block(row.get('reason')))
        or (row['status'] == 'EXPECTED_MATERIAL_RETURN'
            and valid_material_return(row))
    ) for row in results))


def finding_release_block(reason):
    """A finding-level release block (any lifecycle finding code), not a protocol defect."""
    from src.autoslice.finding_lifecycle import is_finding_failure
    return is_finding_failure(reason)


def valid_material_return(row):
    report = row.get('target_material_return') or {}
    material = report.get('target_material') or {}
    missing = material.get('missing_channels')
    expected = 'TARGET_MATERIAL_CHANNELS_REQUIRED:' + ','.join(missing or [])
    required, completed = material.get('required_channels') or [], material.get('completed_channels') or {}
    return bool(missing and material.get('exhausted') is False
        and missing == [channel for channel in required if not completed.get(channel)]
        and report.get('requested_channels') == missing
        and report.get('completion_reason') == report.get('terminal_admission_reason') == expected
        and report.get('judge_dispatched') is False and report.get('audio_owner_dispatched') is False)


def preparation_fingerprint(args):
    fingerprint = {name: hashlib.sha256((ROOT / 'src/autoslice' / (name + '.py')).read_bytes()).hexdigest()
        for name in SOURCE_MODULES}
    fingerprint['evidence'] = {str(path.relative_to(args.evidence)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in source_files(args.evidence) if path.parent.name not in args.exclude_group}
    return fingerprint


def worker(args):
    global _replay_trace
    tracer = ReleaseLineTrace()
    fingerprint = preparation_fingerprint(args)
    manifest = args.state / 'preparation-code.json'
    prepared = args.state / 'prepared.json'
    if manifest.exists() and prepared.exists() and json.loads(manifest.read_text()) == fingerprint:
        cases = json.loads(prepared.read_text())
        # A reviewed STATE can be copied into a new disposable /tmp run.
        for case in cases:
            if case.get('directory'):
                case['directory'] = str(args.state / 'cases' / case['id'].replace(':', '-'))
    else:
        with FullHashSnapshot():
            cases = prepare_cases(args)
        write_json(manifest, fingerprint)
    if args.call_cpa:
        call_cpa(args, cases)
    if args.jobs == 1:
        results = tracer.runfunc(consume_cases, args, cases)
    else:
        # fork retains this loader's exact code/counter bindings; each child
        # gets its own Counter and sole write ownership of one case directory.
        # All CPA dispatch/reservations happened in the parent before forking.
        _replay_trace = tracer
        completed = {}
        with concurrent.futures.ProcessPoolExecutor(max_workers=args.jobs,
                mp_context=multiprocessing.get_context('fork')) as pool:
            pending = [pool.submit(consume_one, (args, case)) for case in cases]
            for future in concurrent.futures.as_completed(pending):
                rows, counts = future.result()
                completed[rows[0]['id']] = rows
                tracer.counts.update(counts)
                write_json(args.state / 'replay-progress.json', [row for c in cases
                    if c['id'] in completed for row in completed[c['id']]])
        sys.meta_path.remove(tracer)
        results = [row for case in cases for row in completed[case['id']]]
        write_json(args.state / 'replay.json', results)
    branches = coverage_report(args, tracer.counts)
    calls = json.loads((args.state / 'calls.json').read_text()) if (args.state / 'calls.json').exists() else []
    lines = ['# REPLAY', '', f'CPA 真实终裁调用：{len(calls)} / {args.max_calls}（批内每条独立请求、逐条原答落盘）。', '',
        '| 片/记录 | cue | 原拒收码 | 新结果 | 全链步骤 |', '|---|---:|---|---|---|']
    for result in results:
        lines.append('| ' + result['id'] + ' | ' + str(result['cue_index']) + ' | ' + str(result['old_reason'])
            + ' | ' + result['status'] + ': ' + result.get('reason', '') + ' | ' + '<br>'.join(result['steps']) + ' |')
    lines += ['', '## 按片、原拒收码汇总', '']
    for key, count in sorted(Counter((r['id'].split(':')[0], str(r['old_reason']), r['status']) for r in results).items()):
        lines.append(f'- {key}: {count}')
    (args.state / 'REPLAY.md').write_text('\n'.join(lines) + '\n')
    lines = ['## 6b', '', f'CPA 调用 {len(calls)}；全部 {len(results)} 行：' + str(dict(Counter(r['status'] for r in results))),
        '', '逐条链路见 REPLAY.md；真实 prompt/回答、生产请求和冷消费审计见 cases/。', '', '## 6c 未执行分支', '']
    lines += [f'- {r["module"]}.{r["function"]}:{r["line"]} {r["branch"]} `{r["source"]}`' for r in branches]
    (args.state / 'GAUNTLET-runtime.md').write_text('\n'.join(lines) + '\n')
    return replay_exit_code(results)


def isolate_replay_writes(args):
    """Retain source receipts, clone only the target recorder's write location.

    Audio/source validators still open their original paths. The actual
    recorder runs on a byte-for-byte copy of its history under this disposable
    run, so preparing a new prompt never writes a production cache entry.
    A process audit hook also rejects every production filesystem mutation.
    """
    from src.autoslice import semantic_retry_progress as progress
    original = progress.SemanticAttemptRecorder
    clone_lock = threading.Lock()
    class ReplayRecorder(original):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            if self.directory is not None:
                source = self.directory
                target = args.state / 'recorder-copy' / self.issue
                with clone_lock:
                    if not target.exists():
                        if source.is_dir():
                            shutil.copytree(source, target)
                        else:
                            target.mkdir(parents=True)
                self.directory = target
    progress.SemanticAttemptRecorder = ReplayRecorder
    production = args.production_root.absolute()
    mutation_events = {'os.remove', 'os.rmdir', 'os.mkdir', 'os.rename', 'os.link',
        'os.symlink', 'os.chmod', 'os.chown', 'os.truncate', 'os.utime'}
    def production_path(value):
        if not isinstance(value, (str, bytes, os.PathLike)):
            return False
        try:
            return Path(os.fsdecode(value)).absolute().is_relative_to(production)
        except (ValueError, TypeError):
            return False
    def guard(event, values):
        if event == 'open':
            path, mode, flags = values
            writable = (isinstance(mode, str) and any(c in mode for c in 'wax+')) or (
                isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
            if writable and production_path(path):
                raise PermissionError('GAUNTLET_PRODUCTION_WRITE_FORBIDDEN')
        elif event in mutation_events and any(production_path(v) for v in values[:2]):
            raise PermissionError('GAUNTLET_PRODUCTION_WRITE_FORBIDDEN')
    sys.addaudithook(guard)


def continuation_phase(args):
    """Cold-continuation authority preflight of every in-flight slice (only earlier).

    Each slice runs this code root's ordinary producer entry in the isolated
    sandbox (private mount + network namespace, disposable overlay, masked
    credentials) up to its first provider boundary. A slice that does not
    reach it fails the phase; its sub-check table stays in --state.
    """
    if args.slices is None:
        raise SystemExit('--slices required for the continuation phase')
    work = args.state / 'continuation-sandbox'
    if work.exists():
        raise SystemExit('continuation phase needs a fresh --state directory')
    work.mkdir(parents=True)
    p = subprocess.run(['sudo', '-n', 'unshare', '-m', '-n', '--propagation', 'private', 'bash',
        str(args.repo / 'scripts/continuation_authority_preflight_sandbox.sh'), str(work), args.snapshot,
        str(args.repo), str(args.repo / 'scripts/continuation_authority_preflight.py'), str(args.slices)],
        text=True, capture_output=True)
    (args.state / 'continuation.log').write_text(p.stdout + p.stderr)
    rows = [json.loads(path.read_text()) for path in sorted((work / 'reports').glob('*.json'))]
    write_json(args.state / 'continuation.json', [
        {key: row.get(key) for key in ('candidate_id', 'outcome', 'reason_code', 'elapsed_s', 'rchar_bytes')}
        for row in rows])
    expected = sum(1 for line in args.slices.read_text().splitlines() if line.strip())
    return int(p.returncode != 0 or len(rows) != expected
               or any(not str(row.get('outcome', '')).startswith('REACHED_') for row in rows))


def whole_path_phase(args):
    """Whole-path offline observation of every in-flight slice (plan A stage one).

    Same isolation as the continuation phase (the identical sandbox script, its
    ``--exec`` view), but each slice runs past its first provider boundary:
    cold continuation, boundary, finalization, packaging and delivery
    preparation, with grouped sub-checks collected instead of stopping and no
    provider call (src/autoslice/whole_path_observation.py).  The phase fails
    only when the observation itself could not run; the per-slice block lists
    in --state are its product, not a verdict.
    """
    if args.slices is None:
        raise SystemExit('--slices required for the whole-path phase')
    work = args.state / 'whole-path-sandbox'
    if work.exists():
        raise SystemExit('whole-path phase needs a fresh --state directory')
    work.mkdir(parents=True)
    command = ['sudo', '-n', 'unshare', '-m', '-n', '--propagation', 'private', 'bash',
        str(args.repo / 'scripts/continuation_authority_preflight_sandbox.sh'), str(work), args.snapshot,
        str(args.repo), str(args.repo / 'src/autoslice/whole_path_observation.py'), '--exec',
        '--slices', str(args.slices.absolute()), '--report-dir', str(work / 'reports'),
        '--time-budget-s', str(args.time_budget_s)]
    if args.exchange_window:
        command += ['--exchange-window', args.exchange_window]
    p = subprocess.run(command, text=True, capture_output=True)
    (args.state / 'whole-path.log').write_text(p.stdout + p.stderr)
    from src.autoslice.whole_path_observation import render_markdown, summary_row
    rows = [json.loads(path.read_text()) for path in sorted((work / 'reports').glob('*.json'))]
    write_json(args.state / 'whole-path.json', [summary_row(row) for row in rows])
    (args.state / 'WHOLE-PATH.md').write_text(render_markdown(rows))
    expected = sum(1 for line in args.slices.read_text().splitlines() if line.strip())
    return int(p.returncode != 0 or len(rows) != expected
               or any(str(row.get('outcome', '')).startswith('REFUSED') for row in rows))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--phase', choices=('static', 'legacy', 'prepare', 'worker', 'call', 'report', 'continuation',
                                            'whole-path'), default='static')
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--evidence', type=Path)
    parser.add_argument('--repo', type=Path, default=ROOT)
    parser.add_argument('--contexts', type=Path)
    parser.add_argument('--test-counts', type=Path, help='Supplemental offline protocol-test line counts for branch dispositions')
    parser.add_argument('--diagnose-after', type=int, default=0, help='Dump a stack (without locals) after N seconds, for slow artifact replay')
    parser.add_argument('--production-root', type=Path, default=Path('/opt/bilive/autoslice'))
    parser.add_argument('--call-cpa', action='store_true')
    parser.add_argument('--batch-size', type=int, default=3)
    parser.add_argument('--jobs', type=int, choices=(1, 2), default=1,
        help='Offline replay consumers only; CPA dispatch uses the exact-review scheduler and a shared real-call ledger')
    parser.add_argument('--max-calls', type=int, default=30)
    parser.add_argument('--exclude-group', action='append', default=[],
        help='Explicitly exclude reference/non-stopped snapshots, recording scope in the report.')
    parser.add_argument('--priority-group', action='append', default=[])
    parser.add_argument('--priority-case', action='append', default=[])
    parser.add_argument('--slices', type=Path,
        help='continuation/whole-path phase: lines "<spec path> <existing_srt|aggregate_asr>" of every in-flight slice')
    parser.add_argument('--snapshot', default='-',
        help='continuation/whole-path phase: snapshot root overlaid on its live roots, or - for the live state')
    parser.add_argument('--exchange-window', default='',
        help='whole-path phase: <iso>..<iso> start window of the observed production run; only its '
             'archived COMPLETED text exchanges are replayed (empty: none)')
    parser.add_argument('--time-budget-s', type=int, default=0,
        help='whole-path phase: per-slice wall-clock budget (0: the observation default)')
    args = parser.parse_args()
    if not 1 <= args.max_calls <= 120 or args.batch_size < 1:
        parser.error('--max-calls must be 1..120 and --batch-size must be positive')
    args.state.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(args.repo))
    if args.diagnose_after:
        faulthandler.dump_traceback_later(args.diagnose_after, repeat=True)
    if args.phase == 'static':
        return int(static_checks(args))
    if args.phase == 'continuation':
        return continuation_phase(args)
    if args.phase == 'whole-path':
        return whole_path_phase(args)
    if args.phase == 'report':
        annotate_report(args)
        return 0
    if not args.evidence:
        parser.error('--evidence required for replay')
    # All replay phases reopen original recorder-backed receipts under the
    # same declared base. A missing base is a replay configuration error.
    os.environ['AUTOSLICE_BASE'] = str(args.production_root)
    if args.production_root.is_dir():
        isolate_replay_writes(args)
    if args.phase == 'legacy':
        results = legacy_guard(args)
        return int(not results or any(not row['matches'] for row in results))
    elif args.phase == 'prepare':
        fingerprint = preparation_fingerprint(args)
        with FullHashSnapshot():
            results = prepare_cases(args)
        if preparation_fingerprint(args) != fingerprint:
            raise ValueError('GAUNTLET_PREPARATION_SOURCE_OR_EVIDENCE_DRIFT')
        write_json(args.state / 'preparation-code.json', fingerprint)
        return int(not results or any(row['status'] != 'PREPARED'
            and not (row['status'] == 'EXPECTED_MATERIAL_RETURN' and valid_material_return(row))
            for row in results))
    elif args.phase == 'call':
        if not args.call_cpa:
            parser.error('call requires explicit --call-cpa')
        cases = json.loads((args.state / 'prepared.json').read_text())
        call_cpa(args, cases)
    else:
        return worker(args)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
