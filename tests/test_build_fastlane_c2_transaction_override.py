import json,subprocess,sys
def test_c2_bridge_is_one_cue_and_no_upload():
 d=json.loads(subprocess.check_output([sys.executable,'scripts/build_fastlane_c2_transaction_override.py'],text=True)); assert d['schema_version']==4 and d['upload'] is False and len(d['overrides'])==1 and d['overrides'][0]['source_cue']==5
