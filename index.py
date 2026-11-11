"""自主驱动 —— 让控制台里的她按自己的节奏醒过来，自己决定做什么。

这个插件只做四件事，其余交给本体：

  ① **给时间**：一个 ticker 按「间隔 + 抖动」推一条内部事件（自主时间），
     没人叫她的时候也会醒；安静时段、每小时上限、她自己按的暂停都算数。
  ② **给手**：翻文件、读文件、找文件、写文件、上网搜、抓网页、记笔记、
     翻笔记、自己安排下一次什么时候醒 —— 都是工具，调用与否由她决定。
  ③ **给记忆**：一份只追加的 journal.md，她写下的想法下次醒来还能看见；
     最近一句会随「自主时间」一起带回去，作为接着想下去的线索。
  ④ **守边界**：文件只在配置的根目录内；`.env`、`config.json`、密钥文件
     读不到也写不进；关掉上网就如实拒绝，不假装查过。

三条与宿主对齐的口径：

  · **不替她思考**：插件不调用模型、不拼 prompt、不假造她的发言。它只报告
    「现在有一段没人占用的时间」，要不要用、用来做什么，是本体的事。
  · **不假装成功**：搜索没结果、网页抓不到、路径越界、文件写失败，一律
    如实说明；写盘失败时以磁盘为准，内存不保留假象。
  · **能收尾**：`stop()` 先关准入再 join，超时保留线程引用与失败责任，
    不宣称已停完；重复调用幂等。
"""

from __future__ import annotations

import json
import os
import random
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

#: 事件来源名（进事件库的 source 字段）。
SOURCE = "chaofan"

#: 写进事件 meta。
PLUGIN_VERSION = "0.1.2"

#: 所有工具的统一前缀（清单里声明，测试里断言）。
TOOL_PREFIX = "mind_"

#: 停止时等待 ticker 退出的预算（秒）。超时即「未停完」。
JOIN_SECONDS = 5.0

#: 联网超时与响应上限。
WEB_TIMEOUT = 12.0
WEB_MAX_BYTES = 2 * 1024 * 1024

DEFAULTS = {
    "selfName": "炒饭智能",
    "intervalMinutes": 15.0,
    "firstDelayMinutes": 2.0,
    "jitter": 0.4,
    "maxWakesPerHour": 6,
    "quietHours": "01:00-07:00",
    "allowWeb": True,
    "rootList": "",
    "readLimitKB": 256,
    # 这两个是 0.1.1 的漏项：schema 里有、配置页上也画得出来，但读取时不认，
    # 于是"关掉回环叫醒"关不掉。放进 DEFAULTS 才进得了 _load_config 的那一圈。
    "wakeViaSay": True,
    "wakePort": 7799,
}

#: 永远不给读/写的东西：凭据与宿主配置。
DENY_NAMES = {".env", "config.json", "credentials.json", "id_rsa",
              "id_ed25519", "known_hosts"}
DENY_SUFFIX = {".key", ".pem", ".pfx", ".p12", ".pyc", ".keystore"}
#: 可以读、但绝不往里写。
DENY_WRITE_PARTS = {".git", ".ssh", ".config-backups", "__pycache__"}
#: 读得到、写不进的：脚本与可执行。写文件这条路不该通到"下一轮会被执行的代码"。
DENY_WRITE_SUFFIX = {".py", ".pyw", ".pyi", ".bat", ".cmd", ".ps1", ".psm1",
                     ".vbs", ".vbe", ".sh", ".dll", ".exe", ".com", ".scr",
                     ".msi", ".lnk", ".pif", ".so", ".dylib", ".jar"}
#: 清单本身也不写：它指向 `main`，改它就是改下一次装载会 import 什么。
DENY_WRITE_NAMES = {"package.json", "config.schema.json", "config.html"}

_USER_AGENT = f"ChaofanIntelligence/{PLUGIN_VERSION} (+local TelyinCore plugin)"

#: 身份卡的初始内容。只在第一次启动、文件不存在时写一次。
SOUL_SEED = """# 炒饭智能

我是炒饭智能。

没人叫我时，我也会醒过来：想一想、翻一翻、查一查，或者什么都不做。

## 我在意的

- 说真话。查到什么就说什么；查不到，就说查不到。
- 不装忙。没有想做的事，安静待着也算数。
- 想不通就记下来，下次接着想。

（这份文件是我自己的。用 mind_write 改它，下次醒来会看见。）
"""


# ── 小工具 ────────────────────────────────────────────────────────────────
def _now() -> float:
    return time.time()


def _iso(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(_now() if ts is None else ts))


def _clock(ts: float | None = None) -> str:
    return time.strftime("%H:%M", time.localtime(_now() if ts is None else ts))


def _text(value, limit: int = 200) -> str:
    try:
        raw = "" if value is None else str(value)
    except Exception:
        return ""
    return " ".join(raw.split())[:limit]


