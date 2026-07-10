# vtuber-slice

Remote-first VTuber 自动切片工作区。这个项目的目标不是继续手工修片，而是把李豆沙等直播切片做成无人值守、fail-closed 的自动流水线。

## Source of truth

最终执行与验收只认远端真实面：

```text
free:/opt/bilive/autoslice/repo            # autoslice 已提交部署树；DEPLOYED_COMMIT 是版本指纹
free:/opt/bilive/autoslice/{state,out,reports} # autoslice 运行状态、尝试证据与验收报告
free:/opt/bilive/app                       # 录制栈源码
container:/app                             # bilive_record 容器内运行路径
container:/app/Videos                      # 原始录播输入
```

本地 `/Users/ivan/Project/vtuber-slice` 是源码、测试、设计文档和审片镜像工作区。它可以服务于抽查、复盘、对照和调试，但生产结论必须回到远端 commit 指纹、真实 state/report 和 artifact hash；不要只凭本地媒体拉取物宣布运行时成功。

## 当前路线

```text
原始录播 + 粗字幕 + 弹幕
→ 高召回内容锚点
→ 扩展源上下文
→ agy 精听 source-context
→ 自动 evidence / boundary resolver
→ AUTO_RECUT / DROP / BLOCK / RETRY（语义结果只作证据，不替代歌曲正证据）
→ no-upload review 成片 + QA + hash gate
→ Ivan 逐条明确授权后，另走 AUTO_UPLOAD manifest + artifact hash 的幂等上传
```

重点约束：

- 候选是 anchor，不是最终切片边界。
- `SLICE_DURATION=90` 只能是召回窗口。
- `GLOBAL_SLICE_NUM=10` 是最多 10 条，不是必须凑满。
- `.jingting.done` 不等于 release-ready。
- 没有 `AUTO_UPLOAD` manifest 和 artifact hash gate 就不能发布。
- 无人值守意味着自动隔离/重试/丢弃/放行，不意味着把坏片自动发出去。
- 日语/稀疏 ASR 歌曲会保留 seed song anchor，并在扩大后的 full window 上用唯一 canonical LRC + 当前音频证明；找不到唯一可靠 LRC、音频证据不完整或 hash 不一致时仍 fail-closed，不承诺所有日语歌都自动成功。

## 文档入口

- `docs/remote-first-autoslice-route.md`：当前整理后的远端优先路线、目录职责和下一步工程顺序。
- `docs/lidousha-auto-review-architecture.md`：详细 gate、manifest、状态机、风险和分阶段设计。
- `docs/workflows/lidousha-song-finished-package-workflow.md`：歌曲 no-upload review package 的当前权威流程。
- `.agent/skills/song-lyrics-timeline-aligner/SKILL.md`：外部同步歌词、当前音频证明、单一位移和尾部验收规则。
- `docs/reviews/2026-07-09-mebukutoki-lrc-repair.md`：yonige《芽吹くとき》事故、修复和 live rerun 证据。
- `reports/slice_monitor/README.md`：本地监控 free/bilive 的运行说明；`reports/` 下面其他内容视为生成输出，不是源代码。

## 代码入口

- `src/autoslice/auto_review.py`：`AUTO_UPLOAD / AUTO_RECUT / DROP / BLOCK / RETRY` 判定核心。
- `src/autoslice/review_evidence.py`：source-timeline evidence schema。
- `src/autoslice/source_context_planner.py`：从 anchor 规划 source-context 精听 job。
- `src/autoslice/source_context_executor.py`：执行 source-context job 并 fail-closed 记录 review-required。
- `src/autoslice/boundary_resolver.py`：对话边界 resolver 原型。
- `scripts/run_auto_review_shadow_pipeline.py`：本地/远端 no-upload shadow runner。
- `scripts/lidousha_auto_review_shadow_daemon.py`：生产安全的 no-upload shadow daemon。
- `scripts/lidousha_slice_monitor.py`：本地监控 `free` 上 bilive 运行状态。
- `scripts/free_session_autoslice.py`：`free` 上 cron 每 10 分钟调用的 post-stream autoslice runner。
- `src/autoslice/song_repair.py`：canonical LRC 搜索、稀疏 ASR 音频证明与 fail-closed 校验。
- `src/autoslice/agy_lrc_alignment.py`：当前 full window 音频对同步歌词逐行观察的隔离执行器。

## 本地验证

```bash
python3 -m compileall -q src scripts tests
python3 -m pytest tests -q
```

## 清理原则

长期保留：源码、测试、docs、AGENTS.md、`.agent/skills/`、`ops/`、`prompts/`、`cleanup_manifests/`。

不长期保留：`reports/**` 运行输出、`lidousha/YYYY-MM-DD/**` 本地媒体拉取物、`.hermes/**` agent 临时状态、`*.mp4/*.flv/*.m4s/*.part`、`*.bak-*`、缓存和 `.DS_Store`。
