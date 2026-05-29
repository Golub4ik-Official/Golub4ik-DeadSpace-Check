import asyncio
import logging
import os
import types
from typing import List, Dict, Any

import discord

from core.analyzer import PlayerAnalyzer
from core.scanner import Scanner
from services.admin_service import AdminService
from services.cache_service import CacheService
from services.database_service import DatabaseService
from services.discord_service import DiscordService
from services.reporting import ReportService
from services.reporting.config import ReportConfig
from utils.path_utils import app_dir


class BanCheckerBot:
    def __init__(self, token: str, admin_panel, config: Dict[str, Any], progress_queue=None) -> None:
        self.token = token
        self.config = config
        intents = discord.Intents.default()
        intents.message_content = True
        self.client = discord.Client(intents=intents)
        self._patch_http_client()
        self._patch_client_login()
        self._patch_sticker_format()
        self.db = DatabaseService()
        self.discord_service = DiscordService(self.client)
        self.admin_service = AdminService(admin_panel, self.db)
        app_data = app_dir()
        self.cache_service = CacheService(self.db)
        self.report_service = ReportService(config=ReportConfig(report_output_dir=os.path.join(app_data, "reports")))
        self.player_analyzer = PlayerAnalyzer()
        self.scanner = Scanner(
            self.discord_service,
            self.admin_service,
            self.cache_service,
            self.report_service,
            self.player_analyzer,
            progress_queue=progress_queue
        )
        self.client.event(self.on_ready)

    def _patch_http_client(self):
        import aiohttp

        http = self.client.http
        SESSION_ATTR = '_HTTPClient__session'

        class _SelfbotRequest:
            def __init__(self, original_fn, method, url, kwargs):
                self._original_fn = original_fn
                self._method = method
                self._url = url
                self._kwargs = dict(kwargs)
                self._cm = None

            def _patch_headers(self):
                headers = self._kwargs.get('headers', {})
                auth = headers.get('Authorization', '')
                if auth.startswith('Bot '):
                    headers['Authorization'] = auth[4:]
                self._kwargs['headers'] = headers

            def _get_cm(self):
                if self._cm is None:
                    self._patch_headers()
                    self._cm = self._original_fn(self._method, self._url, **self._kwargs)
                return self._cm

            def __await__(self):
                return self._get_cm().__await__()

            async def __aenter__(self):
                return await self._get_cm().__aenter__()

            async def __aexit__(self, *args):
                return await self._get_cm().__aexit__(*args)

        async def patched_static_login(self_http, token):
            if self_http.connector is discord.http.MISSING:
                self_http.connector = aiohttp.TCPConnector(limit=0)

            session = aiohttp.ClientSession(
                connector=self_http.connector,
                ws_response_class=discord.http.DiscordClientWebSocketResponse,
                trace_configs=None if self_http.http_trace is None else [self_http.http_trace],
            )
            setattr(self_http, SESSION_ATTR, session)
            self_http._global_over = asyncio.Event()
            self_http._global_over.set()

            original_session_req = session.request

            def selfbot_request(method, url, **kwargs):
                return _SelfbotRequest(original_session_req, method, url, kwargs)

            session.request = selfbot_request

            old_token = self_http.token
            self_http.token = token

            try:
                data = await self_http.request(discord.http.Route('GET', '/users/@me'))
            except discord.HTTPException as exc:
                self_http.token = old_token
                if exc.status == 401:
                    raise discord.LoginFailure('Improper token has been passed.') from exc
                raise

            return data

        http.static_login = types.MethodType(patched_static_login, http)

    def _patch_sticker_format(self):
        import discord.sticker
        original = discord.sticker.Sticker._from_data

        def patched_from_data(self, data):
            try:
                return original(self, data)
            except KeyError:
                fmt = data.get('format_type', 0)
                self.id = int(data['id'])
                self.name = data['name']
                self.description = data['description']
                self.format = f'unknown_{fmt}'
                self.url = f'{discord.Asset.BASE}/stickers/{self.id}.png'

        discord.sticker.Sticker._from_data = patched_from_data

    def _patch_client_login(self):
        client = self.client
        _loop = discord.client._loop

        async def patched_login(self_client, token):
            logging.info('logging in using static token (selfbot mode)')

            if self_client.loop is _loop:
                loop = asyncio.get_running_loop()
                self_client.loop = loop
                self_client.http.loop = loop
                self_client._connection.loop = loop
                self_client._ready = asyncio.Event()

            if not isinstance(token, str):
                raise TypeError(f'expected token to be a str, received {token.__class__.__name__} instead')
            token = token.strip()

            data = await self_client.http.static_login(token)
            self_client._connection.user = discord.user.ClientUser(state=self_client._connection, data=data)

            mock_app = types.SimpleNamespace(id=0, flags=discord.ApplicationFlags._from_value(0))
            self_client._application = mock_app
            if self_client._connection.application_id is None:
                self_client._connection.application_id = mock_app.id
            if not self_client._connection.application_flags:
                self_client._connection.application_flags = mock_app.flags

            await self_client.setup_hook()

        client.login = types.MethodType(patched_login, client)

    async def on_ready(self):
        logging.info(f"Logged in as: {self.client.user} (ID: {self.client.user.id})")
        target_channel_id = self.config.get("TARGET_CHANNEL_ID")
        complaint_channel_ids = self.config.get("COMPLAINT_CHANNEL_IDS", [])

        if not await self.scanner.setup(target_channel_id, complaint_channel_ids):
            logging.error("Failed to set up scanner. Exiting.")
            await self.close()
            return

        try:
            report_data: List[Dict[str, Any]] = []

            message_interval_start = self.config.get("message_interval_start")
            message_interval_end = self.config.get("message_interval_end")

            if message_interval_start and message_interval_end:
                logging.info(f"Starting interval scan from {message_interval_start} to {message_interval_end}")
                report_data = await self.scanner.scan_message_interval(
                    message_interval_start,
                    message_interval_end
                )
            elif self.config.get("check_ban_bypass"):
                logging.info("Starting ban bypass check")
                report_data = await self.scanner.scan_ban_bypasses(
                    max_pages=self.config.get("ban_bypass_pages", 5)
                )
            elif self.config.get("username"):
                logging.info(f"Starting nickname scan for: {self.config.get('username')}")
                report_data = await self.scanner.scan_nickname(
                    self.config.get("username")
                )
            elif self.config.get("message_limit") is not None:
                logging.info(f"Starting message scan with limit: {self.config.get('message_limit')}")
                report_data = await self.scanner.scan_messages(
                    message_limit=self.config.get("message_limit", 10)
                )
            else:
                logging.warning("No scan type specified or missing parameters.")

            if report_data:
                self.report_service.write_json_report(report_data)
                logging.info(f"Report with {len(report_data)} items written to file")
        except Exception as e:
            logging.error(f"Error during scan: {e}", exc_info=True)

        logging.info("Scan complete. Disconnecting from Discord.")
        await self.close()

    async def close(self):
        if hasattr(self, 'admin_service') and self.admin_service:
            try:
                await self.admin_service.close()
                logging.info("AdminService closed successfully.")
            except Exception as e:
                logging.error(f"Error closing AdminService: {e}", exc_info=True)

        if self.client:
            try:
                await self.client.close()
                logging.info("Discord client closed successfully.")
            except Exception as e:
                logging.error(f"Error closing Discord client: {e}", exc_info=True)

    def run(self):
        self.client.run(self.token)
