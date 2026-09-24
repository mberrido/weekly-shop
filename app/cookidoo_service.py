"""Thin, failure-tolerant wrapper around the unofficial cookidoo-api client.

- One aiohttp session for the app's lifetime, created lazily on first use so
  the app starts fine when Cookidoo is down or not configured.
- Tokens are persisted on the data volume so restarts don't re-login.
- Every call is funnelled through `call()`, which turns any failure into a
  `CookidooError` (-> HTTP 502) and logs it without credentials or tokens.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import date
from pathlib import Path
from typing import Any, Awaitable, Callable, TypeVar

log = logging.getLogger(__name__)
T = TypeVar("T")

LOGIN_BACKOFF_SECONDS = 60


class CookidooError(Exception):
    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.message = message
        self.status = status


class CookidooService:
    def __init__(
        self,
        email: str,
        password: str,
        country: str = "gb",
        language: str = "en-GB",
        token_path: str | Path = "/data/cookidoo_token.json",
        api_factory: Callable[[], Awaitable[Any]] | None = None,
    ):
        self._email = email or ""
        self._password = password or ""
        self._country = country
        self._language = language
        self._token_path = Path(token_path)
        self._api_factory = api_factory
        self._api: Any = None
        self._session: Any = None
        self._lock = asyncio.Lock()
        self._login_failed_at = 0.0

    @property
    def configured(self) -> bool:
        return bool(self._email and self._password) or self._api_factory is not None

    # -- lifecycle ---------------------------------------------------------

    async def _build_api(self) -> Any:
        import aiohttp
        from cookidoo_api import Cookidoo, CookidooConfig, get_localization_options

        if self._session is None:
            self._session = aiohttp.ClientSession(
                cookie_jar=aiohttp.CookieJar(unsafe=True),
                timeout=aiohttp.ClientTimeout(total=30),
            )
        loc = (await get_localization_options(country=self._country, language=self._language))[0]
        api = Cookidoo(
            self._session,
            cfg=CookidooConfig(email=self._email, password=self._password, localization=loc),
        )
        api.on_auth_data_update = lambda _: self._save_token(api)
        return api

    def _save_token(self, api: Any) -> None:
        try:
            api.save_token(self._token_path)
            os.chmod(self._token_path, 0o600)
        except Exception as e:  # never let token persistence break a request
            log.warning("Could not save Cookidoo token: %s", type(e).__name__)

    async def _login(self, api: Any) -> None:
        if time.monotonic() - self._login_failed_at < LOGIN_BACKOFF_SECONDS:
            raise CookidooError("Cookidoo login failed recently; try again in a minute.")
        try:
            await api.login()
        except Exception:
            self._login_failed_at = time.monotonic()
            raise
        self._save_token(api)
        log.info("Logged in to Cookidoo")

    async def _get_api(self) -> Any:
        async with self._lock:
            if self._api is not None:
                return self._api
            if self._api_factory is not None:
                self._api = await self._api_factory()
                return self._api
            api = await self._build_api()
            restored = False
            if self._token_path.exists():
                try:
                    api.load_token(self._token_path)
                    await api.get_user_info()
                    restored = True
                    log.info("Reused saved Cookidoo token")
                except Exception as e:
                    log.info("Saved Cookidoo token not usable (%s); logging in", type(e).__name__)
            if not restored:
                await self._login(api)
            self._api = api
            return api

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None
        self._api = None

    # -- error funnel ------------------------------------------------------

    async def call(self, what: str, fn: Callable[[Any], Awaitable[T]]) -> T:
        if not self.configured:
            raise CookidooError("Cookidoo isn't configured on this server.", status=503)
        from cookidoo_api import CookidooAuthException

        try:
            api = await self._get_api()
            try:
                return await fn(api)
            except CookidooAuthException:
                # Token rejected despite refresh: log in again once and retry.
                await self._login(api)
                return await fn(api)
        except CookidooError:
            raise
        except Exception as e:
            log.warning("Cookidoo %s failed: %s: %s", what, type(e).__name__, self._scrub(str(e)))
            raise CookidooError(f"Couldn't reach Cookidoo while {what} ({type(e).__name__}). "
                                "The rest of the app still works.") from None

    def _scrub(self, text: str) -> str:
        for secret in (self._password, self._email):
            if secret:
                text = text.replace(secret, "***")
        return text[:300]

    # -- operations used by the app ----------------------------------------

    async def recipe(self, recipe_id: str) -> Any:
        return await self.call("loading a recipe", lambda api: api.get_recipe_details(recipe_id))

    async def custom_recipe(self, recipe_id: str) -> Any:
        return await self.call("loading a created recipe", lambda api: api.get_custom_recipe(recipe_id))

    async def search(self, query: str) -> Any:
        return await self.call("searching", lambda api: api.search_recipes(query, page_size=12))

    async def calendar_week(self, day: date) -> Any:
        return await self.call("loading My Week", lambda api: api.get_recipes_in_calendar_week(day))

    async def push_list(self, recipe_ids: list[str], custom_ids: list[str], items: list[str]) -> None:
        async def push(api: Any) -> None:
            if recipe_ids:
                await api.add_ingredient_items_for_recipes(recipe_ids)
            if custom_ids:
                await api.add_ingredient_items_for_custom_recipes(custom_ids)
            if items:
                await api.add_additional_items(items)

        await self.call("sending the shopping list", push)
