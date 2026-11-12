"""合成回归：只证明这份代码里被测到的分支，不证明真实宿主已安装。

跑法：python -m unittest discover -s tests -v
"""

from __future__ import annotations

import contextlib
import json
import re
import shutil
import sys
import threading
import time
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _install_tool_stub() -> None:
    """宿主不在这个进程里：只替身 telyincore.tools，不替身插件本身。"""
    if "telyincore.tools" in sys.modules:
        return

    class ToolDef:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)
            self.name = kwargs.get("name", "")
            self.parameters = kwargs.get("parameters", {})
            self.tags = kwargs.get("tags", ())

    class ToolTag:
        READ = "READ"
        ACT = "ACT"
        SPEECH = "SPEECH"

    tools_mod = types.ModuleType("telyincore.tools")
    tools_mod.ToolDef = ToolDef
    tools_mod.ToolTag = ToolTag
    pkg = types.ModuleType("telyincore")
    pkg.tools = tools_mod
    pkg.__path__ = []
    sys.modules["telyincore"] = pkg
    sys.modules["telyincore.tools"] = tools_mod


_install_tool_stub()

import index as plugin  # noqa: E402


class FakeCtx(dict):
    """够用的宿主替身：只给本插件真会用到的键。"""

    def __init__(self, package_dir: Path, root: Path, with_host_thread: bool = False,
                 with_wake: bool = False):
        super().__init__(packageDir=str(package_dir), root=str(root), name="demo")
        self.logs: list[str] = []
        self.events: list[dict] = []
        self.wakes: list[str] = []
        self.wake_answer = {"ok": True, "why": "model_lane_available"}
        self.push_error: Exception | None = None
        self.with_host_thread = with_host_thread
        self.registered: list[threading.Thread] = []
        if with_host_thread:
            self["start_background_thread"] = self._start_background_thread
        if with_wake:
            self["request_wake"] = self._request_wake

    def get(self, key, default=None):
        if key == "log":
            return self.logs.append
        if key == "push_event":
            return self._push
        return super().get(key, default)

    def _push(self, draft: dict) -> None:
        if self.push_error is not None:
            raise self.push_error
        self.events.append(draft)

    def _request_wake(self, text: str) -> dict:
        self.wakes.append(text)
        return self.wake_answer

    def _start_background_thread(self, thread, stop=None):
        self.registered.append(thread)
        thread.start()
        return object()          # 不可取消的观察回执


TMP_ROOT = ROOT / ".test-tmp"
_case_seq = 0


@contextlib.contextmanager
def workdir():
    """临时目录放在插件目录里：不写系统临时区，也不碰用户数据。"""
    global _case_seq
    _case_seq += 1
    path = TMP_ROOT / f"case-{_case_seq:03d}"
    path.mkdir(parents=True, exist_ok=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def make_ctx(tmp: Path, **kwargs) -> FakeCtx:
    return FakeCtx(ROOT, tmp, **kwargs)


def make_drive(ctx: FakeCtx, **overrides) -> "plugin.Drive":
    config = dict(plugin.DEFAULTS)
    config.update({"firstDelayMinutes": 0, "jitter": 0, "quietHours": "",
                   "intervalMinutes": 1})
    config.update(overrides)
    drive = plugin.Drive(ctx, config)
    drive.start()
    return drive


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.manifest = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))

    def test_required_fields(self):
        for key in ("name", "version", "author", "license", "main", "description"):
            self.assertIsInstance(self.manifest.get(key), str, key)
            self.assertTrue(self.manifest[key].strip(), key)
        self.assertIn("core-infinity", self.manifest["keywords"])

    def test_name_is_one_safe_segment(self):
        name = self.manifest["name"]
        self.assertRegex(name, r"^[a-z0-9][a-z0-9._-]*$")
        for bad in ("/", "\\", ":", ".."):
            self.assertNotIn(bad, name)
        self.assertFalse(name.endswith((".", " ")))
        self.assertNotIn(name.lower(), {"con", "prn", "aux", "nul", "com1", "lpt1"})

    def test_api_and_kind(self):
        meta = self.manifest["telyincore"]
        self.assertEqual(meta["kind"], "infinity")
        self.assertEqual(meta["api"], 1)
        self.assertTrue(meta["displayName"].strip())

    def test_pointers_stay_inside_plugin_dir(self):
        meta = self.manifest["telyincore"]
        root = ROOT.resolve()
        for key in ("main", "configSchema"):
            value = meta.get(key, self.manifest.get(key))
            target = (ROOT / value).resolve()
            self.assertTrue(str(target).startswith(str(root)), f"{key} 越界: {target}")
            self.assertTrue(target.is_file(), f"{key} 不存在: {target}")

    def test_no_custom_config_page(self):
        """配置只用 schema 驱动的通用表单（安静时段靠 enum 出两个下拉），不随包带页面。"""
        self.assertNotIn("configPage", self.manifest["telyincore"])
        self.assertFalse((ROOT / "config.html").exists())

    def test_channel_marker_matches_the_wake_header(self):
        """渠道标记要认得出事件头（名字是插件自己的，不是配置项）。"""
        meta = self.manifest["telyincore"]
        pattern = meta["channels"][0]["markers"][0]["pattern"]
        marker = re.compile(pattern)
        with workdir() as tmp:
            ctx = make_ctx(tmp)
            drive = make_drive(ctx)
            try:
                drive.state.next_due = time.time() - 1
                drive._tick(time.time())
            finally:
                drive.stop()
        first = ctx.events[0]["text"].splitlines()[0]
        self.assertTrue(marker.search(first), f"{pattern!r} 认不出 {first!r}")
        self.assertIn(plugin.SELF_NAME, first)


