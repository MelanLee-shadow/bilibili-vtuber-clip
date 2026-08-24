# C9 快车道 private prepare（`auto_143025_1112_1285`）

## 结论

本次没有生成字幕 override、after-image、封面或投稿包。C9 在候选私有阶段被
`BLOCKED_UNSEALED_FOREIGN_VIDEO_CUE_TRUTH` 阻断，不能把机械的“连线”标签或字幕文字当作
“其他 VTuber 视频内话语”的证据。

Ivan 对 #9 的唯一变更范围是：其他 VTuber 视频中自带字幕/话语应直接弃掉，不修复、也不做主播
字幕。它没有授权修复其他 cue、移动边界、改变标题、重做封面，或把不确定/混合 cue 整条删除。

## 已复核的留存 authority

留存 create-only 快照位于
`/private/tmp/fastlane-prep-20260824/9-auto_143025_1112_1285`。它的 manifest SHA-256 为
`c5bdfcda0deded5dd4f66d173886da043755c4e6adf1fcce588d65d9c0aa5a8c`，并绑定 recut 视频
`40025aa2a2727493207a0deea69013d2a8f5f9c49b29003627f398349a847e7e`、burned preview
`af7d5d1c7107a950e63029872454a685af48e246df619cbca445e298e9470f12`、66-cue truth table
`6443a55611778c1934c2de6ef5fe1d2fafd52b69abfbf6872c2d6eaacffcfb52`。

该 table 的每个 `human_source_verdict` 和 `human_action` 都仍是 `PENDING`，且 README 明示
“embedded playback overlap”只是机械候选、不能代替人工 source/action 决定。因此没有一条 cue
可以被证明是可整条弃掉的内嵌视频话语。

另有独立 drift：record 的 `subtitle_sha256` 是
`4b0402dafbffbc9cf3358f21ff09a3d4aa07c052978848b70cc6b860844c2ac1`，而留存 `current.srt`
为 `3327cb186ba44785df255c544e5e7ac1ce989a3872ccb91958a7b43ba77823c7`。这进一步禁止将旧
字幕文件伪装为当前正式字幕真值。

## 逐 cue 结果

66 条 cue 的 dropped 集合为空；1--66 全部冻结，含所有曾被机械标记 overlap 的 cue。这一“冻结”
只表示本次不对它们产生任何字幕修改，并不声称它们都是李豆沙话语。完整索引、哈希与 self-hash
写在
[`auto_143025_1112_1285.foreign-video-truth-blocker.v1.json`](../../assets/lidousha/fastlane_c9_private/auto_143025_1112_1285.foreign-video-truth-blocker.v1.json)。

## 唯一下一步

后续只能建立 candidate/date/hash-bound 的 source/action receipt：记录内嵌视频播放的精确区间，
并逐 affected cue/sub-cue 判定 `IN_VIDEO`、`HOST_LIVE`、`MIXED` 或 `UNCERTAIN`。仅完整
`IN_VIDEO` cue 可 drop；mixed 必须做精确子区间投影，uncertain 继续冻结。该 receipt 生成前，
不得 full-dry、apply、SSH、provider、上传或公开发布。
