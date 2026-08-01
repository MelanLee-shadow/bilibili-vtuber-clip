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
   LLM/HTTP 边界全部 mock；缺 `ffmpeg` 或表情包媒体时个别用例 skip）。
3. **配环境**：`cp .env.example .env`，填 `CPA_BASE_URL`/`CPA_API_KEY` 与
   Gemini key。没有 CPA？先读 README 的 LLM 通道一节——没有 LLM 入口时
   选题/校对/封面 lane 会 fail-closed 拒绝，而不是降级。
4. **认识 profile**：读 [profiles/README.md](profiles/README.md)。默认
   profile 是 `lidousha`（示例频道）；`AUTOSLICE_PROFILE` 在进程 import 时
   读取。校验：`python3 scripts/validate_channel_profile.py --profile lidousha
   --config-only`（全量校验会因声纹缺失 BLOCKED，属预期）。
5. **给新频道建 profile**：复制 `profiles/_template/profile.json` 与
   `assets/_template/` 骨架，逐文件填充后用 validator 收敛到 READY。
   哪些代码点仍绑定默认 profile：见
   [docs/profile-coupling.md](docs/profile-coupling.md)——改这些点之前先读
   对应 step 文档。
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
| `docs/profile-coupling.md` | 换 profile 时剩余的默认 profile 耦合点清单 |
| `.agent/skills/` | 可复用的代理技能（发布闭环、标题风格、歌词对轴等） |

## 常用命令

```bash
python3 -m pytest -q                  # 全套件；无凭据可跑（网络层全 mock）
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

## 架构约定

- **债务棘轮**：`tests/test_runtime_architecture.py` 冻结每个超限函数/模块的
  行数，只许降不许升；新增行数=显式改账本并在提交里说明。
- **内容寻址缓存**：声学/裁决调用按输入哈希缓存，重试轮零重复请求。
- **精确重放**：同稿修复用 `subtitle-redelivery-baseline.v2` 逐字节恢复已审
  文本，只有真值台账拥有的区间允许偏离。
