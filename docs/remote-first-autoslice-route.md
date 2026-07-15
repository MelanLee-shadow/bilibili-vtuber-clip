# Remote-first 自动切片路线

更新时间：2026-07-10

## 一句话结论

`vtuber-slice` 的最终执行面只认远端 `free`；录制/app 与 post-stream autoslice 是两个明确分开的运行面：

```text
free:/opt/bilive/autoslice/repo   # commit-only 部署的 autoslice scripts/src/assets + DEPLOYED_COMMIT
free:/opt/bilive/autoslice/       # runner state/cache/out/reports/logs/locks/DISABLED
free:/opt/bilive/app              # recorder/legacy app 与受保护的人工 uploader bridge
container:/app                    # bilive_record 容器内 app 路径
container:/app/Videos             # 录播输入与 app 侧产物
```

本地 `/Users/ivan/Project/vtuber-slice` 只用于：

- 设计文档；
- 小型纯逻辑原型；
- 单元测试；
- 临时 replay/shadow 证据归档；
- 项目早期的人工核查、抽查、复盘和异常对照。

本地可以在项目早期支撑人工核查，但它不再作为最终产物仓库，也不应该长期保存 MP4/FLV/CloudDrive 拉取物。项目完成后的正常路径应是完整无人值守自动切片。

## 当前事实边界

- 本地目录现在是 git repo；生产 autoslice 只允许从 clean commit 经 `scripts/deploy_free_autoslice.sh` 部署。
- unattended runner 读取 `/opt/bilive/autoslice/repo`，部署后以 `DEPLOYED_COMMIT` 与 runner hash 核对实际代码；不要向远端单文件热补丁。
- `/opt/bilive/app` / container `/app` 仍是 recorder 和 legacy/emergency upload 运行面，但不再冒充 post-stream autoslice 的 commit 指纹。
- 上传必须 fail-closed：没有 `AUTO_UPLOAD` manifest 和 artifact hash gate，就不能发布。
- `*.jingting.done` 只代表精听完成，不代表 release-ready。
- 2026-07-10 的《芽吹くとき》historical v4 只验证了日语稀疏/garbled ASR 下的 canonical-LRC 召回、对齐与完整边界；它后来被确认为下播卡播放的背景原曲，已 superseded/rejected，不是李豆沙歌切正例。当前生产 commit `f64cd29` 已部署：AGY v4 要求首尾演唱、至少七行且至少 80% 演唱，只允许一个受六行/12 秒/20%/15 秒四重限制的歌曲内戏剧对白块，且头/中/尾三点证据必须落演唱行；`host-vocal-proof.v2` 的 CAM++ checkpoint 只从演唱行取样，并重新绑定 required role 与五项逐行断言。原唱/背景播放、普通说话+BGM、其他歌手/和声、预录/回放、静态/离屏 replay 仍硬 BLOCK，song interval 上的重叠 talk 候选仍持久隔离。事故六文件修复已 `COMMITTED`；真唱《屑屑》v5 已 no-upload `READY/MATERIALIZED`，同音轨静态回放被 AGY 阻断，fresh《芽吹くとき》自动找到正确日文 LRC 后即使 AGY 误报 live，仍由 0/7 的 `host-vocal-proof.v2` 最终 `BLOCK / SONG_NOT_LIDOUSHA_SINGING`。10:50Z exact cron smoke 无 state/summary/upload-ledger 漂移，`DISABLED` 已移除。

## 目标流水线

```text
原始录播 + 粗字幕 + 弹幕
→ 高召回内容锚点（candidate 是 anchor，不是最终边界）
→ 扩展源上下文
→ talk: aggregate ASR/CPA correction；song: auto LRC discovery + full-window audio/LRC proof + AGY-live AND CAM++ identity gate
→ 歌曲/对话结构 evidence
→ 自动边界解析
→ AUTO_UPLOAD / AUTO_RECUT / DROP / BLOCK / RETRY
→ 最终渲染
→ render/PTS QA
→ artifact hash release gate
→ 幂等上传
```

关键约束：

- `SLICE_DURATION=90` 只能是召回窗口，不能直接变成最终成片长度。
- `GLOBAL_SLICE_NUM=10` 是上限，不是凑满 10 条。
- 歌曲必须按完整歌曲或明确 BLOCK/DROP 处理，不能截成固定 90 秒。
- 对话找不到自然起止点就 DROP/BLOCK，不为了发片硬裁。
- 无人值守不等于自动乱发；无人值守的含义是系统自己分类、隔离、重试和放行。

## 推荐目录职责

```text
AGENTS.md                         # 给后续 agent 的项目约束
README.md                         # 仓库入口，只写当前路线和 source-of-truth
docs/remote-first-autoslice-route.md
                                  # 本文件：远端优先执行路线
docs/lidousha-auto-review-architecture.md
                                  # 详细架构、gate、manifest、阶段拆解
src/autoslice/                    # 本地可测的纯逻辑 gate / evidence / resolver
scripts/                          # autoslice 部署到 /opt/bilive/autoslice/repo/scripts；app 专用脚本另按入口落位
tests/                            # 本地纯逻辑测试，部署前必须通过
ops/                              # 远端运行补丁或运维脚本，必须注明落点
prompts/                          # 可复用 prompt，不放运行输出
cleanup_manifests/                # 本地/远端清理审计记录
```

不应长期留在 repo 工作树：

