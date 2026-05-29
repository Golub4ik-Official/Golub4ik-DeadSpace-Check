import asyncio
import functools
import hashlib
import heapq
import logging
import re
import time
from collections import defaultdict
from datetime import datetime
from typing import List, Dict, Any, Optional, Set

from config_system import get_config
from core.analyzer import PlayerAnalyzer
from models.message import ScanResult
from models.player import Player
from services.admin_service import AdminService
from services.cache_service import CacheService
from services.discord_service import DiscordService
from services.reporting import ReportService
from utils.async_utils import gather_with_concurrency
from utils.discord_utils import extract_message_id
from utils.performance_monitor import PerformanceTracker, monitor_performance
from utils.url_utils import extract_effective_search_term


def cached(ttl=300):
    def decorator(func):
        cache = {}

        @functools.wraps(func)
        async def wrapper(self, *args, **kwargs):
            cache_key = str(args[0]) if args else "default"
            if cache_key in cache:
                timestamp, value = cache[cache_key]
                if time.time() - timestamp < ttl:
                    return value
            result = await func(self, *args, **kwargs)
            cache[cache_key] = (time.time(), result)
            return result

        return wrapper

    return decorator


class CircuitBreaker:

    def __init__(self, failure_threshold=10, recovery_timeout=60, half_open_max_calls=5):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.half_open_max_calls = half_open_max_calls

        self.failure_count = 0
        self.last_failure_time = None
        self.state = 'CLOSED'
        self.half_open_calls = 0

    def call_succeeded(self):
        self.failure_count = 0
        if self.state == 'HALF_OPEN':
            self.state = 'CLOSED'
        self.half_open_calls = 0

    def call_failed(self):
        self.failure_count += 1
        self.last_failure_time = time.time()

        if self.failure_count >= self.failure_threshold:
            self.state = 'OPEN'

    def can_execute(self):
        if self.state == 'CLOSED':
            return True

        if self.state == 'OPEN':
            if time.time() - self.last_failure_time > self.recovery_timeout:
                self.state = 'HALF_OPEN'
                self.half_open_calls = 0
                return True
            return False

        if self.state == 'HALF_OPEN':
            if self.half_open_calls < self.half_open_max_calls:
                self.half_open_calls += 1
                return True
            return False

        return False


class ExponentialBackoff:

    def __init__(self, initial_delay=1.0, max_delay=60.0, multiplier=2.0, jitter=True):
        self.initial_delay = initial_delay
        self.max_delay = max_delay
        self.multiplier = multiplier
        self.jitter = jitter
        self.current_delay = initial_delay

    def get_delay(self):
        delay = min(self.current_delay, self.max_delay)
        if self.jitter:
            import random
            delay = delay * (0.5 + random.random() * 0.5)

        self.current_delay *= self.multiplier
        return delay

    def reset(self):
        self.current_delay = self.initial_delay