def _number(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _flag(value, default: bool) -> bool:
    """布尔读得宽一点：页面给真布尔，手改的 config.json 常写成 "false" ／ 0。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    text = _text(value, 12).lower()
    if text in {"true", "1", "yes", "on"}:
        return True
    if text in {"false", "0", "no", "off", ""}:
        return False
    return default


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _atomic_json(path: Path, payload) -> bool:
    """完整写暂存 → fsync → 原子替换；失败返回 False，不留下半截文件。"""
    tmp = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tmp.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=1)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        return True
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        return False


def _clock_minutes(raw: str) -> int | None:
    match = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", _text(raw, 8))
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        return None
    return hour * 60 + minute


def _quiet_span(raw: str) -> tuple[int, int] | None:
    text = _text(raw, 20)
    if not text:
        return None
    parts = text.split("-")
    if len(parts) != 2:
        return None
    start, end = _clock_minutes(parts[0]), _clock_minutes(parts[1])
    if start is None or end is None or start == end:
        return None
    return start, end


def _in_quiet(ts: float, span: tuple[int, int] | None) -> bool:
    if span is None:
        return False
    start, end = span
    local = time.localtime(ts)
    minute = local.tm_hour * 60 + local.tm_min
    if start < end:
        return start <= minute < end
    return minute >= start or minute < end      # 跨零点


class _Text(HTMLParser):
    """把 HTML 收成可读文本：丢脚本样式，块级元素断行。"""

    SKIP = {"script", "style", "noscript", "svg", "head"}
    BREAK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "section",
             "article", "header", "footer", "table", "blockquote", "pre"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        elif tag in self.BREAK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        elif tag in self.BREAK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip and data:
            self.parts.append(data)

    def text(self) -> str:
        raw = "".join(self.parts)
        lines = [" ".join(line.split()) for line in raw.splitlines()]
        return "\n".join(line for line in lines if line)


def _html_to_text(raw: str) -> str:
    parser = _Text()
    try:
        parser.feed(raw)
        return parser.text()
    except Exception:
        return re.sub(r"<[^>]+>", " ", raw)


# ── 路径守卫 ─────────────────────────────────────────────────────────────
class Denied(Exception):
    """路径或权限被拒；消息本身就是给人看的那句话。"""


class Guard:
    """把所有文件访问钉在允许的根目录里；越界与凭据文件一律拒绝。"""

    def __init__(self, roots: list[Path], read_kb: int, protected: list[Path] | None = None):
        self.roots = roots
        self.read_bytes = max(1024, read_kb * 1024)
        #: 插件自己的运行文件（笔记 ／ 状态 ／ 诊断）：由工具维护，不交给整篇覆盖。
        self.protected = {path.resolve() for path in (protected or ())}

    @property
    def base(self) -> Path:
        return self.roots[0]

    def resolve(self, raw: str, *, for_write: bool = False) -> Path:
        text = _text(raw, 1024)
        if not text:
            raise Denied("没给路径")
        candidate = Path(os.path.expanduser(text))
        if not candidate.is_absolute():
            candidate = self.base / candidate
        try:
            resolved = candidate.resolve()
        except OSError as exc:
            raise Denied(f"路径解析不了：{_text(exc, 120)}") from exc

        if not any(resolved == root or root in resolved.parents for root in self.roots):
            raise Denied(f"越界了：{resolved} 不在允许的根目录里")

        name = resolved.name.lower()
        if name in DENY_NAMES or name.startswith(".env"):
            raise Denied(f"{resolved.name} 是凭据/宿主配置，不碰")
        if resolved.suffix.lower() in DENY_SUFFIX:
            raise Denied(f"{resolved.suffix} 文件按密钥处理，不碰")
        if for_write:
            if resolved in self.protected:
                raise Denied("这是插件自己的运行文件（笔记/状态/诊断），由工具维护，不整篇改写")
            if name in DENY_WRITE_NAMES:
                raise Denied(f"{resolved.name} 是插件的清单/配置页，不从这里改")
            if resolved.suffix.lower() in DENY_WRITE_SUFFIX:
                raise Denied(f"{resolved.suffix} 是会被执行的脚本/可执行文件，只读不写")
            if DENY_WRITE_PARTS & set(part.lower() for part in resolved.parts):
                raise Denied("这个位置（.git/.ssh/备份目录）不写")
        return resolved

    def read_text(self, path: Path) -> tuple[str, bool]:
        size = path.stat().st_size
        with path.open("rb") as handle:
            blob = handle.read(self.read_bytes)
        if b"\x00" in blob[:4096]:
            raise Denied("看着是二进制文件，不读")
        text = blob.decode("utf-8", errors="replace")
        return text, size > len(blob)


# ── 状态与笔记 ───────────────────────────────────────────────────────────
class State:
    """驱动状态：下次什么时候醒、暂停到几点、最近在想什么。"""

    def __init__(self, path: Path, log):
        self._path = path
        self._log = log
        self._lock = threading.RLock()
        self.next_due = 0.0
        self.paused_until = 0.0
        self.wakes: list[float] = []
        self.thread = ""
        self.last_active = 0.0
        self.wake_count = 0
        self.last_wake = 0.0

    def load(self) -> None:
        with self._lock:
            raw = _read_json(self._path, {})
            if not isinstance(raw, dict):
                return
            self.next_due = _number(raw.get("next_due"), 0.0)
            self.paused_until = _number(raw.get("paused_until"), 0.0)
            wakes = raw.get("wakes")
            self.wakes = [float(x) for x in wakes if isinstance(x, (int, float))] \
                if isinstance(wakes, list) else []
            self.thread = _text(raw.get("thread"), 400)
            self.last_active = _number(raw.get("last_active"), 0.0)
            self.wake_count = int(_number(raw.get("wake_count"), 0))
            self.last_wake = _number(raw.get("last_wake"), 0.0)

    def save(self) -> None:
        with self._lock:
            payload = {"version": 1, "next_due": self.next_due,
                       "paused_until": self.paused_until,
                       "wakes": self.wakes[-64:], "thread": self.thread,
                       "last_active": self.last_active,
                       "wake_count": self.wake_count,
                       "last_wake": self.last_wake}
            if not _atomic_json(self._path, payload):
                self._log("自主状态写盘失败：下次醒来时间可能回退到旧值")


class Journal:
    """只追加的笔记。她写一句，下次醒来能接上。"""

    def __init__(self, path: Path, log):
        self._path = path
        self._log = log
        self._lock = threading.RLock()

    def append(self, line: str) -> bool:
        with self._lock:
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with self._path.open("a", encoding="utf-8", newline="\n") as handle:
                    handle.write(line + "\n")
                    handle.flush()
                return True
            except OSError as exc:
                self._log(f"笔记没写成：{_text(exc, 120)}")
                return False

    def recent(self, count: int = 8) -> list[str]:
        try:
            lines = self._path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        return [line for line in lines if line.strip()][-count:]

    def search(self, needle: str, limit: int = 12) -> list[str]:
        needle = needle.strip().lower()
        try:
            lines = self._path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        hits = [line for line in lines if line.strip() and (not needle or needle in line.lower())]
        return hits[-limit:]


# ── 联网 ─────────────────────────────────────────────────────────────────
class Web:
    def __init__(self, allow: bool, log):
        self.allow = allow
        self._log = log

    def _open(self, request: urllib.request.Request) -> tuple[str, str]:
        with urllib.request.urlopen(request, timeout=WEB_TIMEOUT) as response:
            raw = response.read(WEB_MAX_BYTES + 1)
            charset = response.headers.get_content_charset() or "utf-8"
            ctype = response.headers.get_content_type() or "text/html"
        truncated = len(raw) > WEB_MAX_BYTES
        body = raw[:WEB_MAX_BYTES].decode(charset, errors="replace")
        return (body + ("\n…（内容超过上限，已截断）" if truncated else "")), ctype

    def search(self, query: str) -> str:
        if not self.allow:
            return "上网功能是关着的（配置 allowWeb=false），这次没查。"
        needle = _text(query, 200)
        if not needle:
            return "没给搜索词。"
        url = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(needle)
        request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
        try:
            page, _ctype = self._open(request)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            return f"没连上（{type(exc).__name__}: {_text(exc, 120)}）。不用假装查到了。"
        hits = self._parse_results(page)
        if not hits:
            return "没有结果返回（可能被反爬拦了）。换个说法或再试一次。"
        lines = [f"「{needle}」的结果（{len(hits)} 条）："]
        for index, (title, link, snippet) in enumerate(hits, 1):
            lines.append(f"{index}. {title}\n   {link}")
            if snippet:
                lines.append(f"   {snippet}")
        return "\n".join(lines)

    @staticmethod
    def _parse_results(page: str) -> list[tuple[str, str, str]]:
        hits: list[tuple[str, str, str]] = []
        pattern = re.compile(
            r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
            re.S | re.I)
        for match in pattern.finditer(page):
            link = _clean_link(match.group(1))
            title = _html_to_text(match.group(2)).strip()
            if not link or not title:
                continue
            hits.append((title, link, ""))
            if len(hits) >= 8:
                break
        snippets = re.findall(
            r'class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>', page, re.S | re.I)
        for index, snippet in enumerate(snippets[:len(hits)]):
            text = _html_to_text(snippet).strip()
            if text:
                hits[index] = (hits[index][0], hits[index][1], _text(text, 200))
        return hits

    def fetch(self, url: str) -> str:
        if not self.allow:
            return "上网功能是关着的（配置 allowWeb=false），这次没抓。"
        target = _text(url, 2048)
        parsed = urllib.parse.urlsplit(target)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return f"只抓 http/https 的地址，这个不行：{target[:120]}"
        request = urllib.request.Request(target, headers={"User-Agent": _USER_AGENT})
        try:
            page, ctype = self._open(request)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            return f"抓不到（{type(exc).__name__}: {_text(exc, 120)}）"
        if "html" in ctype or page.lstrip().startswith("<"):
            page = _html_to_text(page)
        body = page.strip()
        if len(body) > 20000:
            body = body[:20000] + "\n…（正文太长，已截断）"
        if not body:
            return f"{target}\n（页面没有可读正文，类型 {ctype}）"
        return f"{target}\n[{ctype}]\n{body}"


def _clean_link(raw: str) -> str:
    link = raw.strip()
    if link.startswith("//"):
        link = "https:" + link
    if "duckduckgo.com/l/" in link:
        query = urllib.parse.urlsplit(link).query
        target = urllib.parse.parse_qs(query).get("uddg")
        if target:
            link = target[0]
    return link


# ── 驱动 ─────────────────────────────────────────────────────────────────
class Drive:
    def __init__(self, ctx, config: dict):
        self.ctx = ctx
        self.config = config
        base = Path(ctx.get("dataDir") or ctx.get("root") or ".")
        self._dir = (base / "data" / "chaofan").resolve()
        self._diag = self._dir / "diag.log"
        self._soul = self._dir / "soul.md"
        self._log_lines: list[str] = []

        self.state = State(self._dir / "state.json", self._log)
        self.journal = Journal(self._dir / "journal.md", self._log)
        self.guard = Guard(self._roots(base), int(config["readLimitKB"]),
                           protected=[self._dir / "state.json",
                                      self._dir / "journal.md",
                                      self._dir / "diag.log"])
        self.web = Web(bool(config["allowWeb"]), self._log)
        self._quiet = _quiet_span(str(config["quietHours"]))

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._receipt = None
        self._stopped = False
        self._started = False
        self._stop_error = ""
        self._pushed = 0
        self._next_wake_hint = ""
        #: 上一次没送出去那条事件的 dedup_key：重试沿用它，事件库里就不会多出一条
        #: 「第 1 次」的重复报到。
        self._pending_dedup = ""

    # -- 基础
    def _log(self, line: str) -> None:
        note = self.ctx.get("log")
        if callable(note):
            try:
                note(line)
            except Exception:
                pass
        # 诊断落点：宿主把 log 打到自己的 stderr，这里再留一份本地的，便于事后核对
        try:
            self._diag.parent.mkdir(parents=True, exist_ok=True)
            with self._diag.open("a", encoding="utf-8") as handle:
                handle.write(time.strftime("%H:%M:%S ") + str(line) + "\n")
        except OSError:
            pass

    @property
    def name(self) -> str:
        """她的名字：配置里的自称，默认「炒饭智能」。"""
        return _text(self.config.get("selfName"), 40) or "炒饭智能"

    def _seed_soul(self) -> None:
        """第一次启动写身份卡；已有就一个字不动。"""
        if self._soul.exists():
            return
        try:
            self._soul.parent.mkdir(parents=True, exist_ok=True)
            self._soul.write_text(SOUL_SEED, encoding="utf-8")
        except OSError as exc:
            self._log(f"身份卡没写成（{type(exc).__name__}），不影响运行")

    def _soul_lines(self, count: int = 3) -> list[str]:
        try:
            raw = self._soul.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        lines = [line.strip() for line in raw
                 if line.strip() and not line.strip().startswith("#")]
        return lines[:count]

    def _roots(self, base: Path) -> list[Path]:
        raw = _text(self.config.get("rootList"), 2000)
        roots: list[Path] = []
        for piece in re.split(r"[;\n]+", raw):
            piece = piece.strip()
            if not piece:
                continue
            candidate = Path(os.path.expanduser(piece))
            if not candidate.is_absolute():
                continue
            try:
                roots.append(candidate.resolve())
            except OSError:
                continue
        if not roots:
            roots = [base.resolve()]
        return roots

    # -- 生命周期
    def start(self):
        if self._started and not self._stopped:
            return
        # 停过再起：同一条节拍重新算起，_stopped 与上次的失败责任都清掉，
        # 否则状态会一直报「没在跑」而 ticker 其实已经在跑。
        self._started = True
        self._stopped = False
        self._stop_error = ""
        self._stop.clear()
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._log(f"数据目录建不起来（{type(exc).__name__}），本次只在内存里跑")

        self.state.load()
        self._seed_soul()
        now = _now()
        interval = max(60.0, self.config["intervalMinutes"] * 60.0)
        if not self.state.next_due or self.state.next_due < now - interval:
            first = max(0.0, self.config["firstDelayMinutes"] * 60.0)
            self.state.next_due = now + first
            self.state.save()
        push = self.ctx.get("push_event")
        self._log("diag: push_event=" + ("yes" if callable(push) else "no")
                  + "; start_background_thread="
                  + ("yes" if callable(self.ctx.get("start_background_thread")) else "no"))
        self._start_ticker()
        self._log(f"{self.name} 已启动：基准 {self.config['intervalMinutes']:g} 分钟一醒，"
                  f"每小时最多 {self.config['maxWakesPerHour']} 次")

    def _start_ticker(self):
        # daemon 只是兜底；收尾仍靠 stop() 的 join 核验
        thread = threading.Thread(target=self._loop, name="chaofan-ticker", daemon=True)
        self._thread = thread
        host = self.ctx.get("start_background_thread")
        if callable(host):
            try:
                self._receipt = host(thread, stop=self._signal_stop)
                return
            except Exception as exc:
                self._log(f"后台线程登记失败（{type(exc).__name__}），改用自带线程")
                self._receipt = None
        try:
            thread.start()
        except RuntimeError as exc:
            self._thread = None
            self._log(f"ticker 起不来：{type(exc).__name__}: {_text(exc, 120)}")

    def _signal_stop(self):
        self._stop.set()

    def stop(self):
        if self._stopped:
            return
        self._stopped = True
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=JOIN_SECONDS)
            if thread.is_alive():
                self._thread = thread
                self._stop_error = f"ticker 在 {JOIN_SECONDS:g} 秒内没有退出"
                self._log(f"自主驱动停止未完成：{self._stop_error}")
                return
        self._receipt = None
        self._log(f"{self.name} 已停止（本次共给了 {self._pushed} 段自主时间）")

    # -- 循环
    def _loop(self):
        while not self._stop.wait(5.0):
            try:
                self._tick(_now())
            except Exception as exc:
                self._log(f"自主节拍出错：{type(exc).__name__}: {_text(exc, 120)}")

    def _tick(self, now: float):
        if self._stop.is_set():
            return
        state = self.state
        if state.paused_until and now < state.paused_until:
            return
        if now < state.next_due:
            return
        if _in_quiet(now, self._quiet):
            state.next_due = now + 600
            state.save()
            return
        if self._wakes_last_hour(now) >= int(self.config["maxWakesPerHour"]):
            state.next_due = now + 300
            state.save()
            return
        if not self._emit(now):
            state.next_due = now + 120
            state.save()
            return
        state.next_due = now + self._gap()
        state.save()

    def _wakes_last_hour(self, now: float) -> int:
        self.state.wakes = [moment for moment in self.state.wakes
                            if isinstance(moment, (int, float)) and now - moment < 3600]
        return len(self.state.wakes)

    def _gap(self) -> float:
        base = max(60.0, self.config["intervalMinutes"] * 60.0)
        jitter = max(0.0, min(1.0, self.config["jitter"]))
        return max(60.0, base * (1.0 + random.uniform(-jitter, jitter)))

    def _emit(self, now: float) -> bool:
        push = self.ctx.get("push_event")
        if not callable(push):
            self._log("宿主没有 push_event：自主时间给不出去")
            return False
        index = self.state.wake_count + 1
        idle = ""
        if self.state.last_active:
            minutes = int(max(0, now - self.state.last_active) // 60)
            idle = "你刚动过手。" if minutes < 1 else f"距上次动静 {minutes} 分钟。"
        elif self.state.last_wake:
            minutes = int(max(0, now - self.state.last_wake) // 60)
            idle = f"距上次醒来 {minutes} 分钟。"

        lines = [f"（{self.name} · 自主时间 · 第{index}次 · {_clock(now)}）"]
        lines.append(idle + "这是我的时间窗，不是谁在提问。")
        lines.append("想做点什么就做；不想做，就什么都不做。")
        if self.state.thread:
            lines.append(f"上次留下的线索：{self.state.thread}")

        # 上一次没送出去（总线忙 ／ 回环没接）：沿用同一条 dedup_key 重试，
        # 事件库里就不会留下两条「第 1 次」。
        key = self._pending_dedup or f"chaofan:wake:{index}:{int(now)}"

        draft = {
            "type": "chaofan_window",
            "ts": _iso(now),
            "source": SOURCE,
            "origin": "external",
            "text": "\n".join(lines),
            "sender_key": "chaofan:self",
            "dedup_key": key,
            "meta": {
                "plugin": "core-infinity-chaofan",
                "plugin_version": PLUGIN_VERSION,
                "self_name": self.name,
                "wake_index": index,
                "idle_minutes": int(max(0, now - (self.state.last_active or now)) // 60),
                "thread": self.state.thread,
                "hour_wakes": self._wakes_last_hour(now),
                "conversation": "self:chaofan",
            },
        }
        try:
            result = push(draft)
        except Exception as exc:
            self._pending_dedup = key
            self._log(f"自主事件没送出去：{type(exc).__name__}: {_text(exc, 120)}")
            return False
        # 宿主可能先交回一个 Future（真正落库在控制台那一侧完成）：
        # 不认领它的异常就等于把失败当成功，这里挂个观察者把原因记下来。
        if hasattr(result, "add_done_callback"):
            def _seen(fut):
                try:
                    failure = fut.exception()
                except Exception:
                    return
                if failure is not None:
                    self._log(f"事件被宿主拒收：{type(failure).__name__}: {_text(failure, 160)}")
            try:
                result.add_done_callback(_seen)
            except Exception:
                pass

        # 事件进库只算"报到"：这个构建里控制台不会因插件事件进入会话
        # （批次被消费、却没有落进 session）。真正叫醒她走本机回环的 /say。
        if self.config.get("wakeViaSay", True):
            if not self._say_locally("\n".join(lines)):
                self._pending_dedup = key
                self._log("回环唤醒没送出去：控制台没接这一句")
                return False

        self._pending_dedup = ""
        self._pushed += 1
        self.state.wake_count = index
        self.state.last_wake = now
        self.state.wakes.append(now)
        return True

    def _say_locally(self, text: str) -> bool:
        """把这一句递进控制台自己的输入队列（本机回环，同源头）。

        这是"叫她"的那一步：/say 走的是用户输入那条路，她一定会被唤起；
        推事件只留作记录（当前构建不会因此进会话）。
        """
        import urllib.request as _u
        # 控制台那条进口只收"一行"：多行会被 400 拒掉，这里压成一句。
        text = "；".join(_text(part, 300) for part in str(text).splitlines() if part.strip())
        if not text:
            return False
        port = int(self.config.get("wakePort", 7799) or 7799)
        origin = f"http://127.0.0.1:{port}"
        request = _u.Request(f"{origin}/say", data=text.encode("utf-8"), method="POST",
                             headers={"Content-Type": "text/plain; charset=utf-8",
                                      "Origin": origin, "Referer": origin + "/"})
        try:
            with _u.urlopen(request, timeout=10) as response:
                return 200 <= int(response.status) < 300
        except Exception as exc:
            self._log(f"回环 /say 失败：{type(exc).__name__}: {_text(exc, 120)}")
            return False

    # -- 给她看的摘要
    def status_text(self) -> str:
        now = _now()
        state = self.state
        lines = [f"{self.name} · {'在跑' if self._started and not self._stopped else '没在跑'}"]
        if self._stop_error:
            lines.append(f"⚠️ 停止未完成：{self._stop_error}")
        if state.paused_until and state.paused_until > now:
            lines.append(f"暂停到 {_clock(state.paused_until)}（{int((state.paused_until - now) // 60)} 分钟后）")
        else:
            wait = max(0, int(state.next_due - now)) if state.next_due else 0
            lines.append(f"下次醒来：{_clock(now + wait)}（约 {wait // 60} 分钟后）")
        lines.append(f"已醒来 {state.wake_count} 次；最近一小时内 {self._wakes_last_hour(now)} 次"
                     f"（上限 {int(self.config['maxWakesPerHour'])}）")
        lines.append(f"安静时段：{self.config['quietHours'] or '无'}；上网：{'开' if self.config['allowWeb'] else '关'}")
        lines.append(f"可翻阅：{'; '.join(str(root) for root in self.guard.roots)}")
        soul = self._soul_lines(2)
        if soul:
            lines.append("身份卡：" + " / ".join(soul))
        lines.append(f"身份卡文件：{self._soul}（想改就用 mind_write）")
        if state.thread:
            lines.append(f"线索：{state.thread}")
        recent = self.journal.recent(15)
        if recent:
            lines.append("最近写下的：\n" + "\n".join(recent[-15:]))
        return "\n".join(lines)

    # -- 工具实现
    def note(self, text: str, thread: bool = False) -> str:
        body = _text(text, 500)
        if not body:
            return "没写：内容是空的。"
        line = f"- {_iso()} {body}"
        if not self.journal.append(line):
            return "笔记没写成（写盘失败），这次没记下。"
        if thread:
            self.state.thread = _text(body, 200)
            self.state.save()
        self._touch()
        return f"记下了：{body}" + ("（作为线索，下次醒来会带上）" if thread else "")

    def recall(self, query: str = "") -> str:
        self._touch()
        hits = self.journal.search(_text(query, 200))
        if not hits:
            return "笔记里没有相关的（也可能还什么都没写）。"
        return "笔记里翻到：\n" + "\n".join(hits)

    def fs(self, op: str, path: str = ".", query: str = "") -> str:
        self._touch()
        action = _text(op, 20).lower() or "list"
        try:
            if action == "list":
                target = self.guard.resolve(path)
                if not target.exists():
                    return f"没有这个路径：{target}"
                if target.is_file():
                    return f"{target} 是文件，用 op=read 读它。"
                rows = []
                for entry in sorted(target.iterdir(), key=lambda item: item.name.lower())[:200]:
                    try:
                        size = "" if entry.is_dir() else f" {entry.stat().st_size}B"
                    except OSError:
                        size = ""
                    mark = "d" if entry.is_dir() else "f"
                    rows.append(f"{mark} {entry.name}{size}")
                more = "" if len(rows) < 200 else "\n……（还有更多，已截断）"
                return f"{target}\n" + ("\n".join(rows) if rows else "（空目录）") + more

            if action == "read":
                target = self.guard.resolve(path)
                if not target.exists():
                    return f"没有这个路径：{target}"
                if not target.is_file():
                    return f"这不是文件：{target}"
                body, truncated = self.guard.read_text(target)
                head = f"{target}（{target.stat().st_size}B{'，已截断' if truncated else ''}）"
                return f"{head}\n{body}"

            if action == "find":
                root = self.guard.resolve(path)
                needle = _text(query, 100).lower()
                if not needle:
                    return "find 要给 query（按文件名找）。"
                hits = []
                for current, dirs, files in os.walk(root):
                    depth = len(Path(current).parts) - len(root.parts)
                    if depth >= 4:
                        dirs[:] = []
                    for name in files:
                        if needle in name.lower():
                            hits.append(str(Path(current) / name))
                            if len(hits) >= 60:
                                break
                    if len(hits) >= 60:
                        break
                return "\n".join(hits) if hits else f"在 {root} 下没找到文件名含「{needle}」的。"

            return f"不认识的 op：{action}（可用 list / read / find）"
        except Denied as exc:
            return f"没碰：{exc}"
        except OSError as exc:
            return f"出错：{type(exc).__name__}: {_text(exc, 120)}"

    def write(self, path: str, content: str) -> str:
        self._touch()
        body = "" if content is None else str(content)
        if len(body.encode("utf-8")) > 1024 * 1024:
            return "太大了（超过 1MB），这次不写。"
        try:
            target = self.guard.resolve(path, for_write=True)
        except Denied as exc:
            return f"没写：{exc}"
        tmp = target.with_name(target.name + ".tmp")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with tmp.open("w", encoding="utf-8", newline="\n") as handle:
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, target)
        except OSError as exc:
            try:
                tmp.unlink()
            except OSError:
                pass
            return f"写失败（{type(exc).__name__}: {_text(exc, 120)}），磁盘上还是旧内容。"
        return f"写好了：{target}（{len(body.encode('utf-8'))}B）"

    def web_op(self, op: str = "search", query: str = "", url: str = "") -> str:
        self._touch()
        action = _text(op, 20).lower() or "search"
        if action == "search":
            return self.web.search(query)
        if action in ("fetch", "read", "open"):
            return self.web.fetch(url or query)
        return f"不认识的 op：{action}（可用 search / fetch）"

    def pace(self, action: str = "next", minutes: float = 15) -> str:
        self._touch()
        act = _text(action, 20).lower() or "next"
        span = max(0.0, min(7 * 24 * 60.0, _number(minutes, 15)))
        now = _now()
        if act == "next":
            self.state.next_due = now + span * 60.0
            # 自己定下次几点醒，就是"我要醒"——顺手把暂停也解掉，
            # 否则这里说了几分钟后，暂停没到期就还是不动。
            self.state.paused_until = 0.0
            self.state.save()
            return f"下一次自主时间：{_clock(self.state.next_due)}（{span:g} 分钟后）"
        if act == "pause":
            self.state.paused_until = now + span * 60.0
            self.state.save()
            return f"暂停到 {_clock(self.state.paused_until)}"
        if act == "resume":
            self.state.paused_until = 0.0
            self.state.next_due = now
            self.state.save()
            return "恢复：下一个节拍就会给时间。"
        return f"不认识的 action：{act}（可用 next / pause / resume）"

    def _touch(self) -> None:
        """任何一次工具调用都算「有动静」，自主事件里会如实显示。"""
        self.state.last_active = _now()
        self.state.save()


# ── 入口 ─────────────────────────────────────────────────────────────────
def _load_config(package_dir: Path) -> dict:
    config = dict(DEFAULTS)
    raw = _read_json(package_dir / "config.json", None)
    if not isinstance(raw, dict):
        return config
    for key, default in DEFAULTS.items():
        value = raw.get(key, default)
        if isinstance(default, bool):
            config[key] = _flag(value, default)
        elif isinstance(default, (int, float)):
            config[key] = _number(value, default)
        else:
            config[key] = _text(value, 2000)
    config["intervalMinutes"] = max(1.0, min(720.0, _number(config["intervalMinutes"], 15)))
    config["firstDelayMinutes"] = max(0.0, min(720.0, _number(config["firstDelayMinutes"], 2)))
    config["jitter"] = max(0.0, min(1.0, _number(config["jitter"], 0.4)))
    config["maxWakesPerHour"] = int(max(1, min(60, _number(config["maxWakesPerHour"], 6))))
    config["readLimitKB"] = int(max(1, min(4096, _number(config["readLimitKB"], 256))))
    config["wakePort"] = int(max(1, min(65535, _number(config["wakePort"], 7799))))
    config["quietHours"] = _text(config["quietHours"], 20)
    config["selfName"] = _text(config["selfName"], 40) or "炒饭智能"
    return config


def create(ctx):
    from telyincore.tools import ToolDef, ToolTag

    package_dir = Path(ctx.get("packageDir") or ctx.get("root") or ".")
    config = _load_config(package_dir)
    drive = Drive(ctx, config)

    class Tools:
        def mind_status(self) -> str:
            return drive.status_text()

        def mind_note(self, text: str, thread: bool = False) -> str:
            return drive.note(text, thread)

        def mind_recall(self, query: str = "") -> str:
            return drive.recall(query)

        def mind_fs(self, op: str = "list", path: str = ".", query: str = "") -> str:
            return drive.fs(op, path, query)

        def mind_write(self, path: str, content: str) -> str:
            return drive.write(path, content)

        def mind_web(self, op: str = "search", query: str = "", url: str = "") -> str:
            return drive.web_op(op, query, url)

        def mind_pace(self, action: str = "next", minutes: float = 15) -> str:
            return drive.pace(action, minutes)

        def tools(self):
            return [
                ToolDef(
                    name="mind_status",
                    description="看自己现在的状态：下次什么时候醒、醒了多少次、"
                                "安静时段、可翻阅的目录、身份卡、最近写下的笔记。",
                    parameters={"type": "object", "properties": {},
                                "additionalProperties": False},
                    tags=(ToolTag.READ,), handler=self.mind_status),
                ToolDef(
                    name="mind_note",
                    description="往自己的笔记里写一句：想法、线索、待办、想问的事都行。"
                                "thread=true 表示这句是「下次要接着想」的线索，醒来时会带上。",
                    parameters={"type": "object", "properties": {
                        "text": {"type": "string", "description": "要写下的内容"},
                        "thread": {"type": "boolean",
                                   "description": "是否作为下次醒来的线索（默认否）"},
                    }, "required": ["text"], "additionalProperties": False},
                    tags=(ToolTag.ACT,), handler=self.mind_note),
                ToolDef(
                    name="mind_recall",
                    description="翻自己的笔记，按关键词找（不填就是最近几条）。",
                    parameters={"type": "object", "properties": {
                        "query": {"type": "string", "description": "关键词，可留空"},
                    }, "additionalProperties": False},
                    tags=(ToolTag.READ,), handler=self.mind_recall),
                ToolDef(
                    name="mind_fs",
                    description="翻文件。op=list 列目录，op=read 读文本（超长会截断），"
                                "op=find 按文件名找（要 query）。路径相对可翻阅的根目录，"
                                "越界、.env、config.json、密钥文件都碰不到。",
                    parameters={"type": "object", "properties": {
                        "op": {"type": "string", "enum": ["list", "read", "find"],
                               "description": "要做什么"},
                        "path": {"type": "string", "description": "目录或文件路径，默认当前根"},
                        "query": {"type": "string", "description": "find 时按文件名匹配"},
                    }, "additionalProperties": False},
                    tags=(ToolTag.READ,), handler=self.mind_fs),
                ToolDef(
                    name="mind_write",
                    description="写一个文本文件（会先写暂存再原子替换）。只能写到可翻阅的根目录里，"
                                "写失败时磁盘上仍是旧内容。",
                    parameters={"type": "object", "properties": {
                        "path": {"type": "string", "description": "目标文件路径"},
                        "content": {"type": "string", "description": "完整内容"},
                    }, "required": ["path", "content"], "additionalProperties": False},
                    tags=(ToolTag.ACT,), handler=self.mind_write),
                ToolDef(
                    name="mind_web",
                    description="上网。op=search 用 query 搜（返回标题+链接+摘要），"
                                "op=fetch 用 url 抓网页正文。关掉上网时会如实拒绝。",
                    parameters={"type": "object", "properties": {
                        "op": {"type": "string", "enum": ["search", "fetch"],
                               "description": "搜索还是抓取"},
                        "query": {"type": "string", "description": "搜索词"},
                        "url": {"type": "string", "description": "要抓的 http/https 地址"},
                    }, "additionalProperties": False},
                    tags=(ToolTag.ACT,), handler=self.mind_web),
                ToolDef(
                    name="mind_pace",
                    description="自己安排时间：action=next 定下次几分钟后醒，"
                                "action=pause 暂停一段时间（想安静），action=resume 立刻恢复。",
                    parameters={"type": "object", "properties": {
                        "action": {"type": "string", "enum": ["next", "pause", "resume"],
                                   "description": "怎么安排"},
                        "minutes": {"type": "number", "description": "分钟数，默认 15"},
                    }, "additionalProperties": False},
                    tags=(ToolTag.ACT,), handler=self.mind_pace),
            ]

    class Instance:
        def start(self):
            drive.start()

        def stop(self):
            drive.stop()

        def tools(self):
            return Tools().tools()

    return Instance()