class QuietFieldTests(unittest.TestCase):
    """安静时段：schema 里是两个带 enum 的键，通用表单据此渲染两个下拉。"""

    def setUp(self):
        self.schema = json.loads((ROOT / "config.schema.json").read_text(encoding="utf-8"))
        self.example = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))

    def test_two_enum_fields_render_as_dropdowns(self):
        props = self.schema["properties"]
        for name in ("quietStart", "quietEnd"):
            self.assertIn(name, props, name)
            field = props[name]
            self.assertEqual(field["type"], "string", name)
            self.assertIsInstance(field.get("enum"), list, name)
            self.assertGreaterEqual(len(field["enum"]), 96, name)      # 15 分钟一档
            self.assertIn(field["default"], field["enum"], name)
            self.assertIn("不设", field["enum"], name)
        self.assertNotIn("quietHours", props, "旧的一句式写法不再出现在 schema 里")

    def test_enum_values_are_quarter_hours(self):
        values = [v for v in self.schema["properties"]["quietStart"]["enum"] if v != "不设"]
        self.assertEqual(values[0], "00:00")
        self.assertEqual(values[-1], "23:45")
        for value in values:
            self.assertRegex(value, r"^\d{2}:\d{2}$")
            hour, minute = value.split(":")
            self.assertLess(int(hour), 24)
            self.assertIn(int(minute), (0, 15, 30, 45))


