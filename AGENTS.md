# AGENTS.md — 给 AI 代理的项目地图与操作约定

本仓是一条 fail-closed、证据链驱动的 B 站直播切片流水线。给代理的三条铁律：

1. **过不了门就修产物，不许修门**。所有质量门（字幕真值、边界、封面身份、
   出版登记）默认拒绝；让某个门"变松"的改动必须有独立证据并写进提交信息。
2. **证据先于结论**。"已运行/已通过/已交付"只能来自实际执行输出；改字幕
   必须有出处（平台弹幕/礼物记录、独立听写、闭集裁决回执）。
3. **运营状态 ≠ 代码**。出版登记、真值台账、评审契约是部署自己的数据
   （本仓只有模板）；不要把示例 profile 的内容当成约束。

## 第一次拿到本仓（人与代理通用）

按顺序做，每步有验证点：

1. **装依赖**：`python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`。
2. **冒烟**：`.venv/bin/python -m pytest -q` —— 应全绿（无凭据/无网络也能跑，
   LLM/HTTP 边界全部 mock；缺 `ffmpeg` 时个别用例 skip）。套件以
   **默认 profile** 为基准：跑测试时不要设置 `AUTOSLICE_PROFILE`。
3. **配环境**：`cp .env.example .env`，填 `CPA_BASE_URL`/`CPA_API_KEY` 与
   听音腿（**AGY 订阅或 Gemini API key 二选一**——同一个模型的两种接入）。**`.env` 只是模板，不会被任何脚本自动加载**——跑脚本前
   `set -a; source .env; set +a`，无人值守 runner 的参考部署读
   `$AUTOSLICE_BASE/cpa.env`（`deploy_autoslice.sh` 生成）。配完跑
   `python3 scripts/preflight.py` 一次体检（字体/ffmpeg/目录/凭据/VAD）。
   没有 CPA？先读 README 的 LLM 通道一节——没有 LLM 入口时
   选题/校对/封面 lane 会 fail-closed 拒绝，而不是降级。
4. **认识 profile**：读 [profiles/README.md](profiles/README.md)。默认
   profile 是 `lidousha`（示例频道）；`AUTOSLICE_PROFILE` 在进程 import 时
   读取。校验：`python3 scripts/validate_channel_profile.py --profile lidousha
   --config-only`（输出里的 `voiceprint_status` 为 UNCONFIGURED 属预期——
   声纹不随仓分发，READY≠声纹已 enroll）。
5. **给新频道建 profile**（本仓的预期配置者就是你——agent；人类用户会把
   这一步整个交给你）：复制 `profiles/_template/profile.json` 与
   `assets/_template/` 骨架，按骨架 README 的**分层**填充——层 0 默认给全
   （字体/政策/度量/原则，agent 独立完成）；层 1 身份四件套**先问后写**
   （词表、persona、标题风格、封面形象——模板内已写好该问频道主人的问题与
   真实示例，答完代写；先写 3–5 条就能开跑，之后边用边攒）；层 2 配 cron
   由 crawler 代填；
   层 3 台账运行时自长；层 4（声纹/片头）用到才配。每层收敛后跑 validator
   与入口 `--help` 验证。**发布 lane 另需 `AUTOSLICE_SEASON_IDS`（部署方
   自己账号的合集/小节 ID，账号专属、无默认值、必填；获取方式见
   `.env.example`）。**
   换频道的耦合现状见下方「换频道耦合现状」一节（发布面已全 profile 化，
   持久证据词汇保留示例拼写属刻意）。凭据逐项按
   [docs/credentials.md](docs/credentials.md) 配置并跑其校验命令。
6. **跑第一支切片**：README「快速开始」的 `--smoke-segment` 冒烟路径。
7. **整线部署**：`ops/recording/README.md`（录制层）→
   `scripts/deploy_autoslice.sh`（参考部署，按主机改写）→ cron `--once`。

## 仓库地图

