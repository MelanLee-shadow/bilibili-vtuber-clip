"""Test defaults: the suite exercises the full two-pass adjudication unless a test asks for a variant, and variant C's
Gemini-only ladder unless a test asks for another one (the window-ASR ladders have their own tests,
test_task11_witnesses_cover_lone.py).  The production defaults are stage_impl.DEFAULT_ADJUDICATE_MODE and
lane_rules listening.ladder (assets/manual_lane/lane_rules.json)."""

import pytest
from fakes import RealMediaHost


@pytest.fixture(autouse=True)
def _adjudicate_full_by_default(monkeypatch):
    monkeypatch.setenv("MANUAL_LANE_ADJUDICATE_MODE", "full")
    monkeypatch.setenv("MANUAL_LANE_LADDER", "gemini")


@pytest.fixture(autouse=True)
def _one_agent_run_by_default(monkeypatch):
    """A test scripts one agent conversation; the production default (lane_rules subtitle_agent.samples, several
    independent runs and a per-cue majority) has its own tests in test_playbook_vote.py, which set the number."""
    from src.autoslice.manual_lane import playbook_step3

    monkeypatch.setattr(playbook_step3, "samples", lambda: 1)


@pytest.fixture
def real_media_host(tmp_path, monkeypatch):
    """The conditions of `run --on-media-host` on the real media host (fakes.RealMediaHost): the clip's local directory is
    its remote directory, and the publication registry / receipt are root-only.  Use it for the test of any command that
    creates a clip directory or puts a file on both sides."""
    return RealMediaHost(tmp_path, monkeypatch)


@pytest.fixture(autouse=True)
def _no_picture_reading_by_default(monkeypatch):
    """The shipped rules read the on-screen bar before the subtitle agent starts (lane_rules screen_work); a test that
    scripts an agent conversation has no picture to read, so the suite switches it off and test_work_context.py, which
    exercises it, switches it on."""
    from src.autoslice.manual_lane import work_context

    monkeypatch.setattr(work_context, "enabled", lambda: False)


@pytest.fixture(autouse=True)
def _no_other_recognisers_by_default(monkeypatch):
    """The subtitle agent starts with the whole clip as MAI and MOSS heard it (playbook_step3.other_recognitions, one
    remote pass per clip); a test that scripts an agent conversation has no such pass, so the suite gives none and the
    test that exercises it supplies its own."""
    from src.autoslice.manual_lane import playbook_step3

    monkeypatch.setattr(playbook_step3, "_asr_witnesses", lambda _ctx: ({}, {}))


@pytest.fixture(autouse=True)
def _no_voiceprint_answer_by_default(monkeypatch):
    """Before the whole clip goes to the paid recognisers the lane asks the host's voiceprint whether anyone else is
    heard (voices.others_heard, a run on the media host); a test without that run gets no answer, which leaves the
    recognisers as configured.  test_voices_gate.py supplies the answers it tests."""
    from src.autoslice.manual_lane import voices

    monkeypatch.setattr(voices, "others_heard", lambda _ctx: {"others": None, "basis": "", "share": None, "longest_run": None})


@pytest.fixture(autouse=True)
def _no_real_waiting_for_a_rate_limit(monkeypatch):
    """A listening pass waits out a provider rate limit (step3_evidence._listen_pass); no test sleeps for real."""
    from src.autoslice.manual_lane import step3_evidence

    monkeypatch.setattr(step3_evidence, "_sleep", lambda _seconds: None)



@pytest.fixture(autouse=True)
def _no_reference_reading_by_default(request, monkeypatch):
    """The shipped lane reads the recognisers' draft for references before the subtitle agent starts (references.py: a
    model call and web searches); a test that scripts an agent conversation has neither, so the suite gives the agent
    no leads and test_references.py, which exercises the reading, keeps the real one."""
    if request.module.__name__.endswith("test_references"):
        return
    from src.autoslice.manual_lane import references

    monkeypatch.setattr(references, "for_subtitles", lambda _ctx, _draft: {"leads": [], "cpa": [], "line": ""})


@pytest.fixture(autouse=True)
def _no_persona_observation_by_default(request, monkeypatch):
    """Every run of a clip ends by reading the sealed clip for what it shows of the host (persona_observe.after_run: a
    model call); a test that counts a run's calls or its printed lines has no such call, so the suite switches it off
    and test_persona_observe.py, which exercises it, keeps the real one."""
    if request.module.__name__.endswith("test_persona_observe"):
        return
    from src.autoslice.manual_lane import persona_observe

    monkeypatch.setattr(persona_observe, "after_run", lambda _ctx: "")


@pytest.fixture(autouse=True)
def _reference_seeds_in_a_temporary_place(tmp_path_factory, monkeypatch):
    """The lane leaves what its clips looked up for the news crawler in a file of the media host's shared assets
    (references.seeds_path); no test writes there."""
    from src.autoslice.manual_lane import references

    place = tmp_path_factory.mktemp("reference-seeds") / "reference_seeds.json"
    monkeypatch.setattr(references, "seeds_path", lambda: place)
