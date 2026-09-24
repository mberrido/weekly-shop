"""Household-password auth: scrypt hash, signed session cookie, login lockout.

The real client IP comes from `request.client.host`, which uvicorn rewrites
from X-Forwarded-For only when the connection comes from an address in
--forwarded-allow-ips (the Docker bridge gateway / localhost). It takes the
rightmost untrusted hop, so a client can't spoof its way past the lockout.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import logging
import secrets
import time
from collections import deque
from urllib.parse import parse_qs, quote

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from itsdangerous import BadSignature, URLSafeTimedSerializer

log = logging.getLogger("weekly_shop.auth")

COOKIE = "ws_session"
MAX_AGE = 90 * 24 * 3600
REFRESH_AFTER = 24 * 3600            # re-issue the cookie daily so active use keeps it alive
MAX_FAILURES = 5
WINDOW = 15 * 60

PUBLIC_PATHS = {"/login", "/logout", "/healthz", "/manifest.webmanifest", "/sw.js", "/favicon.ico"}
PUBLIC_PREFIXES = ("/icons/",)

SECURITY_HEADERS = {
    "Strict-Transport-Security": "max-age=31536000",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "frame-ancestors 'none'; base-uri 'self'; object-src 'none'; form-action 'self'",
}


# -- password hashing ----------------------------------------------------------

_SCRYPT = {"n": 2**14, "r": 8, "p": 1}


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, dklen=32, **_SCRYPT)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt_b64, digest_b64 = stored.split("$")
        salt, expected = base64.b64decode(salt_b64), base64.b64decode(digest_b64)
    except ValueError:
        return False
    digest = hashlib.scrypt(password.encode(), salt=salt, dklen=len(expected), **_SCRYPT)
    return hmac.compare_digest(digest, expected)


# -- rate limiting -------------------------------------------------------------

class LoginLimiter:
    """5 failed attempts per IP per 15 minutes, then locked out until the oldest expires."""

    def __init__(self, max_failures: int = MAX_FAILURES, window: int = WINDOW, clock=time.monotonic):
        self.max_failures = max_failures
        self.window = window
        self.clock = clock
        self._fails: dict[str, deque[float]] = {}

    def _prune(self, ip: str) -> deque[float]:
        now = self.clock()
        q = self._fails.get(ip, deque())
        while q and now - q[0] >= self.window:
            q.popleft()
        if q:
            self._fails[ip] = q
        else:
            self._fails.pop(ip, None)
        return q

    def retry_after(self, ip: str) -> int:
        """Seconds until this IP may try again (0 = allowed)."""
        q = self._prune(ip)
        if len(q) < self.max_failures:
            return 0
        return max(1, int(q[0] + self.window - self.clock()) + 1)

    def remaining(self, ip: str) -> int:
        return max(0, self.max_failures - len(self._prune(ip)))

    def fail(self, ip: str) -> None:
        if len(self._fails) > 10_000:  # bound memory under a spray of IPs
            for key in list(self._fails)[:5_000]:
                self._prune(key)
        self._prune(ip)
        self._fails.setdefault(ip, deque()).append(self.clock())

    def reset(self, ip: str) -> None:
        self._fails.pop(ip, None)


# -- sessions ------------------------------------------------------------------

class Sessions:
    def __init__(self, password: str, secret: str):
        self._serializer = URLSafeTimedSerializer(secret, salt="weekly-shop.session")
        # Ties sessions to the current PIN: changing APP_PIN logs everyone out.
        self._fingerprint = hmac.new(secret.encode(), b"pw:" + password.encode(), hashlib.sha256).hexdigest()[:24]

    def issue(self) -> str:
        return self._serializer.dumps({"pw": self._fingerprint})

    def age(self, token: str | None) -> float | None:
        """Age in seconds of a valid token, else None."""
        if not token:
            return None
        try:
            data, ts = self._serializer.loads(token, max_age=MAX_AGE, return_timestamp=True)
        except BadSignature:
            return None
        if not isinstance(data, dict) or not hmac.compare_digest(str(data.get("pw", "")), self._fingerprint):
            return None
        return time.time() - ts.timestamp()


def set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(COOKIE, token, max_age=MAX_AGE, path="/", secure=True, httponly=True, samesite="lax")


def _safe_next(target: str | None) -> str:
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return "/"


def is_public(path: str) -> bool:
    return path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES)


# -- login page ----------------------------------------------------------------

LOGIN_PAGE = """<!doctype html>
<html lang="en-GB"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Log in · Weekly Shop</title>
<meta name="theme-color" content="#F3F5F2" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#0F1A15" media="(prefers-color-scheme: dark)">
<link rel="manifest" href="/manifest.webmanifest">
<link rel="icon" href="/icons/favicon.svg" type="image/svg+xml">
<link rel="apple-touch-icon" href="/icons/apple-touch-icon.png">
<style>
:root { --paper:#F3F5F2; --surface:#fff; --ink:#17362A; --ink-2:#53685C; --line:#D6DED8; --accent:#3F8F4E; --accent-ink:#fff; --warn:#F7EBC4;
  --display:"Bricolage Grotesque","Helvetica Neue","Arial Nova",Arial,system-ui,sans-serif; color-scheme: light dark; }
