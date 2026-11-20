"""The prompts consume the profile's section rather than restating it."""
from pathlib import Path
import re

import pytest

from src.autoslice.manual_lane import step_io


def rule_like(text):
    """Catch independently worded pronoun prescriptions in prose and code strings."""
    return any(re.search(r'代词|指代|观众|主播|性别|动物|物体', line)
               and re.search(r'用|写|改|保留|默认|应|必须|一律', line)
               and len(set(re.findall(r'(?<![A-Za-z])TA(?![A-Za-z])|[他她它]', line))) >= 2
               and re.search(r'(用|写作|写成|写|改成|改为|保留|统一)[「“"]?(?:他|她|它|(?<![A-Za-z])TA(?![A-Za-z]))', line)
               for line in text.splitlines())


def test_no_second_pronoun_rule_is_stated_in_lane_sources():
    assert rule_like('代词指观众用他们，主播用她们')
    assert rule_like('性别不明保留他，不统一改成TA')
    # prose that merely uses her/it is no rule (speaker_video.md, subtitle_agent.md were flagged for these)
    assert not rule_like('这时她的声音和视频的声音混在一起…站在视频外面对它评论、向观众说话（主播）')
    assert not rule_like('主播是在化用、自己改了词的地方，按她实际说的写；音对不上就不套它')
    roots = (step_io.PKG_ROOT / 'src/autoslice/manual_lane', step_io.PKG_ROOT / 'assets/manual_lane')
    duplicates = [str(p) for root in roots for p in root.rglob('*')
                  if p.suffix in {'.py', '.md'} and rule_like(p.read_text(encoding='utf-8'))]
    assert not duplicates


def test_section_is_loaded_from_active_profile_and_required(tmp_path, monkeypatch):
    path = tmp_path / 'custom-principles.md'
    path.write_text('## 五、其他\n\n其他规则\n\n## 六、代词与指代对象一致\n\n- 本频道测试规则\n\n## 七、其他\n', encoding='utf-8')
    class Profile:
        asset_root = tmp_path

        def asset_file(self, key):
            assert key == 'subtitle_correction_principles'
            return path
    monkeypatch.setattr(step_io, 'load_channel_profile', lambda _root: Profile())
    assert step_io._pronoun_rule() == '## 六、代词与指代对象一致\n\n- 本频道测试规则'
    path.write_text('## 五、缺少第六节\n', encoding='utf-8')
    with pytest.raises(ValueError, match='pronoun section'):
        step_io._pronoun_rule()
    path.write_text('## 六、代词与指代对象一致\n\n- 规则\n\n## 七、其他\n\n## 六、代词与指代对象一致\n', encoding='utf-8')
    with pytest.raises(ValueError, match='pronoun section'):
        step_io._pronoun_rule()


def test_adjudication_and_reviews_render_the_same_section():
    source = step_io._pronoun_rule()
    for prompt in ('subtitle_adjudicate.md', 'review.md', 'review_held.md', 'name_consistency.md'):
        assert source in step_io._render(prompt)


def test_people_whose_pronoun_the_graph_records_join_the_rule(tmp_path, monkeypatch):
    """维护者: her advisor is a man in the knowledge graph, so the subtitles write 他.  Any accepted node with
    a pronoun reaches every writer of the subtitles through the one rule; a candidate node does not."""
    import json

    path = tmp_path / 'principles.md'
    path.write_text('## 六、代词与指代对象一致\n\n- 身边人暂写默认\n\n## 七、其他\n', encoding='utf-8')
    src = [{'kind': 'owner', 'ref': 'test', 'at': None}]
    nodes = [{'id': 'person:新来的同学', 'kind': 'person', 'names': {'display': '新来的同学'}, 'aliases': ['同桌'],
              'summary': '', 'status': 'accepted', 'sources': src, 'pronoun': '他'},
             {'id': 'person:没核实的人', 'kind': 'person', 'names': {'display': '没核实的人'}, 'aliases': [],
              'summary': '', 'status': 'candidate', 'sources': src, 'pronoun': '他'},
             {'id': 'person:没记代词的人', 'kind': 'person', 'names': {'display': '没记代词的人'}, 'aliases': [],
              'summary': '', 'status': 'accepted', 'sources': src}]
    (tmp_path / 'knowledge').mkdir()
    (tmp_path / 'knowledge/nodes.jsonl').write_text(''.join(json.dumps(n, ensure_ascii=False) + '\n' for n in nodes),
                                                    encoding='utf-8')
    (tmp_path / 'knowledge/edges.jsonl').write_text('', encoding='utf-8')

    class Profile:
        asset_root = tmp_path

        def asset_file(self, _key):
            return path

    monkeypatch.setattr(step_io, 'load_channel_profile', lambda _root: Profile())
    rule = step_io._pronoun_rule()
    assert rule.startswith('## 六、代词与指代对象一致\n\n- 身边人暂写默认\n')
    assert '新来的同学（同桌）→他' in rule and '没核实的人' not in rule and '没记代词的人' not in rule


def test_the_channels_rule_names_her_advisor_as_he():
    assert '李豆沙的导师' in step_io.PRONOUN_RULE and '→他' in step_io.PRONOUN_RULE.split('李豆沙的导师')[1][:60]
    assert '小室→她' in step_io.PRONOUN_RULE
