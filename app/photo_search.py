"""Meal photos from Pexels (https://www.pexels.com/api/).

Search returns thumbnails for the picker; `download` fetches a chosen photo by
its Pexels id (never an arbitrary URL) so it can be stored in <data>/photos.
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.parse
import urllib.request

log = logging.getLogger(__name__)

API = "https://api.pexels.com/v1"
IMAGE_HOST = "images.pexels.com"
MAX_BYTES = 8 * 1024 * 1024
TIMEOUT = 8


class PhotoSearchError(Exception):
    pass


class PexelsClient:
    def __init__(self, api_key: str):
        self._key = (api_key or "").strip()

    @property
    def configured(self) -> bool:
        return bool(self._key)

    def _open(self, url: str, accept: str):
        req = urllib.request.Request(url, headers={
            "Authorization": self._key, "Accept": accept, "User-Agent": "WeeklyShop/1.0"})
        return urllib.request.urlopen(req, timeout=TIMEOUT)

    def _json(self, url: str) -> dict:
        with self._open(url, "application/json") as resp:
            return json.load(resp)

    async def _call(self, what: str, fn, *args):
        if not self.configured:
            raise PhotoSearchError("Online photo search isn't set up (PEXELS_API_KEY).")
        try:
            return await asyncio.to_thread(fn, *args)
        except PhotoSearchError:
            raise
        except Exception as e:
            log.warning("Pexels %s failed: %s", what, type(e).__name__)
            raise PhotoSearchError(f"Couldn't reach Pexels while {what}.") from None

    async def search(self, query: str, per_page: int = 15) -> list[dict]:
        q = urllib.parse.urlencode({"query": query, "per_page": per_page})
        data = await self._call("searching", self._json, f"{API}/search?{q}")
        return [
            {"id": int(p["id"]), "thumb": p["src"]["medium"], "alt": p.get("alt") or "",
             "photographer": p.get("photographer") or ""}
            for p in data.get("photos", []) if p.get("id") and p.get("src")
        ]

    def _download(self, photo_id: int) -> tuple[bytes, str]:
        meta = self._json(f"{API}/photos/{int(photo_id)}")
        url = meta["src"]["large"]
        if urllib.parse.urlparse(url).hostname != IMAGE_HOST:
            raise PhotoSearchError("Unexpected image host.")
        with self._open(url, "image/jpeg") as resp:
            body = resp.read(MAX_BYTES + 1)
        if len(body) > MAX_BYTES or not body.startswith(b"\xff\xd8\xff"):
            raise PhotoSearchError("That photo isn't a usable JPEG.")
        return body, f"{meta.get('photographer') or 'Unknown'} / Pexels"

    async def download(self, photo_id: int) -> tuple[bytes, str]:
        """(jpeg bytes, credit line) for a Pexels photo id."""
        return await self._call("downloading the photo", self._download, photo_id)
