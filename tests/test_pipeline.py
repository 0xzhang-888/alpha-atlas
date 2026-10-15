import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src import grok, pipeline
from src.parser import parse_message, parse_followers
from src.storage import Storage


def meta(cat="project", **kw):
    return {**grok._default_meta(), "category": cat, "is_project": cat == "project", "summary": "已知简介", **kw}


class StorageRulesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = str(Path(self.tmp.name) / "test.db")
        self.s = Storage(self.db)

    def test_two_distinct_people_promote_other(self):
        self.assertFalse(self.s.promote_other("Demo", "other", ["A", "a"]))
        self.assertTrue(self.s.promote_other("Demo", "other", ["A", "B"]))
        self.assertTrue(Storage(self.db).promote_other("demo", "other", ["A"]))

    def test_new_person_after_broadcast_promotes_across_windows(self):
        self.s.mark_announced("Demo", ["Alice"])
        self.assertFalse(self.s.promote_other("Demo", "other", ["ALICE"]))
        self.assertTrue(self.s.promote_other("Demo", "other", ["Bob"]))

    def test_known_developer_not_promoted_by_other_rule(self):
        self.assertFalse(self.s.promote_other("Demo", "crypto_dev", ["A", "B"]))

    def test_source_follower_snapshot_preserves_zero_and_ignores_older_updates(self):
        self.s.add_event("A", "Demo", 1, followers=0)
        self.s.add_event("B", "Demo", 2, event_ts=1, followers=900)
        self.assertEqual(self.s.get_source_followers(["demo"])["demo"]["followers"], 0)

    def test_follower_message_formats(self):
        self.assertEqual(parse_followers("粉丝数:1.2万 用户简介:foo"), 12000)
        self.assertEqual(parse_followers("粉丝数：12,345"), 12345)
        self.assertEqual(parse_followers("粉丝数:1.5K"), 1500)
        ev = parse_message("A 关注了 Demo\n粉丝数:29 用户简介:token\nhttps://x.com/Demo")[0]
        self.assertEqual(ev.followers, 29)

    def test_exclusions_and_developer_threshold(self):
        self.assertIsNone(pipeline.research_mode(meta(is_nft=True), 5, False, False, True))
        self.assertIsNone(pipeline.research_mode(meta(is_launchpad=True), 5, False, False, True))
        self.assertIsNone(pipeline.research_mode(meta("crypto_dev"), 1, False, False, True))
        self.assertEqual(pipeline.research_mode(meta("crypto_dev"), 2, False, False, True), "crypto_dev")


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.s = Storage(str(Path(self.tmp.name) / "test.db"))
        self.app = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()), bot_data={
            "storage": self.s, "db_path": self.s.db_path, "window_minutes": 60, "target_chat_id": 1})
        self.metas = {}
        self.analysis = self.mock("src.pipeline.grok.analyze_batch", self.metas)
        self.profiles = self.mock("src.pipeline.profile", {"bio": "known bio", "followers": 123, "source": "频道"})
        self.kols = self.mock("src.pipeline.get_many", {})
        self.research = self.mock("src.pipeline.deepseek.research", {**meta(), "_research_status": "complete", "_verified": True})
        self.chain_check = self.mock("src.pipeline.chain_preflight.check", {"status": "complete", "ai_checked": True, "chains": ["base"]})
        p = patch("src.pipeline.deepseek.cached_research", return_value=None)
        self.cache = p.start()
        self.addCleanup(p.stop)
        self.mid = 0

    def mock(self, name, result):
        p = patch(name, new_callable=AsyncMock, return_value=result)
        m = p.start()
        self.addCleanup(p.stop)
        return m

    def follow(self, h="Demo", kol="A"):
        self.mid += 1
        self.s.add_event(kol, h, self.mid)

    async def test_every_category_has_followers_and_exempt_projects_skip_deep(self):
        for h, m in (("NFT", meta(is_nft=True)), ("Launch", meta(is_launchpad=True)), ("Dev", meta("crypto_dev")), ("Trader", meta("trader"))):
            self.follow(h)
            self.metas[h.lower()] = m
        await pipeline.push_deepseek_summary(self.app)
        self.assertEqual(self.profiles.await_count, 4)
        self.research.assert_not_called()
        text = self.app.bot.send_message.call_args.kwargs["text"]
        self.assertEqual(text.count("粉丝 123"), 4)
        for handle in ("NFT", "Launch", "Dev", "Trader"):
            self.assertIn(f"<code>@{handle}</code>", text)
        self.assertNotIn("<b><code>", text)
        self.assertNotIn("<a ", text)

    async def test_research_usernames_are_code_but_website_and_evidence_stay_links(self):
        self.follow()
        self.metas["demo"] = meta(devs=["Builder"], dev_projects=["RelatedProject"])
        self.research.return_value = {
            **self.metas["demo"], "_verified": True, "_research_status": "complete",
            "website": {"url": "https://demo.example", "status": "read", "finding": "Protocol"},
            "official_ca": [{"address": "0x123", "chain": "base", "source": "https://x.com/Demo/status/1"}],
            "_sources": ["https://x.com/Demo/status/1"],
        }
        await pipeline.push_deepseek_summary(self.app)
        text = self.app.bot.send_message.call_args.kwargs["text"]
        for handle in ("Demo", "Builder", "RelatedProject"):
            self.assertIn(f"<code>@{handle}</code>", text)
            self.assertNotIn(f">@{handle}</a>", text)
        self.assertIn('<a href="https://demo.example">官网</a>', text)
        self.assertIn('<a href="https://x.com/Demo/status/1">官方发布/认领证据</a>', text)
        self.assertIn('<a href="https://x.com/Demo/status/1">来源1</a>', text)
        self.assertIn("<code>0x123</code>", text)
        self.assertNotIn("<b><code>", text)
        self.assertEqual(self.app.bot.send_message.call_args.kwargs["parse_mode"], "HTML")

    async def test_project_and_two_followed_developer_modes(self):
        self.follow("Demo")
        self.follow("Dev", "A")
        self.follow("Dev", "B")
        self.metas.update(demo=meta(), dev=meta("crypto_dev"))
        await pipeline.push_deepseek_summary(self.app)
        modes = {c.args[0]: c.args[1] for c in self.research.await_args_list}
        self.assertEqual(modes, {"Demo": "project", "Dev": "crypto_dev"})
        self.assertEqual(self.chain_check.await_count, 2)
        self.assertTrue(all(c.args[2]["chain_preflight"]["ai_checked"] for c in self.research.await_args_list))

    async def test_chain_ai_check_finishes_before_deep_research_for_each_account(self):
        seen = []
        async def check(h, context, storage):
            seen.append((h, "chain"))
            return {"status": "unknown", "ai_checked": True, "chains": []}
        async def research(h, mode, context, storage):
            self.assertIn((h, "chain"), seen)
            self.assertEqual(context["chain_preflight"]["status"], "unknown")
            seen.append((h, "deep"))
            return {**meta(), "_research_status": "complete", "_verified": True}
        self.chain_check.side_effect = check
        self.research.side_effect = research
        self.follow("Demo")
        self.follow("Second")
        self.metas.update(demo=meta(), second=meta())
        await pipeline.push_deepseek_summary(self.app)
        for h in ("Demo", "Second"):
            self.assertLess(seen.index((h, "chain")), seen.index((h, "deep")))

    async def test_preflight_failure_does_not_block_deep_research_or_claim_success(self):
        self.chain_check.side_effect = RuntimeError("provider failure")
        self.follow()
        self.metas["demo"] = meta()
        await pipeline.push_deepseek_summary(self.app)
        self.research.assert_awaited_once()
        check = self.research.call_args.args[2]["chain_preflight"]
        self.assertEqual(check["status"], "failed")
        self.assertFalse(check["ai_checked"])

    async def test_profile_only_skips_chain_ai_and_legacy_limit_cannot_truncate_targets(self):
        await pipeline.research_after_chain_check("Profile", "profile", {}, self.s)
        self.chain_check.assert_not_called()
        self.research.reset_mock()
        self.app.bot_data["deep_limit"] = 1
        self.follow("Low", "A")
        self.follow("High", "A")
        self.follow("High", "B")
        self.metas.update(low=meta(), high=meta())
        await pipeline.push_deepseek_summary(self.app)
        self.assertEqual([c.args[0] for c in self.chain_check.await_args_list], ["High", "Low"])
        self.assertEqual([c.args[0] for c in self.research.await_args_list], ["High", "Low"])

    async def test_more_than_twenty_candidates_all_classified_checked_and_researched(self):
        self.app.bot_data["deep_limit"] = 1  # Stale caller configuration must have no effect.
        handles = {f"Account{i:02d}" for i in range(25)}
        expected_modes = {}
        for i, handle in enumerate(sorted(handles)):
            cat = ("project", "crypto_dev", "other")[i % 3]
            self.follow(handle, "A")
            self.follow(handle, "B")
            self.metas[handle.lower()] = meta(cat)
            expected_modes[handle] = "crypto_dev" if cat == "crypto_dev" else "project"
        self.analysis.side_effect = lambda items, db: {
            item["handle"].lower(): self.metas[item["handle"].lower()] for item in items}

        await pipeline.push_deepseek_summary(self.app)

        batches = [c.args[0] for c in self.analysis.await_args_list]
        self.assertEqual([len(batch) for batch in batches], [20, 5])
        self.assertEqual({item["handle"] for batch in batches for item in batch}, handles)
        self.assertEqual(self.chain_check.await_count, 25)
        self.assertEqual({c.args[0] for c in self.chain_check.await_args_list}, handles)
        self.assertEqual(self.research.await_count, 25)
        self.assertEqual({c.args[0]: c.args[1] for c in self.research.await_args_list}, expected_modes)
        text = "\n".join(c.kwargs["text"] for c in self.app.bot.send_message.await_args_list)
        for handle in handles:
            self.assertIn(f"<code>@{handle}</code>", text)
        self.assertNotIn("超过本轮深研上限", text)
        self.assertNotIn("本轮未执行深入研究", text)

    async def test_other_promoted_and_sent_state_recorded_only_after_delivery(self):
        self.follow("Demo", "B")
        self.s.mark_announced("Demo", ["A"])
        self.metas["demo"] = meta("other")
        await pipeline.push_deepseek_summary(self.app)
        self.assertEqual(self.research.call_args.args[1], "project")
        text = self.app.bot.send_message.call_args.kwargs["text"]
        self.assertIn("按新增关注规则纳入项目观察", text)
        self.assertEqual(self.s.known_kols("Demo", []), ["a", "b"])

    async def test_delivery_failure_does_not_mark_broadcast_or_people(self):
        self.follow()
        self.metas["demo"] = meta(is_nft=True)
        self.app.bot.send_message.side_effect = RuntimeError("offline")
        await pipeline.push_deepseek_summary(self.app)
        self.assertEqual(self.s.next_broadcast_counts(["Demo"])["Demo"], 1)
        self.assertEqual(self.s.known_kols("Demo", []), [])

    async def test_cache_hit_skips_research_without_limiting_uncached_accounts(self):
        self.app.bot_data["deep_limit"] = 1
        self.follow("Cached")
        self.follow("New")
        self.follow("Another")
        self.metas.update(cached=meta(), new=meta(), another=meta())
        self.cache.side_effect = lambda h, *args: {**meta(), "_verified": True} if h == "Cached" else None
        await pipeline.push_deepseek_summary(self.app)
        self.assertEqual(self.research.await_count, 2)
        self.assertEqual({c.args[0] for c in self.research.await_args_list}, {"New", "Another"})
        self.assertEqual(self.chain_check.await_count, 2)
        self.assertEqual({c.args[0] for c in self.chain_check.await_args_list}, {"New", "Another"})

    async def test_related_developer_also_gets_follower_lookup(self):
        self.follow()
        self.metas["demo"] = meta(is_nft=True, devs=["Builder"])
        await pipeline.push_deepseek_summary(self.app)
        self.assertIn("Builder", [c.args[0] for c in self.profiles.await_args_list])


if __name__ == "__main__":
    unittest.main()
