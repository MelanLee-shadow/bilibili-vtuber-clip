"""Fail closed when a registered module gains an unexercised rejection guard."""
from pathlib import Path

import pytest

from rejection_reachability_registry import CASES, MODULES
from rejection_reachability_support import emitted_code, rejection_sites
from src import autoslice as source_package

ROOT = Path(source_package.__file__).resolve().parents[2]


def test_registered_modules_have_no_unregistered_validator_or_code():
    registered = {}
    for case in CASES:
        registered.setdefault(case.function,set()).add(case.code)
    missing = []
    for name in MODULES:
        sites = rejection_sites((ROOT/'src/autoslice'/f'{name}.py').read_text())
        for function,codes in sites.items():
            key=name+'.'+function
            if key not in registered:
                missing.append((key,sorted(codes)))
            else:
                missing.extend((key,code) for code in sorted(codes-registered[key]))
    assert not missing, 'Missing real-producer / one-change witnesses: '+repr(missing)


@pytest.mark.parametrize('case',CASES,ids=lambda case:case.function+':'+case.code)
def test_real_producer_roundtrip_and_one_change_rejection(case,tmp_path,monkeypatch):
    for suffix, mutate in (('ordinary',False),('mutant',True)):
        location=tmp_path.resolve()/suffix
        location.mkdir()
        with monkeypatch.context() as patch:
            row=case.producer(location,patch)
            if mutate:
                case.mutate(row)
                if case.code == '<FALSE>':
                    assert emitted_code(lambda:case.consume(row),case.function,boolean=True) == '<FALSE>',case.description
                else:
                    assert emitted_code(lambda:case.consume(row),case.function) == case.code, case.description
            else:
                assert case.accepted(case.consume(row)), case.description


def test_ast_discovery_detects_new_function_and_new_dynamic_code():
    source='''
def old(value):
    require(value, "OLD_CODE")
def added(value):
    for name in ("prompt", "response"):
        require(value, "ARTIFACT_" + name.upper() + "_DRIFT")
def opaque_predicate(value):
    if value is None:
        return False
    return bool(value)
def local_code(value):
    for name in ("prompt", "response"):
        rejection = f"LOCAL_{name.upper()}_DRIFT"
        require(value, rejection)
'''
    assert rejection_sites(source)=={'old':{'OLD_CODE'},'added':{'ARTIFACT_PROMPT_DRIFT','ARTIFACT_RESPONSE_DRIFT'},'opaque_predicate':set(),
                                    'local_code':{'LOCAL_PROMPT_DRIFT','LOCAL_RESPONSE_DRIFT'}}


def test_rejection_must_enter_its_registered_function_even_on_exception():
    def unrelated_failure():
        raise ValueError('SAME_CODE')
    assert emitted_code(unrelated_failure,'fixture.actual_validator')=='TARGET_NOT_CALLED'
    assert emitted_code(unrelated_failure,'fixture.'+unrelated_failure.__qualname__)=='TARGET_NOT_CALLED'
