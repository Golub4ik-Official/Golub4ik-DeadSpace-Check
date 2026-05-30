import asyncio
import logging
import os
import types
from datetime import datetime
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

            if self.config.get("check_ban_bypass"):
                self._generate_ban_bypass_html_report(report_data if report_data else [])
            else:
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
.copy-btn{background:#3a3a3a;color:#c3e88d;border:1px solid #555;border-radius:4px;cursor:pointer;font-size:13px;padding:2px 8px;transition:.15s;white-space:nowrap}
.copy-btn:hover{background:#4a4a4a;border-color:#82aaff}
.copy-btn::after{content:attr(data-tip);display:none;position:absolute;bottom:130%;left:50%;transform:translateX(-50%);background:#333;color:#d4d4d4;padding:4px 10px;border-radius:4px;font-size:11px;white-space:nowrap;pointer-events:none;z-index:10}
.copy-btn:hover::after{display:block}
.copy-btn-wrap{position:relative;display:inline-flex;align-items:center}
.nick-item{display:inline-flex;align-items:center;gap:4px;margin:2px 0}
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

        nickname_data = {}
        for item in data[1:]:
            typ = item.get("type", "")
            if typ == "associated_accounts":
                for nick in item.get("nicknames", []):
                    nickname_data.setdefault(nick, {"ips": [], "hwids": []})
            elif typ == "associated_ips":
                for ip_entry in item.get("ips", []):
                    ip = ip_entry.get("direct_ip_connections", "")
                    for user in ip_entry.get("raw_users", []):
                        entry = nickname_data.setdefault(user, {"ips": [], "hwids": []})
                        if ip and ip not in entry["ips"]:
                            entry["ips"].append(ip)
            elif typ == "associated_hwids":
                for hw_entry in item.get("hwids", []):
                    hwid = hw_entry.get("hwid", "")
                    for user in hw_entry.get("raw_users", []):
                        entry = nickname_data.setdefault(user, {"ips": [], "hwids": []})
                        if hwid and hwid not in entry["hwids"]:
                            entry["hwids"].append(hwid)

        html_parts.append("""<script>
function copyText(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).catch(function() { fallbackCopy(text); });
    } else { fallbackCopy(text); }
}
function fallbackCopy(text) {
    var ta = document.createElement('textarea');
    ta.value = text; ta.style.position='fixed'; ta.style.left='-9999px';
    document.body.appendChild(ta); ta.select();
    try { document.execCommand('copy'); } catch(e) {}
    document.body.removeChild(ta);
}
function copyNicknameData(nick) {
    var data = nicknameData[nick]; if (!data) return;
    var lines = [];
    for (var i = 0; i < data.ips.length; i++) lines.push(data.ips[i]);
    for (var i = 0; i < data.hwids.length; i++) lines.push(data.hwids[i]);
    copyText(lines.join('\\n'));
}
var nicknameData = """ + _json.dumps(nickname_data, ensure_ascii=False) + """;
</script>""")

        graph_injected = False
        for item in data[1:]:
            typ = item.get("type", "")
            if typ == "associated_accounts":
                nicks = item.get("nicknames", [])
                all_nicks_text = "\n".join(esc(n) for n in nicks)
                html_parts.append('<div class="section-title" style="display:flex;align-items:center;gap:10px"><span>👤 Связанные никнеймы (%d)</span><div class="copy-btn-wrap"><button class="copy-btn" onclick="copyText(\'%s\')" data-tip="Скопировать все никнеймы">📋</button></div></div>' % (len(nicks), all_nicks_text.replace("'", "\\'")))
                nick_items = []
                for n in nicks:
                    safe_n = esc(n).replace("'", "\\'")
                    nick_items.append('<span class="nick-item"><span class="copy-btn-wrap"><button class="copy-btn" onclick="copyNicknameData(\'%s\')" data-tip="Скопировать IP и HWID для %s">📋</button></span>%s</span>' % (safe_n, esc(n), esc(n)))
                html_parts.append('<div class="nick-list">%s</div>\n' % "<br>".join(nick_items))
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
                all_ips_text = "\n".join(esc(ip_entry.get("direct_ip_connections", "?")) for ip_entry in ips)
                html_parts.append('<div class="section-title" style="display:flex;align-items:center;gap:10px"><span>🌐 Связанные IP-адреса (%d)</span><div class="copy-btn-wrap"><button class="copy-btn" onclick="copyText(\'%s\')" data-tip="Скопировать все IP-адреса">📋</button></div></div>' % (len(ips), all_ips_text.replace("'", "\\'")))
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
                all_hwids_text = "\n".join(esc(hw_entry.get("hwid", "?")) for hw_entry in hwids)
                html_parts.append('<div class="section-title" style="display:flex;align-items:center;gap:10px"><span>🔑 Связанные HWID (%d)</span><div class="copy-btn-wrap"><button class="copy-btn" onclick="copyText(\'%s\')" data-tip="Скопировать все HWID">📋</button></div></div>' % (len(hwids), all_hwids_text.replace("'", "\\'")))
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

    async def run_offline(self):
        try:
            auth_cookie = self.config.get("auth_cookie", "")
            if auth_cookie:
                cookie_ok = await self.admin_service.admin_panel.try_auth_with_cookie(auth_cookie)
                if cookie_ok:
                    logging.info("Authenticated via auth cookie, skipping OIDC login")
                    self.admin_service.admin_panel._is_authenticated = True
                else:
                    logging.warning("Auth cookie invalid, falling back to OIDC login")
            if not await self.admin_service.login():
                msg = "❌ Не удалось войти в админ-панель. Сервер авторизации account.spacestation14.com недоступен.\nПроверьте VPN/прокси или сетевое подключение.\n"
                logging.error(msg.strip())
                if self.scanner.progress_queue is not None:
                    self.scanner.progress_queue.put_nowait(msg)
                return
            self.scanner.complaint_channels = self.scanner.cache.load_complaint_cache()
            if self.config.get("check_ban_bypass"):
                logging.info("Starting ban bypass check (offline mode)")
                report_data = await self.scanner.scan_ban_bypasses(
                    max_pages=self.config.get("ban_bypass_pages", 5)
                )
            elif self.config.get("username"):
                logging.info(f"Starting nickname scan for: {self.config.get('username')}")
                report_data = await self.scanner.scan_nickname(self.config.get("username"))
            else:
                report_data = []
            self.report_service.write_json_report(report_data if report_data else [])
            if self.config.get("check_ban_bypass"):
                self._generate_ban_bypass_html_report(report_data if report_data else [])
            elif report_data:
                self._generate_html_report_with_graph(
                    os.path.join(app_dir(), "reports", "scan_report.json"),
                    os.path.join(app_dir(), "reports")
                )
        except Exception as e:
            logging.error(f"Error during offline scan: {e}", exc_info=True)
        finally:
            await self.close()

    def _generate_ban_bypass_html_report(self, report_data):
        esc = lambda s: str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
        confidence_colors = {
            "HWID_MATCH": "#f07178", "IP_VERY_CLOSE_TIME": "#ffcb6b",
            "IP_CLOSE_TIME": "#ffcb6b", "IP_MODERATE_TIME": "#82aaff",
            "IP_DISTANT_TIME": "#82aaff", "IP_MATCH": "#546e7a", "NO_MATCH": "#546e7a",
        }
        confidence_labels = {
            "HWID_MATCH": "100%", "IP_VERY_CLOSE_TIME": "80-90%", "IP_CLOSE_TIME": "60-70%",
            "IP_MODERATE_TIME": "40-50%", "IP_DISTANT_TIME": "20-30%", "IP_MATCH": "10-20%",
        }
        html_parts = ['<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8">']
        html_parts.append('<title>Проверка обхода банов — DeadSpace Checker</title><style>')
        html_parts.append("""
*{margin:0;padding:0;box-sizing:border-box}
body{background:#1a1a1a;color:#d4d4d4;font-family:'Segoe UI',sans-serif;padding:24px;display:flex;justify-content:center}
.wrap{max-width:960px;width:100%}
h1{font-size:22px;margin-bottom:4px;color:#eeffff}
.sub{color:#888;font-size:13px;margin-bottom:16px}
.summary{display:flex;gap:16px;margin:16px 0;flex-wrap:wrap}
.sum-card{background:#252526;border-radius:8px;padding:14px 20px;flex:1;min-width:140px}
.sum-card .num{font-size:28px;font-weight:700;color:#82aaff}
.sum-card .lbl{font-size:12px;color:#888;margin-top:2px}
table{width:100%;border-collapse:collapse;margin-top:12px}
th{text-align:left;padding:10px 12px;font-size:12px;text-transform:uppercase;color:#888;border-bottom:2px solid #333}
td{padding:10px 12px;font-size:13px;border-bottom:1px solid #2a2a2a}
tr:hover{background:#252526}
.badge{display:inline-block;padding:2px 10px;border-radius:4px;font-size:12px;font-weight:600;color:#fff}
.mono{font-family:'Consolas','Courier New',monospace;font-size:12px;word-break:break-all}
.gray{color:#888}
.footer{margin-top:32px;padding-top:12px;border-top:1px solid #333;font-size:11px;color:#555;text-align:center}
.footer .brand{color:#82aaff;font-weight:600}
""")
        html_parts.append('</style></head><body><div class="wrap">')
        scan_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        html_parts.append(f'<h1>🔍 Проверка обхода банов</h1>')
        html_parts.append(f'<div class="sub">Время сканирования: {esc(scan_time)} | Всего записей: {len(report_data)}</div>')
        total = len(report_data)
        hwid_matches = sum(1 for r in report_data if r.get("ban_bypass_confidence") == "HWID_MATCH")
        ip_matches = sum(1 for r in report_data if r.get("ban_bypass_confidence", "").startswith("IP_"))
        with_findings = sum(1 for r in report_data if r.get("ban_bypass_confidence") != "NO_MATCH")
        html_parts.append(f'<div class="summary">')
        html_parts.append(f'<div class="sum-card"><div class="num">{total}</div><div class="lbl">Всего проверено</div></div>')
        html_parts.append(f'<div class="sum-card"><div class="num">{with_findings}</div><div class="lbl">Найдено обходов</div></div>')
        html_parts.append(f'<div class="sum-card"><div class="num">{hwid_matches}</div><div class="lbl">HWID совпадений</div></div>')
        html_parts.append(f'<div class="sum-card"><div class="num">{ip_matches}</div><div class="lbl">IP совпадений</div></div>')
        html_parts.append('</div>')
        if report_data:
            html_parts.append('<table><thead><tr>')
            html_parts.append('<th>#</th><th>Игрок</th><th>Твинки</th><th>Уверенность</th><th>IP</th><th>HWID</th><th>Статус</th>')
            html_parts.append('</tr></thead><tbody>')
            for idx, r in enumerate(report_data, 1):
                banned = esc(r.get("author_name", "?"))
                confidence = r.get("ban_bypass_confidence", "NO_MATCH")
                bypass_users = esc(", ".join(r.get("bypass_user_names", []))) if r.get("bypass_user_names") else "—"
                ip = esc(r.get("results", [{}])[0].get("ip_address", "")) if r.get("results") else ""
                hwid_val = esc(r.get("results", [{}])[0].get("hwid", "")) if r.get("results") else ""
                status = r.get("bypass_success_status", "?")
                color = confidence_colors.get(confidence, "#546e7a")
                label = confidence_labels.get(confidence, confidence)
                stripe = "#1e1e1e" if idx % 2 == 0 else "#1a1a1a"
                status_badge = '<span class="badge" style="background:#c3e88d;color:#1a1a1a">OK</span>' if "success" in str(status).lower() else '<span class="badge" style="background:#f07178">Ошибка</span>'
                html_parts.append(f'<tr style="background:{stripe}">')
                html_parts.append(f'<td class="gray">{idx}</td>')
                html_parts.append(f'<td>{banned}</td>')
                html_parts.append(f'<td style="color:#c792ea">{bypass_users}</td>')
                html_parts.append(f'<td><span class="badge" style="background:{color}">{esc(label)}</span></td>')
                html_parts.append(f'<td class="mono" style="color:#89ddff">{esc(ip)}</td>')
                html_parts.append(f'<td class="mono gray">{esc(hwid_val[:48])}</td>')
                html_parts.append(f'<td>{status_badge}</td>')
                html_parts.append('</tr>')
            html_parts.append('</tbody></table>')
        else:
            html_parts.append('<div style="text-align:center;padding:48px 0;color:#888;font-size:16px">✅ Обходов бана не обнаружено</div>')
        html_parts.append('<div class="footer"><span class="brand">Golub4ik (WikiHampter) DeadSpace Checker</span></div>')
        html_parts.append('</div></body></html>')
        out_dir = os.path.join(app_dir(), "reports")
        os.makedirs(out_dir, exist_ok=True)
        out_path = os.path.join(out_dir, "ban_bypass_report.html")
        try:
            with open(out_path, 'w', encoding='utf-8') as f:
                f.write("".join(html_parts))
            logging.info(f"Ban bypass HTML report saved to '{out_path}'")
            import webbrowser
            webbrowser.open(f'file://{os.path.abspath(out_path)}')
        except Exception as e:
            logging.error(f"Failed to write ban bypass HTML report: {e}")

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
