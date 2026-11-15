"""The day's default session names the host by the channel's display name (goldfish 4, 枝堇Sumire: display name
枝堇Sumire, self-reference 枝堇 -> every cover was vetoed HOST_MISSING because the judge named her 枝堇)."""
import dataclasses

from src.autoslice.channel_profile import load_channel_profile
from src.autoslice.manual_lane import channel, config, discover


def _with_profile(monkeypatch, **identity):
    real = load_channel_profile(discover.REPO_ROOT)
    fake = dataclasses.replace(real, **identity)
    monkeypatch.setattr('src.autoslice.channel_profile.load_channel_profile', lambda *a, **k: fake)
    return fake


def test_the_host_is_named_by_her_display_name_even_when_she_calls_herself_otherwise(tmp_path, monkeypatch):
    fake = _with_profile(monkeypatch, display_name='枝堇Sumire', self_reference_aliases=('枝堇', 'sumire'))
    session, _ = discover.resolve_session(tmp_path, '2026-11-15')
    assert config.participant_names({'participants': session.participants}) == ['枝堇Sumire']
    assert [list(g) for g in session.participants] == [['枝堇Sumire', '枝堇', 'sumire']]
    assert fake.display_name == '枝堇Sumire'


def test_the_default_channel_session_is_unchanged(tmp_path):
    session, _ = discover.resolve_session(tmp_path, '2026-11-15')
    profile = load_channel_profile(discover.REPO_ROOT)
    assert [list(g) for g in session.participants] == [list(profile.self_reference_aliases)]
    assert config.participant_names({'participants': session.participants}) == [channel.HOST]
