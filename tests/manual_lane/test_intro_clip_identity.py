import json

from fakes import FakeCpa, FakeRemote, make_ctx

from src.autoslice.manual_lane.adapters import RemoteResult
from src.autoslice.manual_lane.step5_burn import step5_burn


def test_burn_passes_session_date_and_candidate_to_remote_helper(tmp_path):
    def run(name, body, remote):
        if name == "s5-burn":
            remote.files["/r/clip/burn/intro_result.json"] = json.dumps(
                {"status": "PREPENDED", "intro_id": "A", "intro_offset_ms": 1000}).encode()
        return RemoteResult(0, "", "")

    remote = FakeRemote(on_script=run)
    ctx = make_ctx(tmp_path, remote=remote, cpa=FakeCpa([]))
    ctx.state.data["clip"]["cid"] = "semantictalk_686120_762640"
    speaker = ctx.clip_dir / "speaker/speaker.srt"
    speaker.parent.mkdir(parents=True)
    speaker.write_text("1\n00:00:00,000 --> 00:00:01,000\nhi\n")
    assert step5_burn(ctx).ok
    script = remote.scripts[-1][1]
    assert "2026-10-24 semantictalk_686120_762640" in script


def test_burn_refuses_conflicting_clip_and_session_dates(tmp_path):
    ctx = make_ctx(tmp_path, remote=FakeRemote(), cpa=FakeCpa([]))
    ctx.state.data["clip"]["recording_date"] = "2026-11-06"
    outcome = step5_burn(ctx)
    assert not outcome.ok and outcome.reason == "INTRO_CLIP_DATE_MISMATCH"
    assert not ctx.adapters.remote.scripts
