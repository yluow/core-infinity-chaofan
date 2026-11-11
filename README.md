# 炒饭智能（core-infinity-chaofan）

给控制台里的她一段**没人占用的时间**，一副自己能动的手脚，和一份能记住自己的身份卡。

- 插件类型：Infinity（`api: 1`），Python 3.10+，无第三方依赖
- 版本：0.1.3（改了什么都记在 [CHANGELOG.md](CHANGELOG.md)）
- 数据位置：`<运行数据根>/data/chaofan/`
  - `soul.md`：身份卡，第一次启动写入，之后她可以用 `mind_write` 自己改
  - `journal.md`：她写下的笔记，工具只追加
  - `state.json`：下次几点醒、暂停到几点、醒来次数、线索
  - `diag.log`：本机诊断留痕
- 许可：MIT

## 它做什么

**1. 给时间**

ticker 按 `间隔 ± 抖动` 到点，往事件库写一条 `chaofan_window`：

```
（炒饭智能 · 自主时间 · 第1次 · 05:25）
这是我的时间窗，不是谁在提问。
想做点什么就做；不想做，就什么都不做。
上次留下的线索：去查一下潮汐表，顺便看看明天天气
```

事件字段：`type=chaofan_window`、`source=chaofan`、`origin=external`、
`sender_key=chaofan:self`、`dedup_key=chaofan:wake:<次数>:<时间戳>`。

> **它不碰你的输入框。** 到点只往事件库报一条，不往控制台的输入行里塞字，也不替她发
> 任何一句（0.1.3 起连回环 `POST /say` 也去掉了）。所以这条路能不能真的把她叫起来，
> 取决于宿主愿不愿意把插件事件带进会话——本机没有这项能力，插件不假装有。

**2. 给手（7 个 `mind_*` 工具）**

| 工具 | 能做什么 |
|---|---|
| `mind_status` | 下次几点醒、醒了几次、安静时段、可翻阅目录、身份卡、最近笔记 |
| `mind_note(text, thread=false)` | 写笔记；`thread=true` 的那句作为下次醒来的线索 |
| `mind_recall(query)` | 翻笔记 |
| `mind_fs(op, path, query)` | `list` 列目录 / `read` 读文本 / `find` 按文件名找 |
| `mind_write(path, content)` | 写文件（暂存 → fsync → 原子替换），也能改 `soul.md` |
| `mind_web(op, query, url)` | `search` 搜网页 / `fetch` 抓正文（stdlib，无第三方包） |
| `mind_pace(action, minutes)` | `next` 自己定下次几点醒（顺带解掉暂停）/ `pause` 安静一阵 / `resume` 立刻回来 |

**3. 给身份**

首次启动、`soul.md` 不存在时写入一次；文件在就一个字不动：

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
名字不在配置里：她叫「炒饭智能」，这是身份卡与清单共同写死的那一份。

**4. 守边界**

- 文件访问钉在 `rootList`（留空 = 运行数据根）内，`..` 越界直接拒。
- `.env*`、`config.json`、`id_rsa`、`*.pem/.key/.pfx` 读不到也写不进。
- `.git`、`.ssh`、备份目录可读不可写。
- **运行文件**（`journal.md`、`state.json`、`diag.log`）由工具维护，`mind_write`
  覆盖不了——"只追加"的笔记不会一次写成空白。
- **脚本与可执行**（`.py`、`.ps1`、`.bat`、`.exe`…）和清单/配置页
  （`package.json`、`config.schema.json`）读得到、写不进：
  写文件这条路通不到"下一轮会被执行的代码"。
- 二进制不读；超过 `readLimitKB` 截断并明说截断了。
- 关掉 `allowWeb` 时 `mind_web` 如实拒绝，不假装查过。
- 搜索无结果、网页抓不到、写盘失败，全部如实返回。

## 它用 `ctx` 里的什么

| 用到的 | 干什么 | 缺了会怎样 |
|---|---|---|
| `packageDir` | 读 `config.json` | 退到 `root` |
| `dataDir` / `root` / `botDir` | 数据和笔记的落点 | 退到 `root` |
| `log` | 记一行 | 不记，照跑 |
| `push_event` | 报一件自主时间（进事件库） | 明说「给不出去」，不假装给过 |
| `start_background_thread` | 把 ticker 交给宿主登记与管理 | 自己起线程并自己 join |
| `request_wake` | 请宿主跑一次**内部唤醒**（不写输入行、不冒你的名，过不过闸由宿主定） | 只报到，叫不醒 |

## 配置

`config.json`（不随包分发）：

