"""The publication registry and the first-publication receipt it binds are root-only on the media host, the lane runs as an
ordinary user there (nine-clip run: `revise` died with Permission denied, a successor stopped at step 8 with
REVISION_AUTHORITY_UNBUILT).  Every question the lane asks of them goes through title_history.through_boundary: here when
this process can read, else through the root boundary, which returns only the answer.  The fake remote below is that
boundary: it answers a request by running the same `serve` the media host's script runs, with the files readable only
inside it; the same fake files, readable, give the local answer to compare with."""

from __future__ import annotations

import json

import pytest
from fakes import FakeRemote
from test_fmr_calls import RD, REVIEW_PASS
from test_published_revision import (
    _adapters_for,
    _closure,
    _published_clip,
    _successor,
)

from src.autoslice import publication_registry
from src.autoslice.manual_lane import revise_run, revision_authority, title_history
from src.autoslice.manual_lane import stage_package as sp
from src.autoslice.manual_lane.adapters import RemoteResult
from src.autoslice.manual_lane.state import ClipState, StateError
from tests.test_publication_reconciliation import first_publication_registries  # noqa: F401  (the fixture)

ROOT_ONLY_CALLS = ("s8-published-status", "s8-first-publication", "s8-first-digests", "s6-published-titles")


class RootBoundary:
    """The registry is unreadable (PermissionError) for this process; the fake remote reads it as root."""

    def __init__(self, monkeypatch, *, root_fails: str | None = None):
        self.calls: list[tuple[str, dict]] = []
        self.as_root = False
        self.root_fails = root_fails
        real = publication_registry.load_publication_registry

        def guarded(*args, **kwargs):
            if not self.as_root:
                raise PermissionError("[Errno 13] Permission denied: '/opt/bilive/autoslice/state/publication_registry.runtime.v1.json'")
            return real(*args, **kwargs)

        monkeypatch.setattr(publication_registry, "load_publication_registry", guarded)
        monkeypatch.setattr(title_history, "load_publication_registry", guarded)

    def remote(self) -> FakeRemote:
        def on_script(name, body, remote):
            if name not in ROOT_ONLY_CALLS:
                return RemoteResult(0, "", "")
            assert "sudo -n" in body and "history_reader.py" in body            # run through the root wrapper, as the titles are
            request = json.loads(remote.files[f"{RD}/title/history_request.json"])
            self.calls.append((name, request))
            if self.root_fails:
                return RemoteResult(1, "", self.root_fails)
            self.as_root = True
            try:
                return RemoteResult(0, json.dumps(title_history.serve(request), ensure_ascii=False), "")
            finally:
                self.as_root = False

        remote = FakeRemote(on_script=on_script)
        return remote


def test_step_8_builds_the_same_authority_and_revision_through_the_root_boundary_as_when_it_can_read(
        tmp_path, monkeypatch, first_publication_registries):  # noqa: F811
    _closure(tmp_path, monkeypatch)
    local_ctx, local_rec = _successor(tmp_path / "ref", monkeypatch, review=REVIEW_PASS)
    local_out = sp.step8_package(local_ctx)
    assert local_out.ok and sp.step9_review(local_ctx).ok
    for call in (local_rec.calls[1], local_rec.calls[3]):                 # check, seal: the evidence is replayed in full
        assert call[call.index("--repair-authority") + 1] == str(local_ctx.clip_dir / revision_authority.AUTHORITY_FILE)
        assert "--authority-shape-only" not in call
    assert any("完整重放" in line for line in local_out.process)
    local = {name: (local_ctx.clip_dir / name).read_text(encoding="utf-8")
             for name in (revision_authority.AUTHORITY_FILE, revision_authority.REVISION_FILE)}

    boundary = RootBoundary(monkeypatch)
    ctx, rec = _successor(tmp_path / "root", monkeypatch, review=REVIEW_PASS)
    ctx.adapters.remote.on_script = boundary.remote().on_script
    out = sp.step8_package(ctx)
    assert out.ok, out.note
    assert sp.step9_review(ctx).ok
    authority_file = str(ctx.clip_dir / revision_authority.AUTHORITY_FILE)
    for call in (rec.calls[1], rec.calls[3]):        # check, seal: built in the boundary, this process cannot replay the evidence
        assert list(call[call.index("--repair-authority") + 1:][:2]) == [authority_file, "--authority-shape-only"]
    assert "--repair-authority" not in rec.calls[2] and "--authority-shape-only" not in rec.calls[2]       # attest
    assert any("--authority-shape-only" in line and "root 边界内重放核对" in line for line in out.process)
    through = {name: (ctx.clip_dir / name).read_text(encoding="utf-8")
               for name in (revision_authority.AUTHORITY_FILE, revision_authority.REVISION_FILE)}
    authority_local, authority_root = json.loads(local[revision_authority.AUTHORITY_FILE]), json.loads(through[revision_authority.AUTHORITY_FILE])
    assert authority_root == authority_local                              # every field the same as when this process can read
    revision_local, revision_root = json.loads(local[revision_authority.REVISION_FILE]), json.loads(through[revision_authority.REVISION_FILE])
    assert revision_root["published"] == revision_local["published"] and revision_root["changed"] == revision_local["changed"]
    assert revision_root["bvid"] == revision_local["bvid"] and revision_root["revision"] == revision_local["revision"]
    names = [name for name, _request in boundary.calls]
    assert names == ["s8-published-status", "s8-first-publication", "s8-first-digests"]
    assert sorted(boundary.calls[1][1]) == ["build_authority", "candidate_id"]
    assert not (ctx.clip_dir / "title/history_request.json").exists()      # nothing of the request is left in the clip's tree
    for path in ctx.clip_dir.rglob("*"):
        assert path.stat().st_uid == ctx.clip_dir.stat().st_uid


