from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.autoslice import authorized_upload_cli_parser
from src.autoslice import same_bv_title_cover_cli as cli
from src.autoslice import same_bv_title_cover_reconciliation as reconciliation


def _handlers():
    return {
        "title_cover_repair_plan": object(),
        "title_cover_repair_run": object(),
        "title_cover_repair_status": object(),
        "title_cover_repair_verify_live": object(),
    }


def _defaults(tmp_path: Path):
    return {
        "cookie_json": tmp_path / "cookie.json",
        "biliup_cookie_json": tmp_path / "biliup.json",
    }


def test_parser_registers_title_cover_commands(tmp_path: Path) -> None:
    handlers = _handlers()
    args = authorized_upload_cli_parser.parse_args(
        [
            "title-cover-repair-verify-live",
            "--authority",
            "authority.json",
            "--plan",
            "plan.json",
            "--journal",
            "journal.jsonl",
            "--out",
            "completed.json",
            "--reconciliation-out",
            "reconciliation.json",
        ],
        description="x",
        handlers=handlers,
        defaults=_defaults(tmp_path),
    )
    assert args.func is cli.entry_verify_live
    assert args.reconciliation_out == "reconciliation.json"


def test_unique_authority_fields_fail_closed() -> None:
    assert cli._unique_string({"a": {"bvid": "BV1x"}, "b": {"bvid": "BV1x"}}, "bvid") == "BV1x"
    with pytest.raises(Exception, match="unambiguous"):
        cli._unique_string({"a": {"bvid": "BV1x"}, "b": {"bvid": "BV1y"}}, "bvid")


def test_plan_uses_live_snapshot_then_writes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    authority = {"bvid": "BV1x", "section_id": 7}
    calls: list[object] = []

    class Adapter:
        def observe(self, bvid, section_id):
            calls.append((bvid, section_id))
            return {"bvid": bvid, "section_id": section_id}

    monkeypatch.setattr(cli, "load_authority", lambda _path: authority)
    monkeypatch.setattr(cli, "_promote_adapter", lambda _base: Adapter())
    monkeypatch.setattr(
        cli,
        "create_plan",
        lambda **kwargs: {"schema_version": "plan", "bvid": "BV1x", "kwargs": sorted(kwargs)},
    )
    monkeypatch.setattr(cli, "write_plan", lambda path, value: path.write_text(json.dumps(value)))
    monkeypatch.setattr(cli, "initialise_journal", lambda path, *_args: path.write_text("journal\n"))

    @contextmanager
    def lock(_path):
        yield

    args = SimpleNamespace(
        authority=tmp_path / "authority.json",
        out=tmp_path / "plan.json",
        journal=tmp_path / "journal.jsonl",
        cookie_json=tmp_path / "cookie.json",
        biliup_cookie_json=tmp_path / "biliup.json",
        lock=None,
        dry_run=False,
    )
    assert cli.plan(
        args,
        default_upload_lock=tmp_path / "lock",
        exclusive_upload_lock=lock,
        base_adapter_factory=lambda *_args: object(),
    ) == 0
    assert calls == [("BV1x", 7)]
    assert Path(args.out).is_file()
    assert Path(args.journal).is_file()


