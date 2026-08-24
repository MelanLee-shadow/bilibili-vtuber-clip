import json, shutil
from pathlib import Path
import pytest
from scripts.audit_lidousha_review_package import audit_package
from src.autoslice.fastlane_c1_technical_receipt import C1TechnicalReceiptError, template, validate_completed

ROOT=Path(__file__).resolve().parents[1]
SOURCE=Path('/private/tmp/vtuber-slice-fastlane-c1-minimal-20260824/.private/c1-formal-private-v7-bf5dd845-20260824')
EVIDENCE=Path('/private/tmp/vtuber-slice-fastlane-c1-minimal-20260824/.private/c1-root-review-evidence-v7-bf5dd845-20260824/root-review-evidence.v1.json')
def package(tmp_path):
 p=tmp_path/'p'; shutil.copytree(SOURCE,p); shutil.copy2(EVIDENCE,p/'c1.root-review-evidence.v1.json'); a=p/'package_audit.json'; a.write_text(json.dumps(audit_package(p),sort_keys=True)); return p,a
def test_c1_template_is_six_point_no_grant(tmp_path):
 p,a=package(tmp_path); value=template(p,a); assert value['accepted'] is False and value['upload_allowed'] is False and len(value['six_named_points'])==6
def test_c1_template_rejects_root_evidence_drift(tmp_path):
 p,a=package(tmp_path); (p/'c1.root-review-evidence.v1.json').write_text('{}');
 with pytest.raises(C1TechnicalReceiptError,match='ROOT_EVIDENCE'): template(p,a)
def test_completed_requires_exact_six_passes_and_root_identity(tmp_path):
 p,a=package(tmp_path); value=template(p,a); value.update(status='ACCEPTED_FOR_SAME_BV_TECHNICAL',accepted=True,reviewed_by='Codex root',reviewed_at='2026-08-24T00:00:00Z')
 for row in value['six_named_points']: row.update(verdict='PASS',evidence='anchor-specific technical observation')
 assert validate_completed(value,p,a)['accepted'] is True
 value['six_named_points'][0]['verdict']='FAIL'
 with pytest.raises(C1TechnicalReceiptError,match='POINT'): validate_completed(value,p,a)