@media (prefers-color-scheme: dark) { :root { --paper:#0F1A15; --surface:#16241D; --ink:#E3EEE6; --ink-2:#9DB3A6; --line:#2A4135; --accent:#6CC17D; --accent-ink:#0C1A12; --warn:#3A3218; } }
* { box-sizing: border-box; }
body { margin:0; min-height:100vh; display:grid; place-items:center; background:var(--paper); color:var(--ink);
  font:16px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif;
  padding: max(24px, env(safe-area-inset-top)) max(16px, env(safe-area-inset-right)) max(24px, env(safe-area-inset-bottom)) max(16px, env(safe-area-inset-left)); }
main { width:100%; max-width:360px; }
img { width:64px; height:64px; border-radius:16px; display:block; margin-bottom:20px; }
h1 { font:800 34px/1.05 var(--display); letter-spacing:-.03em; margin:0 0 6px; }
p { color:var(--ink-2); margin:0 0 24px; }
label { display:block; font-weight:700; font-size:14px; margin-bottom:6px; }
input { width:100%; min-height:48px; border-radius:12px; border:1px solid var(--line); background:var(--surface); color:var(--ink); padding:10px 14px; font:inherit; font-size:17px; }
button { margin-top:14px; width:100%; min-height:48px; border-radius:12px; border:0; background:var(--accent); color:var(--accent-ink); font:inherit; font-weight:700; cursor:pointer; }
:focus-visible { outline:3px solid var(--accent); outline-offset:2px; }
.msg { background:var(--warn); color:var(--ink); border-radius:10px; padding:10px 12px; margin:0 0 16px; }
input.pin { text-align:center; font:700 32px/1 ui-monospace,SFMono-Regular,Menlo,monospace; letter-spacing:.45em;
  padding:12px 0 12px .45em; min-height:64px; }
input.pin::placeholder { color:var(--line); }
</style></head>
<body><main>
<img src="/icons/icon-192.png" alt="">
<h1>Weekly Shop</h1>
<p>Enter the household PIN.</p>
{message}
<form method="post" action="/login" id="login">
  <input type="hidden" name="next" value="{next}">
  <input type="text" name="username" value="household" autocomplete="username" hidden>
  <label for="pin">6-digit PIN</label>
  <input id="pin" name="pin" class="pin" type="password" inputmode="numeric" pattern="[0-9]{6}" minlength="6" maxlength="6"
         placeholder="••••••" autocomplete="current-password" required autofocus>
  <button type="submit">Log in</button>
</form>
<script>
  // Log in as soon as the 6th digit is typed.
  const pin = document.getElementById("pin"), form = document.getElementById("login");
  let sent = false;
  pin.addEventListener("input", () => {
    pin.value = pin.value.replace(/[^0-9]/g, "").slice(0, 6);
    if (pin.value.length === 6 && !sent) { sent = true; form.requestSubmit ? form.requestSubmit() : form.submit(); }
  });
</script>
</main></body></html>"""


def login_page(next_url: str = "/", message: str = "", status: int = 200, headers: dict | None = None) -> HTMLResponse:
    msg = f'<p class="msg" role="alert">{html.escape(message)}</p>' if message else ""
    body = LOGIN_PAGE.replace("{message}", msg).replace("{next}", html.escape(_safe_next(next_url), quote=True))
    return HTMLResponse(body, status_code=status, headers={"Cache-Control": "no-store", **(headers or {})})


# -- wiring --------------------------------------------------------------------

def install(app: FastAPI, password: str, secret: str, limiter: LoginLimiter | None = None) -> None:
    stored_hash = hash_password(password)
    sessions = Sessions(password, secret)
    limiter = limiter or LoginLimiter()
    app.state.login_limiter = limiter

    @app.middleware("http")
    async def require_session(request: Request, call_next):
        path = request.url.path
        refresh = False
        if is_public(path):
            response = await call_next(request)
        else:
            age = sessions.age(request.cookies.get(COOKIE))
            if age is None:
                if path.startswith("/api/"):
                    response = JSONResponse({"detail": "Not logged in"}, status_code=401)
                elif request.method in ("GET", "HEAD"):
                    dest = "/login" if path == "/" else f"/login?next={quote(path)}"
                    response = RedirectResponse(dest, status_code=303)
                else:
                    response = JSONResponse({"detail": "Not logged in"}, status_code=401)
            else:
                response = await call_next(request)
                refresh = age > REFRESH_AFTER
        if refresh:
            set_session_cookie(response, sessions.issue())
        for k, v in SECURITY_HEADERS.items():
            response.headers.setdefault(k, v)
        return response

    @app.get("/login", include_in_schema=False)
    async def login_form(request: Request, next: str = "/"):
        if sessions.age(request.cookies.get(COOKIE)) is not None:
            return RedirectResponse(_safe_next(next), status_code=303)
        return login_page(next)

    @app.post("/login", include_in_schema=False)
    async def login(request: Request):
        ip = request.client.host if request.client else "unknown"
        raw = (await request.body())[:4096]
        if request.headers.get("content-type", "").startswith("application/json"):
            import json
            try:
                data = json.loads(raw or b"{}")
            except ValueError:
                data = {}
            password_in, next_url = str(data.get("pin") or data.get("password") or ""), str(data.get("next", "/"))
        else:
            form = parse_qs(raw.decode("utf-8", "replace"))
            password_in = (form.get("pin") or form.get("password") or [""])[0]
            next_url = form.get("next", ["/"])[0]

        wait = limiter.retry_after(ip)
        if wait:
            log.warning("Login blocked for %s (locked out, %ss left)", ip, wait)
            return login_page(next_url, f"Too many attempts. Try again in {max(1, round(wait / 60))} min.",
                              status=429, headers={"Retry-After": str(wait)})

        password_in = password_in.strip()
        if password_in and await run_in_threadpool(verify_password, password_in, stored_hash):
            limiter.reset(ip)
            log.info("Login from %s", ip)
            response = RedirectResponse(_safe_next(next_url), status_code=303)
            set_session_cookie(response, sessions.issue())
            return response

        limiter.fail(ip)
        left = limiter.remaining(ip)
        log.warning("Failed login from %s (%d attempts left)", ip, left)
        if left == 0:
            wait = limiter.retry_after(ip)
            return login_page(next_url, f"Too many attempts. Try again in {max(1, round(wait / 60))} min.",
                              status=429, headers={"Retry-After": str(wait)})
        return login_page(next_url, f"Wrong PIN. {left} attempt{'s' if left != 1 else ''} left.", status=401)

    @app.post("/logout", include_in_schema=False)
    async def logout():
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(COOKIE, path="/", secure=True, httponly=True, samesite="lax")
        return response
