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


@pytest.fixture(scope="session", autouse=True)
def _never_production_runtime():
    """Keep unit tests off the real production root, preserving simulated roots."""
    from src.autoslice import provider_slots

    production = Path("/opt/bilive/autoslice").resolve()
    original = provider_slots.configured_runtime_root

    def guarded_runtime_root():
        root = original()
        if root is not None:
            resolved = root.resolve()
            if resolved == production or production in resolved.parents:
                if not os.environ.get("AUTOSLICE_BASE"):
                    return None
                pytest.fail(
                    "TEST_PRODUCTION_RUNTIME_BLOCKED: configured_runtime_root() "
                    f"resolved to production runtime {root}", pytrace=False,
                )
        return root

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(provider_slots, "configured_runtime_root", guarded_runtime_root)
        yield


@pytest.fixture(autouse=True)
def _fake_argon_inrun_retrieval_clock(monkeypatch):
    """Exercise bounded polling without waiting for wall-clock minutes."""
    from src.autoslice import final_review_auditor as auditor

    now = [0.0]

    def advance(seconds):
        now[0] += seconds

    monkeypatch.setattr(auditor, "_argon_retrieval_clock", lambda: now[0])
    monkeypatch.setattr(auditor, "_argon_retrieval_sleep", advance)


@pytest.fixture(autouse=True)
def _restore_process_scoped_evaluator_env():
    # CLI entry points intentionally mutate their process environment. Tests
    # invoking them in-process must not lend settings to a later test.
    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)


@pytest.fixture(autouse=True)
def _hermetic_autoslice_base(monkeypatch):
    """磁盘密闭：默认清掉 ambient AUTOSLICE_BASE。

    内容寻址裁决缓存按 env 根在调用时落盘：shell 带着可写 AUTOSLICE_BASE
    跑套件时用例互吃缓存，带着不可写 /opt 默认时全绿只是巧合。套件的作者
    基线是"未设置 base=缓存关"——这里把它变成保证：逐测试删除 ambient
    值；需要缓存行为的测试自己 setenv 一个 tmp 根即可。
    """

    monkeypatch.delenv("AUTOSLICE_BASE", raising=False)

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


_llm_client._call_command = _hermetic_call_command
