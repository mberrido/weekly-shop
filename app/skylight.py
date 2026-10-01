"""Send a week's dinners to Skylight's Meal Planner.

Skylight has no public API. This talks to the private app API at app.ourskylight.com
the way the community clients do (pyskylight, skylight-mcp): sign in through the
OAuth2 + PKCE web login, then JSON:API calls with a Bearer token. It's unofficial and
can break if Skylight changes its app; every failure becomes a SkylightError (-> 502)
and the rest of Weekly Shop carries on.

Sending a week replaces that week's dinners on the Skylight with Weekly Shop's
(the household never adds meals on the Skylight itself). Meals are Skylight
"recipes" (name only), created once and reused by name.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import http.cookiejar
import json
import logging
import os
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

BASE_URL = "https://app.ourskylight.com"
CLIENT_ID = "skylight-mobile"
REDIRECT_URI = "skylight-family://welcome"
SCOPE = "everything"
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
TIMEOUT = 20
_CSRF = re.compile(r'name="authenticity_token"[^>]*value="([^"]+)"')


class SkylightError(Exception):
    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.message = message
        self.status = status


@dataclass
class Response:
    status: int
    headers: dict[str, str]
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body or b"{}")


# (method, url, headers, body) -> Response, never following redirects. Swappable in tests.
Transport = Callable[[str, str, dict[str, str], bytes | None], Response]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # hand 3xx back to the caller
        return None


def urllib_transport() -> Transport:
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()), _NoRedirect())

    def send(method: str, url: str, headers: dict[str, str], body: bytes | None) -> Response:
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with opener.open(req, timeout=TIMEOUT) as r:
                return Response(r.status, {k.lower(): v for k, v in r.headers.items()}, r.read())
        except urllib.error.HTTPError as e:   # includes 3xx, since redirects aren't followed
            return Response(e.code, {k.lower(): v for k, v in e.headers.items()}, e.read())

    return send


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


class SkylightService:
    def __init__(self, email: str, password: str, token_path: str | Path,
                 frame_id: str = "", transport_factory: Callable[[], Transport] = urllib_transport):
        self._email = (email or "").strip()
        self._password = password or ""
        self._token_path = Path(token_path)
        self._frame_id = (frame_id or "").strip()
        self._transport_factory = transport_factory
        self._token: dict | None = None
        self._lock = asyncio.Lock()

    @property
    def configured(self) -> bool:
        return bool(self._email and self._password)

    # -- auth -------------------------------------------------------------

    def _login(self, send: Transport) -> dict:
        verifier = _b64url(secrets.token_bytes(32))
        challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
        state = _b64url(secrets.token_bytes(18))
        query = urllib.parse.urlencode({
            "response_type": "code", "client_id": CLIENT_ID, "redirect_uri": REDIRECT_URI,
            "scope": SCOPE, "state": state, "code_challenge": challenge,
            "code_challenge_method": "S256", "prompt": "login"})
        ua = {"User-Agent": BROWSER_UA}

        def follow(resp: Response, hops: int = 10) -> tuple[Response, str | None]:
            loc = resp.headers.get("location")
            while resp.status in (301, 302, 303, 307, 308) and loc and not loc.startswith("skylight-family:") and hops:
                resp = send("GET", loc if loc.startswith("http") else BASE_URL + loc, ua, None)
                loc = resp.headers.get("location")
                hops -= 1
            return resp, loc

        page, _ = follow(send("GET", f"{BASE_URL}/oauth/authorize?{query}", ua, None))
        csrf = _CSRF.search(page.body.decode("utf-8", "replace"))
        if not csrf:
            raise SkylightError("Couldn't load Skylight's sign-in page.")
        form = urllib.parse.urlencode({"authenticity_token": csrf.group(1),
                                       "email": self._email, "password": self._password}).encode()
        resp = send("POST", f"{BASE_URL}/auth/session",
                    {**ua, "Content-Type": "application/x-www-form-urlencoded"}, form)
        _, loc = follow(resp)
        if not (loc and loc.startswith("skylight-family:")):
            raise SkylightError("Skylight didn't accept the email or password in .env.", status=502)
        q = urllib.parse.parse_qs(urllib.parse.urlparse(loc).query)
        if (q.get("state") or [""])[0] != state or not (q.get("code") or [""])[0]:
            raise SkylightError("Skylight sign-in returned something unexpected.")
        return self._token_request(send, {
            "grant_type": "authorization_code", "client_id": CLIENT_ID, "code": q["code"][0],
            "redirect_uri": REDIRECT_URI, "code_verifier": verifier})

    def _token_request(self, send: Transport, fields: dict) -> dict:
        resp = send("POST", f"{BASE_URL}/oauth/token",
                    {"User-Agent": BROWSER_UA, "Content-Type": "application/x-www-form-urlencoded",
                     "Accept": "application/json"}, urllib.parse.urlencode(fields).encode())
        if resp.status >= 400:
            raise SkylightError(f"Skylight sign-in failed (HTTP {resp.status}).")
        data = resp.json()
        if not data.get("access_token"):
            raise SkylightError("Skylight sign-in didn't return an access token.")
        expires = None
        if isinstance(data.get("created_at"), (int, float)) and isinstance(data.get("expires_in"), (int, float)):
            expires = float(data["created_at"]) + float(data["expires_in"])
        token = {"access_token": data["access_token"], "refresh_token": data.get("refresh_token"),
                 "expires_at": expires}
        self._save_token(token)
        return token

    def _save_token(self, token: dict) -> None:
        self._token = token
        try:
            self._token_path.write_text(json.dumps(token))
            os.chmod(self._token_path, 0o600)
        except OSError as e:
            log.warning("Couldn't save the Skylight token: %s", type(e).__name__)

    def _ensure_token(self, send: Transport) -> dict:
        if self._token is None and self._token_path.exists():
            try:
                self._token = json.loads(self._token_path.read_text())
            except (OSError, ValueError):
                self._token = None
        tok = self._token
        if tok and (tok.get("expires_at") is None or time.time() < tok["expires_at"] - 60):
            return tok
        if tok and tok.get("refresh_token"):
            try:
                return self._token_request(send, {"grant_type": "refresh_token", "client_id": CLIENT_ID,
                                                  "refresh_token": tok["refresh_token"]})
            except SkylightError:
                pass   # fall through to a fresh sign-in
        return self._login(send)

    # -- API --------------------------------------------------------------

    def _api(self, send: Transport, method: str, path: str, params: dict | None = None,
             body: dict | None = None, _retry: bool = True) -> Any:
        tok = self._ensure_token(send)
        url = BASE_URL + "/api" + path + ("?" + urllib.parse.urlencode(params) if params else "")
        headers = {"Authorization": "Bearer " + tok["access_token"], "Accept": "application/json",
                   "User-Agent": "WeeklyShop/1.0"}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode()
        resp = send(method, url, headers, data)
        if resp.status == 401 and _retry:      # token revoked: sign in again once
            self._token = None
            self._token_path.unlink(missing_ok=True)
            return self._api(send, method, path, params, body, _retry=False)
        if resp.status == 403:
            raise SkylightError("Skylight refused (the Meal Planner may need Skylight Plus).")
        if resp.status >= 400:
            raise SkylightError(f"Skylight returned an error (HTTP {resp.status}).")
        return resp.json() if resp.body else {}

    def _send_week(self, week_start: date, dinners: list[tuple[date, str]]) -> dict:
        send = self._transport_factory()
        frame = self._frame_id
        if not frame:
            frames = self._api(send, "GET", "/frames").get("data") or []
            if not frames:
                raise SkylightError("No Skylight found on that account.")
            frame = str(frames[0]["id"])
        cats = self._api(send, "GET", f"/frames/{frame}/meals/categories").get("data") or []
        dinner = next((c for c in cats if str((c.get("attributes") or {}).get("label")
                                              or (c.get("attributes") or {}).get("name") or "").lower() == "dinner"), None)
        if not dinner:
            raise SkylightError("Couldn't find Skylight's Dinner meal time.")
        cat_id = str(dinner["id"])

        # Clear this week's dinners on the Skylight.
        week = {(week_start + timedelta(days=i)).isoformat() for i in range(7)}
        end = (week_start + timedelta(days=6)).isoformat()
        sittings = self._api(send, "GET", f"/frames/{frame}/meals/sittings",
                             {"date_min": week_start.isoformat(), "date_max": end}).get("data") or []
        removed = 0
        for s in sittings:
            attrs = s.get("attributes") or {}
            rel = ((s.get("relationships") or {}).get("meal_category") or {}).get("data") or {}
            if str(rel.get("id") or attrs.get("meal_category_id") or "") != cat_id:
                continue
            dates = attrs.get("instances") if isinstance(attrs.get("instances"), list) else [attrs.get("date")]
            for d in dates:
                d = str(d or "")[:10]
                if d in week:
                    self._api(send, "DELETE", f"/frames/{frame}/meals/sittings/{s['id']}/instances/{d}")
                    removed += 1

        # Reuse recipes by name; create the missing ones.
        recipes = self._api(send, "GET", f"/frames/{frame}/meals/recipes").get("data") or []
        by_name = {str((r.get("attributes") or {}).get("summary") or "").strip().lower(): str(r["id"])
                   for r in recipes}
        sent = 0
        for day, name in dinners:
            key = name.strip().lower()
            if key not in by_name:
                created = self._api(send, "POST", f"/frames/{frame}/meals/recipes",
                                    body={"summary": name.strip(), "meal_category_id": cat_id}).get("data") or {}
                if isinstance(created, list):
                    created = created[0] if created else {}
                by_name[key] = str(created["id"])
            self._api(send, "POST", f"/frames/{frame}/meals/sittings",
                      body={"date": day.isoformat(), "meal_category_id": cat_id, "meal_recipe_id": by_name[key]})
            sent += 1
        return {"sent": sent, "removed": removed}

    async def send_week(self, week_start: date, dinners: list[tuple[date, str]]) -> dict:
        if not self.configured:
            raise SkylightError("Skylight isn't set up (SKYLIGHT_EMAIL / SKYLIGHT_PASSWORD).", status=503)
        async with self._lock:
            try:
                return await asyncio.to_thread(self._send_week, week_start, dinners)
            except SkylightError as e:
                log.warning("Skylight: %s", e.message)
                raise
            except Exception as e:
                log.warning("Skylight request failed: %s", type(e).__name__)
                raise SkylightError(f"Couldn't reach Skylight ({type(e).__name__}).") from None