def test_the_receipt_and_the_first_manifest_are_read_only_inside_the_boundary_when_they_are_root_only(tmp_path, monkeypatch, first_publication_registries):  # noqa: F811
    _closure(tmp_path, monkeypatch)
    boundary = RootBoundary(monkeypatch)
    ctx, _ = _successor(tmp_path, monkeypatch)
    ctx.adapters.remote.on_script = boundary.remote().on_script
    for module_name in ("build_new_bv_repair_authority", "replay_first_publication_manifest"):
        from src.autoslice import new_bv_repair_authority as module
        real = getattr(module, module_name)

        def only_as_root(*args, _real=real, **kwargs):
            if not boundary.as_root:          # the receipt and the first manifest are root 0600 too
                raise PermissionError("[Errno 13] Permission denied: the first-publication receipt")
            return _real(*args, **kwargs)

        monkeypatch.setattr(module, module_name, only_as_root)
    assert sp.step8_package(ctx).ok


def test_when_the_boundary_fails_too_the_reason_has_both_errors(tmp_path, monkeypatch, first_publication_registries):  # noqa: F811
    _closure(tmp_path, monkeypatch)
    boundary = RootBoundary(monkeypatch, root_fails="sudo: a password is required")
    ctx, rec = _successor(tmp_path, monkeypatch)
    ctx.adapters.remote.on_script = boundary.remote().on_script
    out = sp.step8_package(ctx)
    assert not out.ok and out.reason == "REVISION_AUTHORITY_UNBUILT"
    assert "PermissionError" in out.note and "Permission denied" in out.note and "sudo: a password is required" in out.note
    assert rec.calls == []


def test_revise_judges_a_published_clip_through_the_root_boundary_and_builds_the_successor(tmp_path, monkeypatch):
    ctx, scripts = _published_clip(tmp_path, monkeypatch)
    boundary = RootBoundary(monkeypatch)
    remote = FakeRemote()

    def on_script(name, body, rem):
        scripts.append((name, body))
        if name == "s8-published-status":
            boundary.calls.append((name, json.loads(rem.files["/r/clip/title/history_request.json"])))
            return RemoteResult(0, json.dumps({"published": True}), "")
        return RemoteResult(0, "", "")

    remote.on_script = on_script
    before = {p: p.read_bytes() for p in (tmp_path / "clips/clip").rglob("*") if p.is_file()}
    assert revise_run.revise_clip(task=tmp_path, cid="clip", reason="r", adapters_for=_adapters_for(remote)) == "clip_r2"
    assert [n for n, _r in boundary.calls] == ["s8-published-status"] and boundary.calls[0][1]["candidate_id"] == "clip"
    assert {p: p.read_bytes() for p in (tmp_path / "clips/clip").rglob("*") if p.is_file()} == before     # the original's tree is untouched
    assert ClipState.load(tmp_path / "state/clip.json").data["clip"]["superseded_by"] == "clip_r2"


def test_revise_names_both_errors_when_neither_side_can_read_and_leaves_nothing(tmp_path, monkeypatch):
    ctx, _scripts = _published_clip(tmp_path, monkeypatch)
    boundary = RootBoundary(monkeypatch, root_fails="sudo: a password is required")
    with pytest.raises(StateError, match=r"cannot be read.*PermissionError.*Permission denied.*sudo: a password is required"):
        revise_run.revise_clip(task=tmp_path, cid="clip", reason="r", adapters_for=_adapters_for(boundary.remote()))
    assert not (tmp_path / "state/clip_r2.json").exists() and not (tmp_path / "clips/clip_r2").exists()


def test_a_remote_lane_always_asks_the_boundary_without_trying_to_read_here(tmp_path, monkeypatch):
    """Over ssh the registry is on the other machine: no local read is attempted (published_status, as it was)."""
    from src.autoslice.manual_lane import adapters

    ctx, _ = _published_clip(tmp_path, monkeypatch)
    monkeypatch.setattr(title_history, "load_publication_registry", lambda: (_ for _ in ()).throw(AssertionError("read here")))
    ctx.adapters.remote = adapters.SshRemote("nowhere", scripts_dir="/r/steps")
    seen = []
    monkeypatch.setattr(title_history, "_remote_read", lambda c, request, call: seen.append((call, request)) or {"published": True})
    assert title_history.published_status(ctx, "clip") is True
    assert seen == [("s8-published-status", {"candidate_id": "clip", "check_published": True})]
