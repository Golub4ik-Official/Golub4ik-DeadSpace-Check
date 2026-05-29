import json
import os
import tempfile
import time
import threading

import pytest

from models.complaint import ComplaintChannel, ComplaintMessage
from services.database_service import DatabaseService


@pytest.fixture
def db():
    old_instance = DatabaseService._instance
    old_lock = DatabaseService._lock

    DatabaseService._instance = None
    DatabaseService._lock = threading.Lock()

    tmp = tempfile.mktemp(suffix=".db")
    instance = DatabaseService(tmp, _test_mode=True)
    yield instance

    instance.close()
    try:
        os.unlink(tmp)
    except PermissionError:
        pass
    DatabaseService._instance = old_instance
    DatabaseService._lock = old_lock


class TestDatabaseServiceInit:
    def test_singleton(self):
        old = DatabaseService._instance
        DatabaseService._instance = None
        DatabaseService._lock = threading.Lock()

        tmp = tempfile.mktemp(suffix=".db")
        a = None
        try:
            a = DatabaseService(tmp, _test_mode=True)
            b = DatabaseService()
            assert a is b
            assert a.db_path == tmp
        finally:
            if a:
                a.close()
            try:
                os.unlink(tmp)
            except PermissionError:
                pass
            DatabaseService._instance = old

    def test_tables_created(self, db):
        tables = {
            r["name"] for r in db._execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "complaint_channels" in tables
        assert "complaint_messages" in tables
        assert "admin_cache" in tables
        assert "gui_settings" in tables
        assert "change_history" in tables

    def test_indexes_created(self, db):
        indexes = {
            r["name"] for r in db._execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
        assert "idx_complaint_messages_channel" in indexes
        assert "idx_complaint_messages_content" in indexes
        assert "idx_complaint_messages_id" in indexes
        assert "idx_admin_cache_ts" in indexes
        assert "idx_change_history_entity" in indexes


class TestDatabaseServiceGUI:
    def test_set_and_get(self, db):
        db.gui_set_all({"key1": "value1", "key2": 42, "key3": True})
        settings = db.gui_get_all()
        assert settings["key1"] == "value1"
        assert settings["key2"] == 42
        assert settings["key3"] is True

    def test_overwrite_existing(self, db):
        db.gui_set_all({"theme": "dark", "volume": 80})
        db.gui_set_all({"theme": "light", "volume": 80})
        settings = db.gui_get_all()
        assert settings["theme"] == "light"
        assert settings["volume"] == 80

    def test_remove_stale_keys(self, db):
        db.gui_set_all({"a": "1", "b": "2", "c": "3"})
        db.gui_set_all({"a": "1", "b": "2"})
        settings = db.gui_get_all()
        assert "c" not in settings
        assert len(settings) == 2

    def test_empty_settings(self, db):
        settings = db.gui_get_all()
        assert settings == {}

    def test_boolean_conversion(self, db):
        db.gui_set_all({"flag": True, "no_flag": False})
        settings = db.gui_get_all()
        assert settings["flag"] is True
        assert settings["no_flag"] is False

    def test_numeric_conversion(self, db):
        db.gui_set_all({"int_val": "42", "float_val": "3.14"})
        settings = db.gui_get_all()
        assert settings["int_val"] == 42
        assert settings["float_val"] == 3.14


class TestDatabaseServiceAdminCache:
    def test_put_and_get(self, db):
        data = {"nicknames": ["foo", "bar"], "ips": ["1.2.3.4"]}
        db.admin_cache_put("connections:test", data)
        result = db.admin_cache_get("connections:test")
        assert result == data

    def test_get_missing_key(self, db):
        result = db.admin_cache_get("nonexistent")
        assert result is None

    def test_get_expired_key(self, db):
        db.admin_cache_put("temp", "data")
        result = db.admin_cache_get("temp", ttl=0)
        assert result is None

    def test_delete(self, db):
        db.admin_cache_put("to_delete", "value")
        db.admin_cache_delete("to_delete")
        assert db.admin_cache_get("to_delete") is None

    def test_clear(self, db):
        db.admin_cache_put("a", 1)
        db.admin_cache_put("b", 2)
        db.admin_cache_clear()
        assert db.admin_cache_get("a") is None
        assert db.admin_cache_get("b") is None
        assert db.admin_cache_count() == 0

    def test_count(self, db):
        assert db.admin_cache_count() == 0
        db.admin_cache_put("x", 1)
        assert db.admin_cache_count() == 1
        db.admin_cache_put("y", 2)
        assert db.admin_cache_count() == 2

    def test_cleanup_removes_old_entries(self, db):
        db.admin_cache_put("fresh", "data")
        ts = time.time() - 100000
        db._execute(
            "UPDATE admin_cache SET timestamp=? WHERE key=?",
            (ts, "fresh")
        )
        db._commit()
        db.admin_cache_cleanup(max_age=86400)
        assert db.admin_cache_get("fresh") is None

    def test_complex_data_types(self, db):
        data = {
            "list": [1, 2, 3],
            "dict": {"a": 1},
            "nested": {"inner": [{"x": "y"}]},
            "none": None,
            "bool": False,
        }
        db.admin_cache_put("complex", data)
        result = db.admin_cache_get("complex")
        assert result == data

    def test_overwrite_existing_cache_key(self, db):
        db.admin_cache_put("key", "old")
        db.admin_cache_put("key", "new")
        assert db.admin_cache_get("key") == "new"


class TestDatabaseServiceComplaintCache:
    def test_save_and_load_single_channel(self, db):
        channels = {
            1: ComplaintChannel(
                id="1", name="test-channel", guild_id="100",
                messages=[
                    ComplaintMessage(id="101", content="hello", embeds=[],
                                     channel_id="1", guild_id="100"),
                ],
                last_cached_id="101",
            )
        }
        assert db.save_complaint_cache(channels) is True
        loaded = db.load_complaint_channels()
        assert 1 in loaded
        assert loaded[1].name == "test-channel"
        assert loaded[1].last_cached_id == "101"
        assert len(loaded[1].messages) == 1
        assert loaded[1].messages[0].content == "hello"

    def test_save_and_load_multiple_channels(self, db):
        channels = {
            1: ComplaintChannel(id="1", name="ch1", guild_id="100", messages=[], last_cached_id=None),
            2: ComplaintChannel(id="2", name="ch2", guild_id="200", messages=[], last_cached_id=None),
        }
        db.save_complaint_cache(channels)
        loaded = db.load_complaint_channels()
        assert len(loaded) == 2

    def test_save_empty_cache_returns_false(self, db):
        assert db.save_complaint_cache({}) is False

    def test_channel_count(self, db):
        assert db.complaint_channel_count() == 0
        channels = {
            1: ComplaintChannel(id="1", name="ch", guild_id="100", messages=[], last_cached_id=None),
        }
        db.save_complaint_cache(channels)
        assert db.complaint_channel_count() == 1

    def test_messages_preserve_embeds(self, db):
        embeds = [{"title": "Test", "fields": [{"name": "n", "value": "v"}]}]
        channels = {
            1: ComplaintChannel(
                id="1", name="ch", guild_id="100",
                messages=[
                    ComplaintMessage(id="m1", content="text", embeds=embeds,
                                     channel_id="1", guild_id="100"),
                ],
                last_cached_id="m1",
            )
        }
        db.save_complaint_cache(channels)
        loaded = db.load_complaint_channels()
        assert loaded[1].messages[0].embeds == embeds

    def test_update_channel_messages(self, db):
        channels = {
            1: ComplaintChannel(
                id="1", name="ch", guild_id="100",
                messages=[
                    ComplaintMessage(id="m1", content="old", embeds=[],
                                     channel_id="1", guild_id="100"),
                ],
                last_cached_id="m1",
            )
        }
        db.save_complaint_cache(channels)

        channels[1].messages.append(
            ComplaintMessage(id="m2", content="new", embeds=[],
                             channel_id="1", guild_id="100")
        )
        channels[1].last_cached_id = "m2"
        db.save_complaint_cache(channels)

        loaded = db.load_complaint_channels()
        assert len(loaded[1].messages) == 2
        # messages sorted by id DESC
        assert loaded[1].messages[0].id == "m2"
        assert loaded[1].messages[1].id == "m1"
        assert loaded[1].last_cached_id == "m2"

    def test_message_search(self, db):
        channels = {
            1: ComplaintChannel(
                id="1", name="ch", guild_id="100",
                messages=[
                    ComplaintMessage(id="m1", content="player1 is toxic", embeds=[],
                                     channel_id="1", guild_id="100"),
                    ComplaintMessage(id="m2", content="player2 is fine", embeds=[],
                                     channel_id="1", guild_id="100"),
                ],
                last_cached_id="m2",
            )
        }
        db.save_complaint_cache(channels)
        results = db.search_complaint_messages("toxic")
        assert len(results) == 1
        assert results[0]["content"] == "player1 is toxic"

        results = db.search_complaint_messages("player")
        assert len(results) == 2

    def test_load_from_empty_db(self, db):
        channels = db.load_complaint_channels()
        assert channels == {}


class TestDatabaseServiceChangeHistory:
    def test_record_and_query(self, db):
        db.record_change("player", "123", "insert", new_value='{"name": "foo"}')
        db.record_change("player", "123", "update", old_value='{"name": "foo"}', new_value='{"name": "bar"}')
        db.record_change("player", "456", "insert", new_value='{"name": "baz"}')

        history = db.get_change_history()
        assert len(history) == 3

        player_history = db.get_change_history("player", "123")
        assert len(player_history) == 2
        assert player_history[0]["action"] == "update"

        player_only = db.get_change_history(entity_type="player")
        assert len(player_only) == 3

    def test_record_delete(self, db):
        db.record_change("complaint_message", "m1", "delete", old_value="m1")
        history = db.get_change_history("complaint_message", "m1")
        assert len(history) == 1
        assert history[0]["action"] == "delete"
        assert history[0]["old_value"] == "m1"

    def test_empty_history(self, db):
        history = db.get_change_history()
        assert history == []

    def test_limit(self, db):
        for i in range(10):
            db.record_change("test", str(i), "insert", new_value=str(i))
        history = db.get_change_history(limit=3)
        assert len(history) == 3


class TestDatabaseServiceMigration:
    def test_migrate_from_json(self, db):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f:
            json_path = f.name
            json.dump({
                "1": {
                    "name": "migrated-channel",
                    "guild_id": "100",
                    "messages": [
                        {"id": "m1", "content": "migrated msg", "embeds": []}
                    ],
                    "last_cached_id": "m1",
                }
            }, f)

        from services.database_service import DATABASE_FILENAME
        old_json = os.path.join(os.getcwd(), "complaint_message_cache.json")
        if os.path.exists(old_json):
            backup_name = old_json + ".test_backup"
            os.rename(old_json, backup_name)
            restored_backup = True
        else:
            restored_backup = False

        try:
            import services.database_service as db_mod
            original = db_mod.DatabaseService._instance
            db_mod.DatabaseService._instance = None
            db_mod.DatabaseService._lock = threading.Lock()

            real_json = os.path.join(os.getcwd(), "complaint_message_cache.json")
            with open(json_path, "r") as src:
                with open(real_json, "w", encoding="utf-8") as dst:
                    dst.write(src.read())

            tmp = tempfile.mktemp(suffix=".db")
            try:
                migrated = db_mod.DatabaseService(tmp, _test_mode=False)
                assert migrated.complaint_channel_count() > 0
                loaded = migrated.load_complaint_channels()
                ch = loaded.get(1)
                assert ch is not None
                assert ch.name == "migrated-channel"
                assert len(ch.messages) == 1
                assert ch.messages[0].content == "migrated msg"
                migrated.close()

                assert os.path.exists(real_json + ".bak")
            finally:
                db_mod.DatabaseService._instance = original
                db_mod.DatabaseService._lock = threading.Lock()
                try:
                    os.unlink(tmp)
                except PermissionError:
                    pass
                if os.path.exists(real_json):
                    os.unlink(real_json)
                if os.path.exists(real_json + ".bak"):
                    os.unlink(real_json + ".bak")
        finally:
            os.unlink(json_path)
            if restored_backup:
                os.rename(backup_name, old_json)

    def test_skips_reinit_when_already_initialized(self):
        old = DatabaseService._instance
        DatabaseService._instance = None
        DatabaseService._lock = threading.Lock()

        tmp = tempfile.mktemp(suffix=".db")
        try:
            first = DatabaseService(tmp, _test_mode=True)
            first.gui_set_all({"existing": "data"})

            second = DatabaseService()
            assert second is first
            assert second.gui_get_all() == {"existing": "data"}
        finally:
            first.close()
            try:
                os.unlink(tmp)
            except PermissionError:
                pass
            DatabaseService._instance = old


class TestDatabaseServiceClose:
    def test_close_and_reopen(self, db):
        db.gui_set_all({"key": "val"})
        db.close()

        old = DatabaseService._instance
        DatabaseService._instance = None
        DatabaseService._lock = threading.Lock()

        tmp2 = tempfile.mktemp(suffix=".db")
        try:
            reopened = DatabaseService(tmp2, _test_mode=True)
            settings = reopened.gui_get_all()
            assert settings == {}
            reopened.close()
        finally:
            DatabaseService._instance = old
            DatabaseService._lock = threading.Lock()
            try:
                os.unlink(tmp2)
            except PermissionError:
                pass

    def test_vacuum(self, db):
        db.admin_cache_put("k", "v")
        db.vacuum()
        assert db.admin_cache_get("k") == "v"
