import copy
import json
from pathlib import Path
import pytest
from src.autoslice.producer_source_truth_checkpoint import freeze_ledger, rebase_pending_previews
from src.autoslice.source_subtitle_truth import (
    apply_source_subtitle_truth,
    build_source_truth_preview_receipt,
)

SRT = "1\n00:00:00,000 --> 00:00:01,000\n旧文字\n\n2\n00:00:02,000 --> 00:00:03,000\n继续说话\n"


def fixture(tmp_path):
    old = tmp_path / "old.json"
    new = tmp_path / "new.json"
    old.write_text(json.dumps({"schema_version": "source-subtitle-truth-ledger.v1", "entries": []}))
    new.write_text(
        json.dumps(
            {
                "schema_version": "source-subtitle-truth-ledger.v1",
                "entries": [],
                "note": "unrelated edit",
            }
        )
    )
    spec = {
        "pieces": [
            {
                "remote_media": "source.flv",
                "start_ms": 0,
                "end_ms": 4000,
                "source_media_sha256": "sha256:" + "a" * 64,
            }
        ]
    }
    _, formal = apply_source_subtitle_truth(SRT, spec=spec, durations=[4000], ledger_path=old)
    previews = {}
    for stage, path in [("pre_entity_arbitration", old), ("pre_correction_review", new)]:
        _, audit = apply_source_subtitle_truth(SRT, spec=spec, durations=[4000], ledger_path=path)
        previews[stage] = build_source_truth_preview_receipt(
            input_srt_text=SRT, source_truth_audit=audit, stage=stage
        )
    a = {"source_truth_preview_receipts": previews, "source_subtitle_truth_audit": formal}
    out = tmp_path / "out"
    frozen = freeze_ledger(new, out)
    kw = dict(
        spec=spec,
        durations=[4000],
        ledger_path=frozen,
        origin_path=new,
        clip_context={"whole_clip_draft_srt": SRT},
        name_reference=SRT,
        current_text=SRT.replace("旧文字", "修正后文字"),
        out_root=out,
    )
    return (a, kw, old, new)


def test_pending_phase_replays_two_real_ledgers_and_keeps_old_preview(tmp_path):
    a, kw, old, new = fixture(tmp_path)
    before = copy.deepcopy(a)
    rebase_pending_previews(a, **kw)
    previews = a["source_truth_preview_receipts"]
    assert (
        previews["pre_entity_arbitration"]["ledger_sha256"]
        == previews["pre_correction_review"]["ledger_sha256"]
    )
    assert all(
        (
            h["preview"] == before["source_truth_preview_receipts"][h["stage"]]
            for h in a["source_truth_preview_history"]
        )
    )
    assert all(
        (Path(h["input"]["path"]).read_text() == SRT for h in a["source_truth_preview_history"])
    )
    history = copy.deepcopy(a["source_truth_preview_history"])
    rebase_pending_previews(a, **kw)
    assert a["source_truth_preview_history"] == history


@pytest.mark.parametrize("damage", ["preview", "text", "old_ledger", "new_relevant", "frozen"])
def test_pending_truth_rejects_changed_protection_or_unreplayable_history(tmp_path, damage):
    a, kw, old, new = fixture(tmp_path)
    if damage == "preview":
        a["source_truth_preview_receipts"]["pre_entity_arbitration"]["protected_cue_indexes"] = [1]
    elif damage == "text":
        kw.update(
            clip_context={"whole_clip_draft_srt": SRT + "tamper"}, name_reference=SRT + "tamper"
        )
    elif damage == "old_ledger":
        old.write_text("{}")
    elif damage == "new_relevant":
        doc = {
            "schema_version": "source-subtitle-truth-ledger.v1",
            "entries": [
                {
                    "truth_id": "actual-truth",
                    "knowledge_type": "SOURCE_INTERVAL_TRUTH",
                    "recording_basename": "source.flv",
                    "source_media_sha256": "sha256:" + "a" * 64,
                    "source_start_ms": 0,
                    "source_end_ms": 1000,
                    "action": "drop_cue",
                    "required": True,
                }
            ],
        }
        p = tmp_path / "related.json"
        p.write_text(json.dumps(doc))
        kw["ledger_path"] = freeze_ledger(p, kw["out_root"])
    else:
        kw["ledger_path"].write_text(
            json.dumps(
                {
                    "schema_version": "source-subtitle-truth-ledger.v1",
                    "entries": [],
                    "note": "tampered frozen bytes",
                }
            )
        )
    before = copy.deepcopy(a)
    with pytest.raises((ValueError, RuntimeError)):
        rebase_pending_previews(a, **kw)
    assert a == before


