"""Local-only regression tests for the password-gated dashboard.

TestServer uses HTTP, so Secure cookies are sent explicitly in test requests.
All credentials and database records below are disposable test fixtures.
"""
import hashlib
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer

from src.alpha_api import create_api
from src.dashboard_access import COOKIE, LOGIN_COOKIE, SESSION_SECONDS, DashboardAccess
from src.storage import Storage


WEB_DIR = str(Path(__file__).resolve().parents[1] / "alpha-atlas" / "web")
ORIGIN = "https://alpha.example"
PASSWORD = "test-only-password-123"


class DashboardAccessTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = json.dumps({
            "sessionSecret": (b"test-session-secret-not-production" * 2).hex(),
            "passwordSalt": b"test-password-salt".hex(),
            "passwordHash": hashlib.pbkdf2_hmac(
                "sha256", PASSWORD.encode(), b"test-password-salt", 600_000
            ).hex(),
            "origins": [ORIGIN],
        })

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp.name) / "dashboard-test.db")
        Storage(self.db_path)
        self.token = "test-only-server-token-" + "a" * 40
        self.access = DashboardAccess(self.config, WEB_DIR)
        with patch("src.dashboard_access.DashboardAccess", return_value=self.access):
            app = create_api(self.db_path, self.token,
                             dashboard_config=self.config, dashboard_dir=WEB_DIR)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.temp.cleanup()

    async def login_csrf(self):
        response = await self.client.get("/login", allow_redirects=False)
        self.assertEqual(response.status, 200)
        csrf = response.cookies[LOGIN_COOKIE].value
        self.assertIn(csrf, await response.text())
        return csrf

    async def login(self, csrf, password=PASSWORD, *, cookie=None, extra_headers=None):
        headers = {"Origin": ORIGIN, "Sec-Fetch-Site": "same-origin",
                   "Cookie": f"{LOGIN_COOKIE}={csrf if cookie is None else cookie}"}
        headers.update(extra_headers or {})
        return await self.client.post("/login", data={"csrf": csrf, "password": password},
                                      headers=headers, allow_redirects=False)

    def session_headers(self):
        return {"Cookie": f"{COOKIE}={self.access.signed('session', SESSION_SECONDS)}"}

    def assert_secure_cookie(self, cookie, max_age):
        self.assertTrue(cookie["secure"])
        self.assertTrue(cookie["httponly"])
        self.assertEqual(cookie["samesite"], "Strict")
        self.assertEqual(cookie["path"], "/")
        self.assertEqual(cookie["domain"], "")
        self.assertEqual(cookie["max-age"], str(max_age))

    async def test_login_page_cookie_security_and_no_secrets(self):
        response = await self.client.get("/login", allow_redirects=False)
        self.assertEqual(response.status, 200)
        self.assert_secure_cookie(response.cookies[LOGIN_COOKIE], 600)
        self.assertNotIn(COOKIE, response.cookies)
        self.assertIn("no-store", response.headers["Cache-Control"])
        # no-referrer turns a browser's native form POST Origin into null.
        self.assertEqual(response.headers["Referrer-Policy"], "same-origin")
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
        body = await response.text()
        self.assertNotIn(self.token, body)
        self.assertNotIn(PASSWORD, body)
        self.assertNotIn(json.loads(self.config)["sessionSecret"], body)

    async def test_successful_login_secure_session_and_cookie_api(self):
        response = await self.login(await self.login_csrf())
        self.assertEqual(response.status, 303)
        self.assertEqual(response.headers["Location"], "/")
        self.assert_secure_cookie(response.cookies[COOKIE], SESSION_SECONDS)
        self.assert_secure_cookie(response.cookies[LOGIN_COOKIE], 0)
        session = response.cookies[COOKIE].value
        headers = {"Cookie": f"{COOKIE}={session}"}
        page = await self.client.get("/", headers=headers, allow_redirects=False)
        self.assertEqual(page.status, 200)
        self.assertEqual(page.headers["Referrer-Policy"], "same-origin")
        self.assertIn('data-password-auth="true"', await page.text())
        api = await self.client.get("/api/alpha", headers=headers)
        self.assertEqual(api.status, 200)
        self.assertEqual((await api.json())["records"], [])
        login_page = await self.client.get("/login", headers=headers, allow_redirects=False)
        self.assertEqual(login_page.status, 303)

    async def test_api_bearer_remains_compatible_but_does_not_unlock_assets(self):
        valid = {"Authorization": f"Bearer {self.token}"}
        response = await self.client.get("/api/alpha", headers=valid)
        self.assertEqual(response.status, 200)
        response = await self.client.get("/api/alpha", headers={"Authorization": "Bearer wrong"})
        self.assertEqual(response.status, 401)
        response = await self.client.get("/", headers=valid, allow_redirects=False)
        self.assertEqual(response.status, 303)
        self.assertEqual(response.headers["Location"], "/login")
        response = await self.client.get("/api/alpha?token=ignored", headers=valid)
        self.assertEqual(response.status, 400)

    async def test_all_four_assets_are_protected_including_head(self):
        for path in ("/", "/app.js", "/data.js", "/styles.css"):
            for method in ("get", "head"):
                with self.subTest(path=path, method=method):
                    response = await getattr(self.client, method)(path, allow_redirects=False)
                    self.assertEqual(response.status, 303)
                    self.assertEqual(response.headers["Location"], "/login")
            response = await self.client.get(path, headers=self.session_headers())
            self.assertEqual(response.status, 200)
            body = await response.text()
            self.assertNotIn(self.token, body)
            self.assertNotIn(json.loads(self.config)["sessionSecret"], body)

    async def test_static_route_allowlist_and_method_restrictions(self):
        headers = self.session_headers()
        for path in ("/index.html", "/.env", "/src/bot.py", "/Dockerfile",
                     "/app.js.map", "/../src/bot.py", "/%2e%2e/.env", "/web/app.js"):
            with self.subTest(path=path):
                response = await self.client.get(path, headers=headers, allow_redirects=False)
                self.assertEqual(response.status, 404)
        for method, path in (("post", "/app.js"), ("post", "/api/alpha"),
                             ("get", "/logout"), ("head", "/api/alpha")):
            response = await getattr(self.client, method)(path, headers=headers, allow_redirects=False)
            self.assertEqual(response.status, 405)

    async def test_missing_expired_tampered_or_wrong_purpose_session_is_rejected(self):
        valid = self.access.signed("session", SESSION_SECONDS)
        invalid = ("", "malformed", valid[:-1] + ("0" if valid[-1] != "0" else "1"),
                   self.access.signed("session", -1), self.access.signed("login", 600),
                   self.access.signed("session", SESSION_SECONDS + 60), "x" * 201)
        for value in invalid:
            with self.subTest(value_kind=value.split(".")[0][:20]):
                response = await self.client.get("/api/alpha", headers={"Cookie": f"{COOKIE}={value}"})
                self.assertEqual(response.status, 401)

    async def test_wrong_password_never_issues_session(self):
        response = await self.login(await self.login_csrf(), password="incorrect-test-password")
        self.assertEqual(response.status, 401)
        self.assertNotIn(COOKIE, response.cookies)
        self.assert_secure_cookie(response.cookies[LOGIN_COOKIE], 600)

    async def test_login_requires_exact_origin_and_rejects_cross_site(self):
        csrf = await self.login_csrf()
        for origin in ("", "null", "https://attacker.example", ORIGIN + ".attacker.example"):
            with self.subTest(origin=origin):
                response = await self.login(csrf, extra_headers={"Origin": origin})
                self.assertEqual(response.status, 403)
                self.assertNotIn(COOKIE, response.cookies)
                self.assertEqual(response.headers["Referrer-Policy"], "same-origin")
                self.assertIn("请刷新此页", await response.text())
        response = await self.login(csrf, extra_headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(response.status, 403)
        self.assertEqual(len(self.access.attempts), 0)

    async def test_csrf_requires_matching_valid_login_cookie_before_hashing(self):
        csrf = await self.login_csrf()
        for token, cookie in ((csrf, ""), (csrf, self.access.signed("login", 600)),
                              ("invalid", "invalid"),
                              (self.access.signed("session", 600), csrf)):
            with patch("src.dashboard_access.hashlib.pbkdf2_hmac") as derive:
                response = await self.login(token, cookie=cookie)
                self.assertEqual(response.status, 403)
                derive.assert_not_called()
        expired = self.access.signed("login", -1)
        response = await self.login(expired, cookie=expired)
        self.assertEqual(response.status, 403)

    async def test_non_ascii_login_cookie_is_rejected_without_server_error(self):
        csrf = await self.login_csrf()
        # SimpleCookie decodes the ASCII octal escape into a non-ASCII character.
        response = await self.login(csrf, cookie='"\\351"')
        self.assertEqual(response.status, 403)
        self.assertNotIn(COOKIE, response.cookies)

    async def test_login_rate_limit_and_window_recovery(self):
        csrf = await self.login_csrf()
        with patch("src.dashboard_access.hashlib.pbkdf2_hmac", return_value=b"\0" * 32) as derive:
            for _ in range(10):
                response = await self.login(csrf, password="incorrect")
                self.assertEqual(response.status, 401)
            response = await self.login(csrf)
            self.assertEqual(response.status, 429)
            self.assertEqual(response.headers["Retry-After"], "60")
            self.assertNotIn(COOKIE, response.cookies)
            self.assertEqual(derive.call_count, 10)
        self.access.attempts.clear()
        self.access.attempts.extend([time.monotonic() - 61] * 10)
        response = await self.login(csrf)
        self.assertEqual(response.status, 303)

    async def test_invalid_content_type_and_oversized_password_skip_hashing(self):
        csrf = await self.login_csrf()
        with patch("src.dashboard_access.hashlib.pbkdf2_hmac") as derive:
            response = await self.client.post("/login", json={"csrf": csrf, "password": PASSWORD},
                                              headers={"Origin": ORIGIN}, allow_redirects=False)
            self.assertEqual(response.status, 400)
            response = await self.login(csrf, password="a" * 257)
            self.assertEqual(response.status, 403)
            response = await self.login(csrf, password="a" * 5000)
            self.assertEqual(response.status, 413)
            derive.assert_not_called()

    async def test_logout_requires_same_origin_and_clears_secure_cookie(self):
        headers = self.session_headers()
        response = await self.client.post("/logout", headers=headers, allow_redirects=False)
        self.assertEqual(response.status, 403)
        self.assertNotIn(COOKIE, response.cookies)
        headers["Origin"] = ORIGIN
        headers["Sec-Fetch-Site"] = "cross-site"
        response = await self.client.post("/logout", headers=headers, allow_redirects=False)
        self.assertEqual(response.status, 403)
        headers["Sec-Fetch-Site"] = "same-origin"
        response = await self.client.post("/logout", headers=headers, allow_redirects=False)
        self.assertEqual(response.status, 303)
        self.assertEqual(response.headers["Location"], "/login")
        self.assert_secure_cookie(response.cookies[COOKIE], 0)
        self.assertEqual(response.cookies[COOKIE].value, "")


if __name__ == "__main__":
    unittest.main()
