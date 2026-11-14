# AGENTS.md — 公开版操作约定

本项目优先由 AI 代理完成配置与验证，人类提供真实频道信息与授权。开始前读 [README.md](README.md)，
修改某一步前读 [流水线索引](docs/pipeline/README.md) 及对应 step。
以下是操作地图，不覆盖代码、schema、profile 或分步规则。

公开文档、首次配置问卷、写法建议、校正机制和频道示例都是可复用资产。保留详细的
Agent 配置流程与李豆沙示例，供使用者按自己的频道选择、替换或删减；不能仅因它们
详细、包含频道身份或维护者判例，就当成隐私或冗余删掉。改变产品定位、缩短或删除
这些资产时，必须在 PR 中逐项说明具体理由与影响。实际凭据和私有授权仍不得提交。

## 首次运行

1. 创建 Python 3.11+ 虚拟环境并安装 `requirements.txt`。
2. 不加载凭据、不设置 `AUTOSLICE_PROFILE`，先执行配置验证、CLI `--help` 和测试：

   ```bash
   .venv/bin/python scripts/validate_channel_profile.py --profile lidousha --config-only
   .venv/bin/python scripts/manual_lane.py --help
   .venv/bin/python -m pytest -q
   ```

3. 按 [profiles/SETUP.md](profiles/SETUP.md) 从零搭起来，按 [profiles/README.md](profiles/README.md) 建立自己的频道；词表、人设、标题风格和
   封面身份描述来自频道提供者，不能代为编造。模板资产和真实运营状态必须区分。
4. 按 [凭据说明](docs/credentials.md) 配置服务。文字/看图模型、复听模型和整片识别服务要分别验证。
5. 真实制作前确认源素材、目录、额度和所需外部工具。这不是离线测试：会联网、写入工作目录并调用计费服务。
6. 发布不在这一版里：出版登记、上传和同稿修订需要频道自己的发布工具。
   不从“制作一个包”的请求推断“允许上传”。

示例 profile 的片头媒体和声纹不随仓分发。配置检查的 READY 不等于声纹已录入，也不等于所有生产前提齐备。

## 关键不变量

**修产物，不靠放松检查通过。** 任何检查的变化都需要独立依据和回归测试。
不能删除测试、静默接受不合法数据或用旧结果替代当前输入的验证。

**证据先于结论。** 只对实际运行过的测试、真实读取的状态和验收过的产物声称通过。
明确区分离线测试、服务体检、真实制作、完整包与公开发布。

**识别稿是底稿，模型按证据定字。** 字幕由三个独立的代理各写一遍、逐处取多数；拿不准的句子复听；
弹幕、名表、人设和圈内指称是证据，不是答案。没有出处、各来源连读音都不一致的名字不自己推一个写上去。

**唯一不放行的是“定不下且影响意思”。** 流水线照常做到成片包，只是不封存。

## 仓库地图

| 位置 | 用途 |
|---|---|
| `src/autoslice/manual_lane/` | 流水线：各步骤、检查、提示词渲染 |
| `src/autoslice/`（其余） | 流水线用到的支撑模块：选片打分、排版、名表、封面画布、知识图等 |
| `scripts/manual_lane.py` | 入口：`discover`、`auto`、`run`、`status`、`revise` 等 |
| `assets/manual_lane/` | 规则数值（`lane_rules.json`）、提示词、媒体主机上运行的工具 |
| `docs/pipeline/` | 分步规则 |
| `profiles/`、`assets/_template/` | 频道配置与资产骨架 |
| `tests/`、`.github/workflows/ci.yml` | 隔离回归测试与实际 CI 命令 |
| `.agent/skills/` | 操作配方；不能反向覆盖分步规则 |

## 配置与验证

默认 profile 是 `lidousha`，在进程 import 时读取；不要在长进程运行中切换。
频道知识只放进 profile 资产，不硬编码到业务逻辑。

修改后先跑相关回归，再跑全量公开测试；记录 Python 版本、commit、命令、通过和跳过范围。
模型调用点的标识与档位要从当前源码和 `assets/manual_lane/lane_rules.json` 核实。

凭据、真实账号状态、私有授权和声纹不得提交。安全问题使用 [SECURITY.md](SECURITY.md)
的私密渠道；贡献流程见 [CONTRIBUTING.md](CONTRIBUTING.md)。
