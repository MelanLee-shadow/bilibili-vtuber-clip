试点 golden 直接复制自 channel-knowledge-design-20261025/pilot/out，未重新生成期望值。

pilot-input-summary.json 从同一份七十二个标签文件提取全部窗口的最小统计输入；保留原文件 SHA，不含转写、摘要或真实人物字符串（只保留人物是否出现）。称呼原样保留，起止时间由窗口号重建，不参与统计。旧试点的 input_sha256 是按文件名排序的标签文件 SHA 对的规范 JSON 哈希；新标注则使用 manifest 与窗口的规范 JSON 哈希。
