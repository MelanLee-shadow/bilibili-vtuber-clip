import json
import os
import stat
import subprocess
from pathlib import Path

import pytest


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
# last entry once exhausted); "200" means a normal completion body and "empty"
# means HTTP 200 carrying the reasoning-model empty-output_text quirk.
sequence = [s for s in os.environ.get("FAKE_CURL_STATUS_SEQUENCE", "").split(",") if s]
status = ""
if sequence:
    status = sequence[min(capture["invocation_count"] - 1, len(sequence) - 1)]

if os.environ.get("FAKE_CURL_FAIL") == "1" and not status:
    raise SystemExit(22)

if status == "empty":
    if out_path:
        open(out_path, "w", encoding="utf-8").write(
            json.dumps({{"status": "completed", "output_text": ""}})
        )
    if write_out:
        sys.stdout.write("200")
    raise SystemExit(0)

# Representative upstream group-capability error payload used by this regression.
# 三个 token 拆出三种字样，验证桥接层三种都认：
#   400gc   = 线上完整形状（中文 message + 机器码 code + metadata.message_en）
#   400gczh = 只有中文 message（无机器码）
#   400gcen = 只有英文机器码 + 英文 message
# 另外 <code>gcbody 形式（如 403gcbody）= 非 400 的状态码配同一正文，用来钉住
# "必须 http=400 与正文同时成立"的与门。
GROUPCAP_ZH = (
    "当前分组不支持本次请求所需"
    "能力，请调整请求或切换分组"
    "后重试。 (request id: 20260810-test-req-id)"
)
GROUPCAP_EN = (
    "The current group does not support the capability required by this request. "
    "Please adjust the request or switch groups."
)
GROUPCAP_FULL = json.dumps(
    dict(
        error=dict(
            message=GROUPCAP_ZH,
            type="invalid_request_error",
            param=None,
            code="group_capability_unavailable",
            metadata=dict(message_en=GROUPCAP_EN, message_zh=GROUPCAP_ZH),
        )
    ),
    ensure_ascii=False,
)
GROUPCAP_ZH_ONLY = json.dumps(
    dict(error=dict(message=GROUPCAP_ZH, type="invalid_request_error")),
    ensure_ascii=False,
)
GROUPCAP_EN_ONLY = json.dumps(
    dict(
        error=dict(
            message=GROUPCAP_EN,
            type="invalid_request_error",
            code="group_capability_unavailable",
        )
    ),
    ensure_ascii=False,
)
GROUPCAP_BODIES = dict(
    gc=GROUPCAP_FULL, gczh=GROUPCAP_ZH_ONLY, gcen=GROUPCAP_EN_ONLY, gcbody=GROUPCAP_FULL
)

# "<code>nobody" = 收到了状态码但 curl 没往 -o 写任何正文（连接在响应体阶段
# 断掉）。用来钉住"每枪先清空 RESP_FILE"，否则上一枪的分组正文会给这一枪定性。
if status.endswith("nobody"):
    if write_out:
        sys.stdout.write(status[: -len("nobody")])
    raise SystemExit(22)

groupcap_suffix = ""
for suffix in ("gcbody", "gczh", "gcen", "gc"):
    if status.endswith(suffix):
        groupcap_suffix = suffix
        break
if groupcap_suffix:
    if out_path:
        open(out_path, "w", encoding="utf-8").write(GROUPCAP_BODIES[groupcap_suffix])
    if write_out:
        sys.stdout.write(status[: -len(groupcap_suffix)])
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
    """No env/argv override → the default chain, 3 attempts each."""
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

    终审面按 attempts_per_model=1 调用；上游落到次级 leg 后
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

    的 400 是次级 leg 的 ``group_capability_unavailable``：同一请求
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


def _cpa_lines(completed: subprocess.CompletedProcess[str]) -> list[str]:
    """只取桥接自己打的 ``[cpa] `` 行。

    测试用 ``bash -x`` 跑脚本，于是 group_capability_body() 的 grep 命令行
    （连同 ``group_capability_unavailable`` 模式串）每次 400 都会被 trace 到
    stderr 上。直接对整块 stderr 做子串断言会被这条 trace 蒙混过关，必须先把
    脚本自己的日志行筛出来。
    """

    return [line for line in completed.stderr.splitlines() if line.startswith("[cpa] ")]


