import ast
import asyncio
import base64
import logging
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox, filedialog
import json
import os
import re

import discord

from admin_panel import AdminPanel
from bot import BanCheckerBot
from config_system import load_file, config as cfg
from services.database_service import DatabaseService
from utils.logging_utils import setup_logging
from utils.path_utils import app_dir, bundle_dir
from services.graph_service import generate_vis_graph_from_report_data
from services.vpn_detector import enrich_report_data, get_vpn_detector

ROOT_DIR = bundle_dir()
CONFIG_FILE = os.path.join(bundle_dir(), "config.py")
LOGO_PATH = os.path.join(bundle_dir(), "DeadSpaceLogo.png")

ANSI_RE = re.compile(r'\x1b\[[\d;]*[a-zA-Z]')

ANSI_TAG_MAP = {
    '0': '_reset',
    '1': 'bold',
    '4': 'underline',
    '90': 'gray',
    '91': 'red',
    '92': 'green',
    '93': 'yellow',
    '94': 'blue',
    '95': 'magenta',
    '96': 'cyan',
    '97': 'white',
}


def _read_config_raw():
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""


def _parse_config_value(src, name, default):
    try:
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == name:
                        return ast.literal_eval(node.value)
    except Exception:
        pass
    return default


_RAW_CFG = _read_config_raw()
CFG_MESSAGE_LIMIT = _parse_config_value(_RAW_CFG, "MESSAGE_LIMIT", 10)
CFG_BAN_BYPASS_PAGES = _parse_config_value(_RAW_CFG, "BAN_BYPASS_PAGES", 3)

CFG_TARGET_CHANNEL_ID = _parse_config_value(_RAW_CFG, "TARGET_CHANNEL_ID", 0)
CFG_COMPLAINT_CHANNEL_IDS = _parse_config_value(_RAW_CFG, "COMPLAINT_CHANNEL_IDS", [])
CFG_MESSAGE_HISTORY_LIMIT = _parse_config_value(_RAW_CFG, "MESSAGE_HISTORY_LIMIT", 70000)
CFG_BASE_ADMIN_URL = _parse_config_value(_RAW_CFG, "BASE_ADMIN_URL", "https://admin.deadspace14.net/admin")
CFG_ACCOUNT_URL = _parse_config_value(_RAW_CFG, "ACCOUNT_URL", "https://account.spacestation14.com")
CFG_OPERATION_TIMEOUT = _parse_config_value(_RAW_CFG, "OPERATION_TIMEOUT", 180)
CFG_REQUEST_TIMEOUT = _parse_config_value(_RAW_CFG, "REQUEST_TIMEOUT", 90)
CFG_SEARCH_TIMEOUT = _parse_config_value(_RAW_CFG, "SEARCH_TIMEOUT", 240)
CFG_BATCH_TIMEOUT = _parse_config_value(_RAW_CFG, "BATCH_TIMEOUT", 480)
CFG_TERM_TIMEOUT = _parse_config_value(_RAW_CFG, "TERM_TIMEOUT", 240)
CFG_MAX_CONCURRENT_REQUESTS = _parse_config_value(_RAW_CFG, "MAX_CONCURRENT_REQUESTS", 5)
CFG_SEARCH_MAX_DEPTH = _parse_config_value(_RAW_CFG, "SEARCH_MAX_DEPTH", 3)
CFG_SEARCH_LIMIT_ROOT = _parse_config_value(_RAW_CFG, "SEARCH_LIMIT_ROOT", 10)
CFG_SEARCH_LIMIT_LEVEL1 = _parse_config_value(_RAW_CFG, "SEARCH_LIMIT_LEVEL1", 5)
CFG_SEARCH_LIMIT_LEVEL2 = _parse_config_value(_RAW_CFG, "SEARCH_LIMIT_LEVEL2", 3)
CFG_SEARCH_LIMIT_DEFAULT = _parse_config_value(_RAW_CFG, "SEARCH_LIMIT_DEFAULT", 2)
CFG_BYPASS_SEARCH_MAX_DEPTH = _parse_config_value(_RAW_CFG, "BYPASS_SEARCH_MAX_DEPTH", 2)
CFG_SEARCH_CACHE_MAX_SIZE = _parse_config_value(_RAW_CFG, "SEARCH_CACHE_MAX_SIZE", 12000)
CFG_SEARCH_CACHE_TTL = _parse_config_value(_RAW_CFG, "SEARCH_CACHE_TTL", 9000)
CFG_CLOSE_TIME_THRESHOLD_MINUTES = _parse_config_value(_RAW_CFG, "CLOSE_TIME_THRESHOLD_MINUTES", 10)
CFG_TIME_THRESHOLD_MINUTES = _parse_config_value(_RAW_CFG, "TIME_THRESHOLD_MINUTES", 30)
CFG_SUSPICIOUS_TIME_THRESHOLD_MINUTES = _parse_config_value(_RAW_CFG, "SUSPICIOUS_TIME_THRESHOLD_MINUTES", 60)
CFG_IP_MATCH_TIMEDELTA_MINUTES = _parse_config_value(_RAW_CFG, "IP_MATCH_TIMEDELTA_MINUTES", 30)

def _force_close_loop(loop):
    if loop.is_closed():
        return
    try:
        pending = asyncio.all_tasks(loop)
        for t in pending:
            t.cancel()
    except Exception:
        pass
    try:
        if not loop.is_closed():
            loop.run_until_complete(loop.shutdown_asyncgens())
    except Exception:
        pass
    try:
        if not loop.is_closed():
            loop.close()
    except Exception:
        pass


CONFIG_OVERRIDE_MAP = {
    "TARGET_CHANNEL_ID": ("discord", "target_channel_id"),
    "COMPLAINT_CHANNEL_IDS": ("discord", "complaint_channel_ids"),
    "MESSAGE_HISTORY_LIMIT": ("discord", "message_history_limit"),
    "BASE_ADMIN_URL": ("api", "base_admin_url"),
    "ACCOUNT_URL": ("api", "account_url"),
    "OPERATION_TIMEOUT": ("api", "operation_timeout"),
    "REQUEST_TIMEOUT": ("api", "request_timeout"),
    "SEARCH_TIMEOUT": ("api", "search_timeout"),
    "BATCH_TIMEOUT": ("api", "batch_timeout"),
    "TERM_TIMEOUT": ("api", "term_timeout"),
    "MAX_CONCURRENT_REQUESTS": ("api", "max_concurrent_requests"),
    "SEARCH_MAX_DEPTH": ("scan", "search_max_depth"),
    "SEARCH_LIMIT_ROOT": ("scan", "search_limit_root"),
    "SEARCH_LIMIT_LEVEL1": ("scan", "search_limit_level1"),
    "SEARCH_LIMIT_LEVEL2": ("scan", "search_limit_level2"),
    "SEARCH_LIMIT_DEFAULT": ("scan", "search_limit_default"),
    "BYPASS_SEARCH_MAX_DEPTH": ("scan", "bypass_search_max_depth"),
    "SEARCH_CACHE_MAX_SIZE": ("scan", "search_cache_max_size"),
    "SEARCH_CACHE_TTL": ("scan", "search_cache_ttl"),
    "CLOSE_TIME_THRESHOLD_MINUTES": ("time_thresholds", "close_time_threshold_minutes"),
    "TIME_THRESHOLD_MINUTES": ("time_thresholds", "time_threshold_minutes"),
    "SUSPICIOUS_TIME_THRESHOLD_MINUTES": ("time_thresholds", "suspicious_time_threshold_minutes"),
    "IP_MATCH_TIMEDELTA_MINUTES": ("time_thresholds", "ip_match_timedelta_minutes"),
}


class QueueLogHandler(logging.Handler):
    def __init__(self, out_queue, formatter):
        super().__init__()
        self.out_queue = out_queue
        self.setFormatter(formatter)

    def emit(self, record):
        try:
            self.out_queue.put({"type": "log", "text": self.format(record) + "\n"})
        except Exception:
            pass


class QueueStream:
    def __init__(self, out_queue):
        self.out_queue = out_queue

    def write(self, text):
        if text.strip():
            self.out_queue.put(text)

    def flush(self):
        pass

    def isatty(self):
        return False

    @property
    def encoding(self):
        return "utf-8"


class BanCheckerGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("Golub4ik (WikiHampter) DeadSpace Checker")
        self.root.geometry("820x720")
        self.root.minsize(650, 550)

        self.db = DatabaseService()
        self._set_icon()
        self.settings = self._load_settings()
        self.bot = None
        self.bot_loop = None
        self.running = False
        self.output_queue = queue.Queue()
        self._admin_panel_loop = None
        self._admin_panel = None

        self._setup_color_tags()
        self._build_ui()
        self._fix_shortcuts()
        self._apply_settings()
        self._poll_output()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _setup_color_tags(self):
        tag_cfg = {
            'red': ('#f07178', 'normal'),
            'green': ('#c3e88d', 'normal'),
            'yellow': ('#ffcb6b', 'normal'),
            'blue': ('#82aaff', 'normal'),
            'magenta': ('#c792ea', 'normal'),
            'cyan': ('#89ddff', 'normal'),
            'white': ('#eeffff', 'normal'),
            'gray': ('#546e7a', 'normal'),
            'bold_red': ('#f07178', 'bold'),
            'bold_green': ('#c3e88d', 'bold'),
            'bold_yellow': ('#ffcb6b', 'bold'),
            'bold_blue': ('#82aaff', 'bold'),
            'bold_magenta': ('#c792ea', 'bold'),
            'bold_cyan': ('#89ddff', 'bold'),
            'bold_white': ('#ffffff', 'bold'),
            'bold': ('#eeffff', 'bold'),
            'underline': ('#eeffff', 'normal'),
        }
        self._tags = {}
        for name, (fg, weight) in tag_cfg.items():
            opts = {'foreground': fg}
            if weight == 'bold':
                opts['font'] = ("Consolas", 9, "bold")
            if name == 'underline':
                opts['underline'] = True
            self._tags[name] = opts

    def _ensure_tag(self, name):
        if name not in self.output_text.tag_names():
            opts = self._tags.get(name, {})
            if opts:
                self.output_text.tag_config(name, **opts)

    def _insert_colored(self, text):
        parts = re.split(r'(\x1b\[[\d;]*m)', text)
        active_tags = []
        for part in parts:
            if not part:
                continue
            if part.startswith('\x1b[') and part.endswith('m'):
                code = part[2:-1]
                if not code or code == '0':
                    active_tags = []
                else:
                    codes = code.split(';')
                    for c in codes:
                        tag = ANSI_TAG_MAP.get(c)
                        if tag == '_reset':
                            active_tags = []
                        elif tag and tag not in active_tags:
                            active_tags.append(tag)
            else:
                for t in active_tags:
                    self._ensure_tag(t)
                if active_tags:
                    self.output_text.insert(tk.END, part, tuple(active_tags))
                else:
                    self.output_text.insert(tk.END, part)

    def _set_icon(self):
        try:
            logo = tk.PhotoImage(file=LOGO_PATH)
            self.root.iconphoto(True, logo)
            self._logo_img = logo
        except Exception:
            pass

    def _load_settings(self):
        try:
            return self.db.gui_get_all()
        except Exception:
            return {}

    def _save_settings(self):
        try:
            self.db.gui_set_all(self.settings)
        except Exception as e:
            logging.warning(f"Failed to save GUI settings: {e}")

    def _apply_settings(self):
        self.username_var.set(self.settings.get("admin_username", ""))
        self.password_var.set(self.settings.get("admin_password", ""))
        self.token_var.set(self.settings.get("discord_token", ""))
        self.msg_count_var.set(str(self.settings.get("message_count", CFG_MESSAGE_LIMIT)))
        self.bypass_pages_var.set(str(self.settings.get("bypass_pages", CFG_BAN_BYPASS_PAGES)))
        self.auto_ban_var.set(bool(self.settings.get("auto_ban", False)))
        self.auth_cookie_var.set(self.settings.get("auth_cookie", ""))
        self.nickname_var.set(self.settings.get("last_nickname", ""))


    def _build_ui(self):
        main = ttk.Frame(self.root, padding="12")
        main.grid(row=0, column=0, sticky="nsew")
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        main.columnconfigure(0, weight=1)
        main.rowconfigure(2, weight=1)

        cred = ttk.LabelFrame(main, text="Настройки доступа", padding="10")
        cred.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        cred.columnconfigure(1, weight=1)

        self.username_var = tk.StringVar()
        self.password_var = tk.StringVar()
        self.token_var = tk.StringVar()
        self.show_secrets = tk.BooleanVar(value=False)

        ttk.Label(cred, text="Имя администратора:").grid(row=0, column=0, sticky="w", padx=(0, 6), pady=2)
        ttk.Entry(cred, textvariable=self.username_var).grid(row=0, column=1, sticky="ew", pady=2)

        ttk.Label(cred, text="Пароль администратора:").grid(row=1, column=0, sticky="w", padx=(0, 6), pady=2)
        self.pw_entry = ttk.Entry(cred, textvariable=self.password_var, show="*")
        self.pw_entry.grid(row=1, column=1, sticky="ew", pady=2)

        ttk.Label(cred, text="Токен Discord:").grid(row=2, column=0, sticky="w", padx=(0, 6), pady=2)
        token_frame = ttk.Frame(cred)
        token_frame.grid(row=2, column=1, sticky="ew", pady=2)
        token_frame.columnconfigure(0, weight=1)
        self.tk_entry = ttk.Entry(token_frame, textvariable=self.token_var, show="*")
        self.tk_entry.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        ttk.Button(token_frame, text="?", width=2, command=self._show_token_help).grid(row=0, column=1)

        ttk.Label(cred, text="Кука auth (опционально):").grid(row=3, column=0, sticky="w", padx=(0, 6), pady=2)
        self.auth_cookie_var = tk.StringVar()
        auth_cookie_frame = ttk.Frame(cred)
        auth_cookie_frame.grid(row=3, column=1, sticky="ew", pady=2)
        auth_cookie_frame.columnconfigure(0, weight=1)
        ttk.Entry(auth_cookie_frame, textvariable=self.auth_cookie_var, show="*").grid(row=0, column=0, sticky="ew")

        btn_row = ttk.Frame(cred)
        btn_row.grid(row=4, column=1, sticky="e", pady=(6, 0))
        ttk.Checkbutton(btn_row, text="Показать", variable=self.show_secrets,
                        command=self._toggle_secrets).pack(side="left", padx=(0, 8))
        ttk.Button(btn_row, text="Сохранить настройки",
                   command=self._on_save).pack(side="left")

        notebook = ttk.Notebook(main)
        notebook.grid(row=1, column=0, sticky="nsew", pady=(0, 0))
        main.rowconfigure(1, weight=1)

        scan_tab = ttk.Frame(notebook, padding="8")
        notebook.add(scan_tab, text="Поиск")
        scan_tab.columnconfigure(0, weight=1)
        scan_tab.rowconfigure(4, weight=1)

        mode = ttk.LabelFrame(scan_tab, text="Режим сканирования", padding="10")
        mode.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        mode.columnconfigure(1, weight=1)

        self.scan_mode = tk.StringVar(value="username")
        ttk.Radiobutton(mode, text="Пробив игрока по нику",
                        variable=self.scan_mode, value="username",
                        command=self._on_mode_change).grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Radiobutton(mode, text="Сканирование новых сообщений",
                        variable=self.scan_mode, value="messages",
                        command=self._on_mode_change).grid(row=1, column=0, columnspan=2, sticky="w")
        ttk.Radiobutton(mode, text="Проверка обхода банов",
                        variable=self.scan_mode, value="banbypass",
                        command=self._on_mode_change).grid(row=2, column=0, columnspan=2, sticky="w")

        ttk.Label(mode, text="Имя игрока:").grid(row=3, column=0, sticky="w", padx=(20, 4), pady=(10, 2))
        self.nickname_var = tk.StringVar()
        self.nickname_entry = ttk.Entry(mode, textvariable=self.nickname_var)
        self.nickname_entry.grid(row=3, column=1, sticky="ew", pady=(10, 2))

        params = ttk.Frame(mode)
        params.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        params.columnconfigure(1, weight=1)
        params.columnconfigure(3, weight=1)

        ttk.Label(params, text="Кол-во сообщений:").grid(row=0, column=0, sticky="w", padx=(20, 2))
        self.msg_count_var = tk.StringVar(value="10")
        ttk.Entry(params, textvariable=self.msg_count_var, width=8).grid(row=0, column=1, sticky="w")

        ttk.Label(params, text="Страниц обхода:").grid(row=0, column=2, sticky="w", padx=(12, 2))
        self.bypass_pages_var = tk.StringVar(value="3")
        ttk.Entry(params, textvariable=self.bypass_pages_var, width=8).grid(row=0, column=3, sticky="w")

        self.auto_ban_var = tk.BooleanVar(value=False)
        self.auto_ban_cb = ttk.Checkbutton(params, text="Авто-бан IP/HWID",
                                            variable=self.auto_ban_var)
        self.auto_ban_cb.grid(row=1, column=0, columnspan=4, sticky="w", padx=(20, 2), pady=(4, 0))



        actions = ttk.Frame(scan_tab)
        actions.grid(row=1, column=0, sticky="ew", pady=(0, 4))
        self.start_btn = ttk.Button(actions, text="▶ Запуск", command=self._on_start)
        self.start_btn.pack(side="left", padx=(0, 8))
        self.stop_btn = ttk.Button(actions, text="■ Остановить", command=self._on_stop, state="disabled")
        self.stop_btn.pack(side="left")
        self.config_btn = ttk.Button(actions, text="⚙️", width=3, command=self._open_config_dialog)
        self.config_btn.pack(side="left", padx=(8, 0))

        progress_frame = ttk.LabelFrame(scan_tab, text="Прогресс", padding="4")
        progress_frame.grid(row=2, column=0, sticky="ew", pady=(0, 6))
        progress_frame.columnconfigure(1, weight=1)

        self.progress_var = tk.IntVar(value=0)
        self.progress_bar = ttk.Progressbar(
            progress_frame, mode="determinate",
            variable=self.progress_var, length=200
        )
        self.progress_bar.grid(row=0, column=0, padx=(0, 8), sticky="w")
        self.progress_label = ttk.Label(progress_frame, text="")
        self.progress_label.grid(row=0, column=1, sticky="w")

        out = ttk.LabelFrame(scan_tab, text="Результаты", padding="4")
        out.grid(row=3, column=0, sticky="nsew", pady=(4, 0))
        out.columnconfigure(0, weight=1)
        out.rowconfigure(0, weight=1)

        self.output_text = scrolledtext.ScrolledText(
            out, wrap="word", font=("Consolas", 9),
            bg="#1e1e1e", fg="#d4d4d4", insertbackground="white",
        )
        self.output_text.grid(row=0, column=0, sticky="nsew")

        self._build_ban_tab(notebook)
        self._on_mode_change()

    def _build_ban_tab(self, notebook):
        ban_tab = ttk.Frame(notebook, padding="8")
        notebook.add(ban_tab, text="Блокировка")
        ban_tab.columnconfigure(0, weight=1)
        ban_tab.rowconfigure(2, weight=1)

        input_frame = ttk.LabelFrame(ban_tab, text="Цели (HWID / IP / Username — по одному на строку)", padding="6")
        input_frame.grid(row=0, column=0, sticky="nsew", pady=(0, 6))
        input_frame.columnconfigure(0, weight=1)
        input_frame.rowconfigure(0, weight=1)

        self.ban_targets_text = scrolledtext.ScrolledText(
            input_frame, wrap="none", font=("Consolas", 9),
            bg="#1e1e1e", fg="#d4d4d4", insertbackground="white",
            height=6,
        )
        self.ban_targets_text.grid(row=0, column=0, sticky="nsew")
        self.ban_targets_text.insert("1.0", "")

        opts_frame = ttk.LabelFrame(ban_tab, text="Параметры блокировки", padding="8")
        opts_frame.grid(row=1, column=0, sticky="ew", pady=(0, 6))
        opts_frame.columnconfigure(1, weight=1)

        # Причина бана
        ttk.Label(opts_frame, text="Причина:").grid(row=0, column=0, sticky="w", padx=(0, 6), pady=2)
        self.ban_reason_var = tk.StringVar(value="Перманентная блокировка, Правило 0: Набегатор или твинк набегатора, обход блокировки путём создания нового аккаунта. Бан в реестр. Обжалование в Discord")
        reason_entry = ttk.Entry(opts_frame, textvariable=self.ban_reason_var)
        reason_entry.grid(row=0, column=1, sticky="ew", pady=2)
        
        # Кнопки пресетов причин
        preset_frame = ttk.Frame(opts_frame)
        preset_frame.grid(row=0, column=2, padx=(4, 0), pady=2)
        ttk.Button(preset_frame, text="📋 Пресеты", command=self._show_ban_reason_presets).pack(side="left")
        ttk.Button(preset_frame, text="🔄 Сброс", command=self._reset_ban_reason).pack(side="left", padx=(2, 0))

        # Чекбоксы для авто-бана IP/HWID
        chk_frame = ttk.LabelFrame(opts_frame, text="Дополнительно", padding="4")
        chk_frame.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(6, 0))
        
        self.use_latest_ip_var = tk.BooleanVar(value=False)
        self.use_latest_hwid_var = tk.BooleanVar(value=True)  # По умолчанию включено для защиты
        
        ttk.Checkbutton(
            chk_frame, 
            text="📍 Забанить последний IP (если бан по нику)",
            variable=self.use_latest_ip_var
        ).grid(row=0, column=0, sticky="w", pady=2, padx=4)
        
        ttk.Checkbutton(
            chk_frame, 
            text="🔑 Забанить последний HWID (если бан по нику)",
            variable=self.use_latest_hwid_var
        ).grid(row=0, column=1, sticky="w", pady=2, padx=4)

        ttk.Label(opts_frame, text="Длительность (минут, 0 = навсегда):").grid(row=2, column=0, sticky="w", padx=(0, 6), pady=2)
        self.ban_minutes_var = tk.StringVar(value="0")
        ttk.Entry(opts_frame, textvariable=self.ban_minutes_var, width=10).grid(row=2, column=1, sticky="w", pady=2)

        btn_frame = ttk.Frame(opts_frame)
        btn_frame.grid(row=3, column=0, columnspan=3, sticky="e", pady=(6, 0))
        self.ban_execute_btn = ttk.Button(
            btn_frame, text="🔨 Выдать блокировку",
            command=self._on_ban_execute,
        )
        self.ban_execute_btn.pack(side="right")

        results_frame = ttk.LabelFrame(ban_tab, text="Результат", padding="4")
        results_frame.grid(row=2, column=0, sticky="nsew")
        results_frame.columnconfigure(0, weight=1)
        results_frame.rowconfigure(0, weight=1)

        self.ban_result_text = scrolledtext.ScrolledText(
            results_frame, wrap="word", font=("Consolas", 9),
            bg="#1e1e1e", fg="#d4d4d4", insertbackground="white",
            state="disabled",
        )
        self.ban_result_text.grid(row=0, column=0, sticky="nsew")

    def _fix_shortcuts(self):
        self.root.bind_all("<KeyPress>", self._on_global_keypress, add=True)

    def _on_global_keypress(self, event):
        if not (event.state & 0x0004):
            return None
        if re.match(r'^[a-z]$', event.keysym):
            return None
        action = {67: "<<Copy>>", 86: "<<Paste>>", 88: "<<Cut>>", 65: "<<SelectAll>>"}.get(event.keycode)
        if action and isinstance(event.widget, (tk.Text, tk.Entry, tk.Listbox)):
            try:
                event.widget.event_generate(action)
                return "break"
            except Exception:
                pass
        return None

    def _toggle_secrets(self):
        show = "" if self.show_secrets.get() else "*"
        self.pw_entry.config(show=show)
        self.tk_entry.config(show=show)

    @staticmethod
    def _show_token_help():
        msg = (
            "Как получить Discord токен:\n\n"
            "1. Откройте Discord (десктоп или браузер)\n"
            "2. Нажмите F12 (или Ctrl+Shift+I)\n"
            "3. Перейдите на вкладку Network\n"
            "4. Отправьте любое сообщение в чат\n"
            "5. В списке запросов нажмите на любой\n"
            "   запрос к discord.com/api/\n"
            "6. В правой панели найдите заголовок\n"
            "   authorization: и скопируйте его значение\n"
        )
        messagebox.showinfo("Как получить токен Discord", msg)

    def _on_mode_change(self):
        self.nickname_entry.config(state="normal" if self.scan_mode.get() == "username" else "disabled")
        self.auto_ban_cb.config(state="normal" if self.scan_mode.get() == "banbypass" else "disabled")

    def _on_save(self):
        self.settings["admin_username"] = self.username_var.get()
        self.settings["admin_password"] = self.password_var.get()
        self.settings["discord_token"] = self.token_var.get()
        self.settings["auth_cookie"] = self.auth_cookie_var.get()
        self.settings["last_nickname"] = self.nickname_var.get()
        try:
            self.settings["message_count"] = int(self.msg_count_var.get())
        except ValueError:
            pass
        try:
            self.settings["bypass_pages"] = int(self.bypass_pages_var.get())
        except ValueError:
            pass
        self.settings["auto_ban"] = bool(self.auto_ban_var.get())
        self._save_settings()
        messagebox.showinfo("", "Настройки сохранены")

    def _show_first_run_dialog(self):
        dialog = tk.Toplevel(self.root)
        dialog.title("Первый запуск — предупреждение")
        dialog.geometry("520x350")
        dialog.resizable(False, False)
        dialog.transient(self.root)
        dialog.grab_set()

        frame = ttk.Frame(dialog, padding="20")
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text="⚠ ПЕРВЫЙ ЗАПУСК", font=("", 14, "bold"),
                  foreground="#f57c00").pack(anchor="w")

        ttk.Label(frame, text="Данные о наказаниях ещё не загружены.",
                  wraplength=460).pack(anchor="w", pady=(10, 4))

        ttk.Label(frame, text=(
            "Скачиваются все сообщения из каналов жалоб Discord.\n"
            "В среднем это занимает 10–15 минут."
        ), wraplength=460).pack(anchor="w", pady=(0, 4))

        ttk.Label(frame, text=(
            "Это нормально. После завершения данные сохранятся локально, "
            "и следующие запуски будут быстрыми."
        ), wraplength=460).pack(anchor="w")

        link_frame = ttk.Frame(frame)
        link_frame.pack(fill="x", pady=(10, 4))
        ttk.Label(link_frame, text="💡 Совет: ", font=("", 10, "bold")).pack(side="left")
        ttk.Label(link_frame, text=(
            "можно скачать уже готовую базу в разделе Releases — "
            "положить deadspace_checker.db рядом с программой и запустить сразу"
        ), wraplength=400).pack(side="left")

        sep = ttk.Separator(frame, orient="horizontal")
        sep.pack(fill="x", pady=(10, 10))

        btn_frame = ttk.Frame(frame)
        btn_frame.pack(fill="x")

        self._start_countdown = 10
        start_btn = ttk.Button(btn_frame, text=f"Начать сканирование (через {self._start_countdown}с)",
                               state="disabled", command=lambda: self._on_first_run_confirm(dialog))
        start_btn.pack(side="right", padx=(6, 0))

        def tick():
            self._start_countdown -= 1
            if self._start_countdown > 0:
                start_btn.config(text=f"Начать сканирование (через {self._start_countdown}с)")
                dialog.after(1000, tick)
            else:
                start_btn.config(text="Начать сканирование", state="normal")

        dialog.after(1000, tick)

        ttk.Button(btn_frame, text="Отмена",
                   command=dialog.destroy).pack(side="right")

        self.root.wait_window(dialog)
        return getattr(self, "_first_run_confirmed", False)

    def _on_first_run_confirm(self, dialog):
        self._first_run_confirmed = True
        dialog.destroy()

    def _on_start(self):
        if not self.username_var.get() or not self.password_var.get():
            messagebox.showerror("Ошибка", "Укажите ADMIN_USERNAME и ADMIN_PASSWORD")
            return
        if not self.token_var.get() and self.scan_mode.get() != "banbypass":
            messagebox.showerror("Ошибка", "Укажите DISCORD_TOKEN")
            return

        mode = self.scan_mode.get()
        nickname = self.nickname_var.get()
        if mode == "username" and not nickname:
            messagebox.showerror("Ошибка", "Укажите имя игрока для пробива")
            return

        complaint_count = self.db.complaint_channel_count()
        if complaint_count == 0:
            if not self._show_first_run_dialog():
                return

        self.output_text.delete("1.0", tk.END)
        self.progress_var.set(0)
        self.progress_label.config(text="")
        self._scan_start = time.time()
        self._last_progress_msg = ""
        self._log("▶ Запуск сканирования...\n")

        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.running = True

        thread_args = (
            self.username_var.get(),
            self.password_var.get(),
            self.token_var.get(),
            mode,
            nickname,
            int(self.msg_count_var.get()),
            int(self.bypass_pages_var.get()),
        )
        threading.Thread(target=self._run_bot, args=thread_args, daemon=True).start()

    def _run_bot(self, admin_username, admin_password, discord_token,
                 scan_mode, scan_nickname, msg_limit, bypass_pages):
        original_stdout = sys.stdout

        self._cleanup_previous_bot()

        try:
            log_format = logging.Formatter("%(asctime)s | %(levelname)-8s | %(message)s")

            setup_logging(
                log_file=None, level=logging.INFO, use_colors=False,
            )
            for h in logging.getLogger().handlers:
                logging.getLogger().removeHandler(h)

            queue_handler = QueueLogHandler(self.output_queue, log_format)
            logging.getLogger().addHandler(queue_handler)
            logging.getLogger().setLevel(logging.INFO)

            discord_logger = logging.getLogger("discord")
            discord_logger.setLevel(logging.WARNING)
            discord_logger.addHandler(queue_handler)

            sys.stdout = QueueStream(self.output_queue)

            load_file(CONFIG_FILE, cfg)
            self._apply_config_overrides()

            cfg.auth.admin_username = admin_username
            cfg.auth.admin_password = admin_password
            cfg.discord.discord_user_token = discord_token

            cfg.scan.username = scan_nickname if scan_mode == "username" else None
            cfg.scan.check_ban_bypass = scan_mode == "banbypass"
            cfg.scan.message_limit = msg_limit
            cfg.scan.ban_bypass_pages = bypass_pages
            cfg.scan.auto_ban_enabled = bool(self.settings.get("auto_ban", False)) and scan_mode == "banbypass"
            cfg.scan.html_report_mode = scan_mode == "banbypass"
            cfg.logging.log_level = "INFO"

            logging.info("Starting Ban Checker Bot")
            mode_desc = (
                f"Username: {cfg.scan.username}" if cfg.scan.username else
                "Ban Bypass Check" if cfg.scan.check_ban_bypass else
                f"Messages: {cfg.scan.message_limit}"
            )
            logging.info(f"Scan mode: {mode_desc}")

            admin_panel = AdminPanel(cfg.auth.admin_username, cfg.auth.admin_password)
            self._admin_panel = admin_panel

            bot_config = {
                "TARGET_CHANNEL_ID": cfg.discord.target_channel_id,
                "COMPLAINT_CHANNEL_IDS": cfg.discord.complaint_channel_ids,
                "COMPLAINT_MESSAGE_HISTORY_LIMIT": cfg.discord.message_history_limit,
                "message_limit": cfg.scan.message_limit,
                "username": cfg.scan.username,
                "check_ban_bypass": cfg.scan.check_ban_bypass,
                "ban_bypass_pages": cfg.scan.ban_bypass_pages,
                "html_report_filename": cfg.report.html_report_filename,
                "graph_format": cfg.report.graph_format,
                "graph_output": cfg.report.graph_output,
                "message_interval_start": None,
                "message_interval_end": None,
                "html_report_mode": cfg.scan.html_report_mode,
                "auth_cookie": self.settings.get("auth_cookie", ""),
            }

            logging.info(f"Discord token length: {len(discord_token)}, starts with: {discord_token[:10]}...")

            bot = BanCheckerBot(discord_token, admin_panel, bot_config,
                               progress_queue=self.output_queue)
            self.bot = bot

            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self.bot_loop = loop
            self._admin_panel_loop = loop
            if cfg.scan.html_report_mode:
                loop.run_until_complete(bot.run_offline())
            else:
                loop.run_until_complete(bot.client.start(discord_token))

        except Exception as e:
            self.output_queue.put(f"\nОшибка: {e}\n")
            import traceback
            self.output_queue.put(traceback.format_exc() + "\n")
        finally:
            self._cleanup_loop()
            sys.stdout = original_stdout
            self.bot = None
            self.bot_loop = None
            self.output_queue.put(f"\n{'─'*50}\nПроцесс завершён\n")
            self.output_queue.put("__DONE__")

    def _cleanup_previous_bot(self):
        loop = getattr(self, 'bot_loop', None)
        if loop and not loop.is_closed():
            try:
                if loop.is_running():
                    async def _do_close():
                        if self.bot:
                            try:
                                await self.bot.close()
                            except Exception:
                                pass
                        _force_close_loop(loop)
                    future = asyncio.run_coroutine_threadsafe(_do_close(), loop)
                    future.result(timeout=10)
                else:
                    try:
                        if self.bot:
                            loop.run_until_complete(self.bot.close())
                    except Exception:
                        pass
                    _force_close_loop(loop)
            except Exception:
                pass
        self.bot_loop = None
        self.bot = None

    def _cleanup_loop(self):
        loop = getattr(self, 'bot_loop', None)
        if loop and not loop.is_closed():
            try:
                if loop.is_running():
                    async def _full_cleanup():
                        try:
                            if self.bot:
                                await self.bot.close()
                        except Exception:
                            pass
                        _force_close_loop(loop)
                    asyncio.run_coroutine_threadsafe(_full_cleanup(), loop).result(timeout=10)
                else:
                    try:
                        if self.bot:
                            loop.run_until_complete(self.bot.close())
                    except Exception:
                        pass
                    _force_close_loop(loop)
            except Exception:
                pass

    def _copy_to_clipboard(self, text):
        self.root.clipboard_clear()
        self.root.clipboard_append(text)

    def _add_section_embed(self, title, accent_color, fields, copy_text=None):
        outer = tk.Frame(self.output_text, bg='#252526', bd=1, relief='solid',
                         highlightbackground='#3a3a3a', padx=0, pady=0)
        accent = tk.Frame(outer, bg=accent_color, width=4)
        accent.pack(side='left', fill='y')
        content = tk.Frame(outer, bg='#252526', padx=10, pady=6)
        content.pack(side='left', fill='x', expand=True)

        title_row = tk.Frame(content, bg='#252526')
        title_row.pack(fill='x')
        tk.Label(title_row, text=title,
                 font=('Consolas', 10, 'bold'), fg=accent_color, bg='#252526').pack(side='left')
        if copy_text:
            tk.Button(title_row, text='📋', font=('Consolas', 9),
                      command=lambda t=copy_text: self._copy_to_clipboard(t),
                      bg='#333333', fg='#eeffff', bd=0, padx=4, cursor='hand2',
                      activebackground='#444444').pack(side='right')

        tk.Frame(content, bg='#3a3a3a', height=1).pack(fill='x', pady=4)

        for field in fields:
            suffix = None
            if len(field) == 4:
                label, value, val_color, suffix = field
            else:
                label, value, val_color = field
            row = tk.Frame(content, bg='#252526')
            row.pack(fill='x', pady=1)
            tk.Label(row, text=label + ':', font=('Consolas', 9, 'bold'),
                     fg='#969696', bg='#252526', width=14, anchor='w').pack(side='left')
            lbl = tk.Label(row, text=str(value), font=('Consolas', 9),
                           fg=val_color, bg='#252526', anchor='w', wraplength=500, justify='left')
            lbl.pack(side='left', fill='x', expand=True)
            if suffix:
                tk.Label(row, text=str(suffix), font=('Consolas', 9),
                         fg='#969696', bg='#252526', anchor='w').pack(side='left')

        self.output_text.window_create(tk.END, window=outer)
        self.output_text.insert(tk.END, '\n')

    def _build_punishment_fields(self, data):
        st = data.get('status', '').upper()
        if st == 'BANNED':
            return '#f07178', 'ЗАБАНЕН', '#f07178'
        elif st == 'SUSPICIOUS':
            return '#ffcb6b', 'ПОДОЗРИТЕЛЬНЫЙ', '#ffcb6b'
        elif st == 'CLEAN':
            return '#c3e88d', 'ЧИСТ', '#c3e88d'
        return '#546e7a', st, '#546e7a'

    def _render_player_summary(self, d):
        st = d.get('status', '').upper()
        if st == 'BANNED':
            ac, st_txt, sc = '#f07178', 'ЗАБАНЕН', '#f07178'
        elif st == 'SUSPICIOUS':
            ac, st_txt, sc = '#ffcb6b', 'ПОДОЗРИТЕЛЬНЫЙ', '#ffcb6b'
        elif st == 'CLEAN':
            ac, st_txt, sc = '#c3e88d', 'ЧИСТ', '#c3e88d'
        else:
            ac, st_txt, sc = '#546e7a', st, '#546e7a'
        search_nick = d.get('nickname', '?')
        primary_nick = d.get('primary', search_nick)
        fields = [
            ('Статус', st_txt, sc),
            ('Наказаний', str(d.get('ban_counts', 0)), '#ffcb6b'),
            ('HWID стёрт', 'Да' if d.get('hwid_erased') else 'Нет', '#969696'),
        ]
        if primary_nick != search_nick:
            fields.insert(0, ('Основной ник', primary_nick, '#ffcb6b'))
        copy = '\n'.join([
            f"Игрок: {search_nick}",
            f"Статус: {st}",
            f"Наказаний: {d.get('ban_counts', 0)}",
        ])
        self._add_section_embed(
            f"ИГРОК: {search_nick}", ac, fields, copy_text=copy
        )

    def _render_punishment(self, d):
        ac, st_txt, sc = self._build_punishment_fields(d)
        player_nick = d.get('player', '?')
        banned_nick = d.get('banned_nickname', player_nick)
        is_alt = banned_nick != player_nick
        nickname_display = banned_nick
        nickname_suffix = ' (альт)' if is_alt else None
        admin = d.get('admin', 'N/A')
        ban_type = d.get('ban_type', '')
        ban_date = d.get('ban_date', '')
        ban_expires = d.get('ban_expires', '')
        date_str = ''
        if ban_date and ban_date != 'N/A':
            date_str = ban_date
            if ban_expires and ban_expires != 'N/A' and ban_expires.lower() not in ('никогда', 'never'):
                date_str += f' → {ban_expires}'
        copy = '\n'.join([
            f"Наказание #{d.get('index', '?')}",
            f"Игрок: {player_nick}",
            f"Статус: {d.get('status', '?')}",
            f"Причина: {d.get('reason', '?')}",
            f"Никнейм: {banned_nick}{' (альт)' if is_alt else ''}",
            f"Выдал: {admin}",
            f"Тип: {ban_type}" if ban_type and ban_type != 'N/A' else '',
            f"Дата: {date_str}" if date_str else '',
        ])
        fields = [
            ('Игрок', player_nick, '#eeffff'),
            ('Статус', st_txt, sc),
            ('Причина', d.get('reason', '?'), '#ffcb6b'),
        ]
        if ban_type and ban_type != 'N/A':
            fields.append(('Тип', ban_type, '#c792ea'))
        if nickname_suffix:
            fields.append(('Никнейм', nickname_display, '#82aaff', nickname_suffix))
        else:
            fields.append(('Никнейм', nickname_display, '#82aaff'))
        fields.append(('Выдал', admin, '#82aaff'))
        if date_str:
            fields.append(('Дата', date_str, '#89ddff'))
        self._add_section_embed(
            f"НАКАЗАНИЕ #{d.get('index', '?')}", ac, fields, copy_text=copy
        )

    def _render_nicknames(self, d):
        nicks = d.get('nicknames', [])
        copy = '\n'.join(nicks)
        max_show = 12
        shown = nicks[:max_show]
        rest = len(nicks) - max_show
        lines = shown[:]
        if rest > 0:
            lines.append(f"... и ещё {rest}")
        self._add_section_embed(
            f"НИКНЕЙМЫ ({len(nicks)})", '#82aaff', [
                ('Основной', d.get('primary', '?'), '#eeffff'),
                ('Всего', str(len(nicks)), '#82aaff'),
                ('Список', '\n'.join(lines), '#c792ea'),
            ], copy_text=copy
        )

    def _render_complaint(self, d):
        link = d.get('link', '?')
        copy = '\n'.join([
            f"Жалоба #{d.get('index', '?')}",
            f"Канал: {d.get('channel', '?')}",
            f"Автор: {d.get('author', '?')}",
            f"Ссылка: {link}",
        ])
        self._add_section_embed(
            f"ЖАЛОБА #{d.get('index', '?')}", '#f07178', [
                ('Канал', d.get('channel', '?'), '#eeffff'),
                ('Автор', d.get('author', '?'), '#82aaff'),
                ('Ссылка', link, '#89ddff'),
                ('Содержание', d.get('content', '')[:200], '#969696'),
            ], copy_text=copy
        )

    def _render_ips(self, d):
        items = d.get('items', [])
        copy = '\n'.join(items)
        max_show = 15
        shown = items[:max_show]
        rest = len(items) - max_show
        lines = shown[:]
        if rest > 0:
            lines.append(f"... и ещё {rest}")
        self._add_section_embed(
            f"IP-АДРЕСА ({len(items)})", '#89ddff', [
                ('Всего', str(len(items)), '#89ddff'),
                ('Основной', d.get('primary', '?'), '#eeffff'),
                ('Список', '\n'.join(lines), '#c792ea'),
            ], copy_text=copy
        )

    def _render_hwids(self, d):
        items = d.get('items', [])
        copy = '\n'.join(items)
        max_show = 15
        shown = items[:max_show]
        rest = len(items) - max_show
        lines = shown[:]
        if rest > 0:
            lines.append(f"... и ещё {rest}")
        self._add_section_embed(
            f"HWID ({len(items)})", '#89ddff', [
                ('Всего', str(len(items)), '#89ddff'),
                ('Основной', d.get('primary', '?'), '#eeffff'),
                ('Список', '\n'.join(lines), '#c792ea'),
            ], copy_text=copy
        )

    def _render_denied_logins(self, d):
        logins = d.get('logins', [])
        max_show = 8
        copy = '\n'.join([
            f"{l.get('time', '?')} | {l.get('user_name', '?')} | {l.get('ip_address', '?')}"
            for l in logins
        ])
        lines = []
        for l in logins[:max_show]:
            t = l.get('time', '?')[:19]
            u = l.get('user_name', '?')
            ip = l.get('ip_address', '?')
            lines.append(f"{t} | {u} | {ip}")
        if len(logins) > max_show:
            lines.append(f"... и ещё {len(logins) - max_show}")
        self._add_section_embed(
            f"ОТКЛОНЁННЫЕ ВХОДЫ ({len(logins)})", '#f07178', [
                ('Всего', str(len(logins)), '#f07178'),
                ('Последние', '\n'.join(lines), '#eeffff'),
            ], copy_text=copy
        )

    def _poll_output(self):
        try:
            processed = 0
            while processed < 200:
                item = self.output_queue.get_nowait()
                processed += 1

                if isinstance(item, dict):
                    msg_type = item.get("type", "")
                    if msg_type == "progress":
                        cur = item.get("current", 0)
                        total = item.get("total", 1)
                        pct = int(cur / max(total, 1) * 100)
                        self.progress_var.set(pct)
                        msg = item.get("msg", "")
                        if msg:
                            self._last_progress_msg = msg
                    elif msg_type == "progress_done":
                        self.progress_var.set(100)
                        self.progress_label.config(text="Завершено")
                    elif msg_type == "log":
                        text = item.get("text", "")
                        if not text:
                            continue
                        self._insert_colored(text)
                    elif msg_type == "punishment":
                        self._render_punishment(item)
                    elif msg_type == "player_summary":
                        self._render_player_summary(item)
                    elif msg_type == "punishments_done":
                        self.output_text.see(tk.END)
                    elif msg_type == "nicknames":
                        self._render_nicknames(item)
                    elif msg_type == "complaint":
                        self._render_complaint(item)
                    elif msg_type == "complaints_done":
                        self.output_text.see(tk.END)
                    elif msg_type == "ips":
                        self._render_ips(item)
                    elif msg_type == "hwids":
                        self._render_hwids(item)
                    elif msg_type == "denied_logins":
                        self._render_denied_logins(item)
                    elif msg_type == "scan_results_done":
                        self.output_text.see(tk.END)
                        self._insert_colored("\n")
                elif isinstance(item, str):
                    if item == "__DONE__":
                        self.running = False
                        self.start_btn.config(state="normal")
                        self.stop_btn.config(state="disabled")
                        self.progress_var.set(100)
                        self.progress_label.config(text="Завершено")
                        self._scan_start = None
                        self._last_progress_msg = ""
                        self._show_report_button()
                        if self.scan_mode.get() != "banbypass":
                            self.root.after(500, self._generate_html_report)
                    elif item == "__BAN_DONE__":
                        self.ban_execute_btn.config(state="normal")
                    elif re.match(r'^\d{4}-\d{2}-\d{2}', item) or 'API Calls=' in item or 'Depth Dist=' in item:
                        continue
                    else:
                        self._insert_colored(item)
                        self._ban_log(item)

            self.output_text.see(tk.END)
        except queue.Empty:
            pass

        if getattr(self, '_scan_start', None) and getattr(self, '_last_progress_msg', None):
            dt = time.time() - self._scan_start
            if dt >= 3600:
                elapsed = f"{int(dt//3600)}ч {int((dt%3600)//60)}м {int(dt%60)}с"
            elif dt >= 60:
                elapsed = f"{int(dt//60)}м {int(dt%60)}с"
            else:
                elapsed = f"{int(dt)}с"
            self.progress_label.config(text=f"{self._last_progress_msg}  ⏱ {elapsed}")

        self.root.after(50, self._poll_output)

    def _on_global_ctrl(self, event):
        # Устаревший метод, больше не используется
        # Стандартные Ctrl+C/V/X работают автоматически в tkinter
        return None

    def _log(self, text):
        self.output_text.insert(tk.END, text)
        self.output_text.see(tk.END)

    def _on_stop(self):
        self.running = False
        self._cleanup_previous_bot()
        self._log("\n⏹ Остановлено\n")
        self.start_btn.config(state="normal")
        self.stop_btn.config(state="disabled")
        self.progress_label.config(text="Остановлено")
        self._scan_start = None
        self._last_progress_msg = ""

    def _show_report_button(self):
        if hasattr(self, '_report_btn') and self._report_btn.winfo_exists():
            return
        btn_frame = tk.Frame(self.output_text, bg="#2d2d2d", highlightbackground="#4a4a4a", highlightthickness=1, padx=8, pady=6)
        self._report_btn = tk.Button(
            btn_frame, text="📄 СФОРМИРОВАТЬ ОТЧЁТ",
            command=self._generate_html_report,
            font=("Segoe UI", 11, "bold"),
            bg="#82aaff", fg="#1a1a1a",
            activebackground="#6e8cd9", activeforeground="#1a1a1a",
            relief="raised", bd=2, padx=20, pady=6, cursor="hand2"
        )
        self._report_btn.pack()
        self.output_text.window_create(tk.END, window=btn_frame)
        self.output_text.insert(tk.END, '\n')
        self.output_text.see(tk.END)

    @staticmethod
    def _html_escape(text):
        if not text:
            return ""
        return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

    def _generate_html_report(self):
        report_dir = os.path.join(app_dir(), "reports")
        json_path = os.path.join(report_dir, "scan_report.json")
        if not os.path.exists(json_path):
            self._insert_colored(f"\nФайл отчёта не найден: {json_path}\n")
            return
        try:
            import json
            with open(json_path, encoding='utf-8') as f:
                data = json.load(f)
        except Exception as e:
            self._insert_colored(f"\nОшибка чтения отчёта: {e}\n")
            return

        self._insert_colored("\n🔍 Проверка IP на VPN...\n")
        try:
            enrich_report_data(data)
        except Exception:
            pass

        esc = self._html_escape

        logo_b64 = ""
        try:
            with open(LOGO_PATH, "rb") as f:
                logo_b64 = base64.b64encode(f.read()).decode()
        except Exception:
            pass

        player = data[0] if data else {}
        nick = player.get("nickname", "Неизвестно")
        primary = player.get("primary_nickname", nick)
        status = player.get("status", "unknown")
        bans = player.get("ban_counts", 0)
        reasons = player.get("ban_reasons", [])
        hwid_erased = player.get("hwid_erased", False)

        status_color = {"banned": "#f07178", "suspicious": "#ffcb6b", "clean": "#c3e88d"}.get(status.lower(), "#546e7a")
        status_ru = {"banned": "ЗАБАНЕН", "suspicious": "ПОДОЗРИТЕЛЬНЫЙ", "clean": "ЧИСТ"}.get(status.lower(), status)

        reasons_html = ""
        for i, r in enumerate(reasons):
            reason = r.get("reason", str(r)) if isinstance(r, dict) else str(r)
            banned_nick = r.get("username", primary) if isinstance(r, dict) else primary
            admin = r.get("admin", "N/A") if isinstance(r, dict) else "N/A"
            ban_type = r.get("type", "") if isinstance(r, dict) else ""
            ban_date = r.get("date", "") if isinstance(r, dict) else ""
            ban_expires = r.get("expires", "") if isinstance(r, dict) else ""
            is_alt = banned_nick != primary
            reason_short = (reason[:600] + "...") if len(reason) > 600 else reason
            stripe = "#2a2a2a" if i % 2 == 0 else "#252526"
            nickname_html = f'<span class="val blue">{esc(banned_nick)}</span>'
            if is_alt:
                nickname_html += f' <span class="gray">(альт)</span>'
            date_html = ""
            if ban_date and ban_date != "N/A":
                date_str = esc(ban_date)
                if ban_expires and ban_expires != "N/A" and ban_expires.lower() not in ("никогда", "never"):
                    date_str += f" → {esc(ban_expires)}"
                date_html = f'\n                <div class="field"><span class="key">Дата</span><span class="val cyan">{date_str}</span></div>'
            type_html = ""
            if ban_type and ban_type != "N/A":
                type_html = f'\n                <div class="field"><span class="key">Тип</span><span class="val purple">{esc(ban_type)}</span></div>'
            reasons_html += f"""
            <div class="info-card" style="background:{stripe}">
              <div class="badge bad-red">{i+1}</div>
              <div class="card-fields">
                <div class="field"><span class="key">Причина</span><span class="val yellow">{esc(reason_short)}</span></div>{type_html}
                <div class="field"><span class="key">Никнейм</span>{nickname_html}</div>
                <div class="field"><span class="key">Выдал</span><span class="val blue">{esc(admin)}</span></div>{date_html}
              </div>
            </div>"""

        html = f"""<!DOCTYPE html>
<html lang="ru">
<head><meta charset="utf-8"><title>DeadSpace Check — {esc(primary)}</title>
<style>
  *{{margin:0;padding:0;box-sizing:border-box}}
  body{{background:#1a1a1a;color:#d4d4d4;font-family:'Segoe UI',sans-serif;padding:24px;display:flex;justify-content:center}}
  .report{{max-width:780px;width:100%}}
  .header{{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:20px}}
  .header-left h1{{font-size:24px;margin-bottom:4px}}
  .header-left .sub{{color:#888;font-size:13px}}
  .status-badge{{display:inline-block;padding:4px 14px;border-radius:4px;font-weight:700;font-size:13px;color:#fff;background:{status_color}}}
  .section-title{{font-size:16px;font-weight:700;margin:24px 0 10px;padding-bottom:6px;border-bottom:1px solid #333;color:#eeffff}}
  .info-card{{border-radius:6px;padding:12px 16px;margin-bottom:6px;display:flex;gap:12px;align-items:start}}
  .badge{{color:#fff;font-weight:700;font-size:13px;border-radius:50%;width:28px;height:28px;min-width:28px;display:flex;align-items:center;justify-content:center}}
  .bad-red{{background:#f07178}}
  .bad-green{{background:#c3e88d;color:#1a1a1a}}
  .bad-blue{{background:#82aaff;color:#1a1a1a}}
  .bad-purple{{background:#c792ea}}
  .bad-orange{{background:#ffcb6b;color:#1a1a1a}}
  .card-fields{{flex:1;min-width:0}}
  .field{{display:flex;gap:8px;margin-bottom:3px;font-size:13px;align-items:baseline}}
  .key{{color:#888;min-width:70px;flex-shrink:0;font-weight:600}}
  .val{{word-break:break-word}}
  .yellow{{color:#ffcb6b}}
  .blue{{color:#82aaff}}
  .green{{color:#c3e88d}}
  .purple{{color:#c792ea}}
  .gray{{color:#888}}
  .orange{{color:#ffcb6b}}
  .cyan{{color:#89ddff}}
  .mono{{font-family:'Consolas','Courier New',monospace;font-size:12px;word-break:break-all}}
  .link{{color:#82aaff;text-decoration:underline;word-break:break-all}}
  .nick-list{{background:#252526;border-radius:6px;padding:12px 16px;font-size:13px;line-height:1.7;color:#c792ea}}
  .content-box{{background:#1e1e1e;border-radius:4px;padding:8px 10px;margin-top:4px;font-size:12px;line-height:1.5;color:#d4d4d4;white-space:pre-wrap;word-break:break-word;max-height:200px;overflow-y:auto;border:1px solid #333}}
  .footer{{margin-top:32px;padding-top:12px;border-top:1px solid #333;font-size:11px;color:#555;text-align:center}}
  .footer .brand{{color:#82aaff;font-weight:600}}
  .tag{{display:inline-block;padding:1px 8px;border-radius:3px;font-size:11px;font-weight:600;margin-right:4px}}
  .tag-red{{background:#f0717844;color:#f07178}}
  .tag-green{{background:#c3e88d44;color:#c3e88d}}
  .tag-orange{{background:#ffcb6b44;color:#ffcb6b}}
  .tag-blue{{background:#82aaff44;color:#82aaff}}
  .copy-btn{background:#3a3a3a;color:#c3e88d;border:1px solid #555;border-radius:4px;cursor:pointer;font-size:13px;padding:2px 8px;transition:.15s;white-space:nowrap}
  .copy-btn:hover{background:#4a4a4a;border-color:#82aaff}
  .copy-btn::after{content:attr(data-tip);display:none;position:absolute;bottom:130%;left:50%;transform:translateX(-50%);background:#333;color:#d4d4d4;padding:4px 10px;border-radius:4px;font-size:11px;white-space:nowrap;pointer-events:none;z-index:10}
  .copy-btn:hover::after{display:block}
  .copy-btn-wrap{position:relative;display:inline-flex;align-items:center}
  .nick-item{display:inline-flex;align-items:center;gap:4px;margin:2px 0}
  #graph-container{{border:1px solid #333}}
</style>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/vis-network/9.1.2/dist/dist/vis-network.min.css">
<script src="https://cdnjs.cloudflare.com/ajax/libs/vis-network/9.1.2/dist/vis-network.min.js"></script>
</head>
<body><div class="report">
  <div class="header">
    <div class="header-left">
      <h1>🔍 {esc(primary)}</h1>
      <div class="sub">Ник поиска: {esc(nick)}</div>
    </div>
    {f'<img src="data:image/png;base64,{logo_b64}" width="64" height="64" alt="Logo" style="border-radius:8px">' if logo_b64 else ''}
  </div>
  <span class="status-badge">{status_ru}</span>

  <div class="section-title">📜 Наказания</div>
  {reasons_html if reasons_html else '<div class="gray" style="padding:8px 0;font-size:13px">Нет наказаний</div>'}
"""
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

        import json as _json
        nickname_data_json = _json.dumps(nickname_data, ensure_ascii=False)
        html += f"""<script>
function copyText(text) {{
    if (navigator.clipboard && navigator.clipboard.writeText) {{
        navigator.clipboard.writeText(text).catch(function() {{ fallbackCopy(text); }});
    }} else {{ fallbackCopy(text); }}
}}
function fallbackCopy(text) {{
    var ta = document.createElement('textarea');
    ta.value = text; ta.style.position='fixed'; ta.style.left='-9999px';
    document.body.appendChild(ta); ta.select();
    try {{ document.execCommand('copy'); }} catch(e) {{}}
    document.body.removeChild(ta);
}}
function copyNicknameData(nick) {{
    var data = nicknameData[nick]; if (!data) return;
    var lines = [];
    for (var i = 0; i < data.ips.length; i++) lines.push(data.ips[i]);
    for (var i = 0; i < data.hwids.length; i++) lines.push(data.hwids[i]);
    copyText(lines.join('\\n'));
}}
var nicknameData = {nickname_data_json};
</script>"""

        graph_injected = False
        for item in data[1:]:
            typ = item.get("type", "")
            if typ == "associated_accounts":
                nicks = item.get("nicknames", [])
                all_nicks_text = "\\n".join(esc(n) for n in nicks)
                html += f'<div class="section-title" style="display:flex;align-items:center;gap:10px"><span>👤 Связанные никнеймы ({len(nicks)})</span><div class="copy-btn-wrap"><button class="copy-btn" onclick=\'copyText("{all_nicks_text}")\' data-tip="Скопировать все никнеймы">📋</button></div></div>'
                nick_items = []
                for n in nicks:
                    safe_n = esc(n)
                    nick_items.append(f'<span class="nick-item"><span class="copy-btn-wrap"><button class="copy-btn" onclick=\'copyNicknameData("{safe_n}")\' data-tip="Скопировать IP и HWID для {esc(n)}">📋</button></span>{esc(n)}</span>')
                html += f'<div class="nick-list">{"<br>".join(nick_items)}</div>\n'
                if not graph_injected:
                    html += generate_vis_graph_from_report_data(data)
                    graph_injected = True

            elif typ == "complaints":
                links = item.get("links", [])
                html += f'<div class="section-title">📋 Наказания на других серверах ({len(links)})</div>'
                for ci, c in enumerate(links[:30]):
                    ch = c.get("channel", "?")
                    auth = c.get("author", "?")
                    content = c.get("content", "")
                    link = c.get("link", "")
                    stripe = "#2a2a2a" if ci % 2 == 0 else "#252526"
                    content_short = (content[:800] + "...") if len(content) > 800 else content
                    content_html = f'<div class="content-box">{esc(content_short)}</div>' if content else ""
                    link_html = f'<div class="field"><span class="key">Ссылка</span><a class="val link" href="{esc(link)}">{esc(link[:90])}{"..." if len(link)>90 else ""}</a></div>' if link else ""
                    html += f'''<div class="info-card" style="background:{stripe}">
                <div class="badge bad-blue">{ci+1}</div>
                <div class="card-fields">
                  <div class="field"><span class="key">Канал</span><span class="val orange">#{esc(ch)}</span></div>
                  <div class="field"><span class="key">Автор</span><span class="val blue">{esc(auth)}</span></div>
                  {link_html}
                  {content_html}
                </div>
              </div>'''
                html += '\n'

            elif typ == "associated_ips":
                ips = item.get("ips", [])
                all_ips_text = "\\n".join(esc(ip_entry.get("direct_ip_connections", "?")) for ip_entry in ips)
                html += f'<div class="section-title" style="display:flex;align-items:center;gap:10px"><span>🌐 Связанные IP-адреса ({len(ips)})</span><div class="copy-btn-wrap"><button class="copy-btn" onclick=\'copyText("{all_ips_text}")\' data-tip="Скопировать все IP-адреса">📋</button></div></div>'
                for idx, ip_entry in enumerate(ips[:20]):
                    ip = ip_entry.get("direct_ip_connections", "?")
                    owner = ip_entry.get("owner", "")
                    shared = ip_entry.get("shared_with", [])
                    owned_by_primary = ip_entry.get("owned_by_primary", False)
                    owned_by_alt = ip_entry.get("owned_by_alt", False)
                    stripe = "#252526"
                    vpn_info = ip_entry.get("vpn_info", {})
                    vpn_badges = ""
                    if vpn_info.get("proxy"):
                        vpn_badges += '<span class="tag tag-red">VPN</span> '
                    if vpn_info.get("hosting"):
                        vpn_badges += '<span class="tag tag-orange">Хостинг</span> '
                    owner_tag = ""
                    if owned_by_primary:
                        owner_tag = '<span class="tag tag-green">Основной</span>'
                    elif owned_by_alt:
                        owner_tag = '<span class="tag tag-orange">Альт</span>'
                    else:
                        owner_tag = '<span class="tag tag-red">Чужой</span>'
                    shared_html = ""
                    if shared:
                        shared_html = f'<div class="field"><span class="key">Общие с</span><span class="val purple">{esc(", ".join(shared[:8]))}</span></div>'
                    html += f'''<div class="info-card" style="background:{stripe}">
                <div class="badge bad-purple">{idx+1}</div>
                <div class="card-fields">
                  <div class="field"><span class="key">IP</span><span class="val mono cyan">{esc(ip)}</span> {owner_tag} {vpn_badges}</div>
                  {shared_html}
                </div>
              </div>'''
                html += '\n'

            elif typ == "associated_hwids":
                hwids = item.get("hwids", [])
                all_hwids_text = "\\n".join(esc(hw_entry.get("hwid", "?")) for hw_entry in hwids)
                html += f'<div class="section-title" style="display:flex;align-items:center;gap:10px"><span>🔑 Связанные HWID ({len(hwids)})</span><div class="copy-btn-wrap"><button class="copy-btn" onclick=\'copyText("{all_hwids_text}")\' data-tip="Скопировать все HWID">📋</button></div></div>'
                for idx, hw_entry in enumerate(hwids[:20]):
                    hwid = hw_entry.get("hwid", "?")
                    owner = hw_entry.get("owner", "")
                    shared = hw_entry.get("shared_with", [])
                    owned_by_primary = hw_entry.get("owned_by_primary", False)
                    owned_by_alt = hw_entry.get("owned_by_alt", False)
                    stripe = "#252526"
                    owner_tag = ""
                    if owned_by_primary:
                        owner_tag = '<span class="tag tag-green">Основной</span>'
                    elif owned_by_alt:
                        owner_tag = '<span class="tag tag-orange">Альт</span>'
                    else:
                        owner_tag = '<span class="tag tag-red">Чужой</span>'
                    shared_html = ""
                    if shared:
                        shared_html = f'<div class="field"><span class="key">Общие с</span><span class="val purple">{esc(", ".join(shared[:8]))}</span></div>'
                    html += f'''<div class="info-card" style="background:{stripe}">
                <div class="badge bad-orange">{idx+1}</div>
                <div class="card-fields">
                  <div class="field"><span class="key">HWID</span><span class="val mono">{esc(hwid)}</span> {owner_tag}</div>
                  {shared_html}
                </div>
              </div>'''
                html += '\n'

            elif typ == "denied_login_attempts":
                attempts = item.get("attempts", [])
                if attempts:
                    html += f'<div class="section-title">🚫 Отклонённые входы ({len(attempts)})</div>'
                    for ai, a in enumerate(attempts[:12]):
                        t = a.get("time", "?")[:19]
                        u = a.get("user_name", "?")
                        ip = a.get("ip_address", "?")
                        server = a.get("server", "?")
                        hwid = a.get("hwid", "")
                        stripe = "#2a2a2a" if ai % 2 == 0 else "#252526"
                        vpn_info = a.get("vpn_info", {})
                        vpn_badge = ""
                        if vpn_info.get("proxy"):
                            vpn_badge = '<span class="tag tag-red">VPN</span>'
                        elif vpn_info.get("hosting"):
                            vpn_badge = '<span class="tag tag-orange">Хостинг</span>'
                        hwid_html = f'<div class="field"><span class="key">HWID</span><span class="val mono gray">{esc(hwid)}</span></div>' if hwid else ""
                        html += f'''<div class="info-card" style="background:{stripe}">
                <div class="badge bad-red">{ai+1}</div>
                <div class="card-fields">
                  <div class="field"><span class="key">Время</span><span class="val">{esc(t)}</span></div>
                  <div class="field"><span class="key">Ник</span><span class="val yellow">{esc(u)}</span></div>
                  <div class="field"><span class="key">IP</span><span class="val mono cyan">{esc(ip)}</span> {vpn_badge}</div>
                  <div class="field"><span class="key">Сервер</span><span class="val">{esc(server)}</span></div>
                  {hwid_html}
                </div>
              </div>'''
                    html += '\n'

        html += """<div class="footer"><span class="brand">Golub4ik (WikiHampter) DeadSpace Checker</span></div></div></body></html>"""

        out_path = os.path.join(report_dir, "scan_report.html")
        try:
            with open(out_path, 'w', encoding='utf-8') as f:
                f.write(html)
            import webbrowser
            webbrowser.open(f'file://{os.path.abspath(out_path)}')
            self._insert_colored(f"\n📄 Отчёт открыт в браузере: {out_path}\n")
            save_to = filedialog.asksaveasfilename(
                parent=self.root,
                title="Сохранить отчёт как",
                defaultextension=".html",
                initialfile=f"{primary}.html",
                filetypes=[("HTML files", "*.html"), ("All files", "*.*")]
            )
            if save_to:
                try:
                    with open(save_to, 'w', encoding='utf-8') as f:
                        f.write(html)
                    self._insert_colored(f"💾 Отчёт сохранён: {save_to}\n")
                except Exception as e:
                    self._insert_colored(f"Ошибка сохранения: {e}\n")
        except Exception as e:
            self._insert_colored(f"\nОшибка сохранения отчета: {e}\n")

    def _apply_config_overrides(self):
        overrides = self.settings.get("config", {})
        if not overrides:
            return
        for name, value in overrides.items():
            path = CONFIG_OVERRIDE_MAP.get(name)
            if path is None:
                continue
            parent = getattr(cfg, path[0])
            setattr(parent, path[1], value)

    def _open_config_dialog(self):
        dialog = tk.Toplevel(self.root)
        dialog.title("Настройки конфигурации")
        dialog.geometry("640x520")
        dialog.transient(self.root)
        dialog.grab_set()

        overrides = dict(self.settings.get("config", {}))

        def _val(name, default):
            return overrides.get(name, default)

        notebook = ttk.Notebook(dialog)
        notebook.pack(fill="both", expand=True, padx=8, pady=8)

        def make_entry(parent, label, default, row):
            ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(4, 8), pady=3)
            var = tk.StringVar(value=str(default))
            entry = ttk.Entry(parent, textvariable=var, width=40)
            entry.grid(row=row, column=1, sticky="ew", pady=3)
            parent.columnconfigure(1, weight=1)
            return var

        def make_text(parent, label, default, row):
            ttk.Label(parent, text=label).grid(row=row, column=0, sticky="nw", padx=(4, 8), pady=3)
            frame = ttk.Frame(parent)
            frame.grid(row=row, column=1, sticky="ew", pady=3)
            frame.columnconfigure(0, weight=1)
            text = tk.Text(frame, height=6, width=40)
            text.grid(row=0, column=0, sticky="ew")
            scroll = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
            scroll.grid(row=0, column=1, sticky="ns")
            text.config(yscrollcommand=scroll.set)
            if isinstance(default, list):
                text.insert("1.0", "\n".join(str(x) for x in default))
            return text

        tab1 = ttk.Frame(notebook)
        notebook.add(tab1, text="Discord")
        r = 0
        v_target_id = make_entry(tab1, "TARGET_CHANNEL_ID:", _val("TARGET_CHANNEL_ID", CFG_TARGET_CHANNEL_ID), r); r += 1
        t_complaint_ids = make_text(tab1, "COMPLAINT_CHANNEL_IDS:", _val("COMPLAINT_CHANNEL_IDS", CFG_COMPLAINT_CHANNEL_IDS), r); r += 1
        v_msg_hist = make_entry(tab1, "MESSAGE_HISTORY_LIMIT:", _val("MESSAGE_HISTORY_LIMIT", CFG_MESSAGE_HISTORY_LIMIT), r); r += 1

        tab2 = ttk.Frame(notebook)
        notebook.add(tab2, text="API")
        r = 0
        v_base_url = make_entry(tab2, "BASE_ADMIN_URL:", _val("BASE_ADMIN_URL", CFG_BASE_ADMIN_URL), r); r += 1
        v_acc_url = make_entry(tab2, "ACCOUNT_URL:", _val("ACCOUNT_URL", CFG_ACCOUNT_URL), r); r += 1
        v_op_timeout = make_entry(tab2, "OPERATION_TIMEOUT:", _val("OPERATION_TIMEOUT", CFG_OPERATION_TIMEOUT), r); r += 1
        v_req_timeout = make_entry(tab2, "REQUEST_TIMEOUT:", _val("REQUEST_TIMEOUT", CFG_REQUEST_TIMEOUT), r); r += 1
        v_search_timeout = make_entry(tab2, "SEARCH_TIMEOUT:", _val("SEARCH_TIMEOUT", CFG_SEARCH_TIMEOUT), r); r += 1
        v_batch_timeout = make_entry(tab2, "BATCH_TIMEOUT:", _val("BATCH_TIMEOUT", CFG_BATCH_TIMEOUT), r); r += 1
        v_term_timeout = make_entry(tab2, "TERM_TIMEOUT:", _val("TERM_TIMEOUT", CFG_TERM_TIMEOUT), r); r += 1
        v_max_conc = make_entry(tab2, "MAX_CONCURRENT_REQUESTS:", _val("MAX_CONCURRENT_REQUESTS", CFG_MAX_CONCURRENT_REQUESTS), r); r += 1

        tab3 = ttk.Frame(notebook)
        notebook.add(tab3, text="Сканирование")
        r = 0
        v_search_max_depth = make_entry(tab3, "SEARCH_MAX_DEPTH:", _val("SEARCH_MAX_DEPTH", CFG_SEARCH_MAX_DEPTH), r); r += 1
        v_search_limit_root = make_entry(tab3, "SEARCH_LIMIT_ROOT:", _val("SEARCH_LIMIT_ROOT", CFG_SEARCH_LIMIT_ROOT), r); r += 1
        v_search_limit_l1 = make_entry(tab3, "SEARCH_LIMIT_LEVEL1:", _val("SEARCH_LIMIT_LEVEL1", CFG_SEARCH_LIMIT_LEVEL1), r); r += 1
        v_search_limit_l2 = make_entry(tab3, "SEARCH_LIMIT_LEVEL2:", _val("SEARCH_LIMIT_LEVEL2", CFG_SEARCH_LIMIT_LEVEL2), r); r += 1
        v_search_limit_def = make_entry(tab3, "SEARCH_LIMIT_DEFAULT:", _val("SEARCH_LIMIT_DEFAULT", CFG_SEARCH_LIMIT_DEFAULT), r); r += 1
        v_bypass_depth = make_entry(tab3, "BYPASS_SEARCH_MAX_DEPTH:", _val("BYPASS_SEARCH_MAX_DEPTH", CFG_BYPASS_SEARCH_MAX_DEPTH), r); r += 1
        v_cache_size = make_entry(tab3, "SEARCH_CACHE_MAX_SIZE:", _val("SEARCH_CACHE_MAX_SIZE", CFG_SEARCH_CACHE_MAX_SIZE), r); r += 1
        v_cache_ttl = make_entry(tab3, "SEARCH_CACHE_TTL:", _val("SEARCH_CACHE_TTL", CFG_SEARCH_CACHE_TTL), r); r += 1

        tab4 = ttk.Frame(notebook)
        notebook.add(tab4, text="Тайминги")
        r = 0
        v_close_time = make_entry(tab4, "CLOSE_TIME_THRESHOLD_MINUTES:", _val("CLOSE_TIME_THRESHOLD_MINUTES", CFG_CLOSE_TIME_THRESHOLD_MINUTES), r); r += 1
        v_time_thresh = make_entry(tab4, "TIME_THRESHOLD_MINUTES:", _val("TIME_THRESHOLD_MINUTES", CFG_TIME_THRESHOLD_MINUTES), r); r += 1
        v_susp_time = make_entry(tab4, "SUSPICIOUS_TIME_THRESHOLD_MINUTES:", _val("SUSPICIOUS_TIME_THRESHOLD_MINUTES", CFG_SUSPICIOUS_TIME_THRESHOLD_MINUTES), r); r += 1
        v_ip_time = make_entry(tab4, "IP_MATCH_TIMEDELTA_MINUTES:", _val("IP_MATCH_TIMEDELTA_MINUTES", CFG_IP_MATCH_TIMEDELTA_MINUTES), r); r += 1

        def _save_config():
            overrides.clear()

            def _add(name, var):
                raw = var.get().strip()
                try:
                    val = ast.literal_eval(raw)
                except Exception:
                    val = raw
                if val != _parse_config_value(_RAW_CFG, name, None):
                    overrides[name] = val

            def _add_text(name, text_widget):
                raw = text_widget.get("1.0", tk.END).strip()
                if not raw:
                    vals = []
                else:
                    vals = [ast.literal_eval(line.strip()) for line in raw.split("\n") if line.strip()]
                if vals != _parse_config_value(_RAW_CFG, name, None):
                    overrides[name] = vals

            _add("TARGET_CHANNEL_ID", v_target_id)
            _add_text("COMPLAINT_CHANNEL_IDS", t_complaint_ids)
            _add("MESSAGE_HISTORY_LIMIT", v_msg_hist)
            _add("BASE_ADMIN_URL", v_base_url)
            _add("ACCOUNT_URL", v_acc_url)
            _add("OPERATION_TIMEOUT", v_op_timeout)
            _add("REQUEST_TIMEOUT", v_req_timeout)
            _add("SEARCH_TIMEOUT", v_search_timeout)
            _add("BATCH_TIMEOUT", v_batch_timeout)
            _add("TERM_TIMEOUT", v_term_timeout)
            _add("MAX_CONCURRENT_REQUESTS", v_max_conc)
            _add("SEARCH_MAX_DEPTH", v_search_max_depth)
            _add("SEARCH_LIMIT_ROOT", v_search_limit_root)
            _add("SEARCH_LIMIT_LEVEL1", v_search_limit_l1)
            _add("SEARCH_LIMIT_LEVEL2", v_search_limit_l2)
            _add("SEARCH_LIMIT_DEFAULT", v_search_limit_def)
            _add("BYPASS_SEARCH_MAX_DEPTH", v_bypass_depth)
            _add("SEARCH_CACHE_MAX_SIZE", v_cache_size)
            _add("SEARCH_CACHE_TTL", v_cache_ttl)
            _add("CLOSE_TIME_THRESHOLD_MINUTES", v_close_time)
            _add("TIME_THRESHOLD_MINUTES", v_time_thresh)
            _add("SUSPICIOUS_TIME_THRESHOLD_MINUTES", v_susp_time)
            _add("IP_MATCH_TIMEDELTA_MINUTES", v_ip_time)

            self.settings["config"] = dict(overrides)
            self._save_settings()
            dialog.destroy()

        btn_frame = ttk.Frame(dialog)
        btn_frame.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Button(btn_frame, text="Сохранить", command=_save_config).pack(side="right", padx=(4, 0))
        ttk.Button(btn_frame, text="Отмена", command=dialog.destroy).pack(side="right")

    def _ban_log(self, text, color=None):
        self.ban_result_text.config(state="normal")
        if color:
            self.ban_result_text.insert(tk.END, text, color)
        else:
            self.ban_result_text.insert(tk.END, text)
        self.ban_result_text.see(tk.END)
        self.ban_result_text.config(state="disabled")

    @staticmethod
    def _detect_target_type(value):
        value = value.strip()
        if not value:
            return None
        # IP адрес
        if re.match(r'^\d{1,3}(\.\d{1,3}){3}$', value):
            return "ip"
        # User ID (GUID)
        if re.match(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', value, re.I):
            return "user_id"
        # HWID - обычно очень длинная строка с base64-подобными символами или GUID-подобные форматы
        # HWID обычно > 30 символов и содержит специфичные символы
        if len(value) > 30 and re.match(r'^[A-Za-z0-9+/=,\-{}: ]+$', value):
            return "hwid"
        # Всё остальное считаем никнеймом/именем пользователя
        return "username"

    def _reset_ban_reason(self):
        """Сброс причины на стандартную"""
        self.ban_reason_var.set("Перманентная блокировка, Правило 0: Набегатор или твинк набегатора, обход блокировки путём создания нового аккаунта. Бан в реестр. Обжалование в Discord")

    def _show_ban_reason_presets(self):
        dialog = tk.Toplevel(self.root)
        dialog.title("Пресеты причин бана")
        dialog.geometry("600x500")
        dialog.transient(self.root)
        dialog.grab_set()

        frame = ttk.Frame(dialog, padding="15")
        frame.pack(fill="both", expand=True)

        presets = [
            ("Обход блокировки",
             "Перма ДК. Правило 9.2. Попытка обхода блокировки путем создания нового аккаунта. Просим откликнуться на нашем Discord сервере в канале с обжалованиями."),

            ("ПДК по жалобе",
             "Перма ДК. На вас поступила жалоба, просим откликнуться в канале жалоб на игроков. [Ссылка на жалобу]."),

            ("Набег на сервер партнёров",
             "Перманентная блокировка. Правило 0. Набег на сервер партнёров. Обжалование в Discord."),

            ("Набегаторский твинк HWID/IP",
             "Перманентная блокировка, Правило 0: Набегатор или твинк набегатора, обход блокировки путём создания нового аккаунта. Бан в реестр. Обжалование в Discord"),

            ("Перманентная блокировка",
             "Перманентная блокировка, Правило X, рецидив(если имеется): [краткое, понятное описание ситуации]. Обжалование в Discord."),

            ("Набегатор",
             "Перманентная блокировка, Правило 0: Набегатор. [краткое, понятное описание ситуации]. Бан в реестр. Обжалование в Discord."),

            ("БВО",
             "Перманентная блокировка БВО: Правило X, [Краткое, понятное описания ситуации]. Без возможности обжалования."),
        ]

        canvas = tk.Canvas(frame, highlightthickness=0)
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=canvas.yview)
        scrollable = ttk.Frame(canvas)
        scrollable.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=scrollable, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)

        ttk.Label(scrollable, text="Локальные пресеты:", font=("", 11, "bold")).pack(anchor="w", pady=(0, 5))

        for title, reason in presets:
            btn = ttk.Button(
                scrollable, text=title,
                command=lambda r=reason: self._apply_preset_reason(r, dialog)
            )
            btn.pack(fill="x", pady=2)

        ttk.Separator(scrollable, orient="horizontal").pack(fill="x", pady=(10, 5))
        ttk.Label(scrollable, text="Пресеты с админ-сайта:", font=("", 11, "bold")).pack(anchor="w", pady=(0, 5))

        load_btn = ttk.Button(scrollable, text="📥 Загрузить с админки")
        load_btn.pack(fill="x", pady=2)
        loading_label = ttk.Label(scrollable, text="")
        loading_label.pack()

        def load_templates():
            load_btn.config(state="disabled")
            loading_label.config(text="Загрузка...")
            import threading, asyncio
            threading.Thread(target=self._load_admin_templates_thread, args=(scrollable, loading_label, dialog), daemon=True).start()

        load_btn.config(command=load_templates)

        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        ttk.Button(frame, text="Отмена", command=dialog.destroy).pack(fill="x", pady=(10, 0))

    def _load_admin_templates_thread(self, parent, loading_label, dialog):
        try:
            if self._admin_panel and self._admin_panel_loop and not self._admin_panel_loop.is_closed():
                try:
                    future = asyncio.run_coroutine_threadsafe(
                        self._fetch_templates_shared(parent, loading_label, dialog),
                        self._admin_panel_loop
                    )
                    future.result(timeout=15)
                    return
                except asyncio.TimeoutError:
                    parent.after(0, lambda: loading_label.config(text="❌ Таймаут загрузки шаблонов"))
                    return
                except Exception as e:
                    parent.after(0, lambda m=str(e): loading_label.config(text=f"❌ Ошибка: {m[:80]}"))
                    return
            parent.after(0, lambda: loading_label.config(text="❌ Запустите сканирование игрока для авторизации на админ-сайте"))
        except Exception as exc:
            parent.after(0, lambda m=str(exc): loading_label.config(text=f"❌ Ошибка: {m[:80]}"))

    async def _fetch_templates_shared(self, parent, loading_label, dialog):
        try:
            templates = await self._admin_panel.fetch_ban_templates(require_auth=False)
            if not templates and not self._admin_panel._is_authenticated:
                parent.after(0, lambda: loading_label.config(text="❌ Нет активной сессии. Сканируйте игрока для авторизации"))
                return
            parent.after(0, lambda t=templates: self._display_admin_templates(parent, t, dialog, loading_label))
        except Exception as e:
            parent.after(0, lambda m=str(e): loading_label.config(text=f"❌ Ошибка: {m[:80]}"))

    def _display_admin_templates(self, parent, templates, dialog, loading_label):
        loading_label.config(text="")
        if not templates:
            loading_label.config(text="Не удалось загрузить шаблоны с админки")
            return
        loading_label.config(text=f"✅ Загружено {len(templates)} шаблонов с админки")
        for t in templates:
            btn = ttk.Button(
                parent, text=f"📌 {t['title']}",
                command=lambda r=t["reason"]: self._apply_preset_reason(r, dialog)
            )
            btn.pack(fill="x", pady=2, before=loading_label)

    def _apply_preset_reason(self, reason, dialog):
        """Применить выбранный пресет"""
        self.ban_reason_var.set(reason)
        dialog.destroy()

    def _on_ban_execute(self):
        admin_username = self.username_var.get()
        admin_password = self.password_var.get()
        if not admin_username or not admin_password:
            messagebox.showerror("Ошибка", "Укажите имя и пароль администратора")
            return

        raw = self.ban_targets_text.get("1.0", tk.END).strip()
        if not raw:
            messagebox.showerror("Ошибка", "Введите цели для блокировки")
            return

        reason = self.ban_reason_var.get().strip()
        if not reason:
            messagebox.showerror("Ошибка", "Укажите причину блокировки")
            return

        try:
            minutes = int(self.ban_minutes_var.get())
        except ValueError:
            minutes = 0

        targets = [line.strip() for line in raw.split("\n") if line.strip()]
        if not targets:
            messagebox.showerror("Ошибка", "Нет целей для блокировки")
            return

        self.ban_result_text.config(state="normal")
        self.ban_result_text.delete("1.0", tk.END)
        self.ban_result_text.config(state="disabled")
        self.ban_execute_btn.config(state="disabled")

        threading.Thread(
            target=self._run_ban_worker,
            args=(admin_username, admin_password, targets, reason, minutes),
            daemon=True,
        ).start()

    def _run_ban_worker(self, admin_username, admin_password, targets, reason, minutes):
        import sys as _sys
        import traceback as _tb
        try:
            import asyncio as _asyncio

            loop = _asyncio.new_event_loop()
            _asyncio.set_event_loop(loop)
            result = loop.run_until_complete(
                self._ban_worker_async(admin_username, admin_password, targets, reason, minutes)
            )
            loop.close()
            self.output_queue.put(f"\n{'─'*50}\nБлокировка завершена: {result.get('ok', 0)} успешно, {result.get('fail', 0)} ошибок\n")
        except Exception as e:
            self.output_queue.put(f"\nОшибка выполнения блокировки: {e}\n{_tb.format_exc()}\n")
        finally:
            self.output_queue.put("__BAN_DONE__")

    async def _ban_worker_async(self, admin_username, admin_password, targets, reason, minutes):
        from admin_panel import AdminPanel
        import os, datetime
        panel = AdminPanel(admin_username, admin_password)
        panel._set_debug_callback(lambda msg: self.output_queue.put(msg + "\n"))
        ok_count = 0
        fail_count = 0
        error_html_dir = os.path.join(os.path.dirname(__file__) or ".", "ban_errors")

        self.output_queue.put("🔑 Выполняю вход в админ-панель...\n")
        logged_in = await panel.login()
        if not logged_in:
            self.output_queue.put("❌ Ошибка входа в админ-панель\n")
            return {"ok": 0, "fail": len(targets)}

        # Читаем значения чекбоксов
        use_latest_ip = self.use_latest_ip_var.get()
        use_latest_hwid = self.use_latest_hwid_var.get()

        self.output_queue.put(f"📋 Начинаю блокировку {len(targets)} целей...\n")
        self.output_queue.put(f"   Использовать последний IP: {'Да' if use_latest_ip else 'Нет'}\n")
        self.output_queue.put(f"   Использовать последний HWID: {'Да' if use_latest_hwid else 'Нет'}\n\n")

        for idx, target in enumerate(targets):
            detected_type = self._detect_target_type(target)
            log_line = f"[{idx + 1}/{len(targets)}] {target}  →  тип: {detected_type}  ...  "
            self.output_queue.put(log_line)

            try:
                kwargs = dict(reason=reason, minutes=minutes)
                if detected_type == "ip":
                    kwargs["ip_address"] = target
                elif detected_type == "hwid":
                    kwargs["hwid"] = target
                elif detected_type == "user_id":
                    kwargs["user_id"] = target
                else:
                    # Бан по нику/имени пользователя
                    kwargs["user_id"] = target
                    # Если включены чекбоксы, используем последние IP и HWID
                    if use_latest_ip:
                        kwargs["use_latest_ip"] = True
                    if use_latest_hwid:
                        kwargs["use_latest_hwid"] = True

                success = await panel.create_ban(**kwargs)
                if success:
                    ok_count += 1
                    self.output_queue.put("✅ УСПЕХ\n")
                else:
                    fail_count += 1
                    self.output_queue.put("❌ ОШИБКА\n")
            except Exception as e:
                fail_count += 1
                self.output_queue.put(f"❌ ИСКЛЮЧЕНИЕ: {e}\n")

        self.output_queue.put(f"\n{'─'*50}\n")
        self.output_queue.put(f"✅ Успешно: {ok_count}\n")
        self.output_queue.put(f"❌ Ошибок: {fail_count}\n")

        await panel.close()
        return {"ok": ok_count, "fail": fail_count}

    def _on_close(self):
        self.running = False
        self._cleanup_previous_bot()
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    app = BanCheckerGUI(root)
    root.mainloop()
