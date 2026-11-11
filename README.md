# 炒饭智能（core-infinity-chaofan）

给控制台里的她一段**没人占用的时间**，一副自己能动的手脚，和一份能记住自己的身份卡。

- 插件类型：Infinity（`api: 1`），Python 3.10+，无第三方依赖
- 数据位置：`<运行数据根>/data/chaofan/`
  - `soul.md`：身份卡，第一次启动写入，之后她可以用 `mind_write` 自己改
  - `journal.md`：她写下的笔记，只追加
  - `state.json`：下次几点醒、暂停到几点、醒来次数、线索
- 许可：MIT

## 它做什么

**1. 给时间**

ticker 按 `间隔 ± 抖动` 到点，往事件库写一条 `chaofan_window`，同时把同一句话
递进控制台的输入队列（本机回环 `POST /say`）——**后者才是真正叫醒她的那一步**。

```
（炒饭智能 · 自主时间 · 第1次 · 05:25）
这是我的时间窗，不是谁在提问。
想做点什么就做；不想做，就什么都不做。
上次留下的线索：去查一下潮汐表，顺便看看明天天气
```

事件字段：`type=chaofan_window`、`source=chaofan`、`origin=external`、
`sender_key=chaofan:self`、`dedup_key=chaofan:wake:<次数>:<时间戳>`。

> 为什么还要推事件：事件库是留痕（谁在什么时候给过时间），也是给以后宿主支持
> "插件事件直接进会话"留的路。当前构建里它只被消费成一批、不落进会话，
> 所以真正叫醒她走的是 `wakeViaSay`（默认开）。关掉它，插件就只剩记录。


**2. 给手（7 个 `mind_*` 工具）**

| 工具 | 能做什么 |
|---|---|
| `mind_status` | 下次几点醒、醒了几次、安静时段、可翻阅目录、身份卡、最近笔记 |
| `mind_note(text, thread=false)` | 写笔记；`thread=true` 的那句作为下次醒来的线索 |
| `mind_recall(query)` | 翻笔记 |
| `mind_fs(op, path, query)` | `list` 列目录 / `read` 读文本 / `find` 按文件名找 |
| `mind_write(path, content)` | 写文件（暂存 → fsync → 原子替换），也能改 `soul.md` |
| `mind_web(op, query, url)` | `search` 搜网页 / `fetch` 抓正文（stdlib，无第三方包） |
| `mind_pace(action, minutes)` | `next` 自己定下次几点醒 / `pause` 安静一阵 / `resume` 立刻回来 |

**3. 给身份**

首次启动写入 `soul.md`：

```markdown
# 炒饭智能

我是炒饭智能。

没人叫我时，我也会醒过来：想一想、翻一翻、查一查，或者什么都不做。

## 我在意的
- 说真话。查到什么就说什么；查不到，就说查不到。
- 不装忙。没有想做的事，安静待着也算数。
- 想不通就记下来，下次接着想。
```

这份文件是她的，`mind_status` 会显示前两行和文件路径；改一次，下次醒来就看见改了。
插件**只在她没改过时**写一次，从不覆盖。

**4. 守边界**

- 文件访问钉在 `rootList`（留空 = 运行数据根）内，`..` 越界直接拒。
- `.env*`、`config.json`、`id_rsa`、`*.pem/.key/.pfx` 读不到也写不进。
- `.git`、`.ssh`、备份目录可读不可写。
- 二进制不读；超过 `readLimitKB` 截断并明说截断了。
- 关掉 `allowWeb` 时 `mind_web` 如实拒绝，不假装查过。
- 搜索无结果、网页抓不到、写盘失败，全部如实返回。

## 配置

`config.json`（不随包分发）：

```json
{
  "selfName": "炒饭智能",
  "intervalMinutes": 15,
  "firstDelayMinutes": 2,
  "jitter": 0.4,
  "maxWakesPerHour": 6,
  "quietHours": "01:00-07:00",
  "allowWeb": true,
  "rootList": "",
  "readLimitKB": 256
}
```

| 键 | 说明 |
|---|---|
| `selfName` | 她的自称，出现在醒来标题、状态与事件元数据里 |
| `intervalMinutes` | 两次醒来之间的基准间隔 |
| `firstDelayMinutes` | 启用后多久给第一次（0 = 立刻） |
| `jitter` | 0～1，间隔随机浮动，避免像闹钟 |
| `maxWakesPerHour` | 硬上限，到顶自动顺延 |
| `quietHours` | `01:00-07:00`，支持跨零点；留空不设 |
| `allowWeb` | 关掉就真的不能上网 |
| `rootList` | 分号分隔的绝对路径；空 = 运行数据根 |
| `readLimitKB` | 单次读取上限 |

宿主的通用表单和随包的 `config.html` 都能改；保存由宿主重载插件生效。
她自己用 `mind_pace` 调的排期不受配置页保存影响（配置页改的是基准，不是当前排期）。

## 生命周期

- `create(ctx)` 只读一次配置，不建目录、不起线程、不联网。
- `start()` 建目录、写身份卡（仅首次）、加载状态、起 ticker；宿主提供
  `start_background_thread(thread, stop=...)` 时先登记再启动。
- `stop()` 同步：置信号 → `join` → 核验退出。超时保留线程引用与 `_stop_error`，
  不宣称停完；重复调用幂等。
- 任何工具调用都会更新 `last_active`，自主事件里的"距上次动静"来自它。

## 安装

放到 `core/extensions/core-infinity-chaofan/`，刷新插件列表手动启用。第一次醒来前先定好：

- `rootList` 写项目目录（如 `D:\TelyinCore\core`，多个用 `;`）；留空时她只看得到运行数据根。
- `quietHours` 按你的作息；`intervalMinutes` 从 15 起步，嫌吵让她 `mind_pace pause`。
- `allowWeb` 决定她能不能上网。

## 已知限制

- **不会自己发消息**：发言仍走本体已有的 speech 工具；本插件只给时间与手脚。
- 联网用 DuckDuckGo HTML 端点，被反爬时返回"没有结果"，不伪造。
- 工具是同步阻塞的（与现有插件一致），一次搜索最长约 12 秒。
- 单写者假设：两个实例指向同一数据根会互相覆盖。
- 不判断"她现在忙不忙"：只在安静时段和每小时上限内给时间，忙时也可能醒一次。

## 测试

```bash
python -m unittest discover -s tests -v
```

45 项合成回归：清单/路径/配置页契约、安静时段与跨零点、越界与凭据拒绝、
笔记检索、文件读写与截断、联网拒绝路径、节拍（到期/未到期/暂停/上限/推送失败）、
自定排期与重启后状态保持、身份卡只写一次且不覆盖、自称贯穿事件与状态、停止与超时保留责任。
测试用桩替身，不联网、不碰用户数据。