def test_group_capability_400_retries_the_same_model_and_then_succeeds(tmp_path):
    """金丝雀⑤：分组抽签 400 必须退避重试，不是判死候选。

    上游 sudocode 把凭据分成两组（维护者 裁定 #8：一组带 gpt-image 能力、一组带
    gpt-5.6-sol 能力）。请求轮询落到没有该能力的分组就 400
    ``group_capability_unavailable``——**请求本身合法**，原样重发就可能落到对
    的分组。实测单次失败率 ~15–17%，与 payload 大小无关（0B 失败而 2000B 成
    功，非单调）、与模型无关（sol 5/6、gpt-5.5 5/6、gpt-5.4 6/6）。

    修复前 ``transient_failure()`` 的 ``4??) return 1`` 把它判成确定性拒绝，
    一次抽签失败就把整条候选打死；本用例在修复前必红（第一枪 400 直接换模型，
    第二枪打在 gpt-second 上）。
    """

    completed, capture, completion, _tmp = _run_bridge(
        tmp_path,
        chat_models_env="gpt-first gpt-second",
        extra_args=("gpt-first gpt-second", "medium", "1"),
        status_sequence=("400gc", "400gc", "200"),
    )

    assert completed.returncode == 0, completed.stderr
    assert completion.read_text(encoding="utf-8") == "safe completion"
    assert capture["invocation_count"] == 3
    # 三枪全打在同一个模型上：抽签失败不该烧掉失效备援。
    assert [body["model"] for body in capture["bodies"]] == ["gpt-first"] * 3


def test_group_capability_400_without_the_retry_fix_burns_the_whole_chain(tmp_path):
    """反面：同一序列若只给每个模型一枪（=修复前语义），整条链打空判死。"""

    completed, capture, completion, _tmp = _run_bridge(
        tmp_path,
        chat_models_env="gpt-first gpt-second",
        extra_args=("gpt-first gpt-second", "medium", "1"),
        status_sequence=("400gc", "400gc", "400gc"),
        env_overrides={"CPA_TRANSIENT_ATTEMPTS_PER_MODEL": "1"},
    )

    assert completed.returncode == 1
    assert not completion.exists()
    assert [body["model"] for body in capture["bodies"]] == ["gpt-first", "gpt-second"]


@pytest.mark.parametrize("token", ["400gc", "400gczh", "400gcen"])
def test_group_capability_400_is_recognised_from_either_language(tmp_path, token):
    """中文正文、英文机器码、两者齐全——三种字样都要认。

    线上响应体同时带 ``"code":"group_capability_unavailable"``、中文
    ``当前分组不支持…`` 和 ``metadata.message_en``；但不能假设三者永远同时出现，
    所以任一出现即成立。
    """

    completed, capture, completion, _tmp = _run_bridge(
        tmp_path,
        chat_models_env="gpt-only",
        extra_args=("gpt-only", "medium", "1"),
        status_sequence=(token, "200"),
    )

    assert completed.returncode == 0, completed.stderr
    assert completion.read_text(encoding="utf-8") == "safe completion"
    assert capture["invocation_count"] == 2


def test_group_capability_400_puts_its_code_on_the_stderr_cascade(tmp_path):
    """stderr 上必须留下 token：上层分类器只看得见 stderr，看不见响应体。

    ``src/autoslice/provider_failure.py`` 靠这个 token 把这类 400 归到
    service 而不是 rejected；不写进 stderr，那边的归类就是死代码。
    """

    completed, _capture, _completion, _tmp = _run_bridge(
        tmp_path,
        chat_models_env="gpt-only",
        extra_args=("gpt-only", "medium", "1"),
        status_sequence=("400gc",),
    )

    assert completed.returncode == 1
    attempt_lines = [line for line in _cpa_lines(completed) if "attempt=" in line]
    assert attempt_lines
    assert all("http=400" in line for line in attempt_lines)
    assert all(
        "upstream_code=group_capability_unavailable" in line for line in attempt_lines
    )


def test_plain_400_carries_no_group_capability_token_and_never_retries(tmp_path):
    """反向门①：不带分组字样的普通 400 仍是确定性拒绝，立刻换模型。"""

    completed, capture, completion, _tmp = _run_bridge(
        tmp_path,
        chat_models_env="gpt-a gpt-b gpt-c",
        status_sequence=("400",),
    )

    assert completed.returncode == 1
    assert not completion.exists()
    assert [body["model"] for body in capture["bodies"]] == ["gpt-a", "gpt-b", "gpt-c"]
    assert "no same-model retry" in completed.stderr
    assert _sleeps(tmp_path) == []
    assert not any("upstream_code=" in line for line in _cpa_lines(completed))