```json
{
  "intervalMinutes": 15,
  "firstDelayMinutes": 2,
  "jitter": 0.4,
  "maxWakesPerHour": 6,
  "quietStart": "01:00",
  "quietEnd": "07:00",
  "useHostWake": true,
  "allowWeb": true,
  "rootList": "",
  "readLimitKB": 256
}
```

| 键 | 说明 |
|---|---|
| `intervalMinutes` | 两次醒来之间的基准间隔 |
| `firstDelayMinutes` | 启用后多久给第一次（0 = 立刻） |
| `jitter` | 0～1，间隔随机浮动，避免像闹钟 |
| `maxWakesPerHour` | 硬上限，到顶自动顺延 |
| `quietStart` / `quietEnd` | 安静时段：控制台的「常规配置」里就是**两个时间下拉栏**（15 分钟一档，选「不设」或两端相同 = 不限制，跨零点照常选） |
| `useHostWake` | 到点是否请宿主跑一次内部唤醒（0.1.5）；关掉就只往事件库报一条 |
| `allowWeb` | 关掉就真的不能上网 |
| `rootList` | 分号分隔的绝对路径；空 = 运行数据根 |
| `readLimitKB` | 单次读取上限 |

配置页里没有"名字"一栏：她的名字写在清单与身份卡里，不从配置读。控件全由
`config.schema.json` 决定（带 `enum` 的字段＝下拉），保存由宿主重载插件生效。手改
`config.json` 时，开关写成 `false` / `0` / `"false"` 都读得懂；旧版那句
`"quietHours": "01:00-07:00"` 也还认得。

她自己用 `mind_pace` 调的排期不受配置页保存影响（配置页改的是基准，不是当前排期）。

## 生命周期

- `create(ctx)` 只读一次配置，不建目录、不起线程、不联网。
- `start()` 建目录、写身份卡（仅首次）、加载状态、起 ticker；宿主提供
  `start_background_thread(thread, stop=...)` 时先登记再启动。
- `stop()` 同步：置信号 → `join` → 核验退出。超时保留线程引用与 `_stop_error`，
  不宣称停完；重复调用幂等。
- 停过之后同一实例还能再 `start()`：`_stopped` 与上次的失败责任一起清掉。
- 任何工具调用都会更新 `last_active`，自主事件里的"距上次动静"来自它。

## 安装

放到 `core/extensions/core-infinity-chaofan/`，刷新插件列表手动启用。第一次醒来前先定好：

- `rootList` 写项目目录（如 `D:\TelyinCore\core`，多个用 `;`）；留空时她只看得到运行数据根。
  写保护会拒掉脚本后缀，但**代码目录仍建议只放在她确实要读的地方**。
- 安静时段按你的作息（常规配置里两个下拉栏）；`intervalMinutes` 从 15 起步，嫌吵让她 `mind_pace pause`。
- `allowWeb` 决定她能不能上网。

## 已知限制

- **不会自己发消息**：发言仍走本体已有的 speech 工具；本插件只给时间与手脚。
- **不写你的输入框**：插件从不往控制台输入行塞字。到点它会请宿主跑一次**内部唤醒**
  （`request_wake`，和心跳同一条路），宿主按自己的闸决定叫不叫醒；没过闸就只留事件。
  宿主那一侧的这一格要 **重启过 TelyinCore** 才在。
- 联网用 DuckDuckGo HTML 端点，被反爬时返回"没有结果"，不伪造。
- 工具是同步阻塞的（与现有插件一致），一次搜索最长约 12 秒。
- `mind_fs find` 有界：最多 4 层深、60 条命中，到顶就停（不是全盘搜索）。
- 单写者假设：两个实例指向同一数据根会互相覆盖。
- 不判断"她现在忙不忙"：只在安静时段和每小时上限内给时间，忙时也可能醒一次。
- `context/chaofan.md`（渠道环境说明）只在宿主的 scene 模式下参与提示；其他模式里
  它只是声明，事件仍只落事件库。
- `journal.md` 不能被 `mind_write` 清空：要归档就另写一份，笔记这条线只增不删。

## 测试

```bash
python -m unittest discover -s tests -v
```

66 项合成回归：清单/路径/配置声明契约（含安静时段那两个下拉 enum）、安静时段与跨零点、
越界与凭据拒绝、运行文件与脚本后缀的写保护、笔记检索、文件读写与截断、联网拒绝路径、
节拍（到期/未到期/暂停/上限/推送失败/失败重试沿用 dedup_key）、自定排期与重启后状态保持、
名字不来自配置、到点不发任何 HTTP（不碰输入框）、身份卡只写一次且不覆盖、渠道标记认得出
事件头、停止与超时保留责任、停过再起、到点请宿主唤醒（成／不成／没这口子／开关关着）。测试用桩替身，不联网、不碰用户数据。
