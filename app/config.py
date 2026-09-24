"""Settings from environment variables (.env). No defaults for secrets."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

PIN_PATTERN = re.compile(r"^[0-9]{6}$")
MIN_SECRET_BYTES = 32



class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    db_path: str
    app_pin: str = field(repr=False)
    session_secret: str = field(repr=False)
    cookidoo_email: str = ""
    cookidoo_password: str = field(default="", repr=False)
    cookidoo_country: str = "gb"
    cookidoo_language: str = "en-GB"
    pantry_url: str = ""
    pantry_read_key: str = field(default="", repr=False)
    pexels_api_key: str = field(default="", repr=False)

    @property
    def data_dir(self) -> Path:
        return Path(self.db_path).parent

    @property
    def cookidoo_token_path(self) -> Path:
        return self.data_dir / "cookidoo_token.json"


def load_settings(env: dict[str, str] | None = None) -> Settings:
    """Read settings and refuse to start with missing or weak secrets."""
    env = os.environ if env is None else env
    pin = (env.get("APP_PIN") or "").strip()
    secret = env.get("SESSION_SECRET") or ""
    problems = []
    if not pin:
        problems.append("APP_PIN is not set" + (" (APP_PASSWORD has been replaced by a 6-digit APP_PIN)"
                                                if env.get("APP_PASSWORD") else ""))
    elif not PIN_PATTERN.match(pin):
        problems.append("APP_PIN must be exactly 6 digits")
    if not secret:
        problems.append("SESSION_SECRET is not set")
    elif len(secret.encode()) < MIN_SECRET_BYTES:
        problems.append(f"SESSION_SECRET must be at least {MIN_SECRET_BYTES} bytes "
                        "(generate one with: python3 -c 'import secrets; print(secrets.token_urlsafe(48))')")
    if problems:
        raise ConfigError("Refusing to start: " + "; ".join(problems) + ".")
    return Settings(
        db_path=env.get("DB_PATH") or "/data/shop.db",
        app_pin=pin,
        session_secret=secret,
        cookidoo_email=(env.get("COOKIDOO_EMAIL") or "").strip(),
        cookidoo_password=env.get("COOKIDOO_PASSWORD") or "",
        cookidoo_country=(env.get("COOKIDOO_COUNTRY") or "gb").strip().lower(),
        cookidoo_language=(env.get("COOKIDOO_LANGUAGE") or "en-GB").strip(),
        pantry_url=(env.get("PANTRY_URL") or "").strip(),
        pantry_read_key=(env.get("PANTRY_READ_KEY") or "").strip(),
        pexels_api_key=(env.get("PEXELS_API_KEY") or "").strip(),
    )