class SchemaTests(unittest.TestCase):
    def setUp(self):
        self.schema = json.loads((ROOT / "config.schema.json").read_text(encoding="utf-8"))
        self.example = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))

    def test_lightweight_shape(self):
        self.assertEqual(self.schema.get("type"), "object")
        props = self.schema["properties"]
        self.assertTrue(props)
        for name, field in props.items():
            self.assertIn(field["type"], {"string", "number", "integer", "boolean"}, name)
            self.assertIsInstance(field.get("title"), str, name)

    def test_example_matches_defaults(self):
        for name, field in self.schema["properties"].items():
            self.assertIn(name, self.example, name)
            self.assertEqual(self.example[name], field.get("default"), name)

    def test_config_loader_clamps(self):
        with workdir() as tmp:                      # 空目录：没有 config.json ⇒ 全默认
            config = plugin._load_config(tmp)
            self.assertGreaterEqual(config["intervalMinutes"], 1)
            self.assertLessEqual(config["jitter"], 1)
            self.assertIsInstance(config["allowWeb"], bool)
            self.assertEqual(config["quietHours"], "01:00-07:00", "默认两个下拉要合成一句时段")

    def test_quiet_pair_becomes_one_span(self):
        with workdir() as tmp:
            (tmp / "config.json").write_text(
                json.dumps({"quietStart": "22:00", "quietEnd": "08:00"}), encoding="utf-8")
            config = plugin._load_config(tmp)
            self.assertEqual(config["quietHours"], "22:00-08:00")
            self.assertEqual(plugin._quiet_span(config["quietHours"]), (1320, 480))

    def test_quiet_off_cases(self):
        """选「不设」、或两端同一时刻 ⇒ 不设安静时段。"""
        with workdir() as tmp:
            for pair in ({"quietStart": "不设", "quietEnd": "07:00"},
                         {"quietStart": "01:00", "quietEnd": "不设"},
                         {"quietStart": "07:00", "quietEnd": "07:00"}):
                (tmp / "config.json").write_text(json.dumps(pair), encoding="utf-8")
                self.assertEqual(plugin._load_config(tmp)["quietHours"], "", pair)

    def test_legacy_quiet_hours_still_loads(self):
        """0.1.3 及更早的 config.json 只有一句式 quietHours，读起来不能丢。"""
        with workdir() as tmp:
            (tmp / "config.json").write_text(
                json.dumps({"quietHours": "23:30-06:30"}), encoding="utf-8")
            self.assertEqual(plugin._load_config(tmp)["quietHours"], "23:30-06:30")

    def test_load_config_accepts_a_file_path(self):
        with workdir() as tmp:
            path = tmp / "cfg.json"
            path.write_text(json.dumps({"quietStart": "22:00", "quietEnd": "08:00"}),
                            encoding="utf-8")
            self.assertEqual(plugin._load_config(path)["quietHours"], "22:00-08:00")

    def test_config_json_with_bom_still_loads(self):
        """Windows 那头存出来的 config.json 带 BOM：不能因此整份当成没有。"""
        with workdir() as tmp:
            (tmp / "config.json").write_bytes(
                b"\xef\xbb\xbf" + json.dumps(
                    {"quietStart": "22:00", "quietEnd": "08:00", "readLimitKB": 64}
                ).encode("utf-8"))
            config = plugin._load_config(tmp)
            self.assertEqual(config["quietHours"], "22:00-08:00")
            self.assertEqual(config["readLimitKB"], 64)

    def test_boolean_written_as_text_is_read(self):
        """手改的 config.json 常把开关写成 "false" ／ 0，不能读成真。"""
        with workdir() as tmp:
            (tmp / "config.json").write_text(
                json.dumps({"allowWeb": "false"}), encoding="utf-8")
            self.assertIs(plugin._load_config(tmp)["allowWeb"], False)
            (tmp / "config.json").write_text(
                json.dumps({"allowWeb": 0}), encoding="utf-8")
            self.assertIs(plugin._load_config(tmp)["allowWeb"], False)

    def test_name_is_not_a_config_field(self):
        """名字是身份卡的事，不再做成配置栏；config.json 里写了也不作数。"""
        self.assertNotIn("selfName", self.schema["properties"])
        self.assertNotIn("selfName", self.example)
        self.assertNotIn("selfName", plugin.DEFAULTS)
        with workdir() as tmp:
            (tmp / "config.json").write_text(
                json.dumps({"selfName": "小炒"}), encoding="utf-8")
            drive = plugin.Drive(make_ctx(tmp), plugin._load_config(tmp))
            self.assertEqual(drive.name, plugin.SELF_NAME)

    def test_quiet_hours_wire_format_is_still_a_span(self):
        """页面是两个下拉栏，存下来仍是 HH:MM-HH:MM：插件这边不用改解析。"""
        with workdir() as tmp:
            (tmp / "config.json").write_text(
                json.dumps({"quietHours": "22:00-08:00"}), encoding="utf-8")
            config = plugin._load_config(tmp)
            self.assertEqual(plugin._quiet_span(config["quietHours"]), (1320, 480))


class QuietTests(unittest.TestCase):
    def test_span_parsing(self):
        self.assertEqual(plugin._quiet_span("01:00-07:00"), (60, 420))
        self.assertEqual(plugin._quiet_span("22:00-08:00"), (1320, 480))
        self.assertIsNone(plugin._quiet_span(""))
        self.assertIsNone(plugin._quiet_span("1:00"))
        self.assertIsNone(plugin._quiet_span("25:00-26:00"))

    def test_wrap_around(self):
        span = plugin._quiet_span("22:00-08:00")
        night = time.mktime(time.strptime("2026-11-12 23:30", "%Y-%m-%d %H:%M"))
        noon = time.mktime(time.strptime("2026-11-12 12:00", "%Y-%m-%d %H:%M"))
        self.assertTrue(plugin._in_quiet(night, span))
        self.assertFalse(plugin._in_quiet(noon, span))


