import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from src import research_tools as rt
from src.storage import Storage


class EvidenceToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.s = Storage(str(Path(self.tmp.name) / "test.db"))

    def http(self, handler):
        real_client = httpx.AsyncClient
        p = patch.object(rt.httpx, "AsyncClient", side_effect=lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
        p.start()
        self.addCleanup(p.stop)

    async def test_non_web_urls_and_embedded_credentials_rejected(self):
        for url in ("file:///etc/passwd", "http://u:p@example.com", "http://localhost:8080", "javascript:alert(1)"):
            self.assertFalse(rt.public_url(url))

    async def test_private_dns_target_rejected(self):
        rows = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 0))]
        with patch.object(socket, "getaddrinfo", return_value=rows):
            with self.assertRaises(ValueError):
                await rt.validate_url("https://some.example/")

    async def test_redirect_revalidated_before_following(self):
        requests = []
        def handler(r):
            requests.append(r)
            return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data"})
        self.http(handler)
        async def validate(url):
            if "169.254" in url:
                raise ValueError("private")
        with patch.object(rt, "validate_url", side_effect=validate):
            r = await rt.read_page("https://public.example/", self.s)
        self.assertFalse(r["read"])
        self.assertEqual(len(requests), 1)

    async def test_page_text_cached_and_scripts_removed(self):
        requests = []
        def handler(r):
            requests.append(r)
            return httpx.Response(200, headers={"content-type": "text/html"}, text=
                "<html><script>secret script</script><p>" + "Payment protocol. " * 20 +
                '</p><a href="/docs">Docs</a></html>')
        self.http(handler)
        with patch.object(rt, "validate_url", AsyncMock()):
            r = await rt.read_page("https://public.example/", self.s)
            again = await rt.read_page("https://public.example/", self.s)
        self.assertTrue(r["read"])
        self.assertNotIn("secret script", r["text"])
        self.assertIn("https://public.example/docs", r["links"])
        self.assertEqual(r, again)
        self.assertEqual(len(requests), 1)

    async def test_channel_profile_avoids_paid_api_and_keeps_zero(self):
        self.s.add_event("A", "Demo", 1, bio="Protocol", followers=0)
        with patch.object(rt.ot, "get_user_info", AsyncMock()) as lookup:
            result = await rt.profile("Demo", self.s)
        self.assertEqual(result["followers"], 0)
        lookup.assert_not_called()

    async def test_market_lookup_excludes_other_contracts(self):
        ca = "0x" + "a" * 40
        self.http(lambda r: httpx.Response(200, json={"pairs": [
            {"baseToken": {"address": ca}, "url": "https://dexscreener.com/eth/right", "priceChange": {"h24": 10}},
            {"baseToken": {"address": "0x" + "b" * 40}, "url": "https://dexscreener.com/eth/wrong"}]}))
        r = await rt.token_market(ca, self.s)
        self.assertEqual(len(r["pairs"]), 1)
        self.assertEqual(r["pairs"][0]["baseToken"]["address"], ca)


if __name__ == "__main__":
    unittest.main()