```text
reports/**                        # 运行报告、shadow/replay 输出、monitor 历史
lidousha/YYYY-MM-DD/**             # 本地媒体拉取物、手动 recut、review package
.hermes/**                         # agent 临时计划和 consult 状态
*.mp4 *.flv *.m4s *.part            # 媒体和录制碎片
*.bak-*                             # 热修/部署备份，确认无用后进 cleanup manifest
__pycache__ .pytest_cache .DS_Store # 工具缓存
```

例外：短小的总结性文档可以从 `reports/` 提炼进 `docs/`；不要把整包运行证据长期堆在 repo 根目录。

## 本地人工核查边界

当前项目还在早期，无法一步到位达成无人值守；本地目录允许作为人工核查工作区，用来抽看 blocker、复盘 bad case、对照字幕/边界/evidence、验证远端 shadow 决策是否可信。

但人工核查不是最终产品路径：它不能变成“每条片都要 Ivan 手动挑、手动改时间线、手动决定上传”的常态 gate。最终目标仍是远端自动完成切片、精听、边界解析、review 决策、渲染 QA 和发布 gate；人只看系统隔离出的异常样本或阶段性评估结果。

## 当前剩余缺口

这轮整理只解决本地工作区混乱和文档路线问题；它没有把生产推进到 full unattended。当前必须继续收敛的缺口是：

- 远端 autoslice 代码与运行数据已经分目录，但 state/cache/out/reports 仍要按 manifest 与保留策略清理，不能批量盲删。
- post-stream runner 已由远端 cron + flock + `DISABLED` kill switch 管理；本轮联合门三组媒体验收与 exact cron smoke 已完成，`DISABLED` 已在 lock 下移除。监控仍需持续证明 heartbeat、录制输入、锁和 mount 的真实状态。
- auto-review 仍是 shadow/no-upload；`is_publish_gate_satisfied()` 尚未接入真实上传器。
- 唯一可靠 LRC 身份的 sparse-ASR 歌曲已经能走 current-audio/AGY High 正证据、external-LRC burn、精确重渲染和 hash gate；无同步 LRC、身份歧义或 live arrangement 不匹配仍会 fail closed，不能宣称任意日语歌自动成功。
- AGY v4 + `host-vocal-proof.v2` 已部署，真唱、同音轨静态 replay、当前《芽吹くとき》背景原曲和 cron entrypoint 均完成 no-upload 验收。当前剩余风险不是上线闸，而是 AGY 模型的重复运行方差：本轮《芽吹くとき》AGY 误报 live，最终由独立 CAM++ 0/7 正确阻断；不得因此把单层判断写成充分条件。
- review-package audit 仍需继续扩充音频观察、波形/频谱和 approved-cover-style 的结构化检查；真实发布仍是单独授权面。
- Bilibili 上传/编辑脚本属于 legacy/emergency path，不能重新成为正常流水线入口。

## 下一步工程路线

### 0. 清理与防回潮

- 本地：删除生成媒体、shadow/replay 输出、monitor 历史，只保留源代码/测试/文档/cleanup manifest。
- 远端：先做只读分类，不要盲删。尤其不要碰 `cookie.json`、生产配置、当前 hotfix、正在运行的脚本。
- 加 `.gitignore`，避免本地媒体和运行输出再次进工作树。

### 1. 远端 shadow decision ledger

把本地已通过测试的 P7 shadow gate 部署到 `free`，但保持 no-upload：

```text
.jingting.done / manifest
→ build review evidence
→ auto-review decision
→ write shadow decision artifact
→ never mutate publish.json
→ never enqueue upload
```

产物建议：

```text
*.auto_review.shadow.json
*.auto_review.decision.json
*.auto_review.done
*.auto_review.block
*.auto_review.retry
*.auto_review.drop
*.auto_review.would_upload   # 仅 shadow 语义
```

### 2. Source-context first

把当前“先切最终片段，再精听”的顺序改成：

```text
anchor → source-context clip/SRT → agy → evidence → boundary resolver → final render
```

这一步解决歌曲开头/结尾、对话铺垫、punchline 后反应被提前切掉的问题。

### 3. Render QA + hash gate

上传器只能接受当前 artifact hash 与 `slice-auto-review.v1` manifest 完全一致的产物。任何旧 title/cover/publish.json 在 recut 后必须失效重建。

### 4. Canary，再 limited，最后 full

```text
shadow：只决策，不上传
→ canary：每场最多 1 条
→ limited：每日/每场限量
→ full：最多 10 条，但允许 0 条
```

full 前必须有历史样本证明：严重截断、严重字幕错、重复上传等指标达到门槛。

## 验证命令

本地纯逻辑验证：

```bash
uvx --from ruff==0.15.21 ruff check src scripts
python3 -m compileall -q src scripts tests
python3 -m pytest tests -q
```

远端只读状态验证：

```bash
ssh free 'cat /opt/bilive/autoslice/repo/DEPLOYED_COMMIT'
ssh free 'test -f /opt/bilive/autoslice/DISABLED && echo paused || echo enabled'
ssh free 'docker exec bilive_record sh -lc "ps -ef | grep -E \"src\\.upload\\.upload|src\\.upload\\.local_prepare|run_auto_review\" | grep -v grep || true"'
```

远端部署/清理不是本文档自动授权项。涉及生产文件删除、上传开关、cookie、配置和 live daemon 的动作都必须先列出 manifest，再执行。
