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


def set_session_cookie(response: Response, token: str, secure: bool) -> None:
    # Secure over HTTPS (reverse proxy / Tailscale); plain HTTP on the home network needs it off.
    response.set_cookie(COOKIE, token, max_age=MAX_AGE, path="/", secure=secure, httponly=True, samesite="lax")


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
:root { --paper:#F3F5F2; --surface:#fff; --key:#E4EAE5; --key-down:#C9D6CC; --ink:#17362A; --ink-2:#53685C; --line:#D6DED8;
  --accent:#3F8F4E; --accent-ink:#fff; --danger:#B3412E;
  --display:"Bricolage Grotesque","Helvetica Neue","Arial Nova",Arial,system-ui,sans-serif; color-scheme: light dark; }
@media (prefers-color-scheme: dark) { :root { --paper:#0F1A15; --surface:#16241D; --key:#1D2F26; --key-down:#2E4A3B; --ink:#E3EEE6;
  --ink-2:#9DB3A6; --line:#2A4135; --accent:#6CC17D; --accent-ink:#0C1A12; --danger:#F08A76; } }
* { box-sizing: border-box; }
html, body { height: 100%; }
body { margin:0; display:flex; align-items:center; justify-content:center; background:var(--paper); color:var(--ink);
  font:16px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif; -webkit-tap-highlight-color: transparent;
  user-select:none; -webkit-user-select:none;
  padding: max(24px, env(safe-area-inset-top)) max(16px, env(safe-area-inset-right)) max(24px, env(safe-area-inset-bottom)) max(16px, env(safe-area-inset-left)); }
main { width:100%; max-width:320px; display:flex; flex-direction:column; align-items:center; text-align:center; }
.logo { width:56px; height:56px; border-radius:14px; margin-bottom:14px; }
h1 { font:700 21px/1.2 var(--display); letter-spacing:-.01em; margin:0; }
.msg { min-height:22px; margin:8px 0 0; font-size:14px; color:var(--ink-2); }
.msg.err { color:var(--danger); font-weight:600; }
form { display:flex; flex-direction:column; align-items:center; width:100%; }
.dots { display:flex; gap:18px; margin:22px 0 34px; }
.dots span { width:14px; height:14px; border-radius:50%; border:1.5px solid var(--ink); transition: background .12s, transform .12s; }
.dots span.on { background:var(--ink); }
.dots.err span { border-color:var(--danger); }
.dots.shake { animation: shake .4s; }
@keyframes shake { 20%,60% { transform: translateX(-10px); } 40%,80% { transform: translateX(10px); } }
.pad { display:grid; grid-template-columns: repeat(3, 78px); gap:16px 26px; }
.pad button { width:78px; height:78px; border-radius:50%; border:0; background:var(--key); color:var(--ink); cursor:pointer;
  display:flex; flex-direction:column; align-items:center; justify-content:center; font:inherit; padding:0; touch-action:manipulation; }
.pad button:active, .pad button.down { background:var(--key-down); }
.pad .n { font-size:34px; font-weight:400; line-height:1; }
.pad .l { font-size:9.5px; font-weight:700; letter-spacing:.18em; margin-top:3px; color:var(--ink-2); min-height:11px; }
.pad .fn { background:transparent; font-size:15px; font-weight:600; }
.pad .fn svg { width:28px; height:28px; }
.pad .blank { visibility:hidden; }
:focus-visible { outline:3px solid var(--accent); outline-offset:3px; }
noscript input { width:100%; min-height:48px; margin-top:16px; border-radius:12px; border:1px solid var(--line); background:var(--surface);
  color:var(--ink); font-size:24px; text-align:center; }
noscript button { margin-top:10px; width:100%; min-height:48px; border-radius:12px; border:0; background:var(--accent); color:var(--accent-ink); font-weight:700; }
@media (max-height: 620px) { .pad { grid-template-columns: repeat(3, 66px); gap:12px 22px; } .pad button { width:66px; height:66px; } .dots { margin:16px 0 22px; } }
@media (prefers-reduced-motion: reduce) { .dots.shake { animation:none; } .dots span { transition:none; } }
</style></head>
<body><main>
<img class="logo" src="/icons/icon-192.png" alt="">
<h1>Enter PIN</h1>
{message}
<form method="post" action="/login" id="login">
  <input type="hidden" name="next" value="{next}">
  <input type="hidden" name="pin" id="pinval">
  <div class="dots" id="dots" role="status" aria-live="polite" aria-label="0 of 6 digits entered">
    <span></span><span></span><span></span><span></span><span></span><span></span>
  </div>
  <div class="pad" id="pad">
    <button type="button" data-d="1"><span class="n">1</span><span class="l"></span></button>
    <button type="button" data-d="2"><span class="n">2</span><span class="l">ABC</span></button>
    <button type="button" data-d="3"><span class="n">3</span><span class="l">DEF</span></button>
    <button type="button" data-d="4"><span class="n">4</span><span class="l">GHI</span></button>
    <button type="button" data-d="5"><span class="n">5</span><span class="l">JKL</span></button>
    <button type="button" data-d="6"><span class="n">6</span><span class="l">MNO</span></button>
    <button type="button" data-d="7"><span class="n">7</span><span class="l">PQRS</span></button>
    <button type="button" data-d="8"><span class="n">8</span><span class="l">TUV</span></button>
    <button type="button" data-d="9"><span class="n">9</span><span class="l">WXYZ</span></button>
    <span class="blank"></span>
    <button type="button" data-d="0"><span class="n">0</span><span class="l"></span></button>
    <button type="button" class="fn" id="del" aria-label="Delete">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <path d="M21 5H9l-6 7 6 7h12a1 1 0 0 0 1-1V6a1 1 0 0 0-1-1z"/><path d="M17 9l-6 6M11 9l6 6"/></svg>
    </button>
  </div>
  <noscript>
    <input name="pin" type="password" inputmode="numeric" pattern="[0-9]{6}" maxlength="6" autocomplete="current-password" required>
    <button type="submit">Log in</button>
  </noscript>
</form>
<script>
(() => {
  const form = document.getElementById("login"), val = document.getElementById("pinval");
  const dots = document.getElementById("dots"), marks = [...dots.children];
  let pin = "", busy = false;
  const draw = () => {
    marks.forEach((m, i) => m.classList.toggle("on", i < pin.length));
    dots.setAttribute("aria-label", `${pin.length} of 6 digits entered`);
  };
  const press = d => {
    if (busy || pin.length >= 6) return;
    pin += d; draw();
    if (navigator.vibrate) navigator.vibrate(8);
    if (pin.length === 6) {
      busy = true; val.value = pin;
      setTimeout(() => form.requestSubmit ? form.requestSubmit() : form.submit(), 120);  // let the last dot show
    }
  };
  const del = () => { if (!busy && pin) { pin = pin.slice(0, -1); draw(); } };
  document.getElementById("pad").addEventListener("click", e => {
    const b = e.target.closest("button"); if (!b) return;
    if (b.id === "del") del(); else press(b.dataset.d);
  });
  // Physical keyboard: digits, Backspace, Escape.
  document.addEventListener("keydown", e => {
    if (/^[0-9]$/.test(e.key)) { press(e.key); flash(e.key); }
    else if (e.key === "Backspace") del();
    else if (e.key === "Escape") { if (!busy) { pin = ""; draw(); } }
  });
  const flash = d => {
    const b = document.querySelector(`.pad button[data-d="${d}"]`);
    if (b) { b.classList.add("down"); setTimeout(() => b.classList.remove("down"), 120); }
  };
  // Wrong PIN: shake the dots.
  if (document.querySelector(".msg.err")) {
    dots.classList.add("err", "shake");
    if (navigator.vibrate) navigator.vibrate([40, 40, 40]);
    setTimeout(() => dots.classList.remove("shake"), 450);
  }
})();
</script>
</main></body></html>"""


def login_page(next_url: str = "/", message: str = "", status: int = 200, headers: dict | None = None) -> HTMLResponse:
    msg = (f'<p class="msg err" role="alert">{html.escape(message)}</p>' if message
           else '<p class="msg">Weekly Shop</p>')
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
            set_session_cookie(response, sessions.issue(), request.url.scheme == "https")
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
            set_session_cookie(response, sessions.issue(), request.url.scheme == "https")
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
    async def logout(request: Request):
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(COOKIE, path="/", secure=request.url.scheme == "https", httponly=True, samesite="lax")
        return response
