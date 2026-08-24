# C4 快车道 private prepare（`auto_113028_1602_1698`）

## 目的与范围

本文件只准备 2026-08-19 Ivan 快车道裁定 #4 的候选私有执行面。它不构成
上传、部署、apply、公开投稿或 Ivan 二次审片授权。C4 没有当前可验证 BVID，故不得把它
误作 same-BV 修复；实际投稿仍须在 Qixi-first 队列之后、按原审片顺序由单一 uploader
执行。

唯一可变文本范围来自
[`2026-08-19-ivan-review-batch-rulings.md`](2026-08-19-ivan-review-batch-rulings.md)：

1. 约 `0:13`：`见面发现，あれ？`。
2. 约 `0:36` / `0:52`：《花海》日文哼唱不做字幕。
3. 标题围绕「你竟然嗑男的女的，违背这个直播间世界观了」。

这不是新的 StoryContract 或全内容复审。除上列文本修复外，字幕文字冻结；标题要求是
方向性要求，不把这里的示例擅自当成一个已批准的精确标题。封面也没有 C4 专属的重做
裁定：只能在完整私有 after-image 中对实际选择的标题与候选 cover 做常规 bytes/QC，
不能把 cover 检查扩大为新的叙事门。

## 已复核的可复用 authority

现有候选基线全部在版本库中，可由 canonical runner 自动发现：

| 项目 | 已确认值 |
|---|---|
| source diagnostic | `assets/lidousha/reviewed_subtitle_baselines/auto_113028_1602_1698.pipeline-diagnostic.srt`，SHA-256 `512f74a7159c36c88b6c49d48ef46055f9caa6b094d4c6bf77336fe29a8956d5`，24 cues |
| frozen release SRT | `auto_113028_1602_1698.reviewed.srt`，SHA-256 `46e769a270867a3f3eb12047778d4628151771539ec7d67e4cb4040c02e31efd`，20 cues |
| decision ledger | `auto_113028_1602_1698.operator-decisions.v3.json`，SHA-256 `3b860b2cc00ffaed8ab7d888094c1a41433bf3bee87cf0d03efbec73ee0390f8` |
| exact source binding | basename `22966160_20260814-11-30-28.mp4`，SHA-256 `20c2fc8ccf054c038eb2e308a9ed2a0bb00593de8c7e8895e87ae0b1ee13734c`，`[1592760, 1746900)` ms |
| projection | 24 source cues → 20 reviewed/release cues；cue 3 exact replacement；source cues 8/12/13/14 drop；其余 19 cues unchanged freeze |

本次在独立 worktree 上重新 materialize 该 authority。输出 SRT、pipeline diagnostic、
decision ledger 和 diagnostic diff 分别与上述版本库字节完全相同；materializer 输出为
20 cues、5 changed decisions。临时输出未被安装到 runtime 或提交为另一份 authority。

历史 live 证据（不是本次新执行）已记录 C4 在 deployed `5f90525` 的一次
`FULL_DRY_RUN`：`rc=0`、`READY_TO_COMMIT`、`upload_allowed=false`，所有 preparation
predicate 通过，speaker `READY`、guess `null`，且 formal state/record/publish before/after
未变。见
[`2026-08-24-fastlane-source-and-live-readiness.md`](2026-08-24-fastlane-source-and-live-readiness.md)。
因此旧 private stage 已清理、其 package/cover/media 不可当作当前可上传成品复用；可复用的
是 hash-bound subtitle authority 与 runner 路径，不是旧 stage 的路径或 receipt。

## 本地验证

在本 worktree 运行：

```text
python3 -m pytest -q \
  tests/test_materialize_operator_reviewed_subtitle_baseline.py \
  tests/test_reviewed_baseline_replay.py \
  tests/test_historical_fastlane_authority.py
```

结果：`105 passed`（仅两条第三方 `pypinyin` deprecation warnings）。该组覆盖 exact
materialization、24→20 drop projection、full-dry candidate-private after-image 路径及 historical
authority binding；其中 replay 测试明确断言 release cue 3 为 `见面发现，あれ？`。

## 下一次受控动作

在 root 审查并按队列轮到 C4 后，worker 只需一次新的 candidate-private full dry：

```text
python3 scripts/replay_reviewed_subtitle_baseline.py \
  --full-dry-run --runtime-root /opt/bilive/autoslice \
  --date 2026-08-14 --candidate-id auto_113028_1602_1698 \
  --private-stage-parent /opt/bilive/autoslice/private-fastlane-preflight
```

该命令不得带 `--apply`，不得调用 uploader，也不得复用旧 private stage。它必须重新从当前
deployed identity、source binding 和 baseline authority 生成 candidate-private after-image，
确认：字幕四 drop + cue 3；其余冻结文本；实际标题符合 C4 方向且不触犯全局标题铁律；
实际 cover/metadata 与 package bytes 一致；package audit 和 receipt 均通过。若任一项漂移，
fail closed，而不是补做未点名内容修复。

只有该 full dry 合格且 root 接受 exact artifact receipt 后，才由后续 worker 依次执行短
commit lease 的 `--apply`、canonical package/upload manifest 与串行 `authorized_upload.py`
投稿；每一步仍需当时的 live state 和 manifest readback。C4 未发现现存 BVID，故下一次投稿
应为新稿而非 same-BV repair；投稿后再核对 Bilibili public、Creator 与 section 三面。
