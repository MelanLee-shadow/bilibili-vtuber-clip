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
bodies = [json.load(open(path, encoding="utf-8")) for path in body_refs]
capture_path = os.environ["CURL_CAPTURE"]
previous = {{}}
if os.path.exists(capture_path):
    previous = json.load(open(capture_path, encoding="utf-8"))
capture = {{
    "bodies": previous.get("bodies", []) + bodies,
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

out_path = ""
for index, arg in enumerate(args[:-1]):
    if arg == "-o":
        out_path = args[index + 1]
write_out = any(arg == "-w" for arg in args)

# FAKE_CURL_STATUS_SEQUENCE drives one HTTP status per invocation ("" reuses the
# last entry once exhausted); "200" means a normal completion body.
sequence = [s for s in os.environ.get("FAKE_CURL_STATUS_SEQUENCE", "").split(",") if s]
status = ""
if sequence:
    status = sequence[min(capture["invocation_count"] - 1, len(sequence) - 1)]

if os.environ.get("FAKE_CURL_FAIL") == "1" and not status:
    raise SystemExit(22)

if status and status != "200":
    if out_path:
        open(out_path, "w", encoding="utf-8").write(
            json.dumps({{"error": {{"message": "injected", "code": "injected"}}}})
        )
    if write_out:
        sys.stdout.write(status)
    raise SystemExit(22)

body = json.dumps({{"status": "completed", "output_text": "safe completion"}})
if out_path:
    open(out_path, "w", encoding="utf-8").write(body)
    if write_out:
        sys.stdout.write("200")
else:
    sys.stdout.write(body)
""",
        encoding="utf-8",
    )
    fake_curl.chmod(0o755)

    # Backoff must be observable without making the suite sleep.
    fake_sleep = tool_dir / "sleep"
    fake_sleep.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys, time\n"
        "path = os.environ.get('SLEEP_CAPTURE')\n"
        "if path:\n"
        "    open(path, 'a', encoding='utf-8').write(sys.argv[1] + '\\n')\n"
        "if os.environ.get('SLEEP_REAL') == '1':\n"
        "    time.sleep(float(sys.argv[1]))\n",
        encoding="utf-8",
    )
    fake_sleep.chmod(0o755)
    return tool_dir, fake_tmp_root


def _run_bridge(
    tmp_path: Path,
    *,
    curl_fails: bool = False,
    chat_models_env: str | None = "gpt-test",
    extra_args: tuple[str, ...] = (),
    status_sequence: tuple[str, ...] = (),
    env_overrides: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], dict, Path, Path]:
    tool_dir, fake_tmp_root = _write_fake_tools(tmp_path)
    prompt = tmp_path / "prompt.txt"
    completion = tmp_path / "completion.txt"
    capture_path = tmp_path / "curl-capture.json"
    prompt.write_text("return a safe completion", encoding="utf-8")

    env = os.environ.copy()
    env.pop("CPA_CHAT_MODELS", None)
    env.pop("CPA_CHAT_MODEL", None)
    env.pop("CPA_REASONING_EFFORT", None)
    for key in (
        "CPA_TRANSIENT_ATTEMPTS_PER_MODEL",
        "CPA_BACKOFF_BASE_SECONDS",
        "CPA_BACKOFF_MAX_TOTAL_SECONDS",
        "CPA_DEADLINE_SECONDS",
    ):
        env.pop(key, None)
    env.update(
        {
            "PATH": f"{tool_dir}{os.pathsep}{env['PATH']}",
            "CPA_BASE_URL": "https://cpa.invalid/v1",
            "CPA_API_KEY": SENTINEL_SECRET,
            "CURL_CAPTURE": str(capture_path),
            "FAKE_TMP_ROOT": str(fake_tmp_root),
            "SLEEP_CAPTURE": str(tmp_path / "sleep-capture.txt"),
            # Backoff behaviour is asserted through the stubbed sleep; keep the
            # wall clock out of the suite.
            "CPA_BACKOFF_BASE_SECONDS": "0",
        }
    )
    if status_sequence:
        env["FAKE_CURL_STATUS_SEQUENCE"] = ",".join(status_sequence)
    if env_overrides:
        env.update(env_overrides)
    if chat_models_env is not None:
        env["CPA_CHAT_MODELS"] = chat_models_env
    if curl_fails:
        env["FAKE_CURL_FAIL"] = "1"

    completed = subprocess.run(
        [
            "bash",
            "-c",
            'umask 000; script="$1"; shift; exec bash -x "$script" "$@"',
            "llm-via-cpa-test",
            str(SCRIPT),
            str(prompt),
            str(completion),
            *extra_args,
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


def test_default_chain_is_sol_then_55_then_54_medium(tmp_path):
    """No env/argv override → the 2026-07-10 default chain, 3 attempts each."""
    completed, capture, _completion, _tmp = _run_bridge(
        tmp_path, curl_fails=True, chat_models_env=None
    )

    assert completed.returncode == 1
    models = [body["model"] for body in capture["bodies"]]
    assert models == ["gpt-5.6-sol"] * 3 + ["gpt-5.5"] * 3 + ["gpt-5.4"] * 3
    assert {body["reasoning"]["effort"] for body in capture["bodies"]} == {"medium"}


def test_argv_chain_and_effort_override_env(tmp_path):
    """Per-stage argv 3/4 (quoted chain + effort) beat CPA_CHAT_MODELS env."""
    completed, capture, completion, _tmp = _run_bridge(
        tmp_path,
        chat_models_env="gpt-env-model",
        extra_args=("stage-model-a stage-model-b", "high"),
    )

    assert completed.returncode == 0, completed.stderr
    assert completion.read_text(encoding="utf-8") == "safe completion"
    assert [body["model"] for body in capture["bodies"]] == ["stage-model-a"]
    assert capture["bodies"][0]["reasoning"]["effort"] == "high"


def test_stage_can_bound_one_attempt_per_model_to_fit_outer_deadline(tmp_path):
    """attempts_per_model=1 still means one attempt per model.

    2026-08-10 起服务类失败另有一条 transient floor（见下面的用例），所以这条
    "一模型一枪"的原始保证要显式关掉 floor 才成立；调用方的 600s 外层期限
    现在由 ``CPA_DEADLINE_SECONDS`` 独立守住。
    """

    completed, capture, completion, _tmp = _run_bridge(
        tmp_path,
        curl_fails=True,
        chat_models_env="gpt-env-model",
        extra_args=("stage-model-a stage-model-b", "medium", "1"),
        env_overrides={"CPA_TRANSIENT_ATTEMPTS_PER_MODEL": "1"},
    )

    assert completed.returncode == 1
    assert not completion.exists()
    assert [body["model"] for body in capture["bodies"]] == [
        "stage-model-a",
        "stage-model-b",
    ]


def _sleeps(tmp_path: Path) -> list[int]:
    path = tmp_path / "sleep-capture.txt"
    if not path.exists():
        return []
    return [int(line) for line in path.read_text(encoding="utf-8").split()]


def test_service_class_failures_retry_the_same_model_and_then_succeed(tmp_path):
    """金丝雀①：注入 503 序列 → 退避重试后成功（修复前是整条候选判死）。

    终审面按 attempts_per_model=1 调用；2026-08-10 上游落到次级 leg 后
    503/408 混着来，一枪打空就把 15–55 分钟的 produce 判死。服务类失败必须
    自带 transient floor。
    """

    completed, capture, completion, _tmp = _run_bridge(
        tmp_path,
        chat_models_env="gpt-test",
        extra_args=("gpt-test", "medium", "1"),
        status_sequence=("503", "503", "200"),
    )

    assert completed.returncode == 0, completed.stderr
    assert completion.read_text(encoding="utf-8") == "safe completion"
    assert capture["invocation_count"] == 3
    assert "http=503" in completed.stderr


def test_service_class_failure_without_floor_fails_closed(tmp_path):
    """金丝雀①的反面：关掉 floor（=修复前语义）同一序列必红。"""

    completed, _capture, completion, _tmp = _run_bridge(
        tmp_path,
        chat_models_env="gpt-test",
        extra_args=("gpt-test", "medium", "1"),
        status_sequence=("503", "503", "200"),
        env_overrides={"CPA_TRANSIENT_ATTEMPTS_PER_MODEL": "1"},
    )

    assert completed.returncode == 1
    assert not completion.exists()


def test_service_class_retry_backs_off_with_jitter(tmp_path):
    completed, _capture, completion, _tmp = _run_bridge(
        tmp_path,
        chat_models_env="gpt-test",
        extra_args=("gpt-test", "medium", "1"),
        status_sequence=("503", "503", "200"),
        env_overrides={"CPA_BACKOFF_BASE_SECONDS": "2"},
    )

    assert completed.returncode == 0, completed.stderr
    slept = _sleeps(tmp_path)
    assert len(slept) == 2
    # base<<(attempt-1) plus [0, delay] jitter, clamped by the total budget.
    assert 2 <= slept[0] <= 4
    assert 4 <= slept[1] <= 8


def test_backoff_total_budget_is_bounded(tmp_path):
    completed, _capture, _completion, _tmp = _run_bridge(
        tmp_path,
        chat_models_env="gpt-a gpt-b",
        status_sequence=("503",),
        env_overrides={
            "CPA_BACKOFF_BASE_SECONDS": "8",
            "CPA_BACKOFF_MAX_TOTAL_SECONDS": "10",
        },
    )

    assert completed.returncode == 1
    assert sum(_sleeps(tmp_path)) <= 10


def test_deterministic_rejection_fails_over_without_same_model_retry(tmp_path):
    """金丝雀②：注入 400 → 立即换模型，不在同一模型上空转重试。

    2026-08-10 的 400 是次级 leg 的 ``group_capability_unavailable``：同一请求
    再发一次仍然被拒，重试只是浪费时间与配额。
    """

    completed, capture, completion, _tmp = _run_bridge(
        tmp_path,
        chat_models_env="gpt-a gpt-b gpt-c",
        status_sequence=("400", "400", "400"),
    )

    assert completed.returncode == 1
    assert not completion.exists()
    assert [body["model"] for body in capture["bodies"]] == [
        "gpt-a",
        "gpt-b",
        "gpt-c",
    ]
    assert "no same-model retry" in completed.stderr
    assert _sleeps(tmp_path) == []


def test_quota_status_is_retried_and_reported_in_the_cascade(tmp_path):
    """金丝雀③：429 走等待重试（配额窗口会重开），并在级联里写清状态码。

    key 轮换是 CPA 侧既有策略（fill-first + 多凭据），桥接层不另造一套；这里
    只保证 429 不被当成"确定性拒绝"直接放弃，且状态码留在 stderr 上供上层
    归类成 quota。
    """

    completed, capture, completion, _tmp = _run_bridge(
        tmp_path,
        chat_models_env="gpt-test",
        extra_args=("gpt-test", "medium", "1"),
        status_sequence=("429", "200"),
    )

    assert completed.returncode == 0, completed.stderr
    assert completion.read_text(encoding="utf-8") == "safe completion"
    assert capture["invocation_count"] == 2
    assert "http=429" in completed.stderr


def test_every_attempt_reports_its_status_in_stderr(tmp_path):
    completed, _capture, _completion, _tmp = _run_bridge(
        tmp_path,
        chat_models_env="gpt-a gpt-b",
        status_sequence=("503", "503", "503", "400"),
        env_overrides={"CPA_TRANSIENT_ATTEMPTS_PER_MODEL": "3"},
    )

    assert completed.returncode == 1
    lines = [line for line in completed.stderr.splitlines() if line.startswith("[cpa] ")]
    attempt_lines = [line for line in lines if "attempt=" in line]
    assert len(attempt_lines) == 4
    assert attempt_lines[0].startswith("[cpa] model=gpt-a attempt=1 http=503 ")
    assert attempt_lines[3].startswith("[cpa] model=gpt-b attempt=1 http=400 ")


def test_deadline_gates_every_dispatch_not_just_same_model_retries(tmp_path):
    """外层 600s 期限由 deadline 独立守住，而不是靠 attempts_per_model=1。

    transient floor 会让单个模型最多打满 3 枪；如果 deadline 只拦"同模型重试"，
    第一个模型烧掉 3×180s 之后下一个模型仍会起一枪，调用方 600s 一到就把桥
    拦腰砍断，后两个模型一枪都没打成——比修复前更糟。所以 deadline 必须拦住
    **每一次** 派发（第一次除外，保证至少打一枪）。这里用真实 sleep 把
    wall-clock 推过 deadline 来验证。
    """

    completed, capture, _completion, _tmp = _run_bridge(
        tmp_path,
        chat_models_env="gpt-a gpt-b gpt-c",
        status_sequence=("503",),
        env_overrides={
            "CPA_DEADLINE_SECONDS": "1",
            "CPA_BACKOFF_BASE_SECONDS": "2",
            "SLEEP_REAL": "1",
        },
    )

    assert completed.returncode == 1
    # One attempt happened, backoff pushed the clock past the deadline, and no
    # further dispatch (same model or next model) was allowed.
    assert capture["invocation_count"] == 1
    assert [body["model"] for body in capture["bodies"]] == ["gpt-a"]
    assert "deadline 1s reached" in completed.stderr


def test_first_attempt_always_runs_even_with_a_zero_length_budget(tmp_path):
    completed, capture, completion, _tmp = _run_bridge(
        tmp_path,
        chat_models_env="gpt-a",
        env_overrides={"CPA_DEADLINE_SECONDS": "1", "SLEEP_REAL": "1"},
    )

    assert completed.returncode == 0, completed.stderr
    assert completion.read_text(encoding="utf-8") == "safe completion"
    assert capture["invocation_count"] == 1
