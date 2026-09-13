"""Byte-preserving screenshot successors do not fabricate a fresh witness."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from scripts import repair_screenshot_cover as cli
from tests.cover_binding_test_support import _screenshot_polish_binding_fixture


def _fixture(tmp_path, monkeypatch, direct=False):
    fx = _screenshot_polish_binding_fixture(tmp_path, monkeypatch)
    g = json.loads(fx['generation_path'].read_bytes())
    if direct:
        from src.autoslice.cover_route_evidence import build_cover_route_decision, record_cover_route_execution
        g.update(method='screenshot_direct', model='none', image_gen_model='none',
                 attempted_models=[], model_fallback_used=False, cover_origin='SOURCE_SCREENSHOT',
                 image_generation_planned=False, image_generation_attempted=False, image_generation_used=False)
        g['screenshot_graphic_poster']['source_frame_transform']['ai_modified'] = False
        g['route_decision'] = build_cover_route_decision(
            selected_treatment='screenshot_direct', selected_rationale='same-pixel test source',
            story_contract=None, reference_authority=None, title=fx['title'], cover_text=g['cover_text'],
            decision_inputs={'cover_mode':'screenshot','subject_confident':True,'verified_stream_frame':True})
        g['route_decision']['host_identity_required'] = True
        record_cover_route_execution(g, actual_treatment='screenshot_direct', execution_status='READY',
                                    image_generation_attempted=False, image_generation_used=False)
    record = json.loads(fx['delivery_record'].read_bytes())
    record['publish_staging']['cover_generation'] = g
    record['publish_staging']['publish_json_path'] = ''
    path = tmp_path/'current.record.json'
    path.write_text(json.dumps(record, ensure_ascii=False))
    out = tmp_path/'successor'/'final.cover.png'
    for name in ['_overlay_cover_title','_compose_screenshot_poster_background',
                 '_verify_polish_face_integrity','verify_final_host_identity']:
        monkeypatch.setattr(cli, name, lambda *_a, **_k: pytest.fail('No render or provider in preservation mode'))
    return fx, record, path, out


def _invoke(monkeypatch, fx, path, out, *extra):
    monkeypatch.setattr(sys, 'argv', ['repair_screenshot_cover.py','--record',str(path),
        '--candidate-id',fx['cid'],'--out',str(out),'--preserve-existing-pixels',*extra])
    return cli.main()


@pytest.mark.parametrize('direct', [False, True])
def test_cli_preserves_exact_pixels_and_witnesses(tmp_path, monkeypatch, direct):
    from src.autoslice.cover_repair_route_lineage import validate_cover_generation_for_binding
    fx, record, path, out = _fixture(tmp_path, monkeypatch, direct)
    raw = path.read_bytes()
    g = record['publish_staging']['cover_generation']
    cover_raw = Path(g['final_cover']).read_bytes()
    assert _invoke(monkeypatch, fx, path, out) == 0
    new, _ = validate_cover_generation_for_binding(cover=out, title=fx['title'], candidate_id=fx['cid'])
    assert out.read_bytes() == cover_raw and path.read_bytes() == raw
    for key in ['polish_face_verification','final_host_identity_verification','route_decision',
                'rendered_text_pixels','art_direction','screenshot_graphic_poster']:
        assert new[key] == g[key]
    assert new['final_cover'] == str(out)
    assert new['pixel_preserving_successor']['provider_calls'] == 0
    assert new['pixel_preserving_successor']['rendered_new_pixels'] is False
    assert Path(new['pixel_preserving_successor']['record_preimage_path']).read_bytes() == raw


@pytest.mark.parametrize('drift', ['face', 'host', 'cover_hash', 'cover_bytes', 'mask_bytes',
    'font', 'frame_time', 'route', 'existing_directory', 'dangling_link', 'source_symlink',
    'override_witness', 'override_layout', 'candidate'])
def test_invalid_source_or_override_refuses_before_new_files(tmp_path, monkeypatch, drift):
    fx, record, path, out = _fixture(tmp_path, monkeypatch)
    g = record['publish_staging']['cover_generation']
    extra = []
    if drift in {'face', 'host'}:
        g['polish_face_verification' if drift == 'face' else 'final_host_identity_verification']['status'] = 'FAIL'
    elif drift == 'cover_hash':
        g['final_cover_sha256'] = 'sha256:' + '0'*64
    elif drift in {'cover_bytes', 'mask_bytes'}:
        target = Path(g['final_cover'] if drift == 'cover_bytes' else g['rendered_text_pixels']['mask_path'])
        target.write_bytes(b'damaged historical bytes')
    elif drift == 'font':
        g['rendered_text_pixels']['font_file_sha256'] = 'sha256:' + '0'*64
    elif drift == 'frame_time':
        g['screenshot_frame']['frame_ms'] += 1
    elif drift == 'route':
        g['route_decision']['execution_status'] = 'BLOCKED'
    elif drift == 'existing_directory':
        out.parent.mkdir()
        (out.parent/'owned-by-prior-run.txt').write_text('keep')
    elif drift == 'dangling_link':
        out.parent.symlink_to(tmp_path/'absent', target_is_directory=True)
    elif drift == 'source_symlink':
        original = Path(g['final_cover'])
        alias = tmp_path/'cover-alias.png'
        alias.symlink_to(original)
        g['final_cover'] = str(alias)
    elif drift == 'override_witness':
        extra = ['--host-identity-receipt', 'unapproved.json']
    elif drift == 'override_layout':
        extra = ['--identity-landmark-title-exclusion', 'unapproved.json']
    else:
        record['candidate_id'] = 'wrong-candidate'
    path.write_text(json.dumps(record, ensure_ascii=False))
    before = path.read_bytes()
    assert _invoke(monkeypatch, fx, path, out, *extra) == 2
    assert not out.exists() and path.read_bytes() == before
    if drift == 'existing_directory':
        assert (out.parent/'owned-by-prior-run.txt').read_text() == 'keep'
    elif drift != 'dangling_link':
        assert not out.parent.exists()


@pytest.mark.parametrize('drift', ['receipt_hash', 'receipt_shape', 'preimage_bytes', 'preimage_link',
    'witness', 'model', 'rendered_text', 'output_bytes', 'claimed_provider', 'claimed_render'])
def test_successor_replay_rejects_drift(tmp_path, monkeypatch, drift):
    from src.autoslice.cover_repair_route_lineage import validate_cover_generation_for_binding
    fx, _record, path, out = _fixture(tmp_path, monkeypatch)
    assert _invoke(monkeypatch, fx, path, out) == 0
    mp = out.with_suffix('.cover_generation.json')
    g = json.loads(mp.read_bytes())
    receipt = g['pixel_preserving_successor']
    preimage = Path(receipt['record_preimage_path'])
    if drift == 'receipt_hash':
        receipt['record_sha256'] = 'sha256:'+'0'*64
    elif drift == 'receipt_shape':
        receipt['unknown'] = True
    elif drift == 'preimage_bytes':
        preimage.write_bytes(b'{}')
    elif drift == 'preimage_link':
        other = tmp_path/'other-source.json'
        other.write_bytes(preimage.read_bytes())
        preimage.unlink()
        preimage.symlink_to(other)
    elif drift == 'witness':
        g['polish_face_verification']['witness']['model'] = 'new-opinion-never-called'
    elif drift == 'model':
        g['model'] = 'none'
    elif drift == 'rendered_text':
        g['rendered_lines'] = ['unapproved']
    elif drift == 'output_bytes':
        out.write_bytes(b'changed')
    elif drift == 'claimed_provider':
        receipt['provider_calls'] = 1
    else:
        receipt['rendered_new_pixels'] = True
    mp.write_text(json.dumps(g, ensure_ascii=False))
    with pytest.raises((ValueError, OSError)):
        validate_cover_generation_for_binding(cover=out, title=fx['title'], candidate_id=fx['cid'])


def test_binder_uses_successor_and_preserves_old_evidence(tmp_path, monkeypatch):
    import scripts.session_autoslice as runner
    from src.autoslice.cover_repair_route_lineage import validate_cover_generation_for_binding
    fx, record, path, _out = _fixture(tmp_path, monkeypatch)
    out = fx['generated_cover'].parent.parent/'new-owned-generation'/'final.cover.png'
    old_manifest = fx['generation_path'].read_bytes()
    old_cover = fx['generated_cover'].read_bytes()
    assert _invoke(monkeypatch, fx, path, out) == 0
    runner._bind_repaired_cover(fx['date'], fx['rec'], fx['mp4'], fx['cover'], out)
    assert runner._cover_binding_valid(fx['date'], fx['rec'], fx['mp4'], fx['cover'])
    assert fx['generation_path'].read_bytes() == old_manifest
    assert fx['generated_cover'].read_bytes() == old_cover == out.read_bytes()
    # The mutable original record is not the predecessor authority after binding.
    path.write_text('{"later":"changed independently"}')
    validate_cover_generation_for_binding(cover=out, title=fx['title'], candidate_id=fx['cid'])
    assert record['publish_staging']['cover_generation']['final_cover'] != str(out)


def test_second_preservation_does_not_overwrite_successful_namespace(tmp_path, monkeypatch):
    fx, _record, path, out = _fixture(tmp_path, monkeypatch)
    assert _invoke(monkeypatch, fx, path, out) == 0
    files = {p: p.read_bytes() for p in out.parent.iterdir()}
    assert _invoke(monkeypatch, fx, path, out) == 2
    assert files == {p: p.read_bytes() for p in out.parent.iterdir()}


def test_partial_write_stays_owned_and_does_not_touch_sources(tmp_path, monkeypatch):
    from src.autoslice import cover_pixel_preservation as preserve
    fx, record, path, out = _fixture(tmp_path, monkeypatch)
    source = path.read_bytes()
    image = Path(record['publish_staging']['cover_generation']['final_cover']).read_bytes()
    real_write = preserve._write_new
    def fail_manifest(target, payload):
        if target.name.endswith('.cover_generation.json'):
            raise OSError('simulated storage interruption')
        real_write(target, payload)
    monkeypatch.setattr(preserve, '_write_new', fail_manifest)
    assert _invoke(monkeypatch, fx, path, out) == 2
    assert out.read_bytes() == image and path.read_bytes() == source
    assert not out.with_suffix('.pixel-preservation.json').exists()
    snapshot = {p:p.read_bytes() for p in out.parent.iterdir()}
    monkeypatch.setattr(preserve, '_write_new', real_write)
    assert _invoke(monkeypatch, fx, path, out) == 2
    assert snapshot == {p:p.read_bytes() for p in out.parent.iterdir()}
