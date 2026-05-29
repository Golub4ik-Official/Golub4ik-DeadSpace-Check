import asyncio
import logging
import random
import time
from dataclasses import dataclass
from typing import Dict, Union, List, Any, Optional, Tuple, OrderedDict
from urllib.parse import urljoin, quote_plus

import aiohttp
import re

try:
    from selectolax.parser import HTMLParser, Node

    LXML_AVAILABLE = True
except ImportError:
    LXML_AVAILABLE = False
    from selectolax.parser import HTMLParser, Node

from config_system import get_config
from utils.performance_monitor import PerformanceStats

N_A = "N/A"


@dataclass
class ConnectionData:
    user_name: str
    user_id: str
    time: str
    ip_address: str
    hwid: str
    status: str
    server: str
    trust_score: str
    ban_hits_link: Optional[str] = None
    connection_id: Optional[str] = None
    is_denied_banned: bool = False

    def get(self, key: str, default=None):
        return getattr(self, key, default)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "user_name": self.user_name,
            "user_id": self.user_id,
            "time": self.time,
            "ip_address": self.ip_address,
            "hwid": self.hwid,
            "status": self.status,
            "server": self.server,
            "trust_score": self.trust_score,
            "ban_hits_link": self.ban_hits_link,
            "connection_id": self.connection_id,
            "is_denied_banned": self.is_denied_banned
        }


