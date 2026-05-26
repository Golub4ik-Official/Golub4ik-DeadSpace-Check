import asyncio
import functools
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, List, Coroutine, Callable, TypeVar, Optional, Dict, Awaitable

T = TypeVar('T')

logger = logging.getLogger(__name__)


async def run_with_semaphore(semaphore: asyncio.Semaphore, coro: Coroutine) -> Any:
    async with semaphore:
        return await coro


async def gather_with_concurrency(n: int, *coros: Coroutine[Any, Any, Any]) -> List[Any]:
    """Run coroutines with a concurrency limit.

    Slightly optimized to start tasks lazily instead of creating all
    tasks up-front when there are many coroutines. If the number of
    coroutines is <= n we just gather directly.
    """
    if not coros:
        return []
    if len(coros) <= n:
        return await asyncio.gather(*coros)

    semaphore = asyncio.Semaphore(n)

    async def sem_task(c: Coroutine[Any, Any, Any]):
        async with semaphore:
            return await c

    return await asyncio.gather(*(sem_task(c) for c in coros))


def to_thread(func):
    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        return await asyncio.to_thread(func, *args, **kwargs)

    return wrapper


@dataclass
class _CacheEntry:
    value: Any
    timestamp: float  # monotonic timestamp when stored


class AsyncCache:
    """An async LRU + TTL cache.

    Optimizations over the previous version:
    - Uses time.monotonic() for stable elapsed time computation.
    - Avoids duplicate factory execution via an in-flight task registry.
    - Reduces lock contention with a double-checked pattern.
    - Periodic cleanup is scheduled sparingly; cleanup skips if lock is busy.
    - Provides stats() & close() helpers.
    Behavior (TTL semantics & LRU) is preserved.
    """

    def __init__(self, max_size: int = 1000, default_ttl: float = 3600, cleanup_interval: float = 1800):
        self.cache: OrderedDict[str, _CacheEntry] = OrderedDict()
        self.max_size = max_size
        self.default_ttl = default_ttl
        self._cleanup_interval = cleanup_interval
        self.lock = asyncio.Lock()
        self.hits = 0
        self.misses = 0
        self.evictions = 0
        self._last_cleanup_time = time.monotonic()
        self._ttl_overrides: Dict[str, float] = {}
        self._inflight: Dict[str, asyncio.Task] = {}
        self._closed = False

    # ---------------- Public API ---------------- #
    def set_ttl_override(self, pattern: str, ttl: float):
        self._ttl_overrides[pattern] = ttl

    def __contains__(self, key: str) -> bool:  # fast membership
        entry = self.cache.get(key)
        if not entry:
            return False
        return (time.monotonic() - entry.timestamp) < self._get_ttl_for_key(key)

    async def get(self, key: str, factory: Callable[[], Awaitable[T]], ttl: Optional[float] = None) -> T:
        if self._closed:
            raise RuntimeError("AsyncCache has been closed")

        now = time.monotonic()
        effective_ttl = ttl if ttl is not None else self._get_ttl_for_key(key)

        # Fast path without lock (stale OK check will be validated under lock)
        entry = self.cache.get(key)
        if entry and (now - entry.timestamp) < effective_ttl:
            async with self.lock:  # still move to end under lock
                # Re-verify after acquiring lock
                entry2 = self.cache.get(key)
                if entry2 and (time.monotonic() - entry2.timestamp) < effective_ttl:
                    self.hits += 1
                    self.cache.move_to_end(key)
                    return entry2.value

        # Miss path
        async with self.lock:
            # Double-check inside lock in case another coroutine filled it
            entry = self.cache.get(key)
            if entry and (time.monotonic() - entry.timestamp) < effective_ttl:
                self.hits += 1
                self.cache.move_to_end(key)
                return entry.value

            # If an in-flight task exists, await it (avoid thundering herd)
            task = self._inflight.get(key)
            if task:
                self.hits += 1  # treat joining an inflight as a hit
                return await task

            # Register new in-flight task
            self.misses += 1
            loop = asyncio.get_running_loop()
            task = loop.create_task(self._create_and_store(key, factory, effective_ttl))
            self._inflight[key] = task

            # Maybe schedule cleanup (non-blocking)
            if (time.monotonic() - self._last_cleanup_time) > self._cleanup_interval:
                self._last_cleanup_time = time.monotonic()
                asyncio.create_task(self._cleanup_expired())

        try:
            return await task
        finally:
            # Ensure inflight registry cleanup
            async with self.lock:
                self._inflight.pop(key, None)

    async def clear(self):
        async with self.lock:
            for t in self._inflight.values():
                if not t.done():
                    t.cancel()
            self._inflight.clear()
            self.cache.clear()
            self.hits = self.misses = self.evictions = 0
            logger.info("AsyncCache cleared.")

    async def close(self):
        await self.clear()
        self._closed = True

    def stats(self) -> Dict[str, Any]:
        size = len(self.cache)
        return {
            "size": size,
            "max_size": self.max_size,
            "hits": self.hits,
            "misses": self.misses,
            "evictions": self.evictions,
            "hit_ratio": round(self.hits / (self.hits + self.misses), 4) if (self.hits + self.misses) else 0.0,
            "inflight": len(self._inflight),
        }

    # ---------------- Internal helpers ---------------- #
    def _get_ttl_for_key(self, key: str) -> float:
        for pattern, ttl in self._ttl_overrides.items():
            if pattern in key:
                return ttl
        return self.default_ttl

    async def _create_and_store(self, key: str, factory: Callable[[], Awaitable[T]], effective_ttl: float) -> T:
        start = time.monotonic()
        # Run factory outside lock
        try:
            value = await factory()
        except Exception as e:
            logger.error(f"Cache factory error for key '{key}': {e}", exc_info=True)
            raise

        # Store
        async with self.lock:
            now = time.monotonic()
            self.cache[key] = _CacheEntry(value=value, timestamp=now)
            self.cache.move_to_end(key)

            # Evict LRU until size constraint satisfied
            while len(self.cache) > self.max_size:
                old_key, _old_entry = self.cache.popitem(last=False)
                self.evictions += 1
                # Best-effort: cancel inflight (should not exist typically)
                inflight_task = self._inflight.pop(old_key, None)
                if inflight_task and not inflight_task.done():
                    inflight_task.cancel()

        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                f"AsyncCache store: key={key} took={time.monotonic() - start:.3f}s ttl={effective_ttl}s size={len(self.cache)}"
            )
        return value

    async def _cleanup_expired(self):
        # Skip if lock busy to avoid contention
        if self.lock.locked():
            return
        expired = 0
        now = time.monotonic()
        async with self.lock:
            keys = list(self.cache.keys())  # snapshot
            for k in keys:
                entry = self.cache.get(k)
                if not entry:
                    continue
                if (now - entry.timestamp) > self._get_ttl_for_key(k):
                    del self.cache[k]
                    self.evictions += 1
                    expired += 1
        if expired and logger.isEnabledFor(logging.DEBUG):
            logger.debug(f"AsyncCache cleanup: expired={expired} size={len(self.cache)}")
