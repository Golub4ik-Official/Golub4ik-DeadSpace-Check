from models.player import Player


class TestPlayerConstruction:
    def test_minimal(self):
        p = Player(user_id="1", nicknames=["PlayerOne"])
        assert p.user_id == "1"
        assert p.nicknames == ["PlayerOne"]
        assert p.status == "unknown"
        assert p.ban_counts == 0
        assert p.ban_reasons == []
        assert p.connection_link == "N/A"
        assert p.associated_ips == {}
        assert p.associated_hwids == {}
        assert p.is_primary is False
        assert p.hwid_erased is False

    def test_full_construction(self, sample_player):
        assert sample_player.user_id == "12345"
        assert sample_player.nicknames == ["TestPlayer", "AltNick"]
        assert sample_player.status == "clean"
        assert sample_player.is_primary is True
        assert "192.168.1.1" in sample_player.associated_ips
        assert "hwid_abc123" in sample_player.associated_hwids


class TestPrimaryNickname:
    def test_explicit_primary(self):
        p = Player(user_id="1", nicknames=["Main", "Alt"], _primary_nickname="ExplicitMain")
        assert p.primary_nickname == "ExplicitMain"

    def test_no_nicknames_returns_unknown(self):
        p = Player(user_id="1", nicknames=[])
        assert p.primary_nickname == "Unknown"

    def test_is_primary_uses_first_nickname(self):
        p = Player(user_id="1", nicknames=["MainNick", "AltNick"], is_primary=True)
        assert p.primary_nickname == "MainNick"

    def test_not_primary_uses_first_nickname(self):
        p = Player(user_id="1", nicknames=["FirstNick", "SecondNick"], is_primary=False)
        assert p.primary_nickname == "FirstNick"

    def test_setter_overrides(self):
        p = Player(user_id="1", nicknames=["OldName"])
        p.primary_nickname = "ForcedName"
        assert p.primary_nickname == "ForcedName"

    def test_setter_none_clears_override(self):
        p = Player(user_id="1", nicknames=["Name"], _primary_nickname="Forced")
        p.primary_nickname = None
        assert p.primary_nickname == "Name"
