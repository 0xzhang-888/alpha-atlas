import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from src import chain_preflight as cp
from src.storage import Storage


def claim(chain="base", evidence_id="bio", quote="Building on Base", relation="ecosystem"):
    return {"chain": chain, "evidence_id": evidence_id, "quote": quote, "relation": relation}


class ChainPreflightTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.storage = Storage(str(Path(self.tmp.name) / "test.db"))
        self.context = {"bio": "Building on Base", "known_kols": ["alice"]}
        self.clock = patch.object(cp.time, "time", return_value=2_000_000_000)
        self.now = self.clock.start()
        self.addCleanup(self.clock.stop)
        self.request_patch = patch.object(cp, "_request", new_callable=AsyncMock, return_value={"claims": [claim()]})
        self.request = self.request_patch.start()
        self.addCleanup(self.request_patch.stop)

    async def test_real_ai_stage_and_cached_evidence_signature(self):
        first = await cp.check("Demo", self.context, self.storage)
        self.assertEqual(first["status"], "complete")
        self.assertTrue(first["ai_checked"])
        self.assertEqual(first["chains"], ["base"])
        self.assertEqual(first["model"], "deepseek-flash")
        self.assertEqual(self.storage.cache_get(f"chain-check:v{cp.VERSION}:demo", 1), first)
        repeated = await cp.check("demo", {**self.context, "known_kols": ["alice", "bob"]}, self.storage)
        self.assertEqual(first, repeated)
        self.request.assert_awaited_once()
        self.request.return_value = {"claims": [claim("monad", quote="Building on Monad")]}
        changed = await cp.check("Demo", {"bio": "Building on Monad"}, self.storage)
        self.assertEqual(changed["chains"], ["monad"])
        self.assertNotEqual(changed["signature"], first["signature"])
        self.assertEqual(self.request.await_count, 2)

    async def test_success_unknown_failure_have_distinct_ttls(self):
        await cp.check("Demo", self.context, self.storage)
        self.now.return_value += cp.SUCCESS_TTL - 1
        await cp.check("Demo", self.context, self.storage)
        self.request.assert_awaited_once()
        self.now.return_value += 1
        self.request.return_value = {"claims": []}
        unknown = await cp.check("Demo", self.context, self.storage)
        self.assertEqual(unknown["status"], "unknown")
        self.assertTrue(unknown["ai_checked"])
        self.assertEqual(unknown["chains"], [])
        self.now.return_value += cp.UNKNOWN_TTL
        self.request.side_effect = TimeoutError("sensitive provider response")
        failed = await cp.check("Demo", self.context, self.storage)
        self.assertEqual(failed["status"], "failed")
        self.assertFalse(failed["ai_checked"])
        self.assertNotIn("sensitive", json.dumps(failed))
        calls = self.request.await_count
        self.now.return_value += cp.FAILURE_TTL - 1
        self.assertEqual(await cp.check("Demo", self.context, self.storage), failed)
        self.assertEqual(self.request.await_count, calls)
        self.now.return_value += 1
        await cp.check("Demo", self.context, self.storage)
        self.assertEqual(self.request.await_count, calls + 1)

    async def test_model_change_invalidates_cached_check_only_once(self):
        with patch.dict(cp.os.environ, {"DEEPSEEK_MODEL": "deepseek-v4-pro"}):
            old = await cp.check("Demo", self.context, self.storage)
            self.assertEqual(await cp.check("Demo", self.context, self.storage), old)
        self.request.assert_awaited_once()
        with patch.dict(cp.os.environ, {"DEEPSEEK_MODEL": "deepseek-flash"}):
            changed = await cp.check("Demo", self.context, self.storage)
            self.assertEqual(changed["model"], "deepseek-flash")
            self.assertNotEqual(changed["signature"], old["signature"])
            self.assertEqual(changed["chains"], old["chains"])
            self.assertEqual(self.request.await_count, 2)
            self.assertEqual(await cp.check("demo", self.context, self.storage), changed)
            self.assertEqual(await cp.check("demo", self.context, self.storage), changed)
        self.assertEqual(self.request.await_count, 2)
        self.assertEqual([call.args[2] for call in self.request.await_args_list],
                         ["deepseek-v4-pro", "deepseek-flash"])

    async def test_identical_concurrent_checks_share_one_request(self):
        async def delayed(*args):
            await asyncio.sleep(0)
            return {"claims": [claim()]}
        self.request.side_effect = delayed
        first, second = await asyncio.gather(cp.check("Demo", self.context, self.storage), cp.check("Demo", self.context, self.storage))
        self.assertEqual(first, second)
        self.request.assert_awaited_once()

    async def test_changed_failure_evidence_retries_without_waiting(self):
        self.request.side_effect = RuntimeError("offline")
        await cp.check("Demo", self.context, self.storage)
        self.request.side_effect = None
        self.request.return_value = {"claims": [claim("monad", quote="Building on Monad")]}
        result = await cp.check("Demo", {"bio": "Building on Monad"}, self.storage)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(self.request.await_count, 2)

    async def test_fabricated_or_negative_evidence_is_not_a_completed_check(self):
        for body in ({"claims": [claim("solana")]}, {"claims": [claim(quote="We are live on Base")]}, {"claims": [claim(evidence_id="invented")]}, {"claims": "base"}):
            self.request.return_value = body
            result = await cp.check("Demo", self.context, self.storage)
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["chains"], [])
            self.now.return_value += cp.FAILURE_TTL
        self.request.return_value = {"claims": [claim(quote="Not building on Base")]}
        result = await cp.check("Demo", {"bio": "Not building on Base"}, self.storage)
        self.assertEqual(result["status"], "failed")

    async def test_empty_evidence_can_only_produce_unknown_not_guess(self):
        self.request.return_value = {"claims": []}
        result = await cp.check("Empty", {"bio": ""}, self.storage)
        self.assertEqual(result["status"], "unknown")
        self.assertTrue(result["ai_checked"])
        self.assertEqual(self.request.call_args.args[1], [])

    async def test_follow_direction_registry_and_original_posts(self):
        self.storage.add_event("Demo", "Base", 1, follower_handle="Demo")
        self.storage.add_event("Monad", "Demo", 2, follower_handle="Monad")
        self.storage.add_event("Demo", "FakeBase", 3, follower_handle="Demo")
        self.storage.cache_set("x-account-evidence:v1:demo", {"searched_at": int(cp.time.time()), "posts": [
            {"author": "Demo", "url": "https://x.com/Demo/status/123?token=must-not-leak", "text": "We will launch on Monad next month"},
            {"author": "Other", "url": "https://x.com/Other/status/456", "text": "Building on Solana"},
            {"author": "Demo", "url": "https://x.com/Demo/status/789", "text": "  RT @Other: Building on Arc"},
        ]})
        evidence = cp.collect_evidence("Demo", self.context, self.storage)
        self.assertEqual([e["id"] for e in evidence], ["bio", "post:0", "follow:base"])
        self.assertNotIn("must-not-leak", json.dumps(evidence))
        self.assertEqual(evidence[1]["source"], "https://x.com/Demo/status/123")
        self.request.return_value = {"claims": [claim("base", "follow:base", "@demo follows @base", "follows_official")]}
        result = await cp.check("Demo", {"bio": ""}, self.storage)
        self.assertEqual(result["chains"], ["base"])
        self.assertEqual(result["chain_evidence"][0]["confidence"], "inferred")
        self.assertIsNone(result["chain_evidence"][0]["source"])

    async def test_planned_chain_does_not_claim_deployed_and_beats_generic_follows(self):
        self.storage.add_event("Demo", "Base", 1, follower_handle="Demo")
        self.request.return_value = {"claims": [
            claim("monad", quote="We will launch on Monad next month"),
            claim("base", "follow:base", "@demo follows @base", "follows_official"),
        ]}
        result = await cp.check("Demo", {"bio": "We will launch on Monad next month"}, self.storage)
        self.assertEqual(result["chains"], ["monad"])
        self.assertEqual(result["chain_evidence"][0]["relation"], "planned_launch")

    async def test_new_chain_supported_with_explicit_network_entity(self):
        self.request.return_value = {"claims": [claim("freshnet", quote="We are building on Freshnet chain")]}
        result = await cp.check("Demo", {"bio": "We are building on Freshnet chain"}, self.storage)
        self.assertEqual(result["chains"], ["freshnet"])


