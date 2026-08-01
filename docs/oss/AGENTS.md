# AGENTS.md — 给 AI 代理的项目地图

本仓是一条 fail-closed、证据链驱动的 B 站直播切片流水线。给代理的三条铁律：

1. **过不了门就修产物，不许修门**。所有质量门（字幕真值、边界、封面身份、
   出版登记）默认拒绝；让某个门"变松"的改动必须有独立证据并写进提交信息。
2. **证据先于结论**。"已运行/已通过/已交付"只能来自实际执行输出；改字幕
   必须有出处（平台弹幕/礼物记录、独立听写、闭集裁决回执）。
3. **运营状态 ≠ 代码**。出版登记、真值台账、评审契约是部署自己的数据
   （本仓只有模板）；不要把示例 profile 的内容当成约束。

## 仓库地图

| 位置 | 内容 |
|---|---|
| `docs/pipeline/README.md` | 分步权威文档入口（源录像→选片→边界→字幕→歌切→标题/封面→打包→发布） |
| `src/autoslice/` | 全部管线模块（约 190 个文件） |
| `scripts/produce_slice_package.py` | 单候选产线入口 |
| `scripts/free_session_autoslice.py` | 无人值守 runner（cron 驱动） |
| `scripts/authorized_upload.py` | 发布/同稿修复的唯一副作用入口 |
| `profiles/`、`assets/lidousha/` | 频道 profile 模板与完整实战示例 |
| `.agent/skills/` | 可复用的代理技能（发布闭环、标题风格、歌词对轴等） |

## 常用命令

```bash
python3 -m pytest -q                 # 全套件（个别用例需要自建 CPA 端点）
python3 scripts/produce_slice_package.py --spec <spec.json> --ssh-host localhost
```

## 架构约定

- **债务棘轮**：`tests/test_runtime_architecture.py` 冻结每个超限函数/模块的
  行数，只许降不许升；新增行数=显式改账本并在提交里说明。
- **内容寻址缓存**：声学/裁决调用按输入哈希缓存，重试轮零重复请求。
- **精确重放**：同稿修复用 `subtitle-redelivery-baseline.v2` 逐字节恢复已审
  文本，只有真值台账拥有的区间允许偏离。
