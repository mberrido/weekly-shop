"""Read-only client for the Pantry Tracker app (GET /api/products).

Never writes to the pantry. Any failure returns None so the shopping list
simply works without pantry data.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.request
from typing import Any

log = logging.getLogger(__name__)

CACHE_SECONDS = 10
TIMEOUT_SECONDS = 4


class PantryClient:
    def __init__(self, base_url: str, read_key: str = ""):
        self.base_url = (base_url or "").rstrip("/")
        self._read_key = read_key or ""
        self._cache: tuple[float, list[dict]] | None = None
        self._last_error_logged = 0.0

    @property
    def configured(self) -> bool:
        return bool(self.base_url)

    def _fetch(self) -> Any:
        headers = {"Accept": "application/json"}
        if self._read_key:  # lets us read while Pantry Tracker's PIN is on
            headers["X-Pantry-Key"] = self._read_key
        req = urllib.request.Request(f"{self.base_url}/api/products", headers=headers)
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            return json.load(resp)

    async def products(self) -> list[dict] | None:
        """Active pantry products (id, name, quantity), or None if unavailable."""
        if not self.configured:
            return None
        now = time.monotonic()
        if self._cache and now - self._cache[0] < CACHE_SECONDS:
            return self._cache[1]
        try:
            data = await asyncio.to_thread(self._fetch)
            rows = data if isinstance(data, list) else data.get("products", [])
            products = [
                {"id": int(p["id"]), "name": str(p.get("name") or ""), "quantity": int(p.get("quantity") or 0),
                 "reorder_threshold": int(p.get("reorder_threshold") or 0),
                 "weekly_shop_qty": int(p["weekly_shop_qty"]) if p.get("weekly_shop_qty") else None}
                for p in rows if isinstance(p, dict) and "id" in p and not p.get("archived")
            ]
        except Exception as e:
            if now - self._last_error_logged > 300:  # don't spam the log every refresh
                log.warning("Pantry Tracker unavailable at %s: %s", self.base_url, type(e).__name__)
                self._last_error_logged = now
            return None
        self._cache = (now, products)
        return products

    def invalidate(self) -> None:
        self._cache = None