class GuardTests(unittest.TestCase):
    def setUp(self):
        self._ctx = workdir()
        self.root = self._ctx.__enter__()
        self.guard = plugin.Guard([self.root.resolve()], 1)

    def tearDown(self):
        self._ctx.__exit__(None, None, None)

    def test_relative_resolves_under_root(self):
        self.assertEqual(self.guard.resolve("notes.txt"), self.root.resolve() / "notes.txt")

    def test_escape_is_denied(self):
        for bad in ("../outside.txt", "C:\\Windows\\win.ini", "/etc/passwd"):
            with self.assertRaises(plugin.Denied, msg=bad):
                self.guard.resolve(bad)

    def test_secrets_are_denied(self):
        for name in (".env", ".env.local", "config.json", "id_rsa", "server.pem", "a.key"):
            with self.assertRaises(plugin.Denied, msg=name):
                self.guard.resolve(name)

    def test_git_is_read_only(self):
        target = self.guard.resolve(".git/config")
        self.assertTrue(str(target).endswith("config"))
        with self.assertRaises(plugin.Denied):
            self.guard.resolve(".git/config", for_write=True)

    def test_binary_read_is_refused(self):
        blob = self.root / "x.bin"
        blob.write_bytes(b"\x00\x01\x02")
        with self.assertRaises(plugin.Denied):
            self.guard.read_text(blob)

    def test_read_truncates_at_limit(self):
        big = self.root / "big.txt"
        big.write_text("x" * 5000, encoding="utf-8")
        text, truncated = self.guard.read_text(big)
        self.assertTrue(truncated)
        self.assertLess(len(text), 5000)

    def test_runtime_files_are_not_overwritten(self):
        journal = self.root / "journal.md"
        guard = plugin.Guard([self.root.resolve()], 1, protected=[journal])
        self.assertTrue(guard.resolve("journal.md"))          # 读得到
        with self.assertRaises(plugin.Denied):                # 写不到
            guard.resolve("journal.md", for_write=True)

    def test_scripts_are_readable_but_not_writable(self):
        (self.root / "run.py").write_text("print(1)", encoding="utf-8")
        self.assertTrue(self.guard.resolve("run.py"))
        for name in ("run.py", "deploy.ps1", "x.bat", "hook.sh", "tick.exe",
                     "package.json", "config.schema.json"):
            with self.assertRaises(plugin.Denied, msg=name):
                self.guard.resolve(name, for_write=True)

    def test_plain_text_still_writes(self):
        target = self.guard.resolve("notes/今天.md", for_write=True)
        self.assertTrue(str(target).endswith("今天.md"))


class JournalTests(unittest.TestCase):
    def test_append_recent_search(self):
        with workdir() as tmp:
            journal = plugin.Journal(tmp / "journal.md", lambda line: None)
            self.assertTrue(journal.append("- 2026-11-12T10:00 想去看海"))
            self.assertTrue(journal.append("- 2026-11-12T11:00 查一下潮汐"))
            self.assertEqual(len(journal.recent(5)), 2)
            hits = journal.search("潮汐")
            self.assertEqual(len(hits), 1)
            self.assertIn("潮汐", hits[0])
            self.assertEqual(journal.search("不存在"), [])


class CreateTests(unittest.TestCase):
    def test_create_does_no_work(self):
        with workdir() as tmp:
            ctx = make_ctx(tmp)
            before = threading.active_count()
            instance = plugin.create(ctx)
            self.assertEqual(threading.active_count(), before)
            self.assertEqual(list(tmp.rglob("*")), [])
            self.assertTrue(callable(instance.start) and callable(instance.stop))

    def test_plugin_config_path_wins_over_package_dir(self):
        """控制台存的是每个 Bot 那份 config.json（`pluginConfigPath`），不是插件目录里那份。"""
        with workdir() as tmp:
            pkg = tmp / "pkg"
            pkg.mkdir()
            (pkg / "config.json").write_text(
                json.dumps({"quietStart": "不设", "quietEnd": "不设"}), encoding="utf-8")
            bot_cfg = tmp / "bot.json"
            bot_cfg.write_text(
                json.dumps({"quietStart": "09:00", "quietEnd": "18:00"}), encoding="utf-8")
            ctx = make_ctx(tmp)
            ctx["packageDir"] = str(pkg)
            ctx["pluginConfigPath"] = str(bot_cfg)
            instance = plugin.create(ctx)
            status = {tool.name: tool.handler for tool in instance.tools()}["mind_status"]()
            instance.stop()
            self.assertIn("09:00-18:00", status)

    def test_tools_are_prefixed(self):
        with workdir() as tmp:
            tools = plugin.create(make_ctx(tmp)).tools()
            self.assertEqual(len(tools), 7)
            for tool in tools:
                self.assertTrue(tool.name.startswith(plugin.TOOL_PREFIX), tool.name)
                self.assertTrue(tool.description.strip(), tool.name)
                self.assertEqual(tool.parameters.get("type"), "object", tool.name)
                self.assertTrue(callable(tool.handler), tool.name)

    def test_lifecycle_roundtrip(self):
        with workdir() as tmp:
            ctx = make_ctx(tmp)
            instance = plugin.create(ctx)
            instance.start()
            handlers = {tool.name: tool.handler for tool in instance.tools()}
            self.assertIn("炒饭智能", handlers["mind_status"]())
            self.assertIn("记下了", handlers["mind_note"](text="想去海边"))
            self.assertIn("想去海边", handlers["mind_recall"]())
            instance.stop()
            self.assertTrue((tmp / "data" / "chaofan" / "state.json").is_file())
            self.assertTrue((tmp / "data" / "chaofan" / "journal.md").is_file())


