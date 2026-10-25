"""Execute the real post-commit hook without a remote runtime."""
import json
from pathlib import Path
import subprocess
import sys

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/deploy_oci3_shadow_autoslice.sh'


def run(tmp_path, mode=None, installer_exit=None):
    base = tmp_path / 'base'
    base.mkdir(exist_ok=True)
    repo = base / 'repo'
    repo.mkdir(exist_ok=True)
    (repo / 'DEPLOYED_COMMIT').write_text('a' * 40)
    if mode is not None:
        (base / 'execution-backends.json').write_text(json.dumps({'mode': mode}))
    if installer_exit is not None:
        (base / 'venv-main/bin').mkdir(parents=True)
        (base / 'venv-main/bin/python').symlink_to(sys.executable)
        (repo / 'scripts').mkdir()
        (repo / 'scripts/sync_overflow_runtime.py').write_text(
            "import sys\nfrom pathlib import Path\n"
            "Path(sys.argv[sys.argv.index('--base')+1], 'installer-ran').write_text('yes')\n"
            f"sys.exit({installer_exit})\n"
        )
    source = SCRIPT.read_text()
    body = source.split("<<'POST_COMMIT_OVERFLOW'\n", 1)[1].split('\nPOST_COMMIT_OVERFLOW', 1)[0]
    proc = subprocess.run([sys.executable, '-', str(base), 'a' * 40, 'test-owner'], input=body,
                          text=True, capture_output=True)
    receipt = json.loads((base / '.oci3-shadow-upload-test-owner/overflow-status.json').read_text())
    assert (repo / 'DEPLOYED_COMMIT').read_text() == 'a' * 40
    assert receipt['main_status'] == 'COMMITTED'
    return proc, receipt, base


def test_post_commit_sync_success_and_missing_installation(tmp_path):
    proc, receipt, base = run(tmp_path, 'auto', 0)
    assert proc.returncode == 0 and receipt['status'] == 'SYNCED'
    assert (base / 'installer-ran').is_file()
    other = tmp_path / 'missing'; other.mkdir()
    proc, receipt, _ = run(other, 'remote')
    assert proc.returncode == 9 and receipt['reason'] == 'OVERFLOW_INSTALLER_UNAVAILABLE'


def test_component_failure_never_relabels_main_and_local_skips(tmp_path):
    proc, receipt, _ = run(tmp_path, 'remote', 17)
    assert proc.returncode == 9 and receipt['installer_exit_code'] == 17
    other = tmp_path / 'local'; other.mkdir()
    proc, receipt, base = run(other, 'local')
    assert proc.returncode == 0 and receipt['status'] == 'LOCAL_MODE'
    assert not (base / 'installer-ran').exists()
    source = SCRIPT.read_text()
    # Main apply is one completed SSH; the component has no enclosing EX lease.
    assert source.index('remote "$APPLY_MODE" "$OWNER"') < source.index('COMMITTED=1') < source.index("<<'POST_COMMIT_OVERFLOW'")
