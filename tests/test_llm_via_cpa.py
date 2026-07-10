import json
import os
import stat
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "llm_via_cpa.sh"
SENTINEL_SECRET = "test-cpa-secret-must-not-leak-7f3a"


def _write_fake_tools(tmp_path: Path) -> tuple[Path, Path]:
    tool_dir = tmp_path / "bin"
    tool_dir.mkdir()

    fake_tmp_root = tmp_path / "temporary-files"
    fake_tmp_root.mkdir()
    fake_mktemp = tool_dir / "mktemp"
    fake_mktemp.write_text(
        "#!/usr/bin/env python3\n"
        "import os, tempfile\n"
        "fd, path = tempfile.mkstemp(dir=os.environ['FAKE_TMP_ROOT'])\n"
        "os.close(fd)\n"
        "print(path)\n",
        encoding="utf-8",
    )
    fake_mktemp.chmod(0o755)

    fake_curl = tool_dir / "curl"
    fake_curl.write_text(
        f"""#!/usr/bin/env python3
import json
import os
import stat
import sys

SECRET = {SENTINEL_SECRET!r}
args = sys.argv[1:]
header_refs = [
    args[index + 1][1:]
    for index, arg in enumerate(args[:-1])
    if arg == "-H" and args[index + 1].startswith("@")
]
body_refs = [
    args[index + 1][1:]
    for index, arg in enumerate(args[:-1])
    if arg == "-d" and args[index + 1].startswith("@")
]
header_path = header_refs[0] if len(header_refs) == 1 else ""
headers = open(header_path, encoding="utf-8").read().splitlines() if header_path else []
capture_path = os.environ["CURL_CAPTURE"]
previous = {{}}
if os.path.exists(capture_path):
    previous = json.load(open(capture_path, encoding="utf-8"))
capture = {{
    "invocation_count": previous.get("invocation_count", 0) + 1,
    "argv": args,
    "secret_in_argv": previous.get("secret_in_argv", False) or any(SECRET in arg for arg in args),
    "secret_in_environment": previous.get("secret_in_environment", False) or any(
        SECRET in value for value in os.environ.values()
    ),
    "header_ref_count": len(header_refs),
    "header_path": header_path,
    "header_mode": stat.S_IMODE(os.stat(header_path).st_mode) if header_path else None,
    "authorization_ok": f"Authorization: Bearer {{SECRET}}" in headers,
    "content_type_ok": "Content-Type: application/json" in headers,
    "user_agent_ok": any(line.startswith("User-Agent: Mozilla/5.0") for line in headers),
    "body_paths": body_refs,
}}
json.dump(capture, open(capture_path, "w", encoding="utf-8"))
if os.environ.get("FAKE_CURL_FAIL") == "1":
    raise SystemExit(22)
sys.stdout.write(json.dumps({{"status": "completed", "output_text": "safe completion"}}))
""",
        encoding="utf-8",
    )
    fake_curl.chmod(0o755)
    return tool_dir, fake_tmp_root


def _run_bridge(tmp_path: Path, *, curl_fails: bool = False) -> tuple[subprocess.CompletedProcess[str], dict, Path, Path]:
    tool_dir, fake_tmp_root = _write_fake_tools(tmp_path)
    prompt = tmp_path / "prompt.txt"
    completion = tmp_path / "completion.txt"
    capture_path = tmp_path / "curl-capture.json"
    prompt.write_text("return a safe completion", encoding="utf-8")

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{tool_dir}{os.pathsep}{env['PATH']}",
            "CPA_BASE_URL": "https://cpa.invalid/v1",
            "CPA_API_KEY": SENTINEL_SECRET,
            "CPA_CHAT_MODELS": "gpt-test",
            "CURL_CAPTURE": str(capture_path),
            "FAKE_TMP_ROOT": str(fake_tmp_root),
        }
    )
    if curl_fails:
        env["FAKE_CURL_FAIL"] = "1"

    completed = subprocess.run(
        [
            "bash",
            "-c",
            'umask 000; exec bash -x "$1" "$2" "$3"',
            "llm-via-cpa-test",
            str(SCRIPT),
            str(prompt),
            str(completion),
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    capture = json.loads(capture_path.read_text(encoding="utf-8"))
    return completed, capture, completion, fake_tmp_root


def _assert_secret_is_not_observable(completed: subprocess.CompletedProcess[str], capture: dict) -> None:
    assert SENTINEL_SECRET not in completed.stdout
    assert SENTINEL_SECRET not in completed.stderr
    assert capture["secret_in_argv"] is False
    assert capture["secret_in_environment"] is False


def test_cpa_bearer_uses_private_header_file_not_curl_argv(tmp_path):
    completed, capture, completion, fake_tmp_root = _run_bridge(tmp_path)

    assert completed.returncode == 0, completed.stderr
    assert completion.read_text(encoding="utf-8") == "safe completion"
    assert stat.S_IMODE(completion.stat().st_mode) == 0o600
    assert capture["header_ref_count"] == 1
    assert capture["authorization_ok"] is True
    assert capture["content_type_ok"] is True
    assert capture["user_agent_ok"] is True
    assert capture["header_mode"] == 0o600
    assert "Authorization:" not in "\0".join(capture["argv"])
    _assert_secret_is_not_observable(completed, capture)
    assert not Path(capture["header_path"]).exists()
    assert list(fake_tmp_root.iterdir()) == []


def test_cpa_failure_does_not_print_secret_and_cleans_temp_files(tmp_path):
    completed, capture, completion, fake_tmp_root = _run_bridge(tmp_path, curl_fails=True)

    assert completed.returncode == 1
    assert capture["invocation_count"] == 3
    assert not completion.exists()
    _assert_secret_is_not_observable(completed, capture)
    assert not Path(capture["header_path"]).exists()
    assert list(fake_tmp_root.iterdir()) == []