def test_one_attempt_uses_frozen_ledger_after_origin_changes(tmp_path):
    _, kw, old, new = fixture(tmp_path)
    selected = kw["ledger_path"].read_bytes()
    new.write_text("{}")
    assert kw["ledger_path"].read_bytes() == selected
    _, audit = apply_source_subtitle_truth(
        SRT, spec=kw["spec"], durations=[4000], ledger_path=kw["ledger_path"]
    )
    assert audit["status"] == "NO_RELEVANT_INTERVAL"


@pytest.mark.parametrize('damage',[None,'drop','source','previous_protection'])
def test_withheld_acquires_only_source_bound_local_terms_for_fresh_review(tmp_path,damage):
    authority,kw,_,_=fixture(tmp_path)
    for stage in authority['source_truth_preview_receipts']:
        _,old=apply_source_subtitle_truth(SRT,spec=kw['spec'],durations=[4000],ledger_path=None)
        authority['source_truth_preview_receipts'][stage]=build_source_truth_preview_receipt(
            input_srt_text=SRT,source_truth_audit=old,stage=stage)
    entry={'truth_id':'local','knowledge_type':'SOURCE_INTERVAL_TRUTH',
        'evidence_class':'HUMAN_CONFIRMED_LOCAL_TERM','recording_basename':'source.flv',
        'source_media_sha256':'sha256:'+'a'*64,'source_start_ms':0,'source_end_ms':1000,
        'action':'replace_substring','required':True,'required_text':'旧文义',
        'replacements':[{'surface':'旧文词','canonical':'旧文义'}]}
    if damage=='drop':
        entry['action']='drop_cue'
    elif damage=='source':
        entry['source_media_sha256']='sha256:'+'b'*64
    elif damage=='previous_protection':
        authority['source_truth_preview_receipts']['pre_entity_arbitration']['protected_cue_indexes']=[1]
    path=tmp_path/'local.json'
    path.write_text(json.dumps(
        {'schema_version':'source-subtitle-truth-ledger.v1','entries':[entry]}))
    kw.update(ledger_path=freeze_ledger(path,kw['out_root']),origin_path=path,current_text=SRT)
    original=copy.deepcopy(authority)
    with pytest.raises(ValueError):
        rebase_pending_previews(authority,**kw)
    assert authority==original
    if damage:
        with pytest.raises(ValueError):
            rebase_pending_previews(authority,**kw,allow_added_local_terms=True)
        assert authority==original
    else:
        rebase_pending_previews(authority,**kw,allow_added_local_terms=True)
        transition=authority['source_truth_protection_transition']
        assert transition['status']=='CURRENT_REVIEW_REQUIRED' and not transition['release_authorized']
        assert all(r['protected_cue_indexes']==[1] for r in authority['source_truth_preview_receipts'].values())
        assert len(authority['source_truth_preview_history'])==2


@pytest.mark.parametrize('damage',[False,True])
def test_old_phase_can_replay_ordinary_partial_emitter_reference(tmp_path,damage):
    authority,kw,_,_=fixture(tmp_path)
    correction=SRT.replace('旧文字','原纠错输入')
    _,audit=apply_source_subtitle_truth(correction,spec=kw['spec'],durations=kw['durations'],ledger_path=kw['origin_path'])
    receipt=build_source_truth_preview_receipt(input_srt_text=correction,source_truth_audit=audit,stage='pre_correction_review')
    authority['source_truth_preview_receipts']['pre_correction_review']=receipt
    root=kw['out_root']/'partial-source-text-input'
    root.mkdir(parents=True)
    saved=root/('name_reference.'+receipt['input_srt_sha256'].removeprefix('sha256:')+'.srt')
    saved.write_text(correction+('drift' if damage else ''))
    if damage:
        with pytest.raises(ValueError,match='INPUT_DRIFT'):
            rebase_pending_previews(authority,**kw)
    else:
        rebase_pending_previews(authority,**kw)
        assert any(Path(h['input']['path']).read_text()==correction for h in authority['source_truth_preview_history'])


