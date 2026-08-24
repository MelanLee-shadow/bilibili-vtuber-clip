# C9 本地 source-fact acquisition

本文件记录 #9 `auto_143025_1112_1285` 的第二阶段、候选私有 source/action 投影。它只落实
“其他 VTuber 视频内话语不做字幕”的逐 cue 范围；不是部署、full dry、apply、上传或公开投稿。

留存 recut（SHA-256 `40025aa2a2727493207a0deea69013d2a8f5f9c49b29003627f398349a847e7e`）与
burned preview（`af7d5d1c7107a950e63029872454a685af48e246df619cbca445e298e9470f12`）均为
1920x1080 H.264/AAC。逐窗口本地检查确认：播放中的视频面板在 26s、50s、84s、100s、120s
持续可见；84s 的内嵌画面实际带有自身字幕。所有 frame MD5、输入 SHA 与决策集合均封存在
`foreign-video-source-action.v1.json`。

仅 25 个完整、单一 guest-audio cue 被标为 `IN_VIDEO` 并弃掉：10--13、15--21、28--32、36--42、46、50。
其余 41 cue 字节冻结。特别地，5、47--49、52 有 host/guest 混合窗口，35/44 是短 guest-labelled
回应但无法可靠来源拆分，均维持 `UNCERTAIN`，没有整条删除。

投影从 66 条 source cue 生成 41 条 reviewed cue，SRT SHA-256 为
`f436e1913b8eb2bd948c37b18bce9c2a9970dd9be07cf114f309a9c162c2b51b`。完整可复算图以
“source 1..66 的升序、精确 drop 集合、其余 cue 原字节保留并连续编号”定义，避免第二份手工
映射漂移；回归测试会验证其覆盖全部 source cue、drop/frozen 互斥及 mixed cue 保留。

此候选私有 authority 仍需在 root 审查后才可被后续 canonical materializer 消费；当前不允许
对标题、封面、边界、任何未点名文本或远端状态产生修改。
