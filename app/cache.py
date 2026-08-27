"""Cache profiles so we call LinkedIn as rarely as possible.

Every avoided call lowers the risk to the account. In memory works for one
process. Set REDIS_URL and every instance shares one cache, which matters as
soon as the service runs more than one machine.
"""

from __future__ import annotations

import json
import logging
import time
from collections import OrderedDict
from typing import Any, Protocol

log = logging.getLogger(__name__)


class Cache(Protocol):
    async def get(self, key: str) -> dict[str, Any] | None: ...
    async def set(self, key: str, value: dict[str, Any], ttl: int) -> None: ...
    async def delete(self, key: str) -> None: ...
    async def stats(self) -> dict[str, Any]: ...


class MemoryCache:
    """A least recently used cache with a time to live on each entry."""

    def __init__(self, max_entries: int = 1000) -> None:
        self.max_entries = max_entries
        self._data: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()
        self.hits = 0
        self.misses = 0

    async def get(self, key: str) -> dict[str, Any] | None:
        entry = self._data.get(key)
        if entry is None:
            self.misses += 1
            return None
        expires_at, value = entry
        if time.time() > expires_at:
            del self._data[key]
            self.misses += 1
            return None
        self._data.move_to_end(key)
        self.hits += 1
        return value

    async def set(self, key: str, value: dict[str, Any], ttl: int) -> None:
        self._data[key] = (time.time() + ttl, value)
        self._data.move_to_end(key)
        while len(self._data) > self.max_entries:
            self._data.popitem(last=False)

    async def delete(self, key: str) -> None:
        self._data.pop(key, None)

    async def stats(self) -> dict[str, Any]:
        total = self.hits + self.misses
        return {
            "backend": "memory",
            "entries": len(self._data),
            "max_entries": self.max_entries,
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": round(self.hits / total, 3) if total else 0.0,
        }


class RedisCache:
    """Shared cache. Falls back to memory when Redis is unreachable."""

    def __init__(self, url: str, fallback: MemoryCache) -> None:
        import redis.asyncio as redis

        self._redis = redis.from_url(url, decode_responses=True)
        self._fallback = fallback
        self.hits = 0
        self.misses = 0

    async def get(self, key: str) -> dict[str, Any] | None:
        try:
            raw = await self._redis.get(key)
        except Exception as exc:  # noqa: BLE001 - a cache miss must never fail a request
            log.warning("Redis get failed, using memory: %s", exc)
            return await self._fallback.get(key)
        if raw is None:
            self.misses += 1
            return None
        self.hits += 1
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    async def set(self, key: str, value: dict[str, Any], ttl: int) -> None:
        try:
            await self._redis.setex(key, ttl, json.dumps(value, default=str))
        except Exception as exc:  # noqa: BLE001
            log.warning("Redis set failed, using memory: %s", exc)
            await self._fallback.set(key, value, ttl)

    async def delete(self, key: str) -> None:
        try:
            await self._redis.delete(key)
        except Exception:  # noqa: BLE001
            pass
        await self._fallback.delete(key)

    async def stats(self) -> dict[str, Any]:
        total = self.hits + self.misses
        return {
            "backend": "redis",
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": round(self.hits / total, 3) if total else 0.0,
        }


def build_cache(redis_url: str | None, max_entries: int) -> Cache:
    memory = MemoryCache(max_entries)
    if redis_url:
        try:
            return RedisCache(redis_url, memory)
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not start Redis, using memory only: %s", exc)
    return memory


def profile_key(public_identifier: str) -> str:
    return f"profile:v1:{public_identifier.lower()}"
