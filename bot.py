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
from services.graph_service import render_identity_graph, render_player_graph, generate_vis_graph_from_report_data
from services.vpn_detector import enrich_report_data
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

            self._generate_graphs(report_data)
        except Exception as e:
            logging.error(f"Error during scan: {e}", exc_info=True)

        logging.info("Scan complete. Disconnecting from Discord.")
        await self.close()

    def _generate_graphs(self, report_data: List[Dict[str, Any]]) -> None:
        if not self.config.get("graph_format"):
            return

        app_data = app_dir()
        reports_dir = os.path.join(app_data, "reports")
        json_path = os.path.join(reports_dir, "scan_report.json")

        if not os.path.exists(json_path):
            return

        self._generate_html_report_with_graph(json_path, reports_dir)

    def _generate_html_report_with_graph(self, json_path: str, reports_dir: str) -> None:
        try:
            import json as _json
            with open(json_path, encoding='utf-8') as f:
                data = _json.load(f)
        except Exception as e:
            logging.error(f"Failed to read JSON report for HTML generation: {e}")
            return

        try:
            enrich_report_data(data)
        except Exception as e:
            logging.warning(f"VPN enrichment failed: {e}")

        esc = lambda s: str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

        player = data[0] if data else {}
        nick = player.get("nickname", "Неизвестно")
        primary = player.get("primary_nickname", nick)
        status = player.get("status", "unknown")

        status_color = {"banned": "#f07178", "suspicious": "#ffcb6b", "clean": "#c3e88d"}.get(status.lower(), "#546e7a")
        status_ru = {"banned": "ЗАБАНЕН", "suspicious": "ПОДОЗРИТЕЛЬНЫЙ", "clean": "ЧИСТ"}.get(status.lower(), status)

        html_parts = ["<!DOCTYPE html><html lang='ru'><head><meta charset='utf-8'>"]
        html_parts.append(f"<title>DeadSpace Check — {esc(primary)}</title>")
        html_parts.append("""
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#1a1a1a;color:#d4d4d4;font-family:'Segoe UI',sans-serif;padding:24px;display:flex;justify-content:center}
.report{max-width:780px;width:100%}
.header{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:20px}
.header-left h1{font-size:24px;margin-bottom:4px}
.header-left .sub{color:#888;font-size:13px}
.status-badge{display:inline-block;padding:4px 14px;border-radius:4px;font-weight:700;font-size:13px;color:#fff;background:%s}
.section-title{font-size:16px;font-weight:700;margin:24px 0 10px;padding-bottom:6px;border-bottom:1px solid #333;color:#eeffff}
.info-card{border-radius:6px;padding:12px 16px;margin-bottom:6px;display:flex;gap:12px;align-items:start}
.badge{color:#fff;font-weight:700;font-size:13px;border-radius:50%%;width:28px;height:28px;min-width:28px;display:flex;align-items:center;justify-content:center}
.bad-red{background:#f07178}
.bad-green{background:#c3e88d;color:#1a1a1a}
.bad-blue{background:#82aaff;color:#1a1a1a}
.bad-purple{background:#c792ea}
.bad-orange{background:#ffcb6b;color:#1a1a1a}
.card-fields{flex:1;min-width:0}
.field{display:flex;gap:8px;margin-bottom:3px;font-size:13px;align-items:baseline}
.key{color:#888;min-width:70px;flex-shrink:0;font-weight:600}
.val{word-break:break-word}
.yellow{color:#ffcb6b}
.blue{color:#82aaff}
.green{color:#c3e88d}
.purple{color:#c792ea}
.gray{color:#888}
.orange{color:#ffcb6b}
.cyan{color:#89ddff}
.mono{font-family:'Consolas','Courier New',monospace;font-size:12px;word-break:break-all}
.link{color:#82aaff;text-decoration:underline;word-break:break-all}
.nick-list{background:#252526;border-radius:6px;padding:12px 16px;font-size:13px;line-height:1.7;color:#c792ea}
.content-box{background:#1e1e1e;border-radius:4px;padding:8px 10px;margin-top:4px;font-size:12px;line-height:1.5;color:#d4d4d4;white-space:pre-wrap;word-break:break-word;max-height:200px;overflow-y:auto;border:1px solid #333}
.footer{margin-top:32px;padding-top:12px;border-top:1px solid #333;font-size:11px;color:#555;text-align:center}
.footer .brand{color:#82aaff;font-weight:600}
.tag{display:inline-block;padding:1px 8px;border-radius:3px;font-size:11px;font-weight:600;margin-right:4px}
.tag-red{background:#f0717844;color:#f07178}
.tag-green{background:#c3e88d44;color:#c3e88d}
.tag-orange{background:#ffcb6b44;color:#ffcb6b}
.tag-blue{background:#82aaff44;color:#82aaff}
#graph-container{border:1px solid #333}
</style>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/vis-network/9.1.2/dist/dist/vis-network.min.css">
<script src="https://cdnjs.cloudflare.com/ajax/libs/vis-network/9.1.2/dist/vis-network.min.js"></script>
</head><body><div class="report">
  <div class="header">
    <div class="header-left">
      <h1>🔍 %s</h1>
      <div class="sub">Ник поиска: %s</div>
    </div>
  </div>
  <span class="status-badge">%s</span>
""" % (status_color, esc(primary), esc(nick), status_ru))

        graph_injected = False
        for item in data[1:]:
            typ = item.get("type", "")
            if typ == "associated_accounts":
                nicks = item.get("nicknames", [])
                html_parts.append('<div class="section-title">👤 Связанные никнеймы (%d)</div><div class="nick-list">%s</div>\n' % (
                    len(nicks), "<br>".join(esc(n) for n in nicks)))
                if not graph_injected:
                    html_parts.append(generate_vis_graph_from_report_data(data))
                    graph_injected = True

            elif typ == "complaints":
                links = item.get("links", [])
                html_parts.append('<div class="section-title">📋 Наказания на других серверах (%d)</div>' % len(links))
                for ci, c in enumerate(links[:30]):
                    ch = c.get("channel", "?")
                    auth = c.get("author", "?")
                    content = c.get("content", "")
                    link = c.get("link", "")
                    stripe = "#2a2a2a" if ci % 2 == 0 else "#252526"
                    content_short = (content[:800] + "...") if len(content) > 800 else content
                    content_html = '<div class="content-box">%s</div>' % esc(content_short) if content else ""
                    link_html = ('<div class="field"><span class="key">Ссылка</span><a class="val link" href="%s">%s</a></div>' % (
                        esc(link), esc(link[:90]) + ("..." if len(link) > 90 else ""))) if link else ""
                    html_parts.append('''<div class="info-card" style="background:%s">
                <div class="badge bad-blue">%d</div>
                <div class="card-fields">
                  <div class="field"><span class="key">Канал</span><span class="val orange">#%s</span></div>
                  <div class="field"><span class="key">Автор</span><span class="val blue">%s</span></div>
                  %s
                  %s
                </div>
              </div>''' % (stripe, ci + 1, esc(ch), esc(auth), link_html, content_html))

            elif typ == "associated_ips":
                ips = item.get("ips", [])
                html_parts.append('<div class="section-title">🌐 Связанные IP-адреса (%d)</div>' % len(ips))
                for idx, ip_entry in enumerate(ips[:20]):
                    ip_addr = ip_entry.get("direct_ip_connections", "?")
                    shared = ip_entry.get("shared_with", [])
                    owned_by_primary = ip_entry.get("owned_by_primary", False)
                    owned_by_alt = ip_entry.get("owned_by_alt", False)
                    vpn_info = ip_entry.get("vpn_info", {})
                    vpn_badges = ""
                    if vpn_info.get("proxy"):
                        vpn_badges = '<span class="tag tag-red">VPN</span>'
                    elif vpn_info.get("hosting"):
                        vpn_badges = '<span class="tag tag-orange">Хостинг</span>'
                    owner_tag = '<span class="tag tag-green">Основной</span>' if owned_by_primary else ('<span class="tag tag-orange">Альт</span>' if owned_by_alt else '<span class="tag tag-red">Чужой</span>')
                    shared_html = '<div class="field"><span class="key">Общие с</span><span class="val purple">%s</span></div>' % esc(", ".join(shared[:8])) if shared else ""
                    html_parts.append('''<div class="info-card" style="background:#252526">
                <div class="badge bad-purple">%d</div>
                <div class="card-fields">
                  <div class="field"><span class="key">IP</span><span class="val mono cyan">%s</span> %s %s</div>
                  %s
                </div>
              </div>''' % (idx + 1, esc(ip_addr), owner_tag, vpn_badges, shared_html))

            elif typ == "associated_hwids":
                hwids = item.get("hwids", [])
                html_parts.append('<div class="section-title">🔑 Связанные HWID (%d)</div>' % len(hwids))
                for idx, hw_entry in enumerate(hwids[:20]):
                    hwid = hw_entry.get("hwid", "?")
                    shared = hw_entry.get("shared_with", [])
                    owned_by_primary = hw_entry.get("owned_by_primary", False)
                    owned_by_alt = hw_entry.get("owned_by_alt", False)
                    owner_tag = '<span class="tag tag-green">Основной</span>' if owned_by_primary else ('<span class="tag tag-orange">Альт</span>' if owned_by_alt else '<span class="tag tag-red">Чужой</span>')
                    shared_html = '<div class="field"><span class="key">Общие с</span><span class="val purple">%s</span></div>' % esc(", ".join(shared[:8])) if shared else ""
                    html_parts.append('''<div class="info-card" style="background:#252526">
                <div class="badge bad-orange">%d</div>
                <div class="card-fields">
                  <div class="field"><span class="key">HWID</span><span class="val mono">%s</span> %s</div>
                  %s
                </div>
              </div>''' % (idx + 1, esc(hwid), owner_tag, shared_html))

            elif typ == "denied_login_attempts":
                attempts = item.get("attempts", [])
                if attempts:
                    html_parts.append('<div class="section-title">🚫 Отклонённые входы (%d)</div>' % len(attempts))
                    for ai, a in enumerate(attempts[:12]):
                        t = a.get("time", "?")[:19]
                        u = a.get("user_name", "?")
                        ip_addr = a.get("ip_address", "?")
                        server = a.get("server", "?")
                        hwid = a.get("hwid", "")
                        stripe = "#2a2a2a" if ai % 2 == 0 else "#252526"
                        vpn_info = a.get("vpn_info", {})
                        vpn_badge = ""
                        if vpn_info.get("proxy"):
                            vpn_badge = '<span class="tag tag-red">VPN</span>'
                        elif vpn_info.get("hosting"):
                            vpn_badge = '<span class="tag tag-orange">Хостинг</span>'
                        hwid_html = '<div class="field"><span class="key">HWID</span><span class="val mono gray">%s</span></div>' % esc(hwid) if hwid else ""
                        html_parts.append('''<div class="info-card" style="background:%s">
                <div class="badge bad-red">%d</div>
                <div class="card-fields">
                  <div class="field"><span class="key">Время</span><span class="val">%s</span></div>
                  <div class="field"><span class="key">Ник</span><span class="val yellow">%s</span></div>
                  <div class="field"><span class="key">IP</span><span class="val mono cyan">%s</span> %s</div>
                  <div class="field"><span class="key">Сервер</span><span class="val">%s</span></div>
                  %s
                </div>
              </div>''' % (stripe, ai + 1, esc(t), esc(u), esc(ip_addr), vpn_badge, esc(server), hwid_html))

        html_parts.append('<div class="footer"><span class="brand">Golub4ik (WikiHampter) DeadSpace Checker</span></div></div></body></html>')

        out_path = self.config.get("graph_output") or os.path.join(reports_dir, "scan_report.html")
        try:
            with open(out_path, 'w', encoding='utf-8') as f:
                f.write("".join(html_parts))
            logging.info(f"HTML report with graph saved to '{out_path}'")
            import webbrowser
            webbrowser.open(f'file://{os.path.abspath(out_path)}')
        except Exception as e:
            logging.error(f"Failed to write HTML report: {e}")

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