class ClaimContextTests(unittest.TestCase):
    def validate(self, text, quote=None, *, kind="bio", chain="base", source=None):
        source = source or ("https://x.com/demo/status/123" if kind == "official_post" else "https://x.com/demo")
        evidence = [{"id": "bio", "kind": kind, "source": source, "text": text}]
        return cp.validate_claims({"claims": [claim(chain, quote=quote or text)]}, evidence, "demo")

    def test_semantic_self_claims_are_not_limited_to_fixed_relationship_phrases(self):
        for text in ("Our home is Base.", "We chose Base for our upcoming launch.",
                     "Base is where we are building."):
            for kind in ("bio", "official_post"):
                with self.subTest(text=text, kind=kind):
                    rows = self.validate(text, kind=kind)
                    self.assertEqual(rows[0]["chain"], "base")
                    self.assertEqual(rows[0]["quote"], text)
        self.assertEqual(self.validate("A proud member of the Solana community.", chain="solana")[0]["chain"], "solana")

    def test_cutting_negation_or_a_neighbouring_denial_does_not_create_affiliation(self):
        for text, quote in (
            ("Not building on Base", "building on Base"),
            ("We aren't building on Base.", "building on Base"),
            ("We are building on Base. That claim is false.", "We are building on Base"),
            ("This is not true. Our home is Base.", "Our home is Base"),
            ("如果我们在 Base 上构建，也不会改变定位。", "我们在 Base 上构建"),
        ):
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.validate(text, quote)

    def test_third_party_investment_wallet_and_market_context_is_not_affiliation(self):
        for text, quote in (
            ("Our investors are building on Base.", "building on Base"),
            ("Our partners announced this. We are building on Base.", "We are building on Base"),
            ('They said "Our home is Base."', "Our home is Base"),
            ("Our wallet supports assets on Base.", "assets on Base"),
            ("Our partners are great. @Another: We are building on Base.", "We are building on Base"),
            ("We love Base.", "We love Base"),
            ("Base is great.", "Base is great"),
            ("We are bullish on Base.", "We are bullish on Base"),
            ("Base is faster than Ethereum.", "Base is faster than Ethereum"),
        ):
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.validate(text, quote)

    def test_explicit_third_party_attribution_is_rejected(self):
        for text in ("@Another: We are building on Base.", "@Another said they build on Base."):
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.validate(text, kind="official_post")

    def test_wallet_identity_creator_credit_and_subjectless_self_claims_are_allowed(self):
        for text in ("Our privacy wallet lives on Base. No gas fees.",
                     "Our home is Base. We work without gas fees.",
                     "Built on Base by @builder", "Launching on Base tomorrow"):
            for kind in ("bio", "official_post"):
                with self.subTest(text=text, kind=kind):
                    self.assertEqual(self.validate(text, kind=kind)[0]["chain"], "base")

    def test_original_context_is_saved_not_model_selected_fragment(self):
        text = "Welcome.\nOur home is Base.\nWe build tools."
        row = self.validate(text, "home is Base")[0]
        self.assertEqual(row["quote"], text)
        self.assertEqual(row["confidence"], "declared")

    def test_plans_in_context_cannot_be_cropped_into_current_deployment(self):
        text = "We plan to launch soon. Our home is Base."
        row = self.validate(text, "Our home is Base")[0]
        self.assertEqual(row["quote"], text)
        self.assertEqual(row["relation"], "planned_launch")
        self.assertEqual(row["confidence"], "planned")
        self.assertEqual(self.validate("We chose Base for our upcoming launch.")[0]["relation"], "planned_launch")

    def test_changed_case_ambiguous_quote_and_wrong_chain_are_rejected(self):
        for text, quote, chain in (
            ("Our home is Base.", "our home is base", "base"),
            ("Our home is Base. Our home is Base.", "Our home is Base", "base"),
            ("Our home is Base.", "Our home is Base", "solana"),
        ):
            with self.subTest(text=text, quote=quote, chain=chain), self.assertRaises(ValueError):
                self.validate(text, quote, chain=chain)

    def test_own_account_source_is_still_required(self):
        for kind, source in (("bio", "https://x.com/other"),
                             ("official_post", "https://x.com/other/status/123"),
                             ("official_post", "https://example.com/demo/status/123")):
            with self.subTest(kind=kind, source=source), self.assertRaises(ValueError):
                self.validate("Our home is Base.", kind=kind, source=source)


class RequestBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_request_is_one_bounded_call_and_uses_server_configuration(self):
        response = unittest.mock.Mock()
        response.json.return_value = {"stop_reason": "end_turn", "content": [{"type": "text", "text": '{"claims":[]}'}]}
        client = AsyncMock()
        client.post.return_value = response
        manager = AsyncMock()
        manager.__aenter__.return_value = client
        with patch.object(cp.httpx, "AsyncClient", return_value=manager) as factory, patch.dict(cp.os.environ, {"DEEPSEEK_API_KEY": "unit-test-only", "DEEPSEEK_MODEL": "deepseek-v4-pro"}, clear=True):
            self.assertEqual(await cp._request("demo", [], "deepseek-v4-pro"), {"claims": []})
        factory.assert_called_once_with(timeout=25)
        client.post.assert_awaited_once()
        self.assertEqual(client.post.call_args.args[0], "https://api.deepseek.com/anthropic/v1/messages")
        request = client.post.call_args.kwargs
        self.assertEqual(request["json"]["max_tokens"], 1000)
        self.assertEqual(request["json"]["thinking"], {"type": "disabled"})
        self.assertNotIn("tools", request["json"])
        self.assertEqual(request["headers"]["x-api-key"], "unit-test-only")

    async def test_chain_uses_explicit_gateway_messages_endpoint(self):
        response = unittest.mock.Mock()
        response.json.return_value = {"stop_reason": "end_turn", "model": "deepseek-v4-1-flash-260910",
                                      "content": [{"type": "text", "text": '{"claims":[]}'}]}
        client = AsyncMock()
        client.post.return_value = response
        manager = AsyncMock()
        manager.__aenter__.return_value = client
        env = {"DEEPSEEK_API_KEY": "unit-test-only", "DEEPSEEK_MODEL": "deepseek-flash",
               "DEEPSEEK_BASE_URL": "https://api.teamorouter.com/v1/",
               "DEEPSEEK_MESSAGES_URL": " https://api.teamorouter.com/v1/messages/ "}
        with patch.dict(cp.os.environ, env, clear=True), \
             patch.object(cp.httpx, "AsyncClient", return_value=manager) as factory:
            self.assertEqual(await cp._request("demo", [], "deepseek-flash"), {"claims": []})
        factory.assert_called_once_with(timeout=25)
        client.post.assert_awaited_once()
        self.assertEqual(client.post.call_args.args[0], "https://api.teamorouter.com/v1/messages")
        request = client.post.call_args.kwargs
        self.assertEqual(request["json"]["model"], "deepseek-flash")
        self.assertEqual(request["json"]["max_tokens"], 1000)
        self.assertEqual(request["json"]["thinking"], {"type": "disabled"})
        self.assertNotIn("tools", request["json"])
        self.assertEqual(request["headers"]["x-api-key"], "unit-test-only")
        self.assertEqual(request["headers"]["anthropic-version"], "2023-06-01")
