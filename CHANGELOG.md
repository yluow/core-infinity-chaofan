# 变更记录

## 0.1.1 — 2026-11-12

让自主时间真的能叫醒她。

- 事件改成宿主接受的形状：`origin=external`、`source=chaofan`、字段只留白名单里的
  （`conversation` 挪进 `meta`）。
- `dedup_key` 带上醒来时刻：否则每次重置后都算"同一条"，会被事件库静默去重。
- 新增回环唤醒 `wakeViaSay`：这个构建里插件事件进得了事件库、也会被消费成一批，
  但不会落进会话；真正叫醒她的是把同一句递进控制台自己的输入队列（`POST /say`，
  与页面同源、单行）。
- `_say_locally` 把多行压成一句（控制台那条进口只收一行，多行是 400）。
- 事件推送失败改为认领 Future 异常，不再把"没落库"当成功。

## 0.1.0 — 2026-11-12

首个版本。

- **自主时间**：按「间隔 ± 抖动」推 `chaofan_window` 事件；安静时段、每小时上限、
  自我暂停均生效。
- **身份卡**：首次启动写入 `data/chaofan/soul.md`，此后只读不覆盖；`mind_status`
  显示前两行与文件路径。
- **7 个工具**：`mind_status`、`mind_note`、`mind_recall`、`mind_fs`、`mind_write`、
  `mind_web`、`mind_pace`。
- **边界**：文件访问限制在 `rootList`；凭据与宿主配置（`.env*`、`config.json`、
  `*.pem` 等）拒绝读写；`.git`/`.ssh` 只读；二进制拒读；超限截断并说明。
- **联网**：DuckDuckGo HTML 搜索与网页正文抓取，`allowWeb=false` 时如实拒绝。
- **生命周期**：`create` 无副作用；`stop` 同步 join 并核验退出，超时保留线程引用
  与失败责任，重复调用幂等。
- **配置**：轻量 schema + 单文件配置页（`postMessage` 桥接，`revision` 校验）。
- **测试**：45 项合成回归。
