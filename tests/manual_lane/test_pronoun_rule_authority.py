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
               for line in text.splitlines())


def test_no_second_pronoun_rule_is_stated_in_lane_sources():
    assert rule_like('代词指观众用他们，主播用她们')
    assert rule_like('性别不明保留他，不统一改成TA')
    roots = (step_io.PKG_ROOT / 'src/autoslice/manual_lane', step_io.PKG_ROOT / 'assets/manual_lane')
    duplicates = [str(p) for root in roots for p in root.rglob('*')
                  if p.suffix in {'.py', '.md'} and rule_like(p.read_text(encoding='utf-8'))]
    assert not duplicates


def test_section_is_loaded_from_active_profile_and_required(tmp_path, monkeypatch):
    path = tmp_path / 'custom-principles.md'
    path.write_text('## 五、其他\n\n其他规则\n\n## 六、代词与指代对象一致\n\n- 本频道测试规则\n\n## 七、其他\n', encoding='utf-8')
    class Profile:
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
