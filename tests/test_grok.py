import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from src import grok


def meta_response(handle="demo"):
    return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps({
        "results": [{"handle": handle, "category": "project", "is_project": True,
                     "is_nft": False, "devs": ["Builder"], "dev_projects": [],
                     "summary": "测试项目"}],
    })}}]}


def deep_response(devs=None):
    return {"status": "completed", "output": [
        {"type": "web_search_call", "status": "completed"},
        {"type": "message", "content": [{"type": "output_text", "text": json.dumps({
            "devs": ["@Builder", "builder"] if devs is None else devs,
        })}]},
        {"type": "custom_tool_call", "status": "completed"},
    ]}


def research_response(found=True):
    rows = json.loads(meta_response()["choices"][0]["message"]["content"])
    rows["results"][0]["information_found"] = found
    return {"status": "completed", "output": [
        {"type": "web_search_call", "status": "completed", "action": {"sources": [
            {"url": "https://a-unrelated.example/"}, {"url": "https://x.com/demo"}]}},
        {"type": "message", "content": [{"type": "output_text", "text": json.dumps(rows),
            "annotations": [{"type": "url_citation", "url": "https://demo.example/docs"}]}]},
    ]}


class GrokRoutingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = str(Path(self.tmp.name) / "test.db")
        self.items = [{"handle": "demo", "bio": "Built by @Builder", "tweets": []}]
        self.requests = []
        env = patch.dict(os.environ, {
            "TEAMOROUTER_API_KEY": "primary-secret", "XAI_API_KEY": "fallback-secret",
            "XAI_MODEL_BASIC": "original-basic", "XAI_MODEL_DEEP": "original-deep",
        }, clear=True)
        env.start()
        self.addCleanup(env.stop)

    def mock_http(self, handler):
        def capture(request):
            self.requests.append(request)
            expected_key = {"api.teamorouter.com": "primary-secret", "api.x.ai": "fallback-secret",
                            "api.deepseek.com": "ds-secret"}[request.url.host]
            if request.url.host == "api.teamorouter.com" and json.loads(request.content)["model"].startswith("deepseek-"):
                expected_key = "ds-secret"
            self.assertEqual(request.headers["Authorization"], f"Bearer {expected_key}")
            return handler(request)

        real_client = httpx.AsyncClient
        client_patch = patch.object(grok.httpx, "AsyncClient", side_effect=lambda **kw:
                                    real_client(transport=httpx.MockTransport(capture), **kw))
        client_patch.start()
        self.addCleanup(client_patch.stop)

    def assert_route(self, kind):
        self.assertEqual([r.url.host for r in self.requests],
                         ["api.teamorouter.com", "api.x.ai"])
        self.assertEqual([json.loads(r.content)["model"] for r in self.requests],
                         ["grok-4.6", f"original-{kind}"])

    async def test_primary_classification_and_cache(self):
        self.mock_http(lambda r: httpx.Response(200, json=meta_response()))
        result = await grok.analyze_batch(self.items, self.db)
        self.assertEqual(result["demo"]["devs"], ["Builder"])
        self.assertEqual(await grok.analyze_batch(self.items, self.db), result)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.requests[0].url.path, "/v1/chat/completions")
        self.assertEqual(json.loads(self.requests[0].content)["model"], "grok-4.6")

    async def test_deepseek_basic_is_non_thinking_and_original_providers_retained(self):
        os.environ["DEEPSEEK_API_KEY"] = "ds-secret"
        self.mock_http(lambda r: httpx.Response(200, json=meta_response()))
        await grok.analyze_batch(self.items, self.db)
        p = json.loads(self.requests[0].content)
        self.assertEqual(self.requests[0].url.host, "api.deepseek.com")
        self.assertEqual(p["model"], "deepseek-flash")
        self.assertEqual(p["thinking"], {"type": "disabled"})
        self.assertEqual([p.name for p in grok._providers("deep")], ["teamorouter", "xai"])

    async def test_deepseek_basic_failure_falls_back_to_teamo(self):
        os.environ["DEEPSEEK_API_KEY"] = "ds-secret"
        self.mock_http(lambda r: httpx.Response(503) if r.url.host == "api.deepseek.com"
                       else httpx.Response(200, json=meta_response()))
        r = await grok.analyze_batch(self.items, self.db)
        self.assertTrue(r["demo"]["is_project"])
        self.assertEqual([r.url.host for r in self.requests], ["api.deepseek.com", "api.teamorouter.com"])

    async def test_deepseek_basic_respects_explicit_model_override(self):
        os.environ.update(DEEPSEEK_API_KEY="ds-secret", DEEPSEEK_MODEL="deepseek-v4-pro")
        self.mock_http(lambda r: httpx.Response(200, json=meta_response()))
        result = await grok.analyze_batch(self.items, self.db)
        self.assertTrue(result["demo"]["is_project"])
        self.assertEqual(len(self.requests), 1)
        payload = json.loads(self.requests[0].content)
        self.assertEqual(payload["model"], "deepseek-v4-pro")
        self.assertEqual(payload["thinking"], {"type": "disabled"})

    async def test_deepseek_gateway_classification_keeps_chat_route(self):
        os.environ.update(DEEPSEEK_API_KEY="ds-secret", DEEPSEEK_BASE_URL="https://api.teamorouter.com/v1/",
                          DEEPSEEK_MESSAGES_URL="https://api.teamorouter.com/v1/messages")
        self.mock_http(lambda r: httpx.Response(200, json=meta_response()))
        result = await grok.analyze_batch(self.items, self.db)
        self.assertTrue(result["demo"]["is_project"])
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(str(self.requests[0].url), "https://api.teamorouter.com/v1/chat/completions")
        payload = json.loads(self.requests[0].content)
        self.assertEqual(payload["model"], "deepseek-flash")
        self.assertEqual(payload["thinking"], {"type": "disabled"})
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertNotIn("x-api-key", self.requests[0].headers)

    async def test_deepseek_gateway_failure_keeps_original_grok_and_xai_fallbacks(self):
        os.environ.update(DEEPSEEK_API_KEY="ds-secret", DEEPSEEK_BASE_URL="https://api.teamorouter.com/v1",
                          DEEPSEEK_MESSAGES_URL="https://api.teamorouter.com/v1/messages")
        self.mock_http(lambda r: httpx.Response(200, json=meta_response())
                       if r.url.host == "api.x.ai" else httpx.Response(503))
        result = await grok.analyze_batch(self.items, self.db)
        self.assertTrue(result["demo"]["is_project"])
        self.assertEqual([str(r.url) for r in self.requests],
                         ["https://api.teamorouter.com/v1/chat/completions",
                          "https://api.teamorouter.com/v1/chat/completions",
                          "https://api.x.ai/v1/chat/completions"])
        self.assertEqual([json.loads(r.content)["model"] for r in self.requests],
                         ["deepseek-flash", "grok-4.6", "original-basic"])
        self.assertEqual([(p.name, p.model) for p in grok._providers("deep")],
                         [("teamorouter", "grok-4.6"), ("xai", "original-deep")])

    async def test_deepseek_and_teamo_failures_preserve_original_xai_model(self):
        os.environ["DEEPSEEK_API_KEY"] = "ds-secret"
        self.mock_http(lambda r: httpx.Response(200, json=meta_response())
                       if r.url.host == "api.x.ai" else httpx.Response(503))
        result = await grok.analyze_batch(self.items, self.db)
        self.assertTrue(result["demo"]["is_project"])
        self.assertEqual([r.url.host for r in self.requests],
                         ["api.deepseek.com", "api.teamorouter.com", "api.x.ai"])
        self.assertEqual([json.loads(r.content)["model"] for r in self.requests],
                         ["deepseek-flash", "grok-4.6", "original-basic"])
        self.assertNotIn("thinking", json.loads(self.requests[-1].content))
        self.assertEqual([(p.name, p.model) for p in grok._providers("deep")],
                         [("teamorouter", "grok-4.6"), ("xai", "original-deep")])

    async def test_http_failure_falls_back_preserving_original_model(self):
        self.mock_http(lambda r: httpx.Response(429) if r.url.host == "api.teamorouter.com"
                       else httpx.Response(200, json=meta_response()))
        result = await grok.analyze_batch(self.items, self.db)
        self.assertTrue(result["demo"]["is_project"])
        self.assert_route("basic")

    async def test_timeout_falls_back(self):
        def handler(r):
            if r.url.host == "api.teamorouter.com":
                raise httpx.ReadTimeout("timed out", request=r)
            return httpx.Response(200, json=meta_response())
        self.mock_http(handler)
        self.assertTrue((await grok.analyze_batch(self.items, self.db))["demo"]["is_project"])
        self.assert_route("basic")

    async def test_invalid_json_falls_back(self):
        self.mock_http(lambda r: httpx.Response(200, text="not JSON")
                       if r.url.host == "api.teamorouter.com"
                       else httpx.Response(200, json=meta_response()))
        self.assertTrue((await grok.analyze_batch(self.items, self.db))["demo"]["is_project"])
        self.assert_route("basic")

    async def test_missing_handle_does_not_poison_cache(self):
        self.mock_http(lambda r: httpx.Response(200, json=meta_response("wrong"))
                       if r.url.host == "api.teamorouter.com"
                       else httpx.Response(200, json=meta_response()))
        result = await grok.analyze_batch(self.items, self.db)
        self.assertTrue(result["demo"]["is_project"])
        self.assert_route("basic")

    async def test_all_providers_fail_defaults_not_cached(self):
        self.mock_http(lambda r: httpx.Response(503))
        with self.assertLogs(grok.log, level="WARNING") as captured:
            result = await grok.analyze_batch(self.items, self.db)
        self.assertEqual(result["demo"], grok._default_meta())
        with sqlite3.connect(self.db) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM handle_meta").fetchone()[0], 0)
        self.assertNotIn("primary-secret", " ".join(captured.output))
        self.assertNotIn("fallback-secret", " ".join(captured.output))

    async def test_no_teamo_key_uses_xai_directly(self):
        os.environ.pop("TEAMOROUTER_API_KEY")
        self.mock_http(lambda r: httpx.Response(200, json=meta_response()))
        await grok.analyze_batch(self.items, self.db)
        self.assertEqual([r.url.host for r in self.requests], ["api.x.ai"])
        self.assertEqual(json.loads(self.requests[0].content)["model"], "original-basic")

    async def test_deep_primary_preserves_search_tools_and_cache(self):
        self.mock_http(lambda r: httpx.Response(200, json=deep_response()))
        result = await grok.find_devs_deep("demo", self.db)
        self.assertEqual(result, ["Builder"])
        self.assertEqual(await grok.find_devs_deep("DEMO", self.db), result)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.requests[0].url.path, "/v1/responses")
        payload = json.loads(self.requests[0].content)
        self.assertEqual(payload["tools"], [{"type": "x_search"}, {"type": "web_search"}])
        self.assertEqual(payload["max_tool_calls"], 1)
        self.assertEqual(payload["tool_choice"], "auto")

    async def test_deep_unauthorized_falls_back(self):
        self.mock_http(lambda r: httpx.Response(401) if r.url.host == "api.teamorouter.com"
                       else httpx.Response(200, json=deep_response()))
        self.assertEqual(await grok.find_devs_deep("demo", self.db), ["Builder"])
        self.assert_route("deep")

    async def test_deep_malformed_output_falls_back(self):
        self.mock_http(lambda r: httpx.Response(200, json={"output": []})
                       if r.url.host == "api.teamorouter.com"
                       else httpx.Response(200, json=deep_response()))
        self.assertEqual(await grok.find_devs_deep("demo", self.db), ["Builder"])
        self.assert_route("deep")

    async def test_deep_incomplete_response_falls_back(self):
        self.mock_http(lambda r: httpx.Response(200, json={**deep_response(), "status": "incomplete"})
                       if r.url.host == "api.teamorouter.com"
                       else httpx.Response(200, json=deep_response()))
        self.assertEqual(await grok.find_devs_deep("demo", self.db), ["Builder"])
        self.assert_route("deep")

    async def test_deep_valid_empty_result_does_not_trigger_fallback(self):
        self.mock_http(lambda r: httpx.Response(200, json=deep_response([])))
        self.assertEqual(await grok.find_devs_deep("demo", self.db), [])
        self.assertEqual(len(self.requests), 1)

    async def test_deep_total_failure_not_cached(self):
        self.mock_http(lambda r: httpx.Response(502))
        self.assertEqual(await grok.find_devs_deep("demo", self.db), [])
        with sqlite3.connect(self.db) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM handle_deep").fetchone()[0], 0)

    async def test_empty_profile_skips_useless_basic_call(self):
        self.mock_http(lambda r: self.fail("empty inputs must not call basic AI"))
        item = {"handle": "demo", "bio": "", "tweets": []}
        result = await grok.analyze_batch([item], self.db)
        self.assertTrue(grok.needs_research(item, result["demo"]))
        self.assertEqual(result["demo"]["summary"], "待联网核查")
        with sqlite3.connect(self.db) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM handle_meta").fetchone()[0], 0)

    async def test_old_insufficient_cache_ignored(self):
        grok._ensure_tables(self.db)
        grok._write_meta_cache(self.db, {"demo": {**grok._default_meta(), "summary": "信息不足无法推断"}})
        self.mock_http(lambda r: httpx.Response(200, json=meta_response()))
        result = await grok.analyze_batch(self.items, self.db)
        self.assertTrue(result["demo"]["is_project"])
        self.assertEqual(len(self.requests), 1)

    async def test_changed_source_bio_invalidates_basic_cache(self):
        self.mock_http(lambda r: httpx.Response(200, json=meta_response()))
        await grok.analyze_batch(self.items, self.db)
        self.items[0]["bio"] = "A new privacy protocol"
        await grok.analyze_batch(self.items, self.db)
        self.assertEqual(len(self.requests), 2)
        self.assertIn("privacy", json.loads(self.requests[-1].content)["messages"][1]["content"])

    async def test_research_requires_tools_and_reuses_team_cache(self):
        self.mock_http(lambda r: httpx.Response(200, json=research_response()))
        result = await grok.research_account("demo", self.db)
        self.assertTrue(result["_verified"])
        self.assertEqual(result["_sources"][0], "https://x.com/demo")
        self.assertEqual(await grok.research_account("DEMO", self.db), result)
        self.assertEqual(await grok.find_devs_deep("demo", self.db), ["Builder"])
        cached = await grok.analyze_batch([{"handle": "demo", "bio": "", "tweets": []}], self.db)
        self.assertFalse(grok.needs_research({}, cached["demo"]))
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(json.loads(self.requests[0].content)["tool_choice"], "required")

    async def test_research_without_actual_search_falls_back(self):
        fake = research_response()
        fake["output"] = fake["output"][1:]
        self.mock_http(lambda r: httpx.Response(200, json=fake if r.url.host == "api.teamorouter.com"
                                               else research_response()))
        self.assertTrue((await grok.research_account("demo", self.db))["_verified"])
        self.assert_route("deep")

    async def test_research_without_sources_falls_back(self):
        fake = research_response()
        fake["output"][0]["action"]["sources"] = []
        fake["output"][1]["content"][0]["annotations"] = []
        self.mock_http(lambda r: httpx.Response(200, json=fake if r.url.host == "api.teamorouter.com"
                                               else research_response()))
        self.assertTrue((await grok.research_account("demo", self.db))["_verified"])
        self.assert_route("deep")

    async def test_search_not_found_cannot_claim_project_and_expires_soon(self):
        self.mock_http(lambda r: httpx.Response(200, json=research_response(False)))
        with patch.object(grok.time, "time", return_value=1800000000) as clock:
            result = await grok.research_account("demo", self.db)
            self.assertFalse(result["_verified"])
            self.assertFalse(result["is_project"])
            self.assertFalse(result["devs"])
            self.assertEqual(result["summary"], "联网检索未找到可核实资料")
            clock.return_value += 1799
            await grok.research_account("demo", self.db)
            self.assertEqual(len(self.requests), 1)
            clock.return_value += 1
            await grok.research_account("demo", self.db)
            self.assertEqual(len(self.requests), 2)

    async def test_verified_research_expires_after_one_day(self):
        self.mock_http(lambda r: httpx.Response(200, json=research_response()))
        with patch.object(grok.time, "time", return_value=1800000000) as clock:
            await grok.research_account("demo", self.db)
            clock.return_value += 86400
            await grok.research_account("demo", self.db)
            self.assertEqual(len(self.requests), 2)

    async def test_research_failure_not_cached(self):
        self.mock_http(lambda r: httpx.Response(503))
        result = await grok.research_account("demo", self.db)
        self.assertNotIn("_verified", result)
        with sqlite3.connect(self.db) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM handle_meta").fetchone()[0], 0)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM handle_deep").fetchone()[0], 0)

    def test_description_not_silently_cut_at_forty_characters(self):
        data = meta_response()
        body = json.loads(data["choices"][0]["message"]["content"])
        summary = "账号自述：" + "隐私协议" * 15 + "，团队身份尚未独立证实。"
        body["results"][0]["summary"] = summary
        data["choices"][0]["message"]["content"] = json.dumps(body)
        self.assertEqual(grok._parse_meta_response(data, {"demo"})["demo"]["summary"], summary)

    def test_oversize_description_explicitly_ellipsized(self):
        self.assertEqual(grok._short_summary("测" * 300), "测" * 159 + "…")


if __name__ == "__main__":
    unittest.main()
