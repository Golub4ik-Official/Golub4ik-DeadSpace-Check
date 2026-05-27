import ast
import asyncio
import logging
import queue
import sys
import threading
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
import json
import os
import re

import discord

from admin_panel import AdminPanel
from bot import BanCheckerBot
from config_system import load_file, config as cfg
from utils.logging_utils import setup_logging

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(ROOT_DIR, "gui_settings.json")
CONFIG_FILE = os.path.join(ROOT_DIR, "config.py")

ANSI_RE = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]')


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
            self.out_queue.put(self.format(record) + "\n")
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


class BanCheckerGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("Golub4ik (WikiHampter) DeadSpace Checker")
        self.root.geometry("820x720")
        self.root.minsize(650, 550)

        self.settings = self._load_settings()
        self.bot = None
        self.bot_loop = None
        self.running = False
        self.output_queue = queue.Queue()

        self._build_ui()
        self._apply_settings()
        self._poll_output()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _load_settings(self):
        if os.path.exists(SETTINGS_FILE):
            try:
                with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def _save_settings(self):
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(self.settings, f, indent=2, ensure_ascii=False)

    def _apply_settings(self):
        self.username_var.set(self.settings.get("admin_username", ""))
        self.password_var.set(self.settings.get("admin_password", ""))
        self.token_var.set(self.settings.get("discord_token", ""))
        self.msg_count_var.set(str(self.settings.get("message_count", CFG_MESSAGE_LIMIT)))
        self.bypass_pages_var.set(str(self.settings.get("bypass_pages", CFG_BAN_BYPASS_PAGES)))
        self.nickname_var.set(self.settings.get("last_nickname", ""))

    def _build_ui(self):
        main = ttk.Frame(self.root, padding="12")
        main.grid(row=0, column=0, sticky="nsew")
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        main.columnconfigure(0, weight=1)
        main.rowconfigure(3, weight=1)

        cred = ttk.LabelFrame(main, text="Настройки доступа", padding="10")
        cred.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        cred.columnconfigure(1, weight=1)

        self.username_var = tk.StringVar()
        self.password_var = tk.StringVar()
        self.token_var = tk.StringVar()
        self.show_secrets = tk.BooleanVar(value=False)

        ttk.Label(cred, text="ADMIN_USERNAME:").grid(row=0, column=0, sticky="w", padx=(0, 6), pady=2)
        ttk.Entry(cred, textvariable=self.username_var).grid(row=0, column=1, sticky="ew", pady=2)

        ttk.Label(cred, text="ADMIN_PASSWORD:").grid(row=1, column=0, sticky="w", padx=(0, 6), pady=2)
        self.pw_entry = ttk.Entry(cred, textvariable=self.password_var, show="*")
        self.pw_entry.grid(row=1, column=1, sticky="ew", pady=2)

        ttk.Label(cred, text="DISCORD_TOKEN:").grid(row=2, column=0, sticky="w", padx=(0, 6), pady=2)
        token_frame = ttk.Frame(cred)
        token_frame.grid(row=2, column=1, sticky="ew", pady=2)
        token_frame.columnconfigure(0, weight=1)
        self.tk_entry = ttk.Entry(token_frame, textvariable=self.token_var, show="*")
        self.tk_entry.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        ttk.Button(token_frame, text="?", width=2, command=self._show_token_help).grid(row=0, column=1)

        btn_row = ttk.Frame(cred)
        btn_row.grid(row=3, column=1, sticky="e", pady=(6, 0))
        ttk.Checkbutton(btn_row, text="Показать", variable=self.show_secrets,
                        command=self._toggle_secrets).pack(side="left", padx=(0, 8))
        ttk.Button(btn_row, text="Сохранить настройки",
                   command=self._on_save).pack(side="left")

        mode = ttk.LabelFrame(main, text="Режим сканирования", padding="10")
        mode.grid(row=1, column=0, sticky="ew", pady=(0, 8))
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

        actions = ttk.Frame(main)
        actions.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        self.start_btn = ttk.Button(actions, text="▶ Запуск", command=self._on_start)
        self.start_btn.pack(side="left", padx=(0, 8))
        self.stop_btn = ttk.Button(actions, text="■ Остановить", command=self._on_stop, state="disabled")
        self.stop_btn.pack(side="left")
        self.config_btn = ttk.Button(actions, text="⚙️", width=3, command=self._open_config_dialog)
        self.config_btn.pack(side="left", padx=(8, 0))

        out = ttk.LabelFrame(main, text="Вывод", padding="4")
        out.grid(row=3, column=0, sticky="nsew")
        out.columnconfigure(0, weight=1)
        out.rowconfigure(0, weight=1)

        self.output_text = scrolledtext.ScrolledText(
            out, wrap="word", font=("Consolas", 9),
            bg="#1e1e1e", fg="#d4d4d4", insertbackground="white",
        )
        self.output_text.grid(row=0, column=0, sticky="nsew")
        self.output_text.bind("<Control-c>", self._copy_selection)
        self.output_text.bind("<Control-C>", self._copy_selection)

        self._on_mode_change()

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

    def _on_save(self):
        self.settings["admin_username"] = self.username_var.get()
        self.settings["admin_password"] = self.password_var.get()
        self.settings["discord_token"] = self.token_var.get()
        self.settings["last_nickname"] = self.nickname_var.get()
        try:
            self.settings["message_count"] = int(self.msg_count_var.get())
        except ValueError:
            pass
        try:
            self.settings["bypass_pages"] = int(self.bypass_pages_var.get())
        except ValueError:
            pass
        self._save_settings()
        messagebox.showinfo("", "Настройки сохранены")

    def _on_start(self):
        if not self.username_var.get() or not self.password_var.get():
            messagebox.showerror("Ошибка", "Укажите ADMIN_USERNAME и ADMIN_PASSWORD")
            return
        if not self.token_var.get():
            messagebox.showerror("Ошибка", "Укажите DISCORD_TOKEN")
            return

        mode = self.scan_mode.get()
        if mode == "username" and not self.nickname_var.get():
            messagebox.showerror("Ошибка", "Укажите имя игрока для пробива")
            return

        self.output_text.delete("1.0", tk.END)
        self._log("▶ Запуск сканирования...\n")

        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.running = True

        threading.Thread(target=self._run_bot, daemon=True).start()

    def _run_bot(self):
        original_stdout = sys.stdout

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

            cfg.auth.admin_username = self.username_var.get()
            cfg.auth.admin_password = self.password_var.get()
            cfg.discord.discord_user_token = self.token_var.get()

            mode = self.scan_mode.get()
            cfg.scan.username = self.nickname_var.get() if mode == "username" else None
            cfg.scan.check_ban_bypass = mode == "banbypass"
            cfg.scan.message_limit = int(self.msg_count_var.get())
            cfg.scan.ban_bypass_pages = int(self.bypass_pages_var.get())
            cfg.logging.log_level = "INFO"

            logging.info("Starting Ban Checker Bot")
            mode_desc = (
                f"Username: {cfg.scan.username}" if cfg.scan.username else
                "Ban Bypass Check" if cfg.scan.check_ban_bypass else
                f"Messages: {cfg.scan.message_limit}"
            )
            logging.info(f"Scan mode: {mode_desc}")

            admin_panel = AdminPanel(cfg.auth.admin_username, cfg.auth.admin_password)

            bot_config = {
                "TARGET_CHANNEL_ID": cfg.discord.target_channel_id,
                "COMPLAINT_CHANNEL_IDS": cfg.discord.complaint_channel_ids,
                "COMPLAINT_MESSAGE_HISTORY_LIMIT": cfg.discord.message_history_limit,
                "message_limit": cfg.scan.message_limit,
                "username": cfg.scan.username,
                "check_ban_bypass": cfg.scan.check_ban_bypass,
                "ban_bypass_pages": cfg.scan.ban_bypass_pages,
                "html_report_filename": cfg.report.html_report_filename,
                "message_interval_start": None,
                "message_interval_end": None,
            }

            bot = BanCheckerBot(cfg.discord.discord_user_token, admin_panel, bot_config)
            self.bot = bot
            self.bot_loop = bot.client.loop

            bot.run()

        except Exception as e:
            self.output_queue.put(f"\nОшибка: {e}\n")
            import traceback
            self.output_queue.put(traceback.format_exc() + "\n")
        finally:
            sys.stdout = original_stdout
            self.bot = None
            self.bot_loop = None
            self.output_queue.put(f"\n{'─'*50}\nПроцесс завершён\n")
            self.output_queue.put("__DONE__")

    def _poll_output(self):
        try:
            while True:
                line = self.output_queue.get_nowait()
                if line == "__DONE__":
                    self.running = False
                    self.start_btn.config(state="normal")
                    self.stop_btn.config(state="disabled")
                else:
                    self._log(ANSI_RE.sub("", line))
        except queue.Empty:
            pass
        self.root.after(100, self._poll_output)

    def _copy_selection(self, event=None):
        try:
            selected = self.output_text.selection_get()
            self.root.clipboard_clear()
            self.root.clipboard_append(selected)
        except tk.TclError:
            pass
        return "break"

    def _log(self, text):
        self.output_text.insert(tk.END, text)
        self.output_text.see(tk.END)

    def _on_stop(self):
        if self.bot and self.bot_loop and not self.bot_loop.is_closed():
            async def stop():
                await self.bot.close()
            asyncio.run_coroutine_threadsafe(stop(), self.bot_loop)
            self._log("\n⏹ Остановка...\n")
        else:
            self._log("\n⏹ Остановлено\n")
            self.running = False
            self.start_btn.config(state="normal")
            self.stop_btn.config(state="disabled")

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

    def _on_close(self):
        self.running = False
        if self.bot and self.bot_loop and not self.bot_loop.is_closed():
            async def stop():
                await self.bot.close()
            asyncio.run_coroutine_threadsafe(stop(), self.bot_loop)
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    app = BanCheckerGUI(root)
    root.mainloop()
