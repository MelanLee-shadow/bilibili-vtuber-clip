---
name: channel-census
description: "对主播的历史录播做频道普查，生成或更新选题召回线索；用于准备输入、运行与续跑普查、查看报告和核对线索资产。"
---

# 频道普查操作

先读 [20-selection：频道普查线索](../../../docs/pipeline/20-selection.md#频道普查线索)。
本 skill 只提供操作方法；维度、线索用途、资产接线、漂移与验收均以该节为准。

## 1. 准备录播清单与执行环境

从 [仓库执行位置说明](../../../AGENTS.md#开发主线与协作) 选择能读取本批录播的执行主机，
进入该主机上的仓库根目录。`--host` 只记录出处，不会远程派发任务。
先查看 [普查 CLI](../../../scripts/channel_census.py) 的当前接口：

```bash
python3 scripts/channel_census.py --help
python3 scripts/validate_channel_profile.py --help
```

选择【主播名】对应的现有 profile。普查读取其 metric 和普查配置；配置骨架见
[模板](../../../profiles/_template/channel_census_config.v1.json)，资产接线按
[20-selection](../../../docs/pipeline/20-selection.md#频道普查线索) 操作。
使用独立 manifest 时，用 `--manifest` 替代 `--profile`；临时配置可用 `--config` 指定。

在本次运行目录准备 JSON 清单：顶层 `recordings` 是非空数组，每项有 `path` 和正整数
`duration_ms`。`path` 指向实际录播，相对路径从清单所在目录解析；填入实测完整时长。
可选 `recording_date` 使用 ISO 日期，`danmaku_starts_ms` 填录播内的弹幕起点毫秒数组。
字段格式和 provider 签名见 CLI 的 `--help` 与源码头注释。

下列命令是待填变量模板。先设置本批的 `CENSUS_PROFILE`（频道 id）、`CENSUS_RECORDINGS`
（已有清单路径）、`CENSUS_OUT`（本批独立运行目录）、`CENSUS_HOST`（实际主机）、
`CENSUS_ID`（普查标识）、`CENSUS_LABELER`（可导入的 `module:callable`）和
`CENSUS_LABEL_MODEL`（实际标注模型版本）。这些值不预设频道、主机或 provider。
原始数据与运行产物保存在运行目录，不加入 Git。

## 2. 运行、暂停与续跑

```bash
python3 scripts/channel_census.py \
  --profile "$CENSUS_PROFILE" --recordings "$CENSUS_RECORDINGS" \
  --out "$CENSUS_OUT" --host "$CENSUS_HOST" --census-id "$CENSUS_ID" \
  --labeler "$CENSUS_LABELER" --label-model "$CENSUS_LABEL_MODEL"
```

默认转写入口是已有 BCUT client；标注器没有默认 provider，运行前核实所选适配器及其调用授权。
要注入自定义转写器，同时传 `--transcriber` 和 `--transcript-algorithm`，取值与签名按 CLI。

需要暂停时，在已创建的输出目录放置 STOP 标记：

```bash
touch "$CENSUS_OUT/STOP"
```

工具在阶段间检查标记，以 75 退出；当前 provider 调用不会被该标记立即打断。
等待原进程退出后，移除该标记并重跑原命令：

```bash
rm -- "$CENSUS_OUT/STOP"
```

保持原输入和参数，已完整落盘的阶段可复用；中断的 provider 调用可能重做。
锁占用也返回 75，先核对原进程；不要删除锁文件来抢跑。同一输出目录由工具加锁，
已有产物冲突时保留现场并核对输入，不覆盖缓存。

## 3. 读报告、接线与重新生成

成功时读取标准输出的 `run_dir`，打开其中的 `report.md`：先看覆盖，再看活动与维度、
线索例窗、人物/称呼原样表、月度覆盖和局限；同目录保留 `manifest.json`、`windows.json`
及 `selection_census_clues.v1.json` 供追溯。这些是 CLI 生成的运行文件，不是仓库预置文件。

CLI 只在运行目录生成结果。将结果接入 profile、处理旧手工线索节并验收，均按
[20-selection](../../../docs/pipeline/20-selection.md#频道普查线索) 执行；不要把运行完成当作资产已接线。
接线后在同一执行环境检查：

```bash
python3 scripts/validate_channel_profile.py --profile "$CENSUS_PROFILE"
```

分别读取 `census_config_lint`、`census_clues_status` 和 `missing_runtime_paths`；
线索状态的含义与处理按 [20-selection](../../../docs/pipeline/20-selection.md#频道普查线索)，
不能只看进程退出码。

新增录播、调整 metric/普查配置或更换标注模型/适配器后，记录新输入与版本并重跑，
从新返回的 `run_dir` 读取报告；输入摘要决定阶段复用，不手改旧产物。
只是中断则续跑原命令。每次接入重新生成的资产仍按
[20-selection](../../../docs/pipeline/20-selection.md#频道普查线索) 核对影响与验收。
需要从终审字幕准备人设审核时，转到
[人设批量审核操作](../bilive-autoslice-publish/SKILL.md#7-人设批量审核与投影计划)。