def test_verify_consumes_completed_receipt(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    plan_path = tmp_path / "plan.json"
    plan_path.write_text("{}")
    completed = tmp_path / "completed.json"
    reconciliation_out = tmp_path / "reconciliation.json"
    calls: list[str] = []
    monkeypatch.setattr(cli, "load_plan", lambda _path: {"bvid": "BV1x"})
    monkeypatch.setattr(cli, "_promote_adapter", lambda _base: object())

    def verify_live_receipt(**_kwargs):
        calls.append("verify")
        completed.write_text('{"schema_version":"same-bv-title-cover-repair-completed.v1"}\n')

    def reconcile(**kwargs):
        calls.append("reconcile")
        Path(kwargs["out"]).write_text('{"status":"CONSUMED"}\n')
        return {"status": "CONSUMED"}

    monkeypatch.setattr(cli, "verify_live_receipt", verify_live_receipt)
    monkeypatch.setattr(cli, "reconcile", reconcile)

    @contextmanager
    def lock(_path):
        yield

    args = SimpleNamespace(
        authority=tmp_path / "authority.json",
        plan=plan_path,
        journal=tmp_path / "journal.jsonl",
        out=completed,
        reconciliation_out=reconciliation_out,
        cookie_json=tmp_path / "cookie.json",
        biliup_cookie_json=tmp_path / "biliup.json",
        lock=None,
    )
    assert cli.verify_live(
        args,
        default_upload_lock=tmp_path / "lock",
        exclusive_upload_lock=lock,
        base_adapter_factory=lambda *_args: object(),
        now=lambda: "2026-09-24T00:00:00Z",
    ) == 0
    assert calls == ["verify", "reconcile"]


def _reconciliation_fixture(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[dict[str, object], dict[str, object], dict[str, Path]]:
    authority_path = tmp_path / "authority.json"
    plan_path = tmp_path / "plan.json"
    journal = tmp_path / "journal.jsonl"
    completed_path = tmp_path / "completed.json"
    for path in (authority_path, plan_path, journal):
        path.write_text("{}\n")
    authority = {
        "bvid": "BV1x",
        "cid": 123,
        "target": {"title": "new title"},
    }
    plan = {
        "plan_id": "plan-1",
        "candidate_id": "candidate-1",
        "bvid": "BV1x",
        "aid": 456,
        "unchanged_cid": 123,
        "old_title": "old title",
        "target_title": "new title",
        "replacement_cover": {"path": "cover.png", "sha256": "c" * 64, "bytes": 1},
        "media_identity": {"video_sha256": "v" * 64, "subtitle_sha256": "s" * 64},
        "authority": {
            "path": str(authority_path.resolve()),
            "sha256": "a" * 64,
            "authority_sha256": "b" * 64,
        },
    }
    terminal = {
        "state": "VERIFIED",
        "seq": 9,
        "at": "2026-09-24T00:00:00Z",
        "row_sha256": "r" * 64,
        "details": {"live_snapshot": {"surface": "target", "cid": 123}},
    }
    completed = {
        "schema_version": "same-bv-title-cover-repair-completed.v1",
        "status": "VERIFIED_FRESH_LIVE",
        "remote_mutation": False,
        "candidate_id": plan["candidate_id"],
        "bvid": plan["bvid"],
        "aid": plan["aid"],
        "cid": 123,
        "unchanged_cid": plan["unchanged_cid"],
        "old_title": plan["old_title"],
        "new_title": plan["target_title"],
        "new_cover": plan["replacement_cover"],
        "media_identity": plan["media_identity"],
        "authority": plan["authority"],
        "plan": {
            "path": str(plan_path.resolve()),
            "sha256": hashlib.sha256(plan_path.read_bytes()).hexdigest(),
            "plan_id": plan["plan_id"],
        },
        "verified_journal_row": {
            "journal_path": str(journal.resolve()),
            "seq": terminal["seq"],
            "at": terminal["at"],
            "row_sha256": terminal["row_sha256"],
        },
        "live_snapshot": terminal["details"]["live_snapshot"],
    }
    completed_path.write_text(json.dumps(completed) + "\n")
    monkeypatch.setattr(reconciliation, "load_authority", lambda _path: authority)
    monkeypatch.setattr(reconciliation, "load_plan", lambda _path: plan)
    monkeypatch.setattr(reconciliation, "validate_plan", lambda *_args, **_kwargs: plan)
    monkeypatch.setattr(
        reconciliation,
        "repair_status",
        lambda **_kwargs: SimpleNamespace(state="VERIFIED", message="ok"),
    )
    monkeypatch.setattr(
        reconciliation.journal_binding,
        "plan_rows",
        lambda *_args, **_kwargs: [terminal],
    )
    paths = {
        "authority": authority_path,
        "plan": plan_path,
        "journal": journal,
        "completed": completed_path,
    }
    return completed, terminal, paths


def test_reconciliation_is_create_only_and_identity_preserving(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _completed, _terminal, paths = _reconciliation_fixture(monkeypatch, tmp_path)
    out = tmp_path / "reconciliation.json"
    kwargs = {
        "authority_path": paths["authority"],
        "plan_path": paths["plan"],
        "journal": paths["journal"],
        "completed_path": paths["completed"],
        "out": out,
    }
    receipt = reconciliation.reconcile(
        **kwargs,
        reconciled_at="2026-09-24T00:00:00Z",
    )
    assert receipt["status"] == "CONSUMED"
    assert receipt["projection"]["registry_identity_mutation_required"] is False
    assert reconciliation.reconcile(**kwargs, reconciled_at="later") == receipt


def test_reconciliation_rejects_completed_terminal_row_drift(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    completed, _terminal, paths = _reconciliation_fixture(monkeypatch, tmp_path)
    completed["verified_journal_row"]["row_sha256"] = "x" * 64
    paths["completed"].write_text(json.dumps(completed) + "\n")
    with pytest.raises(
        reconciliation.TitleCoverReconciliationError,
        match="VERIFIED journal row differs",
    ):
        reconciliation.reconcile(
            authority_path=paths["authority"],
            plan_path=paths["plan"],
            journal=paths["journal"],
            completed_path=paths["completed"],
            out=tmp_path / "reconciliation.json",
            reconciled_at="now",
        )


def test_reconciliation_rejects_symlinked_completed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    real = tmp_path / "real.json"
    real.write_text("{}")
    link = tmp_path / "completed.json"
    link.symlink_to(real)
    with pytest.raises(reconciliation.TitleCoverReconciliationError, match="non-symlink"):
        reconciliation.reconcile(
            authority_path=real,
            plan_path=real,
            journal=real,
            completed_path=link,
            out=tmp_path / "out.json",
            reconciled_at="now",
        )