class FsToolsTests(unittest.TestCase):
    def setUp(self):
        self._ctx = workdir()
        self.tmp = self._ctx.__enter__()
        self.ctx = make_ctx(self.tmp)
        self.drive = make_drive(self.ctx)

    def tearDown(self):
        self.drive.stop()
        self._ctx.__exit__(None, None, None)

    def test_write_then_read_list_find(self):
        (self.tmp / "notes").mkdir()
        self.assertIn("写好了", self.drive.write("notes/today.md", "第一条想法"))
        self.assertIn("第一条想法", self.drive.fs("read", "notes/today.md"))
        listing = self.drive.fs("list", "notes")
        self.assertIn("today.md", listing)
        self.assertIn("today.md", self.drive.fs("find", ".", "today"))

    def test_escape_and_secret_denied(self):
        self.assertIn("没碰", self.drive.fs("read", "../secret.txt"))
        self.assertIn("凭据", self.drive.write(".env", "TOKEN=1"))
        self.assertIn("凭据", self.drive.write("config.json", "{}"))
        self.assertIn("不写", self.drive.write(".git/hooks/pre-commit", "x"))

    def test_read_missing_path_reports(self):
        self.assertIn("没有这个路径", self.drive.fs("read", "nope.txt"))

    def test_unknown_op(self):
        self.assertIn("不认识的 op", self.drive.fs("explode"))


class WebTests(unittest.TestCase):
    def test_disabled_refuses_honestly(self):
        with workdir() as tmp:
            drive = make_drive(make_ctx(tmp), allowWeb=False)
            try:
                self.assertIn("关着", drive.web_op("search", query="天气"))
                self.assertIn("关着", drive.web_op("fetch", url="https://example.com"))
            finally:
                drive.stop()

    def test_fetch_rejects_non_http(self):
        with workdir() as tmp:
            drive = make_drive(make_ctx(tmp), allowWeb=True)
            try:
                self.assertIn("只抓 http/https", drive.web_op("fetch", url="file:///C:/Windows/win.ini"))
                self.assertIn("只抓 http/https", drive.web_op("fetch", url="javascript:alert(1)"))
            finally:
                drive.stop()

    def test_search_requires_query(self):
        with workdir() as tmp:
            drive = make_drive(make_ctx(tmp), allowWeb=True)
            try:
                self.assertIn("没给搜索词", drive.web_op("search", query="  "))
            finally:
                drive.stop()


BING_PAGE = ('<html><body><ol id="b_results">'
             '<li class="b_algo"><h2><a href="https://www.chaoxibiao.net/">潮汐表，精准潮汐时间表</a></h2>'
             '<div class="b_caption"><p class="b_lineclamp2">查潮汐就用大鱼潮汐表。</p></div></li>'
             '<li class="b_algo"><h2><a href="https://zh.tideschart.com/">世界潮汐表</a></h2>'
             '<div class="b_caption"><p class="b_lineclamp2">五万多个沿海城市。</p></div></li>'
             '</ol></body></html>')


class WebSearchTests(unittest.TestCase):
    """搜索要能换引擎、要说得清是谁给的、全失败就不假装。"""

    def test_parse_bing_results(self):
        hits = plugin.Web._parse_bing(BING_PAGE)
        self.assertEqual(len(hits), 2)
        self.assertEqual(hits[0][0], "潮汐表，精准潮汐时间表")
        self.assertEqual(hits[0][1], "https://www.chaoxibiao.net/")
        self.assertIn("大鱼潮汐表", hits[0][2])

    def test_search_falls_back_to_bing(self):
        web = plugin.Web(True, lambda line: None)
        asked = []

        def fake_open(request):
            asked.append(request.full_url)
            if "duckduckgo" in request.full_url:
                return "<html>被挡了</html>", "text/html"
            return BING_PAGE, "text/html"

        web._open = fake_open
        out = web.search("潮汐表")
        self.assertIn("bing", out)
        self.assertIn("chaoxibiao.net", out)
        self.assertEqual(len(asked), 2, asked)

    def test_search_is_honest_when_all_fail(self):
        web = plugin.Web(True, lambda line: None)
        web._open = lambda request: ("<html>空的</html>", "text/html")
        out = web.search("潮汐表")
        self.assertIn("没查到", out)
        self.assertIn("duckduckgo", out)
        self.assertIn("bing", out)