| 位置 | 内容 |
|---|---|
| `docs/pipeline/README.md` | 分步权威文档入口（源录像→选片→边界→字幕→歌切→标题/封面→打包→发布） |
| `src/autoslice/` | 全部管线模块（约 190 个文件） |
| `scripts/README.md` | 脚本地图（按 lane 分组，先读这个再翻 scripts/） |
| `scripts/session_autoslice.py` | 无人值守 runner（cron 驱动；`--smoke-segment` 单段冒烟） |
| `scripts/produce_slice_package.py` | 单候选产线入口（spec 字段见 docstring） |
| `scripts/authorized_upload.py` | 发布/同稿修复的唯一副作用入口 |
| `profiles/`、`assets/_template/`、`assets/lidousha/` | 频道 profile 模板、最小骨架与完整实战示例 |
| `.agent/skills/` | 可复用的代理技能（发布闭环、标题风格、歌词对轴等） |

## 常用命令

```bash
python3 -m pytest -q                  # 全套件；无凭据可跑（密闭守卫机械封死真实 LLM 通道）
python3 scripts/preflight.py          # 部署体检：字体/ffmpeg/目录/凭据/VAD
python3 scripts/validate_channel_profile.py --profile <id> [--config-only]
python3 scripts/produce_slice_package.py --spec <spec.json> --ssh-host localhost
```

## 代理禁区

- **上传只走 `authorized_upload.py` 的 manifest 闭环**；`do_upload.sh` 拒绝
  裸调（需要 `AUTHORIZED_UPLOAD=1`），不要绕。出版登记里 `published` 的候选
  永远不允许再新投稿，修复走同 BV repair lane。
- **不要把示例 profile 的专名/风格写进代码**；频道知识只进 profile 资产。
- **不要靠删测试/放宽断言过门**；债务棘轮（`tests/test_runtime_architecture.py`
  的行数账本）只许降不许升，改了要在提交里说明。

## 架构约定与核心不变量

- **证据链而非黑箱**：每处字幕修正都要有出处（平台弹幕/礼物记录、独立声学
  听写、闭集裁决回执），并以哈希绑定进交付包。
- **出版登记是唯一上传授权**：已发布内容只能走同 BV 修复链（换源不换稿）。
- **精确重放**：同稿修复用 `subtitle-redelivery-baseline.v2` 逐字节恢复已审
  文本，只有真值台账拥有的区间允许偏离——修复不会引入新的回归。
- **内容寻址缓存**：声学/裁决调用按输入哈希缓存，重试轮零重复请求。
- **债务棘轮**：`tests/test_runtime_architecture.py` 冻结每个超限函数/模块的
  行数，只许降不许升；新增行数=显式改账本并在提交里说明。
- **凭据**：每个 cookie/key 的模板与校验命令见
  [docs/credentials.md](docs/credentials.md)；全部凭据不入库。

## 换频道耦合现状（发布前必读）

身份/prompt/控制流/**发布与声纹 lane 的资产路径**已全部 profile 化（默认
profile 渲染与路径逐字节等价）：出版登记、终审契约、真值台账、手动标题
授权目录、审计指纹源、声纹接受门都按 `CHANNEL_PROFILE.asset_file()`/
`profile_id` 派生——换频道即各用各的登记台账与授权面，可产包评审也可走
完整发布闭环（上传授权仍归 `authorized_upload` 的 manifest+出版登记门）。

**词汇级兼容，绝对不要改**：持久证据/schema 词汇保留 `lidousha-` 拼写以不打碎
旧包哈希——`lidousha-*.v1` schema 串、`lidousha_role`、`human_reviewed_lidousha`、
`verified_lidousha_voiceprint`、`LIDOUSHA_*` 兼容 env 别名、scorecard 维度键
`lidousha_centrality`。

部署位默认值全部指向本机或由用户显式决定：`--ssh-host` 默认 `localhost`
（媒体在别的机器时显式传）、监控脚本的宿主/房间号走
`AUTOSLICE_MONITOR_SSH_HOST`/`AUTOSLICE_MONITOR_ROOM` 环境变量、
`pull` 工具的 `--host` 必填。`/opt/bilive` 布局是参考部署约定，可整体换路径。
