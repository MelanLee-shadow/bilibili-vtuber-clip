from scripts.build_fastlane_c2_private_successor import load, project
from pathlib import Path
def test_c2_authority_is_candidate_scoped_and_no_upload():
 a=load(); assert a['candidate_id']=='auto_203011_328_389'; assert a['upload_allowed'] is False; assert a['operator_source']['raw_line_sha256']=='e64d4409aaf36193c27f3d67cd8e3fae69a6d3ae543a29a6c26f57c77d61c2aa'
def test_c2_only_changes_exact_cue_and_freezes_danmaku_response():
 a=load(); source=(Path('assets/lidousha/fastlane_c2_private/auto_203011_328_389.pipeline-diagnostic.srt')).read_text(); result=project(source,a)
 assert '小豆老公；； 不是你老公' in result and '是刚吗？小豆老公不是你老公' not in result
 assert '00:00:56,080 --> 00:00:58,860\n小豆哪有好吵' in result
 assert result.count('\n\n')==21
def test_c2_identity_is_not_two_people():
 a=load(); assert a['identity']['resolved']=='小李 is 李豆沙'; assert '李豆沙：不是你老公' in a['identity']['title']