@pytest.mark.parametrize(
    "damage", [None, "input", "input_binding", "stage", "ledger_binding", "receipt"]
)
def test_source_checkpoint_consumes_saved_phase_inputs_after_transient_references_change(
    tmp_path, damage
):
    authority, kw, old, new = fixture(tmp_path)
    correction = SRT.replace("旧文字", "原纠错输入")
    _, audit = apply_source_subtitle_truth(
        correction, spec=kw["spec"], durations=kw["durations"], ledger_path=new
    )
    authority["source_truth_preview_receipts"]["pre_correction_review"] = (
        build_source_truth_preview_receipt(
            input_srt_text=correction, source_truth_audit=audit, stage="pre_correction_review"
        )
    )
    kw["name_reference"] = correction
    rebase_pending_previews(authority, **kw)
    # The normal source checkpoint carries current text as its name reference.
    kw["name_reference"] = kw["current_text"]
    kw["clip_context"] = {"whole_clip_draft_srt": kw["current_text"]}
    row = next(
        r
        for r in authority["source_truth_preview_history"]
        if r["stage"] == "pre_correction_review"
    )
    if damage == "input":
        Path(row["input"]["path"]).write_text("tampered")
    elif damage == "input_binding":
        row["input"]["sha256"] = "sha256:" + "0" * 64
    elif damage == "stage":
        row["stage"] = "pre_entity_arbitration"
    elif damage == "ledger_binding":
        row["ledger"]["sha256"] = "sha256:" + "0" * 64
    elif damage == "receipt":
        row["preview"]["protected_cue_indexes"] = [1]
    before = copy.deepcopy(authority)
    if damage:
        with pytest.raises(ValueError):
            rebase_pending_previews(authority, **kw)
        assert authority == before
    else:
        rebase_pending_previews(authority, **kw)
        after = copy.deepcopy(authority)
        rebase_pending_previews(authority, **kw)
        assert authority == after
        assert Path(row["input"]["path"]).read_text() == correction


@pytest.mark.parametrize("complete_version", [True, False])
def test_legacy_preview_replays_its_actual_prior_version_locator(
    tmp_path, monkeypatch, complete_version
):
    from src.autoslice import producer_source_truth_checkpoint as checkpoint

    authority, kw, _, new = fixture(tmp_path)
    runtime = tmp_path / "runtime"
    current = runtime / "versions" / ("a" * 40) / "repo"
    prior = runtime / "versions" / ("b" * 40) / "repo"
    relative = Path("assets/lidousha/truth.json")
    for root, commit in [(current, "a" * 40), (prior, "b" * 40)]:
        (root / relative).parent.mkdir(parents=True)
        (root / "DEPLOYED_COMMIT").write_text(commit)
        (root / "DEPLOYED_MANIFEST.json").write_text("{}")
        (root / relative).write_bytes(new.read_bytes())
    _, audit = apply_source_subtitle_truth(
        SRT, spec=kw["spec"], durations=kw["durations"], ledger_path=prior / relative
    )
    authority["source_truth_preview_receipts"]["pre_correction_review"] = (
        build_source_truth_preview_receipt(
            input_srt_text=SRT, source_truth_audit=audit, stage="pre_correction_review"
        )
    )
    kw["origin_path"] = current / relative
    monkeypatch.setattr(
        checkpoint, "__file__", str(current / "src/autoslice/producer_source_truth_checkpoint.py")
    )
    if not complete_version:
        (prior / "DEPLOYED_MANIFEST.json").unlink()
        with pytest.raises(ValueError, match="ORIGINAL_REPLAY_DRIFT"):
            rebase_pending_previews(authority, **kw)
    else:
        rebase_pending_previews(authority, **kw)
        assert any(
            h["preview"]["source_truth_audit_sha256"] == audit_sha
            for h in authority["source_truth_preview_history"]
            for audit_sha in [
                build_source_truth_preview_receipt(
                    input_srt_text=SRT, source_truth_audit=audit, stage="pre_correction_review"
                )["source_truth_audit_sha256"]
            ]
        )
