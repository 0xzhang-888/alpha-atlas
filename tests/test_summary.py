import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src import bot, grok
from src.storage import Storage


class UsernameFormattingTests(unittest.TestCase):
    def test_all_summary_roles_use_standalone_code(self):
        blocks = [
            bot.fmt_project_block("Project", 2, 1, None, "", None, False, False,
                                  [{"handle": "@Builder", "kol": 3}]),
            bot.fmt_dev_block("Developer", 2, 1, None, "", ["RelatedProject"]),
            bot.fmt_simple_block("Trader", 2, 1, None, ""),
        ]
        text = "\n".join(line for block in blocks for line in block)
        for handle in ("Project", "Builder", "Developer", "RelatedProject", "Trader"):
            self.assertIn(f"<code>@{handle}</code>", text)
        self.assertNotIn("<a ", text)
        self.assertNotIn("<b><code>", text)
        self.assertNotIn("@@", text)

    def test_username_is_html_escaped(self):
        self.assertEqual(bot.fmt_username("@bad<&>"), "<code>@bad&lt;&amp;&gt;</code>")


class SummaryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = str(Path(self.tmp.name) / "summary.db")
        self.storage = Storage(self.db)
        self.app = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()), bot_data={
            "storage": self.storage, "db_path": self.db, "window_minutes": 60,
            "target_chat_id": 1})
        env = patch.dict(bot.os.environ, {}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.profiles = self.mock("src.bot.ot.get_many_user_info", {})
        self.kols = self.mock("src.bot.get_many", {})
        self.dev_kols = self.mock("src.bot.get_kol_followers", None)
        self.devs = self.mock("src.bot.find_devs_deep", [])
        self.research = self.mock("src.bot.research_account", {
            **grok._default_meta(), "category": "project", "is_project": True,
            "summary": "自述：隐私支付协议", "_evidence": "search", "_verified": True,
            "_sources": ["https://x.com/Demo"]})

    def mock(self, name, value):
        p = patch(name, new_callable=AsyncMock, return_value=value)
        m = p.start()
        self.addCleanup(p.stop)
        return m

    async def test_missing_profiles_researched_before_classification_filter(self):
        self.storage.add_event("Alice", "Demo", 1)
        grok._ensure_tables(self.db)
        grok._write_meta_cache(self.db, {"demo": {**grok._default_meta(), "summary": "信息不足无法推断"}})
        await bot.push_summary(self.app)
        self.research.assert_awaited_once_with("Demo", self.db)
        self.devs.assert_not_called()
        text = self.app.bot.send_message.call_args.kwargs["text"]
        self.assertIn("🏗️ 项目方", text)
        self.assertIn("自述：隐私支付协议", text)
        self.assertIn('href="https://x.com/Demo"', text)
        self.assertIn("<code>@Demo</code>", text)
        self.assertNotIn('>@Demo</a>', text)
        self.assertEqual(self.app.bot.send_message.call_args.kwargs["parse_mode"], "HTML")
        self.assertNotIn("信息不足", text)

    async def test_source_bio_used_when_profile_api_unavailable(self):
        self.storage.add_event("Alice", "Demo", 1, bio="Building a payment protocol")
        analysis = self.mock("src.bot.analyze_batch", {"demo": {
            **grok._default_meta(), "category": "project", "is_project": True,
            "devs": ["Builder"], "summary": "支付协议"}})
        await bot.push_summary(self.app)
        self.assertEqual(analysis.call_args.args[0][0]["bio"], "Building a payment protocol")
        self.research.assert_not_called()
        self.devs.assert_not_called()

    async def test_research_and_team_lookup_both_run_despite_legacy_limit(self):
        for i in range(3):
            self.storage.add_event(f"KOL{i}", "HotUnknown", i)
        self.storage.add_event("KOL0", "KnownCold", 10, bio="Protocol")
        self.storage.add_event("KOL0", "KnownHot", 11, bio="Protocol")
        self.storage.add_event("KOL1", "KnownHot", 12, bio="Protocol")
        self.app.bot_data["deep_limit"] = 1
        self.mock("src.bot.analyze_batch", {
            "hotunknown": grok._default_meta(), **{
                h: {**grok._default_meta(), "category": "project", "is_project": True, "summary": "协议"}
                for h in ("knowncold", "knownhot")}})
        await bot.push_summary(self.app)
        self.research.assert_awaited_once_with("HotUnknown", self.db)
        self.assertEqual([c.args[0] for c in self.devs.await_args_list], ["KnownHot", "KnownCold"])

    async def test_all_unknown_targets_start_in_heat_order_without_a_round_cap(self):
        for i in range(3):
            self.storage.add_event(f"KOL{i}", "Hot", i)
        self.storage.add_event("KOL0", "Cold", 10)
        self.app.bot_data["deep_limit"] = 1
        await bot.push_summary(self.app)
        self.assertEqual([c.args[0] for c in self.research.await_args_list], ["Hot", "Cold"])
        self.assertNotIn("待联网核查", self.app.bot.send_message.call_args.kwargs["text"])

    async def test_all_research_and_team_targets_queue_with_bounded_concurrency(self):
        unknown = {f"Unknown{i:02d}" for i in range(25)}
        known = {f"Known{i:02d}" for i in range(5)}
        for i, handle in enumerate(sorted(unknown | known)):
            self.storage.add_event("KOL", handle, i, bio="Protocol")
        self.app.bot_data["deep_limit"] = 1
        self.mock("src.bot.analyze_batch", {
            **{h.lower(): grok._default_meta() for h in unknown},
            **{h.lower(): {**grok._default_meta(), "category": "project", "is_project": True,
                          "summary": "已知协议"} for h in known},
        })
        active = {"research": 0, "team": 0}
        peak = dict(active)

        async def tracked(kind, result):
            active[kind] += 1
            peak[kind] = max(peak[kind], active[kind])
            try:
                await asyncio.sleep(0)
                return result
            finally:
                active[kind] -= 1

        async def research(*args):
            return await tracked("research", dict(self.research.return_value))

        async def team(*args):
            return await tracked("team", [])

        self.research.side_effect = research
        self.devs.side_effect = team
        await bot.push_summary(self.app)
        self.assertEqual(self.research.await_count, 25)
        self.assertEqual({c.args[0] for c in self.research.await_args_list}, unknown)
        self.assertEqual(self.devs.await_count, 5)
        self.assertEqual({c.args[0] for c in self.devs.await_args_list}, known)
        for kind in active:
            self.assertEqual(active[kind], 0)
            self.assertGreater(peak[kind], 1)
            self.assertLessEqual(peak[kind], 3)
        text = "\n".join(c.kwargs["text"] for c in self.app.bot.send_message.await_args_list)
        for handle in unknown | known:
            self.assertIn(f"<code>@{handle}</code>", text)


if __name__ == "__main__":
    unittest.main()
