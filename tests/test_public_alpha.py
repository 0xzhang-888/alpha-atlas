"""Anonymous access must publish only sanitized saved research, never private state."""
import asyncio
import copy
import hashlib
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer

from src.alpha_api import PublicProjection, PublicSnapshotCache, _public_secrets, create_api, read_snapshot
from src.storage import Storage


# Intentionally assemble synthetic credential shapes at runtime so source exports
# contain no contiguous strings that resemble deployed secrets or routing IDs.
SYNTHETIC_API_KEY = "sk-" + "teamo-" + "disposable0123456789"
SYNTHETIC_BOT_TOKEN = "123456789" + ":" + "disposable_" + "abcdefghijklmnopqrstuvwxyz"
SYNTHETIC_SOURCE_ID = "-100" + "123456789"
SYNTHETIC_TARGET_ID = "-100" + "987654321"


class PublicProjectionTests(unittest.TestCase):
    def fixture(self):
        return {
            "source": "railway-monitor", "scope": "private", "schemaVersion": 999,
            "generatedAt": "2026-10-15T01:00:00Z", "windowStart": "2026-10-13T01:00:00Z",
            "windowEnd": "2026-10-15T01:00:00Z", "lastEventAt": "2026-10-15T00:59:00Z",
            "totals": {"visible": 1, "observed": 9, "kolNames": ["Private Whale"]},
            "raw_bio": "private source", "records": [{
                "id": "unsafe-id", "handle": "EarlyProject", "name": "Early Project", "category": "project",
                "summary": "Private Whale and @secret_kol noticed this Base project. "
                           + SYNTHETIC_API_KEY + " https://example.com/?sessionid=private "
                           "runtime-only-secret", "chains": ["fake-unknown"],
                "chainEvidence": [
                    {"chain": "base", "relation": "bio", "finding": "Built on Base", "source": "https://x.com/EarlyProject",
                     "confidence": "declared", "observedAt": "2026-10-15T00:00:00Z", "private": "private source"},
                    {"chain": "arc", "relation": "follows_official", "finding": "@secret_kol follows @arc",
                     "source": "https://x.com/secret_kol", "officialAccount": "arc", "officialAccountSource": "https://arc.io", "follower": "secret_kol"}],
                "chainCheck": {"status": "complete", "checkedAt": "2026-10-15T00:00:00Z", "aiChecked": True,
                               "model": "private-model", "signature": "private-signature"},
                "followers": 123, "distinctKols": 2, "distinctKols3m": 2, "signal3m": True,
                "lastEventAt": "2026-10-15T00:59:00Z", "researchStatus": "snapshot", "researchedAt": "2026-10-15T00:00:00Z",
                "website": {"status": "read", "url": "https://example.com", "finding": "A lending project", "raw": "private source"},
                "whitepaper": {"status": "not_found"},
                "officialCa": [{"address": "0x" + "a" * 40, "chain": "Base", "source": "https://x.com/EarlyProject/status/1", "narrative": "Lending", "private": "private source"}],
                "thirdParty": {"finding": "Private Whale wrote about it", "sources": ["https://example.com/report", "https://x.com/secret_kol/status/1"]},
                "catalyst": "Launch", "catalystSources": ["https://example.com/launch"],
                "catalystEvents": [{"date": "2026-10-15", "finding": "Launch", "source": "https://example.com/launch", "message_id": 88}],
                "sources": ["https://docs.example.com/intro", "https://example.com/?X-Amz-Credential=private"],
                "devs": ["PublicBuilder", "secret_kol"], "devProjects": ["PublicDapp"],
                "kolNames": ["Private Whale"], "chat_id": -100123, "message_id": 88,
                "following": ["privateFollowing"], "cache": {"key": "private source"}}]}

    def test_strict_projection_redacts_text_and_preserves_public_research_without_mutation(self):
        raw = self.fixture()
        before = copy.deepcopy(raw)
        result = PublicProjection(["Private Whale", "@secret_kol"], ["runtime-only-secret"]).snapshot(raw)
        self.assertEqual(raw, before)
        self.assertEqual((result["source"], result["scope"], result["schemaVersion"]), ("alpha-atlas-public", "public", 1))
        record = result["records"][0]
        self.assertEqual(record["handle"], "EarlyProject")
        self.assertEqual(record["chains"], ["base", "arc"])
        self.assertEqual(record["followers"], 123)
        self.assertEqual((record["distinctKols"], record["distinctKols3m"], record["signal3m"]), (2, 2, True))
        self.assertEqual(record["officialCa"][0]["address"], "0x" + "a" * 40)
        self.assertEqual(record["website"]["finding"], "A lending project")
        self.assertEqual(record["devs"], ["PublicBuilder"])
        self.assertIsNone(record["chainEvidence"][1]["source"])
        self.assertIn("公开关注关系", record["chainEvidence"][1]["finding"])
        encoded = json.dumps(result, ensure_ascii=False).casefold()
        for secret in ("private whale", "secret_kol", "sk-teamo-", "runtime-only-secret", "private-model", "private-signature",
                       "privatefollowing", "chat_id", "message_id", "officialaccount", "raw_bio", "private source", "x-amz-credential"):
            self.assertNotIn(secret, encoded)

    def test_sensitive_urls_and_credentials_are_rejected_even_if_encoded_or_embedded(self):
        project = PublicProjection(["Private Observer"], ["runtime-secret"])
        unsafe = ["https://example.com/?access%255Ftoken=hello", "https://example.com/#client_secret=hello",
                  "https://example.com/?X-Goog-Signature=hello", "https://example.com/runtime-secret",
                  "https://api.telegram.org/bot" + SYNTHETIC_BOT_TOKEN + "/sendMessage",
                  "https://example.com/Private%20Observer", "https://example.com/a%0d%0ab",
                  "https://example.com/?foo=sk-test-disposable0123456789"]
        for value in unsafe:
            with self.subTest(url=value):
                self.assertIsNone(project.url(value))
                self.assertNotIn(value, project.text("See " + value))
        self.assertEqual(project.url("https://example.com/?chain=base"), "https://example.com/?chain=base")

    def test_public_project_handle_is_retained_even_when_it_also_is_an_observer(self):
        raw = self.fixture()
        raw["records"][0]["handle"] = "secret_kol"
        result = PublicProjection(["@secret_kol"]).snapshot(raw)
        self.assertEqual(result["records"][0]["handle"], "secret_kol")
        self.assertEqual(result["totals"]["visible"], 1)
        self.assertNotIn("kolNames", result["records"][0])

    def test_short_observer_labels_do_not_erase_normal_ai_research(self):
        project = PublicProjection(["AI", "K", "@AB"])
        self.assertEqual(project.text("AI agents build on Base; key K has no specific identity."),
                         "AI agents build on Base; key K has no specific identity.")
        self.assertNotIn("@AI", project.text("@AI mentioned @AB and @K"))
        self.assertIsNone(project.url("https://x.com/AI/status/123"))

    def test_runtime_credentials_and_private_routing_ids_are_hidden_from_prose(self):
        with patch.dict("os.environ", {"SOURCE_CHAT_ID": SYNTHETIC_SOURCE_ID, "TARGET_CHAT_ID": SYNTHETIC_TARGET_ID,
                                       "DEEPSEEK_API_KEY": "arbitrary-runtime-credential"}):
            project = PublicProjection(secrets=_public_secrets("disposable-server-token"))
        output = project.text(f"Channel {SYNTHETIC_SOURCE_ID} to {SYNTHETIC_TARGET_ID} uses arbitrary-runtime-credential")
        for private in (SYNTHETIC_SOURCE_ID, SYNTHETIC_TARGET_ID, "arbitrary-runtime-credential"):
            self.assertNotIn(private, output)

    def test_extensible_chain_identifiers_cannot_publish_credentials(self):
        known_secret = "RuntimeCredential" + "X" * 10
        unknown_key = "sk-" + "a" * 30
        raw = self.fixture()
        raw["records"][0]["chainEvidence"] = [
            {"chain": value, "relation": "bio", "source": "https://x.com/EarlyProject", "finding": "Chain declaration"}
            for value in (known_secret, unknown_key, "Base")]
        result = PublicProjection(secrets=[known_secret]).snapshot(raw)
        self.assertEqual(result["records"][0]["chains"], ["base"])
        self.assertEqual(len(result["records"][0]["chainEvidence"]), 1)
        encoded = json.dumps(result).casefold()
        self.assertNotIn(known_secret.casefold(), encoded)
        self.assertNotIn(unknown_key, encoded)


class PublicApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / "data.db")
        self.web_dir = str(Path(self.temp.name) / "public")
        Path(self.web_dir).mkdir()
        for name, content in (("index.html", "<!doctype html><title>Alpha Atlas</title>"), ("app.js", "void 0;"), ("styles.css", "body{}")):
            (Path(self.web_dir) / name).write_text(content)
        self.storage = Storage(self.path)
        now = int(time.time())
        self.storage.add_event("Private Watcher", "EarlyProject", 10001, now - 10)
        self.storage.add_event("Another Observer", "EarlyProject", 10002, now - 5)
        with sqlite3.connect(self.path) as conn:
            conn.execute("CREATE TABLE handle_meta(handle TEXT PRIMARY KEY,payload TEXT,ts INTEGER)")
            conn.execute("INSERT INTO handle_meta VALUES (?,?,?)", ("earlyproject", json.dumps({"category": "project", "summary": "Private Watcher found a Base lending project"}), now))
        self.token = "server-only-disposable-token-" + "a" * 40
        self.clients = []

    async def asyncTearDown(self):
        for client in self.clients:
            await client.close()
        self.temp.cleanup()

    async def client(self, enabled=True, **kwargs):
        app = create_api(self.path, self.token, public_enabled=enabled, public_dir=self.web_dir, **kwargs)
        client = TestClient(TestServer(app))
        await client.start_server()
        self.clients.append(client)
        return client

    async def test_anonymous_live_projection_and_private_auth_boundary_with_read_only_storage(self):
        salt = b"disposable-test-salt"
        config = json.dumps({"sessionSecret": (b"test-session-secret" * 3).hex(), "passwordSalt": salt.hex(),
                             "passwordHash": hashlib.pbkdf2_hmac("sha256", b"test-password", salt, 600_000).hex(),
                             "origins": ["https://alpha.example"]})
        private_dir = str(Path(__file__).resolve().parents[1] / "alpha-atlas" / "web")
        client = await self.client(dashboard_config=config, dashboard_dir=private_dir)
        before = Path(self.path).read_bytes()
        response = await client.get("/api/public/alpha")
        self.assertEqual(response.status, 200)
        data = await response.json()
        self.assertEqual(data["scope"], "public")
        self.assertEqual(data["records"][0]["distinctKols"], 2)
        self.assertIn("Base lending project", data["records"][0]["summary"])
        self.assertNotIn("Private Watcher", json.dumps(data))
        self.assertNotIn(self.token, json.dumps(data))
        self.assertNotIn("Set-Cookie", response.headers)
        self.assertNotIn("Access-Control-Allow-Origin", response.headers)
        self.assertEqual(Path(self.path).read_bytes(), before)
        self.assertEqual((await client.get("/api/alpha")).status, 401)
        root = await client.get("/", allow_redirects=False)
        self.assertEqual((root.status, root.headers.get("Location")), (303, "/login"))
        self.assertEqual((await client.get("/login")).status, 200)
        self.assertEqual((await client.get("/api/alpha", headers={"Authorization": "Bearer " + self.token})).status, 200)

    async def test_exact_assets_methods_parameters_and_opt_in_flag(self):
        disabled, client = await self.client(False), await self.client()
        for path in ("/showcase/", "/api/public/alpha"):
            self.assertEqual((await disabled.get(path)).status, 404)
        redirect = await client.get("/showcase", allow_redirects=False)
        self.assertEqual(redirect.headers.get("Location"), "/showcase/")
        for path in ("/showcase/", "/showcase/app.js", "/showcase/styles.css"):
            response = await client.get(path)
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
            self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
        for path in ("/showcase/.env", "/showcase/data.js", "/showcase/../.env", "/showcase/index.html", "/showcase/src/bot.py"):
            self.assertEqual((await client.get(path)).status, 404)
        self.assertEqual((await client.get("/api/public/alpha?token=anything")).status, 400)
        for method in ("post", "put", "head"):
            self.assertEqual((await getattr(client, method)("/api/public/alpha")).status, 405)

    async def test_concurrent_reads_share_one_refresh_and_failures_back_off(self):
        client = await self.client()
        actual = read_snapshot
        with patch("src.alpha_api.read_snapshot", wraps=actual) as reader:
            responses = await asyncio.gather(*(client.get("/api/public/alpha") for _ in range(20)))
            self.assertTrue(all(r.status == 200 for r in responses))
            await client.get("/api/public/alpha")
            self.assertEqual(reader.call_count, 1)
        failed = await self.client()
        with patch("src.alpha_api.read_snapshot", side_effect=sqlite3.OperationalError("secret details")) as reader:
            responses = await asyncio.gather(*(failed.get("/api/public/alpha") for _ in range(10)))
            self.assertTrue(all(r.status == 503 for r in responses))
            self.assertEqual(reader.call_count, 1)
            self.assertEqual(await responses[0].json(), {"error": "data_unavailable"})
            self.assertEqual(responses[0].headers["Retry-After"], "5")

    async def test_timeout_keeps_one_worker_in_flight_and_recovers(self):
        entered, release = threading.Event(), threading.Event()
        snapshot = PublicProjectionTests().fixture()
        cache = PublicSnapshotCache(self.path, {self.token})

        def slow_reader(*args, **kwargs):
            entered.set()
            release.wait(3)
            return snapshot

        try:
            with patch("src.alpha_api.read_snapshot", side_effect=slow_reader) as reader, patch("src.alpha_api.PUBLIC_READ_TIMEOUT", 0.02):
                self.assertIsNone(await cache.get())
                self.assertTrue(entered.is_set())
                cache.retry_at = 0
                self.assertIsNone(await cache.get())
                self.assertEqual(reader.call_count, 1)
                self.assertFalse(cache.task.done())
                release.set()
                await cache.task
                result = await cache.get()
                self.assertEqual(result["source"], "alpha-atlas-public")
                self.assertEqual(reader.call_count, 1)
        finally:
            release.set()
            await cache.close(None)


if __name__ == "__main__":
    unittest.main()
