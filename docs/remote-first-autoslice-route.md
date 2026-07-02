# Remote-first 自动切片路线

更新时间：2026-06-26

## 一句话结论

`vtuber-slice` 的最终执行面只认远端 `free` 主机上的 bilive 运行树：

```text
free:/opt/bilive/app        # git 工作树 / 生产代码落点
container:/app              # bilive_record 容器内运行路径
container:/app/Videos       # 录播、候选切片、shadow/review 产物
```

本地 `/Users/ivan/Project/vtuber-slice` 只用于：

- 设计文档；
- 小型纯逻辑原型；
- 单元测试；
- 临时 replay/shadow 证据归档；
- 项目早期的人工核查、抽查、复盘和异常对照。

本地可以在项目早期支撑人工核查，但它不再作为最终产物仓库，也不应该长期保存 MP4/FLV/CloudDrive 拉取物。项目完成后的正常路径应是完整无人值守自动切片。

## 当前事实边界

- 本地目录不是 git repo；`git status` 在本地会失败。
- 远端 `/opt/bilive/app` 是 git repo，并且是最终执行目的地。
- 远端现在仍有生产运行修改、备份文件、测试产物和运行产物混在工作树里；清远端前必须先分清“生产热补丁 / 运行必需文件 / 纯垃圾”。
- 上传必须 fail-closed：没有 `AUTO_UPLOAD` manifest 和 artifact hash gate，就不能发布。
- `*.jingting.done` 只代表精听完成，不代表 release-ready。

## 目标流水线

```text
原始录播 + 粗字幕 + 弹幕
→ 高召回内容锚点（candidate 是 anchor，不是最终边界）
→ 扩展源上下文
→ agy 精听 source-context
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
scripts/                          # 远端/本地桥接脚本；部署后应在 /opt/bilive/app/scripts 下运行
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

- 远端 `/opt/bilive/app` 仍是实际 source of truth，但工作树里混有 hotfix、配置、cookie、备份、运行产物和未跟踪代码；远端清理必须先做只读分类和 cleanup manifest，不能直接批量删除。
- 当前监控入口仍由本地 Mac LaunchAgent 驱动；目标状态应改为远端 systemd/timer 管理 scan、local_prepare、jingting、auto-review shadow 和未来 gated uploader。
- `AUTOSLICE_ENABLED` 与 dirty backlog 防护仍会让冷启动依赖人工确认；无人值守路线需要把这些情况转成明确的 `BLOCK`/`RETRY_INFRA`/自动隔离策略。
- auto-review 仍是 shadow/no-upload；`is_publish_gate_satisfied()` 尚未接入真实上传器。
- source-context executor 目前接收 agy/refined SRT 结果，但真实 source ASR、真实 agy 调度、歌词自动对齐、duplicate/PTS/title/cover artifact hash 还没有完整串进生产闭环。
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
python3 -m compileall -q src scripts tests
python3 -m pytest tests -q
```

远端只读状态验证：

```bash
ssh free 'cd /opt/bilive/app && git status --short --branch'
ssh free 'docker exec bilive_record sh -lc "ps -ef | grep -E \"src\\.upload\\.upload|src\\.upload\\.local_prepare|run_auto_review\" | grep -v grep || true"'
```

远端部署/清理不是本文档自动授权项。涉及生产文件删除、上传开关、cookie、配置和 live daemon 的动作都必须先列出 manifest，再执行。
