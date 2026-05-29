import json
import logging
import os
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional

from models.complaint import ComplaintChannel, ComplaintMessage

DATABASE_FILENAME = "deadspace_checker.db"


class DatabaseService:
    _instance = None
    _lock = threading.Lock()

    def __new__(cls, db_path: Optional[str] = None, **kwargs):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self, db_path: Optional[str] = None, _test_mode: bool = False):
        if self._initialized:
            return
        self._initialized = True

        if db_path is None:
            from utils.path_utils import app_dir
            db_path = os.path.join(app_dir(), DATABASE_FILENAME)

        self.db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None
        self._conn_lock = threading.Lock()
        self.logger = logging.getLogger(__name__)

        self._init_db()
        if not _test_mode:
            self._migrate_from_json()
            self._migrate_from_pickle()
            self._migrate_from_gui_json()

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            os.makedirs(os.path.dirname(self.db_path) or '.', exist_ok=True)
            self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA synchronous=NORMAL")
        return self._conn

    def _execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._conn_lock:
            conn = self._get_conn()
            return conn.execute(sql, params)

    def _executemany(self, sql: str, params_list: list) -> sqlite3.Cursor:
        with self._conn_lock:
            conn = self._get_conn()
            return conn.executemany(sql, params_list)

    def _commit(self):
        with self._conn_lock:
            self._get_conn().commit()

    def _init_db(self):
        with self._conn_lock:
            conn = self._get_conn()
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS complaint_channels (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL DEFAULT '',
                    guild_id TEXT NOT NULL DEFAULT '',
                    last_cached_id TEXT,
                    created_at REAL NOT NULL DEFAULT (strftime('%s','now')),
                    updated_at REAL NOT NULL DEFAULT (strftime('%s','now'))
                );

                CREATE TABLE IF NOT EXISTS complaint_messages (
                    id TEXT PRIMARY KEY,
                    channel_id TEXT NOT NULL,
                    content TEXT NOT NULL DEFAULT '',
                    embeds TEXT NOT NULL DEFAULT '[]',
                    guild_id TEXT NOT NULL DEFAULT '',
                    cached_at REAL NOT NULL DEFAULT (strftime('%s','now')),
                    FOREIGN KEY (channel_id) REFERENCES complaint_channels(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_complaint_messages_channel
                    ON complaint_messages(channel_id);

                CREATE INDEX IF NOT EXISTS idx_complaint_messages_content
                    ON complaint_messages(content);

                CREATE INDEX IF NOT EXISTS idx_complaint_messages_id
                    ON complaint_messages(id);

                CREATE TABLE IF NOT EXISTS admin_cache (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    timestamp REAL NOT NULL,
                    created_at REAL NOT NULL DEFAULT (strftime('%s','now'))
                );

                CREATE INDEX IF NOT EXISTS idx_admin_cache_ts
                    ON admin_cache(timestamp);

                CREATE TABLE IF NOT EXISTS gui_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at REAL NOT NULL DEFAULT (strftime('%s','now'))
                );

                CREATE TABLE IF NOT EXISTS change_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_type TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    old_value TEXT,
                    new_value TEXT,
                    changed_at REAL NOT NULL DEFAULT (strftime('%s','now'))
                );

                CREATE INDEX IF NOT EXISTS idx_change_history_entity
                    ON change_history(entity_type, entity_id);
            """)
            conn.commit()

    def record_change(self, entity_type: str, entity_id: str, action: str,
                      old_value: Optional[str] = None, new_value: Optional[str] = None):
        self._execute(
            "INSERT INTO change_history (entity_type, entity_id, action, old_value, new_value) VALUES (?, ?, ?, ?, ?)",
            (entity_type, entity_id, action, old_value, new_value)
        )
        self._commit()

    def get_change_history(self, entity_type: Optional[str] = None,
                           entity_id: Optional[str] = None,
                           limit: int = 100) -> List[Dict[str, Any]]:
        if entity_type and entity_id:
            rows = self._execute(
                "SELECT * FROM change_history WHERE entity_type=? AND entity_id=? ORDER BY id DESC LIMIT ?",
                (entity_type, entity_id, limit)
            ).fetchall()
        elif entity_type:
            rows = self._execute(
                "SELECT * FROM change_history WHERE entity_type=? ORDER BY id DESC LIMIT ?",
                (entity_type, limit)
            ).fetchall()
        else:
            rows = self._execute(
                "SELECT * FROM change_history ORDER BY id DESC LIMIT ?",
                (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    # ── Complaint Cache ────────────────────────────────────────────────

    def load_complaint_channels(self) -> Dict[int, ComplaintChannel]:
        channels: Dict[int, ComplaintChannel] = {}
        try:
            ch_rows = self._execute(
                "SELECT id, name, guild_id, last_cached_id FROM complaint_channels ORDER BY name"
            ).fetchall()
            for ch_row in ch_rows:
                ch_id = ch_row["id"]
                msg_rows = self._execute(
                    "SELECT id, content, embeds, guild_id FROM complaint_messages WHERE channel_id=? ORDER BY id DESC",
                    (ch_id,)
                ).fetchall()
                messages = []
                for msg_row in msg_rows:
                    try:
                        embeds = json.loads(msg_row["embeds"]) if msg_row["embeds"] else []
                    except (json.JSONDecodeError, TypeError):
                        embeds = []
                    messages.append(ComplaintMessage(
                        id=msg_row["id"],
                        content=msg_row["content"] or "",
                        embeds=embeds,
                        channel_id=ch_id,
                        guild_id=msg_row["guild_id"] or ch_row["guild_id"],
                    ))
                channels[int(ch_id)] = ComplaintChannel(
                    id=ch_id,
                    name=ch_row["name"] or f"Channel {ch_id}",
                    guild_id=ch_row["guild_id"] or "",
                    messages=messages,
                    last_cached_id=ch_row["last_cached_id"],
                )
            self.logger.info(f"Loaded {len(channels)} complaint channel(s) from DB ({sum(len(ch.messages) for ch in channels.values())} messages)")
        except Exception as e:
            self.logger.error(f"Error loading complaint channels from DB: {e}", exc_info=True)
        return channels

    def save_complaint_cache(self, channels: Dict[int, ComplaintChannel]) -> bool:
        if not channels:
            self.logger.warning("No complaint channels to save.")
            return False
        try:
            with self._conn_lock:
                conn = self._get_conn()
                conn.execute("BEGIN IMMEDIATE")
                try:
                    total_messages = 0
                    change_records = []
                    for ch_id, channel in channels.items():
                        ch_str = str(ch_id)
                        conn.execute(
                            "INSERT INTO complaint_channels (id, name, guild_id, last_cached_id) VALUES (?, ?, ?, ?) "
                            "ON CONFLICT(id) DO UPDATE SET name=excluded.name, guild_id=excluded.guild_id, last_cached_id=excluded.last_cached_id, updated_at=strftime('%s','now')",
                            (ch_str, channel.name, channel.guild_id, channel.last_cached_id)
                        )

                        existing_ids = {
                            r["id"] for r in conn.execute(
                                "SELECT id FROM complaint_messages WHERE channel_id=?", (ch_str,)
                            ).fetchall()
                        }
                        new_ids = {m.id for m in channel.messages}
                        to_delete = existing_ids - new_ids
                        if to_delete:
                            placeholders = ",".join("?" for _ in to_delete)
                            conn.execute(f"DELETE FROM complaint_messages WHERE id IN ({placeholders})", tuple(to_delete))
                            for mid in to_delete:
                                change_records.append(("complaint_message", mid, "delete", mid, None))

                        new_messages = [m for m in channel.messages if m.id not in existing_ids]
                        if new_messages:
                            conn.executemany(
                                "INSERT INTO complaint_messages (id, channel_id, content, embeds, guild_id) VALUES (?, ?, ?, ?, ?)",
                                [(m.id, ch_str, m.content or "", json.dumps(m.embeds, ensure_ascii=False), channel.guild_id) for m in new_messages]
                            )
                        total_messages += len(channel.messages)

                    if change_records:
                        conn.executemany(
                            "INSERT INTO change_history (entity_type, entity_id, action, old_value, new_value) VALUES (?, ?, ?, ?, ?)",
                            change_records
                        )

                    conn.commit()
                    self.logger.info(
                        f"Saved {len(channels)} channel(s) with {total_messages} messages to DB"
                    )
                    return True
                except Exception:
                    conn.rollback()
                    raise
        except Exception as e:
            self.logger.error(f"Error saving complaint cache to DB: {e}", exc_info=True)
            return False

    def complaint_channel_count(self) -> int:
        row = self._execute("SELECT COUNT(*) as cnt FROM complaint_channels").fetchone()
        return row["cnt"] if row else 0

    def search_complaint_messages(self, search_text: str, limit: int = 100) -> List[Dict[str, Any]]:
        like_pattern = f"%{search_text}%"
        rows = self._execute(
            """SELECT cm.id, cm.content, cm.channel_id, cm.guild_id, cc.name as channel_name
               FROM complaint_messages cm
               LEFT JOIN complaint_channels cc ON cm.channel_id = cc.id
               WHERE cm.content LIKE ?
               ORDER BY cm.id DESC LIMIT ?""",
            (like_pattern, limit)
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Admin Cache ────────────────────────────────────────────────────

    def admin_cache_get(self, key: str, ttl: float = 86400) -> Optional[Any]:
        row = self._execute(
            "SELECT value, timestamp FROM admin_cache WHERE key=?", (key,)
        ).fetchone()
        if row is None:
            return None
        if time.time() - row["timestamp"] > ttl:
            self._execute("DELETE FROM admin_cache WHERE key=?", (key,))
            self._commit()
            return None
        try:
            return json.loads(row["value"])
        except (json.JSONDecodeError, TypeError):
            return None

    def admin_cache_put(self, key: str, value: Any):
        try:
            serialized = json.dumps(value, default=str, ensure_ascii=False)
            self._execute(
                "INSERT OR REPLACE INTO admin_cache (key, value, timestamp) VALUES (?, ?, ?)",
                (key, serialized, time.time())
            )
            self._commit()
        except Exception as e:
            self.logger.warning(f"Failed to cache admin data for key {key}: {e}")

    def admin_cache_delete(self, key: str):
        self._execute("DELETE FROM admin_cache WHERE key=?", (key,))
        self._commit()

    def admin_cache_clear(self):
        self._execute("DELETE FROM admin_cache")
        self._commit()

    def admin_cache_count(self) -> int:
        row = self._execute("SELECT COUNT(*) as cnt FROM admin_cache").fetchone()
        return row["cnt"] if row else 0

    def admin_cache_cleanup(self, max_age: float = 86400):
        cutoff = time.time() - max_age
        self._execute("DELETE FROM admin_cache WHERE timestamp < ?", (cutoff,))
        self._commit()

    # ── GUI Settings ───────────────────────────────────────────────────

    def gui_get_all(self) -> Dict[str, Any]:
        rows = self._execute("SELECT key, value FROM gui_settings").fetchall()
        settings = {}
        for row in rows:
            val = row["value"]
            if val.lower() == "true":
                settings[row["key"]] = True
            elif val.lower() == "false":
                settings[row["key"]] = False
            else:
                try:
                    if val.isdigit():
                        settings[row["key"]] = int(val)
                    else:
                        try:
                            settings[row["key"]] = float(val)
                            if "." not in val:
                                settings[row["key"]] = int(val)
                        except ValueError:
                            settings[row["key"]] = val
                except (ValueError, AttributeError):
                    settings[row["key"]] = val
        return settings

    def gui_set_all(self, settings: Dict[str, Any]):
        try:
            with self._conn_lock:
                conn = self._get_conn()
                conn.execute("BEGIN IMMEDIATE")
                try:
                    existing = {r["key"] for r in conn.execute("SELECT key FROM gui_settings").fetchall()}
                    for key, value in settings.items():
                        str_val = str(value)
                        if key in existing:
                            conn.execute(
                                "UPDATE gui_settings SET value=?, updated_at=strftime('%s','now') WHERE key=?",
                                (str_val, key)
                            )
                        else:
                            conn.execute(
                                "INSERT INTO gui_settings (key, value) VALUES (?, ?)",
                                (key, str_val)
                            )
                    to_remove = existing - set(settings.keys())
                    for key in to_remove:
                        conn.execute("DELETE FROM gui_settings WHERE key=?", (key,))
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
        except Exception as e:
            self.logger.warning(f"Failed to save GUI settings to DB: {e}")

    # ── Migration helpers ──────────────────────────────────────────────

    def _migrate_from_json(self):
        from utils.path_utils import app_dir
        json_path = os.path.join(app_dir(), "complaint_message_cache.json")
        if not os.path.exists(json_path):
            return
        existing_count = self._execute("SELECT COUNT(*) as cnt FROM complaint_channels").fetchone()["cnt"]
        if existing_count > 0:
            return
        self.logger.info("Migrating complaint_message_cache.json to SQLite...")
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                raw_data = json.load(f)
            channels: Dict[int, ComplaintChannel] = {}
            for ch_str_id, ch_data in raw_data.items():
                try:
                    ch_id = int(ch_str_id)
                    messages = []
                    for msg in ch_data.get("messages", []):
                        messages.append(ComplaintMessage(
                            id=msg["id"],
                            content=msg.get("content", ""),
                            embeds=msg.get("embeds", []),
                            channel_id=ch_str_id,
                            guild_id=ch_data.get("guild_id", "0"),
                        ))
                    channels[ch_id] = ComplaintChannel(
                        id=ch_str_id,
                        name=ch_data.get("name", f"Channel {ch_str_id}"),
                        guild_id=ch_data.get("guild_id", ""),
                        messages=messages,
                        last_cached_id=ch_data.get("last_cached_id"),
                    )
                except (ValueError, TypeError, KeyError) as e:
                    self.logger.warning(f"Skipping channel {ch_str_id} during migration: {e}")
            if channels:
                self.save_complaint_cache(channels)
                backup = json_path + ".bak"
                if not os.path.exists(backup):
                    os.rename(json_path, backup)
                    self.logger.info(f"Migrated JSON cache. Original renamed to {backup}")
        except Exception as e:
            self.logger.error(f"Failed to migrate JSON cache: {e}")

    def _migrate_from_pickle(self):
        pickle_path = os.path.join(os.getcwd(), "cache", "admin_service_cache.pkl")
        if not os.path.exists(pickle_path):
            return
        existing_count = self._execute("SELECT COUNT(*) as cnt FROM admin_cache").fetchone()["cnt"]
        if existing_count > 0:
            return
        self.logger.info("Migrating admin_service_cache.pkl to SQLite...")
        try:
            import pickle
            with open(pickle_path, "rb") as f:
                cache_data = pickle.load(f)
            if isinstance(cache_data, dict):
                for key, (value, timestamp) in cache_data.items():
                    try:
                        serialized = json.dumps(value, default=str, ensure_ascii=False)
                        self._execute(
                            "INSERT OR REPLACE INTO admin_cache (key, value, timestamp) VALUES (?, ?, ?)",
                            (key, serialized, timestamp)
                        )
                    except Exception as e:
                        self.logger.warning(f"Failed to migrate cache key {key}: {e}")
                self._commit()
                self.logger.info(f"Migrated {len(cache_data)} entries from pickle cache")
                backup = pickle_path + ".bak"
                if not os.path.exists(backup):
                    os.rename(pickle_path, backup)
                    self.logger.info(f"Original pickle renamed to {backup}")
        except Exception as e:
            self.logger.error(f"Failed to migrate pickle cache: {e}")

    def _migrate_from_gui_json(self):
        from utils.path_utils import app_dir
        json_path = os.path.join(app_dir(), "gui_settings.json")
        if not os.path.exists(json_path):
            return
        existing_count = self._execute("SELECT COUNT(*) as cnt FROM gui_settings").fetchone()["cnt"]
        if existing_count > 0:
            return
        self.logger.info("Migrating gui_settings.json to SQLite...")
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                settings = json.load(f)
            if isinstance(settings, dict):
                self.gui_set_all(settings)
                backup = json_path + ".bak"
                if not os.path.exists(backup):
                    os.rename(json_path, backup)
                    self.logger.info(f"Migrated GUI settings. Original renamed to {backup}")
        except Exception as e:
            self.logger.error(f"Failed to migrate GUI settings: {e}")

    def close(self):
        with self._conn_lock:
            if self._conn is not None:
                try:
                    self._conn.commit()
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None

    def vacuum(self):
        self._execute("VACUUM")
        self._commit()