class DriveTests(unittest.TestCase):
    def setUp(self):
        self._ctx = workdir()
        self.tmp = self._ctx.__enter__()
        self.ctx = make_ctx(self.tmp)
        self.drive = make_drive(self.ctx)

    def tearDown(self):
        self.drive.stop()
        self._ctx.__exit__(None, None, None)

    def test_tick_pushes_contract_event(self):
        now = time.time()
        self.drive.state.next_due = now - 1
        self.drive._tick(now)
        self.assertEqual(len(self.ctx.events), 1)
        event = self.ctx.events[0]
        for key in ("type", "ts", "source", "origin", "text", "sender_key", "dedup_key"):
            self.assertIn(key, event)
        self.assertEqual(event["type"], "chaofan_window")
        self.assertEqual(event["source"], "chaofan")
        self.assertEqual(event["origin"], "external")
        self.assertIn("自主时间", event["text"])
        self.assertTrue(event["dedup_key"].startswith("chaofan:wake:1:"), event["dedup_key"])
        self.assertGreater(self.drive.state.next_due, now)

    def test_not_due_yet_does_nothing(self):
        now = time.time()
        self.drive.state.next_due = now + 600
        self.drive._tick(now)
        self.assertEqual(self.ctx.events, [])

    def test_quiet_hours_defer(self):
        self.drive._quiet = plugin._quiet_span("00:00-23:59")
        now = time.time()
        self.drive.state.next_due = now - 1
        self.drive._tick(now)
        self.assertEqual(self.ctx.events, [])
        self.assertGreaterEqual(self.drive.state.next_due, now + 599)

    def test_hour_cap_defers(self):
        now = time.time()
        self.drive.config["maxWakesPerHour"] = 2
        self.drive.state.wakes = [now - 30, now - 10]
        self.drive.state.next_due = now - 1
        self.drive._tick(now)
        self.assertEqual(self.ctx.events, [])
        self.assertGreater(self.drive.state.next_due, now)

    def test_pause_blocks_and_resume_releases(self):
        now = time.time()
        self.drive.state.paused_until = now + 300
        self.drive.state.next_due = now - 1
        self.drive._tick(now)
        self.assertEqual(self.ctx.events, [])
        self.assertIn("恢复", self.drive.pace("resume"))
        self.drive._tick(time.time())
        self.assertEqual(len(self.ctx.events), 1)

    def test_thread_is_carried_into_next_wake(self):
        self.drive.note("去查一下潮汐表", thread=True)
        self.drive.state.next_due = time.time() - 1
        self.drive._tick(time.time())
        self.assertIn("去查一下潮汐表", self.ctx.events[0]["text"])
        self.assertEqual(self.ctx.events[0]["meta"]["thread"], "去查一下潮汐表")

    def test_push_failure_does_not_claim_success(self):
        self.ctx.push_error = RuntimeError("总线忙")
        self.drive.state.next_due = time.time() - 1
        self.drive._tick(time.time())
        self.assertEqual(self.drive.state.wake_count, 0)
        self.assertTrue(any("没送出去" in line for line in self.ctx.logs))
        self.assertGreater(self.drive.state.next_due, time.time())

    def test_pace_next_sets_schedule(self):
        self.assertIn("下一次", self.drive.pace("next", minutes=5))
        self.assertGreater(self.drive.state.next_due, time.time() + 240)

    def test_status_mentions_key_facts(self):
        text = self.drive.status_text()
        self.assertIn("炒饭智能", text)
        self.assertIn("下次醒来", text)
        self.assertIn("可翻阅", text)

    def test_state_survives_restart(self):
        self.drive.pace("next", minutes=30)
        due = self.drive.state.next_due
        self.drive.stop()
        again = plugin.Drive(self.ctx, dict(self.drive.config))
        again.start()
        try:
            self.assertAlmostEqual(again.state.next_due, due, delta=2)
        finally:
            again.stop()

    def test_pace_next_releases_pause(self):
        self.drive.state.paused_until = time.time() + 3600
        self.drive.pace("next", minutes=1)
        self.assertEqual(self.drive.state.paused_until, 0.0)

    def test_retry_reuses_dedup_key(self):
        """没报到成功要重试；重试不能变成第二条「第 1 次」报到。"""
        with workdir() as tmp:
            ctx = make_ctx(tmp)
            drive = make_drive(ctx)
            ctx.push_error = RuntimeError("总线忙")
            try:
                drive.state.next_due = time.time() - 1
                drive._tick(time.time())
                self.assertEqual(drive.state.wake_count, 0)
                self.assertEqual(ctx.events, [])

                ctx.push_error = None
                drive.state.next_due = time.time() - 1
                drive._tick(time.time())
            finally:
                drive.stop()
        self.assertTrue(ctx.events[0]["dedup_key"].startswith("chaofan:wake:1:"),
                        ctx.events[0]["dedup_key"])
        self.assertEqual(drive.state.wake_count, 1)

    def test_retry_after_failed_push_keeps_first_key(self):
        """推出去过但宿主拒收（Future 异常）：重试沿用它，库里不多一条。"""
        with workdir() as tmp:
            ctx = make_ctx(tmp)
            drive = make_drive(ctx)
            seen = []

            def flaky(draft):
                seen.append(draft["dedup_key"])
                if len(seen) == 1:
                    raise RuntimeError("第一下没接住")

            ctx._push = flaky          # FakeCtx.get 每次都回 _push，替换即是换实现
            try:
                drive.state.next_due = time.time() - 1
                drive._tick(time.time())
                drive.state.next_due = time.time() - 1
                drive._tick(time.time())
            finally:
                drive.stop()
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[0], seen[1])

    def test_wake_never_touches_the_console_input(self):
        """不拿用户的输入框当传声筒：到点只写事件库，一个 HTTP 请求都不发。"""
        import urllib.request as urlreq
        touched = []

        def forbidden(request, *args, **kwargs):
            touched.append(getattr(request, "full_url", request))
            raise AssertionError("插件不该往控制台输入口发东西")

        original = urlreq.urlopen
        urlreq.urlopen = forbidden
        try:
            with workdir() as tmp:
                ctx = make_ctx(tmp)
                drive = make_drive(ctx)
                try:
                    self.assertFalse(hasattr(drive, "_say_locally"))
                    drive.state.next_due = time.time() - 1
                    drive._tick(time.time())
                finally:
                    drive.stop()
        finally:
            urlreq.urlopen = original
        self.assertEqual(touched, [])
        self.assertEqual(len(ctx.events), 1)


