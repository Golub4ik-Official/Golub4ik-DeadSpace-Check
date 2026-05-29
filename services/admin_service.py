import asyncio
import hashlib
import json
import logging
import os
import pickle
import time
from collections import deque, OrderedDict
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional, Set, Tuple, Callable
from urllib.parse import quote_plus

from aiolimiter import AsyncLimiter

from admin_panel import N_A, AdminPanel
from config_system import get_config
from models.player import Player
from utils.async_utils import AsyncCache
from utils.performance_monitor import monitor_performance, PerformanceTracker


class LRUCache:
    def __init__(self, max_size: int = 1000, ttl: float = 3600):
        self.max_size = max_size
        self.ttl = ttl
        self.cache: OrderedDict[str, Tuple[Any, float]] = OrderedDict()
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> Optional[Any]:
        if key in self.cache:
            value, timestamp = self.cache[key]
            if time.time() - timestamp < self.ttl:
                self.cache.move_to_end(key)
                self.hits += 1
                return value
            else:
                del self.cache[key]

        self.misses += 1
        return None

    def put(self, key: str, value: Any) -> None:
        current_time = time.time()

        if key in self.cache:
            self.cache[key] = (value, current_time)
            self.cache.move_to_end(key)
        else:
            self.cache[key] = (value, current_time)

            while len(self.cache) > self.max_size:
                self.cache.popitem(last=False)

    def clear(self) -> None:
        self.cache.clear()
        self.hits = 0
        self.misses = 0

    def stats(self) -> Dict[str, Any]:
        total = self.hits + self.misses
        hit_rate = (self.hits / total) if total > 0 else 0.0
        return {
            'size': len(self.cache),
            'max_size': self.max_size,
            'hits': self.hits,
            'misses': self.misses,
            'hit_rate': hit_rate
        }


class PersistentCache:
    def __init__(self, cache_file: str, max_size: int = 5000):
        self.cache_file = cache_file
        self.max_size = max_size
        self.cache: Dict[str, Tuple[Any, float]] = {}
        self.dirty = False
        self.load_from_disk()

    def load_from_disk(self) -> None:
        if os.path.exists(self.cache_file):
            try:
                with open(self.cache_file, 'rb') as f:
                    self.cache = pickle.load(f)
                current_time = time.time()
                expired_keys = [
                    k for k, (_, timestamp) in self.cache.items()
                    if current_time - timestamp > 86400
                ]
                for k in expired_keys:
                    del self.cache[k]
                if expired_keys:
                    self.dirty = True
            except Exception as e:
                logging.warning(f"Failed to load cache from {self.cache_file}: {e}")
                self.cache = {}

    def save_to_disk(self) -> None:
        if not self.dirty:
            return

        try:
            if len(self.cache) > self.max_size:
                sorted_items = sorted(
                    self.cache.items(),
                    key=lambda x: x[1][1],
                    reverse=True
                )
                self.cache = dict(sorted_items[:self.max_size])

            os.makedirs(os.path.dirname(self.cache_file), exist_ok=True)
            with open(self.cache_file, 'wb') as f:
                pickle.dump(self.cache, f)
            self.dirty = False
        except Exception as e:
            logging.warning(f"Failed to save cache to {self.cache_file}: {e}")

    def get(self, key: str, ttl: float = 86400) -> Optional[Any]:
        if key in self.cache:
            value, timestamp = self.cache[key]
            if time.time() - timestamp < ttl:
                return value
            else:
                del self.cache[key]
                self.dirty = True
        return None

    def put(self, key: str, value: Any) -> None:
        self.cache[key] = (value, time.time())
        self.dirty = True