class AdminPanel:
    def __init__(self, username: str, password: str) -> None:
        self.logger = logging.getLogger(__name__)
        self.username = username
        self.password = password
        cfg = get_config()
        self.BASE_ADMIN_URL = cfg.api.base_admin_url
        self.ACCOUNT_URL = cfg.api.account_url
        self.PLAYERS_URL = f"{self.BASE_ADMIN_URL}/Players"
        self.CONNECTIONS_URL = f"{self.BASE_ADMIN_URL}/Connections"
        self.BAN_HITS_URL_PATTERN = f"{self.BASE_ADMIN_URL}/Connections/Hits"
        self.PLAYER_INFO_URL_PATTERN = f"{self.BASE_ADMIN_URL}/Players/Info/{{}}"
        self.BANS_URL = f"{self.BASE_ADMIN_URL}/Bans"
        self.LOGIN_RETRY_LIMIT = cfg.api.login_retry_limit
        self.TIMEOUT = aiohttp.ClientTimeout(total=cfg.api.request_timeout)
        self.SLOW_REQUEST_THRESHOLD = 5.0

        self.DEFAULT_PER_PAGE = 2000

        self._connector = None
        self._client_session: Optional[aiohttp.ClientSession] = None

        self.login_attempts = 0
        self._is_authenticated = False
        self._auth_token_timestamp = 0
        self._auth_token_ttl = 1800
        self._request_metrics = {"total": 0, "slow_requests": 0, "errors": 0}
        self._setup_loggers()
        self.perf_stats = PerformanceStats(self.perf_logger)

        self._response_cache: OrderedDict[str, Tuple[str, float]] = OrderedDict()
        self._RESPONSE_CACHE_MAX_SIZE = 1000
        self._RESPONSE_CACHE_TTL = 1800

        self._async_lock = asyncio.Lock()
        self._singleflight_fetches: dict[str, asyncio.Future] = {}

        self.use_lxml = LXML_AVAILABLE
        if not LXML_AVAILABLE:
            self.logger.warning("lxml not available, falling back to html.parser")

        if self.logger.isEnabledFor(logging.INFO):
            self.logger.info(
                f"AdminPanel (async) initialized with URLs: BASE={self.BASE_ADMIN_URL}, "
                f"CONNECTIONS={self.CONNECTIONS_URL}, perPage={self.DEFAULT_PER_PAGE}, "
                f"parser={'lxml' if self.use_lxml else 'html.parser'}"
            )

    def _get_parser_type(self) -> str:
        return "lxml" if self.use_lxml else "html.parser"

    def _parse_html(self, html_content: str) -> HTMLParser:
        try:
            return HTMLParser(html_content)
        except Exception as e:
            self.logger.warning(f"HTML parsing failed: {e}")
            return HTMLParser(html_content)

    def _build_connections_url(self, search: str = "", user_id: str = "", show_accepted: str = "true",
                               show_banned: str = "true", show_whitelist: str = "true",
                               show_full: str = "true", show_panic: str = "true",
                               per_page: Optional[int] = None) -> str:
        if per_page is None:
            per_page = self.DEFAULT_PER_PAGE

        search_term = quote_plus(user_id if user_id else search)
        return (f"{self.BASE_ADMIN_URL}/Connections?perPage={per_page}&showSet=true"
                f"&search={search_term}&showAccepted={show_accepted}&showBanned={show_banned}"
                f"&showWhitelist={show_whitelist}&showFull={show_full}&showPanic={show_panic}")

    def _log_debug(self, msg: str):
        if self.logger.isEnabledFor(logging.DEBUG):
            self.logger.debug(msg)

    def _log_info(self, msg: str):
        if self.logger.isEnabledFor(logging.INFO):
            self.logger.info(msg)

    def _log_warning(self, msg: str):
        if self.logger.isEnabledFor(logging.WARNING):
            self.logger.warning(msg)

    async def _get_html(self, url: str, *, use_cache: bool = True, retry_on_auth: bool = True) -> Tuple[
        Optional[str], bool]:
        if use_cache:
            cached = await self._get_cached_response(url)
            if cached is not None:
                return cached, True

        if retry_on_auth and not await self._ensure_authenticated():
            self._log_warning(f"Authentication failed before request: {url}")
            return None, False

        session = await self._get_session()
        try:
            self._request_metrics["total"] += 1
            async with session.get(url) as resp:
                if resp.status in (401, 403) and retry_on_auth:
                    self._log_warning(f"Auth status {resp.status} for {url}; re-authenticating once.")
                    if await self.login():
                        async with session.get(url) as retry_resp:
                            retry_resp.raise_for_status()
                            html_text = await retry_resp.text()
                            if use_cache and html_text:
                                await self._cache_response(url, html_text)
                            return html_text, False
                resp.raise_for_status()
                html_text = await resp.text()
                if use_cache and html_text:
                    await self._cache_response(url, html_text)
                return html_text, False
        except aiohttp.ClientError as e:
            self._request_metrics["errors"] += 1
            self.logger.error(f"Request error for {url}: {e}")
        except Exception as e:
            self._request_metrics["errors"] += 1
            self.logger.error(f"Unexpected error for {url}: {e}", exc_info=True)
        return None, False

    async def _initialise(self):
        await self.close()

        self._connector = aiohttp.TCPConnector(limit_per_host=8, limit=10, ssl=False)
        self._client_session: Optional[aiohttp.ClientSession] = None

        self.login_attempts = 0
        self._is_authenticated = False
        self._request_metrics = {
            "total": 0,
            "cache_hits": 0,
            "cache_misses": 0,
        }
        self._response_cache: OrderedDict[str, Tuple[str, float]] = OrderedDict()

    def _setup_loggers(self):
        from utils.logging_utils import get_logger
        self.perf_logger = get_logger(f"{__name__}.performance")

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._client_session is None or self._client_session.closed:
            if self._connector is None or self._connector.closed:
                self._connector = aiohttp.TCPConnector(limit_per_host=10, limit=50, ssl=False)
            self._client_session = aiohttp.ClientSession(
                connector=self._connector,
                timeout=self.TIMEOUT,
                headers={
                    "User-Agent": "Mozilla/5.0 (compatible; MyAppBot/1.0)",
                    "Connection": "keep-alive"
                },
                cookie_jar=aiohttp.CookieJar(unsafe=True)
            )
        return self._client_session

    async def close(self):
        if self._client_session and not self._client_session.closed:
            await self._client_session.close()
            if self.logger.isEnabledFor(logging.DEBUG):
                self.logger.debug("Aiohttp client session closed.")

        if self._connector and not self._connector.closed:
            await self._connector.close()
            if self.logger.isEnabledFor(logging.DEBUG):
                self.logger.debug("Aiohttp TCPConnector closed.")

        self._client_session = None

    async def login(self) -> bool:
        async with self._async_lock:
            if self._is_authenticated and (time.time() - self._auth_token_timestamp) < self._auth_token_ttl:
                return True

            session = await self._get_session()
            current_attempts = 0
            while current_attempts < self.LOGIN_RETRY_LIMIT:
                current_attempts += 1
                self.login_attempts = current_attempts
                if self.logger.isEnabledFor(logging.INFO):
                    self.logger.info(f"Login attempt {self.login_attempts}/{self.LOGIN_RETRY_LIMIT}")
                try:
                    start_time = time.time()
                    result = await self._attempt_login(session)
                    elapsed = time.time() - start_time
                    self.perf_stats.record("login", elapsed)
                    if result:
                        self._is_authenticated = True
                        self._auth_token_timestamp = time.time()
                        self.login_attempts = 0
                        return True
                except Exception as e:
                    self.logger.error(f"Login error: {str(e)}", exc_info=True)
                if self.logger.isEnabledFor(logging.WARNING):
                    self.logger.warning(f"Login attempt {self.login_attempts} failed")
                await asyncio.sleep(1)

            self.logger.error(f"Login failed after {self.LOGIN_RETRY_LIMIT} attempts")
            self._is_authenticated = False
            return False

    async def _attempt_login(self, session: aiohttp.ClientSession) -> bool:
        try:
            async with session.get(self.PLAYERS_URL, allow_redirects=False) as response:
                if response.status == 200:
                    if self.logger.isEnabledFor(logging.DEBUG):
                        self.logger.debug("Already logged in (direct access to PLAYERS_URL)")
                    return True

            async with session.get(self.PLAYERS_URL, allow_redirects=True) as response:
                response_text = await response.text()
                response.raise_for_status()

                if str(response.url) == self.PLAYERS_URL:
                    if self.logger.isEnabledFor(logging.DEBUG):
                        self.logger.debug("Already logged in (redirected to PLAYERS_URL)")
                    return True

                if self.ACCOUNT_URL not in str(response.url):
                    self.logger.error(
                        f"Unexpected redirect during login. Expected to be on '{self.ACCOUNT_URL}', but was redirected to '{response.url}'. The website's login flow may have changed.")
                    return False

                sso_login_url = str(response.url)

                soup = self._parse_html(response_text)
                token_input = soup.css_first("input[name='__RequestVerificationToken']")
                if not token_input or not token_input.attributes.get("value"):
                    self.logger.error(
                        f"Anti-forgery token not found on the login page ({sso_login_url}). This is a critical part of the login process. The page structure might have changed.")
                    if self.logger.isEnabledFor(logging.DEBUG):
                        self.logger.debug(f"HTML content where token was expected:\n{response_text[:2000]}")
                    return False
                token = token_input.attributes["value"]

                payload = {
                    "Input.EmailOrUsername": self.username,
                    "Input.Password": self.password,
                    "__RequestVerificationToken": token
                }
                headers = {
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Referer": sso_login_url,
                    "Origin": self.ACCOUNT_URL.rstrip('/'),
                }

            async with session.post(sso_login_url, data=payload, headers=headers, allow_redirects=True) as response:
                response_text = await response.text()
                response.raise_for_status()
                final_url = str(response.url)

                if f"{self.BASE_ADMIN_URL}/signin-oidc" in response_text:
                    self.logger.info("OIDC redirect detected, processing...")
                    soup_oidc = self._parse_html(response_text)
                    form = soup_oidc.css_first("form[action*='signin-oidc']")
                    if not form:
                        form = soup_oidc.css_first("form")

                    if not form:
                        self.logger.error(
                            "signin-oidc: Redirect form not found. This is an expected part of the OIDC authentication flow and its absence is an error.")
                        if self.logger.isEnabledFor(logging.DEBUG):
                            self.logger.debug(f"Page content for missing OIDC form:\n{response_text[:1500]}")
                        return "Logout" in response_text or "Players" in response_text

                    redirect_action_url = form.attributes.get("action")
                    if not redirect_action_url:
                        self.logger.error(
                            "signin-oidc: Redirect form 'action' URL not found. Cannot complete authentication.")
                        return False

                    redirect_action_url = urljoin(final_url, redirect_action_url)
                    inputs = form.css("input")
                    form_data = {inp.attributes.get("name"): inp.attributes.get("value", "") for inp in inputs if
                                 inp.attributes.get("name")}

                    async with session.post(redirect_action_url, data=form_data, headers={"Referer": final_url},
                                            allow_redirects=True) as final_response:
                        final_response_text = await final_response.text()
                        final_response.raise_for_status()
                        if "Logout" in final_response_text or "Players" in final_response_text or self.BASE_ADMIN_URL in str(
                                final_response.url):
                            self.logger.info("Successfully authenticated after OIDC redirect.")
                            return True
                        else:
                            self.logger.error(
                                "Authentication failed after OIDC redirect. The final page did not contain expected content ('Logout'/'Players').")
                            if self.logger.isEnabledFor(logging.DEBUG):
                                self.logger.debug(
                                    f"Final OIDC response URL: {str(final_response.url)}\nFinal OIDC response text (snippet):\n{final_response_text[:2000]}")
                            return False

                elif "Logout" in response_text or "Players" in response_text or self.BASE_ADMIN_URL in final_url:
                    self.logger.info("Successfully authenticated.")
                    return True

                else:
                    self.logger.warning(
                        "Authentication failed. The final page did not contain expected success markers (like 'Logout' or 'Players' links).")

                    if "Invalid login attempt" in response_text or "Invalid credentials" in response_text:
                        self.logger.error("LOGIN FAILURE REASON: Invalid username or password.")
                    elif "Two-Factor" in response_text or "2FA" in response_text:
                        self.logger.error(
                            "LOGIN FAILURE REASON: Two-Factor Authentication (2FA) is likely required. This script cannot handle 2FA prompts.")
                    elif "CAPTCHA" in response_text:
                        self.logger.error("LOGIN FAILURE REASON: A CAPTCHA was detected. The script cannot solve this.")
                    elif final_url == sso_login_url:
                        self.logger.error(
                            f"LOGIN FAILURE REASON: The script is still on the login page ('{final_url}'). This almost always means the credentials are incorrect.")
                    else:
                        self.logger.error(
                            f"LOGIN FAILURE REASON: Unknown. The script landed on an unexpected page ('{final_url}').")

                    self.logger.debug(f"--- LOGIN FAILURE DIAGNOSTICS ---\n"
                                      f"Final URL: {final_url}\n"
                                      f"HTTP Status: {response.status}\n"
                                      f"Response HTML (first 2000 chars):\n{response_text[:2000]}\n"
                                      f"--- END DIAGNOSTICS ---")
                    return False

        except aiohttp.ClientError as e:
            self.logger.error(
                f"A network error occurred during login: {str(e)}. Check the connection to the server and DNS resolution.",
                exc_info=True)
            return False
        except Exception as e:
            self.logger.error(f"An unexpected programming error occurred during the login process: {str(e)}",
                              exc_info=True)
            return False

    async def _ensure_authenticated(self) -> bool:
        async with self._async_lock:
            if not self._is_authenticated or (time.time() - self._auth_token_timestamp) >= self._auth_token_ttl:
                if self.logger.isEnabledFor(logging.DEBUG):
                    self.logger.debug("Authentication required or expired. Attempting login.")
                return await self.login()
        return True

    def _parse_connection_row(self, row_node: Node) -> Optional[ConnectionData]:
        try:
            cols = row_node.css("td")
            col_count = len(cols)
            if col_count < 8:
                if self.logger.isEnabledFor(logging.DEBUG):
                    self.logger.debug(
                        f"Too few columns in connection row: {col_count}. Row HTML: {row_node.html[:200]}")
                return None

            ban_hits_link, connection_id = None, None
            if col_count >= 9:
                link_tag = cols[8].css_first("a")
                if link_tag:
                    raw_link = link_tag.attributes.get("href")
                    if raw_link and raw_link.strip() != "#":
                        potential_ban_hits_link = urljoin(self.BASE_ADMIN_URL, raw_link)
                        if "connection=" in potential_ban_hits_link:
                            ban_hits_link = potential_ban_hits_link
                            try:
                                connection_id = ban_hits_link.split("connection=", 1)[1].split("&", 1)[0]
                            except IndexError:
                                if self.logger.isEnabledFor(logging.WARNING):
                                    self.logger.warning(
                                        f"Could not parse connection_id from ban_hits_link: {ban_hits_link}")

            user_name_el = cols[0].css_first("strong")
            user_name = user_name_el.text(strip=True) if user_name_el else cols[0].text(strip=True)
            user_id = cols[1].text(strip=True)
            time_val = cols[2].text(strip=True)
            ip_address = cols[3].text(strip=True)
            hwid = cols[4].text(strip=True)
            status_el = cols[5].css_first("strong")
            status = status_el.text(strip=True) if status_el else cols[5].text(strip=True)
            server = cols[6].text(strip=True)
            trust_score = cols[7].text(strip=True)

            return ConnectionData(
                user_name=user_name, user_id=user_id, time=time_val, ip_address=ip_address,
                hwid=hwid, status=status, server=server, trust_score=trust_score,
                ban_hits_link=ban_hits_link, connection_id=connection_id,
                is_denied_banned=("Denied: Banned" in status)
            )
        except Exception as e:
            self.logger.error(f"Error parsing connection row: {str(e)}", exc_info=True)
            if self.logger.isEnabledFor(logging.DEBUG):
                self.logger.debug(f"Problematic row HTML: {row_node.html[:500]}")
            return None

    def _parse_connections_table(self, soup: HTMLParser,
                                 existing_data_sets: Optional[Dict[str, set]] = None) -> Tuple[
        List[ConnectionData], bool]:
        connections = []
        has_new_info = True

        if existing_data_sets is None:
            existing_data_sets = {'user_names': set(), 'user_ids': set(), 'ips': set(), 'hwids': set()}

        table = soup.css_first("table.table")
        if not table:
            if self.logger.isEnabledFor(logging.DEBUG):
                self.logger.debug("No table.table found in the HTML")
            return connections, False

        tbody = table.css_first("tbody")
        if not tbody:
            if self.logger.isEnabledFor(logging.DEBUG):
                self.logger.debug("No tbody found in the table")
            return connections, False

        rows = tbody.css("tr")
        is_search_page_context = bool(soup.css_first("form[action*='search='], form input[name='search']"))

        if not rows and is_search_page_context and self.logger.isEnabledFor(logging.INFO):
            self.logger.info("Found 0 <tr> rows in <tbody> on what appears to be a search results page.")

        if self.logger.isEnabledFor(logging.DEBUG):
            self.logger.debug(f"Found {len(rows)} rows in the connections table to process.")

        new_user_names = set()
        new_user_ids = set()
        new_ips = set()
        new_hwids = set()

        existing_names = existing_data_sets['user_names']
        existing_ids = existing_data_sets['user_ids']
        existing_ips = existing_data_sets['ips']
        existing_hwids = existing_data_sets['hwids']

        for row_idx, row_node in enumerate(rows):
            conn = self._parse_connection_row(row_node)
            if conn:
                connections.append(conn)

                if conn.user_name and conn.user_name not in existing_names:
                    new_user_names.add(conn.user_name)
                if conn.user_id and conn.user_id not in existing_ids:
                    new_user_ids.add(conn.user_id)
                if conn.ip_address and conn.ip_address != N_A and conn.ip_address not in existing_ips:
                    new_ips.add(conn.ip_address)
                if conn.hwid and conn.hwid != N_A and conn.hwid not in existing_hwids:
                    new_hwids.add(conn.hwid)

            elif self.logger.isEnabledFor(logging.WARNING):
                self.logger.warning(
                    f"Failed to parse connection data from row {row_idx}. Row content snippet (debug): {row_node.html[:300]}")

        existing_data_sets['user_names'].update(new_user_names)
        existing_data_sets['user_ids'].update(new_user_ids)
        existing_data_sets['ips'].update(new_ips)
        existing_data_sets['hwids'].update(new_hwids)

        has_new_info = bool(new_user_names or new_user_ids or new_ips or new_hwids)

        if self.logger.isEnabledFor(logging.DEBUG) and existing_data_sets:
            self.logger.debug(
                f"Page processing: {len(connections)} connections, "
                f"new: {len(new_user_names)} names, {len(new_user_ids)} IDs, "
                f"{len(new_ips)} IPs, {len(new_hwids)} HWIDs. Has new info: {has_new_info}"
            )

        return connections, has_new_info

    def _get_next_page_link(self, soup: HTMLParser) -> Optional[str]:
        next_page_link_tag = soup.css_first("a.page-link[rel='next']")
        if next_page_link_tag:
            href = next_page_link_tag.attributes.get('href')
            if href and href.strip() != '#':
                return urljoin(self.BASE_ADMIN_URL, href)

        potential_next_buttons = soup.css("a.btn")
        for btn_link_tag in potential_next_buttons:
            if "Next" not in btn_link_tag.text(strip=True):
                continue
            if "disabled" in btn_link_tag.attributes.get("class", ""):
                continue
            href_value = btn_link_tag.attributes.get("href")
            if not href_value or href_value.strip() == "#":
                continue
            if "page=" in href_value.lower() or "pageindex=" in href_value.lower():
                return urljoin(self.BASE_ADMIN_URL, href_value)
        return None

    async def _get_cached_response(self, url: str) -> Optional[str]:
        async with self._async_lock:
            cache_entry = self._response_cache.get(url)
            if cache_entry:
                html, timestamp = cache_entry
                if time.time() - timestamp < self._RESPONSE_CACHE_TTL:
                    self._response_cache.move_to_end(url)
                    self._request_metrics["cache_hits"] = self._request_metrics.get("cache_hits", 0) + 1
                    return html
                else:
                    del self._response_cache[url]
            return None

    async def _cache_response(self, url: str, html: str) -> None:
        async with self._async_lock:
            self._response_cache[url] = (html, time.time())
            self._request_metrics["cache_misses"] = self._request_metrics.get("cache_misses", 0) + 1
            if len(self._response_cache) > self._RESPONSE_CACHE_MAX_SIZE:
                self._response_cache.popitem(last=False)

    async def _make_request(self, url: str) -> Optional[str]:
        if not await self._ensure_authenticated():
            self.logger.error(f"Authentication failed before making request to {url}")
            return None

        session = await self._get_session()
        try:
            async with session.get(url) as response:
                if response.status in (401, 403):
                    self.logger.warning(
                        f"Request to {url} failed with status {response.status}. Re-authenticating and retrying once.")
                    if not await self.login():
                        self.logger.error("Re-login attempt failed. Aborting request.")
                        return None

                    async with session.get(url) as retry_response:
                        retry_response.raise_for_status()
                        return await retry_response.text()

                response.raise_for_status()
                return await response.text()

        except aiohttp.ClientError as e:
            self.logger.error(f"Aiohttp client error during request to {url}: {e}")
            return None
        except Exception as e:
            self.logger.error(f"Unexpected error during request to {url}: {e}", exc_info=True)
            return None

    async def fetch_paginated_data(self, url: str, max_pages: int = 0,
                                   enable_early_stop: bool = False) -> List[ConnectionData]:
        key = f"{url}|{max_pages}|{enable_early_stop}"
        existing = self._singleflight_fetches.get(key)
        if existing:
            try:
                return await existing
            except Exception:
                pass

        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._singleflight_fetches[key] = fut

        try:
            result = await self._fetch_paginated_data_inner(url, max_pages=max_pages,
                                                            enable_early_stop=enable_early_stop)
            if not fut.done():
                fut.set_result(result)
            return result
        except Exception as e:
            if not fut.done():
                fut.set_exception(e)
            raise
        finally:
            self._singleflight_fetches.pop(key, None)

    async def _fetch_paginated_data_inner(self, url: str, max_pages: int = 0,
                                          enable_early_stop: bool = False) -> List[ConnectionData]:
        self._log_info(
            f"Fetching paginated data from URL: {url}, max_pages={max_pages if max_pages > 0 else 'unlimited'}, early_stop={enable_early_stop}")

        all_connections: List[ConnectionData] = []
        current_url: Optional[str] = url
        page_num = 1
        pages_fetched = 0
        start_time_total = time.time()

        base_total = getattr(self.TIMEOUT, 'total', 90) or 90
        global_timeout = min(base_total + 30, base_total * 1.4)
        slow_page_threshold = max(self.SLOW_REQUEST_THRESHOLD, min(20.0, base_total * 0.35))

        consecutive_slow_pages = 0
        consecutive_empty_pages = 0

        existing_data_sets = {'user_names': set(), 'user_ids': set(), 'ips': set(),
                              'hwids': set()} if enable_early_stop else None
        pages_without_new_info = 0
        max_pages_without_info = 2

        try:
            async with asyncio.timeout(global_timeout):
                while current_url:
                    if max_pages > 0 and pages_fetched >= max_pages:
                        self._log_info(f"Reached max pages limit ({max_pages}) after fetching {pages_fetched} pages.")
                        break

                    self._log_debug(f"Fetching page {page_num} from URL: {current_url}")

                    req_start_time = time.time()
                    html_content: Optional[str] = None
                    from_cache = False

                    dynamic_factor = 0.6 - (consecutive_slow_pages * 0.1)
                    if dynamic_factor < 0.3:
                        dynamic_factor = 0.3
                    per_page_timeout = min(max(20, int(base_total * dynamic_factor)), int(base_total))

                    attempt = 0
                    while attempt < 2 and html_content is None:
                        attempt += 1
                        try:
                            async with asyncio.timeout(per_page_timeout):
                                html_content, from_cache = await self._get_html(current_url, use_cache=True)
                        except asyncio.TimeoutError:
                            if attempt < 2:
                                self.logger.warning(
                                    f"Timeout fetching page {page_num} (attempt {attempt}) for {current_url}; retrying once (timeout={per_page_timeout}s)...")
                                await asyncio.sleep(1 + random.random())
                            else:
                                self.logger.error(
                                    f"Timed out fetching page {page_num} for {current_url} after {per_page_timeout}s (final attempt) — keeping partial results.")
                        except Exception as e:
                            self.logger.error(f"Unexpected error fetching page {page_num} for {current_url}: {e}")
                            break

                    req_elapsed_time = time.time() - req_start_time
                    if req_elapsed_time > self.SLOW_REQUEST_THRESHOLD and html_content and not from_cache:
                        self._request_metrics["slow_requests"] += 1
                        consecutive_slow_pages += 1
                        if self.perf_logger.isEnabledFor(logging.DEBUG):
                            log_url_display = current_url[:67] + "..." if len(current_url) > 70 else current_url
                            self.perf_logger.debug(f"Slow request ({req_elapsed_time:.2f}s): {log_url_display}")
                    else:
                        if req_elapsed_time <= slow_page_threshold:
                            consecutive_slow_pages = 0

                    if not html_content:
                        self.logger.warning(
                            f"Stopping pagination at page {page_num}; no HTML content retrieved (timeout or error). Returning {len(all_connections)} partial connections.")
                        break

                    self._log_debug(f"Page {page_num} response length: {len(html_content)}. Parsing...")
                    soup = self._parse_html(html_content)

                    if enable_early_stop and existing_data_sets is not None:
                        connections_on_page, has_new_info = self._parse_connections_table(soup, existing_data_sets)
                        if not has_new_info:
                            pages_without_new_info += 1
                            self._log_info(
                                f"Page {page_num} provided no new information (streak: {pages_without_new_info})")
                        else:
                            pages_without_new_info = 0

                        if pages_without_new_info >= max_pages_without_info:
                            self._log_info(
                                f"Early stopping: {pages_without_new_info} consecutive pages without new information")
                            break
                    else:
                        connections_on_page, _ = self._parse_connections_table(soup)

                    prev_total = len(all_connections)
                    all_connections.extend(connections_on_page)
                    pages_fetched += 1

                    if not connections_on_page:
                        consecutive_empty_pages += 1
                    else:
                        consecutive_empty_pages = 0

                    if consecutive_empty_pages >= 2:
                        self._log_info(
                            f"Encountered {consecutive_empty_pages} consecutive empty pages; stopping early.")
                        break

                    is_likely_search_page = "search=" in current_url.lower()
                    if not connections_on_page and is_likely_search_page and page_num == 1:
                        self._log_info(
                            f"Search results page {current_url} (page {page_num}) yielded no connections. Assuming end of relevant results.")
                        current_url = None
                    else:
                        next_url = self._get_next_page_link(soup)
                        current_url = next_url

                    if current_url:
                        page_num += 1
                        await asyncio.sleep(0.05 + random.random() * 0.1)

        except asyncio.TimeoutError:
            elapsed = time.time() - start_time_total
            self.logger.warning(
                f"Global pagination timeout after {elapsed:.1f}s for base URL {url}. Returning {len(all_connections)} partial connections from {pages_fetched} page(s).")
        except asyncio.CancelledError:
            self.logger.warning(
                f"Pagination task cancelled for {url}; returning {len(all_connections)} partial connections.")
            raise
        finally:
            total_elapsed_time = time.time() - start_time_total
            self.perf_stats.record("fetch_paginated_data", total_elapsed_time)
            early_stop_info = ""
            if enable_early_stop and existing_data_sets is not None:
                early_stop_info = f", early_stop_triggered={'yes' if pages_without_new_info >= max_pages_without_info else 'no'}"
            self._log_info(
                f"Fetched {len(all_connections)} connections from {pages_fetched} page(s) in {total_elapsed_time:.2f}s "
                f"(partial={'yes' if current_url else 'no'}{early_stop_info})"
            )

        return all_connections

    def get_connections_url(self, user_id: str = "", search: str = "", show_accepted: str = "true",
                            show_banned: str = "true", show_whitelist: str = "true", show_full: str = "true",
                            show_panic: str = "true") -> str:
        return self._build_connections_url(
            search=search, user_id=user_id, show_accepted=show_accepted,
            show_banned=show_banned, show_whitelist=show_whitelist,
            show_full=show_full, show_panic=show_panic
        )

    async def fetch_connections_for_user(self, user_id: str, enable_early_stop: bool = False) -> List[Dict[str, Any]]:
        url = self.get_connections_url(user_id=user_id)
        if self.logger.isEnabledFor(logging.DEBUG):
            self.logger.debug(
                f"Fetching connections for user_id: {user_id} from URL: {url} (early_stop={enable_early_stop})")
        start_time = time.time()
        connections = await self.fetch_paginated_data(url, enable_early_stop=enable_early_stop)
        elapsed = time.time() - start_time
        self.perf_stats.record(f"fetch_connections_for_user", elapsed)
        connection_dicts = [conn.to_dict() for conn in connections]
        if self.logger.isEnabledFor(logging.DEBUG):
            self.logger.debug(f"Found {len(connection_dicts)} connections for user_id: {user_id}")
        return connection_dicts

    async def check_account_on_site(self, url: str, single_user: bool = False,
                                    enable_early_stop: bool = False) -> Union[
        List[Dict[str, Any]], Dict[str, Union[str, List[str], bool, int]]]:
        if self.logger.isEnabledFor(logging.DEBUG):
            self.logger.debug(
                f"Checking account on site: url={url}, single_user={single_user}, early_stop={enable_early_stop}")
        start_time = time.time()
        connections_data = await self.fetch_paginated_data(url, enable_early_stop=enable_early_stop)
        elapsed = time.time() - start_time
        self.perf_stats.record("check_account_on_site", elapsed)
        if self.logger.isEnabledFor(logging.DEBUG):
            self.logger.debug(f"Found {len(connections_data)} connections for URL: {url}")

        if single_user:
            if self.logger.isEnabledFor(logging.DEBUG):
                self.logger.debug("Aggregating single user info from connections data.")
            result = await self.aggregate_single_user_info(connections_data, fetch_player_details=True)
            if self.logger.isEnabledFor(logging.DEBUG):
                self.logger.debug(f"Aggregated result for single user, status: {result.get('status', 'unknown')}")
            return result

        connection_dicts = [conn.to_dict() for conn in connections_data]
        if self.logger.isEnabledFor(logging.DEBUG):
            self.logger.debug(f"Returning {len(connection_dicts)} raw connection dicts.")
        return connection_dicts

    async def fetch_player_info(self, user_id: str) -> Dict[str, Union[int, List[Dict[str, str]]]]:
        if not await self._ensure_authenticated():
            self._log_warning(f"Not authenticated, cannot fetch player info for {user_id}")
            return {"ban_counts": 0, "ban_reasons": []}

        info_result: Dict[str, Union[int, List[Dict[str, str]]]] = {"ban_counts": 0, "ban_reasons": []}
        info_url = self.PLAYER_INFO_URL_PATTERN.format(user_id)
        self._log_debug(f"Fetching player info from URL: {info_url}")

        start_time = time.time()
        from_cache = False
        try:
            html_content, from_cache = await self._get_html(info_url, use_cache=True)
            if not html_content:
                self.logger.error(f"Failed to get HTML content for player info: {user_id}")
                return info_result

            soup = self._parse_html(html_content)
            player_name = "Unknown"
            name_header = soup.css_first("h1")
            if name_header:
                name_text = name_header.text(strip=True)
                if "information for" in name_text.lower():
                    parts = name_text.split("information for ", 1)
                    if len(parts) > 1:
                        player_name = parts[1].strip()
                    else:
                        parts_no_space = name_text.lower().split("information for", 1)
                        if len(parts_no_space) > 1:
                            player_name = name_text[len(name_text) - len(parts_no_space[1]):].strip()

            ban_table_node = None
            for h2_node in soup.css("h2"):
                text = h2_node.text(strip=True)
                if "Bans" in text and "Role Bans" not in text:
                    current_node = h2_node.next
                    while current_node:
                        if current_node.tag == 'table' and 'table' in current_node.attributes.get('class', ''):
                            ban_table_node = current_node
                            break
                        if current_node.tag == 'h2':
                            break
                        current_node = current_node.next
                    break

            if ban_table_node:
                ban_body = ban_table_node.css_first("tbody")
                if ban_body:
                    ban_info_list: List[Dict[str, str]] = []
                    rows = ban_body.css("tr")
                    col_indices = {}
                    header_row = ban_table_node.css_first("thead tr")
                    if header_row:
                        for idx, th in enumerate(header_row.css("th, td")):
                            col_indices[th.text(strip=True).lower()] = idx

                    def _cell(idx, fallback=None):
                        nonlocal col_indices
                        if idx is not None and len(cols) > idx:
                            return cols[idx].text(separator=' ', strip=True)
                        if fallback is not None:
                            for fb in (fallback if isinstance(fallback, (list, tuple)) else [fallback]):
                                if len(cols) > fb:
                                    return cols[fb].text(separator=' ', strip=True)
                        return "N/A"

                    def _cell_raw(idx):
                        if len(cols) > idx:
                            return cols[idx].text(separator=' ', strip=True)
                        return ""

                    def _clean_date_cell(text):
                        if not text:
                            return text
                        m = re.search(r'\d{4}-\d{2}-\d{2}(?:\s+\d{2}:\d{2}:\d{2})?', text)
                        if m:
                            return m.group(0)
                        return text.strip()

                    for row_idx, row_node in enumerate(rows):
                        cols = row_node.css("td")
                        if cols and len(cols) >= 2:
                            ban_reason = _cell_raw(1)
                            banned_username_for_entry = player_name
                            name_cell_content_strong = cols[0].css_first("strong")
                            if name_cell_content_strong:
                                banned_username_for_entry = name_cell_content_strong.text(separator=' ', strip=True)
                            else:
                                potential_name_in_cell = _cell_raw(0)
                                if potential_name_in_cell and potential_name_in_cell.lower() != player_name.lower():
                                    if not any(x in potential_name_in_cell for x in ["N/A", "User ID", "IP", "HWID"]):
                                        banned_username_for_entry = potential_name_in_cell

                            admin_name = "N/A"
                            for try_key in ["admin", "issued by", "выдал", "moderator", "staff", "administrator"]:
                                if try_key in col_indices:
                                    val = _cell(col_indices[try_key])
                                    if val and val.lower() not in ("n/a", "server", "role ban", "", "unknown", "-"):
                                        admin_name = val
                                        break
                            if admin_name == "N/A":
                                for idx in [5, 4, 3, 6, 7]:
                                    val = _cell_raw(idx)
                                    if val and val.lower() not in ("n/a", "server", "role ban", "", "unknown", "-", "permanent", "temporary", "never", "local", "127.0.0.1", "none"):
                                        admin_name = val
                                        break

                            ban_type = "N/A"
                            for try_key in ["type", "ban type", "категория", "тип"]:
                                if try_key in col_indices:
                                    ban_type = _cell(col_indices[try_key])
                                    break
                            if ban_type == "N/A":
                                val = _cell_raw(2)
                                if val and val.lower() not in ("n/a", "", "unknown", "-", banned_username_for_entry.lower()):
                                    ban_type = val

                            ban_date = "N/A"
                            for try_key in ["ban time", "time", "issued", "date", "timestamp", "дата", "когда"]:
                                if try_key in col_indices:
                                    ban_date = _clean_date_cell(_cell(col_indices[try_key]))
                                    break
                            if ban_date == "N/A":
                                val = _cell_raw(3)
                                if val and val.lower() not in ("n/a", "", "unknown", "-", "never", "permanent"):
                                    ban_date = _clean_date_cell(val)

                            ban_expires = "Никогда"
                            for try_key in ["expires", "expiration", "expiry", "истекает", "срок"]:
                                if try_key in col_indices:
                                    ban_expires = _clean_date_cell(_cell(col_indices[try_key]))
                                    break
                            if ban_expires == "Никогда":
                                val = _cell_raw(4)
                                if val and val.lower() not in ("n/a", "", "unknown", "-"):
                                    ban_expires = _clean_date_cell(val)

                            ban_info_list.append({
                                "reason": ban_reason, "username": banned_username_for_entry,
                                "admin": admin_name, "type": ban_type,
                                "date": ban_date, "expires": ban_expires
                            })
                        elif self.logger.isEnabledFor(logging.WARNING):
                            self.logger.warning(
                                f"Ban table row {row_idx} for {user_id} has < 2 columns: {row_node.html[:200]}")
                    info_result["ban_reasons"] = ban_info_list
                    info_result["ban_counts"] = len(ban_info_list)
            elif self.logger.isEnabledFor(logging.DEBUG):
                self.logger.debug(f"No bans table found for player {user_id} on their info page.")

        except aiohttp.ClientResponseError as e:
            if e.status == 404:
                self._log_debug(f"Player profile not found (404) for user_id: {user_id} at {info_url}")
            else:
                self._request_metrics["errors"] += 1
                self.logger.error(f"HTTP error fetching player info for {user_id} from {info_url}: {str(e)}")
        except aiohttp.ClientError as e:
            self._request_metrics["errors"] += 1
            self.logger.error(f"Request error fetching player info for {user_id} from {info_url}: {str(e)}")
        except Exception as e:
            self._request_metrics["errors"] += 1
            self.logger.error(f"Error parsing player info for {user_id} from {info_url}: {str(e)}", exc_info=True)

        elapsed_time = time.time() - start_time
        self.perf_stats.record("fetch_player_info", elapsed_time)
        if elapsed_time > self.SLOW_REQUEST_THRESHOLD and not from_cache:
            if self.perf_logger.isEnabledFor(logging.DEBUG):
                self.perf_logger.debug(f"Slow player info fetch: {elapsed_time:.2f}s for user {user_id}")
        return info_result

    async def aggregate_single_user_info(self, connections: List[Union[ConnectionData, Dict[str, Any]]],
                                         fetch_player_details: bool = True) -> Dict[
        str, Union[str, List[str], bool, int]]:
        if self.logger.isEnabledFor(logging.DEBUG):
            self.logger.debug(
                f"Aggregating user info from {len(connections)} connections (fetch_details={fetch_player_details})")

        result: Dict[str, Any] = {
            "status": "unknown", "nicknames": set(), "ban_counts": 0, "ban_reasons": set(),
            "shared_hwid_nicknames": set(), "associated_ips": {}, "associated_hwids": {},
            "user_id": N_A, "connection_link": N_A, "denied_banned_connections": []
        }

        if not connections:
            self.logger.warning("No connections provided to aggregate_single_user_info. Returning empty aggregation.")
            result["nicknames"], result["ban_reasons"], result["shared_hwid_nicknames"] = [], [], []
            return result

        all_ips, all_hwids = {}, {}
        banned_status_found, denied_banned_status_found = False, False
        first_valid_conn_id = None

        for conn_data in connections:
            if isinstance(conn_data, ConnectionData):
                nickname, ip, hwid_val, status_txt, curr_uid, time_val, srv, curr_conn_id, is_den_ban = \
                    conn_data.user_name, conn_data.ip_address, conn_data.hwid, conn_data.status, conn_data.user_id, \
                        conn_data.time, conn_data.server, conn_data.connection_id, conn_data.is_denied_banned
            elif isinstance(conn_data, dict):
                nickname, ip, hwid_val, status_txt, curr_uid, time_val, srv, curr_conn_id = \
                    conn_data.get("user_name", ""), conn_data.get("ip_address", ""), conn_data.get("hwid", ""), \
                        conn_data.get("status", ""), conn_data.get("user_id", ""), conn_data.get("time", ""), \
                        conn_data.get("server", ""), conn_data.get("connection_id")
                is_den_ban = "Denied: Banned" in status_txt
            else:
                self.logger.warning(f"Unexpected connection data type: {type(conn_data)}")
                continue

            if curr_uid and curr_uid != N_A and result["user_id"] == N_A:
                result["user_id"] = curr_uid
            if not first_valid_conn_id and curr_conn_id:
                first_valid_conn_id = curr_conn_id
            if nickname:
                result["nicknames"].add(nickname)
            if ip and ip != N_A:
                all_ips.setdefault(ip, set()).add(nickname)
            if hwid_val and hwid_val != N_A:
                all_hwids.setdefault(hwid_val, set()).add(nickname)

            if status_txt:
                if "Accepted" in status_txt and result["status"] == "unknown":
                    result["status"] = "clean"
                if is_den_ban:
                    denied_banned_status_found = True
                    result["denied_banned_connections"].append({
                        "user_name": nickname, "time": time_val, "ip_address": ip,
                        "hwid": hwid_val, "server": srv, "status": status_txt})
                elif "Banned" in status_txt:
                    banned_status_found = True

        if denied_banned_status_found:
            result["status"], result["ban_counts"] = "banned", max(result["ban_counts"], 1)
            if self.logger.isEnabledFor(logging.DEBUG):
                self.logger.debug("Status set to 'banned' due to 'Denied: Banned' connections.")
        elif banned_status_found and result["status"] != "banned":
            result["status"] = "banned"
            if self.logger.isEnabledFor(logging.DEBUG):
                self.logger.debug("Status set to 'banned' due to 'Banned' status in connections.")

        if first_valid_conn_id:
            result["connection_link"] = f"{self.BASE_ADMIN_URL}/Connections/Info/{first_valid_conn_id}"

        final_uid_fetch = result["user_id"]
        if fetch_player_details and final_uid_fetch and final_uid_fetch != N_A:
            enrich_timeout = min(30, getattr(self.TIMEOUT, 'total', 90) or 90)
            try:
                async with asyncio.timeout(enrich_timeout):
                    player_page_info = await self.fetch_player_info(final_uid_fetch)
                result["ban_counts"] = max(result["ban_counts"], player_page_info.get("ban_counts", 0))
                for ban_entry in player_page_info.get("ban_reasons", []):
                    if isinstance(ban_entry, dict) and "reason" in ban_entry and "username" in ban_entry:
                        result["ban_reasons"].add((ban_entry["reason"], ban_entry["username"],
                                                    ban_entry.get("admin", "N/A"),
                                                    ban_entry.get("type", "N/A"),
                                                    ban_entry.get("date", "N/A"),
                                                    ban_entry.get("expires", "Никогда")))
                    elif self.logger.isEnabledFor(logging.WARNING):
                        self.logger.warning(f"Malformed ban entry from fetch_player_info: {ban_entry}")
            except asyncio.TimeoutError:
                self.logger.warning(
                    f"Timeout fetching player info for {final_uid_fetch} after {enrich_timeout}s; proceeding without extra ban reasons.")
            except Exception as e:
                self.logger.error(f"Error fetching player info for {final_uid_fetch}: {e}")
        elif not fetch_player_details and self.logger.isEnabledFor(logging.DEBUG):
            self.logger.debug(f"Skipping player details fetch for {final_uid_fetch} (delayed enrichment)")

        result["associated_ips"] = {ip_k: sorted(list(nicks_v)) for ip_k, nicks_v in all_ips.items()}
        result["associated_hwids"] = {hwid_k: sorted(list(nicks_v)) for hwid_k, nicks_v in all_hwids.items()}
        for hwid_k, nicks_s in all_hwids.items():
            if len(nicks_s) > 1 and hwid_k != N_A:
                result["shared_hwid_nicknames"].update(nicks_s)

        if result["ban_counts"] > 0 and result["status"] != "banned":
            result["status"] = "banned"
        if result["ban_counts"] >= 5 and result["status"] == "banned":
            result["status"] = "suspicious"

        result["nicknames"] = sorted(list(result["nicknames"]))
        result["ban_reasons"] = [
            {"reason": r, "username": u, "admin": a, "type": t, "date": d, "expires": e}
            for r, u, a, t, d, e in sorted(list(result["ban_reasons"]))
        ]
        result["shared_hwid_nicknames"] = sorted(list(result["shared_hwid_nicknames"]))
        result["raw_html_snippet"] = []
        for conn_prev in connections[:10]:
            if isinstance(conn_prev, ConnectionData):
                result["raw_html_snippet"].append(
                    {"time": conn_prev.time, "status": conn_prev.status, "user_name": conn_prev.user_name})
            elif isinstance(conn_prev, dict):
                result["raw_html_snippet"].append(
                    {"time": conn_prev.get("time", ""), "status": conn_prev.get("status", ""),
                     "user_name": conn_prev.get("user_name", "")})

        if self.logger.isEnabledFor(logging.DEBUG):
            self.logger.debug(
                f"Aggregation complete for user_id '{result['user_id']}': status={result['status']}, "
                f"nicknames_count={len(result['nicknames'])}, ban_counts={result['ban_counts']}, "
                f"details_fetched={fetch_player_details}"
            )
        return result

    async def fetch_ban_hit_connections(self, max_pages: int = 0) -> List[Dict[str, str]]:
        url = self._build_connections_url(show_banned="true", search="")
        if self.logger.isEnabledFor(logging.DEBUG):
            self.logger.debug(f"Fetching ban hit connections, max_pages={max_pages if max_pages > 0 else 'unlimited'}")

        connections_data = await self.fetch_paginated_data(url, max_pages=max_pages)
        ban_hit_list = [conn.to_dict() for conn in connections_data if conn.is_denied_banned and conn.ban_hits_link]
        if self.logger.isEnabledFor(logging.DEBUG):
            self.logger.debug(
                f"Found {len(ban_hit_list)} connections with 'Denied: Banned' status and a ban_hits_link.")
        return ban_hit_list

    async def fetch_ban_info(self, ban_hits_link: str) -> List[Dict[str, str]]:
        if not ban_hits_link:
            self._log_warning("fetch_ban_info called with empty ban_hits_link.")
            return []
        if not await self._ensure_authenticated():
            self._log_warning(f"Not authenticated, cannot fetch ban info from {ban_hits_link}")
            return []

        ban_entries: List[Dict[str, str]] = []
        self._log_debug(f"Fetching ban info from URL: {ban_hits_link}")

        start_time = time.time()
        from_cache = False
        try:
            html_content, from_cache = await self._get_html(ban_hits_link, use_cache=True)
            if not html_content:
                self.logger.error(f"Failed to get HTML content for ban info: {ban_hits_link}")
                return ban_entries

            soup = self._parse_html(html_content)
            
            common_info = {}
            dl_element = soup.css_first("dl.row, dl")
            if dl_element:
                dt_nodes, dd_nodes = dl_element.css("dt"), dl_element.css("dd")
                info_dl = {dt.text(strip=True).rstrip(":").lower().replace(" ", "_"): dd.text(strip=True)
                           for dt, dd in zip(dt_nodes, dd_nodes) if dt and dd}
                common_info["banned_user_name"] = info_dl.get("name", "")
                common_info["user_id"] = info_dl.get("user_id", info_dl.get("user_id", ""))
                common_info["ip_address"] = info_dl.get("ip", "")
                common_info["hwid"] = info_dl.get("hwid", "")
                common_info["time"] = info_dl.get("time", "")

            table = soup.css_first("table.table")
            if table:
                tbody = table.css_first("tbody")
                rows_src = tbody if tbody else table
                rows = rows_src.css("tr") if rows_src else []
                
                for row_idx, row in enumerate(rows):
                    cols = row.css("td")
                    if len(cols) >= 6:
                        ban_entry = common_info.copy()
                        ban_entry["ban_time"] = cols[2].text(strip=True)
                        ban_entry["expires"] = cols[4].text(strip=True)
                        
                        if len(cols) > 6:
                            ban_entry["ban_reason"] = cols[1].text(strip=True) if len(cols) > 1 else ""
                            ban_entry["admin"] = cols[3].text(strip=True) if len(cols) > 3 else ""
                            ban_entry["ban_id"] = cols[0].text(strip=True) if len(cols) > 0 else ""
                        
                        ban_entries.append(ban_entry)
                        self._log_debug(f"Extracted ban entry {row_idx + 1}: {ban_entry.get('ban_time', 'unknown time')}")

            if not ban_entries:
                if common_info:
                    ban_entries.append(common_info)
                    self._log_debug("No ban table found, returning common connection info only")
                elif self.logger.isEnabledFor(logging.WARNING):
                    self.logger.warning(f"Could not parse any ban info from {ban_hits_link}.")

        except aiohttp.ClientResponseError as e:
            if e.status == 404:
                self._log_warning(f"Ban hits link not found (404): {ban_hits_link}")
            else:
                self._request_metrics["errors"] += 1
                self.logger.error(f"HTTP error fetching ban info from {ban_hits_link}: {e}")
        except aiohttp.ClientError as e:
            self._request_metrics["errors"] += 1
            self.logger.error(f"Request error fetching ban info from {ban_hits_link}: {e}")
        except Exception as e:
            self._request_metrics["errors"] += 1
            self.logger.error(f"Error parsing ban info from {ban_hits_link}: {e}", exc_info=True)

        elapsed = time.time() - start_time
        self.perf_stats.record("fetch_ban_info", elapsed)
        if elapsed > self.SLOW_REQUEST_THRESHOLD and not from_cache:
            if self.perf_logger.isEnabledFor(logging.DEBUG):
                self.perf_logger.debug(f"Slow ban info fetch: {elapsed:.2f}s for link: {ban_hits_link}")
        
        self._log_debug(f"Extracted {len(ban_entries)} ban entries from {ban_hits_link}")
        return ban_entries