"""Password-gated dashboard, independent of the monitor's server-to-server token."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import json
import secrets
import time
from collections import deque
from pathlib import Path

from aiohttp import web

COOKIE = "__Host-alpha-session"
LOGIN_COOKIE = "__Host-alpha-login"
SESSION_SECONDS = 7 * 86400
SECURITY = {
    "Cache-Control": "private, no-store",
    "X-Content-Type-Options": "nosniff",
    # Native form POSTs use Origin:null under no-referrer, even on this same host.
    # Keep real same-origin form origins while withholding cross-site referrers.
    "Referrer-Policy": "same-origin",
    "X-Frame-Options": "DENY",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
    "Strict-Transport-Security": "max-age=31536000",
}


class PublicShowcase:
    """Only these three reviewed assets are published; no directory traversal."""
    def __init__(self, directory):
        self.assets = {}
        for name, content_type in (("index.html", "text/html"), ("app.js", "text/javascript"), ("styles.css", "text/css")):
            self.assets["/showcase/" if name == "index.html" else "/showcase/" + name] = (
                (Path(directory) / name).read_text(), content_type)

    async def redirect(self, request):
        return web.HTTPPermanentRedirect("/showcase/", headers={"X-Content-Type-Options": "nosniff"})

    async def asset(self, request):
        body, content_type = self.assets[request.path]
        headers = {**SECURITY, "Cache-Control": "no-cache" if content_type == "text/html" else "public, max-age=300"}
        return web.Response(text=body, content_type=content_type, headers=headers)

    def install(self, app):
        app.router.add_get("/showcase", self.redirect)
        for path in self.assets:
            app.router.add_get(path, self.asset)


class DashboardAccess:
    def __init__(self, config: str, directory: str):
        try:
            data = json.loads(config)
            self.secret = bytes.fromhex(data["sessionSecret"])
            self.salt = bytes.fromhex(data["passwordSalt"])
            self.digest = bytes.fromhex(data["passwordHash"])
            self.origins = set(data["origins"])
            if len(self.secret) < 32 or len(self.salt) < 16 or len(self.digest) != 32:
                raise ValueError
            if not self.origins or any(not s.startswith("https://") or s.endswith("/") for s in self.origins):
                raise ValueError
        except (KeyError, ValueError, TypeError):
            raise ValueError("invalid dashboard access configuration") from None
        self.attempts = deque()
        self.verify_slots = asyncio.Semaphore(2)
        self.assets = {}
        for name, content_type in (("index.html", "text/html"), ("app.js", "text/javascript"),
                                   ("data.js", "text/javascript"), ("styles.css", "text/css")):
            body = (Path(directory) / name).read_text()
            if name == "index.html":
                body = body.replace('data-app="alpha-atlas"', 'data-app="alpha-atlas" data-password-auth="true"')
                body = body.replace('<span class="avatar">α</span>', '<form action="/logout" method="post"><button class="logout-button" type="submit">退出</button></form>')
            self.assets["/" if name == "index.html" else "/" + name] = (body, content_type)

    def signed(self, purpose: str, seconds: int) -> str:
        value = f"{purpose}.{int(time.time()) + seconds}.{secrets.token_hex(16)}"
        return value + "." + hmac.new(self.secret, value.encode(), hashlib.sha256).hexdigest()

    def valid(self, value: str, purpose: str, max_age: int) -> bool:
        if not value or len(value) > 200:
            return False
        try:
            kind, expires, nonce, signature = value.split(".")
            expires = int(expires)
            now = int(time.time())
            expected = hmac.new(self.secret, f"{kind}.{expires}.{nonce}".encode(), hashlib.sha256).hexdigest()
            return kind == purpose and now < expires <= now + max_age and hmac.compare_digest(signature, expected)
        except (ValueError, TypeError):
            return False

    def authorized(self, request: web.Request) -> bool:
        return self.valid(request.cookies.get(COOKIE, ""), "session", SESSION_SECONDS)

    def same_origin(self, request: web.Request) -> bool:
        return request.headers.get("Origin") in self.origins and request.headers.get("Sec-Fetch-Site") != "cross-site"

    @staticmethod
    def cookie(response, name, value, max_age):
        response.set_cookie(name, value, max_age=max_age, secure=True, httponly=True, samesite="Strict", path="/")

    def login_page(self, message="", status=200):
        csrf = self.signed("login", 600)
        body = f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#0d1012"><meta name="robots" content="noindex,nofollow">
<title>Alpha Atlas · 登录</title><style>
*{{box-sizing:border-box}}body{{margin:0;min-height:100dvh;display:grid;place-items:center;background:#0d1012;color:#f0f2ed;font:16px/1.7 -apple-system,BlinkMacSystemFont,'PingFang SC',sans-serif;padding:24px}}
main{{width:min(100%,420px);padding:32px;border:1px solid #30382d;border-radius:16px;background:#15191c}}
.mark{{display:grid;place-items:center;width:44px;height:44px;background:#bcf582;color:#152010;font-size:26px;font-weight:700;border-radius:10px;margin-bottom:24px}}
h1{{font-size:24px;line-height:1.4;margin:0 0 12px;overflow-wrap:anywhere}}p{{color:#a6b0aa;margin:0 0 28px}}label{{display:block;margin-bottom:8px}}input,button{{font:inherit;width:100%;min-height:48px;border-radius:8px}}input{{background:#0d1012;color:#fff;padding:10px 12px;border:1px solid #465141}}button{{background:#bcf582;color:#162010;border:0;font-weight:600;margin-top:18px;cursor:pointer}}:focus-visible{{outline:2px solid #bcf582;outline-offset:4px}}.error{{color:#ffb5a7;margin:12px 0 0}}small{{display:block;color:#949f99;margin-top:24px}}@media(max-width:380px){{main{{padding:24px}}h1{{font-size:22px}}}}
</style></head><body><main><div class="mark">A·</div><h1>Alpha Atlas</h1><p>私人早期项目观察站</p>
<form action="/login" method="post"><input type="hidden" name="csrf" value="{html.escape(csrf)}">
<label for="password">访问密码</label><input id="password" name="password" type="password" autocomplete="current-password" required maxlength="256" autofocus>
<button type="submit">进入观察站</button></form>{f'<p class="error" role="alert">{html.escape(message)}</p>' if message else ''}
<small>仅供自己查看 · 登录有效期 7 天<br>请勿分享访问密码。</small></main></body></html>'''
        response = web.Response(text=body, status=status, content_type="text/html", headers=SECURITY)
        self.cookie(response, LOGIN_COOKIE, csrf, 600)
        return response

    async def login_get(self, request):
        if self.authorized(request):
            return web.HTTPSeeOther("/", headers=SECURITY)
        return self.login_page()

    async def login_post(self, request):
        if not self.same_origin(request):
            return self.login_page("登录页面来源校验未通过，请刷新此页后重新输入密码。", 403)
        now = time.monotonic()
        while self.attempts and self.attempts[0] < now - 60:
            self.attempts.popleft()
        if len(self.attempts) >= 10:
            response = self.login_page("尝试次数较多，请 1 分钟后重试。", 429)
            response.headers["Retry-After"] = "60"
            return response
        self.attempts.append(now)
        if request.content_type != "application/x-www-form-urlencoded":
            return web.Response(status=400, headers=SECURITY)
        form = await request.post()
        csrf = form.get("csrf", "")
        password = form.get("password", "")
        if (not isinstance(csrf, str) or not isinstance(password, str) or len(password) > 256
                or not self.valid(csrf, "login", 600)
                or not hmac.compare_digest(csrf.encode(), request.cookies.get(LOGIN_COOKIE, "").encode())):
            return self.login_page("登录页已失效，请重新输入密码。", 403)
        async with self.verify_slots:
            digest = await asyncio.to_thread(hashlib.pbkdf2_hmac, "sha256", password.encode(), self.salt, 600_000)
        if not hmac.compare_digest(digest, self.digest):
            return self.login_page("密码不正确，请重试。", 401)
        response = web.HTTPSeeOther("/", headers=SECURITY)
        self.cookie(response, COOKIE, self.signed("session", SESSION_SECONDS), SESSION_SECONDS)
        self.cookie(response, LOGIN_COOKIE, "", 0)
        return response

    async def logout(self, request):
        if not self.same_origin(request):
            return web.Response(status=403, headers=SECURITY)
        response = web.HTTPSeeOther("/login", headers=SECURITY)
        self.cookie(response, COOKIE, "", 0)
        return response

    async def asset(self, request):
        if not self.authorized(request):
            return web.HTTPSeeOther("/login", headers=SECURITY)
        body, content_type = self.assets[request.path]
        return web.Response(text=body, content_type=content_type, headers=SECURITY)

    def install(self, app: web.Application):
        app.router.add_get("/login", self.login_get)
        app.router.add_post("/login", self.login_post)
        app.router.add_post("/logout", self.logout)
        for path in self.assets:
            app.router.add_get(path, self.asset)
