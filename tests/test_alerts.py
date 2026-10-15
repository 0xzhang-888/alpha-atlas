import asyncio
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src import bot
from src.storage import Storage


class AlertTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = str(Path(self.tmp.name) / "events.db")
        self.storage = Storage(self.db)
        clock_patch = patch("src.bot.time.time", return_value=1800000000)
        self.clock = clock_patch.start()
        self.addCleanup(clock_patch.stop)
        self.tasks = []
        self.app = SimpleNamespace(
            bot=SimpleNamespace(send_message=AsyncMock()),
            bot_data={"storage": self.storage, "db_path": self.db,
                      "target_chat_id": 1, "source_chat_id": -1,
                      "alert_threshold": 3, "alert_cooldown_seconds": 3600,
                      "window_minutes": 60, "deep_limit": 20},
        )
        def create_task(coro):
            task = asyncio.create_task(coro)
            self.tasks.append(task)
            return task
        self.app.create_task = create_task
        self.profile = self.mock("src.bot.ot.get_user_info", None)
        self.kol = self.mock("src.bot.get_kol_followers", None)
        self.analysis = self.mock("src.bot.analyze_batch", {})
        self.mid = 0

    def mock(self, name, result):
        p = patch(name, new_callable=AsyncMock, return_value=result)
        m = p.start()
        self.addCleanup(p.stop)
        return m

    async def asyncTearDown(self):
        for t in self.tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    def follow(self, kol, ago=0, handle="Demo", bio=""):
        self.mid += 1
        self.storage.add_event(kol, handle, self.mid, event_ts=int(self.clock()) - ago, bio=bio)

    async def check(self):
        await bot.check_alert(self.app, {"demo"})

    async def test_two_distinct_kols_sends_immediately_without_ai(self):
        self.follow("Alice", 179, bio="A protocol <demo>")
        self.follow("Bob")
        await self.check()
        self.app.bot.send_message.assert_awaited_once()
        text = self.app.bot.send_message.call_args.kwargs["text"]
        self.assertIn("3 分钟内 2 位不同 KOL", text)
        self.assertIn("Alice", text)
        self.assertIn("<code>@Demo</code>", text)
        self.assertIn("<code>Alice</code>", text)
        self.assertNotIn("<a ", text)
        self.assertEqual(self.app.bot.send_message.call_args.kwargs["parse_mode"], "HTML")
        self.assertIn("&lt;demo&gt;", text)
        self.profile.assert_not_called()
        self.analysis.assert_not_called()
        self.assertFalse(self.tasks)

    async def test_kol_usernames_are_copyable_without_inventing_handles_for_names(self):
        self.follow("@Collector", 10)
        self.follow("Display <name>")
        await self.check()
        text = self.app.bot.send_message.call_args.kwargs["text"]
        self.assertIn("<code>@Collector</code>", text)
        self.assertIn("<code>Display &lt;name&gt;</code>", text)
        self.assertNotIn("@Display", text)
        self.assertNotIn("<a ", text)

    async def test_same_kol_and_handle_different_case_not_two_people(self):
        self.follow("Alice", handle="Demo")
        self.follow("alice", handle="demo")
        await self.check()
        self.app.bot.send_message.assert_not_called()

    async def test_exact_three_minute_boundary_included(self):
        self.follow("Alice", 180)
        self.follow("Bob")
        await self.check()
        self.app.bot.send_message.assert_awaited_once()

    async def test_outside_three_minutes_does_not_trigger(self):
        self.follow("Alice", 181)
        self.follow("Bob")
        await self.check()
        self.app.bot.send_message.assert_not_called()

    async def test_future_dated_event_not_counted(self):
        self.follow("Alice", -1)
        self.follow("Bob")
        await self.check()
        self.app.bot.send_message.assert_not_called()

    async def test_fast_alert_not_suppressed_by_hourly_cooldown(self):
        self.storage.record_alert("Demo", 9)
        self.follow("Alice", 10)
        self.follow("Bob")
        await self.check()
        self.app.bot.send_message.assert_awaited_once()

    async def test_third_kol_in_same_burst_does_not_duplicate(self):
        self.follow("Alice", 10)
        self.follow("Bob")
        await self.check()
        self.follow("Carol")
        await self.check()
        self.app.bot.send_message.assert_awaited_once()
        self.assertFalse(self.tasks)

    async def test_new_burst_after_cooldown_sends_again(self):
        self.follow("Alice", 10)
        self.follow("Bob")
        await self.check()
        self.clock.return_value += 181
        self.follow("Carol", 1)
        self.follow("Dan")
        await self.check()
        self.assertEqual(self.app.bot.send_message.await_count, 2)

    async def test_restart_preserves_fast_cooldown(self):
        self.follow("Alice", 10)
        self.follow("Bob")
        await self.check()
        self.app.bot_data["storage"] = Storage(self.db)
        await self.check()
        self.app.bot.send_message.assert_awaited_once()

    async def test_failed_send_does_not_consume_alert(self):
        self.follow("Alice", 10)
        self.follow("Bob")
        self.app.bot.send_message.side_effect = RuntimeError("offline")
        await self.check()
        self.assertEqual(self.storage.get_fast_alert_info("Demo"), (0, 0))
        self.app.bot.send_message.side_effect = None
        await self.check()
        self.assertEqual(self.storage.get_fast_alert_info("Demo")[0], 1)

    async def test_simultaneous_checks_only_send_once(self):
        self.follow("Alice", 10)
        self.follow("Bob")
        await asyncio.gather(self.check(), self.check())
        self.app.bot.send_message.assert_awaited_once()

    async def test_original_hourly_rule_still_works_in_background(self):
        for name, ago in (("A", 600), ("B", 400), ("C", 200)):
            self.follow(name, ago)
        await self.check()
        self.assertEqual(len(self.tasks), 1)
        await asyncio.gather(*self.tasks)
        text = self.app.bot.send_message.call_args.kwargs["text"]
        self.assertIn("1 小时内 3 人关注", text)
        self.assertIn("<code>@Demo</code>", text)
        self.assertIn("<code>A</code>", text)
        self.assertNotIn("<b><code>", text)
        self.assertNotIn("<a ", text)

    async def test_slow_hourly_analysis_does_not_block_or_duplicate_fast_alert(self):
        ready = asyncio.Event()
        async def slow(*args):
            await ready.wait()
            return None
        self.profile.side_effect = slow
        for name, ago in (("A", 600), ("B", 400), ("C", 200)):
            self.follow(name, ago)
        await self.check()
        await asyncio.sleep(0)
        self.follow("D", 1)
        self.follow("E")
        await self.check()
        self.app.bot.send_message.assert_awaited_once()
        ready.set()
        await asyncio.gather(*self.tasks)
        self.app.bot.send_message.assert_awaited_once()

    async def test_telegram_backlog_uses_message_time_and_saves_bio(self):
        context = SimpleNamespace(application=self.app)
        for i, name in enumerate(("Alice", "Bob")):
            msg = SimpleNamespace(chat_id=-1, chat=SimpleNamespace(type="channel"),
                                  text=f"{name} 关注了 Demo\n用户简介:Building a protocol\nhttps://x.com/Demo",
                                  caption=None, message_id=100+i,
                                  date=datetime.fromtimestamp(self.clock()-500, tz=timezone.utc))
            await bot.on_message(SimpleNamespace(effective_message=msg), context)
        self.app.bot.send_message.assert_not_called()
        self.assertEqual(self.storage.get_source_bios(["Demo"])["Demo"], "Building a protocol")


if __name__ == "__main__":
    unittest.main()
