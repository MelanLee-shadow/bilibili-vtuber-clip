"""Pytest bootstrap: sys.path + 密闭性守卫。

Tests import ``src.autoslice`` and ``scripts.*`` as top-level packages.
``python3 -m pytest`` already prepends the CWD, but bare ``pytest`` (and many
IDE runners) does not — this conftest makes both invocations equivalent.
"""

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 套件以默认 profile 为基准：机械兜底，防 shell 带着 AUTOSLICE_PROFILE 时
# 整套件换 profile（profile 在 import 时读取，必须在测试模块导入前清掉）。
os.environ.pop("AUTOSLICE_PROFILE", None)


def _grouped_by_directory(args: list[str]) -> list[str]:
    """命令行上的测试路径按所在目录归拢（目录首次出现的先后不变，组内顺序不变）。

    pytest 9：同一目录的文件被别的目录的文件隔开时（a/x.py b/y.py a/z.py），后一组
    a/z.py 拿不到 a/conftest.py 的夹具——包括 autouse 的隔离夹具（例如
    tests/manual_lane/conftest.py 的“不写主机共享资产”“不真等限流”）。测试因此
    走生产默认路径：轻则假失败，重则碰到真东西。CI 的定向清单常是这种顺序。"""
    groups: dict[str, list[str]] = {}
    for arg in args:
        path = Path(arg.split("::", 1)[0])
        key = str(path if path.is_dir() else path.parent)
        groups.setdefault(key, []).append(arg)
    return [arg for group in groups.values() for arg in group]


def pytest_configure(config):
    config.args[:] = _grouped_by_directory(list(config.args))


def _absent_default_base() -> str:
    """一个不存在、也建不出来的路径（在一个只读的空临时目录里），进程结束时把那个目录删掉。"""
    import atexit
    import tempfile

    holder = Path(tempfile.mkdtemp(prefix="autoslice-default-base-"))
    holder.chmod(0o555)
    atexit.register(lambda: (holder.chmod(0o755), holder.rmdir()) if holder.is_dir() and not any(holder.iterdir()) else None)
    return str(holder / "absent")


# 代码在 AUTOSLICE_BASE 未设置时落到的默认根（src/autoslice/base_default.py，唯一定义）。生产是
# /opt/bilive/autoslice：开发机上没有，生产主机上有——在那台主机上跑的测试读到了生产的运行时出版
# 登记、回执和锁文件（2026-11-21 全量 80 多个失败）。这里在任何测试模块导入之前把它指到一个不存在、
# 也建不出来的路径（有的模块在导入时就算好默认根）：和开发机上的 /opt/bilive/autoslice 一样——不是“空目录”，
# 有的代码见到默认根存在就往里写，见到不存在才走临时根的回退。
os.environ["AUTOSLICE_DEFAULT_BASE"] = _absent_default_base()


@pytest.fixture(autouse=True)
def _hermetic_autoslice_base(monkeypatch):
    """磁盘密闭：逐测试清掉 ambient AUTOSLICE_BASE，并保证默认根仍是上面那个不存在的路径。

    内容寻址裁决缓存按 env 根在调用时落盘：shell 带着可写 AUTOSLICE_BASE 跑套件时用例互吃缓存
    （实测 11 红）。套件的作者基线是“未设置 base＝缓存关、不落盘”——设成任何目录（哪怕只读）都会
    打开那些分支（实测：只读根让请求生命周期去建 cache 目录而失败）。所以 base 仍然删掉，
    落到的默认根由上面那一行管；需要缓存行为或别的根的测试自己 setenv 一个 tmp 根。
    """

    monkeypatch.delenv("AUTOSLICE_BASE", raising=False)
    monkeypatch.setenv("AUTOSLICE_DEFAULT_BASE", os.environ["AUTOSLICE_DEFAULT_BASE"])

# ---- 密闭性守卫：单测永不真打 LLM 网络 ----------------------------------
# 套件的承诺是"无凭据/无网络可跑、LLM 边界全部 mock"。若跑测试的 shell 恰好
# 带着真实 CPA 凭据，mock 打在死 seam 上的测试会静默走真网络"变绿"——烧配额
# 且结果取决于远端模型。这里把真实 CPA 命令通道在 pytest 进程内机械封死：
# 任何测试打到它都显式失败。需要 LLM 输出的测试请 monkeypatch 使用方模块
# 自己的 llm-call builder（例如 ``pipeline._build_final_review_llm_call``）。

from src.autoslice import llm_client as _llm_client

_REAL_CALL_COMMAND = _llm_client._call_command


def _hermetic_call_command(prompt, config):
    if "llm_via_cpa.sh" in (getattr(config, "command_template", "") or ""):
        raise _llm_client.LlmCallError(
            "TEST_HERMETIC_LLM_BLOCKED: pytest 内禁止真实 CPA 通道；"
            "请 monkeypatch 该 stage 的 llm-call builder"
        )
    return _REAL_CALL_COMMAND(prompt, config)


# a test of the channel itself takes the real one from here; `from conftest import ...` finds whichever
# directory's conftest was imported last (tests/manual_lane has one too)
_hermetic_call_command.real = _REAL_CALL_COMMAND
_llm_client._call_command = _hermetic_call_command


# ---- tests that cannot hold in the public tree (tests/public_expected_failures.txt) ----------------------------
def _public_expected_failures():
    from pathlib import Path as _Path

    path = _Path(__file__).resolve().parent / "tests" / "public_expected_failures.txt"
    rows = []
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip() and not line.startswith("#") and "|" in line:
                prefix, reason = (part.strip() for part in line.split("|", 1))
                rows.append((prefix, reason))
    return rows


def pytest_collection_modifyitems(config, items):
    import pytest as _pytest

    rows = _public_expected_failures()
    for item in items:
        for prefix, reason in rows:
            if item.nodeid.startswith(prefix):
                item.add_marker(_pytest.mark.xfail(reason=reason, strict=False))
                break
