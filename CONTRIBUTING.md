# 贡献指南

先读 [AGENTS.md](AGENTS.md)——它是本仓的完整操作约定，人与 AI 代理通用。
本项目预期的贡献者本来就包括 AI agent：**agent 写的 PR 完全欢迎**，但提交
PR 的人对结果负责（跑过测试、读过 diff）。

## 开工前

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest -q   # 必须全绿；无凭据无网络可跑（密闭守卫强制）
```

## 三条铁律（与 AGENTS.md 一致，PR 审查按此执行）

1. **过不了门就修产物，不许修门。** 所有质量门默认拒绝；让门"变松"的改动
   必须有独立证据并写进提交信息。
2. **债务棘轮只降不升。** `tests/test_runtime_architecture.py` 的行数账本
   变了要在提交里说明理由；靠删测试/放宽断言过门的 PR 直接拒。
3. **上传只走 `authorized_upload.py` 的 manifest 闭环**，别绕。

## PR 期望

- 小步、单一目的；行为变化必须配测试；提交信息写清"为什么"。
- 频道知识只进 profile 资产，不进代码（不要把任何主播的专名写进 src/）。
- 跑全量套件（不是只跑定向测试），红了先修红。

## 安全问题

不要开公开 issue，走 [SECURITY.md](SECURITY.md) 的私密披露通道。
