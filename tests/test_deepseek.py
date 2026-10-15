import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from src import deepseek as ds
from src import grok
from src.storage import Storage


def report(**updates):
    return {"category": "project", "is_project": True, "is_nft": False, "is_launchpad": False,
            "devs": [], "dev_projects": [], "information_found": True, "summary": "隐私支付协议",
            "followers": None, "followers_source": "",
            "website": {"url": "https://demo.example/", "status": "read", "finding": "为支付提供隐私保护"},
            "whitepaper": {"url": "", "status": "not_found", "finding": ""},
            "third_party": {"status": "not_found", "finding": "", "sources": []},
            "official_ca": [], "catalyst": {"status": "unknown", "type": "unknown", "finding": "", "sources": []},
            "sources": ["https://demo.example/"], **updates}


def response(body=None, blocks=None, stop="end_turn"):
    return {"stop_reason": stop, "model": "deepseek-v4-pro", "content": blocks if blocks is not None else [
        {"type": "text", "text": json.dumps(body or report(), ensure_ascii=False)}]}


class ReportValidationTests(unittest.TestCase):
    def setUp(self):
        self.e = ds.Evidence("Demo")
        self.e.searches = 1
        self.e.add("https://demo.example/", "Privacy payment protocol")

    def parse(self, r):
        return ds.parse_report(json.dumps(r), self.e, "project")

    def test_model_cannot_claim_unread_website_was_read(self):
        parsed = self.parse(report())
        self.assertEqual(parsed["website"]["status"], "unavailable")

    def test_read_excerpt_is_marked_partial(self):
        self.e.read.add("https://demo.example/")
        self.e.partial.add("https://demo.example/")
        parsed = self.parse(report())
        self.assertEqual(parsed["website"]["status"], "read")
        self.assertTrue(parsed["website"]["partial"])

    def test_no_actual_search_rejected_even_with_bio(self):
        self.e.searches = 0
        with self.assertRaises(ValueError):
            self.parse(report())

    def test_unseen_source_is_not_published(self):
        parsed = self.parse(report(sources=["https://invented.example/"]))
        self.assertNotIn("https://invented.example/", parsed["_sources"])

    def test_ca_requires_original_account_post_and_exact_address(self):
        ca = "0x" + "a" * 40
        real, fake = "https://x.com/Demo/status/123", "https://x.com/Imposter/status/456"
        self.e.add(real, "Our CA: " + ca)
        self.e.add(fake, "Our CA: " + ca)
        parsed = self.parse(report(official_ca=[
            {"address": ca, "source": fake, "narrative": "假的"},
            {"address": "0x" + "b" * 40, "source": real},
            {"address": ca, "source": real, "chain": "ethereum", "narrative": "隐私支付"},
        ]))
        self.assertEqual(len(parsed["official_ca"]), 1)
        self.assertEqual(parsed["official_ca"][0]["source"], real)

    def test_token_rally_without_market_evidence_is_not_a_cause(self):
        parsed = self.parse(report(catalyst={"status": "supported", "type": "token_rally",
                      "finding": "币涨了100倍，所以被关注", "sources": ["https://demo.example/"]}))
        self.assertIn("不能归因", parsed["catalyst"]["finding"])
        self.assertEqual(parsed["catalyst"]["status"], "possible")

    def test_chain_without_ca_needs_own_source_real_quote_and_matching_chain(self):
        real = 'https://x.com/Demo/status/123'
        other = 'https://x.com/Other/status/456'
        quote = 'We will launch on Base next month'
        self.e.add(real, quote)
        self.e.add(other, quote)
        rows = [{'chain':'base','kind':'official_post','relation':'planned_launch','source':u,'quote':q}
                for u,q in [(real,quote),(other,quote),(real,'Launching on Solana')]]
        rows.append({**rows[0], 'chain':'ethereum'})
        parsed = self.parse(report(chain_evidence=rows))
        self.assertEqual(len(parsed['chain_evidence']),1)
        self.assertEqual(parsed['chain_evidence'][0]['chain'],'base')
        self.assertEqual(parsed['chain_evidence'][0]['relation'],'planned_launch')
        self.assertEqual(parsed['official_ca'],[])
        negative = 'Not building on Base'
        self.e.add(real, negative)
        parsed = self.parse(report(chain_evidence=[{**rows[0], 'quote':negative}]))
        self.assertEqual(parsed['chain_evidence'],[])

    def test_follower_count_must_be_exact_and_account_specific(self):
        self.e.add("https://x.com/Demo", "3124 Followers")
        self.assertIsNone(self.parse(report(followers=12, followers_source="https://x.com/Demo"))["followers"])
        self.assertEqual(self.parse(report(followers=3124, followers_source="https://x.com/Demo"))["followers"], 3124)

    def test_not_found_does_not_fabricate_project(self):
        parsed = self.parse(report(information_found=False))
        self.assertFalse(parsed["is_project"])
        self.assertEqual(parsed["category"], "other")

    def test_stale_developer_event_not_used_as_recent_catalyst(self):
        body = report(catalyst={"type": "new_project", "sources": ["https://demo.example/"],
                               "events": [{"date": "2020-01-01", "source": "https://demo.example/", "finding": "发布新项目"}]})
        parsed = ds.parse_report(json.dumps(body), self.e, "crypto_dev")
        self.assertEqual(parsed["catalyst"]["status"], "unknown")
        self.assertIn("近30天", parsed["catalyst"]["finding"])

    def test_recent_developer_event_is_dated_and_only_possible_cause(self):
        body = report(catalyst={"type": "new_project", "events": [
            {"date": "2026-09-27", "source": "https://demo.example/", "finding": "发布测试网"}]})
        with patch.object(ds.time, "time", return_value=1790460000):
            parsed = ds.parse_report(json.dumps(body), self.e, "crypto_dev")
        self.assertEqual(parsed["catalyst"]["status"], "possible")
        self.assertIn("发布测试网", parsed["catalyst"]["finding"])


class RequestPayloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_selection_preserves_research_and_final_request_contract(self):
        requests = []

        def capture(request):
            requests.append(request)
            payload = json.loads(request.content)
            return httpx.Response(200, json={"model": payload["model"], "content": [], "stop_reason": "end_turn"})

        real_client = httpx.AsyncClient
        tools = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}, *ds.TOOLS]
        messages = [{"role": "user", "content": "Research the test project"}]
        for override, expected in ((None, "deepseek-flash"), ("deepseek-v4-pro", "deepseek-v4-pro")):
            env = {"DEEPSEEK_API_KEY": "unit-test-only"}
            if override:
                env["DEEPSEEK_MODEL"] = override
            with self.subTest(model=expected), patch.dict(os.environ, env, clear=True), \
                 patch.object(ds.httpx, "AsyncClient", side_effect=lambda **kw:
                              real_client(transport=httpx.MockTransport(capture), **kw)):
                for final in (False, True):
                    result = await ds._post(messages, [] if final else tools, final=final)
                    request = requests[-1]
                    payload = json.loads(request.content)
                    self.assertEqual(str(request.url), "https://api.deepseek.com/anthropic/v1/messages")
                    self.assertEqual(request.headers["x-api-key"], "unit-test-only")
                    self.assertEqual(payload["model"], expected)
                    self.assertEqual(result["model"], expected)
                    self.assertEqual(payload["messages"], messages)
                    self.assertEqual(payload["system"], ds.SYSTEM)
                    self.assertEqual(payload["output_config"], {"effort": "high"})
                    self.assertEqual(payload["thinking"], {"type": "disabled" if final else "enabled"})
                    self.assertEqual(payload["max_tokens"], 2500 if final else 5500)
                    if final:
                        self.assertNotIn("tools", payload)
                    else:
                        self.assertEqual(payload["tools"], tools)
        self.assertEqual(len(requests), 4)

    async def test_messages_endpoint_override_and_empty_legacy_fallback(self):
        requests = []

        def capture(request):
            requests.append(request)
            return httpx.Response(200, json={"model": "deepseek-v4-1-flash-260910",
                                            "content": [], "stop_reason": "end_turn"})

        real_client = httpx.AsyncClient
        direct_url = "https://api.deepseek.com/anthropic/v1/messages"
        gateway_url = "https://api.teamorouter.com/v1/messages"
        tools = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}, *ds.TOOLS]
        cases = (
            ({}, direct_url),
            ({"DEEPSEEK_MESSAGES_URL": ""}, direct_url),
            ({"DEEPSEEK_MESSAGES_URL": " \t "}, direct_url),
            ({"DEEPSEEK_BASE_URL": "https://legacy.example/", "DEEPSEEK_MESSAGES_URL": ""},
             "https://legacy.example/anthropic/v1/messages"),
            ({"DEEPSEEK_BASE_URL": "https://api.teamorouter.com/v1/",
              "DEEPSEEK_MESSAGES_URL": " " + gateway_url + "/ \t"}, gateway_url),
        )
        for config, expected_url in cases:
            with self.subTest(config=config), \
                 patch.dict(os.environ, {"DEEPSEEK_API_KEY": "unit-test-only", **config}, clear=True), \
                 patch.object(ds.httpx, "AsyncClient", side_effect=lambda **kw:
                              real_client(transport=httpx.MockTransport(capture), **kw)):
                for final in (False, True):
                    result = await ds._post([{"role": "user", "content": "Test"}],
                                            [] if final else tools, final=final)
                    request = requests[-1]
                    payload = json.loads(request.content)
                    self.assertEqual(str(request.url), expected_url)
                    self.assertEqual(request.headers["x-api-key"], "unit-test-only")
                    self.assertEqual(request.headers["anthropic-version"], "2023-06-01")
                    self.assertEqual(payload["model"], "deepseek-flash")
                    self.assertEqual(result["model"], "deepseek-v4-1-flash-260910")
                    self.assertEqual(payload["output_config"], {"effort": "high"})
                    self.assertEqual(payload["thinking"], {"type": "disabled" if final else "enabled"})
                    self.assertEqual(payload["max_tokens"], 2500 if final else 5500)
                    if final:
                        self.assertNotIn("tools", payload)
                    else:
                        self.assertEqual(payload["tools"], tools)
        self.assertEqual(len(requests), 2 * len(cases))


class ResearchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.storage = Storage(str(Path(self.tmp.name) / "test.db"))
        self.ctx = {"bio": "privacy protocol", "known_kols": ["alice", "bob"]}

    async def test_real_tool_loop_reads_and_caches(self):
        start = response(blocks=[
            {"type": "server_tool_use", "name": "web_search", "id": "s1"},
            {"type": "web_search_tool_result", "content": [{"url": "https://demo.example/", "title": "Demo"}]},
            {"type": "tool_use", "id": "f1", "name": "read_page", "input": {"url": "https://demo.example/"}},
        ], stop="tool_use")
        with patch.object(ds, "_post", AsyncMock(side_effect=[start, response()])) as post, \
             patch.object(ds, "read_page", AsyncMock(return_value={"read": True, "url": "https://demo.example/", "text": "protocol", "links": []})) as reader:
            r = await ds.research("Demo", "project", self.ctx, self.storage)
            self.assertEqual(r["website"]["status"], "read")
            self.assertEqual(r["_research_status"], "complete")
            again = await ds.research("Demo", "project", self.ctx, self.storage)
            self.assertEqual(again, r)
            self.assertEqual(post.await_count, 2)
            reader.assert_awaited_once()
            first_tools = post.await_args_list[0].args[1]
            self.assertEqual(first_tools, [
                {"type": "web_search_20250305", "name": "web_search", "max_uses": 3}, *ds.TOOLS])

    async def test_new_kol_invalidates_research_but_repeated_one_does_not(self):
        result = {"_verified": True, "_researched_at": 1800000000, "summary": "测试"}
        with patch.object(ds, "_run", AsyncMock(return_value=result)) as run, patch.object(ds.time, "time", return_value=1800000000):
            await ds.research("Demo", "project", self.ctx, self.storage)
            await ds.research("Demo", "project", self.ctx, self.storage)
            self.assertEqual(run.await_count, 1)
            changed = {**self.ctx, "known_kols": ["alice", "bob", "carol"]}
            await ds.research("Demo", "project", changed, self.storage)
            self.assertEqual(run.await_count, 2)

    async def test_failure_retains_old_grok_fallback_without_caching_as_deep(self):
        with patch.object(ds, "_run", AsyncMock(side_effect=RuntimeError("offline"))), \
             patch.object(ds.grok, "research_account", AsyncMock(return_value=grok._default_meta())) as fallback:
            r = await ds.research("Demo", "project", self.ctx, self.storage)
            self.assertEqual(r["_research_status"], "fallback_brief")
            self.assertIsNone(ds.cached_research("Demo", "project", self.ctx, self.storage))
            fallback.assert_awaited_once()

    async def test_market_tool_refuses_unverified_ca(self):
        start = response(blocks=[{"type": "tool_use", "id": "f1", "name": "token_market",
                    "input": {"ca": "0x" + "a" * 40, "official_source": "https://x.com/Demo/status/123"}}], stop="tool_use")
        end = response(blocks=[{"type": "server_tool_use", "name": "web_search"},
                              {"type": "text", "text": json.dumps(report())}])
        with patch.object(ds, "_post", AsyncMock(side_effect=[start, end])) as post, \
             patch.object(ds, "token_market", AsyncMock()) as market:
            await ds._run("Demo", "project", self.ctx, self.storage)
            market.assert_not_called()

    async def test_developer_cache_is_one_hour(self):
        result = {"_verified": True, "_researched_at": 1800000000}
        with patch.object(ds, "_run", AsyncMock(return_value=result)) as run, patch.object(ds.time, "time", return_value=1800000000) as clock:
            await ds.research("Demo", "crypto_dev", self.ctx, self.storage)
            clock.return_value += 3600
            await ds.research("Demo", "crypto_dev", self.ctx, self.storage)
            self.assertEqual(run.await_count, 2)


if __name__ == "__main__":
    unittest.main()
