# vtuber-slice

李豆沙直播的 remote-first、fail-closed 自动切片流水线。产品目标是无人值守地完成召回、选片、边界、字幕、标题、封面、打包与发布门；人工复核用于校准与异常处理，不是日常逐片剪辑流程。

## 实时权威

任何“已部署、已生成、已发布、可审”结论都必须从真实运行面读回：

```text
free:/opt/bilive/autoslice/repo
  已提交部署树；DEPLOYED_COMMIT 标识实际部署版本

free:/opt/bilive/autoslice/{state,out,reports}
  运行状态、产物、审计与发布账本

free:/opt/bilive/bilive-recorder
container bililive_recorder:/rec
  唯一录制器与原始录播输入
```

`free:/opt/bilive/app` 与容器 `bilive_record:/app` 只保留 tooling / legacy app 职责，不是录制或 autoslice runtime authority，也不得启动另一套 blrec。

本地 `/Users/ivan/Project/vtuber-slice` 是源码、测试、文档与审片镜像。它不能单独证明远端已部署、cron 已执行、产物为当前字节或 B 站公开面已生效。

## 文档权威

当前可执行规则只从 [docs/pipeline/README.md](docs/pipeline/README.md) 进入。每一步的规则只写在对应 step 文档，并由其指向的代码、schema、profile 与资产强制；skills 和 workflows 只提供操作方法，不另立冲突规则。

历史事故报告、评测与日期化 runbook 只保留证据价值，文件顶部必须标明“历史快照”。它们的旧命令、旧 commit、旧模型、旧状态或旧验收结果不能覆盖当前 step 文档和 live readback。

## 流水线

```text
BililiveRecorder 原始录播 + 弹幕/SC
→ 高召回内容锚点
→ Tier 准入、量化校准与 exact-contract 状态机
→ 源语境扩窗与语义闭环边界
→ ASR / LRC → 长程语境 → 专名与声学仲裁 → 最终权威存活验证
→ 严格 SRT / StoryContract / 标题 / 封面最终像素门
→ talk 片头或 song 无片头的最终烧录
→ package audit v2 输入闭包与 policy fingerprint
→ Ivan 明确授权后的 authorized upload
→ 公开面、创作中心与合集 section 精确读回
```

关键不变量：

- candidate 是内容 anchor，不是最终切点。
- Tier 是硬准入；有效分只在 Tier 内排序，confidence 只破同分。
- 人工标题拥有正文，不拥有绕过频道发布外壳与合规门的权限。
- reviewed baseline 先恢复，source truth 后覆盖；两类 owner 都必须在最终 SRT/说话人面真实存活。
- 截图与 AI 都可作为封面路线；二者都必须证明最终像素、文字与人物关系。
- `review_ready`、本地产物存在、脚本返回 0、audit JSON 自报 `passed` 都不能单独证明可发布。
- 没有当前 package audit、artifact hash、Ivan 授权和 `AUTO_UPLOAD` manifest 就不发布。
- 已发稿修复使用同 BV 编辑/换源，不为修正新建 BV。

## 主要入口

- 生产 runner：`scripts/free_session_autoslice.py`
- 单片 producer：`scripts/produce_slice_package.py`
- package auditor：`scripts/audit_lidousha_review_package.py`
- 授权发布器：`scripts/authorized_upload.py`
- 部署器：`scripts/deploy_free_autoslice.sh`
- 当前 step 索引：[docs/pipeline/README.md](docs/pipeline/README.md)
- 活字乱刷：[docs/workflows/huozi-luanshua.md](docs/workflows/huozi-luanshua.md)
- 时效专名：[docs/workflows/timely-term-crawler.md](docs/workflows/timely-term-crawler.md)

## 本地验证

```bash
uvx --from ruff==0.15.21 ruff check src scripts
python3 -m compileall -q src scripts tests
PYTHONPATH=. uv run pytest -q
git diff --check
```

这些命令只证明本地源码；部署后仍须核对远端 `DEPLOYED_COMMIT`、运行树 hash、state、产物与公开面。

## 工作树边界

源码、测试、`docs/`、`assets/`、`profiles/`、`.agent/skills/` 和审计 manifest 可入库。`reports/**`、`lidousha/YYYY-MM-DD/**`、媒体、缓存与临时 replay 通常是可丢弃运行产物；除非任务明确把它们指定为证据，否则不要把它们当源码或当前运行权威。