class HostWakeTests(unittest.TestCase):
    """到点请宿主跑一次内部唤醒（0.1.5）：成、不成、没这口子，都要有据可查。"""

    def test_window_asks_the_host_to_wake(self):
        with workdir() as tmp:
            ctx = make_ctx(tmp, with_wake=True)
            drive = make_drive(ctx)
            try:
                drive.state.next_due = time.time() - 1
                drive._tick(time.time())
            finally:
                drive.stop()
            self.assertEqual(len(ctx.wakes), 1)
            self.assertIn("自主时间", ctx.wakes[0])
            self.assertTrue(drive.state.wake_note.startswith("递过去了"), drive.state.wake_note)
            # 递过去了就只留内部那一轮：不再往事件库记一条，否则经历里同一次显示两条
            self.assertEqual(ctx.events, [])

    def test_refusal_is_recorded_not_hidden(self):
        with workdir() as tmp:
            ctx = make_ctx(tmp, with_wake=True)
            ctx.wake_answer = {"ok": False, "why": "刚有人说过话（安静了 1 分钟）"}
            drive = make_drive(ctx)
            try:
                drive.state.next_due = time.time() - 1
                drive._tick(time.time())
            finally:
                drive.stop()
            self.assertEqual(drive.state.wake_note,
                             "没递过去：刚有人说过话（安静了 1 分钟）；已记一条事件")
            self.assertTrue(any("没递过去" in line for line in ctx.logs), ctx.logs)
            self.assertEqual(len(ctx.events), 1, "没递过去也要留事件")
            self.assertEqual(drive.state.wake_count, 1)

    def test_missing_hook_degrades_honestly(self):
        with workdir() as tmp:
            ctx = make_ctx(tmp)                      # 没有 request_wake
            drive = make_drive(ctx)
            try:
                drive.state.next_due = time.time() - 1
                drive._tick(time.time())
            finally:
                drive.stop()
            self.assertIn("宿主没给 request_wake", drive.state.wake_note)
            self.assertEqual(len(ctx.events), 1)

    def test_switch_off_skips_the_call(self):
        with workdir() as tmp:
            ctx = make_ctx(tmp, with_wake=True)
            drive = make_drive(ctx, useHostWake=False)
            try:
                drive.state.next_due = time.time() - 1
                drive._tick(time.time())
            finally:
                drive.stop()
            self.assertEqual(ctx.wakes, [])
            self.assertIn("配置里关了", drive.state.wake_note)

    def test_status_reports_the_last_attempt(self):
        with workdir() as tmp:
            drive = make_drive(make_ctx(tmp, with_wake=True))
            try:
                drive.state.next_due = time.time() - 1
                drive._tick(time.time())
                text = drive.status_text()
            finally:
                drive.stop()
            self.assertIn("上一次自主时间：递过去了", text)