@pytest.mark.parametrize("status", ["401", "403", "404", "422"])
def test_other_4xx_rejections_are_still_not_retried(tmp_path, status):
    """反向门②：401/403/404/422 照旧不重试——没有把 4xx 一把放开。"""

    completed, capture, completion, _tmp = _run_bridge(
        tmp_path,
        chat_models_env="gpt-a gpt-b gpt-c",
        status_sequence=(status,),
    )

    assert completed.returncode == 1
    assert not completion.exists()
    # 每个模型恰好一枪，没有同模型重试。
    assert [body["model"] for body in capture["bodies"]] == ["gpt-a", "gpt-b", "gpt-c"]
    assert "no same-model retry" in completed.stderr
    assert _sleeps(tmp_path) == []


def test_group_capability_body_on_a_non_400_status_is_still_a_rejection(tmp_path):
    """反向门③：与门必须是 http=400 **且** 正文匹配。

    正文是上游可控的，状态码不是。若只按正文放行，一个 403（凭据被吊销/UA 被
    Cloudflare 拦）只要正文里出现同样字样就会被无限重试。
    """

    completed, capture, completion, _tmp = _run_bridge(
        tmp_path,
        chat_models_env="gpt-a gpt-b gpt-c",
        status_sequence=("403gcbody",),
    )

    assert completed.returncode == 1
    assert not completion.exists()
    assert [body["model"] for body in capture["bodies"]] == ["gpt-a", "gpt-b", "gpt-c"]
    assert "no same-model retry" in completed.stderr
    assert _sleeps(tmp_path) == []
    assert not any("upstream_code=" in line for line in _cpa_lines(completed))


def test_a_stale_group_capability_body_cannot_relabel_a_later_failure(tmp_path):
    """响应体每枪先清空：上一次的分组正文不能给这一次的失败定性。

    curl 只在真收到响应体时才写 ``-o``；正文阶段断掉的那一枪会把上一次的正文
    原样留在文件里。这里第一枪是分组 400（写正文），第二枪是 400 但**没写正
    文**——没有先清空的话，第二枪会读到上一枪的分组正文，被误判成可重试。
    """

    completed, capture, _completion, _tmp = _run_bridge(
        tmp_path,
        chat_models_env="gpt-only",
        extra_args=("gpt-only", "medium", "1"),
        status_sequence=("400gc", "400nobody"),
    )

    assert completed.returncode == 1
    attempt_lines = [line for line in _cpa_lines(completed) if "attempt=" in line]
    assert len(attempt_lines) == 2
    assert "upstream_code=group_capability_unavailable" in attempt_lines[0]
    assert "upstream_code=" not in attempt_lines[1]
    # 第二枪被判成确定性拒绝 → 不再重试，链子到此为止。
    assert capture["invocation_count"] == 2


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


def test_empty_completion_also_honors_the_transient_floor(tmp_path):
    """金丝雀④（补漏）：空补全在 attempts_per_model=1 下同样退避重试。

    ``transient_failure()`` 早就把空补全算进服务类，但此前有一条**先于它**的
    特例分支把上限钉回 ``ATTEMPTS_PER_MODEL``。于是恰恰是唯一按
    ``attempts_per_model=1`` 调用的终审面传输层（boundary 语义复核、终审审片、
    代词一致性、逐条裁决等十条腿全共用它）碰上推理模型的空 output_text 就是
    "一枪换一模型"，三枪打空判死候选。

    修复前本用例会失败：第一次空补全后直接换模型，invocation_count==2 且
    第二个模型被用掉；修复后同模型退避重试，第二枪就拿到正常补全。
    """

    completed, capture, completion, _tmp = _run_bridge(
        tmp_path,
        chat_models_env="gpt-first gpt-second",
        extra_args=("gpt-first gpt-second", "medium", "1"),
        status_sequence=("empty", "200"),
    )

    assert completed.returncode == 0, completed.stderr
    assert completion.read_text(encoding="utf-8") == "safe completion"
    assert capture["invocation_count"] == 2
    # 关键：两枪都打在同一个模型上，没有为了一次空补全就烧掉失效备援。
    assert [body["model"] for body in capture["bodies"]] == [
        "gpt-first",
        "gpt-first",
    ]
    assert "empty_completion=1" in completed.stderr


def test_empty_completion_floor_can_still_be_switched_off(tmp_path):
    """floor 是可调的：显式关掉后，空补全仍旧立刻失效备援到下一个模型。"""

    completed, capture, completion, _tmp = _run_bridge(
        tmp_path,
        chat_models_env="gpt-first gpt-second",
        extra_args=("gpt-first gpt-second", "medium", "1"),
        status_sequence=("empty", "200"),
        env_overrides={"CPA_TRANSIENT_ATTEMPTS_PER_MODEL": "1"},
    )

    assert completed.returncode == 0, completed.stderr
    assert completion.read_text(encoding="utf-8") == "safe completion"
    assert [body["model"] for body in capture["bodies"]] == [
        "gpt-first",
        "gpt-second",
    ]
