from datetime import datetime

from models.message import DiscordMessage, ScanResult
from models.player import Player


class TestDiscordMessage:
    def test_construction(self, sample_discord_message):
        msg = sample_discord_message
        assert msg.id == "987654321"
        assert msg.author_name == "AuthorUser"
        assert msg.author_id == "11111"
        assert msg.content == "Check this player: TestPlayer"
        assert msg.channel_id == "22222"
        assert msg.guild_id == "33333"
        assert msg.created_at == datetime(2024, 1, 1, 12, 0, 0)
        assert msg.embed_titles == ["Player Info"]
        assert len(msg.embed_links) == 1

    def test_link_property(self, sample_discord_message):
        msg = sample_discord_message
        expected = "https://discord.com/channels/33333/22222/987654321"
        assert msg.link == expected

    def test_default_lists(self):
        msg = DiscordMessage(
            id="1", author_name="A", author_id="2",
            content="hello", channel_id="3", guild_id="4",
            created_at=datetime(2024, 1, 1),
        )
        assert msg.embed_titles == []
        assert msg.embed_links == []


class TestScanResult:
    def test_construction(self, sample_scan_result):
        result = sample_scan_result
        assert result.message.id == "987654321"
        assert len(result.players) == 2
        assert result.players[0].user_id == "12345"
        assert result.players[1].user_id == "67890"
        assert result.players[1].status == "banned"

    def test_scan_time_default(self):
        msg = DiscordMessage(
            id="1", author_name="A", author_id="2",
            content="hello", channel_id="3", guild_id="4",
            created_at=datetime(2024, 1, 1),
        )
        result = ScanResult(message=msg, players=[])
        assert result.scan_time is not None
