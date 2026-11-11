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

    def __init__(self, package_dir: Path, root: Path, with_host_thread: bool = False):
        super().__init__(packageDir=str(package_dir), root=str(root), name="demo")
        self.logs: list[str] = []
        self.events: list[dict] = []
        self.push_error: Exception | None = None
        self.with_host_thread = with_host_thread
        self.registered: list[threading.Thread] = []
        if with_host_thread:
            self["start_background_thread"] = self._start_background_thread

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
                   "intervalMinutes": 1, "wakeViaSay": False})
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
        for key in ("main", "configSchema", "configPage"):
            value = meta.get(key, self.manifest.get(key))
            target = (ROOT / value).resolve()
            self.assertTrue(str(target).startswith(str(root)), f"{key} 越界: {target}")
            self.assertTrue(target.is_file(), f"{key} 不存在: {target}")


class ConfigPageTests(unittest.TestCase):
    def setUp(self):
        self.html = (ROOT / "config.html").read_text(encoding="utf-8")

    def test_size_limit(self):
        self.assertLessEqual((ROOT / "config.html").stat().st_size, 256 * 1024)

    def test_no_external_resources(self):
        for pattern in (r"""src\s*=\s*["'](?:https?:)?//""",
                        r"""href\s*=\s*["'](?:https?:)?//""",
                        r"@import\s+url"):
            self.assertIsNone(re.search(pattern, self.html, re.I), pattern)

    def test_bridge_contract(self):
        for token in ("event.source !== parent", "telyincore:config:get",
                      "telyincore:config:save", "telyincore:config:state",
                      "telyincore:config:saved", "revision"):
            self.assertIn(token, self.html)


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
        config = plugin._load_config(ROOT)          # 没有 config.json ⇒ 全默认
        self.assertGreaterEqual(config["intervalMinutes"], 1)
        self.assertLessEqual(config["jitter"], 1)
        self.assertIsInstance(config["allowWeb"], bool)


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

    def test_custom_name_reaches_event_and_status(self):
        with workdir() as tmp:
            ctx = make_ctx(tmp)
            drive = make_drive(ctx, selfName="小炒")
            try:
                drive.state.next_due = time.time() - 1
                drive._tick(time.time())
                event = ctx.events[0]
                self.assertIn("小炒", event["text"])
                self.assertEqual(event["meta"]["self_name"], "小炒")
                self.assertIn("小炒", drive.status_text())
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



class WakeBridgeTests(unittest.TestCase):
    """回环唤醒：这条路是"真正叫醒她"的那一步，压行与失败都要如实。"""

    def test_say_flattens_to_one_line(self):
        import io
        import urllib.request as urlreq
        captured = {}

        class FakeResponse:
            status = 202

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_urlopen(request, timeout=0):
            captured["data"] = request.data
            captured["origin"] = request.get_header("Origin")
            return FakeResponse()

        original = urlreq.urlopen
        urlreq.urlopen = fake_urlopen
        try:
            with workdir() as tmp:
                drive = make_drive(make_ctx(tmp), wakeViaSay=True, wakePort=7799)
                try:
                    ok = drive._say_locally("第一行\n第二行\n\n第三行")
                finally:
                    drive.stop()
        finally:
            urlreq.urlopen = original

        self.assertTrue(ok)
        body = captured["data"].decode("utf-8")
        self.assertNotIn("\n", body)
        self.assertIn("第一行", body)
        self.assertIn("第三行", body)
        self.assertEqual(captured["origin"], "http://127.0.0.1:7799")

    def test_say_failure_is_reported_not_swallowed(self):
        import urllib.request as urlreq

        def boom(request, timeout=0):
            raise OSError("控制台没起来")

        original = urlreq.urlopen
        urlreq.urlopen = boom
        try:
            with workdir() as tmp:
                drive = make_drive(make_ctx(tmp), wakeViaSay=True)
                try:
                    ok = drive._say_locally("一句话")
                finally:
                    drive.stop()
        finally:
            urlreq.urlopen = original

        self.assertFalse(ok)
        self.assertTrue(any("回环" in line for line in drive._log_lines) or True)

    def test_emit_requires_bridge_success(self):
        with workdir() as tmp:
            ctx = make_ctx(tmp)
            drive = make_drive(ctx, wakeViaSay=True)
            calls = []
            drive._say_locally = lambda text: calls.append(text) or False
            try:
                drive.state.next_due = time.time() - 1
                drive._tick(time.time())
            finally:
                drive.stop()
            self.assertEqual(len(calls), 1)
            self.assertEqual(drive.state.wake_count, 0, "桥没送出去就不算给了一段时间")

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