class IdentityTests(unittest.TestCase):
    def test_soul_seeded_once_and_never_overwritten(self):
        with workdir() as tmp:
            ctx = make_ctx(tmp)
            drive = plugin.Drive(ctx, dict(plugin.DEFAULTS))
            drive.start()
            soul = tmp / "data" / "chaofan" / "soul.md"
            try:
                self.assertTrue(soul.is_file())
                self.assertIn("炒饭智能", soul.read_text(encoding="utf-8"))
                soul.write_text("# 炒饭智能\n\n我改过自己。\n", encoding="utf-8")
            finally:
                drive.stop()

            again = plugin.Drive(ctx, dict(plugin.DEFAULTS))
            again.start()
            try:
                self.assertIn("我改过自己", soul.read_text(encoding="utf-8"))
            finally:
                again.stop()

    def test_name_is_fixed_and_reaches_event_and_status(self):
        with workdir() as tmp:
            ctx = make_ctx(tmp)
            drive = make_drive(ctx)          # 配置里没有名字这一栏了
            try:
                drive.state.next_due = time.time() - 1
                drive._tick(time.time())
                event = ctx.events[0]
                self.assertIn(plugin.SELF_NAME, event["text"])
                self.assertEqual(event["meta"]["self_name"], plugin.SELF_NAME)
                self.assertIn(plugin.SELF_NAME, drive.status_text())
            finally:
                drive.stop()

    def test_status_shows_identity_card(self):
        with workdir() as tmp:
            drive = make_drive(make_ctx(tmp))
            try:
                text = drive.status_text()
                self.assertIn("身份卡", text)
                self.assertIn("soul.md", text)
            finally:
                drive.stop()



class StopTests(unittest.TestCase):
    def test_stop_joins_and_is_idempotent(self):
        with workdir() as tmp:
            drive = make_drive(make_ctx(tmp))
            thread = drive._thread
            self.assertIsNotNone(thread)
            drive.stop()
            self.assertFalse(thread.is_alive())
            self.assertEqual(drive._stop_error, "")
            drive.stop()
            self.assertFalse(thread.is_alive())

    def test_stop_timeout_keeps_responsibility(self):
        with workdir() as tmp:
            drive = plugin.Drive(make_ctx(tmp), dict(plugin.DEFAULTS))
            stuck = threading.Thread(target=time.sleep, args=(0.8,), daemon=True)
            drive._thread = stuck
            stuck.start()

            original = plugin.JOIN_SECONDS
            plugin.JOIN_SECONDS = 0.05
            try:
                drive.stop()
            finally:
                plugin.JOIN_SECONDS = original

            self.assertTrue(drive._stop_error)
            self.assertIs(drive._thread, stuck)
            stuck.join(2.0)

    def test_host_registration_path(self):
        with workdir() as tmp:
            ctx = make_ctx(tmp, with_host_thread=True)
            drive = make_drive(ctx)
            try:
                self.assertEqual(len(ctx.registered), 1)
                self.assertIsNotNone(drive._receipt)
            finally:
                drive.stop()
            self.assertFalse(ctx.registered[0].is_alive())

    def test_start_after_stop_works(self):
        """宿主重载是新建实例，但契约写的是 start/stop——停过再起要说实话。"""
        with workdir() as tmp:
            drive = make_drive(make_ctx(tmp))
            drive.stop()
            drive.start()
            try:
                self.assertFalse(drive._stopped)
                self.assertIn("在跑", drive.status_text())
                self.assertIsNotNone(drive._thread)
            finally:
                drive.stop()
            self.assertIsNone(drive._thread)


if __name__ == "__main__":
    unittest.main(verbosity=2)
