import pytest
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.auth import LoginLimiter, hash_password, verify_password
from app.config import ConfigError, load_settings
from tests.asgi_client import Client
from tests.conftest import PASSWORD, SECRET

BAD = "000000"


# -- config validation -----------------------------------------------------------

@pytest.mark.parametrize("env,msg", [
    ({}, "APP_PIN is not set"),
    ({"APP_PASSWORD": "old password", "SESSION_SECRET": SECRET}, "replaced by a 6-digit APP_PIN"),
    ({"APP_PIN": "12345", "SESSION_SECRET": SECRET}, "exactly 6 digits"),
    ({"APP_PIN": "1234567", "SESSION_SECRET": SECRET}, "exactly 6 digits"),
    ({"APP_PIN": "12a456", "SESSION_SECRET": SECRET}, "exactly 6 digits"),
    ({"APP_PIN": PASSWORD}, "SESSION_SECRET is not set"),
    ({"APP_PIN": PASSWORD, "SESSION_SECRET": "x" * 31}, "at least 32 bytes"),
])
def test_refuses_weak_or_missing_secrets(env, msg):
    with pytest.raises(ConfigError, match=msg):
        load_settings(env)


def test_pin_whitespace_trimmed():
    assert load_settings({"APP_PIN": " 012345 ", "SESSION_SECRET": SECRET}).app_pin == "012345"


def test_settings_repr_hides_secrets():
    s = load_settings({"APP_PIN": PASSWORD, "SESSION_SECRET": SECRET, "COOKIDOO_PASSWORD": "cookpw"})
    assert PASSWORD not in repr(s) and SECRET not in repr(s) and "cookpw" not in repr(s)


def test_password_hash_roundtrip():
    h = hash_password(PASSWORD)
    assert PASSWORD not in h and h.startswith("scrypt$")
    assert verify_password(PASSWORD, h) and not verify_password(BAD, h)
    assert not verify_password(PASSWORD, "garbage")


# -- access control ------------------------------------------------------------

def test_api_requires_session(anon):
    for path in ["/api/meals", "/api/list", "/api/config", "/api/week"]:
        r = anon.get(path)
        assert r.status_code == 401 and r.json() == {"detail": "Not logged in"}
    assert anon.post("/api/meals", json={"name": "x"}).status_code == 401


def test_pages_redirect_to_login(anon):
    r = anon.get("/")
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert anon.get("/whatever").headers["location"] == "/login?next=/whatever"


def test_public_routes(anon):
    assert anon.get("/healthz").status_code == 200
    login = anon.get("/login")
    assert login.status_code == 200 and 'id="pad"' in login.text and 'name="pin"' in login.text
    assert anon.get("/manifest.webmanifest").status_code == 200
    assert anon.get("/icons/icon-192.png").status_code == 200


def test_login_sets_secure_cookie_and_logout_clears(anon):
    r = anon.post("/login", data={"password": PASSWORD, "next": "/"})
    assert r.status_code == 303 and r.headers["location"] == "/"
    cookie = r.set_cookies[0]
    for flag in ["ws_session=", "HttpOnly", "Secure", "SameSite=lax", "Max-Age=7776000", "Path=/"]:
        assert flag.lower() in cookie.lower(), flag
    assert anon.get("/api/meals").status_code == 200
    assert anon.get("/login").status_code == 303          # already logged in
    r = anon.post("/logout")
    assert r.status_code == 303 and "max-age=0" in r.set_cookies[0].lower()
    assert anon.get("/api/meals").status_code == 401


def test_json_login(anon):
    assert anon.post("/login", json={"password": PASSWORD}).status_code == 303


def test_pin_field_login(anon):
    assert anon.post("/login", data={"pin": PASSWORD}).status_code == 303
    anon.post("/logout")
    assert anon.post("/login", data={"pin": " " + PASSWORD + " "}).status_code == 303   # stray spaces ignored
    anon.post("/logout")
    r = anon.post("/login", data={"pin": "999999"})
    assert r.status_code == 401 and "Wrong PIN" in r.text


def test_tampered_or_foreign_cookie_rejected(anon, make_client, settings):
    anon.post("/login", data={"password": PASSWORD})
    token = anon.cookies["ws_session"]
    anon.cookies["ws_session"] = token[:-2] + ("AA" if not token.endswith("AA") else "BB")
    assert anon.get("/api/meals").status_code == 401
    # A session issued under a different password is no longer valid.
    other = make_client(app_pin="111111")
    other.cookies["ws_session"] = token
    assert other.get("/api/meals").status_code == 401


