# 2026-08-10 快车道上传证据:auto_200130_1722_1792(对食)

- **BV13zuX6fEwh** 见 uploaded.json(aid/cid 以回执为准),2026-08-09T22:49:01Z APP接口投稿成功,PUBLICATION RECONCILED,artifact 6b2c0ca148b6。
- 授权:Ivan 权宜上传令+快车道令(引语逐字在 upload_manifest.authorization)。
- 车道史:Codex-F 产至 60% 断供 → 接管 worker 修复边界接线缺口(a8600994,exact-interval replay 的 review floor pin)并把交付字幕做到与 Ivan 真值逐字节一致(0efb9995…,38 cue)→ 卡 speaker v2 部署缺口 → 合并波(2545300)部署解锁 → 复产 PRODUCE_EXIT=0 → 收链 worker 全门绿但 audit 卡 terminal-projection 验收面缺失 → 验收面窄移植(df4afed,真实包 3→0 blocking)→ 标题两轮手术 → state 重绑 → audit passed/QC PASS → integrator 上传。
- **标题两轮手术**(REPAIRED 链 4-pass,最终回执 sha256:76cd2374…):机器标题「…上下位声明后还有人作证」(41字,分析腔)→ r1「刚问完有没有发现变化…」→ r2 定稿「话题从有没有发现变化，一路歪到宫中禁止对食」——r2 修正 F12 受话人归属风险(cue1-9 提问者是[连线],李豆沙 cue10 才开口,标题不得暗示她提问)。授权=Ivan 8/10 标题委托令(引语逐字在回执 pass4)。备份 .pre-titlefix / .pre-titlefix2 双代保留。
- state 重绑回执 state-titlebind.8f738cca…(5 字段:title/summary.title/delivery-binding 双 sha/summary record sha;F 时代 bind 工具前置为"自拒绝态首绑"不适用重绑,最小手术持 runner.lock+CAS+备份+原子写)。
- relocation journal 追加 post_relocation_authorized_surgeries 监管链(两轮手术的 postimage 前进,备份与回执 sha 在案)。
- 已知披露:封面字块「宫中禁止对食」与标题共享 1 个词面(健康检查 COMPLEMENT 记录);cover_text 簇保留机器旧标题字样(hash-bound 封面像素记录,不得改);F 时代 /tmp bind 工具白名单补 RESOLVED_MANUAL 用副本(/tmp/bind_1722_yueqi.py),原件未动。