class Scanner:
    def __init__(self, discord_service: DiscordService, admin_service: AdminService,
                 cache_service: CacheService, report_service: ReportService,
                 player_analyzer: PlayerAnalyzer,
                 progress_queue=None) -> None:
        self.discord = discord_service
        self.admin = admin_service
        self.admin_panel = admin_service.admin_panel
        self.cache = cache_service
        self.report = report_service
        self.analyzer = player_analyzer
        self.cfg = get_config()
        self.max_concurrent = self.cfg.api.max_concurrent_requests
        self.complaint_channels = {}
        self.cache_data = {
            "connections": {},
            "ban_info": {},
            "players": {},
        }
        self.identity_graph = defaultdict(set)
        self.logger = logging.getLogger(__name__)
        self.perf = PerformanceTracker()
        self.status_priority = {
            'banned': 3,
            'suspicious': 2,
            'clean': 1,
            'unknown': 0
        }
        self.connections_cache = {}

        self._operation_timeout = self.cfg.api.operation_timeout
        self._term_timeout = self.cfg.api.term_timeout
        self._batch_timeout = self.cfg.api.batch_timeout
        self._set_progress_queue(progress_queue)

        self.circuit_breaker = CircuitBreaker(
            failure_threshold=self.cfg.api.circuit_breaker.failure_threshold,
            recovery_timeout=self.cfg.api.circuit_breaker.recovery_timeout,
            half_open_max_calls=self.cfg.api.circuit_breaker.half_open_max_calls
        )

        self.backoff = ExponentialBackoff(
            initial_delay=self.cfg.api.backoff.initial_delay,
            max_delay=self.cfg.api.backoff.max_delay,
            multiplier=self.cfg.api.backoff.multiplier,
            jitter=self.cfg.api.backoff.jitter
        )

    def _set_progress_queue(self, q):
        self.progress_queue = q

    def _report_progress(self, current, total, msg=""):
        if self.progress_queue is not None:
            try:
                self.progress_queue.put_nowait({"type": "progress", "current": current, "total": total, "msg": msg})
            except Exception:
                pass

    def _report_log(self, text):
        if self.progress_queue is not None:
            try:
                self.progress_queue.put_nowait({"type": "log", "text": text})
            except Exception:
                pass

        self._conservative_batch_size = self.cfg.scan.batch_processing.conservative_batch_size
        self._aggressive_batch_size = self.cfg.scan.batch_processing.aggressive_batch_size
        self._batch_delay_base = self.cfg.scan.batch_processing.batch_delay_base
        self._max_terms_per_scan = self.cfg.scan.max_terms_per_scan

        self.error_stats = {
            'timeouts': 0,
            'circuit_breaker_trips': 0,
            'successful_requests': 0,
            'failed_requests': 0,
            'retries': 0
        }

        if self.logger.isEnabledFor(logging.INFO):
            self.logger.info(
                f"Scanner initialized with settings: "
                f"max_concurrent={self.max_concurrent}, "
                f"batch_sizes={self._conservative_batch_size}-{self._aggressive_batch_size}, "
                f"batch_delay={self._batch_delay_base}s, "
                f"max_terms={self._max_terms_per_scan}"
            )

    async def setup(self, target_channel_id: int, complaint_channel_ids: List[int]) -> bool:
        self.logger.info("Setting up scanner...")
        if not await self.discord.setup_channels(target_channel_id, complaint_channel_ids):
            return False
        if not await self.admin.login():
            self.logger.error("Failed to log in to the admin panel")
            return False
        self.complaint_channels = self.cache.load_complaint_cache()
        self.logger.info("Scanner setup complete")
        return True

    def _should_limit_processing(self, total_terms: int) -> tuple[bool, int]:
        if total_terms <= self._max_terms_per_scan:
            return False, total_terms

        self.logger.warning(
            f"Found {total_terms} terms, which exceeds limit of {self._max_terms_per_scan}. "
            f"Will process first {self._max_terms_per_scan} terms."
        )
        return True, self._max_terms_per_scan

    def _get_adaptive_batch_size(self) -> int:
        if self.circuit_breaker.state == 'OPEN':
            return 1
        elif self.circuit_breaker.state == 'HALF_OPEN':
            return max(2, self._conservative_batch_size // 2)
        elif self.circuit_breaker.failure_count > 5:
            return self._conservative_batch_size
        else:
            total_requests = self.error_stats['successful_requests'] + self.error_stats['failed_requests']
            if total_requests > 10:
                success_rate = self.error_stats['successful_requests'] / total_requests
                if success_rate > 0.9:
                    return self._aggressive_batch_size
                elif success_rate > 0.7:
                    return (self._conservative_batch_size + self._aggressive_batch_size) // 2

            return self._conservative_batch_size

    def _get_adaptive_delay(self) -> float:
        base_delay = self._batch_delay_base

        if self.circuit_breaker.state == 'OPEN':
            return base_delay * 10
        elif self.circuit_breaker.state == 'HALF_OPEN':
            return base_delay * 3
        elif self.circuit_breaker.failure_count > 0:
            return base_delay * (1 + self.circuit_breaker.failure_count * 0.5)

        return base_delay

    @monitor_performance()
    async def scan_messages(self, message_limit: int) -> List[Dict[str, Any]]:
        start_time = datetime.now()
        self.logger.info(f"Starting message scan with limit {message_limit}")
        processed_terms = set()

        try:
            self.error_stats = {k: 0 for k in self.error_stats}

            self._report_log("Начинаю загрузку данных о наказаниях... (10–15 минут)\n")

            def _cache_progress(ch_idx, total_ch, fetched, total_to_fetch, msg):
                self._report_log(f"  {msg}\n")
                self._report_progress(fetched, total_to_fetch, msg)

            self.complaint_channels = await self.discord.update_complaint_cache(
                self.complaint_channels,
                history_limit=self.cfg.discord.message_history_limit,
                progress_callback=_cache_progress
            )

            self._report_log("Загрузка данных о наказаниях завершена.\n")
            messages = await self.discord.scan_target_channel(
                message_limit,
                lambda m: any(embed.title == 'Arrived new player' for embed in m.embeds)
            )

            if not messages:
                self.logger.info("No matching messages found")
                return []

            self.logger.info(f"Found {len(messages)} messages to process")
            message_data = self._extract_message_data(messages)
            all_terms = message_data['all_terms']

            should_limit, terms_to_process = self._should_limit_processing(len(all_terms))
            if should_limit:
                all_terms = list(all_terms)[:terms_to_process]

            self.logger.info(f"Processing {len(all_terms)} unique terms")

            term_results = await self._process_all_terms_enhanced(
                all_terms,
                message_data['term_is_login_event'],
                message_data['user_id_terms'],
                processed_terms,
                message_data
            )

            scan_results = await self._create_scan_results(
                messages,
                message_data,
                term_results
            )

            consolidated_results = self._consolidate_results(scan_results)
            report_data = self.report.generate_message_scan_report(consolidated_results)

            duration = (datetime.now() - start_time).total_seconds()
            hit_rate = (len(consolidated_results) / len(messages)) * 100 if messages else 0

            self.logger.info(
                f"Message scan completed in {duration:.2f}s: processed {len(messages)} messages, "
                f"found {len(consolidated_results)} results ({hit_rate:.1f}% hit rate)"
            )

            self._log_error_statistics()

            self.perf.log_summary_if_needed()
            return report_data

        except Exception as e:
            self.logger.error(f"Error during message scan: {str(e)}", exc_info=True)
            return []
        finally:
            self._report_log("Сохраняю данные в базу данных...\n")
            self.cache.save_complaint_cache(self.complaint_channels)
            self._report_log("Сохранение завершено.\n")

    async def _process_all_terms_enhanced(self, all_terms, term_is_login_event, user_id_terms,
                                          processed_terms, message_data):
        if not all_terms:
            return {}

        high_priority_terms = []
        normal_priority_terms = []

        for term in all_terms:
            if term in user_id_terms.values() or term_is_login_event.get(term, False):
                high_priority_terms.append(term)
            else:
                normal_priority_terms.append(term)

        all_priority_terms = high_priority_terms + normal_priority_terms

        if not all_priority_terms:
            return {}

        self.logger.info(f"Processing {len(all_priority_terms)} terms with enhanced batching")

        term_results = {}
        message_nicknames = message_data.get('message_nicknames', {})
        term_to_message_id = message_data.get('term_to_message_id', {})
        cache_lock = asyncio.Lock()

        batch_number = 0
        successful_batches = 0
        failed_batches = 0

        i = 0
        while i < len(all_priority_terms):
            batch_number += 1

            if not self.circuit_breaker.can_execute():
                self.logger.warning(
                    f"Circuit breaker is OPEN. Waiting {self.circuit_breaker.recovery_timeout}s before retry..."
                )
                self.error_stats['circuit_breaker_trips'] += 1
                await asyncio.sleep(self.circuit_breaker.recovery_timeout)
                continue

            batch_size = self._get_adaptive_batch_size()
            batch_delay = self._get_adaptive_delay()

            batch_terms = all_priority_terms[i:i + batch_size]

            self.logger.info(
                f"Processing batch {batch_number} with {len(batch_terms)} terms "
                f"(batch_size={batch_size}, delay={batch_delay:.1f}s, "
                f"circuit_state={self.circuit_breaker.state})"
            )

            delay_task = asyncio.create_task(asyncio.sleep(batch_delay)) if i + batch_size < len(
                all_priority_terms) else None

            try:
                batch_results = await self._process_batch_with_retry(
                    batch_terms, cache_lock, processed_terms, term_is_login_event,
                    user_id_terms, message_nicknames, term_to_message_id
                )

                for term, result in zip(batch_terms, batch_results):
                    if result:
                        term_results[term] = result

                successful_batches += 1
                self.circuit_breaker.call_succeeded()
                self.backoff.reset()

                i += batch_size

                progress_pct = (i / len(all_priority_terms)) * 100
                self.logger.info(
                    f"Completed batch {batch_number}. Progress: {i}/{len(all_priority_terms)} "
                    f"({progress_pct:.1f}%). Success rate: "
                    f"{successful_batches}/{successful_batches + failed_batches}"
                )

                self._report_progress(i, len(all_priority_terms), f"Batch {batch_number} ({progress_pct:.0f}%)")

                if delay_task:
                    await delay_task

            except Exception as e:
                failed_batches += 1
                self.circuit_breaker.call_failed()
                self.error_stats['failed_requests'] += len(batch_terms)

                self.logger.error(f"Batch {batch_number} failed: {e}")

                backoff_delay = self.backoff.get_delay()
                self.logger.info(f"Applying backoff delay: {backoff_delay:.1f}s")
                await asyncio.sleep(backoff_delay)

                i += batch_size

        self.logger.info(
            f"Term processing completed. Processed {len(term_results)} successful terms. "
            f"Successful batches: {successful_batches}, Failed batches: {failed_batches}"
        )

        return term_results

    async def _process_batch_with_retry(self, batch_terms, cache_lock, processed_terms,
                                        term_is_login_event, user_id_terms, message_nicknames,
                                        term_to_message_id, max_retries=2):

        for attempt in range(max_retries + 1):
            try:
                async with asyncio.timeout(self._batch_timeout):
                    term_tasks = []

                    for term in batch_terms:
                        async with cache_lock:
                            if term in processed_terms:
                                continue
                            processed_terms.add(term)

                        message_id = term_to_message_id.get(term)
                        nickname = message_nicknames.get(message_id) if message_id else None

                        term_tasks.append(
                            self._process_term_with_enhanced_timeout(
                                term,
                                use_cache=True,
                                shared_cache=None,
                                cache_lock=None,
                                is_login_event=term_is_login_event.get(term, False),
                                is_user_id=(term in user_id_terms.values()),
                                message_nickname=nickname
                            )
                        )

                    if term_tasks:
                        batch_results = await asyncio.gather(*term_tasks, return_exceptions=True)

                        processed_results = []
                        for i, result in enumerate(batch_results):
                            if isinstance(result, Exception):
                                self.logger.warning(f"Term '{batch_terms[i][:50]}' failed: {result}")
                                self.error_stats['failed_requests'] += 1
                                processed_results.append(None)
                            else:
                                if result:
                                    self.error_stats['successful_requests'] += 1
                                processed_results.append(result)

                        return processed_results

                    return []

            except asyncio.TimeoutError:
                self.error_stats['timeouts'] += 1
                if attempt < max_retries:
                    self.error_stats['retries'] += 1
                    retry_delay = (attempt + 1) * 5
                    self.logger.warning(
                        f"Batch timeout on attempt {attempt + 1}/{max_retries + 1}. "
                        f"Retrying in {retry_delay}s..."
                    )
                    await asyncio.sleep(retry_delay)
                else:
                    self.logger.error(f"Batch timed out after {max_retries + 1} attempts")
                    raise

            except Exception as e:
                if attempt < max_retries:
                    self.error_stats['retries'] += 1
                    retry_delay = (attempt + 1) * 3
                    self.logger.warning(
                        f"Batch error on attempt {attempt + 1}/{max_retries + 1}: {e}. "
                        f"Retrying in {retry_delay}s..."
                    )
                    await asyncio.sleep(retry_delay)
                else:
                    self.logger.error(f"Batch failed after {max_retries + 1} attempts: {e}")
                    raise

    async def _process_term_with_enhanced_timeout(self, term: str, **kwargs) -> Optional[Player]:
        start_time = time.time()

        try:
            async with asyncio.timeout(self._term_timeout):
                result = await self.process_term(term, **kwargs)

                elapsed = time.time() - start_time
                if elapsed > self._term_timeout * 0.8:
                    self.logger.warning(
                        f"Term '{term[:50]}' took {elapsed:.1f}s (close to timeout of {self._term_timeout}s)"
                    )

                return result

        except asyncio.TimeoutError:
            elapsed = time.time() - start_time
            self.error_stats['timeouts'] += 1
            self.logger.error(
                f"Term processing timed out for '{term[:50]}' after {elapsed:.1f}s "
                f"(timeout: {self._term_timeout}s)"
            )
            return None

        except Exception as e:
            elapsed = time.time() - start_time
            self.logger.error(
                f"Error in _process_term_with_enhanced_timeout for '{term[:50]}' "
                f"after {elapsed:.1f}s: {e}"
            )
            return None

    def _log_error_statistics(self):
        total_requests = self.error_stats['successful_requests'] + self.error_stats['failed_requests']
        if total_requests > 0:
            success_rate = (self.error_stats['successful_requests'] / total_requests) * 100

            self.logger.info("=== Scan Error Statistics ===")
            self.logger.info(f"Total requests: {total_requests}")
            self.logger.info(f"Successful: {self.error_stats['successful_requests']} ({success_rate:.1f}%)")
            self.logger.info(f"Failed: {self.error_stats['failed_requests']}")
            self.logger.info(f"Timeouts: {self.error_stats['timeouts']}")
            self.logger.info(f"Circuit breaker trips: {self.error_stats['circuit_breaker_trips']}")
            self.logger.info(f"Retries: {self.error_stats['retries']}")
            self.logger.info(f"Final circuit breaker state: {self.circuit_breaker.state}")
            self.logger.info("=============================")

    async def scan_message_interval(self, start_message: str, end_message: str) -> List[Dict[str, Any]]:
        start_time = datetime.now()

        start_id = extract_message_id(start_message)
        end_id = extract_message_id(end_message)

        if not start_id or not end_id:
            self.logger.error(f"Invalid message IDs: start={start_message}, end={end_message}")
            return []

        self.logger.info(f"Starting interval scan from message {start_id} to {end_id}")

        processed_terms = set()

        try:
            self.error_stats = {k: 0 for k in self.error_stats}

            self._report_log("Начинаю загрузку данных о наказаниях... (10–15 минут)\n")

            def _cache_progress(ch_idx, total_ch, fetched, total_to_fetch, msg):
                self._report_log(f"  {msg}\n")
                self._report_progress(fetched, total_to_fetch, msg)

            self.complaint_channels = await self.discord.update_complaint_cache(
                self.complaint_channels,
                history_limit=self.cfg.discord.message_history_limit,
                progress_callback=_cache_progress
            )

            self._report_log("Загрузка данных о наказаниях завершена.\n")
            messages = await self.discord.scan_target_channel_interval(
                start_id,
                end_id,
                lambda m: any(embed.title == 'Arrived new player' for embed in m.embeds)
            )

            if not messages:
                self.logger.info("No matching messages found in the interval")
                return []

            self.logger.info(f"Found {len(messages)} messages to process in the interval")

            message_data = self._extract_message_data(messages)
            all_terms = message_data['all_terms']

            should_limit, terms_to_process = self._should_limit_processing(len(all_terms))
            if should_limit:
                all_terms = list(all_terms)[:terms_to_process]

            self.logger.info(f"Processing {len(all_terms)} unique terms")

            term_results = await self._process_all_terms_enhanced(
                all_terms,
                message_data['term_is_login_event'],
                message_data['user_id_terms'],
                processed_terms,
                message_data
            )

            scan_results = await self._create_scan_results(
                messages,
                message_data,
                term_results
            )

            consolidated_results = self._consolidate_results(scan_results)
            report_data = self.report.generate_message_scan_report(consolidated_results)

            duration = (datetime.now() - start_time).total_seconds()
            hit_rate = (len(consolidated_results) / len(messages)) * 100 if messages else 0

            self.perf.logger.info(
                f"Interval scan completed in {duration:.2f}s: processed {len(messages)} messages, "
                f"found {len(consolidated_results)} results ({hit_rate:.1f}% hit rate)"
            )

            self._log_error_statistics()
            self.perf.log_summary_if_needed()
            return report_data

        except Exception as e:
            self.logger.error(f"Error during interval scan: {str(e)}", exc_info=True)
            return []
        finally:
            self._report_log("Сохраняю данные в базу данных...\n")
            self.cache.save_complaint_cache(self.complaint_channels)
            self._report_log("Сохранение завершено.\n")

    def _consolidate_results(self, scan_results):
        if not scan_results:
            return []
        user_id_groups = {}
        no_user_id_players = []
        for result in scan_results:
            message_id = result.message.id
            for player in result.players:
                if player.user_id and player.user_id != "N/A":
                    if player.user_id not in user_id_groups:
                        user_id_groups[player.user_id] = {"players": [], "messages": set()}
                    user_id_groups[player.user_id]["players"].append(player)
                    user_id_groups[player.user_id]["messages"].add(message_id)
                else:
                    no_user_id_players.append((player, message_id))
        player_registry = {}
        message_to_players = defaultdict(set)
        for user_id, data in user_id_groups.items():
            players = data["players"]
            message_ids = data["messages"]
            merged_player = players[0]
            for i in range(1, len(players)):
                self._merge_player_info(merged_player, players[i])
            player_id = f"uid:{user_id}"
            player_registry[player_id] = merged_player
            for message_id in message_ids:
                message_to_players[message_id].add(player_id)
        for player, message_id in no_user_id_players:
            key_parts = []
            primary_nickname = player.primary_nickname if hasattr(player, 'primary_nickname') else (
                player.nicknames[0] if player.nicknames else "")
            if primary_nickname:
                key_parts.append(f"name:{primary_nickname}")
            if hasattr(player, 'associated_hwids') and player.associated_hwids:
                first_hwid = next(iter(player.associated_hwids.keys()), "")
                if first_hwid and first_hwid != "N/A":
                    key_parts.append(f"hwid:{first_hwid}")
            if hasattr(player, 'associated_ips') and player.associated_ips:
                first_ip = next(iter(player.associated_ips.keys()), "")
                if first_ip and first_ip != "N/A":
                    key_parts.append(f"ip:{first_ip}")
            if not key_parts and player.nicknames:
                for nick in player.nicknames:
                    key_parts.append(f"name:{nick}")
            identifier_string = '|'.join(key_parts)
            player_id = hashlib.md5(identifier_string.encode()).hexdigest()
            if player_id not in player_registry:
                player_registry[player_id] = player
            else:
                self._merge_player_info(player_registry[player_id], player)
            message_to_players[message_id].add(player_id)
        consolidated_results = []
        processed_messages = set()
        for result in scan_results:
            message_id = result.message.id
            if message_id in processed_messages:
                continue
            processed_messages.add(message_id)
            message_player_ids = message_to_players[message_id]
            consolidated_players = [player_registry[pid] for pid in message_player_ids]
            consolidated_results.append(
                ScanResult(
                    message=result.message,
                    players=consolidated_players,
                    scan_time=result.scan_time
                )
            )
        self.logger.info(
            f"Consolidated {len(scan_results)} results into {len(consolidated_results)} unique message results"
        )
        return consolidated_results

    def _merge_player_info(self, target_player, source_player):
        if not hasattr(target_player, 'nicknames_sources'):
            target_player.nicknames_sources = {}
        if not hasattr(target_player, 'is_from_user_id'):
            target_player.is_from_user_id = False
        if hasattr(source_player, 'is_from_user_id') and source_player.is_from_user_id:
            target_player.is_from_user_id = True
        source_is_primary = hasattr(source_player, 'is_primary') and source_player.is_primary
        target_is_primary = hasattr(target_player, 'is_primary') and target_player.is_primary
        source_primary = source_player.primary_nickname if source_player.nicknames else None
        if source_is_primary and source_primary and source_primary in source_player.nicknames:
            if source_primary not in target_player.nicknames:
                target_player.nicknames.append(source_primary)
            target_player.nicknames.remove(source_primary)
            target_player.nicknames.insert(0, source_primary)
            target_player.nicknames_sources[source_primary] = "login"
            target_player.is_primary = True
            target_player.primary_nickname = source_primary
            if hasattr(source_player, 'search_term'):
                target_player.search_term = source_player.search_term
        source_is_login_event = False
        if hasattr(source_player, 'raw_message') and source_player.raw_message:
            source_is_login_event = "Arrived new player" in source_player.raw_message
            if source_is_login_event and (not hasattr(target_player, 'raw_message') or not target_player.raw_message):
                target_player.raw_message = source_player.raw_message
        for nickname in source_player.nicknames:
            if source_is_primary and source_primary and nickname == source_primary:
                continue
            is_login_event = source_is_login_event
            if hasattr(source_player, 'is_from_user_id') and source_player.is_from_user_id and nickname == \
                    source_player.nicknames[0]:
                is_login_event = True
            if is_login_event:
                target_player.nicknames_sources[nickname] = "login"
                if nickname in target_player.nicknames:
                    target_player.nicknames.remove(nickname)
                if target_is_primary and hasattr(target_player, 'primary_nickname'):
                    target_player.nicknames.insert(1, nickname)
                else:
                    target_player.nicknames.insert(0, nickname)
            elif nickname not in target_player.nicknames:
                target_player.nicknames_sources[nickname] = "other"
                target_player.nicknames.append(nickname)
        source_status = source_player.status.lower()
        target_status = target_player.status.lower()
        if self.status_priority.get(source_status, 0) > self.status_priority.get(target_status, 0):
            target_player.status = source_player.status
        target_player.ban_counts = max(target_player.ban_counts, source_player.ban_counts)
        if hasattr(source_player, 'associated_ips') and hasattr(target_player, 'associated_ips'):
            for ip, nicks in source_player.associated_ips.items():
                if ip in target_player.associated_ips:
                    combined_nicks = set(target_player.associated_ips[ip])
                    combined_nicks.update(nicks)
                    target_player.associated_ips[ip] = list(combined_nicks)
                else:
                    target_player.associated_ips[ip] = nicks
        if hasattr(source_player, 'associated_hwids') and hasattr(target_player, 'associated_hwids'):
            for hwid, nicks in source_player.associated_hwids.items():
                if hwid in target_player.associated_hwids:
                    combined_nicks = set(target_player.associated_hwids[hwid])
                    combined_nicks.update(nicks)
                    target_player.associated_hwids[hwid] = list(combined_nicks)
                else:
                    target_player.associated_hwids[hwid] = nicks
        if hasattr(source_player, 'complaint_links') and source_player.complaint_links:
            if not hasattr(target_player, 'complaint_links'):
                target_player.complaint_links = []
            existing_links = {
                tuple(sorted((k, str(v)) for k, v in link.items()))
                for link in target_player.complaint_links
            } if target_player.complaint_links else set()
            for link in source_player.complaint_links:
                link_tuple = tuple(sorted((k, str(v)) for k, v in link.items()))
                if link_tuple not in existing_links:
                    target_player.complaint_links.append(link)
                    existing_links.add(link_tuple)

        if hasattr(source_player, 'ban_reasons') and source_player.ban_reasons:
            if not hasattr(target_player, 'ban_reasons'):
                target_player.ban_reasons = []

            existing_ban_reasons = set()
            for ban_info in target_player.ban_reasons:
                if isinstance(ban_info, dict) and "reason" in ban_info and "username" in ban_info:
                    existing_ban_reasons.add((ban_info["reason"], ban_info["username"]))
                elif isinstance(ban_info, str):
                    existing_ban_reasons.add((ban_info, "Unknown"))

            for ban_info in source_player.ban_reasons:
                if isinstance(ban_info, dict) and "reason" in ban_info and "username" in ban_info:
                    key = (ban_info["reason"], ban_info["username"])
                    if key not in existing_ban_reasons:
                        target_player.ban_reasons.append(ban_info)
                        existing_ban_reasons.add(key)
                elif isinstance(ban_info, str):
                    key = (ban_info, "Unknown")
                    if key not in existing_ban_reasons:
                        target_player.ban_reasons.append({
                            "reason": ban_info,
                            "username": "Unknown"
                        })
                        existing_ban_reasons.add(key)

    def _extract_message_data(self, messages):
        all_terms = set()
        message_terms = {}
        term_is_login_event = {}
        user_id_terms = {}
        message_nicknames = {}
        term_to_message_id = {}
        for message in messages:
            if 'Arrived new player' not in message.embed_titles:
                continue
            nickname = None
            candidate_nicknames = []
            for key, url in message.embed_links.items():
                if key.startswith('search:'):
                    term = key[7:]
                    if re.match(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', term, re.I):
                        continue
                    elif re.match(r'^(\d{1,3}\.){3}\d{1,3}$', term):
                        continue
                    elif term.startswith('V2-'):
                        continue
                    else:
                        candidate_nicknames.append(term)
            if candidate_nicknames:
                nickname = candidate_nicknames[0]
                message_nicknames[message.id] = nickname
            if not nickname and hasattr(message, 'embeds'):
                for embed in message.embeds:
                    if embed.title == 'Arrived new player':
                        for field in embed.fields:
                            if field.name.lower() == 'name':
                                nickname = field.value.strip()
                                message_nicknames[message.id] = nickname
                                break
            unique_terms = {
                extract_effective_search_term(url)
                for url in message.embed_links.values()
                if extract_effective_search_term(url)
            }
            if not unique_terms:
                continue
            message_terms[message.id] = unique_terms
            all_terms.update(unique_terms)
            for term in unique_terms:
                term_is_login_event[term] = True
                term_to_message_id[term] = message.id
            for url in message.embed_links.values():
                term = extract_effective_search_term(url)
                if term and re.match(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', term, re.I):
                    user_id_terms[message.id] = term
                    break
        return {
            'all_terms': all_terms,
            'message_terms': message_terms,
            'term_is_login_event': term_is_login_event,
            'user_id_terms': user_id_terms,
            'message_nicknames': message_nicknames,
            'term_to_message_id': term_to_message_id
        }

    async def _process_all_terms(self, all_terms, term_is_login_event, user_id_terms, processed_terms, message_data):
        high_priority_terms = []
        normal_priority_terms = []

        for term in all_terms:
            if term in user_id_terms.values() or term_is_login_event.get(term, False):
                high_priority_terms.append(term)
            else:
                normal_priority_terms.append(term)

        all_priority_terms = high_priority_terms + normal_priority_terms

        if not all_priority_terms:
            return {}

        self.logger.info(f"Processing {len(all_priority_terms)} terms in batches.")

        term_results = {}
        message_nicknames = message_data.get('message_nicknames', {})
        term_to_message_id = message_data.get('term_to_message_id', {})
        cache_lock = asyncio.Lock()

        batch_size = self.max_concurrent * 2

        for i in range(0, len(all_priority_terms), batch_size):
            batch_terms = all_priority_terms[i:i + batch_size]
            term_tasks = []

            for term in batch_terms:
                async with cache_lock:
                    if term in processed_terms:
                        continue
                    processed_terms.add(term)

                message_id = term_to_message_id.get(term)
                nickname = message_nicknames.get(message_id) if message_id else None

                term_tasks.append(
                    self._process_term_with_timeout(
                        term,
                        use_cache=True,
                        shared_cache=None,
                        cache_lock=None,
                        is_login_event=term_is_login_event.get(term, False),
                        is_user_id=(term in user_id_terms.values()),
                        message_nickname=nickname
                    )
                )

            if term_tasks:
                try:
                    batch_results = await asyncio.gather(*term_tasks)
                    for term, result in zip(batch_terms, batch_results):
                        if result:
                            term_results[term] = result
                except Exception as e:
                    self.logger.error(f"Error processing a batch of terms: {e}", exc_info=True)

            processed_count = i + len(batch_terms)
            total_count = len(all_priority_terms)
            self.logger.info(
                f"Completed batch {i // batch_size + 1}/{(total_count + batch_size - 1) // batch_size}. "
                f"Processed {processed_count}/{total_count} terms."
            )

            if processed_count < total_count:
                await asyncio.sleep(0.5)

        return term_results

    async def _process_term_with_timeout(self, term: str, **kwargs) -> Optional[Player]:
        try:
            async with asyncio.timeout(self._term_timeout):
                return await self.process_term(term, **kwargs)
        except asyncio.TimeoutError:
            self.logger.error(f"Term processing timed out for '{term[:50]}' after {self._term_timeout}s")
            return None
        except Exception as e:
            self.logger.error(f"Error in _process_term_with_timeout for '{term[:50]}': {e}")
            return None

    async def _create_scan_results(self, messages, message_data, term_to_player):
        scan_results = []
        for message in messages:
            if message.id not in message_data['message_terms']:
                continue
            message_terms = message_data['message_terms'][message.id]
            players = [term_to_player[term] for term in message_terms if term in term_to_player]
            if not players:
                continue
            message_nickname = message_data.get('message_nicknames', {}).get(message.id)
            user_id_term = message_data['user_id_terms'].get(message.id)
            user_id_player = term_to_player.get(user_id_term) if user_id_term else None
            self._annotate_players_with_login_info(players, user_id_player, message, message_nickname)
            grouped_players = self.analyzer.group_players_by_nicknames(players)
            all_nicknames = {nickname for player in grouped_players for nickname in player.nicknames}
            complaint_links = await self.discord.find_nickname_mentions(
                list(all_nicknames), self.complaint_channels
            )
            for player in grouped_players:
                player.complaint_links = [
                    link for link in complaint_links
                    if any(nickname in link.get('content', '') for nickname in player.nicknames)
                ]
            scan_results.append(
                ScanResult(message=message, players=grouped_players, scan_time=datetime.now())
            )
        return scan_results

    def _annotate_players_with_login_info(self, players, user_id_player, message, message_nickname=None):
        for player in players:
            if message.embed_titles and 'Arrived new player' in message.embed_titles:
                player.raw_message = "Arrived new player"
                if not hasattr(player, 'nicknames_sources'):
                    player.nicknames_sources = {}
                if message_nickname and message_nickname in player.nicknames:
                    player.is_primary = True
                    player.nicknames.remove(message_nickname)
                    player.nicknames.insert(0, message_nickname)
                    player.nicknames_sources[message_nickname] = "login"
                    player.primary_nickname = message_nickname
                elif user_id_player and player is user_id_player and player.nicknames:
                    player.is_primary = True
                    primary_nick = player.nicknames[0]
                    player.nicknames_sources[primary_nick] = "login"
                    player.primary_nickname = primary_nick
                elif player.nicknames:
                    player.is_primary = False
                    primary_nick = player.nicknames[0]
                    player.nicknames_sources[primary_nick] = "login"

    @monitor_performance()
    async def process_term(self, term: str, use_cache: bool = False,
                           shared_cache: Optional[Set[str]] = None,
                           cache_lock: Optional[asyncio.Lock] = None,
                           is_login_event: bool = False,
                           is_user_id: bool = False,
                           message_nickname: Optional[str] = None) -> Optional[Player]:
        term_start = datetime.now()
        try:
            if use_cache and shared_cache is not None and cache_lock is not None:
                async with cache_lock:
                    if term in shared_cache:
                        return None
                    shared_cache.add(term)

            if term in self.cache_data["players"]:
                player = self.cache_data["players"][term]
                if message_nickname and message_nickname in player.nicknames:
                    player.nicknames.remove(message_nickname)
                    player.nicknames.insert(0, message_nickname)
                    if not hasattr(player, 'nicknames_sources'):
                        player.nicknames_sources = {}
                    player.nicknames_sources[message_nickname] = "login"
                    player.is_primary = True
                    player.primary_nickname = message_nickname
                elif is_login_event:
                    self._update_player_login_info(player, is_user_id)
                return player

            self.logger.info(f"Searching for player with term: '{term}'")
            account_info = await self.admin.search_player(term)
            if not account_info:
                self.logger.info(f"No account found for term: '{term}'")
                return None

            player = self.admin.convert_to_player(account_info)
            player.is_from_user_id = is_user_id
            player.search_term = term

            if message_nickname and message_nickname in player.nicknames:
                player.nicknames.remove(message_nickname)
                player.nicknames.insert(0, message_nickname)
                if not hasattr(player, 'nicknames_sources'):
                    player.nicknames_sources = {}
                player.nicknames_sources[message_nickname] = "login"
                player.is_primary = True
                player.primary_nickname = message_nickname
            elif is_login_event:
                self._update_player_login_info(player, is_user_id)

            await self._fetch_player_connections(player)

            if not getattr(player, 'is_primary', False):
                self._identify_primary_nickname_from_search_term(player)

            self.cache_data["players"][term] = player
            processing_duration = (datetime.now() - term_start).total_seconds()
            self.perf.record("process_term", processing_duration)
            self.logger.info(f"Processed term '{term}' in {processing_duration:.2f}s")
            return player
        except Exception as e:
            self.logger.error(f"Error processing term '{term}': {str(e)}", exc_info=True)
            return None

    def _identify_primary_nickname_from_search_term(self, player: Player) -> None:
        search_term = getattr(player, 'search_term', None)
        if not search_term or not player.nicknames or getattr(player, 'is_primary', False):
            return
        if hasattr(player, 'associated_ips') and search_term in player.associated_ips:
            nicks = player.associated_ips[search_term]
            if nicks:
                primary_nick = nicks[0]
                if primary_nick in player.nicknames:
                    player.nicknames.remove(primary_nick)
                    player.nicknames.insert(0, primary_nick)
                    if not hasattr(player, 'nicknames_sources'):
                        player.nicknames_sources = {}
                    player.nicknames_sources[primary_nick] = "login"
                    player.is_primary = True
                    player.primary_nickname = primary_nick
        elif hasattr(player, 'associated_hwids') and search_term in player.associated_hwids:
            nicks = player.associated_hwids[search_term]
            if nicks:
                primary_nick = nicks[0]
                if primary_nick in player.nicknames:
                    player.nicknames.remove(primary_nick)
                    player.nicknames.insert(0, primary_nick)
                    if not hasattr(player, 'nicknames_sources'):
                        player.nicknames_sources = {}
                    player.nicknames_sources[primary_nick] = "login"
                    player.is_primary = True
                    player.primary_nickname = primary_nick

    def _update_player_login_info(self, player, is_user_id):
        player.raw_message = "Arrived new player"
        if not player.nicknames:
            return
        primary_nick = player.nicknames[0]
        if not hasattr(player, 'nicknames_sources'):
            player.nicknames_sources = {}
        player.nicknames_sources[primary_nick] = "login"
        if is_user_id:
            player.is_primary = True
            player.primary_nickname = primary_nick

    async def _fetch_player_connections(self, player: Player) -> None:
        identifiers = self._get_player_identifiers(player)
        if not identifiers:
            return

        max_identifiers = min(10, len(identifiers))
        selected_identifiers = identifiers[:max_identifiers]

        identifiers_to_fetch = [
            identifier for identifier in selected_identifiers if identifier not in self.cache_data["connections"]
        ]

        if not identifiers_to_fetch:
            return

        connection_tasks = [
            asyncio.create_task(self._fetch_connections_with_timeout(identifier))
            for identifier in identifiers_to_fetch
        ]

        if connection_tasks:
            try:
                connection_results = await gather_with_concurrency(
                    self.max_concurrent,
                    *connection_tasks
                )

                all_connections = []

                for identifier, result in zip(identifiers_to_fetch, connection_results):
                    if result is not None:
                        self.cache_data["connections"][identifier] = result
                        all_connections.extend(result)
                        self._update_identity_graph(result)

                self._process_player_connections(player, all_connections)
            except Exception as e:
                self.logger.error(f"Error fetching player connections for player '{player.user_id}': {e}",
                                  exc_info=True)

    async def _fetch_connections_with_timeout(self, identifier: str) -> List[Dict[str, Any]]:
        try:
            async with asyncio.timeout(self._operation_timeout):
                return await self.admin.fetch_with_rate_limit(
                    self.admin_panel.fetch_connections_for_user, identifier
                )
        except asyncio.TimeoutError:
            self.logger.error(f"Connection fetch timed out for identifier: {identifier}")
            return []
        except Exception as e:
            self.logger.error(f"Error fetching connections for {identifier}: {e}")
            return []

    def _get_player_identifiers(self, player: Player) -> List[str]:
        identifiers = set()
        if player.user_id and player.user_id != "N/A":
            identifiers.add(player.user_id)
        identifiers.update(player.nicknames)
        if hasattr(player, 'associated_ips') and player.associated_ips:
            identifiers.update(ip for ip in player.associated_ips if ip != "N/A")
        if hasattr(player, 'associated_hwids') and player.associated_hwids:
            identifiers.update(hwid for hwid in player.associated_hwids if hwid != "N/A")
        return list(identifiers)

    def _update_identity_graph(self, connections: List[Dict[str, Any]]) -> None:
        for conn in connections:
            user_name = conn.get("user_name")
            user_id = conn.get("user_id")
            ip = conn.get("ip_address")
            hwid = conn.get("hwid")
            if not user_name or user_name == "N/A":
                continue
            if user_id and user_id != "N/A":
                self.identity_graph[f"uid:{user_id}"].add(f"name:{user_name}")
                self.identity_graph[f"name:{user_name}"].add(f"uid:{user_id}")
            if ip and ip != "N/A":
                self.identity_graph[f"ip:{ip}"].add(f"name:{user_name}")
                self.identity_graph[f"name:{user_name}"].add(f"ip:{ip}")
            if hwid and hwid != "N/A":
                self.identity_graph[f"hwid:{hwid}"].add(f"name:{user_name}")
                self.identity_graph[f"name:{user_name}"].add(f"hwid:{hwid}")
            if ip and ip != "N/A" and hwid and hwid != "N/A":
                self.identity_graph[f"ip:{ip}"].add(f"hwid:{hwid}")
                self.identity_graph[f"hwid:{hwid}"].add(f"ip:{ip}")
            if user_id and user_id != "N/A":
                if ip and ip != "N/A":
                    self.identity_graph[f"uid:{user_id}"].add(f"ip:{ip}")
                    self.identity_graph[f"ip:{ip}"].add(f"uid:{user_id}")
                if hwid and hwid != "N/A":
                    self.identity_graph[f"uid:{user_id}"].add(f"hwid:{hwid}")
                    self.identity_graph[f"hwid:{hwid}"].add(f"uid:{user_id}")

    def _process_player_connections(self, player: Player, connections: List[Dict[str, Any]]) -> None:
        nickname_connections = defaultdict(list)
        for conn in connections:
            user_name = conn.get("user_name", "")
            if user_name:
                nickname_connections[user_name].append(conn)
        if hasattr(player, 'associated_ips'):
            for ip, nicknames in player.associated_ips.items():
                for conn in connections:
                    if conn.get("ip_address") == ip:
                        user_name = conn.get("user_name")
                        if user_name and user_name not in nicknames:
                            nicknames.append(user_name)
        if hasattr(player, 'associated_hwids'):
            for hwid, nicknames in player.associated_hwids.items():
                for conn in connections:
                    if conn.get("hwid") == hwid:
                        user_name = conn.get("user_name")
                        if user_name and user_name not in nicknames:
                            nicknames.append(user_name)
        denied_logins = []
        for conn in connections:
            if "Denied: Banned" in conn.get("status", ""):
                denied_logins.append({
                    "user_name": conn.get("user_name", ""),
                    "time": conn.get("time", ""),
                    "ip_address": conn.get("ip_address", ""),
                    "hwid": conn.get("hwid", ""),
                    "server": conn.get("server", "")
                })
        player.denied_logins = denied_logins
        if denied_logins and self.status_priority.get(player.status.lower(), 0) < self.status_priority['suspicious']:
            player.status = "suspicious"
            player.ban_counts = max(player.ban_counts, 1)

    @monitor_performance()
    async def scan_nickname(self, nickname: str, complaint_search_term: Optional[str] = None) -> List[Dict[str, Any]]:
        start_time = datetime.now()
        self.logger.info(f"Starting nickname search for: {nickname}")
        self._report_progress(0, 5, "Загрузка данных о наказаниях...")
        try:
            self.complaint_channels = await self.discord.update_complaint_cache(
                self.complaint_channels,
                history_limit=self.cfg.discord.message_history_limit
            )
            self._report_progress(1, 5, "Поиск игрока по связям (IP/HWID)...")
            player = await self.process_term(nickname)
            if not player:
                self.logger.info(f"No player found for nickname: {nickname}")
                return []
            self._report_progress(2, 5, "Поиск наказаний в Discord...")
            complaint_links = await self.discord.find_nickname_mentions(
                player.nicknames,
                self.complaint_channels,
                search_term=complaint_search_term
            )
            player.complaint_links = complaint_links
            if complaint_search_term:
                self.logger.info(f"Found {len(complaint_links)} complaints with '{complaint_search_term}'")
            self._report_progress(3, 5, "Анализ и объединение данных...")
            self._report_progress(4, 5, "Формирование отчета...")

            if self.progress_queue is not None:
                def _send(data):
                    try:
                        self.progress_queue.put_nowait(data)
                    except Exception:
                        pass

                primary = getattr(player, 'primary_nickname', player.nicknames[0] if player.nicknames else nickname)
                status = getattr(player, 'status', 'unknown')
                hwid_erased = getattr(player, 'hwid_erased', False)

                _send({"type": "player_summary", "nickname": nickname, "primary": primary,
                       "status": status, "ban_counts": getattr(player, 'ban_counts', 0),
                       "hwid_erased": hwid_erased})

                if hasattr(player, 'ban_reasons') and player.ban_reasons:
                    for i, ban in enumerate(player.ban_reasons):
                        _send({
                            "type": "punishment", "player": primary, "status": status,
                            "reason": ban.get("reason", str(ban)) if isinstance(ban, dict) else str(ban),
                            "banned_nickname": ban.get("username", primary) if isinstance(ban, dict) else primary,
                            "admin": ban.get("admin", "N/A") if isinstance(ban, dict) else "N/A",
                            "ban_type": ban.get("type", "N/A") if isinstance(ban, dict) else "N/A",
                            "ban_date": ban.get("date", "N/A") if isinstance(ban, dict) else "N/A",
                            "ban_expires": ban.get("expires", "Никогда") if isinstance(ban, dict) else "Никогда",
                            "search_nickname": nickname,
                            "index": i + 1,
                        })
                    _send({"type": "punishments_done"})

                if hasattr(player, 'nicknames') and player.nicknames and len(player.nicknames) > 1:
                    _send({"type": "nicknames", "nicknames": player.nicknames, "primary": primary})

                if hasattr(player, 'complaint_links') and player.complaint_links:
                    for i, c in enumerate(player.complaint_links):
                        _send({
                            "type": "complaint",
                            "channel": c.get("channel", "?"),
                            "author": c.get("author", "?"),
                            "content": c.get("content", "")[:300],
                            "link": c.get("link", "?"),
                            "index": i + 1,
                        })
                    _send({"type": "complaints_done"})

                if hasattr(player, 'associated_ips') and player.associated_ips:
                    items = []
                    for ip, users in player.associated_ips.items():
                        if primary in users and len(users) == 1:
                            items.append(ip)
                        else:
                            items.append(f"{ip}  ▶  {', '.join(users[:5])}")
                    _send({"type": "ips", "items": items, "primary": primary})

                if hasattr(player, 'associated_hwids') and player.associated_hwids:
                    items = []
                    for hwid, users in player.associated_hwids.items():
                        if primary in users and len(users) == 1:
                            items.append(hwid[:32])
                        else:
                            items.append(f"{hwid[:32]}  ▶  {', '.join(users[:5])}")
                    _send({"type": "hwids", "items": items, "primary": primary})

                if hasattr(player, 'denied_logins') and player.denied_logins:
                    _send({"type": "denied_logins", "logins": list(player.denied_logins)})

                _send({"type": "scan_results_done"})

            report_data = self.report.generate_nickname_search_report(nickname, player, gui_mode=self.progress_queue is not None)
            self._report_progress(5, 5, "Готово")
            duration = (datetime.now() - start_time).total_seconds()
            self.perf.logger.info(f"Nickname search for '{nickname}' completed in {duration:.2f}s")
            return report_data
        except Exception as e:
            self.logger.error(f"Error in scan_nickname for '{nickname}': {str(e)}", exc_info=True)
            return []
        finally:
            self.cache.save_complaint_cache(self.complaint_channels)

    @monitor_performance()
    async def scan_ban_bypasses(self, max_pages: int = 5) -> List[Dict[str, Any]]:
        start_time = datetime.now()
        max_depth = getattr(self.cfg.scan, 'bypass_search_max_depth', 2)
        self.logger.info(f"Starting Ban Bypass Check, fetching up to {max_pages} pages with max depth {max_depth}...")

        try:
            async with asyncio.timeout(self._operation_timeout * 10):
                return await self._scan_ban_bypasses_internal(max_pages, max_depth, start_time)
        except asyncio.TimeoutError:
            self.logger.error(f"Ban bypass scan timed out after {self._operation_timeout * 10}s")
            return []
        except Exception as e:
            self.logger.error(f"Error during ban bypass check: {str(e)}", exc_info=True)
            return []
        finally:
            self.cache.save_complaint_cache(self.complaint_channels)

    async def _scan_ban_bypasses_internal(self, max_pages: int, max_depth: int, start_time: datetime) -> List[
        Dict[str, Any]]:
        try:
            self.complaint_channels = await self.discord.update_complaint_cache(
                self.complaint_channels,
                history_limit=self.cfg.discord.message_history_limit
            )

            ban_hit_connections = await asyncio.wait_for(
                self.admin_panel.fetch_ban_hit_connections(max_pages=max_pages),
                timeout=self._operation_timeout
            )

            if not ban_hit_connections:
                self.logger.info("No ban hit connections found.")
                return []

            self.logger.info(f"Processing {len(ban_hit_connections)} ban hits with max depth {max_depth}")
            processed_terms = set()
            self.connections_cache = {}
            ban_hit_connections.sort(key=lambda x: x.get("time", ""), reverse=True)
            batch_size = min(self.max_concurrent // 2, 10)
            results = []

            for i in range(0, len(ban_hit_connections), batch_size):
                batch = ban_hit_connections[i:i + batch_size]
                self.logger.info(
                    f"Processing batch {i // batch_size + 1}/{(len(ban_hit_connections) + batch_size - 1) // batch_size}")
                progress_stats = defaultdict(int)
                batch_tasks = []

                for ban_hit in batch:
                    task = self._process_ban_hit_with_timeout(
                        ban_hit,
                        max_depth,
                        processed_terms,
                        progress_stats
                    )
                    batch_tasks.append(task)

                try:
                    batch_results = await asyncio.gather(*batch_tasks, return_exceptions=True)
                    valid_results = []
                    for result in batch_results:
                        if isinstance(result, Exception):
                            self.logger.error(f"Error in batch processing: {str(result)}")
                            continue
                        if result:
                            valid_results.append(result)
                    results.extend(valid_results)

                    self.logger.info(f"Batch {i // batch_size + 1} stats: " +
                                     f"processed={progress_stats['processed']}, " +
                                     f"hwid_matches={progress_stats.get('hwid_matches', 0)}, " +
                                     f"ip_matches={progress_stats.get('ip_matches', 0)}")

                    if i + batch_size < len(ban_hit_connections):
                        await asyncio.sleep(0.5)
                except Exception as e:
                    self.logger.error(f"Error processing batch {i // batch_size + 1}: {e}")

            cache_hits = sum(1 for term in processed_terms if term in self.connections_cache)
            duration = (datetime.now() - start_time).total_seconds()
            self.logger.info(
                f"Ban Bypass Check completed in {duration:.2f}s: processed {len(ban_hit_connections)} ban hits, "
                f"found {len(results)} results with depth {max_depth}, "
                f"processed {len(processed_terms)} unique terms, cache hits: {cache_hits}"
            )
            self.connections_cache.clear()
            return results

        except asyncio.TimeoutError:
            self.logger.error("Ban bypass scan internal process timed out")
            return []

    async def _process_ban_hit_with_timeout(self, ban_hit, max_depth, processed_terms, progress_stats):
        try:
            async with asyncio.timeout(self._operation_timeout):
                return await self._process_ban_hit(ban_hit, max_depth, processed_terms, progress_stats)
        except asyncio.TimeoutError:
            self.logger.error(f"Ban hit processing timed out for {ban_hit.get('ban_hits_link')}")
            return None
        except Exception as e:
            self.logger.error(f"Error in _process_ban_hit_with_timeout: {e}")
            return None

    async def _process_ban_hit(self, ban_hit, max_depth, processed_terms, progress_stats):
        try:
            progress_stats['processed'] += 1
            ban_id = ban_hit.get("connection_id", "") or ban_hit.get("ban_hits_link", "")
            ban_hit_time = datetime.strptime(ban_hit["time"], "%Y-%m-%d %H:%M:%S")
            user_id = ban_hit.get("user_id")
            if not user_id or user_id == "N/A":
                return None
            ban_hits_link = ban_hit.get("ban_hits_link")
            async with asyncio.Lock():
                if ban_id and ban_id in processed_terms:
                    return None
                if ban_id:
                    processed_terms.add(ban_id)
            ban_info_list = await self.admin.fetch_with_rate_limit(
                self.admin_panel.fetch_ban_info,
                ban_hits_link
            )
            
            if not ban_info_list:
                self.logger.warning(f"No ban info found for link: {ban_hits_link}")
                return None
            
            if len(ban_info_list) > 1:
                self.logger.info(f"Found {len(ban_info_list)} ban bypass attempts for connection {ban_id}")
                for idx, entry in enumerate(ban_info_list):
                    ban_time = entry.get("ban_time", "unknown")
                    ban_reason = entry.get("ban_reason", "unknown")
                    self.logger.info(f"  Ban bypass #{idx + 1}: {ban_time} (reason: {ban_reason})")
            
            ban_info = ban_info_list[0]
            banned_user_name = ban_info.get("banned_user_name") or ban_hit.get("user_name", "")
            user_id = ban_info.get("user_id") or user_id
            ip_address = ban_info.get("ip_address") or ban_hit.get("ip_address", "")
            hwid = ban_info.get("hwid") or ban_hit.get("hwid", "")
            hwid_erased = not hwid or hwid.strip() == ""
            ban_time_str = ban_info.get("ban_time", ban_hit["time"])
            ban_expires_str = ban_info.get("expires", ban_hit["time"])
            self.logger.info(f"Processing ban hit for user '{banned_user_name}' (ID: {user_id})")
            connections = await self._gather_connections(
                user_id,
                hwid,
                ip_address,
                max_depth,
                processed_terms,
                banned_user_name
            )
            hwid_match_users = set()
            if hwid and hwid != "N/A":
                for conn in connections:
                    if conn.get("hwid") == hwid and conn.get("user_name") != banned_user_name:
                        hwid_match_users.add(conn.get("user_name"))
                if hwid_match_users:
                    progress_stats['hwid_matches'] = progress_stats.get('hwid_matches', 0) + 1
                    self.logger.info(f"HWID match found for {banned_user_name}: {', '.join(sorted(hwid_match_users))}")
            account_info = await self.admin_panel.aggregate_single_user_info(connections)
            bypass_reason = self.analyzer.confidence_levels['no_match']
            bypass_user_names = []
            if hwid_match_users:
                bypass_reason = self.analyzer.confidence_levels['hwid_match']
                bypass_user_names = sorted(hwid_match_users)
            elif ip_address and ip_address != "N/A":
                time_suspected_users = self._check_time_based_bypass(ban_hit_time, ip_address, banned_user_name,
                                                                     connections)
                if time_suspected_users:
                    time_diff_minutes = self._get_minimum_time_difference(ban_hit_time, time_suspected_users,
                                                                          connections)
                    if time_diff_minutes <= self.analyzer.very_close_time_threshold_minutes:
                        bypass_reason = self.analyzer.confidence_levels['ip_very_close_time']
                    elif time_diff_minutes <= self.analyzer.close_time_threshold_minutes:
                        bypass_reason = self.analyzer.confidence_levels['ip_close_time']
                    elif time_diff_minutes <= self.analyzer.moderate_time_threshold_minutes:
                        bypass_reason = self.analyzer.confidence_levels['ip_moderate_time']
                    elif time_diff_minutes <= self.analyzer.distant_time_threshold_minutes:
                        bypass_reason = self.analyzer.confidence_levels['ip_distant_time']
                    else:
                        bypass_reason = self.analyzer.confidence_levels['ip_match']
                    bypass_user_names = sorted(set(time_suspected_users))
                    progress_stats['ip_matches'] = progress_stats.get('ip_matches', 0) + 1
                elif ip_address in account_info.get("associated_ips", {}):
                    ip_nicks = set(account_info["associated_ips"][ip_address]) - {banned_user_name}
                    if ip_nicks:
                        bypass_reason = self.analyzer.confidence_levels['ip_match']
                        bypass_user_names = sorted(ip_nicks)
                        progress_stats['ip_matches'] = progress_stats.get('ip_matches', 0) + 1
            bypass_success_status = self._determine_bypass_success(connections, bypass_user_names, ban_time_str, hwid,
                                                                   ip_address)
            player = self.admin.convert_to_player(account_info)
            player.hwid_erased = hwid_erased
            complaint_task = asyncio.create_task(self.discord.find_nickname_mentions(
                [banned_user_name] + bypass_user_names,
                self.complaint_channels
            ))
            complaint_links = await complaint_task
            has_meaningful_result = (
                    bypass_reason != self.analyzer.confidence_levels['no_match'] or
                    bypass_user_names or
                    hwid_erased or
                    complaint_links
            )
            if not has_meaningful_result:
                self.logger.info(f"No meaningful bypass detected for {banned_user_name}")
                return None
            report = {
                "message_id": "BanBypassCheck",
                "message_link": ban_hits_link,
                "author_name": banned_user_name,
                "author_id": user_id,
                "scan_time": datetime.now().isoformat(),
                "ban_time": ban_time_str,
                "ban_expires": ban_expires_str,
                "ban_bypass_confidence": bypass_reason,
                "bypass_user_names": bypass_user_names,
                "bypass_success_status": bypass_success_status,
                "hwid_erased": hwid_erased,
                "search_depth": max_depth,
                "connections_analyzed": len(connections),
                "ban_entries_count": len(ban_info_list),
                "all_ban_entries": ban_info_list,
                "results": [{
                    "initial_account": account_info,
                    "complaint_links": complaint_links,
                    "nicknames": player.nicknames,
                    "hwid_erased": hwid_erased,
                    "banned_user_name": banned_user_name,
                    "ip_address": ip_address,
                    "hwid": hwid
                }]
            }
            self.logger.info(
                f"Ban hit for {banned_user_name}: Confidence: {bypass_reason}, " +
                f"Bypass status: {bypass_success_status}, " +
                f"Potential bypassers: {', '.join(bypass_user_names) if bypass_user_names else 'None'}, " +
                f"Analyzed {len(connections)} connections, " +
                f"Found {len(ban_info_list)} ban entries"
            )
            return report
        except Exception as e:
            self.logger.error(f"Error processing ban hit {ban_hit.get('ban_hits_link')}: {str(e)}", exc_info=True)
            return None

    async def _gather_connections(self, user_id, hwid, ip_address, max_depth, processed_terms,
                                  banned_user_name):
        search_processed = set()
        priority_queue = []
        if user_id and user_id != "N/A":
            heapq.heappush(priority_queue, (0, 1, "user_id", user_id))
            search_processed.add(user_id)
        if hwid and hwid != "N/A":
            heapq.heappush(priority_queue, (0, 2, "hwid", hwid))
            search_processed.add(hwid)
        if ip_address and ip_address != "N/A":
            heapq.heappush(priority_queue, (0, 3, "ip", ip_address))
            search_processed.add(ip_address)
        all_connections = []
        max_by_type_depth = {
            "user_id": {0: 100, 1: 50, 2: 30, 3: 20},
            "hwid": {0: 100, 1: 40, 2: 20, 3: 10},
            "ip": {0: 50, 1: 30, 2: 15, 3: 5},
            "username": {0: 30, 1: 20, 2: 10, 3: 5}
        }
        active_tasks = {}
        while priority_queue:
            batch = []
            batch_size = min(5, len(priority_queue))
            for _ in range(batch_size):
                if not priority_queue:
                    break
                item = heapq.heappop(priority_queue)
                depth, type_priority, id_type, identifier = item
                if depth > max_depth:
                    continue
                batch.append((depth, type_priority, id_type, identifier))
            fetch_tasks = []
            for depth, type_priority, id_type, identifier in batch:
                if identifier in active_tasks:
                    continue
                if identifier in self.connections_cache:
                    connections = self.connections_cache[identifier]
                    await self._process_connections_for_queue(
                        connections, identifier, depth, priority_queue,
                        search_processed, processed_terms, all_connections,
                        max_by_type_depth
                    )
                else:
                    task = self._fetch_and_process_connections(
                        identifier, depth, priority_queue, search_processed,
                        processed_terms, all_connections, max_by_type_depth,
                        banned_user_name
                    )
                    active_tasks[identifier] = asyncio.create_task(task)
                    fetch_tasks.append(active_tasks[identifier])
            if fetch_tasks:
                await asyncio.gather(*fetch_tasks, return_exceptions=True)
                for depth, _, _, identifier in batch:
                    if identifier in active_tasks:
                        del active_tasks[identifier]
            await asyncio.sleep(0)
        if active_tasks:
            await asyncio.gather(*active_tasks.values(), return_exceptions=True)
        return all_connections

    async def _fetch_and_process_connections(self, identifier, depth, priority_queue, search_processed,
                                             processed_terms, all_connections, max_by_type_depth,
                                             banned_user_name):
        try:
            connections = await self.admin.fetch_with_rate_limit(
                self.admin_panel.fetch_connections_for_user,
                identifier
            )
            self.connections_cache[identifier] = connections or []
            await self._process_connections_for_queue(
                connections, identifier, depth, priority_queue,
                search_processed, processed_terms, all_connections,
                max_by_type_depth
            )
            return connections or []
        except Exception as e:
            self.logger.error(f"Error fetching connections for {identifier}: {e}")
            return []

    async def _process_connections_for_queue(self, connections, identifier, depth, priority_queue,
                                             search_processed, processed_terms, all_connections,
                                             max_by_type_depth):
        if not connections:
            return
        depth_limit = max_by_type_depth.get(identifier, {}).get(depth, 10)
        limited_connections = connections[:depth_limit]
        all_connections.extend(limited_connections)
        if depth >= self.cfg.scan.bypass_search_max_depth:
            return
        next_depth = depth + 1
        new_identifiers = []
        banned_identifiers = []
        for conn in limited_connections:
            status = conn.get("status", "")
            if "Banned" in status or "Denied" in status:
                user_name = conn.get("user_name")
                user_id = conn.get("user_id")
                conn_hwid = conn.get("hwid")
                conn_ip = conn.get("ip_address")
                if user_id and user_id != "N/A" and user_id not in search_processed:
                    banned_identifiers.append((1, "user_id", user_id))
                if conn_hwid and conn_hwid != "N/A" and conn_hwid not in search_processed:
                    banned_identifiers.append((2, "hwid", conn_hwid))
                if conn_ip and conn_ip != "N/A" and conn_ip not in search_processed:
                    banned_identifiers.append((3, "ip", conn_ip))
                if user_name and user_name != "N/A" and user_name not in search_processed:
                    banned_identifiers.append((4, "username", user_name))
        for conn in limited_connections:
            user_name = conn.get("user_name")
            user_id = conn.get("user_id")
            conn_hwid = conn.get("hwid")
            conn_ip = conn.get("ip_address")
            if user_id and user_id != "N/A" and user_id not in search_processed:
                new_identifiers.append((1, "user_id", user_id))
            if conn_hwid and conn_hwid != "N/A" and conn_hwid not in search_processed:
                new_identifiers.append((2, "hwid", conn_hwid))
            if conn_ip and conn_ip != "N/A" and conn_ip not in search_processed:
                new_identifiers.append((3, "ip", conn_ip))
            if user_name and user_name != "N/A" and user_name not in search_processed:
                new_identifiers.append((4, "username", user_name))
        for type_priority, id_type, new_id in banned_identifiers:
            async with asyncio.Lock():
                if new_id not in processed_terms:
                    processed_terms.add(new_id)
            if new_id not in search_processed:
                search_processed.add(new_id)
                effective_depth = max(0, next_depth - 0.5)
                heapq.heappush(priority_queue, (effective_depth, type_priority, id_type, new_id))
        for type_priority, id_type, new_id in new_identifiers:
            async with asyncio.Lock():
                if new_id not in processed_terms:
                    processed_terms.add(new_id)
            if new_id not in search_processed:
                search_processed.add(new_id)
                heapq.heappush(priority_queue, (next_depth, type_priority, id_type, new_id))

    def _get_minimum_time_difference(self, ban_hit_time, suspected_users, connections):
        min_diff = float('inf')
        for conn in connections:
            if conn.get("user_name") in suspected_users:
                try:
                    conn_time = datetime.strptime(conn.get("time", ""), "%Y-%m-%d %H:%M:%S")
                    diff_minutes = abs((conn_time - ban_hit_time).total_seconds() / 60.0)
                    min_diff = min(min_diff, diff_minutes)
                except (ValueError, TypeError):
                    pass
        return min_diff if min_diff != float('inf') else 60

    def _check_time_based_bypass(self, ban_hit_time: datetime, ip_address: str, banned_user_name: str,
                                 connections: List[Dict]) -> List[str]:
        time_suspected_users = []
        if ip_address == "N/A":
            return time_suspected_users
        try:
            for conn in connections:
                if conn.get("ip_address") == ip_address and conn.get("user_name") != banned_user_name:
                    conn_time = conn.get("time", "")
                    if not conn_time:
                        continue
                    try:
                        conn_dt = datetime.strptime(conn_time, "%Y-%m-%d %H:%M:%S")
                        diff_minutes = abs((conn_dt - ban_hit_time).total_seconds() / 60.0)
                        if 5 <= diff_minutes <= 10:
                            time_suspected_users.append(conn.get("user_name"))
                    except ValueError:
                        self.logger.warning(f"Invalid time format: {conn_time}")
        except Exception as ex:
            self.logger.error(f"Error processing time difference for ban hit: {str(ex)}", exc_info=True)
        return time_suspected_users

    def _determine_bypass_success(self, connections, bypass_user_names, ban_time_str, banned_hwid, banned_ip):
        if not bypass_user_names or not connections:
            return "Unknown"
        try:
            ban_time = datetime.strptime(ban_time_str, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return "Unknown"
        successful_logins = []
        unsuccessful_logins = []
        for conn in connections:
            user_name = conn.get("user_name", "")
            if user_name not in bypass_user_names:
                continue
            conn_time_str = conn.get("time", "")
            if not conn_time_str:
                continue
            try:
                conn_time = datetime.strptime(conn_time_str, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
            if conn_time <= ban_time:
                continue
            status = conn.get("status", "")
            if "Denied: Banned" in status:
                unsuccessful_logins.append(conn)
            elif "Accepted" in status:
                successful_logins.append(conn)
        if successful_logins:
            hwid_changed = any(conn.get("hwid", "") != banned_hwid for conn in successful_logins)
            ip_changed = any(conn.get("ip_address", "") != banned_ip for conn in successful_logins)
            if hwid_changed:
                return "Successful Bypass"
            elif ip_changed:
                return "Possibly Successful Bypass"
            else:
                return "Unknown"
        elif unsuccessful_logins:
            return "Unsuccessful Bypass"
        else:
            return "Unknown"