def test_open_redirect_blocked(anon):
    for evil in ["//evil.example", "https://evil.example", "/\\evil.example"]:
        r = anon.post("/login", data={"password": PASSWORD, "next": evil})
        assert r.headers["location"] == "/"
        anon.post("/logout")
    assert anon.post("/login", data={"password": PASSWORD, "next": "/?x=1"}).headers["location"] == "/?x=1"


def test_security_headers(anon, client):
    for r in [anon.get("/login"), anon.get("/api/meals"), client.get("/"), client.get("/api/meals")]:
        assert r.headers["strict-transport-security"].startswith("max-age=")
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["referrer-policy"] == "same-origin"
        assert "frame-ancestors 'none'" in r.headers["content-security-policy"]


def test_login_page_escapes_next(anon):
    r = anon.get('/login?next=/"><script>alert(1)</script>')
    assert "<script>alert" not in r.text


# -- rate limiting -------------------------------------------------------------

def test_lockout_after_five_failures(anon):
    for i in range(4):
        r = anon.post("/login", data={"password": BAD})
        assert r.status_code == 401 and f"{4 - i} attempt" in r.text
    assert anon.post("/login", data={"password": BAD}).status_code == 429
    # Even the right password is refused while locked out...
    r = anon.post("/login", data={"password": PASSWORD})
    assert r.status_code == 429 and int(r.headers["retry-after"]) > 0
    # ...but another IP is unaffected.
    assert anon.post("/login", data={"password": PASSWORD}, client_ip="10.0.0.9").status_code == 303


def test_success_resets_failures(anon):
    for _ in range(4):
        anon.post("/login", data={"password": BAD})
    assert anon.post("/login", data={"password": PASSWORD}).status_code == 303
    anon.post("/logout")
    for _ in range(4):
        assert anon.post("/login", data={"password": BAD}).status_code == 401


def test_limiter_window_expires():
    now = [1000.0]
    lim = LoginLimiter(clock=lambda: now[0])
    for _ in range(5):
        lim.fail("1.2.3.4")
    assert lim.retry_after("1.2.3.4") > 0
    now[0] += 15 * 60 - 1
    assert lim.retry_after("1.2.3.4") > 0
    now[0] += 2
    assert lim.retry_after("1.2.3.4") == 0 and lim.remaining("1.2.3.4") == 5


def test_forwarded_for_trusted_only_from_proxy(make_client):
    """Mirror of the container's uvicorn --proxy-headers --forwarded-allow-ips setup."""
    inner = make_client()
    client = Client(ProxyHeadersMiddleware(inner.app, trusted_hosts="127.0.0.1,172.30.84.1"))
    limiter = inner.app.state.login_limiter
    try:
        # Via the trusted reverse proxy: the real client IP is used.
        client.post("/login", data={"password": BAD}, client_ip="172.30.84.1",
                    headers={"X-Forwarded-For": "81.2.69.160"})
        assert limiter.remaining("81.2.69.160") == 4
        # A spoofed header prepended by the client doesn't help: rightmost untrusted hop wins.
        client.post("/login", data={"password": BAD}, client_ip="172.30.84.1",
                    headers={"X-Forwarded-For": "1.1.1.1, 81.2.69.160"})
        assert limiter.remaining("81.2.69.160") == 3 and limiter.remaining("1.1.1.1") == 5
        # From an untrusted peer the header is ignored entirely.
        client.post("/login", data={"password": BAD}, client_ip="192.168.1.50",
                    headers={"X-Forwarded-For": "9.9.9.9"})
        assert limiter.remaining("192.168.1.50") == 4 and limiter.remaining("9.9.9.9") == 5
    finally:
        client.close()


def test_plain_http_login_works_without_secure_flag(make_client):
    """Home-network access at http://<nas-ip>:8420 needs a non-Secure cookie."""
    c = make_client()
    c.scheme = "http"
    r = c.post("/login", data={"pin": PASSWORD})
    assert r.status_code == 303 and "secure" not in r.set_cookies[0].lower()
    assert c.get("/api/meals").status_code == 200
