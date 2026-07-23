from pathlib import Path

from ops.recording import patch_autoslice_runner_recorder_status as patcher


def test_transform_is_narrow_and_idempotent(tmp_path: Path) -> None:
    source = (
        "before\n"
        + patcher.OLD_CONSTANT
        + "\n"
        + patcher.OLD_FUNCTION
        + "\n"
        + "def tick():\n"
        + "    live = blrec_live_status()\n"
        + '            write_heartbeat("live=? source=ok (blrec API unavailable — fail-safe skip)")\n'
        + "after\n"
    )

    patched, changed = patcher.transform(source)
    repeated, repeated_changes = patcher.transform(patched)

    assert changed == 4
    assert repeated_changes == 0
    assert repeated == patched
    assert "before\n" in patched and "after\n" in patched
    assert "BLREC_PORT" not in patched
    assert "def recorder_live_status()" in patched
