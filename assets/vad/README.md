# assets/vad — 随仓分发的 VAD 模型

- `silero_vad.onnx`：silero-vad **v5**（[snakers4/silero-vad](https://github.com/snakers4/silero-vad)，MIT 许可）。
  sha256 `2623a2953f6ff3d2c1e61740c6cdb7168133479b267dfef114a4a3cc5bdd788f`。
- 消费方：`scripts/silero_vad_spans.py`（字幕时轴 QA 的语音区间证据）。模型解析
  顺序：`AUTOSLICE_VAD_MODEL` env → 脚本同目录 → 本目录。
- 参考部署（媒体在远端宿主时）：把脚本与模型一起放到
  `<host>:/opt/bilive/vad/`，并设 `AUTOSLICE_VAD_SCRIPT` 指向远端脚本路径。
- 校准注意：响 BGM 下 recall 低（喊叫/笑声可能打 0 分）。管线只把 VAD 区间当
  **正向证据**——没检出语音永远不足以删除字幕 cue。
