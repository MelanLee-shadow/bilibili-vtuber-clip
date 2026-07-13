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
→ 外部同步 LRC + agy 当前音频对齐/现场演唱观察
→ CAM++ 李豆沙主播声纹子门（仅采样明确演唱行）
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
- 日语/稀疏 ASR 歌曲会保留 seed song anchor；可用日文原名/kana 做 Google/公开网页人工检索，自动路径的 `--lrc-provider auto` 会查 NetEase + LRCLIB + Kugou，再在扩大后的 full window 上用唯一 canonical LRC + 当前音频证明。Google 结果页不作为无人值守 API 或歌词证据；日语或歌唱 ASR 乱码本身不再是失败理由，但找不到唯一可靠同步 LRC、版本不符或后续现场/身份证据不足时仍 fail closed。
- “歌切”只指以李豆沙本人现场演唱为主体的完整歌曲。原唱播放、片尾/下播卡音乐、游戏/视频 BGM、其他歌手主唱、合唱/和声、李豆沙只在音乐上说话都不得切。交付必须同时通过：① AGY v4 同主体硬门（首尾与至少 80%/7 行明确为 `SINGING_THIS_LYRIC`；只容许一段受 6 行/12 秒/20%/15 秒四重限制的 canonical 戏剧台词；无其他歌手/和声、无预录/回放人声）；② hash-bound `host-vocal-proof.v2` 只从七个互异的明确演唱行采样，至少 5/7 且头/中/尾均通过，台词行永不采样。只有两者 AND 才产生 `VERIFIED_LIDOUSHA_SINGING`；单独 LRC、AGY 或声纹命中都不算。已知歌段会按 full-proof retry 范围向锚点前后各扩 45 秒并持久隔离所有重叠 talk 候选，不能只截前奏/尾奏或换 lane 洗白。

## 文档入口

- `docs/remote-first-autoslice-route.md`：当前整理后的远端优先路线、目录职责和下一步工程顺序。
- `docs/lidousha-auto-review-architecture.md`：详细 gate、manifest、状态机、风险和分阶段设计。
- `docs/workflows/lidousha-song-finished-package-workflow.md`：歌曲 no-upload review package 的当前权威流程。
- `docs/workflows/huozi-luanshua.md`：从历史直播高置信语音重组句子、最小改句建议与双版本 no-upload 交付流程。
- `.agent/skills/song-lyrics-timeline-aligner/SKILL.md`：外部同步歌词、当前音频证明、单一位移和尾部验收规则。
- `docs/reviews/2026-07-09-mebukutoki-lrc-repair.md`：yonige《芽吹くとき》事故、修复和 live rerun 证据。
- `reports/slice_monitor/README.md`：本地监控 free/bilive 的运行说明；`reports/` 下面其他内容视为生成输出，不是源代码。

## 代码入口

- `scripts/crawl_timely_terms.py`：有界的动漫/二次元新闻/漫展专名快照生成器，
  默认向前看 6 个月、向后回溯 9 个月；详见
  `docs/workflows/timely-term-crawler.md`。
- `scripts/crawl_topic_entity_graph.py`：把时效话题进一步展开成
  `话题 -> 作品 -> 角色` 子图。字幕先解析当前话题/作品，只向 AGY/CPA
  暴露该子图内的中文规范名、别名和读音；角色身份仍须由原始音频或
  结构化 SC/弹幕确认，图谱不能按热度硬改。

- `src/autoslice/auto_review.py`：`AUTO_UPLOAD / AUTO_RECUT / DROP / BLOCK / RETRY` 判定核心。
- `src/autoslice/review_evidence.py`：source-timeline evidence schema。
- `src/autoslice/source_context_planner.py`：从 anchor 规划 source-context 精听 job。
- `src/autoslice/source_context_executor.py`：执行 source-context job 并 fail-closed 记录 review-required。
- `src/autoslice/boundary_resolver.py`：对话边界 resolver 原型。
- `scripts/run_auto_review_shadow_pipeline.py`：本地/远端 no-upload shadow runner。
- `scripts/lidousha_auto_review_shadow_daemon.py`：生产安全的 no-upload shadow daemon。
- `scripts/lidousha_slice_monitor.py`：本地监控 `free` 上 bilive 运行状态。
- `scripts/free_session_autoslice.py`：`free` 上 cron 每 10 分钟调用的 post-stream autoslice runner。
- `scripts/huozi_luanshua.py`：活字乱刷的全历史发现、最长片段规划、建议句、双 ASR 证据和 no-upload 渲染入口。
- `src/autoslice/branding_intro.py`：强制片头（活字乱刷候选2）的 hash 绑定解析与成品前置拼接；策略开关在 `assets/lidousha/intro/branding_intro.v1.json`。
- `src/autoslice/song_repair.py`：canonical LRC 搜索、稀疏 ASR 音频证明与 fail-closed 校验。
- `src/autoslice/agy_lrc_alignment.py`：当前 full window 音频对同步歌词逐行观察，并输出 AGY v4 以现场演唱为主体或背景播放的分类证据。
- `src/autoslice/host_vocal_proof.py`：生成 `host-vocal-proof.v2`，CAM++ 七点只取明确演唱行；runner 复核歌词角色/hash 绑定、分数中位数和门槛，复核器不重跑 ML 推理。

## 本地验证

```bash
python3 -m compileall -q src scripts tests
python3 -m pytest tests -q
```

## 清理原则

长期保留：源码、测试、docs、AGENTS.md、`.agent/skills/`、`ops/`、`prompts/`、`cleanup_manifests/`。

不长期保留：`reports/**` 运行输出、`lidousha/YYYY-MM-DD/**` 本地媒体拉取物、`.hermes/**` agent 临时状态、`*.mp4/*.flv/*.m4s/*.part`、`*.bak-*`、缓存和 `.DS_Store`。
