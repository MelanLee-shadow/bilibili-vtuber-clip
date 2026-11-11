import hashlib
import json

import pytest


SHA = hashlib.sha256(b"media").hexdigest()


def scanner():
    from scripts import scan_state_dangling_media_refs
    return scan_state_dangling_media_refs


def put_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))
    return path


def lease(base, path, *, sha256=SHA, until="2099-01-01T00:00:00Z", lease_id="input"):
    from src.autoslice.evidence_lease import write_lease
    return write_lease(base, lease_id, path=path, sha256=sha256, holder="owner",
                       purpose="bound input", until_utc=until)


def test_result_and_commands_only_declared_inputs_no_prose_scan(tmp_path):
    source = tmp_path / "out/2026-10-11/auto_195637_47_259/source.mp4"
    second = tmp_path / "out/2026-10-11/auto_200311_218_282/source.wav"
    root = tmp_path / "private-runs/four-repair"
    put_json(root / "RESULT.json", {"native-audio": {"source_media": str(source), "source_media_sha256": SHA},
                                    "note": "/ignore/prose.mp4", "output": "/ignore/output.mp4"})
    (root / "COMMANDS.md").write_text(
        'Prose /ignore/prose.mp4\n```json\n' + json.dumps({"inputs": [str(second)]}) + '\n```\n'
        + 'source_media: `/missing/base.mp4`\n'
        + 'inputs = ["/missing/other.wav"]\n'
        + 'inputs: `["/missing/backticked.wav"]`\n'
        + '"inputs": [\n  "/missing/multiline.flac"\n],\n')
    put_json(root / "unrelated.json", {"inputs": ["/ignore/other.mp4"]})
    put_json(tmp_path / "reports/elsewhere/RESULT.json", {"inputs": ["/ignore/report.mp4"]})
    rows = scanner().unleased_bindings(tmp_path)
    assert {r["bound_path"] for r in rows} == {
        str(source), str(second), "/missing/base.mp4", "/missing/other.wav",
        "/missing/backticked.wav", "/missing/multiline.flac"}
    assert all(r["reason"] == "no-active-lease" for r in rows)
    assert next(r for r in rows if r["bound_path"] == str(source))["sha256"] == SHA
    lease(tmp_path, source)
    assert str(source) not in {r["bound_path"] for r in scanner().unleased_bindings(tmp_path)}


def test_stage_request_plan_completed_hash_bindings_and_cli(tmp_path, capsys):
    base = tmp_path / "runtime"
    stages = tmp_path / "administrative/subtitle-repair/02"
    source = stages / "base.mp4"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"media")
    binding = {"path": str(source), "sha256": SHA, "bytes": 5}
    for kind in ("request", "plan", "completed"):
        put_json(stages / f"{kind}.json", {"schema_version": f"same-bv-stage-{kind}.v1",
                                         "nested": {"video": binding}, "note": "/ignore/note.mp4"})
    assert scanner().unleased_bindings(base) == []
    rows = scanner().unleased_bindings(base, stage_roots=[stages])
    assert len(rows) == 3 and {row["bound_path"] for row in rows} == {str(source)}
    destination = tmp_path / "scan.json"
    assert scanner().main(["--base", str(base), "--unleased-bindings", "--stage-root", str(stages),
                           "--json", str(destination)]) == 0
    assert json.loads(destination.read_text()) == rows
    assert "unleased structured bindings: 3" in capsys.readouterr().out
    lease(base, source, sha256="0" * 64)
    assert {r["reason"] for r in scanner().unleased_bindings(base, stage_roots=[stages])} == {"sha256-mismatch"}
    lease(base, source)
    assert scanner().unleased_bindings(base, stage_roots=[stages]) == []
    lease(base, source, until="2000-01-01T00:00:00Z")
    assert len(scanner().unleased_bindings(base, stage_roots=[stages])) == 3


