"""Minimal synchronous ASGI test client.

FastAPI's TestClient needs httpx, which isn't an approved dependency, so this
drives the ASGI app directly. Supports JSON/form bodies, cookies and a
configurable client IP / headers.
"""

from __future__ import annotations

import asyncio
import json as _json
from dataclasses import dataclass
from http.cookies import SimpleCookie
from urllib.parse import urlencode, urlsplit


@dataclass
class Response:
    status_code: int
    headers: dict[str, str]
    set_cookies: list[str]
    content: bytes

    def json(self):
        return _json.loads(self.content)

    @property
    def text(self) -> str:
        return self.content.decode()


class Client:
    def __init__(self, app, client_ip: str = "127.0.0.1", scheme: str = "https"):
        self.app = app
        self.client_ip = client_ip
        self.scheme = scheme
        self.cookies: dict[str, str] = {}
        self.loop = asyncio.new_event_loop()

    def close(self):
        self.loop.close()

    def request(self, method, url, *, json=None, data=None, content=None, headers=None, client_ip=None) -> Response:
        parts = urlsplit(url)
        body = b""
        hdrs = {"host": "testserver"}
        if json is not None:
            body = _json.dumps(json).encode()
            hdrs["content-type"] = "application/json"
        elif content is not None:
            body = content
        elif data is not None:
            body = urlencode(data).encode()
            hdrs["content-type"] = "application/x-www-form-urlencoded"
        if self.cookies:
            hdrs["cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        hdrs["content-length"] = str(len(body))
        hdrs.update({k.lower(): v for k, v in (headers or {}).items()})
        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "method": method.upper(), "scheme": self.scheme, "path": parts.path,
            "raw_path": parts.path.encode(), "query_string": parts.query.encode(),
            "root_path": "", "headers": [(k.encode(), v.encode()) for k, v in hdrs.items()],
            "client": (client_ip or self.client_ip, 50000), "server": ("testserver", 443),
        }
        sent = False
        out = {"status": 0, "headers": [], "body": b""}

        async def receive():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            await asyncio.sleep(3600)
            return {"type": "http.disconnect"}

        async def send(msg):
            if msg["type"] == "http.response.start":
                out["status"] = msg["status"]
                out["headers"] = [(k.decode().lower(), v.decode()) for k, v in msg["headers"]]
            elif msg["type"] == "http.response.body":
                out["body"] += msg.get("body", b"")

        self.loop.run_until_complete(self.app(scope, receive, send))
        set_cookies = [v for k, v in out["headers"] if k == "set-cookie"]
        for sc in set_cookies:
            c = SimpleCookie()
            c.load(sc)
            for name, morsel in c.items():
                if morsel["max-age"] == "0" or not morsel.value.strip('"'):
                    self.cookies.pop(name, None)
                else:
                    self.cookies[name] = morsel.value
        return Response(out["status"], dict(out["headers"]), set_cookies, out["body"])

    def get(self, url, **kw):
        return self.request("GET", url, **kw)

    def post(self, url, **kw):
        return self.request("POST", url, **kw)

    def put(self, url, **kw):
        return self.request("PUT", url, **kw)

    def patch(self, url, **kw):
        return self.request("PATCH", url, **kw)

    def delete(self, url, **kw):
        return self.request("DELETE", url, **kw)
