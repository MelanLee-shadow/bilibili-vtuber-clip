# vtuber-slice

Remote-first VTuber 自动切片工作区。这个项目的目标不是继续手工修片，而是把李豆沙等直播切片做成无人值守、fail-closed 的自动流水线。

## Source of truth

最终执行目的地只认远端：

```text
free:/opt/bilive/app        # 生产 git 工作树
container:/app              # bilive_record 容器内运行路径
container:/app/Videos       # 录播、候选、精听、review/shadow 产物
```

本地 `/Users/ivan/Project/vtuber-slice` 是早期建设阶段的源码、测试、设计文档、清理 manifest、replay/shadow 证据和人工核查工作区。它可以服务于抽查、复盘、对照和调试，但不要把本地媒体拉取物当成最终产物仓库；项目完成后的正常路径应是完整无人值守自动切片。

## 当前路线

```text
原始录播 + 粗字幕 + 弹幕
→ 高召回内容锚点
→ 扩展源上下文
→ agy 精听 source-context
→ 自动 evidence / boundary resolver
→ AUTO_UPLOAD / AUTO_RECUT / DROP / BLOCK / RETRY
→ 最终渲染 + QA + hash gate
→ 幂等上传
```

重点约束：

- 候选是 anchor，不是最终切片边界。
- `SLICE_DURATION=90` 只能是召回窗口。
- `GLOBAL_SLICE_NUM=10` 是最多 10 条，不是必须凑满。
- `.jingting.done` 不等于 release-ready。
- 没有 `AUTO_UPLOAD` manifest 和 artifact hash gate 就不能发布。
- 无人值守意味着自动隔离/重试/丢弃/放行，不意味着把坏片自动发出去。

## 文档入口

- `docs/remote-first-autoslice-route.md`：当前整理后的远端优先路线、目录职责和下一步工程顺序。
- `docs/lidousha-auto-review-architecture.md`：详细 gate、manifest、状态机、风险和分阶段设计。
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

## 本地验证

```bash
python3 -m compileall -q src scripts tests
python3 -m pytest tests -q
```

## 清理原则

长期保留：源码、测试、docs、AGENTS.md、`.agent/skills/`、`ops/`、`prompts/`、`cleanup_manifests/`。

不长期保留：`reports/**` 运行输出、`lidousha/YYYY-MM-DD/**` 本地媒体拉取物、`.hermes/**` agent 临时状态、`*.mp4/*.flv/*.m4s/*.part`、`*.bak-*`、缓存和 `.DS_Store`。