def test_directory_lease_covers_nested_inputs_and_symlink_sources_are_skipped(tmp_path):
    root = tmp_path / "private-runs/reaction-nanaliya-20261023/manual/out-r3"
    source = root / "clip.mp4"
    put_json(root.parent.parent / "RESULT.json", {"inputs": [{"path": str(source), "sha256": SHA}]})
    outside = put_json(tmp_path / "outside.json", {"inputs": ["/ignore/link.mp4"]})
    (root.parent.parent / "alias").symlink_to(tmp_path, target_is_directory=True)
    (root.parent.parent / "COMMANDS.md").symlink_to(outside)
    assert len(scanner().unleased_bindings(tmp_path)) == 1
    lease(tmp_path, root)
    assert scanner().unleased_bindings(tmp_path) == []


def test_original_dangling_state_mode_is_preserved(tmp_path, capsys, monkeypatch):
    import sys

    put_json(tmp_path / "state/2026-10-01.json", {"picks": [{"candidate_id": "c1", "status": "failed",
                                                            "video_path": str(tmp_path / "out/gone.mp4")}]})
    destination = tmp_path / "dangling.json"
    monkeypatch.setattr(sys, "argv", ["scan_state_dangling_media_refs.py", "--base", str(tmp_path),
                                     "--json", str(destination)])
    assert scanner().main() == 0
    assert "dangling media references: 1" in capsys.readouterr().out
    assert json.loads(destination.read_text())[0]["cls"] == "B_cleanup_deleted"


@pytest.mark.parametrize("shape", ["result", "commands", "stage-request", "stage-plan", "stage-completed"])
@pytest.mark.parametrize("declared_digest", ["bare", "prefixed", "different-bare", "different-prefixed"])
def test_persisted_lease_digest_value_across_declarations(tmp_path, capsys, shape, declared_digest):
    base = tmp_path / "runtime"
    media = base / "out/new-work/input.mp4"
    media.parent.mkdir(parents=True)
    media.write_bytes(b"same-media")
    digest = hashlib.sha256(media.read_bytes()).hexdigest()
    different = hashlib.sha256(b"different-media").hexdigest()
    value = different if declared_digest.startswith("different-") else digest
    if declared_digest.endswith("prefixed"):
        value = "sha256:" + value
    options = {}
    cli_options = []
    if shape == "result":
        source = put_json(base / "private-runs/new-work/RESULT.json",
                          {"native-audio": {"source_media": str(media), "source_media_sha256": value}})
    elif shape == "commands":
        source = base / "private-runs/new-work/COMMANDS.md"
        source.parent.mkdir(parents=True)
        source.write_text('```json\n' + json.dumps({"inputs": [{"path": str(media), "sha256": value}]})
                          + '\n```\n')
    else:
        stages = tmp_path / "administrative/new-work"
        source = put_json(stages / "request.json",
                          {"schema_version": f"same-bv-{shape}.v1",
                           "nested": {"video": {"path": str(media), "sha256": value}}})
        options["stage_roots"] = [stages]
        cli_options = ["--stage-root", str(stages)]
    source_bytes = source.read_bytes()
    cli = scanner()
    assert {row["reason"] for row in cli.unleased_bindings(base, **options)} == {"no-active-lease"}
    persisted = lease(base, media, sha256=digest)
    lease_bytes = persisted.read_bytes()
    assert json.loads(lease_bytes)["sha256"] == digest
    destination = tmp_path / "scan.json"
    assert cli.main(["--base", str(base), "--unleased-bindings", *cli_options,
                     "--json", str(destination)]) == 0
    rows = json.loads(destination.read_text())
    if declared_digest.startswith("different-"):
        assert len(rows) == 1
        assert rows[0]["reason"] == "sha256-mismatch"
        assert rows[0]["sha256"] == value  # Audit preserves the original spelling.
        assert rows[0]["exists"] is True
    else:
        assert rows == []
    capsys.readouterr()
    assert source.read_bytes() == source_bytes
    assert persisted.read_bytes() == lease_bytes
    assert hashlib.sha256(media.read_bytes()).hexdigest() == digest
