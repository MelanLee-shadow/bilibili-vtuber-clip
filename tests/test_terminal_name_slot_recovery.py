"""Versioned recovery of a name-only answer with a separate outside change."""
from copy import deepcopy
import hashlib
import json
import subprocess
import sys
import pytest

from tests.test_targeted_relisten_public_contract import rig  # noqa: F401
from tests.test_terminal_contract_roundtrip import c_reply, produce


@pytest.mark.parametrize('recorded_version', [4, 7])
def test_old_name_slot_rejection_reasks_with_current_contract_and_cold_reads(rig, tmp_path, monkeypatch, recorded_version):  # noqa: F811
    from src.autoslice import semantic_terminal_adjudication as terminal
    from src.autoslice import targeted_relisten as relisten
    from src.autoslice.editorial_subtitle import replay_terminal_adjudication
    from src.autoslice.review_evidence_store import persist_review
    from src.autoslice.final_review_contract import is_release_resolved_keep

    def bad_reply(data, refs):
        # The declared name slot does not cover the omitted final character.
        return {**c_reply(data, refs), 'name_only': True, 'name_span': [1, 2],
                'comparison_text': data['current'][:-1]}

    # An ordinary recorded producer and its unmodified, rejected raw answer.
    with monkeypatch.context() as old:
        old.setattr(terminal, 'TERMINAL_CONTRACT_VERSION', recorded_version)
        rejected, captured = produce(rig, bad_reply)
    assert rejected['reason_code'] == 'TERMINAL_NAME_SLOT_BINDING_REQUIRED'
    review = rejected['terminal_review']
    original = deepcopy(review)
    row = {**rig.finding, 'exact_release_adjudication': rejected}
    replay = replay_terminal_adjudication(rig.srt, row,
        check_request=review['request']['check_request'], clip_context=rig.context)
    assert replay['reason_code'] == 'TERMINAL_NAME_SLOT_BINDING_REQUIRED'
    assert replay['contract_retry']['kind'] == 'REASK_UNDER_CURRENT_CONTRACT'
    calls = []
    def judge(prompt):
        calls.append(prompt)
        assert '槽前和槽后必须分别与原稿逐字相同' in prompt
        data = json.loads(prompt.rsplit('\n', 1)[1])
        refs = json.loads(next(line.split('：', 1)[1] for line in prompt.splitlines()
                               if line.startswith('可引用编号')))
        return json.dumps({**c_reply(data, refs), 'name_only': False,
                           'comparison_text': data['current']}, ensure_ascii=False)
    result = relisten.close_with_existing_editorial(srt_text=rig.srt, finding=row,
        recovery_audit={'terminal_check_request': review['request']['check_request'],
                        'no_gain_proof': review['request']['audio_gain_termination']},
        prior=rejected, clip_context=rig.context, entity_verifier=rig.verifier, judge_llm_call=judge)
    assert len(calls) == 1
    assert result.get('policy_branch') == 'SUB18_CONTEXT_MEANING_EQUIVALENT_KEEP', result.get('reason_code')
    assert review == original
    link = result['superseded_contract_answer']
    assert link['original_raw_sha256'] == hashlib.sha256(review['raw_completion'].encode()).hexdigest()
    assert link['original_prompt_sha256'] == hashlib.sha256(review['prompt'].encode()).hexdigest()
    released = {**result['rebuilt_finding'], 'exact_release_adjudication': result}
    assert is_release_resolved_keep(released)
    path = tmp_path / 'review.json'
    persist_review(path, {'findings': [], 'unresolved_findings_disclosed': [released]})
    code = ('import sys; from src.autoslice.review_evidence_store import load_review; '
            'from src.autoslice.final_review_contract import is_release_resolved_keep; '
            'assert is_release_resolved_keep(load_review(sys.argv[1])["unresolved_findings_disclosed"][0])')
    subprocess.run([sys.executable, '-B', '-c', code, str(path)], check=True)