class StabilizedLoadOptimizer:
    def __init__(self, logger: logging.Logger, config: Any, initial_concurrency: int = 12) -> None:
        self.logger = logger
        self.cfg = config.api.load_optimizer

        self.high_latency_threshold = self.cfg.high_latency_threshold
        self.very_high_latency_threshold = self.cfg.very_high_latency_threshold
        self.low_latency_threshold = self.cfg.low_latency_threshold
        self.target_latency = self.cfg.target_latency

        self.max_concurrency = config.api.max_concurrent_requests
        self.min_concurrency = 3
        init_c = max(self.min_concurrency, min(initial_concurrency, self.max_concurrency))
        self.current_concurrency_level = init_c
        self.concurrency_semaphore = asyncio.Semaphore(self.current_concurrency_level)

        self.current_delay = 0.0
        self.max_delay = 15.0
        self.delay_increment = 0.2
        self.delay_decrement = 0.4

        self.latencies = deque(maxlen=60)
        self.recent_latencies = deque(maxlen=15)
        self.success_rate_tracker = deque(maxlen=50)
        self.average_latency = 0.0
        self.recent_average = 0.0
        self._ema_latency = 0.0
        self._ema_alpha = 0.2
        self.success_rate = 1.0
        self.error_requests = 0

        self.last_adjustment_time = 0
        self.min_adjustment_interval = self.cfg.min_adjustment_interval
        self.consecutive_reductions = 0
        self.consecutive_increases = 0
        self.max_consecutive_adjustments = self.cfg.max_consecutive_adjustments

        self.adjustment_history = deque(maxlen=20)
        self.lock = asyncio.Lock()

        self.adaptive_backoff_active = False
        self.original_concurrency = self.current_concurrency_level

        self.logger.info(
            f"StabilizedLoadOptimizer initialized: concurrency={self.current_concurrency_level}, "
            f"target_latency={self.target_latency}s"
        )

    def _update_metrics(self) -> None:
        if self.latencies:
            self.average_latency = sum(self.latencies) / len(self.latencies)
        if self.recent_latencies:
            self.recent_average = sum(self.recent_latencies) / len(self.recent_latencies)
        if self.success_rate_tracker:
            successful_requests = sum(self.success_rate_tracker)
            total_requests = len(self.success_rate_tracker)
            self.success_rate = successful_requests / total_requests if total_requests > 0 else 1.0
            self.error_requests = total_requests - successful_requests
        if self.latencies:
            last = self.latencies[-1]
            if self._ema_latency == 0.0:
                self._ema_latency = last
            else:
                self._ema_latency = (self._ema_alpha * last) + (1 - self._ema_alpha) * self._ema_latency

    async def record_latency(self, duration: float, success: bool) -> None:
        async with self.lock:
            effective_duration = duration if success else duration + self.target_latency * 2
            self.latencies.append(effective_duration)
            self.recent_latencies.append(effective_duration)
            self.success_rate_tracker.append(1 if success else 0)

            self._update_metrics()

            await self._check_adaptive_backoff()

            current_time = time.time()
            if current_time - self.last_adjustment_time < self.min_adjustment_interval:
                return

            if len(self.recent_latencies) < 5:
                return

            await self._consider_adjustment()

    async def _check_adaptive_backoff(self) -> None:
        latency_threshold = self.target_latency * 2.0

        if self.recent_average > latency_threshold and not self.adaptive_backoff_active:
            self.adaptive_backoff_active = True
            self.original_concurrency = self.current_concurrency_level
            new_concurrency = max(self.min_concurrency, self.current_concurrency_level // 2)

            if new_concurrency != self.current_concurrency_level:
                self.current_concurrency_level = new_concurrency
                self.concurrency_semaphore = asyncio.Semaphore(self.current_concurrency_level)
                self.logger.warning(
                    f"Adaptive backoff activated: reduced concurrency to {new_concurrency} "
                    f"(avg latency: {self.recent_average:.2f}s)"
                )

        elif self.recent_average < latency_threshold * 0.7 and self.adaptive_backoff_active:
            self.adaptive_backoff_active = False
            if self.original_concurrency != self.current_concurrency_level:
                self.current_concurrency_level = self.original_concurrency
                self.concurrency_semaphore = asyncio.Semaphore(self.current_concurrency_level)
                self.logger.info(
                    f"Adaptive backoff removed: restored concurrency to {self.original_concurrency} "
                    f"(avg latency: {self.recent_average:.2f}s)"
                )

    async def _consider_adjustment(self) -> None:
        adjustment_type = ""

        if self.success_rate < 0.8 and self.error_requests > 5:
            adjustment_type = "emergency_reduce"
        elif self.recent_average > self.very_high_latency_threshold:
            adjustment_type = "emergency_reduce"
        elif self.recent_average > self.target_latency * 1.75 and self.consecutive_reductions < self.max_consecutive_adjustments:
            adjustment_type = "reduce"
        elif self.recent_average < self.target_latency * 0.75 and self.success_rate > 0.95 and self.consecutive_increases < self.max_consecutive_adjustments:
            adjustment_type = "increase"
        else:
            self.consecutive_increases = 0
            self.consecutive_reductions = 0

        if adjustment_type:
            await self._make_adjustment(adjustment_type)
            self.last_adjustment_time = time.time()

    async def _make_adjustment(self, adjustment_type: str) -> None:
        old_concurrency = self.current_concurrency_level
        old_delay = self.current_delay

        if adjustment_type == "emergency_reduce":
            self.consecutive_reductions += 1
            self.consecutive_increases = 0
            if self.current_concurrency_level > self.min_concurrency:
                reduction = max(2, int(self.current_concurrency_level * 0.5))
                self.current_concurrency_level = max(self.min_concurrency, self.current_concurrency_level - reduction)
            self.current_delay = min(self.max_delay, self.current_delay + self.delay_increment * 2)
        elif adjustment_type == "reduce":
            self.consecutive_reductions += 1
            self.consecutive_increases = 0
            if self.current_concurrency_level > self.min_concurrency:
                reduction = 2 if self.current_concurrency_level > self.min_concurrency + 2 else 1
                self.current_concurrency_level = max(self.min_concurrency, self.current_concurrency_level - reduction)
            elif self.current_delay < self.max_delay:
                latency_ratio = self.recent_average / self.target_latency
                scaling_factor = max(1.0, min(latency_ratio, 3.0))
                increment = self.delay_increment * scaling_factor
                self.current_delay = min(self.max_delay, self.current_delay + increment)
        elif adjustment_type == "increase":
            self.consecutive_increases += 1
            self.consecutive_reductions = 0
            if self.current_delay > 0:
                self.current_delay = max(0.0, self.current_delay - self.delay_decrement)
            elif self.recent_average < self.target_latency * 0.6:
                self.current_concurrency_level = min(self.max_concurrency, self.current_concurrency_level + 1)

        if self.current_concurrency_level != old_concurrency:
            self.concurrency_semaphore = asyncio.Semaphore(self.current_concurrency_level)

        if self.current_concurrency_level != old_concurrency or self.current_delay != old_delay:
            log_msg = (
                f"Load adjustment ({adjustment_type}): "
                f"concurrency {old_concurrency}→{self.current_concurrency_level}, "
                f"delay {old_delay:.2f}s→{self.current_delay:.2f}s | "
                f"Recent Latency: {self.recent_average:.2f}s (Target: {self.target_latency}s), "
                f"Success Rate: {self.success_rate:.2%}"
            )
            self.logger.info(log_msg)
            self.adjustment_history.append(log_msg)

    async def wait_adaptive_delay(self) -> None:
        if self.current_delay > 0:
            await asyncio.sleep(self.current_delay)

    def get_current_stats(self) -> Dict[str, Any]:
        return {
            'concurrency_level': self.current_concurrency_level,
            'current_delay': round(self.current_delay, 2),
            'average_latency': round(self.average_latency, 2),
            'recent_average': round(self.recent_average, 2),
            'ema_latency': round(self._ema_latency, 2),
            'success_rate': round(self.success_rate, 3),
            'samples_collected': len(self.latencies),
            'adaptive_backoff_active': self.adaptive_backoff_active,
        }


class AdminService:
    def __init__(self, admin_panel: AdminPanel, max_concurrent_requests: int = 50) -> None:
        cfg = get_config()
        self.admin_panel = admin_panel

        self.initial_concurrency = min(max_concurrent_requests, cfg.api.max_concurrent_requests)
        self.rate_limiter = AsyncLimiter(1.2, 1.0)

        self.cache = AsyncCache(max_size=30000, default_ttl=3600)
        self.base_admin_connections_url = (
            f"{self.admin_panel.BASE_ADMIN_URL}/Connections?showSet=true&showAccepted=true&showBanned=true"
            "&showWhitelist=true&showFull=true&showPanic=true&perPage=2000"
        )

        from utils.logging_utils import get_logger
        self.logger = logging.getLogger(__name__)
        self.perf_logger = get_logger(f"{__name__}.performance")
        self.slow_operation_threshold = cfg.logging.slow_operation_threshold
        self.perf_tracker = PerformanceTracker(self.perf_logger)

        self.connections_cache = LRUCache(max_size=5000, ttl=1800)
        self.player_info_cache = LRUCache(max_size=2000, ttl=3600)
        self.search_results_cache = LRUCache(max_size=3000, ttl=1800)

        cache_dir = getattr(cfg, 'cache_dir', './cache')
        self.persistent_cache = PersistentCache(
            os.path.join(cache_dir, 'admin_service_cache.pkl')
        )

        self.expansion_terms_seen: Set[str] = set()
        self.expansion_terms_lock = asyncio.Lock()

        self._search_cache: OrderedDict[str, Tuple[Optional[Dict[str, Any]], float]] = OrderedDict()
        self._search_cache_max_size = cfg.scan.search_cache_max_size
        self._search_cache_ttl = cfg.scan.search_cache_ttl

        self._global_login_lock = asyncio.Lock()
        self._last_login_check = 0
        self._auth_ttl = 2400
        self._login_check_interval = 120
        self._is_authenticated = False
        self._auth_timestamp = 0

        self._operation_timeout = cfg.api.operation_timeout
        self._request_timeout = cfg.api.request_timeout
        self._search_timeout = cfg.api.search_timeout

        self._optimizer = StabilizedLoadOptimizer(
            logging.getLogger(f"{__name__}.optimizer"),
            cfg,
            self.initial_concurrency
        )

        self.error_tracking = {
            'consecutive_timeouts': 0,
            'consecutive_errors': 0,
            'last_success_time': time.time(),
            'total_requests': 0,
            'successful_requests': 0,
            'timeout_requests': 0,
            'error_requests': 0,
        }

        self.cooldown_active = False
        self.cooldown_until = 0
        self.cooldown_duration = cfg.api.cooldown_duration

        if self.logger.isEnabledFor(logging.INFO):
            self.logger.info(
                f"AdminService initialized with optimized settings: "
                f"concurrency={self.initial_concurrency}, "
                f"target_latency={cfg.api.load_optimizer.target_latency}s, "
                f"batch_sizes={cfg.scan.batch_processing.conservative_batch_size}-{cfg.scan.batch_processing.aggressive_batch_size}, "
                f"perPage=2000 (optimized)"
            )

    async def close(self):
        try:
            stats = self._optimizer.get_current_stats()
            self.logger.info(f"AdminService closing. Final optimizer stats: {stats}")

            self.logger.info(f"Connections cache stats: {self.connections_cache.stats()}")
            self.logger.info(f"Player info cache stats: {self.player_info_cache.stats()}")
            self.logger.info(f"Search results cache stats: {self.search_results_cache.stats()}")

            self.persistent_cache.save_to_disk()

            self._search_cache.clear()
            await self.cache.clear()
            await self.admin_panel.close()

            if self.logger.isEnabledFor(logging.INFO):
                self.logger.info("AdminService closed.")
        except Exception as e:
            self.logger.error(f"Error during AdminService cleanup: {e}", exc_info=True)

    async def add_expansion_term(self, term: str) -> bool:
        async with self.expansion_terms_lock:
            if term in self.expansion_terms_seen:
                return False
            self.expansion_terms_seen.add(term)
            return True

    def clear_expansion_terms(self) -> None:
        self.expansion_terms_seen.clear()

    def _should_apply_cooldown(self) -> bool:
        return (
                self.error_tracking['consecutive_timeouts'] > 5 or
                self.error_tracking['consecutive_errors'] > 8 or
                (time.time() - self.error_tracking['last_success_time']) > 300
        )

    async def _apply_emergency_cooldown(self):
        if not self.cooldown_active:
            self.cooldown_active = True
            self.cooldown_until = time.time() + self.cooldown_duration

            self.logger.warning(
                f"Applying emergency cooldown for {self.cooldown_duration}s. "
                f"Consecutive timeouts: {self.error_tracking['consecutive_timeouts']}, "
                f"consecutive errors: {self.error_tracking['consecutive_errors']}"
            )

            await asyncio.sleep(self.cooldown_duration)

            self.error_tracking['consecutive_timeouts'] = 0
            self.error_tracking['consecutive_errors'] = 0
            self.cooldown_active = False

            self.logger.info("Emergency cooldown completed")

    @monitor_performance
    async def login(self) -> bool:
        current_time = time.time()

        if self._is_authenticated and (current_time - self._auth_timestamp) < self._auth_ttl:
            return True

        if (current_time - self._last_login_check) < self._login_check_interval:
            return self._is_authenticated

        try:
            async with asyncio.timeout(self._operation_timeout):
                async with self._global_login_lock:
                    current_time = time.time()
                    if self._is_authenticated and (current_time - self._auth_timestamp) < self._auth_ttl:
                        return True

                    self._last_login_check = current_time

                    if self.admin_panel._is_authenticated:
                        time_since_auth = current_time - self.admin_panel._auth_token_timestamp
                        if time_since_auth < self._auth_ttl:
                            self._is_authenticated = True
                            self._auth_timestamp = self.admin_panel._auth_token_timestamp
                            return True

                    if self.logger.isEnabledFor(logging.INFO):
                        self.logger.info("Attempting AdminPanel login via AdminService")

                    start_time = time.time()
                    result = await self.admin_panel.login()
                    elapsed = time.time() - start_time
                    self.perf_tracker.record("admin_panel_login", elapsed)

                    if result:
                        self._is_authenticated = True
                        self._auth_timestamp = time.time()
                        self.error_tracking['last_success_time'] = time.time()
                        if self.logger.isEnabledFor(logging.INFO):
                            self.logger.info(f"AdminPanel login successful in {elapsed:.2f}s")
                    else:
                        self._is_authenticated = False
                        if self.logger.isEnabledFor(logging.ERROR):
                            self.logger.error(f"AdminPanel login failed after {elapsed:.2f}s")

                    return result

        except asyncio.TimeoutError:
            self.logger.error(f"Login attempt timed out after {self._operation_timeout} seconds")
            return False
        except Exception as e:
            self.logger.error(f"Unexpected error during login: {e}", exc_info=True)
            return False

    def _make_cache_key(self, func: Callable, args: Tuple[Any, ...], kwargs: Dict[str, Any]) -> str:
        func_name = func.__name__ if hasattr(func, '__name__') else str(func)
        if not kwargs and len(args) <= 2:
            key_parts = [func_name]
            for arg in args:
                if isinstance(arg, (str, int, float, bool, type(None))):
                    key_parts.append(str(arg))
                else:
                    key_parts.append(repr(arg)[:100])
            cache_key_str = "|".join(key_parts)
        else:
            payload = {"f": func_name, "a": args[:3], "k": kwargs}
            try:
                raw = json.dumps(payload, sort_keys=True, default=str)
            except Exception:
                raw = repr(payload)[:500]
            cache_key_str = raw
        return hashlib.md5(cache_key_str.encode('utf-8'), usedforsecurity=False).hexdigest()

    async def fetch_connections_with_cache(self, identifier: str) -> Optional[List[Dict[str, Any]]]:
        cache_key = f"connections:{identifier}"

        cached_result = self.connections_cache.get(cache_key)
        if cached_result is not None:
            return cached_result

        persistent_result = self.persistent_cache.get(cache_key, ttl=3600)
        if persistent_result is not None:
            self.connections_cache.put(cache_key, persistent_result)
            return persistent_result

        try:
            result = await self.admin_panel.fetch_connections_for_user(identifier)
            if result:
                self.connections_cache.put(cache_key, result)
                self.persistent_cache.put(cache_key, result)
            return result
        except Exception as e:
            self.logger.error(f"Error fetching connections for {identifier}: {e}")
            return None

    async def fetch_player_info_with_cache(self, user_id: str, fetch_player_details: bool = True) -> Dict[str, Any]:
        if not fetch_player_details:
            return {"ban_counts": 0, "ban_reasons": []}

        cache_key = f"player_info:{user_id}"

        cached_result = self.player_info_cache.get(cache_key)
        if cached_result is not None:
            return cached_result

        persistent_result = self.persistent_cache.get(cache_key, ttl=7200)
        if persistent_result is not None:
            self.player_info_cache.put(cache_key, persistent_result)
            return persistent_result

        try:
            result = await self.admin_panel.fetch_player_info(user_id)
            if result:
                self.player_info_cache.put(cache_key, result)
                self.persistent_cache.put(cache_key, result)
            return result
        except Exception as e:
            self.logger.error(f"Error fetching player info for {user_id}: {e}")
            return {"ban_counts": 0, "ban_reasons": []}

    @monitor_performance
    async def fetch_with_rate_limit(self, func: Callable, *args, **kwargs) -> Any:
        cache_key = self._make_cache_key(func, args, kwargs)
        func_name = func.__name__ if hasattr(func, '__name__') else str(func)

        if self._should_apply_cooldown():
            await self._apply_emergency_cooldown()

        async def factory_coro():
            if self.logger.isEnabledFor(logging.DEBUG):
                self.logger.debug(f"Cache miss for {func_name}. Applying load controls.")

            await self._optimizer.wait_adaptive_delay()

            if not await self.login():
                self.logger.error(f"Authentication failed for {func_name}")
                raise Exception(f"Authentication failed, cannot execute {func_name}")

            op_start_time = time.time()
            success = False
            result = None

            try:
                async with self._optimizer.concurrency_semaphore:
                    timeout = self._search_timeout if 'search' in func_name.lower() else self._request_timeout
                    async with asyncio.timeout(timeout):
                        async with self.rate_limiter:
                            result = await func(*args, **kwargs)
                            success = True
                            return result

            except asyncio.TimeoutError:
                self.error_tracking['consecutive_timeouts'] += 1
                self.error_tracking['timeout_requests'] += 1
                self.logger.error(f"Operation {func_name} timed out after {timeout}s")
                raise
            except Exception as e:
                self.error_tracking['consecutive_errors'] += 1
                self.error_tracking['error_requests'] += 1
                self.logger.error(f"Error in {func_name}: {e}")
                raise
            finally:
                op_elapsed_time = time.time() - op_start_time
                self.error_tracking['total_requests'] += 1

                if success:
                    self.error_tracking['consecutive_timeouts'] = 0
                    self.error_tracking['consecutive_errors'] = 0
                    self.error_tracking['last_success_time'] = time.time()
                    self.error_tracking['successful_requests'] += 1

                await self._optimizer.record_latency(op_elapsed_time, success=success)
                self.perf_tracker.record(func_name, op_elapsed_time)

                if op_elapsed_time > self.slow_operation_threshold:
                    if self.perf_logger.isEnabledFor(logging.WARNING):
                        self.perf_logger.warning(
                            f"Slow operation: {func_name} took {op_elapsed_time:.2f}s"
                        )

        try:
            result = await self.cache.get(cache_key, factory_coro)
        except Exception as e:
            self.logger.error(f"Failed to get result for {func_name}: {e}")
            return None

        if self.perf_tracker.should_log_summary():
            summary_lines = self.perf_tracker.get_summary()
            for line in summary_lines:
                if self.perf_logger.isEnabledFor(logging.INFO):
                    self.perf_logger.info(line)

            optimizer_stats = self._optimizer.get_current_stats()
            self.perf_logger.info(f"Optimizer stats: {optimizer_stats}")

            total = self.error_tracking['total_requests']
            if total > 0:
                success_rate = (self.error_tracking['successful_requests'] / total) * 100
                self.perf_logger.info(
                    f"Request stats: {total} total, {success_rate:.1f}% success, "
                    f"{self.error_tracking['timeout_requests']} timeouts, "
                    f"{self.error_tracking['error_requests']} errors"
                )

        if len(self._search_cache) > self._search_cache_max_size * 1.5:
            self._cleanup_search_cache()

        return result

    def _cleanup_search_cache(self):
        current_time = time.time()
        keys_to_remove = []

        for key, (data, timestamp) in list(self._search_cache.items())[:200]:
            if current_time - timestamp > self._search_cache_ttl:
                keys_to_remove.append(key)

        for key in keys_to_remove:
            del self._search_cache[key]

        while len(self._search_cache) > self._search_cache_max_size:
            self._search_cache.popitem(last=False)

        if keys_to_remove and self.logger.isEnabledFor(logging.DEBUG):
            self.logger.debug(f"Cleaned up {len(keys_to_remove)} old entries from search cache")

    def _is_recent(self, time_str: str, days: int = 7) -> bool:
        if not time_str or time_str == N_A:
            return False
        try:
            conn_time = datetime.strptime(time_str, "%m/%d/%Y %I:%M:%S %p")
            if datetime.now() - conn_time < timedelta(days=days):
                return True
        except ValueError:
            if self.logger.isEnabledFor(logging.DEBUG):
                self.logger.debug(f"Could not parse time_str '{time_str}' for recency check.")
            return False
        return False

    def _has_information_gain(self, new_data: Dict[str, Any], existing_sets: Dict[str, Set[str]]) -> bool:
        if not new_data:
            return False

        new_nicknames = set(new_data.get('nicknames', []))
        new_ips = set(new_data.get('associated_ips', {}).keys())
        new_hwids = set(new_data.get('associated_hwids', {}).keys())

        nickname_gain = len(new_nicknames - existing_sets.get('nicknames', set())) > 0
        ip_gain = len(new_ips - existing_sets.get('ips', set())) > 0
        hwid_gain = len(new_hwids - existing_sets.get('hwids', set())) > 0

        existing_sets.setdefault('nicknames', set()).update(new_nicknames)
        existing_sets.setdefault('ips', set()).update(new_ips)
        existing_sets.setdefault('hwids', set()).update(new_hwids)

        return nickname_gain or ip_gain or hwid_gain

    @monitor_performance
    async def search_player(self, term: str, single_user: bool = True, max_depth: Optional[int] = None,
                            early_stop: bool = False) -> Optional[Dict[str, Any]]:
        try:
            async with asyncio.timeout(self._search_timeout):
                return await self._search_player_internal(term, single_user, max_depth, early_stop)
        except asyncio.TimeoutError:
            self.logger.error(f"Player search for '{term[:50]}' timed out after {self._search_timeout}s")
            return None
        except Exception as e:
            self.logger.error(f"Error in search_player for '{term[:50]}': {e}", exc_info=True)
            return None

    async def _search_player_internal(self, term: str, single_user: bool = True, max_depth: Optional[int] = None,
                                      early_stop: bool = False) -> Optional[Dict[str, Any]]:
        cfg = get_config()
        effective_max_depth = max_depth if max_depth is not None else getattr(cfg.scan, 'search_max_depth', 2)
        start_time_search = time.time()

        initial_term_is_likely_hwid = len(term) > 20 and any(c.islower() for c in term) and any(
            c.isupper() for c in term) and ('/' in term or '+' in term or '=' in term)

        initial_search_term_str = term
        initial_term_canonical = term.strip() if initial_term_is_likely_hwid else term.lower().strip()

        if self.logger.isEnabledFor(logging.INFO):
            self.logger.info(
                f"Initiating player search for term: '{initial_search_term_str[:50]}' "
                f"(canonical: '{initial_term_canonical[:50]}'), single_user={single_user}, max_depth={effective_max_depth}, early_stop={early_stop}"
            )

        cache_key = f"search_player_v6:{initial_term_canonical}:single_user={single_user}:max_depth={effective_max_depth}:early_stop={early_stop}"

        cached_result = self.search_results_cache.get(cache_key)
        if cached_result is not None:
            if self.logger.isEnabledFor(logging.DEBUG):
                self.logger.debug(f"Returning cached search result for '{initial_term_canonical[:50]}'")
            return cached_result

        processed_terms: Set[str] = set()
        search_queue = deque([((term.strip(), initial_term_is_likely_hwid), 0)])
        terms_in_flight: Set[str] = {initial_term_canonical}

        merged_result_data: Optional[Dict[str, Any]] = None
        search_stats = {"unique_api_calls": 0, "depth_distribution": {}, "timeouts": 0, "errors": 0}

        information_sets: Dict[str, Set[str]] = {'nicknames': set(), 'ips': set(), 'hwids': set()}
        pages_without_gain = 0
        max_pages_without_gain = 3

        while search_queue:
            batch_size = min(getattr(cfg.scan, 'search_batch_size', 2), len(search_queue))
            items_to_process_this_batch = [search_queue.popleft() for _ in range(batch_size)]

            tasks, actual_items_for_api = [], []
            for (term_str_to_process, term_is_hwid_flag), depth in items_to_process_this_batch:
                canonical_term_to_process = term_str_to_process if term_is_hwid_flag else term_str_to_process.lower()
                if canonical_term_to_process in processed_terms:
                    continue

                tasks.append(self._process_search_term_enhanced(
                    term_str_to_process, term_is_hwid_flag, depth, single_user, effective_max_depth,
                    processed_terms, terms_in_flight
                ))
                actual_items_for_api.append(
                    ((term_str_to_process, term_is_hwid_flag), depth, canonical_term_to_process))

            if not tasks:
                continue

            if search_stats["unique_api_calls"] > 0:
                await asyncio.sleep(1.0)

            try:
                async with asyncio.timeout(self._search_timeout // 2):
                    results_from_batch = await asyncio.gather(*tasks, return_exceptions=True)
            except asyncio.TimeoutError:
                search_stats["timeouts"] += 1
                self.logger.error(f"Search batch processing timed out for term '{term[:50]}'")
                break

            batch_had_gain = False
            for i, res_or_exc in enumerate(results_from_batch):
                (_original_term_tuple, original_depth, canonical_original_term) = actual_items_for_api[i]
                original_term_str, _ = _original_term_tuple

                if canonical_original_term not in processed_terms:
                    processed_terms.add(canonical_original_term)
                    search_stats["unique_api_calls"] += 1
                    search_stats["depth_distribution"][original_depth] = search_stats["depth_distribution"].get(
                        original_depth, 0) + 1

                if isinstance(res_or_exc, Exception):
                    search_stats["errors"] += 1
                    if self.logger.isEnabledFor(logging.ERROR):
                        self.logger.error(f"Error processing search term '{original_term_str[:50]}': {res_or_exc}",
                                          exc_info=False)
                    continue

                individual_result: Optional[Dict[str, Any]] = res_or_exc
                if not individual_result or not individual_result.get('result_data'):
                    continue

                if early_stop:
                    has_gain = self._has_information_gain(individual_result['result_data'], information_sets)
                    if has_gain:
                        batch_had_gain = True

                if merged_result_data is None:
                    merged_result_data = individual_result['result_data']
                else:
                    self._merge_search_results(merged_result_data, individual_result['result_data'])

                expansion_limit = self._get_search_limit_for_depth(original_depth)
                new_terms = individual_result.get('new_terms_to_search', [])[:expansion_limit]

                for new_term_str, new_term_is_hwid_flag in new_terms:
                    canonical_new_term = new_term_str if new_term_is_hwid_flag else new_term_str.lower()
                    if canonical_new_term not in terms_in_flight and len(search_queue) < 20:
                        search_queue.append(((new_term_str, new_term_is_hwid_flag), original_depth + 1))
                        terms_in_flight.add(canonical_new_term)

            if early_stop:
                if batch_had_gain:
                    pages_without_gain = 0
                else:
                    pages_without_gain += 1

                if pages_without_gain >= max_pages_without_gain:
                    self.logger.info(f"Early stopping: {pages_without_gain} pages without information gain")
                    break

        search_elapsed_time = time.time() - start_time_search
        if self.logger.isEnabledFor(logging.INFO):
            self.logger.info(
                f"Search for '{initial_search_term_str[:50]}' completed in {search_elapsed_time:.2f}s. "
                f"API Calls={search_stats['unique_api_calls']}, Depth Dist={search_stats['depth_distribution']}, "
                f"Timeouts={search_stats['timeouts']}, Errors={search_stats['errors']}, "
                f"Early Stop={'Yes' if early_stop and pages_without_gain >= max_pages_without_gain else 'No'}"
            )

        if merged_result_data:
            self.search_results_cache.put(cache_key, merged_result_data)

        return merged_result_data

    async def _process_search_term_enhanced(
            self, current_term_str: str, current_term_is_hwid: bool, current_depth: int,
            single_user_mode: bool, max_search_depth: int,
            glob_processed_terms: Set[str], glob_terms_in_flight: Set[str]
    ) -> Optional[Dict[str, Any]]:

        canonical_term_for_url = current_term_str
        if not current_term_is_hwid:
            canonical_term_for_url = current_term_str.lower()

        if self.logger.isEnabledFor(logging.DEBUG):
            self.logger.debug(
                f"Processing search for term: '{current_term_str[:50]}' at depth {current_depth}"
            )

        try:
            async with asyncio.timeout(self._request_timeout):
                if canonical_term_for_url.replace('-', '').replace('.', '').replace('_', '').isalnum():
                    connections_search_url = f"{self.base_admin_connections_url}&search={canonical_term_for_url}"
                else:
                    encoded_term = quote_plus(canonical_term_for_url)
                    connections_search_url = f"{self.base_admin_connections_url}&search={encoded_term}"

                term_data = await self.fetch_with_rate_limit(
                    self.admin_panel.check_account_on_site,
                    connections_search_url,
                    single_user=single_user_mode
                )

                if not term_data or (isinstance(term_data, list) and not term_data):
                    if self.logger.isEnabledFor(logging.DEBUG):
                        self.logger.debug(f"No data returned for term '{current_term_str[:50]}'.")
                    return None

                aggregated_data_for_term = term_data
                new_terms_to_queue: List[Tuple[str, bool]] = []

                if current_depth < max_search_depth and current_depth < 2:
                    if isinstance(aggregated_data_for_term, dict):
                        all_connections_recent = False
                        if "raw_html_snippet" in aggregated_data_for_term and aggregated_data_for_term[
                            "raw_html_snippet"]:
                            all_connections_recent = all(
                                self._is_recent(conn_prev.get("time"), days=7)
                                for conn_prev in aggregated_data_for_term["raw_html_snippet"] if conn_prev.get("time")
                            ) if aggregated_data_for_term["raw_html_snippet"] else False

                        if all_connections_recent and aggregated_data_for_term["raw_html_snippet"]:
                            if self.logger.isEnabledFor(logging.DEBUG):
                                self.logger.debug(
                                    f"Term '{current_term_str[:50]}' data appears very recent. Suppressing further expansion.")
                        else:
                            extracted_identifiers_with_type = self._extract_prioritized_identifiers(
                                aggregated_data_for_term, current_term_str, current_term_is_hwid,
                                glob_processed_terms, glob_terms_in_flight
                            )
                            search_limit_for_depth = self._get_search_limit_for_depth(current_depth)

                            optimizer_stats = self._optimizer.get_current_stats()
                            if optimizer_stats['average_latency'] > 20:
                                search_limit_for_depth = max(1, search_limit_for_depth - 1)

                            new_terms_to_queue.extend(extracted_identifiers_with_type[:search_limit_for_depth])
                            if new_terms_to_queue and self.logger.isEnabledFor(logging.DEBUG):
                                self.logger.debug(
                                    f"Identified {len(new_terms_to_queue)} new terms from '{current_term_str[:50]}' "
                                    f"for depth {current_depth + 1}.")
                    elif self.logger.isEnabledFor(logging.DEBUG):
                        self.logger.debug(
                            f"Data for '{current_term_str[:50]}' (type: {type(aggregated_data_for_term)}) not dict, "
                            f"cannot extract new ids.")

                return {'result_data': aggregated_data_for_term, 'new_terms_to_search': new_terms_to_queue}

        except asyncio.TimeoutError:
            self.logger.error(
                f"Search term processing timed out for '{current_term_str[:50]}' at depth {current_depth}")
            return None
        except Exception as e:
            if self.logger.isEnabledFor(logging.ERROR):
                self.logger.error(
                    f"Error processing search term '{current_term_str[:50]}' at depth {current_depth}: {e}",
                    exc_info=False)
            return None

    def _extract_prioritized_identifiers(
            self, result_dict: Dict[str, Any],
            origin_term_str: str, origin_term_is_hwid: bool,
            glob_processed_terms: Set[str], glob_terms_in_flight: Set[str]
    ) -> List[Tuple[str, bool]]:

        potential_new_ids: List[Tuple[int, str, bool]] = []

        def add_if_valid(identifier: Optional[str], priority: int, term_is_hwid: bool):
            if not identifier or identifier == N_A or not isinstance(identifier, str) or not identifier.strip():
                return

            id_str_stripped = identifier.strip()

            comp_term = id_str_stripped if term_is_hwid else id_str_stripped.lower()
            origin_comp_term = origin_term_str.strip() if origin_term_is_hwid else origin_term_str.lower().strip()

            if comp_term == origin_comp_term:
                return

            if comp_term in glob_processed_terms or comp_term in glob_terms_in_flight:
                return

            if priority == 2:
                is_private = False
                try:
                    if (id_str_stripped.startswith("192.168.") or
                            id_str_stripped.startswith("10.") or
                            (id_str_stripped.startswith("172.") and 16 <= int(id_str_stripped.split('.')[1]) <= 31)):
                        is_private = True
                except (ValueError, IndexError):
                    pass
                if is_private:
                    if self.logger.isEnabledFor(logging.DEBUG):
                        self.logger.debug(f"Skipping private IP for expansion: {id_str_stripped}")
                    return

            potential_new_ids.append((priority, id_str_stripped, term_is_hwid))

        add_if_valid(result_dict.get("user_id"), 0, False)
        for hwid_val in result_dict.get("associated_hwids", {}).keys():
            add_if_valid(hwid_val, 1, True)
        for ip_val in result_dict.get("associated_ips", {}).keys():
            add_if_valid(ip_val, 2, False)
        for nickname_val in result_dict.get("nicknames", []):
            add_if_valid(nickname_val, 3, False)

        potential_new_ids.sort(key=lambda x: (x[0], x[1]))

        return [(id_str, is_hwid) for _, id_str, is_hwid in potential_new_ids]

    def _get_search_limit_for_depth(self, depth: int) -> int:
        cfg = get_config()
        if depth == 0:
            return getattr(cfg.scan, 'search_limit_root', 5)
        if depth == 1:
            return getattr(cfg.scan, 'search_limit_level1', 3)
        if depth == 2:
            return getattr(cfg.scan, 'search_limit_level2', 2)
        return getattr(cfg.scan, 'search_limit_default', 1)

    def _merge_search_results(self, main_result: Dict[str, Any], new_data: Dict[str, Any]) -> None:
        if not isinstance(main_result, dict) or not isinstance(new_data, dict):
            if self.logger.isEnabledFor(logging.WARNING):
                self.logger.warning(
                    f"Attempted to merge non-dict results. Main: {type(main_result)}, New: {type(new_data)}")
            return

        main_nicks = set(main_result.get("nicknames", []))
        main_nicks.update(new_data.get("nicknames", []))
        main_result["nicknames"] = sorted(main_nicks)

        main_shared = set(main_result.get("shared_hwid_nicknames", []))
        main_shared.update(new_data.get("shared_hwid_nicknames", []))
        main_result["shared_hwid_nicknames"] = sorted(main_shared)

        def br_key(br):
            return frozenset(br.items())

        merged_brs = {br_key(br): br for br in main_result.get("ban_reasons", [])}
        for br_new in new_data.get("ban_reasons", []):
            merged_brs.setdefault(br_key(br_new), br_new)
        main_result["ban_reasons"] = sorted(list(merged_brs.values()),
                                            key=lambda x: (x.get("username", ""), x.get("reason", "")))

        def dc_key(dc):
            return frozenset(dc.items())

        merged_dcs = {dc_key(dc): dc for dc in main_result.get("denied_banned_connections", [])}
        for dc_new in new_data.get("denied_banned_connections", []):
            merged_dcs.setdefault(dc_key(dc_new), dc_new)
        main_result["denied_banned_connections"] = sorted(list(merged_dcs.values()), key=lambda x: x.get("time", ""))

        assoc_ips = main_result.get("associated_ips", {})
        for ip, nicks in new_data.get("associated_ips", {}).items():
            if ip in assoc_ips:
                existing = set(assoc_ips[ip])
                existing.update(nicks)
                assoc_ips[ip] = sorted(existing)
            else:
                assoc_ips[ip] = sorted(nicks) if not isinstance(nicks, list) or nicks != sorted(nicks) else nicks
        main_result["associated_ips"] = assoc_ips

        assoc_hwids = main_result.get("associated_hwids", {})
        for hwid, nicks in new_data.get("associated_hwids", {}).items():
            if hwid in assoc_hwids:
                existing_nicks = set(assoc_hwids[hwid])
                existing_nicks.update(nicks)
                assoc_hwids[hwid] = sorted(existing_nicks)
            else:
                assoc_hwids[hwid] = sorted(nicks) if not isinstance(nicks, list) or nicks != sorted(nicks) else nicks
        main_result["associated_hwids"] = assoc_hwids

        main_result["ban_counts"] = max(main_result.get("ban_counts", 0), new_data.get("ban_counts", 0))

        s_pri = {'suspicious': 4, 'banned': 3, 'clean': 1, 'unknown': 0, N_A: 0}
        cur_s, new_s = str(main_result.get("status", "u")).lower(), str(new_data.get("status", "u")).lower()
        if s_pri.get(new_s, 0) > s_pri.get(cur_s, 0):
            main_result["status"] = new_data.get("status")

        if main_result.get("user_id", N_A) == N_A and new_data.get("user_id", N_A) != N_A:
            main_result["user_id"] = new_data.get("user_id")
        if main_result.get("connection_link", N_A) == N_A and new_data.get("connection_link", N_A) != N_A:
            main_result["connection_link"] = new_data.get("connection_link")
        main_result["hwid_erased"] = bool(main_result.get("hwid_erased", False) or new_data.get("hwid_erased", False))
        if not main_result.get("raw_html_snippet") and new_data.get("raw_html_snippet"):
            main_result["raw_html_snippet"] = new_data.get("raw_html_snippet")

    def convert_to_player(self, account_info_dict: Optional[Dict[str, Any]]) -> Player:
        if not account_info_dict or not isinstance(account_info_dict, dict):
            if self.logger.isEnabledFor(logging.WARNING):
                self.logger.warning("Empty/None/non-dict account_info for convert_to_player. Default Player returned.")
            return Player(user_id=N_A, nicknames=[], status="unknown")

        p_uid = str(account_info_dict.get("user_id", N_A))
        nicks_list = account_info_dict.get("nicknames", [])
        p_nicks = nicks_list if isinstance(nicks_list, list) else []
        p_status = str(account_info_dict.get("status", "unknown"))
        p_ban_c = int(bc) if isinstance((bc := account_info_dict.get("ban_counts", 0)), (int, float)) else 0

        fmt_brs: List[Dict[str, str]] = []
        for r_entry in (raw_brs if isinstance((raw_brs := account_info_dict.get("ban_reasons", [])), list) else []):
            if isinstance(r_entry, dict) and "reason" in r_entry and "username" in r_entry:
                fmt_brs.append({
                    "reason": str(r_entry["reason"]), "username": str(r_entry["username"]),
                    "admin": str(r_entry.get("admin", "N/A")),
                    "type": str(r_entry.get("type", "N/A")),
                    "date": str(r_entry.get("date", "N/A")),
                    "expires": str(r_entry.get("expires", "Никогда")),
                })

        p_conn_link = str(account_info_dict.get("connection_link", N_A))
        p_assoc_ips = ips if isinstance((ips := account_info_dict.get("associated_ips", {})), dict) else {}
        p_assoc_hwids = hwids if isinstance((hwids := account_info_dict.get("associated_hwids", {})), dict) else {}
        p_shared_hwids = sh_hwids if isinstance((sh_hwids := account_info_dict.get("shared_hwid_nicknames", [])),
                                                list) else []
        p_denied_logins = d_logins if isinstance((d_logins := account_info_dict.get("denied_banned_connections", [])),
                                                 list) else []
        p_hwid_erased = bool(account_info_dict.get("hwid_erased", False))

        return Player(user_id=p_uid, nicknames=p_nicks, status=p_status, ban_counts=p_ban_c,
                      ban_reasons=fmt_brs, connection_link=p_conn_link, associated_ips=p_assoc_ips,
                      associated_hwids=p_assoc_hwids, shared_hwid_nicknames=p_shared_hwids,
                      denied_logins=p_denied_logins, hwid_erased=p_hwid_erased)