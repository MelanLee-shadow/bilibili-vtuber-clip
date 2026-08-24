import json, shutil
from pathlib import Path
import pytest
from scripts.audit_lidousha_review_package import audit_package
from src.autoslice.fastlane_c1_technical_receipt import C1TechnicalReceiptError, template, validate_completed
from src.autoslice.final_human_review import replay_final_human_review_attestation

ROOT=Path(__file__).resolve().parents[1]
SOURCE=ROOT/'.private/c1-formal-private-v9-20260824'
EVIDENCE=SOURCE/'c1.root-review-evidence.v1.json'
def package(tmp_path):
 p=tmp_path/'p'; shutil.copytree(SOURCE,p); shutil.copy2(EVIDENCE,p/'c1.root-review-evidence.v1.json'); a=p/'package_audit.json'; a.write_text(json.dumps(audit_package(p),sort_keys=True)); return p,a
def test_c1_template_is_six_point_no_grant(tmp_path):
 p,a=package(tmp_path); value=template(p,a); assert value['accepted'] is False and value['upload_allowed'] is False and len(value['six_named_points'])==6
def test_c1_template_rejects_root_evidence_drift(tmp_path):
 p,a=package(tmp_path); (p/'c1.root-review-evidence.v1.json').write_text('{}');
 with pytest.raises(C1TechnicalReceiptError,match='ROOT_EVIDENCE'): template(p,a)
def test_completed_requires_exact_six_passes_and_root_identity(tmp_path):
 p,a=package(tmp_path); value=template(p,a); value.update(status='ACCEPTED_FOR_SAME_BV_TECHNICAL',accepted=True,reviewed_by='Codex root',reviewed_at='2026-08-24T00:00:00Z')
 for row in value['six_named_points']: row.update(verdict='PASS',evidence=row['root_evidence_point_sha256'])
 assert validate_completed(value,p,a)['accepted'] is True
 value['six_named_points'][0]['verdict']='FAIL'
 with pytest.raises(C1TechnicalReceiptError,match='POINT'): validate_completed(value,p,a)
def test_completed_requires_timezone_aware_iso8601(tmp_path):
 p,a=package(tmp_path); value=template(p,a); value.update(status='ACCEPTED_FOR_SAME_BV_TECHNICAL',accepted=True,reviewed_by='Codex root',reviewed_at='2026-08-24 00:00:00')
 for row in value['six_named_points']: row.update(verdict='PASS',evidence=row['root_evidence_point_sha256'])
 with pytest.raises(C1TechnicalReceiptError,match='REVIEWED_AT'): validate_completed(value,p,a)
def test_completed_rejects_direct_upload_seal_drift(tmp_path):
 p,a=package(tmp_path); value=template(p,a); value.update(status='ACCEPTED_FOR_SAME_BV_TECHNICAL',accepted=True,reviewed_by='Codex root',reviewed_at='2026-08-24T00:00:00+00:00')
 for row in value['six_named_points']: row.update(verdict='PASS',evidence=row['root_evidence_point_sha256'])
 value['bindings']['direct_upload_authority']['seals']['line1643']['raw_sha256']='sha256:'+'0'*64
 with pytest.raises(C1TechnicalReceiptError,match='BINDING'): validate_completed(value,p,a)
def test_same_bv_attestation_dispatches_only_c1_formal_receipt(tmp_path):
 p,a=package(tmp_path); receipt=template(p,a); receipt.update(status='ACCEPTED_FOR_SAME_BV_TECHNICAL',accepted=True,reviewed_by='Codex root',reviewed_at='2026-08-24T00:00:00+00:00')
 for row in receipt['six_named_points']: row.update(verdict='PASS',evidence=row['root_evidence_point_sha256'])
 r=p/'c1.receipt.json'; r.write_text(json.dumps(receipt)); bind=lambda x:{'path':str(x.resolve()),'sha256':__import__('hashlib').sha256(x.read_bytes()).hexdigest(),'bytes':x.stat().st_size}
 manifest={'recovery_publication_authority':{},'package_attestation':{'package_root':str(p.resolve()),'review_manifest':bind(p/'review_manifest.json'),'package_audit':bind(a),'c1_technical_receipt':bind(r)}}
 assert replay_final_human_review_attestation(manifest)['c1_technical_receipt']['path']==str(r.resolve())
