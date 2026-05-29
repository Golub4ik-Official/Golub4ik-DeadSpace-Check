"""
Скрипт для предварительного создания базы данных кэша жалоб.

Запуск: python build_cache.py --token MTA...==
или через config.py: python build_cache.py

После завершения файл deadspace_checker.db можно распространять в релизах.
Пользователь кладёт его в папку приложения — первый запуск без ожидания.
"""

import argparse
import asyncio
import logging
import os
import sys
import types

import discord

from config_system import Config, load_file
from services.database_service import DatabaseService
from services.discord_service import DiscordService


class CacheBuilder:
    def __init__(self, token: str, config: Config):
        self.token = token
        self.config = config
        intents = discord.Intents.default()
        intents.message_content = True
        self.client = discord.Client(intents=intents)
        self._patch_discord()
        self.db = DatabaseService()
        self.discord_service = DiscordService(self.client)

    def _patch_discord(self):
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

        self.client.login = types.MethodType(patched_login, self.client)

        import discord.sticker
        original = discord.sticker.Sticker._from_data

        def patched_from_data(self, data):
            try:
                return original(self, data)
            except KeyError:
                self.id = int(data['id'])
                self.name = data['name']
                self.description = data['description']
                self.format = f'unknown_{data.get("format_type", 0)}'
                self.url = f'{discord.Asset.BASE}/stickers/{self.id}.png'

        discord.sticker.Sticker._from_data = patched_from_data

    async def run(self):
        logging.info("Logging in to Discord...")

        async def on_ready():
            logging.info(f"Logged in as {self.client.user}")
            ch_ids = self.config.discord.complaint_channel_ids
            if not ch_ids:
                logging.error("No complaint_channel_ids in config! Заполните config.py")
                await self.client.close()
                return

            logging.info(f"Настраиваю {len(ch_ids)} каналов жалоб...")
            ok = await self.discord_service.setup_channels(
                self.config.discord.target_channel_id, ch_ids
            )
            if not ok:
                logging.warning("Некоторые каналы не найдены. Продолжаю...")

            empty_channels = {}
            logging.info(f"Скачиваю сообщения (history_limit={self.config.discord.message_history_limit})...")
            logging.info("ЭТО МОЖЕТ ЗАНЯТЬ 10-15 МИНУТ.")
            channels = await self.discord_service.update_complaint_cache(
                empty_channels,
                history_limit=self.config.discord.message_history_limit,
            )

            logging.info("Сохраняю в SQLite...")
            self.db.save_complaint_cache(channels)

            db_path = self.db.db_path
            size_mb = os.path.getsize(db_path) / (1024 * 1024)
            msg_count = sum(len(c.messages) for c in channels.values())
            logging.info(f"Готово! База: {db_path} ({size_mb:.1f} MB, {msg_count} сообщений)")
            await self.client.close()

        self.client.event(on_ready)
        await self.client.start(self.token)


def main():
    parser = argparse.ArgumentParser(description="Build DeadSpace Checker cache DB")
    parser.add_argument("--token", help="Discord user token")
    parser.add_argument("--config", default="config.py", help="Config file path")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%H:%M:%S",
    )

    cfg = Config()
    if os.path.exists(args.config):
        load_file(args.config, cfg)
        logging.info(f"Config loaded from {args.config}")

    token = args.token or cfg.discord.discord_user_token
    if not token:
        logging.error("Токен не указан. Используйте --token или заполните config.py")
        sys.exit(1)

    logging.info(f"Target: {cfg.discord.target_channel_id or 'не указан'}")
    logging.info(f"Channels: {cfg.discord.complaint_channel_ids or 'не указаны'}")
    logging.info(f"History limit: {cfg.discord.message_history_limit}")
    logging.info(f"Token length: {len(token)}")

    builder = CacheBuilder(token, cfg)
    asyncio.run(builder.run())


if __name__ == "__main__":
    main()
