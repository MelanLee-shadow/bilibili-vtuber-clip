# AGENTS.md — 公开版操作约定

本项目优先由 AI 代理完成配置与验证，人类提供真实频道信息与授权。开始前读 [README.md](README.md)，
修改某一步前读 [流水线索引](docs/pipeline/README.md) 及对应 step。
以下是操作地图，不覆盖代码、schema、profile 或分步规则。

## 首次运行

1. 创建 Python 3.11+ 虚拟环境并安装 `requirements.txt`。
2. 不加载凭据、不设置 `AUTOSLICE_PROFILE`，先执行配置验证、CLI `--help` 和测试：

   ```bash
   .venv/bin/python scripts/validate_channel_profile.py --profile lidousha --config-only
   .venv/bin/python scripts/session_autoslice.py --help
   .venv/bin/python scripts/produce_slice_package.py --help
   .venv/bin/python -m pytest -q
   ```

   离线测试可在普通用户或 root 容器中运行，但真实 consumer-web adapter 不能以 root
   身份运行。root 启动器必须把 `ENTITY_AUDIO_GEMINI_WEB_USER` 指向专用非 root 账号；
   测试夹具模拟该降权边界，不会打开浏览器或读取登录资料。

3. 按 [profiles/README.md](profiles/README.md) 建立自己的频道；词表、人设、标题风格和
   封面身份描述来自频道提供者，不能代为编造。模板资产和真实运营状态必须区分。
4. 按 `.env.example` 与 [凭据说明](docs/credentials.md) 配置服务，再跑 `preflight.py`。
   `.env` 不会自动加载；`set -a; source .env; set +a` 才会导出它。参考部署另读
   `$AUTOSLICE_BASE/cpa.env`。CPA 的文字/视觉能力与局部听音服务要分别验证。
5. 运行 README 的真实切片示例前确认源素材、目录、额度和所需外部工具。
   这不是离线测试：可能联网、写入工作目录并调用计费服务；不保证任何输入都出片。
6. 发布需要另行授权、账号凭据、出版登记和自己的 `AUTOSLICE_SEASON_IDS`；
   不从“制作一个包”的请求推断“允许上传”。

示例 profile 的片头媒体和声纹不随仓分发。配置检查的 READY 不等于声纹已录入，
也不等于所有生产前提齐备。普通样式归属不表示音频身份已经逐段确认。

## 关键不变量

**修产物，不靠放松门通过。** 任何质量门变化都需要独立依据和回归测试。
不能删除测试、静默接受不合法数据或用旧回执替代当前输入的验证。

**证据先于结论。** 只对实际运行过的测试、真实读取的状态和验收过的产物声称通过。
明确区分离线测试、服务体检、真实阶段实验、完整包与公开发布。

**BCUT 是基础，CPA 裁决文字。** 普通谈话先做整片文字校对，再按具体疑点请求局部盲音频证据。
不默认整片 AGY 精听，不以未经验证的 MOSS / MAI 研究替代 BCUT。

**缓存不授予放行权。** 成功的阶段结果只在内容、上下文、配置和代码绑定仍有效时复用。
后置检查仍执行，也可能调用模型；不要承诺最终字幕一致或整个重试零请求。

**已审原稿只改授权范围。** 冻结未受影响内容，按需要重新烧录并检查实际成片。
公共仓库的空台账或兼容占位不是维护者的历史授权。

**上传只走 `authorized_upload.py` 的闭环。** 禁止裸调 `do_upload.sh`。
已发布候选不得重复新投稿，修复走同 BV 路径；`review_ready` 不等于已发布。

## 仓库地图

| 位置 | 用途 |
|---|---|
| `src/autoslice/` | 流水线实现、契约、门和可复用模块 |
| `scripts/README.md` | 按用途组织的 CLI 地图 |
| `scripts/session_autoslice.py` | runner；`--smoke-segment` 是单段真实制作入口 |
| `scripts/produce_slice_package.py` | 指定候选的制作入口，spec 以其文档为准 |
| `docs/pipeline/` | 分步规则及其机器强制层 |
| `profiles/`、`assets/_template/` | 频道配置与资产骨架 |
| `tests/`、`.github/workflows/ci.yml` | 隔离回归测试与实际 CI 命令 |
| `.agent/skills/` | 操作配方；不能反向覆盖分步规则 |

## 配置、兼容与验证

默认 profile 是 `lidousha`，在进程 import 时读取；不要在长进程运行中切换。
频道知识只放进 profile 资产，不硬编码到业务逻辑。部署路径按自己的实际机器配置；
公共版媒体宿主的参考默认是 `localhost`，部分音频入口仍可能需要 SSH。

持久证据词汇保留 `lidousha-` 拼写以兼容旧包，例如 schema、`lidousha_role`、
`human_reviewed_lidousha`、`verified_lidousha_voiceprint`、`LIDOUSHA_*` 兼容环境变量和
`lidousha_centrality`。这些兼容键不是当前频道身份，不能为美观直接重命名。

修改后先跑相关回归，再跑全量公开测试；记录 Python 版本、commit、命令、通过和跳过范围。
`tests/test_runtime_architecture.py` 的债务基线不能为了过测试随意上调。
模型调用点的标识、effort 与环境覆盖范围要从当前源码核实，不能照搬旧 README 或私有部署值。

凭据、真实账号状态、私有授权和声纹不得提交。安全问题使用 [SECURITY.md](SECURITY.md)
的私密渠道；贡献流程见 [CONTRIBUTING.md](CONTRIBUTING.md)。
